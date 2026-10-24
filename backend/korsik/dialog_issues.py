"""Read-only per-question support projection; no model or conversation state writes."""
from collections import defaultdict
from sqlalchemy import select, or_, and_
from .models import Entity, Message
from .db import aware


def problem_index(db, conversations):
    """Return one issue per original employee message, using durable source links.

    Old bot HANDOFF messages have no turn link; only then use the immediately
    preceding employee message. A greeting/system message is never a question.
    Generic handoff/claim state and arbitrary HR replies do not create/resolve it.
    """
    ids = {c.id for c in conversations}
    if not ids:
        return {}
    messages = list(db.scalars(select(Message).where(Message.conversation_id.in_(ids)).order_by(Message.seq)))
    rows = list(db.scalars(select(Entity).where(or_(
        and_(Entity.kind.in_(["answer", "conversation_turn"]),
             or_(Entity.parent_id.in_(ids), Entity.data["conversation_id"].as_string().in_(ids))),
        and_(Entity.kind == "case", Entity.data["conversation_id"].as_string().in_(ids)),
    ))))
    by_id = {m.id: m for m in messages}
    by_seq = {(m.conversation_id, m.seq): m for m in messages}
    turns = {r.id: r for r in rows if r.kind == "conversation_turn" and r.parent_id in ids}
    answers = [r for r in rows if r.kind == "answer" and (r.parent_id in ids or r.data.get("conversation_id") in ids)]
    conv_by_id = {c.id: c for c in conversations}
    cases = {r.id: r for r in rows if r.kind == "case" and r.data.get("conversation_id") in ids
             and r.data.get("employee_id") == conv_by_id[r.data["conversation_id"]].data.get("employee_id")
             and r.data.get("episode_id") in {None, conv_by_id[r.data["conversation_id"]].data.get("episode_id")}}
    previous, latest = {}, {}
    for message in messages:
        previous[message.id] = latest.get(message.conversation_id)
        if message.author_type == "EMPLOYEE":
            latest[message.conversation_id] = message
    answer_ids = {a.id for a in answers}
    issues = defaultdict(dict)

    def add(message, *, answer=None, case=None, inferred=False):
        if not message or message.conversation_id not in ids or message.author_type != "EMPLOYEE":
            return
        issue = issues[message.conversation_id].setdefault(message.id, {
            "status": "OPEN", "answer_ids": [], "case_ids": [], "legacy_inferred": inferred,
        })
        if answer and answer.id not in issue["answer_ids"]:
            issue["answer_ids"].append(answer.id)
        if case and case.id not in issue["case_ids"]:
            issue["case_ids"].append(case.id)

    def previous_employee(message):
        return previous.get(message.id)

    linked_bot_answers = {}
    for message in messages:
        if message.author_type in {"BOT", "SYSTEM"} and (message.data or {}).get("answer_id"):
            linked_bot_answers[message.data["answer_id"]] = message
    for answer in answers:
        mode = answer.data.get("mode")
        if not (mode == "HANDOFF" or answer.status in {"FAILED", "EXPIRED"}
                or answer.status == "ABSTAINED" and mode != "CLARIFY"):
            continue
        conv_id = answer.data.get("conversation_id") or answer.parent_id
        turn = turns.get(answer.data.get("turn_id"))
        original = by_id.get(turn.data.get("message_id")) if turn else None
        if original is None:
            original = by_id.get(answer.data.get("message_id"))
        if original is None:
            original = by_seq.get((conv_id, answer.data.get("context_upto_seq")))
        inferred = False
        if original is None and answer.id in linked_bot_answers:
            original = previous_employee(linked_bot_answers[answer.id]); inferred = True
        if original is not None and original.conversation_id != conv_id:
            continue
        case = cases.get(answer.data.get("support_case_id"))
        add(original, answer=answer, case=case, inferred=inferred)
    # Explicitly marked historical bot failures, even when their answer entity
    # predates durable turns. Never infer a failure from generic conversation.mode.
    for message in messages:
        if message.author_type in {"BOT", "SYSTEM"} and (message.data or {}).get("mode") == "HANDOFF":
            if message.data.get("answer_id") in answer_ids:
                continue
            add(previous_employee(message), inferred=True)
    for case in cases.values():
        target = by_id.get(case.data.get("question_message_id") or case.data.get("message_id"))
        if target and case.data.get("kind") == "HANDOFF":
            add(target, case=case)
        elif (case.data.get("kind") == "HANDOFF" and
              (case.data.get("summary") or case.data.get("reason")) in {
                  "NO_EVIDENCE", "INSUFFICIENT_EVIDENCE", "MODEL_ABSTAINED",
                  "LOCAL_QUEUE_TIMEOUT", "LOCAL_DEADLINE", "RETRIEVAL_UNAVAILABLE",
              } and not any(case.id in issue["case_ids"] for issue in issues.get(case.data["conversation_id"], {}).values())):
            candidates = [m for m in messages if m.conversation_id == case.data["conversation_id"]
                          and m.author_type == "EMPLOYEE" and aware(m.created_at) <= aware(case.created_at)]
            add(max(candidates, key=lambda m: m.seq, default=None), case=case, inferred=True)
    # Explicit historical case resolution is preserved. A first_human_response_at
    # or a random HR note is not evidence that this particular question was answered.
    for conversation_issues in issues.values():
        for issue in conversation_issues.values():
            related = [cases[c] for c in issue["case_ids"] if c in cases]
            if related and all(c.status in {"RESOLVED", "CLOSED"} and
                               c.data.get("resolved_at") and c.data.get("resolved_by") and
                               c.data.get("result_summary") for c in related):
                issue.update(status="RESOLVED", legacy_case_resolution=True)
    for message in messages:
        data = message.data or {}
        target = data.get("resolves_problem_message_id")
        if message.author_type == "HR" and target and data.get("reply_to_message_id") == target:
            original = by_id.get(target)
            issue = issues[message.conversation_id].get(target)
            if original and original.conversation_id == message.conversation_id and issue:
                issue.update(status="RESOLVED", resolution_message_id=message.id,
                             resolved_at=message.created_at.isoformat())
    return dict(issues)


def unresolved_count(issues):
    return sum(issue["status"] == "OPEN" for issue in issues.values())
