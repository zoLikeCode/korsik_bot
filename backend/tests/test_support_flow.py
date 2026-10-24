"""Support escalation is explicit, scoped, replay-safe and separate from RAG.

All identities, source documents and message receipts are isolated SQLite
fixtures. No MAX/model HTTP calls or production state are involved.
"""

import json
import uuid

import pytest
from sqlalchemy import select

from korsik import chat, domain as d, retrieval, transport_api as tx, worker
from korsik.auth import Actor
from korsik.common import queue_message, save_entity
from korsik.db import new_id
from korsik.models import Entity, Job, Message, Outbox, User
from test_domain import session as session, hr as hr, profile, employee_actor, req
from test_max import bind, secret_settings as secret_settings


@pytest.fixture
def support(session, hr, secret_settings):
    employee = profile(session, hr)
    _, conversation = bind(session, hr, employee)
    actor = employee_actor(employee)
    return employee, conversation, actor


def ask(db, conversation, actor, text="Где находится парковка?"):
    turn = chat.submit_turn(
        db, actor, conversation, {"kind": "TEXT", "text": text}, str(uuid.uuid4())
    )
    assert turn["dispatch"] == "BOT_ANSWER"
    return db.get(Entity, turn["answer_id"])


def abstain(db, answer):
    return worker.finish_answer(
        db,
        answer,
        {
            "mode": "HANDOFF",
            "reason": "INSUFFICIENT_TOPIC_EVIDENCE",
            "text": "Исходный технический отказ не должен отправляться сотруднику",
            "citations": [],
            "evidence": [],
        },
    )


def answer_outbox(db, answer):
    return db.scalar(select(Outbox).where(Outbox.unique_key == f"answer:{answer.id}"))


def test_handoff_answer_creates_one_neutral_notice_case_and_scoped_notification(
    session, hr, support
):
    employee, conversation, actor = support
    answer = ask(session, conversation, actor)
    assert abstain(session, answer)
    case = session.get(Entity, answer.data["support_case_id"])
    notice = answer_outbox(session, answer)
    assert case.status == "OPEN" and case.data["kind"] == "HANDOFF"
    assert case.data["employee_id"] == employee.id
    assert case.data["conversation_id"] == conversation.id
    assert case.data["episode_id"] == employee.data["episode_id"]
    assert conversation.data["mode"] == "BOT"
    assert answer.status == "ABSTAINED"
    assert notice.text == "Вопрос принят. Уточняем информацию; ответ появится здесь."
    assert notice.data["support_case_id"] == case.id
    assert notice.callback_buttons == []
    assert "HR" not in notice.text and "нейрос" not in notice.text.casefold()
    notifications = list(
        session.scalars(
            select(Outbox).where(Outbox.unique_key.like(f"handoff-notify:{case.id}:%"))
        )
    )
    assert {out.recipient_id for out in notifications} == {hr.id}
    assert all(out.recipient_kind == "USER" for out in notifications)
    assert not abstain(session, answer)
    assert len(d.entities(session, "case", employee.id)) == 1
    assert (
        len(
            list(
                session.scalars(
                    select(Outbox).where(Outbox.unique_key == f"answer:{answer.id}")
                )
            )
        )
        == 1
    )
    bot_messages = list(
        session.scalars(
            select(Message).where(Message.conversation_id == conversation.id)
        )
    )
    assert sum(m.data.get("answer_id") == answer.id for m in bot_messages) == 1


def test_pending_support_does_not_block_next_normal_bot_question(session, hr, support):
    _, conversation, actor = support
    first = ask(session, conversation, actor)
    abstain(session, first)
    second = ask(session, conversation, actor, "Где найти опубликованные правила?")
    assert second.id != first.id and second.status == "QUEUED"
    assert conversation.data["mode"] == "BOT"
    assert len(d.entities(session, "conversation_turn", conversation.id)) == 2
    assert session.scalar(
        select(Job).where(
            Job.kind == "ANSWER", Job.payload["answer_id"].as_string() == second.id
        )
    )


def test_claim_switches_to_human_without_arrival_announcement(session, hr, support):
    employee, conversation, actor = support
    answer = ask(session, conversation, actor)
    abstain(session, answer)
    case = session.get(Entity, answer.data["support_case_id"])
    before_outboxes = len(list(session.scalars(select(Outbox))))
    before_messages = len(list(session.scalars(select(Message))))
    d.claim_case(case.id, session, hr)
    assert case.status == "IN_PROGRESS" and case.data["assigned_user_id"] == hr.id
    assert conversation.data["mode"] == "HUMAN"
    assert len(list(session.scalars(select(Outbox)))) == before_outboxes
    assert len(list(session.scalars(select(Message)))) == before_messages
    # From this explicit claim onward the existing input lane goes to the human
    # inbox, not a model answer falsely presented as an operator response.
    turn = chat.submit_turn(
        session,
        actor,
        conversation,
        {"kind": "TEXT", "text": "Добавляю детали обращения"},
        str(uuid.uuid4()),
    )
    assert turn["dispatch"] == "HR_INBOX" and "answer_id" not in turn


@pytest.mark.parametrize("revocation", ["ROLE", "SCOPE"])
def test_human_reply_keeps_neutral_copy_and_presend_rechecks_current_author_access(
    session, hr, support, revocation
):
    _, conversation, actor = support
    answer = ask(session, conversation, actor)
    abstain(session, answer)
    case = session.get(Entity, answer.data["support_case_id"])
    d.claim_case(case.id, session, hr)
    body = chat.HumanReply(
        text="Уточнили информацию: встреча назначена на завтра.",
        client_message_id=uuid.uuid4(),
    )
    result = chat.human_reply(conversation.id, body, hr, session)
    message = session.get(Message, result["message_id"])
    out = session.scalar(
        select(Outbox).where(Outbox.unique_key == f"hr-reply:{conversation.id}:{body.client_message_id}")
    )
    assert message.author_type == "HR" and message.data["actor_id"] == hr.id
    assert out.text == body.text
    assert out.data["human_response"] is True and out.data["author_id"] == hr.id
    assert "answer_id" not in out.data
    assert tx.valid_outbox(session, out)[1] is None
    user = session.get(User, hr.id)
    if revocation == "ROLE":
        user.role = "MANAGER"
    else:
        user.scope = {"site_ids": ["GTP"]}
    assert tx.valid_outbox(session, out)[1] == "HR_ACCESS_REVOKED"


def test_closed_case_suppresses_late_pending_notice(session, hr, support):
    _, conversation, actor = support
    answer = ask(session, conversation, actor)
    abstain(session, answer)
    case = session.get(Entity, answer.data["support_case_id"])
    out = answer_outbox(session, answer)
    assert tx.valid_outbox(session, out)[1] is None
    d.claim_case(case.id, session, hr)
    d.resolve_case(
        case.id,
        {"result_summary": "Вопрос разъяснён сотруднику"},
        req(case),
        session,
        hr,
    )
    assert case.status == "RESOLVED"
    assert tx.valid_outbox(session, out)[1] == "SUPPORT_NOTICE_STALE"


@pytest.mark.parametrize(
    "invalid_assignment", ["UNKNOWN", "DISABLED", "FOREIGN_SCOPE", "MANAGER"]
)
def test_invalid_assigned_hr_falls_back_to_enabled_scoped_hr_and_can_be_claimed(
    session, hr, support, invalid_assignment
):
    employee, conversation, actor = support
    recipients = [
        User(
            id="regional-hr",
            email="regional@test",
            password_hash="no-login",
            name="Regional",
            role="HR_SPECIALIST",
            scope={"site_ids": ["GrPP"]},
            enabled=True,
        ),
        User(
            id="foreign-hr",
            email="foreign@test",
            password_hash="no-login",
            name="Foreign",
            role="HR_SPECIALIST",
            scope={"site_ids": ["GTP"]},
            enabled=True,
        ),
        User(
            id="disabled-hr",
            email="disabled@test",
            password_hash="no-login",
            name="Disabled",
            role="HR_SPECIALIST",
            scope={"all": True},
            enabled=False,
        ),
        User(
            id="manager",
            email="manager@test",
            password_hash="no-login",
            name="Manager",
            role="MANAGER",
            scope={"all": True},
            enabled=True,
        ),
    ]
    session.add_all(recipients)
    session.flush()
    bad = {
        "UNKNOWN": "missing-user",
        "DISABLED": "disabled-hr",
        "FOREIGN_SCOPE": "foreign-hr",
        "MANAGER": "manager",
    }[invalid_assignment]
    employee.data = {**employee.data, "assigned_hr_id": bad}
    answer = ask(session, conversation, actor)
    abstain(session, answer)
    case = session.get(Entity, answer.data["support_case_id"])
    out = list(
        session.scalars(
            select(Outbox).where(Outbox.unique_key.like(f"handoff-notify:{case.id}:%"))
        )
    )
    assert {r.recipient_id for r in out} == {hr.id, "regional-hr"}
    regional = Actor("regional-hr", "Regional", "HR_SPECIALIST", {"site_ids": ["GrPP"]})
    d.claim_case(case.id, session, regional)
    assert case.data["assigned_user_id"] == regional.id
    assert conversation.data["mode"] == "HUMAN"


def test_signed_employee_buttons_filter_legacy_hr_actions_without_breaking_other_buttons(
    session, hr, support, secret_settings
):
    employee, _, _ = support
    buttons = [
        {"text": "Уточнить", "payload": {"action": "CONTACT_HR"}},
        {"text": "Обсудить", "payload": {"action": "HANDOFF"}},
        {"text": "Контакт", "payload": {"action": "CONTACT_RECRUITER"}},
        {"text": "Прежний формат", "payload": json.dumps({"action": "HANDOFF"})},
        {"text": "Старый kind", "payload": {"kind": "HANDOFF"}},
        {"text": "Связаться с HR", "payload": {"action": "OTHER"}},
        {"text": "Тема", "payload": {"action": "RAG_QUERY", "prompt_key": "food"}},
        {"text": "Оригинал", "payload": {"action": "REQUEST_SOURCE"}},
    ]
    out = queue_message(
        session,
        "EMPLOYEE",
        employee.id,
        "Кнопки",
        "support-filter-fixture",
        buttons=buttons,
    )
    signed = [button for row in tx.signed_buttons(session, out) for button in row]
    assert {button["text"] for button in signed} == {"Тема", "Оригинал"}
    assert len(d.entities(session, "callback_action", out.id)) == 2


def test_source_citation_answer_keeps_its_normal_text_citation_and_source_button(
    session, hr, support, monkeypatch
):
    employee, conversation, actor = support
    doc = save_entity(
        session,
        "document",
        {
            "title": "Расположение офиса",
            "scope": {
                "site_ids": ["GrPP"],
                "employment_stages": ["PREBOARDING", "ACTIVE"],
            },
        },
        status="ACTIVE",
    )
    revision = save_entity(
        session,
        "document_revision",
        {"effective_from": "2020-01-01T00:00:00+00:00"},
        parent_id=doc.id,
        status="PUBLISHED",
    )
    doc.data = {**doc.data, "active_revision_id": revision.id}
    text = "Офис находится у проходной. Вход расположен рядом с главным зданием."
    save_entity(
        session,
        "knowledge_chunk",
        {
            "document_id": doc.id,
            "text": text,
            "parent_text": text,
            "block_id": new_id(),
            "page_no": 1,
            "kind": "TEXT",
            "extraction_revision_id": new_id(),
        },
        parent_id=revision.id,
        status="PUBLISHED",
    )
    monkeypatch.setattr(retrieval, "MODE", "CORE")
    monkeypatch.setattr(
        retrieval,
        "_request",
        lambda *a, **k: pytest.fail(
            "CORE support regression must never call model HTTP"
        ),
    )
    answer = ask(session, conversation, actor, "Где находится офис?")
    result = retrieval.answer_question(session, conversation, answer.data["question"])
    assert result["mode"] == "EXTRACT" and text in result["text"]
    assert worker.finish_answer(session, answer, result)
    out = answer_outbox(session, answer)
    assert out.text == result["text"]
    assert out.data["answer_id"] == answer.id and "support_case_id" not in out.data
    assert len(answer.data["citations"]) == 1
    assert answer.data["citations"][0]["revision_id"] == revision.id
    assert any(
        b["payload"].get("action") == "REQUEST_SOURCE" for b in out.callback_buttons
    )
    assert not d.entities(session, "case", employee.id)
    assert tx.valid_outbox(session, out)[1] is None


def test_pending_case_recovers_when_previously_valid_assigned_hr_loses_access(
    session, hr, support
):
    employee, conversation, actor = support
    assigned = User(
        id="assigned-before-revoke",
        email="assigned-before-revoke@test",
        password_hash="no-login",
        name="Assigned HR",
        role="HR_SPECIALIST",
        scope={"site_ids": ["GrPP"]},
        enabled=True,
    )
    session.add(assigned)
    session.flush()
    employee.data = {**employee.data, "assigned_hr_id": assigned.id}
    first = ask(session, conversation, actor)
    abstain(session, first)
    case = session.get(Entity, first.data["support_case_id"])
    assert case.data["assigned_user_id"] == assigned.id
    assigned.enabled = False
    second = ask(session, conversation, actor, "Где находится гардероб?")
    abstain(session, second)
    assert second.data["support_case_id"] == case.id
    notifications = list(
        session.scalars(
            select(Outbox).where(Outbox.unique_key.like(f"handoff-notify:{case.id}:%"))
        )
    )
    assert hr.id in {out.recipient_id for out in notifications}
    d.claim_case(case.id, session, hr)
    assert (
        case.data["assigned_user_id"] == hr.id and conversation.data["mode"] == "HUMAN"
    )
