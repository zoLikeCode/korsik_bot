"""Reversible editor lifecycle and receipt-counted unfinished-step repeats."""

from copy import deepcopy
from datetime import timedelta
import json

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from korsik import domain as d, simple_scenarios as simple, transport_api as tx
from korsik.auth import Actor
from korsik.common import save_entity
from korsik.models import Entity
from korsik.scenario_engine import validate_graph
from test_domain import session as session, hr as hr, profile, req
from test_simple_scenarios import clock as clock, active, graph, program, queued, accept


def draft(db, hr, scenario=None, definition=None):
    if scenario is None:
        scenario = save_entity(db, "scenario", {"name": "Схема", "scope": {}})
    response = d.create_scenario_version(
        scenario.id,
        {"editor_mode": "SIMPLE", **({"graph": definition} if definition else {})},
        db,
        hr,
    )
    return scenario, db.get(Entity, json.loads(response.body)["id"])


def start_waiting(db, hr, clock, maximum=2):
    definition = graph()
    if maximum is not None:
        definition["resume_policy"]["max_reminders"] = maximum
    employee, scenario, version, journey = active(db, hr, clock, definition)
    simple.run_due(db, journey, employee, clock[0])
    accept(db, queued(db, journey)[0], clock)
    return employee, scenario, version, journey


def repeat(db, employee, journey, clock):
    clock[0] += timedelta(hours=2)
    simple.run_due(db, journey, employee, clock[0])
    return queued(db, journey)[-1]


def test_schema_rename_uses_etag_and_keeps_published_graph_and_journey(
    session, hr, clock
):
    employee, scenario, version, journey = active(session, hr, clock)
    old_graph, old_journey = deepcopy(version.data["graph"]), deepcopy(journey.data)
    stale = req(scenario)
    response = d.rename_scenario(
        scenario.id, {"name": " Новое название "}, req(scenario), session, hr
    )
    assert json.loads(response.body)["name"] == "Новое название"
    assert version.data["graph"] == old_graph and journey.data == old_journey
    with pytest.raises(HTTPException) as exc:
        d.rename_scenario(
            scenario.id, {"name": "Устаревшая правка"}, stale, session, hr
        )
    assert exc.value.status_code == 412


def test_published_schema_card_status_does_not_regress_when_draft_added(session, hr):
    scenario, published = program(session)
    _, version = draft(session, hr, scenario)
    row = next(
        row for row in d.scenarios(session, hr)["items"] if row["id"] == scenario.id
    )
    assert row["has_published_versions"] is True
    assert row["current_published_version_id"] == published.id
    assert (
        row["latest_version_id"] == version.id
        and row["latest_version_status"] == "DRAFT"
    )
    unpublished, _ = draft(session, hr)
    assert d.scenario_summary(session, unpublished)["has_published_versions"] is False


def test_archive_hides_parent_blocks_new_enrollments_and_keeps_pending_delivery_valid(
    session, hr, clock
):
    employee, scenario, version, journey = active(session, hr, clock)
    binding = save_entity(
        session,
        "max_binding",
        {"max_user_id": "99234", "episode_id": employee.data["episode_id"]},
        status="ACTIVE",
        parent_id=employee.id,
    )
    d.update(employee, binding_id=binding.id)
    simple.run_due(session, journey, employee, clock[0])
    outbound = queued(session, journey)[0]
    before = deepcopy(journey.data)
    d.archive_scenario(scenario.id, req(scenario), session, hr)
    assert scenario.status == "ARCHIVED" and version.status == "PUBLISHED"
    assert journey.data == before
    assert scenario.id not in {row["id"] for row in d.scenarios(session, hr)["items"]}
    assert scenario.id in {
        row["id"] for row in d.scenarios(session, hr, include_archived=True)["items"]
    }
    assert tx.valid_outbox(session, outbound) == ({"user_id": 99234}, None)
    accept(session, outbound, clock)
    simple.choose(
        session,
        employee,
        {"journey_id": journey.id, "step_key": "hello", "answer_key": "yes"},
    )
    assert simple.run_due(session, journey, employee, clock[0]) == 1
    other = profile(session, hr)
    with pytest.raises(HTTPException) as exc:
        d.enroll_employee(session, other, scenario.id)
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException) as exc:
        simple.enroll(session, other, scenario)
    assert exc.value.status_code == 409
    assert d.enroll_employee(session, employee, scenario.id).id == journey.id
    with pytest.raises(HTTPException) as exc:
        d.create_scenario_version(scenario.id, {"editor_mode": "SIMPLE"}, session, hr)
    assert exc.value.status_code == 409


def test_archived_parent_does_not_cancel_previously_pinned_unstarted_bot_start(
    session, hr, clock
):
    employee = profile(session, hr)
    d.update(employee, max_binding_status="VERIFIED")
    scenario, _ = program(session)
    journey = simple.enroll(session, employee, scenario)
    assert not journey.data.get("started_at")
    d.archive_scenario(scenario.id, req(scenario), session, hr)
    assert [j.id for j in simple.notify_bot_started(session, employee)] == [journey.id]
    assert simple.run_due(session, journey, employee, clock[0]) == 1


def test_schema_restore_recovers_history_without_retroactive_scheduled_broadcast(
    session, hr, clock
):
    employee = profile(session, hr)
    d.update(employee, max_binding_status="VERIFIED")
    definition = graph()
    scheduled = clock[0] + timedelta(days=1)
    definition["start_policy"] = {
        "condition": "SCHEDULED",
        "date": scheduled.date().isoformat(),
        "time": "18:00",
        "timezone": "Europe/Moscow",
    }
    scenario, version = program(session, definition)
    d.update(version, activation_at=clock[0].isoformat())
    d.archive_scenario(scenario.id, req(scenario), session, hr)
    clock[0] += timedelta(days=3)
    d.restore_scenario(scenario.id, req(scenario), session, hr)
    assert scenario.status == "ACTIVE" and version.status == "PUBLISHED"
    assert simple.enroll_future_programs(session) == 0
    assert not any(
        j.data["scenario_id"] == scenario.id
        for j in d.entities(session, "journey", employee.id)
    )


def test_deleted_draft_is_soft_recoverable_and_parent_must_be_active(session, hr):
    scenario, version = draft(session, hr)
    old_graph = deepcopy(version.data["graph"])
    d.delete_scenario_version(version.id, req(version), session, hr)
    assert version.status == "DELETED" and version.data["graph"] == old_graph
    assert not d.scenario_versions(scenario.id, session, hr)["items"]
    assert (
        d.scenario_versions(scenario.id, session, hr, include_deleted=True)["items"][0][
            "id"
        ]
        == version.id
    )
    d.archive_scenario(scenario.id, req(scenario), session, hr)
    with pytest.raises(HTTPException) as exc:
        d.restore_scenario_version(version.id, req(version), session, hr)
    assert exc.value.status_code == 409
    d.restore_scenario(scenario.id, req(scenario), session, hr)
    d.restore_scenario_version(version.id, req(version), session, hr)
    assert version.status == "DRAFT" and version.data["graph"] == old_graph


@pytest.mark.parametrize("status", ["PUBLISHED", "ARCHIVED", "IN_REVIEW", "APPROVED"])
def test_only_draft_can_be_deleted(session, hr, status):
    _, version = draft(session, hr)
    version.status = status
    session.flush()
    with pytest.raises(HTTPException) as exc:
        d.delete_scenario_version(version.id, req(version), session, hr)
    assert exc.value.status_code == 409


def test_draft_with_pinned_references_cannot_be_deleted(session, hr):
    scenario, version = draft(session, hr)
    save_entity(
        session,
        "journey",
        {"scenario_id": scenario.id, "version_id": version.id},
        status="SCHEDULED",
    )
    with pytest.raises(HTTPException) as exc:
        d.delete_scenario_version(version.id, req(version), session, hr)
    assert exc.value.status_code == 409 and version.status == "DRAFT"


@pytest.mark.parametrize(
    "role", ["EMPLOYEE", "MANAGER", "HEAD_TALENT", "PLATFORM_ADMIN"]
)
def test_lifecycle_actions_require_hr_role(session, hr, role):
    scenario, version = draft(session, hr)
    actor = Actor("reader", "Читатель", role, {"all": True})
    if role == "HEAD_TALENT":
        d.update(scenario, scope={"site_ids": ["GrPP"]})
        session.commit()
        actor = Actor(hr.id, "Head", role, {"site_ids": ["GrPP"]})
        foreign = Actor("foreign-head", "Head", role, {"site_ids": ["GTP"]})
        with pytest.raises(HTTPException) as exc:
            d.archive_scenario(scenario.id, req(scenario), session, foreign)
        assert exc.value.status_code == 404
        with pytest.raises(HTTPException) as exc:
            d.publish_scenario(version.id, {"reason": "Проверка прав"}, session, actor)
        assert exc.value.status_code == 403  # Publication remains HR_ADMIN-only.
        assert d.rename_scenario(scenario.id, {"name": "Имя"}, req(scenario), session, actor).status_code == 200
        assert d.delete_scenario_version(version.id, req(version), session, actor).status_code == 200
        assert d.archive_scenario(scenario.id, req(scenario), session, actor).status_code == 200
        assert scenario.status == "ARCHIVED" and version.status == "DELETED"
        return
    for action in (
        lambda: d.archive_scenario(scenario.id, req(scenario), session, actor),
        lambda: d.rename_scenario(
            scenario.id, {"name": "Имя"}, req(scenario), session, actor
        ),
        lambda: d.delete_scenario_version(version.id, req(version), session, actor),
    ):
        with pytest.raises(HTTPException) as exc:
            action()
        assert exc.value.status_code == 403


def test_lifecycle_scope_and_missing_stale_etag_guards(session, hr):
    scenario, version = draft(session, hr)
    scoped = Actor("scope-hr", "HR площадки", "HR_SPECIALIST", {"site_ids": ["GTP"]})
    with pytest.raises(HTTPException) as exc:
        d.archive_scenario(scenario.id, req(scenario), session, scoped)
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        d.archive_scenario(
            scenario.id, Request({"type": "http", "headers": []}), session, hr
        )
    assert exc.value.status_code == 428
    stale = req(version)
    d.rename_scenario_version(
        version.id, {"name": "Новое имя"}, req(version), session, hr
    )
    with pytest.raises(HTTPException) as exc:
        d.delete_scenario_version(version.id, stale, session, hr)
    assert exc.value.status_code == 412


def test_unpublished_approved_and_legacy_drafts_remain_editable(session, hr):
    scenario, version = draft(session, hr)
    version.status = "APPROVED"
    d.update(version, review={"old": True})
    session.flush()
    old_graph = deepcopy(version.data["graph"])
    d.rename_scenario_version(
        version.id, {"name": "Имя версии"}, req(version), session, hr
    )
    assert (
        version.status == "DRAFT"
        and version.data["graph"] == old_graph
        and version.data["review"] is None
    )
    version.status = "APPROVED"
    changed = deepcopy(old_graph)
    changed["steps"][0]["content"]["text_ty"] = "Обновлённый текст"
    session.flush()
    d.put_scenario_version(
        version.id, {"name": "Версия", "graph": changed}, req(version), session, hr
    )
    assert version.status == "DRAFT" and version.data["graph"] == changed
    original = d.entities(session, "scenario")[0]
    response = d.create_scenario_version(original.id, {}, session, hr)
    legacy = session.get(Entity, json.loads(response.body)["id"])
    legacy_graph = deepcopy(legacy.data["graph"])
    legacy_graph["steps"][0]["content"]["text_ty"] = "Обновление старого сценария"
    d.put_scenario_version(legacy.id, {"graph": legacy_graph}, req(legacy), session, hr)
    assert (
        "editor_mode" not in legacy.data["graph"]
        and legacy.data["graph"] == legacy_graph
    )


def test_zero_repeats_does_not_finish_step_or_block_actual_answer(session, hr, clock):
    employee, _, _, journey = start_waiting(session, hr, clock, maximum=0)
    clock[0] += timedelta(days=10)
    simple.run_due(session, journey, employee, clock[0])
    assert (
        len(queued(session, journey)) == 1
        and journey.data["waiting_step_key"] == "hello"
    )
    simple.choose(
        session,
        employee,
        {"journey_id": journey.id, "step_key": "hello", "answer_key": "yes"},
    )
    assert simple.run_due(session, journey, employee, clock[0]) == 1


def test_repeat_cap_counts_only_actual_receipts_and_duplicate_receipts_are_idempotent(
    session, hr, clock
):
    employee, _, _, journey = start_waiting(session, hr, clock, maximum=2)
    first = repeat(session, employee, journey, clock)
    assert journey.data["reminder_count"] == 0
    first.status = "UNKNOWN_DELIVERY"
    clock[0] += timedelta(days=2)
    simple.run_due(session, journey, employee, clock[0])
    assert len(queued(session, journey)) == 2 and journey.data["reminder_count"] == 0
    accept(session, first, clock)
    accept(session, first, clock)
    assert journey.data["reminder_count"] == 1
    second = repeat(session, employee, journey, clock)
    assert second.id != first.id and journey.data["reminder_count"] == 1
    accept(session, second, clock)
    assert journey.data["reminder_count"] == 2
    clock[0] += timedelta(days=10)
    simple.run_due(session, journey, employee, clock[0])
    assert len(queued(session, journey)) == 3
    assert journey.status != "COMPLETED" and journey.data["waiting_step_key"] == "hello"


def test_repeat_count_resets_per_waiting_step_and_old_receipt_cannot_consume_new_cap(
    session, hr, clock
):
    definition = graph()
    definition["resume_policy"]["max_reminders"] = 1
    definition["steps"][3]["transitions"] = [{"target_step_key": "ack"}]
    definition["steps"].append(
        {
            "step_key": "ack",
            "kind": "ANSWER",
            "answer": {"label_ty": "Понятно"},
            "transitions": [{"target_step_key": "finish"}],
        }
    )
    employee, _, _, journey = active(session, hr, clock, definition)
    simple.run_due(session, journey, employee, clock[0])
    accept(session, queued(session, journey)[0], clock)
    first = repeat(session, employee, journey, clock)
    accept(session, first, clock)
    assert journey.data["reminder_count"] == 1
    simple.choose(
        session,
        employee,
        {"journey_id": journey.id, "step_key": "hello", "answer_key": "yes"},
    )
    simple.run_due(session, journey, employee, clock[0])
    accept(session, queued(session, journey)[-1], clock)
    assert (
        journey.data["waiting_step_key"] == "next"
        and journey.data["reminder_count"] == 0
    )
    assert not simple.accept_resume(session, first.data, clock[0])
    second = repeat(session, employee, journey, clock)
    assert second.id != first.id and second.text == "Отлично"
    accept(session, second, clock)
    assert journey.data["reminder_count"] == 1


def test_existing_missing_limit_stays_unbounded_and_new_default_is_three(
    session, hr, clock
):
    assert simple.default_graph()["resume_policy"]["max_reminders"] == 3
    employee, _, version, journey = start_waiting(session, hr, clock, maximum=None)
    for _ in range(4):
        accept(session, repeat(session, employee, journey, clock), clock)
    assert journey.data["reminder_count"] == 4
    assert "max_reminders" not in version.data["graph"]["resume_policy"]


@pytest.mark.parametrize("value", [-1, 101, True, 1.5, None])
def test_repeat_limit_validation_rejects_invalid_values(value):
    definition = graph()
    definition["resume_policy"]["max_reminders"] = value
    assert not validate_graph(definition)["valid"]


def test_terminal_repeat_failure_does_not_consume_cap_or_create_retry_storm(
    session, hr, clock
):
    employee, _, _, journey = start_waiting(session, hr, clock, maximum=1)
    reminder = repeat(session, employee, journey, clock)
    reminder.status = "FAILED"
    clock[0] += timedelta(days=1)
    simple.run_due(session, journey, employee, clock[0])
    assert (
        journey.status == "PAUSED"
        and journey.data["review_reason"] == "DELIVERY_FAILED"
    )
    assert journey.data["reminder_count"] == 0 and len(queued(session, journey)) == 2
