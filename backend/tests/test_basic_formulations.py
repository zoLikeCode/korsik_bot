import json
import uuid

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from korsik import basic_formulations as basic, chat, domain as d
from korsik.auth import Actor
from korsik.models import Entity
from test_domain import session, hr, profile, employee_actor


def request(tag=None):
    return Request(
        {"type": "http", "headers": [(b"if-match", tag.encode())] if tag else []}
    )


def save(db, actor, label="Парковка", query="Где находится парковка?"):
    current = basic.get_basic_formulations(actor, db)
    body = basic.BasicFormulationsInput(
        items=[{"id": "parking", "label": label, "query_text": query}]
    )
    return basic.put_basic_formulations(
        body, request(current.headers["etag"]), actor, db
    )


def test_default_read_is_pure_and_scope_roles_are_enforced(session, hr):
    response = basic.get_basic_formulations(hr, session)
    data = json.loads(response.body)
    assert data["configured"] is False and len(data["items"]) == 4
    assert d.entities(session, "basic_formulations") == []
    for role in ("EMPLOYEE", "MANAGER", "PLATFORM_ADMIN"):
        with pytest.raises(HTTPException) as denied:
            basic.get_basic_formulations(Actor(role, role, role, {}), session)
        assert denied.value.status_code == 403


def test_save_requires_fresh_etag_and_keeps_immutable_revision(session, hr):
    first = save(session, hr)
    data = json.loads(first.body)
    old = session.get(Entity, data["menu_version_id"])
    old_items = json.loads(json.dumps(old.data["items"]))
    changed = basic.BasicFormulationsInput(
        items=[{"id": "parking", "label": "Обед", "query_text": "Где пообедать?"}]
    )
    with pytest.raises(HTTPException) as missing:
        basic.put_basic_formulations(changed, request(), hr, session)
    assert missing.value.status_code == 428
    second = basic.put_basic_formulations(
        changed, request(first.headers["etag"]), hr, session
    )
    assert second.headers["etag"] != first.headers["etag"]
    assert old.data["items"] == old_items
    with pytest.raises(HTTPException) as stale:
        basic.put_basic_formulations(
            changed, request(first.headers["etag"]), hr, session
        )
    assert stale.value.status_code == 412


def test_labels_queries_and_duplicate_ids_are_validated():
    for item in (
        {"label": " ", "query_text": "Вопрос"},
        {"label": "Кнопка", "query_text": " "},
        {"label": "x" * 61, "query_text": "Вопрос"},
    ):
        with pytest.raises(ValidationError):
            basic.BasicFormulationsInput(items=[item])
    with pytest.raises(ValidationError):
        basic.BasicFormulationsInput(
            items=[
                {"id": "same", "label": "A", "query_text": "A"},
                {"id": "same", "label": "B", "query_text": "B"},
            ]
        )
    with pytest.raises(ValidationError):
        basic.BasicFormulationsInput(items=[])


def test_scoped_override_does_not_leak_to_other_sites(session, hr):
    first = profile(session, hr, site_id="GrPP")
    other = profile(session, hr, site_id="GTP")
    save(session, hr, "Общее", "Общий вопрос")
    branch = Actor("branch", "HR", "HR_SPECIALIST", {"site_ids": ["GrPP"]})
    save(session, branch, "Для площадки", "Вопрос своей площадки")
    assert (
        basic.basic_prompts_for(session, first)["items"][0]["label"] == "Для площадки"
    )
    assert basic.basic_prompts_for(session, other)["items"][0]["label"] == "Общее"
    assert (
        json.loads(basic.get_basic_formulations(hr, session).body)["items"][0]["label"]
        == "Общее"
    )


def test_base_button_resolves_current_server_question_and_rejects_stale(session, hr):
    employee = profile(session, hr)
    conv = chat.conversation_for(session, employee)
    actor = employee_actor(employee)
    menu = basic.basic_prompts_for(session, employee)
    old_action = menu["items"][0]["action"]
    save(session, hr)
    with pytest.raises(HTTPException) as stale:
        chat.submit_turn(session, actor, conv, old_action, str(uuid.uuid4()))
    assert stale.value.status_code == 409
    menu = basic.basic_prompts_for(session, employee)
    turn = chat.submit_turn(
        session, actor, conv, menu["items"][0]["action"], str(uuid.uuid4())
    )
    answer = session.get(Entity, turn["answer_id"])
    assert answer.data["question"] == "Где находится парковка?"
    assert turn["dispatch"] == "BOT_ANSWER"


def test_unclaimed_support_alert_does_not_block_next_bot_turn(session, hr):
    employee = profile(session, hr)
    conv = chat.conversation_for(session, employee)
    actor = employee_actor(employee)
    case = chat.handoff(session, actor, conv, "Нужна помощь")
    case = session.get(Entity, case["id"])
    assert conv.data["mode"] == "WAITING_HR" and case.status == "OPEN"
    turn = chat.submit_turn(
        session,
        actor,
        conv,
        {"kind": "TEXT", "text": "Где пообедать?"},
        str(uuid.uuid4()),
    )
    assert turn["dispatch"] == "BOT_ANSWER"
    assert conv.data["mode"] == "BOT" and case.status == "OPEN"
    assert not case.data.get("claimed_at")


def test_actual_human_claim_is_never_automatically_overridden(session, hr):
    employee = profile(session, hr)
    conv = chat.conversation_for(session, employee)
    actor = employee_actor(employee)
    case = chat.handoff(session, actor, conv, "Нужна помощь")
    d.claim_case(case["id"], session, hr)
    turn = chat.submit_turn(
        session, actor, conv, {"kind": "TEXT", "text": "Уточнение"}, str(uuid.uuid4())
    )
    assert turn["dispatch"] == "HR_INBOX" and conv.data["mode"] == "HUMAN"
    conv.data = {**conv.data, "mode": "WAITING_HR"}
    assert chat.continue_unclaimed_bot_search(session, conv) is False


def test_generic_welcome_is_suppressed_only_for_an_immediate_message(session, hr):
    from korsik.simple_scenarios import default_graph
    from korsik.common import save_entity
    from korsik.db import now

    employee = profile(session, hr)
    graph = default_graph()
    version = save_entity(session, "scenario_version", {"graph": graph})
    journey = save_entity(
        session, "journey", {"version_id": version.id, "started_at": now().isoformat()}
    )
    assert chat.has_immediate_simple_greeting(session, employee, [journey])
    graph["steps"][0]["trigger"] = {
        "type": "ANCHOR_TIME",
        "offset_days": 1,
        "local_time": "10:00",
    }
    version.data = {"graph": graph}
    assert not chat.has_immediate_simple_greeting(session, employee, [journey])
    graph["steps"][0]["kind"] = "SURVEY"
    version.data = {"graph": graph}
    assert not chat.has_immediate_simple_greeting(session, employee, [journey])


def test_default_confidentiality_question_gets_a_new_identity_without_editing_published_custom_questions(
    session, hr
):
    employee = profile(session, hr)
    menu = basic.basic_prompts_for(session, employee)
    default = next(item for item in menu["items"] if item["id"] == "confidentiality")
    assert default["query_text"] == "Что относится к коммерческой тайне компании?"
    old_default_revision = str(
        uuid.uuid5(uuid.NAMESPACE_URL, menu["menu_id"] + ":defaults-v1")
    )
    assert menu["menu_version_id"] != old_default_revision
    conv = chat.conversation_for(session, employee)
    with pytest.raises(HTTPException) as stale:
        chat.submit_turn(
            session,
            employee_actor(employee),
            conv,
            {**default["action"], "menu_version_id": old_default_revision},
            "old-default",
        )
    assert (
        stale.value.status_code == 409 and stale.value.detail["code"] == "stale_action"
    )
    response = save(session, hr, "Мой заголовок", "Мой вопрос о паролях")
    configured = json.loads(response.body)
    pinned = session.get(Entity, configured["menu_version_id"])
    assert (
        basic.basic_prompts_for(session, employee)["items"][0]["query_text"]
        == "Мой вопрос о паролях"
    )
    assert pinned.data["items"][0]["query_text"] == "Мой вопрос о паролях"
