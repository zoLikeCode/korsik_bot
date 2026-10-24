"""Question-level support counts, contextual replies and atomic takeover guards."""
import uuid
from copy import deepcopy

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from starlette.responses import Response

from korsik import chat, domain as d, retrieval
from korsik.auth import Actor
from korsik.common import add_message, save_entity
from korsik.dialog_issues import problem_index, unresolved_count
from korsik.models import Entity, Message, Outbox, User
from test_domain import session as session, hr as hr, profile, employee_actor
from test_max import secret_settings as secret_settings
from test_support_flow import support as support, ask, abstain


def question_message(db, answer):
    return db.get(Message, db.get(Entity, answer.data["turn_id"]).data["message_id"])


def respond(db, hr, conv, text="Ответ на вопрос", target=None, client_id=None):
    return chat.human_reply(conv.id, chat.HumanReply(text=text,
        client_message_id=client_id or uuid.uuid4(), reply_to_message_id=target.id if target else None), hr, db)


def fails(code, function, *args, **kwargs):
    with pytest.raises(HTTPException) as caught:
        function(*args, **kwargs)
    assert caught.value.status_code == code


def test_actual_unanswered_questions_are_tagged_on_original_messages_and_summary_counts_conversations(session, hr, support):
    _, conv, actor = support
    first = ask(session, conv, actor, "Вопрос первый"); abstain(session, first)
    second = ask(session, conv, actor, "Вопрос второй"); abstain(session, second)
    expected = {question_message(session, first).id, question_message(session, second).id}
    rows = chat.messages(conv.id, actor=hr, db=session)["items"]
    assert {m["id"] for m in rows if m.get("support_issue", {}).get("status") == "OPEN"} == expected
    assert all(m["author_type"] == "EMPLOYEE" for m in rows if "support_issue" in m)
    assert chat.problem_summary(hr, session) == {"problem_conversations_count": 1, "unresolved_question_count": 2}
    assert chat.conversations(hr, session)["items"][0]["unresolved_question_count"] == 2
    assert chat.conversation_detail(conv.id, Response(), hr, session)["unresolved_question_count"] == 2
    assert all("support_issue" not in m for m in chat.messages(conv.id, actor=actor, db=session)["items"])


def test_only_explicit_reply_to_each_problem_decrements_that_question(session, hr, support):
    _, conv, actor = support
    first = ask(session, conv, actor, "Первый"); abstain(session, first)
    second = ask(session, conv, actor, "Второй"); abstain(session, second)
    respond(session, hr, conv, "Общее сообщение")
    assert chat.problem_summary(hr, session)["unresolved_question_count"] == 2
    bot = next(m for m in session.scalars(select(Message)) if m.data.get("answer_id") == first.id)
    respond(session, hr, conv, target=bot)
    assert chat.problem_summary(hr, session)["unresolved_question_count"] == 2
    result = respond(session, hr, conv, target=question_message(session, first))
    assert result["unresolved_question_count"] == 1
    issues = problem_index(session, [conv])[conv.id]
    assert issues[question_message(session, first).id]["resolution_message_id"] == result["message_id"]
    assert issues[question_message(session, second).id]["status"] == "OPEN"
    result = respond(session, hr, conv, target=question_message(session, second))
    assert result["unresolved_question_count"] == 0
    assert chat.problem_summary(hr, session)["problem_conversations_count"] == 0


def test_direct_send_claims_and_cancels_pending_generation_without_arrival_announcement(session, hr, support):
    _, conv, actor = support
    pending = ask(session, conv, actor)
    before = list(session.scalars(select(Message).where(Message.conversation_id == conv.id)))
    result = respond(session, hr, conv, "Уточнили вопрос")
    case = session.get(Entity, conv.data["active_handoff_case_id"])
    assert conv.data["mode"] == "HUMAN" and case.status == "IN_PROGRESS" and case.owner_id == hr.id
    assert pending.status == "CANCELLED" and pending.data["reason_code"] == "HUMAN_TAKEOVER"
    after = list(session.scalars(select(Message).where(Message.conversation_id == conv.id)))
    assert len(after) == len(before) + 1
    out = session.scalar(select(Outbox).where(Outbox.data["message_id"].as_string() == result["message_id"]))
    assert out.text == "Уточнили вопрос" and "поддерж" not in out.text and "HR" not in out.text
    assert chat.problem_summary(hr, session)["unresolved_question_count"] == 0


def test_reply_replay_is_atomic_idempotent_and_conflicting_payload_is_rejected(session, hr, support):
    _, conv, actor = support
    answer = ask(session, conv, actor); abstain(session, answer)
    target = question_message(session, answer)
    client = uuid.uuid4()
    first = respond(session, hr, conv, target=target, client_id=client)
    second = respond(session, hr, conv, target=target, client_id=client)
    assert first == second
    assert len(list(session.scalars(select(Message).where(Message.author_type == "HR")))) == 1
    assert len(list(session.scalars(select(Outbox).where(Outbox.unique_key == f"hr-reply:{conv.id}:{client}")))) == 1
    fails(409, respond, session, hr, conv, "Changed", target, client)


def test_direct_send_respects_existing_claim_and_scoped_roles(session, hr, support):
    _, conv, _ = support
    respond(session, hr, conv)
    other = Actor("other-hr", "Other", "HR_ADMIN", {"all": True})
    before = len(list(session.scalars(select(Message))))
    fails(409, respond, session, other, conv)
    assert len(list(session.scalars(select(Message)))) == before
    fails(404, respond, session, Actor("foreign", "Foreign", "HR_ADMIN", {"site_ids": ["foreign"]}), conv)
    head = Actor("head", "Head", "HEAD_TALENT", {"site_ids": ["GrPP"]})
    fails(409, respond, session, head, conv)  # HEAD has HR rights, but cannot steal another claim.
    fails(404, respond, session, Actor("head", "Head", "HEAD_TALENT", {"site_ids": ["foreign"]}), conv)
    owner_as_head = Actor(hr.id, hr.name, "HEAD_TALENT", {"site_ids": ["GrPP"]})
    assert respond(session, owner_as_head, conv)["message_id"]
    for role in ["MANAGER", "EMPLOYEE", "PLATFORM_ADMIN"]:
        fails(403, respond, session, Actor("actor", "Actor", role, {"all": True}), conv)
    fails(403, chat.problem_summary, Actor("manager", "Manager", "MANAGER", {}), session)


def test_cross_conversation_quote_rejected_before_any_claim_or_message(session, hr, support):
    _, conv, _ = support
    other_employee = profile(session, hr)
    other_conv = chat.conversation_for(session, other_employee)
    secret = add_message(session, other_conv, "EMPLOYEE", "Foreign private message")
    session.flush()
    before = len(list(session.scalars(select(Message))))
    fails(404, respond, session, hr, conv, target=secret)
    assert conv.data["mode"] == "BOT" and not conv.data.get("active_handoff_case_id")
    assert len(list(session.scalars(select(Message)))) == before


def test_employee_and_hr_quote_context_persists_to_history_and_bounded_max_copy(session, hr, support):
    _, conv, actor = support
    original = add_message(session, conv, "BOT", "Исходное сообщение")
    session.flush()
    turn = chat.submit_turn(session, actor, conv, {"kind": "TEXT", "text": "Уточнение"},
                            str(uuid.uuid4()), original.id)
    employee_message = session.get(Message, turn["message_id"])
    assert employee_message.data["reply_to_message_id"] == original.id
    result = respond(session, hr, conv, target=employee_message)
    row = next(m for m in chat.messages(conv.id, actor=hr, db=session)["items"] if m["id"] == result["message_id"])
    assert row["reply_to_message_id"] == employee_message.id and row["reply_to"]["text"] == "Уточнение"
    out = session.scalar(select(Outbox).where(Outbox.data["message_id"].as_string() == result["message_id"]))
    assert out.text == "В ответ на: «Уточнение»\n\nОтвет на вопрос" and len(out.text) <= 3900


def test_quote_of_revoked_source_is_redacted_in_history_and_outbound(session, hr, support, monkeypatch):
    _, conv, _ = support
    original = add_message(session, conv, "BOT", "Private outdated fact", {"citations": [{"revision_id": "removed"}]})
    session.flush()
    monkeypatch.setattr(retrieval, "answer_still_authorized", lambda *_: False)
    result = respond(session, hr, conv, target=original)
    row = next(m for m in chat.messages(conv.id, actor=hr, db=session)["items"] if m["id"] == result["message_id"])
    assert row["reply_to"]["text"] == "Источники ответа больше недоступны."
    out = session.scalar(select(Outbox).where(Outbox.data["message_id"].as_string() == result["message_id"]))
    assert "Private outdated fact" not in out.text


def test_legacy_handoff_fallback_tags_employee_never_greeting_and_reads_do_not_mutate(session, hr):
    employee = profile(session, hr)
    conv = chat.conversation_for(session, employee)
    greeting = add_message(session, conv, "BOT", "Привет")
    original = add_message(session, conv, "EMPLOYEE", "Где кабинет?")
    add_message(session, conv, "BOT", "Уточняем", {"mode": "HANDOFF"})
    session.commit()
    before = deepcopy(conv.data)
    issues = problem_index(session, [conv])[conv.id]
    assert set(issues) == {original.id} and greeting.id not in issues and issues[original.id]["legacy_inferred"]
    chat.problem_summary(hr, session)
    assert conv.data == before and not session.new and not session.dirty


def test_failed_answer_context_sequence_links_exact_question_and_clarification_is_not_failure(session, hr):
    employee = profile(session, hr)
    conv = chat.conversation_for(session, employee)
    first = add_message(session, conv, "EMPLOYEE", "First")
    second = add_message(session, conv, "EMPLOYEE", "Second")
    session.flush()
    save_entity(session, "answer", {"conversation_id": conv.id, "context_upto_seq": first.seq}, parent_id=conv.id, status="FAILED")
    save_entity(session, "answer", {"conversation_id": conv.id, "context_upto_seq": second.seq, "mode": "CLARIFY"}, parent_id=conv.id, status="ABSTAINED")
    assert set(problem_index(session, [conv])[conv.id]) == {first.id}


def test_old_explicit_case_resolution_is_preserved_but_arbitrary_hr_note_does_not_resolve(session, hr, support):
    _, conv, actor = support
    answer = ask(session, conv, actor); abstain(session, answer)
    case = session.get(Entity, answer.data["support_case_id"])
    add_message(session, conv, "HR", "Заметка без ответа")
    session.flush()
    assert unresolved_count(problem_index(session, [conv])[conv.id]) == 1
    case.status = "RESOLVED"
    d.update(case, resolved_at=d.now().isoformat(), resolved_by=hr.id, result_summary="Ответ предоставлен")
    assert unresolved_count(problem_index(session, [conv])[conv.id]) == 0


def test_quote_length_is_checked_before_takeover_and_off_or_old_episode_cannot_send(session, hr, support):
    employee, conv, _ = support
    target = add_message(session, conv, "EMPLOYEE", "x" * 1000); session.flush()
    fails(422, respond, session, hr, conv, "x" * 3900, target)
    assert conv.data["mode"] == "BOT"
    d.update(employee, enabled=False)
    fails(409, respond, session, hr, conv)
    d.update(employee, enabled=True, episode_id="new-episode")
    fails(409, respond, session, hr, conv)


def test_quoted_source_is_rechecked_at_last_transport_boundary(session, hr, support, monkeypatch):
    from korsik import transport_api as tx
    _, conv, _ = support
    target = add_message(session, conv, "BOT", "Source fact", {"citations": [{"revision_id": "one"}]})
    session.flush()
    monkeypatch.setattr(retrieval, "answer_still_authorized", lambda *_: True)
    result = respond(session, hr, conv, target=target)
    out = session.scalar(select(Outbox).where(Outbox.data["message_id"].as_string() == result["message_id"]))
    assert tx.valid_outbox(session, out)[1] is None
    monkeypatch.setattr(retrieval, "answer_still_authorized", lambda *_: False)
    assert tx.valid_outbox(session, out) == (None, "SOURCE_REVOKED")


def test_multiple_failed_attempts_for_one_employee_message_count_once(session, hr, support):
    _, conv, actor = support
    answer = ask(session, conv, actor); abstain(session, answer)
    original = question_message(session, answer)
    duplicate = save_entity(session, "answer", {"conversation_id": conv.id,
        "context_upto_seq": original.seq, "mode": "HANDOFF"}, parent_id=conv.id, status="ABSTAINED")
    issue = problem_index(session, [conv])[conv.id][original.id]
    assert set(issue["answer_ids"]) == {answer.id, duplicate.id}
    assert chat.problem_summary(hr, session)["unresolved_question_count"] == 1
    respond(session, hr, conv, target=original)
    assert chat.problem_summary(hr, session)["unresolved_question_count"] == 0


def test_summary_filters_foreign_scope_deleted_and_previous_episode_dialogs(session, hr, support):
    employee, conv, actor = support
    answer = ask(session, conv, actor); abstain(session, answer)
    other = profile(session, hr, site_id="GTP")
    foreign = chat.conversation_for(session, other)
    question = add_message(session, foreign, "EMPLOYEE", "Foreign question")
    add_message(session, foreign, "BOT", "Working", {"mode": "HANDOFF"})
    session.flush()
    assert chat.problem_summary(hr, session)["problem_conversations_count"] == 2
    scoped = Actor("scoped", "Scoped HR", "HR_SPECIALIST", {"site_ids": ["GrPP"]})
    assert chat.problem_summary(scoped, session) == {"problem_conversations_count": 1, "unresolved_question_count": 1}
    assert question.id not in problem_index(session, [conv]).get(conv.id, {})
    other.status = "DELETED"; d.update(other, enabled=False)
    assert chat.problem_summary(hr, session)["problem_conversations_count"] == 1
    d.update(employee, episode_id="next-episode")
    assert chat.problem_summary(hr, session)["problem_conversations_count"] == 0


def test_unclaimed_routing_hint_can_be_claimed_but_explicit_case_owner_is_preserved(session, hr, support):
    _, conv, actor = support
    answer = ask(session, conv, actor); abstain(session, answer)
    case = session.get(Entity, answer.data["support_case_id"])
    d.update(case, assigned_user_id="routing-hint-only")
    case.owner_id = "explicit-other-owner"
    fails(409, respond, session, hr, conv)
    case.owner_id = None
    result = respond(session, hr, conv)
    assert result["message_id"] and case.owner_id == hr.id
