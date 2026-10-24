"""Responsible HR assignment. Ownership never grants document access."""

from fastapi import HTTPException
from sqlalchemy import select
from .auth import user_actor
from .common import require_role
from .models import User


def _eligible_owner(db, doc, owner_id):
    from .knowledge import _scope_access

    if not isinstance(owner_id, str) or not owner_id.strip() or len(owner_id) > 36:
        raise HTTPException(422, "enabled_scoped_hr_owner_required")
    user = db.scalar(
        select(User)
        .where(User.id == owner_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not user or not user.enabled or user.role not in {"HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT"}:
        raise HTTPException(422, "enabled_scoped_hr_owner_required")
    if not _scope_access(user_actor(user), doc, write=True):
        raise HTTPException(422, "owner_scope_does_not_cover_document")
    return user.id


def validate_document_owner(db, actor, doc, owner_id, reason=None, changing=False):
    from .knowledge import _scope_access

    require_role(actor, "HR_ADMIN", "HR_SPECIALIST")
    if not _scope_access(actor, doc, write=True):
        raise HTTPException(404, "document_not_found")
    if changing or owner_id != actor.id:
        require_role(actor, "HR_ADMIN")
    if changing and (not isinstance(reason, str) or len(reason.strip()) < 3):
        raise HTTPException(422, "owner_change_reason_required")
    return _eligible_owner(db, doc, owner_id)


def validate_current_document_owner(db, actor, doc):
    """A scope edit preserves an eligible owner without being a reassignment."""
    from .knowledge import _scope_access

    require_role(actor, "HR_ADMIN", "HR_SPECIALIST")
    if not _scope_access(actor, doc, write=True):
        raise HTTPException(404, "document_not_found")
    return _eligible_owner(db, doc, doc.owner_id)
