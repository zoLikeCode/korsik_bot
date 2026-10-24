"""Isolated restored PostgreSQL proof: generator-down must not block HR work.

The caller stops only its isolated generator and creates/restores a database
whose name starts with korsik_offline_, korsik_restore_, or korsik_mixed_. Set
KORSIK_MIXED_PROBE_DATABASE to that exact name. No transport send function runs.
This script inserts synthetic fixtures into that isolated restored database.
"""

from __future__ import annotations
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from sqlalchemy import create_engine, select, func, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker


def main():
    from korsik.settings import get_settings
    from korsik.db import now, new_id
    from korsik.models import User, Outbox, Message, TransportDelivery
    from korsik.auth import Actor
    from korsik.common import get_entity
    from korsik import domain as d, chat, retrieval
    from korsik.seed import default_graph, seed_defaults

    database = os.environ.get("KORSIK_MIXED_PROBE_DATABASE", "")
    if not database.startswith(("korsik_offline_", "korsik_restore_", "korsik_mixed_")):
        raise RuntimeError("EXPLICIT_ISOLATED_DATABASE_NAME_REQUIRED")
    url = make_url(get_settings().database_url).set(database=database)
    if url.get_backend_name() != "postgresql":
        raise RuntimeError("POSTGRES_REQUIRED")
    if retrieval.MODE != "FULL":
        raise RuntimeError("FULL_PROFILE_REQUIRED_FOR_GENERATOR_FAULT_PROOF")
    generator = retrieval.GENERATOR.rstrip("/")
    if urlsplit(generator).hostname not in {"generator", "localhost", "127.0.0.1"}:
        raise RuntimeError("ISOLATED_INTERNAL_GENERATOR_ENDPOINT_REQUIRED")
    report = {
        "probe": "MIXED_AI_FAILURE_BUSINESS_PERSISTENCE",
        "database": database,
        "synthetic_fixtures_only": True,
        "restored_company_sources_read_only": True,
        "external_MAX_sends": 0,
        "model_calls_mocked": False,
        "started_at": now().isoformat(),
        "checks": [],
    }
    try:
        response = httpx.get(generator + "/healthz", timeout=2, trust_env=False)
        ready = response.is_success and response.json().get("status") in {
            "READY",
            "ready",
            "loaded",
        }
    except (httpx.HTTPError, ValueError):
        ready = False
    if ready:
        raise RuntimeError("STOP_ONLY_THE_ISOLATED_GENERATOR_BEFORE_THIS_PROBE")
    report["generator_available_at_start"] = False
    engine = create_engine(
        url,
        pool_size=8,
        max_overflow=2,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={"options": "-c statement_timeout=15000 -c lock_timeout=7000"},
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        if db.scalar(text("select current_database()")) != database:
            raise RuntimeError("WRONG_DATABASE")
        seed_defaults(db)
        hr_user = User(
            id=new_id(),
            email="mixed-hr-" + new_id() + "@integration.invalid",
            name="Synthetic fault HR",
            role="HR_ADMIN",
            scope={"all": True},
            enabled=True,
            password_hash="synthetic-no-login",
        )
        manager = User(
            id=new_id(),
            email="mixed-manager-" + new_id() + "@integration.invalid",
            name="Synthetic fault manager",
            role="MANAGER",
            scope={},
            enabled=True,
            password_hash="synthetic-no-login",
        )
        db.add_all([hr_user, manager])
        db.flush()
        hr = Actor(hr_user.id, hr_user.name, hr_user.role, hr_user.scope)
        manager_actor = Actor(manager.id, manager.name, manager.role, manager.scope)
        profiles = []
        for index in range(4):
            employee = d.create_employee_record(
                db,
                hr,
                {
                    "name": "Synthetic mixed fault " + str(index),
                    "site_id": "GrPP",
                    "start_date": now().date().isoformat(),
                    "employment_type": "OFFICE",
                    "assigned_hr_id": hr.id,
                    "manager_id": manager.id,
                },
            )
            d.update(employee, employment_stage="ACTIVE")
            d.sync_employment_episode(db, employee)
            conv = chat.conversation_for(db, employee)
            profiles.append((employee.id, conv.id))
        db.commit()
        fixture_recipient_ids = {eid for eid, _ in profiles} | {hr.id, manager_actor.id}
        before_accepted = set(
            db.scalars(
                select(Outbox.id).where(
                    Outbox.status == "ACCEPTED",
                    Outbox.recipient_id.in_(fixture_recipient_ids),
                )
            )
        )
        fixture_outbox_ids = set(
            db.scalars(
                select(Outbox.id).where(Outbox.recipient_id.in_(fixture_recipient_ids))
            )
        )
        before_deliveries = set(
            db.scalars(
                select(TransportDelivery.id).where(
                    TransportDelivery.outbox_id.in_(fixture_outbox_ids)
                )
            )
        )
        applicable = retrieval.published_corpus(
            db, get_entity(db, profiles[0][0], "employee")
        )
        if not applicable:
            raise RuntimeError("RESTORED_PUBLISHED_CORPUS_REQUIRED")
        report["applicable_published_revisions"] = len(applicable)

    questions = json.loads(
        os.environ.get(
            "KORSIK_MIXED_PROBE_QUESTIONS",
            '["Как передавать пароли к конфиденциальной информации?", "Какова миссия компании ПОЛИПЛАСТИК?", "Куда направляются копии соглашения о конфиденциальности?"]',
        )
    )
    if (
        not isinstance(questions, list)
        or len(questions) != 3
        or any(not isinstance(q, str) or not 1 <= len(q) <= 500 for q in questions)
    ):
        raise RuntimeError("THREE_BOUNDED_QUESTIONS_REQUIRED")
    barrier = threading.Barrier(4)

    def query(index):
        with factory() as db:
            conv = get_entity(db, profiles[index][1], "conversation")
            barrier.wait(timeout=10)
            started = time.monotonic()
            try:
                answer = retrieval.answer_question(db, conv, questions[index])
                return {
                    "query_index": index + 1,
                    "started": started,
                    "ended": time.monotonic(),
                    "mode": answer.get("mode"),
                    "reason": answer.get("reason"),
                    "degraded": bool(answer.get("provenance", {}).get("degraded")),
                    "citations": len(answer.get("citations", [])),
                    "local_generated": answer.get("mode") == "LOCAL_GENERATED",
                }
            except Exception as error:
                return {
                    "query_index": index + 1,
                    "started": started,
                    "ended": time.monotonic(),
                    "mode": "ERROR",
                    "error_type": type(error).__name__,
                    "local_generated": False,
                }
            finally:
                db.rollback()

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(query, index) for index in range(3)]
        barrier.wait(timeout=10)
        business_started = time.monotonic()
        eid, cid = profiles[3]
        with factory() as db:
            employee = get_entity(db, eid, "employee")
            actor = Actor(
                employee.id, employee.data["name"], "EMPLOYEE", {}, employee.id
            )
            journey = next(
                j
                for j in d.entities(db, "journey", employee.id)
                if j.data.get("episode_id") == employee.data.get("episode_id")
            )
            step = next(
                s
                for s in default_graph()["steps"]
                if s.get("survey", {}).get("survey_key") == "SUR-D14"
            )
            run = d.open_survey(db, journey, step, employee)
            d.submit_survey_answer(
                db, actor, run.id, {"question_id": "D14-Q1", "value": "no"}
            )
            capture = d.open_capture(
                db,
                actor,
                {
                    "survey_run_id": run.id,
                    "question_id": "D14-Q1",
                    "capture_kind": "COMMENT",
                },
            )
            d.capture_draft(
                db,
                actor,
                capture.id,
                {
                    "text": "Синтетический комментарий для проверки сохранения при остановленной модели."
                },
            )
            d.confirm_capture(
                db,
                actor,
                capture.id,
                {"draft_fingerprint": capture.data["draft_fingerprint"]},
            )
            db.commit()
            run_id, capture_id = run.id, capture.id
            conv = get_entity(db, cid, "conversation")
            case = chat.handoff(
                db, actor, conv, "Synthetic generator-down fault handoff"
            )
            db.commit()
            case_id = case["id"]
            d.claim_case(case_id, db, hr)
            chat.human_reply(
                cid,
                chat.HumanReply(
                    text="Синтетический ответ HR сохранён независимо от локальной генерации."
                ),
                hr,
                db,
            )
            d.refresh_dashboards(db)
            db.commit()
        business_ended = time.monotonic()
        results = [future.result(timeout=120) for future in futures]

    with factory() as db:
        run = get_entity(db, run_id, "survey_run")
        capture = get_entity(db, capture_id, "input_capture")
        conv = get_entity(db, cid, "conversation")
        case = get_entity(db, case_id, "case")
        answer = run.data["answers"].get("D14-Q1", {})
        dashboard = d.dashboard_data(db, manager_actor)
        snapshots = d.entities(db, "dashboard_snapshot", manager_actor.id)
        hr_messages = db.scalar(
            select(func.count(Message.id)).where(
                Message.conversation_id == cid, Message.author_type == "HR"
            )
        )
        hr_outboxes = [
            o
            for o in db.scalars(select(Outbox))
            if o.data.get("human_response") and o.data.get("case_id") == case_id
        ]
        accepted_now = set(
            db.scalars(
                select(Outbox.id).where(
                    Outbox.status == "ACCEPTED",
                    Outbox.recipient_id.in_(fixture_recipient_ids),
                )
            )
        )
        fixture_outbox_ids = set(
            db.scalars(
                select(Outbox.id).where(Outbox.recipient_id.in_(fixture_recipient_ids))
            )
        )
        deliveries_now = set(
            db.scalars(
                select(TransportDelivery.id).where(
                    TransportDelivery.outbox_id.in_(fixture_outbox_ids)
                )
            )
        )
        assertions = {
            "survey_answer_committed": answer.get("value") == "no"
            and bool(answer.get("comment")),
            "capture_confirmation_committed": capture.status == "CONFIRMED",
            "next_survey_question_preserved": run.data.get("current_question_key")
            == "D14-Q2",
            "handoff_and_claim_committed": conv.data.get("mode") == "HUMAN"
            and case.data.get("assigned_user_id") == hr.id,
            "human_reply_message_and_outbox_committed": hr_messages == 1
            and len(hr_outboxes) == 1,
            "manager_dashboard_available": dashboard["metrics"]["employees"] == 4
            and bool(snapshots),
            "no_transport_send_or_delivery_created": accepted_now == before_accepted
            and deliveries_now == before_deliveries,
            "rag_and_business_intervals_overlap": any(
                r["started"] <= business_ended and r["ended"] >= business_started
                for r in results
            ),
            "no_generated_output_while_generator_down": all(
                not r["local_generated"] for r in results
            ),
            "actual_generator_fault_observed": any(
                r.get("reason") == "LOCAL_GENERATOR_UNAVAILABLE" for r in results
            ),
            "rag_returns_explicit_fallback": all(
                r.get("mode") in {"EXTRACT", "HANDOFF", "CLARIFY", "APPROVED_CARD"}
                for r in results
            ),
        }
    report["business_command_seconds"] = round(business_ended - business_started, 3)
    report["queries"] = [
        {k: v for k, v in r.items() if k not in {"started", "ended"}}
        | {"seconds": round(r["ended"] - r["started"], 3)}
        for r in results
    ]
    report["actual_generator_fault_query_count"] = sum(
        r.get("reason") == "LOCAL_GENERATOR_UNAVAILABLE" for r in results
    )
    report["queries_not_requiring_generator"] = sum(
        r.get("reason") in {"LITERAL_NORMATIVE_POLICY", "LITERAL_ENUMERATION_SOURCE"}
        for r in results
    )
    report["checks"] = [
        {"name": name, "status": "PASS" if passed else "FAIL"}
        for name, passed in assertions.items()
    ]
    report["status"] = "PASS" if all(assertions.values()) else "FAIL"
    report["finished_at"] = now().isoformat()
    output = Path(
        os.environ.get("KORSIK_MIXED_PROBE_REPORT", "/tmp/mixed-ai-failure-report.json")
    )
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False))
    engine.dispose()
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
