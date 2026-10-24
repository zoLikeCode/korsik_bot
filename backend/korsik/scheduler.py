"""Replay-safe database scheduler, transport reconciler and deadline watchdog."""

import time, logging
from datetime import datetime
from sqlalchemy import select, text
from .db import SessionLocal, now, aware
from .models import Entity, Job, TransportInbox, TransportDelivery, Outbox
from .common import enqueue, save_entity, list_kind
from .settings import get_settings

log = logging.getLogger("korsik.scheduler")


def tick(db):
    if db.bind.dialect.name == "postgresql" and not db.scalar(
        text("SELECT pg_try_advisory_xact_lock(76810102)")
    ):
        return
    minute = int(now().timestamp() // 60)
    enqueue(db, "RETENTION_SWEEP", {}, f"retention:{int(now().timestamp()//86400)}")
    for inbox in db.scalars(
        select(TransportInbox)
        .where(TransportInbox.status == "PENDING")
        .limit(100)
        .with_for_update(skip_locked=True)
    ):
        enqueue(db, "MAX_UPDATE", {"inbox_id": inbox.id}, f"max-update:{inbox.id}")
        inbox.status = "ENQUEUED"
    from .domain import run_due_scenarios

    run_due_scenarios(db)
    from .survey_campaigns import tick as survey_campaign_tick

    survey_campaign_tick(db)
    from .broadcasts import tick as broadcast_tick

    broadcast_tick(db)
    enqueue(db, "DOMAIN_DASHBOARDS", {}, f"dashboard-refresh:{minute}")
    enqueue(db, "MODEL_HEALTH", {}, f"model-health:{minute}")
    # Model work never owns the expiry clock.
    from .worker import finish_answer, fallback_result

    for answer in db.scalars(
        select(Entity).where(
            Entity.kind == "answer", Entity.status.in_(["QUEUED", "RUNNING"])
        )
    ):
        deadline = (
            answer.data.get("queue_wait_deadline_at")
            if answer.status == "QUEUED"
            else answer.data.get("deadline_at")
        )
        if deadline and aware(datetime.fromisoformat(deadline)) <= now():
            finish_answer(
                db,
                answer,
                fallback_result(
                    "LOCAL_QUEUE_TIMEOUT"
                    if answer.status == "QUEUED"
                    else "LOCAL_DEADLINE"
                ),
            )
    for capture in db.scalars(
        select(Entity)
        .where(
            Entity.kind == "input_capture",
            Entity.status.in_(["OPEN", "DRAFT", "READY_TO_CONFIRM"]),
        )
        .with_for_update(skip_locked=True)
    ):
        expires = capture.data.get("expires_at")
        if expires and aware(datetime.fromisoformat(expires)) <= now():
            capture.status = "EXPIRED"
    for job in db.scalars(
        select(Job)
        .where(Job.status == "PROCESSING", Job.lease_until < now())
        .with_for_update(skip_locked=True)
    ):
        job.status = "FAILED" if job.attempts >= 8 else "RETRY"
        job.error = "LEASE_EXPIRED"
        job.run_at = now()
    for delivery in db.scalars(
        select(TransportDelivery)
        .where(
            TransportDelivery.status == "SENDING", TransportDelivery.lease_until < now()
        )
        .with_for_update(skip_locked=True)
    ):
        delivery.status = "UNKNOWN"
        delivery.error_code = "SEND_LEASE_EXPIRED"
        outbox = db.get(Outbox, delivery.outbox_id)
        if outbox:
            outbox.status = "UNKNOWN_DELIVERY"
    from .transport_api import prepare_deliveries

    prepare_deliveries(db)
    heartbeat = next(iter(list_kind(db, "scheduler_heartbeat")), None)
    if heartbeat:
        heartbeat.data = {"at": now().isoformat()}
    else:
        save_entity(db, "scheduler_heartbeat", {"at": now().isoformat()})


def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    while True:
        try:
            with SessionLocal() as db:
                tick(db)
                db.commit()
            from .runtime_health import heartbeat

            heartbeat()
        except KeyboardInterrupt:
            break
        except Exception as error:
            log.error("scheduler_cycle_failed type=%s", type(error).__name__)
        time.sleep(get_settings().scheduler_tick_seconds)


if __name__ == "__main__":
    main()
