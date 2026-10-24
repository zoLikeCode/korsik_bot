"""Operational metadata and integration controls; no raw HR content or credentials."""

import json, uuid
from datetime import timedelta
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from fastapi.responses import StreamingResponse
from sqlalchemy import delete
from .db import get_db, now
from .auth import get_actor
from .models import (
    Entity,
    Job,
    TransportCursor,
    SessionToken,
    TransportInbox,
    IdempotencyRecord,
)
from .common import (
    get_entity,
    save_entity,
    list_kind,
    serialize,
    require_role,
    check_etag,
    commit,
    audit,
    etag,
)

router = APIRouter()
MAX_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, "korsik:integration:max"))
KNOWLEDGE_RETENTION_ID = str(
    uuid.uuid5(uuid.NAMESPACE_URL, "korsik:knowledge-retention-policy")
)


class KnowledgeRetentionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool
    backup_retention_days: int | None = Field(default=None, ge=0, le=36500)
    hold: bool = False
    approval_reference: str = Field(default="", max_length=2000)
    approval_confirmed: bool = False
    reason: str = Field(min_length=3, max_length=2000)

    @model_validator(mode="after")
    def configured_approval(self):
        if self.enabled and (
            self.backup_retention_days is None
            or len(self.approval_reference.strip()) < 3
            or not self.approval_confirmed
        ):
            raise ValueError(
                "Approved company reference, confirmation and backup retention are required"
            )
        return self


def knowledge_retention_view(row):
    return {
        "id": KNOWLEDGE_RETENTION_ID,
        "status": row.status if row else "UNCONFIGURED",
        "enabled": bool(row and row.status == "ACTIVE" and row.data.get("configured")),
        "backup_retention_days": row.data.get("backup_retention_days") if row else None,
        "hold": bool(row and row.data.get("hold")),
        "approval_reference": row.data.get("approval_reference", "") if row else "",
        "policy_version": row.data.get("policy_version", 0) if row else 0,
        "automatic_business_deletion": False,
        "backup_deletion_responsibility": "OPERATOR_MANAGED_NOT_AUTOMATIC",
        "etag": etag(row) if row else f'"{KNOWLEDGE_RETENTION_ID}:0"',
    }


@router.get("/retention/knowledge-policy")
def get_knowledge_retention(
    response: Response, db=Depends(get_db), actor=Depends(get_actor)
):
    require_role(actor, "HR_ADMIN", "PLATFORM_ADMIN")
    row = db.get(Entity, KNOWLEDGE_RETENTION_ID)
    value = knowledge_retention_view(row)
    response.headers["ETag"] = value["etag"]
    return value


@router.put("/retention/knowledge-policy")
def put_knowledge_retention(
    body: KnowledgeRetentionInput,
    request: Request,
    response: Response,
    db=Depends(get_db),
    actor=Depends(get_actor),
):
    require_role(actor, "HR_ADMIN")
    if actor.scope.get("all") is not True:
        raise HTTPException(403, "Global HR administration scope is required")
    from sqlalchemy import select, text

    if db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(76810107)"))
    row = db.scalar(
        select(Entity)
        .where(Entity.id == KNOWLEDGE_RETENTION_ID)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    expected = knowledge_retention_view(row)["etag"]
    if not request.headers.get("if-match"):
        raise HTTPException(428, "If-Match is required")
    if request.headers["if-match"] != expected:
        raise HTTPException(412, "Retention policy changed; reload before saving")
    value = {
        "configured": body.enabled,
        "data_class": "KNOWLEDGE_DOCUMENT",
        "backup_retention_days": body.backup_retention_days,
        "hold": body.hold,
        "approval_reference": body.approval_reference.strip(),
        "approval_confirmed": body.approval_confirmed,
        "approved_by": actor.id,
        "approved_at": now().isoformat(),
        "reason": body.reason,
        "automatic_business_deletion": False,
        "policy_version": (row.data.get("policy_version", 0) if row else 0) + 1,
    }
    if row:
        row.data = value
        row.status = "ACTIVE" if body.enabled else "DISABLED"
    else:
        row = save_entity(
            db,
            "retention_policy",
            value,
            status="ACTIVE" if body.enabled else "DISABLED",
            id=KNOWLEDGE_RETENTION_ID,
        )
    audit(
        db,
        actor,
        "retention.knowledge_policy_configured",
        row.id,
        {
            "enabled": body.enabled,
            "hold": body.hold,
            "policy_version": value["policy_version"],
        },
        reason=body.reason,
    )
    commit(db)
    result = knowledge_retention_view(row)
    response.headers["ETag"] = result["etag"]
    return result


def max_integration(db):
    row = db.get(Entity, MAX_ID)
    if not row:
        row = save_entity(
            db,
            "integration",
            {
                "type": "MAX",
                "enabled": True,
                "transport": "WEBHOOK",
                "secret_management": "FILES_ONLY",
            },
            id=MAX_ID,
        )
    return row


def integration_view(db, row):
    registration = db.get(TransportCursor, "webhook-registration")
    state = registration.value if registration else {}
    return {
        **serialize(row),
        "integration_id": row.id,
        "runtime": {
            "status": state.get("status", "WAITING_REGISTRATION"),
            "registered_at": state.get("registered_at"),
            "webhook_url": state.get("url"),
        },
        "token_exposed": False,
    }


@router.get("/health/live")
def live():
    return {"status": "OK"}


@router.get("/health/ready")
def ready(db=Depends(get_db)):
    db.execute(__import__("sqlalchemy").text("SELECT 1"))
    return {"status": "READY"}


@router.get("/health/dependencies")
def dependencies(db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, "HR_ADMIN", "PLATFORM_ADMIN")
    return {"items": [e.data for e in list_kind(db, "service_health")]}


@router.get("/jobs/{job_id}")
def job_detail(job_id: str, db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, "HR_ADMIN", "PLATFORM_ADMIN")
    row = db.get(Job, job_id)
    if not row:
        raise HTTPException(404, "Job not found")
    return {
        "job_id": row.id,
        "kind": row.kind,
        "status": row.status,
        "attempts": row.attempts,
        "run_at": row.run_at.isoformat(),
        "lease_until": row.lease_until.isoformat() if row.lease_until else None,
        "error_code": row.error,
    }


@router.get("/answers/{answer_id}/events")
def answer_events(answer_id: str, db=Depends(get_db), actor=Depends(get_actor)):
    from .chat import answer_detail

    result = answer_detail(answer_id, actor, db)

    # A persisted state event, not unverified token streaming. Clients reconnect/poll while pending.
    async def events():
        yield "event: answer_state\nid: " + str(
            result.get("version", 1)
        ) + "\ndata: " + json.dumps(result, ensure_ascii=False) + "\n\n"

    return StreamingResponse(
        events(), media_type="text/event-stream", headers={"Cache-Control": "no-store"}
    )


@router.get("/integrations")
def integrations(db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, "HR_ADMIN", "PLATFORM_ADMIN")
    row = max_integration(db)
    commit(db)
    return {"items": [integration_view(db, row)]}


@router.get("/integrations/{integration_id}")
def integration(integration_id: str, db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, "HR_ADMIN", "PLATFORM_ADMIN")
    row = get_entity(db, integration_id, "integration")
    return integration_view(db, row)


@router.patch("/integrations/{integration_id}")
def integration_update(
    integration_id: str,
    body: dict,
    request: Request,
    db=Depends(get_db),
    actor=Depends(get_actor),
):
    require_role(actor, "HR_ADMIN", "PLATFORM_ADMIN")
    if actor.role == "HR_ADMIN" and actor.scope.get("all") is not True:
        raise HTTPException(403, "Global HR administration scope is required")
    row = get_entity(db, integration_id, "integration", lock=True)
    check_etag(request, row)
    if (
        set(body) - {"enabled", "reason"}
        or type(body.get("enabled")) is not bool
        or len(str(body.get("reason", ""))) < 3
    ):
        raise HTTPException(422, "Укажите enabled и причину")
    row.data = {**row.data, "enabled": body["enabled"]}
    control = db.get(TransportCursor, "max-control")
    if control:
        control.value = {"enabled": body["enabled"]}
    else:
        db.add(TransportCursor(id="max-control", value={"enabled": body["enabled"]}))
    audit(
        db,
        actor,
        "integration.update",
        row.id,
        {"enabled": body["enabled"], "reason": body["reason"]},
    )
    commit(db)
    return integration_view(db, row)


@router.post("/integrations/{integration_id}/tests", status_code=202)
def test_integration(
    integration_id: str, body: dict, db=Depends(get_db), actor=Depends(get_actor)
):
    require_role(actor, "HR_ADMIN", "PLATFORM_ADMIN")
    if actor.role == "HR_ADMIN" and actor.scope.get("all") is not True:
        raise HTTPException(403, "Global HR administration scope is required")
    row = get_entity(db, integration_id, "integration")
    tid = str(uuid.uuid4())
    db.add(
        TransportCursor(
            id="integration-test:" + tid,
            value={
                "status": "PENDING",
                "integration_id": row.id,
                "requested_at": now().isoformat(),
            },
        )
    )
    commit(db)
    return {
        "test_id": tid,
        "status": "PENDING",
        "result_url": f"/api/v1/integrations/{row.id}/tests/{tid}",
    }


@router.get("/integrations/{integration_id}/tests/{test_id}")
def integration_result(
    integration_id: str, test_id: str, db=Depends(get_db), actor=Depends(get_actor)
):
    require_role(actor, "HR_ADMIN", "PLATFORM_ADMIN")
    row = db.get(TransportCursor, "integration-test:" + test_id)
    if not row or row.value.get("integration_id") != integration_id:
        raise HTTPException(404, "Test not found")
    return {"test_id": test_id, **row.value}


@router.get("/retention/policies")
def retention(db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, "HR_ADMIN", "PLATFORM_ADMIN")
    policy = knowledge_retention_view(db.get(Entity, KNOWLEDGE_RETENTION_ID))
    return {
        "configured_business_retention": policy["enabled"],
        "knowledge_policy": policy,
        "automatic_business_deletion": False,
        "policies": [
            {"kind": "EXPIRED_SESSIONS", "action": "DELETE_AFTER_EXPIRY"},
            {"kind": "COMPLETED_TRANSPORT_INBOX", "retention_days": 30},
            {"kind": "FINISHED_IDEMPOTENCY_RESULTS", "retention_days": 7},
        ],
        "business_records": "Удаление персональных и нормативных данных требует утверждённого регламента компании; по умолчанию не удаляются автоматически.",
    }


def retention_sweep(db, job):
    if job.kind != "RETENTION_SWEEP":
        return False
    db.execute(delete(SessionToken).where(SessionToken.expires_at < now()))
    db.execute(
        delete(TransportInbox).where(
            TransportInbox.status == "DONE",
            TransportInbox.created_at < now() - timedelta(days=30),
        )
    )
    db.execute(
        delete(IdempotencyRecord).where(
            IdempotencyRecord.status_code != 102,
            IdempotencyRecord.created_at < now() - timedelta(days=7),
        )
    )
    for action in list_kind(db, "callback_action", status="ACTIVE"):
        expires = action.data.get("expires_at")
        if expires and __import__("datetime").datetime.fromisoformat(expires) < now():
            action.status = "EXPIRED"
    return True
