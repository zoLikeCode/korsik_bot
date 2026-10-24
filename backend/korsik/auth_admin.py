"""Offline account administration and one-use password recovery; no external mail."""

import secrets
import re
from datetime import timedelta, datetime
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, delete, func, text
from argon2.exceptions import VerifyMismatchError, InvalidHashError
from .db import get_db, now
from .models import Entity, User, SessionToken
from .auth import (get_actor, user_actor, actor_dict, hash_token, ph, ROLES,
                   is_hidden_setup, account_is_deleted, setup_change_required, SETUP_MARKER_ID, session_user)
from .common import (
    require_role,
    actor_can_employee,
    fingerprint,
    audit,
    commit,
    save_entity,
    list_kind,
    can_delegate_scope,
    get_entity,
)

router = APIRouter()


def user_etag(user):
    return (
        '"user:'
        + user.id
        + ":"
        + fingerprint(
            {
                "enabled": user.enabled,
                "role": user.role,
                "scope": user.scope,
                "name": user.name,
                "login": user.email,
                "employee_id": user.employee_id,
                "password": user.password_hash,
            }
        )[:16]
        + '"'
    )


def accessible(db, actor, user):
    if is_hidden_setup(user) or account_is_deleted(db, user):
        return False
    if actor.id == user.id:
        return True
    if actor.role not in {"HR_ADMIN", "PLATFORM_ADMIN"}:
        return False
    if actor.role == "PLATFORM_ADMIN" and user.role != "PLATFORM_ADMIN":
        return False
    if actor.scope.get("all"):
        return True
    if user.role == "PLATFORM_ADMIN":
        # Technical administration is deployment-wide even if an old account
        # happens to carry site labels. A local HR grant cannot reset its login.
        return False
    if user.scope.get("all"):
        return False
    if user.role == "EMPLOYEE" and user.employee_id:
        # An employee account inherits the actual card's complete site/legal scope.
        # A linked card never grants access to an administrative account.
        return actor_can_employee(db, actor, user.employee_id)
    if user.role == "MANAGER" and not user.scope:
        # Team membership is derived from cards, not a complete user grant.
        return False
    return can_delegate_scope(actor.scope, user.scope or {})


class OwnCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str = Field(min_length=1, max_length=256)
    login: str = Field(min_length=3, max_length=255)
    new_password: str = Field(min_length=12, max_length=256)


@router.get("/auth/credentials")
def own_credentials(db=Depends(get_db), actor=Depends(get_actor)):
    user = db.scalar(select(User).where(User.id == actor.id).execution_options(populate_existing=True))
    if not user or not user.enabled:
        raise HTTPException(401, "Учётная запись недоступна")
    return {"login": user.email, "credentials_change_required": setup_change_required(db, user)}


@router.patch("/auth/credentials")
def change_own_credentials(body: OwnCredentials, request: Request, db=Depends(get_db), actor=Depends(get_actor)):
    if db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(76810229)"))
    user = db.scalar(select(User).where(User.id == actor.id).with_for_update().execution_options(populate_existing=True))
    if not user or not user.enabled:
        raise HTTPException(401, "Учётная запись недоступна")
    try:
        valid = ph.verify(user.password_hash, body.current_password)
    except (VerifyMismatchError, InvalidHashError):
        valid = False
    if not valid:
        raise HTTPException(403, "Текущий пароль неверен")
    login = body.login.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.@+\-]{2,254}", login):
        raise HTTPException(422, "Логин: от 3 до 255 латинских букв, цифр или символов . _ @ + -")
    if setup_change_required(db, user) and (login == "admin" or body.new_password == "admin"):
        raise HTTPException(422, "Измените начальный логин и пароль")
    if db.scalar(select(User.id).where(func.lower(User.email) == login, User.id != user.id)):
        raise HTTPException(409, "Этот логин уже используется")
    _, current = session_user(request, db)
    if not current or current.user_id != user.id:
        raise HTTPException(401, "Войдите снова перед сменой реквизитов")
    user.email = login
    user.password_hash = ph.hash(body.new_password)
    marker = db.get(Entity, SETUP_MARKER_ID) if is_hidden_setup(user) else None
    if marker:
        marker.data = {**marker.data, "credentials_change_required": False, "changed_at": now().isoformat()}
    db.execute(delete(SessionToken).where(SessionToken.user_id == user.id, SessionToken.id != current.id))
    for reset in list_kind(db, "password_reset", parent_id=user.id):
        if reset.status == "ISSUED":
            reset.status = "REVOKED"
    audit(db, user_actor(user), "auth.credentials.change", user.id)
    commit(db)
    return {"status": "CREDENTIALS_CHANGED", "actor": actor_dict(user_actor(user))}


def target(db, actor, id, lock=False):
    user = (
        db.scalar(
            select(User)
            .where(User.id == id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if lock
        else db.get(User, id)
    )
    if not user or not accessible(db, actor, user):
        raise HTTPException(404, "Учётная запись не найдена")
    return user


@router.get("/auth/config")
def config():
    return {
        "modes": ["LOCAL"],
        "oidc_enabled": False,
        "password_recovery": "ADMIN_ONE_USE_LINK",
        "external_email_required": False,
    }


@router.get("/users/{user_id}")
def user_detail(
    user_id: str, response: Response, db=Depends(get_db), actor=Depends(get_actor)
):
    user = target(db, actor, user_id)
    response.headers["ETag"] = user_etag(user)
    return {
        **actor_dict(user_actor(user)),
        "enabled": user.enabled,
        "etag": user_etag(user),
    }


@router.patch("/users/{user_id}")
def update_user(
    user_id: str,
    body: dict,
    request: Request,
    db=Depends(get_db),
    actor=Depends(get_actor),
):
    require_role(actor, "HR_ADMIN", "PLATFORM_ADMIN")
    candidate = target(db, actor, user_id)
    employee = (
        get_entity(db, candidate.employee_id, "employee", lock=True)
        if candidate.employee_id
        else None
    )
    user = target(db, actor, user_id, True)
    if not request.headers.get("If-Match"):
        raise HTTPException(428, "Обновите учётную запись")
    if request.headers["If-Match"] != user_etag(user):
        raise HTTPException(412, "Учётная запись уже изменена")
    if (
        set(body) - {"name", "enabled", "role", "scope", "reason"}
        or len(str(body.get("reason", ""))) < 3
    ):
        raise HTTPException(422, "Укажите разрешённые изменения и причину")
    role = body.get("role", user.role)
    scope = body.get("scope", dict(user.scope))
    if role not in ROLES or not isinstance(scope, dict):
        raise HTTPException(422, "Недопустимая роль или область")
    if actor.role == "PLATFORM_ADMIN" and role != "PLATFORM_ADMIN":
        raise HTTPException(403, "Нет права делегировать бизнес-доступ")
    if not actor.scope.get("all") and (
        not can_delegate_scope(actor.scope, scope)
        or role in {"HR_ADMIN", "PLATFORM_ADMIN"}
    ):
        raise HTTPException(403, "Нельзя расширить область полномочий")
    if user.id == actor.id and (
        body.get("enabled") is False or role != user.role or scope != user.scope
    ):
        raise HTTPException(
            409, "Для изменения собственных полномочий нужен другой администратор"
        )
    if "enabled" in body and type(body["enabled"]) is not bool:
        raise HTTPException(422, "enabled должен быть boolean")
    enabled = body.get("enabled", user.enabled)
    if role == "EMPLOYEE":
        if not employee:
            raise HTTPException(422, "Для employee account нужна кадровая карточка")
        if enabled:
            if employee.data.get("employment_stage") == "ENDED":
                raise HTTPException(409, "Нельзя открыть доступ завершённому найму")
            other = db.scalar(
                select(User).where(
                    User.employee_id == employee.id,
                    User.id != user.id,
                    User.enabled == True,
                )
            )
            if other:
                raise HTTPException(
                    409, "Карточка уже связана с действующей учётной записью"
                )
            employee.data = {**employee.data, "user_id": user.id}
            for conversation in list_kind(db, "conversation", parent_id=employee.id):
                if conversation.data.get("episode_id") == employee.data.get(
                    "episode_id"
                ):
                    conversation.owner_id = user.id
    user.role = role
    user.scope = scope
    user.enabled = enabled
    if "name" in body:
        name = str(body["name"]).strip()
        if not 1 <= len(name) <= 255:
            raise HTTPException(422, "Недопустимое имя")
        user.name = name
    # Revoke all sessions after a grant change/disable, current actor read is fresh on every request too.
    db.execute(delete(SessionToken).where(SessionToken.user_id == user.id))
    audit(
        db,
        actor,
        "user.update",
        user.id,
        {"role": user.role, "enabled": user.enabled},
        reason=body["reason"],
    )
    commit(db)
    return {
        **actor_dict(user_actor(user)),
        "enabled": user.enabled,
        "etag": user_etag(user),
    }


class ResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=255)


@router.post("/auth/password-reset/request")
def request_reset(body: ResetRequest, request: Request, db=Depends(get_db)):
    email = body.email.strip().lower()
    key = fingerprint({"email": email, "window": int(now().timestamp() // 900)})[:36]
    if not list_kind(db, "password_recovery_request", parent_id=key):
        user = db.scalar(select(User).where(User.email == email, User.enabled == True))
        save_entity(
            db,
            "password_recovery_request",
            {"account_hash": hash_token(email)},
            status="REQUESTED",
            parent_id=key,
            owner_id=user.id if user else None,
        )
        commit(db)
    return {
        "status": "REQUEST_ACCEPTED",
        "message": "Если учётная запись существует, администратор сможет выдать одноразовую ссылку после проверки личности. Обратитесь к администратору по известному вам каналу.",
    }


@router.post("/users/{user_id}/password-reset")
def issue_reset(user_id: str, body: dict, db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, "HR_ADMIN", "PLATFORM_ADMIN")
    user = target(db, actor, user_id, True)
    if len(str(body.get("reason", ""))) < 3 or not user.enabled:
        raise HTTPException(422, "Подтвердите проверку личности и причину")
    for old in list_kind(db, "password_reset", parent_id=user.id):
        if old.status == "ISSUED":
            old.status = "REVOKED"
    raw = secrets.token_urlsafe(32)
    expires = now() + timedelta(minutes=15)
    row = save_entity(
        db,
        "password_reset",
        {
            "token_hash": hash_token(raw),
            "expires_at": expires.isoformat(),
            "issued_by": actor.id,
        },
        status="ISSUED",
        parent_id=user.id,
    )
    audit(db, actor, "password_reset.issue", user.id, reason=body["reason"])
    commit(db)
    return {
        "reset_id": row.id,
        "token": raw,
        "expires_at": expires.isoformat(),
        "delivery": "MANUAL_KNOWN_CHANNEL",
    }


class ResetConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=32, max_length=128)
    new_password: str = Field(min_length=16, max_length=256)


@router.post("/auth/password-reset/confirm")
def confirm_reset(body: ResetConfirm, db=Depends(get_db)):
    hashed = hash_token(body.token)
    row = next(
        (
            e
            for e in list_kind(db, "password_reset")
            if e.data.get("token_hash") == hashed
        ),
        None,
    )
    if row:
        row = db.scalar(
            select(Entity)
            .where(Entity.id == row.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    if (
        not row
        or row.status != "ISSUED"
        or datetime.fromisoformat(row.data["expires_at"]) <= now()
    ):
        raise HTTPException(400, "Недействительная или использованная ссылка")
    user = db.scalar(select(User).where(User.id == row.parent_id).with_for_update())
    if not user or not user.enabled:
        raise HTTPException(400, "Недействительная ссылка")
    user.password_hash = ph.hash(body.new_password)
    row.status = "USED"
    row.data = {**row.data, "used_at": now().isoformat()}
    db.execute(delete(SessionToken).where(SessionToken.user_id == user.id))
    audit(db, None, "password_reset.confirm", user.id)
    commit(db)
    return {"status": "PASSWORD_CHANGED"}
