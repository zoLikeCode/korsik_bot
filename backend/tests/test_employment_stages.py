"""Virtual clocks exercise trusted scheduled stage transitions and stale guards."""

import json
from datetime import timedelta, date
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from korsik import domain as d, employment_stages as es
from korsik.auth import Actor
from korsik.models import Entity, Job, User
from test_domain import session as session, hr as hr, profile, req


def scheduled(db, actor, employee, *, stage="ACTIVE", effective=None):
    episode = db.get(Entity, employee.data["episode_id"])
    response = d.employment_episode_stage(
        episode.id,
        {
            "expected_episode_id": episode.id,
            "expected_state_version": episode.version,
            "stage": stage,
            "effective_at": (effective or (es.now() + timedelta(hours=2))).isoformat(),
            "reason": "HR подтвердил плановую дату выхода",
        },
        db,
        actor,
    )
    assert response.status_code == 202
    return db.get(Entity, json.loads(response.body)["id"])


def execute(db, op, monkeypatch):
    clock = d.aware(op.data["effective_at"]) + timedelta(seconds=1)
    monkeypatch.setattr(d, "now", lambda: clock)
    monkeypatch.setattr(es, "now", lambda: clock)
    job = db.scalar(select(Job).where(Job.payload["operation_id"].as_string() == op.id))
    job.status = "PROCESSING"
    job.lease_until = clock + timedelta(seconds=90)
    job.payload = {**job.payload, "_lease_token": "virtual-claim"}
    db.commit()
    assert d.process_domain_job(db, job)
    db.commit()
    return job


def test_future_stage_persists_due_and_applies_once_at_virtual_clock(
    session, hr, monkeypatch
):
    employee = profile(session, hr)
    assert employee.data["employment_stage"] == "PREBOARDING"
    op = scheduled(session, hr, employee)
    job = session.scalar(
        select(Job).where(Job.payload["operation_id"].as_string() == op.id)
    )
    assert (
        job.kind == es.JOB
        and d.aware(job.run_at) > es.now()
        and job.status == "PENDING"
    )
    assert employee.data["employment_stage"] == "PREBOARDING"
    listing = d.employment_stage_changes(op.data["episode_id"], session, hr)
    assert listing["active_operation_id"] == op.id
    assert listing["items"][0]["etag"] and "actor_pin" not in listing["items"][0]
    job = execute(session, op, monkeypatch)
    assert op.status == "SUCCEEDED" and employee.data["employment_stage"] == "ACTIVE"
    assert d.process_domain_job(session, job)
    session.commit()
    changes = [
        e
        for e in d.entities(session, "employment_episode_event", op.data["episode_id"])
        if e.data.get("operation_id") == op.id
    ]
    assert len(changes) == 1


def test_newer_schedule_immediate_command_and_cancel_fence_old_jobs(
    session, hr, monkeypatch
):
    employee = profile(session, hr)
    old = scheduled(session, hr, employee)
    current = scheduled(session, hr, employee)
    assert (
        old.status == "CANCELLED"
        and old.data["reason_code"] == "SUPERSEDED_BY_NEW_STAGE_COMMAND"
    )
    d.operation_cancel(
        current.id, {"reason": "План изменён HR"}, req(current), session, hr
    )
    assert current.status == "CANCELLED"
    execute(session, old, monkeypatch)
    execute(session, current, monkeypatch)
    assert employee.data["employment_stage"] == "PREBOARDING"
    # An immediate trusted update supersedes a later pending command as well.
    newer = scheduled(session, hr, employee, effective=es.now() + timedelta(days=1))
    episode = session.get(Entity, employee.data["episode_id"])
    d.employment_episode_stage(
        episode.id,
        {"stage": "ACTIVE", "reason": "Сотрудник фактически вышел"},
        session,
        hr,
    )
    assert newer.status == "CANCELLED" and employee.data["employment_stage"] == "ACTIVE"


@pytest.mark.parametrize("change", ["disabled", "role", "scope"])
def test_due_stage_rechecks_current_author_permissions(
    session, hr, monkeypatch, change
):
    employee = profile(session, hr)
    op = scheduled(session, hr, employee)
    author = session.get(User, hr.id)
    if change == "disabled":
        author.enabled = False
    elif change == "role":
        author.role = "HEAD_TALENT"
    else:
        author.scope = {"site_ids": ["GTP"]}
    session.commit()
    execute(session, op, monkeypatch)
    assert (
        op.status == "CANCELLED" and op.data["reason_code"] == "AUTHOR_ACCESS_CHANGED"
    )
    assert employee.data["employment_stage"] == "PREBOARDING"


def test_episode_changes_and_offboard_rehire_do_not_apply_old_stage(
    session, hr, monkeypatch
):
    employee = profile(session, hr)
    stale = scheduled(session, hr, employee)
    episode = session.get(Entity, employee.data["episode_id"])
    d.update(episode, start_date="2027-02-01")
    session.commit()
    execute(session, stale, monkeypatch)
    assert (
        stale.status == "CANCELLED"
        and stale.data["reason_code"] == "STALE_EPISODE_OR_STAGE_COMMAND"
    )
    pending = scheduled(session, hr, employee, effective=es.now() + timedelta(days=1))
    d.update(employee, employment_stage="ENDED")
    d.end_employee_access(session, employee, hr, "Прекращение найма")
    session.commit()
    assert (
        pending.status == "CANCELLED"
        and pending.data["reason_code"] == "EMPLOYMENT_ENDED"
    )
    old_episode = employee.data["episode_id"]
    d.rehire_employee_record(
        session,
        employee,
        {"site_id": "GrPP", "start_date": date.today().isoformat()},
        hr,
        "Повторный найм",
    )
    session.commit()
    assert employee.data["episode_id"] != old_episode
    execute(session, pending, monkeypatch)
    assert employee.data["employment_stage"] == "PREBOARDING"


def test_stage_operations_are_hr_scoped_and_cancel_requires_current_etag(session, hr):
    employee = profile(session, hr)
    op = scheduled(session, hr, employee)
    for actor in (
        Actor("p", "p", "PLATFORM_ADMIN", {"all": True}),
        Actor("m", "m", "MANAGER", {"all": True}),
        Actor("h", "h", "HEAD_TALENT", {"site_ids": ["GTP"]}),
        Actor("r", "r", "HR_ADMIN", {"site_ids": ["GTP"]}),
    ):
        with pytest.raises(HTTPException) as denied:
            d.operation_authorize(session, actor, op)
        assert denied.value.status_code in {403, 404}
    head = User(id="stage-head", name="Head", email="stage-head@example.test", role="HEAD_TALENT",
                scope={"site_ids": ["GrPP"]}, password_hash="unused", enabled=True)
    session.add(head); session.commit()
    actor = Actor(head.id, head.name, head.role, head.scope)
    assert d.operation_authorize(session, actor, op) is None
    old_request = req(op)
    assert d.operation_cancel(op.id, {"reason": "Отменено HR"}, old_request, session, actor).status_code == 200
    assert op.status == "CANCELLED"
    with pytest.raises(HTTPException) as stale:
        d.operation_cancel(
            op.id, {"reason": "Устаревшая отмена"}, old_request, session, actor
        )
    assert stale.value.status_code == 412


def test_expired_stage_executor_cannot_apply_without_a_new_claim(
    session, hr, monkeypatch
):
    employee = profile(session, hr)
    op = scheduled(session, hr, employee)
    clock = d.aware(op.data["effective_at"]) + timedelta(seconds=1)
    monkeypatch.setattr(es, "now", lambda: clock)
    job = session.scalar(
        select(Job).where(Job.payload["operation_id"].as_string() == op.id)
    )
    job.status = "PROCESSING"
    job.lease_until = clock - timedelta(seconds=1)
    job.payload = {**job.payload, "_lease_token": "expired-claim"}
    session.commit()
    with pytest.raises(RuntimeError, match="STAGE_LEASE_EXPIRED"):
        d.process_domain_job(session, job)
    assert (
        op.status == "SCHEDULED" and employee.data["employment_stage"] == "PREBOARDING"
    )


@pytest.mark.parametrize("invalid", [None, "not-a-date", "2028-01-01T10:00:00"])
def test_invalid_future_stage_time_is_not_applied(session, hr, invalid):
    employee = profile(session, hr)
    episode = session.get(Entity, employee.data["episode_id"])
    with pytest.raises(HTTPException) as error:
        d.employment_episode_stage(
            episode.id,
            {
                "stage": "ACTIVE",
                "effective_at": invalid,
                "reason": "Проверка даты",
                "expected_episode_id": episode.id,
                "expected_state_version": episode.version,
            },
            session,
            hr,
        )
    assert error.value.status_code == 422
    assert employee.data["employment_stage"] == "PREBOARDING"
