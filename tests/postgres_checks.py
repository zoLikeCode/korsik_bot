"""Isolated PostgreSQL integration/fault checks; never run against the product DB.

Run only after a schema-only copy into korsik_integration_check. The caller
creates and drops that database. This script inserts synthetic fixtures only.
"""

from __future__ import annotations
import hashlib
import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from sqlalchemy import create_engine, select, delete, func, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

EXPECTED_DATABASE = "korsik_integration_check"
REPORT_PATH = Path(
    os.environ.get("KORSIK_PG_REPORT", "/tmp/korsik-postgres-check-report.json")
)


def main():
    from korsik.settings import get_settings
    from korsik.models import (
        Entity,
        User,
        Job,
        Message,
        Outbox,
        TransportInbox,
        TransportDelivery,
    )
    from korsik.common import save_entity, get_entity, etag
    from korsik.auth import Actor
    from korsik.db import now, new_id
    from korsik.seed import seed_defaults, default_graph
    from korsik import domain as d, chat, max_events as mx, transport_api as tx, worker
    from starlette.requests import Request
    from fastapi import HTTPException

    source_url = make_url(get_settings().database_url)
    if source_url.get_backend_name() != "postgresql":
        raise RuntimeError("POSTGRES_REQUIRED")
    test_url = source_url.set(database=EXPECTED_DATABASE)
    if test_url.database != EXPECTED_DATABASE:
        raise RuntimeError("UNSAFE_DATABASE")
    engine = create_engine(
        test_url,
        pool_size=8,
        max_overflow=2,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={
            "options": "-c statement_timeout=12000 -c lock_timeout=7000 -c idle_in_transaction_session_timeout=20000"
        },
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        if db.scalar(text("select current_database()")) != EXPECTED_DATABASE:
            raise RuntimeError("UNSAFE_DATABASE")
        server_version = db.scalar(text("SELECT version()"))
        seed_defaults(db)
        user = User(
            id=new_id(),
            email="synthetic-pg-hr@integration.invalid",
            name="Synthetic PG HR",
            role="HR_ADMIN",
            scope={"all": True},
            password_hash="no-login-test-account",
            enabled=True,
        )
        db.add(user)
        db.commit()
        hr = Actor(user.id, user.name, user.role, user.scope)
    report = {
        "database": EXPECTED_DATABASE,
        "source_data_copied": False,
        "synthetic_fixtures_only": True,
        "external_model_requests": 0,
        "postgresql_server_version": server_version,
        "checks": [],
    }

    def check(name, fn):
        started = time.monotonic()
        try:
            detail = fn() or {}
            report["checks"].append(
                {
                    "name": name,
                    "status": "PASS",
                    "duration_seconds": round(time.monotonic() - started, 3),
                    **detail,
                }
            )
        except Exception as error:
            report["checks"].append(
                {
                    "name": name,
                    "status": "FAIL",
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "error_type": type(error).__name__,
                    "assertion": (
                        str(error)[:160] if isinstance(error, AssertionError) else None
                    ),
                }
            )

    def create_employee(label):
        with factory() as db:
            e = d.create_employee_record(
                db,
                hr,
                {
                    "name": "Synthetic " + label,
                    "site_id": "GrPP",
                    "start_date": now().date().isoformat(),
                    "employment_type": "OFFICE",
                },
            )
            db.commit()
            return e.id

    def make_actor(e):
        return Actor(
            e.data.get("user_id") or e.id, e.data["name"], "EMPLOYEE", {}, e.id
        )

    def bind_employee(eid, uid):
        with factory() as db:
            e = get_entity(db, eid, "employee")
            b = save_entity(
                db,
                "max_binding",
                {
                    "employee_id": e.id,
                    "episode_id": e.data["episode_id"],
                    "max_user_id": str(uid),
                    "max_chat_id": "1",
                },
                status="ACTIVE",
                parent_id=e.id,
            )
            d.update(e, binding_id=b.id, max_binding_status="VERIFIED")
            conv = chat.conversation_for(db, e)
            db.commit()
            return b.id, conv.id

    def parallel(functions):
        barrier = threading.Barrier(len(functions))

        def invoke(fn):
            return fn(barrier)

        with ThreadPoolExecutor(max_workers=len(functions)) as pool:
            futures = [pool.submit(invoke, fn) for fn in functions]
            return [f.result(timeout=20) for f in futures]

    def request(entity):
        return Request(
            {"type": "http", "headers": [(b"if-match", etag(entity).encode())]}
        )

    def conversation_creation():
        eid = create_employee("conversation creation")

        def create(barrier):
            with factory() as db:
                e = get_entity(db, eid, "employee")
                barrier.wait(8)
                c = chat.conversation_for(db, e)
                db.commit()
                return c.id

        ids = parallel([create, create])
        with factory() as db:
            count = db.scalar(
                select(func.count(Entity.id)).where(
                    Entity.kind == "conversation", Entity.parent_id == eid
                )
            )
        assert ids[0] == ids[1] and count == 1, "DUPLICATE_EPISODE_CONVERSATION"
        return {"conversation_count": count}

    check("concurrent_episode_conversation_creation", conversation_creation)

    def duplicate_turn():
        eid = create_employee("duplicate turn")
        with factory() as db:
            c = chat.conversation_for(db, get_entity(db, eid, "employee"))
            cid = c.id
            db.commit()
        client = str(uuid.uuid4())

        def submit(barrier):
            with factory() as db:
                c = get_entity(db, cid, "conversation")
                e = get_entity(db, eid, "employee")
                barrier.wait(8)
                result = chat.submit_turn(
                    db,
                    make_actor(e),
                    c,
                    {"kind": "TEXT", "text": "Как оформить пропуск?"},
                    client,
                )
                db.commit()
                return result["id"]

        values = parallel([submit, submit])
        with factory() as db:
            turns = db.scalar(
                select(func.count(Entity.id)).where(
                    Entity.kind == "conversation_turn", Entity.parent_id == cid
                )
            )
            messages = db.scalar(
                select(func.count(Message.id)).where(Message.conversation_id == cid)
            )
        assert (
            values[0] == values[1] and turns == 1 and messages == 1
        ), "DUPLICATE_TYPED_TURN_SIDE_EFFECT"
        return {"turns": turns, "messages": messages}

    check("concurrent_duplicate_typed_turn", duplicate_turn)

    def distinct_turn_sequence():
        eid = create_employee("distinct turns")
        with factory() as db:
            c = chat.conversation_for(db, get_entity(db, eid, "employee"))
            cid = c.id
            db.commit()

        def submit(barrier):
            with factory() as db:
                c = get_entity(db, cid, "conversation")
                e = get_entity(db, eid, "employee")
                barrier.wait(8)
                value = chat.submit_turn(
                    db,
                    make_actor(e),
                    c,
                    {"kind": "TEXT", "text": "Какой следующий этап?"},
                    str(uuid.uuid4()),
                )
                db.commit()
                return value["id"]

        values = parallel([submit, submit])
        with factory() as db:
            seq = list(
                db.scalars(
                    select(Message.seq)
                    .where(Message.conversation_id == cid)
                    .order_by(Message.seq)
                )
            )
        assert (
            len(set(values)) == 2
            and seq == list(range(1, len(seq) + 1))
            and len(seq) >= 2
        ), "MESSAGE_SEQUENCE_COLLISION"
        return {"messages": len(seq), "contiguous_sequence": True}

    check("concurrent_distinct_turn_message_sequence", distinct_turn_sequence)

    def duplicate_max_update():
        eid = create_employee("duplicate MAX")
        _, cid = bind_employee(eid, 771001)
        event_id = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
        with factory() as db:
            db.add(
                TransportInbox(
                    id=event_id,
                    update_type="message_created",
                    status="ENQUEUED",
                    payload={
                        "update_type": "message_created",
                        "message": {
                            "sender": {"user_id": 771001},
                            "recipient": {"chat_type": "dialog", "chat_id": 1},
                            "body": {"text": "Где найти правила?"},
                        },
                    },
                )
            )
            db.commit()

        def process(barrier):
            with factory() as db:
                db.get(TransportInbox, event_id)
                barrier.wait(8)
                mx.process_max_update(
                    db, SimpleNamespace(payload={"inbox_id": event_id})
                )
                db.commit()
                return True

        parallel([process, process])
        with factory() as db:
            turns = db.scalar(
                select(func.count(Entity.id)).where(
                    Entity.kind == "conversation_turn", Entity.parent_id == cid
                )
            )
            messages = db.scalar(
                select(func.count(Message.id)).where(Message.conversation_id == cid)
            )
            status = db.get(TransportInbox, event_id).status
        assert (
            turns == 1 and messages == 1 and status == "DONE"
        ), "DUPLICATE_MAX_EVENT_SIDE_EFFECT"
        return {"turns": turns, "messages": messages}

    check("concurrent_duplicate_MAX_update", duplicate_max_update)

    def one_capture_across_legacy_conversations():
        eid = create_employee("capture aggregate")
        with factory() as db:
            e = get_entity(db, eid, "employee")
            chat.conversation_for(db, e)
            save_entity(
                db,
                "conversation",
                {
                    "employee_id": eid,
                    "episode_id": e.data["episode_id"],
                    "mode": "BOT",
                    "message_seq": 0,
                },
                parent_id=eid,
            )
            journey = d.entities(db, "journey", eid)[0]
            questions = []
            for i in range(2):
                q = {
                    "question_key": "PG-CAPTURE-" + str(i),
                    "type": "TEXT",
                    "prompt": {
                        "text_ty": "Synthetic question",
                        "text_vy": "Synthetic question",
                    },
                    "required": True,
                    "allow_decline": True,
                    "source_requiredness": "REQUIRED",
                    "max_text_length": 1000,
                }
                questions.append(
                    save_entity(
                        db,
                        "scenario_question",
                        {
                            "employee_id": eid,
                            "step_key": "pg-capture-" + str(i),
                            "question": q,
                        },
                        status="OPEN",
                        parent_id=journey.id,
                    ).id
                )
            db.commit()

        def open_target(index):
            def run(barrier):
                with factory() as db:
                    e = get_entity(db, eid, "employee")
                    barrier.wait(8)
                    try:
                        d.open_capture(
                            db,
                            make_actor(e),
                            {"scenario_question_id": questions[index]},
                        )
                        db.commit()
                        return 201
                    except HTTPException as error:
                        db.rollback()
                        return error.status_code

            return run

        statuses = parallel([open_target(0), open_target(1)])
        with factory() as db:
            count = db.scalar(
                select(func.count(Entity.id)).where(
                    Entity.kind == "input_capture",
                    Entity.parent_id == eid,
                    Entity.status.in_(["OPEN", "DRAFT"]),
                )
            )
        assert (
            sorted(statuses) == [201, 409] and count == 1
        ), "MORE_THAN_ONE_ACTIVE_CAPTURE"
        return {"active_captures": count, "statuses": sorted(statuses)}

    check(
        "single_capture_for_employee_across_legacy_conversations",
        one_capture_across_legacy_conversations,
    )

    def stale_capture_after_human_resume():
        eid = create_employee("stale capture")
        _, cid = bind_employee(eid, 771002)
        with factory() as db:
            e = get_entity(db, eid, "employee")
            c = get_entity(db, cid, "conversation")
            journey = d.entities(db, "journey", eid)[0]
            step = next(
                s
                for s in default_graph()["steps"]
                if s.get("survey", {}).get("survey_key") == "SUR-M1"
            )
            run = d.open_survey(db, journey, step, e)
            cap = d.open_capture(db, make_actor(e), {"survey_run_id": run.id})
            d.capture_draft(db, make_actor(e), cap.id, {"text": "1"})
            capid = cap.id
            fingerprint = cap.data["draft_fingerprint"]
            case = chat.handoff(db, make_actor(e), c, "Synthetic handoff")
            db.commit()
            d.claim_case(case["id"], db, hr)
            c = get_entity(db, cid, "conversation")
            chat.resume_bot(cid, request(c), hr, db)
            try:
                d.confirm_capture(
                    db, make_actor(e), capid, {"draft_fingerprint": fingerprint}
                )
            except HTTPException as error:
                assert error.status_code == 409
            else:
                raise AssertionError("STALE_CAPTURE_CONFIRMED_AFTER_HUMAN_RESUME")
            assert not run.data["answers"], "STALE_CAPTURE_WROTE_SURVEY_ANSWER"
        return {"old_capture_rejected": True}

    check(
        "stale_capture_callback_after_HUMAN_and_resume",
        stale_capture_after_human_resume,
    )

    def two_scheduler_leaders():
        eid = create_employee("scheduler replay")
        bind_employee(eid, 771003)
        with factory() as db:
            e = get_entity(db, eid, "employee")
            d.update(e, employment_stage="ACTIVE")
            journey = d.entities(db, "journey", eid)[0]
            for action in d.entities(db, "scheduled_action", journey.id):
                step = {
                    **action.data["step"],
                    "delivery_policy": {
                        **action.data["step"].get("delivery_policy", {}),
                        "quiet_hours_policy": "KEEP",
                    },
                }
                d.update(
                    action, step=step, due_at=(now() - timedelta(minutes=1)).isoformat()
                )
            db.commit()

        def tick(barrier):
            with factory() as db:
                barrier.wait(8)
                d.run_due_scenarios(db)
                db.commit()
                return True

        parallel([tick, tick])
        with factory() as db:
            rows = list(db.scalars(select(Outbox).where(Outbox.recipient_id == eid)))
            keys = [o.unique_key for o in rows]
        assert keys and len(keys) == len(set(keys)), "SCHEDULER_DUPLICATE_OUTBOX"
        return {"outbox_count": len(keys), "unique_keys": len(set(keys))}

    check("two_scheduler_leaders_no_duplicate_outbox", two_scheduler_leaders)

    def lease_fencing_fault():
        marker = "pg-lease-side-effect-" + new_id()
        with factory() as db:
            db.execute(delete(Job))
            db.add(
                Job(
                    id=new_id(),
                    kind="PG_LEASE_FAULT",
                    payload={},
                    status="PENDING",
                    unique_key="pg-lease-fault",
                    run_at=now(),
                )
            )
            db.commit()
        original_factory = worker.SessionLocal
        original_handler = d.process_domain_job
        new_token = []

        def injected(db, job):
            save_entity(db, "pg_test_probe", {"marker": marker})
            with factory() as other:
                fresh = other.get(Job, job.id)
                fresh.status = "RETRY"
                fresh.run_at = now()
                fresh.lease_until = now() - timedelta(seconds=1)
                other.commit()
                claim = worker.claim_job(other)
                new_token.append(claim[1])
            return True

        worker.SessionLocal = factory
        d.process_domain_job = injected
        try:
            assert worker.run_once() is True
        finally:
            worker.SessionLocal = original_factory
            d.process_domain_job = original_handler
        with factory() as db:
            count = db.scalar(
                select(func.count(Entity.id)).where(Entity.kind == "pg_test_probe")
            )
            job = db.scalar(select(Job).where(Job.unique_key == "pg-lease-fault"))
            assert (
                count == 0
                and job.status == "PROCESSING"
                and job.payload["_lease_token"] == new_token[0]
            ), "EXPIRED_WORKER_COMMITTED_NEW_LEASE_OR_SIDE_EFFECT"
        return {"stale_side_effects_rolled_back": True, "new_lease_preserved": True}

    check("worker_lease_fencing_rolls_back_stale_side_effect", lease_fencing_fault)

    def unknown_receipt_no_retry():
        eid = create_employee("unknown delivery")
        _, cid = bind_employee(eid, 771004)
        with factory() as db:
            out = d.emit(db, eid, "Synthetic delivery", unique_key="pg-unknown")
            db.commit()
            tx.prepare_deliveries(db)
            db.commit()
            delivery = db.scalar(
                select(TransportDelivery).where(TransportDelivery.outbox_id == out.id)
            )
            claim = tx.claim_delivery(delivery.id, db)
            tx.delivery_receipt(
                delivery.id,
                tx.DeliveryReceipt(attempt_id=claim["attempt_id"], status="UNKNOWN"),
                db,
            )
            tx.delivery_receipt(
                delivery.id,
                tx.DeliveryReceipt(attempt_id=claim["attempt_id"], status="RETRY"),
                db,
            )
            assert (
                delivery.status == "UNKNOWN" and out.status == "UNKNOWN_DELIVERY"
            ), "UNKNOWN_RETRIED"
            try:
                tx.claim_delivery(delivery.id, db)
            except HTTPException as error:
                assert error.status_code == 409
            else:
                raise AssertionError("UNKNOWN_DELIVERY_RECLAIMED")
        return {"manual_resolution_required": True}

    check("UNKNOWN_delivery_is_not_retried", unknown_receipt_no_retry)

    def concurrent_local_profile_switch():
        from korsik import local_ai, retrieval
        from korsik.common import fingerprint
        from tempfile import TemporaryDirectory
        from unittest.mock import patch

        # This check measures real PostgreSQL activation serialization only.
        # Files, semantic PASSED rows and health responses are explicit fixtures,
        # not evidence of a working decoder or a model-quality review.
        with TemporaryDirectory(prefix="korsik-pg-model-fixture-") as temporary:
            root = Path(temporary)
            health = {}
            for role in ("embedder", "reranker", "generator"):
                bundle = root / role
                bundle.mkdir()
                encoded = json.dumps(
                    {
                        "repository": "synthetic-" + role,
                        "revision": "pg-lock-fixture",
                        "files": [],
                    }
                ).encode()
                (bundle / "manifest.json").write_bytes(encoded)
                artifact_fp = hashlib.sha256(encoded).hexdigest()
                health[role] = {"status": "READY", "fingerprint": artifact_fp}
                if role == "generator":
                    runtime_fp = local_ai.pipeline_identities()[
                        "generation_runtime_source_fingerprint"
                    ]
                    health[role]["renderer_fingerprint"] = runtime_fp
                    (bundle / "quality-report.json").write_text(
                        json.dumps(
                            {
                                "status": "PASS",
                                "artifact_fingerprint": artifact_fp,
                                "reviewer": "SYNTHETIC_LOCK_FIXTURE_NOT_MODEL_REVIEW",
                                "cases": [],
                                "pipeline_source_sha256": hashlib.sha256(
                                    Path(retrieval.__file__).read_bytes()
                                ).hexdigest(),
                                "generation_runtime_source_sha256": runtime_fp,
                            }
                        )
                    )
            settings = SimpleNamespace(
                model_manifest_dir=str(root),
                generator_model_subdir="generator",
                product_mode="FULL",
                generation_enabled=True,
            )
            endpoints = {
                "embedder": retrieval.EMBEDDER,
                "reranker": retrieval.RERANKER,
                "generator": retrieval.GENERATOR,
            }

            def fixture_health(url, payload=None, method="POST", timeout=30):
                role = next(
                    (
                        key
                        for key, value in endpoints.items()
                        if url == value + "/healthz"
                    ),
                    None,
                )
                assert (
                    role and method == "GET" and payload is None
                ), "NO_MODEL_HTTP_IN_PG_LOCK_FIXTURE"
                return health[role]

            with patch.object(
                local_ai, "get_settings", return_value=settings
            ), patch.object(retrieval, "_request", side_effect=fixture_health):
                with factory() as db:
                    for answer in db.scalars(
                        select(Entity).where(
                            Entity.kind == "answer",
                            Entity.status.in_(["QUEUED", "RUNNING"]),
                        )
                    ):
                        answer.status = "CANCELLED"
                    profiles = []
                    for label in ("A", "B"):
                        result = local_ai.create_profile(
                            {"name": "Synthetic lock " + label}, db, hr
                        )
                        row = db.get(Entity, result["id"])
                        save_entity(
                            db,
                            "local_ai_validation",
                            {
                                "configuration_fingerprint": fingerprint(
                                    row.data["configuration"]
                                ),
                                "scope": "SYNTHETIC_LOCK_TEST_ONLY_NOT_MODEL_ACCEPTANCE",
                            },
                            status="PASSED",
                            parent_id=row.id,
                        )
                        profiles.append(row.id)
                    db.commit()

                def switch(profile_id):
                    def invoke(barrier):
                        with factory() as db:
                            profile = get_entity(db, profile_id, "local_ai_profile")
                            pinned = request(profile)
                            barrier.wait(8)
                            result = local_ai.activate_profile(
                                profile_id, {}, pinned, db, hr
                            )
                            return result["profile_id"]

                    return invoke

                results = parallel([switch(pid) for pid in profiles])
                with factory() as db:
                    controls = d.entities(db, "local_ai_control")
                    active = [
                        r
                        for r in d.entities(db, "local_ai_profile")
                        if r.status == "ACTIVE"
                    ]
                    assert (
                        len(controls) == 1 and len(active) == 1
                    ), "MULTIPLE_ACTIVE_MODEL_PROFILES"
                    assert (
                        controls[0].data["active_profile_id"] == active[0].id
                    ), "MODEL_CONTROL_POINTER_MISMATCH"
        return {
            "switches_committed": len(results),
            "active_profiles": 1,
            "synthetic_quality_rows_only": True,
            "runtime_health_fixture": "EXPLICIT_CURRENT_MANIFESTS_AND_STUBBED_HEALTH_NO_HTTP",
        }

    check("concurrent_local_AI_profile_activation", concurrent_local_profile_switch)

    def concurrent_unique_helpers():
        from korsik.common import enqueue, queue_message

        key = "pg-helper-concurrent:" + new_id()

        def write(barrier):
            with factory() as db:
                barrier.wait(8)
                job = enqueue(db, "SYNTHETIC_CHECK_ONLY", {"fixture": True}, key)
                out = queue_message(db, "USER", hr.id, "Synthetic helper fixture", key)
                db.commit()
                return job.id, out.id

        pairs = parallel([write, write])
        assert pairs[0] == pairs[1], "DUPLICATE_HELPER_OBJECTS"
        with factory() as db:
            jobs = db.scalar(select(func.count(Job.id)).where(Job.unique_key == key))
            outboxes = db.scalar(
                select(func.count(Outbox.id)).where(Outbox.unique_key == key)
            )
        assert jobs == 1 and outboxes == 1, "HELPER_UNIQUE_KEY_RACE"
        return {
            "logical_jobs": jobs,
            "logical_outboxes": outboxes,
            "both_transactions_committed": True,
        }

    check("concurrent_unique_job_and_outbox_helpers", concurrent_unique_helpers)

    def process_crash_and_scheduler_reclaim():
        import multiprocessing
        import signal
        from korsik import scheduler
        from korsik.common import enqueue

        marker = "process-crash:" + new_id()
        with factory() as db:
            # The guarded isolated database contains synthetic jobs only.
            db.execute(delete(Job))
            job = enqueue(db, "PG_PROCESS_CRASH_ONLY", {"marker": marker}, marker)
            job_id = job.id
            db.commit()
        context = multiprocessing.get_context("fork")
        parent_pipe, child_pipe = context.Pipe(duplex=False)

        def crashing_worker():
            child_engine = create_engine(
                test_url,
                pool_pre_ping=True,
                connect_args={
                    "options": "-c statement_timeout=12000 -c lock_timeout=7000"
                },
            )
            child_factory = sessionmaker(bind=child_engine, expire_on_commit=False)
            worker.SessionLocal = child_factory

            def handler(db, running):
                if running.kind != "PG_PROCESS_CRASH_ONLY":
                    return False
                save_entity(db, "pg_process_probe", {"marker": marker})
                db.flush()
                child_pipe.send({"started": True, "job_id": running.id})
                time.sleep(30)
                return True

            d.process_domain_job = handler
            worker.run_once()

        process = context.Process(target=crashing_worker)
        process.start()
        try:
            assert parent_pipe.poll(
                10
            ), "CRASH_WORKER_DID_NOT_REACH_UNCOMMITTED_HANDLER"
            notice = parent_pipe.recv()
            assert notice["job_id"] == job_id
            os.kill(process.pid, signal.SIGKILL)
            process.join(timeout=8)
            assert process.exitcode == -signal.SIGKILL, "WORKER_NOT_KILLED"
            with factory() as db:
                count = db.scalar(
                    select(func.count(Entity.id)).where(
                        Entity.kind == "pg_process_probe"
                    )
                )
                claimed = db.get(Job, job_id)
                assert (
                    count == 0 and claimed.status == "PROCESSING"
                ), "CRASH_COMMITTED_UNFINISHED_WORK"
                claimed.lease_until = now() - timedelta(seconds=1)
                db.commit()
            with factory() as db:
                scheduler.tick(db)
                db.commit()
                reclaimed = db.get(Job, job_id)
                assert (
                    reclaimed.status == "RETRY"
                ), "SCHEDULER_DID_NOT_RECLAIM_EXPIRED_PROCESS"
                db.execute(delete(Job).where(Job.id != job_id))
                db.commit()
            original_factory, original_handler = (
                worker.SessionLocal,
                d.process_domain_job,
            )

            def replay_handler(db, running):
                if running.kind != "PG_PROCESS_CRASH_ONLY":
                    return False
                save_entity(db, "pg_process_probe", {"marker": marker})
                return True

            try:
                worker.SessionLocal = factory
                d.process_domain_job = replay_handler
                assert worker.run_once()
                assert worker.run_once() is False
            finally:
                worker.SessionLocal, d.process_domain_job = (
                    original_factory,
                    original_handler,
                )
            with factory() as db:
                row = db.get(Job, job_id)
                count = db.scalar(
                    select(func.count(Entity.id)).where(
                        Entity.kind == "pg_process_probe"
                    )
                )
                assert (
                    row.status == "COMPLETED" and row.attempts == 2 and count == 1
                ), "CRASH_RETRY_DID_NOT_COMMIT_EXACTLY_ONE_LOGICAL_EFFECT"
            return {
                "worker_signal": "SIGKILL",
                "uncommitted_side_effect_rolled_back": True,
                "scheduler_reclaimed_lease": True,
                "worker_attempts": 2,
                "committed_effects": 1,
                "external_MAX_sends": 0,
            }
        finally:
            if process.is_alive():
                process.kill()
                process.join(timeout=5)
            parent_pipe.close()
            child_pipe.close()

    check(
        "worker_process_SIGKILL_and_scheduler_reclaim",
        process_crash_and_scheduler_reclaim,
    )

    def cancel_and_checkpoint_lock_order():
        job_locked = threading.Event()
        cancel_started = threading.Event()
        with factory() as db:
            operation = save_entity(
                db,
                "operation",
                {"type": "REPORT_EXPORT", "export_scope": {"all": True}},
                status="PENDING",
                owner_id=hr.id,
            )
            job = Job(
                id=new_id(),
                kind="DOMAIN_REPORT_EXPORT",
                payload={"operation_id": operation.id},
                unique_key="pg-cancel-order:" + operation.id,
                status="PENDING",
                run_at=now(),
            )
            db.add(job)
            db.commit()
            op_id, job_id, pinned_request = operation.id, job.id, request(operation)

        def checkpoint():
            with factory() as db:
                job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
                job_locked.set()
                assert cancel_started.wait(8)
                time.sleep(0.15)
                operation = get_entity(db, op_id, "operation", lock=True)
                job.status = "PROCESSING"
                operation.status = "RUNNING"
                db.commit()
                return True

        def cancel():
            assert job_locked.wait(8)
            with factory() as db:
                cancel_started.set()
                try:
                    d.operation_cancel(
                        op_id,
                        {"reason": "Synthetic concurrent cancellation"},
                        pinned_request,
                        db,
                        hr,
                    )
                    return 200
                except HTTPException as error:
                    db.rollback()
                    return error.status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            worker_future = pool.submit(checkpoint)
            cancel_future = pool.submit(cancel)
            assert worker_future.result(timeout=15)
            status = cancel_future.result(timeout=15)
        assert status in {200, 412}, "CANCEL_AND_CHECKPOINT_DEADLOCKED_OR_FAILED"
        with factory() as db:
            operation = get_entity(db, op_id, "operation")
            if operation.status != "CANCELLED":
                d.operation_cancel(
                    op_id,
                    {"reason": "Retry using fresh operation revision"},
                    request(operation),
                    db,
                    hr,
                )
            job = db.get(Job, job_id)
            assert operation.status == "CANCELLED" and operation.data.get(
                "cancel_requested"
            ), "CANCEL_FLAG_LOST"
            assert job.status == "PROCESSING", "CANCEL_OVERWROTE_FRESH_PROCESSING_CLAIM"
        return {
            "initial_cancel_status": status,
            "safe_stale_retry": True,
            "job_lock_precedes_operation": True,
            "processing_claim_preserved": True,
        }

    check(
        "concurrent_operation_cancel_and_checkpoint_lock_order",
        cancel_and_checkpoint_lock_order,
    )

    def administrative_retry_cannot_overwrite_claim():
        from korsik import main as api_main

        locked = threading.Event()
        retry_started = threading.Event()
        with factory() as db:
            row = Job(
                id=new_id(),
                kind="SYNTHETIC_RETRY_RACE",
                payload={},
                unique_key="pg-admin-retry:" + new_id(),
                status="RETRY",
                run_at=now(),
            )
            db.add(row)
            db.commit()
            job_id = row.id
        previous_factory = api_main.SessionLocal
        api_main.SessionLocal = factory
        try:

            def worker_claim():
                with factory() as db:
                    job = db.scalar(
                        select(Job).where(Job.id == job_id).with_for_update()
                    )
                    locked.set()
                    assert retry_started.wait(8)
                    time.sleep(0.2)
                    job.status = "PROCESSING"
                    job.payload = {"_lease_token": "fresh-worker-claim"}
                    job.lease_until = now() + timedelta(minutes=1)
                    db.commit()

            def administrative_retry():
                assert locked.wait(8)
                retry_started.set()
                try:
                    api_main.retry_job(job_id, hr)
                    return 200
                except HTTPException as error:
                    return error.status_code

            with ThreadPoolExecutor(max_workers=2) as pool:
                worker_future = pool.submit(worker_claim)
                admin_future = pool.submit(administrative_retry)
                worker_future.result(timeout=15)
                result = admin_future.result(timeout=15)
            assert result == 409, "ADMIN_RETRY_OVERWROTE_ACTIVE_WORKER_CLAIM"
            with factory() as db:
                job = db.get(Job, job_id)
                assert (
                    job.status == "PROCESSING"
                    and job.payload.get("_lease_token") == "fresh-worker-claim"
                )
            return {"retry_status": result, "fresh_worker_claim_preserved": True}
        finally:
            api_main.SessionLocal = previous_factory

    check(
        "administrative_retry_cannot_overwrite_concurrent_worker_claim",
        administrative_retry_cannot_overwrite_claim,
    )

    def reverse_multi_member_publication():
        from korsik import knowledge as kb, retrieval
        from fastapi import Response

        previous_mode, previous_request = retrieval.MODE, retrieval._request
        retrieval.MODE = "CORE"

        def offline_model_boundary(*args, **kwargs):
            raise RuntimeError("SYNTHETIC_CORE_LOCK_TEST_NO_MODEL_HTTP")

        retrieval._request = offline_model_boundary
        try:
            with factory() as db:
                revisions, documents = [], []
                for index in range(2):
                    doc_value = kb.create_document(
                        {
                            "title": "Synthetic concurrent publication " + str(index),
                            "scope": {"site_ids": ["GrPP"]},
                            "document_category": "REGULATION",
                        },
                        Response(),
                        hr,
                        db,
                    )
                    document = get_entity(db, doc_value["id"], "document")
                    value = kb.create_article(
                        document.id,
                        {
                            "text": "Синтетический локальный текст для атомарной публикации. Индекс "
                            + str(index)
                        },
                        request(document),
                        Response(),
                        hr,
                        db,
                    )
                    revision = get_entity(db, value["id"], "document_revision")
                    extraction = kb._extraction(db, revision)
                    kb.review_page(
                        revision.id,
                        1,
                        {
                            "action": "REVIEW_CONTENT",
                            "reason": "Synthetic source reviewed",
                            "expected_extraction_revision_id": extraction.id,
                        },
                        request(extraction),
                        Response(),
                        hr,
                        db,
                    )
                    revisions.append(revision.id)
                    documents.append(document.id)
                primary = get_entity(db, revisions[0], "document_revision")
                candidate_op = kb.build_candidate(
                    primary.id,
                    {"additional_revisions": [{"revision_id": revisions[1]}]},
                    request(primary),
                    hr,
                    db,
                )
                job = db.scalar(
                    select(Job).where(
                        Job.payload["operation_id"].as_string() == candidate_op["id"]
                    )
                )
                assert kb.process_ingest_job(db, job)
                candidate_operation = get_entity(db, candidate_op["id"], "operation")
                assert (
                    candidate_operation.status == "SUCCEEDED"
                ), candidate_operation.data.get("error")
                primary = get_entity(db, primary.id, "document_revision")
                qa_op = kb.run_qa(primary.id, {}, request(primary), hr, db)
                job = db.scalar(
                    select(Job).where(
                        Job.payload["operation_id"].as_string() == qa_op["id"]
                    )
                )
                assert kb.process_ingest_job(db, job)
                qa_operation = get_entity(db, qa_op["id"], "operation")
                assert qa_operation.status == "SUCCEEDED", qa_operation.data.get(
                    "error"
                )
                for revision_id in revisions:
                    revision = get_entity(db, revision_id, "document_revision")
                    kb.review_revision(
                        revision.id,
                        {
                            "action": "APPROVE",
                            "reason": "Synthetic engineering approval",
                        },
                        request(revision),
                        Response(),
                        hr,
                        db,
                    )
                head = kb._head(db)
                initial_snapshot = head.data["snapshot_id"]
                versions = {
                    rid: etag(get_entity(db, rid, "document_revision"))
                    for rid in revisions
                }
                review_pins = {
                    rid: get_entity(db, rid, "document_revision").data[
                        "review_fingerprint"
                    ]
                    for rid in revisions
                }
                db.commit()

            start = threading.Barrier(3)
            done = threading.Event()
            observed = []

            def publish(primary_id, secondary_id):
                with factory() as db:
                    start.wait(8)
                    try:
                        req = Request(
                            {
                                "type": "http",
                                "headers": [
                                    (b"if-match", versions[primary_id].encode())
                                ],
                            }
                        )
                        kb.publish_revision(
                            primary_id,
                            {
                                "expected_snapshot_id": initial_snapshot,
                                "additional_revision_reviews": [
                                    {
                                        "revision_id": secondary_id,
                                        "expected_review_fingerprint": review_pins[
                                            secondary_id
                                        ],
                                    }
                                ],
                            },
                            req,
                            hr,
                            db,
                        )
                        return 202
                    except HTTPException as error:
                        db.rollback()
                        return error.status_code

            def observe():
                start.wait(8)
                with factory() as db:
                    while not done.is_set():
                        observed.append(
                            db.scalar(
                                select(func.count(Entity.id)).where(
                                    Entity.kind == "document",
                                    Entity.id.in_(documents),
                                    Entity.status == "ACTIVE",
                                )
                            )
                        )
                        time.sleep(0.002)

            with ThreadPoolExecutor(max_workers=3) as pool:
                monitor = pool.submit(observe)
                first = pool.submit(publish, revisions[0], revisions[1])
                second = pool.submit(publish, revisions[1], revisions[0])
                try:
                    statuses = [first.result(timeout=20), second.result(timeout=20)]
                finally:
                    done.set()
                monitor.result(timeout=10)
            assert sorted(statuses) in ([202, 409], [202, 412]), statuses
            assert observed and set(observed).issubset(
                {0, 2}
            ), "PARTIAL_CANDIDATE_VISIBILITY"
            with factory() as db:
                head = kb._head(db)
                final = get_entity(db, head.data["snapshot_id"], "knowledge_snapshot")
                assert final.id != initial_snapshot
                for doc_id, rid in zip(documents, revisions):
                    document, revision = get_entity(db, doc_id, "document"), get_entity(
                        db, rid, "document_revision"
                    )
                    assert (
                        document.status == "ACTIVE"
                        and document.data["active_revision_id"] == rid
                    )
                    assert (
                        revision.status == "PUBLISHED"
                        and final.data["members"][doc_id] == rid
                    )
                publications = db.scalar(
                    select(func.count(Entity.id)).where(
                        Entity.kind == "knowledge_snapshot",
                        Entity.data["previous_snapshot_id"].as_string()
                        == initial_snapshot,
                    )
                )
                assert publications == 1, "MORE_THAN_ONE_PUBLICATION_COMMITTED"
            return {
                "publish_statuses": statuses,
                "committed_publications": 1,
                "member_count": 2,
                "observer_saw_only_atomic_states": True,
                "synthetic_CORE_boundary_no_model_HTTP": True,
            }
        finally:
            retrieval.MODE, retrieval._request = previous_mode, previous_request

    check(
        "reverse_multi_member_publication_serializes_without_partial_visibility",
        reverse_multi_member_publication,
    )

    def scheduled_publication(mode):
        from korsik import knowledge as kb, retrieval
        from fastapi import Response

        previous_mode, previous_request = retrieval.MODE, retrieval._request
        previous_factory, previous_publish = worker.SessionLocal, kb._publish
        retrieval.MODE = "CORE"
        worker.SessionLocal = factory

        def no_models(*args, **kwargs):
            raise RuntimeError("SYNTHETIC_CORE_PUBLICATION_NO_MODEL_HTTP")

        retrieval._request = no_models
        try:
            with factory() as db:
                value = kb.create_document(
                    {
                        "title": "Synthetic scheduled " + mode,
                        "scope": {"site_ids": ["GrPP"]},
                        "document_category": "REGULATION",
                    },
                    Response(),
                    hr,
                    db,
                )
                doc = get_entity(db, value["id"], "document")
                value = kb.create_article(
                    doc.id,
                    {
                        "text": "Синтетический текст проверки плановой публикации "
                        + mode
                    },
                    request(doc),
                    Response(),
                    hr,
                    db,
                )
                revision = get_entity(db, value["id"], "document_revision")
                extraction = kb._extraction(db, revision)
                kb.review_page(
                    revision.id,
                    1,
                    {
                        "action": "REVIEW_CONTENT",
                        "reason": "Synthetic page review",
                        "expected_extraction_revision_id": extraction.id,
                    },
                    request(extraction),
                    Response(),
                    hr,
                    db,
                )
                candidate_op = kb.build_candidate(
                    revision.id, {}, request(revision), hr, db
                )
                candidate_job = db.scalar(
                    select(Job).where(
                        Job.payload["operation_id"].as_string() == candidate_op["id"]
                    )
                )
                assert kb.process_ingest_job(db, candidate_job)
                assert (
                    get_entity(db, candidate_op["id"], "operation").status
                    == "SUCCEEDED"
                )
                candidate_job.status = "COMPLETED"
                qa_op = kb.run_qa(revision.id, {}, request(revision), hr, db)
                qa_job = db.scalar(
                    select(Job).where(
                        Job.payload["operation_id"].as_string() == qa_op["id"]
                    )
                )
                assert kb.process_ingest_job(db, qa_job)
                assert get_entity(db, qa_op["id"], "operation").status == "SUCCEEDED"
                qa_job.status = "COMPLETED"
                kb.review_revision(
                    revision.id,
                    {
                        "action": "APPROVE",
                        "reason": "Synthetic HR publication approval",
                    },
                    request(revision),
                    Response(),
                    hr,
                    db,
                )
                old_head = kb._head(db).data["snapshot_id"]
                snapshot_count = db.scalar(
                    select(func.count(Entity.id)).where(
                        Entity.kind == "knowledge_snapshot"
                    )
                )
                op = kb.publish_revision(
                    revision.id,
                    {
                        "effective_at": (now() + timedelta(hours=1)).isoformat(),
                        "reason": "Synthetic scheduled publication",
                        "expected_snapshot_id": old_head,
                    },
                    request(revision),
                    hr,
                    db,
                )
                publish_job = db.scalar(
                    select(Job).where(
                        Job.payload["operation_id"].as_string() == op["id"]
                    )
                )
                assert publish_job and publish_job.kind == "KNOWLEDGE_PUBLISH"
                publish_job.run_at = now() - timedelta(seconds=1)
                if mode == "revoked_actor":
                    db.get(User, hr.id).enabled = False
                operation_id, job_id, document_id, revision_id = (
                    op["id"],
                    publish_job.id,
                    doc.id,
                    revision.id,
                )
                db.commit()

            if mode == "postwrite_fault":

                def fail_after_head_write(*args, **kwargs):
                    previous_publish(*args, **kwargs)
                    raise HTTPException(409, "SYNTHETIC_POSTWRITE_SAVEPOINT_FAILURE")

                kb._publish = fail_after_head_write

            for _ in range(50):
                with factory() as db:
                    if db.get(Job, job_id).status in {"COMPLETED", "FAILED", "RETRY"}:
                        break
                assert worker.run_once(
                    ingest=True
                ), "SCHEDULED_PUBLICATION_NOT_CLAIMABLE"
            with factory() as db:
                op = get_entity(db, operation_id, "operation")
                job = db.get(Job, job_id)
                document = get_entity(db, document_id, "document")
                revision = get_entity(db, revision_id, "document_revision")
                head = kb._head(db).data["snapshot_id"]
                snapshots_after = db.scalar(
                    select(func.count(Entity.id)).where(
                        Entity.kind == "knowledge_snapshot"
                    )
                )
                assert (
                    job.status == "COMPLETED" and job.attempts == 1
                ), "PUBLICATION_WORKER_DID_NOT_COMMIT_TERMINAL_JOB"
                if mode == "success":
                    assert op.status == "SUCCEEDED" and op.data.get("published") is True
                    assert (
                        revision.status == "PUBLISHED"
                        and document.data.get("active_revision_id") == revision.id
                    )
                    assert head != old_head and snapshots_after == snapshot_count + 1
                else:
                    assert op.status == "FAILED" and op.data.get("published") is False
                    assert (
                        revision.status == "REVIEW_REQUIRED"
                        and not document.data.get("active_revision_id")
                    )
                    assert (
                        head == old_head and snapshots_after == snapshot_count
                    ), "POSTWRITE_SAVEPOINT_LEAKED_HEAD_OR_SNAPSHOT"
                return {
                    "mode": mode,
                    "operation_status": op.status,
                    "worker_job_status": job.status,
                    "worker_attempts": job.attempts,
                    "head_changed": head != old_head,
                    "snapshot_count_delta": snapshots_after - snapshot_count,
                    "actual_worker_claim_handler_commit": True,
                    "synthetic_CORE_boundary_no_model_HTTP": True,
                }
        finally:
            worker.SessionLocal, kb._publish = previous_factory, previous_publish
            retrieval.MODE, retrieval._request = previous_mode, previous_request
            with factory() as db:
                user = db.get(User, hr.id)
                if user and not user.enabled:
                    user.enabled = True
                    db.commit()

    for mode in ("success", "revoked_actor", "postwrite_fault"):
        check(
            "scheduled_publish_worker_" + mode,
            lambda mode=mode: scheduled_publication(mode),
        )

    def scheduled_employment_stage_worker():
        eid = create_employee("scheduled-stage")
        previous_factory = worker.SessionLocal
        worker.SessionLocal = factory
        try:
            with factory() as db:
                employee = get_entity(db, eid, "employee")
                episode = get_entity(
                    db, employee.data["episode_id"], "employment_episode"
                )
                effective = now() + timedelta(seconds=2)
                response = d.employment_episode_stage(
                    episode.id,
                    {
                        "expected_episode_id": episode.id,
                        "expected_state_version": episode.version,
                        "stage": "ACTIVE",
                        "effective_at": effective.isoformat(),
                        "reason": "Synthetic actual worker confirms approved future stage",
                    },
                    db,
                    hr,
                )
                assert response.status_code == 202
                oid = json.loads(response.body)["id"]
                target = db.scalar(
                    select(Job).where(Job.payload["operation_id"].as_string() == oid)
                )
                jid = target.id
                assert employee.data["employment_stage"] == "PREBOARDING"
                # Earlier synthetic cases can leave unrelated business jobs.
                # Retire them in this schema-only DB so the actual business
                # executor cannot call health/model/MAX paths in this proof.
                for other in db.scalars(
                    select(Job).where(
                        Job.id != jid, Job.status.in_(["PENDING", "RETRY"])
                    )
                ):
                    other.status = "CANCELLED"
                db.commit()
            time.sleep(2.1)
            assert worker.run_once(), "DUE_STAGE_JOB_NOT_CLAIMED"
            with factory() as db:
                op, job = db.get(Entity, oid), db.get(Job, jid)
                employee = db.get(Entity, eid)
                assert (
                    op.status == "SUCCEEDED"
                    and employee.data["employment_stage"] == "ACTIVE"
                )
                assert job.status == "COMPLETED" and job.attempts == 1
                events = [
                    e
                    for e in d.entities(db, "employment_episode_event", episode.id)
                    if e.data.get("operation_id") == oid
                ]
                assert len(events) == 1
                return {
                    "operation_status": op.status,
                    "worker_job_status": job.status,
                    "worker_attempts": job.attempts,
                    "stage": employee.data["employment_stage"],
                    "committed_stage_events": len(events),
                    "actual_business_worker_claim_handler_commit": True,
                    "external_model_requests": 0,
                    "external_MAX_sends": 0,
                }
        finally:
            worker.SessionLocal = previous_factory

    check("scheduled_employment_stage_actual_worker", scheduled_employment_stage_worker)

    report["passed"] = sum(c["status"] == "PASS" for c in report["checks"])
    report["failed"] = sum(c["status"] == "FAIL" for c in report["checks"])
    report["status"] = "PASS" if not report["failed"] else "FAIL"
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False))
    engine.dispose()
    return 0 if not report["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
