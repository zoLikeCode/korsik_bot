"""Scope-limited, one-use HR invitations redeemed only by a signed MAX launch."""
from datetime import datetime, timedelta
import secrets
from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, delete, and_, or_
from .auth import get_actor, hash_token, ph, user_actor, is_hidden_setup, account_is_deleted, SETUP_USER_ID
from .common import audit, can_delegate_scope, check_etag, commit, etag, get_entity, require_role, save_entity
from .db import get_db, new_id, now, aware
from .models import Entity, User, SessionToken
from .settings import get_settings

router = APIRouter()
EDIT = {"HR_ADMIN", "HEAD_TALENT"}


class HRInvitation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=255)
    email: str = Field(min_length=3, max_length=255)
    scope: dict | None = None
    expires_in_seconds: int = Field(default=86400, ge=600, le=604800)


def _allowed(actor, scope):
    return actor.role in EDIT and can_delegate_scope(actor.scope or {}, scope or {})


def _view(row):
    return {"id": row.id, "name": row.data["name"], "email": row.data["email"], "scope": row.data["scope"],
            "status": "EXPIRED" if row.status == "ISSUED" and aware(datetime.fromisoformat(row.data["expires_at"])) <= now() else row.status,
            "expires_at": row.data["expires_at"], "created_at": row.created_at.isoformat(), "etag": etag(row)}


def _lock(db):
    from .chat import employee_binding_lock
    employee_binding_lock(db)


@router.get("/hr-accounts")
def accounts(db=Depends(get_db), actor=Depends(get_actor)):
    from .auth_admin import user_etag
    require_role(actor, *EDIT)
    return {"items": [{"id": u.id, "name": u.name, "email": u.email, "role": u.role, "enabled": u.enabled, "scope": dict(u.scope),
                        "etag": user_etag(u), "can_delete": actor.role == "HEAD_TALENT" and u.id != actor.id}
                      for u in db.scalars(select(User).where(User.role == "HR_SPECIALIST").order_by(User.name))
                      if _allowed(actor, u.scope) and not is_hidden_setup(u) and not account_is_deleted(db, u)]}


@router.delete("/hr-accounts/{user_id}")
def delete_account(user_id: str, request: Request, db=Depends(get_db), actor=Depends(get_actor)):
    """Delete only a scoped HR staff identity, preserving all business history."""
    from .auth_admin import user_etag
    require_role(actor, "HEAD_TALENT")
    _lock(db)
    issuer = db.scalar(select(User).where(User.id == actor.id).with_for_update().execution_options(populate_existing=True))
    if not issuer or not issuer.enabled or issuer.role != "HEAD_TALENT":
        raise HTTPException(403, "Право удалять HR изменилось")
    if user_id == issuer.id or user_id == SETUP_USER_ID:
        raise HTTPException(409, "Собственный или установочный аккаунт удалить нельзя")
    user = db.scalar(select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True))
    if not user or user.role != "HR_SPECIALIST" or not _allowed(user_actor(issuer), user.scope) or account_is_deleted(db, user):
        raise HTTPException(404, "HR-менеджер не найден")
    supplied = request.headers.get("If-Match")
    if not supplied:
        raise HTTPException(428, "Обновите список HR-менеджеров")
    if supplied != user_etag(user):
        raise HTTPException(412, "Учётная запись уже изменена")
    user.enabled = False
    save_entity(db, "account_lifecycle", {"deleted_at": now().isoformat(), "deleted_by": issuer.id,
        "role": user.role, "scope": dict(user.scope)}, parent_id=user.id, owner_id=issuer.id, status="DELETED")
    db.execute(delete(SessionToken).where(SessionToken.user_id == user.id))
    owned_rows = select(Entity).where(or_(
        and_(Entity.kind.in_(["principal_binding", "max_miniapp_session"]), Entity.owner_id == user.id),
        and_(Entity.kind == "callback_action", Entity.data["recipient_kind"].as_string() == "USER", Entity.data["recipient_id"].as_string() == user.id),
        and_(Entity.kind == "password_reset", Entity.parent_id == user.id),
        and_(Entity.kind == "employee_invite", or_(Entity.owner_id == user.id, Entity.data["issued_by"].as_string() == user.id)),
        and_(Entity.kind == "hr_invitation", or_(Entity.owner_id == user.id, Entity.data["user_id"].as_string() == user.id)),
    )).with_for_update(skip_locked=True).execution_options(populate_existing=True)
    # A held recovery/callback row cannot block account revocation. Its current
    # user.enabled check still fences it after this transaction commits.
    for row in db.scalars(owned_rows):
        if row.status not in {"REVOKED", "EXPIRED", "FAILED", "CANCELLED"}:
            row.data = {**row.data, "revoked_by": issuer.id, "revoked_at": now().isoformat(), "previous_status": row.status}
            row.status = "REVOKED"
    audit(db, user_actor(issuer), "hr.account.delete", user.id)
    commit(db)
    return JSONResponse({"id": user.id, "status": "DELETED", "enabled": False})


@router.get("/hr-accounts/invitations")
def invitations(db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *EDIT)
    return {"items": [_view(row) for row in db.scalars(select(Entity).where(Entity.kind == "hr_invitation").order_by(Entity.created_at.desc()))
                      if _allowed(actor, row.data.get("scope"))]}


@router.post("/hr-accounts/invitations", status_code=201)
def issue_invitation(body: HRInvitation, db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *EDIT)
    _lock(db)
    issuer = db.scalar(select(User).where(User.id == actor.id).with_for_update().execution_options(populate_existing=True))
    if not issuer or not issuer.enabled or issuer.role not in EDIT:
        raise HTTPException(403, "Право приглашать HR изменилось")
    scope = dict(issuer.scope) if body.scope is None else dict(body.scope)
    if not _allowed(user_actor(issuer), scope) or not (scope.get("all") or scope.get("site_ids") or scope.get("legal_entity_ids")):
        raise HTTPException(403, "Нельзя расширить область полномочий")
    email = body.email.lower()
    if "@" not in email or email.startswith("@") or email.endswith("@") or any(c.isspace() for c in email):
        raise HTTPException(422, "Укажите рабочую почту")
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "Учётная запись с этой почтой уже существует")
    for old in db.scalars(select(Entity).where(Entity.kind == "hr_invitation", Entity.status == "ISSUED")):
        if old.data.get("email") == email:
            if not _allowed(user_actor(issuer), old.data.get("scope")):
                raise HTTPException(409, "Приглашение с этой почтой уже существует")
            old.status = "REVOKED"
    token = secrets.token_urlsafe(32)
    row = save_entity(db, "hr_invitation", {"name": body.name, "email": email, "scope": scope,
        "role": "HR_SPECIALIST", "token_hash": hash_token(token), "expires_at": (now() + timedelta(seconds=body.expires_in_seconds)).isoformat()},
        owner_id=issuer.id, status="ISSUED")
    audit(db, actor, "hr.invitation.issue", row.id, {"role": "HR_SPECIALIST"})
    commit(db)
    return {**_view(row), "invite_url": get_settings().max_bot_url + "?startapp=hr_" + token}


@router.post("/hr-accounts/invitations/{invitation_id}/revoke")
def revoke_invitation(invitation_id: str, request: Request, db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *EDIT)
    _lock(db)
    row = get_entity(db, invitation_id, "hr_invitation", lock=True)
    if not _allowed(actor, row.data.get("scope")):
        raise HTTPException(404, "Приглашение не найдено")
    check_etag(request, row)
    if row.status != "ISSUED":
        raise HTTPException(409, "Приглашение уже использовано или отозвано")
    row.status = "REVOKED"
    row.data = {**row.data, "revoked_at": now().isoformat(), "revoked_by": actor.id}
    audit(db, actor, "hr.invitation.revoke", row.id)
    commit(db)
    return _view(row)


def redeem_invitation(db, launch):
    """Caller authenticates initData and commits invitation/account/session atomically."""
    payload = launch.start_param or ""
    if not payload.startswith("hr_"):
        return None
    _lock(db)
    token_hash = hash_token(payload[3:])
    row = next((r for r in db.scalars(select(Entity).where(Entity.kind == "hr_invitation")).all()
                if secrets.compare_digest(r.data.get("token_hash", ""), token_hash)), None)
    if row:
        row = get_entity(db, row.id, "hr_invitation", lock=True)
    if not row:
        raise HTTPException(403, "Приглашение недействительно")
    if row.status == "CONSUMED" and row.data.get("redeemed_max_user_hash") == hash_token(launch.max_user_id):
        # Reopening a used link grants nothing; the ordinary existing MAX binding
        # must still pass its current role/enabled/revocation/session checks.
        return None
    if row.status != "ISSUED" or aware(datetime.fromisoformat(row.data["expires_at"])) <= now():
        raise HTTPException(409, "Приглашение недействительно или уже использовано")
    issuer = db.scalar(select(User).where(User.id == row.owner_id).with_for_update().execution_options(populate_existing=True))
    if not issuer or not issuer.enabled or not _allowed(user_actor(issuer), row.data.get("scope")):
        raise HTTPException(403, "Полномочия автора приглашения изменились")
    active = db.scalars(select(Entity).where(Entity.kind.in_(["principal_binding", "max_binding"]), Entity.status == "ACTIVE")
                        .execution_options(populate_existing=True)).all()
    if any(str(b.data.get("max_user_id")) == launch.max_user_id for b in active):
        raise HTTPException(409, "MAX уже связан с учётной записью; приглашение не меняет существующие полномочия")
    if db.scalar(select(User).where(User.email == row.data["email"])):
        raise HTTPException(409, "Учётная запись с этой почтой уже существует")
    user = User(id=new_id(), name=row.data["name"], email=row.data["email"], role="HR_SPECIALIST",
                scope=dict(row.data["scope"]), password_hash=ph.hash(secrets.token_urlsafe(64)), enabled=True)
    db.add(user); db.flush()
    binding = save_entity(db, "principal_binding", {"channel": "MAX", "max_user_id": launch.max_user_id,
        "confirmed_at": now().isoformat(), "verification_method": "HEAD_HR_INVITATION", "invitation_id": row.id}, owner_id=user.id)
    row.status = "CONSUMED"
    row.data = {**row.data, "user_id": user.id, "binding_id": binding.id,
                "redeemed_max_user_hash": hash_token(launch.max_user_id), "redeemed_at": now().isoformat()}
    audit(db, user_actor(issuer), "hr.invitation.redeem", row.id, {"user_id": user.id, "role": user.role})
    return user, binding, None
