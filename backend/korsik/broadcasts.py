"""Scoped, scheduled MAX mailings. The existing outbox owns actual delivery."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
import re
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text as sql_text

from .auth import get_actor, user_actor
from .common import (
    actor_can_employee,
    audit,
    commit,
    employee_is_enabled,
    fingerprint,
    queue_message,
    require_role,
    save_entity,
)
from .db import aware, get_db, now
from .models import Entity, Outbox, User
from .organization_scope import org_unit_ancestors

router = APIRouter()
ROLES = ("HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT")
MAX_RECIPIENTS = 5000
BATCH = 100
MENTION = re.compile(r"\{\{mention:([a-zA-Z0-9_-]{1,120})\}\}")
FINAL_FAILURES = {"FAILED", "REJECTED", "SUPPRESSED", "CANCELLED", "UNKNOWN_DELIVERY"}
REASONS = {
    "EMPLOYEE_UNAVAILABLE": "Доступ сотрудника выключен или период найма завершён",
    "NO_BINDING": "MAX не подключён",
    "AUTHOR_SCOPE_REVOKED": "Полномочия автора или область доступа изменились",
    "STALE_EPISODE": "Период найма сотрудника изменился",
    "BINDING_CHANGED": "Подключение MAX изменилось; старую рассылку не переадресуем",
    "ORG_MEMBERSHIP_CHANGED": "Сотрудник больше не входит в выбранные подразделения",
    "MENTION_UNAVAILABLE": "Упомянутый сотрудник больше недоступен автору",
    "UNKNOWN_DELIVERY": "MAX не подтвердил результат; автоматический повтор исключён",
}


class BroadcastInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_request_id: uuid.UUID
    text: str = Field(min_length=1, max_length=3900)
    recipient_ids: list[str] = Field(default_factory=list, max_length=MAX_RECIPIENTS)
    org_unit_ids: list[str] = Field(default_factory=list, max_length=1000)
    scheduled_date: date
    scheduled_time: str = Field(pattern=r"^\d{2}:\d{2}$")
    timezone: str = Field(min_length=1, max_length=100)


def _actor(db, actor):
    require_role(actor, *ROLES)
    user = db.scalar(
        select(User)
        .where(User.id == actor.id)
        .execution_options(populate_existing=True)
    )
    if not user or not user.enabled or user.role not in ROLES:
        raise HTTPException(403, "Полномочия автора изменились")
    return user_actor(user)


def _binding(db, employee):
    if (
        not employee_is_enabled(employee)
        or employee.data.get("employment_stage") == "ENDED"
    ):
        return None, "EMPLOYEE_UNAVAILABLE"
    binding = db.scalar(
        select(Entity)
        .where(
            Entity.id == employee.data.get("binding_id", ""),
            Entity.kind == "max_binding",
        )
        .execution_options(populate_existing=True)
    )
    if (
        not binding
        or binding.kind != "max_binding"
        or binding.status != "ACTIVE"
        or binding.parent_id != employee.id
        or employee.data.get("max_binding_status") != "VERIFIED"
        or binding.data.get("episode_id") != employee.data.get("episode_id")
        or not str(binding.data.get("max_user_id", "")).isdigit()
    ):
        return None, "NO_BINDING"
    return binding, None


def _employees(db, actor):
    return {
        row.id: row
        for row in db.scalars(
            select(Entity)
            .where(Entity.kind == "employee", Entity.status != "DELETED")
            .execution_options(populate_existing=True)
        )
        if actor_can_employee(db, actor, row.id)
    }


def _units(db, actor, employees):
    from .organization import _unit_read

    return {
        row.id: row
        for row in db.scalars(
            select(Entity)
            .where(Entity.kind == "org_unit", Entity.status == "ACTIVE")
            .execution_options(populate_existing=True)
        )
        if _unit_read(actor, row, employees)
    }


def _membership(employee, units):
    # Server-owned directory path, including OFF people for transparent skips.
    # Authorization still checks actor grants and _binding before any delivery.
    result, key = set(), employee.data.get("org_unit_id")
    while key:
        if key in result or len(result) >= 512:
            return set()
        unit = units.get(key)
        if (
            not unit
            or unit.status != "ACTIVE"
            or unit.data.get("legal_entity_id") != employee.data.get("legal_entity_id")
            or (
                unit.data.get("site_id")
                and unit.data["site_id"] != employee.data.get("site_id")
            )
        ):
            break
        result.add(key)
        key = unit.parent_id
    return result


def _projection(db, employee, units):
    binding, reason = _binding(db, employee)
    return {
        "id": employee.id,
        "name": employee.data.get("name", "Сотрудник"),
        "position": employee.data.get("position", ""),
        "department": (
            units.get(employee.data.get("org_unit_id")).data.get("title")
            if units.get(employee.data.get("org_unit_id"))
            else None
        )
        or employee.data.get("department")
        or "Без подразделения",
        "preferred_name": employee.data.get("preferred_name")
        or employee.data.get("name", "Сотрудник"),
        "mention_available": employee_is_enabled(employee)
        and employee.data.get("employment_stage") != "ENDED",
        "org_unit_id": employee.data.get("org_unit_id"),
        "org_unit_ancestor_ids": sorted(_membership(employee, units)),
        "available": reason is None,
        "unavailable_reason": REASONS.get(reason),
        "mention_name": employee.data.get("preferred_name")
        or employee.data.get("name", "Сотрудник"),
    }


@router.get("/broadcasts/recipients")
def recipients(actor=Depends(get_actor), db=Depends(get_db)):
    actor = _actor(db, actor)
    employees = _employees(db, actor)
    units = _units(db, actor, employees)
    return {
        "employees": [_projection(db, row, units) for row in employees.values()],
        "units": [
            {
                "id": row.id,
                "title": row.data.get("title", ""),
                "type": row.data.get("type"),
                "parent_id": row.parent_id if row.parent_id in units else None,
            }
            for row in units.values()
        ],
        "max_recipients": MAX_RECIPIENTS,
    }


def _scheduled(body):
    try:
        zone = ZoneInfo(body.timezone)
        local = datetime.combine(
            body.scheduled_date, time.fromisoformat(body.scheduled_time)
        )
    except (ValueError, ZoneInfoNotFoundError):
        raise HTTPException(422, "Укажите действительные дату, время и часовой пояс")
    first, second = local.replace(tzinfo=zone, fold=0), local.replace(
        tzinfo=zone, fold=1
    )
    # Reject gaps and folds instead of silently moving a civil-time appointment.
    if (
        first.utcoffset() != second.utcoffset()
        or first.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != local
    ):
        raise HTTPException(422, "Выбранное местное время отсутствует или неоднозначно")
    scheduled = first.astimezone(timezone.utc)
    if scheduled <= now():
        raise HTTPException(422, "Время рассылки должно быть в будущем")
    return scheduled


def _render(db, actor, value, employees=None):
    # Legacy markers are accepted only to resolve ordinary, scoped address text.
    # New clients submit the inserted name directly, with no MAX identity semantics.
    employees = employees if employees is not None else _employees(db, actor)
    mentions = {}
    for employee_id in set(MENTION.findall(value)):
        employee = employees.get(employee_id)
        if (
            not employee_is_enabled(employee)
            or employee.data.get("employment_stage") == "ENDED"
        ):
            raise HTTPException(422, "Для обращения выберите доступного сотрудника")
        mentions[employee_id] = {
            "employee_id": employee_id,
            "episode_id": employee.data.get("episode_id"),
            "name": employee.data.get("preferred_name")
            or employee.data.get("name", "Сотрудник"),
        }
    if "{{mention:" in MENTION.sub("", value):
        raise HTTPException(422, "Некорректное обращение к сотруднику")
    readable = MENTION.sub(lambda m: mentions[m.group(1)]["name"], value)
    if not readable.strip() or len(readable) > 3900:
        raise HTTPException(422, "Текст слишком длинный: сократите сообщение")
    return readable, readable, list(mentions.values())


def _rows(db, mailing_id):
    return list(
        db.scalars(
            select(Entity)
            .where(Entity.kind == "broadcast_recipient", Entity.parent_id == mailing_id)
            .order_by(Entity.created_at, Entity.id)
        )
    )


def _view(db, mailing, actor, detail=False):
    rows = _rows(db, mailing.id)
    visible = _employees(db, actor)
    historical_ids = {row.data["employee_id"] for row in rows} | {
        m["employee_id"] for m in mailing.data.get("mentions", [])
    }
    missing = historical_ids - visible.keys()
    if missing:
        for person in db.scalars(
            select(Entity)
            .where(
                Entity.id.in_(missing),
                Entity.kind == "employee",
                Entity.status == "DELETED",
            )
            .execution_options(populate_existing=True)
        ):
            if actor_can_employee(db, actor, person.id, include_deleted=True):
                visible[person.id] = person
    # Free-form text may itself mention recipients. Do not disclose it after scope loss.
    if any(row.data["employee_id"] not in visible for row in rows) or any(
        m["employee_id"] not in visible for m in mailing.data.get("mentions", [])
    ):
        raise HTTPException(404, "Рассылка недоступна в текущей области доступа")
    outs = {
        row.id: row
        for row in db.scalars(
            select(Outbox).where(
                Outbox.id.in_(
                    [r.data["outbox_id"] for r in rows if r.data.get("outbox_id")]
                )
            )
        )
    }
    delivered = []
    for row in rows:
        out = outs.get(row.data.get("outbox_id"))
        state = out.status if out else row.status
        delivered.append(
            {
                "employee_id": row.data["employee_id"],
                "name": (
                    "Сотрудник удалён"
                    if visible[row.data["employee_id"]].status == "DELETED"
                    else visible[row.data["employee_id"]].data.get("name", "Сотрудник")
                ),
                "status": state,
                "error": REASONS.get(
                    (
                        (out.data or {}).get("suppression_reason")
                        or (state if state == "UNKNOWN_DELIVERY" else None)
                    )
                    if out
                    else row.data.get("reason")
                )
                or (
                    "Не удалось доставить сообщение"
                    if state in FINAL_FAILURES
                    else None
                ),
            }
        )
    accepted = sum(row["status"] == "ACCEPTED" for row in delivered)
    skipped = sum(row["status"] == "SKIPPED" for row in delivered)
    failed = sum(
        row["status"] in FINAL_FAILURES and row["status"] != "UNKNOWN_DELIVERY"
        for row in delivered
    )
    unknown = sum(row["status"] == "UNKNOWN_DELIVERY" for row in delivered)
    eligible = len(rows) - skipped
    completed = eligible > 0 and accepted == eligible
    return {
        "id": mailing.id,
        "text": mailing.data["display_text"],
        "scheduled_date": mailing.data["scheduled_date"],
        "scheduled_time": mailing.data["scheduled_time"],
        "timezone": mailing.data["timezone"],
        "scheduled_at": mailing.data["scheduled_at"],
        "status": "COMPLETED" if completed else "WAITING",
        "status_label": "Исполнено" if completed else "В ожидании",
        "sending": mailing.status == "SENDING" and not completed,
        "counts": {
            "total": len(rows),
            "eligible": eligible,
            "accepted": accepted,
            "skipped": skipped,
            "failed": failed,
            "unknown": unknown,
            "pending": eligible - accepted - failed,
        },
        "created_at": mailing.created_at.isoformat(),
        **({"recipients": delivered} if detail else {}),
    }


@router.get("/broadcasts")
def list_broadcasts(actor=Depends(get_actor), db=Depends(get_db)):
    actor = _actor(db, actor)
    result = []
    for row in db.scalars(
        select(Entity)
        .where(Entity.kind == "broadcast", Entity.owner_id == actor.id)
        .order_by(Entity.created_at.desc())
        .limit(100)
    ):
        try:
            result.append(_view(db, row, actor))
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
    return {"items": result}


@router.get("/broadcasts/{broadcast_id}")
def broadcast_detail(broadcast_id: str, actor=Depends(get_actor), db=Depends(get_db)):
    actor = _actor(db, actor)
    row = db.get(Entity, broadcast_id)
    if not row or row.kind != "broadcast" or row.owner_id != actor.id:
        raise HTTPException(404, "Рассылка не найдена")
    return _view(db, row, actor, True)


@router.post("/broadcasts", status_code=201)
def create_broadcast(
    body: BroadcastInput, actor=Depends(get_actor), db=Depends(get_db)
):
    actor = _actor(db, actor)
    identifier = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL, f"korsik:broadcast:{actor.id}:{body.client_request_id}"
        )
    )
    if db.bind.dialect.name == "postgresql":
        db.execute(
            sql_text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": int(uuid.UUID(identifier)) % (2**63)},
        )
    request_hash = fingerprint(body.model_dump(mode="json"))
    old = db.get(Entity, identifier)
    if old:
        if (
            old.kind != "broadcast"
            or old.owner_id != actor.id
            or old.data.get("request_hash") != request_hash
        ):
            raise HTTPException(
                409, "Идентификатор запроса уже использован для другой рассылки"
            )
        return _view(db, old, actor, True)
    scheduled = _scheduled(body)
    if len(set(body.recipient_ids)) != len(body.recipient_ids) or len(
        set(body.org_unit_ids)
    ) != len(body.org_unit_ids):
        raise HTTPException(422, "Повторяющиеся получатели или подразделения")
    employees = _employees(db, actor)
    units = _units(db, actor, employees)
    if (
        set(body.recipient_ids) - employees.keys()
        or set(body.org_unit_ids) - units.keys()
    ):
        raise HTTPException(404, "Получатель или подразделение недоступны")
    selected = set(body.recipient_ids)
    org_members = (
        {
            key
            for key, row in employees.items()
            if set(body.org_unit_ids) & _membership(row, units)
        }
        if body.org_unit_ids
        else set()
    )
    selected |= org_members
    if not selected or len(selected) > MAX_RECIPIENTS:
        raise HTTPException(422, f"Выберите от 1 до {MAX_RECIPIENTS} сотрудников")
    readable, html, mentions = _render(db, actor, body.text, employees)
    resolved = []
    for key in sorted(selected):
        employee = employees[key]
        binding, reason = _binding(db, employee)
        if reason and key in body.recipient_ids:
            raise HTTPException(
                422, f"{employee.data.get('name', 'Сотрудник')}: {REASONS[reason]}"
            )
        resolved.append((employee, binding, reason))
    if all(reason for _, _, reason in resolved):
        raise HTTPException(
            422, "В выбранных подразделениях нет доступных получателей с MAX"
        )
    mailing = save_entity(
        db,
        "broadcast",
        {
            "request_hash": request_hash,
            "source_text": body.text,
            "display_text": readable,
            "html_text": html,
            "mentions": mentions,
            "scheduled_date": body.scheduled_date.isoformat(),
            "scheduled_time": body.scheduled_time,
            "timezone": body.timezone,
            "scheduled_at": scheduled.isoformat(),
            "org_unit_ids": body.org_unit_ids,
            "recipient_ids": body.recipient_ids,
        },
        owner_id=actor.id,
        status="WAITING",
        id=identifier,
    )
    for employee, binding, reason in resolved:
        db.add(
            Entity(
                id=str(uuid.uuid5(uuid.UUID(identifier), employee.id)),
                kind="broadcast_recipient",
                parent_id=identifier,
                status="SKIPPED" if reason else "WAITING",
                data={
                    "employee_id": employee.id,
                    "episode_id": employee.data.get("episode_id"),
                    "binding_id": binding.id if binding else None,
                    "max_user_id": (
                        str(binding.data["max_user_id"]) if binding else None
                    ),
                    "explicit": employee.id in body.recipient_ids,
                    "reason": reason,
                },
            )
        )
    db.flush()
    audit(
        db,
        actor,
        "broadcast.created",
        mailing.id,
        {"recipients": len(resolved), "scheduled_at": scheduled.isoformat()},
    )
    commit(db)
    return _view(db, mailing, actor, True)


def _delivery_check(db, mailing, recipient):
    user = db.scalar(
        select(User)
        .where(User.id == mailing.owner_id)
        .execution_options(populate_existing=True)
    )
    if not user or not user.enabled or user.role not in ROLES:
        return False, "AUTHOR_SCOPE_REVOKED"
    actor = user_actor(user)
    employee = db.scalar(
        select(Entity)
        .where(Entity.id == recipient.data["employee_id"], Entity.kind == "employee")
        .execution_options(populate_existing=True)
    )
    if not employee or not actor_can_employee(db, actor, employee.id):
        return False, "AUTHOR_SCOPE_REVOKED"
    binding, reason = _binding(db, employee)
    if reason:
        return False, reason
    if employee.data.get("episode_id") != recipient.data.get("episode_id"):
        return False, "STALE_EPISODE"
    if binding.id != recipient.data.get("binding_id") or str(
        binding.data["max_user_id"]
    ) != recipient.data.get("max_user_id"):
        return False, "BINDING_CHANGED"
    if not recipient.data.get("explicit") and not set(
        mailing.data.get("org_unit_ids", [])
    ) & org_unit_ancestors(db, employee):
        return False, "ORG_MEMBERSHIP_CHANGED"
    for mention in mailing.data.get("mentions", []):
        person = db.scalar(
            select(Entity)
            .where(Entity.id == mention["employee_id"], Entity.kind == "employee")
            .execution_options(populate_existing=True)
        )
        if (
            not employee_is_enabled(person)
            or not actor_can_employee(db, actor, mention["employee_id"])
            or person.data.get("episode_id") != mention["episode_id"]
        ):
            return False, "MENTION_UNAVAILABLE"
    return True, None


def delivery_allowed(db, outbox):
    mailing = db.get(Entity, outbox.data.get("broadcast_id"))
    recipient = db.get(Entity, outbox.data.get("broadcast_recipient_id"))
    if (
        not mailing
        or mailing.kind != "broadcast"
        or not recipient
        or recipient.kind != "broadcast_recipient"
        or recipient.parent_id != mailing.id
        or recipient.data.get("employee_id") != outbox.recipient_id
    ):
        return False, "BROADCAST_UNAVAILABLE"
    if mailing.status not in {"WAITING", "SENDING"} or recipient.status in {
        "SKIPPED",
        "SUPPRESSED",
    }:
        return False, "BROADCAST_UNAVAILABLE"
    return _delivery_check(db, mailing, recipient)


def message_body(db, outbox):
    mailing = db.get(Entity, outbox.data["broadcast_id"])
    return {"text": mailing.data["display_text"]}


def tick(db):
    """One transaction, at most 100 new outbox messages per scheduler cycle."""
    budget = BATCH
    rows = db.scalars(
        select(Entity)
        .where(Entity.kind == "broadcast", Entity.status.in_(["WAITING", "SENDING"]))
        .order_by(Entity.created_at)
        .with_for_update(skip_locked=True)
    )
    for mailing in rows:
        if aware(datetime.fromisoformat(mailing.data["scheduled_at"])) > now():
            continue
        targets = _rows(db, mailing.id)
        outs = {
            out.id: out
            for out in db.scalars(
                select(Outbox).where(
                    Outbox.id.in_(
                        [
                            r.data["outbox_id"]
                            for r in targets
                            if r.data.get("outbox_id")
                        ]
                    )
                )
            )
        }
        for recipient in targets:
            out = outs.get(recipient.data.get("outbox_id"))
            if out:
                if out.status == "ACCEPTED":
                    recipient.status = "ACCEPTED"
                elif out.status in FINAL_FAILURES:
                    recipient.status = out.status
                continue
            if recipient.status != "WAITING" or budget <= 0:
                continue
            budget -= 1
            valid, reason = _delivery_check(db, mailing, recipient)
            if not valid:
                recipient.status = "SUPPRESSED"
                recipient.data = {**recipient.data, "reason": reason}
                continue
            out = queue_message(
                db,
                "EMPLOYEE",
                recipient.data["employee_id"],
                mailing.data["display_text"],
                f"broadcast:{mailing.id}:{recipient.data['employee_id']}",
                data={
                    "broadcast_id": mailing.id,
                    "broadcast_recipient_id": recipient.id,
                    "episode_id": recipient.data["episode_id"],
                },
            )
            recipient.status = "QUEUED"
            recipient.data = {**recipient.data, "outbox_id": out.id}
        eligible = [recipient for recipient in targets if recipient.status != "SKIPPED"]
        mailing.status = (
            "COMPLETED"
            if eligible and all(r.status == "ACCEPTED" for r in eligible)
            else "SENDING"
        )
        db.flush()
        if budget <= 0:
            break
