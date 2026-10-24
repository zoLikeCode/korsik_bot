"""Release18 real PostgreSQL checks: schema-only synthetic DB, no MAX/model I/O.

The wrapper creates/drops korsik_integration_check. This process refuses any
other target and refuses non-empty domain/user/session tables before seeding.
It uses the deployed Python modules and actual FastAPI middleware/routes, with
only SessionLocal redirected to the isolated DB and a synthetic MAX signer.
"""

from __future__ import annotations

import hmac
import json
import os
from pathlib import Path
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import quote, urlencode
import uuid

import httpx
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient

EXPECTED_DATABASE = "korsik_integration_check"
DUMMY_TOKEN = "SYNTHETIC_RELEASE18_BOT_KEY_NOT_A_REAL_MAX_TOKEN"
REPORT = Path(
    os.environ.get("KORSIK_RELEASE18_PG_REPORT", "/tmp/release18-postgres-report.json")
)


def main():
    from korsik import auth, db as dbmodule, main as core, max_miniapp as mini
    from korsik import domain as d, survey_campaigns as sc, worker
    from korsik.common import save_entity
    from korsik.models import Entity, User, SessionToken, Job, Outbox
    from korsik.db import get_db, now, new_id
    from korsik.seed import seed_defaults
    from korsik.settings import get_settings

    url = make_url(get_settings().database_url)
    if url.get_backend_name() != "postgresql":
        raise RuntimeError("POSTGRES_REQUIRED")
    isolated = url.set(database=EXPECTED_DATABASE)
    assert isolated.database == EXPECTED_DATABASE
    engine = create_engine(
        isolated,
        hide_parameters=True,
        pool_pre_ping=True,
        pool_size=8,
        max_overflow=2,
        connect_args={
            "options": "-c statement_timeout=15000 -c lock_timeout=8000 -c idle_in_transaction_session_timeout=30000"
        },
    )
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as db:
        assert db.scalar(text("select current_database()")) == EXPECTED_DATABASE
        version = db.scalar(text("select version()"))
        assert all(
            db.scalar(select(func.count(model.id))) == 0
            for model in (Entity, User, SessionToken)
        ), "REFUSE_NONEMPTY_DATABASE"
        seed_defaults(db)
        hr_user = User(
            id=new_id(),
            email="release18-hr@integration.invalid",
            name="Synthetic release18 HR",
            password_hash="not-login-credential",
            role="HR_ADMIN",
            scope={"all": True},
            enabled=True,
        )
        db.add(hr_user)
        db.commit()
        hr = auth.user_actor(hr_user)

    report = {
        "database": EXPECTED_DATABASE,
        "postgresql_server_version": version,
        "source_data_copied": False,
        "synthetic_fixtures_only": True,
        "real_MAX_token_read": False,
        "external_MAX_sends": 0,
        "external_model_requests": 0,
        "production_business_mutations": False,
        "http_context": "Real deployed FastAPI middleware/routes in an in-process ASGI client; isolated PostgreSQL sessions",
        "binding_fixture": "Synthetic pre-approved server binding, not a real MAX launch or HR identity verification",
        "checks": [],
    }
    transport_calls = []
    previous_http = httpx.HTTPTransport.handle_request
    previous_async = httpx.AsyncHTTPTransport.handle_async_request

    def reject_external(self, request):
        transport_calls.append(request.url.host)
        raise RuntimeError("EXTERNAL_HTTP_FORBIDDEN_IN_PG_CHECK")

    async def reject_async(self, request):
        transport_calls.append(request.url.host)
        raise RuntimeError("EXTERNAL_HTTP_FORBIDDEN_IN_PG_CHECK")

    httpx.HTTPTransport.handle_request = reject_external
    httpx.AsyncHTTPTransport.handle_async_request = reject_async
    old_core_factory, old_db_factory, old_worker_factory = (
        core.SessionLocal,
        dbmodule.SessionLocal,
        worker.SessionLocal,
    )
    old_mini_settings, old_mini_secret = mini.get_settings, mini.read_secret
    old_overrides = dict(core.app.dependency_overrides)
    core.SessionLocal = dbmodule.SessionLocal = worker.SessionLocal = factory

    def isolated_session():
        with factory() as db:
            yield db

    core.app.dependency_overrides[get_db] = isolated_session
    mini.get_settings = lambda: SimpleNamespace(
        public_url="https://release18.invalid",
        secure_cookies=True,
        max_token_file="/synthetic-never-mounted-token",
        session_ttl_seconds=3600,
    )

    def synthetic_secret(path, required=True):
        assert path == "/synthetic-never-mounted-token", "REFUSE_REAL_SECRET_READ"
        return DUMMY_TOKEN

    mini.read_secret = synthetic_secret

    def check(name, action):
        started = time.monotonic()
        try:
            details = action() or {}
            report["checks"].append(
                {
                    "name": name,
                    "status": "PASS",
                    "seconds": round(time.monotonic() - started, 3),
                    **details,
                }
            )
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            report["checks"].append(
                {
                    "name": name,
                    "status": "FAIL",
                    "seconds": round(time.monotonic() - started, 3),
                    "error_type": type(exc).__name__,
                    "assertion": (
                        str(exc)[:200] if isinstance(exc, AssertionError) else None
                    ),
                }
            )

    def client():
        # Not entering TestClient's lifespan avoids starting unrelated startup
        # or connector processes. Requests use the actual main middleware.
        return TestClient(core.app, base_url="https://release18.invalid")

    def signed(uid, query=None, **fields):
        params = {
            "user": json.dumps({"id": uid, **fields}, separators=(",", ":")),
            "auth_date": str(int(now().timestamp())),
            "query_id": query or str(uuid.uuid4()),
        }
        message = "\n".join(f"{key}={params[key]}" for key in sorted(params))
        secret = hmac.digest(b"WebAppData", DUMMY_TOKEN.encode(), "sha256")
        params["hash"] = hmac.digest(secret, message.encode(), "sha256").hex()
        return urlencode(params, quote_via=quote)

    def principal(role="MANAGER", scope=None):
        uid = int(uuid.uuid4().hex[:12], 16)
        with factory() as db:
            user = User(
                id=new_id(),
                email=f"{new_id()}@integration.invalid",
                name="Synthetic release18 principal",
                role=role,
                scope=scope or {"site_ids": ["GrPP"]},
                enabled=True,
                password_hash="not-login-credential",
            )
            db.add(user)
            db.flush()
            binding = save_entity(
                db,
                "principal_binding",
                {
                    "channel": "MAX",
                    "max_user_id": str(uid),
                    "confirmed_at": now().isoformat(),
                },
                owner_id=user.id,
            )
            db.commit()
            return uid, user.id, binding.id

    def post(c, path, payload, etag=None):
        headers = {
            "X-CSRF-Token": c.cookies.get("korsik_csrf", ""),
            "Idempotency-Key": str(uuid.uuid4()),
        }
        if etag:
            headers["If-Match"] = etag
        return c.post("/api/v1" + path, json=payload, headers=headers)

    def principal_login(role="HR_ADMIN", scope=None):
        uid, user_id, binding_id = principal(role, scope)
        c = client()
        result = post(
            c, "/auth/max-miniapp", {"init_data": signed(uid, role="PLATFORM_ADMIN")}
        )
        assert result.status_code == 200, f"SYNTHETIC_LOGIN_HTTP_{result.status_code}"
        assert result.json()["actor"]["role"] == role
        return c, user_id, binding_id

    def role_session_metadata():
        uid, user_id, binding_id = principal("MANAGER")
        launch = signed(uid, role="HR_ADMIN", scope={"all": True})
        c = client()
        result = post(c, "/auth/max-miniapp", {"init_data": launch})
        assert result.status_code == 200
        assert result.json()["actor"]["role"] == "MANAGER"
        assert result.json()["actor"]["scope"] == {"site_ids": ["GrPP"]}
        assert all(
            "Secure" in value and "SameSite=none" in value
            for value in result.headers.get_list("set-cookie")
        )
        assert c.get("/api/v1/auth/session").status_code == 200
        same = post(c, "/auth/max-miniapp", {"init_data": launch})
        assert (
            same.status_code == 200
            and same.json()["csrf_token"] == result.json()["csrf_token"]
        )
        other = client()
        replay = post(other, "/auth/max-miniapp", {"init_data": launch})
        assert replay.status_code == 409
        assert replay.json()["detail"]["code"] == "max_init_data_replayed"
        with factory() as db:
            sessions = list(
                db.scalars(select(SessionToken).where(SessionToken.user_id == user_id))
            )
            metas = list(
                db.scalars(
                    select(Entity).where(
                        Entity.kind == "max_miniapp_session", Entity.owner_id == user_id
                    )
                )
            )
            assert len(sessions) == len(metas) == 1
            meta = metas[0]
            assert len(meta.id) <= 36 and len(meta.parent_id) <= 36
            assert meta.data["session_id"] == sessions[0].id
            assert meta.data["binding_id"] == binding_id
            assert DUMMY_TOKEN not in str(meta.data) and result.json()[
                "csrf_token"
            ] not in str(meta.data)
            db.get(Entity, binding_id).status = "REVOKED"
            db.commit()
        assert c.get("/api/v1/auth/session").status_code == 401
        return {
            "actual_HTTP_cookie_session": True,
            "server_role_ignores_forged_launch_role": True,
            "postgres_String36_metadata_enforced": True,
            "same_browser_replay_idempotent": True,
            "cross_browser_replay_denied": True,
            "revoked_binding_denies_session": True,
        }

    def concurrent_replay_unique():
        uid, user_id, _ = principal("HR_SPECIALIST")
        launch = signed(uid)
        barrier = threading.Barrier(2)

        def exchange():
            c = client()
            barrier.wait(timeout=5)
            return post(c, "/auth/max-miniapp", {"init_data": launch}).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            codes = sorted(pool.map(lambda _: exchange(), range(2)))
        assert codes == [200, 409], f"CONCURRENT_REPLAY_CODES_{codes}"
        with factory() as db:
            assert (
                db.scalar(
                    select(func.count(SessionToken.id)).where(
                        SessionToken.user_id == user_id
                    )
                )
                == 1
            )
            assert (
                db.scalar(
                    select(func.count(Entity.id)).where(
                        Entity.kind == "max_miniapp_session", Entity.owner_id == user_id
                    )
                )
                == 1
            )
        return {
            "concurrent_HTTP_statuses": codes,
            "committed_sessions": 1,
            "real_PostgreSQL_unique_race": True,
        }

    employee_context = {}

    def employee_provision_and_revoke():
        with factory() as db:
            employee = d.create_employee_record(
                db,
                hr,
                {
                    "name": "Synthetic release18 employee",
                    "site_id": "GrPP",
                    "start_date": now().date().isoformat(),
                    "employment_type": "OFFICE",
                },
            )
            uid = int(uuid.uuid4().hex[:12], 16)
            binding = save_entity(
                db,
                "max_binding",
                {"max_user_id": str(uid), "episode_id": employee.data["episode_id"]},
                parent_id=employee.id,
            )
            employee.data = {
                **employee.data,
                "binding_id": binding.id,
                "max_binding_status": "VERIFIED",
            }
            db.commit()
            eid = employee.id
        c = client()
        result = post(
            c, "/auth/max-miniapp", {"init_data": signed(uid, role="HR_ADMIN")}
        )
        assert result.status_code == 200
        actor = result.json()["actor"]
        assert (
            actor["role"] == "EMPLOYEE"
            and actor["scope"] == {}
            and actor["employee_id"] == eid
        )
        with factory() as db:
            employee = db.get(Entity, eid)
            user = db.get(User, actor["id"])
            assert user.employee_id == eid and employee.data["user_id"] == user.id
            assert user.email.endswith("@internal.invalid")
            historical = User(
                id=new_id(),
                email=f"{new_id()}@integration.invalid",
                name="Disabled synthetic history",
                password_hash="not-login-credential",
                role="EMPLOYEE",
                scope={},
                employee_id=eid,
                enabled=False,
            )
            db.add(historical)
            db.commit()
        renewed = post(client(), "/auth/max-miniapp", {"init_data": signed(uid)})
        assert (
            renewed.status_code == 200 and renewed.json()["actor"]["id"] == actor["id"]
        )
        with factory() as db:
            employee = db.get(Entity, eid)
            employee.data = {**employee.data, "user_id": new_id()}
            db.commit()
        denied = post(client(), "/auth/max-miniapp", {"init_data": signed(uid)})
        assert (
            denied.status_code == 403
            and denied.json()["detail"]["code"] == "max_account_unavailable"
        )
        with factory() as db:
            employee = db.get(Entity, eid)
            employee.data = {**employee.data, "user_id": actor["id"]}
            episode = employee.data["episode_id"]
            employee.data = {**employee.data, "episode_id": new_id()}
            db.commit()
        assert c.get("/api/v1/auth/session").status_code == 401
        with factory() as db:
            employee = db.get(Entity, eid)
            employee.data = {**employee.data, "episode_id": episode}
            db.commit()
        employee_context.update(employee_id=eid, uid=uid)
        return {
            "trusted_binding_fixture_only": True,
            "provisioned_role": "EMPLOYEE",
            "no_card_or_HR_role_from_launch": True,
            "old_episode_session_denied": True,
            "current_pointer_with_disabled_history_authenticates": True,
            "stale_pointer_does_not_provision_around_history": True,
        }

    campaign_context = {}

    def campaign_cas_schedule_real_worker():
        c, creator_id, _ = principal_login("HR_ADMIN", {"site_ids": ["GrPP"]})
        eid = employee_context["employee_id"]
        body = {
            "title": "Synthetic release18 support survey",
            "purpose": "WELLBEING",
            "description": "Voluntary identified test",
            "questions": [
                {
                    "question_key": "support",
                    "prompt": "Насколько доступна поддержка коллег?",
                    "type": "LIKERT",
                },
                {
                    "question_key": "comment",
                    "prompt": "Что могло бы помочь адаптации?",
                    "type": "TEXT",
                },
            ],
            "reason": "Synthetic isolated PostgreSQL verification",
        }
        response = post(c, "/survey-templates", body)
        assert response.status_code == 201
        tid = response.json()["id"]
        detail = c.get("/api/v1/survey-templates/" + tid).json()
        version = detail["versions"][0]
        vid = version["id"]
        path = "/api/v1/survey-template-versions/" + vid
        old_etag = version["etag"]
        headers = {
            "X-CSRF-Token": c.cookies.get("korsik_csrf"),
            "Idempotency-Key": str(uuid.uuid4()),
        }
        missing = c.patch(path, json=body, headers=headers)
        assert missing.status_code == 428
        saved = c.patch(
            path,
            json={**body, "description": "Checked identified voluntary test"},
            headers={
                **headers,
                "Idempotency-Key": str(uuid.uuid4()),
                "If-Match": old_etag,
            },
        )
        assert saved.status_code == 200
        etag = saved.json()["etag"]
        stale = c.patch(
            path,
            json=body,
            headers={
                **headers,
                "Idempotency-Key": str(uuid.uuid4()),
                "If-Match": old_etag,
            },
        )
        assert stale.status_code == 412
        for action, extra in [
            ("submit-review", {}),
            ("review", {"decision": "APPROVE"}),
            ("publish", {}),
        ]:
            r = post(
                c,
                f"/survey-template-versions/{vid}/{action}",
                {"reason": "Synthetic controlled review", **extra},
                etag,
            )
            assert r.status_code == 200, f"SURVEY_{action}_HTTP_{r.status_code}"
            etag = r.json()["etag"]
        immutable = c.patch(
            path,
            json=body,
            headers={**headers, "Idempotency-Key": str(uuid.uuid4()), "If-Match": etag},
        )
        assert immutable.status_code == 409
        start = now() + timedelta(seconds=2)
        campaign = {
            "name": "Synthetic release18 scheduled survey",
            "version_id": vid,
            "audience": {"employee_ids": [eid]},
            "schedule": {
                "timezone": "Europe/Moscow",
                "starts_at": start.isoformat(),
                "ends_at": (start + timedelta(days=3)).isoformat(),
                "response_hours": 48,
                "reminder_hours": [1, 24],
            },
            "reason": "Synthetic scheduled voluntary survey",
        }
        preview = post(c, "/survey-campaigns/preview", campaign)
        assert preview.status_code == 200 and preview.json()["recipient_count"] == 1
        pin = preview.json()["preview_hash"]
        changed = post(
            c,
            "/survey-campaigns",
            {**campaign, "name": "Changed after preview", "preview_hash": pin},
        )
        assert changed.status_code == 409
        created = post(c, "/survey-campaigns", {**campaign, "preview_hash": pin})
        assert created.status_code == 201
        cid = created.json()["id"]
        with factory() as db:
            # No scheduled model/connector job from unrelated fixtures may run.
            for job in db.scalars(
                select(Job).where(Job.status.in_(["PENDING", "RETRY"]))
            ):
                job.status = "CANCELLED"
            db.commit()
        time.sleep(max(0, (start - now()).total_seconds()) + 0.1)
        with factory() as db:
            sc.tick(db)
            sc.tick(db)
            db.commit()
            jobs = list(
                db.scalars(
                    select(Job).where(Job.kind == sc.DELIVER, Job.status == "PENDING")
                )
            )
            assert len(jobs) == 1
            jid = jobs[0].id
        assert worker.run_once(), "CAMPAIGN_JOB_NOT_CLAIMED"
        with factory() as db:
            job = db.get(Job, jid)
            assert job.status == "COMPLETED" and job.attempts == 1
            enrollments = list(
                db.scalars(
                    select(Entity).where(
                        Entity.kind == "survey_campaign_enrollment",
                        Entity.parent_id == cid,
                    )
                )
            )
            assert len(enrollments) == 1 and enrollments[0].status == "OPENED"
            rid = enrollments[0].data["run_id"]
            run = db.get(Entity, rid)
            assert run.data["campaign_id"] == cid and run.status == "IN_PROGRESS"
            opening = db.scalar(
                select(Outbox).where(Outbox.unique_key.like(f"survey:{rid}:q:%"))
            )
            opening.status = "ACCEPTED"
            opening.data = {**opening.data, "accepted_at": now().isoformat()}
            # A trusted receipt fixture tests the hook; no real MAX send occurs.
            d.on_outbox_accepted(db, opening)
            d.on_outbox_accepted(db, opening)
            db.commit()
            reminders = list(db.scalars(select(Job).where(Job.kind == sc.REMIND)))
            assert len(reminders) == 2
            assert not db.scalar(
                select(Job).where(Job.kind == "DOMAIN_SURVEY_REMINDER")
            )
            sc.tick(db)
            db.commit()
            assert (
                len(
                    list(
                        db.scalars(
                            select(Entity).where(
                                Entity.kind == "survey_run",
                                Entity.parent_id == enrollments[0].id,
                            )
                        )
                    )
                )
                == 1
            )
        campaign_context.update(
            creator_client=c,
            creator_id=creator_id,
            campaign_id=cid,
            run_id=rid,
            version_id=vid,
            campaign_payload=campaign,
        )
        return {
            "actual_HTTP_CAS_statuses": {
                "missing": 428,
                "stale": 412,
                "published_immutable": 409,
                "changed_preview": 409,
            },
            "actual_business_worker_claim_handler_commit": True,
            "worker_job_status": "COMPLETED",
            "worker_attempts": 1,
            "survey_runs": 1,
            "opening_receipt": "SYNTHETIC_TRUSTED_RECEIPT_NO_MAX_SEND",
            "configured_unique_reminders": 2,
        }

    def persisted_answers_scope_and_cancel():
        ctx = campaign_context
        employee = client()
        login = post(
            employee,
            "/auth/max-miniapp",
            {"init_data": signed(employee_context["uid"])},
        )
        assert login.status_code == 200
        rid, cid = ctx["run_id"], ctx["campaign_id"]
        for qid, value, revision in [
            ("CUSTOM-support", 4, 1),
            ("CUSTOM-comment", "Synthetic private optional response", 2),
        ]:
            result = post(
                employee,
                f"/survey-runs/{rid}/answers",
                {"question_id": qid, "value": value, "state_revision": revision},
            )
            assert result.status_code == 200, f"SURVEY_ANSWER_HTTP_{result.status_code}"
        finished = post(employee, f"/survey-runs/{rid}/commands", {"action": "FINISH"})
        assert finished.status_code == 200 and finished.json()["status"] == "COMPLETED"
        creator = ctx["creator_client"]
        responses = creator.get(f"/api/v1/survey-campaigns/{cid}/responses")
        assert responses.status_code == 200
        assert responses.json()["items"][0]["answers"]["CUSTOM-support"]["value"] == 4
        head, _, _ = principal_login("HEAD_TALENT", {"site_ids": ["GrPP"]})
        aggregate = head.get(f"/api/v1/survey-campaigns/{cid}/analytics")
        assert aggregate.status_code == 200 and aggregate.json()["suppressed"] is True
        assert (
            "Synthetic private" not in aggregate.text
            and "employee_id" not in aggregate.text
        )
        assert head.get(f"/api/v1/survey-campaigns/{cid}/responses").status_code == 403
        # Current creator scope, not the stored scope at scheduling, governs raw access.
        with factory() as db:
            user = db.get(User, ctx["creator_id"])
            user.scope = {"site_ids": ["GTP"]}
            db.commit()
        assert (
            creator.get(f"/api/v1/survey-campaigns/{cid}/responses").status_code == 404
        )
        with factory() as db:
            user = db.get(User, ctx["creator_id"])
            user.scope = {"site_ids": ["GrPP"]}
            db.commit()
        start = now() + timedelta(hours=1)
        future = {
            **ctx["campaign_payload"],
            "name": "Synthetic future cancellable survey",
            "schedule": {
                **ctx["campaign_payload"]["schedule"],
                "starts_at": start.isoformat(),
                "ends_at": (start + timedelta(days=2)).isoformat(),
            },
        }
        preview = post(creator, "/survey-campaigns/preview", future)
        assert preview.status_code == 200
        future["preview_hash"] = preview.json()["preview_hash"]
        created = post(creator, "/survey-campaigns", future)
        assert created.status_code == 201
        row = created.json()
        cancelled = post(
            creator,
            f"/survey-campaigns/{row['id']}/cancel",
            {"reason": "Synthetic future cancellation"},
            row["etag"],
        )
        assert (
            cancelled.status_code == 200 and cancelled.json()["status"] == "CANCELLED"
        )
        with factory() as db:
            run = db.get(Entity, rid)
            assert run.status == "COMPLETED" and len(run.data["answers"]) == 2
            assert not list(db.scalars(select(Entity).where(Entity.kind == "feedback")))
            assert not list(
                db.scalars(select(Job).where(Job.kind == "FEEDBACK_ANALYSIS"))
            )
            assert all(
                row.status == "CANCELLED"
                for row in db.scalars(
                    select(Entity).where(
                        Entity.kind == "survey_campaign_enrollment",
                        Entity.parent_id == created.json()["id"],
                    )
                )
            )
        return {
            "actual_HTTP_typed_answers_and_finish": True,
            "persisted_answer_count": 2,
            "no_general_feedback_or_sentiment_job": True,
            "HEAD_group_1_suppressed_and_raw_denied": True,
            "creator_current_scope_denies_old_raw_access": True,
            "future_campaign_cancel_preserves_previous_answers": True,
        }

    try:
        check(
            "miniapp_real_HTTP_role_cookie_replay_metadata_and_binding_revoke",
            role_session_metadata,
        )
        check("miniapp_concurrent_HTTP_replay_unique_session", concurrent_replay_unique)
        check(
            "miniapp_verified_employee_provision_and_episode_invalidation",
            employee_provision_and_revoke,
        )
        check(
            "survey_HTTP_CAS_preview_publish_durable_actual_worker_and_receipt",
            campaign_cas_schedule_real_worker,
        )
        check(
            "survey_HTTP_answers_aggregate_scope_and_future_cancel",
            persisted_answers_scope_and_cancel,
        )
        assert not transport_calls, "EXTERNAL_TRANSPORT_WAS_ATTEMPTED"
    finally:
        core.SessionLocal, dbmodule.SessionLocal, worker.SessionLocal = (
            old_core_factory,
            old_db_factory,
            old_worker_factory,
        )
        mini.get_settings, mini.read_secret = old_mini_settings, old_mini_secret
        core.app.dependency_overrides.clear()
        core.app.dependency_overrides.update(old_overrides)
        httpx.HTTPTransport.handle_request = previous_http
        httpx.AsyncHTTPTransport.handle_async_request = previous_async
        report["passed"] = sum(c["status"] == "PASS" for c in report["checks"])
        report["failed"] = sum(c["status"] == "FAIL" for c in report["checks"])
        report["external_http_attempts_blocked"] = len(transport_calls)
        report["status"] = (
            "PASS"
            if report["failed"] == 0
            and len(report["checks"]) == 5
            and not transport_calls
            else "FAIL"
        )
        report["company_accepted"] = False
        REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(
            json.dumps(
                {
                    "status": report["status"],
                    "passed": report["passed"],
                    "failed": report["failed"],
                }
            ),
            flush=True,
        )
        engine.dispose()
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
