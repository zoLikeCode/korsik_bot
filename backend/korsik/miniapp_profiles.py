"""Self and direct-team profiles; neither a photo URL nor an org link grants HR rights."""
import io
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from .auth import get_actor
from .common import employee_is_enabled, require_role
from .db import get_db
from .models import Entity, User
from .settings import get_settings
from . import organization as org

router = APIRouter()


def _current(employee):
    return bool(employee and employee.kind == "employee" and employee_is_enabled(employee)
                and employee.status not in {"ARCHIVED", "ENDED", "CANCELLED", "DELETED"}
                and employee.data.get("employment_stage") in {"PREBOARDING", "ACTIVE"})


def _context(db, actor):
    require_role(actor, "EMPLOYEE", "MANAGER")
    rows = list(db.scalars(select(Entity).where(Entity.kind.in_(["employee", "org_unit", "journey"]))
                          .execution_options(populate_existing=True)))
    employees = {e.id: e for e in rows if _current(e)}
    units = {e.id: e for e in rows if e.kind == "org_unit" and e.status == "ACTIVE"}
    own = employees.get(actor.employee_id)
    if actor.employee_id and not own or actor.role == "EMPLOYEE" and not own:
        raise HTTPException(403, "Кадровая карточка недоступна")
    # Keep legacy principal lookups bounded even for a manager with a large team.
    # Holding these fresh rows also prevents db.get() from reusing stale owners.
    candidates = list(employees.values()) if actor.role == "MANAGER" else [own]
    principal_ids = {e.data.get("manager_id") for e in candidates if e and e.data.get("manager_id")}
    principals = list(db.scalars(select(User).where(User.id.in_(principal_ids))
                                .execution_options(populate_existing=True))) if principal_ids else []
    managers = {e.id: org._effective_manager(db, e, employees, units) for e in candidates if e}
    manager = employees.get(managers.get(own.id)) if own else None
    reports = []
    if actor.role == "MANAGER":
        for candidate in employees.values():
            if own and candidate.id == own.id:
                continue
            effective = managers.get(candidate.id)
            direct = bool(own and effective == own.id)
            if not effective and not candidate.data.get("reports_to_employee_id"):
                direct = direct or candidate.data.get("manager_id") == actor.id
            if own and not org._same_legal(own, candidate):
                continue
            restricted = any(actor.scope.get(k) for k in ["all", "site_ids", "legal_entity_ids"])
            if direct and (not restricted or org._grant(actor.scope, candidate.data.get("legal_entity_id"), candidate.data.get("site_id"))):
                reports.append(candidate)
    adapting = {j.parent_id for j in rows if j.kind == "journey" and j.status in {"SCHEDULED", "ACTIVE", "PAUSED"}
                and j.parent_id in employees and j.data.get("episode_id") == employees[j.parent_id].data.get("episode_id")}
    return own, manager, reports, units, adapting


def _photo_asset(db, employee):
    if not _current(employee):
        return None
    asset = db.scalar(select(Entity).where(Entity.id == employee.data["profile_photo_asset_id"])
                      .execution_options(populate_existing=True)) if employee.data.get("profile_photo_asset_id") else None
    if (not asset or asset.kind != "asset" or asset.status not in {"READY", "APPROVED"}
            or asset.owner_id != employee.id or not asset.data.get("validated")
            or asset.data.get("employee_id", employee.id) != employee.id
            or not employee.data.get("episode_id")
            or asset.data.get("episode_id") != employee.data.get("episode_id")
            or asset.data.get("content_type") not in {"image/png", "image/jpeg", "image/webp"}):
        return None
    return asset


def _photo_url(db, employee):
    return f"/api/v1/profiles/{employee.id}/photo" if _photo_asset(db, employee) else None


def _summary(db, employee, units, adapting):
    unit = units.get(employee.data.get("org_unit_id"))
    return {"id": employee.id, "name": employee.data.get("name") or "Сотрудник",
            "department": unit.data.get("title") if unit else employee.data.get("department"),
            "position": employee.data.get("position"), "profile_photo_url": _photo_url(db, employee),
            "is_adapting": employee.id in adapting}


def _legacy_manager_allowed(employee, units):
    if employee.data.get("reports_to_employee_id"):
        return False
    unit_id = employee.data.get("org_unit_id")
    seen = set()
    while unit_id and unit_id not in seen:
        seen.add(unit_id)
        unit = units.get(unit_id)
        if not unit or not org._same_legal(employee, unit):
            break
        head = unit.data.get("head_employee_id")
        if head and head != employee.id:
            return False
        unit_id = unit.parent_id
    return True


@router.get("/me/profile")
def my_profile(db=Depends(get_db), actor=Depends(get_actor)):
    own, manager, reports, units, adapting = _context(db, actor)
    user = db.get(User, actor.id)
    email = (user.email if user else actor.email) or None
    if email and email.endswith("@internal.invalid"):
        email = None
    own_data = None
    if own:
        own_data = {**_summary(db, own, units, adapting), "email": own.data.get("email") or email,
                    "start_date": own.data.get("start_date"), "probation_end_date": own.data.get("probation_end_date"),
                    "employment_stage": own.data.get("employment_stage")}
    manager_data = _summary(db, manager, units, adapting) if manager else None
    if own and manager_data is None and own.data.get("manager_id") and _legacy_manager_allowed(own, units):
        principal = db.get(User, own.data["manager_id"])
        if principal and principal.enabled and principal.role == "MANAGER":
            manager_data = {"user_id": principal.id, "name": principal.name, "profile_photo_url": None}
    return {"user": {"id": actor.id, "name": user.name if user else actor.name, "email": email, "role": actor.role},
            "employee": own_data, "manager": manager_data,
            "direct_reports": [_summary(db, report, units, adapting) for report in reports]}


@router.get("/profiles/{employee_id}/photo")
def profile_photo(employee_id: str, db=Depends(get_db), actor=Depends(get_actor)):
    own, manager, reports, _, _ = _context(db, actor)
    allowed = {e.id: e for e in [own, manager, *reports] if e}
    employee = allowed.get(employee_id)
    asset = _photo_asset(db, employee)
    if not asset:
        raise HTTPException(404, "Фото недоступно")
    root = Path(get_settings().blob_root).resolve()
    path = Path(asset.data.get("storage_path", "")).resolve()
    if not path.is_file() or not path.is_relative_to(root) or path.stat().st_size > 10 * 1024 * 1024:
        raise HTTPException(404, "Фото недоступно")
    try:
        from PIL import Image, ImageOps
        with Image.open(path) as image:
            if image.format not in {"JPEG", "PNG", "WEBP"} or image.width * image.height > 30_000_000:
                raise ValueError()
            output = io.BytesIO()
            image = ImageOps.exif_transpose(image)
            image.thumbnail((256, 256))
            image.convert("RGB").save(output, "JPEG", quality=82)
    except Exception:
        raise HTTPException(404, "Фото недоступно")
    return Response(output.getvalue(), media_type="image/jpeg",
                    headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
