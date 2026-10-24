from copy import deepcopy
import hashlib, json
from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError
from .models import Entity, Audit, Job, Outbox, Message
from .db import now, new_id

ALIASES = {
    "employee": "employee_id",
    "site": "site_id",
    "organization": "legal_entity_id",
    "scenario": "scenario_id",
    "scenario_version": "version_id",
    "journey": "journey_id",
    "survey_run": "survey_run_id",
    "conversation": "conversation_id",
    "case": "case_id",
    "manager_task": "task_id",
    "document": "document_id",
    "document_revision": "revision_id",
    "assistant_menu": "menu_id",
    "assistant_menu_version": "version_id",
    "input_capture": "capture_id",
    "knowledge_gap": "gap_id",
    "operation": "operation_id",
    "answer": "answer_id",
}


def require_role(actor, *roles):
    if actor.role not in roles and not (actor.role == "HEAD_TALENT" and "HR_SPECIALIST" in roles):
        raise HTTPException(
            403, detail={"code": "forbidden", "message": "Недостаточно прав"}
        )


def get_entity(db: Session, id: str, kind=None, lock=False):
    q = select(Entity).where(Entity.id == id)
    if kind:
        q = q.where(Entity.kind == kind)
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    e = db.scalar(q)
    if not e:
        raise HTTPException(
            404, detail={"code": "not_found", "message": "Объект не найден"}
        )
    return e


def save_entity(
    db, kind, data, status="ACTIVE", parent_id=None, owner_id=None, id=None
):
    e = Entity(
        id=id or new_id(),
        kind=kind,
        data=deepcopy(data),
        status=status,
        parent_id=parent_id,
        owner_id=owner_id,
    )
    db.add(e)
    db.flush()
    return e


def list_kind(db, kind, status=None, parent_id=None, owner_id=None):
    q = select(Entity).where(Entity.kind == kind)
    if status:
        q = q.where(Entity.status == status)
    if parent_id:
        q = q.where(Entity.parent_id == parent_id)
    if owner_id:
        q = q.where(Entity.owner_id == owner_id)
    return list(db.scalars(q.order_by(Entity.created_at)).all())


def employee_is_enabled(employee):
    return bool(employee and employee.kind == "employee" and employee.status != "DELETED" and employee.data.get("enabled", True) is not False)


def actor_can_employee(db, actor, employee_id, *, include_deleted=False):
    # Directory and normal employee APIs share one deletion fence. The explicit
    # exception is reserved for the scoped HR restore transaction. Mutation and
    # transport boundaries refresh/lock the row; list reads reuse their query.
    e = db.get(Entity, employee_id)
    if not e or e.kind != "employee" or (e.status == "DELETED" and not include_deleted):
        return False
    d = e.data or {}
    if actor.role == "EMPLOYEE":
        return actor.employee_id == employee_id and employee_is_enabled(e)
    if actor.role == "MANAGER":
        return d.get("manager_id") == actor.id and employee_is_enabled(e)
    if actor.role in {"HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT"}:
        scope = actor.scope or {}
        if scope.get("all") is True:
            return True
        if (
            scope.get("legal_entity_ids")
            and d.get("legal_entity_id") not in scope["legal_entity_ids"]
        ):
            return False
        if scope.get("site_ids") and d.get("site_id") not in scope["site_ids"]:
            return False
        return bool(
            scope.get("site_ids")
            or scope.get("legal_entity_ids")
            or d.get("assigned_hr_id") == actor.id
        )
    return False


def assert_employee(db, actor, employee_id):
    if not actor_can_employee(db, actor, employee_id):
        raise HTTPException(
            404, detail={"code": "not_found", "message": "Объект не найден"}
        )
    return get_entity(db, employee_id, "employee")


def audit(db, actor, action, entity_id=None, data=None, *, reason=None):
    # Technical events never contain free-form HR reasons, which can quote
    # private documents or employee feedback. A separate business record is
    # readable only by the global HR audit endpoint.
    metadata = deepcopy(data or {})
    supplied_reason = metadata.pop("reason", None)
    reason = reason if reason is not None else supplied_reason
    row = Audit(
        id=new_id(),
        actor_id=getattr(actor, "id", None),
        action=action,
        entity_id=entity_id,
        data=metadata,
    )
    if isinstance(reason, str) and reason.strip():
        private = Entity(
            id=new_id(),
            kind="audit_reason",
            parent_id=row.id,
            owner_id=getattr(actor, "id", None),
            status="PRIVATE",
            data={
                "reason": reason,
                "action": action,
                "entity_id": entity_id,
                "actor_id": getattr(actor, "id", None),
                "recorded_at": now().isoformat(),
            },
        )
        db.add(private)
        row.data = {**metadata, "has_reason": True, "reason_ref": private.id}
    db.add(row)
    return row


def enqueue(db, kind, payload, unique_key, run_at=None):
    e = db.scalar(select(Job).where(Job.unique_key == unique_key))
    if e:
        return e
    if db.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert

        db.execute(
            insert(Job)
            .values(
                id=new_id(),
                kind=kind,
                payload=deepcopy(payload),
                unique_key=unique_key,
                run_at=run_at or now(),
                status="PENDING",
            )
            .on_conflict_do_nothing(index_elements=["unique_key"])
        )
        return db.scalar(select(Job).where(Job.unique_key == unique_key))
    e = Job(
        id=new_id(),
        kind=kind,
        payload=deepcopy(payload),
        unique_key=unique_key,
        run_at=run_at or now(),
        status="PENDING",
    )
    db.add(e)
    db.flush()
    return e


def queue_message(
    db,
    recipient_kind,
    recipient_id,
    text,
    unique_key,
    buttons=None,
    attachments=None,
    data=None,
):
    e = db.scalar(select(Outbox).where(Outbox.unique_key == unique_key))
    if e:
        return e
    if db.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert

        db.execute(
            insert(Outbox)
            .values(
                id=new_id(),
                recipient_kind=recipient_kind,
                recipient_id=recipient_id,
                text=text,
                unique_key=unique_key,
                callback_buttons=buttons or [],
                attachments=attachments or [],
                data=data or {},
                status="PENDING",
            )
            .on_conflict_do_nothing(index_elements=["unique_key"])
        )
        return db.scalar(select(Outbox).where(Outbox.unique_key == unique_key))
    e = Outbox(
        id=new_id(),
        recipient_kind=recipient_kind,
        recipient_id=recipient_id,
        text=text,
        unique_key=unique_key,
        callback_buttons=buttons or [],
        attachments=attachments or [],
        data=data or {},
        status="PENDING",
    )
    db.add(e)
    db.flush()
    return e


def add_message(db, conversation, author_type, text, data=None):
    # Caller locks conversation aggregate; seq is pinned and unique.
    seq = int(conversation.data.get("message_seq", 0)) + 1
    conversation.data = {**conversation.data, "message_seq": seq}
    message = Message(
        id=new_id(),
        conversation_id=conversation.id,
        seq=seq,
        author_type=author_type,
        text=text,
        data=data or {},
    )
    db.add(message)
    db.flush()
    return message


def etag(entity):
    return f'"{entity.id}:{entity.version}"'


def check_etag(request: Request, entity, required=True):
    value = request.headers.get("if-match")
    if not value:
        if required:
            raise HTTPException(
                428,
                detail={
                    "code": "precondition_required",
                    "message": "Обновите объект перед изменением",
                },
            )
        return
    if value != etag(entity):
        raise HTTPException(
            412,
            detail={
                "code": "stale_version",
                "message": "Объект изменился. Обновите страницу",
            },
        )


def serialize(e):
    if isinstance(e, Entity):
        result = {
            **deepcopy(e.data or {}),
            "id": e.id,
            "status": e.status,
            "version": e.version,
            "row_version": e.version,
            "created_at": e.created_at.isoformat(),
            "updated_at": e.updated_at.isoformat(),
            "etag": etag(e),
        }
        if e.kind in ALIASES:
            result[ALIASES[e.kind]] = e.id
        return result
    return e


def commit(db):
    try:
        db.commit()
    except StaleDataError:
        db.rollback()
        raise HTTPException(
            409,
            detail={
                "code": "concurrent_update",
                "message": "Конкурентное изменение; повторите после обновления",
            },
        )


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()


def can_delegate_scope(parent, child):
    """Every restriction on a delegator remains a restriction on a child grant."""
    if not isinstance(parent, dict) or not isinstance(child, dict):
        return False
    allowed = {"all", "site_ids", "legal_entity_ids", "employee_drilldown"}
    if set(child) - allowed:
        return False
    if "all" in child and type(child["all"]) is not bool:
        return False
    if "employee_drilldown" in child and type(child["employee_drilldown"]) is not bool:
        return False
    for field in ("site_ids", "legal_entity_ids"):
        value = child.get(field, [])
        if (
            not isinstance(value, list)
            or any(not isinstance(v, str) or not v for v in value)
            or len(value) != len(set(value))
        ):
            return False
    if parent.get("all") is True:
        return True
    if child.get("all") is True:
        return False
    if (
        child.get("employee_drilldown") is True
        and parent.get("employee_drilldown") is not True
    ):
        return False
    constrained = False
    for field in ("site_ids", "legal_entity_ids"):
        limit = parent.get(field)
        if limit:
            constrained = True
            if not child.get(field) or not set(child[field]).issubset(limit):
                return False
    return constrained
