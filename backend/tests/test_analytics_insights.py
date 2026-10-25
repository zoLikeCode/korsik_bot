import json
from datetime import datetime, timezone
import pytest
from fastapi import HTTPException, FastAPI
from fastapi.testclient import TestClient
from korsik import analytics_insights as ai
from korsik.auth import Actor, get_actor
from korsik.common import save_entity
from korsik.db import get_db
from korsik.models import Message, Entity
from test_domain import session as session, hr as hr

FILTER = {
    "period_from": "2026-10-25",
    "period_to": "2026-10-25",
    "timezone": "Europe/Moscow",
}
WHEN = "2026-10-25T10:00:00+03:00"


def person(db, i, **extra):
    return save_entity(
        db,
        "employee",
        {
            "name": "PRIVATE_EMP_NAME",
            "enabled": True,
            "employment_stage": "ACTIVE",
            "episode_id": f"ep-{i}",
            "site_id": "A",
            "legal_entity_id": "legal",
            **extra,
        },
        id=f"person-{i}",
    )


def run(db, e, value=4, **extra):
    return save_entity(
        db,
        "survey_run",
        {
            "employee_id": e.id,
            "episode_id": e.data["episode_id"],
            "first_question_accepted_at": WHEN,
            "survey_key": "support-v1",
            "definition": {
                "questions": [
                    {
                        "question_key": "q",
                        "type": "NUMERIC",
                        "minimum": 1,
                        "maximum": 5,
                        "prompt": {"text_vy": "Поддержка наставника"},
                    }
                ]
            },
            "answers": {"q": {"value": value, "outcome": "ANSWERED"}},
            **extra,
        },
        status="COMPLETED",
    )


def test_empty_state_is_real_no_score_no_synthetic_surveys(session, hr):
    data = ai.build_insights(session, hr, FILTER)
    assert data["summary"]["employees"] == 0
    assert data["summary"]["completion_rate"] is None
    assert data["surveys"] == data["survey_results"] == []
    assert all(p["questions"] == 0 for p in data["trend"])
    assert not session.dirty


def test_role_scope_and_episode_fences(session, hr):
    a = person(session, 1, manager_id="manager")
    b = person(session, 2, site_id="B")
    person(session, 3, enabled=False)
    person(session, 4, employment_stage="ENDED")
    run(session, a)
    run(session, b, 1)
    run(session, a, 1, episode_id="old-episode")
    scoped = Actor("regional", "HR", "HR_SPECIALIST", {"site_ids": ["A"]})
    manager = Actor("manager", "Manager", "MANAGER", {})
    for actor in (scoped, manager):
        data = ai.build_insights(session, actor, FILTER)
        assert data["summary"]["employees"] == 1
        assert data["surveys"][0]["delivered"] == 1
        assert "PRIVATE_EMP_NAME" not in json.dumps(data)
    with pytest.raises(HTTPException) as exc:
        ai.build_insights(
            session, Actor(a.id, "Employee", "EMPLOYEE", {}, a.id), FILTER
        )
    assert exc.value.status_code == 403


def test_distinct_respondents_not_runs_and_no_raw_text(session, hr):
    e = person(session, 1)
    for i in range(6):
        run(session, e)
    data = ai.build_insights(session, hr, FILTER)
    assert data["survey_results"][0]["suppressed"]
    assert data["survey_results"][0]["average"] is None
    for i in range(2, 6):
        run(session, person(session, i))
    private = run(session, e, 5)
    private.data = {
        **private.data,
        "answers": {
            **private.data["answers"],
            "text": {"outcome": "ANSWERED", "value": "PRIVATE_HR_RESPONSE"},
        },
    }
    private.created_at = datetime(2030, 1, 1, tzinfo=timezone.utc)
    data = ai.build_insights(session, hr, FILTER)
    r = data["survey_results"][0]
    assert r["respondents"] == 5 and r["average"] == 4.2 and not r["suppressed"]
    assert "PRIVATE_HR_RESPONSE" not in json.dumps(data)


def test_custom_survey_small_complementary_cell_suppresses_counts(session, hr):
    campaign = save_entity(session, "survey_campaign", {"generation": 1})
    for i in range(6):
        r = run(
            session, person(session, i), campaign_id=campaign.id, campaign_generation=1
        )
        if i == 0:
            r.status = "DECLINED"
    data = ai.build_insights(session, hr, FILTER)
    row = data["surveys"][0]
    assert row["suppressed"] and row["delivered"] is None and row["declined"] is None
    assert data["summary"]["completion_rate"] is None
    assert data["summary"]["surveys_completed"] is None


def test_answer_options_hide_small_cells_and_validate_values(session, hr):
    q = {
        "question_key": "q",
        "type": "SINGLE_CHOICE",
        "prompt": "Где нужна помощь?",
        "options": [
            {"value": "a", "label": {"text_vy": "Пропуск"}},
            {"value": "b", "label": {"text_vy": "Рабочее место"}},
        ],
    }
    for i in range(6):
        run(
            session,
            person(session, i),
            "a" if i else "b",
            definition={"questions": [q]},
        )
    data = ai.build_insights(session, hr, FILTER)
    r = data["survey_results"][0]
    assert not r["suppressed"] and r["respondents"] == 6 and r["distribution"] is None


def test_moscow_date_boundaries_and_answer_outcomes(session, hr):
    e = person(session, 1)
    for stamp, mode in [
        ("2026-10-24T20:59:59+00:00", "HANDOFF"),
        ("2026-10-24T21:00:00+00:00", "EXTRACT"),
        ("2026-10-25T20:59:59+00:00", "HANDOFF"),
        ("2026-10-25T21:00:00+00:00", "EXTRACT"),
    ]:
        a = save_entity(
            session,
            "answer",
            {"employee_id": e.id, "episode_id": e.data["episode_id"], "mode": mode},
            status="COMPLETED",
        )
        a.created_at = datetime.fromisoformat(stamp)
    data = ai.build_insights(session, hr, FILTER)
    assert data["summary"]["questions"] == 2
    assert data["summary"]["automatic"] == data["summary"]["handoff"] == 1
    assert sum(p["questions"] for p in data["trend"]) == 2
    with pytest.raises(HTTPException):
        ai.build_insights(session, hr, {"timezone": "not/a-zone"})
    with pytest.raises(HTTPException):
        ai.build_insights(
            session, hr, {"period_from": "2026-10-26", "period_to": "2026-10-25"}
        )


def test_department_descendants_and_foreign_legal_entity_are_fenced(session, hr):
    parent = save_entity(
        session, "org_unit", {"title": "Направление", "legal_entity_id": "legal"}
    )
    child = save_entity(
        session,
        "org_unit",
        {"title": "Команда", "legal_entity_id": "legal"},
        parent_id=parent.id,
    )
    foreign = save_entity(
        session,
        "org_unit",
        {"title": "FOREIGN_PRIVATE_UNIT", "legal_entity_id": "other"},
    )
    person(session, 1, org_unit_id=child.id)
    person(session, 2, org_unit_id=foreign.id)
    data = ai.build_insights(session, hr, {**FILTER, "org_unit_id": parent.id})
    assert data["summary"]["employees"] == 1
    assert "FOREIGN_PRIVATE_UNIT" not in json.dumps(data)
    assert (
        ai.build_insights(session, hr, {**FILTER, "org_unit_id": foreign.id})[
            "summary"
        ]["employees"]
        == 0
    )


def test_full_history_trend_and_blocker_confirmation(session, hr):
    e = person(session, 1)
    for stamp in ("2026-06-01T10:00:00+00:00", "2026-10-25T10:00:00+00:00"):
        a = save_entity(
            session,
            "answer",
            {
                "employee_id": e.id,
                "episode_id": e.data["episode_id"],
                "mode": "EXTRACT",
            },
            status="COMPLETED",
        )
        a.created_at = datetime.fromisoformat(stamp)
    for status in ("OPEN", "RESOLVED", "CONFIRMED"):
        save_entity(
            session,
            "case",
            {
                "employee_id": e.id,
                "episode_id": e.data["episode_id"],
                "kind": "BLOCKER",
            },
            status=status,
        )
    data = ai.build_insights(session, hr, {"period_to": "2026-10-25"})
    assert data["summary"]["questions"] == 2
    assert sum(p["questions"] for p in data["trend"]) == 2
    assert len(data["trend"]) <= 30
    # Resolved by HR still needs employee confirmation; confirmed is complete.
    assert data["summary"]["open_blockers"] == 2
    assert data["summary"]["open_cases"] == 1


def test_native_chat_answers_inherit_episode_only_from_the_matching_conversation(
    session, hr
):
    e = person(session, 1)
    other = person(session, 2)
    for episode, owner, explicit in [
        (e.data["episode_id"], e.id, None),
        ("old", e.id, None),
        (e.data["episode_id"], other.id, None),
        (e.data["episode_id"], e.id, "old"),
    ]:
        conv = save_entity(
            session, "conversation", {"employee_id": owner, "episode_id": episode}
        )
        answer = save_entity(
            session,
            "answer",
            {
                "employee_id": e.id,
                "conversation_id": conv.id,
                "mode": "EXTRACT",
                **({"episode_id": explicit} if explicit else {}),
            },
            parent_id=conv.id,
            status="COMPLETED",
        )
        answer.created_at = datetime.fromisoformat(WHEN)
    data = ai.build_insights(session, hr, FILTER)
    assert data["summary"]["automatic"] == data["summary"]["questions"] == 1
    assert sum(p["automatic"] for p in data["trend"]) == 1


def test_problem_topics_require_review_current_episode_and_five_people(session, hr):
    head = Actor(hr.id, "Head", "HEAD_TALENT", hr.scope)
    for i in range(5):
        e = person(session, i)
        for episode, topic, status in [
            (e.data["episode_id"], "Рабочее место", "REVIEWED"),
            ("old", "PRIVATE_OLD_TOPIC", "REVIEWED"),
            (e.data["episode_id"], "PRIVATE_UNREVIEWED_TOPIC", "PENDING_HR_REVIEW"),
        ]:
            feedback = save_entity(
                session,
                "feedback",
                {
                    "employee_id": e.id,
                    "episode_id": episode,
                    "text": "PRIVATE_FEEDBACK",
                },
            )
            feedback.created_at = datetime.fromisoformat(WHEN)
            save_entity(
                session,
                "feedback_analysis",
                {"feedback_id": feedback.id, "employee_id": e.id, "topics": [topic]},
                status=status,
            )
    data = ai.build_insights(session, head, FILTER)
    assert data["problems"]["items"] == [
        {
            "topic": "Рабочее место",
            "employee_count": 5,
            "suppressed": False,
            "method": "HUMAN_REVIEWED_FEEDBACK",
        }
    ]
    assert data["problems"]["pending_analysis_count"] == 5
    assert "PRIVATE_" not in json.dumps(data)
    assert ai.build_insights(session, hr, FILTER)["problems"]["items"] == []


def test_real_problem_projection_only_resolves_explicit_reply(session, hr):
    e = person(session, 1)
    conv = save_entity(
        session,
        "conversation",
        {"employee_id": e.id, "episode_id": e.data["episode_id"]},
    )
    question = Message(
        id="question",
        conversation_id=conv.id,
        seq=1,
        author_type="EMPLOYEE",
        text="PRIVATE_QUESTION",
        data={},
    )
    session.add(question)
    session.flush()
    save_entity(
        session,
        "answer",
        {
            "employee_id": e.id,
            "episode_id": e.data["episode_id"],
            "conversation_id": conv.id,
            "message_id": question.id,
            "mode": "HANDOFF",
        },
        status="ABSTAINED",
        parent_id=conv.id,
    )
    assert ai.build_insights(session, hr, FILTER)["summary"]["open_questions"] == 1
    session.add(
        Message(
            id="reply",
            conversation_id=conv.id,
            seq=2,
            author_type="HR",
            text="PRIVATE_REPLY",
            data={
                "reply_to_message_id": question.id,
                "resolves_problem_message_id": question.id,
            },
        )
    )
    session.flush()
    data = ai.build_insights(session, hr, FILTER)
    assert data["summary"]["open_questions"] == 0
    assert not any(x in json.dumps(data) for x in ("PRIVATE_QUESTION", "PRIVATE_REPLY"))


def test_http_router_and_csv_use_same_scope_and_formula_safety(session, hr):
    person(session, 1)
    r = run(session, session.get(Entity, "person-1"))
    r.data = {
        **r.data,
        "definition": {**r.data["definition"], "title": "=UNTRUSTED_FORMULA"},
    }
    app = FastAPI()
    app.include_router(ai.router)
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_actor] = lambda: hr
    with TestClient(app) as client:
        url = "/analytics/insights?period_from=2026-10-25&period_to=2026-10-25&timezone=Europe/Moscow"
        response = client.get(url)
        assert response.status_code == 200
        csv = client.get(url.replace("/insights?", "/insights/export?"))
        assert csv.status_code == 200 and "'=" in csv.text
        assert "PRIVATE_EMP_NAME" not in csv.text
        assert (
            client.get(url + "&site_id=not-visible").json()["summary"]["employees"] == 0
        )
