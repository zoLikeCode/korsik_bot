"""Durable job executor; SQL is authoritative, stale model results are fenced."""

import os, time, logging
from datetime import timedelta, datetime
import httpx
from sqlalchemy import select
from .db import SessionLocal, now, new_id, aware
from .models import Entity, Job, Message, Outbox
from .common import (
    get_entity,
    save_entity,
    list_kind,
    add_message,
    queue_message,
)
from .settings import get_settings

log = logging.getLogger("korsik.worker")


def claim_job(db, ingest=False):
    kinds = {
        "KNOWLEDGE_INGEST",
        "DOCUMENT_INGEST",
        "INGEST_DOCUMENT",
        "KNOWLEDGE_PUBLISH",
        "KNOWLEDGE_PURGE",
        "KNOWLEDGE_ROLLBACK",
        "KNOWLEDGE_QA",
        "KNOWLEDGE_CANDIDATE_BUILD",
        "KNOWLEDGE_SIMPLE_PREPARE",
        "LOCAL_AI_VALIDATE",
    }
    q = select(Job).where(Job.status.in_(["PENDING", "RETRY"]), Job.run_at <= now())
    q = q.where(Job.kind.in_(kinds) if ingest else ~Job.kind.in_(kinds))
    if not ingest:
        q = q.where(
            Job.kind == "ANSWER"
            if os.getenv("KORSIK_WORKER_KIND") == "answer"
            else Job.kind != "ANSWER"
        )
    job = db.scalar(
        q.order_by(Job.run_at, Job.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if not job:
        return None
    job.status = "PROCESSING"
    job.attempts += 1
    job.lease_until = now() + timedelta(
        seconds=(
            250
            if job.kind == "LOCAL_AI_VALIDATE"
            else 650 if ingest else 180 if job.kind == "ANSWER" else 90
        )
    )
    token = new_id()
    job.payload = {**job.payload, "_lease_token": token}
    db.commit()
    return job.id, token


def finish_answer(db, answer, result):
    # Only one terminal transition and one outbound key per answer.
    conv = get_entity(db, answer.data["conversation_id"], "conversation", lock=True)
    answer = db.scalar(
        select(Entity)
        .where(Entity.id == answer.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if answer.status not in {"QUEUED", "RUNNING"}:
        return False
    if conv.data.get("mode", "BOT") != "BOT":
        answer.status = "CANCELLED"
        answer.data = {**answer.data, "reason_code": "HUMAN_TAKEOVER"}
        return False
    employee = get_entity(db, conv.data["employee_id"], "employee")
    if employee.data.get("employment_stage") == "ENDED":
        answer.status = "CANCELLED"
        answer.data = {**answer.data, "reason_code": "EMPLOYMENT_ENDED"}
        return False
    if len(result.get("text", "")) > 3900:
        result = fallback_result("MESSAGE_TOO_LARGE")
    needs_support = result.get("mode") == "HANDOFF"
    if needs_support:
        # Delivery copy is independent of the immutable RAG source evaluation.
        # An unanswered question alerts support without pausing normal search;
        # only an explicit operator claim switches the conversation to HUMAN.
        result = {**result, "text": "Вопрос принят. Уточняем информацию; ответ появится здесь.",
                  "citations": [], "evidence": []}
    citations = []
    for citation in result.get("citations", []):
        cid = new_id()
        data = {
            **citation,
            "citation_id": cid,
            "employee_id": employee.id,
            "answer_id": answer.id,
            "conversation_id": conv.id,
        }
        save_entity(
            db,
            "citation",
            data,
            status="AVAILABLE",
            parent_id=answer.id,
            owner_id=employee.id,
            id=cid,
        )
        citations.append({**data, "source_url": f"/api/v1/citations/{cid}/source"})
    status = (
        "ABSTAINED" if result.get("mode") in {"HANDOFF", "CLARIFY"} else "COMPLETED"
    )
    answer.data = {
        **answer.data,
        **result,
        "citations": citations,
        "completed_at": now().isoformat(),
        "reason_code": result.get("reason_code", result.get("reason")),
    }
    answer.status = status
    message = add_message(
        db,
        conv,
        "BOT",
        result.get("text", "Не удалось подготовить ответ. Обратитесь к HR."),
        {"answer_id": answer.id, "mode": result.get("mode"), "citations": citations},
    )
    buttons = []
    if result.get("mode") not in {"HANDOFF", "CLARIFY"}:
        from .chat import quick_prompts_for

        menu = quick_prompts_for(db, conv)
        buttons = [
            {"text": p["label"], "payload": p["action"]}
            for p in menu.get("items", [])[:4]
        ]
    from .assets import citation_pages_for_answer

    attachments = citation_pages_for_answer(db, employee, answer)
    source_revisions = set()
    for c in citations:
        if c["revision_id"] in source_revisions:
            continue
        source_revisions.add(c["revision_id"])
        buttons.append(
            {
                "text": "Оригинал: " + c.get("document_title", "Источник")[:45],
                "payload": {
                    "action": "REQUEST_SOURCE",
                    "answer_id": answer.id,
                    "revision_id": c["revision_id"],
                },
            }
        )
    support_case = None
    if needs_support:
        from .chat import handoff

        # Actor construction grants only the employee's existing own scope.
        from .auth import Actor

        support_case = handoff(
            db, Actor(employee.id, "", "EMPLOYEE", {}, employee.id), conv,
            str(result.get("reason", "NO_EVIDENCE")),
            notify_employee=False, wait_for_human=False,
        )
        answer.data = {**answer.data, "support_case_id": support_case["id"]}
    queue_message(
        db,
        "EMPLOYEE",
        employee.id,
        message.text,
        f"answer:{answer.id}",
        buttons=buttons,
        attachments=attachments,
        data={
            **({"support_case_id": support_case["id"]} if support_case else {"answer_id": answer.id}),
            "conversation_id": conv.id,
            "message_id": message.id,
        },
    )
    if result.get("mode") == "HANDOFF" and result.get("reason") not in {
        "LOCAL_QUEUE_TIMEOUT",
        "LOCAL_DEADLINE",
        "HUMAN_TAKEOVER",
    }:
        from .domain import ensure_knowledge_gap

        ensure_knowledge_gap(
            db,
            employee.id,
            answer.data["question"],
            answer.id,
            result.get("reason", "INSUFFICIENT_EVIDENCE"),
        )
    return True


def fallback_result(reason):
    return {
        "mode": "HANDOFF",
        "text": "Вопрос принят. Уточняем информацию; ответ появится здесь.",
        "citations": [],
        "evidence": [],
        "reason": reason,
    }


def process_answer_job(db, job):
    answer = get_entity(db, job.payload["answer_id"], "answer", lock=True)
    if answer.status not in {"QUEUED", "RUNNING"}:
        return
    if aware(datetime.fromisoformat(answer.data["queue_wait_deadline_at"])) <= now():
        finish_answer(db, answer, fallback_result("LOCAL_QUEUE_TIMEOUT"))
        return
    answer.status = "RUNNING"
    started = now()
    answer.data = {
        **answer.data,
        "started_at": started.isoformat(),
        "queue_seconds": max(0, (started - aware(answer.created_at)).total_seconds()),
    }
    db.commit()
    conv = get_entity(db, answer.data["conversation_id"], "conversation")
    context = db.scalars(
        select(Message)
        .where(
            Message.conversation_id == conv.id,
            Message.seq < answer.data["context_upto_seq"],
        )
        .order_by(Message.seq.desc())
        .limit(6)
    ).all()
    context = list(reversed(context))
    try:
        from .retrieval import answer_question

        result = answer_question(db, conv, answer.data["question"], context=context)
    except Exception as error:
        log.error("answer_failed type=%s answer=%s", type(error).__name__, answer.id)
        result = fallback_result("LOCAL_RUNTIME_ERROR")
    db.expire_all()
    answer = get_entity(db, job.payload["answer_id"], "answer")
    if aware(datetime.fromisoformat(answer.data["deadline_at"])) <= now():
        result = fallback_result("LOCAL_DEADLINE")
    finish_answer(db, answer, result)


def record_model_health(db):
    s = get_settings()
    urls = {
        "encoder": os.getenv("EMBEDDER_URL", s.encoder_url),
        "reranker": os.getenv("RERANKER_URL", s.reranker_url),
        "parser": os.getenv("PARSER_URL", "http://parser:8000"),
    }
    if s.generation_enabled:
        urls["generator"] = os.getenv("GENERATOR_URL", s.generator_url)
    for service, url in urls.items():
        try:
            response = httpx.get(url.rstrip("/") + "/healthz", timeout=2)
            response.raise_for_status()
            value = response.json()
            loaded = value.get(
                "ready", value.get("status") in {"ok", "ready", "READY", "loaded"}
            )
            data = {
                "service": service,
                "ready": bool(loaded),
                "detail": value,
                "checked_at": now().isoformat(),
            }
        except Exception as error:
            data = {
                "service": service,
                "ready": False,
                "error_type": type(error).__name__,
                "checked_at": now().isoformat(),
            }
        item = next(
            (
                x
                for x in list_kind(db, "service_health")
                if x.data.get("service") == service
            ),
            None,
        )
        if item:
            item.data = data
        else:
            save_entity(db, "service_health", data)


def run_once(ingest=False):
    with SessionLocal() as db:
        claimed = claim_job(db, ingest)
    if not claimed:
        return False
    jid, token = claimed
    with SessionLocal() as db:
        job = db.get(Job, jid)
        try:
            handled = False
            if job.kind == "ANSWER":
                process_answer_job(db, job)
                handled = True
            elif job.kind == "MAX_UPDATE":
                from .max_events import process_max_update

                process_max_update(db, job)
                handled = True
            elif job.kind == "LOCAL_AI_VALIDATE":
                from .local_ai import validate_runtime_job

                handled = validate_runtime_job(db, job)
            elif job.kind == "RETENTION_SWEEP":
                from .operations import retention_sweep

                handled = retention_sweep(db, job)
            elif job.kind == "MODEL_HEALTH":
                record_model_health(db)
                handled = True
            elif job.kind == "OUTBOX_ACCEPTED":
                from .domain import on_outbox_accepted

                outbox = db.get(Outbox, job.payload["outbox_id"])
                if outbox and outbox.status == "ACCEPTED":
                    on_outbox_accepted(db, outbox)
                handled = True
            elif ingest:
                from .knowledge import process_ingest_job

                handled = process_ingest_job(db, job)
                if not handled:
                    from .knowledge_admin import process_knowledge_admin_job

                    handled = process_knowledge_admin_job(db, job)
            else:
                from .domain import process_domain_job

                handled = process_domain_job(db, job)
                if not handled:
                    from .analysis import process_analysis_job

                    handled = process_analysis_job(db, job)
            if not handled:
                raise RuntimeError("unknown_job_kind")
            db.flush()
            fresh = db.scalar(
                select(Job)
                .where(Job.id == jid)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if not fresh or fresh.payload.get("_lease_token") != token:
                db.rollback()
                return True
            fresh.status = "COMPLETED"
            fresh.lease_until = None
            fresh.error = None
            db.commit()
            return True
        except Exception as error:
            db.rollback()
            job = db.get(Job, jid)
            if job and job.payload.get("_lease_token") == token:
                job.error = type(error).__name__
                job.status = "FAILED" if job.attempts >= 8 else "RETRY"
                job.run_at = now() + timedelta(seconds=min(300, 5 * 2**job.attempts))
                job.lease_until = None
                db.commit()
            log.error("job_failed id=%s type=%s", jid, type(error).__name__)
            return True


def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    ingest = os.getenv("KORSIK_WORKER_KIND") == "ingest"
    while True:
        try:
            from .runtime_health import heartbeat

            heartbeat()
            if not run_once(ingest):
                time.sleep(0.5)
        except KeyboardInterrupt:
            break
        except Exception as error:
            log.error("worker_cycle_failed type=%s", type(error).__name__)
            time.sleep(2)


if __name__ == "__main__":
    main()
