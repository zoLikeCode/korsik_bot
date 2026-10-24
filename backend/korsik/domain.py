"""Persisted onboarding and HR workflows. No model controls a transition."""

from __future__ import annotations
import copy
import csv
import hashlib
import io
import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from .db import get_db, now, new_id
from .models import Entity, User, Outbox, Job
from .auth import get_actor, Actor, is_hidden_setup
from .common import (
    require_role,
    get_entity,
    save_entity,
    assert_employee,
    actor_can_employee,
    audit,
    enqueue,
    etag,
    check_etag,
    serialize,
    commit,
)
from .scenario_engine import (
    validate_graph,
    semantic_hash,
    simulate,
    calculate_due,
    eligible,
    evaluate_condition,
    validate_answer,
    add_months,
    local_datetime,
    active_path,
    normalize_calendar,
    add_working_minutes,
    condition_ready,
)

router = APIRouter()
HR = ("HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT")
PUBLISH = ("HR_ADMIN",)
READ_HR = HR
TERMINAL_SURVEYS = {"COMPLETED", "DECLINED", "EXPIRED", "CANCELLED"}


def entities(db, kind, parent_id=None):
    q = select(Entity).where(Entity.kind == kind)
    if parent_id is not None:
        q = q.where(Entity.parent_id == parent_id)
    return list(db.execute(q.order_by(Entity.created_at)).scalars())


def update(e, **values):
    if all(k in e.data and e.data[k] == v for k, v in values.items()):
        return e
    e.data = {**e.data, **values}
    e.version = (e.version or 1) + 1
    e.updated_at = now()
    return e


def aware(value):
    dt = datetime.fromisoformat(value) if isinstance(value, str) else value
    return dt.replace(tzinfo=timezone.utc) if dt and dt.tzinfo is None else dt


def reporting_window(filters=None):
    filters = filters or {}
    if filters.get("_reporting_window"):
        return filters["_reporting_window"]
    name = filters.get("timezone", "Europe/Moscow")
    try:
        zone = ZoneInfo(name)
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        raise HTTPException(422, "Укажите IANA timezone отчёта")
    current = now().astimezone(zone)

    def boundary(value, upper=False):
        if not value:
            return None
        if not isinstance(value, str):
            raise HTTPException(422, "Некорректная граница периода")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=zone)
            if upper and len(value) == 10:
                parsed += timedelta(days=1)
            return parsed.astimezone(zone)
        except (ValueError, TypeError):
            raise HTTPException(422, "Некорректная граница периода")

    start = boundary(filters.get("period_from"))
    end = boundary(filters.get("period_to"), upper=True) or current
    if start and end <= start:
        raise HTTPException(422, "Конец периода должен быть после начала")
    return {
        "period_from": start.isoformat() if start else None,
        "period_to": end.isoformat(),
        "timezone": name,
        "as_of": current.isoformat(),
        "period_semantics": "FROM_INCLUSIVE_TO_EXCLUSIVE",
        "date_only_end_is_inclusive": True,
        "period_kind": "BOUNDED" if start else "ALL_AVAILABLE_UNTIL_END",
    }


def metric_context(numerator, denominator, missing_answers, window, **extra):
    return {
        "numerator": numerator,
        "denominator": denominator,
        "missing_answers": missing_answers,
        "rate": (
            numerator / denominator if denominator and numerator is not None else None
        ),
        "no_data": denominator == 0 if denominator is not None else None,
        **window,
        **extra,
    }


def pack(e, extra=None):
    value = serialize(e)
    if extra:
        value.update(extra)
    return value


def reply(e, status=200, extra=None):
    return JSONResponse(pack(e, extra), status_code=status, headers={"ETag": etag(e)})


def can_employee_drilldown(actor):
    return True


def current_employee_record(db, actor, employee_id, record):
    """A new employment account never inherits a former episode's private records."""
    if actor.role != "EMPLOYEE":
        return True
    employee = db.get(Entity, employee_id)
    episode_id = record.data.get("episode_id")
    if not episode_id and record.kind in ("scenario_question", "input_capture"):
        target = record
        if record.kind == "input_capture":
            target_id = record.data.get("survey_run_id") or record.data.get(
                "scenario_question_id"
            )
            target = db.get(Entity, target_id) if target_id else None
        if target:
            episode_id = target.data.get("episode_id")
            if not episode_id and target.kind == "scenario_question":
                journey = db.get(Entity, target.parent_id)
                episode_id = journey.data.get("episode_id") if journey else None
    return bool(
        employee
        and actor.employee_id == employee_id
        and employee.data.get("employment_stage") != "ENDED"
        and episode_id
        and episode_id == employee.data.get("episode_id")
    )


def employee_scope(db, actor):
    return [e for e in entities(db, "employee") if actor_can_employee(db, actor, e.id)]


def employee_view(employee, actor):
    value = pack(employee)
    value["enabled"] = employee.data.get("enabled", True) is not False
    if actor.role in ("MANAGER",):
        for field in (
            "profile_bio",
            "profile_photo_asset_id",
            "profile_publication_status",
            "personal_notes",
            "max_user_id",
            "max_chat_id",
        ):
            value.pop(field, None)
    return value


def journey_view(journey, actor):
    value = pack(journey)
    if actor.role in ("MANAGER", "PLATFORM_ADMIN"):
        for field in ("answers", "cancel_reason", "review_reason"):
            value.pop(field, None)
    return value


def episode_view(episode, actor):
    value = pack(episode)
    if actor.role not in HR:
        for field in ("reason", "end_reason"):
            value.pop(field, None)
    return value


def check_scope_site(actor, site_id, db=None):
    scope = actor.scope or {}
    if actor.role not in READ_HR + ("MANAGER",):
        raise HTTPException(403, "Нет доступа к площадке")
    if actor.role == "MANAGER":
        if db and any(
            e.data.get("site_id") == site_id and e.data.get("manager_id") == actor.id
            for e in entities(db, "employee")
        ):
            return
        raise HTTPException(403, "Площадка вне вашей команды")
    if scope.get("all") is True:
        return
    if scope.get("site_ids") and site_id not in scope["site_ids"]:
        raise HTTPException(403, "Площадка вне вашей области доступа")
    site = db.get(Entity, site_id) if db else None
    if scope.get("legal_entity_ids") and (
        not site or site.data.get("legal_entity_id") not in scope["legal_entity_ids"]
    ):
        raise HTTPException(403, "Юрлицо вне вашей области доступа")
    if not (scope.get("site_ids") or scope.get("legal_entity_ids")):
        raise HTTPException(
            403, "Для создания или переноса нужна область площадки/юрлица"
        )


def conversation_for(db, employee_id):
    employee = db.get(Entity, employee_id)
    episode_id = employee.data.get("episode_id") if employee else None
    return next(
        (
            c
            for c in entities(db, "conversation")
            if c.status != "CLOSED"
            and c.data.get("employee_id") == employee_id
            and c.data.get("episode_id", episode_id) == episode_id
        ),
        None,
    )


def emit(db, employee_id, text, buttons=None, unique_key=None, data=None):
    employee = get_entity(db, employee_id, "employee")
    data = {
        "domain_generated": True,
        "employee_id": employee_id,
        "episode_id": employee.data.get("episode_id"),
        **(data or {}),
    }
    unique_key = unique_key or "domain:" + new_id()
    old = db.execute(
        select(Outbox).where(Outbox.unique_key == unique_key)
    ).scalar_one_or_none()
    if old:
        return old
    o = Outbox(
        id=new_id(),
        recipient_kind="EMPLOYEE",
        recipient_id=employee_id,
        text=text,
        attachments=[],
        callback_buttons=buttons or [],
        status="PENDING",
        unique_key=unique_key,
        created_at=now(),
        data=data or {},
    )
    db.add(o)
    db.flush()
    if data:
        save_entity(
            db, "outbox_domain_context", {"outbox_id": o.id, **data}, parent_id=o.id
        )
    return o


def render_text(content, employee):
    field = "text_vy" if employee.data.get("address_form", "VY") == "VY" else "text_ty"
    text = content.get(field) or content.get("text_ty") or content.get("text_vy") or ""
    values = {
        **employee.data,
        "preferred_name": employee.data.get("preferred_name")
        or employee.data.get("name", "коллега"),
    }
    for key in (
        "name",
        "preferred_name",
        "site_name",
        "start_date",
        "probation_end_date",
        "hr_name",
        "manager_name",
    ):
        text = text.replace("{{" + key + "}}", str(values.get(key, "")))
    # Old published scenarios retain their audited content. Retire only the
    # former welcome CTA when rendering it for an employee.
    return text.replace(
        "найти информацию во внутренних документах и связаться с HR.",
        "найти информацию во внутренних документах.",
    )


def event(db, kind, parent_id, event_type, actor_id=None, **data):
    return save_entity(
        db,
        kind,
        {
            "event_type": event_type,
            "actor_id": actor_id,
            "occurred_at": now().isoformat(),
            **data,
        },
        parent_id=parent_id,
    )


def calendar_for(db, employee):
    site = db.get(Entity, employee.data.get("site_id"))
    cid = employee.data.get("calendar_id") or (
        site.data.get("calendar_id") if site else None
    )
    cal = db.get(Entity, cid) if cid else None
    version = db.get(Entity, cal.data.get("active_version_id")) if cal else None
    return (
        version.data
        if version
        else {
            "configuration_error": "NEEDS_CALENDAR_REVIEW: сотруднику не назначен рабочий календарь",
            "working_weekdays": [],
        }
    )


def visible_sites(db, actor):
    if actor.role not in READ_HR + ("MANAGER",):
        return []
    employee_sites = {e.data.get("site_id") for e in employee_scope(db, actor)}
    rows = []
    for site in entities(db, "site"):
        if site.id in employee_sites:
            rows.append(site)
            continue
        try:
            check_scope_site(actor, site.id, db)
            rows.append(site)
        except HTTPException:
            pass
    return rows


def directory_user_rows(db, actor):
    cards = employee_scope(db, actor) if can_employee_drilldown(actor) else []
    ids = {actor.id} | {
        e.data.get(field)
        for e in cards
        for field in ("manager_id", "assigned_hr_id", "user_id")
    }
    from .common import can_delegate_scope

    rows = []
    for user in db.scalars(select(User).where(User.enabled == True)):
        if is_hidden_setup(user):
            continue
        permitted = user.id in ids
        if actor.role in HR:
            permitted = permitted or (
                user.role in READ_HR + ("MANAGER",)
                and (
                    actor.scope.get("all")
                    or can_delegate_scope(actor.scope, user.scope or {})
                    or can_delegate_scope(user.scope or {}, actor.scope)
                )
            )
        if not permitted:
            continue
        item = {"id": user.id, "name": user.name, "role": user.role}
        if user.id == actor.id or (actor.role == "HR_ADMIN" and actor.scope.get("all")):
            item["email"] = user.email
        rows.append(item)
    return rows


def verified_hr_contact(db, employee):
    user_id = employee.data.get("assigned_hr_id")
    user = db.get(User, user_id) if user_id else None
    if not user or not user.enabled or is_hidden_setup(user) or user.role not in HR:
        return {"available": False, "reason": "NO_ACTIVE_ASSIGNED_HR"}
    who = Actor(user.id, user.name, user.role, user.scope, user.employee_id)
    if not actor_can_employee(db, who, employee.id):
        return {"available": False, "reason": "ASSIGNED_HR_SCOPE_REVOKED"}
    contact = next(
        (c for c in entities(db, "hr_contact", user.id) if c.status == "VERIFIED"), None
    )
    if not contact:
        return {"available": False, "reason": "CONTACT_NOT_VERIFIED"}
    return {
        "available": True,
        "label": user.name,
        "url": contact.data["url"],
        "contact_id": contact.id,
        "contact_version": contact.version,
        "user_id": user.id,
    }


@router.get("/employees/{employee_id}/hr-contact")
def employee_hr_contact(employee_id: str, db=Depends(get_db), actor=Depends(get_actor)):
    employee = assert_employee(db, actor, employee_id)
    if employee.data.get("employment_stage") == "ENDED":
        raise HTTPException(404, "Доступ к контакту завершён")
    return verified_hr_contact(db, employee)


@router.get("/users/{user_id}/hr-contact")
def hr_contact_get(user_id: str, db=Depends(get_db), actor=Depends(get_actor)):
    from .auth_admin import target

    require_role(actor, "HR_ADMIN")
    user = target(db, actor, user_id)
    if user.role not in HR:
        raise HTTPException(422, "Контакт настраивается для HR пользователя")
    row = next(iter(entities(db, "hr_contact", user.id)), None)
    return reply(row) if row else {"user_id": user.id, "status": "NOT_CONFIGURED"}


@router.put("/users/{user_id}/hr-contact")
def hr_contact_put(
    user_id: str,
    payload: dict,
    request: Request,
    db=Depends(get_db),
    actor=Depends(get_actor),
):
    from urllib.parse import urlsplit
    from .auth_admin import target

    require_role(actor, "HR_ADMIN")
    user = target(db, actor, user_id, True)
    if user.role not in HR or not user.enabled:
        raise HTTPException(422, "Выберите действующего HR пользователя")
    if (
        set(payload) - {"url", "verified", "reason"}
        or len(str(payload.get("reason", ""))) < 3
    ):
        raise HTTPException(422, "Укажите ссылку и основание проверки контакта")
    url = payload.get("url")
    if not isinstance(url, str) or len(url) > 2048:
        raise HTTPException(422, "Некорректная ссылка MAX")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise HTTPException(422, "Некорректная ссылка MAX")
    if (
        parsed.scheme != "https"
        or parsed.hostname != "max.ru"
        or parsed.username
        or parsed.password
        or port not in (None, 443)
        or not parsed.path.strip("/")
        or any(c in url for c in ("\n", "\r", "\t"))
    ):
        raise HTTPException(
            422, "Нужна HTTPS ссылка профиля на max.ru без встроенных учётных данных"
        )
    if payload.get("verified") is not True:
        raise HTTPException(
            422, "Администратор должен подтвердить контакт по известному каналу"
        )
    row = next(iter(entities(db, "hr_contact", user.id)), None)
    if row:
        row = get_entity(db, row.id, "hr_contact", lock=True)
        check_etag(request, row)
        previous = {
            "url": row.data.get("url"),
            "verified_at": row.data.get("verified_at"),
        }
        update(
            row,
            url=url,
            verified_by=actor.id,
            verified_at=now().isoformat(),
            verification_reason=str(payload["reason"])[:2000],
        )
        row.status = "VERIFIED"
    else:
        previous = None
        row = save_entity(
            db,
            "hr_contact",
            {
                "user_id": user.id,
                "url": url,
                "channel": "MAX",
                "verified_by": actor.id,
                "verified_at": now().isoformat(),
                "verification_reason": str(payload["reason"])[:2000],
            },
            status="VERIFIED",
            parent_id=user.id,
            owner_id=actor.id,
        )
    event(
        db,
        "hr_contact_event",
        row.id,
        "ADMIN_VERIFIED",
        actor.id,
        previous=previous,
        reason=str(payload["reason"])[:2000],
    )
    audit(db, actor, "hr.contact.verify", row.id, reason=payload.get("reason"))
    commit(db)
    return reply(row)


@router.delete("/users/{user_id}/hr-contact")
def hr_contact_revoke(
    user_id: str, request: Request, db=Depends(get_db), actor=Depends(get_actor)
):
    from .auth_admin import target

    require_role(actor, "HR_ADMIN")
    target(db, actor, user_id, True)
    row = next(iter(entities(db, "hr_contact", user_id)), None)
    if not row:
        raise HTTPException(404, "Контакт не найден")
    check_etag(request, row)
    row.status = "REVOKED"
    update(row, revoked_by=actor.id, revoked_at=now().isoformat())
    audit(
        db, actor, "hr.contact.revoke", row.id, {"reason_code": "ADMIN_REVOKED_CONTACT"}
    )
    commit(db)
    return reply(row)


@router.get("/directory")
def directory(db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *READ_HR, "MANAGER")
    return {
        "sites": [pack(e) for e in visible_sites(db, actor)],
        "legal_entities": legal_entities(db, actor)["items"],
        "users": directory_user_rows(db, actor),
    }


@router.get("/sites")
def list_sites(db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *READ_HR, "MANAGER")
    return {"items": [pack(e) for e in visible_sites(db, actor)]}


@router.post("/sites")
def create_site(payload: dict, db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *PUBLISH)
    if not payload.get("name"):
        raise HTTPException(422, "Укажите название площадки")
    if not actor.scope.get("all"):
        if actor.scope.get("site_ids") or payload.get(
            "legal_entity_id"
        ) not in actor.scope.get("legal_entity_ids", []):
            raise HTTPException(
                403,
                "Новую площадку создаёт администратор всего контура или своего юрлица",
            )
    if payload.get("legal_entity_id"):
        get_entity(db, payload["legal_entity_id"], "organization")
    try:
        ZoneInfo(payload.get("timezone", "Europe/Moscow"))
    except (ZoneInfoNotFoundError, TypeError):
        raise HTTPException(422, "Неизвестный часовой пояс")
    e = save_entity(db, "site", payload)
    audit(db, actor, "site.create", e.id)
    commit(db)
    return reply(e, 201)


@router.patch("/sites/{site_id}")
def change_site(
    site_id: str,
    payload: dict,
    request: Request,
    db=Depends(get_db),
    actor=Depends(get_actor),
):
    require_role(actor, *PUBLISH)
    e = get_entity(db, site_id, "site", lock=True)
    check_scope_site(actor, e.id, db)
    check_etag(request, e)
    if payload.get("legal_entity_id"):
        get_entity(db, payload["legal_entity_id"], "organization")
        if (
            not actor.scope.get("all")
            and actor.scope.get("legal_entity_ids")
            and payload["legal_entity_id"] not in actor.scope["legal_entity_ids"]
        ):
            raise HTTPException(403, "Нельзя расширить область юрлица")
        if payload["legal_entity_id"] != e.data.get("legal_entity_id") and any(
            employee.data.get("site_id") == e.id
            for employee in entities(db, "employee")
        ):
            raise HTTPException(
                409,
                "Для смены юрлица действующих сотрудников используйте перенос карточек с preview",
            )
    update(e, **payload)
    audit(db, actor, "site.update", e.id)
    commit(db)
    return reply(e)


@router.get("/employees")
def list_employees(
    db=Depends(get_db),
    actor=Depends(get_actor),
    page: int | None = None,
    page_size: int | None = None,
    search: str | None = None,
    stage: str | None = None,
):
    require_role(actor, *READ_HR, "MANAGER")
    if not can_employee_drilldown(actor):
        raise HTTPException(
            403, "Для персонального списка нужен отдельный drilldown grant"
        )
    rows = employee_scope(db, actor)
    if page is None and page_size is None:
        return {"items": [employee_view(e, actor) for e in rows]}
    page = 1 if page is None else page
    page_size = 10 if page_size is None else page_size
    if not 1 <= page <= 1_000_000 or not 1 <= page_size <= 100:
        raise HTTPException(
            422, "Номер страницы положительный; размер страницы — от 1 до 100"
        )
    if stage and stage not in {"PREBOARDING", "ACTIVE", "ENDED"}:
        raise HTTPException(422, "Неизвестная стадия найма")
    query = (search or "").strip().casefold()
    if len(query) > 255:
        raise HTTPException(422, "Поисковый запрос слишком длинный")
    rows = [
        e
        for e in rows
        if (not stage or e.data.get("employment_stage") == stage)
        and (
            not query
            or query
            in " ".join(
                str(e.data.get(key) or "")
                for key in ("name", "email", "department", "position")
            ).casefold()
        )
    ]
    rows.sort(key=lambda e: (e.created_at, e.id), reverse=True)
    total = len(rows)
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, pages)
    return {
        "items": [
            employee_view(e, actor)
            for e in rows[(page - 1) * page_size : page * page_size]
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
    }


def default_employee_site(db, actor):
    eligible = []
    for site in entities(db, "site"):
        if site.status == "ARCHIVED":
            continue
        try:
            check_scope_site(actor, site.id, db)
            eligible.append(site)
        except HTTPException:
            continue
    if len(eligible) == 1:
        return eligible[0].id
    demo = next((site for site in eligible if site.id == "GrPP"), None)
    if demo:
        return demo.id
    used = {e.data.get("site_id") for e in employee_scope(db, actor)}
    candidates = [site for site in eligible if site.id in used]
    if len(candidates) == 1:
        return candidates[0].id
    raise HTTPException(
        409,
        "Для вашего доступа не определена единственная площадка создания сотрудника",
    )


def create_employee_record(db, actor, payload):
    require_role(actor, *HR)
    payload = copy.deepcopy(payload)
    simplified = not payload.get("site_id")
    payload["site_id"] = payload.get("site_id") or default_employee_site(db, actor)
    if simplified and "assigned_hr_id" not in payload:
        payload["assigned_hr_id"] = actor.id
    if (
        not isinstance(payload.get("name"), str)
        or not 1 <= len(payload["name"].strip()) <= 255
    ):
        raise HTTPException(422, "Имя должно содержать 1–255 символов")
    if (
        not payload.get("name")
        or not payload.get("site_id")
        or not payload.get("start_date")
    ):
        raise HTTPException(422, "Нужны имя, площадка и дата выхода")
    site = get_entity(db, payload["site_id"], "site")
    check_scope_site(actor, payload["site_id"], db)
    if (
        payload.get("legal_entity_id")
        and site.data.get("legal_entity_id")
        and payload["legal_entity_id"] != site.data["legal_entity_id"]
    ):
        raise HTTPException(422, "Юрлицо не соответствует площадке")
    for field, roles in [("manager_id", ("MANAGER",)), ("assigned_hr_id", HR)]:
        if payload.get(field):
            user = db.get(User, payload[field])
            if not user or user.role not in roles or not user.enabled:
                raise HTTPException(
                    422, "Назначенный пользователь не найден или имеет другую роль"
                )
    try:
        start = date.fromisoformat(payload["start_date"])
        ZoneInfo(payload.get("timezone", "Europe/Moscow"))
        end = date.fromisoformat(
            payload.get("probation_end_date")
            or (add_months(start, 3) - timedelta(days=1)).isoformat()
        )
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        raise HTTPException(422, "Неверная дата или часовой пояс")
    if end < start:
        raise HTTPException(422, "Конец испытательного срока раньше выхода")
    external = payload.get("external_employee_id")
    if external and any(
        e.data.get("external_employee_id") == external for e in entities(db, "employee")
    ):
        raise HTTPException(409, "Сотрудник с таким внешним ID уже есть")
    allowed = {
        "name",
        "site_id",
        "start_date",
        "probation_end_date",
        "employment_type",
        "timezone",
        "assigned_hr_id",
        "manager_id",
        "external_employee_id",
        "legal_entity_id",
        "department",
        "email",
    }
    data = {k: v for k, v in payload.items() if k in allowed}
    data.setdefault("legal_entity_id", site.data.get("legal_entity_id"))
    data.update(
        enabled=True,
        probation_end_date=end.isoformat(),
        employment_type=payload.get("employment_type", "OFFICE"),
        employment_stage="PREBOARDING",
        timezone=payload.get("timezone", "Europe/Moscow"),
        max_binding_status="UNBOUND",
    )
    if data["employment_type"] not in ("OFFICE", "PRODUCTION"):
        raise HTTPException(422, "Тип сотрудника OFFICE или PRODUCTION")
    e = save_entity(db, "employee", data, status="ACTIVE")
    update(e, employee_id=e.id)
    ep = save_entity(
        db,
        "employment_episode",
        {
            "employee_id": e.id,
            "start_date": data["start_date"],
            "probation_end_date": data["probation_end_date"],
            "stage": "PREBOARDING",
        },
        parent_id=e.id,
        status="PREBOARDING",
    )
    update(e, episode_id=ep.id)
    enroll_employee(db, e, payload.get("scenario_id"))
    audit(db, actor, "employee.create", e.id)
    return e


@router.post("/employees")
def create_employee(payload: dict, db=Depends(get_db), actor=Depends(get_actor)):
    e = create_employee_record(db, actor, payload)
    commit(db)
    return reply(e, 201)


@router.get("/employees/{employee_id}")
def get_employee(employee_id: str, db=Depends(get_db), actor=Depends(get_actor)):
    if not can_employee_drilldown(actor):
        raise HTTPException(404, "Карточка недоступна")
    assert_employee(db, actor, employee_id)
    e = get_entity(db, employee_id, "employee")
    return JSONResponse(employee_view(e, actor), headers={"ETag": etag(e)})


@router.patch("/employees/{employee_id}")
def patch_employee(
    employee_id: str,
    payload: dict,
    request: Request,
    db=Depends(get_db),
    actor=Depends(get_actor),
):
    require_role(actor, *HR)
    assert_employee(db, actor, employee_id)
    e = get_entity(db, employee_id, "employee", lock=True)
    check_etag(request, e)
    allowed = {
        "name",
        "preferred_name",
        "manager_id",
        "assigned_hr_id",
        "department",
        "email",
    }
    if any(k not in allowed for k in payload):
        raise HTTPException(
            422, "Изменение дат/площадки/стадии выполняется через preview изменений"
        )
    update(e, **payload)
    audit(db, actor, "employee.update", e.id)
    commit(db)
    return reply(e)


def _suspend_employee_access(db, employee, *, skip_busy_deliveries=False):
    from .models import TransportDelivery

    episode = employee.data.get("episode_id")
    for journey in entities(db, "journey", employee.id):
        if journey.data.get("episode_id") == episode and journey.status in {
            "SCHEDULED",
            "ACTIVE",
        }:
            previous = journey.status
            journey.status = "PAUSED"
            update(
                journey,
                access_suspended=True,
                access_previous_status=previous,
                access_suspended_at=now().isoformat(),
            )
    outbound_query = select(Outbox).where(
        Outbox.recipient_kind == "EMPLOYEE", Outbox.recipient_id == employee.id
    )
    if skip_busy_deliveries:
        # Queue preparation locks Outbox before the employee. Deletion already
        # owns the employee; never wait in the inverse order. The permanent
        # deletion cutoff also fences these occupied rows after restoration.
        outbound_query = outbound_query.where(Outbox.status.in_([
            "PENDING", "READY", "RETRY", "QUEUED", "WAITING_BINDING",
        ])).with_for_update(skip_locked=True)
    for outbound in db.scalars(outbound_query):
        if outbound.status not in {
            "PENDING",
            "READY",
            "RETRY",
            "QUEUED",
            "WAITING_BINDING",
        }:
            continue
        outbound.status = "SUPPRESSED"
        outbound.data = {**outbound.data, "suppression_reason": "EMPLOYEE_DISABLED"}
        delivery_query = select(TransportDelivery).where(
            TransportDelivery.outbox_id == outbound.id,
            TransportDelivery.status.in_(["PENDING", "RETRY"]),
        )
        if skip_busy_deliveries:
            delivery_query = delivery_query.with_for_update(skip_locked=True)
        for delivery in db.scalars(delivery_query):
            delivery.status = "SUPPRESSED"
            delivery.error_code = "EMPLOYEE_DISABLED"
        action_id = outbound.data.get("action_id")
        action = db.get(Entity, action_id) if action_id else None
        journey = (
            db.get(Entity, outbound.data.get("journey_id"))
            if outbound.data.get("journey_id")
            else None
        )
        if action and journey and action.status == "ENQUEUED":
            version = db.get(Entity, journey.data.get("version_id"))
            if version and version.data.get("graph", {}).get("editor_mode") == "SIMPLE":
                action.status = "PLANNED"
                update(action, access_resume_current=True)
            else:
                action.status = "SKIPPED"
                update(
                    action, skip_reason="ACCESS_DISABLED", skipped_at=now().isoformat()
                )
                update(
                    journey,
                    events={
                        **journey.data.get("events", {}),
                        action.data["step_key"] + ":STEP_COMPLETED": now().isoformat(),
                    },
                )
    for answer in entities(db, "answer"):
        if answer.data.get("employee_id") == employee.id and answer.status in {
            "QUEUED",
            "RUNNING",
        }:
            answer.status = "CANCELLED"
            update(answer, reason_code="EMPLOYEE_DISABLED")


def skip_access_milestone(db, journey, action, clock):
    """Consume a missed timed step without claiming an employee saw its message."""
    action.status = "SKIPPED"
    update(action, skip_reason="ACCESS_DISABLED_INTERVAL", skipped_at=clock.isoformat())
    update(
        journey,
        events={
            **journey.data.get("events", {}),
            action.data["step_key"] + ":STEP_COMPLETED": clock.isoformat(),
        },
    )


@router.patch("/employees/{employee_id}/administration")
def employee_administration(
    employee_id: str,
    payload: dict,
    request: Request,
    db=Depends(get_db),
    actor=Depends(get_actor),
):
    require_role(actor, *HR)
    employee = get_entity(db, employee_id, "employee", lock=True)
    assert_employee(db, actor, employee.id)
    check_etag(request, employee)
    allowed = {"enabled", "start_date", "probation_end_date", "timezone"}
    if not payload or set(payload) - allowed:
        raise HTTPException(422, "Разрешены статус доступа, даты и часовой пояс")
    if "enabled" in payload and type(payload["enabled"]) is not bool:
        raise HTTPException(422, "Статус доступа должен быть логическим значением")
    if employee.data.get("employment_stage") == "ENDED":
        raise HTTPException(
            409, "Период найма завершён; доступ восстанавливается через повторный найм"
        )
    merged = {**employee.data, **payload}
    try:
        start = date.fromisoformat(merged["start_date"])
        end = date.fromisoformat(merged["probation_end_date"])
        ZoneInfo(merged.get("timezone", "Europe/Moscow"))
        if end < start:
            raise ValueError()
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        raise HTTPException(422, "Проверьте даты и часовой пояс")
    was_enabled = employee.data.get("enabled", True) is not False
    is_enabled = payload.get("enabled", was_enabled)
    dates_changed = any(
        key in payload and payload[key] != employee.data.get(key)
        for key in ("start_date", "probation_end_date", "timezone")
    )
    update(employee, **payload)
    if was_enabled and not is_enabled:
        update(employee, access_disabled_at=now().isoformat())
        _suspend_employee_access(db, employee)
    if not was_enabled and is_enabled:
        update(employee, access_enabled_at=now().isoformat())
    sync_employment_episode(db, employee)
    for journey in entities(db, "journey", employee.id):
        if journey.data.get("episode_id") != employee.data.get(
            "episode_id"
        ) or journey.status in {"COMPLETED", "CANCELLED"}:
            continue
        restoring = (
            not was_enabled and is_enabled and journey.data.get("access_suspended")
        )
        if restoring:
            journey.status = journey.data.get("access_previous_status", "SCHEDULED")
            update(
                journey,
                access_suspended=False,
                access_resumed_at=now().isoformat(),
                last_reminder_at=now().isoformat(),
            )
        if dates_changed or restoring:
            replan_journey(db, journey, employee, bump_revision=True)
    event(
        db,
        "employee_event",
        employee.id,
        "ADMINISTRATION_UPDATED",
        actor.id,
        fields=sorted(payload),
        enabled=is_enabled,
    )
    audit(
        db, actor, "employee.administration", employee.id, {"fields": sorted(payload)}
    )
    commit(db)
    return reply(employee, extra={"enabled": is_enabled})


@router.delete("/employees/{employee_id}")
def delete_employee(
    employee_id: str,
    request: Request,
    db=Depends(get_db),
    actor=Depends(get_actor),
):
    """Hide an employment card and revoke access without deleting its history."""
    from sqlalchemy import delete
    from .chat import employee_binding_lock
    from .employment_stages import cancel_pending
    from .models import SessionToken

    require_role(actor, *HR)
    # Same order as invitation issue/redeem: shared channel lock, employee row.
    # A redeemed invitation either finishes first and is revoked here, or sees
    # the committed deletion; it can never re-enable the deleted card.
    employee_binding_lock(db)
    employee = get_entity(db, employee_id, "employee", lock=True)
    if not actor_can_employee(db, actor, employee.id, include_deleted=True):
        raise HTTPException(404, "Сотрудник не найден")
    check_etag(request, employee)
    if employee.status == "DELETED":
        raise HTTPException(409, "Сотрудник уже удалён")
    previous_status = employee.status
    episode_id = employee.data.get("episode_id")
    cancel_pending(db, employee, "EMPLOYEE_DELETED", actor)
    update(
        employee,
        enabled=False,
        deleted_at=now().isoformat(),
        deleted_by=actor.id,
        deleted_episode_id=episode_id,
        deletion_previous_status=previous_status,
        access_disabled_at=now().isoformat(),
        max_binding_status="REVOKED",
        binding_id=None,
        pending_binding_id=None,
    )
    employee.status = "DELETED"
    _suspend_employee_access(db, employee, skip_busy_deliveries=True)
    for kind in ("employee_invite", "max_binding"):
        for row in entities(db, kind, employee.id):
            if row.status not in {"REVOKED", "EXPIRED", "REJECTED", "SUPERSEDED"}:
                row.status = "REVOKED"
                update(row, revoked_at=now().isoformat(), revoke_reason="EMPLOYEE_DELETED")
    for row in entities(db, "input_capture", employee.id):
        if row.status in {"OPEN", "DRAFT", "READY_TO_CONFIRM"}:
            row.status = "CANCELLED"
            update(row, cancelled_reason="EMPLOYEE_DELETED")
    for callback in entities(db, "callback_action"):
        if (callback.status == "ACTIVE"
                and callback.data.get("recipient_kind") == "EMPLOYEE"
                and callback.data.get("recipient_id") == employee.id):
            callback.status = "REVOKED"
            update(callback, revoke_reason="EMPLOYEE_DELETED")
    # Employment removal is separate from principal HR administration. Preserve
    # HR users/roles/channels; the linked employee/manager sessions are revoked.
    for user in db.scalars(select(User).where(
        User.employee_id == employee.id, User.role.in_(["EMPLOYEE", "MANAGER"])
    )):
        db.execute(delete(SessionToken).where(SessionToken.user_id == user.id))
        for binding in entities(db, "principal_binding"):
            if binding.owner_id == user.id and binding.status not in {"REVOKED", "EXPIRED"}:
                binding.status = "REVOKED"
                update(binding, revoke_reason="EMPLOYEE_DELETED")
    event(db, "employee_event", employee.id, "DELETED", actor.id, episode_id=episode_id)
    audit(db, actor, "employee.delete", employee.id, {"episode_id": episode_id})
    commit(db)
    return reply(employee, extra={"enabled": False})


@router.post("/employees/{employee_id}/restore")
def restore_employee(
    employee_id: str,
    request: Request,
    db=Depends(get_db),
    actor=Depends(get_actor),
):
    """Recover the card as OFF; old links and sessions remain revoked."""
    from .chat import employee_binding_lock

    require_role(actor, *HR)
    employee_binding_lock(db)
    employee = get_entity(db, employee_id, "employee", lock=True)
    if not actor_can_employee(db, actor, employee.id, include_deleted=True):
        raise HTTPException(404, "Сотрудник не найден")
    check_etag(request, employee)
    if employee.status != "DELETED":
        raise HTTPException(409, "Сотрудник не удалён")
    if employee.data.get("deleted_episode_id") != employee.data.get("episode_id"):
        raise HTTPException(409, "Период найма изменился; восстановление требует проверки HR")
    previous = employee.data.get("deletion_previous_status") or "ACTIVE"
    employee.status = "ENDED" if employee.data.get("employment_stage") == "ENDED" else previous
    update(employee, enabled=False, restored_at=now().isoformat(), restored_by=actor.id)
    event(db, "employee_event", employee.id, "RESTORED_OFF", actor.id,
          episode_id=employee.data.get("episode_id"))
    audit(db, actor, "employee.restore", employee.id,
          {"episode_id": employee.data.get("episode_id"), "enabled": False})
    commit(db)
    return reply(employee, extra={"enabled": False})


@router.get("/employees/{employee_id}/employment-episodes")
def employee_episodes(employee_id: str, db=Depends(get_db), actor=Depends(get_actor)):
    if not can_employee_drilldown(actor):
        raise HTTPException(
            404, "Для персонального просмотра нужен отдельный drilldown grant"
        )
    employee = assert_employee(db, actor, employee_id)
    return {
        "items": [
            episode_view(e, actor)
            for e in entities(db, "employment_episode", employee_id)
            if actor.role != "EMPLOYEE"
            or (
                employee.data.get("employment_stage") != "ENDED"
                and e.id == employee.data.get("episode_id")
            )
        ]
    }


def normalize_employee_change(payload):
    action = payload.get("action")
    changes = (
        copy.deepcopy(payload.get("changes", {}))
        if action
        else copy.deepcopy(payload.get("changes", payload))
    )
    aliases = {
        "new_start_date": "start_date",
        "new_site_id": "site_id",
        "new_legal_entity_id": "legal_entity_id",
        "new_probation_end_date": "probation_end_date",
    }
    for source, dest in aliases.items():
        if source in payload:
            changes[dest] = payload[source]
    if action == "OFFBOARD":
        changes["employment_stage"] = "ENDED"
    elif action == "REHIRE":
        changes["employment_stage"] = "PREBOARDING"
        if not changes.get("start_date") or not changes.get("site_id"):
            raise HTTPException(
                422, "Для повторного найма нужны новые дата выхода и площадка"
            )
        if not changes.get("probation_end_date"):
            try:
                changes["probation_end_date"] = (
                    add_months(date.fromisoformat(changes["start_date"]), 3)
                    - timedelta(days=1)
                ).isoformat()
            except (ValueError, TypeError):
                raise HTTPException(422, "Неверная дата выхода")
    elif action not in (None, "CHANGE_ANCHOR", "TRANSFER_SITE"):
        raise HTTPException(422, "Неизвестное изменение периода найма")
    return action, changes


def end_employee_access(db, employee, actor, reason):
    """Terminate future access, preserving immutable employment and delivery history."""
    from sqlalchemy import delete
    from .models import SessionToken
    from .employment_stages import cancel_pending

    cancel_pending(db, employee, "EMPLOYMENT_ENDED", actor, reason)

    episode_id = employee.data.get("episode_id")
    sync_employment_episode(db, employee)
    episode = db.get(Entity, episode_id) if episode_id else None
    if episode:
        update(episode, ended_at=now().isoformat(), end_reason=reason)
    for journey in entities(db, "journey", employee.id):
        if journey.data.get("episode_id") == episode_id and journey.status not in (
            "COMPLETED",
            "CANCELLED",
        ):
            cancel_journey(db, journey, "EMPLOYMENT_ENDED")
    for kind in ("employee_invite", "max_binding"):
        for row in entities(db, kind, employee.id):
            if row.status not in ("REVOKED", "EXPIRED", "REJECTED"):
                row.status = "REVOKED"
                update(
                    row, revoked_at=now().isoformat(), revoke_reason="EMPLOYMENT_ENDED"
                )
    for capture in entities(db, "input_capture", employee.id):
        if capture.status in ("OPEN", "DRAFT"):
            capture.status = "CANCELLED"
    for conv in entities(db, "conversation", employee.id):
        if conv.data.get("episode_id") == episode_id and conv.status != "CLOSED":
            conv.status = "CLOSED"
            update(
                conv,
                closed_at=now().isoformat(),
                close_reason="EMPLOYMENT_ENDED",
                mode="CLOSED",
                state_seq=conv.data.get("state_seq", 0) + 1,
            )
    for action in entities(db, "callback_action"):
        if (
            action.data.get("recipient_kind") == "EMPLOYEE"
            and action.data.get("recipient_id") == employee.id
            and action.status == "ACTIVE"
        ):
            action.status = "REVOKED"
    for out in db.scalars(
        select(Outbox).where(
            Outbox.recipient_kind == "EMPLOYEE", Outbox.recipient_id == employee.id
        )
    ):
        if out.status in ("PENDING", "WAITING_BINDING", "QUEUED", "SENDING"):
            out.status = "SUPPRESSED"
            out.data = {**out.data, "suppression_reason": "EMPLOYMENT_ENDED"}
    for user in db.scalars(
        select(User).where(User.employee_id == employee.id, User.role == "EMPLOYEE")
    ):
        user.enabled = False
        db.execute(delete(SessionToken).where(SessionToken.user_id == user.id))
        for binding in entities(db, "principal_binding"):
            if binding.owner_id == user.id and binding.status not in (
                "REVOKED",
                "EXPIRED",
            ):
                binding.status = "REVOKED"
    update(
        employee, max_binding_status="REVOKED", binding_id=None, pending_binding_id=None
    )
    event(
        db,
        "employee_event",
        employee.id,
        "ACCESS_REVOKED",
        actor.id,
        episode_id=episode_id,
        reason=reason,
    )


def rehire_employee_record(db, employee, changes, actor, reason, scenario_id=None):
    old_episode = db.get(Entity, employee.data.get("episode_id"))
    if (
        employee.data.get("employment_stage") != "ENDED"
        or not old_episode
        or old_episode.status != "ENDED"
    ):
        raise HTTPException(409, "Сначала завершите текущий период найма")
    from .employment_stages import cancel_pending

    cancel_pending(db, employee, "EMPLOYMENT_REHIRED", actor, reason)
    site = get_entity(db, changes["site_id"], "site")
    check_scope_site(actor, site.id, db)
    data = {
        **employee.data,
        **changes,
        "legal_entity_id": site.data.get("legal_entity_id"),
        "employment_stage": "PREBOARDING",
    }
    for key in (
        "binding_id",
        "pending_binding_id",
        "profile_bio",
        "profile_photo_asset_id",
        "profile_publication_status",
        "profile_publication_id",
        "preferred_name",
        "address_form",
    ):
        data.pop(key, None)
    data["max_binding_status"] = "UNBOUND"
    # Account access is renewed separately by HR after checking the identity.
    data.pop("user_id", None)
    employee.data = data
    employee.status = "ACTIVE"
    episode = save_entity(
        db,
        "employment_episode",
        {
            "employee_id": employee.id,
            "previous_episode_id": old_episode.id,
            "start_date": data["start_date"],
            "probation_end_date": data["probation_end_date"],
            "site_id": data["site_id"],
            "legal_entity_id": data.get("legal_entity_id"),
            "stage": "PREBOARDING",
            "reason": reason,
        },
        status="PREBOARDING",
        parent_id=employee.id,
    )
    update(employee, episode_id=episode.id)
    journey = enroll_employee(db, employee, scenario_id)
    if not journey:
        raise HTTPException(409, "Для нового периода нужна опубликованная программа")
    from .chat import conversation_for as canonical_conversation

    canonical_conversation(db, employee)
    event(
        db,
        "employee_event",
        employee.id,
        "REHIRED",
        actor.id,
        previous_episode_id=old_episode.id,
        episode_id=episode.id,
        reason=reason,
    )
    return episode


def make_change_preview(db, e, payload):
    action, change = normalize_employee_change(payload)
    try:
        effective = aware(payload.get("effective_at", now()))
    except (ValueError, TypeError):
        raise HTTPException(422, "Некорректное effective_at")
    if effective > now() + timedelta(minutes=1):
        raise HTTPException(
            422,
            "Подтвердите изменение при фактическом наступлении; будущая дата не применяется немедленно",
        )
    allowed = {
        "start_date",
        "probation_end_date",
        "site_id",
        "legal_entity_id",
        "employment_type",
        "timezone",
        "employment_stage",
        "manager_id",
        "assigned_hr_id",
    }
    change = {k: v for k, v in change.items() if k in allowed}
    merged = {**e.data, **change}
    try:
        start = date.fromisoformat(merged["start_date"])
        end = date.fromisoformat(merged["probation_end_date"])
        ZoneInfo(merged.get("timezone", "Europe/Moscow"))
        if end < start:
            raise ValueError()
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        raise HTTPException(422, "Некорректные даты/часовой пояс")
    if merged.get("employment_stage") not in (
        "PREBOARDING",
        "ACTIVE",
        "ENDED",
    ) or merged.get("employment_type") not in ("OFFICE", "PRODUCTION"):
        raise HTTPException(422, "Некорректная стадия/тип найма")
    if (
        action != "REHIRE"
        and e.data.get("employment_stage") == "ENDED"
        and merged.get("employment_stage") != "ENDED"
    ):
        raise HTTPException(409, "Новый найм создаётся командой REHIRE")
    if action == "REHIRE" and e.data.get("employment_stage") != "ENDED":
        raise HTTPException(409, "Сначала завершите действующий найм")
    if payload.get("expected_episode_id") and payload[
        "expected_episode_id"
    ] != e.data.get("episode_id"):
        raise HTTPException(409, "Период найма изменился")
    episode = db.get(Entity, e.data.get("episode_id"))
    if (
        payload.get("expected_state_version")
        and episode
        and payload["expected_state_version"] != episode.version
    ):
        raise HTTPException(409, "Состояние периода найма изменилось")
    site = get_entity(db, merged["site_id"], "site")
    if change.get("legal_entity_id") and change["legal_entity_id"] != site.data.get(
        "legal_entity_id"
    ):
        raise HTTPException(422, "Юрлицо не соответствует площадке")
    if "site_id" in change:
        change["legal_entity_id"] = site.data.get("legal_entity_id")
        merged.update(legal_entity_id=site.data.get("legal_entity_id"))
    actions = []
    js = entities(db, "journey", e.id)
    if action == "REHIRE":
        scenario = (
            get_entity(db, payload["scenario_id"], "scenario")
            if payload.get("scenario_id")
            else next(
                (
                    row
                    for row in entities(db, "scenario")
                    if row.data.get("published_version_id")
                ),
                None,
            )
        )
        if not scenario or not scenario.data.get("published_version_id"):
            raise HTTPException(409, "Нет опубликованной программы")
        ver = get_entity(db, scenario.data["published_version_id"], "scenario_version")
        actions = simulate(
            ver.data["graph"],
            merged,
            calendar_for(db, type("PreviewEmployee", (), {"data": merged})()),
        )["timeline"]
    else:
        for j in js:
            if j.status in ("CANCELLED", "COMPLETED"):
                continue
            ver = db.get(Entity, j.data["version_id"])
            actions.extend(
                simulate(
                    ver.data["graph"],
                    merged,
                    calendar_for(db, type("PreviewEmployee", (), {"data": merged})()),
                )["timeline"]
            )
    digest = hashlib.sha256(
        json.dumps(
            {
                "employee_id": e.id,
                "employee_version": e.version,
                "action": action,
                "changes": change,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return change, actions, digest


@router.post("/employees/{employee_id}/changes/preview")
def employee_change_preview(
    employee_id: str, payload: dict, db=Depends(get_db), actor=Depends(get_actor)
):
    require_role(actor, *HR)
    assert_employee(db, actor, employee_id)
    e = get_entity(db, employee_id, "employee")
    change, actions, digest = make_change_preview(db, e, payload)
    if change.get("site_id"):
        check_scope_site(actor, change["site_id"], db)
    p = save_entity(
        db,
        "employee_change_preview",
        {
            "employee_id": e.id,
            "employee_version": e.version,
            "episode_id": e.data.get("episode_id"),
            "action": payload.get("action"),
            "scenario_id": payload.get("scenario_id"),
            "changes": change,
            "preview_hash": digest,
            "timeline": actions,
            "expires_at": (now() + timedelta(minutes=30)).isoformat(),
        },
        parent_id=e.id,
    )
    commit(db)
    return {**pack(p), "preview_id": p.id}


@router.post("/employees/{employee_id}/changes")
def employee_apply_change(
    employee_id: str, payload: dict, db=Depends(get_db), actor=Depends(get_actor)
):
    require_role(actor, *HR)
    assert_employee(db, actor, employee_id)
    e = get_entity(db, employee_id, "employee", lock=True)
    p = get_entity(
        db, payload.get("preview_id", ""), "employee_change_preview", lock=True
    )
    if (
        p.parent_id != e.id
        or p.data["employee_version"] != e.version
        or aware(p.data["expires_at"]) < now()
        or payload.get("preview_hash") != p.data["preview_hash"]
    ):
        raise HTTPException(409, "Предпросмотр устарел")
    if not payload.get("reason"):
        raise HTTPException(422, "Укажите причину изменения")
    if p.data.get("episode_id") != e.data.get("episode_id"):
        raise HTTPException(409, "Период найма изменился")
    if p.data["changes"].get("site_id"):
        check_scope_site(actor, p.data["changes"]["site_id"], db)
    old = copy.deepcopy(e.data)
    if p.data.get("action") == "REHIRE":
        rehire_employee_record(
            db,
            e,
            p.data["changes"],
            actor,
            payload["reason"],
            p.data.get("scenario_id"),
        )
    else:
        update(e, **p.data["changes"])
        sync_employment_episode(db, e)
        if e.data.get("employment_stage") == "ENDED":
            end_employee_access(db, e, actor, payload["reason"])
        else:
            for j in entities(db, "journey", e.id):
                if j.data.get("episode_id") == e.data.get(
                    "episode_id"
                ) and j.status not in ("COMPLETED", "CANCELLED"):
                    replan_journey(db, j, e, bump_revision=True)
    p.status = "APPLIED"
    event(
        db,
        "employee_event",
        e.id,
        "PROFILE_CHANGED",
        actor.id,
        reason=payload["reason"],
        before=old,
        changes=p.data["changes"],
    )
    audit(db, actor, "employee.change", e.id, reason=payload["reason"])
    commit(db)
    return reply(e)


@router.post("/employees/{employee_id}/employment-episodes")
def employee_rehire(
    employee_id: str, payload: dict, db=Depends(get_db), actor=Depends(get_actor)
):
    preview = get_entity(db, payload.get("preview_id", ""), "employee_change_preview")
    if preview.parent_id != employee_id or preview.data.get("action") != "REHIRE":
        raise HTTPException(409, "Сначала подтвердите preview повторного найма")
    return employee_apply_change(employee_id, payload, db, actor)


def profile_content_fingerprint(db, employee):
    from .common import fingerprint

    asset_id = employee.data.get("profile_photo_asset_id")
    asset = db.get(Entity, asset_id) if asset_id else None
    return fingerprint(
        {
            "employee_id": employee.id,
            "episode_id": employee.data.get("episode_id"),
            "name": employee.data.get("name"),
            "bio": employee.data.get("profile_bio"),
            "photo_asset_id": asset_id,
            "photo_sha256": (asset.data.get("sha256") if asset else None),
        }
    )


def profile_publication_view(db, row):
    deliveries = [
        {
            "outbox_id": o.id,
            "recipient_user_id": o.recipient_id,
            "status": o.status,
            "suppression_reason": o.data.get("suppression_reason"),
        }
        for o in db.scalars(select(Outbox).where(Outbox.recipient_kind == "USER"))
        if o.data.get("profile_publication_id") == row.id
    ]
    return {
        **pack(row),
        "deliveries": deliveries,
        "accepted_count": sum(o["status"] == "ACCEPTED" for o in deliveries),
    }


@router.get("/employees/{employee_id}/profile-publication")
def profile_publication(employee_id: str, db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *HR)
    employee = assert_employee(db, actor, employee_id)
    return {
        "employee_id": employee.id,
        "episode_id": employee.data.get("episode_id"),
        "employee_version": employee.version,
        "profile_bio": employee.data.get("profile_bio"),
        "profile_photo_asset_id": employee.data.get("profile_photo_asset_id"),
        "status": employee.data.get("profile_publication_status", "NOT_SUBMITTED"),
        "publications": [
            profile_publication_view(db, row)
            for row in entities(db, "profile_publication", employee.id)
        ],
    }


def validate_profile_recipients(db, employee, ids):
    if (
        not isinstance(ids, list)
        or not 1 <= len(ids) <= 50
        or len(ids) != len(set(ids))
    ):
        raise HTTPException(
            422, "Выберите от 1 до 50 конкретных получателей без повторов"
        )
    users = []
    for uid in ids:
        user = db.get(User, uid)
        if not user or not user.enabled or user.role not in HR + ("MANAGER",):
            raise HTTPException(
                422, "Получатель должен быть действующим HR или руководителем"
            )
        who = Actor(user.id, user.name, user.role, user.scope, user.employee_id)
        if not actor_can_employee(db, who, employee.id):
            raise HTTPException(
                403, "Получатель не имеет текущего доступа к сотруднику"
            )
        users.append(user)
    return users


def validate_profile_content(db, employee):
    if employee.data.get("employment_stage") == "ENDED":
        raise HTTPException(409, "Найм завершён")
    bio = str(employee.data.get("profile_bio") or "").strip()
    photo = employee.data.get("profile_photo_asset_id")
    if not bio and not photo:
        raise HTTPException(409, "Сотрудник ещё не предоставил биографию или фото")
    text = (
        "Представляем коллегу: " + employee.data["name"] + ("\n\n" + bio if bio else "")
    )
    if len(text) > 3900:
        raise HTTPException(
            422,
            "Биография превышает лимит сообщения MAX; запросите сокращённый вариант",
        )
    if photo:
        asset = get_entity(db, photo, "asset")
        if (
            asset.owner_id != employee.id
            or asset.status not in ("READY", "APPROVED")
            or asset.data.get("content_type")
            not in ("image/jpeg", "image/png", "image/webp")
            or not asset.data.get("validated")
        ):
            raise HTTPException(
                409, "Фото ещё не прошло проверку либо принадлежит другому сотруднику"
            )
    return text


@router.post("/employees/{employee_id}/profile-publication/preview")
def profile_publication_preview(
    employee_id: str, payload: dict, db=Depends(get_db), actor=Depends(get_actor)
):
    require_role(actor, *HR)
    employee = assert_employee(db, actor, employee_id)
    if len(str(payload.get("reason", ""))) < 3:
        raise HTTPException(422, "Укажите назначение и разрешённую аудиторию")
    users = validate_profile_recipients(db, employee, payload.get("recipient_user_ids"))
    text = validate_profile_content(db, employee)
    content_hash = profile_content_fingerprint(db, employee)
    from .common import fingerprint

    digest = fingerprint(
        {
            "content": content_hash,
            "recipients": sorted(u.id for u in users),
            "employee_version": employee.version,
        }
    )
    preview = save_entity(
        db,
        "profile_publication_preview",
        {
            "employee_id": employee.id,
            "episode_id": employee.data.get("episode_id"),
            "employee_version": employee.version,
            "content_fingerprint": content_hash,
            "preview_hash": digest,
            "recipient_user_ids": [u.id for u in users],
            "text": text,
            "photo_asset_id": employee.data.get("profile_photo_asset_id"),
            "reason": str(payload["reason"])[:2000],
            "expires_at": (now() + timedelta(minutes=15)).isoformat(),
        },
        status="READY",
        parent_id=employee.id,
        owner_id=actor.id,
    )
    commit(db)
    return {
        **pack(preview),
        "preview_id": preview.id,
        "recipients": [
            {
                "id": u.id,
                "name": u.name,
                "role": u.role,
                "max_connected": any(
                    b.status == "ACTIVE" and b.owner_id == u.id
                    for b in entities(db, "principal_binding")
                ),
            }
            for u in users
        ],
        "sends_messages": False,
    }


@router.post("/employees/{employee_id}/profile-publication/approve")
def profile_publication_approve(
    employee_id: str, payload: dict, db=Depends(get_db), actor=Depends(get_actor)
):
    require_role(actor, *HR)
    employee = get_entity(db, employee_id, "employee", lock=True)
    assert_employee(db, actor, employee_id)
    preview = get_entity(
        db, payload.get("preview_id", ""), "profile_publication_preview", lock=True
    )
    if preview.parent_id != employee.id or preview.data.get(
        "preview_hash"
    ) != payload.get("preview_hash"):
        raise HTTPException(409, "Предпросмотр не соответствует публикации")
    if preview.status == "APPLIED" and preview.data.get("publication_id"):
        return profile_publication_view(
            db, get_entity(db, preview.data["publication_id"], "profile_publication")
        )
    if (
        preview.status != "READY"
        or aware(preview.data["expires_at"]) <= now()
        or preview.data["episode_id"] != employee.data.get("episode_id")
        or preview.data["employee_version"] != employee.version
        or preview.data["content_fingerprint"]
        != profile_content_fingerprint(db, employee)
    ):
        raise HTTPException(
            409,
            "Материал, период найма или аудитория изменились; создайте новый preview",
        )
    if len(str(payload.get("reason", ""))) < 3:
        raise HTTPException(422, "Укажите основание разрешения распространения")
    users = validate_profile_recipients(
        db, employee, preview.data["recipient_user_ids"]
    )
    validate_profile_content(db, employee)
    publication = save_entity(
        db,
        "profile_publication",
        {
            **{
                k: preview.data[k]
                for k in (
                    "employee_id",
                    "episode_id",
                    "content_fingerprint",
                    "recipient_user_ids",
                    "text",
                    "photo_asset_id",
                )
            },
            "approved_by": actor.id,
            "approved_at": now().isoformat(),
            "reason": str(payload["reason"])[:2000],
            "channels": ["MAX_DIRECT_AUTHORIZED_RECIPIENTS"],
            "corporate_site_published": False,
        },
        status="APPROVED",
        parent_id=employee.id,
        owner_id=actor.id,
    )
    for user in users:
        out = notify_user(
            db,
            user.id,
            preview.data["text"],
            f"profile:{publication.id}:recipient:{user.id}",
            data={
                "notification_type": "PROFILE_PUBLICATION",
                "profile_publication_id": publication.id,
                "employee_id": employee.id,
                "episode_id": employee.data.get("episode_id"),
                "required_roles": list(HR) + ["MANAGER"],
            },
        )
        out.attachments = (
            [{"asset_id": preview.data["photo_asset_id"]}]
            if preview.data.get("photo_asset_id")
            else []
        )
    update(
        employee,
        profile_publication_status="APPROVED",
        profile_publication_id=publication.id,
    )
    preview.status = "APPLIED"
    update(preview, publication_id=publication.id)
    audit(
        db,
        actor,
        "profile.publication.approve",
        publication.id,
        {"recipient_count": len(users)},
        reason=payload.get("reason"),
    )
    commit(db)
    return profile_publication_view(db, publication)


@router.post("/employees/{employee_id}/profile-publication/reject")
def profile_publication_reject(
    employee_id: str, payload: dict, db=Depends(get_db), actor=Depends(get_actor)
):
    require_role(actor, *HR)
    employee = get_entity(db, employee_id, "employee", lock=True)
    assert_employee(db, actor, employee_id)
    if len(str(payload.get("reason", ""))) < 3:
        raise HTTPException(422, "Укажите причину отказа или отзыва разрешения")
    for publication in entities(db, "profile_publication", employee.id):
        if publication.status == "APPROVED":
            publication.status = "REVOKED"
            update(
                publication,
                revoked_by=actor.id,
                revoked_at=now().isoformat(),
                revoke_reason=str(payload["reason"])[:2000],
            )
    for preview in entities(db, "profile_publication_preview", employee.id):
        if preview.status == "READY":
            preview.status = "REJECTED"
    for out in db.scalars(select(Outbox)):
        if (
            out.data.get("notification_type") == "PROFILE_PUBLICATION"
            and out.data.get("employee_id") == employee.id
            and out.status in ("PENDING", "WAITING_BINDING", "QUEUED", "SENDING")
        ):
            out.status = "SUPPRESSED"
            out.data = {**out.data, "suppression_reason": "PROFILE_PUBLICATION_REVOKED"}
    update(employee, profile_publication_status="REJECTED")
    event(
        db,
        "employee_event",
        employee.id,
        "PROFILE_PUBLICATION_REJECTED",
        actor.id,
        reason=str(payload["reason"])[:2000],
    )
    audit(
        db, actor, "profile.publication.reject", employee.id, reason=payload["reason"]
    )
    commit(db)
    return profile_publication(employee.id, db, actor)


@router.post("/employees/imports")
def import_employees(payload: dict, db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *HR)
    rows = payload.get("rows")
    if rows is None:
        try:
            rows = list(
                csv.DictReader(
                    io.StringIO(payload.get("csv_text", "").lstrip("\ufeff"))
                )
            )
        except csv.Error:
            raise HTTPException(422, "Некорректный CSV")
    if not isinstance(rows, list) or not rows or len(rows) > 1000:
        raise HTTPException(422, "Допускается 1–1000 строк")
    mode = payload.get("mode", "CREATE").upper()
    if mode not in ("CREATE", "UPDATE", "UPSERT"):
        raise HTTPException(422, "Режим CREATE, UPDATE или UPSERT")
    existing = {
        e.data["external_employee_id"]: e
        for e in entities(db, "employee")
        if e.data.get("external_employee_id")
    }
    results = []
    seen = set()
    for i, row in enumerate(rows, 1):
        errors = []
        if not isinstance(row, dict):
            results.append(
                {"row": i, "errors": ["Ожидалась строка CSV"], "valid": False}
            )
            continue
        external = row.get("external_employee_id")
        target = existing.get(external) if external else None
        if not all(row.get(k) for k in ("name", "site_id", "start_date")):
            errors.append("Не заполнены name/site_id/start_date")
        try:
            start = date.fromisoformat(row.get("start_date", ""))
            ZoneInfo(row.get("timezone", "Europe/Moscow"))
            if (
                row.get("probation_end_date")
                and date.fromisoformat(row["probation_end_date"]) < start
            ):
                errors.append("Конец ИС раньше выхода")
        except (ValueError, TypeError, ZoneInfoNotFoundError):
            errors.append("Неверная дата или часовой пояс")
        site = db.get(Entity, row.get("site_id"))
        if not site or site.kind != "site":
            errors.append("Площадка не найдена")
        else:
            try:
                check_scope_site(actor, site.id, db)
            except HTTPException:
                errors.append("Площадка вне ваших полномочий")
        if external in seen:
            errors.append("В файле повторяется внешний ID")
        if external:
            seen.add(external)
        if mode == "CREATE" and target:
            errors.append("Внешний ID уже существует; выберите обновление")
        if mode == "UPDATE" and not target:
            errors.append("Существующий сотрудник по внешнему ID не найден")
        if target and not actor_can_employee(db, actor, target.id):
            errors.append("Карточка недоступна")
        if row.get("employment_type", "OFFICE") not in ("OFFICE", "PRODUCTION"):
            errors.append("Тип OFFICE или PRODUCTION")
        for field, roles in [("manager_id", ("MANAGER",)), ("assigned_hr_id", HR)]:
            if row.get(field):
                user = db.get(User, row[field])
                if not user or not user.enabled or user.role not in roles:
                    errors.append("Назначенный пользователь не найден")
        if (
            row.get("legal_entity_id")
            and site
            and site.data.get("legal_entity_id")
            and row["legal_entity_id"] != site.data["legal_entity_id"]
        ):
            errors.append("Юрлицо не соответствует площадке")
        results.append(
            {
                "row": i,
                "data": row,
                "employee_id": target.id if target else None,
                "operation": "UPDATE" if target and mode != "CREATE" else "CREATE",
                "errors": errors,
                "valid": not errors,
            }
        )
    if payload.get("validate_only", True):
        return {
            "items": results,
            "valid": all(r["valid"] for r in results),
            "created": 0,
            "updated": 0,
        }
    if any(not r["valid"] for r in results) and not payload.get("allow_partial"):
        raise HTTPException(
            422,
            {
                "message": "Исправьте ошибки или разрешите частичный импорт",
                "items": results,
            },
        )
    changed = []
    created = 0
    updated = 0
    try:
        for item in results:
            if not item["valid"]:
                continue
            if item["operation"] == "CREATE":
                employee = create_employee_record(db, actor, item["data"])
                created += 1
            else:
                employee = get_entity(db, item["employee_id"], "employee", lock=True)
                fields = {
                    "name",
                    "site_id",
                    "start_date",
                    "probation_end_date",
                    "employment_type",
                    "timezone",
                    "manager_id",
                    "assigned_hr_id",
                    "department",
                    "email",
                    "legal_entity_id",
                }
                changes = {k: v for k, v in item["data"].items() if k in fields}
                site = get_entity(db, changes["site_id"], "site")
                changes.setdefault("legal_entity_id", site.data.get("legal_entity_id"))
                update(employee, **changes)
                sync_employment_episode(db, employee)
                for journey in entities(db, "journey", employee.id):
                    if journey.status not in ("CANCELLED", "COMPLETED"):
                        replan_journey(db, journey, employee, bump_revision=True)
                audit(db, actor, "employee.import-update", employee.id)
                updated += 1
            changed.append(employee)
        commit(db)
    except Exception:
        db.rollback()
        raise
    return {
        "items": results,
        "employees": [pack(e) for e in changed],
        "created": created,
        "updated": updated,
        "invitations_sent": 0,
    }


def _scenario_allowed(actor, scenario, write=False):
    from .knowledge import _scope_access
    from types import SimpleNamespace

    proxy = SimpleNamespace(
        role=(
            "HR_SPECIALIST" if actor.role == "HEAD_TALENT" else actor.role
        ),
        scope=actor.scope,
    )
    probe = Entity(
        kind="document",
        status="ACTIVE" if scenario.data.get("published_version_id") else "DRAFT",
        data={"scope": scenario.data.get("scope", {})},
    )
    return _scope_access(proxy, probe, write=write)


def _scenario_access(db, actor, scenario_id, write=False):
    scenario = get_entity(db, scenario_id, "scenario")
    if not _scenario_allowed(actor, scenario, write):
        raise HTTPException(404, "Программа не найдена")
    return scenario


def _scenario_version_access(db, actor, version, write=False):
    scenario = _scenario_access(db, actor, version.parent_id, write)
    if write and scenario.status == "ARCHIVED":
        raise HTTPException(409, "Сначала восстановите удалённую схему")
    return version


def _scenario_applies(scenario, employee):
    from .retrieval import scope_matches

    return scope_matches(
        {
            **scenario.data.get("scope", {}),
            "employment_stages": ["PREBOARDING", "ACTIVE"],
        },
        employee,
    )


@router.get("/scenarios")
def scenarios(db=Depends(get_db), actor=Depends(get_actor), include_archived: bool = False):
    require_role(actor, *READ_HR)
    return {
        "items": [
            {**pack(e), "can_edit": _scenario_allowed(actor, e, True), **scenario_summary(db, e)}
            for e in entities(db, "scenario")
            if (include_archived or e.status != "ARCHIVED") and _scenario_allowed(actor, e)
        ]
    }


def scenario_summary(db, scenario):
    versions = entities(db, "scenario_version", scenario.id)
    latest = next((v for v in reversed(versions) if v.status in {"DRAFT", "IN_REVIEW", "APPROVED"}), None)
    latest = latest or (db.get(Entity, scenario.data["published_version_id"]) if scenario.data.get("published_version_id") else None)
    graph = latest.data.get("graph", {}) if latest else {}
    ever_published = any(v.status in {"PUBLISHED", "ARCHIVED"} or v.data.get("published_at") for v in versions)
    return {"has_published_versions": ever_published,
            "current_published_version_id": scenario.data.get("published_version_id"),
            "latest_version_id": latest.id if latest else None,
            "latest_version_status": latest.status if latest else None,
            "editor_mode": graph.get("editor_mode", "LEGACY"),
            "start_policy": graph.get("start_policy")}


@router.post("/scenarios")
def create_scenario(payload: dict, db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *HR)
    if not payload.get("name"):
        raise HTTPException(422, "Укажите название")
    scope = payload.get(
        "scope",
        (
            {}
            if actor.scope.get("all") is True
            else {
                key: actor.scope[key]
                for key in ("site_ids", "legal_entity_ids")
                if actor.scope.get(key)
            }
        ),
    )
    from .knowledge import _validate_scope

    if not isinstance(scope, dict) or set(scope) - {"site_ids", "legal_entity_ids"}:
        raise HTTPException(422, "Область программы задаётся площадками и юрлицами")
    validated = _validate_scope(scope)
    scope = {
        key: validated[key]
        for key in ("site_ids", "legal_entity_ids")
        if validated.get(key)
    }
    if not _scenario_allowed(
        actor, Entity(kind="scenario", data={"scope": scope}), True
    ):
        raise HTTPException(403, "Область программы не предоставлена")
    e = save_entity(
        db,
        "scenario",
        {
            "name": payload["name"],
            "description": payload.get("description", ""),
            "scope": scope,
        },
    )
    audit(db, actor, "scenario.create", e.id)
    commit(db)
    return reply(e, 201)


@router.get("/scenarios/{scenario_id}")
def get_scenario(scenario_id: str, db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *READ_HR)
    return reply(_scenario_access(db, actor, scenario_id))


def _scenario_name(payload):
    if set(payload) != {"name"} or not isinstance(payload.get("name"), str) or not 1 <= len(payload["name"].strip()) <= 160:
        raise HTTPException(422, "Передайте название от 1 до 160 символов")
    return payload["name"].strip()


@router.patch("/scenarios/{scenario_id}")
def rename_scenario(scenario_id: str, payload: dict, request: Request, db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *HR)
    scenario = get_entity(db, scenario_id, "scenario", lock=True)
    _scenario_access(db, actor, scenario_id, True)
    check_etag(request, scenario)
    if scenario.status == "ARCHIVED":
        raise HTTPException(409, "Удалённую схему нельзя редактировать")
    update(scenario, name=_scenario_name(payload))
    audit(db, actor, "scenario.rename", scenario.id)
    commit(db)
    return reply(scenario, extra=scenario_summary(db, scenario))


@router.delete("/scenarios/{scenario_id}")
def archive_scenario(scenario_id: str, request: Request, db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *HR)
    scenario = get_entity(db, scenario_id, "scenario", lock=True)
    _scenario_access(db, actor, scenario_id, True)
    check_etag(request, scenario)
    scenario.status = "ARCHIVED"
    update(scenario, archived_at=now().isoformat(), archived_by=actor.id)
    audit(db, actor, "scenario.archive", scenario.id)
    commit(db)
    return reply(scenario, extra=scenario_summary(db, scenario))


@router.post("/scenarios/{scenario_id}/restore")
def restore_scenario(scenario_id: str, request: Request, db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *HR)
    scenario = get_entity(db, scenario_id, "scenario", lock=True)
    _scenario_access(db, actor, scenario_id, True)
    check_etag(request, scenario)
    if scenario.status != "ARCHIVED":
        raise HTTPException(409, "Схема не удалена")
    scenario.status = "ACTIVE"
    update(scenario, restored_at=now().isoformat(), restored_by=actor.id)
    audit(db, actor, "scenario.restore", scenario.id)
    commit(db)
    return reply(scenario, extra=scenario_summary(db, scenario))


@router.get("/scenarios/{scenario_id}/versions")
def scenario_versions(scenario_id: str, db=Depends(get_db), actor=Depends(get_actor), include_deleted: bool = False):
    require_role(actor, *READ_HR)
    _scenario_access(db, actor, scenario_id)
    return {"items": [pack(e) for e in entities(db, "scenario_version", scenario_id) if include_deleted or e.status != "DELETED"]}


@router.post("/scenarios/{scenario_id}/versions")
def create_scenario_version(
    scenario_id: str, payload: dict, db=Depends(get_db), actor=Depends(get_actor)
):
    require_role(actor, *HR)
    s = get_entity(db, scenario_id, "scenario", lock=True)
    _scenario_access(db, actor, scenario_id, True)
    if s.status == "ARCHIVED":
        raise HTTPException(409, "В удалённой схеме нельзя создавать версии")
    base_id = payload.get("base_version_id") or s.data.get("published_version_id")
    base = get_entity(db, base_id, "scenario_version") if base_id else None
    if base and base.parent_id != s.id:
        raise HTTPException(422, "Версия относится к другой программе")
    if base and base.status == "DELETED":
        raise HTTPException(409, "Удалённый черновик нельзя использовать как основу")
    graph = copy.deepcopy(
        payload.get("graph")
        or (
            base.data["graph"]
            if base
            else {
                "schema_version": "korsik-scenario-v2",
                "algorithm_version": "calendar-v1",
                "start_step_key": "welcome",
                "source_mapping": [],
                "steps": [
                    {
                        "step_key": "welcome",
                        "kind": "MESSAGE",
                        "sequence_no": 1,
                        "mandatory": True,
                        "eligibility": {},
                        "delivery_policy": {"late_policy": "send_once_on_join"},
                        "trigger": {
                            "type": "ANCHOR_TIME",
                            "anchor": "START_DATE",
                            "day_basis": "CALENDAR",
                            "offset_days": 0,
                            "local_time": "10:00",
                            "calendar_binding": {"mode": "SITE_AT_ENROLLMENT"},
                        },
                        "content": {
                            "text_ty": "Добро пожаловать!",
                            "text_vy": "Добро пожаловать!",
                        },
                        "transitions": [],
                    }
                ],
            }
        )
    )
    if payload.get("editor_mode") == "SIMPLE" and not payload.get("graph") and (
        not base or base.data["graph"].get("editor_mode") != "SIMPLE"
    ):
        from .simple_scenarios import default_graph
        graph = default_graph()
    if graph.get("editor_mode") == "SIMPLE":
        from .simple_scenarios import prepare_graph
        graph = prepare_graph(db, actor, graph)
    e = save_entity(
        db,
        "scenario_version",
        {
            "scenario_id": s.id,
            "name": payload.get("name", "Новая редакция"),
            "graph": graph,
            "base_version_id": base_id,
        },
        status="DRAFT",
        parent_id=s.id,
    )
    audit(db, actor, "scenario.draft", e.id)
    commit(db)
    return reply(e, 201)


@router.get("/scenario-versions/{version_id}")
def get_scenario_version(version_id: str, db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *READ_HR)
    return reply(
        _scenario_version_access(
            db, actor, get_entity(db, version_id, "scenario_version")
        )
    )


@router.put("/scenario-versions/{version_id}")
def put_scenario_version(
    version_id: str,
    payload: dict,
    request: Request,
    db=Depends(get_db),
    actor=Depends(get_actor),
):
    require_role(actor, *HR)
    e = get_entity(db, version_id, "scenario_version", lock=True)
    _scenario_version_access(db, actor, e, True)
    check_etag(request, e)
    if e.status not in ("DRAFT", "IN_REVIEW", "APPROVED"):
        raise HTTPException(409, "Опубликованная версия неизменяема; создайте черновик")
    graph = payload.get("graph")
    if not isinstance(graph, dict):
        raise HTTPException(422, "Передайте graph")
    # Invalid but bounded drafts can be saved; validate/publish reject errors.
    if len(json.dumps(graph)) > 2_000_000:
        raise HTTPException(413, "Программа слишком велика")
    from .scenario_media import validate_authored_materials
    validate_authored_materials(db, actor, graph)
    if graph.get("editor_mode") == "SIMPLE":
        from .simple_scenarios import prepare_graph
        graph = prepare_graph(db, actor, graph)
    update(
        e,
        graph=graph,
        name=_scenario_name({"name": payload["name"]}) if "name" in payload else e.data.get("name"),
        review=None,
        validation=None,
    )
    e.status = "DRAFT"
    audit(db, actor, "scenario.save", e.id)
    commit(db)
    return reply(e)


@router.patch("/scenario-versions/{version_id}")
def rename_scenario_version(version_id: str, payload: dict, request: Request, db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *HR)
    version = get_entity(db, version_id, "scenario_version", lock=True)
    _scenario_version_access(db, actor, version, True)
    check_etag(request, version)
    if version.status not in {"DRAFT", "IN_REVIEW", "APPROVED"}:
        raise HTTPException(409, "Опубликованная или удалённая версия неизменяема")
    update(version, name=_scenario_name(payload), review=None, validation=None)
    version.status = "DRAFT"
    audit(db, actor, "scenario.version.rename", version.id)
    commit(db)
    return reply(version)


@router.delete("/scenario-versions/{version_id}")
def delete_scenario_version(version_id: str, request: Request, db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *HR)
    version = get_entity(db, version_id, "scenario_version", lock=True)
    _scenario_version_access(db, actor, version, True)
    check_etag(request, version)
    if version.status != "DRAFT":
        raise HTTPException(409, "Удалить можно только неопубликованный черновик")
    pinned = any(row.data.get("version_id") == version.id or row.data.get("scenario_version_id") == version.id for kind in ("journey", "scheduled_action", "survey_run") for row in entities(db, kind))
    if pinned or any(row.data.get("published_version_id") == version.id for row in entities(db, "scenario")):
        raise HTTPException(409, "Версия используется назначенным сценарием и не может быть удалена")
    version.status = "DELETED"
    update(version, deleted_at=now().isoformat(), deleted_by=actor.id)
    audit(db, actor, "scenario.version.delete", version.id)
    commit(db)
    return reply(version)


@router.post("/scenario-versions/{version_id}/restore")
def restore_scenario_version(version_id: str, request: Request, db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *HR)
    version = get_entity(db, version_id, "scenario_version", lock=True)
    _scenario_version_access(db, actor, version, True)
    check_etag(request, version)
    if version.status != "DELETED":
        raise HTTPException(409, "Черновик не удалён")
    version.status = "DRAFT"
    update(version, restored_at=now().isoformat(), restored_by=actor.id, review=None, validation=None)
    audit(db, actor, "scenario.version.restore", version.id)
    commit(db)
    return reply(version)


@router.post("/scenario-versions/{version_id}/validate")
def validate_scenario(version_id: str, db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *HR)
    e = get_entity(db, version_id, "scenario_version")
    _scenario_version_access(db, actor, e, True)
    result = _scenario_validation(db, e)
    update(e, validation=result)
    commit(db)
    return {**result, "version_id": e.id, "version": e.version}


def _scenario_validation(db, version):
    """Pin deliberate changes to the published mandatory route for HR review."""
    result = validate_graph(version.data["graph"])
    if not result["valid"]:
        return {
            **result,
            "source_changes": [],
            "source_change_fingerprint": None,
            "source_baseline_version_id": None,
        }
    scenario = db.get(Entity, version.parent_id)
    baseline = (
        db.get(Entity, scenario.data.get("published_version_id"))
        if scenario and scenario.data.get("published_version_id")
        else None
    )
    if baseline and baseline.id == version.id:
        baseline = None
    old_graph = baseline.data.get("graph", {}) if baseline else {}
    new_graph = version.data["graph"]
    if new_graph.get("editor_mode") == "SIMPLE":
        # Simple schemas deliberately replace mandatory gating with durable progress.
        result.update(source_changes=[], source_change_fingerprint=semantic_hash({"simple": result["semantic_hash"]}), source_baseline_version_id=baseline.id if baseline else None)
        return result
    by_key = {
        step.get("step_key"): step
        for step in new_graph.get("steps", [])
        if isinstance(step, dict)
    }
    changes = []
    for step in old_graph.get("steps", []):
        if not step.get("mandatory"):
            continue
        key = step["step_key"]
        proposed = by_key.get(key)
        if proposed is None:
            changes.append(
                {
                    "step_key": key,
                    "change": "REMOVED_MANDATORY_STEP",
                    "fields": ["step"],
                }
            )
            continue
        # Sequence labels and editor coordinates are presentation; actual edges,
        # questions, content, eligibility and schedule remain reviewable changes.
        fields = sorted(
            field
            for field in set(step) | set(proposed)
            if field not in {"step_key", "sequence_no"}
            and step.get(field) != proposed.get(field)
        )
        if fields:
            changes.append(
                {"step_key": key, "change": "CHANGED_MANDATORY_STEP", "fields": fields}
            )
    if old_graph.get("source_mapping") != new_graph.get("source_mapping") and baseline:
        changes.append(
            {
                "step_key": None,
                "change": "CHANGED_SOURCE_MAPPING",
                "fields": ["source_mapping"],
            }
        )
    pins = {
        "baseline_version_id": baseline.id if baseline else None,
        "baseline_semantic_hash": semantic_hash(old_graph) if baseline else None,
        "proposed_semantic_hash": result["semantic_hash"],
        "changes": changes,
    }
    result.update(
        source_changes=changes,
        source_change_fingerprint=semantic_hash(pins),
        source_baseline_version_id=pins["baseline_version_id"],
    )
    return result


@router.post("/scenario-versions/{version_id}/simulations")
def simulate_scenario(
    version_id: str, payload: dict, db=Depends(get_db), actor=Depends(get_actor)
):
    require_role(actor, *HR)
    e = get_entity(db, version_id, "scenario_version")
    _scenario_version_access(db, actor, e)
    profile = payload.get("employee", payload.get("profile", {}))
    if not profile.get("start_date"):
        raise HTTPException(422, "Для симуляции укажите дату выхода")
    profile.setdefault(
        "probation_end_date",
        (
            add_months(date.fromisoformat(profile["start_date"]), 3) - timedelta(days=1)
        ).isoformat(),
    )
    profile.setdefault("employment_stage", "ACTIVE")
    profile.setdefault("employment_type", "OFFICE")
    profile.setdefault("site_id", "GrPP")
    from types import SimpleNamespace

    selected_calendar = payload.get("calendar") or calendar_for(
        db, SimpleNamespace(data=profile)
    )
    result = simulate(
        e.data["graph"],
        profile,
        selected_calendar,
        payload.get("answers"),
        payload.get("signals"),
    )
    run = save_entity(
        db,
        "scenario_simulation",
        result,
        parent_id=e.id,
        status="PASSED" if result["valid"] else "FAILED",
    )
    commit(db)
    return {**pack(run), **result}


@router.post("/scenario-versions/{version_id}/review")
def review_scenario(
    version_id: str, payload: dict, db=Depends(get_db), actor=Depends(get_actor)
):
    require_role(actor, *PUBLISH)
    e = get_entity(db, version_id, "scenario_version", lock=True)
    _scenario_version_access(db, actor, e, True)
    if e.status not in ("DRAFT", "IN_REVIEW", "APPROVED"):
        raise HTTPException(409, "Версию нельзя отправить на review")
    result = _scenario_validation(db, e)
    if not result["valid"]:
        raise HTTPException(422, result)
    change_reason = str(payload.get("source_change_reason", "")).strip()
    if result["source_changes"] and (
        payload.get("source_change_acknowledged") is not True
        or payload.get("source_change_fingerprint")
        != result["source_change_fingerprint"]
        or len(change_reason) < 3
    ):
        raise HTTPException(
            409, detail={"code": "source_changes_require_explicit_review", **result}
        )
    update(
        e,
        review={
            "reviewer_id": actor.id,
            "semantic_hash": result["semantic_hash"],
            "reviewed_at": now().isoformat(),
            "notes": payload.get("notes", ""),
            "source_change_fingerprint": result["source_change_fingerprint"],
            "source_changes": result["source_changes"],
            "source_change_reason": change_reason if result["source_changes"] else None,
        },
        validation=result,
    )
    e.status = "APPROVED"
    audit(
        db,
        actor,
        "scenario.approve",
        e.id,
        {
            "reason": change_reason or payload.get("notes", ""),
            "source_changes": result["source_changes"],
        },
    )
    commit(db)
    return reply(e)


@router.post("/scenario-versions/{version_id}/publish")
def publish_scenario(
    version_id: str, payload: dict, db=Depends(get_db), actor=Depends(get_actor)
):
    require_role(actor, *PUBLISH)
    reason = payload.get("reason")
    if not isinstance(reason, str) or len(reason) > 2000 or len(reason.strip()) < 3:
        raise HTTPException(422, "Укажите причину публикации от 3 до 2000 символов")
    e = get_entity(db, version_id, "scenario_version", lock=True)
    s = get_entity(db, e.parent_id, "scenario", lock=True)
    _scenario_access(db, actor, s.id, True)
    if s.status == "ARCHIVED":
        raise HTTPException(409, "Удалённую схему нельзя публиковать")
    result = _scenario_validation(db, e)
    review = e.data.get("review") or {}
    if (
        e.status != "APPROVED"
        or not result["valid"]
        or review.get("semantic_hash") != result["semantic_hash"]
        or review.get("source_change_fingerprint")
        != result["source_change_fingerprint"]
    ):
        raise HTTPException(409, "Нужны актуальная проверка и одобрение версии")
    policy = payload.get("migration_policy", "NEW_ENROLLMENTS_ONLY")
    if policy not in ("NEW_ENROLLMENTS_ONLY", "MIGRATE_NOT_STARTED"):
        raise HTTPException(
            422,
            "Действующие анкеты нельзя молча заменить; выберите новые enrollments или не начатые",
        )
    simple_graph = e.data["graph"].get("editor_mode") == "SIMPLE"
    if simple_graph and policy != "NEW_ENROLLMENTS_ONLY":
        raise HTTPException(422, "Новая схема применяется к новым назначениям; текущие сценарии сотрудников сохраняются")
    if simple_graph and e.data["graph"].get("start_policy", {}).get("condition") == "SCHEDULED":
        from .simple_scenarios import launch_at
        if launch_at(e.data["graph"], {}, {}, now()) < now():
            raise HTTPException(422, "Дата и время запуска должны быть в будущем")
    old = (
        db.get(Entity, s.data["published_version_id"])
        if s.data.get("published_version_id")
        else None
    )
    if old:
        old.status = "ARCHIVED"
    e.status = "PUBLISHED"
    update(e, published_at=now().isoformat(), semantic_hash=result["semantic_hash"])
    if simple_graph:
        update(e, activation_at=now().isoformat())
    update(s, published_version_id=e.id)
    migrated = 0
    if policy == "MIGRATE_NOT_STARTED":
        for j in entities(db, "journey"):
            if (
                j.data.get("scenario_id") == s.id
                and j.status == "SCHEDULED"
                and not j.data.get("events")
            ):
                update(j, version_id=e.id)
                replan_journey(db, j, get_entity(db, j.parent_id, "employee"))
                migrated += 1
    audit(
        db,
        actor,
        "scenario.publish",
        e.id,
        {"migration_policy": policy, "migrated": migrated},
        reason=reason,
    )
    commit(db)
    return reply(e, extra={"migrated": migrated})


def enroll_employee(db, employee, scenario_id=None):
    employee = get_entity(db, employee.id, "employee", lock=True)
    if employee.status == "DELETED" or employee.data.get("employment_stage") == "ENDED":
        return None
    scenario = (
        get_entity(db, scenario_id, "scenario")
        if scenario_id
        else next(
            (
                s
                for s in entities(db, "scenario")
                if s.status != "ARCHIVED" and s.data.get("published_version_id") and _scenario_applies(s, employee)
            ),
            None,
        )
    )
    if not scenario:
        return None
    scenario = get_entity(db, scenario.id, "scenario", lock=True)
    if scenario.status == "ARCHIVED":
        existing = next((j for j in entities(db, "journey", employee.id) if j.data.get("scenario_id") == scenario.id and j.data.get("episode_id") == employee.data.get("episode_id")), None)
        if existing:
            return existing
        raise HTTPException(409, "Удалённая схема не назначается новым сотрудникам")
    if not _scenario_applies(scenario, employee):
        raise HTTPException(
            422, "Программа не применяется к площадке или юрлицу сотрудника"
        )
    version = db.get(Entity, scenario.data.get("published_version_id"))
    if version and version.data.get("graph", {}).get("editor_mode") == "SIMPLE":
        from .simple_scenarios import enroll
        return enroll(db, employee, scenario)
    for j in entities(db, "journey", employee.id):
        if j.status not in ("CANCELLED", "COMPLETED") and j.data.get(
            "episode_id"
        ) == employee.data.get("episode_id"):
            return j
    j = save_entity(
        db,
        "journey",
        {
            "employee_id": employee.id,
            "episode_id": employee.data.get("episode_id"),
            "scenario_id": scenario.id,
            "version_id": scenario.data["published_version_id"],
            "schedule_revision": 1,
            "signals": {},
            "events": {},
            "answers": {},
            "completed_steps": [],
        },
        status="SCHEDULED",
        parent_id=employee.id,
    )
    replan_journey(db, j, employee)
    if employee.data.get("probation_end_date") and now().astimezone(
        ZoneInfo(employee.data.get("timezone", "Europe/Moscow"))
    ).date() > date.fromisoformat(employee.data["probation_end_date"]):
        j.status = "PAUSED"
        update(
            j,
            requires_hr_review=True,
            review_reason="JOINED_AFTER_PROBATION_END",
            paused_at=now().isoformat(),
            previous_status="SCHEDULED",
        )
    return j


def notify_simple_bot_started(db, employee):
    from .simple_scenarios import notify_bot_started
    return notify_bot_started(db, employee)


def resolve_simple_formulation(db, employee, payload):
    from .simple_scenarios import resolve_formulation
    return resolve_formulation(db, employee, payload)


def replan_journey(db, journey, employee, bump_revision=False):
    scenario = get_entity(db, journey.data["scenario_id"], "scenario")
    if not _scenario_applies(scenario, employee):
        journey.status = "PAUSED"
        update(journey, requires_hr_review=True, review_reason="PROGRAM_SCOPE_CHANGED")
        return
    version = get_entity(db, journey.data["version_id"], "scenario_version")
    if version.data.get("graph", {}).get("editor_mode") == "SIMPLE":
        from .simple_scenarios import plan
        return plan(db, journey, employee, bump_revision)
    revision = journey.data.get("schedule_revision", 1) + (1 if bump_revision else 0)
    update(journey, schedule_revision=revision)
    version = get_entity(db, journey.data["version_id"], "scenario_version")
    graph = version.data["graph"]
    existing = {
        a.data["step_key"]: a for a in entities(db, "scheduled_action", journey.id)
    }
    cal = calendar_for(db, employee)
    for step in graph["steps"]:
        old = existing.get(step["step_key"])
        if old and old.status in (
            "ACCEPTED",
            "COMPLETED",
            "SKIPPED",
            "SENDING",
            "UNKNOWN",
        ):
            continue
        if old and old.status == "ENQUEUED":
            if not bump_revision or old.data.get("survey_run_id"):
                continue
            outbound = [
                o
                for o in db.execute(select(Outbox)).scalars()
                if (o.data or {}).get("action_id") == old.id
            ]
            if any(
                o.status in ("SENDING", "UNKNOWN", "ACCEPTED", "SENT") for o in outbound
            ):
                continue
            for out in outbound:
                if out.status in ("PENDING", "READY", "RETRY"):
                    out.status = "CANCELLED"
        calendar_warning = None
        step_cal = cal
        binding = step.get("trigger", {}).get("calendar_binding", {})
        if binding.get("mode") == "PINNED_VERSION":
            pinned = db.get(Entity, binding.get("calendar_version_id"))
            step_cal = (
                pinned.data
                if pinned and pinned.kind == "calendar_version"
                else {
                    "configuration_error": "NEEDS_CALENDAR_REVIEW: закреплённый календарь недоступен",
                    "working_weekdays": [],
                }
            )
        try:
            due = (
                calculate_due(
                    step.get("trigger", {}),
                    employee.data,
                    step_cal,
                    journey.data.get("events"),
                    journey.data.get("signals"),
                )
                if step.get("trigger")
                else None
            )
        except ValueError as error:
            due = None
            calendar_warning = str(error)
        applicable = eligible(step.get("eligibility", {}), employee.data)
        data = {
            "employee_id": employee.id,
            "journey_id": journey.id,
            "version_id": version.id,
            "step_key": step["step_key"],
            "sequence_no": step.get("sequence_no", 0),
            "schedule_revision": revision,
            "due_at": due.isoformat() if due else None,
            "original_due_at": (
                old.data.get("original_due_at")
                if old
                else due.isoformat() if due else None
            ),
            "step": step,
            "applicable": applicable,
            "calendar_warning": calendar_warning,
        }
        if old:
            update(old, **data)
            old.status = "PLANNED" if applicable else "NOT_APPLICABLE"
        else:
            save_entity(
                db,
                "scheduled_action",
                data,
                status="PLANNED" if applicable else "NOT_APPLICABLE",
                parent_id=journey.id,
            )
    db.flush()


def cancel_journey(db, j, reason):
    j.status = "CANCELLED"
    update(
        j,
        cancel_reason=reason,
        schedule_revision=j.data.get("schedule_revision", 0) + 1,
    )
    for a in entities(db, "scheduled_action", j.id):
        if a.status not in ("ACCEPTED", "COMPLETED", "SKIPPED"):
            a.status = "CANCELLED"
    for s in entities(db, "survey_run", j.id):
        if s.status not in TERMINAL_SURVEYS:
            s.status = "CANCELLED"
    for q in entities(db, "scenario_question", j.id):
        if q.status == "OPEN":
            q.status = "CANCELLED"
    for cap in entities(db, "input_capture", j.parent_id):
        if cap.status in ("OPEN", "DRAFT"):
            cap.status = "CANCELLED"


@router.get("/journeys")
def journeys(db=Depends(get_db), actor=Depends(get_actor)):
    if not can_employee_drilldown(actor):
        raise HTTPException(
            404, "Для персонального просмотра нужен отдельный drilldown grant"
        )
    return {
        "items": [
            journey_view(j, actor)
            for j in entities(db, "journey")
            if actor_can_employee(db, actor, j.parent_id)
            and current_employee_record(db, actor, j.parent_id, j)
        ]
    }


@router.post("/employees/{employee_id}/journeys")
def start_journey(
    employee_id: str, payload: dict, db=Depends(get_db), actor=Depends(get_actor)
):
    require_role(actor, *HR)
    assert_employee(db, actor, employee_id)
    e = get_entity(db, employee_id, "employee")
    j = enroll_employee(db, e, payload.get("scenario_id"))
    commit(db)
    if not j:
        raise HTTPException(409, "Нет опубликованной программы")
    return reply(j, 201)


@router.get("/journeys/{journey_id}")
def journey_get(journey_id: str, db=Depends(get_db), actor=Depends(get_actor)):
    if not can_employee_drilldown(actor):
        raise HTTPException(
            404, "Для персонального просмотра нужен отдельный drilldown grant"
        )
    j = get_entity(db, journey_id, "journey")
    assert_employee(db, actor, j.parent_id)
    if not current_employee_record(db, actor, j.parent_id, j):
        raise HTTPException(404, "Программа недоступна")
    return JSONResponse(journey_view(j, actor), headers={"ETag": etag(j)})


@router.get("/employees/{employee_id}/timeline")
def employee_timeline(employee_id: str, db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *HR, "EMPLOYEE", "MANAGER")
    assert_employee(db, actor, employee_id)
    js = [
        j
        for j in entities(db, "journey", employee_id)
        if current_employee_record(db, actor, employee_id, j)
    ]
    return {
        "journeys": [journey_view(j, actor) for j in js],
        "items": [
            pack(a)
            for j in js
            for a in sorted(
                entities(db, "scheduled_action", j.id),
                key=lambda x: x.data.get("sequence_no", 0),
            )
        ],
    }


@router.post("/journeys/{journey_id}/commands")
def journey_command(
    journey_id: str, payload: dict, db=Depends(get_db), actor=Depends(get_actor)
):
    require_role(actor, *HR)
    j = get_entity(db, journey_id, "journey", lock=True)
    employee = assert_employee(db, actor, j.parent_id)
    if (
        j.data.get("episode_id") != employee.data.get("episode_id")
        or employee.data.get("employment_stage") == "ENDED"
        or j.status in ("CANCELLED", "COMPLETED")
    ):
        raise HTTPException(
            409,
            "Завершённый маршрут или прежний период найма нельзя активировать заново",
        )
    command = payload.get("command", payload.get("action"))
    if command == "PAUSE" and j.status in ("ACTIVE", "SCHEDULED"):
        update(j, paused_at=now().isoformat(), previous_status=j.status)
        j.status = "PAUSED"
    elif command == "RESUME" and j.status == "PAUSED":
        if j.data.get("requires_hr_review") and not payload.get("reason"):
            raise HTTPException(
                422, "Укажите основание продолжения маршрута после окончания ИС"
            )
        policy = payload.get("resume_policy", {}).get("date_policy")
        if policy not in ("KEEP_DATES", "SHIFT_BY_PAUSE_DURATION"):
            raise HTTPException(
                422, "Выберите сохранить даты или сдвинуть на длительность паузы"
            )
        delta = now() - aware(j.data["paused_at"])
        j.status = j.data.get("previous_status", "ACTIVE")
        if policy == "SHIFT_BY_PAUSE_DURATION":
            for a in entities(db, "scheduled_action", j.id):
                if a.status == "PLANNED" and a.data.get("due_at"):
                    update(a, due_at=(aware(a.data["due_at"]) + delta).isoformat())
        update(
            j,
            resumed_at=now().isoformat(),
            resume_policy=policy,
            requires_hr_review=False,
        )
    elif command == "CANCEL":
        cancel_journey(db, j, payload.get("reason", "HR_CANCELLED"))
    elif command == "ACTIVATE":
        e = get_entity(db, j.parent_id, "employee")
        update(e, employment_stage="ACTIVE")
        sync_employment_episode(db, e)
        j.status = "ACTIVE"
        replan_journey(db, j, e)
    else:
        raise HTTPException(409, "Команда не соответствует состоянию маршрута")
    event(db, "journey_event", j.id, command, actor.id)
    audit(
        db,
        actor,
        "journey.command",
        j.id,
        {"command": command},
        reason=payload.get("reason"),
    )
    commit(db)
    return reply(j)


@router.post("/journeys/{journey_id}/signals")
def journey_signal(
    journey_id: str, payload: dict, db=Depends(get_db), actor=Depends(get_actor)
):
    require_role(actor, *HR)
    j = get_entity(db, journey_id, "journey", lock=True)
    employee = assert_employee(db, actor, j.parent_id)
    if (
        j.status in ("CANCELLED", "COMPLETED")
        or employee.data.get("employment_stage") == "ENDED"
        or j.data.get("episode_id") != employee.data.get("episode_id")
    ):
        raise HTTPException(409, "Нельзя изменять завершённый период адаптации")
    code = payload.get("signal_code")
    allowed = {
        "RECRUITER_PROFILE_APPROVED",
        "RECRUITER_PLAN_APPROVED",
        "RECRUITER_PHOTO_APPROVED",
        "HR_START_APPROVED",
    }
    graph = get_entity(db, j.data["version_id"], "scenario_version").data["graph"]
    allowed |= {
        s["trigger"]["signal_code"]
        for s in graph["steps"]
        if s.get("trigger", {}).get("type") == "SIGNAL"
    }
    allowed |= {
        s["required_signal"] for s in graph["steps"] if s.get("required_signal")
    }
    if code not in allowed:
        raise HTTPException(422, "Неизвестный сигнал программы")
    signals = {**j.data.get("signals", {}), code: now().isoformat()}
    update(j, signals=signals)
    replan_journey(db, j, get_entity(db, j.parent_id, "employee"))
    event(
        db,
        "journey_event",
        j.id,
        "SIGNAL",
        actor.id,
        signal_code=code,
        reason=payload.get("reason"),
    )
    audit(
        db,
        actor,
        "journey.signal",
        j.id,
        {"signal_code": code},
        reason=payload.get("reason"),
    )
    commit(db)
    return reply(j)


@router.post("/journeys/{journey_id}/plans")
def journey_plan(
    journey_id: str, payload: dict, db=Depends(get_db), actor=Depends(get_actor)
):
    require_role(actor, *HR)
    j = get_entity(db, journey_id, "journey")
    assert_employee(db, actor, j.parent_id)
    e = get_entity(db, j.parent_id, "employee")
    scenario = _scenario_access(db, actor, j.data["scenario_id"])
    if not _scenario_applies(scenario, e):
        raise HTTPException(409, "Программа больше не применяется к сотруднику")
    if (
        j.status in ("CANCELLED", "COMPLETED")
        or e.data.get("employment_stage") == "ENDED"
        or j.data.get("episode_id") != e.data.get("episode_id")
    ):
        raise HTTPException(409, "Нельзя перенести завершённый период адаптации")
    v = get_entity(
        db, payload.get("version_id") or j.data["version_id"], "scenario_version"
    )
    if v.parent_id != j.data["scenario_id"]:
        raise HTTPException(422, "Другая программа")
    if v.status != "PUBLISHED":
        raise HTTPException(409, "Для переноса нужна опубликованная версия программы")
    result = simulate(v.data["graph"], e.data, calendar_for(db, e))
    p = save_entity(
        db,
        "journey_plan",
        {
            "journey_id": j.id,
            "journey_version": j.version,
            "version_id": v.id,
            "target_version": v.version,
            "timeline": result["timeline"],
            "preview_hash": semantic_hash(v.data["graph"]),
            "expires_at": (now() + timedelta(minutes=30)).isoformat(),
        },
        parent_id=j.id,
        owner_id=actor.id,
    )
    commit(db)
    return reply(p, 201)


@router.post("/journeys/{journey_id}/plans/apply")
def journey_plan_apply(
    journey_id: str, payload: dict, db=Depends(get_db), actor=Depends(get_actor)
):
    require_role(actor, *HR)
    j = get_entity(db, journey_id, "journey", lock=True)
    employee = assert_employee(db, actor, j.parent_id)
    if (
        j.status in ("CANCELLED", "COMPLETED")
        or employee.data.get("employment_stage") == "ENDED"
        or j.data.get("episode_id") != employee.data.get("episode_id")
    ):
        raise HTTPException(409, "Нельзя перенести завершённый период адаптации")
    p = get_entity(
        db, payload.get("plan_id", payload.get("preview_id", "")), "journey_plan"
    )
    if (
        p.parent_id != j.id
        or p.status != "ACTIVE"
        or p.data["journey_version"] != j.version
        or aware(p.data["expires_at"]) < now()
    ):
        raise HTTPException(409, "План устарел")
    target = get_entity(db, p.data["version_id"], "scenario_version")
    scenario = _scenario_access(db, actor, target.parent_id)
    if not _scenario_applies(scenario, employee):
        raise HTTPException(409, "Программа больше не применяется к сотруднику")
    if (
        target.status != "PUBLISHED"
        or target.parent_id != j.data["scenario_id"]
        or p.data.get("target_version") != target.version
        or semantic_hash(target.data["graph"]) != p.data["preview_hash"]
        or payload.get("preview_hash") != p.data["preview_hash"]
    ):
        raise HTTPException(409, "Опубликованная версия или preview изменились")
    if len(str(payload.get("reason", ""))) < 3:
        raise HTTPException(422, "Укажите причину переноса программы")
    if any(s.status not in TERMINAL_SURVEYS for s in entities(db, "survey_run", j.id)):
        raise HTTPException(409, "Нельзя заменить версию при активной анкете")
    target_steps = {step["step_key"] for step in target.data["graph"]["steps"]}
    for action in entities(db, "scheduled_action", j.id):
        if action.data["step_key"] not in target_steps and action.status in (
            "PLANNED",
            "WAITING",
            "NOT_APPLICABLE",
            "ENQUEUED",
        ):
            linked = list(
                db.scalars(
                    select(Outbox).where(
                        Outbox.data["action_id"].as_string() == action.id
                    )
                )
            )
            if any(
                out.status in ("SENDING", "UNKNOWN", "ACCEPTED", "SENT")
                for out in linked
            ):
                continue
            action.status = "CANCELLED"
            for out in linked:
                if out.status in (
                    "PENDING",
                    "WAITING_BINDING",
                    "READY",
                    "RETRY",
                    "QUEUED",
                ):
                    out.status = "SUPPRESSED"
                    out.data = {
                        **out.data,
                        "suppression_reason": "JOURNEY_VERSION_MIGRATED",
                    }
    update(j, version_id=p.data["version_id"])
    replan_journey(db, j, employee, bump_revision=True)
    p.status = "APPLIED"
    event(
        db,
        "journey_event",
        j.id,
        "VERSION_MIGRATED",
        actor.id,
        version_id=target.id,
        plan_id=p.id,
        reason=str(payload["reason"])[:2000],
    )
    audit(
        db,
        actor,
        "journey.migrate",
        j.id,
        {"plan_id": p.id, "version_id": target.id},
        reason=payload["reason"],
    )
    commit(db)
    return reply(j)


def question_text(q, employee):
    return render_text(q.get("prompt", {}), employee)


def current_question(run):
    return next(
        (
            q
            for q in run.data["definition"]["questions"]
            if q["question_key"] == run.data.get("current_question_key")
        ),
        None,
    )


def survey_view(run):
    q = current_question(run)
    return {
        **pack(run),
        "state_seq": run.data.get("question_revision", 1),
        "current_question": q,
        "progress": {
            "answered": len(run.data.get("answers", {})),
            "total": len(run.data["definition"]["questions"]),
        },
    }


def send_survey_question(db, run, employee, opening=False):
    q = current_question(run)
    if not q:
        if run.data.get("awaiting_completion_confirmation"):
            emit(
                db,
                employee.id,
                "Все ответы сохранены. Подтвердите завершение опроса.",
                [
                    {
                        "text": "Завершить",
                        "payload": {"action": "SURVEY_FINISH", "survey_run_id": run.id},
                    }
                ],
                f"survey:{run.id}:review-resume:{run.version}",
            )
        return
    revision = run.data.get("question_revision", 1)
    base = {
        "survey_run_id": run.id,
        "question_id": q["question_key"],
        "state_revision": revision,
    }
    buttons = []
    if q["type"] in ("SINGLE_CHOICE", "YES_NO"):
        buttons = [
            {
                "text": render_text(o.get("label", {}), employee),
                "payload": {"action": "SURVEY_ANSWER", **base, "value": o["value"]},
            }
            for o in q.get("options", [])
            if not (
                employee.data.get("site_id") == "GTP"
                and q["question_key"] == "M1W3-Q3"
                and o["value"] == "remote"
            )
        ]
    elif q["type"] == "NUMERIC":
        buttons = [
            {"text": str(i), "payload": {"action": "SURVEY_ANSWER", **base, "value": i}}
            for i in range(q.get("minimum", 1), q.get("maximum", 10) + 1)
        ]
    elif q["type"] == "MULTI_CHOICE":
        selected = run.data.get("selection_draft", [])
        buttons = [
            {
                "text": ("✓ " if o["value"] in selected else "")
                + render_text(o["label"], employee),
                "payload": {"action": "SURVEY_TOGGLE", **base, "value": o["value"]},
            }
            for o in q.get("options", [])
            if not (
                employee.data.get("site_id") == "GTP"
                and q["question_key"] == "M1W3-Q3"
                and o["value"] == "remote"
            )
        ]
        buttons.append(
            {"text": "Готово", "payload": {"action": "SURVEY_SUBMIT_SELECTION", **base}}
        )
    else:
        buttons = [
            {
                "text": "Ответить на этот вопрос",
                "payload": {"action": "CAPTURE_OPEN", **base},
            }
        ]
    buttons += [
        {
            "text": "Отказаться от опроса",
            "payload": {"action": "SURVEY_DECLINE", "survey_run_id": run.id},
        },
        {
            "text": "Не хочу отвечать",
            "payload": {"action": "SURVEY_ANSWER", **base, "outcome": "DECLINED"},
        },
        {"text": "Пауза", "payload": {"action": "SURVEY_PAUSE", **base}},
    ]
    emit(
        db,
        employee.id,
        question_text(q, employee),
        buttons,
        f"survey:{run.id}:q:{q['question_key']}:r{revision}:selection{run.data.get('selection_revision',0)}",
        {
            "survey_run_id": run.id,
            "opening": opening,
            "question_key": q["question_key"],
        },
    )


def open_survey(db, journey, step, employee):
    # Campaigns and onboarding share a single employee input lane. Serialize on
    # that aggregate, not just an individual journey, before opening a survey.
    employee = get_entity(db, employee.id, "employee", lock=True)
    key = step["survey"]["survey_key"]
    existing = next(
        (
            s
            for s in entities(db, "survey_run", journey.id)
            if s.data["survey_key"] == key
        ),
        None,
    )
    if existing:
        return existing
    active = next(
        (
            s
            for s in entities(db, "survey_run")
            if s.data.get("employee_id") == employee.id
            and s.data.get("episode_id") == employee.data.get("episode_id")
            and s.status not in TERMINAL_SURVEYS
        ),
        None,
    )
    if active:
        return None
    definition = copy.deepcopy(step["survey"])
    if employee.data.get("site_id") == "GTP":
        for q in definition["questions"]:
            if q["question_key"] == "M1W3-Q3":
                q["options"] = [o for o in q["options"] if o["value"] != "remote"]
                q["max_selected"] = len(q["options"])
    run = save_entity(
        db,
        "survey_run",
        {
            "employee_id": employee.id,
            "journey_id": journey.id,
            "episode_id": journey.data.get("episode_id"),
            "scenario_version_id": journey.data["version_id"],
            "step_key": step["step_key"],
            "survey_key": key,
            "definition": definition,
            "current_question_key": definition["start_question_key"],
            "question_revision": 1,
            "answers": {},
            "opened_at": now().isoformat(),
            "first_question_accepted_at": None,
            "expires_at": None,
            "reminder_count": 0,
        },
        status="IN_PROGRESS",
        parent_id=journey.id,
    )
    send_survey_question(db, run, employee, opening=True)
    return run


@router.get("/employees/{employee_id}/surveys")
def employee_surveys(employee_id: str, db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *HR, "EMPLOYEE")
    assert_employee(db, actor, employee_id)
    return {
        "items": [
            survey_view(s)
            for s in entities(db, "survey_run")
            if s.data.get("employee_id") == employee_id
            and current_employee_record(db, actor, employee_id, s)
        ]
    }


@router.get("/survey-runs/{survey_run_id}")
def survey_get(survey_run_id: str, db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *HR, "EMPLOYEE")
    s = get_entity(db, survey_run_id, "survey_run")
    assert_employee(db, actor, s.data["employee_id"])
    if not current_employee_record(db, actor, s.data["employee_id"], s):
        raise HTTPException(404, "Анкета недоступна")
    return JSONResponse(survey_view(s), headers={"ETag": etag(s)})


def assert_answer_actor(db, actor, employee_id):
    employee = assert_employee(db, actor, employee_id)
    if employee.data.get("employment_stage") == "ENDED":
        raise HTTPException(403, "Период найма завершён")
    if (
        actor.role not in ("EMPLOYEE",)
        and getattr(actor, "employee_id", None) != employee_id
    ):
        raise HTTPException(
            403,
            "Ответ анкеты сохраняет сам сотрудник; HR использует отдельное исправление с аудитом",
        )


def assert_campaign_accepting(db, run):
    if run.data.get("campaign_id"):
        from .survey_campaigns import delivery_allowed

        allowed, _ = delivery_allowed(db, run)
        if not allowed:
            raise HTTPException(409, "Опрос завершён или больше недоступен")


def submit_survey_answer(db, actor, run_id, payload):
    run = get_entity(db, run_id, "survey_run", lock=True)
    assert_answer_actor(db, actor, run.data["employee_id"])
    assert_campaign_accepting(db, run)
    employee = get_entity(db, run.data["employee_id"], "employee")
    if run.data.get("episode_id") and run.data["episode_id"] != employee.data.get(
        "episode_id"
    ):
        raise HTTPException(409, "Анкета относится к прежнему периоду найма")
    conversation = conversation_for(db, employee.id)
    if conversation and conversation.data.get("mode") == "HUMAN":
        raise HTTPException(409, "Анкета приостановлена на время разговора с HR")
    if run.data.get("expires_at") and aware(run.data["expires_at"]) < now():
        run.status = "EXPIRED"
        raise HTTPException(409, "Срок анкеты истёк; HR может переоткрыть её")
    qid = payload.get("question_id") or payload.get("question_key")
    previous = run.data.get("answers", {}).get(qid)
    if previous and run.data.get("current_question_key") != qid:
        if previous.get("request_id") and previous.get("request_id") == payload.get(
            "request_id"
        ):
            return survey_view(run)
        raise HTTPException(
            409, "Этот вопрос уже отвечен; старая кнопка не отвечает на следующий"
        )
    if run.status != "IN_PROGRESS":
        raise HTTPException(409, "Анкета сейчас не принимает ответы")
    if qid != run.data.get("current_question_key"):
        raise HTTPException(409, "Кнопка относится к другому вопросу")
    if payload.get("expected_state_seq") is not None and payload[
        "expected_state_seq"
    ] != run.data.get("question_revision", 1):
        raise HTTPException(409, "Состояние анкеты изменилось")
    if payload.get("state_revision") is not None and payload[
        "state_revision"
    ] != run.data.get("question_revision", 1):
        raise HTTPException(409, "Кнопка устарела")
    q = current_question(run)
    if (
        payload.get("comment") is not None
        and "value" not in payload
        and run.data.get("pending_answer")
    ):
        payload = {**run.data["pending_answer"], **payload}
    try:
        answer = validate_answer(q, payload)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    if answer.get("needs_comment"):
        update(run, pending_answer=answer)
        buttons = [
            {
                "text": "Добавить комментарий",
                "payload": {
                    "action": "CAPTURE_OPEN",
                    "survey_run_id": run.id,
                    "question_id": qid,
                    "capture_kind": "COMMENT",
                },
            },
            {
                "text": "Не раскрывать причину",
                "payload": {
                    "action": "SURVEY_COMMENT_DECLINE",
                    "survey_run_id": run.id,
                    "question_id": qid,
                    "state_revision": run.data.get("question_revision", 1),
                },
            },
        ]
        emit(
            db,
            employee.id,
            render_text(q.get("comment_prompt", {}), employee),
            buttons,
            f"survey:{run.id}:comment:{qid}:r{run.data.get('question_revision',1)}",
        )
        return {**survey_view(run), "needs_comment": True}
    answer.update(
        answered_at=now().isoformat(),
        request_id=payload.get("request_id"),
        revision=len(entities(db, "survey_answer_revision", run.id)) + 1,
    )
    save_entity(db, "survey_answer_revision", answer, parent_id=run.id)
    answers = {**run.data.get("answers", {}), qid: answer}
    next_q = q.get("next_question_key")
    for tr in q.get("transitions", []):
        if evaluate_condition(tr.get("condition"), answers, employee.data):
            next_q = tr.get("target_question_key")
            break
    update(
        run,
        answers=answers,
        pending_answer=None,
        selection_draft=[],
        current_question_key=next_q,
        question_revision=run.data.get("question_revision", 1) + 1,
    )
    feedback_text = answer.get("comment") or (
        answer.get("value")
        if q["type"] == "TEXT" and isinstance(answer.get("value"), str)
        else None
    )
    if feedback_text and not run.data.get("campaign_id"):
        feedback = save_entity(
            db,
            "feedback",
            {
                "text": feedback_text,
                "employee_id": employee.id,
                "episode_id": run.data.get("episode_id"),
                "source_site_id": employee.data.get("site_id"),
                "source_legal_entity_id": employee.data.get("legal_entity_id"),
                "survey_run_id": run.id,
                "question_id": qid,
                "answer_revision": answer["revision"],
                "question_prompt": question_text(q, employee),
            },
            parent_id=run.id,
            status="PENDING_ANALYSIS",
        )
        enqueue(
            db,
            "FEEDBACK_ANALYSIS",
            {"feedback_id": feedback.id},
            f"analyze:{run.id}:{qid}:{answer['revision']}",
        )
    if not run.data.get("campaign_id"):
        survey_branch_rules(db, run, employee, qid, answer)
        journey = get_entity(db, run.parent_id, "journey")
        update(journey, answers={**journey.data.get("answers", {}), qid: answer})
    if run.data.get("editing_question_key"):
        next_q = next(
            (
                q["question_key"]
                for q in run.data["definition"]["questions"]
                if q.get("required") and q["question_key"] not in answers
            ),
            None,
        )
        update(run, current_question_key=next_q, editing_question_key=None)
    if next_q:
        send_survey_question(db, run, employee)
    else:
        update(run, awaiting_completion_confirmation=True, editing_question_key=None)
        emit(
            db,
            employee.id,
            "Ответы сохранены. Подтвердите завершение анкеты; до этого можно исправить ответ.",
            [
                {
                    "text": "Завершить опрос",
                    "payload": {"action": "SURVEY_FINISH", "survey_run_id": run.id},
                }
            ]
            + [
                {
                    "text": "Изменить " + str(i + 1),
                    "payload": {
                        "action": "SURVEY_EDIT",
                        "survey_run_id": run.id,
                        "question_id": item["question_key"],
                    },
                }
                for i, item in enumerate(run.data["definition"]["questions"])
            ],
            f'survey:{run.id}:review:{run.data.get("question_revision",1)}',
        )
    return survey_view(run)


@router.post("/survey-runs/{survey_run_id}/answers")
def survey_answer(
    survey_run_id: str, payload: dict, db=Depends(get_db), actor=Depends(get_actor)
):
    result = submit_survey_answer(db, actor, survey_run_id, payload)
    audit(
        db,
        actor,
        "survey.answer",
        survey_run_id,
        {"question_id": payload.get("question_id")},
    )
    commit(db)
    return result


@router.post("/survey-runs/{survey_run_id}/commands")
def survey_command(
    survey_run_id: str, payload: dict, db=Depends(get_db), actor=Depends(get_actor)
):
    run = get_entity(db, survey_run_id, "survey_run", lock=True)
    employee = assert_employee(db, actor, run.data["employee_id"])
    if (
        run.data.get("episode_id") != employee.data.get("episode_id")
        or employee.data.get("employment_stage") == "ENDED"
    ):
        raise HTTPException(409, "Команда относится к завершённому периоду найма")
    cmd = payload.get("command", payload.get("action"))
    if cmd in {"RESUME", "REOPEN"}:
        assert_campaign_accepting(db, run)
    if cmd == "FINISH":
        result = finish_survey(db, actor, run.id)
        commit(db)
        return result
    if cmd == "EDIT_ANSWER":
        result = edit_survey_answer(db, actor, run.id, payload.get("question_id"))
        commit(db)
        return result
    if cmd == "REOPEN":
        require_role(actor, *HR)
        if not payload.get("reason") or not payload.get("expires_at"):
            raise HTTPException(422, "Нужны причина и новый срок")
        expires = aware(payload["expires_at"])
        if expires <= now():
            raise HTTPException(422, "Новый срок должен быть в будущем")
        if run.status not in ("EXPIRED", "DECLINED", "PAUSED"):
            raise HTTPException(409, "Анкета не требует переоткрытия")
        update(run, expires_at=expires.isoformat(), reopened_at=now().isoformat())
        run.status = "IN_PROGRESS"
    else:
        assert_answer_actor(db, actor, run.data["employee_id"])
        if cmd == "PAUSE" and run.status == "IN_PROGRESS":
            run.status = "PAUSED"
        elif cmd == "RESUME" and run.status == "PAUSED":
            if run.data.get("expires_at") and aware(run.data["expires_at"]) < now():
                run.status = "EXPIRED"
                commit(db)
                raise HTTPException(409, "Срок анкеты истёк")
            run.status = "IN_PROGRESS"
        elif cmd == "DECLINE" and run.status not in TERMINAL_SURVEYS:
            run.status = "DECLINED"
            update(run, declined_at=now().isoformat())
        else:
            raise HTTPException(409, "Команда не подходит к состоянию анкеты")
    event(db, "survey_event", run.id, cmd, actor.id, reason=payload.get("reason"))
    run.version += 1
    if run.status == "IN_PROGRESS":
        send_survey_question(
            db, run, get_entity(db, run.data["employee_id"], "employee")
        )
    commit(db)
    return survey_view(run)


def open_capture(db, actor, payload):
    payload = {**payload, **(payload.get("target") or {})}
    if payload.get("kind") == "SCENARIO_QUESTION" and not payload.get(
        "scenario_question_id"
    ):
        target_question = next(
            (
                q
                for q in entities(db, "scenario_question", payload.get("journey_id"))
                if q.data.get("step_key") == payload.get("step_key")
                and q.data.get("question", {}).get("question_key")
                == payload.get("question_id")
            ),
            None,
        )
        if not target_question:
            raise HTTPException(404, "Вопрос сценария не найден")
        payload["scenario_question_id"] = target_question.id
    run_id = payload.get("survey_run_id")
    sq_id = payload.get("scenario_question_id")
    target = (
        get_entity(db, run_id, "survey_run")
        if run_id
        else get_entity(db, sq_id, "scenario_question")
    )
    eid = target.data["employee_id"]
    from .chat import conversation_for as current_conversation

    conversation = current_conversation(db, get_entity(db, eid, "employee"))
    get_entity(db, conversation.id, "conversation", lock=True)
    target = get_entity(db, target.id, target.kind, lock=True)
    assert_answer_actor(db, actor, eid)
    assert_campaign_accepting(db, target)
    if not current_employee_record(db, actor, eid, target):
        raise HTTPException(404, "Вопрос недоступен")
    q = current_question(target) if run_id else target.data["question"]
    if not q:
        raise HTTPException(409, "Этот вопрос уже неактуален")
    qid = payload.get("question_id", q["question_key"])
    if qid != q["question_key"]:
        raise HTTPException(409, "Этот вопрос уже неактуален")
    if target.status not in ("IN_PROGRESS", "OPEN"):
        raise HTTPException(409, "Ввод для этого вопроса закрыт")
    conv = conversation_for(db, eid)
    if conv and conv.data.get("mode") == "HUMAN":
        raise HTTPException(409, "Сначала завершите разговор с HR")
    from .simple_scenarios import cancel_free_answers

    for cap in entities(db, "input_capture", eid):
        if cap.status in ("OPEN", "DRAFT"):
            if aware(cap.data["expires_at"]) <= now():
                cap.status = "EXPIRED"
                continue
            if (
                cap.data.get("survey_run_id") == run_id
                and cap.data.get("scenario_question_id") == sq_id
                and cap.data.get("question_id") == qid
                and cap.data.get("capture_kind")
                == payload.get("capture_kind", "ANSWER")
            ):
                cancel_free_answers(db, eid, "EXPLICIT_SURVEY_INPUT")
                return cap
            raise HTTPException(
                409, "Сначала отмените или завершите уже открытый ввод ответа"
            )
    kind = payload.get("capture_kind", "ANSWER")
    if kind == "COMMENT" and not target.data.get("pending_answer"):
        raise HTTPException(409, "Комментарий сейчас не запрашивается")
    cancel_free_answers(db, eid, "EXPLICIT_SURVEY_INPUT")
    cap = save_entity(
        db,
        "input_capture",
        {
            "employee_id": eid,
            "episode_id": get_entity(db, eid, "employee").data.get("episode_id"),
            "survey_run_id": run_id,
            "scenario_question_id": sq_id,
            "question_id": qid,
            "target_version": target.version,
            "capture_kind": kind,
            "expires_at": (now() + timedelta(minutes=15)).isoformat(),
            "draft": None,
            "draft_fingerprint": None,
        },
        parent_id=eid,
        status="OPEN",
    )
    emit(
        db,
        eid,
        "Следующее сообщение будет черновиком ответа на конкретный вопрос. После ввода подтвердите сохранение. Обычный вопрос Корсику можно задать после отмены.",
        [
            {
                "text": "Отменить ввод",
                "payload": {"action": "CAPTURE_CANCEL", "capture_id": cap.id},
            }
        ],
        f"capture:{cap.id}:open",
    )
    return cap


def capture_draft(db, actor, cap_id, payload):
    cap = get_entity(db, cap_id, "input_capture", lock=True)
    assert_answer_actor(db, actor, cap.data["employee_id"])
    if not current_employee_record(db, actor, cap.data["employee_id"], cap):
        raise HTTPException(404, "Черновик недоступен")
    if cap.status not in ("OPEN", "DRAFT") or aware(cap.data["expires_at"]) < now():
        raise HTTPException(409, "Ввод отменён или срок истёк")
    value = (
        payload.get("text")
        if payload.get("text") is not None
        else payload.get("attachment_id")
    )
    if not isinstance(value, str) or not value.strip() or len(value) > 12000:
        raise HTTPException(422, "Нужен непустой текст или вложение")
    fingerprint = hashlib.sha256(value.encode()).hexdigest()
    update(
        cap,
        draft=value.strip(),
        draft_fingerprint=fingerprint,
        draft_type="ATTACHMENT" if payload.get("attachment_id") else "TEXT",
    )
    cap.status = "DRAFT"
    emit(
        db,
        cap.data["employee_id"],
        "Сохранить как ответ?\n\n"
        + (value[:2500] if not payload.get("attachment_id") else "Прикреплённое фото"),
        [
            {
                "text": "Сохранить ответ",
                "payload": {
                    "action": "CAPTURE_CONFIRM",
                    "capture_id": cap.id,
                    "draft_fingerprint": fingerprint,
                },
            },
            {
                "text": "Это вопрос Корсику",
                "payload": {"action": "CAPTURE_TO_QUESTION", "capture_id": cap.id},
            },
            {
                "text": "Отмена",
                "payload": {"action": "CAPTURE_CANCEL", "capture_id": cap.id},
            },
        ],
        f"capture:{cap.id}:draft:{fingerprint}",
    )
    return cap


def confirm_capture(db, actor, cap_id, payload):
    cap = get_entity(db, cap_id, "input_capture", lock=True)
    assert_answer_actor(db, actor, cap.data["employee_id"])
    if not current_employee_record(db, actor, cap.data["employee_id"], cap):
        raise HTTPException(404, "Черновик недоступен")
    if cap.status == "CONFIRMED":
        return pack(cap)
    if cap.status != "DRAFT" or aware(cap.data["expires_at"]) < now():
        raise HTTPException(409, "Ввод устарел или отменён")
    if payload.get("draft_fingerprint") != cap.data["draft_fingerprint"]:
        raise HTTPException(409, "Подтвердите текущий черновик")
    conv = conversation_for(db, cap.data["employee_id"])
    if conv and conv.data.get("mode") == "HUMAN":
        raise HTTPException(409, "Во время HUMAN ответ не подтверждается")
    p = {
        "question_id": cap.data["question_id"],
        "value": cap.data["draft"],
        "request_id": "capture:" + cap.id,
    }
    if cap.data.get("survey_run_id"):
        target = get_entity(db, cap.data["survey_run_id"], "survey_run", lock=True)
        assert_campaign_accepting(db, target)
        if target.version != cap.data["target_version"]:
            raise HTTPException(409, "Вопрос изменился после открытия ввода")
        if cap.data.get("capture_kind") == "COMMENT":
            p = {
                "question_id": cap.data["question_id"],
                "comment": cap.data["draft"],
                "comment_outcome": "PROVIDED",
                "request_id": "capture:" + cap.id,
            }
        result = submit_survey_answer(db, actor, target.id, p)
    else:
        result = scenario_question_answer(
            db, actor, cap.data["scenario_question_id"], p
        )
    cap.status = "CONFIRMED"
    update(cap, confirmed_at=now().isoformat())
    return {"capture": pack(cap), "result": result}


@router.post("/input-captures")
def create_input_capture(payload: dict, db=Depends(get_db), actor=Depends(get_actor)):
    e = open_capture(db, actor, payload)
    commit(db)
    return reply(e, 201)


@router.get("/input-captures/{capture_id}")
def get_input_capture(capture_id: str, db=Depends(get_db), actor=Depends(get_actor)):
    e = get_entity(db, capture_id, "input_capture")
    assert_answer_actor(db, actor, e.data["employee_id"])
    if not current_employee_record(db, actor, e.data["employee_id"], e):
        raise HTTPException(404, "Черновик недоступен")
    return reply(e)


@router.put("/input-captures/{capture_id}/draft")
def update_capture_draft(
    capture_id: str, payload: dict, db=Depends(get_db), actor=Depends(get_actor)
):
    e = capture_draft(db, actor, capture_id, payload)
    commit(db)
    return reply(e)


@router.post("/input-captures/{capture_id}/confirm")
def input_capture_confirm(
    capture_id: str, payload: dict, db=Depends(get_db), actor=Depends(get_actor)
):
    result = confirm_capture(db, actor, capture_id, payload)
    commit(db)
    return result


@router.post("/input-captures/{capture_id}/cancel")
def input_capture_cancel(capture_id: str, db=Depends(get_db), actor=Depends(get_actor)):
    e = get_entity(db, capture_id, "input_capture", lock=True)
    assert_answer_actor(db, actor, e.data["employee_id"])
    if not current_employee_record(db, actor, e.data["employee_id"], e):
        raise HTTPException(404, "Черновик недоступен")
    e.status = "CANCELLED"
    commit(db)
    return reply(e)


def scenario_question_answer(db, actor, question_id, payload):
    sq = get_entity(db, question_id, "scenario_question", lock=True)
    assert_answer_actor(db, actor, sq.data["employee_id"])
    if not current_employee_record(db, actor, sq.data["employee_id"], sq):
        raise HTTPException(404, "Вопрос недоступен")
    q = sq.data["question"]
    if sq.status == "COMPLETED":
        return pack(sq)
    if payload.get("question_id") and payload["question_id"] != q["question_key"]:
        raise HTTPException(409, "Старая кнопка")
    if sq.status != "OPEN":
        raise HTTPException(409, "Вопрос закрыт")
    try:
        answer = validate_answer(q, payload)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    e = get_entity(db, sq.data["employee_id"], "employee")
    if q["type"] == "PHOTO" and answer.get("value"):
        from .assets import can_asset

        asset = get_entity(db, answer["value"], "asset")
        if (
            asset.owner_id != e.id
            or not can_asset(db, actor, asset)
            or asset.status not in ("READY", "APPROVED")
            or asset.data.get("content_type")
            not in ("image/jpeg", "image/png", "image/webp")
        ):
            raise HTTPException(403, "Фото недоступно или не проверено")
    if answer.get("outcome") == "ANSWERED":
        if q.get("answer_binding") == "ADDRESS_FORM":
            update(e, address_form=answer["value"])
        elif q.get("answer_binding") == "PREFERRED_NAME":
            update(e, preferred_name=answer["value"])
        elif q["question_key"] == "PROFILE-BIO":
            update(
                e,
                profile_bio=answer["value"],
                profile_publication_status="PENDING_HR_APPROVAL",
            )
        elif q["question_key"] == "PROFILE-PHOTO":
            update(
                e,
                profile_photo_asset_id=answer["value"],
                profile_publication_status="PENDING_HR_APPROVAL",
            )
    update(sq, answer=answer, completed_at=now().isoformat())
    sq.status = "COMPLETED"
    j = get_entity(db, sq.parent_id, "journey")
    answers = {**j.data.get("answers", {}), q["question_key"]: answer}
    events = {
        **j.data.get("events", {}),
        f"{sq.data['step_key']}:STEP_COMPLETED": now().isoformat(),
    }
    update(
        j,
        answers=answers,
        events=events,
        completed_steps=list(
            set(j.data.get("completed_steps", []) + [sq.data["step_key"]])
        ),
    )
    replan_journey(db, j, e)
    if q["question_key"].startswith("ONB-09-") and answer.get("value") == "no":
        create_case_entity(
            db,
            e.id,
            "SURVEY_BRANCH",
            "Не предоставлены доступы или оборудование",
            kind="BLOCKER",
            unique_key="question:" + sq.id,
            category="IT_ACCESS" if e.data.get("site_id") != "GTP" else "EQUIPMENT",
        )
    if answer.get("value") == "yes" and q["question_key"].startswith(
        ("ONB-03-", "ONB-08-", "ONB-11-", "ONB-17-")
    ):
        emit(
            db,
            e.id,
            "Актуального материала по этому вопросу пока нет в базе знаний. Напишите вопрос обычным сообщением — мы уточним информацию.",
            [],
            f"question:{sq.id}:material-pending",
        )
    if q["question_key"] == "READY-Q1" and answer.get("value") == "worried":
        emit(
            db,
            e.id,
            "Если что-то беспокоит перед первым днём, напишите об этом обычным сообщением — мы поможем разобраться.",
            [],
            f"question:{sq.id}:support",
        )
    return pack(sq)


@router.post("/scenario-questions/{question_id}/answers")
def answer_scenario_question(
    question_id: str, payload: dict, db=Depends(get_db), actor=Depends(get_actor)
):
    result = scenario_question_answer(db, actor, question_id, payload)
    commit(db)
    return result


def survey_branch_rules(db, run, employee, qid, answer):
    if answer["outcome"] == "DECLINED":
        return
    value = answer.get("value")
    reasons = []
    if qid == "D3-Q1" and value in ("B", "C"):
        reasons.append("Вопросы при оформлении")
    if qid == "D3-Q2" and value == "C":
        reasons.append("Нужна помощь познакомиться с коллегами")
    if qid == "D3-Q3" and value in ("B", "C"):
        reasons.append("Уточнить задачи на испытательный срок")
    if qid == "M1-Q1" and value == "3":
        reasons.append("Нужна помощь с адаптационным планом")
    if qid == "M1W3-Q1" and value in ("indifferent", "tired"):
        reasons.append("Сотрудник отметил трудности адаптации")
    if qid == "M1W3-Q2" and "nobody" in (value or []):
        reasons.append("Сотрудник отметил отсутствие поддержки")
    if qid == "END-Q2" and value in ("unsure", "leave"):
        reasons.append("Нужен личный разговор о продолжении работы")
    if reasons:
        case = create_case_entity(
            db,
            employee.id,
            "SURVEY_BRANCH",
            "; ".join(reasons),
            unique_key="survey:" + run.id,
            source_survey_run_id=run.id,
        )
        old = case.data.get("reasons", [])
        update(case, reasons=list(dict.fromkeys(old + reasons)))
        emit(
            db,
            employee.id,
            "Если хотите, HR поможет обсудить этот вопрос. Ответ анкеты сохранён; опрос можно продолжить.",
            [
                {
                    "text": "Поговорить с HR",
                    "payload": {"action": "HANDOFF", "case_id": case.id},
                }
            ],
            f"survey:{run.id}:support:{qid}",
        )


def create_case_entity(
    db, employee_id, reason, description, kind="GENERAL", unique_key=None, **extra
):
    e = get_entity(db, employee_id, "employee")
    current_episode = e.data.get("episode_id")
    if unique_key:
        old = next(
            (
                e
                for e in entities(db, "case")
                if e.data.get("unique_key") == unique_key
                and e.data.get("employee_id") == employee_id
                and e.data.get("episode_id") == current_episode
            ),
            None,
        )
        if old:
            return old
    owner = e.data.get("assigned_hr_id")
    policy = next((p for p in entities(db, "sla_policy") if p.status == "ACTIVE"), None)
    due = None
    calendar_pin = None
    sla_warning = None
    if policy:
        for rule in policy.data.get("queue_rules", []):
            scope = rule.get("scope", {})
            if scope.get("site_ids") and e.data.get("site_id") not in scope["site_ids"]:
                continue
            if (
                scope.get("legal_entity_ids")
                and e.data.get("legal_entity_id") not in scope["legal_entity_ids"]
            ):
                continue
            calendar_pin = rule.get("calendar_version_id")
            calendar_version = db.get(Entity, calendar_pin)
            if calendar_version:
                try:
                    due = add_working_minutes(
                        now(),
                        rule["first_response_working_minutes"],
                        calendar_version.data,
                    )
                except ValueError as error:
                    sla_warning = str(error)
            else:
                sla_warning = "NEEDS_CALENDAR_REVIEW"
            break
    data = {
        "employee_id": employee_id,
        "episode_id": e.data.get("episode_id"),
        "reason": reason,
        "description": description[:12000],
        "kind": kind,
        "owner_id": owner,
        "unique_key": unique_key,
        "site_id": e.data.get("site_id"),
        "created_by_rule": True,
        "due_at": due.isoformat() if due else None,
        "sla_calendar_version_id": calendar_pin,
        "sla_warning": sla_warning,
        "sla_policy_id": policy.id if policy else None,
        "sla_policy_version": policy.version if policy else None,
        "sla_demo_assumption": not bool(policy),
        "assignment_revision": 1,
        "resolution_attempts": [],
        **extra,
    }
    case = save_entity(
        db, "case", data, status="OPEN", parent_id=employee_id, owner_id=owner
    )
    event(db, "case_event", case.id, "CREATED", reason=reason)
    return case


def case_authorize(db, actor, case, write=False):
    if actor.role not in HR + ("EMPLOYEE",):
        raise HTTPException(404, "Обращение недоступно")
    assert_employee(db, actor, case.data["employee_id"])
    if not current_employee_record(db, actor, case.data["employee_id"], case):
        raise HTTPException(404, "Обращение недоступно")
    if actor.role == "MANAGER":
        raise HTTPException(404, "Обращение недоступно")
    if write:
        require_role(actor, *HR)


def case_view(case, actor):
    value = pack(case)
    if actor.role == "EMPLOYEE":
        for key in (
            "internal_notes",
            "owner_id",
            "sla_policy_id",
            "source_survey_run_id",
        ):
            value.pop(key, None)
    return value


@router.get("/cases")
def list_cases(db=Depends(get_db), actor=Depends(get_actor)):
    if actor.role in ("MANAGER", "PLATFORM_ADMIN"):
        return {"items": []}
    return {
        "items": [
            case_view(e, actor)
            for e in entities(db, "case")
            if actor_can_employee(db, actor, e.data["employee_id"])
            and current_employee_record(db, actor, e.data["employee_id"], e)
        ]
    }


@router.post("/cases")
def create_case(payload: dict, db=Depends(get_db), actor=Depends(get_actor)):
    eid = payload.get("employee_id") or getattr(actor, "employee_id", None)
    assert_employee(db, actor, eid)
    if actor.role not in HR + ("EMPLOYEE",):
        raise HTTPException(403, "Нет права создавать обращение")
    description = payload.get("description", "Нужна помощь HR")
    if not isinstance(description, str) or not description.strip():
        raise HTTPException(422, "Опишите вопрос")
    kind = payload.get("kind", "GENERAL")
    if kind not in ("GENERAL", "BLOCKER"):
        raise HTTPException(422, "Неизвестный вид обращения")
    c = create_case_entity(
        db,
        eid,
        payload.get("reason", "EMPLOYEE_REQUEST"),
        description,
        kind,
        unique_key=payload.get("request_id"),
        category=payload.get("category", "OTHER"),
    )
    event(db, "case_event", c.id, "REQUESTED", actor.id)
    audit(db, actor, "case.create", c.id)
    commit(db)
    return reply(c, 201)


@router.get("/cases/{case_id}")
def get_case(case_id: str, db=Depends(get_db), actor=Depends(get_actor)):
    c = get_entity(db, case_id, "case")
    case_authorize(db, actor, c)
    return JSONResponse(case_view(c, actor), headers={"ETag": etag(c)})


@router.get("/cases/{case_id}/events")
def case_events(case_id: str, db=Depends(get_db), actor=Depends(get_actor)):
    c = get_entity(db, case_id, "case")
    case_authorize(db, actor, c)
    return {
        "items": [
            pack(e)
            for e in entities(db, "case_event", c.id)
            if actor.role != "EMPLOYEE" or not e.data.get("internal")
        ]
    }


@router.post("/cases/{case_id}/claim")
def claim_case(case_id: str, db=Depends(get_db), actor=Depends(get_actor)):
    candidate = get_entity(db, case_id, "case")
    case_authorize(db, actor, candidate, True)
    conv = None
    if candidate.data.get("kind") == "HANDOFF":
        conv = (
            get_entity(db, candidate.data["conversation_id"], "conversation", lock=True)
            if candidate.data.get("conversation_id")
            else conversation_for(db, candidate.data["employee_id"])
        )
        if conv:
            conv = get_entity(db, conv.id, "conversation", lock=True)
    c = get_entity(db, case_id, "case", lock=True)
    case_authorize(db, actor, c, True)
    if c.data.get("kind") == "HANDOFF":
        employee = get_entity(db, c.data["employee_id"], "employee")
        if (
            not conv
            or conv.status == "CLOSED"
            or employee.data.get("employment_stage") == "ENDED"
            or c.data.get("episode_id") != employee.data.get("episode_id")
            or conv.data.get("episode_id") != employee.data.get("episode_id")
            or conv.data.get("employee_id") != employee.id
        ):
            raise HTTPException(
                409, "Обращение относится к завершённому диалогу или найму"
            )
    owner = c.owner_id or c.data.get("owner_id") or c.data.get("assigned_user_id")
    if owner and owner != actor.id:
        raise HTTPException(409, "Обращение уже принято другим HR")
    if c.status == "IN_PROGRESS" and owner == actor.id:
        return reply(c)
    if c.status not in ("OPEN", "ASSIGNED", "REOPENED"):
        raise HTTPException(409, "Обращение не находится в очереди")
    c.owner_id = actor.id
    update(
        c, owner_id=actor.id, assigned_user_id=actor.id, claimed_at=now().isoformat()
    )
    c.status = "IN_PROGRESS"
    if c.data.get("kind") == "HANDOFF":
        if conv:
            update(
                conv,
                mode="HUMAN",
                active_handoff_case_id=c.id,
                state_seq=int(conv.data.get("state_seq", 0)) + 1,
            )
            for cap in entities(db, "input_capture", c.data["employee_id"]):
                if cap.status in ("OPEN", "DRAFT"):
                    cap.status = "CANCELLED"
            # Claiming is an operator action; employee delivery stays neutral
            # and has no separate connection announcement.
    event(db, "case_event", c.id, "CLAIMED", actor.id)
    commit(db)
    return reply(c)


@router.post("/cases/{case_id}/assign")
def assign_case(
    case_id: str,
    payload: dict,
    request: Request,
    db=Depends(get_db),
    actor=Depends(get_actor),
):
    c = get_entity(db, case_id, "case", lock=True)
    case_authorize(db, actor, c, True)
    check_etag(request, c)
    owner = db.get(User, payload.get("owner_id"))
    if not owner or owner.role not in HR or not owner.enabled:
        raise HTTPException(422, "Назначьте действующего HR")
    owner_actor = Actor(
        id=owner.id,
        name=owner.name,
        role=owner.role,
        scope=owner.scope,
        employee_id=owner.employee_id,
    )
    if not actor_can_employee(db, owner_actor, c.data["employee_id"]):
        raise HTTPException(422, "HR не имеет доступа к этой области")
    event(
        db,
        "case_event",
        c.id,
        "ASSIGNED",
        actor.id,
        old_owner=c.owner_id,
        new_owner=owner.id,
        reason=payload.get("reason"),
    )
    c.owner_id = owner.id
    c.status = "ASSIGNED"
    update(
        c,
        owner_id=owner.id,
        assigned_user_id=owner.id,
        assignment_revision=c.data.get("assignment_revision", 1) + 1,
    )
    audit(
        db,
        actor,
        "case.assign",
        c.id,
        {"owner_id": owner.id},
        reason=payload.get("reason"),
    )
    commit(db)
    return reply(c)


@router.post("/cases/{case_id}/notes")
def case_note(
    case_id: str, payload: dict, db=Depends(get_db), actor=Depends(get_actor)
):
    c = get_entity(db, case_id, "case", lock=True)
    case_authorize(db, actor, c, True)
    if not payload.get("text"):
        raise HTTPException(422, "Введите заметку")
    n = event(
        db,
        "case_event",
        c.id,
        "INTERNAL_NOTE",
        actor.id,
        text=str(payload["text"])[:12000],
        internal=True,
    )
    commit(db)
    return reply(n, 201)


@router.post("/cases/{case_id}/resolve")
def resolve_case(
    case_id: str,
    payload: dict,
    request: Request,
    db=Depends(get_db),
    actor=Depends(get_actor),
):
    c = get_entity(db, case_id, "case", lock=True)
    case_authorize(db, actor, c, True)
    check_etag(request, c)
    summary = payload.get("result_summary") or payload.get("resolution")
    if not summary:
        raise HTTPException(422, "Укажите результат")
    if c.status not in (
        "OPEN",
        "ASSIGNED",
        "IN_PROGRESS",
        "WAITING_EMPLOYEE",
        "WAITING_INTERNAL",
        "REOPENED",
    ):
        raise HTTPException(409, "Обращение уже завершено либо ожидает подтверждения")
    if c.data.get("kind") == "BLOCKER":
        attempt = {
            "resolution_attempt_id": new_id(),
            "confirmation_request_id": new_id(),
            "resolved_at": now().isoformat(),
            "resolved_by": actor.id,
            "summary": summary,
        }
        update(
            c,
            resolution_attempts=c.data.get("resolution_attempts", []) + [attempt],
            current_resolution_attempt_id=attempt["resolution_attempt_id"],
            confirmation_request_id=attempt["confirmation_request_id"],
            confirmation_due_at=(now() + timedelta(days=1)).isoformat(),
        )
        c.status = "RESOLVED_PENDING_CONFIRMATION"
        emit(
            db,
            c.data["employee_id"],
            "Ответственный сообщил об устранении проблемы. Теперь всё работает?",
            [
                {
                    "text": "Да, работает",
                    "payload": {
                        "action": "BLOCKER_CONFIRM",
                        "case_id": c.id,
                        **{
                            k: attempt[k]
                            for k in (
                                "resolution_attempt_id",
                                "confirmation_request_id",
                            )
                        },
                        "resolved": True,
                    },
                },
                {
                    "text": "Нет, ещё не работает",
                    "payload": {
                        "action": "BLOCKER_CONFIRM",
                        "case_id": c.id,
                        **{
                            k: attempt[k]
                            for k in (
                                "resolution_attempt_id",
                                "confirmation_request_id",
                            )
                        },
                        "resolved": False,
                    },
                },
            ],
            f"blocker:{c.id}:confirm:{attempt['resolution_attempt_id']}",
        )
    else:
        c.status = "RESOLVED"
        update(
            c,
            resolved_at=now().isoformat(),
            result_summary=summary,
            resolved_by=actor.id,
        )
    event(
        db,
        "case_event",
        c.id,
        "RESOLVED_ATTEMPT" if c.data.get("kind") == "BLOCKER" else "RESOLVED",
        actor.id,
        result_summary=summary,
    )
    audit(db, actor, "case.resolve", c.id, reason=summary)
    commit(db)
    return reply(c)


def blocker_response(db, actor, case_id, payload):
    c = get_entity(db, case_id, "case", lock=True)
    assert_answer_actor(db, actor, c.data["employee_id"])
    if not current_employee_record(db, actor, c.data["employee_id"], c):
        raise HTTPException(404, "Обращение недоступно")
    attempt = payload.get("resolution_attempt_id")
    request_id = payload.get("confirmation_request_id")
    if attempt != c.data.get(
        "current_resolution_attempt_id"
    ) or request_id != c.data.get("confirmation_request_id"):
        raise HTTPException(409, "Подтверждение относится к прежней попытке")
    if (
        c.status in ("CONFIRMED", "REOPENED")
        and c.data.get("responded_attempt_id") == attempt
    ):
        return pack(c)
    if c.status != "RESOLVED_PENDING_CONFIRMATION":
        raise HTTPException(409, "Обращение не ожидает этого подтверждения")
    resolved = payload.get("resolved")
    if type(resolved) is not bool:
        raise HTTPException(422, "Нужно явное да или нет")
    c.status = "CONFIRMED" if resolved else "REOPENED"
    update(
        c,
        responded_attempt_id=attempt,
        confirmed_at=now().isoformat() if resolved else None,
        employee_response_at=now().isoformat(),
    )
    event(db, "case_event", c.id, c.status, actor.id, resolution_attempt_id=attempt)
    return pack(c)


@router.post("/cases/{case_id}/resolution-response")
def resolution_response(
    case_id: str, payload: dict, db=Depends(get_db), actor=Depends(get_actor)
):
    result = blocker_response(db, actor, case_id, payload)
    commit(db)
    return result


@router.post("/cases/{case_id}/reopen")
def reopen_case(
    case_id: str, payload: dict, db=Depends(get_db), actor=Depends(get_actor)
):
    c = get_entity(db, case_id, "case", lock=True)
    case_authorize(db, actor, c, True)
    if not payload.get("reason"):
        raise HTTPException(422, "Укажите причину")
    if c.status not in (
        "CLOSED",
        "RESOLVED",
        "CONFIRMED",
        "CLOSED_WITHOUT_CONFIRMATION",
    ):
        raise HTTPException(409, "Обращение не закрыто")
    c.status = "REOPENED"
    update(c, reopened_at=now().isoformat())
    event(db, "case_event", c.id, "REOPENED", actor.id, reason=payload["reason"])
    audit(db, actor, "case.reopen", c.id, reason=payload["reason"])
    commit(db)
    return reply(c)


@router.post("/cases/{case_id}/administrative-close")
def admin_close(
    case_id: str, payload: dict, db=Depends(get_db), actor=Depends(get_actor)
):
    require_role(actor, *PUBLISH)
    c = get_entity(db, case_id, "case", lock=True)
    case_authorize(db, actor, c, True)
    if not payload.get("reason"):
        raise HTTPException(422, "Нужна причина административного закрытия")
    c.status = "CLOSED_WITHOUT_CONFIRMATION"
    update(
        c, administrative_close_reason=payload["reason"], closed_at=now().isoformat()
    )
    event(
        db,
        "case_event",
        c.id,
        "ADMINISTRATIVE_CLOSE",
        actor.id,
        reason=payload["reason"],
    )
    audit(db, actor, "case.administrative-close", c.id, reason=payload["reason"])
    commit(db)
    return reply(c)


def task_authorize(db, actor, task, write=False):
    if actor.role not in HR + ("MANAGER",):
        raise HTTPException(403, "Задача доступна HR и назначенному руководителю")
    if actor.role == "MANAGER":
        employee = db.get(Entity, task.data["employee_id"])
        if (
            task.data.get("assigned_manager_id") != actor.id
            or not employee
            or employee.kind != "employee"
            or employee.data.get("employment_stage") == "ENDED"
            or task.data.get("episode_id") != employee.data.get("episode_id")
        ):
            raise HTTPException(404, "Задача недоступна")
        # HR explicitly delegated this task, not the employee's directory card,
        # chat, feedback or case history. Substitutes need no broader HR grant.
        return
    assert_employee(db, actor, task.data["employee_id"])


def task_view(db, actor, task):
    employee = db.get(Entity, task.data["employee_id"])
    return pack(
        task,
        {
            "employee_name": employee.data.get("name") if employee else None,
            "overdue": task.status not in ("COMPLETED", "CANCELLED")
            and bool(task.data.get("due_at"))
            and aware(task.data["due_at"]) < now(),
        },
    )


@router.get("/manager-tasks")
def manager_tasks(db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *HR, "MANAGER")
    items = []
    for task in entities(db, "manager_task"):
        try:
            task_authorize(db, actor, task)
        except HTTPException:
            continue
        items.append(task_view(db, actor, task))
    return {"items": items}


@router.post("/manager-tasks")
def create_manager_task(payload: dict, db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *HR)
    eid = payload.get("employee_id")
    employee = assert_employee(db, actor, eid)
    manager_id = payload.get("assigned_manager_id") or employee.data.get("manager_id")
    if manager_id:
        manager = db.get(User, manager_id)
        if not manager or not manager.enabled or manager.role != "MANAGER":
            raise HTTPException(422, "Назначьте действующего руководителя")
    due = payload.get("due_at") or (now() + timedelta(days=3)).isoformat()
    aware(due)
    t = save_entity(
        db,
        "manager_task",
        {
            "employee_id": eid,
            "episode_id": employee.data.get("episode_id"),
            "assigned_manager_id": manager_id,
            "reason_code": payload.get("reason_code", "ADAPTATION_MEETING"),
            "summary": payload.get("summary", "Обсудить адаптацию сотрудника"),
            "due_at": due,
            "assignment_revision": 1,
            "history": [],
        },
        status="OPEN",
        parent_id=eid,
        owner_id=manager_id,
    )
    audit(db, actor, "manager_task.create", t.id)
    commit(db)
    return reply(t, 201)


@router.get("/manager-tasks/{task_id}")
def get_manager_task(task_id: str, db=Depends(get_db), actor=Depends(get_actor)):
    t = get_entity(db, task_id, "manager_task")
    task_authorize(db, actor, t)
    return JSONResponse(task_view(db, actor, t), headers={"ETag": etag(t)})


@router.post("/manager-tasks/{task_id}/acknowledge")
def acknowledge_task(
    task_id: str, payload: dict, db=Depends(get_db), actor=Depends(get_actor)
):
    t = get_entity(db, task_id, "manager_task", lock=True)
    task_authorize(db, actor, t, True)
    if actor.id != t.data.get("assigned_manager_id"):
        raise HTTPException(403, "Принять задачу может назначенный руководитель")
    if (
        payload.get("assignment_revision") is not None
        and payload["assignment_revision"] != t.data["assignment_revision"]
    ):
        raise HTTPException(409, "Задача переназначена")
    if t.status == "ACKNOWLEDGED":
        return reply(t)
    if t.status != "OPEN":
        raise HTTPException(409, "Задачу нельзя принять")
    t.status = "ACKNOWLEDGED"
    update(t, acknowledged_at=now().isoformat(), acknowledged_by=actor.id)
    event(db, "manager_task_event", t.id, "ACKNOWLEDGED", actor.id)
    commit(db)
    return reply(t)


@router.post("/manager-tasks/{task_id}/complete")
def complete_task(
    task_id: str, payload: dict, db=Depends(get_db), actor=Depends(get_actor)
):
    t = get_entity(db, task_id, "manager_task", lock=True)
    task_authorize(db, actor, t, True)
    if actor.id != t.data.get("assigned_manager_id"):
        raise HTTPException(403, "Завершить задачу может назначенный руководитель")
    if (
        payload.get("assignment_revision") is not None
        and payload["assignment_revision"] != t.data["assignment_revision"]
    ):
        raise HTTPException(409, "Задача переназначена")
    if t.status not in ("OPEN", "ACKNOWLEDGED", "IN_PROGRESS"):
        raise HTTPException(409, "Задача уже завершена")
    if not payload.get("result_summary") or not payload.get("result_code"):
        raise HTTPException(422, "Нужны результат и краткое описание")
    t.status = "COMPLETED"
    update(
        t,
        result_summary=str(payload["result_summary"])[:2000],
        result_code=payload["result_code"],
        completed_at=now().isoformat(),
        completed_by=actor.id,
    )
    event(db, "manager_task_event", t.id, "COMPLETED", actor.id)
    commit(db)
    return reply(t)


@router.patch("/manager-tasks/{task_id}")
def update_task(
    task_id: str,
    payload: dict,
    request: Request,
    db=Depends(get_db),
    actor=Depends(get_actor),
):
    require_role(actor, *HR)
    t = get_entity(db, task_id, "manager_task", lock=True)
    task_authorize(db, actor, t, True)
    check_etag(request, t)
    if not payload.get("reason"):
        raise HTTPException(422, "Укажите причину изменения")
    changes = {
        k: payload[k]
        for k in ("due_at", "assigned_manager_id", "summary")
        if k in payload
    }
    if "assigned_manager_id" in changes and changes[
        "assigned_manager_id"
    ] != t.data.get("assigned_manager_id"):
        u = db.get(User, changes["assigned_manager_id"])
        if not u or u.role != "MANAGER" or not u.enabled:
            raise HTTPException(422, "Руководитель не найден")
        history = t.data.get("history", []) + [
            {
                "manager_id": t.data.get("assigned_manager_id"),
                "assignment_revision": t.data.get("assignment_revision", 1),
                "acknowledged_at": t.data.get("acknowledged_at"),
                "ended_at": now().isoformat(),
            }
        ]
        changes.update(
            assignment_revision=t.data.get("assignment_revision", 1) + 1,
            history=history,
            acknowledged_at=None,
            acknowledged_by=None,
        )
        t.status = "OPEN"
        t.owner_id = changes["assigned_manager_id"]
    if payload.get("status") == "CANCELLED":
        t.status = "CANCELLED"
    update(t, **changes)
    event(db, "manager_task_event", t.id, "UPDATED", actor.id, reason=payload["reason"])
    audit(db, actor, "manager_task.update", t.id, reason=payload["reason"])
    commit(db)
    return reply(t)


def validate_menu(data):
    errors = []
    items = data.get("items", [])
    ids = []
    if not isinstance(items, list) or len(items) > 12:
        return {
            "valid": False,
            "errors": [
                {"path": "items", "message": "Допускается до 12 базовых вопросов"}
            ],
        }
    for i, item in enumerate(items):
        if not item.get("id") or item["id"] in ids:
            errors.append({"path": f"items[{i}].id", "message": "Нужен уникальный ID"})
        ids.append(item.get("id"))
        for f in ("label_ty", "label_vy", "query_text"):
            if (
                not isinstance(item.get(f), str)
                or not item[f].strip()
                or len(item[f]) > (2000 if f == "query_text" else 80)
            ):
                errors.append(
                    {
                        "path": f"items[{i}].{f}",
                        "message": "Заполните текст допустимой длины",
                    }
                )
        if item.get("action_type", "RAG_QUERY") != "RAG_QUERY":
            errors.append(
                {
                    "path": f"items[{i}]",
                    "message": "Меню вопросов поддерживает только поиск RAG_QUERY",
                }
            )
        if any(
            k in item
            for k in ("code", "script", "url", "credential", "system_prompt", "answer")
        ):
            errors.append(
                {
                    "path": f"items[{i}]",
                    "message": "Исполняемые команды и скрытые ответы не разрешены",
                }
            )
    return {
        "valid": not errors,
        "errors": errors,
        "fingerprint": hashlib.sha256(
            json.dumps(items, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest(),
    }


@router.get("/assistant-menus")
def menus(db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *READ_HR)
    return {
        "items": [
            pack(e)
            for e in entities(db, "assistant_menu")
            if (scenario := db.get(Entity, e.data.get("scenario_id")))
            and _scenario_allowed(actor, scenario)
        ]
    }


def _menu_access(db, actor, menu, write=False):
    _scenario_access(db, actor, menu.data["scenario_id"], write)
    return menu


def _menu_version_access(db, actor, version, write=False):
    _menu_access(db, actor, get_entity(db, version.parent_id, "assistant_menu"), write)
    return version


@router.post("/assistant-menus")
def create_menu(payload: dict, db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *HR)
    _scenario_access(db, actor, payload.get("scenario_id", ""), True)
    m = save_entity(
        db,
        "assistant_menu",
        {
            "name": payload.get("name", "Базовые вопросы"),
            "scenario_id": payload["scenario_id"],
        },
        parent_id=payload["scenario_id"],
    )
    commit(db)
    return reply(m, 201)


@router.get("/assistant-menus/{menu_id}")
def menu_get(menu_id: str, db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *READ_HR)
    m = get_entity(db, menu_id, "assistant_menu")
    _menu_access(db, actor, m)
    return reply(
        m,
        extra={
            "versions": [pack(v) for v in entities(db, "assistant_menu_version", m.id)]
        },
    )


@router.post("/assistant-menus/{menu_id}/versions")
def create_menu_version(
    menu_id: str, payload: dict, db=Depends(get_db), actor=Depends(get_actor)
):
    require_role(actor, *HR)
    m = get_entity(db, menu_id, "assistant_menu")
    _menu_access(db, actor, m, True)
    base = db.get(
        Entity, payload.get("base_version_id") or m.data.get("published_version_id")
    )
    if base and base.parent_id != m.id:
        raise HTTPException(422, "Версия другого меню")
    e = save_entity(
        db,
        "assistant_menu_version",
        {
            "menu_id": m.id,
            "scenario_id": m.data["scenario_id"],
            "items": copy.deepcopy(
                payload.get("items", base.data["items"] if base else [])
            ),
        },
        status="DRAFT",
        parent_id=m.id,
    )
    commit(db)
    return reply(e, 201)


@router.get("/assistant-menu-versions/{version_id}")
def menu_version_get(version_id: str, db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *READ_HR)
    return reply(
        _menu_version_access(
            db, actor, get_entity(db, version_id, "assistant_menu_version")
        )
    )


@router.put("/assistant-menu-versions/{version_id}")
def menu_version_put(
    version_id: str,
    payload: dict,
    request: Request,
    db=Depends(get_db),
    actor=Depends(get_actor),
):
    require_role(actor, *HR)
    e = get_entity(db, version_id, "assistant_menu_version", lock=True)
    _menu_version_access(db, actor, e, True)
    check_etag(request, e)
    if e.status not in ("DRAFT", "APPROVED"):
        raise HTTPException(409, "Создайте новую редакцию меню")
    result = validate_menu(payload)
    if not result["valid"]:
        raise HTTPException(422, result)
    update(e, items=payload["items"], review=None)
    e.status = "DRAFT"
    commit(db)
    return reply(e)


@router.post("/assistant-menu-versions/{version_id}/review")
def menu_review(
    version_id: str, payload: dict, db=Depends(get_db), actor=Depends(get_actor)
):
    require_role(actor, *PUBLISH)
    e = get_entity(db, version_id, "assistant_menu_version", lock=True)
    _menu_version_access(db, actor, e, True)
    if e.status not in ("DRAFT", "APPROVED"):
        raise HTTPException(409, "Меню нельзя согласовать")
    result = validate_menu(e.data)
    if not result["valid"]:
        raise HTTPException(422, result)
    update(
        e,
        review={
            "reviewer_id": actor.id,
            "fingerprint": result["fingerprint"],
            "reviewed_at": now().isoformat(),
        },
    )
    e.status = "APPROVED"
    audit(
        db,
        actor,
        "menu.approve",
        e.id,
        {
            "reason_code": "CONTENT_REVIEW_APPROVED",
            "fingerprint": result["fingerprint"],
        },
        reason=payload.get("reason"),
    )
    commit(db)
    return reply(e)


@router.post("/assistant-menu-versions/{version_id}/publish")
def menu_publish(
    version_id: str, payload: dict, db=Depends(get_db), actor=Depends(get_actor)
):
    require_role(actor, *PUBLISH)
    e = get_entity(db, version_id, "assistant_menu_version", lock=True)
    m = get_entity(db, e.parent_id, "assistant_menu", lock=True)
    _menu_access(db, actor, m, True)
    result = validate_menu(e.data)
    if (
        e.status != "APPROVED"
        or (e.data.get("review") or {}).get("fingerprint") != result["fingerprint"]
    ):
        raise HTTPException(409, "Меню нужно согласовать после последнего изменения")
    old = (
        db.get(Entity, m.data["published_version_id"])
        if m.data.get("published_version_id")
        else None
    )
    if old:
        old.status = "ARCHIVED"
    e.status = "PUBLISHED"
    update(e, published_at=now().isoformat())
    update(m, published_version_id=e.id, content_pending=False)
    audit(
        db,
        actor,
        "menu.publish",
        e.id,
        {
            "reason_code": "APPROVED_VERSION_PUBLISHED",
            "fingerprint": result["fingerprint"],
        },
        reason=payload.get("reason"),
    )
    commit(db)
    return reply(e)


def employee_menu(db, employee_id, scenario_id=None):
    employee = get_entity(db, employee_id, "employee")
    items = []
    menu_id = None
    version_id = None
    if not scenario_id:
        journey = next(
            (
                j
                for j in entities(db, "journey", employee_id)
                if j.status != "CANCELLED"
                and j.data.get("episode_id") == employee.data.get("episode_id")
            ),
            None,
        )
        scenario_id = journey.data["scenario_id"] if journey else None
    for m in entities(db, "assistant_menu"):
        if scenario_id and m.data.get("scenario_id") != scenario_id:
            continue
        v = db.get(Entity, m.data.get("published_version_id"))
        if not v or v.status != "PUBLISHED":
            continue
        menu_id, version_id = m.id, v.id
        for item in sorted(v.data["items"], key=lambda i: i.get("order", 0)):
            if item.get("enabled", True) and eligible(
                item.get("eligibility", {}), employee.data
            ):
                items.append(
                    {
                        **item,
                        "menu_id": m.id,
                        "menu_version_id": v.id,
                        "label": (
                            item["label_ty"]
                            if employee.data.get("address_form") == "TY"
                            else item["label_vy"]
                        ),
                    }
                )
    return {"menu_id": menu_id, "menu_version_id": version_id, "items": items}


def resolve_menu_query(db, employee_id, payload):
    menu = employee_menu(db, employee_id)
    if payload.get("menu_version_id") != menu["menu_version_id"]:
        raise HTTPException(409, "Список вопросов обновился; откройте актуальное меню")
    item = next((i for i in menu["items"] if i["id"] == payload.get("item_id")), None)
    if not item:
        raise HTTPException(404, "Вопрос недоступен")
    return item["query_text"]


@router.get("/employees/{employee_id}/assistant-menu")
def employee_menu_api(employee_id: str, db=Depends(get_db), actor=Depends(get_actor)):
    if not can_employee_drilldown(actor):
        raise HTTPException(
            404, "Для персонального просмотра нужен отдельный drilldown grant"
        )
    assert_employee(db, actor, employee_id)
    return employee_menu(db, employee_id)


def validate_calendar(data):
    data = normalize_calendar(data)
    weekdays = data.get("working_weekdays")
    if weekdays is None and data.get("weekly_intervals"):
        weekdays = sorted({int(i["weekday"]) - 1 for i in data["weekly_intervals"]})
        data = {**data, "working_weekdays": weekdays}
    if (
        not isinstance(weekdays, list)
        or (not weekdays and data.get("pattern") != "DATED_ONLY")
        or any(type(i) is not int or not 0 <= i <= 6 for i in weekdays)
    ):
        raise HTTPException(422, "Укажите рабочие дни недели (0 — понедельник)")
    try:
        ZoneInfo(data.get("timezone", "Europe/Moscow"))
        start = date.fromisoformat(data.get("coverage_start", "2020-01-01"))
        end = date.fromisoformat(data.get("coverage_end", "2035-12-31"))
        if end < start:
            raise ValueError()
        for ex in data.get("exceptions", data.get("dated_exceptions", [])):
            if isinstance(ex, dict):
                date.fromisoformat(ex["date"])
    except (ValueError, KeyError):
        raise HTTPException(422, "Проверьте период и исключения календаря")
    # Normalize both form aliases and canonical ISO intervals.
    for weekly in data.get("weekly_intervals", []):
        if type(weekly.get("weekday")) is not int or not 1 <= weekly["weekday"] <= 7:
            raise HTTPException(422, "Дни интервалов должны быть ISO 1–7")
        spans = []
        for interval in weekly.get("intervals", []):
            try:
                lo = datetime.strptime(
                    interval.get("start_local", interval.get("start", "")), "%H:%M"
                )
                hi = datetime.strptime(
                    interval.get("end_local", interval.get("end", "")), "%H:%M"
                ) + timedelta(days=interval.get("end_day_offset", 0))
            except (ValueError, TypeError, ZoneInfoNotFoundError):
                raise HTTPException(422, "Проверьте рабочие интервалы")
            if hi <= lo or hi - lo > timedelta(days=1):
                raise HTTPException(
                    422,
                    "Рабочий интервал должен иметь положительную длительность до 24 часов",
                )
            spans.append((lo, hi))
        spans.sort()
        if any(a[1] > b[0] for a, b in zip(spans, spans[1:])):
            raise HTTPException(422, "Рабочие интервалы пересекаются")
    dates = [e.get("date") for e in data.get("exceptions", []) if isinstance(e, dict)]
    if len(dates) != len(set(dates)):
        raise HTTPException(422, "Дата исключения повторяется")
    return data


def authorize_calendar(db, actor, calendar, write=False):
    site_id = calendar.data.get("site_id")
    if site_id:
        if write:
            check_scope_site(actor, site_id, db)
        elif site_id not in {site.id for site in visible_sites(db, actor)}:
            raise HTTPException(404, "Календарь недоступен")
    elif write and not actor.scope.get("all"):
        raise HTTPException(403, "Общий календарь изменяет администратор всего контура")
    return calendar


@router.get("/work-calendars")
def work_calendars(db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *READ_HR)
    return {
        "items": [
            pack(
                c,
                {"versions": [pack(v) for v in entities(db, "calendar_version", c.id)]},
            )
            for c in entities(db, "work_calendar")
            if not c.data.get("site_id")
            or c.data["site_id"] in {site.id for site in visible_sites(db, actor)}
        ]
    }


@router.post("/work-calendars")
def create_calendar(payload: dict, db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *PUBLISH)
    if not payload.get("name"):
        raise HTTPException(422, "Нужно название календаря")
    if payload.get("site_id"):
        get_entity(db, payload["site_id"], "site")
        check_scope_site(actor, payload["site_id"], db)
    elif not actor.scope.get("all"):
        raise HTTPException(403, "Общий календарь создаёт администратор всего контура")
    e = save_entity(
        db,
        "work_calendar",
        {"name": payload["name"], "site_id": payload.get("site_id"), "epoch": 0},
    )
    commit(db)
    return reply(e, 201)


@router.get("/work-calendars/{calendar_id}")
def calendar_get(calendar_id: str, db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *READ_HR)
    c = get_entity(db, calendar_id, "work_calendar")
    authorize_calendar(db, actor, c)
    return reply(
        c, extra={"versions": [pack(v) for v in entities(db, "calendar_version", c.id)]}
    )


@router.post("/work-calendars/{calendar_id}/versions")
def create_calendar_version(
    calendar_id: str, payload: dict, db=Depends(get_db), actor=Depends(get_actor)
):
    require_role(actor, *PUBLISH)
    c = get_entity(db, calendar_id, "work_calendar")
    authorize_calendar(db, actor, c, write=actor.role in PUBLISH)
    data = validate_calendar(payload)
    e = save_entity(
        db,
        "calendar_version",
        {**data, "calendar_id": c.id},
        status="DRAFT",
        parent_id=c.id,
    )
    commit(db)
    return reply(e, 201)


@router.post("/work-calendars/{calendar_id}/versions/{version_id}/preview-impact")
def calendar_preview(
    calendar_id: str, version_id: str, db=Depends(get_db), actor=Depends(get_actor)
):
    require_role(actor, *PUBLISH)
    c = get_entity(db, calendar_id, "work_calendar")
    authorize_calendar(db, actor, c, write=actor.role in PUBLISH)
    v = get_entity(db, version_id, "calendar_version")
    if v.parent_id != c.id:
        raise HTTPException(404, "Версия другого календаря")
    impacted = []
    for e in employee_scope(db, actor):
        site = db.get(Entity, e.data.get("site_id"))
        if (
            e.data.get("calendar_id")
            or (site.data.get("calendar_id") if site else None)
        ) != c.id:
            continue
        for j in entities(db, "journey", e.id):
            if j.status in ("CANCELLED", "COMPLETED"):
                continue
            graph = get_entity(db, j.data["version_id"], "scenario_version").data[
                "graph"
            ]
            impacted.append(
                {
                    "employee_id": e.id,
                    "journey_id": j.id,
                    "timeline": simulate(graph, e.data, v.data)["timeline"],
                }
            )
    digest = hashlib.sha256(
        json.dumps(
            {
                "calendar_id": c.id,
                "epoch": c.data.get("epoch", 0),
                "version_id": v.id,
                "version": v.version,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    p = save_entity(
        db,
        "calendar_preview",
        {
            "calendar_id": c.id,
            "version_id": v.id,
            "calendar_epoch": c.data.get("epoch", 0),
            "preview_hash": digest,
            "impact": impacted,
            "expires_at": (now() + timedelta(minutes=30)).isoformat(),
        },
        parent_id=c.id,
    )
    commit(db)
    return {**pack(p), "preview_id": p.id}


@router.post("/work-calendars/{calendar_id}/versions/{version_id}/activate")
def calendar_activate(
    calendar_id: str,
    version_id: str,
    payload: dict,
    db=Depends(get_db),
    actor=Depends(get_actor),
):
    require_role(actor, *PUBLISH)
    c = get_entity(db, calendar_id, "work_calendar", lock=True)
    authorize_calendar(db, actor, c, write=True)
    v = get_entity(db, version_id, "calendar_version")
    p = get_entity(db, payload.get("preview_id", ""), "calendar_preview", lock=True)
    if (
        v.parent_id != c.id
        or p.data.get("version_id") != v.id
        or p.data.get("calendar_epoch") != c.data.get("epoch", 0)
        or payload.get("preview_hash") != p.data["preview_hash"]
        or aware(p.data["expires_at"]) < now()
    ):
        raise HTTPException(409, "Предпросмотр устарел")
    old = (
        db.get(Entity, c.data["active_version_id"])
        if c.data.get("active_version_id")
        else None
    )
    if old:
        old.status = "ARCHIVED"
    v.status = "ACTIVE"
    update(c, active_version_id=v.id, epoch=c.data.get("epoch", 0) + 1)
    p.status = "APPLIED"
    if c.data.get("site_id"):
        site = get_entity(db, c.data["site_id"], "site")
        update(site, calendar_id=c.id)
    count = 0
    for item in p.data.get("impact", []):
        j = get_entity(db, item["journey_id"], "journey")
        replan_journey(
            db, j, get_entity(db, item["employee_id"], "employee"), bump_revision=True
        )
        count += 1
    audit(db, actor, "calendar.activate", c.id, reason=payload.get("reason"))
    commit(db)
    return {
        "status": "COMPLETED",
        "calendar_id": c.id,
        "active_version_id": v.id,
        "calendar_epoch": c.data["epoch"],
        "replanned_journeys": count,
        "existing_sla_cases_changed": False,
    }


@router.get("/settings/sla-policy")
def get_sla(db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *READ_HR)
    p = next((e for e in entities(db, "sla_policy") if e.status == "ACTIVE"), None)
    return (
        reply(p)
        if p
        else {
            "queue_rules": [],
            "apply_to_existing_open_cases": False,
            "status": "NOT_CONFIGURED",
        }
    )


@router.patch("/settings/sla-policy")
def set_sla(
    payload: dict, request: Request, db=Depends(get_db), actor=Depends(get_actor)
):
    require_role(actor, *PUBLISH)
    if not actor.scope.get("all"):
        raise HTTPException(
            403, "Общую SLA policy изменяет администратор всего контура"
        )
    if db.bind.dialect.name == "postgresql":
        from sqlalchemy import text as sql_text

        db.execute(sql_text("SELECT pg_advisory_xact_lock(76810104)"))
    current = db.scalar(
        select(Entity)
        .where(Entity.kind == "sla_policy", Entity.status == "ACTIVE")
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if current:
        check_etag(request, current)
    if payload.get("apply_to_existing_open_cases"):
        raise HTTPException(
            422, "Новая SLA policy применяется только к новым обращениям"
        )
    for rule in payload.get("queue_rules", []):
        if (
            type(rule.get("first_response_working_minutes")) is not int
            or not 1 <= rule["first_response_working_minutes"] <= 525600
        ):
            raise HTTPException(422, "Укажите положительный SLA в рабочих минутах")
        cv = get_entity(db, rule.get("calendar_version_id", ""), "calendar_version")
        if not cv.data.get("weekly_intervals") and not any(
            x.get("intervals") for x in cv.data.get("dated_exceptions", [])
        ):
            raise HTTPException(
                422, "Для SLA нужны явно подтверждённые рабочие интервалы"
            )
    for old in entities(db, "sla_policy"):
        if old.status == "ACTIVE":
            old.status = "ARCHIVED"
    p = save_entity(
        db,
        "sla_policy",
        {
            **payload,
            "apply_to_existing_open_cases": False,
            "effective_at": now().isoformat(),
            "policy_version": (
                (current.data.get("policy_version", 1) + 1) if current else 1
            ),
        },
    )
    audit(db, actor, "sla.update", p.id, reason=payload.get("reason"))
    commit(db)
    return reply(p)


def dashboard_data(db, actor, filters=None):
    filters = {**(filters or {})}
    window = reporting_window(filters)
    filters["_reporting_window"] = window
    employees = filtered_employees(db, actor, filters)
    ids = {e.id for e in employees}
    episodes = {e.id: e.data.get("episode_id") for e in employees}
    surveys = [
        s
        for s in entities(db, "survey_run")
        if s.data.get("employee_id") in ids
        and not s.data.get("campaign_id")
        and s.data.get("episode_id") == episodes.get(s.data.get("employee_id"))
        and (
            not s.data.get("first_question_accepted_at")
            or in_report_period(s.data["first_question_accepted_at"], filters)
        )
    ]
    cases = [
        c
        for c in entities(db, "case")
        if c.data.get("employee_id") in ids
        and c.data.get("episode_id") == episodes.get(c.data.get("employee_id"))
    ]
    tasks = [
        t
        for t in entities(db, "manager_task")
        if t.data.get("employee_id") in ids
        and t.data.get("episode_id") == episodes.get(t.data.get("employee_id"))
    ]
    if actor.role == "MANAGER":
        tasks = []
        for task in entities(db, "manager_task"):
            try:
                task_authorize(db, actor, task)
            except HTTPException:
                continue
            tasks.append(task)
    opened = [s for s in surveys if s.data.get("first_question_accepted_at")]
    completed = [s for s in opened if s.status == "COMPLETED"]
    participated = [s for s in opened if s.data.get("answers")]
    declined = [s for s in opened if s.status == "DECLINED"]
    blockers = [c for c in cases if c.data.get("kind") == "BLOCKER"]
    pending = [c for c in blockers if c.data.get("resolution_attempts")]
    bysite = []
    for site in entities(db, "site"):
        n = sum(e.data.get("site_id") == site.id for e in employees)
        if n:
            bysite.append(
                {
                    "site_id": site.id,
                    "name": site.data.get("name"),
                    "employees": n,
                    "sensitive_metrics_suppressed": n < 5,
                }
            )
    stats = {
        "employees": len(employees),
        "active_employees": sum(
            e.data.get("employment_stage") == "ACTIVE" for e in employees
        ),
        "preboarding": sum(
            e.data.get("employment_stage") == "PREBOARDING" for e in employees
        ),
        "surveys_opened": len(opened),
        "surveys_completed": len(completed),
        "surveys_declined": len(declined),
        "surveys_missing": sum(
            not s.data.get("answers") and s.status != "DECLINED" for s in opened
        ),
        "completion": {
            "numerator": len(completed),
            "denominator": len(opened),
            "rate": len(completed) / len(opened) if opened else None,
        },
        "participation": {
            "numerator": len(participated),
            "denominator": len(opened),
            "rate": len(participated) / len(opened) if opened else None,
        },
        "open_cases": sum(
            c.status
            not in (
                "RESOLVED",
                "CLOSED",
                "CONFIRMED",
                "CLOSED_WITHOUT_CONFIRMATION",
                "CANCELLED",
            )
            for c in cases
        ),
        "open_blockers": sum(
            c.status not in ("CONFIRMED", "CLOSED_WITHOUT_CONFIRMATION", "CANCELLED")
            for c in blockers
        ),
        "blocker_confirmation": {
            "numerator": sum(c.status == "CONFIRMED" for c in pending),
            "denominator": len(pending),
        },
        "tasks_open": sum(t.status not in ("COMPLETED", "CANCELLED") for t in tasks),
        "tasks_overdue": sum(
            t.status not in ("COMPLETED", "CANCELLED")
            and aware(t.data["due_at"]) < now()
            for t in tasks
            if t.data.get("due_at")
        ),
    }
    missing = stats["surveys_missing"]
    denominators = {
        "employees": len(employees),
        "active_employees": len(employees),
        "preboarding": len(employees),
        "surveys_opened": len(opened),
        "surveys_completed": len(opened),
        "surveys_declined": len(opened),
        "surveys_missing": len(opened),
        "open_cases": len(cases),
        "open_blockers": len(blockers),
        "tasks_open": len(tasks),
        "tasks_overdue": len(tasks),
    }
    details = {}
    for key, value in stats.items():
        if isinstance(value, dict):
            details[key] = metric_context(
                value["numerator"],
                value["denominator"],
                (
                    value["denominator"] - value["numerator"]
                    if key == "blocker_confirmation"
                    else missing
                ),
                window,
                measure_kind="RATIO",
                cohort_definition=(
                    "BLOCKERS_WITH_RESOLUTION_ATTEMPT_CURRENT_STATUS"
                    if key == "blocker_confirmation"
                    else "SURVEYS_WITH_ACCEPTED_OPENING_IN_PERIOD_CURRENT_STATUS"
                ),
                missing_answers_method=(
                    "RESOLUTION_ATTEMPTS_AWAITING_CONFIRMATION"
                    if key == "blocker_confirmation"
                    else "OPENED_SURVEYS_WITHOUT_ANSWERS_EXCLUDING_DECLINED"
                ),
            )
        else:
            details[key] = metric_context(
                value,
                denominators.get(key, len(employees)),
                missing if key.startswith("surveys_") else None,
                window,
                measure_kind="COUNT",
                rate=None,
                missing_answers_method=(
                    "OPENED_SURVEYS_WITHOUT_ANSWERS_EXCLUDING_DECLINED"
                    if key.startswith("surveys_")
                    else "NOT_APPLICABLE_TO_CURRENT_STATE_COUNT"
                ),
                cohort_definition=(
                    "SURVEYS_WITH_ACCEPTED_OPENING_IN_PERIOD_CURRENT_STATUS"
                    if key.startswith("surveys_")
                    else "CURRENT_VISIBLE_STATE_AS_OF"
                ),
            )
    numeric = {}
    for run in opened:
        for question in run.data.get("definition", {}).get("questions", []):
            if question.get("type") != "NUMERIC":
                continue
            key = (run.data.get("survey_key"), question["question_key"])
            group = numeric.setdefault(
                key, {"values": [], "employees": set(), "eligible": 0}
            )
            group["eligible"] += 1
            prompt = question.get("prompt", {})
            group["question_text"] = (
                (
                    prompt.get("text_vy")
                    or prompt.get("text_ty")
                    or prompt.get("vy")
                    or prompt.get("ty")
                    or question["question_key"]
                )
                if isinstance(prompt, dict)
                else str(prompt)
            )
            answer = run.data.get("answers", {}).get(question["question_key"], {})
            value = answer.get("value")
            if type(value) is int and question.get(
                "minimum", 1
            ) <= value <= question.get("maximum", 10):
                group["values"].append(value)
                group["employees"].add(run.data.get("employee_id"))
    averages = []
    for (survey_key, question_id), group in sorted(numeric.items()):
        valid = len(group["values"])
        suppressed = (
            actor.role in ("MANAGER", "HEAD_TALENT") and len(group["employees"]) < 5
        )
        averages.append(
            {
                "survey_key": survey_key,
                "question_id": question_id,
                "question_text": group["question_text"],
                "suppressed": suppressed,
                "numerator": None if suppressed else sum(group["values"]),
                "denominator": None if suppressed else valid,
                "eligible_answers": None if suppressed else group["eligible"],
                "missing_answers": None if suppressed else group["eligible"] - valid,
                "average": (
                    None if suppressed or not valid else sum(group["values"]) / valid
                ),
                "no_data": None if suppressed else not valid,
                **window,
                "method": "VALID_INTEGER_ANSWERS_ONLY",
            }
        )
    return {
        "role": actor.role,
        "scope": actor.scope,
        "metrics": stats,
        "metric_details": details,
        "reporting_window": window,
        "numeric_averages": averages,
        "sites": bysite,
        "employees": [
            {
                "id": e.id,
                "name": e.data.get("name"),
                "site_id": e.data.get("site_id"),
                "employment_stage": e.data.get("employment_stage"),
                "start_date": e.data.get("start_date"),
            }
            for e in employees
            if can_employee_drilldown(actor)
        ],
        "privacy": {"minimum_aggregate_group_size": 5, "raw_feedback_included": False},
        "source": "PERSISTED_SQL_COUNTS",
        "methodology": "Завершённость и участие считаются от анкет с принятым MAX первым вопросом; отказ и отсутствие ответа отдельны.",
    }


@router.get("/dashboards/current")
def dashboard_current(
    db=Depends(get_db),
    actor=Depends(get_actor),
    period_from: str | None = None,
    period_to: str | None = None,
    timezone: str = "Europe/Moscow",
    site_id: str | None = None,
):
    require_role(actor, *READ_HR, "MANAGER")
    data = dashboard_data(
        db,
        actor,
        {
            "period_from": period_from,
            "period_to": period_to,
            "timezone": timezone,
            "site_ids": [site_id] if site_id else [],
        },
    )
    snapshots = [s for s in entities(db, "dashboard_snapshot", actor.id)]
    latest = snapshots[-1] if snapshots else None
    return {
        **data,
        "snapshot_id": latest.id if latest else None,
        "generated_at": now().isoformat(),
        "notifications_triggered": False,
    }


@router.get("/dashboards/snapshots/{snapshot_id}")
def dashboard_snapshot(snapshot_id: str, db=Depends(get_db), actor=Depends(get_actor)):
    e = get_entity(db, snapshot_id, "dashboard_snapshot")
    if e.parent_id != actor.id:
        raise HTTPException(404, "Снимок недоступен")
    # Recompute current ACL, not a stale stored employee roster.
    return {**pack(e), "dashboard": dashboard_data(db, actor)}


@router.get("/analytics")
def analytics(
    db=Depends(get_db),
    actor=Depends(get_actor),
    period_from: str | None = None,
    period_to: str | None = None,
    timezone: str = "Europe/Moscow",
    site_id: str | None = None,
):
    require_role(actor, *READ_HR)
    return dashboard_data(
        db,
        actor,
        {
            "period_from": period_from,
            "period_to": period_to,
            "timezone": timezone,
            "site_ids": [site_id] if site_id else [],
        },
    )


@router.get("/analytics/export")
def analytics_export(
    db=Depends(get_db),
    actor=Depends(get_actor),
    period_from: str | None = None,
    period_to: str | None = None,
    timezone: str = "Europe/Moscow",
    site_id: str | None = None,
):
    require_role(actor, *READ_HR)
    data = dashboard_data(
        db,
        actor,
        {
            "period_from": period_from,
            "period_to": period_to,
            "timezone": timezone,
            "site_ids": [site_id] if site_id else [],
        },
    )
    out = io.StringIO()
    writer = csv.writer(out)
    columns = [
        "numerator",
        "denominator",
        "rate",
        "missing_answers",
        "no_data",
        "period_from",
        "period_to",
        "timezone",
        "as_of",
        "cohort_definition",
        "measure_kind",
        "question_text",
        "suppressed",
        "average",
    ]
    writer.writerow(["metric", *columns])
    for key, value in data["metric_details"].items():
        writer.writerow([key, *(export_cell(value.get(column)) for column in columns)])
    for value in data["numeric_averages"]:
        label = (
            "numeric_average:"
            + str(value["survey_key"])
            + ":"
            + str(value["question_id"])
        )
        writer.writerow(
            [
                export_cell(label),
                *(
                    export_cell(
                        {
                            **value,
                            "measure_kind": "NUMERIC_AVERAGE",
                            "rate": None,
                            "cohort_definition": "VALID_INTEGER_ANSWERS_IN_ACCEPTED_OPENING_COHORT",
                        }.get(column)
                    )
                    for column in columns
                ),
            ]
        )
    audit(db, actor, "analytics.export")
    commit(db)
    return Response(
        "\ufeff" + out.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="analytics.csv"'},
    )


@router.get("/me/dashboard-notifications")
def get_notifications(db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *READ_HR, "MANAGER")
    e = next(iter(entities(db, "notification_preference", actor.id)), None)
    return (
        pack(e)
        if e
        else {
            "enabled": False,
            "minimum_interval_minutes": 60,
            "channels": ["MAX"],
            "status": "NOT_CONFIGURED",
        }
    )


@router.put("/me/dashboard-notifications")
def put_notifications(payload: dict, db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *READ_HR, "MANAGER")
    interval = payload.get("minimum_interval_minutes", 60)
    if type(interval) is not int or not 30 <= interval <= 10080:
        raise HTTPException(422, "Интервал от 30 минут до недели")
    e = next(iter(entities(db, "notification_preference", actor.id)), None)
    data = {
        "enabled": bool(payload.get("enabled")),
        "minimum_interval_minutes": interval,
        "channels": ["MAX"],
        "dashboard": "DEFAULT_ROLE_SCOPE",
    }
    if e:
        update(e, **data)
    else:
        e = save_entity(
            db, "notification_preference", data, parent_id=actor.id, owner_id=actor.id
        )
    audit(db, actor, "notifications.update", e.id)
    commit(db)
    return reply(e)


def refresh_dashboards(db):
    users = list(db.execute(select(User).where(User.enabled == True)).scalars())
    for user in users:
        if is_hidden_setup(user) or user.role not in READ_HR + ("MANAGER",):
            continue
        actor = Actor(
            id=user.id,
            name=user.name,
            role=user.role,
            scope=user.scope,
            employee_id=user.employee_id,
        )
        data = dashboard_data(db, actor)

        # Wall-clock labels change on every sweep; only business-state changes
        # materialize a new snapshot and may trigger a notification.
        def stable(value):
            if isinstance(value, dict):
                return {
                    key: stable(item)
                    for key, item in value.items()
                    if key not in ("as_of", "period_to")
                }
            if isinstance(value, list):
                return [stable(item) for item in value]
            return value

        digest = hashlib.sha256(
            json.dumps(stable(data), sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        old = entities(db, "dashboard_snapshot", user.id)
        if old and old[-1].data.get("content_hash") == digest:
            continue
        save_entity(
            db,
            "dashboard_snapshot",
            {
                "dashboard": data,
                "content_hash": digest,
                "generated_at": now().isoformat(),
            },
            parent_id=user.id,
            owner_id=user.id,
        )
        pref = next(iter(entities(db, "notification_preference", user.id)), None)
        if not pref or not pref.data.get("enabled") or not old:
            continue
        last = (
            aware(pref.data["last_notified_at"])
            if pref.data.get("last_notified_at")
            else None
        )
        if last and now() - last < timedelta(
            minutes=pref.data["minimum_interval_minutes"]
        ):
            continue
        from .settings import get_settings

        public_url = get_settings().public_url.rstrip("/")
        login_buttons = (
            [{"text": "Открыть кабинет", "url": public_url + "/"}]
            if public_url.startswith("https://")
            else []
        )
        key = f"dashboard:{user.id}:{digest}"
        existing = db.execute(
            select(Outbox).where(Outbox.unique_key == key)
        ).scalar_one_or_none()
        if not existing:
            db.add(
                Outbox(
                    id=new_id(),
                    recipient_kind="USER",
                    recipient_id=user.id,
                    text="Ваш дашборд адаптации обновлён. Откройте веб-кабинет Корсика, чтобы посмотреть изменения.",
                    attachments=[],
                    callback_buttons=login_buttons,
                    status="PENDING",
                    unique_key=key,
                    created_at=now(),
                    data={"required_roles": list(READ_HR) + ["MANAGER"]},
                )
            )
            update(pref, last_notified_at=now().isoformat(), last_notified_hash=digest)
    db.flush()


def run_due_scenarios(db):
    """Small deterministic sweep. Caller owns commit; no outbound network here."""
    scan_deadlines(db)
    dispatch_recurring(db)
    dispatch_digest_schedules(db)
    from .simple_scenarios import enroll_future_programs
    enroll_future_programs(db)
    count = 0
    clock = now()
    journeys = list(
        db.execute(
            select(Entity)
            .where(Entity.kind == "journey", Entity.status.in_(["SCHEDULED", "ACTIVE"]))
            .with_for_update(skip_locked=True)
        ).scalars()
    )
    for j in journeys:
        employee = db.get(Entity, j.parent_id)
        if not employee or employee.data.get("employment_stage") == "ENDED":
            cancel_journey(db, j, "EMPLOYMENT_ENDED")
            continue
        if employee.data.get("enabled", True) is False:
            continue
        if employee.data.get("max_binding_status") != "VERIFIED":
            continue
        conversation = conversation_for(db, employee.id)
        if conversation and conversation.data.get("mode") == "HUMAN":
            continue
        j.status = (
            "ACTIVE"
            if employee.data.get("employment_stage") == "ACTIVE"
            else "SCHEDULED"
        )
        graph = get_entity(db, j.data["version_id"], "scenario_version").data["graph"]
        if graph.get("editor_mode") == "SIMPLE":
            from .simple_scenarios import run_due
            count += run_due(db, j, employee, clock)
            continue
        path = active_path(
            graph, j.data.get("answers", {}), employee.data, j.data.get("signals", {})
        )
        actions = sorted(
            entities(db, "scheduled_action", j.id),
            key=lambda x: (
                x.data.get("due_at") or "9999",
                x.data.get("sequence_no", 0),
            ),
        )
        for action in actions:
            if action.status not in ("PLANNED", "WAITING_APPROVAL"):
                continue
            if action.data.get("schedule_revision") != j.data.get("schedule_revision"):
                continue
            step = action.data["step"]
            if step["step_key"] not in path:
                continue
            if not eligible(step.get("eligibility", {}), employee.data):
                continue
            if not step.get("trigger") and step["kind"] in ("BRANCH", "END"):
                if step["kind"] == "BRANCH":
                    if not all(
                        condition_ready(
                            t.get("condition", {"kind": "ALWAYS"}),
                            j.data.get("answers", {}),
                            employee.data,
                            j.data.get("signals", {}),
                        )
                        for t in step.get("transitions", [])
                    ):
                        continue
                    target = next(
                        (
                            t["target_step_key"]
                            for t in step.get("transitions", [])
                            if evaluate_condition(
                                t.get("condition"),
                                j.data.get("answers", {}),
                                employee.data,
                                j.data.get("signals", {}),
                            )
                        ),
                        None,
                    )
                    update(action, selected_target=target)
                else:
                    if any(
                        a.id != action.id
                        and a.data["step_key"] in path
                        and a.status
                        not in (
                            "ACCEPTED",
                            "COMPLETED",
                            "SKIPPED",
                            "NOT_APPLICABLE",
                            "CANCELLED",
                        )
                        for a in actions
                    ):
                        continue
                complete_control_action(db, j, action, employee)
                continue
            if step.get("required_signal") and not j.data.get("signals", {}).get(
                step["required_signal"]
            ):
                action.status = "WAITING_APPROVAL"
                continue
            if action.data.get("calendar_warning"):
                continue
            if not action.data.get("due_at"):
                due = calculate_due(
                    step.get("trigger", {}),
                    employee.data,
                    calendar_for(db, employee),
                    j.data.get("events"),
                    j.data.get("signals"),
                )
                if due:
                    update(action, due_at=due.isoformat())
                else:
                    continue
            due = aware(action.data["due_at"])
            if due > clock:
                continue
            if (
                j.data.get("access_resumed_at")
                and due < aware(j.data["access_resumed_at"])
                and step.get("trigger", {}).get("type") == "ANCHOR_TIME"
            ):
                skip_access_milestone(db, j, action, clock)
                continue
            local_clock = clock.astimezone(
                ZoneInfo(employee.data.get("timezone", "Europe/Moscow"))
            )
            if (
                step.get("delivery_policy", {}).get("quiet_hours_policy") == "DEFER"
                and not 8 <= local_clock.hour < 23
            ):
                continue
            lag = clock - due
            late = (
                step.get("delivery_policy", {})
                .get("late_policy", "skip_if_past")
                .lower()
            )
            if lag > timedelta(days=1) and late in ("skip_if_past", "skip_if_late"):
                action.status = "SKIPPED"
                update(action, skip_reason="SKIPPED_LATE_JOIN")
                continue
            if lag > timedelta(days=7) and late == "open_if_relevant":
                action.status = "SKIPPED"
                update(action, skip_reason="SKIPPED_LATE_JOIN")
                continue
            # Branch conditions are evaluated from pinned answers, never inferred.
            if step["kind"] == "BRANCH":
                target = next(
                    (
                        tr["target_step_key"]
                        for tr in step.get("transitions", [])
                        if evaluate_condition(
                            tr.get("condition"),
                            j.data.get("answers", {}),
                            employee.data,
                            j.data.get("signals", {}),
                        )
                    ),
                    None,
                )
                update(action, selected_target=target)
                complete_control_action(db, j, action, employee)
                continue
            if step["kind"] == "SURVEY":
                run = open_survey(db, j, step, employee)
                if not run:
                    continue
                action.status = "ENQUEUED"
                update(action, survey_run_id=run.id)
                count += 1
                continue
            if step["kind"] == "END":
                complete_control_action(db, j, action, employee)
                continue
            if step["kind"] == "WAIT_SIGNAL":
                complete_control_action(db, j, action, employee)
                continue
            if step["kind"] == "HR_TASK":
                task = step.get("task", {})
                case = create_case_entity(
                    db,
                    employee.id,
                    "MANAGER_MEETING",
                    task.get("summary", "Обсудить адаптационный план"),
                    unique_key=f"action:{action.id}",
                )
                update(action, case_id=case.id)
                complete_control_action(db, j, action, employee)
                continue
            buttons = []
            question = None
            if step["kind"] == "QUESTION":
                question = next(
                    (
                        s
                        for s in entities(db, "scenario_question", j.id)
                        if s.data["step_key"] == step["step_key"]
                    ),
                    None,
                )
                if not question:
                    question = save_entity(
                        db,
                        "scenario_question",
                        {
                            "employee_id": employee.id,
                            "journey_id": j.id,
                            "step_key": step["step_key"],
                            "scenario_version_id": j.data["version_id"],
                            "question": step["question"],
                        },
                        status="OPEN",
                        parent_id=j.id,
                    )
                q = step["question"]
                base = {
                    "scenario_question_id": question.id,
                    "question_id": q["question_key"],
                }
                if q["type"] in ("SINGLE_CHOICE", "YES_NO"):
                    buttons = [
                        {
                            "text": render_text(o["label"], employee),
                            "payload": {
                                "action": "SCENARIO_ANSWER",
                                **base,
                                "value": o["value"],
                            },
                        }
                        for o in q.get("options", [])
                    ]
                else:
                    buttons = [
                        {
                            "text": "Ответить",
                            "payload": {"action": "CAPTURE_OPEN", **base},
                        }
                    ]
                buttons.append(
                    {
                        "text": "Пропустить",
                        "payload": {
                            "action": "SCENARIO_ANSWER",
                            **base,
                            "outcome": "DECLINED",
                        },
                    }
                )
            text = (
                question_text(step["question"], employee)
                if question
                else render_text(step.get("content", {}), employee)
            )
            if (
                step.get("content", {}).get("missing_assets")
                and step["kind"] == "MESSAGE"
            ):
                text += "\n\nВложение пока не предоставлено. Если материал нужен, напишите об этом обычным сообщением."
            if not text:
                action.status = "SKIPPED"
                update(action, skip_reason="CONTENT_NOT_PROVIDED")
                continue
            from .scenario_media import validated_materials
            try:
                material_ids = validated_materials(db, employee, step.get("content", {}).get("asset_ids", []))
            except HTTPException:
                j.status = "PAUSED"
                update(j, requires_hr_review=True, review_reason="SCENARIO_ASSET_UNAVAILABLE")
                continue
            outbound = emit(
                db,
                employee.id,
                text,
                buttons,
                f"action:{action.id}:schedule:{j.data['schedule_revision']}",
                {
                    "action_id": action.id,
                    "journey_id": j.id,
                    "step_key": step["step_key"],
                    "schedule_revision": j.data["schedule_revision"],
                },
            )
            if outbound is not None:
                outbound.attachments = material_ids
            action.status = "ENQUEUED"
            count += 1
        allactions = entities(db, "scheduled_action", j.id)
        if (
            allactions
            and all(
                a.status
                in ("ACCEPTED", "COMPLETED", "SKIPPED", "NOT_APPLICABLE", "CANCELLED")
                for a in allactions
                if a.data["step_key"] in path
            )
            and all(
                s.status in TERMINAL_SURVEYS for s in entities(db, "survey_run", j.id)
            )
        ):
            j.status = "COMPLETED"
            update(j, completed_at=clock.isoformat())
    for run in entities(db, "survey_run"):
        if (
            run.status not in TERMINAL_SURVEYS
            and run.data.get("expires_at")
            and aware(run.data["expires_at"]) <= clock
        ):
            run.status = "EXPIRED"
            update(run, expired_at=clock.isoformat())
    for cap in entities(db, "input_capture"):
        if cap.status in ("OPEN", "DRAFT") and aware(cap.data["expires_at"]) <= clock:
            cap.status = "EXPIRED"
    return count


def on_outbox_accepted(db, outbox):
    """Called ONLY on an actual MAX accepted receipt, idempotent."""
    if (outbox.data or {}).get("human_response") and outbox.data.get("case_id"):
        case = db.get(Entity, outbox.data["case_id"])
        if case and not case.data.get("first_human_response_at"):
            update(
                case,
                first_human_response_at=(
                    outbox.data.get("accepted_at") or now().isoformat()
                ),
            )
    contexts = entities(db, "outbox_domain_context", outbox.id)
    for context in contexts:
        if context.status == "PROCESSED":
            continue
        data = context.data
        accepted = (
            aware(outbox.data.get("accepted_at"))
            if outbox.data.get("accepted_at")
            else now()
        )
        if data.get("simple_resume"):
            from .simple_scenarios import accept_resume
            context = get_entity(db, context.id, "outbox_domain_context", lock=True)
            if context.status == "PROCESSED":
                continue
            accept_resume(db, data, accepted)
            context.status = "PROCESSED"
            continue
        if data.get("survey_run_id"):
            run = db.get(Entity, data["survey_run_id"])
            if run and run.data.get("campaign_id") and data.get("opening"):
                from .survey_campaigns import on_opening_accepted

                on_opening_accepted(db, run, accepted)
                context.status = "PROCESSED"
                continue
            if (
                run
                and data.get("opening")
                and not run.data.get("first_question_accepted_at")
            ):
                run_journey = db.get(Entity, run.parent_id)
                run_version = db.get(Entity, run_journey.data.get("version_id")) if run_journey and run_journey.kind == "journey" else None
                simple_run = bool(run_version and run_version.data.get("graph", {}).get("editor_mode") == "SIMPLE")
                for action in entities(db, "scheduled_action", run.parent_id):
                    if action.data.get("step_key") == run.data["step_key"]:
                        action.status = "WAITING_SURVEY" if simple_run else "ACCEPTED"
                        update(action, accepted_at=accepted.isoformat())
                employee = get_entity(db, run.data["employee_id"], "employee")
                zone = employee.data.get("timezone", "Europe/Moscow")
                local = accepted.astimezone(ZoneInfo(zone))
                days = run.data["definition"].get("expiry_days", 7)
                expires = local_datetime(
                    local.date() + timedelta(days=days),
                    local.strftime("%H:%M:%S"),
                    zone,
                )
                update(
                    run,
                    first_question_accepted_at=accepted.isoformat(),
                    expires_at=expires.isoformat(),
                )
                if (
                    run.status in TERMINAL_SURVEYS
                    or employee.data.get("employment_stage") == "ENDED"
                ):
                    context.status = "PROCESSED"
                    continue
                if expires <= now():
                    run.status = "EXPIRED"
                    context.status = "PROCESSED"
                    continue
                if simple_run:
                    context.status = "PROCESSED"
                    continue
                for n in (1, 2):
                    due = local_datetime(
                        local.date() + timedelta(days=n),
                        local.strftime("%H:%M:%S"),
                        zone,
                    )
                    enqueue(
                        db,
                        "DOMAIN_SURVEY_REMINDER",
                        {"survey_run_id": run.id, "reminder_number": n},
                        f"survey:{run.id}:reminder:{n}",
                        due,
                    )
        if data.get("action_id"):
            action = db.get(Entity, data["action_id"])
            j = db.get(Entity, data.get("journey_id"))
            if action and j:
                if data.get("simple_scenario"):
                    if j.status not in ("CANCELLED", "COMPLETED") and data.get("schedule_revision") == j.data.get("schedule_revision"):
                        from .simple_scenarios import accepted as accept_simple
                        accept_simple(db, j, action, accepted)
                    context.status = "PROCESSED"
                    continue
                action.status = "ACCEPTED"
                update(action, accepted_at=accepted.isoformat(), outbox_id=outbox.id)
                stale = data.get("schedule_revision") is not None and data[
                    "schedule_revision"
                ] != j.data.get("schedule_revision")
                if j.status in ("CANCELLED", "COMPLETED") or stale:
                    update(action, late_accepted_after_fence=True)
                    context.status = "PROCESSED"
                    continue
                step = action.data["step"]
                events = {
                    **j.data.get("events", {}),
                    f"{data['step_key']}:ACTION_ACCEPTED": accepted.isoformat(),
                }
                if step["kind"] == "MESSAGE":
                    events[f"{data['step_key']}:STEP_COMPLETED"] = accepted.isoformat()
                update(j, events=events)
                replan_journey(db, j, get_entity(db, j.parent_id, "employee"))
        context.status = "PROCESSED"


def process_domain_job(db, job):
    if job.kind in {
        "DOMAIN_SURVEY_CAMPAIGN_DELIVER",
        "DOMAIN_SURVEY_CAMPAIGN_REMINDER",
    }:
        from .survey_campaigns import process_job

        return process_job(db, job)
    if job.kind == "DOMAIN_EMPLOYMENT_STAGE_CHANGE":
        from .employment_stages import process

        return process(db, job)
    if job.kind in ("DOMAIN_TICK", "SCENARIO_TICK"):
        run_due_scenarios(db)
        return True
    if job.kind == "DOMAIN_SURVEY_REMINDER":
        run = db.get(Entity, job.payload.get("survey_run_id"))
        if not run or run.status != "IN_PROGRESS":
            return True
        employee = get_entity(db, run.data["employee_id"], "employee")
        j = get_entity(db, run.parent_id, "journey")
        conv = conversation_for(db, employee.id)
        if (
            j.status not in ("ACTIVE", "SCHEDULED")
            or employee.data.get("employment_stage") == "ENDED"
            or (conv and conv.data.get("mode") == "HUMAN")
        ):
            return True
        if run.data.get("expires_at") and aware(run.data["expires_at"]) <= now():
            run.status = "EXPIRED"
            return True
        number = job.payload["reminder_number"]
        emit(
            db,
            employee.id,
            "Небольшое напоминание: опрос ещё не завершён. Можно продолжить с текущего вопроса или отказаться.",
            [
                {
                    "text": "Продолжить опрос",
                    "payload": {"action": "SURVEY_RESUME", "survey_run_id": run.id},
                }
            ],
            f"survey:{run.id}:reminder-send:{number}",
        )
        update(run, reminder_count=max(number, run.data.get("reminder_count", 0)))
        return True
    if job.kind == "DOMAIN_REPORT_EXPORT":
        process_report_export(db, job)
        return True
    if job.kind == "DOMAIN_WEEKLY_DIGEST":
        materialize_weekly_digest(db, job)
        return True
    if job.kind in ("DASHBOARD_REFRESH", "DOMAIN_DASHBOARDS"):
        refresh_dashboards(db)
        return True
    if job.kind == "WEEKLY_DIGEST":
        refresh_dashboards(db)
        return True
    return False


def handle_domain_callback(db, conversation, payload):
    """Root MAX adapter has already checked callback signature + actor binding."""
    employee = get_entity(db, conversation.data["employee_id"], "employee")
    actor = Actor(
        id=employee.data.get("user_id") or employee.id,
        name=employee.data.get("name", "Сотрудник"),
        role="EMPLOYEE",
        scope={},
        employee_id=employee.id,
    )
    action = payload.get("action")
    if action == "SCENARIO_CHOICE":
        from .simple_scenarios import choose
        return choose(db, employee, payload)
    if action == "SURVEY_FINISH":
        return finish_survey(db, actor, payload["survey_run_id"])
    if action == "SURVEY_EDIT":
        return edit_survey_answer(
            db, actor, payload["survey_run_id"], payload.get("question_id")
        )
    if action == "SURVEY_DECLINE":
        run = get_entity(db, payload["survey_run_id"], "survey_run", lock=True)
        assert_answer_actor(db, actor, run.data["employee_id"])
        if run.status not in TERMINAL_SURVEYS:
            run.status = "DECLINED"
            update(run, declined_at=now().isoformat())
        return survey_view(run)
    if action == "SURVEY_ANSWER":
        return submit_survey_answer(db, actor, payload["survey_run_id"], payload)
    if action == "SURVEY_COMMENT_DECLINE":
        run = get_entity(db, payload["survey_run_id"], "survey_run")
        return submit_survey_answer(
            db,
            actor,
            run.id,
            {
                **(run.data.get("pending_answer") or {}),
                **payload,
                "comment_outcome": "DECLINED",
            },
        )
    if action in ("SURVEY_TOGGLE", "SURVEY_SUBMIT_SELECTION"):
        run = get_entity(db, payload["survey_run_id"], "survey_run", lock=True)
        assert_answer_actor(db, actor, run.data["employee_id"])
        assert_campaign_accepting(db, run)
        q = current_question(run)
        if (
            run.status != "IN_PROGRESS"
            or payload.get("question_id") != run.data.get("current_question_key")
            or payload.get("state_revision") != run.data.get("question_revision", 1)
        ):
            raise HTTPException(409, "Кнопка анкеты устарела")
        selection = list(run.data.get("selection_draft", []))
        if action == "SURVEY_SUBMIT_SELECTION":
            return submit_survey_answer(
                db, actor, run.id, {**payload, "value": selection}
            )
        value = payload.get("value")
        option = next((o for o in q.get("options", []) if o["value"] == value), None)
        if not option:
            raise HTTPException(422, "Вариант недоступен")
        if value in selection:
            selection.remove(value)
        elif option.get("exclusive"):
            selection = [value]
        else:
            exclusive = {o["value"] for o in q.get("options", []) if o.get("exclusive")}
            selection = [s for s in selection if s not in exclusive] + [value]
        update(
            run,
            selection_draft=selection,
            selection_revision=run.data.get("selection_revision", 0) + 1,
        )
        send_survey_question(db, run, employee)
        return survey_view(run)
    if action in ("SURVEY_PAUSE", "SURVEY_RESUME"):
        run = get_entity(db, payload["survey_run_id"], "survey_run", lock=True)
        assert_answer_actor(db, actor, run.data["employee_id"])
        if action == "SURVEY_PAUSE" and run.status == "IN_PROGRESS":
            run.status = "PAUSED"
        elif action == "SURVEY_RESUME" and run.status in ("PAUSED", "IN_PROGRESS"):
            assert_campaign_accepting(db, run)
            if run.data.get("expires_at") and aware(run.data["expires_at"]) < now():
                raise HTTPException(409, "Срок анкеты истёк")
            run.status = "IN_PROGRESS"
            update(run, question_revision=run.data.get("question_revision", 1) + 1)
            send_survey_question(db, run, employee)
        else:
            raise HTTPException(409, "Анкета завершена или недоступна")
        return survey_view(run)
    if action == "SCENARIO_ANSWER":
        return scenario_question_answer(
            db, actor, payload["scenario_question_id"], payload
        )
    if action == "CAPTURE_OPEN":
        return pack(open_capture(db, actor, payload))
    if action == "CAPTURE_CONFIRM":
        return confirm_capture(db, actor, payload["capture_id"], payload)
    if action in ("CAPTURE_CANCEL", "CAPTURE_TO_QUESTION"):
        cap = get_entity(db, payload["capture_id"], "input_capture", lock=True)
        assert_answer_actor(db, actor, cap.data["employee_id"])
        if not current_employee_record(db, actor, cap.data["employee_id"], cap):
            raise HTTPException(404, "Черновик недоступен")
        cap.status = "CANCELLED"
        return {
            "question": (
                cap.data.get("draft") if action == "CAPTURE_TO_QUESTION" else None
            ),
            "capture": pack(cap),
        }
    if action == "BLOCKER_CONFIRM":
        return blocker_response(db, actor, payload["case_id"], payload)
    if action == "CONTINUE_SURVEY":
        run = next(
            (
                s
                for s in entities(db, "survey_run")
                if s.data.get("employee_id") == employee.id
                and s.data.get("episode_id") == employee.data.get("episode_id")
                and s.status in ("IN_PROGRESS", "PAUSED")
            ),
            None,
        )
        if not run:
            return {"text": "Сейчас нет активного опроса."}
        run.status = "IN_PROGRESS"
        send_survey_question(db, run, employee)
        return survey_view(run)
    return None


def notify_user(db, user_id, text, key, data=None):
    if not user_id:
        return None
    user = db.get(User, user_id)
    if not user or not user.enabled:
        return None
    old = db.execute(
        select(Outbox).where(Outbox.unique_key == key)
    ).scalar_one_or_none()
    if old:
        return old
    out = Outbox(
        id=new_id(),
        recipient_kind="USER",
        recipient_id=user_id,
        text=text,
        attachments=[],
        callback_buttons=[],
        status="PENDING",
        unique_key=key,
        created_at=now(),
        data={
            "notification_type": "DOMAIN_DEADLINE",
            "required_roles": list(READ_HR) + ["MANAGER"],
            **(data or {}),
        },
    )
    db.add(out)
    return out


def scan_deadlines(db):
    clock = now()
    for case in entities(db, "case"):
        employee = db.get(Entity, case.data["employee_id"])
        if not employee:
            continue
        if (
            case.data.get("due_at")
            and not case.data.get("first_human_response_at")
            and aware(case.data["due_at"]) <= clock
            and case.status
            not in ("CANCELLED", "CLOSED", "CONFIRMED", "CLOSED_WITHOUT_CONFIRMATION")
        ):
            update(case, first_response_overdue=True)
            recipients = []
            policy = db.get(Entity, case.data.get("sla_policy_id"))
            if policy:
                for rule in policy.data.get("queue_rules", []):
                    if rule.get("calendar_version_id") == case.data.get(
                        "sla_calendar_version_id"
                    ):
                        recipients.extend(rule.get("escalation_recipient_user_ids", []))
            recipients = recipients or [case.owner_id]
            for uid in set(recipients):
                user = db.get(User, uid) if uid else None
                if not user:
                    continue
                actor = Actor(
                    id=user.id,
                    name=user.name,
                    role=user.role,
                    scope=user.scope,
                    employee_id=user.employee_id,
                )
                if actor_can_employee(db, actor, employee.id):
                    notify_user(
                        db,
                        uid,
                        "Просрочен срок первой реакции на обращение. Проверьте очередь HR в кабинете Корсика.",
                        f"case:{case.id}:sla-overdue:{uid}",
                        data={"employee_id": employee.id, "required_roles": list(HR)},
                    )
        if (
            case.status == "RESOLVED_PENDING_CONFIRMATION"
            and case.data.get("confirmation_due_at")
            and aware(case.data["confirmation_due_at"]) <= clock
        ):
            update(case, confirmation_overdue=True)
            if employee.data.get("employment_stage") != "ENDED":
                conv = conversation_for(db, employee.id)
                if not conv or conv.data.get("mode") != "HUMAN":
                    emit(
                        db,
                        employee.id,
                        "Пожалуйста, подтвердите, устранена ли проблема. Отсутствие ответа не считается подтверждением.",
                        [
                            {
                                "text": "Да, работает",
                                "payload": {
                                    "action": "BLOCKER_CONFIRM",
                                    "case_id": case.id,
                                    "resolution_attempt_id": case.data[
                                        "current_resolution_attempt_id"
                                    ],
                                    "confirmation_request_id": case.data[
                                        "confirmation_request_id"
                                    ],
                                    "resolved": True,
                                },
                            },
                            {
                                "text": "Нет, не работает",
                                "payload": {
                                    "action": "BLOCKER_CONFIRM",
                                    "case_id": case.id,
                                    "resolution_attempt_id": case.data[
                                        "current_resolution_attempt_id"
                                    ],
                                    "confirmation_request_id": case.data[
                                        "confirmation_request_id"
                                    ],
                                    "resolved": False,
                                },
                            },
                        ],
                        f"blocker:{case.id}:confirmation-reminder:{case.data['current_resolution_attempt_id']}",
                    )
            notify_user(
                db,
                case.owner_id,
                "По обращению ожидается подтверждение сотрудника. Автоматического закрытия нет.",
                f"blocker:{case.id}:confirmation-overdue:{case.data['current_resolution_attempt_id']}",
                data={"employee_id": employee.id, "required_roles": list(HR)},
            )
    for task in entities(db, "manager_task"):
        if (
            task.status in ("COMPLETED", "CANCELLED")
            or not task.data.get("due_at")
            or aware(task.data["due_at"]) > clock
        ):
            continue
        e = db.get(Entity, task.data["employee_id"])
        if (
            not e
            or e.data.get("employment_stage") == "ENDED"
            or task.data.get("episode_id") != e.data.get("episode_id")
        ):
            continue
        update(task, overdue=True)
        uid = task.data.get("assigned_manager_id")
        user = db.get(User, uid) if uid else None
        if user and user.enabled and user.role == "MANAGER":
            notify_user(
                db,
                uid,
                "Просрочена задача по адаптации сотрудника. Откройте задачи в кабинете Корсика.",
                f"manager-task:{task.id}:overdue:{task.data.get('assignment_revision',1)}",
                data={
                    "manager_task_id": task.id,
                    "assignment_revision": task.data.get("assignment_revision", 1),
                    "required_roles": ["MANAGER"],
                },
            )
    db.flush()


@router.get("/recurring-schedules")
def recurring_schedules(db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *PUBLISH)
    return {"items": [pack(e) for e in entities(db, "recurring_schedule")]}


@router.post("/recurring-schedules")
def create_recurring(payload: dict, db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *PUBLISH)
    if actor.scope.get("all") is not True:
        raise HTTPException(403, "Общие задания изменяет администратор всего контура")
    if payload.get("job_kind") not in ("WEEKLY_DIGEST", "DASHBOARD_REFRESH"):
        raise HTTPException(422, "Выберите разрешённую периодическую работу")
    if payload.get("frequency", "WEEKLY") not in ("DAILY", "WEEKLY"):
        raise HTTPException(
            422, "Поддерживается ежедневное или еженедельное расписание"
        )
    weekday = payload.get("weekday", 0)
    if type(weekday) is not int or not 0 <= weekday <= 6:
        raise HTTPException(422, "Укажите день недели")
    try:
        ZoneInfo(payload.get("timezone", "Europe/Moscow"))
        datetime.strptime(payload.get("local_time", "09:00"), "%H:%M")
    except (ValueError, ZoneInfoNotFoundError):
        raise HTTPException(422, "Неверный часовой пояс или время")
    e = save_entity(
        db,
        "recurring_schedule",
        {
            "name": payload.get("name", "Периодический отчёт"),
            "job_kind": payload["job_kind"],
            "frequency": payload.get("frequency", "WEEKLY"),
            "weekday": weekday,
            "local_time": payload.get("local_time", "09:00"),
            "timezone": payload.get("timezone", "Europe/Moscow"),
            "scheduler_owner": "INTERNAL",
            "owner_epoch": 1,
            "enabled": payload.get("enabled", True),
        },
        parent_id=actor.id,
    )
    audit(db, actor, "recurrence.create", e.id)
    commit(db)
    return reply(e, 201)


@router.patch("/recurring-schedules/{schedule_id}")
def patch_recurring(
    schedule_id: str,
    payload: dict,
    request: Request,
    db=Depends(get_db),
    actor=Depends(get_actor),
):
    require_role(actor, *PUBLISH)
    if actor.scope.get("all") is not True:
        raise HTTPException(403, "Общие задания изменяет администратор всего контура")
    e = get_entity(db, schedule_id, "recurring_schedule", lock=True)
    check_etag(request, e)
    if any(k not in ("enabled", "name", "local_time", "weekday") for k in payload):
        raise HTTPException(422, "Это поле расписания нельзя менять")
    if "local_time" in payload:
        try:
            datetime.strptime(payload["local_time"], "%H:%M")
        except ValueError:
            raise HTTPException(422, "Неверное время")
    if "weekday" in payload and (
        type(payload["weekday"]) is not int or not 0 <= payload["weekday"] <= 6
    ):
        raise HTTPException(422, "Неверный день недели")
    update(e, **payload)
    audit(db, actor, "recurrence.update", e.id)
    commit(db)
    return reply(e)


def dispatch_recurring(db):
    clock = now()
    for schedule in entities(db, "recurring_schedule"):
        data = schedule.data
        if not data.get("enabled") or data.get("scheduler_owner") != "INTERNAL":
            continue
        local = clock.astimezone(ZoneInfo(data["timezone"]))
        day = local.date()
        if data["frequency"] == "WEEKLY":
            day -= timedelta(days=(day.weekday() - data["weekday"]) % 7)
        due = local_datetime(day, data["local_time"], data["timezone"])
        if due > clock:
            continue
        period = day.isoformat()
        key = f"recurring:{schedule.id}:period:{period}"
        # Immutable period identity deliberately excludes settings revision.
        enqueue(
            db,
            data["job_kind"],
            {
                "schedule_id": schedule.id,
                "period": period,
                "owner_epoch": data["owner_epoch"],
            },
            key,
            clock,
        )
    db.flush()


def utc_iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def scope_subset(child, parent):
    if parent.get("all"):
        return True
    if child.get("all"):
        return False
    for key in (
        "site_ids",
        "legal_entity_ids",
        "employment_stages",
        "employment_types",
    ):
        if parent.get(key) and (
            not child.get(key) or not set(child[key]).issubset(set(parent[key]))
        ):
            return False
    return bool(parent.get("site_ids") or parent.get("legal_entity_ids"))


def validate_digest_schedule(db, actor, payload):
    data = copy.deepcopy(payload)
    data["recipient_user_ids"] = data.get(
        "recipient_user_ids", data.get("recipient_ids", [actor.id])
    )
    data.setdefault("scope", actor.scope)
    data.setdefault("channels", ["HR_PANEL", "MAX"])
    data.setdefault("enabled", True)
    data.setdefault("timezone", "Europe/Moscow")
    data.setdefault("weekday", 1)
    data.setdefault("local_time", "09:00")
    if type(data["weekday"]) is not int or not 1 <= data["weekday"] <= 7:
        raise HTTPException(422, "День недели должен быть от 1 до 7")
    try:
        ZoneInfo(data["timezone"])
        datetime.strptime(data["local_time"], "%H:%M")
    except (ValueError, ZoneInfoNotFoundError):
        raise HTTPException(422, "Неверное время или часовой пояс")
    if not isinstance(data["scope"], dict):
        raise HTTPException(422, "Область отчёта должна быть объектом")
    if not scope_subset(data["scope"], actor.scope or {}):
        raise HTTPException(403, "Область отчёта шире ваших полномочий")
    if (
        not data["recipient_user_ids"]
        or len(data["recipient_user_ids"]) > 100
        or len(set(data["recipient_user_ids"])) != len(data["recipient_user_ids"])
    ):
        raise HTTPException(422, "Укажите 1–100 уникальных получателей")
    for uid in data["recipient_user_ids"]:
        user = db.get(User, uid)
        if not user or not user.enabled or user.role not in READ_HR + ("MANAGER",):
            raise HTTPException(422, "Получатель не имеет права на кадровые отчёты")
        if user.role != "MANAGER" and not scope_subset(data["scope"], user.scope or {}):
            raise HTTPException(403, "Получатель не имеет доступа к области отчёта")
    if not set(data["channels"]).issubset({"HR_PANEL", "MAX"}) or not data["channels"]:
        raise HTTPException(422, "Выберите кабинет и/или MAX")
    return {
        k: data[k]
        for k in (
            "name",
            "enabled",
            "weekday",
            "local_time",
            "timezone",
            "scope",
            "recipient_user_ids",
            "channels",
        )
        if k in data
    }


def digest_occurrence(day, data):
    due = local_datetime(day, data["local_time"], data["timezone"])
    zone = ZoneInfo(data["timezone"])
    monday = day - timedelta(days=day.weekday())
    start = local_datetime(monday - timedelta(days=7), "00:00", data["timezone"])
    end = local_datetime(monday, "00:00", data["timezone"])
    return {
        "due_at": utc_iso(due),
        "due_local": due.astimezone(zone).isoformat(),
        "period_from": utc_iso(start),
        "period_to": utc_iso(end),
        "period_from_local": start.astimezone(zone).isoformat(),
        "period_to_local": end.astimezone(zone).isoformat(),
    }


def digest_preview_values(data, after, count):
    local = after.astimezone(ZoneInfo(data["timezone"]))
    day = local.date() + timedelta(days=(data["weekday"] - 1 - local.weekday()) % 7)
    if local_datetime(day, data["local_time"], data["timezone"]) <= after:
        day += timedelta(days=7)
    return [digest_occurrence(day + timedelta(days=7 * i), data) for i in range(count)]


@router.get("/reports/digest-schedules")
def digest_schedules(db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *READ_HR)
    items = []
    for e in entities(db, "digest_schedule"):
        if scope_subset(e.data.get("scope", {}), actor.scope or {}):
            items.append(
                pack(
                    e,
                    {
                        "schedule_id": e.id,
                        "next_due_at": digest_preview_values(e.data, now(), 1)[0][
                            "due_at"
                        ],
                    },
                )
            )
    return {"items": items, "next_cursor": None, "has_more": False}


@router.post("/reports/digest-schedules/preview")
def digest_schedule_preview(
    payload: dict, db=Depends(get_db), actor=Depends(get_actor)
):
    require_role(actor, *READ_HR)
    data = validate_digest_schedule(db, actor, payload.get("schedule", payload))
    after = aware(payload.get("after", now()))
    count = payload.get("occurrence_count", 3)
    if not now() - timedelta(hours=24) <= after <= now() + timedelta(days=365):
        raise HTTPException(422, "Начальная дата вне разрешённого горизонта")
    if type(count) is not int or not 1 <= count <= 5:
        raise HTTPException(422, "Можно рассчитать 1–5 запусков")
    return {
        "timezone": data["timezone"],
        "calculated_at": utc_iso(now()),
        "occurrences": digest_preview_values(data, after, count),
        "warnings": (
            []
            if data["enabled"]
            else [
                {
                    "code": "DISABLED_SCHEDULE",
                    "message": "Показаны возможные даты; расписание выключено",
                }
            ]
        ),
    }


@router.post("/reports/digest-schedules")
def digest_schedule_create(payload: dict, db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *READ_HR)
    data = validate_digest_schedule(db, actor, payload)
    if not payload.get("reason"):
        raise HTTPException(422, "Укажите причину создания расписания")
    e = save_entity(
        db,
        "digest_schedule",
        {
            **data,
            "scheduler_owner": "INTERNAL",
            "owner_epoch": 1,
            "created_by": actor.id,
        },
        parent_id=actor.id,
    )
    audit(db, actor, "digest.schedule.create", e.id, reason=payload["reason"])
    commit(db)
    return reply(
        e,
        201,
        {
            "schedule_id": e.id,
            "next_due_at": digest_preview_values(data, now(), 1)[0]["due_at"],
        },
    )


@router.get("/reports/digest-schedules/{schedule_id}")
def digest_schedule_get(schedule_id: str, db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *READ_HR)
    e = get_entity(db, schedule_id, "digest_schedule")
    if not scope_subset(e.data.get("scope", {}), actor.scope):
        raise HTTPException(404, "Расписание недоступно")
    return reply(
        e,
        extra={
            "schedule_id": e.id,
            "next_due_at": digest_preview_values(e.data, now(), 1)[0]["due_at"],
        },
    )


@router.put("/reports/digest-schedules/{schedule_id}")
def digest_schedule_put(
    schedule_id: str,
    payload: dict,
    request: Request,
    db=Depends(get_db),
    actor=Depends(get_actor),
):
    require_role(actor, *READ_HR)
    e = get_entity(db, schedule_id, "digest_schedule", lock=True)
    check_etag(request, e)
    if not scope_subset(e.data.get("scope", {}), actor.scope):
        raise HTTPException(404, "Расписание недоступно")
    if not payload.get("reason"):
        raise HTTPException(422, "Укажите причину изменения")
    data = validate_digest_schedule(db, actor, payload)
    update(e, **data)
    audit(db, actor, "digest.schedule.update", e.id, reason=payload["reason"])
    commit(db)
    return reply(
        e,
        extra={
            "schedule_id": e.id,
            "next_due_at": digest_preview_values(data, now(), 1)[0]["due_at"],
        },
    )


def dispatch_digest_schedules(db):
    clock = now()
    for schedule in entities(db, "digest_schedule"):
        data = schedule.data
        if not data.get("enabled") or data.get("scheduler_owner") != "INTERNAL":
            continue
        local = clock.astimezone(ZoneInfo(data["timezone"]))
        day = local.date() - timedelta(
            days=(local.weekday() - (data["weekday"] - 1)) % 7
        )
        occurrence = digest_occurrence(day, data)
        if aware(occurrence["due_at"]) > clock:
            continue
        # No automatic history backfill and no duplicate after a settings revision.
        if aware(occurrence["due_at"]) < aware(schedule.created_at):
            continue
        period = occurrence["period_from_local"][:10]
        enqueue(
            db,
            "DOMAIN_WEEKLY_DIGEST",
            {
                "schedule_id": schedule.id,
                "owner_epoch": data["owner_epoch"],
                "period": period,
                **occurrence,
            },
            f"digest:{schedule.id}:period:{period}",
        )
    db.flush()


def materialize_weekly_digest(db, job):
    schedule = db.get(Entity, job.payload.get("schedule_id"))
    if (
        not schedule
        or not schedule.data.get("enabled")
        or schedule.data.get("owner_epoch") != job.payload.get("owner_epoch")
    ):
        return
    start = aware(job.payload["period_from"])
    end = aware(job.payload["period_to"])
    data = schedule.data
    period = job.payload["period"]
    for uid in data["recipient_user_ids"]:
        user = db.get(User, uid)
        if not user or not user.enabled:
            continue
        actor = Actor(
            id=user.id,
            name=user.name,
            role=user.role,
            scope=user.scope,
            employee_id=user.employee_id,
        )
        if actor.role not in READ_HR + ("MANAGER",):
            continue
        if actor.role != "MANAGER" and not scope_subset(data["scope"], actor.scope):
            continue
        key = f"digest:{schedule.id}:period:{period}:recipient:{uid}"
        old = next(
            (
                e
                for e in entities(db, "weekly_digest", uid)
                if e.data.get("unique_key") == key
            ),
            None,
        )
        if old:
            continue
        scoped = employee_scope(db, actor)
        ids = {
            e.id
            for e in scoped
            if (
                not data["scope"].get("site_ids")
                or e.data.get("site_id") in data["scope"]["site_ids"]
            )
            and (
                not data["scope"].get("legal_entity_ids")
                or e.data.get("legal_entity_id") in data["scope"]["legal_entity_ids"]
            )
        }
        episode_ids = {e.id: e.data.get("episode_id") for e in scoped}
        runs = [
            r
            for r in entities(db, "survey_run")
            if r.data["employee_id"] in ids
            and not r.data.get("campaign_id")
            and r.data.get("episode_id") == episode_ids.get(r.data["employee_id"])
            and r.data.get("first_question_accepted_at")
            and start <= aware(r.data["first_question_accepted_at"]) < end
        ]
        completed = [r for r in runs if r.status == "COMPLETED"]
        participated = [r for r in runs if r.data.get("answers")]
        feedback_rows = [
            f
            for f in entities(db, "feedback")
            if f.data.get("employee_id") in ids
            and (
                not f.data.get("episode_id")
                or f.data["episode_id"] == episode_ids.get(f.data["employee_id"])
            )
            and (
                not f.data.get("source_site_id")
                or f.data["source_site_id"]
                in data["scope"].get("site_ids", [f.data["source_site_id"]])
            )
            and start <= aware(f.created_at) < end
        ]
        feedback_ids = {f.id for f in feedback_rows}
        analyses = [
            a
            for a in entities(db, "feedback_analysis")
            if a.data.get("feedback_id") in feedback_ids
        ]
        # Deduplicate re-analysis and use the most recent reviewed feedback per
        # employee as the weekly mood category; topics may overlap independently.
        latest_analyses = {
            a.data["feedback_id"]: a
            for a in sorted(analyses, key=lambda a: (aware(a.created_at), a.id))
        }
        analyses = list(latest_analyses.values())
        reviewed = [a for a in analyses if a.status == "REVIEWED"]
        feedback_by_id = {f.id: f for f in feedback_rows}
        latest_mood = {}
        topics = {}
        for analysis in sorted(
            reviewed,
            key=lambda a: (
                aware(feedback_by_id[a.data["feedback_id"]].created_at),
                a.id,
            ),
        ):
            employee_id = feedback_by_id[analysis.data["feedback_id"]].data[
                "employee_id"
            ]
            latest_mood[employee_id] = analysis.data.get("sentiment", "UNCLEAR")
            for topic in analysis.data.get("topics", []):
                if isinstance(topic, str):
                    topics.setdefault(topic, set()).add(employee_id)
        reviewed_people = set(latest_mood)
        mood = {}
        for employee_id, label in latest_mood.items():
            mood.setdefault(label, set()).add(employee_id)
        permitted_sensitive = len(reviewed_people) >= 5
        open_cases = [
            c
            for c in entities(db, "case")
            if c.data.get("employee_id") in ids
            and c.status
            not in (
                "CLOSED",
                "CONFIRMED",
                "CANCELLED",
                "CLOSED_WITHOUT_CONFIRMATION",
                "RESOLVED",
            )
        ]
        open_tasks = [
            t
            for t in entities(db, "manager_task")
            if t.data.get("employee_id") in ids
            and t.status not in ("COMPLETED", "CANCELLED")
        ]
        metrics = {
            "period_from": start.isoformat(),
            "period_to": end.isoformat(),
            "timezone": data["timezone"],
            "period_semantics": "FROM_INCLUSIVE_TO_EXCLUSIVE",
            "as_of": now().isoformat(),
            "employees_in_scope": len(ids),
            "surveys_missing_answers": sum(
                not r.data.get("answers") and r.status != "DECLINED" for r in runs
            ),
            "feedback_received": len(feedback_rows),
            "feedback_reviewed": len(reviewed),
            "feedback_pending_review": len(
                feedback_ids - {a.data.get("feedback_id") for a in reviewed}
            ),
            "reviewed_respondents": len(reviewed_people),
            "sentiment_distribution": [
                {
                    "sentiment": (
                        label if permitted_sensitive and len(people) >= 5 else None
                    ),
                    "employee_count": (
                        len(people)
                        if permitted_sensitive and len(people) >= 5
                        else None
                    ),
                    "denominator": (
                        len(reviewed_people) if permitted_sensitive else None
                    ),
                    "suppressed": not permitted_sensitive or len(people) < 5,
                    "method": "LATEST_HUMAN_REVIEWED_FEEDBACK_PER_EMPLOYEE",
                }
                for label, people in sorted(mood.items())
            ],
            "topics": [
                {
                    "topic": (
                        topic if permitted_sensitive and len(people) >= 5 else None
                    ),
                    "employee_count": (
                        len(people)
                        if permitted_sensitive and len(people) >= 5
                        else None
                    ),
                    "suppressed": not permitted_sensitive or len(people) < 5,
                    "method": "HUMAN_REVIEWED_UNIQUE_EMPLOYEES",
                }
                for topic, people in sorted(topics.items())
            ],
            "sensitive_group_minimum": 5,
            "analysis_authority": "HUMAN_REVIEW_ONLY",
            "model_quality": "NOT_COMPANY_ACCEPTED",
            "current_open_actions": {
                "open_blockers": sum(
                    c.data.get("kind") == "BLOCKER" for c in open_cases
                ),
                "cases_waiting_first_response": sum(
                    not c.data.get("first_human_response_at") for c in open_cases
                ),
                "sla_overdue": sum(
                    bool(c.data.get("first_response_overdue")) for c in open_cases
                ),
                "manager_tasks_open": len(open_tasks),
                "manager_tasks_overdue": sum(
                    bool(t.data.get("due_at")) and aware(t.data["due_at"]) < now()
                    for t in open_tasks
                ),
                "as_of": now().isoformat(),
                "period_semantics": "CURRENT_BACKLOG_AT_MATERIALIZATION",
            },
            "surveys_opened": len(runs),
            "surveys_completed": len(completed),
            "surveys_participated": len(participated),
            "surveys_declined": sum(r.status == "DECLINED" for r in runs),
            "completion": {
                "numerator": len(completed),
                "denominator": len(runs),
                "rate": len(completed) / len(runs) if runs else None,
            },
            "participation": {
                "numerator": len(participated),
                "denominator": len(runs),
                "rate": len(participated) / len(runs) if runs else None,
            },
            "sensitive_aggregates_suppressed": not permitted_sensitive,
        }
        save_entity(
            db,
            "weekly_digest",
            {
                "unique_key": key,
                "schedule_id": schedule.id,
                "recipient_user_id": uid,
                "period_from": job.payload["period_from"],
                "period_to": job.payload["period_to"],
                "metrics": metrics,
                "employee_ids": sorted(ids),
                "scope": data["scope"],
                "raw_comments_included": False,
                "generated_at": now().isoformat(),
                "method": "SQL_COHORT_ACCEPTED_OPENING",
            },
            parent_id=uid,
            owner_id=uid,
        )
        if "MAX" in data["channels"]:
            notify_user(
                db,
                uid,
                f"Недельный отчёт адаптации готов. Открыто анкет: {len(runs)}, завершено: {len(completed)}. Подробности доступны в кабинете Корсика.",
                key + ":notify",
                data={"employee_ids": sorted(ids), "required_scope": data["scope"]},
            )
    db.flush()


@router.get("/reports/weekly-digests")
def weekly_digests(db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *READ_HR, "MANAGER")
    allowed_ids = {e.id for e in employee_scope(db, actor)}
    items = []
    for digest in entities(db, "weekly_digest", actor.id):
        if actor.role != "MANAGER" and not scope_subset(
            digest.data.get("scope", {}), actor.scope
        ):
            continue
        ids = digest.data.get("employee_ids")
        if ids is None or not set(ids).issubset(allowed_ids):
            continue
        value = pack(digest)
        if not can_employee_drilldown(actor):
            value.pop("employee_ids", None)
        items.append(value)
    return {"items": items}


@router.post("/reports/weekly-digests")
def manual_weekly_digest(payload: dict, db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *READ_HR)
    schedule_id = payload.get("schedule_id")
    schedule = get_entity(db, schedule_id, "digest_schedule")
    if not scope_subset(schedule.data["scope"], actor.scope):
        raise HTTPException(404, "Расписание недоступно")
    day = now().astimezone(ZoneInfo(schedule.data["timezone"])).date()
    occurrence = digest_occurrence(day, schedule.data)
    period = occurrence["period_from_local"][:10]
    job = enqueue(
        db,
        "DOMAIN_WEEKLY_DIGEST",
        {
            "schedule_id": schedule.id,
            "owner_epoch": schedule.data["owner_epoch"],
            "period": period,
            **occurrence,
        },
        f"digest:{schedule.id}:period:{period}",
    )
    audit(db, actor, "digest.request", schedule.id)
    commit(db)
    return {"job_id": job.id, "status": job.status}


def ensure_knowledge_gap(
    db, employee_id, question, answer_id=None, reason="NO_EVIDENCE"
):
    employee = get_entity(db, employee_id, "employee")
    question = str(question)[:6000]
    digest = hashlib.sha256((employee_id + "\n" + question).encode()).hexdigest()
    previous = next(
        (
            e
            for e in entities(db, "knowledge_gap")
            if e.data.get("deduplication_key") == digest
            and e.status not in ("RESOLVED", "CANCELLED")
        ),
        None,
    )
    if previous:
        return previous
    return save_entity(
        db,
        "knowledge_gap",
        {
            "employee_id": employee_id,
            "question": question,
            "answer_id": answer_id,
            "reason": reason,
            "deduplication_key": digest,
            "scope": {
                "site_ids": [employee.data["site_id"]],
                "legal_entity_ids": (
                    [employee.data["legal_entity_id"]]
                    if employee.data.get("legal_entity_id")
                    else []
                ),
                "employment_stages": [employee.data.get("employment_stage", "ACTIVE")],
            },
            "owner_id": employee.data.get("assigned_hr_id"),
            "instruction_revision_ids": [],
            "case_ids": [],
        },
        status="OPEN",
        parent_id=employee_id,
        owner_id=employee.data.get("assigned_hr_id"),
    )


def gap_authorize(db, actor, gap):
    require_role(actor, *HR)
    assert_employee(db, actor, gap.data["employee_id"])


@router.get("/knowledge/gaps")
@router.get("/knowledge-gaps", include_in_schema=False)
def knowledge_gaps(db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *HR)
    return {
        "items": [
            pack(e)
            for e in entities(db, "knowledge_gap")
            if e.data.get("employee_id")
            and actor_can_employee(db, actor, e.data["employee_id"])
        ]
    }


@router.get("/knowledge/gaps/{gap_id}")
def gap_get(gap_id: str, db=Depends(get_db), actor=Depends(get_actor)):
    gap = get_entity(db, gap_id, "knowledge_gap")
    gap_authorize(db, actor, gap)
    return reply(gap)


@router.post("/knowledge/gaps/{gap_id}/assign")
def gap_assign(
    gap_id: str,
    payload: dict,
    request: Request,
    db=Depends(get_db),
    actor=Depends(get_actor),
):
    gap = get_entity(db, gap_id, "knowledge_gap", lock=True)
    gap_authorize(db, actor, gap)
    check_etag(request, gap)
    user = db.get(User, payload.get("owner_id"))
    if not user or not user.enabled or user.role not in HR:
        raise HTTPException(422, "Назначьте HR")
    target = Actor(user.id, user.name, user.role, user.scope, user.employee_id)
    if not actor_can_employee(db, target, gap.data["employee_id"]):
        raise HTTPException(403, "HR не имеет доступа к вопросу")
    gap.owner_id = user.id
    update(gap, owner_id=user.id)
    audit(db, actor, "gap.assign", gap.id)
    commit(db)
    return reply(gap)


@router.post("/knowledge/gaps/{gap_id}/instruction-drafts")
def gap_instruction(
    gap_id: str, payload: dict, db=Depends(get_db), actor=Depends(get_actor)
):
    gap = get_entity(db, gap_id, "knowledge_gap", lock=True)
    gap_authorize(db, actor, gap)
    if payload.get("anonymization_confirmed") is not True:
        raise HTTPException(422, "Подтвердите обезличивание инструкции")
    instruction = payload.get("instruction") or {}
    if (
        not instruction.get("question")
        or not instruction.get("answer_text")
        or not payload.get("dependencies")
    ):
        raise HTTPException(
            422, "Укажите общий вопрос, ответ и ссылки на первоисточники"
        )
    scope = payload.get("scope") or gap.data["scope"]
    if not scope_subset(scope, {**gap.data["scope"], "all": False}):
        raise HTTPException(403, "Нельзя расширить область из приватного вопроса")
    from .knowledge import create_document, create_article

    response = Response()
    doc_result = create_document(
        {
            "title": payload.get("title") or instruction["question"][:180],
            "scope": scope,
            "document_category": "INSTRUCTION",
            "description": "Обезличенная инструкция, подготовленная HR",
        },
        response,
        actor,
        db,
    )
    doc_id = doc_result.get("document_id", doc_result.get("id"))
    doc = get_entity(db, doc_id, "document")
    request = Request({"type": "http", "headers": [(b"if-match", etag(doc).encode())]})
    article = create_article(
        doc.id,
        {
            "article_kind": "APPROVED_INSTRUCTION",
            "instruction": instruction,
            "dependencies": payload["dependencies"],
            "reason": payload.get("reason", "Устранение пробела знаний"),
            "origin_gap_id": gap.id,
            "effective_from": payload.get("effective_from"),
        },
        request,
        Response(),
        actor,
        db,
    )
    revision_id = article.get("revision_id", article.get("id"))
    gap = get_entity(db, gap.id, "knowledge_gap", lock=True)
    update(
        gap,
        instruction_revision_ids=gap.data.get("instruction_revision_ids", [])
        + [revision_id],
        anonymization_confirmed_by=actor.id,
    )
    gap.status = "INSTRUCTION_DRAFT"
    audit(db, actor, "gap.instruction-draft", gap.id)
    commit(db)
    return {
        "gap": pack(gap),
        "document_id": doc.id,
        "revision_id": revision_id,
        "status": "REVIEW_REQUIRED",
        "published": False,
    }


@router.post("/knowledge/gaps/{gap_id}/resolve")
def gap_resolve(
    gap_id: str, payload: dict, db=Depends(get_db), actor=Depends(get_actor)
):
    gap = get_entity(db, gap_id, "knowledge_gap", lock=True)
    gap_authorize(db, actor, gap)
    from .knowledge import _revision
    from .retrieval import is_effective, scope_matches

    revision, document = _revision(
        db,
        actor,
        payload.get("instruction_revision_id", payload.get("revision_id", "")),
        edit=True,
    )
    head = db.scalar(
        select(Entity)
        .where(Entity.kind == "knowledge_head")
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    snapshot = db.get(Entity, head.data.get("snapshot_id")) if head else None
    employee = get_entity(db, gap.data["employee_id"], "employee")
    if (
        document.status != "ACTIVE"
        or document.data.get("active_revision_id") != revision.id
        or not snapshot
        or snapshot.data.get("members", {}).get(document.id) != revision.id
        or not is_effective(revision.data)
        or not scope_matches(document.data.get("scope", {}), employee)
        or revision.status != "PUBLISHED"
        or revision.data.get("article_kind") != "APPROVED_INSTRUCTION"
        or revision.data.get("origin_gap_id") != gap.id
    ):
        raise HTTPException(
            409,
            "Пробел закрывается только своей опубликованной подтверждённой инструкцией",
        )
    gap.status = "RESOLVED"
    update(
        gap,
        resolved_at=now().isoformat(),
        resolved_by=actor.id,
        published_instruction_revision_id=revision.id,
    )
    audit(db, actor, "gap.resolve", gap.id)
    commit(db)
    return reply(gap)


def finish_survey(db, actor, run_id):
    run = get_entity(db, run_id, "survey_run", lock=True)
    assert_answer_actor(db, actor, run.data["employee_id"])
    if not current_employee_record(db, actor, run.data["employee_id"], run):
        raise HTTPException(404, "Анкета недоступна")
    if run.status == "COMPLETED":
        return survey_view(run)
    assert_campaign_accepting(db, run)
    if run.status != "IN_PROGRESS" or not run.data.get(
        "awaiting_completion_confirmation"
    ):
        raise HTTPException(409, "Сначала ответьте на вопросы анкеты")
    if run.data.get("expires_at") and aware(run.data["expires_at"]) <= now():
        raise HTTPException(409, "Срок анкеты истёк")
    conv = conversation_for(db, run.data["employee_id"])
    if conv and conv.data.get("mode") == "HUMAN":
        raise HTTPException(409, "Сначала завершите разговор с HR")
    answers = run.data.get("answers", {})
    if any(
        q.get("required") and q["question_key"] not in answers
        for q in run.data["definition"]["questions"]
    ):
        raise HTTPException(
            409, "Не все обязательные вопросы отвечены или явно пропущены"
        )
    employee = get_entity(db, run.data["employee_id"], "employee")
    run.status = "COMPLETED"
    update(run, completed_at=now().isoformat(), awaiting_completion_confirmation=False)
    emit(
        db,
        employee.id,
        render_text(run.data["definition"].get("thank_you", {}), employee),
        unique_key=f"survey:{run.id}:thanks",
    )
    if run.data.get("campaign_id"):
        return survey_view(run)
    j = get_entity(db, run.parent_id, "journey")
    events = {
        **j.data.get("events", {}),
        f"{run.data['step_key']}:SURVEY_COMPLETED": now().isoformat(),
        f"{run.data['step_key']}:STEP_COMPLETED": now().isoformat(),
    }
    update(
        j,
        events=events,
        completed_steps=list(
            set(j.data.get("completed_steps", []) + [run.data["step_key"]])
        ),
    )
    replan_journey(db, j, employee)
    return survey_view(run)


def edit_survey_answer(db, actor, run_id, question_id):
    run = get_entity(db, run_id, "survey_run", lock=True)
    assert_answer_actor(db, actor, run.data["employee_id"])
    if not current_employee_record(db, actor, run.data["employee_id"], run):
        raise HTTPException(404, "Анкета недоступна")
    if run.status != "IN_PROGRESS" or not run.data.get(
        "awaiting_completion_confirmation"
    ):
        raise HTTPException(409, "Изменение доступно перед подтверждением завершения")
    if question_id not in run.data.get("answers", {}):
        raise HTTPException(422, "Ответ не найден")
    update(
        run,
        current_question_key=question_id,
        editing_question_key=question_id,
        awaiting_completion_confirmation=False,
        pending_answer=None,
        question_revision=run.data.get("question_revision", 1) + 1,
    )
    send_survey_question(db, run, get_entity(db, run.data["employee_id"], "employee"))
    return survey_view(run)


def complete_control_action(db, journey, action, employee):
    action.status = "COMPLETED"
    update(action, completed_at=now().isoformat())
    events = {
        **journey.data.get("events", {}),
        f"{action.data['step_key']}:STEP_COMPLETED": now().isoformat(),
    }
    update(
        journey,
        events=events,
        completed_steps=list(
            set(journey.data.get("completed_steps", []) + [action.data["step_key"]])
        ),
    )
    replan_journey(db, journey, employee)


@router.get("/legal-entities")
def legal_entities(db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *READ_HR, "MANAGER")
    values = entities(db, "organization")
    if not actor.scope.get("all"):
        allowed = set(actor.scope.get("legal_entity_ids", [])) | {
            site.data.get("legal_entity_id") for site in visible_sites(db, actor)
        }
        values = [e for e in values if e.id in allowed]
    return {"items": [pack(e) for e in values]}


@router.post("/legal-entities")
def legal_entity_create(payload: dict, db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *PUBLISH)
    if not actor.scope.get("all"):
        raise HTTPException(403, "Новое юрлицо создаёт администратор всего контура")
    if (
        not isinstance(payload.get("name"), str)
        or not 1 <= len(payload["name"].strip()) <= 255
    ):
        raise HTTPException(422, "Укажите название юрлица до 255 символов")
    e = save_entity(
        db,
        "organization",
        {"name": payload["name"].strip(), "external_id": payload.get("external_id")},
    )
    audit(db, actor, "organization.create", e.id)
    commit(db)
    return reply(e, 201)


@router.post("/conversations/{conversation_id}/input-captures")
def conversation_input_capture(
    conversation_id: str, payload: dict, db=Depends(get_db), actor=Depends(get_actor)
):
    from .chat import require_conversation

    conversation = require_conversation(db, actor, conversation_id, lock=True)
    capture = open_capture(db, actor, payload)
    if capture.data["employee_id"] != conversation.data["employee_id"]:
        db.rollback()
        raise HTTPException(404, "Цель ввода не принадлежит диалогу")
    commit(db)
    return reply(capture, 201)


def sync_employment_episode(db, employee):
    episode = (
        db.get(Entity, employee.data.get("episode_id"))
        if employee.data.get("episode_id")
        else None
    )
    if episode:
        update(
            episode,
            **{
                k: employee.data.get(k)
                for k in (
                    "start_date",
                    "probation_end_date",
                    "site_id",
                    "legal_entity_id",
                )
            },
            stage=employee.data.get("employment_stage"),
        )
        episode.status = employee.data.get("employment_stage", "PREBOARDING")
    return episode


@router.get("/employment-episodes/{episode_id}")
def employment_episode_detail(
    episode_id: str, db=Depends(get_db), actor=Depends(get_actor)
):
    if not can_employee_drilldown(actor):
        raise HTTPException(
            404, "Для персонального просмотра нужен отдельный drilldown grant"
        )
    episode = get_entity(db, episode_id, "employment_episode")
    employee = assert_employee(db, actor, episode.parent_id)
    if actor.role == "EMPLOYEE" and (
        episode.id != employee.data.get("episode_id")
        or employee.data.get("employment_stage") == "ENDED"
    ):
        raise HTTPException(404, "Период найма недоступен")
    return JSONResponse(episode_view(episode, actor), headers={"ETag": etag(episode)})


@router.post(
    "/employment-episodes/{episode_id}/stage",
    responses={
        202: {
            "description": "Будущая HR-стадия сохранена как SCHEDULED durable operation"
        }
    },
)
def employment_episode_stage(
    episode_id: str, payload: dict, db=Depends(get_db), actor=Depends(get_actor)
):
    require_role(actor, *HR)
    from .employment_stages import _locked_target, apply_stage, cancel_pending, schedule

    employee, episode = _locked_target(db, episode_id)
    assert_employee(db, actor, employee.id)
    if (
        payload.get("expected_episode_id", episode_id) != episode.id
        or employee.data.get("episode_id") != episode.id
    ):
        raise HTTPException(409, "Период найма изменился")
    if payload.get("expected_state_version", episode.version) != episode.version:
        raise HTTPException(409, "Состояние периода изменилось")
    stage = payload.get("stage")
    if stage not in ("PREBOARDING", "ACTIVE"):
        raise HTTPException(422, "Выберите PREBOARDING или ACTIVE")
    if not isinstance(payload.get("reason"), str) or len(payload["reason"].strip()) < 3:
        raise HTTPException(422, "Укажите причину подтверждения стадии")
    try:
        effective = aware(payload.get("effective_at", now()))
        if not isinstance(effective, datetime):
            raise ValueError("effective_at_required")
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(422, "Укажите корректный effective_at ISO8601")
    if employee.data.get("employment_stage") == "ENDED":
        raise HTTPException(409, "Для повторного найма создайте новый период")
    if effective > now():
        raw = payload.get("effective_at")
        if isinstance(raw, str) and datetime.fromisoformat(raw).tzinfo is None:
            raise HTTPException(
                422, "Для будущей стадии нужен часовой пояс effective_at"
            )
        operation = schedule(db, actor, employee, episode, payload, effective)
        commit(db)
        return JSONResponse(
            operation_view(operation),
            status_code=202,
            headers={"ETag": etag(operation)},
        )
    cancel_pending(
        db, employee, "SUPERSEDED_BY_IMMEDIATE_STAGE", actor, payload["reason"]
    )
    apply_stage(db, actor, employee, episode, stage, effective, payload["reason"])
    commit(db)
    return reply(episode)


@router.get("/employment-episodes/{episode_id}/stage-changes")
def employment_stage_changes(
    episode_id: str, db=Depends(get_db), actor=Depends(get_actor)
):
    require_role(actor, *HR)
    episode = get_entity(db, episode_id, "employment_episode")
    assert_employee(db, actor, episode.parent_id)
    items = [
        operation_view(op)
        for op in entities(db, "operation", episode.id)
        if op.data.get("type") == "EMPLOYMENT_STAGE_CHANGE"
    ]
    active = next(
        (op["id"] for op in reversed(items) if op["status"] == "SCHEDULED"), None
    )
    return {"items": items, "active_operation_id": active}


@router.get("/sites/{site_id}")
def site_detail(site_id: str, db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *READ_HR, "MANAGER")
    site = get_entity(db, site_id, "site")
    if site.id not in {row.id for row in visible_sites(db, actor)}:
        raise HTTPException(404, "Площадка недоступна")
    return reply(site)


@router.post("/sites/{site_id}/changes/preview")
def site_change_preview(
    site_id: str, payload: dict, db=Depends(get_db), actor=Depends(get_actor)
):
    require_role(actor, *PUBLISH)
    site = get_entity(db, site_id, "site")
    changes = {
        k: v
        for k, v in payload.items()
        if k in ("timezone", "calendar_id", "quiet_hours", "active")
    }
    if not payload.get("reason"):
        raise HTTPException(422, "Укажите причину изменения площадки")
    try:
        ZoneInfo(changes.get("timezone", site.data.get("timezone", "Europe/Moscow")))
    except (ZoneInfoNotFoundError, TypeError):
        raise HTTPException(422, "Неизвестный часовой пояс")
    if changes.get("calendar_id"):
        get_entity(db, changes["calendar_id"], "work_calendar")
    impact = []
    for employee in employee_scope(db, actor):
        if employee.data.get("site_id") != site.id:
            continue
        profile = {
            **employee.data,
            **{k: v for k, v in changes.items() if k in ("timezone", "calendar_id")},
        }
        for journey in entities(db, "journey", employee.id):
            if journey.status not in ("CANCELLED", "COMPLETED"):
                version = get_entity(db, journey.data["version_id"], "scenario_version")
                impact.append(
                    {
                        "employee_id": employee.id,
                        "journey_id": journey.id,
                        "timeline": simulate(
                            version.data["graph"], profile, calendar_for(db, employee)
                        )["timeline"],
                    }
                )
    preview = save_entity(
        db,
        "site_change_preview",
        {
            "site_id": site.id,
            "site_version": site.version,
            "changes": changes,
            "reason": payload["reason"],
            "impact": impact,
            "preview_hash": hashlib.sha256(
                json.dumps(
                    {"site": site.id, "version": site.version, "changes": changes},
                    sort_keys=True,
                ).encode()
            ).hexdigest(),
            "expires_at": (now() + timedelta(minutes=30)).isoformat(),
        },
        parent_id=site.id,
        owner_id=actor.id,
    )
    commit(db)
    return {**pack(preview), "preview_id": preview.id, "impact_count": len(impact)}


def filtered_employees(db, actor, filters=None):
    filters = filters or {}
    employees = employee_scope(db, actor)
    selected = []
    for employee in employees:
        data = employee.data
        if filters.get("site_ids") and data.get("site_id") not in filters["site_ids"]:
            continue
        if (
            filters.get("legal_entity_id")
            and data.get("legal_entity_id") != filters["legal_entity_id"]
        ):
            continue
        if (
            filters.get("legal_entity_ids")
            and data.get("legal_entity_id") not in filters["legal_entity_ids"]
        ):
            continue
        if (
            filters.get("cohort_from")
            and data.get("start_date", "") < filters["cohort_from"]
        ):
            continue
        if (
            filters.get("cohort_to")
            and data.get("start_date", "") > filters["cohort_to"]
        ):
            continue
        if (
            filters.get("statuses")
            and data.get("employment_stage") not in filters["statuses"]
        ):
            continue
        selected.append(employee)
    return selected


def adaptation_rows(db, actor, filters=None):
    if not can_employee_drilldown(actor):
        raise HTTPException(
            403, "Для персонального отчёта нужен отдельный drilldown grant"
        )
    result = []
    window = reporting_window(filters)
    for employee in filtered_employees(db, actor, filters):
        journeys = [
            j
            for j in entities(db, "journey", employee.id)
            if j.data.get("episode_id") == employee.data.get("episode_id")
        ]
        journey = journeys[-1] if journeys else None
        actions = entities(db, "scheduled_action", journey.id) if journey else []
        mandatory = [
            a
            for a in actions
            if a.data["step"].get("mandatory") and a.status != "NOT_APPLICABLE"
        ]
        version = db.get(Entity, journey.data.get("version_id")) if journey else None
        simple_progress = bool(version and version.data.get("graph", {}).get("editor_mode") == "SIMPLE")
        if simple_progress:
            graph = version.data["graph"]
            path = active_path(graph, journey.data.get("answers", {}), employee.data)
            progress_actions = [a for a in actions if a.data["step_key"] in path and a.data["step"]["kind"] in {"MESSAGE", "SURVEY"}]
            completed_count = sum(a.status == "COMPLETED" for a in progress_actions)
        else:
            progress_actions = mandatory
            completed_count = sum(a.status in ("ACCEPTED", "COMPLETED") for a in mandatory)
        cases = [
            c
            for c in entities(db, "case", employee.id)
            if c.data.get("episode_id", employee.data.get("episode_id"))
            == employee.data.get("episode_id")
        ]
        blockers = [c for c in cases if c.data.get("kind") == "BLOCKER"]
        open_cases = [
            c
            for c in cases
            if c.status
            not in (
                "RESOLVED",
                "CLOSED",
                "CONFIRMED",
                "CLOSED_WITHOUT_CONFIRMATION",
                "CANCELLED",
            )
        ]
        result.append(
            {
                "employee_id": employee.id,
                "name": employee.data.get("name"),
                "site_id": employee.data.get("site_id"),
                "stage": employee.data.get("employment_stage"),
                "journey_status": journey.status if journey else "NOT_ENROLLED",
                "completed_mandatory": completed_count,
                "total_mandatory": len(progress_actions),
                "progress_basis": "SIMPLE_SELECTED_CONTENT_STEPS" if simple_progress else "MANDATORY_ACTIONS",
                "last_step_key": journey.data.get("last_step_key") if journey else None,
                "waiting_step_key": journey.data.get("waiting_step_key") if journey else None,
                "progress": metric_context(
                    completed_count,
                    len(progress_actions),
                    None,
                    window,
                    missing_answers_method="NOT_APPLICABLE_TO_ACTION_PROGRESS",
                    cohort_definition="CURRENT_EPISODE_SELECTED_SIMPLE_CONTENT_STEPS_AS_OF" if simple_progress else "CURRENT_EPISODE_APPLICABLE_MANDATORY_ACTIONS_AS_OF",
                ),
                "open_case_count": len(open_cases),
                "needs_attention": bool(
                    open_cases or any(a.data.get("calendar_warning") for a in actions)
                ),
                "open_blocker_count": sum(
                    c.status in ("OPEN", "IN_PROGRESS", "REOPENED") for c in blockers
                ),
                "pending_confirmation_blocker_count": sum(
                    c.status == "RESOLVED_PENDING_CONFIRMATION" for c in blockers
                ),
                "confirmed_blocker_count": sum(
                    c.status == "CONFIRMED" for c in blockers
                ),
                "reopened_blocker_count": sum(c.status == "REOPENED" for c in blockers),
                "closed_without_confirmation_blocker_count": sum(
                    c.status == "CLOSED_WITHOUT_CONFIRMATION" for c in blockers
                ),
            }
        )
    return result


def in_report_period(value, filters=None):
    window = reporting_window(filters)
    if not value:
        return False
    try:
        observed = aware(value)
    except (ValueError, TypeError):
        return False
    lower = aware(window["period_from"]) if window["period_from"] else None
    upper = aware(window["period_to"])
    return (lower is None or observed >= lower) and observed < upper


def risk_topic_rows(db, actor, filters=None):
    filters = {**(filters or {})}
    window = reporting_window(filters)
    filters["_reporting_window"] = window
    ids = {e.id for e in filtered_employees(db, actor, filters)}
    groups = {}
    pending = 0
    reviewed_employees = set()
    for analysis in entities(db, "feedback_analysis"):
        eid = analysis.data.get("employee_id")
        feedback = (
            db.get(Entity, analysis.data.get("feedback_id"))
            if analysis.data.get("feedback_id")
            else None
        )
        occurred_at = feedback.created_at if feedback else analysis.created_at
        if eid not in ids or not in_report_period(occurred_at, filters):
            continue
        if analysis.status != "REVIEWED":
            pending += 1
            continue
        reviewed_employees.add(eid)
        for topic in analysis.data.get("topics", []):
            if isinstance(topic, str):
                groups.setdefault(topic, set()).add(eid)
    rows = [
        {
            "topic": topic if len(employees) >= 5 else None,
            "employee_count": len(employees) if len(employees) >= 5 else None,
            "suppressed": len(employees) < 5,
            "method": "HUMAN_REVIEWED_FEEDBACK",
            **metric_context(
                len(employees) if len(employees) >= 5 else None,
                len(reviewed_employees) if len(employees) >= 5 else None,
                None,
                window,
                missing_answers_method="NOT_APPLICABLE_TO_REVIEWED_FEEDBACK_TOPICS",
                cohort_definition="UNIQUE_EMPLOYEES_WITH_HUMAN_REVIEWED_FEEDBACK_IN_PERIOD",
            ),
        }
        for topic, employees in sorted(groups.items())
    ]
    return rows, pending


@router.get("/reports/adaptation")
def report_adaptation(
    site_id: str | None = None,
    cohort_from: str | None = None,
    cohort_to: str | None = None,
    db=Depends(get_db),
    actor=Depends(get_actor),
    period_from: str | None = None,
    period_to: str | None = None,
    timezone: str = "Europe/Moscow",
):
    require_role(actor, *READ_HR, "MANAGER")
    filters = {
        "site_ids": [site_id] if site_id else [],
        "cohort_from": cohort_from,
        "cohort_to": cohort_to,
        "period_from": period_from,
        "period_to": period_to,
        "timezone": timezone,
    }
    window = reporting_window(filters)
    rows = adaptation_rows(db, actor, {**filters, "_reporting_window": window})
    return {
        "items": rows,
        "next_cursor": None,
        "has_more": False,
        "cutoff_at": now().isoformat(),
        "suppressed_count": 0,
        "filters": filters,
        "reporting_window": window,
        "metric_definition": "CURRENT_EPISODE_MANDATORY_ACTIONS_AS_OF; NOT_A_HISTORICAL_STATE_RECONSTRUCTION",
    }


@router.get("/reports/risk-topics")
def report_risk_topics(
    site_id: str | None = None,
    period_from: str | None = None,
    period_to: str | None = None,
    db=Depends(get_db),
    actor=Depends(get_actor),
    timezone: str = "Europe/Moscow",
):
    require_role(actor, *READ_HR)
    filters = {
        "site_ids": [site_id] if site_id else [],
        "period_from": period_from,
        "period_to": period_to,
        "timezone": timezone,
    }
    window = reporting_window(filters)
    rows, pending = risk_topic_rows(db, actor, {**filters, "_reporting_window": window})
    return {
        "items": rows,
        "pending_analysis_count": pending,
        "suppressed_count": sum(r["suppressed"] for r in rows),
        "minimum_group_size": 5,
        "cutoff_at": now().isoformat(),
        "no_automatic_employment_decisions": True,
        "reporting_window": window,
    }


def operation_authorize(db, actor, operation):
    if operation.data.get("type") == "EMPLOYMENT_STAGE_CHANGE":
        require_role(actor, *HR)
        assert_employee(db, actor, operation.data["employee_id"])
        return
    if operation.owner_id == actor.id:
        operation_type = operation.data.get("type", "")
        if operation_type == "REPORT_EXPORT":
            require_role(actor, *READ_HR, "MANAGER")
        elif operation_type == "KNOWLEDGE_PURGE":
            require_role(actor, *PUBLISH)
        else:
            require_role(actor, *HR)
        operation_scope = operation.data.get("operation_scope")
        if operation_scope:
            projected = {
                k: operation_scope[k]
                for k in ("all", "site_ids", "legal_entity_ids")
                if k in operation_scope
            }
            if operation_scope.get("legal_entity_id"):
                projected["legal_entity_ids"] = [operation_scope["legal_entity_id"]]
            if not scope_subset(projected, actor.scope or {}):
                raise HTTPException(404, "Область операции больше недоступна")
        if operation.data.get("export_scope") and not scope_subset(
            operation.data["export_scope"], actor.scope or {}
        ):
            raise HTTPException(404, "Область операции больше недоступна")
        if operation.data.get("type") == "REPORT_EXPORT":
            require_role(actor, *READ_HR, "MANAGER")
        return
    require_role(actor, *HR)
    revision_id = operation.data.get("revision_id") or operation.data.get(
        "result", {}
    ).get("revision_id")
    if not revision_id and operation.data.get("qa_artifact_id"):
        qa = db.get(Entity, operation.data["qa_artifact_id"])
        revision_id = qa.data.get("revision_id") if qa else None
    if revision_id:
        from .knowledge import _revision

        _revision(db, actor, revision_id)
        return
    raise HTTPException(404, "Операция недоступна")


def operation_view(operation):
    data = pack(operation)
    data.pop("artifact_path", None)
    data.pop("export_request", None)
    data.pop("actor_pin", None)
    data.pop(
        "employee_ids", None
    )  # Internal authorization pins, never an operator roster.

    data["operation_id"] = operation.id
    if operation.data.get("type") == "REPORT_EXPORT":
        request = operation.data.get("export_request", {})
        data["dataset"] = request.get("dataset")
        data["format"] = request.get("format")
    data.setdefault("stage", operation.status)
    data["links"] = {
        "self": "/api/v1/operations/" + operation.id,
        "events": "/api/v1/operations/" + operation.id + "/events",
    }
    if (
        operation.data.get("type") == "REPORT_EXPORT"
        and operation.status == "SUCCEEDED"
    ):
        data["links"]["content"] = (
            "/api/v1/reports/exports/" + operation.id + "/content"
        )
    return data


@router.get("/operations/{operation_id}")
def operation_detail(operation_id: str, db=Depends(get_db), actor=Depends(get_actor)):
    operation = get_entity(db, operation_id, "operation")
    operation_authorize(db, actor, operation)
    return JSONResponse(operation_view(operation), headers={"ETag": etag(operation)})


@router.get("/operations/{operation_id}/events")
def operation_events(operation_id: str, db=Depends(get_db), actor=Depends(get_actor)):
    operation = get_entity(db, operation_id, "operation")
    operation_authorize(db, actor, operation)
    events = [pack(e) for e in entities(db, "operation_event", operation.id)]
    if not events:
        events = [
            {
                "operation_id": operation.id,
                "stage": operation.data.get("stage", operation.status),
                "status": operation.status,
                "occurred_at": operation.updated_at.isoformat(),
            }
        ]
    return {"items": events, "next_cursor": None, "has_more": False}


@router.post("/operations/{operation_id}/cancel")
def operation_cancel(
    operation_id: str,
    payload: dict,
    request: Request,
    db=Depends(get_db),
    actor=Depends(get_actor),
):
    operation = get_entity(db, operation_id, "operation")
    operation_authorize(db, actor, operation)
    if operation.data.get("type") == "EMPLOYMENT_STAGE_CHANGE":
        from .employment_stages import cancel

        operation = cancel(db, actor, operation, payload, request)
        commit(db)
        return JSONResponse(
            operation_view(operation), headers={"ETag": etag(operation)}
        )
    # Match worker checkpoints: Job -> operation. Lock all related job states,
    # then inspect fresh status; a job claimed while cancellation waited must
    # stay PROCESSING and observe cancel_requested at its next checkpoint.
    jobs = list(
        db.scalars(
            select(Job)
            .where(Job.payload["operation_id"].as_string() == operation.id)
            .order_by(Job.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    operation = get_entity(db, operation_id, "operation", lock=True)
    operation_authorize(db, actor, operation)
    check_etag(request, operation)
    if not payload.get("reason"):
        raise HTTPException(422, "Укажите причину отмены")
    if operation.status not in ("PENDING", "SCHEDULED", "RUNNING"):
        raise HTTPException(409, "Эту операцию уже нельзя отменить")
    if operation.data.get("type") not in {
        "KNOWLEDGE_INGEST",
        "KNOWLEDGE_QA",
        "KNOWLEDGE_CANDIDATE_BUILD",
        "REPORT_EXPORT",
    }:
        raise HTTPException(
            409,
            {
                "code": "not_cancellable",
                "message": "Для этой операции нет безопасного протокола отмены; дождитесь результата и используйте её отдельный workflow",
            },
        )
    update(
        operation,
        cancel_requested=True,
        cancelled_by=actor.id,
        cancel_reason=payload["reason"],
    )
    operation.status = "CANCELLED"
    for job in jobs:
        if job.status in ("PENDING", "RETRY"):
            job.status = "CANCELLED"
    if operation.data.get("type") == "KNOWLEDGE_INGEST" and not any(
        job.status == "PROCESSING" for job in jobs
    ):
        revision_id = operation.data.get("revision_id") or operation.data.get(
            "result", {}
        ).get("revision_id")
        revision = db.get(Entity, revision_id) if revision_id else None
        if (
            revision
            and revision.kind == "document_revision"
            and revision.status == "UPLOADED"
        ):
            # No worker will run a cancelled queued job. Complete the visible
            # cancel state while retaining the original upload for safe retry.
            get_entity(db, revision.parent_id, "document", lock=True)
            revision = get_entity(db, revision.id, "document_revision", lock=True)
            if revision.status == "UPLOADED":
                revision.status = "CANCELLED"
    event(
        db,
        "operation_event",
        operation.id,
        "CANCELLED",
        actor.id,
        reason=payload["reason"],
    )
    audit(db, actor, "operation.cancel", operation.id, reason=payload["reason"])
    commit(db)
    return JSONResponse(operation_view(operation), headers={"ETag": etag(operation)})


EXPORT_COLUMNS = {
    "ADAPTATION": {
        "employee_id",
        "name",
        "site_id",
        "stage",
        "journey_status",
        "completed_mandatory",
        "total_mandatory",
        "open_case_count",
        "needs_attention",
        "open_blocker_count",
        "pending_confirmation_blocker_count",
        "confirmed_blocker_count",
        "reopened_blocker_count",
        "closed_without_confirmation_blocker_count",
    },
    "SURVEYS": {
        "employee_id",
        "survey_run_id",
        "survey_key",
        "status",
        "question_id",
        "value",
        "comment",
        "answer_outcome",
        "comment_outcome",
        "answered_at",
    },
    "RISK_TOPICS": {
        "topic",
        "employee_count",
        "suppressed",
        "method",
        "period_from",
        "period_to",
    },
    "CASES": {
        "case_id",
        "employee_id",
        "kind",
        "reason",
        "status",
        "description",
        "due_at",
        "created_at",
        "resolved_at",
        "result_summary",
    },
    "AUDIT": {"id", "actor_id", "action", "entity_id", "created_at"},
}


def validate_export_request(payload, actor):
    dataset = payload.get("dataset", "ADAPTATION")
    fmt = payload.get("format", "CSV").upper()
    if dataset not in EXPORT_COLUMNS or fmt not in ("CSV", "XLSX", "PDF"):
        raise HTTPException(422, "Выберите допустимый набор и формат CSV/XLSX/PDF")
    require_role(
        actor,
        *(
            HR
            if dataset in ("SURVEYS", "CASES")
            else ("HR_ADMIN",) if dataset == "AUDIT" else READ_HR + ("MANAGER",)
        ),
    )
    if dataset == "AUDIT" and actor.scope.get("all") is not True:
        # Technical audit rows span the entire installation. They cannot be
        # safely projected by an employee's current site after a transfer.
        raise HTTPException(
            403, "Аудит всего контура доступен HR_ADMIN с полной областью"
        )
    if dataset == "ADAPTATION" and not can_employee_drilldown(actor):
        raise HTTPException(
            403, "Для персональной выгрузки нужен отдельный drilldown grant"
        )
    if actor.role == "MANAGER" and dataset != "ADAPTATION":
        raise HTTPException(
            403, "Руководителю доступна выгрузка статусов своей команды"
        )
    filters = payload.get("filters", {})
    columns = payload.get("columns") or sorted(EXPORT_COLUMNS[dataset])
    if (
        not isinstance(filters, dict)
        or set(filters)
        - {
            "site_ids",
            "legal_entity_id",
            "legal_entity_ids",
            "cohort_from",
            "cohort_to",
            "period_from",
            "period_to",
            "timezone",
            "statuses",
        }
        or not isinstance(columns, list)
        or not 1 <= len(columns) <= 100
        or not set(columns).issubset(EXPORT_COLUMNS[dataset])
    ):
        raise HTTPException(422, "Проверьте фильтры и разрешённые столбцы")
    if fmt == "PDF" and len(columns) > 20:
        raise HTTPException(422, "Для PDF выберите не более 20 столбцов")
    for key in ("cohort_from", "cohort_to"):
        if filters.get(key):
            try:
                date.fromisoformat(filters[key])
            except (ValueError, TypeError):
                raise HTTPException(422, "Некорректный период отчёта")
    reporting_window(filters)
    if not payload.get("reason"):
        raise HTTPException(422, "Укажите назначение выгрузки")
    return {
        "dataset": dataset,
        "format": fmt,
        "filters": filters,
        "columns": columns,
        "reason": str(payload["reason"])[:2000],
    }


@router.get("/reports/exports")
def list_report_exports(db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *READ_HR, "MANAGER")
    visible = []
    for operation in reversed(entities(db, "operation")):
        if (
            operation.owner_id != actor.id
            or operation.data.get("type") != "REPORT_EXPORT"
        ):
            continue
        try:
            operation_authorize(db, actor, operation)
            validate_export_request(operation.data.get("export_request", {}), actor)
            for employee_id in operation.data.get("employee_ids", []):
                assert_employee(db, actor, employee_id)
        except HTTPException:
            continue
        visible.append(operation_view(operation))
        if len(visible) >= 100:
            break
    return {"items": visible, "next_cursor": None, "has_more": False}


@router.post("/reports/exports")
def create_report_export(payload: dict, db=Depends(get_db), actor=Depends(get_actor)):
    data = validate_export_request(payload, actor)
    operation = save_entity(
        db,
        "operation",
        {
            "type": "REPORT_EXPORT",
            "stage": "PENDING",
            "export_request": data,
            "export_scope": actor.scope,
            "employee_ids": [
                e.id for e in filtered_employees(db, actor, data["filters"])
            ],
            "expires_at": (now() + timedelta(hours=24)).isoformat(),
        },
        status="PENDING",
        owner_id=actor.id,
    )
    enqueue(
        db,
        "DOMAIN_REPORT_EXPORT",
        {"operation_id": operation.id},
        f"export:{operation.id}",
    )
    audit(
        db,
        actor,
        "report.export.request",
        operation.id,
        {"dataset": data["dataset"], "format": data["format"]},
        reason=data["reason"],
    )
    commit(db)
    return JSONResponse(
        operation_view(operation), 202, headers={"ETag": etag(operation)}
    )


def _export_rows(db, actor, request):
    dataset = request["dataset"]
    employees = filtered_employees(db, actor, request["filters"])
    ids = {e.id for e in employees}
    if dataset == "ADAPTATION":
        return adaptation_rows(db, actor, request["filters"])
    if dataset == "RISK_TOPICS":
        return risk_topic_rows(db, actor, request["filters"])[0]
    if dataset == "SURVEYS":
        rows = []
        for survey in entities(db, "survey_run"):
            if survey.data.get("employee_id") not in ids:
                continue
            for question_id, answer in survey.data.get("answers", {}).items():
                if not in_report_period(answer.get("answered_at"), request["filters"]):
                    continue
                rows.append(
                    {
                        "employee_id": survey.data["employee_id"],
                        "survey_run_id": survey.id,
                        "survey_key": survey.data["survey_key"],
                        "status": survey.status,
                        "question_id": question_id,
                        "value": answer.get("value"),
                        "comment": answer.get("comment"),
                        "answer_outcome": answer.get("outcome"),
                        "comment_outcome": answer.get("comment_outcome"),
                        "answered_at": answer.get("answered_at"),
                    }
                )
        return rows
    if dataset == "CASES":
        return [
            {
                **{k: c.data.get(k) for k in EXPORT_COLUMNS["CASES"]},
                "case_id": c.id,
                "status": c.status,
                "created_at": c.created_at.isoformat(),
            }
            for c in entities(db, "case")
            if c.data.get("employee_id") in ids
            and in_report_period(c.created_at, request["filters"])
        ]
    from .models import Audit

    return [
        {
            k: getattr(a, k).isoformat() if k == "created_at" else getattr(a, k)
            for k in EXPORT_COLUMNS["AUDIT"]
        }
        for a in db.scalars(select(Audit).order_by(Audit.created_at.desc()).limit(5000))
        if in_report_period(a.created_at, request["filters"])
    ]


def export_cell(value):
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    if isinstance(value, str) and value.lstrip().startswith(
        ("=", "+", "-", "@", "\t", "\r")
    ):
        return "'" + value
    return value


def export_bytes(rows, columns, fmt):
    if len(rows) > 5000:
        raise ValueError("Сузьте фильтры: выгрузка ограничена 5000 строками")
    if fmt == "CSV":
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow(columns)
        for row in rows:
            writer.writerow([export_cell(row.get(c)) for c in columns])
        return (
            ("\ufeff" + stream.getvalue()).encode("utf-8"),
            "text/csv; charset=utf-8",
            "csv",
        )
    if fmt == "XLSX":
        from openpyxl import Workbook

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Корсик"
        sheet.append(columns)
        for row in rows:
            sheet.append([export_cell(row.get(c)) for c in columns])
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for column in sheet.columns:
            sheet.column_dimensions[column[0].column_letter].width = min(
                60,
                max(14, max(len(str(cell.value or "")) for cell in column[:100]) + 2),
            )
        stream = io.BytesIO()
        workbook.save(stream)
        return (
            stream.getvalue(),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "xlsx",
        )
    from pathlib import Path
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.lib.pagesizes import A3, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import (
        SimpleDocTemplate,
        Paragraph,
        LongTable,
        TableStyle,
        Spacer,
    )
    from reportlab.lib import colors
    from xml.sax.saxutils import escape

    font = next(
        (
            p
            for p in [
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
                "/Library/Fonts/Arial Unicode.ttf",
            ]
            if Path(p).is_file()
        ),
        None,
    )
    if not font:
        raise ValueError(
            "Не установлен Unicode-шрифт для PDF; установите fonts-dejavu-core"
        )
    if "KorsikUnicode" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("KorsikUnicode", font))
    style = ParagraphStyle(
        "cell", fontName="KorsikUnicode", fontSize=7, leading=9, wordWrap="CJK"
    )
    stream = io.BytesIO()
    document = SimpleDocTemplate(
        stream,
        pagesize=landscape(A3),
        leftMargin=24,
        rightMargin=24,
        topMargin=24,
        bottomMargin=24,
    )
    matrix = [[Paragraph(escape(c), style) for c in columns]]
    for row in rows:
        matrix.append(
            [
                Paragraph(
                    escape(str(row.get(c) if row.get(c) is not None else ""))[:6000],
                    style,
                )
                for c in columns
            ]
        )
    table = LongTable(
        matrix,
        colWidths=[(landscape(A3)[0] - 48) / len(columns)] * len(columns),
        repeatRows=1,
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dfeee6")),
                ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#b8c9c0")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    document.build(
        [
            Paragraph(
                "Корсик — отчёт по адаптации",
                ParagraphStyle(
                    "title", fontName="KorsikUnicode", fontSize=15, leading=20
                ),
            ),
            Spacer(1, 12),
            table,
        ]
    )
    return stream.getvalue(), "application/pdf", "pdf"


def process_report_export(db, job):
    from pathlib import Path
    from .settings import get_settings

    operation = get_entity(db, job.payload["operation_id"], "operation", lock=True)
    if operation.status in ("SUCCEEDED", "CANCELLED"):
        return
    user = db.get(User, operation.owner_id)
    if not user or not user.enabled:
        operation.status = "FAILED"
        update(
            operation,
            error={
                "code": "ACCESS_REVOKED",
                "message": "Владелец выгрузки больше не имеет доступа",
            },
        )
        return
    actor = Actor(user.id, user.name, user.role, user.scope, user.employee_id)
    try:
        operation_authorize(db, actor, operation)
        request = validate_export_request(operation.data["export_request"], actor)
        window = reporting_window(request["filters"])
        request["filters"] = {**request["filters"], "_reporting_window": window}
        rows = _export_rows(db, actor, request)
        # Pin the cohort actually read at materialization, including employees
        # added since the request. Download must reauthorize every contributor.
        generated_employee_ids = [
            e.id for e in filtered_employees(db, actor, request["filters"])
        ]
        context = {
            "report_" + key: window[key]
            for key in (
                "period_from",
                "period_to",
                "timezone",
                "as_of",
                "period_semantics",
            )
        }
        rows = [{**row, **context} for row in rows]
        raw, mime, extension = export_bytes(
            rows, request["columns"] + list(context), request["format"]
        )
        root = Path(get_settings().blob_root) / "exports"
        root.mkdir(parents=True, exist_ok=True)
        path = root / (operation.id + "." + extension)
        path.write_bytes(raw)
        path.chmod(0o600)
        update(
            operation,
            stage="COMPLETE",
            artifact_path=str(path),
            content_type=mime,
            filename="korsik-report." + extension,
            bytes=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
            row_count=len(rows),
            reporting_window=window,
            employee_ids=generated_employee_ids,
            completed_at=now().isoformat(),
        )
        operation.status = "SUCCEEDED"
        event(
            db,
            "operation_event",
            operation.id,
            "SUCCEEDED",
            actor.id,
            row_count=len(rows),
        )
        audit(
            db,
            actor,
            "report.export.completed",
            operation.id,
            {"dataset": request["dataset"], "rows": len(rows)},
        )
    except HTTPException:
        operation.status = "FAILED"
        update(
            operation,
            error={
                "code": "ACCESS_REVOKED",
                "message": "Область или полномочия выгрузки изменились",
            },
        )
    except Exception as error:
        operation.status = "FAILED"
        update(
            operation,
            error={
                "code": "EXPORT_FAILED",
                "message": (
                    str(error)[:500]
                    if isinstance(error, ValueError)
                    else "Не удалось подготовить файл"
                ),
            },
            stage="FAILED",
        )


@router.get("/reports/exports/{operation_id}/content")
def report_export_content(
    operation_id: str, db=Depends(get_db), actor=Depends(get_actor)
):
    from pathlib import Path
    from .settings import get_settings
    from fastapi.responses import FileResponse

    operation = get_entity(db, operation_id, "operation")
    operation_authorize(db, actor, operation)
    if operation.data.get("type") != "REPORT_EXPORT" or operation.status != "SUCCEEDED":
        raise HTTPException(409, "Файл ещё не готов")
    # A previously prepared file is not a durable grant to its former owner's
    # role. In particular HEAD/MANAGER cannot inherit an HR survey export.
    validate_export_request(operation.data.get("export_request", {}), actor)
    if aware(operation.data["expires_at"]) <= now():
        raise HTTPException(410, "Срок хранения выгрузки истёк; создайте новую")
    for employee_id in operation.data.get("employee_ids", []):
        assert_employee(db, actor, employee_id)
    path = Path(operation.data["artifact_path"])
    root = (Path(get_settings().blob_root) / "exports").resolve()
    if not path.is_file() or not path.resolve().is_relative_to(root):
        raise HTTPException(404, "Файл недоступен")
    audit(db, actor, "report.export.download", operation.id)
    commit(db)
    return FileResponse(
        path,
        filename=operation.data["filename"],
        media_type=operation.data["content_type"],
    )
