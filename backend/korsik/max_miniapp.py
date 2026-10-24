"""MAX-signed launch exchange; identity and permissions remain server-owned.

Protocol: https://dev.max.ru/docs/webapps/validation (WebAppData, NOT
Telegram's WebAppData key derivation variant). Raw launch data is never stored.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import hashlib
import hmac
import json
import re
import secrets
from urllib.parse import unquote, urlsplit
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auth import actor_dict, hash_token, ph, user_actor, employee_account_enabled
from .common import audit, commit, save_entity, employee_is_enabled
from .db import aware, get_db, new_id, now
from .models import Entity, SessionToken, User
from .settings import get_settings, read_secret

router = APIRouter()
MAX_LAUNCH_AGE_SECONDS = 3600
MAX_FUTURE_SKEW_SECONDS = 30
PRINCIPAL_ROLES = {"HR_ADMIN", "HR_SPECIALIST", "MANAGER", "HEAD_TALENT"}


def reject(code, message, status=401):
    raise HTTPException(status, {"code": code, "message": message})


@dataclass(frozen=True)
class VerifiedLaunch:
    max_user_id: str
    auth_date: int
    query_id: str
    fingerprint: str
    start_param: str | None = None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def validate_init_data(raw: str, bot_token: str, *, timestamp=None) -> VerifiedLaunch:
    """Decode once, reject ambiguity, authenticate, then enforce a bounded age."""
    if not isinstance(raw, str) or not 10 <= len(raw) <= 32768 or not bot_token:
        reject("max_init_data_invalid", "Не удалось проверить запуск MAX. Откройте приложение заново.")
    params = {}
    try:
        for item in raw.split("&"):
            key, separator, encoded = item.partition("=")
            if (not separator or not re.fullmatch(r"[A-Za-z0-9_]+", key)
                    or key in params or re.search(r"%(?![0-9a-fA-F]{2})", encoded)):
                raise ValueError("ambiguous_parameters")
            # MAX specifies decodeURIComponent / URL-decode values exactly once;
            # a literal '+' is not silently changed into a space.
            value = unquote(encoded, encoding="utf-8", errors="strict")
            if any(char in value for char in "\r\n\0"):
                raise ValueError("control_character")
            params[key] = value
        signature = params.pop("hash")
        if not re.fullmatch(r"[0-9a-f]{64}", signature):
            raise ValueError("invalid_hash")
        launch_params = "\n".join(f"{key}={params[key]}" for key in sorted(params))
        secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
        calculated = hmac.new(secret, launch_params.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, calculated):
            raise ValueError("signature_mismatch")
        user = json.loads(params["user"], object_pairs_hook=_unique_object)
        uid = user["id"]
        if isinstance(uid, bool) or not isinstance(uid, int) or not 0 < uid < 2**63:
            raise ValueError("invalid_user_id")
        if not re.fullmatch(r"[0-9]{1,12}", params["auth_date"]):
            raise ValueError("invalid_auth_date")
        issued = int(params["auth_date"])
        start_param = params.get("start_param")
        if start_param is not None and (len(start_param) > 512 or not re.fullmatch(r"[A-Za-z0-9_-]*", start_param)):
            raise ValueError("invalid_start_param")
        query_id = params["query_id"]
        if not 1 <= len(query_id) <= 256 or any(ord(char) < 32 for char in query_id):
            raise ValueError("invalid_query_id")
    except (KeyError, TypeError, ValueError, UnicodeError):
        reject("max_init_data_invalid", "Не удалось проверить запуск MAX. Откройте приложение заново.")
    current = int(now().timestamp()) if timestamp is None else int(timestamp)
    if issued > current + MAX_FUTURE_SKEW_SECONDS:
        reject("max_init_data_future", "Время запуска MAX некорректно. Откройте приложение заново.")
    if current - issued > MAX_LAUNCH_AGE_SECONDS:
        reject("max_init_data_expired", "Срок запуска MAX истёк. Закройте и снова откройте приложение.")
    return VerifiedLaunch(str(uid), issued, query_id, hash_token(launch_params), start_param)


def _fresh_entity(db, entity_id, kind):
    return db.scalar(select(Entity).where(Entity.id == entity_id, Entity.kind == kind)
                     .execution_options(populate_existing=True))


def _employee_for_binding(db, binding):
    employee = _fresh_entity(db, binding.parent_id, "employee")
    if (not employee_is_enabled(employee) or employee.status in {"ARCHIVED", "DELETED"}
            or employee.data.get("employment_stage") == "ENDED"
            or employee.data.get("binding_id") != binding.id
            or binding.data.get("episode_id") != employee.data.get("episode_id", employee.id)):
        return None
    return employee


def _resolve_identity(db, launch):
    bindings = db.scalars(select(Entity).where(Entity.kind.in_(["principal_binding", "max_binding"]), Entity.status == "ACTIVE")
                          .execution_options(populate_existing=True)).all()
    current = []
    for binding in bindings:
        if str(binding.data.get("max_user_id")) != launch.max_user_id:
            continue
        if binding.kind == "principal_binding":
            if binding.data.get("channel") == "MAX" and binding.data.get("confirmed_at"):
                current.append((binding, None))
        else:
            employee = _employee_for_binding(db, binding)
            if employee:
                current.append((binding, employee))
    if len(current) > 1:
        reject("max_identity_ambiguous", "MAX связан с несколькими учётными записями. Обратитесь к HR.", 409)
    if not current:
        reject("max_account_not_linked", "Войдите в существующий кабинет и подключите MAX. Сотруднику нужно приглашение и подтверждение HR.", 403)
    binding, employee = current[0]
    if binding.kind == "principal_binding":
        user = db.scalar(select(User).where(User.id == binding.owner_id).with_for_update().execution_options(populate_existing=True))
        employee = _principal_employee(db, user) if user else None
        if not user or not user.enabled or (user.role not in PRINCIPAL_ROLES and not employee) or not employee_account_enabled(db, user):
            reject("max_account_unavailable", "Учётная запись недоступна. Обратитесь к администратору.", 403)
    else:
        employee = db.scalar(select(Entity).where(Entity.id == employee.id).with_for_update().execution_options(populate_existing=True))
        if not _employee_for_binding(db, binding):
            reject("max_account_not_linked", "Привязка MAX изменилась. Обратитесь к HR.", 403)
        associated = db.scalars(select(User).where(User.employee_id == employee.id).with_for_update().execution_options(populate_existing=True)).all()
        pointer = employee.data.get("user_id")
        enabled = [candidate for candidate in associated if candidate.enabled]
        if len(enabled) > 1:
            reject("max_identity_ambiguous", "Карточка связана с несколькими учётными записями. Обратитесь к HR.", 409)
        if pointer:
            # A current HR-owned card pointer can distinguish the renewed
            # account from disabled history after rehire. It cannot select a
            # missing/disabled account or resolve multiple enabled identities.
            user = next((candidate for candidate in associated if candidate.id == pointer), None)
            if not user or not user.enabled:
                reject("max_account_unavailable", "Связь с учётной записью требует проверки HR.", 403)
        else:
            # Without a trusted pointer preserve the conservative legacy
            # policy: do not choose among historical accounts or bypass disable.
            if len(associated) > 1:
                reject("max_identity_ambiguous", "Карточка связана с несколькими учётными записями. Обратитесь к HR.", 409)
            user = associated[0] if associated else None
        if user and (not user.enabled or not _employee_role_current(employee, user)):
            reject("max_account_unavailable", "Учётная запись недоступна. Обратитесь к HR.", 403)
        if not user:
            # Provision only an account for an already HR-approved employment
            # binding, never a new card, approval, role or scope from initData.
            user = User(id=new_id(), email=f"max-employee-{employee.id}@internal.invalid",
                        name=employee.data.get("name") or "Сотрудник", role="EMPLOYEE", scope={},
                        employee_id=employee.id, password_hash=ph.hash(secrets.token_urlsafe(64)), enabled=True)
            db.add(user)
            db.flush()
        employee.data = {**employee.data, "user_id": user.id}
        for conversation in db.scalars(select(Entity).where(Entity.kind == "conversation", Entity.parent_id == employee.id)):
            conversation.owner_id = user.id
    return user, binding, employee


def _employee_role_current(employee, user):
    if user.role == "EMPLOYEE":
        return True
    grant = employee.data.get("miniapp_role_access") or {}
    return bool(user.role == "MANAGER" and grant.get("role") == "MANAGER"
                and grant.get("user_id") == user.id
                and grant.get("episode_id") == employee.data.get("episode_id"))


def _principal_employee(db, user):
    """An explicit HR demotion keeps an already confirmed principal MAX identity."""
    if user.role != "EMPLOYEE" or not user.employee_id:
        return None
    employee = db.scalar(select(Entity).where(Entity.id == user.employee_id, Entity.kind == "employee")
                         .execution_options(populate_existing=True))
    grant = employee.data.get("miniapp_role_access", {}) if employee else {}
    if (not employee_is_enabled(employee) or employee.data.get("employment_stage") == "ENDED"
            or grant.get("role") != "EMPLOYEE" or grant.get("user_id") != user.id
            or grant.get("episode_id") != employee.data.get("episode_id")):
        return None
    return employee


def _session_meta_id(session_id):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "korsik:max-miniapp-session:" + session_id))


def miniapp_session_is_current(db, session, user):
    """Hook called by auth.session_user; password sessions retain their policy."""
    meta = db.scalar(select(Entity).where(Entity.kind == "max_miniapp_session", Entity.id == _session_meta_id(session.id))
                     .execution_options(populate_existing=True))
    if meta is None:
        return employee_account_enabled(db, user)
    if meta.status != "ACTIVE" or meta.owner_id != user.id or not user.enabled or not employee_account_enabled(db, user):
        return False
    binding = _fresh_entity(db, meta.data.get("binding_id"), meta.data.get("binding_kind"))
    if (not binding or binding.status != "ACTIVE" or hash_token(str(binding.data.get("max_user_id"))) != meta.data.get("max_user_hash")):
        return False
    if binding.kind == "principal_binding":
        return bool(binding.owner_id == user.id and binding.data.get("channel") == "MAX"
                    and binding.data.get("confirmed_at") and (user.role in PRINCIPAL_ROLES or _principal_employee(db, user)))
    employee = _employee_for_binding(db, binding)
    return bool(employee and _employee_role_current(employee, user) and user.employee_id == employee.id
                and employee.data.get("user_id") == user.id and meta.data.get("episode_id") == employee.data.get("episode_id", employee.id))


class MiniAppLogin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    init_data: str = Field(min_length=10, max_length=32768)


def _response(user, csrf, expires):
    actor = actor_dict(user_actor(user))
    return {"user": actor, "actor": actor, "csrf_token": csrf, "auth_method": "MAX_MINIAPP", "session_expires_at": aware(expires).isoformat()}


@router.post("/auth/max-miniapp")
def max_miniapp_login(body: MiniAppLogin, request: Request, response: Response, db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    settings = get_settings()
    public = urlsplit(settings.public_url)
    if request.url.scheme != "https" and not (settings.secure_cookies and public.scheme == "https"):
        reject("max_https_required", "Для входа через MAX требуется защищённое HTTPS-соединение.", 503)
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.headers.get("host"):
        reject("origin_mismatch", "Недопустимый источник запроса.", 403)
    token = read_secret(settings.max_token_file, required=False)
    if not token:
        reject("max_miniapp_unavailable", "Вход через MAX пока не настроен. Используйте обычный вход.", 503)
    launch = validate_init_data(body.init_data, token)
    replay_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "korsik:max-miniapp-launch:" + launch.query_id))
    previous = _fresh_entity(db, replay_id, "max_miniapp_launch")
    if previous:
        raw_session = request.cookies.get("korsik_session", "")
        csrf = request.cookies.get("korsik_csrf", "")
        session = db.get(SessionToken, hash_token(raw_session)) if raw_session else None
        user = db.get(User, session.user_id) if session else None
        if (previous.data.get("launch_fingerprint") == launch.fingerprint and session and user
                and previous.data.get("session_id") == session.id and aware(session.expires_at) > now()
                and csrf and hmac.compare_digest(hash_token(csrf), session.csrf_hash)
                and miniapp_session_is_current(db, session, user)):
            return _response(user, csrf, session.expires_at)
        reject("max_init_data_replayed", "Этот запуск MAX уже использован. Закройте и снова откройте приложение.", 409)
    try:
        # Deterministic primary key makes a simultaneous second exchange fail
        # atomically, including on multi-worker deployments.
        replay = save_entity(db, "max_miniapp_launch", {"launch_fingerprint": launch.fingerprint, "auth_date": launch.auth_date}, id=replay_id, status="CONSUMED")
        db.flush()
        from .hr_accounts import redeem_invitation
        invited = redeem_invitation(db, launch)
        user, binding, employee = invited if invited is not None else _resolve_identity(db, launch)
        raw_session, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        ttl = max(1, min(int(settings.session_ttl_seconds), MAX_LAUNCH_AGE_SECONDS))
        expires = now() + timedelta(seconds=ttl)
        session = SessionToken(id=hash_token(raw_session), user_id=user.id, csrf_hash=hash_token(csrf), expires_at=expires)
        db.add(session)
        db.flush()
        save_entity(db, "max_miniapp_session", {"binding_id": binding.id, "binding_kind": binding.kind,
                    "max_user_hash": hash_token(launch.max_user_id), "session_id": session.id, "episode_id": employee.data.get("episode_id", employee.id) if employee else None}, id=_session_meta_id(session.id), parent_id=user.id, owner_id=user.id)
        replay.data = {**replay.data, "session_id": session.id}
        audit(db, user_actor(user), "auth.max_miniapp_login", user.id, {"binding_id": binding.id, "binding_kind": binding.kind})
        commit(db)
    except IntegrityError:
        db.rollback()
        reject("max_init_data_replayed", "Этот запуск MAX уже обрабатывается. Откройте приложение заново.", 409)
    except BaseException:
        db.rollback()
        raise
    flags = {"secure": True, "samesite": "none", "path": "/", "max_age": ttl}
    response.set_cookie("korsik_session", raw_session, httponly=True, **flags)
    response.set_cookie("korsik_csrf", csrf, httponly=False, **flags)
    return _response(user, csrf, expires)
