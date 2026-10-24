"""One real scheduler cadence/queue timestamp check in an isolated restored DB."""

import json
import os
import time
from datetime import timedelta
from pathlib import Path
from sqlalchemy import select, func, text
from korsik.db import SessionLocal, now
from korsik.settings import get_settings
from korsik.models import Entity, User, Outbox, TransportDelivery
from korsik.common import save_entity
from korsik.auth import user_actor
from korsik import domain as d, scheduler

settings = get_settings()
if (
    not settings.database_name.startswith("korsik_offline_")
    or os.environ.get("KORSIK_SCHEDULER_PROBE_ALLOWED") != "ISOLATED_NO_MAX"
):
    raise RuntimeError("EXPLICIT_ISOLATED_OFFLINE_DATABASE_REQUIRED")
interval = settings.scheduler_tick_seconds
if not 0 < interval <= 30:
    raise RuntimeError("NORMAL_BOUNDED_SCHEDULER_INTERVAL_REQUIRED")
with SessionLocal() as db:
    assert db.scalar(text("select current_database()")) == settings.database_name
    admin = db.scalar(
        select(User).where(User.role == "HR_ADMIN", User.enabled.is_(True))
    )
    employee = d.create_employee_record(
        db,
        user_actor(admin),
        {
            "name": "Synthetic due-time measurement",
            "site_id": "GrPP",
            "start_date": now().date().isoformat(),
            "employment_type": "OFFICE",
        },
    )
    for old in d.entities(db, "journey", employee.id):
        old.status = "CANCELLED"
    d.update(employee, employment_stage="ACTIVE", max_binding_status="VERIFIED")
    due = now() + timedelta(seconds=2)
    step = {
        "step_key": "synthetic-due",
        "sequence_no": 1,
        "kind": "MESSAGE",
        "mandatory": True,
        "content": {
            "text_ty": "Synthetic scheduler queue measurement",
            "text_vy": "Synthetic scheduler queue measurement",
        },
        "delivery_policy": {
            "quiet_hours_policy": "KEEP",
            "late_policy": "send_once_on_join",
        },
        "transitions": [],
    }
    program = save_entity(db, "scenario", {"name": "Synthetic scheduler check only"})
    version = save_entity(
        db,
        "scenario_version",
        {"graph": {"start_step_key": step["step_key"], "steps": [step]}},
        status="PUBLISHED",
        parent_id=program.id,
    )
    journey = save_entity(
        db,
        "journey",
        {
            "employee_id": employee.id,
            "episode_id": employee.data["episode_id"],
            "scenario_id": program.id,
            "version_id": version.id,
            "schedule_revision": 1,
            "answers": {},
            "signals": {},
            "events": {},
            "completed_steps": [],
        },
        status="ACTIVE",
        parent_id=employee.id,
    )
    action = save_entity(
        db,
        "scheduled_action",
        {
            "employee_id": employee.id,
            "journey_id": journey.id,
            "version_id": version.id,
            "step_key": step["step_key"],
            "sequence_no": 1,
            "due_at": due.isoformat(),
            "original_due_at": due.isoformat(),
            "schedule_revision": 1,
            "step": step,
            "applicable": True,
        },
        status="PLANNED",
        parent_id=journey.id,
    )
    employee_id, action_id = employee.id, action.id
    db.commit()
started = time.monotonic()
count = 0
outbox = None
while time.monotonic() - started <= 65:
    with SessionLocal() as db:
        scheduler.tick(db)
        db.commit()
        count += 1
        rows = list(
            db.scalars(select(Outbox).where(Outbox.recipient_id == employee_id))
        )
        if rows:
            assert len(rows) == 1, "DUPLICATE_LOGICAL_SCHEDULED_MESSAGE"
            outbox = rows[0]
            break
    time.sleep(interval)
with SessionLocal() as db:
    deliveries = db.scalar(
        select(func.count(TransportDelivery.id))
        .join(Outbox, Outbox.id == TransportDelivery.outbox_id)
        .where(Outbox.recipient_id == employee_id)
    )
    fresh = db.get(Outbox, outbox.id) if outbox else None
    queued = d.aware(fresh.created_at) if fresh else None
    delay = (queued - due).total_seconds() if queued else None
    report = {
        "schema": "korsik-scheduler-due-v1",
        "database": settings.database_name,
        "fixture": "SYNTHETIC_NO_ACTUAL_MAX_BINDING",
        "due_at": due.isoformat(),
        "outbox_created_at": queued.isoformat() if queued else None,
        "queue_delay_seconds": delay,
        "scheduler_tick_interval_seconds": interval,
        "actual_ticks": count,
        "outbox_status": fresh.status if fresh else None,
        "external_MAX_sends": 0,
        "fixture_deliveries": deliveries,
        "limit_seconds": 60,
        "status": (
            "PASS"
            if delay is not None and 0 <= delay <= 60 and deliveries == 0
            else "FAIL"
        ),
        "limitation": "One actual normal-cadence synthetic sample in the restored database, not a peak-capacity or MAX-delivery latency proof",
    }
    if fresh:
        fresh.status = "SUPPRESSED"
        fresh.data = {
            **fresh.data,
            "reason_code": "SYNTHETIC_SCHEDULER_PROOF_COMPLETED",
        }
    employee = db.get(Entity, employee_id)
    d.update(employee, max_binding_status="UNBOUND")
    db.commit()
Path("/operator/scheduler-due-report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report), flush=True)
if report["status"] != "PASS":
    raise SystemExit(1)
