from dataclasses import dataclass
from datetime import timedelta
import hashlib, secrets
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session, object_session
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError
from .db import get_db, now, aware, new_id
from .models import User, SessionToken, Entity
from .settings import get_settings, read_secret
from .common import (
    audit,
    commit,
    require_role,
    fingerprint,
    can_delegate_scope,
    employee_is_enabled,
)

router = APIRouter()
ph = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
DUMMY_PASSWORD_HASH = ph.hash(secrets.token_urlsafe(32))
ROLES = {
    "PLATFORM_ADMIN",
    "HR_ADMIN",
    "HR_SPECIALIST",
    "MANAGER",
    "HEAD_TALENT",
    "EMPLOYEE",
}
SETUP_USER_ID = "0f8ad066-f639-5d41-9f99-1bdd253bd2da"
SETUP_MARKER_ID = "a08ff387-a0d8-5f8e-93eb-54eedbc12869"


def is_hidden_setup(principal):
    return bool(principal and principal.id == SETUP_USER_ID)


def account_is_deleted(db, principal):
    return bool(principal and not principal.enabled and db.scalar(select(Entity.id).where(
        Entity.kind == "account_lifecycle", Entity.parent_id == principal.id, Entity.status == "DELETED")))


def setup_change_required(db, principal):
    if not is_hidden_setup(principal):
        return False
    marker = db.scalar(select(Entity).where(Entity.id == SETUP_MARKER_ID)
                       .execution_options(populate_existing=True))
    return not marker or marker.data.get("credentials_change_required", True) is not False


@dataclass
class Actor:
    id: str
    name: str
    role: str
    scope: dict
    employee_id: str | None = None
    email: str = ""
    credentials_change_required: bool = False
    hidden_setup: bool = False


def hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def user_actor(user):
    db = object_session(user) if isinstance(user, User) else None
    return Actor(
        user.id,
        user.name,
        user.role,
        dict(user.scope or {}),
        user.employee_id,
        user.email,
        setup_change_required(db, user) if db else is_hidden_setup(user),
        is_hidden_setup(user),
    )


def actor_dict(actor):
    return {
        "id": actor.id,
        "user_id": actor.id,
        "name": actor.name,
        "email": actor.email,
        "role": actor.role,
        "scope": actor.scope,
        "employee_id": actor.employee_id,
        "login": actor.email,
        "credentials_change_required": actor.credentials_change_required,
        "hidden_setup": actor.hidden_setup,
    }


def employee_account_enabled(db, principal):
    """Employee OFF revokes self/manager sessions; HR can still administer."""
    if principal.role not in {"EMPLOYEE", "MANAGER"}:
        return True
    if not principal.employee_id:
        return principal.role != "EMPLOYEE"
    employee = db.scalar(
        select(Entity)
        .where(Entity.id == principal.employee_id, Entity.kind == "employee")
        .execution_options(populate_existing=True)
    )
    return employee_is_enabled(employee)


def session_user(request, db):
    raw = request.cookies.get("korsik_session", "")
    if not raw:
        return None, None
    s = db.get(SessionToken, hash_token(raw))
    if not s or aware(s.expires_at) <= now():
        return None, None
    user = db.scalar(
        select(User).where(User.id == s.user_id).execution_options(populate_existing=True)
    )
    if not user or not user.enabled or not employee_account_enabled(db, user):
        return None, None
    from .max_miniapp import miniapp_session_is_current

    if not miniapp_session_is_current(db, s, user):
        return None, None
    return user, s


def get_actor(request: Request, db: Session = Depends(get_db)):
    if getattr(request.state, "actor", None):
        actor = request.state.actor
        if not employee_account_enabled(db, actor):
            raise HTTPException(
                401,
                detail={"code": "authentication_required", "message": "Войдите в кабинет"},
            )
        enforce_setup_access(request, db, actor)
        return actor
    user, _ = session_user(request, db)
    if not user:
        raise HTTPException(
            401,
            detail={"code": "authentication_required", "message": "Войдите в кабинет"},
        )
    actor = user_actor(user)
    enforce_setup_access(request, db, actor)
    return actor


def enforce_setup_access(request, db, actor):
    if setup_change_required(db, actor) and request.url.path not in {
        "/auth/session", "/auth/credentials", "/auth/logout",
        "/api/v1/auth/session", "/api/v1/auth/credentials", "/api/v1/auth/logout",
    }:
        raise HTTPException(403, detail={"code": "credentials_change_required",
                                        "message": "Сначала измените начальный логин и пароль"})


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=256)


@router.post("/auth/login")
def login(
    body: Login, request: Request, response: Response, db: Session = Depends(get_db)
):
    email = body.email.strip().lower()
    # Persist failures by IP+account, but never store the password or raw address.
    bucket = fingerprint(
        {
            "ip": request.client.host if request.client else "",
            "email": email,
            "window": int(now().timestamp() // 300),
        }
    )[:36]
    rate = db.scalar(
        select(Entity).where(Entity.kind == "login_rate", Entity.parent_id == bucket)
    )
    if rate and int(rate.data.get("failures", 0)) >= 8:
        raise HTTPException(
            429,
            detail={
                "code": "login_rate_limited",
                "message": "Слишком много попыток. Повторите через пять минут",
            },
        )
    user = db.scalar(select(User).where(User.email == email))
    valid = False
    try:
        # Comparable Argon2 work even when account is absent.
        target = user.password_hash if user else DUMMY_PASSWORD_HASH
        valid = ph.verify(target, body.password)
    except (VerifyMismatchError, InvalidHashError):
        pass
    if (
        not user or not user.enabled or not valid
        or not employee_account_enabled(db, user)
    ):
        if not rate:
            rate = Entity(
                id=new_id(),
                kind="login_rate",
                parent_id=bucket,
                data={"failures": 0},
                status="ACTIVE",
            )
            db.add(rate)
        rate.data = {**rate.data, "failures": int(rate.data.get("failures", 0)) + 1}
        commit(db)
        raise HTTPException(
            401,
            detail={
                "code": "invalid_credentials",
                "message": "Неверный логин или пароль",
            },
        )
    raw = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    db.add(
        SessionToken(
            id=hash_token(raw),
            user_id=user.id,
            csrf_hash=hash_token(csrf),
            expires_at=now() + timedelta(seconds=get_settings().session_ttl_seconds),
        )
    )
    audit(db, user_actor(user), "auth.login", user.id)
    commit(db)
    flags = {
        "secure": get_settings().secure_cookies,
        "samesite": "lax",
        "path": "/",
        "max_age": get_settings().session_ttl_seconds,
    }
    response.set_cookie("korsik_session", raw, httponly=True, **flags)
    response.set_cookie("korsik_csrf", csrf, httponly=False, **flags)
    actor = actor_dict(user_actor(user))
    return {"user": actor, "actor": actor, "csrf_token": csrf}


@router.get("/auth/session")
def session(request: Request, actor: Actor = Depends(get_actor)):
    a = actor_dict(actor)
    return {"user": a, "actor": a, "csrf_token": request.cookies.get("korsik_csrf", "")}


@router.post("/auth/logout")
def logout(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_actor),
):
    _, s = session_user(request, db)
    if s:
        db.delete(s)
    audit(db, actor, "auth.logout", actor.id)
    commit(db)
    response.delete_cookie("korsik_session", path="/")
    response.delete_cookie("korsik_csrf", path="/")
    return {"ok": True}


class UserInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=255)
    name: str = Field(min_length=1, max_length=255)
    role: str
    password: str = Field(min_length=12, max_length=256)
    scope: dict = Field(default_factory=dict)
    employee_id: str | None = None


@router.get("/users")
def users(actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    require_role(actor, "HR_ADMIN", "HR_SPECIALIST", "PLATFORM_ADMIN")
    from .auth_admin import accessible
    from .common import actor_can_employee, can_delegate_scope

    result = []
    for u in db.scalars(select(User).order_by(User.name)).all():
        if is_hidden_setup(u):
            continue
        permitted = accessible(db, actor, u)
        if actor.role in {"HR_SPECIALIST", "HEAD_TALENT"} and u.id != actor.id:
            permitted = (
                actor_can_employee(db, actor, u.employee_id)
                if u.role == "EMPLOYEE" and u.employee_id
                else u.role in {"HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT", "MANAGER"}
                and bool(u.scope)
                and can_delegate_scope(actor.scope or {}, u.scope or {})
            )
        if not permitted:
            continue
        item = {**actor_dict(user_actor(u)), "enabled": u.enabled}
        if actor.role in {"HR_SPECIALIST", "HEAD_TALENT"} and u.id != actor.id:
            item.pop("email", None)
        result.append(item)
    return {"items": result}


@router.post("/users", status_code=201)
def create_user(
    body: UserInput, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)
):
    require_role(actor, "HR_ADMIN", "PLATFORM_ADMIN")
    if body.role not in ROLES:
        raise HTTPException(422, detail="Неизвестная роль")
    if actor.role == "PLATFORM_ADMIN" and body.role != "PLATFORM_ADMIN":
        raise HTTPException(403, detail="Нет права делегировать доступ HR")
    if not can_delegate_scope(actor.scope, body.scope):
        raise HTTPException(403, detail="Нельзя расширить область полномочий")
    if not actor.scope.get("all"):
        if body.role in {"HR_ADMIN", "PLATFORM_ADMIN"}:
            raise HTTPException(403, detail="Нельзя повысить полномочия")
    email = body.email.strip().lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, detail="Учётная запись уже существует")
    employee = None
    if body.role == "EMPLOYEE" and not body.employee_id:
        raise HTTPException(422, detail="Для сотрудника выберите кадровую карточку")
    if body.employee_id:
        from .common import assert_employee

        employee = assert_employee(db, actor, body.employee_id)
        employee = db.scalar(
            select(Entity)
            .where(Entity.id == employee.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        employee = assert_employee(db, actor, employee.id)
        previous = db.scalar(
            select(User).where(User.employee_id == employee.id, User.enabled == True)
        )
        if previous:
            raise HTTPException(
                409, detail="Карточка уже связана с действующей учётной записью"
            )
    u = User(
        id=new_id(),
        email=email,
        name=body.name,
        role=body.role,
        password_hash=ph.hash(body.password),
        scope=body.scope,
        employee_id=body.employee_id,
    )
    db.add(u)
    if employee is not None:
        employee.data = {**employee.data, "user_id": u.id}
        for conversation in db.scalars(
            select(Entity).where(
                Entity.kind == "conversation", Entity.parent_id == employee.id
            )
        ):
            conversation.owner_id = u.id
    audit(db, actor, "user.create", u.id, {"role": u.role})
    commit(db)
    return {**actor_dict(user_actor(u)), "enabled": True}


@router.get("/audit-events")
def audit_events(
    actor: Actor = Depends(get_actor), db: Session = Depends(get_db), limit: int = 100
):
    from .models import Audit, Entity

    require_role(actor, "HR_ADMIN", "PLATFORM_ADMIN")
    if not actor.scope.get("all"):
        raise HTTPException(403, detail="Нет разрешения на общий журнал")
    records = db.scalars(
        select(Audit).order_by(Audit.created_at.desc()).limit(max(1, min(limit, 500)))
    ).all()
    return {
        "items": [
            {
                "id": a.id,
                "actor_id": a.actor_id,
                "action": a.action,
                "entity_id": a.entity_id,
                # Business review reasons may quote private source content.
                # Technical operators receive opaque event metadata only.
                "data": a.data if actor.role == "HR_ADMIN" else {},
                "reason": (
                    private.data.get("reason")
                    if actor.role == "HR_ADMIN"
                    and a.data.get("reason_ref")
                    and (private := db.get(Entity, a.data["reason_ref"]))
                    and private.kind == "audit_reason"
                    and private.parent_id == a.id
                    else None
                ),
                "created_at": a.created_at.isoformat(),
            }
            for a in records
        ]
    }


def bootstrap_admin(db):
    """Initialize only an empty installation; retries never reset any identity."""
    if db.bind.dialect.name == "postgresql":
        from sqlalchemy import text
        db.execute(text("SELECT pg_advisory_xact_lock(76810229)"))
    if db.scalar(select(Entity.id).limit(1)) or db.scalar(select(User.id).limit(1)):
        return
    db.add(
        User(
            id=SETUP_USER_ID,
            email="admin",
            name="Первичная настройка",
            role="HEAD_TALENT",
            scope={"all": True},
            password_hash=ph.hash("admin"),
            enabled=True,
        )
    )
    db.add(Entity(id=SETUP_MARKER_ID, kind="setup_account", owner_id=SETUP_USER_ID,
                  status="ACTIVE", data={"credentials_change_required": True}))
    audit(db, None, "auth.bootstrap.initialize", SETUP_USER_ID)
    commit(db)
