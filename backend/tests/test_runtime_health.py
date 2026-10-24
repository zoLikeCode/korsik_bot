"""Progress probes and connector task health without MAX or model network I/O."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from korsik import runtime_health, max_connector, domain as d, survey_campaigns as sc
from korsik.models import Outbox
from test_domain import session as session, hr as hr, profile, employee_actor


@pytest.mark.parametrize(
    "stored,timestamp,max_age,expected",
    [
        ("1000.0", 1000.0, 30, True),
        ("1000.0", 1029.999, 30, True),
        ("1000.0", 1030.0, 30, True),
        ("1000.0", 1030.001, 30, False),
        ("1001.0", 1000.0, 30, False),
        ("not-a-timestamp", 1000.0, 30, False),
        ("", 1000.0, 30, False),
        ("nan", 1000.0, 30, False),
        ("inf", 1000.0, 30, False),
        ("-inf", 1000.0, 30, False),
    ],
)
def test_heartbeat_bounds_malformed_and_nonfinite_fail_closed(
    tmp_path, stored, timestamp, max_age, expected
):
    path = tmp_path / "heartbeat"
    path.write_text(stored)
    assert runtime_health.healthy(max_age, path=path, timestamp=timestamp) is expected


def test_missing_unreadable_and_atomic_heartbeat_progress(tmp_path, monkeypatch):
    path = tmp_path / "heartbeat"
    assert not runtime_health.healthy(30, path=path, timestamp=1000)
    assert not runtime_health.healthy(30, path=tmp_path, timestamp=1000)
    monkeypatch.setattr(runtime_health.time, "time", lambda: 1000.25)
    real_replace = runtime_health.os.replace
    replacements = []

    def observe_replace(source, target):
        assert source == path.with_suffix(".tmp") and target == path
        assert (
            not path.exists()
        )  # Readers see missing/old value, never a partial number.
        assert source.read_text() == "1000.25"
        replacements.append((source, target))
        real_replace(source, target)

    monkeypatch.setattr(runtime_health.os, "replace", observe_replace)
    runtime_health.heartbeat(path=path)
    assert len(replacements) == 1
    assert runtime_health.healthy(30, path=path, timestamp=1001)
    assert not path.with_suffix(".tmp").exists()


def test_heartbeat_failed_replace_preserves_previous_complete_value(
    tmp_path, monkeypatch
):
    path = tmp_path / "heartbeat"
    path.write_text("990.0")
    monkeypatch.setattr(runtime_health.time, "time", lambda: 1000)

    def denied_replace(*args):
        raise PermissionError("isolated replace fault")

    monkeypatch.setattr(runtime_health.os, "replace", denied_replace)
    with pytest.raises(PermissionError):
        runtime_health.heartbeat(path=path)
    assert path.read_text() == "990.0"
    assert runtime_health.healthy(30, path=path, timestamp=1000)


@pytest.mark.parametrize("task_state", ["RUNNING", "COMPLETED", "FAILED", "CANCELLED"])
def test_real_connector_http_health_tracks_lifespan_background_tasks(
    monkeypatch, task_state
):
    started = set()
    cleaned = set()

    def loop(name):
        async def run():
            started.add(name)
            try:
                if name == "incoming_loop" and task_state == "COMPLETED":
                    return
                if name == "incoming_loop" and task_state == "FAILED":
                    raise RuntimeError("isolated background task failure")
                await asyncio.Future()
            finally:
                cleaned.add(name)

        return run

    for name in (
        "incoming_loop",
        "outgoing_loop",
        "media_loop",
        "integration_test_loop",
    ):
        monkeypatch.setattr(max_connector, name, loop(name))
    monkeypatch.setattr(
        max_connector, "get_settings", lambda: SimpleNamespace(max_mode="POLLING")
    )
    monkeypatch.setattr(
        max_connector,
        "token",
        lambda: pytest.fail("No MAX credentials or network allowed"),
    )
    monkeypatch.setattr(
        max_connector,
        "SessionLocal",
        lambda: pytest.fail("No production database access allowed"),
    )
    monkeypatch.setattr(max_connector.app.state, "background_tasks", [], raising=False)

    async def scenario():
        async with max_connector.lifespan(max_connector.app):
            await asyncio.sleep(0)
            tasks = max_connector.app.state.background_tasks
            assert len(tasks) == 4
            assert started == {
                "incoming_loop",
                "outgoing_loop",
                "media_loop",
                "integration_test_loop",
            }
            if task_state == "CANCELLED":
                tasks[0].cancel()
                await asyncio.gather(tasks[0], return_exceptions=True)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=max_connector.app),
                base_url="http://isolated",
            ) as client:
                response = await client.get("/healthz")
            if task_state == "RUNNING":
                assert response.status_code == 200
                assert response.json() == {"status": "ok", "mode": "POLLING"}
                assert not any(task.done() for task in tasks)
            else:
                assert response.status_code == 503
                assert response.json()["detail"] == "background_task_unavailable"
        assert all(task.done() for task in tasks)
        assert cleaned == started

    asyncio.run(scenario())


@pytest.mark.parametrize("missing_count", [0, 1, 3])
def test_real_connector_http_health_rejects_missing_tasks(monkeypatch, missing_count):
    async def scenario():
        tasks = [asyncio.create_task(asyncio.sleep(3600)) for _ in range(missing_count)]
        monkeypatch.setattr(
            max_connector.app.state, "background_tasks", tasks, raising=False
        )
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=max_connector.app),
                base_url="http://isolated",
            ) as client:
                response = await client.get("/healthz")
            assert response.status_code == 503
            assert response.json()["detail"] == "background_task_unavailable"
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())


def test_custom_wellbeing_answers_do_not_change_onboarding_dashboard(
    session, hr, monkeypatch
):
    from test_survey_campaigns import published, open_campaign

    clock = [datetime(2026, 10, 24, 9, tzinfo=timezone.utc)]
    monkeypatch.setattr(sc, "now", lambda: clock[0])
    monkeypatch.setattr(d, "now", lambda: clock[0])
    employee = profile(session, hr)
    _, version = published(session, hr)
    baseline = d.dashboard_data(session, hr)
    campaign, runs = open_campaign(session, hr, version, [employee], clock, monkeypatch)
    run = runs[0]
    opening = session.scalar(
        select(Outbox).where(Outbox.unique_key.like(f"survey:{run.id}:q:%"))
    )
    opening.data = {**opening.data, "accepted_at": clock[0].isoformat()}
    d.on_outbox_accepted(session, opening)
    actor = employee_actor(employee)
    d.submit_survey_answer(
        session, actor, run.id, {"question_id": "CUSTOM-support", "value": 4}
    )
    d.submit_survey_answer(
        session,
        actor,
        run.id,
        {"question_id": "CUSTOM-help", "value": "Private optional wellbeing response"},
    )
    d.finish_survey(session, actor, run.id)
    assert run.status == "COMPLETED" and run.data["first_question_accepted_at"]
    actual = d.dashboard_data(session, hr)
    assert actual["metrics"] == baseline["metrics"]
    assert actual["numeric_averages"] == baseline["numeric_averages"] == []
    assert "Private optional wellbeing" not in str(actual)
    assert (
        sc.campaign_responses(campaign.id, session, hr)["items"][0]["answers"][
            "CUSTOM-support"
        ]["value"]
        == 4
    )
    assert sc.analytics(campaign.id, session, hr)["suppressed"]
    assert not d.entities(session, "feedback")
