"""Free branch answers use the durable scenario state, never an AI answer job."""

from copy import deepcopy
from datetime import timedelta
import json

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from korsik import (
    basic_formulations as basic,
    chat,
    domain as d,
    max_events as mx,
    simple_scenarios as simple,
    transport_api as tx,
)
from korsik.models import Entity, Job, Message
from korsik.common import save_entity
from test_domain import session, hr, employee_actor, profile
from test_simple_scenarios import active, graph, clock, queued, accept
from test_basic_formulations import save as save_formulations
from test_max import secret_settings, inbox, process, callback_event


def free_graph():
    value = graph()
    value["steps"][2]["answer"]["free_text"] = True
    return value


def ready(db, actor, clock):
    employee, scenario, version, journey = active(db, actor, clock, free_graph())
    binding = save_entity(
        db,
        "max_binding",
        {"max_user_id": "12345", "episode_id": employee.data["episode_id"]},
        status="ACTIVE",
        parent_id=employee.id,
    )
    d.update(employee, binding_id=binding.id)
    conv = chat.conversation_for(db, employee)
    simple.run_due(db, journey, employee, clock[0])
    message = next(row for row in queued(db, journey) if row.text == "Как дела?")
    accept(db, message, clock)
    return employee, journey, conv, message


def choice(journey, key="no"):
    return {
        "action": "SCENARIO_CHOICE",
        "journey_id": journey.id,
        "step_key": "hello",
        "answer_key": key,
    }


def test_validation_and_simulation_require_text_before_following_free_branch():
    g = free_graph()
    assert simple.validate(g)["valid"]
    selected = {"choice:hello": {"value": "no"}}
    assert simple.selected_path(g, selected) == ["hello"]
    preview = simple.simulate(g, {"start_date": "2026-10-24"}, selected)
    assert preview["timeline"][0]["status"] == "WAITING_FREE_TEXT"
    selected["choice:hello"]["free_text"] = "Нужна помощь с пропуском"
    assert simple.selected_path(g, selected) == ["hello", "no", "help", "finish"]
    assert (
        simple.simulate(g, {"start_date": "2026-10-24"}, selected)["timeline"][0][
            "status"
        ]
        == "READY"
    )
    g["steps"][2]["answer"]["free_text"] = "yes"
    assert not simple.validate(g)["valid"]


def test_signed_max_choice_next_text_and_replays_never_queue_rag(
    session, hr, clock, secret_settings
):
    employee, journey, conv, message = ready(session, hr, clock)
    signed = tx.signed_buttons(session, message)
    raw = next(
        button["payload"]
        for row in signed
        for button in row
        if button["text"] == "Есть вопрос"
    )
    process(session, callback_event(session, 12345, raw))
    process(session, callback_event(session, 12345, raw))
    prompts = [
        row for row in queued(session, journey) if row.text == simple.FREE_ANSWER_PROMPT
    ]
    assert len(prompts) == 1 and "choice:hello" not in journey.data["answers"]
    assert tx.valid_outbox(session, prompts[0]) == ({"user_id": 12345}, None)
    event = inbox(
        session,
        {
            "update_type": "message_created",
            "message": {
                "sender": {"user_id": 12345, "is_bot": False},
                "recipient": {"chat_type": "dialog", "chat_id": 100},
                "body": {"text": "Мне нужен пропуск на завод", "mid": "free-1"},
            },
        },
    )
    process(session, event)
    process(session, event)
    session.expire_all()
    journey = session.get(Entity, journey.id)
    response = journey.data["answers"]["choice:hello"]
    assert (
        response["value"] == "no"
        and response["free_text"] == "Мне нужен пропуск на завод"
    )
    saved = session.get(Message, response["message_id"])
    assert saved.text == response["free_text"] and saved.conversation_id == conv.id
    turns = d.entities(session, "conversation_turn", conv.id)
    assert len(turns) == 1 and turns[0].data["dispatch"] == "SCENARIO_ANSWER"
    assert not list(session.scalars(select(Job).where(Job.kind == "ANSWER")))
    assert not d.entities(session, "answer", conv.id)
    assert tx.valid_outbox(session, prompts[0])[0] is None
    assert simple.choose(session, employee, choice(journey))["duplicate"]
    assert (
        len(
            [
                row
                for row in queued(session, journey)
                if row.text == simple.FREE_ANSWER_PROMPT
            ]
        )
        == 1
    )
    simple.run_due(session, journey, employee, clock[0])
    emitted = [row.text for row in queued(session, journey)]
    assert emitted.count("Слушаю") == 1 and "Отлично" not in emitted
    simple.run_due(session, journey, employee, clock[0])
    assert [row.text for row in queued(session, journey)].count("Слушаю") == 1


def test_client_id_replay_does_not_capture_following_question(session, hr, clock):
    employee, journey, conv, _ = ready(session, hr, clock)
    simple.choose(session, employee, choice(journey))
    actor = employee_actor(employee)
    first = chat.submit_turn(
        session, actor, conv, {"kind": "TEXT", "text": "Да, расскажите"}, "repeat-free"
    )
    again = chat.submit_turn(
        session, actor, conv, {"kind": "TEXT", "text": "Да, расскажите"}, "repeat-free"
    )
    assert first["id"] == again["id"] and again["dispatch"] == "SCENARIO_ANSWER"
    with pytest.raises(HTTPException) as conflict:
        chat.submit_turn(
            session,
            actor,
            conv,
            {"kind": "TEXT", "text": "Другой текст"},
            "repeat-free",
        )
    assert conflict.value.status_code == 409
    assert (
        len(
            list(
                session.scalars(
                    select(Message).where(
                        Message.conversation_id == conv.id,
                        Message.author_type == "EMPLOYEE",
                    )
                )
            )
        )
        == 1
    )


def test_formulation_buttons_do_not_consume_pending_free_response(session, hr, clock):
    employee, journey, conv, _ = ready(session, hr, clock)
    simple.choose(session, employee, choice(journey))
    mx.handle_callback(
        session,
        employee,
        conv,
        {
            "action": "SCENARIO_FORMULATION",
            "journey_id": journey.id,
            "step_key": "hello",
            "prompt_id": "food",
        },
        "custom-query",
    )
    assert simple.pending_free_answer(session, employee, conv) is not None
    assert "choice:hello" not in journey.data["answers"]
    assert (
        d.entities(session, "conversation_turn", conv.id)[0].data["dispatch"]
        == "BOT_ANSWER"
    )
    base = basic.basic_prompts_for(session, employee)["items"][0]["action"]
    mx.handle_callback(session, employee, conv, base, "base-query")
    assert simple.pending_free_answer(session, employee, conv) is not None


def test_reminder_asks_for_free_text_and_is_fenced_when_fixed_option_wins(
    session, hr, clock
):
    employee, journey, conv, _ = ready(session, hr, clock)
    simple.choose(session, employee, choice(journey))
    initial = next(
        row for row in queued(session, journey) if row.text == simple.FREE_ANSWER_PROMPT
    )
    accept(session, initial, clock)
    clock[0] += timedelta(hours=2)
    simple.run_due(session, journey, employee, clock[0])
    reminder = next(
        row for row in queued(session, journey) if (row.data or {}).get("simple_resume")
    )
    assert (
        reminder.text == simple.FREE_ANSWER_PROMPT and reminder.callback_buttons == []
    )
    assert tx.valid_outbox(session, reminder)[0] is not None
    simple.choose(session, employee, choice(journey, "yes"))
    assert simple.pending_free_answer(session, employee, conv) is None
    assert tx.valid_outbox(session, reminder)[0] is None
    assert journey.data["answers"]["choice:hello"]["value"] == "yes"


def test_new_survey_input_episode_or_step_never_steals_text(session, hr, clock):
    employee, journey, conv, _ = ready(session, hr, clock)
    simple.choose(session, employee, choice(journey))
    capture = simple.pending_free_answer(session, employee, conv)
    newer = save_entity(
        session,
        "input_capture",
        {"episode_id": employee.data["episode_id"]},
        status="OPEN",
        parent_id=employee.id,
    )
    newer.created_at = capture.created_at + timedelta(seconds=1)
    assert simple.pending_free_answer(session, employee, conv) is None
    assert capture.status == "CANCELLED"
    newer.status = "CANCELLED"
    simple.choose(session, employee, choice(journey))
    other = simple.pending_free_answer(session, employee, conv)
    d.update(employee, episode_id="new-hire-episode")
    assert (
        simple.pending_free_answer(session, employee, conv) is None
        and other.status == "CANCELLED"
    )
    with pytest.raises(HTTPException):
        simple.choose(session, employee, choice(journey))


def test_global_topics_never_replace_scenario_keyboard(session, hr, clock):
    employee, _, version, journey = active(session, hr, clock)
    save_formulations(session, hr, "Общее", "Общий вопрос")
    step = deepcopy(next(row for row in version.data["graph"]["steps"] if row["step_key"] == "help"))
    step["content"]["custom_prompts"] = [{"id": "own", "label": "Обед", "query_text": "Где обедать?"}]
    buttons = simple.message_buttons(session, journey, version.data["graph"], step, employee)
    assert [b["text"] for b in buttons] == ["Обед"]
    assert buttons[0]["payload"]["action"] == "SCENARIO_FORMULATION"
    save_formulations(session, hr, "Новое", "Изменённый общий вопрос")
    assert simple.message_buttons(session, journey, version.data["graph"], step, employee) == buttons
    step["content"].pop("custom_prompts")
    assert simple.message_buttons(session, journey, version.data["graph"], step, employee) == []


def test_new_explicit_free_option_targets_only_its_journey(session, hr, clock):
    from test_simple_scenarios import program

    employee, first, conv, _ = ready(session, hr, clock)
    scenario, _ = program(session, free_graph())
    second = simple.enroll(session, employee, scenario)
    d.update(second, started_at=clock[0].isoformat())
    simple.plan(session, second, employee)
    simple.run_due(session, second, employee, clock[0])
    message = next(
        row
        for row in queued(session, second)
        if (row.data or {}).get("journey_id") == second.id
    )
    accept(session, message, clock)
    simple.choose(session, employee, choice(first))
    old = simple.pending_free_answer(session, employee, conv)
    simple.choose(session, employee, choice(second))
    assert old.status == "CANCELLED"
    response = chat.submit_turn(
        session,
        employee_actor(employee),
        conv,
        {"kind": "TEXT", "text": "Ответ второй схемы"},
        "second-journey",
    )
    assert response["scenario_response"]["journey_id"] == second.id
    assert "choice:hello" not in first.data["answers"]
    assert second.data["answers"]["choice:hello"]["free_text"] == "Ответ второй схемы"


def test_formulations_cannot_replace_a_branch_with_the_same_label(session, hr, clock):
    employee, _, version, journey = active(session, hr, clock)
    save_formulations(session, hr, "Хорошо", "Совсем другой вопрос")
    buttons = simple.message_buttons(
        session,
        journey,
        version.data["graph"],
        version.data["graph"]["steps"][0],
        employee,
    )
    matching = [button for button in buttons if button["text"] == "Хорошо"]
    assert len(matching) == 1 and matching[0]["payload"]["action"] == "SCENARIO_CHOICE"


def test_old_default_max_button_is_rejected_with_a_fresh_scoped_keyboard(
    session, hr, clock, secret_settings
):
    import uuid

    employee, journey, conv, _ = ready(session, hr, clock)
    menu = basic.basic_prompts_for(session, employee)
    action = dict(menu["items"][1]["action"])
    action["menu_version_id"] = str(
        uuid.uuid5(uuid.NAMESPACE_URL, menu["menu_id"] + ":defaults-v1")
    )
    old = d.emit(
        session,
        employee.id,
        "Старое приветствие",
        [{"text": "Коммерческая тайна", "payload": action}],
        unique_key="old-default-greeting",
    )
    raw = tx.signed_buttons(session, old)[0][0]["payload"]
    event = callback_event(session, 12345, raw)
    process(session, event)
    recovered = next(
        row
        for row in queued(session, journey)
        if row.unique_key == f"event-rejected:{event.id}"
    )
    assert "Кнопки обновились" in recovered.text
    button = next(
        button
        for button in recovered.callback_buttons
        if button["text"] == "Коммерческая тайна"
    )
    assert button["payload"]["menu_version_id"] == menu["menu_version_id"]
    assert button["payload"]["menu_version_id"] != action["menu_version_id"]
    assert tx.signed_buttons(session, recovered)
    assert not d.entities(session, "conversation_turn", conv.id)
    assert not d.entities(session, "answer", conv.id)
    assert "choice:hello" not in journey.data["answers"]


def test_old_queued_scenario_keyboard_drops_global_topics_without_losing_choices(session, hr, clock, secret_settings):
    employee, journey, conv, outbound = ready(session, hr, clock)
    base=basic.basic_prompts_for(session,employee)['items'][0]
    outbound.callback_buttons = [*outbound.callback_buttons, {'text':base['label'],'payload':base['action']}, {'text':'Старый вопрос','payload':{'action':'SCENARIO_ANSWER','scenario_question_id':'legacy','value':'yes'}}]
    keyboard = [button for row in tx.signed_buttons(session,outbound) for button in row]
    assert base['label'] not in [b['text'] for b in keyboard]
    assert {'Хорошо','Есть вопрос','Обед','Старый вопрос'}.issubset({b['text'] for b in keyboard})
    menu = d.emit(session,employee.id,'Меню',[{'text':base['label'],'payload':base['action']}],unique_key='service-menu-is-separate')
    assert tx.signed_buttons(session,menu)[0][0]['text'] == base['label']
