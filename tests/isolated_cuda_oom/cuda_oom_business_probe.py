"""Optional isolated real CUDA allocator-cap OOM/business proof.

Uses copied synthetic business fixtures from the existing generator-down probe.
The external wrapper must already have loaded the real model and armed its real
allocator cap. No fake exception or mocked model call is used. Physical VRAM
exhaustion is explicitly outside this proof. NEVER run on the production DB/GPU.
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
        "probe": "CONTROLLED_CUDA_ALLOCATOR_OOM_BUSINESS_PERSISTENCE",
        "physical_vram_exhaustion": False,
        "fake_exception_injection": False,
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
    if not ready:
        raise RuntimeError("REAL_CAPPED_ISOLATED_GENERATOR_MUST_BE_READY")
    status = httpx.get(generator + "/__isolated_oom_proof/status", timeout=3, trust_env=False)
    status.raise_for_status()
    initial_proof = status.json()
    if initial_proof.get("status") != "ARMED_NOT_YET_PROVEN" or initial_proof.get("actual_oom_count") != 0:
        raise RuntimeError("FRESH_ARMED_REAL_ALLOCATOR_WRAPPER_REQUIRED")
    expected_artifact = response.json()["fingerprint"]
    report["generator_available_at_start"] = True
    report["gpu_uuid"] = initial_proof["gpu_uuid"]
    report["wrapper_source_sha256"] = initial_proof["wrapper_source_sha256"]
    report["generation_runtime_source_sha256"] = initial_proof["generation_runtime_source_sha256"]
    report["allocator_limit_bytes"] = initial_proof["allocator_limit_bytes"]
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
    # A bounded direct-generation pump forces real OOMs during the short HR
    # transaction window, not merely after CPU retrieval has already completed.
    pump_stop, pump_ready = threading.Event(), threading.Event()
    pump_observations = []
    prefill_fixture = httpx.get(generator + "/__isolated_oom_proof/prefill-fixture", timeout=3, trust_env=False)
    prefill_fixture.raise_for_status()
    synthetic_payload = {"question": "Когда обед?", "evidence": [{"title": "Синтетическое правило", "text": prefill_fixture.json()["text"]}],
                         "artifact_fingerprint": expected_artifact, "max_new_tokens": 32,
                         "max_time_seconds": 1.0, "request_budget_seconds": 2.0}

    def fault_pump():
        for _ in range(40):
            if pump_stop.is_set():
                break
            started = time.monotonic()
            try:
                fault = httpx.post(generator + "/generate", json=synthetic_payload, timeout=4, trust_env=False)
                witness = httpx.get(generator + "/__isolated_oom_proof/status", timeout=3, trust_env=False)
                witness.raise_for_status()
                proof = witness.json()
                genuine = fault.status_code == 500 and proof.get("actual_oom_count", 0) > 0 and proof.get("last_observed_exception") == "torch.cuda.OutOfMemoryError"
                pump_observations.append({"http_status": fault.status_code, "genuine_oom_witness": genuine,
                                          "started": started, "ended": time.monotonic()})
                if genuine:
                    pump_ready.set()
                else:
                    break
            except (httpx.HTTPError, ValueError):
                pump_observations.append({"error": "FAULT_PUMP_HTTP_FAILURE"})
                break
            pump_stop.wait(.15)

    pump = threading.Thread(target=fault_pump, daemon=True, name="isolated-cuda-fault-witness")
    pump.start()
    if not pump_ready.wait(timeout=15):
        pump_stop.set()
        pump.join(timeout=8)
        raise RuntimeError("INCONCLUSIVE_NO_GENUINE_CUDA_ALLOCATOR_OOM_WITNESS")
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
                db, actor, conv, "Synthetic controlled CUDA allocator fault handoff"
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
        pump_stop.set()
        pump.join(timeout=8)
        results = [future.result(timeout=120) for future in futures]

    proof_response = httpx.get(generator + "/__isolated_oom_proof/status", timeout=3, trust_env=False)
    proof_response.raise_for_status()
    actual_proof = proof_response.json()
    report["actual_oom_count"] = actual_proof.get("actual_oom_count", 0)
    report["actual_exception_class"] = actual_proof.get("last_observed_exception")
    report["fault_pump_attempts"] = len(pump_observations)
    report["fault_pump_genuine_witnesses"] = sum(bool(row.get("genuine_oom_witness")) for row in pump_observations)
    disarmed = httpx.post(generator + "/__isolated_oom_proof/disarm", timeout=6, trust_env=False)
    disarmed.raise_for_status()
    recovery_payload = {**synthetic_payload, "evidence": [{"title": "Синтетическое правило", "text": "Обед проходит с 12:00 до 13:00."}], "max_new_tokens": 128, "max_time_seconds": 17.0, "request_budget_seconds": 20.0}
    try:
        recovery_response = httpx.post(generator + "/generate", json=recovery_payload, timeout=24, trust_env=False)
        recovery = recovery_response.json() if recovery_response.is_success else {}
    except (httpx.HTTPError, ValueError):
        recovery_response, recovery = None, {}
    lock_status = httpx.get(generator + "/__isolated_oom_proof/status", timeout=3, trust_env=False).json()
    recovered_text = recovery.get("text", "")
    report["recovery"] = {"http_status": recovery_response.status_code if recovery_response is not None else None,
                          "artifact_matches": recovery.get("artifact_fingerprint") == expected_artifact,
                          "source_numbers_and_citation_present": all(value in recovered_text for value in ["12", "13", "[1]"]),
                          "output_characters": len(recovered_text), "runtime_lock_released": lock_status.get("runtime_inference_lock_locked") is False}

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
            "no_generated_output_while_allocator_capped": all(
                not r["local_generated"] for r in results
            ),
            "genuine_cuda_allocator_oom_observed": report["actual_oom_count"] >= 1 and report["actual_exception_class"] == "torch.cuda.OutOfMemoryError",
            "genuine_oom_events_overlap_business_commits": actual_proof.get("first_oom_at_monotonic", float("inf")) <= business_ended and actual_proof.get("last_oom_at_monotonic", 0) >= business_started,
            "actual_post_disarm_generation_recovered": report["recovery"]["http_status"] == 200 and report["recovery"]["artifact_matches"] and report["recovery"]["source_numbers_and_citation_present"] and report["recovery"]["runtime_lock_released"],
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
    report["checks"] = [
        {"name": name, "status": "PASS" if passed else "FAIL"}
        for name, passed in assertions.items()
    ]
    report["status"] = "PASS" if all(assertions.values()) else "FAIL"
    report["finished_at"] = now().isoformat()
    output = Path(
        os.environ.get("KORSIK_OOM_PROBE_REPORT", "/tmp/cuda-oom-business-report.json")
    )
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({"status": report["status"], "probe": report["probe"], "checks": len(report["checks"]), "actual_oom_count": report["actual_oom_count"], "physical_vram_exhaustion": False, "external_MAX_sends": 0}))
    engine.dispose()
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
