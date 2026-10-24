"""Source introduction uses accepted delivery and explicit question completion."""

import copy
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from korsik import chat, domain as d
from korsik.models import Outbox
from korsik.scenario_engine import validate_graph
from korsik.seed import SOURCE_ROWS, default_graph
from test_domain import session as session, hr as hr, profile, employee_actor


def action(db, journey, key):
    return next(
        row for row in d.entities(db, "scheduled_action", journey.id)
        if row.data["step_key"] == key
    )


def outputs(db):
    return list(db.scalars(select(Outbox).order_by(Outbox.created_at, Outbox.id)))


def accepted(db, outbox, at):
    # Simulate the existing trusted receipt hook; no MAX or model is invoked.
    outbox.status = "ACCEPTED"
    outbox.data = {**outbox.data, "accepted_at": at.isoformat()}
    d.on_outbox_accepted(db, outbox)


def test_serial_intro_preserves_source_copy_mapping_and_required_questions():
    original = copy.deepcopy(SOURCE_ROWS)
    graph = default_graph()
    assert validate_graph(graph)["valid"]
    assert SOURCE_ROWS == original
    mapped = {
        row for group in graph["source_mapping"]
        for row in range(group["row_from"], group["row_to"] + 1)
    }
    assert mapped == {row["row"] for row in original}
    steps = {step["step_key"]: step for step in graph["steps"]}
    intro = graph["source_mapping"][0]
    assert intro["row_from"] == 3 and intro["row_to"] == 8
    assert intro["step_keys"] == ["xls-r3", "xls-r4", "xls-r5", "xls-r7"]
    assert all(steps[key]["mandatory"] for key in intro["step_keys"])
    assert steps["xls-r4"]["content"]["text_ty"] == original[1]["text"].strip()
    assert steps["xls-r5"]["question"]["answer_binding"] == "ADDRESS_FORM"
    assert steps["xls-r7"]["question"]["answer_binding"] == "PREFERRED_NAME"
    for key in ("xls-r3", "xls-r4"):
        content = steps[key]["content"]
        assert content["optional_illustration_reference"]
        assert not content.get("missing_assets") and content["asset_ids"] == []
    # Actual materials elsewhere remain explicitly missing, not hidden globally.
    assert steps["xls-r9"]["content"]["missing_assets"]


@pytest.mark.parametrize("address_answer", [{"value": "VY"}, {"outcome": "DECLINED"}])
def test_intro_waits_for_receipts_and_address_completion_without_blocking_chat(
    session, hr, monkeypatch, address_answer
):
    clock = [datetime(2026, 10, 26, 9, tzinfo=timezone.utc)]
    monkeypatch.setattr(d, "now", lambda: clock[0])
    monkeypatch.setattr(chat, "now", lambda: clock[0])
    # A late preboarding enrollment used to deliver all four intro actions at once.
    employee = profile(session, hr, start_date="2026-10-26")
    journey = d.entities(session, "journey", employee.id)[0]
    assert d.run_due_scenarios(session) == 0
    d.update(employee, max_binding_status="VERIFIED")
    assert d.run_due_scenarios(session) == 1
    assert [out.data["step_key"] for out in outputs(session)] == ["xls-r3"]
    assert not d.entities(session, "scenario_question", journey.id)

    # Ten minutes queued is not a delivered introduction and releases no follow-up.
    clock[0] += timedelta(minutes=10)
    assert d.run_due_scenarios(session) == 0
    assert action(session, journey, "xls-r4").data["due_at"] is None
    accepted(session, outputs(session)[0], clock[0])
    clock[0] += timedelta(seconds=59)
    assert d.run_due_scenarios(session) == 0
    clock[0] += timedelta(seconds=1)
    assert d.run_due_scenarios(session) == 1
    welcome = next(out for out in outputs(session) if out.data["step_key"] == "xls-r4")
    assert "Привет-привет-привет!" in welcome.text
    assert "Вложение пока не предоставлено" not in welcome.text
    assert action(session, journey, "xls-r5").data["due_at"] is None

    clock[0] += timedelta(minutes=4)
    accepted(session, welcome, clock[0])
    clock[0] += timedelta(seconds=59)
    assert d.run_due_scenarios(session) == 0
    clock[0] += timedelta(seconds=1)
    assert d.run_due_scenarios(session) == 1
    address = next(q for q in d.entities(session, "scenario_question", journey.id)
                   if q.data["step_key"] == "xls-r5")
    address_delivery = next(out for out in outputs(session) if out.data["step_key"] == "xls-r5")
    accepted(session, address_delivery, clock[0])
    assert address.status == "OPEN"
    assert action(session, journey, "xls-r7").data["due_at"] is None

    # A normal free-form question creates its independent RAG turn, not an answer
    # to the address preference and not a completion event for the next step.
    actor = employee_actor(employee)
    conversation = chat.conversation_for(session, employee)
    turn = chat.submit_turn(session, actor, conversation,
                            {"kind": "TEXT", "text": "Где найти правила компании?"},
                            str(uuid.uuid4()))
    assert turn["dispatch"] == "BOT_ANSWER"
    assert address.status == "OPEN" and "WELCOME-ADDRESS" not in journey.data["answers"]
    clock[0] += timedelta(hours=1)
    assert d.run_due_scenarios(session) == 0
    assert action(session, journey, "xls-r7").data["due_at"] is None

    d.scenario_question_answer(session, actor, address.id, address_answer)
    assert address.status == "COMPLETED"
    assert journey.data["answers"]["WELCOME-ADDRESS"]["outcome"] == (
        "DECLINED" if "outcome" in address_answer else "ANSWERED"
    )
    assert d.run_due_scenarios(session) == 1
    name = next(q for q in d.entities(session, "scenario_question", journey.id)
                if q.data["step_key"] == "xls-r7")
    assert name.status == "OPEN" and name.data["question"]["answer_binding"] == "PREFERRED_NAME"
    assert [out.data["step_key"] for out in outputs(session)] == [
        "xls-r3", "xls-r4", "xls-r5", "xls-r7"
    ]
