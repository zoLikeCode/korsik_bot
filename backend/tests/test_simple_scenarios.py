"""Deterministic scenario integration: no MAX requests or production data."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from korsik import domain as d, simple_scenarios as simple
from korsik.common import save_entity
from korsik.models import Entity, Outbox
from korsik.scenario_engine import validate_graph, active_path, simulate
from test_domain import session as session, hr as hr, profile, req
from test_survey_campaigns import published


def graph():
    return {
        "schema_version": "korsik-scenario-v2",
        "algorithm_version": "calendar-v1",
        "editor_mode": "SIMPLE",
        "start_step_key": "hello",
        "start_policy": {"condition": "ON_BOT_START", "timezone": "Europe/Moscow"},
        "resume_policy": {"reminder_hours": 2},
        "steps": [
            {
                "step_key": "hello",
                "kind": "MESSAGE",
                "sequence_no": 1,
                "trigger": {"type": "AFTER_PREVIOUS"},
                "content": {
                    "text_ty": "Как дела?",
                    "text_vy": "Как дела?",
                    "custom_prompts": [
                        {"id": "food", "label": "Обед", "query_text": "Где обедать?"}
                    ],
                },
                "transitions": [{"target_step_key": "yes"}, {"target_step_key": "no"}],
            },
            {
                "step_key": "yes",
                "kind": "ANSWER",
                "answer": {"label_ty": "Хорошо", "label_vy": "Хорошо"},
                "transitions": [{"target_step_key": "next"}],
            },
            {
                "step_key": "no",
                "kind": "ANSWER",
                "answer": {"label_ty": "Есть вопрос", "label_vy": "Есть вопрос"},
                "transitions": [{"target_step_key": "help"}],
            },
            {
                "step_key": "next",
                "kind": "MESSAGE",
                "trigger": {"type": "AFTER_PREVIOUS"},
                "content": {"text_ty": "Отлично", "text_vy": "Отлично"},
                "transitions": [{"target_step_key": "finish"}],
            },
            {
                "step_key": "help",
                "kind": "MESSAGE",
                "trigger": {"type": "AFTER_PREVIOUS"},
                "content": {"text_ty": "Слушаю", "text_vy": "Слушаю"},
                "transitions": [{"target_step_key": "finish"}],
            },
            {"step_key": "finish", "kind": "END", "transitions": []},
        ],
        "editor_layout": {},
    }


def program(db, definition=None):
    definition = deepcopy(definition or graph())
    scenario = save_entity(db, "scenario", {"name": "Тестовая схема", "scope": {}})
    version = save_entity(
        db,
        "scenario_version",
        {"scenario_id": scenario.id, "graph": definition},
        status="PUBLISHED",
        parent_id=scenario.id,
    )
    d.update(scenario, published_version_id=version.id)
    db.flush()
    return scenario, version


@pytest.fixture
def clock(monkeypatch):
    value = [datetime(2026, 10, 24, 9, tzinfo=timezone.utc)]
    monkeypatch.setattr(d, "now", lambda: value[0])
    return value


@pytest.fixture(autouse=True)
def no_global_buttons(monkeypatch):
    from korsik import basic_formulations

    monkeypatch.setattr(
        basic_formulations, "simple_basic_prompt_buttons", lambda *_: []
    )


def active(db, hr, clock, definition=None):
    employee = profile(db, hr, start_date="2026-10-24")
    d.update(employee, max_binding_status="VERIFIED")
    scenario, version = program(db, definition)
    journey = simple.enroll(db, employee, scenario)
    d.update(journey, started_at=clock[0].isoformat())
    simple.plan(db, journey, employee)
    return employee, scenario, version, journey


def queued(db, journey):
    return list(
        db.scalars(select(Outbox).where(Outbox.recipient_id == journey.parent_id))
    )


def accept(db, outbox, clock):
    outbox.status = "ACCEPTED"
    outbox.data = {**outbox.data, "accepted_at": clock[0].isoformat()}
    d.on_outbox_accepted(db, outbox)
    db.flush()


def test_simple_validation_choices_and_no_mandatory_gating():
    g = graph()
    g["steps"][0]["mandatory"] = True  # Legacy metadata cannot introduce gating.
    assert validate_graph(g)["valid"]
    assert active_path(g, {}, {}, {}) == {"hello"}
    answers = {"choice:hello": {"value": "yes", "outcome": "ANSWERED"}}
    assert active_path(g, answers, {}, {}) == {"hello", "yes", "next", "finish"}
    result = simulate(g, {"start_date": "2026-10-24"}, answers=answers)
    assert result["valid"] and result["outbound_messages_created"] == 0
    assert (
        next(row for row in result["timeline"] if row["step_key"] == "help")["status"]
        == "NOT_SELECTED_OR_WAITING_ANSWER"
    )


@pytest.mark.parametrize(
    "change",
    [
        lambda g: g["steps"][0]["transitions"].append({"target_step_key": "finish"}),
        lambda g: g["steps"][1].update(transitions=[{"target_step_key": "hello"}]),
        lambda g: g["steps"][0].update(
            trigger={"type": "AFTER_PREVIOUS", "offset_days": 2}
        ),
        lambda g: g.update(resume_policy={"reminder_hours": 0}),
        lambda g: g.update(
            start_policy={
                "condition": "SCHEDULED",
                "date": "2026-10-24",
                "time": "25:00",
            }
        ),
        lambda g: g.update(start_step_key="yes"),
    ],
)
def test_invalid_simple_graph_rejected(change):
    g = graph()
    change(g)
    assert not validate_graph(g)["valid"]


def test_delivery_receipt_waits_for_choice_and_cannot_switch_branch(session, hr, clock):
    employee, _, _, journey = active(session, hr, clock)
    assert simple.run_due(session, journey, employee, clock[0]) == 1
    first = queued(session, journey)[0]
    assert first.text == "Как дела?"
    assert [b["text"] for b in first.callback_buttons] == [
        "Хорошо",
        "Есть вопрос",
        "Обед",
    ]
    assert simple.run_due(session, journey, employee, clock[0]) == 0
    payload = {"journey_id": journey.id, "step_key": "hello", "answer_key": "yes"}
    with pytest.raises(HTTPException) as exc:
        simple.choose(session, employee, payload)
    assert exc.value.status_code == 409
    accept(session, first, clock)
    assert journey.data["waiting_step_key"] == "hello"
    assert "hello:STEP_COMPLETED" not in journey.data["events"]
    assert simple.choose(session, employee, payload)["duplicate"] is False
    assert simple.choose(session, employee, payload)["duplicate"] is True
    with pytest.raises(HTTPException) as exc:
        simple.choose(session, employee, {**payload, "answer_key": "no"})
    assert exc.value.status_code == 409
    assert simple.run_due(session, journey, employee, clock[0]) == 1
    second = queued(session, journey)[-1]
    assert second.text == "Отлично"
    assert len(queued(session, journey)) == 2
    accept(session, second, clock)
    simple.run_due(session, journey, employee, clock[0])
    assert journey.status == "COMPLETED"
    assert "help" not in journey.data["completed_steps"]


def test_reminder_resends_only_last_waiting_message_once(session, hr, clock):
    employee, _, _, journey = active(session, hr, clock)
    simple.run_due(session, journey, employee, clock[0])
    accept(session, queued(session, journey)[0], clock)
    clock[0] += timedelta(hours=1, minutes=59)
    simple.run_due(session, journey, employee, clock[0])
    assert len(queued(session, journey)) == 1
    clock[0] += timedelta(minutes=1)
    simple.run_due(session, journey, employee, clock[0])
    simple.run_due(session, journey, employee, clock[0])
    rows = queued(session, journey)
    assert len(rows) == 2 and rows[-1].text == rows[0].text
    assert rows[-1].callback_buttons == rows[0].callback_buttons
    assert journey.data["waiting_step_key"] == "hello"
    accept(session, rows[-1], clock)
    assert "hello:STEP_COMPLETED" not in journey.data["events"]
    assert journey.data["reminder_count"] == 1


def test_idempotent_bot_start_preserves_completed_journey_and_legacy(
    session, hr, clock
):
    employee = profile(session, hr)
    legacy = d.entities(session, "journey", employee.id)[0]
    legacy_state = deepcopy(legacy.data)
    scenario, _ = program(session)
    assert simple.notify_bot_started(session, employee) == []
    d.update(employee, max_binding_status="VERIFIED")
    first = simple.notify_bot_started(session, employee)
    second = simple.notify_bot_started(session, employee)
    assert len(first) == 1 and second == []
    assert first[0].data["scenario_id"] == scenario.id
    first[0].status = "COMPLETED"
    assert simple.notify_bot_started(session, employee) == []
    assert legacy.data == legacy_state
    assert not queued(session, first[0])


def test_start_date_and_scheduled_timezone_are_exact(session, hr, clock):
    g = graph()
    g["start_policy"] = {
        "condition": "SCHEDULED",
        "date": "2026-10-25",
        "time": "10:30",
        "timezone": "Europe/Moscow",
    }
    employee, _, _, journey = active(session, hr, clock, g)
    assert simple.run_due(session, journey, employee, clock[0]) == 0
    clock[0] = datetime(2026, 10, 25, 7, 30, tzinfo=timezone.utc)
    assert simple.run_due(session, journey, employee, clock[0]) == 1
    g["start_policy"] = {"condition": "START_DATE", "timezone": "Europe/Moscow"}
    g["steps"][0]["trigger"] = {
        "type": "ANCHOR_TIME",
        "anchor": "START_DATE",
        "offset_days": -2,
        "local_time": "09:15",
    }
    assert simple.due_for(
        g,
        g["steps"][0],
        {"start_date": "2026-10-25"},
        {"answers": {}, "events": {}},
        clock[0],
    ) == datetime(2026, 10, 23, 6, 15, tzinfo=timezone.utc)


def test_formulation_is_version_pinned_and_cannot_cross_employee(session, hr, clock):
    employee, _, version, journey = active(session, hr, clock)
    simple.run_due(session, journey, employee, clock[0])
    payload = {
        "journey_id": journey.id,
        "step_key": "hello",
        "prompt_id": "food",
        "query_text": "forged",
    }
    with pytest.raises(HTTPException):
        d.resolve_simple_formulation(session, employee, payload)
    accept(session, queued(session, journey)[0], clock)
    assert d.resolve_simple_formulation(session, employee, payload) == "Где обедать?"
    other = profile(session, hr)
    with pytest.raises(HTTPException) as exc:
        d.resolve_simple_formulation(session, other, payload)
    assert exc.value.status_code == 404


def test_survey_snapshot_all_questions_and_repeat_instances(session, hr, clock):
    employee = profile(session, hr)
    _, template_version = published(session, hr)
    g = graph()
    g["steps"] = [
        {
            "step_key": "survey_one",
            "kind": "SURVEY",
            "survey_template_version_id": template_version.id,
            "trigger": {"type": "AFTER_PREVIOUS"},
            "transitions": [{"target_step_key": "survey_two"}],
        },
        {
            "step_key": "survey_two",
            "kind": "SURVEY",
            "survey_template_version_id": template_version.id,
            "trigger": {"type": "AFTER_PREVIOUS"},
            "transitions": [{"target_step_key": "finish"}],
        },
        {"step_key": "finish", "kind": "END", "transitions": []},
    ]
    g["start_step_key"] = "survey_one"
    prepared = simple.prepare_graph(session, hr, g)
    assert validate_graph(prepared)["valid"]
    first, second = prepared["steps"][:2]
    assert len(first["survey"]["questions"]) == 2
    assert first["survey"]["survey_key"] != second["survey"]["survey_key"]
    assert first["survey"]["questions"][0]["maximum"] == 5
    assert first["survey"]["start_question_key"].startswith("survey_one:")
    scenario, version = program(session, prepared)
    d.update(employee, max_binding_status="VERIFIED")
    journey = simple.enroll(session, employee, scenario)
    d.update(journey, started_at=clock[0].isoformat())
    assert simple.run_due(session, journey, employee, clock[0]) == 1
    run = d.entities(session, "survey_run", journey.id)[0]
    assert len(run.data["definition"]["questions"]) == 2
    assert journey.data["waiting_step_key"] == "survey_one"
    accept(session, queued(session, journey)[0], clock)
    action = next(
        a
        for a in d.entities(session, "scheduled_action", journey.id)
        if a.data["step_key"] == "survey_one"
    )
    assert action.status == "WAITING_SURVEY"
    # A declined voluntary survey does not become mandatory gating.
    run.status = "DECLINED"
    assert simple.run_due(session, journey, employee, clock[0]) == 1
    assert len(d.entities(session, "survey_run", journey.id)) == 2


def test_new_simple_draft_does_not_convert_published_legacy(session, hr):
    scenario = d.entities(session, "scenario")[0]
    original = session.get(Entity, scenario.data["published_version_id"])
    original_graph = deepcopy(original.data["graph"])
    response = d.create_scenario_version(
        scenario.id, {"editor_mode": "SIMPLE"}, session, hr
    )
    version = session.get(Entity, json.loads(response.body)["id"])
    assert version.data["graph"]["editor_mode"] == "SIMPLE"
    assert len(version.data["graph"]["steps"]) == 1
    assert version.data["graph"]["steps"][0]["step_key"].startswith("message_")
    assert original.data["graph"] == original_graph
    assert original.status == "PUBLISHED"


def test_resuming_queued_message_is_fenced_after_choice(session, hr, clock):
    from korsik import transport_api as tx

    employee, _, _, journey = active(session, hr, clock)
    binding = save_entity(
        session,
        "max_binding",
        {
            "max_user_id": "93201",
            "episode_id": employee.data["episode_id"],
        },
        status="ACTIVE",
        parent_id=employee.id,
    )
    d.update(employee, binding_id=binding.id)
    simple.run_due(session, journey, employee, clock[0])
    accept(session, queued(session, journey)[0], clock)
    clock[0] += timedelta(hours=2)
    simple.run_due(session, journey, employee, clock[0])
    reminder = queued(session, journey)[-1]
    assert tx.valid_outbox(session, reminder) == ({"user_id": 93201}, None)
    simple.choose(
        session,
        employee,
        {"journey_id": journey.id, "step_key": "hello", "answer_key": "yes"},
    )
    assert tx.valid_outbox(session, reminder) == (None, "SCENARIO_ALREADY_ADVANCED")


@pytest.mark.parametrize(
    "state", ["PAUSED", "CANCELLED", "COMPLETED", "UNBOUND", "HUMAN", "WAITING_HR"]
)
def test_resume_does_not_emit_when_paused_unbound_or_in_human_dialog(
    session, hr, clock, state
):
    employee, _, _, journey = active(session, hr, clock)
    simple.run_due(session, journey, employee, clock[0])
    accept(session, queued(session, journey)[0], clock)
    clock[0] += timedelta(hours=2)
    if state == "UNBOUND":
        d.update(employee, max_binding_status="REVOKED")
    elif state in {"HUMAN", "WAITING_HR"}:
        save_entity(
            session,
            "conversation",
            {
                "employee_id": employee.id,
                "episode_id": employee.data["episode_id"],
                "mode": state,
            },
            parent_id=employee.id,
        )
    else:
        journey.status = state
    assert simple.run_due(session, journey, employee, clock[0]) == 0
    assert len(queued(session, journey)) == 1


def test_new_publication_does_not_enroll_or_send_to_existing_employee(
    session, hr, clock
):
    employee = profile(session, hr)
    existing = [j.id for j in d.entities(session, "journey", employee.id)]
    scenario = save_entity(session, "scenario", {"name": "Новый сценарий", "scope": {}})
    version = save_entity(
        session,
        "scenario_version",
        {"scenario_id": scenario.id, "graph": graph()},
        status="DRAFT",
        parent_id=scenario.id,
    )
    d.review_scenario(version.id, {}, session, hr)
    d.publish_scenario(version.id, {"reason": "Публикую новую схему"}, session, hr)
    assert [j.id for j in d.entities(session, "journey", employee.id)] == existing
    assert not list(session.scalars(select(Outbox)))


@pytest.mark.parametrize(
    "field,value",
    [
        ("start_step_key", []),
        ("start_policy", []),
        ("resume_policy", []),
        ("steps", None),
    ],
)
def test_malformed_graph_returns_validation_instead_of_crashing(field, value):
    g = graph()
    g[field] = value
    assert not validate_graph(g)["valid"]


def test_existing_legacy_enrollment_not_mutated_by_simple_republication(
    session, hr, clock
):
    employee = profile(session, hr)
    d.update(employee, max_binding_status="VERIFIED")
    legacy = d.entities(session, "journey", employee.id)[0]
    before = deepcopy(legacy.data)
    scenario = session.get(Entity, legacy.data["scenario_id"])
    version = save_entity(
        session,
        "scenario_version",
        {"graph": graph()},
        status="PUBLISHED",
        parent_id=scenario.id,
    )
    d.update(scenario, published_version_id=version.id)
    assert simple.notify_bot_started(session, employee) == []
    assert legacy.data == before


def test_queued_resume_not_duplicated_after_next_interval(session, hr, clock):
    employee, _, _, journey = active(session, hr, clock)
    simple.run_due(session, journey, employee, clock[0])
    accept(session, queued(session, journey)[0], clock)
    clock[0] += timedelta(hours=2)
    simple.run_due(session, journey, employee, clock[0])
    reminder = queued(session, journey)[-1]
    reminder.status = "QUEUED"
    clock[0] += timedelta(hours=2)
    simple.run_due(session, journey, employee, clock[0])
    assert len(queued(session, journey)) == 2


def test_replan_replaces_unsent_delivery_without_replaying_receipt(session, hr, clock):
    from korsik.models import TransportDelivery

    employee, _, _, journey = active(session, hr, clock)
    simple.run_due(session, journey, employee, clock[0])
    outbound = queued(session, journey)[0]
    outbound.status = "QUEUED"
    delivery = TransportDelivery(outbox_id=outbound.id, status="PENDING")
    session.add(delivery)
    session.flush()
    d.replan_journey(session, journey, employee, bump_revision=True)
    assert outbound.status == delivery.status == "CANCELLED"
    assert journey.data["schedule_revision"] == 2
    assert simple.run_due(session, journey, employee, clock[0]) == 1
    replacement = queued(session, journey)[-1]
    assert replacement.id != outbound.id
    # A late old receipt must not complete or await the replacement action.
    accept(session, outbound, clock)
    assert journey.data["waiting_step_key"] is None
    accept(session, replacement, clock)
    assert journey.data["waiting_step_key"] == "hello"


def test_uncertain_delivery_replan_requires_reconciliation(session, hr, clock):
    employee, _, _, journey = active(session, hr, clock)
    simple.run_due(session, journey, employee, clock[0])
    outbound = queued(session, journey)[0]
    outbound.status = "SENDING"
    d.replan_journey(session, journey, employee, bump_revision=True)
    assert journey.status == "PAUSED"
    assert journey.data["schedule_revision"] == 1
    assert outbound.status == "SENDING"
    assert journey.data["review_reason"] == "DELIVERY_RECONCILIATION_REQUIRED"


def test_message_length_rejected_and_variable_expansion_does_not_jam_transport(
    session, hr, clock
):
    g = graph()
    g["steps"][0]["content"]["text_ty"] = "A" * 3901
    assert not validate_graph(g)["valid"]
    g["steps"][0]["content"] = {
        "text_ty": "A" * 3890 + "{{name}}",
        "text_vy": "A" * 3890 + "{{name}}",
    }
    assert validate_graph(g)["valid"]
    employee, _, _, journey = active(session, hr, clock, g)
    assert simple.run_due(session, journey, employee, clock[0]) == 0
    assert (
        journey.status == "PAUSED"
        and journey.data["review_reason"] == "MESSAGE_LENGTH_EXCEEDED"
    )
    assert not queued(session, journey)


def test_message_material_attached_and_denied_material_stops_before_enqueue(
    session, hr, clock
):
    asset = save_entity(
        session,
        "asset",
        {
            "purpose": "SCENARIO_MATERIAL",
            "scope": {"employment_stages": ["PREBOARDING", "ACTIVE"]},
            "filename": "manual.pdf",
        },
        status="READY",
    )
    g = graph()
    g["steps"][0]["content"]["asset_ids"] = [asset.id]
    employee, _, _, journey = active(session, hr, clock, g)
    assert simple.run_due(session, journey, employee, clock[0]) == 1
    assert queued(session, journey)[0].attachments == [asset.id]
    asset.data = {**asset.data, "scope": {"site_ids": ["other-site"]}}
    other, _, _, second = active(session, hr, clock, g)
    assert simple.run_due(session, second, other, clock[0]) == 0
    assert (
        second.status == "PAUSED"
        and second.data["review_reason"] == "SCENARIO_ASSET_UNAVAILABLE"
    )


def test_scheduled_offset_cannot_fire_before_start_and_simulation_does_not_reverse_time():
    g = graph()
    g["start_policy"] = {
        "condition": "SCHEDULED",
        "date": "2026-10-25",
        "time": "14:00",
        "timezone": "Europe/Moscow",
    }
    g["steps"][0]["trigger"] = {
        "type": "ANCHOR_TIME",
        "anchor": "START_DATE",
        "offset_days": -1,
        "local_time": "10:00",
    }
    state = {"answers": {}, "events": {}}
    launch = datetime(2026, 10, 25, 11, tzinfo=timezone.utc)
    assert simple.due_for(g, g["steps"][0], {}, state, launch) == launch
    g["steps"][3]["trigger"] = {
        "type": "ANCHOR_TIME",
        "offset_days": 0,
        "local_time": "09:00",
    }
    result = simulate(g, {"start_date": "2026-10-25"}, answers={"choice:hello": "yes"})
    times = [row["due_at"] for row in result["timeline"] if row["due_at"]]
    assert times == sorted(times)


@pytest.mark.parametrize("condition", ["START_DATE", "SCHEDULED"])
def test_human_publication_enrolls_future_verified_employees_once_without_early_send(
    session, hr, clock, condition
):
    employee = profile(session, hr, start_date="2026-10-26")
    d.update(employee, max_binding_status="VERIFIED")
    unbound = profile(session, hr, start_date="2026-10-26")
    old = profile(session, hr, start_date="2026-10-20")
    d.update(old, max_binding_status="VERIFIED")
    g = graph()
    g["start_policy"] = {"condition": condition, "timezone": "Europe/Moscow"}
    if condition == "SCHEDULED":
        g["start_policy"].update(date="2026-10-26", time="10:00")
    scenario = save_entity(session, "scenario", {"name": "Будущая схема", "scope": {}})
    version = save_entity(
        session,
        "scenario_version",
        {"scenario_id": scenario.id, "graph": g},
        status="DRAFT",
        parent_id=scenario.id,
    )
    d.review_scenario(version.id, {}, session, hr)
    d.publish_scenario(version.id, {"reason": "Запускаю новую схему"}, session, hr)
    assert version.data["activation_at"] == clock[0].isoformat()
    assert simple.enroll_future_programs(session) == (
        1 if condition == "START_DATE" else 2
    )
    assert simple.enroll_future_programs(session) == 0
    new = [
        j
        for j in d.entities(session, "journey", employee.id)
        if j.data["scenario_id"] == scenario.id
    ]
    assert len(new) == 1
    assert simple.run_due(session, new[0], employee, clock[0]) == 0
    assert not queued(session, new[0])
    assert not any(
        j.data["scenario_id"] == scenario.id
        for j in d.entities(session, "journey", unbound.id)
    )
    if condition == "START_DATE":
        assert not any(
            j.data["scenario_id"] == scenario.id
            for j in d.entities(session, "journey", old.id)
        )


def test_past_scheduled_publication_rejected_without_activation(session, hr, clock):
    g = graph()
    g["start_policy"] = {
        "condition": "SCHEDULED",
        "date": "2026-10-23",
        "time": "10:00",
        "timezone": "Europe/Moscow",
    }
    scenario = save_entity(session, "scenario", {"name": "Прошедшая дата", "scope": {}})
    version = save_entity(
        session,
        "scenario_version",
        {"scenario_id": scenario.id, "graph": g},
        status="DRAFT",
        parent_id=scenario.id,
    )
    d.review_scenario(version.id, {}, session, hr)
    with pytest.raises(HTTPException) as exc:
        d.publish_scenario(version.id, {"reason": "Пробная публикация"}, session, hr)
    assert exc.value.status_code == 422
    assert version.status == "APPROVED" and "activation_at" not in version.data


def test_future_enroller_cannot_rewrite_existing_version_or_activate_old_unmarked_graph(
    session, hr, clock
):
    employee = profile(session, hr, start_date="2026-10-26")
    d.update(employee, max_binding_status="VERIFIED")
    legacy = d.entities(session, "journey", employee.id)[0]
    before = deepcopy(legacy.data)
    scenario = session.get(Entity, legacy.data["scenario_id"])
    g = graph()
    g["start_policy"] = {"condition": "START_DATE", "timezone": "Europe/Moscow"}
    version = save_entity(
        session,
        "scenario_version",
        {"graph": g, "activation_at": clock[0].isoformat()},
        status="PUBLISHED",
        parent_id=scenario.id,
    )
    d.update(scenario, published_version_id=version.id)
    assert simple.enroll_future_programs(session) == 0
    assert legacy.data == before
    another, _ = program(session, g)  # No human publication marker.
    assert simple.enroll_future_programs(session) == 0
    assert not any(
        j.data["scenario_id"] == another.id
        for j in d.entities(session, "journey", employee.id)
    )


@pytest.mark.parametrize("status", ["FAILED", "REJECTED", "CANCELLED", "SUPPRESSED"])
def test_terminal_delivery_failure_pauses_without_repeating_message(
    session, hr, clock, status
):
    employee, _, _, journey = active(session, hr, clock)
    assert simple.run_due(session, journey, employee, clock[0]) == 1
    outbound = queued(session, journey)[0]
    outbound.status = status
    assert simple.run_due(session, journey, employee, clock[0]) == 0
    assert (
        journey.status == "PAUSED"
        and journey.data["review_reason"] == "DELIVERY_FAILED"
    )
    assert len(queued(session, journey)) == 1


def test_cancelled_journey_is_not_recreated_by_next_bot_start(session, hr, clock):
    employee, _, _, journey = active(session, hr, clock)
    journey.status = "CANCELLED"
    assert simple.notify_bot_started(session, employee) == []
    assert (
        len(
            [
                j
                for j in d.entities(session, "journey", employee.id)
                if j.data["scenario_id"] == journey.data["scenario_id"]
            ]
        )
        == 1
    )


def test_adaptation_progress_counts_waiting_choice_as_incomplete_and_selected_branch_only(
    session, hr, clock
):
    employee, _, _, journey = active(session, hr, clock)
    simple.run_due(session, journey, employee, clock[0])
    accept(session, queued(session, journey)[0], clock)
    row = next(
        r for r in d.adaptation_rows(session, hr) if r["employee_id"] == employee.id
    )
    assert row["progress_basis"] == "SIMPLE_SELECTED_CONTENT_STEPS"
    assert row["completed_mandatory"] == 0 and row["total_mandatory"] == 1
    assert row["progress"]["rate"] == 0 and row["waiting_step_key"] == "hello"
    assert d.journey_view(journey, hr)["last_step_key"] == "hello"
    simple.choose(
        session,
        employee,
        {"journey_id": journey.id, "step_key": "hello", "answer_key": "yes"},
    )
    simple.run_due(session, journey, employee, clock[0])
    row = next(
        r for r in d.adaptation_rows(session, hr) if r["employee_id"] == employee.id
    )
    assert row["completed_mandatory"] == 1 and row["total_mandatory"] == 2
    assert row["progress"]["rate"] == 0.5
    accept(session, queued(session, journey)[-1], clock)
    simple.run_due(session, journey, employee, clock[0])
    row = next(
        r for r in d.adaptation_rows(session, hr) if r["employee_id"] == employee.id
    )
    assert row["completed_mandatory"] == row["total_mandatory"] == 2
    assert row["journey_status"] == "COMPLETED" and row["progress"]["rate"] == 1
