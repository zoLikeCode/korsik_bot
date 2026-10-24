"""Employee administration/invitation transactions; no outbound network calls."""

from copy import deepcopy
from datetime import date, timedelta
import json
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from starlette.requests import Request

from korsik import chat, domain as d, max_events as mx, simple_scenarios as simple
from korsik.auth import Actor
from korsik.common import etag, save_entity
from korsik.models import Entity, Outbox, TransportDelivery, User
from test_domain import session as session, hr as hr, profile, req, employee_actor
from test_max import secret_settings as secret_settings, inbox, process
from test_simple_scenarios import (
    clock as clock,
    no_global_buttons as no_global_buttons,
    active,
    graph,
    queued,
    accept,
)


def administer(db, actor, employee, **changes):
    return d.employee_administration(employee.id, changes, req(employee), db, actor)


def invite(db, actor, employee):
    result = chat.issue_employee_invite(
        employee.id, chat.EmployeeInvitation(), actor, db
    )
    token = parse_qs(urlparse(result["url"]).query)["start"][0]
    return db.get(Entity, result["invitation_id"]), token


def redeem(db, token, user_id=81001):
    event = inbox(
        db,
        {
            "update_type": "bot_started",
            "user": {"user_id": user_id, "first_name": "Test"},
            "payload": token,
        },
    )
    process(db, event)
    return event


def scoped_hr(db, site_id):
    user = User(
        id="site-hr",
        email="site-hr@test.local",
        name="Site HR",
        role="HR_SPECIALIST",
        password_hash="unused",
        enabled=True,
        scope={"site_ids": [site_id]},
    )
    db.add(user)
    db.commit()
    return Actor(id=user.id, name=user.name, role=user.role, scope=user.scope)


def test_simplified_create_derives_site_and_creator_hr_without_changing_legacy(
    session, hr
):
    response = d.create_employee(
        {"name": "Новый", "start_date": "2026-10-24", "timezone": "Asia/Vladivostok"},
        session,
        hr,
    )
    employee = session.get(Entity, json.loads(response.body)["id"])
    assert employee.data["site_id"] == "GrPP"
    assert employee.data["employment_type"] == "OFFICE"
    assert employee.data["assigned_hr_id"] == hr.id
    assert employee.data["enabled"] is True
    assert employee.data["timezone"] == "Asia/Vladivostok"
    legacy = profile(session, hr)
    assert legacy.data.get("assigned_hr_id") is None


def test_default_site_stays_within_hr_scope_and_ambiguity_is_explicit(session, hr):
    other = next(s for s in d.entities(session, "site") if s.id != "GrPP")
    actor = scoped_hr(session, other.id)
    result = d.create_employee(
        {"name": "Scoped", "start_date": "2026-10-24"}, session, actor
    )
    employee = session.get(Entity, json.loads(result.body)["id"])
    assert employee.data["site_id"] == other.id
    with pytest.raises(HTTPException) as foreign:
        d.create_employee_record(
            session,
            actor,
            {"name": "Foreign", "site_id": "GrPP", "start_date": "2026-10-24"},
        )
    assert foreign.value.status_code == 403
    actor.scope = {
        "site_ids": [s.id for s in d.entities(session, "site") if s.id != "GrPP"]
    }
    # No scoped employees now make one of the eligible sites an intentional default.
    session.delete(employee)
    session.flush()
    with pytest.raises(HTTPException) as ambiguous:
        d.default_employee_site(session, actor)
    assert ambiguous.value.status_code == 409


def test_paginated_search_stage_scope_and_old_client_compatibility(session, hr):
    for index in range(13):
        e = profile(
            session,
            hr,
            name=f"Сотрудник {index:02d}",
            email=f"employee{index}@test.local",
        )
        if index == 12:
            d.update(e, employment_stage="ACTIVE")
    session.commit()
    assert len(d.list_employees(session, hr)["items"]) == 13
    first = d.list_employees(session, hr, page=1, page_size=10)
    assert (first["total"], first["pages"], len(first["items"])) == (13, 2, 10)
    last = d.list_employees(session, hr, page=999, page_size=10)
    assert last["page"] == 2 and len(last["items"]) == 3
    filtered = d.list_employees(
        session, hr, page=1, search="EMPLOYEE12@", stage="ACTIVE"
    )
    assert filtered["total"] == 1 and filtered["items"][0]["name"] == "Сотрудник 12"
    empty = d.list_employees(session, hr, page=1, search="does-not-exist")
    assert empty["total"] == 0 and empty["pages"] == empty["page"] == 1
    other = next(s for s in d.entities(session, "site") if s.id != "GrPP")
    actor = scoped_hr(session, other.id)
    assert d.list_employees(session, actor, page=1)["total"] == 0
    for invalid in ({"page": 0}, {"page_size": 101}, {"stage": "UNSUPPORTED"}):
        with pytest.raises(HTTPException) as error:
            d.list_employees(
                session,
                hr,
                page=invalid.get("page", 1),
                page_size=invalid.get("page_size", 10),
                stage=invalid.get("stage"),
            )
        assert error.value.status_code == 422


def test_administration_etag_role_scope_dates_and_ended_fences(session, hr):
    employee = profile(session, hr)
    old = req(employee)
    administer(
        session,
        hr,
        employee,
        start_date="2026-10-26",
        probation_end_date="2027-01-25",
        timezone="Asia/Yekaterinburg",
    )
    ep = session.get(Entity, employee.data["episode_id"])
    assert ep.data["start_date"] == "2026-10-26"
    with pytest.raises(HTTPException) as stale:
        d.employee_administration(employee.id, {"enabled": False}, old, session, hr)
    assert stale.value.status_code == 412
    with pytest.raises(HTTPException) as role:
        administer(session, employee_actor(employee), employee, enabled=False)
    assert role.value.status_code == 403
    other = next(s for s in d.entities(session, "site") if s.id != "GrPP")
    with pytest.raises(HTTPException) as scope:
        administer(session, scoped_hr(session, other.id), employee, enabled=False)
    assert scope.value.status_code == 404
    for invalid in (
        {"enabled": 0},
        {"start_date": None},
        {"probation_end_date": "2020-01-01"},
        {"timezone": "Invalid/Zone"},
        {"site_id": "GrPP"},
    ):
        with pytest.raises(HTTPException) as error:
            administer(session, hr, employee, **invalid)
        assert error.value.status_code == 422
    d.update(employee, employment_stage="ENDED")
    with pytest.raises(HTTPException) as ended:
        administer(session, hr, employee, enabled=True)
    assert ended.value.status_code == 409


def test_off_preserves_binding_episode_answers_and_user_paused_journey(
    session, hr, clock
):
    employee, _, _, journey = active(session, hr, clock)
    d.update(journey, answers={"kept": {"value": "yes"}}, completed_steps=["prior"])
    manual = save_entity(
        session,
        "journey",
        {"episode_id": employee.data["episode_id"]},
        parent_id=employee.id,
        status="PAUSED",
    )
    binding = save_entity(
        session,
        "max_binding",
        {"episode_id": employee.data["episode_id"], "max_user_id": "81009"},
        parent_id=employee.id,
    )
    d.update(employee, binding_id=binding.id)
    episode = employee.data["episode_id"]
    administer(session, hr, employee, enabled=False)
    assert employee.data["episode_id"] == episode and binding.status == "ACTIVE"
    assert employee.data["max_binding_status"] == "VERIFIED"
    assert journey.status == "PAUSED" and journey.data["access_suspended"]
    assert not manual.data.get("access_suspended")
    assert simple.run_due(session, journey, employee, clock[0]) == 0
    count = len(d.entities(session, "journey", employee.id))
    clock[0] += timedelta(days=2)
    administer(session, hr, employee, enabled=True)
    assert len(d.entities(session, "journey", employee.id)) == count
    assert manual.status == "PAUSED" and journey.status == "SCHEDULED"
    assert journey.data["answers"] == {"kept": {"value": "yes"}}
    assert journey.data["completed_steps"] == ["prior"]
    assert journey.data["last_reminder_at"] == clock[0].isoformat()


def test_off_suppresses_unsent_and_on_resumes_current_message_once(session, hr, clock):
    employee, _, _, journey = active(session, hr, clock)
    assert simple.run_due(session, journey, employee, clock[0]) == 1
    outbound = queued(session, journey)[-1]
    delivery = TransportDelivery(outbox_id=outbound.id, status="PENDING")
    session.add(delivery)
    session.flush()
    administer(session, hr, employee, enabled=False)
    assert outbound.status == delivery.status == "SUPPRESSED"
    clock[0] += timedelta(days=2)
    administer(session, hr, employee, enabled=True)
    assert simple.run_due(session, journey, employee, clock[0]) == 1
    assert simple.run_due(session, journey, employee, clock[0]) == 0
    assert len(queued(session, journey)) == 2
    assert queued(session, journey)[0].status == "SUPPRESSED"


def test_on_preserves_waiting_answer_and_delays_reminder_full_interval(
    session, hr, clock
):
    employee, _, _, journey = active(session, hr, clock)
    simple.run_due(session, journey, employee, clock[0])
    accept(session, queued(session, journey)[-1], clock)
    assert journey.data["waiting_step_key"] == "hello"
    administer(session, hr, employee, enabled=False)
    clock[0] += timedelta(days=4)
    administer(session, hr, employee, enabled=True)
    simple.run_due(session, journey, employee, clock[0])
    assert len(queued(session, journey)) == 1
    clock[0] += timedelta(hours=2)
    simple.run_due(session, journey, employee, clock[0])
    assert len(queued(session, journey)) == 2
    assert journey.data["waiting_step_key"] == "hello"


def test_on_skips_missed_timed_steps_without_claiming_completion(session, hr, clock):
    definition = graph()
    definition["steps"] = [
        deepcopy(definition["steps"][0]),
        deepcopy(definition["steps"][3]),
        deepcopy(definition["steps"][-1]),
    ]
    definition["steps"][0]["transitions"] = [{"target_step_key": "next"}]
    definition["steps"][1]["trigger"] = {
        "type": "ANCHOR_TIME",
        "offset_days": 1,
        "local_time": "10:00",
    }
    employee, _, _, journey = active(session, hr, clock, definition)
    simple.run_due(session, journey, employee, clock[0])
    accept(session, queued(session, journey)[-1], clock)
    administer(session, hr, employee, enabled=False)
    clock[0] += timedelta(days=3)
    administer(session, hr, employee, enabled=True)
    assert simple.run_due(session, journey, employee, clock[0]) == 0
    action = next(
        a
        for a in d.entities(session, "scheduled_action", journey.id)
        if a.data["step_key"] == "next"
    )
    assert (
        action.status == "SKIPPED"
        and action.data["skip_reason"] == "ACCESS_DISABLED_INTERVAL"
    )
    assert "next" not in journey.data["completed_steps"]
    assert len(queued(session, journey)) == 1


def test_unknown_delivery_is_never_automatically_resent_after_on(session, hr, clock):
    employee, _, _, journey = active(session, hr, clock)
    simple.run_due(session, journey, employee, clock[0])
    outbound = queued(session, journey)[-1]
    outbound.status = "UNKNOWN_DELIVERY"
    administer(session, hr, employee, enabled=False)
    administer(session, hr, employee, enabled=True)
    assert outbound.status == "UNKNOWN_DELIVERY"
    assert journey.status == "PAUSED"
    assert journey.data["review_reason"] == "DELIVERY_RECONCILIATION_REQUIRED"
    assert simple.run_due(session, journey, employee, clock[0]) == 0


def test_on_resumes_one_missed_timed_choice_without_fabricating_answer(
    session, hr, clock
):
    definition = graph()
    definition["steps"][0]["trigger"] = {
        "type": "ANCHOR_TIME",
        "offset_days": 1,
        "local_time": "10:00",
    }
    employee, _, _, journey = active(session, hr, clock, definition)
    administer(session, hr, employee, enabled=False)
    clock[0] += timedelta(days=3)
    administer(session, hr, employee, enabled=True)
    assert simple.run_due(session, journey, employee, clock[0]) == 1
    assert simple.run_due(session, journey, employee, clock[0]) == 0
    assert journey.status != "COMPLETED" and not journey.data["answers"]
    accept(session, queued(session, journey)[-1], clock)
    assert journey.data["waiting_step_key"] == "hello"
    assert journey.status != "COMPLETED"


def test_join_auto_verification_is_one_use_and_no_pending_approval_notice(
    session, hr, secret_settings
):
    employee = profile(session, hr)
    invitation, token = invite(session, hr, employee)
    event = redeem(session, token)
    assert invitation.status == "REDEEMED"
    binding = session.get(Entity, invitation.data["binding_id"])
    assert (
        binding.status == "ACTIVE" and employee.data["max_binding_status"] == "VERIFIED"
    )
    assert binding.data["verified_by"] == hr.id
    assert binding.data["verified_method"] == "HR_ISSUED_INVITATION"
    assert mx.current_employee(session, 81001).id == employee.id
    process(session, event)
    redeem(session, token, user_id=81002)
    assert mx.current_employee(session, 81002) is None
    assert len(d.entities(session, "max_binding", employee.id)) == 1
    assert not any(
        "HR должен подтвердить" in o.text for o in session.scalars(select(Outbox))
    )


@pytest.mark.parametrize(
    "invalid",
    ["expired", "ended", "off", "episode", "issuer_role", "issuer_off", "issuer_scope"],
)
def test_join_requires_live_invitation_employee_episode_and_current_issuer(
    session, hr, secret_settings, invalid
):
    employee = profile(session, hr)
    invitation, token = invite(session, hr, employee)
    issuer = session.get(User, hr.id)
    if invalid == "expired":
        d.update(invitation, expires_at=(mx.now() - timedelta(seconds=1)).isoformat())
    elif invalid == "ended":
        d.update(employee, employment_stage="ENDED")
    elif invalid == "off":
        administer(session, hr, employee, enabled=False)
    elif invalid == "episode":
        d.update(employee, episode_id="different")
    elif invalid == "issuer_role":
        issuer.role = "MANAGER"
    elif invalid == "issuer_off":
        issuer.enabled = False
    else:
        issuer.scope = {"site_ids": ["not-GrPP"]}
    session.commit()
    redeem(session, token)
    assert mx.current_employee(session, 81001) is None
    assert not d.entities(session, "max_binding", employee.id)
    assert invitation.status != "REDEEMED"


def test_join_collision_rolls_back_without_consuming_other_employee_token(
    session, hr, secret_settings
):
    first = profile(session, hr)
    _, first_token = invite(session, hr, first)
    redeem(session, first_token)
    second = profile(session, hr, name="Other")
    invitation, token = invite(session, hr, second)
    redeem(session, token)
    assert invitation.status == "ISSUED"
    assert not d.entities(session, "max_binding", second.id)
    assert second.data["max_binding_status"] == "UNBOUND"
    assert mx.current_employee(session, 81001).id == first.id
    redeem(session, token, user_id=81002)
    assert (
        invitation.status == "REDEEMED"
        and mx.current_employee(session, 81002).id == second.id
    )


def test_new_invitation_revokes_older_token_and_off_prevents_issue(
    session, hr, secret_settings
):
    employee = profile(session, hr)
    old, token = invite(session, hr, employee)
    current, _ = invite(session, hr, employee)
    assert old.status == "REVOKED" and current.status == "ISSUED"
    redeem(session, token)
    assert not d.entities(session, "max_binding", employee.id)
    administer(session, hr, employee, enabled=False)
    with pytest.raises(HTTPException) as disabled:
        invite(session, hr, employee)
    assert disabled.value.status_code == 409
