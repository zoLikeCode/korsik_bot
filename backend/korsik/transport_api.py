"""Narrow connector API. Only core resolves recipients and current rights."""

from datetime import datetime, timedelta
import hmac, json, hashlib, uuid
from pathlib import Path
from fastapi import APIRouter, Request, HTTPException, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from .db import get_db, now, new_id, aware
from .models import Entity, Outbox, TransportDelivery, TransportInbox, Message, User
from .settings import get_settings, read_secret
from .common import (
    list_kind, get_entity, add_message, commit, enqueue, employee_is_enabled,
)

router = APIRouter()


def service_identity(request: Request):
    if not hmac.compare_digest(
        read_secret(get_settings().internal_secret_file),
        request.headers.get("X-Korsik-Transport-Key", ""),
    ):
        raise HTTPException(403, detail="forbidden")


def valid_outbox(db, outbox):
    ctx = outbox.data or {}
    if outbox.recipient_kind == "EMPLOYEE":
        employee = db.scalar(
            select(Entity).where(Entity.id == outbox.recipient_id)
            .execution_options(populate_existing=True)
        )
        if (
            not employee
            or employee.kind != "employee"
            or employee.data.get("employment_stage") == "ENDED"
        ):
            return None, "EMPLOYMENT_ENDED"
        if not employee_is_enabled(employee):
            return None, "EMPLOYEE_DISABLED"
        if "deleted_at" in employee.data:
            # Restoration never reauthorizes old queued messages. This also
            # fences rows occupied by a connector while deletion used SKIP LOCKED.
            try:
                deleted_at = aware(datetime.fromisoformat(employee.data["deleted_at"]))
                if not outbox.created_at or aware(outbox.created_at) <= deleted_at:
                    return None, "EMPLOYEE_DELETED"
            except (ValueError, TypeError, AttributeError):
                return None, "EMPLOYEE_DELETED"
        binding = db.get(Entity, employee.data.get("binding_id", ""))
        if (
            not binding
            or binding.kind != "max_binding"
            or binding.status != "ACTIVE"
            or binding.parent_id != employee.id
            or employee.data.get("max_binding_status") != "VERIFIED"
        ):
            return None, "NO_BINDING"
        if binding.data.get("episode_id") != employee.data.get(
            "episode_id", employee.id
        ):
            return None, "STALE_EPISODE"
        current_episode = employee.data.get("episode_id", employee.id)
        if ctx.get("episode_id") and ctx["episode_id"] != current_episode:
            return None, "STALE_EPISODE"
        conv = (
            db.get(Entity, ctx["conversation_id"])
            if ctx.get("conversation_id")
            else next(
                (
                    c
                    for c in list_kind(db, "conversation", parent_id=employee.id)
                    if c.data.get("episode_id")
                    == employee.data.get("episode_id", employee.id)
                ),
                None,
            )
        )
        if ctx.get("conversation_id") and not conv:
            return None, "STALE_CONVERSATION"
        if conv and (
            conv.status == "CLOSED"
            or conv.data.get("employee_id") != employee.id
            or conv.data.get("episode_id") != current_episode
        ):
            return None, "STALE_CONVERSATION"
        if (
            ctx.get("domain_generated")
            and not ctx.get("allow_during_human")
            and conv
            and conv.data.get("mode") in {"HUMAN", "WAITING_HR"}
        ):
            return None, "HUMAN_SUPPRESSION"
        if ctx.get("broadcast_id"):
            from .broadcasts import delivery_allowed

            allowed, reason = delivery_allowed(db, outbox)
            if not allowed:
                return None, reason
        if ctx.get("journey_id"):
            journey = db.get(Entity, ctx["journey_id"])
            if (
                not journey
                or journey.parent_id != employee.id
                or journey.data.get("episode_id") != current_episode
                or journey.status in {"PAUSED", "CANCELLED", "COMPLETED"}
            ):
                return None, "JOURNEY_INACTIVE"
            if ctx.get("schedule_revision") is not None and ctx[
                "schedule_revision"
            ] != journey.data.get("schedule_revision"):
                return None, "STALE_SCHEDULE"
            if conv and conv.data.get("mode") in {"HUMAN", "WAITING_HR"}:
                return None, "HUMAN_SUPPRESSION"
            from .domain import _scenario_applies

            scenario = db.get(Entity, journey.data.get("scenario_id"))
            if not scenario or not _scenario_applies(scenario, employee):
                return None, "PROGRAM_SCOPE_REVOKED"
            if ctx.get("simple_resume") and journey.data.get("waiting_step_key") != ctx.get("step_key"):
                return None, "SCENARIO_ALREADY_ADVANCED"
        if ctx.get("simple_free_answer_capture_id"):
            capture = db.get(Entity, ctx["simple_free_answer_capture_id"])
            capture_journey = db.get(Entity, ctx.get("journey_id", ""))
            if (
                not capture or capture.kind != "scenario_free_answer"
                or capture.status != "OPEN" or capture.parent_id != employee.id
                or capture.data.get("episode_id") != current_episode
                or capture.data.get("conversation_id") != (conv.id if conv else None)
                or capture.data.get("journey_id") != ctx.get("journey_id")
                or capture.data.get("step_key") != ctx.get("step_key")
                or not capture_journey
                or capture_journey.data.get("waiting_step_key") != ctx.get("step_key")
            ):
                return None, "SCENARIO_FREE_ANSWER_CLOSED"
        if ctx.get("survey_run_id"):
            run = db.get(Entity, ctx["survey_run_id"])
            if (
                not run
                or run.data.get("employee_id") != employee.id
                or run.data.get("episode_id")
                != employee.data.get("episode_id", employee.id)
                or run.status
                in {"PAUSED", "COMPLETED", "DECLINED", "CANCELLED", "EXPIRED"}
            ):
                return None, "SURVEY_INACTIVE"
            if run.data.get("campaign_id"):
                from .survey_campaigns import delivery_allowed

                allowed, reason = delivery_allowed(db, run)
                if not allowed:
                    return None, reason
            if (
                run.data.get("expires_at")
                and aware(datetime.fromisoformat(run.data["expires_at"])) <= now()
            ):
                return None, "SURVEY_EXPIRED"
            if (
                ctx.get("question_key")
                and run.data.get("current_question_key")
                and ctx["question_key"] != run.data["current_question_key"]
            ):
                return None, "STALE_QUESTION"
        if ctx.get("answer_id"):
            answer = db.get(Entity, ctx["answer_id"])
            if (
                not answer
                or answer.data.get("employee_id") != employee.id
                or answer.status not in {"COMPLETED", "ABSTAINED"}
            ):
                return None, "ANSWER_INACTIVE"
            if not conv or conv.data.get("mode", "BOT") != "BOT":
                return None, "HUMAN_SUPPRESSION"
            from .retrieval import answer_still_authorized

            if not answer_still_authorized(db, conv, answer.data):
                return None, "SOURCE_REVOKED"
        if ctx.get("support_case_id"):
            case = db.get(Entity, ctx["support_case_id"])
            if (
                not case or case.kind != "case"
                or case.data.get("employee_id") != employee.id
                or case.data.get("episode_id") != current_episode
                or case.status in {"RESOLVED", "CLOSED", "CANCELLED"}
            ):
                return None, "SUPPORT_NOTICE_STALE"
        if ctx.get("contact_id"):
            from .domain import verified_hr_contact

            contact = verified_hr_contact(db, employee)
            if (
                not contact.get("available")
                or contact.get("contact_id") != ctx["contact_id"]
                or contact.get("contact_version") != ctx.get("contact_version")
                or contact.get("user_id") != ctx.get("required_assigned_hr_id")
            ):
                return None, "RECRUITER_CONTACT_REVOKED"
        if ctx.get("human_response") and ctx.get("reply_to_message_id"):
            quoted = db.get(Message, ctx["reply_to_message_id"])
            if not quoted or not conv or quoted.conversation_id != conv.id:
                return None, "QUOTE_CONTEXT_REVOKED"
            if (quoted.data or {}).get("citations"):
                from .retrieval import answer_still_authorized
                if not answer_still_authorized(db, conv, quoted.data):
                    return None, "SOURCE_REVOKED"
        if ctx.get("human_response") and ctx.get("case_id"):
            case = db.get(Entity, ctx["case_id"])
            if (
                not case
                or case.data.get("employee_id") != employee.id
                or not ctx.get("author_id")
                or case.data.get("assigned_user_id") != ctx["author_id"]
            ):
                return None, "CASE_REASSIGNED"
            author = db.get(User, ctx["author_id"])
            from .auth import user_actor
            from .common import actor_can_employee

            if (
                not author
                or not author.enabled
                or author.role not in {"HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT"}
                or not actor_can_employee(db, user_actor(author), employee.id)
            ):
                return None, "HR_ACCESS_REVOKED"
        from .retrieval import scope_matches
        from .assets import can_asset
        from .auth import Actor

        asset_actor = Actor(employee.id, "", "EMPLOYEE", {}, employee.id)
        for entry in outbox.attachments or []:
            asset_id = entry if isinstance(entry, str) else entry.get("asset_id")
            asset = db.get(Entity, asset_id) if asset_id else None
            if (
                not asset
                or asset.kind != "asset"
                or asset.status not in {"READY", "APPROVED"}
            ):
                return None, "ATTACHMENT_REVOKED"
            if asset.data.get("employee_id"):
                if not can_asset(db, asset_actor, asset):
                    return None, "ATTACHMENT_REVOKED"
            elif asset.data.get("purpose") != "SCENARIO_MATERIAL" or not scope_matches(
                asset.data.get("scope", {}), employee
            ):
                return None, "ATTACHMENT_SCOPE_REVOKED"
        return {"user_id": int(binding.data["max_user_id"])}, None
    if outbox.recipient_kind == "USER":
        user = db.scalar(
            select(User).where(User.id == outbox.recipient_id)
            .execution_options(populate_existing=True)
        )
        if not user or not user.enabled:
            return None, "USER_DISABLED"
        if ctx.get("required_roles") and user.role not in ctx["required_roles"]:
            return None, "ROLE_REVOKED"
        from .auth import employee_account_enabled

        if not employee_account_enabled(db, user):
            return None, "EMPLOYEE_DISABLED"
        if ctx.get("manager_task_id"):
            from .auth import user_actor
            from .domain import task_authorize
            from fastapi import HTTPException

            task = db.get(Entity, ctx["manager_task_id"])
            if (
                not task
                or task.kind != "manager_task"
                or task.status in ("COMPLETED", "CANCELLED")
            ):
                return None, "TASK_INACTIVE"
            if ctx.get("assignment_revision") != task.data.get(
                "assignment_revision", 1
            ):
                return None, "TASK_ASSIGNMENT_REVOKED"
            try:
                task_authorize(db, user_actor(user), task)
            except HTTPException:
                return None, "TASK_ASSIGNMENT_REVOKED"
        if ctx.get("required_scope"):
            from .domain import scope_subset

            if user.role != "MANAGER" and not scope_subset(
                ctx["required_scope"], user.scope or {}
            ):
                return None, "SCOPE_REVOKED"
        if ctx.get("employee_id"):
            from .auth import user_actor
            from .common import actor_can_employee

            if not actor_can_employee(db, user_actor(user), ctx["employee_id"]):
                return None, "ASSIGNMENT_REVOKED"
        if ctx.get("employee_ids"):
            from .auth import user_actor
            from .common import actor_can_employee

            if any(
                not actor_can_employee(db, user_actor(user), employee_id)
                for employee_id in ctx["employee_ids"]
            ):
                return None, "ASSIGNMENT_REVOKED"
        if ctx.get("profile_publication_id"):
            from .domain import profile_content_fingerprint

            publication = db.get(Entity, ctx["profile_publication_id"])
            employee = db.get(Entity, ctx.get("employee_id"))
            if (
                not publication
                or publication.kind != "profile_publication"
                or publication.status != "APPROVED"
                or not employee_is_enabled(employee)
                or employee.data.get("employment_stage") == "ENDED"
                or publication.data.get("episode_id") != employee.data.get("episode_id")
                or user.id not in publication.data.get("recipient_user_ids", [])
                or publication.data.get("content_fingerprint")
                != profile_content_fingerprint(db, employee)
            ):
                return None, "PROFILE_PUBLICATION_REVOKED"
        binding = next(
            (
                b
                for b in list_kind(db, "principal_binding", owner_id=user.id)
                if b.status == "ACTIVE"
            ),
            None,
        )
        if not binding:
            return None, "NO_BINDING"
        return {"user_id": int(binding.data["max_user_id"])}, None
    if outbox.recipient_kind == "MAX_USER":
        if not ctx.get("trusted_event_id") or outbox.attachments:
            return None, "UNVERIFIED_DIRECT_RECIPIENT"
        inbox = db.get(TransportInbox, ctx["trusted_event_id"])
        if not inbox:
            return None, "UNVERIFIED_DIRECT_RECIPIENT"
        event = inbox.payload
        source = (
            (event.get("callback") or {}).get("user")
            or event.get("user")
            or (event.get("message") or {}).get("sender")
            or {}
        )
        if str(source.get("user_id")) != str(outbox.recipient_id):
            return None, "UNVERIFIED_DIRECT_RECIPIENT"
        return {"user_id": int(outbox.recipient_id)}, None
    return None, "INVALID_RECIPIENT_KIND"


def signed_buttons(db, outbox):
    from .common import save_entity

    secret = read_secret(get_settings().secret_key_file)
    buttons = []
    for item in (outbox.callback_buttons or [])[:30]:
        if item.get("url"):
            if item["url"].startswith(("https://", "http://localhost:")):
                buttons.append(
                    {
                        "type": "link",
                        "text": str(item.get("text", "Открыть"))[:80],
                        "url": item["url"],
                    }
                )
            continue
        payload = item.get("payload") or item.get("action") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                continue
        # Old queued scenario messages may retain a previous release's global menu.
        # Keep authored scenario choices/formulations and legacy input callbacks.
        if (outbox.data or {}).get("journey_id") and (payload.get("kind") or payload.get("action")) == "RAG_QUERY":
            continue
        if outbox.recipient_kind == "EMPLOYEE" and (
            (isinstance(payload, dict) and (payload.get("action") or payload.get("kind")) in {
                "CONTACT_HR", "HANDOFF", "CONTACT_RECRUITER",
            })
            or str(item.get("text", "")).strip() == "Связаться с HR"
        ):
            continue
        aid = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                "callback:" + outbox.id + ":" + json.dumps(payload, sort_keys=True),
            )
        )
        if not db.get(Entity, aid):
            employee = (
                db.get(Entity, outbox.recipient_id)
                if outbox.recipient_kind == "EMPLOYEE"
                else None
            )
            save_entity(
                db,
                "callback_action",
                {
                    "payload": payload,
                    "recipient_kind": outbox.recipient_kind,
                    "recipient_id": outbox.recipient_id,
                    "outbox_id": outbox.id,
                    "episode_id": (
                        employee.data.get("episode_id", employee.id)
                        if employee
                        else None
                    ),
                    "expires_at": (now() + timedelta(days=14)).isoformat(),
                },
                status="ACTIVE",
                parent_id=outbox.id,
                id=aid,
            )
        signature = hmac.new(secret.encode(), aid.encode(), hashlib.sha256).hexdigest()[
            :24
        ]
        buttons.append(
            {
                "type": "callback",
                "text": str(item.get("text", "Выбрать"))[:80],
                "payload": f"k:{aid}:{signature}",
            }
        )
    return [buttons[i : i + 2] for i in range(0, len(buttons), 2)]


def prepare_deliveries(db):
    rows = db.scalars(
        select(Outbox)
        .where(Outbox.status == "PENDING")
        .order_by(Outbox.created_at)
        .limit(100)
        .with_for_update(skip_locked=True)
    ).all()
    for outbox in rows:
        target, reason = valid_outbox(db, outbox)
        if not target:
            outbox.status = (
                "WAITING_BINDING"
                if reason == "NO_BINDING" and not outbox.data.get("answer_id")
                else "SUPPRESSED"
            )
            outbox.data = {**outbox.data, "suppression_reason": reason}
            continue
        if outbox.recipient_kind == "EMPLOYEE" and not outbox.data.get("message_id"):
            from .chat import conversation_for

            conv = conversation_for(db, db.get(Entity, outbox.recipient_id))
            message = add_message(
                db,
                conv,
                "BOT",
                outbox.text,
                {"outbox_id": outbox.id, "delivery_status": "PENDING"},
            )
            outbox.data = {
                **outbox.data,
                "message_id": message.id,
                "conversation_id": conv.id,
            }
        if not db.scalar(
            select(TransportDelivery).where(TransportDelivery.outbox_id == outbox.id)
        ):
            db.add(
                TransportDelivery(id=new_id(), outbox_id=outbox.id, status="PENDING")
            )
        outbox.status = "QUEUED"
    for outbox in db.scalars(
        select(Outbox).where(Outbox.status == "WAITING_BINDING").limit(50)
    ):
        if (now() - aware(outbox.created_at)).total_seconds() > 86400:
            outbox.status = "SUPPRESSED"
        elif valid_outbox(db, outbox)[0]:
            outbox.status = "PENDING"


@router.post(
    "/internal/transport/deliveries/{delivery_id}/claim",
    dependencies=[Depends(service_identity)],
)
def claim_delivery(delivery_id: str, db=Depends(get_db)):
    delivery = db.scalar(
        select(TransportDelivery)
        .where(TransportDelivery.id == delivery_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        not delivery
        or delivery.status not in {"PENDING", "RETRY"}
        or aware(delivery.not_before) > now()
    ):
        raise HTTPException(409, detail="delivery_not_pending")
    outbox = db.scalar(
        select(Outbox)
        .where(Outbox.id == delivery.outbox_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not outbox or outbox.status not in {"QUEUED", "PENDING"}:
        raise HTTPException(409, detail="outbox_not_pending")
    target, reason = valid_outbox(db, outbox)
    if not target:
        outbox.status = "SUPPRESSED"
        delivery.status = "SUPPRESSED"
        delivery.error_code = reason
        commit(db)
        raise HTTPException(409, detail="delivery_suppressed")
    if len(outbox.text) > 3900:
        raise HTTPException(422, detail="message_length_exceeded")
    assets = []
    for entry in outbox.attachments or []:
        aid = entry if isinstance(entry, str) else entry.get("asset_id")
        asset = db.get(Entity, aid) if aid else None
        if (
            not asset
            or asset.kind != "asset"
            or asset.status not in {"READY", "APPROVED"}
        ):
            raise HTTPException(409, detail="attachment_not_ready")
        assets.append(
            {
                "asset_id": asset.id,
                "filename": asset.data.get("filename", "file"),
                "content_type": asset.data.get(
                    "content_type", "application/octet-stream"
                ),
            }
        )
    keyboard = signed_buttons(db, outbox)
    body = {"text": outbox.text, "notify": True}
    if outbox.data.get("broadcast_id"):
        from .broadcasts import message_body

        body.update(message_body(db, outbox))
    if keyboard:
        body["attachments"] = [
            {"type": "inline_keyboard", "payload": {"buttons": keyboard}}
        ]
    delivery.status = "SENDING"
    delivery.attempt_id = new_id()
    delivery.attempts += 1
    delivery.lease_until = now() + timedelta(seconds=180)
    outbox.status = "SENDING"
    outbox.attempts += 1
    outbox.lease_until = delivery.lease_until
    commit(db)
    return {
        "delivery_id": delivery.id,
        "attempt_id": delivery.attempt_id,
        "recipient": target,
        "body": body,
        "assets": assets,
    }


class DeliveryReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt_id: str
    status: str = Field(pattern="^(ACCEPTED|REJECTED|RETRY|UNKNOWN)$")
    platform_message_id: str | None = None
    error_code: str | None = Field(default=None, max_length=80)


@router.post(
    "/internal/transport/deliveries/{delivery_id}/receipt",
    dependencies=[Depends(service_identity)],
)
def delivery_receipt(delivery_id: str, body: DeliveryReceipt, db=Depends(get_db)):
    delivery = db.scalar(
        select(TransportDelivery)
        .where(TransportDelivery.id == delivery_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not delivery:
        raise HTTPException(404, detail="not_found")
    if delivery.attempt_id != body.attempt_id:
        raise HTTPException(409, detail="stale_delivery_attempt")
    if delivery.status in {"ACCEPTED", "FAILED", "UNKNOWN"}:
        return {"status": delivery.status}
    outbox = db.scalar(
        select(Outbox)
        .where(Outbox.id == delivery.outbox_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not outbox:
        raise HTTPException(404, detail="not_found")
    if body.status == "ACCEPTED":
        delivery.status = "ACCEPTED"
        delivery.platform_message_id = body.platform_message_id
        outbox.status = "ACCEPTED"
        outbox.platform_message_id = body.platform_message_id
        outbox.data = {**outbox.data, "accepted_at": now().isoformat()}
        enqueue(
            db,
            "OUTBOX_ACCEPTED",
            {"outbox_id": outbox.id},
            f"outbox-accepted:{outbox.id}",
        )
    elif body.status == "RETRY" and delivery.attempts < 8:
        delivery.status = "RETRY"
        delivery.not_before = now() + timedelta(
            seconds=min(300, 5 * 2**delivery.attempts)
        )
        outbox.status = "QUEUED"
    else:
        delivery.status = "UNKNOWN" if body.status == "UNKNOWN" else "FAILED"
        outbox.status = "UNKNOWN_DELIVERY" if body.status == "UNKNOWN" else "FAILED"
    delivery.error_code = body.error_code
    delivery.lease_until = None
    outbox.lease_until = None
    message = db.get(Message, outbox.data.get("message_id", ""))
    if message:
        message.data = {
            **message.data,
            "delivery_status": outbox.status,
            "platform_message_id": body.platform_message_id,
        }
    commit(db)
    return {"status": delivery.status}


@router.get(
    "/internal/transport/deliveries/{delivery_id}/assets/{asset_id}",
    dependencies=[Depends(service_identity)],
)
def staged_delivery_asset(delivery_id: str, asset_id: str, db=Depends(get_db)):
    from fastapi.responses import FileResponse

    delivery = db.get(TransportDelivery, delivery_id)
    if (
        not delivery
        or delivery.status != "SENDING"
        or aware(delivery.lease_until) <= now()
    ):
        raise HTTPException(404, detail="not_found")
    outbox = db.get(Outbox, delivery.outbox_id)
    ids = [a if isinstance(a, str) else a.get("asset_id") for a in outbox.attachments]
    if not valid_outbox(db, outbox)[0] or asset_id not in ids:
        raise HTTPException(404, detail="not_found")
    asset = get_entity(db, asset_id, "asset")
    path = Path(asset.data.get("storage_path", ""))
    if not path.is_file() or not path.resolve().is_relative_to(
        Path(get_settings().blob_root).resolve()
    ):
        raise HTTPException(404, detail="not_found")
    return FileResponse(
        path,
        media_type=asset.data.get("content_type", "application/octet-stream"),
        filename=asset.data.get("filename", "file"),
    )


class DeliveryValidation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt_id: str


@router.post(
    "/internal/transport/deliveries/{delivery_id}/validate",
    dependencies=[Depends(service_identity)],
)
def validate_delivery_attempt(
    delivery_id: str, body: DeliveryValidation, db=Depends(get_db)
):
    """Last ACL fence after connector uploads/rate limiting and before MAX send."""
    delivery = db.scalar(
        select(TransportDelivery)
        .where(TransportDelivery.id == delivery_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        not delivery
        or delivery.status != "SENDING"
        or delivery.attempt_id != body.attempt_id
        or not delivery.lease_until
        or aware(delivery.lease_until) <= now()
    ):
        raise HTTPException(409, "delivery_attempt_inactive")
    outbox = db.scalar(
        select(Outbox)
        .where(Outbox.id == delivery.outbox_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    target, reason = valid_outbox(db, outbox) if outbox else (None, "outbox_missing")
    if not outbox or outbox.status != "SENDING" or not target:
        delivery.status = "SUPPRESSED"
        delivery.error_code = reason or "outbox_inactive"
        delivery.lease_until = None
        if outbox:
            outbox.status = "SUPPRESSED"
            outbox.lease_until = None
            outbox.data = {**outbox.data, "suppression_reason": delivery.error_code}
        commit(db)
        raise HTTPException(409, "delivery_suppressed")
    return {
        "status": "VALID",
        "delivery_id": delivery.id,
        "attempt_id": delivery.attempt_id,
    }
