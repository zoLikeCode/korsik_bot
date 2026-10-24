"""Scoped organization directory; reporting links never grant application roles."""

from __future__ import annotations

import io
import secrets
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text, delete

from .auth import get_actor, user_actor, ph, is_hidden_setup
from .common import (
    actor_can_employee,
    employee_is_enabled,
    audit,
    check_etag,
    commit,
    etag,
    get_entity,
    list_kind,
    require_role,
)
from .db import get_db, now
from .models import Entity, User, SessionToken
from .settings import get_settings

router = APIRouter()
READ = ("HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT", "MANAGER", "EMPLOYEE")
EDIT = ("HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT")


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class UnitCreate(Input):
    title: str = Field(min_length=1, max_length=160)
    type: Literal["DIRECTION", "DEPARTMENT", "TEAM"] = "DEPARTMENT"
    parent_id: str | None = Field(default=None, max_length=100)
    head_employee_id: str | None = Field(default=None, max_length=100)
    legal_entity_id: str = Field(min_length=1, max_length=100)
    site_id: str | None = Field(default=None, max_length=100)


class UnitPatch(Input):
    title: str | None = Field(default=None, min_length=1, max_length=160)
    type: Literal["DIRECTION", "DEPARTMENT", "TEAM"] | None = None
    parent_id: str | None = Field(default=None, max_length=100)
    head_employee_id: str | None = Field(default=None, max_length=100)
    legal_entity_id: str | None = Field(default=None, min_length=1, max_length=100)
    site_id: str | None = Field(default=None, max_length=100)
    status: Literal["ACTIVE", "ARCHIVED"] | None = None


class EmployeePatch(Input):
    org_unit_id: str | None = Field(default=None, max_length=100)
    reports_to_employee_id: str | None = Field(default=None, max_length=100)


class AccessRole(Input):
    role: Literal["MANAGER", "EMPLOYEE"]


def _current(employee):
    return (
        employee_is_enabled(employee)
        and employee.data.get("employment_stage") in {"PREBOARDING", "ACTIVE"}
        and employee.status not in {"ENDED", "CANCELLED"}
    )


def _all(db):
    rows = list(
        db.scalars(
            select(Entity)
            .where(Entity.kind.in_(["employee", "org_unit"]))
            .order_by(Entity.created_at)
            .execution_options(populate_existing=True)
        )
    )
    employees = {e.id: e for e in rows if _current(e)}
    units = {u.id: u for u in rows if u.kind == "org_unit"}
    return employees, units


def _grant(scope, legal_id, site_id, write=False):
    if scope.get("all") is True:
        return True
    if scope.get("legal_entity_ids") and legal_id not in scope["legal_entity_ids"]:
        return False
    if scope.get("site_ids") and (
        site_id not in scope["site_ids"]
        if write
        else site_id is not None and site_id not in scope["site_ids"]
    ):
        return False
    return bool(scope.get("legal_entity_ids") or scope.get("site_ids"))


def visible_employees(db, actor, employees=None):
    require_role(actor, *READ)
    employees = employees if employees is not None else _all(db)[0]
    if actor.role in {"HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT"}:
        return {
            key: e for key, e in employees.items() if actor_can_employee(db, actor, key)
        }
    own = employees.get(actor.employee_id)
    if actor.role == "EMPLOYEE":
        if not own:
            raise HTTPException(403, "Период найма завершён или профиль не привязан")
        legal = own.data.get("legal_entity_id")
        return {
            key: e
            for key, e in employees.items()
            if legal and e.data.get("legal_entity_id") == legal
        } or {own.id: own}
    legal_ids = (
        {own.data.get("legal_entity_id")}
        if own
        else {
            e.data.get("legal_entity_id")
            for e in employees.values()
            if e.data.get("manager_id") == actor.id
        }
    )
    legal_ids.discard(None)
    return {
        key: e
        for key, e in employees.items()
        if e.data.get("legal_entity_id") in legal_ids
        and (
            not (
                actor.scope.get("all")
                or actor.scope.get("site_ids")
                or actor.scope.get("legal_entity_ids")
            )
            or _grant(actor.scope, e.data.get("legal_entity_id"), e.data.get("site_id"))
        )
    }


def _unit_read(actor, unit, visible):
    if unit.status != "ACTIVE":
        return False
    legal = unit.data.get("legal_entity_id")
    if actor.role in {"HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT"} and _grant(
        actor.scope, legal, unit.data.get("site_id")
    ):
        return True
    return any(
        e.data.get("legal_entity_id") == legal
        and (
            not unit.data.get("site_id")
            or e.data.get("site_id") == unit.data["site_id"]
        )
        for e in visible.values()
    )


def _unit_edit(actor, unit):
    return actor.role in EDIT and _grant(
        actor.scope,
        unit.data.get("legal_entity_id"),
        unit.data.get("site_id"),
        write=True,
    )


def _lock_graph(db):
    if db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(76810222)"))


def _same_legal(a, b):
    legal = a.data.get("legal_entity_id")
    return bool(legal and legal == b.data.get("legal_entity_id"))


def _effective_manager(db, employee, employees, units):
    explicit = employee.data.get("reports_to_employee_id")
    if explicit:
        target = employees.get(explicit)
        return (
            target.id
            if target and target.id != employee.id and _same_legal(employee, target)
            else None
        )
    unit_key = employee.data.get("org_unit_id")
    seen = set()
    while unit_key and unit_key not in seen:
        seen.add(unit_key)
        unit = units.get(unit_key)
        if not unit or unit.status != "ACTIVE" or not _same_legal(employee, unit):
            break
        head_key = unit.data.get("head_employee_id")
        if head_key and head_key != employee.id:
            head = employees.get(head_key)
            return head.id if head and _same_legal(employee, head) else None
        unit_key = unit.parent_id
    user = (
        db.get(User, employee.data.get("manager_id"))
        if employee.data.get("manager_id")
        else None
    )
    legacy = employees.get(user.employee_id) if user and user.enabled else None
    return (
        legacy.id
        if legacy and legacy.id != employee.id and _same_legal(employee, legacy)
        else None
    )


def _validate_graph(db, employees, units, legal_id):
    active_units = {k: u for k, u in units.items() if u.status == "ACTIVE"}
    for unit in active_units.values():
        if unit.data.get("legal_entity_id") != legal_id:
            continue
        parent = active_units.get(unit.parent_id) if unit.parent_id else None
        if unit.parent_id and (
            not parent
            or not _same_legal(unit, parent)
            or parent.data.get("site_id")
            and parent.data["site_id"] != unit.data.get("site_id")
        ):
            raise HTTPException(
                422,
                "Родительское подразделение недоступно или относится к другой организации/площадке",
            )
        head = (
            employees.get(unit.data.get("head_employee_id"))
            if unit.data.get("head_employee_id")
            else None
        )
        if head and not _same_legal(unit, head):
            raise HTTPException(
                422,
                "Руководитель подразделения должен быть действующим сотрудником той же организации",
            )
        path = set()
        cursor = unit
        while cursor:
            if cursor.id in path:
                raise HTTPException(409, "Подразделения не могут образовывать цикл")
            path.add(cursor.id)
            cursor = active_units.get(cursor.parent_id)
    links = {}
    for employee in employees.values():
        if employee.data.get("legal_entity_id") != legal_id:
            continue
        unit_key = employee.data.get("org_unit_id")
        unit = active_units.get(unit_key)
        if unit_key and (
            not unit
            or not _same_legal(employee, unit)
            or unit.data.get("site_id")
            and unit.data["site_id"] != employee.data.get("site_id")
        ):
            raise HTTPException(
                422,
                "Подразделение сотрудника недоступно или относится к другой организации/площадке",
            )
        manager_key = employee.data.get("reports_to_employee_id")
        manager = employees.get(manager_key)
        if manager and (
            not _same_legal(employee, manager) or manager.id == employee.id
        ):
            raise HTTPException(
                422,
                "Руководитель должен быть другим действующим сотрудником той же организации",
            )
        links[employee.id] = _effective_manager(db, employee, employees, active_units)
    for key in links:
        seen = set()
        cursor = key
        while cursor:
            if cursor in seen:
                raise HTTPException(
                    409, "Подчинение сотрудников не может образовывать цикл"
                )
            seen.add(cursor)
            cursor = links.get(cursor)


def _photo_asset(db, actor, employee):
    from .assets import can_asset

    asset_id = employee.data.get("profile_photo_asset_id")
    asset = db.get(Entity, asset_id) if isinstance(asset_id, str) else None
    if (
        not asset
        or asset.kind != "asset"
        or asset.status not in {"READY", "APPROVED"}
        or asset.owner_id != employee.id
        or not asset.data.get("validated")
        or asset.data.get("content_type")
        not in {"image/jpeg", "image/png", "image/webp"}
    ):
        return None
    if (
        not employee_is_enabled(employee)
        or asset.data.get("episode_id") != employee.data.get("episode_id")
        or employee.data.get("employment_stage") == "ENDED"
    ):
        return None
    if (
        actor.role in EDIT
        or actor.role == "EMPLOYEE"
        and actor.employee_id == employee.id
    ):
        return asset if can_asset(db, actor, asset) else None
    if actor.role != "MANAGER" or not actor_can_employee(db, actor, employee.id):
        return None
    from .domain import profile_content_fingerprint

    digest = profile_content_fingerprint(db, employee)
    approved = any(
        row.status == "APPROVED"
        and row.data.get("episode_id") == employee.data.get("episode_id")
        and actor.id in row.data.get("recipient_user_ids", [])
        and row.data.get("content_fingerprint") == digest
        for row in list_kind(db, "profile_publication", parent_id=employee.id)
    )
    return asset if approved else None


def employee_photo_url(db, actor, employee):
    if actor.role not in {*EDIT, "MANAGER", "EMPLOYEE"} or not actor_can_employee(
        db, actor, employee.id
    ):
        return None
    return (
        f"/api/v1/organization/employees/{employee.id}/photo"
        if _photo_asset(db, actor, employee)
        else None
    )


def employee_projection(db, actor, employee, employees, units):
    manager = _effective_manager(db, employee, employees, units)
    unit = units.get(employee.data.get("org_unit_id"))
    if unit and (
        not _same_legal(employee, unit)
        or unit.data.get("site_id")
        and unit.data["site_id"] != employee.data.get("site_id")
    ):
        unit = None
    explicit_manager = employees.get(employee.data.get("reports_to_employee_id"))
    return {
        "id": employee.id,
        "name": employee.data.get("name", ""),
        "position": employee.data.get("position", employee.data.get("job_title", "")),
        "department": employee.data.get("department", ""),
        "legal_entity_id": employee.data.get("legal_entity_id"),
        "site_id": employee.data.get("site_id"),
        "org_unit_id": unit.id if unit else None,
        "org_unit_title": unit.data.get("title") if unit else None,
        "reports_to_employee_id": (
            employee.data.get("reports_to_employee_id")
            if explicit_manager and _same_legal(employee, explicit_manager)
            else None
        ),
        "manager_employee_id": manager if manager in employees else None,
        "profile_photo_url": (
            f"/api/v1/organization/employees/{employee.id}/photo"
            if _photo_asset(db, actor, employee)
            else None
        ),
        "version": employee.version,
        "etag": etag(employee),
        "can_edit": actor.role in EDIT and employee_is_enabled(employee) and actor_can_employee(db, actor, employee.id),
    }


def unit_projection(actor, unit, employees, units):
    head = employees.get(unit.data.get("head_employee_id"))
    return {
        "id": unit.id,
        "title": unit.data["title"],
        "type": unit.data["type"],
        "parent_id": unit.parent_id if unit.parent_id in units else None,
        "head_employee_id": (
            unit.data.get("head_employee_id")
            if head and _same_legal(unit, head)
            else None
        ),
        "legal_entity_id": unit.data.get("legal_entity_id"),
        "site_id": unit.data.get("site_id"),
        "status": unit.status,
        "version": unit.version,
        "etag": etag(unit),
        "can_edit": _unit_edit(actor, unit),
    }


def _view(db, actor):
    employees, units = _all(db)
    employees = visible_employees(db, actor, employees)
    units = {
        key: unit for key, unit in units.items() if _unit_read(actor, unit, employees)
    }
    return employees, units


@router.get("/organization")
def organization(db=Depends(get_db), actor=Depends(get_actor)):
    employees, units = _view(db, actor)
    return {
        "units": [unit_projection(actor, u, employees, units) for u in units.values()],
        "employees": [
            employee_projection(db, actor, e, employees, units)
            for e in employees.values()
        ],
        "can_edit": actor.role in EDIT,
    }


@router.get("/organization/employees/{employee_id}")
def employee_detail(employee_id: str, db=Depends(get_db), actor=Depends(get_actor)):
    employees, units = _view(db, actor)
    employee = employees.get(employee_id)
    # OFF removes a person from the directory, while scoped HR can still open
    # the existing organizational facts from their administration card.
    if employee is None and actor.role in EDIT:
        suspended = db.scalar(select(Entity).where(
            Entity.id == employee_id, Entity.kind == "employee"
        ).execution_options(populate_existing=True))
        if (suspended and suspended.data.get("enabled", True) is False
                and suspended.data.get("employment_stage") in {"PREBOARDING", "ACTIVE"}
                and suspended.status not in {"ENDED", "CANCELLED", "DELETED"}
                and actor_can_employee(db, actor, employee_id)):
            employee = suspended
            employees = {**employees, employee_id: employee}
    if not employee:
        raise HTTPException(404, "Сотрудник не найден")
    chain, seen = [], {employee.id}
    cursor = _effective_manager(db, employee, employees, units)
    while cursor and cursor in employees and cursor not in seen:
        seen.add(cursor)
        manager = employees[cursor]
        chain.append(employee_projection(db, actor, manager, employees, units))
        cursor = _effective_manager(db, manager, employees, units)
    reports = [
        employee_projection(db, actor, e, employees, units)
        for e in employees.values()
        if _effective_manager(db, e, employees, units) == employee.id
    ]
    access = {}
    if actor.role in EDIT:
        linked = list(db.scalars(select(User).where(User.employee_id == employee.id)
                                .execution_options(populate_existing=True)))
        current = next((u for u in linked if u.id == employee.data.get("user_id")), None)
        if current is None and len(linked) == 1:
            current = linked[0]
        editable = _current(employee) and actor_can_employee(db, actor, employee.id)
        access = {"access_role": current.role if current else "EMPLOYEE",
                  "can_change_access_role": bool(editable and (not linked or current and current.role in {"EMPLOYEE", "MANAGER"})
                                                  and len([u for u in linked if u.enabled]) <= 1)}
    return JSONResponse(
        {
            **employee_projection(db, actor, employee, employees, units),
            **access,
            "manager_chain": chain,
            "direct_reports": reports,
        },
        headers={"ETag": etag(employee)},
    )


@router.post("/organization/employees/{employee_id}/access-role")
def change_access_role(employee_id: str, body: AccessRole, request: Request,
                       db=Depends(get_db), actor=Depends(get_actor)):
    """Explicitly grant team Mini App rights; reporting assignments stay separate."""
    from .chat import employee_binding_lock
    from .db import new_id
    require_role(actor, *EDIT)
    employee_binding_lock(db)
    employee = get_entity(db, employee_id, "employee", lock=True)
    issuer = db.scalar(select(User).where(User.id == actor.id).with_for_update().execution_options(populate_existing=True))
    if not issuer or not issuer.enabled or issuer.role not in EDIT:
        raise HTTPException(403, "Право менять доступ изменилось")
    actor = user_actor(issuer)
    if not actor_can_employee(db, actor, employee.id):
        raise HTTPException(404, "Сотрудник не найден")
    check_etag(request, employee)
    if not _current(employee):
        raise HTTPException(409, "Доступ сотрудника выключен или найм завершён")
    linked = list(db.scalars(select(User).where(User.employee_id == employee.id).with_for_update()
                             .execution_options(populate_existing=True)))
    if len([u for u in linked if u.enabled]) > 1:
        raise HTTPException(409, "К карточке подключено несколько аккаунтов")
    user = next((u for u in linked if u.id == employee.data.get("user_id")), None)
    if user is None and len(linked) == 1:
        user = linked[0]
    if linked and user is None:
        raise HTTPException(409, "Связь с аккаунтом требует проверки")
    if user and (user.role not in {"EMPLOYEE", "MANAGER"} or is_hidden_setup(user)):
        raise HTTPException(409, "Этот аккаунт имеет отдельные права HR или администратора")
    if user is None:
        user = User(id=new_id(), name=employee.data.get("name") or "Сотрудник",
                    email=f"max-employee-{employee.id}@internal.invalid", role=body.role,
                    scope={}, employee_id=employee.id, password_hash=ph.hash(secrets.token_urlsafe(48)), enabled=True)
        db.add(user); db.flush()
    old_role = user.role
    user.role = body.role
    employee.data = {**employee.data, "user_id": user.id,
                     "miniapp_role_access": {"user_id": user.id, "episode_id": employee.data.get("episode_id"), "role": body.role}}
    db.execute(delete(SessionToken).where(SessionToken.user_id == user.id))
    audit(db, actor, "organization.employee.access_role", employee.id, {"from": old_role, "role": body.role, "user_id": user.id})
    commit(db)
    return employee_detail(employee.id, db, actor)


def _validate_scope(db, actor, values):
    legal = get_entity(db, values.get("legal_entity_id", ""), "organization")
    site_id = values.get("site_id")
    if site_id:
        site = get_entity(db, site_id, "site")
        if site.data.get("legal_entity_id") != legal.id:
            raise HTTPException(422, "Площадка не относится к выбранной организации")
    if not _grant(actor.scope, legal.id, site_id, write=True):
        raise HTTPException(403, "Подразделение вне области доступа")


def _references(actor, values, employees, units):
    parent = values.get("parent_id")
    if parent and parent not in units:
        raise HTTPException(404, "Родительское подразделение не найдено")
    head = values.get("head_employee_id")
    if head and head not in employees:
        raise HTTPException(404, "Руководитель не найден")


@router.post("/organization/units", status_code=201)
def create_unit(body: UnitCreate, db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *EDIT)
    _lock_graph(db)
    values = body.model_dump()
    employees, units = _view(db, actor)
    _validate_scope(db, actor, values)
    _references(actor, values, employees, units)
    all_employees, all_units = _all(db)
    unit = Entity(
        id=__import__("uuid").uuid4().hex,
        kind="org_unit",
        status="ACTIVE",
        parent_id=values.pop("parent_id"),
        data=values,
        version=1,
    )
    all_units[unit.id] = unit
    _validate_graph(db, all_employees, all_units, values["legal_entity_id"])
    db.add(unit)
    db.flush()
    audit(db, actor, "organization.unit.create", unit.id, {"type": values["type"]})
    commit(db)
    units[unit.id] = unit
    return JSONResponse(
        unit_projection(actor, unit, employees, units),
        status_code=201,
        headers={"ETag": etag(unit)},
    )


@router.patch("/organization/units/{unit_id}")
def patch_unit(
    unit_id: str,
    body: UnitPatch,
    request: Request,
    db=Depends(get_db),
    actor=Depends(get_actor),
):
    require_role(actor, *EDIT)
    _lock_graph(db)
    unit = get_entity(db, unit_id, "org_unit", lock=True)
    if not _unit_edit(actor, unit):
        raise HTTPException(404, "Подразделение не найдено")
    check_etag(request, unit)
    changes = body.model_dump(exclude_unset=True)
    if any(
        changes.get(key) is None
        for key in ("title", "type", "legal_entity_id", "status")
        if key in changes
    ):
        raise HTTPException(422, "Обязательные поля не могут быть пустыми")
    proposed = {
        **unit.data,
        **{k: v for k, v in changes.items() if k not in {"parent_id", "status"}},
        "parent_id": changes.get("parent_id", unit.parent_id),
    }
    employees, units = _view(db, actor)
    _validate_scope(db, actor, proposed)
    _references(
        actor,
        {
            key: changes[key]
            for key in ("parent_id", "head_employee_id")
            if key in changes
        },
        employees,
        units,
    )
    all_employees, all_units = _all(db)
    if changes.get("status") == "ARCHIVED" and (
        unit.data.get("head_employee_id")
        or any(
            u.status == "ACTIVE" and u.parent_id == unit.id for u in all_units.values()
        )
        or any(e.data.get("org_unit_id") == unit.id for e in all_employees.values())
    ):
        raise HTTPException(
            409,
            "Сначала перенесите сотрудников и дочерние подразделения и снимите руководителя",
        )
    candidate = Entity(
        id=unit.id,
        kind="org_unit",
        status=changes.get("status", unit.status),
        parent_id=proposed.pop("parent_id"),
        data=proposed,
    )
    all_units[unit.id] = candidate
    _validate_graph(db, all_employees, all_units, proposed["legal_entity_id"])
    if unit.data.get("legal_entity_id") != proposed["legal_entity_id"]:
        _validate_graph(db, all_employees, all_units, unit.data.get("legal_entity_id"))
    unit.data, unit.parent_id, unit.status = (
        proposed,
        candidate.parent_id,
        candidate.status,
    )
    unit.updated_at = now()
    db.flush()
    audit(db, actor, "organization.unit.update", unit.id, {"fields": sorted(changes)})
    commit(db)
    units[unit.id] = unit
    return JSONResponse(
        unit_projection(actor, unit, employees, units), headers={"ETag": etag(unit)}
    )


@router.patch("/organization/employees/{employee_id}")
def patch_employee(
    employee_id: str,
    body: EmployeePatch,
    request: Request,
    db=Depends(get_db),
    actor=Depends(get_actor),
):
    require_role(actor, *EDIT)
    _lock_graph(db)
    employees, units = _view(db, actor)
    if employee_id not in employees or not actor_can_employee(db, actor, employee_id):
        raise HTTPException(404, "Сотрудник не найден")
    employee = get_entity(db, employee_id, "employee", lock=True)
    check_etag(request, employee)
    changes = body.model_dump(exclude_unset=True)
    if changes.get("org_unit_id") and changes["org_unit_id"] not in units:
        raise HTTPException(404, "Подразделение не найдено")
    if (
        changes.get("reports_to_employee_id")
        and changes["reports_to_employee_id"] not in employees
    ):
        raise HTTPException(404, "Руководитель не найден")
    all_employees, all_units = _all(db)
    candidate = Entity(
        id=employee.id,
        kind="employee",
        status=employee.status,
        data={**employee.data, **changes},
    )
    all_employees[employee.id] = candidate
    _validate_graph(db, all_employees, all_units, employee.data.get("legal_entity_id"))
    employee.data = candidate.data
    employee.updated_at = now()
    db.flush()
    audit(
        db,
        actor,
        "organization.employee.assign",
        employee.id,
        {"fields": sorted(changes)},
    )
    commit(db)
    employees[employee.id] = employee
    return JSONResponse(
        employee_projection(db, actor, employee, employees, units),
        headers={"ETag": etag(employee)},
    )


@router.get("/organization/employees/{employee_id}/photo")
def employee_photo(employee_id: str, db=Depends(get_db), actor=Depends(get_actor)):
    employees, _ = _view(db, actor)
    employee = employees.get(employee_id)
    asset = _photo_asset(db, actor, employee) if employee else None
    if not asset:
        raise HTTPException(404, "Фото недоступно")
    root = Path(get_settings().blob_root).resolve()
    path = Path(asset.data.get("storage_path", "")).resolve()
    if (
        not path.is_file()
        or not path.is_relative_to(root)
        or path.stat().st_size > 10 * 1024 * 1024
    ):
        raise HTTPException(404, "Фото недоступно")
    try:
        from PIL import Image, ImageOps

        with Image.open(path) as image:
            if (
                image.format not in {"JPEG", "PNG", "WEBP"}
                or image.width * image.height > 30_000_000
            ):
                raise ValueError("invalid_image")
            image = ImageOps.exif_transpose(image)
            image.thumbnail((256, 256))
            output = io.BytesIO()
            image.convert("RGB").save(output, format="JPEG", quality=82)
    except Exception:
        raise HTTPException(404, "Фото недоступно")
    return Response(
        output.getvalue(),
        media_type="image/jpeg",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
