"""Presentation data is scoped, coherent, repeatable and has no delivery jobs."""

import runpy
from pathlib import Path
from datetime import date
import pytest
from sqlalchemy import select, func
from korsik.models import User, Entity, Job, Outbox
from korsik.auth import ph, user_actor, Actor
from korsik.analytics_insights import build_insights
from korsik.organization import organization
from test_domain import session as session, hr as hr

DEMO = runpy.run_path(
    str(Path(__file__).resolve().parents[2] / "scripts/seed-defense-demo.py")
)


def test_demo_is_idempotent_and_usable_by_head_without_outbound_work(session, hr):
    old = Entity(
        id="untouched", kind="employee", data={"name": "Keep", "enabled": False}
    )
    session.add(old)
    session.flush()
    before_jobs = session.scalar(select(func.count()).select_from(Job))
    before_outbox = session.scalar(select(func.count()).select_from(Outbox))
    result = DEMO["seed_demo"](
        session, "Synthetic-only-password!", anchor=date(2026, 10, 25)
    )
    session.commit()
    assert result["created"] and result["employees"] == 15 and result["units"] == 10
    jury = session.get(User, DEMO["key"]("jury"))
    actor = user_actor(jury)
    assert actor.role == "HEAD_TALENT" and actor.scope == {"all": True}
    assert ph.verify(jury.password_hash, "Synthetic-only-password!")
    org = organization(session, actor)
    assert len(org["units"]) == 10 and len(org["employees"]) == 15
    data = build_insights(
        session, actor, {"period_from": "2026-10-01", "period_to": "2026-10-25"}
    )
    assert data["summary"]["employees"] == 15
    assert data["summary"]["surveys_delivered"] == 15
    assert data["summary"]["surveys_completed"] == 10
    assert data["summary"]["completion_rate"] == pytest.approx(2 / 3)
    assert len(data["survey_results"]) == 3
    assert data["survey_results"][0]["average"] == 8
    assert len(data["problems"]["items"]) == 2
    assert all(r["employee_count"] == 5 for r in data["problems"]["items"])
    manager = session.get(User, DEMO["key"]("manager-1"))
    assert build_insights(session, user_actor(manager))["summary"]["employees"] == 3
    employee = session.get(Entity, DEMO["key"]("employee-2"))
    employee.data = {**employee.data, "name": "Edited at presentation"}
    entity_count = session.scalar(select(func.count()).select_from(Entity))
    result = DEMO["seed_demo"](session, "Do-not-reset-this-password!")
    assert not result["created"] and ph.verify(
        jury.password_hash, "Synthetic-only-password!"
    )
    assert employee.data["name"] == "Edited at presentation"
    assert session.scalar(select(func.count()).select_from(Entity)) == entity_count
    assert session.get(Entity, "untouched").data == {"name": "Keep", "enabled": False}
    assert session.scalar(select(func.count()).select_from(Job)) == before_jobs
    assert session.scalar(select(func.count()).select_from(Outbox)) == before_outbox
    assert not list(
        session.scalars(select(Entity).where(Entity.kind == "principal_binding"))
    )


def test_demo_never_takes_over_existing_jury_account(session, hr):
    session.add(
        User(
            id="existing-jury",
            email="jury",
            name="Existing",
            role="HR_SPECIALIST",
            scope={},
            password_hash="KEEP",
            enabled=True,
        )
    )
    session.flush()
    with pytest.raises(ValueError, match="existing account"):
        DEMO["seed_demo"](session, "Synthetic-only-password!")
    session.rollback()
    assert not session.get(Entity, DEMO["key"]("dataset"))
