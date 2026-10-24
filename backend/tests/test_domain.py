import copy
import json
from datetime import date, datetime, timedelta, timezone
import pytest
from fastapi import HTTPException
from starlette.requests import Request
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from korsik.db import make_engine, now
from korsik.models import Base, Entity, User, Outbox, Job
from korsik.auth import Actor
from korsik.common import save_entity, etag
from korsik.seed import seed_defaults, default_graph, source_surveys, SOURCE_ROWS
from korsik.scenario_engine import (
    add_months,
    workday_number,
    calculate_due,
    validate_graph,
    semantic_hash,
    validate_answer,
    evaluate_condition,
    local_datetime,
)
from korsik import domain as d


@pytest.fixture
def session():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        seed_defaults(db)
        db.commit()
        yield db
    engine.dispose()


@pytest.fixture
def hr(session):
    u = User(
        id="hr-1",
        email="hr@test.local",
        password_hash="not-login-hash",
        name="HR",
        role="HR_ADMIN",
        scope={"all": True},
        enabled=True,
    )
    session.add(u)
    session.commit()
    return Actor(id=u.id, name=u.name, role=u.role, scope=u.scope)


def profile(session, hr, **changes):
    payload = {
        "name": "Тестовый сотрудник",
        "site_id": "GrPP",
        "start_date": date.today().isoformat(),
        "employment_type": "OFFICE",
        **changes,
    }
    response = d.create_employee(payload, session, hr)
    return session.get(Entity, json.loads(response.body)["id"])


def employee_actor(e):
    return Actor(
        id=e.id, name=e.data["name"], role="EMPLOYEE", scope={}, employee_id=e.id
    )


def req(e):
    return Request({"type": "http", "headers": [(b"if-match", etag(e).encode())]})


def run_for(session, e, key="SUR-D14"):
    j = d.entities(session, "journey", e.id)[0]
    step = next(
        s
        for s in default_graph()["steps"]
        if s.get("survey", {}).get("survey_key") == key
    )
    run = d.open_survey(session, j, step, e)
    session.flush()
    return run


def test_source_seed_complete_and_idempotent(session):
    assert len(SOURCE_ROWS) == 68
    graph = default_graph()
    assert len(graph["source_mapping"]) == 28
    assert {s["survey"]["survey_key"] for s in graph["steps"] if s.get("survey")} == {
        "SUR-D3",
        "SUR-D14",
        "SUR-M1",
        "SUR-M1W3",
        "SUR-END",
    }
    end = source_surveys()["SUR-END"]["questions"]
    assert sum(q["question_key"].startswith("END-P") for q in end) == 9
    result = validate_graph(graph)
    assert result["valid"], result
    count = len(d.entities(session, "scenario"))
    seed_defaults(session)
    session.commit()
    assert len(d.entities(session, "scenario")) == count
    assert len(d.entities(session, "site")) == 3


def test_calendar_workday_clamp_and_timezone():
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert workday_number(
        date(2026, 10, 23), 3, {"working_weekdays": [0, 1, 2, 3, 4]}
    ) == date(2026, 10, 27)
    assert workday_number(
        date(2026, 10, 23),
        3,
        {"exceptions": [{"date": "2026-10-26", "working": False}]},
    ) == date(2026, 10, 28)
    due = calculate_due(
        {
            "type": "ANCHOR_TIME",
            "anchor": "START_DATE",
            "day_basis": "WORKING",
            "workday_number": 3,
            "local_time": "17:30",
        },
        {"start_date": "2026-10-23", "timezone": "Europe/Moscow"},
        {"working_weekdays": [0, 1, 2, 3, 4]},
    )
    assert due == datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)
    first = local_datetime(date(2026, 3, 28), "17:30", "Europe/Berlin")
    second = local_datetime(date(2026, 3, 29), "17:30", "Europe/Berlin")
    assert second - first == timedelta(hours=23)


def test_conditions_numeric_and_decline():
    q = source_surveys()["SUR-D14"]["questions"][2]
    for value in (0, 11, 7.5, True, "7", None):
        with pytest.raises(ValueError):
            validate_answer(q, {"value": value})
    assert validate_answer(q, {"value": 7})["needs_comment"]
    assert (
        validate_answer(q, {"value": 7, "comment_outcome": "DECLINED"})[
            "comment_outcome"
        ]
        == "DECLINED"
    )
    assert validate_answer(q, {"value": 8})["comment_outcome"] == "NOT_REQUESTED"
    assert validate_answer(q, {"outcome": "DECLINED"})["value"] is None
    assert not evaluate_condition(
        q["comment_when"],
        {q["question_key"]: {"outcome": "DECLINED", "value": None}},
        {},
    )
    with pytest.raises(ValueError):
        validate_answer(q, {"outcome": "DECLINED", "value": 0})


def test_multiselect_exclusive_and_gtp_options(session, hr):
    q = source_surveys()["SUR-M1W3"]["questions"][1]
    with pytest.raises(ValueError):
        validate_answer(q, {"value": ["nobody", "manager"]})
    assert validate_answer(q, {"value": ["manager", "colleagues"]})["value"] == [
        "manager",
        "colleagues",
    ]
    e = profile(session, hr, site_id="GTP")
    run = run_for(session, e, "SUR-M1W3")
    assert "remote" not in [
        o["value"] for o in run.data["definition"]["questions"][2]["options"]
    ]


def test_graph_rejects_cycles_targets_code_variables():
    for mutation in ("cycle", "target", "code", "variable"):
        graph = default_graph()
        step = graph["steps"][0]
        if mutation == "cycle":
            step["transitions"] = [
                {"target_step_key": step["step_key"], "condition": {"kind": "ALWAYS"}}
            ]
        if mutation == "target":
            step["transitions"] = [
                {"target_step_key": "nope", "condition": {"kind": "ALWAYS"}}
            ]
        if mutation == "code":
            step["script"] = "print(1)"
        if mutation == "variable":
            step["content"]["text_ty"] = "{{admin_secret}}"
        assert not validate_graph(graph)["valid"], mutation
    graph = default_graph()
    digest = semantic_hash(graph)
    graph["editor_layout"]["nodes"][0]["x"] = 400
    assert semantic_hash(graph) == digest


def test_simulation_does_not_send(session, hr):
    e = profile(session, hr)
    j = d.entities(session, "journey", e.id)[0]
    v = session.get(Entity, j.data["version_id"])
    before = len(session.execute(select(Outbox)).scalars().all())
    result = d.simulate_scenario(
        v.id, {"employee": {**e.data, "employment_stage": "ACTIVE"}}, session, hr
    )
    assert result["simulation_only"] is True
    assert result["outbound_messages_created"] == 0
    assert len(session.execute(select(Outbox)).scalars().all()) == before


def test_review_publish_immutable_and_stale(session, hr):
    s = d.entities(session, "scenario")[0]
    response = d.create_scenario_version(s.id, {}, session, hr)
    v = session.get(Entity, json.loads(response.body)["id"])
    with pytest.raises(HTTPException) as err:
        d.publish_scenario(
            v.id, {"reason": "Публикация согласованной программы"}, session, hr
        )
    assert err.value.status_code == 409
    d.review_scenario(v.id, {}, session, hr)
    d.publish_scenario(
        v.id, {"reason": "Публикация согласованной программы"}, session, hr
    )
    with pytest.raises(HTTPException):
        d.put_scenario_version(v.id, {"graph": default_graph()}, req(v), session, hr)
    assert session.get(Entity, s.id).data["published_version_id"] == v.id


def test_mandatory_source_changes_require_pinned_explicit_approval(session, hr):
    scenario = d.entities(session, "scenario")[0]
    response = d.create_scenario_version(scenario.id, {}, session, hr)
    draft = session.get(Entity, json.loads(response.body)["id"])
    graph = copy.deepcopy(draft.data["graph"])
    mandatory = next(step for step in graph["steps"] if step.get("mandatory"))
    mandatory["mandatory"] = False
    d.put_scenario_version(draft.id, {"graph": graph}, req(draft), session, hr)
    result = d.validate_scenario(draft.id, session, hr)
    assert result["valid"] and result["source_changes"]
    assert any("mandatory" in change["fields"] for change in result["source_changes"])
    with pytest.raises(HTTPException) as missing_ack:
        d.review_scenario(draft.id, {}, session, hr)
    assert missing_ack.value.status_code == 409
    approval = {
        "source_change_acknowledged": True,
        "source_change_fingerprint": result["source_change_fingerprint"],
        "source_change_reason": "Согласовано ответственным HR: шаг стал необязательным",
    }
    d.review_scenario(draft.id, approval, session, hr)
    assert (
        draft.data["review"]["source_change_reason"] == approval["source_change_reason"]
    )
    # A different published baseline invalidates a prior review even if the
    # proposed graph has not changed; cloning an old draft cannot bypass it.
    other_response = d.create_scenario_version(scenario.id, {}, session, hr)
    other = session.get(Entity, json.loads(other_response.body)["id"])
    d.review_scenario(other.id, {}, session, hr)
    d.publish_scenario(
        other.id, {"reason": "Публикация согласованной программы"}, session, hr
    )
    with pytest.raises(HTTPException) as stale:
        d.publish_scenario(
            draft.id, {"reason": "Публикация согласованной программы"}, session, hr
        )
    assert stale.value.status_code == 409
    current = d.validate_scenario(draft.id, session, hr)
    assert current["source_change_fingerprint"] != result["source_change_fingerprint"]
    approval["source_change_fingerprint"] = current["source_change_fingerprint"]
    d.review_scenario(draft.id, approval, session, hr)
    d.publish_scenario(
        draft.id, {"reason": "Публикация согласованной программы"}, session, hr
    )


def test_regional_hr_program_menu_scope_and_employee_applicability(session, hr):
    regional = Actor("regional", "HR A", "HR_ADMIN", {"site_ids": ["GrPP"]})
    other = Actor("other", "HR B", "HR_ADMIN", {"site_ids": ["Luzhniki"]})
    global_program = d.entities(session, "scenario")[0]
    assert global_program.id in {
        row["id"] for row in d.scenarios(session, regional)["items"]
    }
    with pytest.raises(HTTPException) as global_edit:
        d.create_scenario_version(global_program.id, {}, session, regional)
    assert global_edit.value.status_code == 404
    response = d.create_scenario({"name": "Программа площадки A"}, session, regional)
    local = session.get(Entity, json.loads(response.body)["id"])
    assert local.data["scope"] == {"site_ids": ["GrPP"]}
    with pytest.raises(HTTPException):
        d.get_scenario(local.id, session, other)
    with pytest.raises(HTTPException):
        d.create_scenario(
            {"name": "Попытка расширить доступ", "scope": {}}, session, regional
        )
    response = d.create_scenario_version(local.id, {}, session, regional)
    version = session.get(Entity, json.loads(response.body)["id"])
    d.review_scenario(version.id, {}, session, regional)
    d.publish_scenario(
        version.id, {"reason": "Публикация согласованной программы"}, session, regional
    )
    response = d.create_menu({"scenario_id": local.id}, session, regional)
    menu = session.get(Entity, json.loads(response.body)["id"])
    assert menu.id not in {row["id"] for row in d.menus(session, other)["items"]}
    with pytest.raises(HTTPException):
        d.create_menu_version(menu.id, {}, session, other)
    with pytest.raises(HTTPException):
        d.create_menu({"scenario_id": global_program.id}, session, regional)
    employee = save_entity(
        session,
        "employee",
        {
            "site_id": "Luzhniki",
            "employment_stage": "PREBOARDING",
            "episode_id": "episode",
        },
    )
    with pytest.raises(HTTPException) as wrong_site:
        d.enroll_employee(session, employee, local.id)
    assert wrong_site.value.status_code == 422
    employee.data = {**employee.data, "site_id": "GrPP"}
    assert d._scenario_applies(local, employee)


def test_typed_answers_and_old_callback_preserve_position(session, hr):
    e = profile(session, hr)
    run = run_for(session, e)
    actor = employee_actor(e)
    result = d.submit_survey_answer(
        session,
        actor,
        run.id,
        {
            "question_id": "D14-Q1",
            "value": "yes",
            "state_revision": 1,
            "request_id": "a1",
        },
    )
    assert result["current_question_key"] == "D14-Q2"
    again = d.submit_survey_answer(
        session,
        actor,
        run.id,
        {
            "question_id": "D14-Q1",
            "value": "yes",
            "state_revision": 1,
            "request_id": "a1",
        },
    )
    assert again["current_question_key"] == "D14-Q2"
    with pytest.raises(HTTPException):
        d.submit_survey_answer(
            session,
            actor,
            run.id,
            {"question_id": "D14-Q1", "value": "no", "state_revision": 1},
        )
    assert len(run.data["answers"]) == 1


def test_capture_is_draft_until_explicit_confirm(session, hr):
    e = profile(session, hr)
    run = run_for(session, e, "SUR-END")
    actor = employee_actor(e)
    q = run.data["definition"]["questions"][-1]
    d.update(run, current_question_key=q["question_key"])
    cap = d.open_capture(
        session, actor, {"survey_run_id": run.id, "question_id": q["question_key"]}
    )
    cap = d.capture_draft(
        session, actor, cap.id, {"text": "Нужно улучшить доступ к инструкциям"}
    )
    assert not run.data["answers"]
    assert cap.status == "DRAFT"
    d.confirm_capture(
        session, actor, cap.id, {"draft_fingerprint": cap.data["draft_fingerprint"]}
    )
    assert run.status == "IN_PROGRESS"
    assert run.data["awaiting_completion_confirmation"]
    assert (
        run.data["answers"]["END-Q3"]["value"] == "Нужно улучшить доступ к инструкциям"
    )
    assert d.entities(session, "feedback")[0].status == "PENDING_ANALYSIS"
    assert session.execute(
        select(Job).where(Job.kind == "FEEDBACK_ANALYSIS")
    ).scalar_one()


def test_capture_human_and_expiry_guard(session, hr):
    e = profile(session, hr)
    run = run_for(session, e, "SUR-END")
    actor = employee_actor(e)
    q = run.data["definition"]["questions"][-1]
    d.update(run, current_question_key=q["question_key"])
    cap = d.open_capture(session, actor, {"survey_run_id": run.id})
    d.capture_draft(session, actor, cap.id, {"text": "Комментарий"})
    conversation = d.conversation_for(session, e.id)
    d.update(conversation, mode="HUMAN")
    with pytest.raises(HTTPException):
        d.confirm_capture(
            session, actor, cap.id, {"draft_fingerprint": cap.data["draft_fingerprint"]}
        )
    assert not run.data["answers"]


def test_reminders_anchor_real_accepted_once(session, hr):
    e = profile(session, hr)
    run = run_for(session, e)
    out = session.execute(
        select(Outbox).where(Outbox.unique_key.like(f"survey:{run.id}:q:%"))
    ).scalar_one()
    assert not run.data["first_question_accepted_at"]
    d.on_outbox_accepted(session, out)
    d.on_outbox_accepted(session, out)
    jobs = (
        session.execute(select(Job).where(Job.kind == "DOMAIN_SURVEY_REMINDER"))
        .scalars()
        .all()
    )
    assert len(jobs) == 2
    assert run.data["first_question_accepted_at"]
    assert run.data["expires_at"]
    run.status = "COMPLETED"
    before = len(session.execute(select(Outbox)).scalars().all())
    assert d.process_domain_job(session, jobs[0])
    assert len(session.execute(select(Outbox)).scalars().all()) == before


def test_pause_expiry_keeps_clock(session, hr):
    e = profile(session, hr)
    run = run_for(session, e)
    run.status = "PAUSED"
    d.update(run, expires_at=(now() - timedelta(seconds=1)).isoformat())
    d.run_due_scenarios(session)
    assert run.status == "EXPIRED"
    with pytest.raises(HTTPException):
        d.survey_command(run.id, {"command": "RESUME"}, session, employee_actor(e))
    d.survey_command(
        run.id,
        {
            "command": "REOPEN",
            "reason": "Сотрудник попросил продолжить",
            "expires_at": (now() + timedelta(days=2)).isoformat(),
        },
        session,
        hr,
    )
    assert run.status == "IN_PROGRESS"


def test_blocker_confirmation_pinned_attempt(session, hr):
    e = profile(session, hr)
    c = d.create_case_entity(
        session, e.id, "SURVEY_BRANCH", "Нет доступа", kind="BLOCKER"
    )
    session.commit()
    d.resolve_case(c.id, {"result_summary": "Доступ предоставлен"}, req(c), session, hr)
    assert c.status == "RESOLVED_PENDING_CONFIRMATION"
    with pytest.raises(HTTPException):
        d.blocker_response(
            session,
            employee_actor(e),
            c.id,
            {
                "resolution_attempt_id": "old",
                "confirmation_request_id": "old",
                "resolved": True,
            },
        )
    d.blocker_response(
        session,
        employee_actor(e),
        c.id,
        {
            "resolution_attempt_id": c.data["current_resolution_attempt_id"],
            "confirmation_request_id": c.data["confirmation_request_id"],
            "resolved": False,
        },
    )
    assert c.status == "REOPENED"
    d.resolve_case(c.id, {"result_summary": "Повторно настроено"}, req(c), session, hr)
    assert len(c.data["resolution_attempts"]) == 2
    payload = {
        "resolution_attempt_id": c.data["current_resolution_attempt_id"],
        "confirmation_request_id": c.data["confirmation_request_id"],
        "resolved": True,
    }
    d.blocker_response(session, employee_actor(e), c.id, payload)
    assert c.status == "CONFIRMED"
    d.blocker_response(session, employee_actor(e), c.id, payload)


def test_manager_reassignment_resets_ack_and_revokes_old(session, hr):
    for i in (1, 2):
        session.add(
            User(
                id=f"manager-{i}",
                email=f"m{i}@local",
                password_hash="test",
                name="Manager",
                role="MANAGER",
                scope={},
                enabled=True,
            )
        )
    session.commit()
    e = profile(session, hr, manager_id="manager-1")
    resp = d.create_manager_task(
        {"employee_id": e.id, "summary": "Обсудить адаптацию"}, session, hr
    )
    task = session.get(Entity, json.loads(resp.body)["id"])
    m1 = Actor("manager-1", "M1", "MANAGER", {})
    d.acknowledge_task(task.id, {"assignment_revision": 1}, session, m1)
    assert task.status == "ACKNOWLEDGED"
    d.update_task(
        task.id,
        {"assigned_manager_id": "manager-2", "reason": "Замещение"},
        req(task),
        session,
        hr,
    )
    assert task.status == "OPEN"
    assert task.data["acknowledged_at"] is None
    assert len(task.data["history"]) == 1
    m2 = Actor("manager-2", "M2", "MANAGER", {})
    assert len(d.manager_tasks(session, m2)["items"]) == 1
    assert d.dashboard_data(session, m2)["metrics"]["tasks_open"] == 1
    assert d.dashboard_data(session, m2)["employees"] == []
    assert (
        json.loads(d.get_manager_task(task.id, session, m2).body)["employee_name"]
        == e.data["name"]
    )
    with pytest.raises(HTTPException):
        d.get_employee(e.id, session, m2)
    d.acknowledge_task(task.id, {"assignment_revision": 2}, session, m2)
    d.complete_task(
        task.id,
        {
            "assignment_revision": 2,
            "result_code": "DONE",
            "result_summary": "Substitute completed the assigned meeting",
        },
        session,
        m2,
    )
    assert task.status == "COMPLETED"
    with pytest.raises(HTTPException):
        d.complete_task(
            task.id,
            {
                "assignment_revision": 1,
                "result_code": "DONE",
                "result_summary": "Провёл",
            },
            session,
            m1,
        )


def test_dashboard_uses_real_counts_and_hides_raw_feedback(session, hr):
    e = profile(session, hr)
    run = run_for(session, e)
    result = d.dashboard_data(session, hr)
    assert result["metrics"]["employees"] == 1
    assert result["metrics"]["surveys_opened"] == 0
    assert result["privacy"]["raw_feedback_included"] is False
    out = session.execute(
        select(Outbox).where(Outbox.unique_key.like(f"survey:{run.id}:q:%"))
    ).scalar_one()
    d.on_outbox_accepted(session, out)
    assert d.dashboard_data(session, hr)["metrics"]["completion"] == {
        "numerator": 0,
        "denominator": 1,
        "rate": 0.0,
    }


def test_menu_publishes_independently_and_rejects_stale_callback(session, hr):
    e = profile(session, hr)
    journey = d.entities(session, "journey", e.id)[0]
    oldversion = journey.data["version_id"]
    oldmenu = d.employee_menu(session, e.id)
    menu = d.entities(session, "assistant_menu")[0]
    response = d.create_menu_version(menu.id, {}, session, hr)
    v = session.get(Entity, json.loads(response.body)["id"])
    items = copy.deepcopy(v.data["items"])
    items[0]["query_text"] = "Где находятся инструкции?"
    d.menu_version_put(v.id, {"items": items}, req(v), session, hr)
    d.menu_review(v.id, {}, session, hr)
    d.menu_publish(v.id, {}, session, hr)
    assert journey.data["version_id"] == oldversion
    with pytest.raises(HTTPException):
        d.resolve_menu_query(
            session,
            e.id,
            {"menu_version_id": oldmenu["menu_version_id"], "item_id": "food"},
        )
    assert (
        d.resolve_menu_query(
            session, e.id, {"menu_version_id": v.id, "item_id": "food"}
        )
        == "Где находятся инструкции?"
    )


def test_no_send_before_verified_and_termination_cancels(session, hr, monkeypatch):
    monkeypatch.setattr(
        d,
        "now",
        lambda: datetime.combine(date.today(), datetime.min.time(), tzinfo=timezone.utc)
        + timedelta(hours=9),
    )
    e = profile(session, hr)
    d.run_due_scenarios(session)
    assert not session.execute(select(Outbox)).scalars().all()
    d.update(e, max_binding_status="VERIFIED")
    d.run_due_scenarios(session)
    assert session.execute(select(Outbox)).scalars().all()
    d.update(e, employment_stage="ENDED")
    d.run_due_scenarios(session)
    assert d.entities(session, "journey", e.id)[0].status == "CANCELLED"


def test_current_scope_denies_other_employee(session, hr):
    a = profile(session, hr)
    b = profile(session, hr, name="Другой сотрудник")
    run = run_for(session, b)
    with pytest.raises(HTTPException):
        d.submit_survey_answer(
            session,
            employee_actor(a),
            run.id,
            {"question_id": "D14-Q1", "value": "yes"},
        )


def test_answer_dependent_branch_does_not_send_unselected_path(session, hr):
    from korsik.scenario_engine import active_path

    graph = {
        "start_step_key": "a",
        "steps": [
            {
                "step_key": "a",
                "transitions": [
                    {
                        "target_step_key": "yes",
                        "condition": {
                            "kind": "ANSWER",
                            "question_key": "q",
                            "operator": "EQ",
                            "value": "yes",
                        },
                    },
                    {"target_step_key": "no", "condition": {"kind": "ALWAYS"}},
                ],
            },
            {
                "step_key": "yes",
                "transitions": [
                    {"target_step_key": "end", "condition": {"kind": "ALWAYS"}}
                ],
            },
            {
                "step_key": "no",
                "transitions": [
                    {"target_step_key": "end", "condition": {"kind": "ALWAYS"}}
                ],
            },
            {"step_key": "end", "transitions": []},
        ],
    }
    assert active_path(graph, {}, {}) == {"a"}
    assert active_path(graph, {"q": {"value": "yes"}}, {}) == {"a", "yes", "end"}
    assert active_path(graph, {"q": {"value": "no"}}, {}) == {"a", "no", "end"}


def test_accept_receipt_does_not_requeue_other_outbound_steps(session, hr):
    e = profile(session, hr)
    d.update(e, max_binding_status="VERIFIED")
    j = d.entities(session, "journey", e.id)[0]
    # This receipt regression needs two independently due messages, rather than
    # depending on the product introduction (which now intentionally is serial).
    version = session.get(Entity, j.data["version_id"])
    graph = copy.deepcopy(version.data["graph"])
    steps = {step["step_key"]: step for step in graph["steps"]}
    steps["xls-r4"]["trigger"] = copy.deepcopy(steps["xls-r3"]["trigger"])
    for step in graph["steps"]:
        step["delivery_policy"]["quiet_hours_policy"] = "KEEP"
    d.update(version, graph=graph)
    d.replan_journey(session, j, e)
    d.run_due_scenarios(session)
    outbound = list(session.execute(select(Outbox)).scalars())
    assert len(outbound) >= 2
    revision = j.data["schedule_revision"]
    receipt = next(out for out in outbound if out.data["step_key"] == "xls-r3")
    d.on_outbox_accepted(session, receipt)
    d.run_due_scenarios(session)
    assert j.data["schedule_revision"] == revision
    assert len(list(session.execute(select(Outbox)).scalars())) == len(outbound)


def test_validation_rejects_unknown_question_and_wrong_condition_type():
    graph = default_graph()
    graph["steps"][0]["transitions"][0]["condition"] = {
        "kind": "ANSWER",
        "question_key": "unknown",
        "operator": "LT",
        "value": 8,
    }
    assert not validate_graph(graph)["valid"]
    graph = default_graph()
    graph["steps"][0]["transitions"][0]["condition"] = {
        "kind": "ANSWER",
        "question_key": "WELCOME-ADDRESS",
        "operator": "LT",
        "value": 8,
    }
    assert not validate_graph(graph)["valid"]


def test_explicit_calendar_required_and_coverage_blocks():
    from korsik.scenario_engine import (
        add_working_minutes,
        is_workday,
    )

    with pytest.raises(ValueError):
        workday_number(date(2026, 10, 23), 3)
    cal = {
        "timezone": "Europe/Moscow",
        "pattern": "WEEKLY",
        "weekly_working_days": [1, 2, 3, 4, 5],
        "weekly_intervals": [
            {
                "weekday": i,
                "intervals": [
                    {"start_local": "09:00", "end_local": "18:00", "end_day_offset": 0}
                ],
            }
            for i in range(1, 6)
        ],
        "dated_exceptions": [],
        "coverage_from": "2026-01-01",
        "coverage_to": "2026-12-31",
    }
    assert not is_workday(date(2026, 10, 24), cal)
    assert add_working_minutes(
        datetime(2026, 10, 23, 14, 0, tzinfo=timezone.utc), 120, cal
    ) == datetime(2026, 10, 26, 7, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        workday_number(date(2027, 1, 1), 3, cal)


def test_working_sla_handles_overnight_shift():
    from korsik.scenario_engine import add_working_minutes

    cal = {
        "timezone": "Europe/Moscow",
        "weekly_working_days": [1, 2, 3, 4, 5],
        "weekly_intervals": [
            {
                "weekday": i,
                "intervals": [
                    {"start_local": "22:00", "end_local": "06:00", "end_day_offset": 1}
                ],
            }
            for i in range(1, 6)
        ],
        "dated_exceptions": [],
        "coverage_from": "2026-01-01",
        "coverage_to": "2026-12-31",
    }
    assert add_working_minutes(
        datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc), 180, cal
    ) == datetime(2026, 10, 20, 23, 0, tzinfo=timezone.utc)
    assert add_working_minutes(
        datetime(2026, 10, 20, 22, 0, tzinfo=timezone.utc), 60, cal
    ) == datetime(2026, 10, 20, 23, 0, tzinfo=timezone.utc)


def test_unconfigured_sla_does_not_invent_deadline(session, hr):
    e = profile(session, hr)
    case = d.create_case_entity(session, e.id, "EMPLOYEE_REQUEST", "Нужна помощь")
    assert case.data["due_at"] is None
    assert case.data["sla_policy_id"] is None
    assert e.data["legal_entity_id"] == "00000000-0000-4000-8000-000000000001"


def test_digest_preview_is_sideeffect_free_and_uses_complete_weeks(session, hr):
    beforejobs = len(session.execute(select(Job)).scalars().all())
    beforeout = len(session.execute(select(Outbox)).scalars().all())
    result = d.digest_schedule_preview(
        {
            "schedule": {
                "weekday": 5,
                "local_time": "09:00",
                "timezone": "Europe/Moscow",
                "scope": {"all": True},
                "recipient_user_ids": [hr.id],
                "channels": ["HR_PANEL"],
                "enabled": True,
            },
            "occurrence_count": 5,
        },
        session,
        hr,
    )
    assert len(result["occurrences"]) == 5
    for occurrence in result["occurrences"]:
        start = datetime.fromisoformat(occurrence["period_from_local"])
        end = datetime.fromisoformat(occurrence["period_to_local"])
        assert (
            start.weekday() == 0 and end.weekday() == 0 and start.hour == end.hour == 0
        )
        assert end.date() - start.date() == timedelta(days=7)
        assert occurrence["due_at"].endswith("Z")
    assert len(session.execute(select(Job)).scalars().all()) == beforejobs
    assert len(session.execute(select(Outbox)).scalars().all()) == beforeout


def test_digest_period_deduplicated_across_schedule_revision(session, hr, monkeypatch):
    fixed = datetime(2026, 10, 26, 10, tzinfo=timezone.utc)
    monkeypatch.setattr(d, "now", lambda: fixed)
    response = d.digest_schedule_create(
        {
            "enabled": True,
            "weekday": 1,
            "local_time": "09:00",
            "timezone": "Europe/Moscow",
            "scope": {"all": True},
            "recipient_user_ids": [hr.id],
            "channels": ["HR_PANEL", "MAX"],
            "reason": "Еженедельный отчёт",
        },
        session,
        hr,
    )
    schedule = session.get(Entity, json.loads(response.body)["id"])
    schedule.created_at = fixed - timedelta(days=8)
    d.dispatch_digest_schedules(session)
    jobs = list(
        session.execute(select(Job).where(Job.kind == "DOMAIN_WEEKLY_DIGEST")).scalars()
    )
    assert len(jobs) == 1
    d.update(schedule, name="Новое название")
    d.dispatch_digest_schedules(session)
    assert (
        len(
            list(
                session.execute(
                    select(Job).where(Job.kind == "DOMAIN_WEEKLY_DIGEST")
                ).scalars()
            )
        )
        == 1
    )
    d.process_domain_job(session, jobs[0])
    d.process_domain_job(session, jobs[0])
    assert len(d.entities(session, "weekly_digest")) == 1
    assert (
        len(
            [
                o
                for o in session.execute(select(Outbox)).scalars()
                if o.unique_key.endswith(":notify")
            ]
        )
        == 1
    )


def test_dashboard_get_does_not_send_and_materialization_is_deduplicated(session, hr):
    with pytest.raises(HTTPException):
        d.put_notifications(
            {"enabled": True, "minimum_interval_minutes": 15}, session, hr
        )
    d.put_notifications({"enabled": True, "minimum_interval_minutes": 30}, session, hr)
    d.refresh_dashboards(session)
    assert len(d.entities(session, "dashboard_snapshot")) == 1
    assert not session.execute(select(Outbox)).scalars().all()
    profile(session, hr)
    d.dashboard_current(session, hr)
    assert not session.execute(select(Outbox)).scalars().all()
    d.refresh_dashboards(session)
    d.refresh_dashboards(session)
    assert len(d.entities(session, "dashboard_snapshot")) == 2
    assert len(session.execute(select(Outbox)).scalars().all()) == 1


def test_photo_requires_own_validated_asset(session, hr):
    e = profile(session, hr)
    j = d.entities(session, "journey", e.id)[0]
    q = next(
        s["question"]
        for s in default_graph()["steps"]
        if s.get("question", {}).get("question_key") == "PROFILE-PHOTO"
    )
    sq = save_entity(
        session,
        "scenario_question",
        {"employee_id": e.id, "step_key": "xls-r34-photo", "question": q},
        parent_id=j.id,
        status="OPEN",
    )
    other = save_entity(
        session,
        "asset",
        {"content_type": "image/jpeg"},
        owner_id="someone-else",
        status="READY",
    )
    with pytest.raises(HTTPException):
        d.scenario_question_answer(
            session, employee_actor(e), sq.id, {"value": other.id}
        )
    own = save_entity(
        session,
        "asset",
        {
            "content_type": "image/jpeg",
            "employee_id": e.id,
            "episode_id": e.data["episode_id"],
        },
        owner_id=e.id,
        status="READY",
    )
    d.scenario_question_answer(session, employee_actor(e), sq.id, {"value": own.id})
    assert e.data["profile_photo_asset_id"] == own.id
    assert e.data["profile_publication_status"] == "PENDING_HR_APPROVAL"


def test_gap_is_private_and_cannot_resolve_with_unpublished_instruction(session, hr):
    e = profile(session, hr)
    gap = d.ensure_knowledge_gap(session, e.id, "Как оформить доступ?")
    assert d.ensure_knowledge_gap(session, e.id, "Как оформить доступ?").id == gap.id
    draft = save_entity(
        session,
        "document_revision",
        {"article_kind": "APPROVED_INSTRUCTION", "origin_gap_id": gap.id},
        status="DRAFT",
    )
    with pytest.raises(HTTPException):
        d.gap_resolve(gap.id, {"revision_id": draft.id}, session, hr)
    with pytest.raises(HTTPException):
        d.gap_instruction(gap.id, {"anonymization_confirmed": False}, session, hr)
    assert gap.status == "OPEN"


def test_manager_is_restricted_while_head_has_scoped_hr_case_access(session, hr):
    user = User(
        id="manager-p",
        email="private-manager@local",
        password_hash="test",
        name="Manager",
        role="MANAGER",
        scope={},
        enabled=True,
    )
    session.add(user)
    session.commit()
    e = profile(session, hr, manager_id=user.id)
    run = run_for(session, e)
    case = d.create_case_entity(session, e.id, "EMPLOYEE_REQUEST", "Частная жалоба")
    manager = Actor(user.id, user.name, user.role, {})
    with pytest.raises(HTTPException):
        d.survey_get(run.id, session, manager)
    with pytest.raises(HTTPException):
        d.get_case(case.id, session, manager)
    j = d.entities(session, "journey", e.id)[0]
    d.update(j, answers={"private": {"value": "Личный комментарий"}})
    assert "answers" not in d.journey_view(j, manager)
    head = Actor("head", "Head", "HEAD_TALENT", {"all": True})
    assert d.get_case(case.id, session, head).status_code == 200
    assert len(d.list_cases(session, head)["items"]) == 1
    assert "answers" in d.journey_view(j, head)


def test_finish_explicit_and_edit_keeps_revision_history(session, hr):
    e = profile(session, hr)
    run = run_for(session, e, "SUR-M1")
    actor = employee_actor(e)
    d.submit_survey_answer(
        session, actor, run.id, {"question_id": "M1-Q1", "value": "1"}
    )
    assert run.status == "IN_PROGRESS" and run.data["awaiting_completion_confirmation"]
    d.edit_survey_answer(session, actor, run.id, "M1-Q1")
    d.submit_survey_answer(
        session, actor, run.id, {"question_id": "M1-Q1", "value": "2"}
    )
    assert len(d.entities(session, "survey_answer_revision", run.id)) == 2
    d.finish_survey(session, actor, run.id)
    d.finish_survey(session, actor, run.id)
    assert run.status == "COMPLETED"
    assert run.data["answers"]["M1-Q1"]["value"] == "2"
    j = session.get(Entity, run.parent_id)
    assert f"{run.data['step_key']}:SURVEY_COMPLETED" in j.data["events"]


def test_canonical_answer_outcome_and_state_seq(session, hr):
    e = profile(session, hr)
    run = run_for(session, e, "SUR-M1")
    actor = employee_actor(e)
    with pytest.raises(HTTPException):
        d.submit_survey_answer(
            session,
            actor,
            run.id,
            {"question_id": "M1-Q1", "value": "1", "expected_state_seq": 99},
        )
    result = d.submit_survey_answer(
        session,
        actor,
        run.id,
        {"question_id": "M1-Q1", "answer_outcome": "DECLINED", "expected_state_seq": 1},
    )
    assert result["answers"]["M1-Q1"]["outcome"] == "DECLINED"
    assert result["awaiting_completion_confirmation"]


@pytest.mark.parametrize(
    "site,employment_type",
    [
        (site, employment)
        for site in ("GrPP", "Luzhniki", "GTP")
        for employment in ("OFFICE", "PRODUCTION")
    ],
)
def test_full_source_timeline_virtual_clock_no_models(
    session, hr, monkeypatch, site, employment_type
):
    clock = [datetime(2026, 10, 23, 9, tzinfo=timezone.utc)]
    monkeypatch.setattr(d, "now", lambda: clock[0])
    e = profile(
        session,
        hr,
        site_id=site,
        employment_type=employment_type,
        start_date="2026-10-26",
        probation_end_date="2027-01-25",
    )
    d.update(e, max_binding_status="VERIFIED", employment_stage="ACTIVE")
    j = d.entities(session, "journey", e.id)[0]
    d.update(
        j,
        signals={
            "RECRUITER_PROFILE_APPROVED": clock[0].isoformat(),
            "RECRUITER_PLAN_APPROVED": clock[0].isoformat(),
        },
    )
    d.replan_journey(session, j, e)
    actor = employee_actor(e)
    # Advance to each source milestone rather than sleeping or changing server time.
    times = sorted(
        {
            d.aware(a.data["due_at"])
            for a in d.entities(session, "scheduled_action", j.id)
            if a.data.get("due_at")
        }
    )
    for moment in times:
        clock[0] = max(moment, clock[0])
        for _ in range(7):
            d.run_due_scenarios(session)
            for out in list(
                session.execute(
                    select(Outbox).where(Outbox.status == "PENDING")
                ).scalars()
            ):
                out.status = "ACCEPTED"
                d.on_outbox_accepted(session, out)
            for question in d.entities(session, "scenario_question", j.id):
                if question.status != "OPEN":
                    continue
                q = question.data["question"]
                value = "yes"
                if q["type"] == "SINGLE_CHOICE":
                    value = q["options"][0]["value"]
                if q["type"] == "TEXT":
                    value = "Подтверждённый ответ сотрудника"
                if q["type"] == "PHOTO":
                    value = save_entity(
                        session,
                        "asset",
                        {
                            "content_type": "image/jpeg",
                            "employee_id": e.id,
                            "episode_id": e.data["episode_id"],
                        },
                        status="READY",
                        owner_id=e.id,
                    ).id
                d.scenario_question_answer(
                    session, actor, question.id, {"value": value}
                )
            for run in d.entities(session, "survey_run", j.id):
                if run.status != "IN_PROGRESS":
                    continue
                while d.current_question(run):
                    q = d.current_question(run)
                    if q["type"] == "NUMERIC":
                        value = 8
                    elif q["type"] == "TEXT":
                        value = "Предложение по улучшению"
                    elif q["type"] == "MULTI_CHOICE":
                        value = [q["options"][0]["value"]]
                    else:
                        value = q["options"][0]["value"]
                    d.submit_survey_answer(
                        session,
                        actor,
                        run.id,
                        {
                            "question_id": q["question_key"],
                            "value": value,
                            "comment": "Уточнение" if q.get("comment_when") else None,
                            "comment_outcome": (
                                "PROVIDED" if q.get("comment_when") else "NOT_REQUESTED"
                            ),
                        },
                    )
                if run.data.get("awaiting_completion_confirmation"):
                    d.finish_survey(session, actor, run.id)
    clock[0] += timedelta(minutes=1)
    for _ in range(5):
        d.run_due_scenarios(session)
        for out in list(
            session.execute(select(Outbox).where(Outbox.status == "PENDING")).scalars()
        ):
            out.status = "ACCEPTED"
            d.on_outbox_accepted(session, out)
    runs = d.entities(session, "survey_run", j.id)
    assert len(runs) == 5
    assert all(r.status == "COMPLETED" for r in runs)
    assert j.status == "COMPLETED", [
        (a.data["step_key"], a.status, a.data.get("calendar_warning"))
        for a in d.entities(session, "scheduled_action", j.id)
        if a.status not in ("COMPLETED", "ACCEPTED", "SKIPPED", "NOT_APPLICABLE")
    ]
    final = next(r for r in runs if r.data["survey_key"] == "SUR-END")
    assert sum(k.startswith("END-P") for k in final.data["answers"]) == 9
    assert not any(
        out.text.startswith("#") for out in session.execute(select(Outbox)).scalars()
    )


def test_handoff_claim_connects_human_and_cancels_capture(session, hr):
    e = profile(session, hr)
    conversation = save_entity(
        session,
        "conversation",
        {
            "employee_id": e.id,
            "episode_id": e.data["episode_id"],
            "mode": "WAITING_HR",
            "state_seq": 2,
        },
        parent_id=e.id,
    )
    case = save_entity(
        session,
        "case",
        {
            "employee_id": e.id,
            "kind": "HANDOFF",
            "episode_id": e.data["episode_id"],
            "conversation_id": conversation.id,
            "assigned_user_id": None,
        },
        status="OPEN",
        parent_id=e.id,
    )
    capture = save_entity(
        session, "input_capture", {"employee_id": e.id}, status="OPEN", parent_id=e.id
    )
    session.commit()
    d.claim_case(case.id, session, hr)
    assert conversation.data["mode"] == "HUMAN"
    assert case.data["assigned_user_id"] == hr.id
    assert capture.status == "CANCELLED"
    d.claim_case(case.id, session, hr)
    assert (
        len(
            [
                o
                for o in session.execute(select(Outbox)).scalars()
                if o.unique_key.startswith("case:")
            ]
        )
        == 0
    )


def test_accepted_receipt_uses_real_timestamp_not_processing_time(
    session, hr, monkeypatch
):
    e = profile(session, hr)
    run = run_for(session, e)
    out = session.execute(
        select(Outbox).where(Outbox.unique_key.like(f"survey:{run.id}:q:%"))
    ).scalar_one()
    receipt = datetime(2026, 10, 23, 14, 30, 25, tzinfo=timezone.utc)
    out.data = {**out.data, "accepted_at": receipt.isoformat()}
    monkeypatch.setattr(d, "now", lambda: receipt + timedelta(hours=2))
    d.on_outbox_accepted(session, out)
    assert d.aware(run.data["first_question_accepted_at"]) == receipt
    jobs = list(
        session.execute(
            select(Job).where(Job.kind == "DOMAIN_SURVEY_REMINDER")
        ).scalars()
    )
    assert d.aware(jobs[0].run_at) == receipt + timedelta(days=1)


def test_late_receipt_after_employment_end_does_not_enqueue_reminders(session, hr):
    e = profile(session, hr)
    run = run_for(session, e)
    out = session.execute(
        select(Outbox).where(Outbox.unique_key.like(f"survey:{run.id}:q:%"))
    ).scalar_one()
    j = session.get(Entity, run.parent_id)
    d.cancel_journey(session, j, "ENDED")
    d.update(e, employment_stage="ENDED")
    d.on_outbox_accepted(session, out)
    assert run.data["first_question_accepted_at"]
    assert not list(
        session.execute(
            select(Job).where(Job.kind == "DOMAIN_SURVEY_REMINDER")
        ).scalars()
    )


def test_csv_import_validates_before_atomic_apply_and_updates_by_external_id(
    session, hr
):
    e = profile(session, hr, external_employee_id="E-1")
    before = len(d.entities(session, "employee"))
    rows = [
        {
            "name": "Новый",
            "external_employee_id": "E-2",
            "site_id": "GrPP",
            "start_date": "2026-11-01",
        },
        {
            "name": "Дубль",
            "external_employee_id": "E-1",
            "site_id": "GrPP",
            "start_date": "2026-11-01",
        },
    ]
    preview = d.import_employees({"rows": rows, "validate_only": True}, session, hr)
    assert not preview["valid"]
    assert len(d.entities(session, "employee")) == before
    with pytest.raises(HTTPException):
        d.import_employees({"rows": rows, "validate_only": False}, session, hr)
    assert len(d.entities(session, "employee")) == before
    updated = d.import_employees(
        {
            "rows": [
                {
                    "name": "Обновлённое имя",
                    "external_employee_id": "E-1",
                    "site_id": "GrPP",
                    "start_date": e.data["start_date"],
                }
            ],
            "mode": "UPDATE",
            "validate_only": False,
        },
        session,
        hr,
    )
    assert updated["updated"] == 1 and updated["created"] == 0
    assert e.data["name"] == "Обновлённое имя"


def test_fourth_site_calendar_configurable_without_code(session, hr):
    response = d.create_site(
        {
            "name": "Новая площадка",
            "legal_entity_id": "00000000-0000-4000-8000-000000000001",
            "timezone": "Europe/Moscow",
        },
        session,
        hr,
    )
    site = session.get(Entity, json.loads(response.body)["id"])
    e = profile(session, hr, site_id=site.id)
    j = d.entities(session, "journey", e.id)[0]
    assert any(
        a.data.get("calendar_warning")
        for a in d.entities(session, "scheduled_action", j.id)
        if a.data["step"].get("trigger", {}).get("day_basis") == "WORKING"
    )
    cal_response = d.create_calendar(
        {"name": "Календарь новой площадки", "site_id": site.id}, session, hr
    )
    cal = session.get(Entity, json.loads(cal_response.body)["id"])
    version_response = d.create_calendar_version(
        cal.id,
        {
            "working_weekdays": [0, 1, 2, 3, 4],
            "timezone": "Europe/Moscow",
            "coverage_start": "2026-01-01",
            "coverage_end": "2030-12-31",
        },
        session,
        hr,
    )
    version = session.get(Entity, json.loads(version_response.body)["id"])
    preview = d.calendar_preview(cal.id, version.id, session, hr)
    # New site's assignment is represented by the calendar's site binding.
    d.calendar_activate(
        cal.id,
        version.id,
        {"preview_id": preview["preview_id"], "preview_hash": preview["preview_hash"]},
        session,
        hr,
    )
    assert site.data["calendar_id"] == cal.id
    d.replan_journey(session, j, e)
    assert not any(
        a.data.get("calendar_warning")
        for a in d.entities(session, "scheduled_action", j.id)
    )


def test_chat_uses_published_menu_ids_and_active_questions(session, hr):
    from korsik import chat
    from fastapi import Response

    e = profile(session, hr)
    conv = chat.conversation_for(session, e)
    j = d.entities(session, "journey", e.id)[0]
    q = next(s["question"] for s in default_graph()["steps"] if s.get("question"))
    sq = save_entity(
        session,
        "scenario_question",
        {"employee_id": e.id, "step_key": "xls-r5", "question": q},
        status="OPEN",
        parent_id=j.id,
    )
    detail = chat.conversation_detail(conv.id, Response(), employee_actor(e), session)
    assert detail["employee_name"] == e.data["name"]
    assert detail["scenario_questions"][0]["id"] == sq.id
    menu = chat.quick_prompts_for(session, conv)
    assert len(menu["items"]) == 2
    assert menu["items"][0]["prompt_key"] == "food"
    assert menu["items"][0]["action"]["menu_version_id"] == menu["menu_version_id"]


def test_chat_latest_page_and_current_source_redaction(session, hr, monkeypatch):
    from korsik import chat, retrieval
    from korsik.common import add_message

    e = profile(session, hr)
    conv = chat.conversation_for(session, e)
    for i in range(12):
        add_message(
            session,
            conv,
            "BOT",
            f"Сообщение {i}",
            {"citations": [{"revision_id": "rev-old"}]} if i == 11 else {},
        )
    session.flush()
    monkeypatch.setattr(retrieval, "answer_still_authorized", lambda db, c, a: False)
    page = chat.messages(conv.id, limit=3, actor=employee_actor(e), db=session)
    assert [r["seq"] for r in page["items"]] == [10, 11, 12]
    assert page["items"][-1]["data"]["source_access_revoked"]
    assert "Сообщение 11" != page["items"][-1]["text"]
    first = chat.messages(
        conv.id, after_seq=0, limit=3, actor=employee_actor(e), db=session
    )
    assert [r["seq"] for r in first["items"]] == [1, 2, 3]
    assert first["has_more"]


def test_create_employee_web_user_persists_directory_link(session, hr):
    from korsik import auth, chat

    e = profile(session, hr)
    conv = chat.conversation_for(session, e)
    result = auth.create_user(
        auth.UserInput(
            email="employee@local.test",
            name="Employee account",
            role="EMPLOYEE",
            password="temporary-test-password-123",
            employee_id=e.id,
        ),
        hr,
        session,
    )
    assert e.data["user_id"] == result["id"]
    assert conv.owner_id == result["id"]
    with pytest.raises(HTTPException):
        auth.create_user(
            auth.UserInput(
                email="second@local.test",
                name="Duplicate",
                role="EMPLOYEE",
                password="temporary-test-password-123",
                employee_id=e.id,
            ),
            hr,
            session,
        )


@pytest.mark.parametrize("fmt", ["CSV", "XLSX", "PDF"])
def test_report_export_real_formats_and_formula_safe(
    session, hr, tmp_path, monkeypatch, fmt
):
    import korsik.settings as settings
    from types import SimpleNamespace

    profile(session, hr, name='=HYPERLINK("https://example.invalid")')
    monkeypatch.setattr(
        settings, "get_settings", lambda: SimpleNamespace(blob_root=str(tmp_path))
    )
    response = d.create_report_export(
        {
            "dataset": "ADAPTATION",
            "format": fmt,
            "filters": {},
            "columns": ["name", "stage"],
            "reason": "Проверка отчёта",
        },
        session,
        hr,
    )
    body = json.loads(response.body)
    operation = session.get(Entity, body["id"])
    job = session.scalar(select(Job).where(Job.kind == "DOMAIN_REPORT_EXPORT"))
    assert operation.status == "PENDING"
    d.process_domain_job(session, job)
    session.commit()
    assert operation.status == "SUCCEEDED", operation.data.get("error")
    from pathlib import Path

    content = Path(operation.data["artifact_path"]).read_bytes()
    if fmt == "CSV":
        assert content.startswith(b"\xef\xbb\xbf")
        assert "'=HYPERLINK" in content.decode("utf-8-sig")
    if fmt == "XLSX":
        from openpyxl import load_workbook
        import io

        sheet = load_workbook(io.BytesIO(content)).active
        assert (
            sheet["A2"].value.startswith("'=HYPERLINK") and sheet["A2"].data_type == "s"
        )
    if fmt == "PDF":
        assert content.startswith(b"%PDF-")
    assert (
        d.report_export_content(operation.id, session, hr).media_type
        == operation.data["content_type"]
    )


def test_episode_stage_and_reports_require_current_scope(session, hr):
    e = profile(session, hr)
    episode = session.get(Entity, e.data["episode_id"])
    d.employment_episode_stage(
        episode.id,
        {
            "expected_episode_id": episode.id,
            "expected_state_version": episode.version,
            "stage": "ACTIVE",
            "effective_at": now().isoformat(),
            "reason": "Фактический выход подтверждён",
        },
        session,
        hr,
    )
    assert episode.status == "ACTIVE"
    assert e.data["employment_stage"] == "ACTIVE"
    rows = d.report_adaptation(db=session, actor=hr)["items"]
    assert rows[0]["employee_id"] == e.id
    assert rows[0]["total_mandatory"] > 0
    operator = Actor("operator", "Operator", "PLATFORM_ADMIN", {"all": True})
    with pytest.raises(HTTPException):
        d.report_adaptation(db=session, actor=operator)
    with pytest.raises(HTTPException):
        d.create_scenario({"name": "No business grant"}, session, operator)


def test_capture_canonical_target_and_explicit_replacement(session, hr):
    e = profile(session, hr)
    run = run_for(session, e)
    actor = employee_actor(e)
    first = d.open_capture(
        session,
        actor,
        {
            "target": {
                "kind": "SURVEY_QUESTION",
                "survey_run_id": run.id,
                "question_id": "D14-Q1",
            }
        },
    )
    assert (
        d.open_capture(
            session, actor, {"survey_run_id": run.id, "question_id": "D14-Q1"}
        ).id
        == first.id
    )
    other = run_for(session, e, "SUR-M1")
    # open_survey correctly refuses a simultaneous second questionnaire.
    assert other is None


def test_sla_policy_etag_fences_concurrent_change(session, hr):
    initial = d.set_sla(
        {"queue_rules": []}, Request({"type": "http", "headers": []}), session, hr
    )
    first = session.get(Entity, json.loads(initial.body)["id"])
    stale = req(first)
    second = d.set_sla({"queue_rules": []}, stale, session, hr)
    latest = session.get(Entity, json.loads(second.body)["id"])
    assert first.status == "ARCHIVED" and latest.status == "ACTIVE"
    with pytest.raises(HTTPException) as error:
        d.set_sla({"queue_rules": []}, stale, session, hr)
    assert error.value.status_code == 412
    assert (
        len([x for x in d.entities(session, "sla_policy") if x.status == "ACTIVE"]) == 1
    )
    assert d.get_sla(session, hr).headers["etag"] == etag(latest)


def test_scoped_account_admin_cannot_reset_cross_legal_account(session, hr):
    from korsik import auth_admin

    scoped = Actor(
        "scoped-hr",
        "Scoped HR",
        "HR_ADMIN",
        {"site_ids": ["GrPP"], "legal_entity_ids": ["legal-a"]},
    )
    foreign = User(
        id="foreign-hr",
        email="foreign@test.local",
        name="Foreign HR",
        role="HR_SPECIALIST",
        scope={"site_ids": ["GrPP"], "legal_entity_ids": ["legal-b"]},
        password_hash="test",
        enabled=True,
    )
    session.add(foreign)
    session.commit()
    assert auth_admin.accessible(session, scoped, foreign) is False
    with pytest.raises(HTTPException) as error:
        auth_admin.issue_reset(
            foreign.id, {"reason": "Проверка личности"}, session, scoped
        )
    assert error.value.status_code == 404
    foreign.scope = {"site_ids": ["GrPP"], "legal_entity_ids": ["legal-a"]}
    assert auth_admin.accessible(session, scoped, foreign) is True
    employee = profile(session, hr)
    d.update(employee, legal_entity_id="legal-a")
    foreign.employee_id = employee.id
    foreign.scope = {"site_ids": ["GrPP"], "legal_entity_ids": ["legal-b"]}
    assert auth_admin.accessible(session, scoped, foreign) is False
    foreign.role = "EMPLOYEE"
    assert auth_admin.accessible(session, scoped, foreign) is True
    foreign.role = "MANAGER"
    foreign.scope = {}
    foreign.employee_id = None
    assert auth_admin.accessible(session, scoped, foreign) is False


def test_report_period_filters_actual_feedback_and_answers(session, hr):
    e = profile(session, hr)
    run = run_for(session, e)
    d.update(
        run,
        answers={
            "old": {"value": "old", "answered_at": "2026-01-03T10:00:00+00:00"},
            "current": {"value": "yes", "answered_at": "2026-10-23T10:00:00+00:00"},
        },
    )
    old = save_entity(session, "feedback", {"employee_id": e.id})
    old.created_at = datetime(2026, 1, 3, tzinfo=timezone.utc)
    current = save_entity(session, "feedback", {"employee_id": e.id})
    current.created_at = datetime(2026, 10, 23, tzinfo=timezone.utc)
    for feedback, topic in [(old, "Old topic"), (current, "Current topic")]:
        save_entity(
            session,
            "feedback_analysis",
            {"employee_id": e.id, "feedback_id": feedback.id, "topics": [topic]},
            status="REVIEWED",
        )
    filters = {"period_from": "2026-10-01", "period_to": "2026-10-31"}
    rows, _ = d.risk_topic_rows(session, hr, filters)
    assert len(rows) == 1 and rows[0]["topic"] is None and rows[0]["suppressed"]
    rows = d._export_rows(session, hr, {"dataset": "SURVEYS", "filters": filters})
    assert [r["question_id"] for r in rows] == ["current"]
    with pytest.raises(HTTPException):
        d.risk_topic_rows(session, hr, {"period_from": "not-a-date"})


def test_legal_site_scope_is_checked_for_create_and_transfer(session, hr):
    scoped = Actor(
        "legal-limited",
        "HR",
        "HR_ADMIN",
        {"site_ids": ["GrPP", "GTP"], "legal_entity_ids": ["legal-a"]},
    )
    with pytest.raises(HTTPException):
        d.create_employee_record(
            session,
            scoped,
            {
                "name": "Should deny",
                "site_id": "GrPP",
                "start_date": date.today().isoformat(),
            },
        )
    e = profile(session, hr)
    d.update(session.get(Entity, "GrPP"), legal_entity_id="legal-a")
    d.update(e, legal_entity_id="legal-a")
    with pytest.raises(HTTPException):
        d.employee_change_preview(
            e.id,
            {"action": "TRANSFER_SITE", "new_site_id": "GTP", "reason": "Вне юрлица"},
            session,
            scoped,
        )


def test_local_ai_registry_selects_deployed_generator_and_validates_input(
    session, hr, tmp_path, monkeypatch
):
    import hashlib
    from pathlib import Path
    from types import SimpleNamespace
    from korsik import local_ai

    for folder in ("embedder", "reranker", "generator", "generator-2b"):
        root = tmp_path / folder
        root.mkdir()
        role = "generator" if folder.startswith("generator") else folder
        manifest = {
            "role": role,
            "repository": folder,
            "revision": "pinned-test-revision",
            "files": [],
        }
        encoded = json.dumps(manifest).encode()
        (root / "manifest.json").write_bytes(encoded)
        if role == "generator":
            report = {
                "artifact_fingerprint": hashlib.sha256(encoded).hexdigest(),
                "status": "PASS" if folder == "generator-2b" else "FAIL",
                "cases": [{"fixture": "synthetic"}],
                "reviewer": "implementation-test",
            }
            (root / "quality-report.json").write_text(json.dumps(report))
    monkeypatch.setattr(
        local_ai,
        "get_settings",
        lambda: SimpleNamespace(
            model_manifest_dir=str(tmp_path),
            generator_model_subdir="generator-2b",
            product_mode="FULL",
        ),
    )
    artifacts = local_ai.register_models(session)
    generators = [a for a in artifacts if a.data["role"] == "generator"]
    assert len(generators) == 2
    chosen = next(a for a in generators if a.data["deployment_selected"])
    assert (
        chosen.data["bundle_subdir"] == "generator-2b"
        and chosen.data["quality_evaluation"]["status"] == "STALE_PIPELINE_REVIEW"
    )
    assert chosen.data["quality_evaluation"]["report_status"] == "PASS"
    assert not chosen.data["quality_evaluation"]["pipeline_review_current"]
    failed = next(a for a in generators if not a.data["deployment_selected"])
    assert failed.data["quality_evaluation"]["status"] == "FAIL"
    review_path = tmp_path / "generator-2b" / "quality-report.json"
    current_review = json.loads(review_path.read_text())
    current_review["pipeline_source_sha256"] = hashlib.sha256(
        Path(local_ai.__file__).with_name("retrieval.py").read_bytes()
    ).hexdigest()
    current_review["generation_runtime_source_sha256"] = local_ai.pipeline_identities()[
        "generation_runtime_source_fingerprint"
    ]
    review_path.write_text(json.dumps(current_review))
    artifacts = local_ai.register_models(session)
    chosen = next(
        a
        for a in artifacts
        if a.data["role"] == "generator" and a.data["deployment_selected"]
    )
    assert chosen.data["quality_evaluation"]["status"] == "PASS"
    assert chosen.data["quality_evaluation"]["pipeline_review_current"]
    assert "NOT_COMPANY_ACCEPTANCE" in chosen.data["quality_evaluation"]["scope"]
    result = local_ai.create_profile({"name": "CPU 2B"}, session, hr)
    assert result["configuration"]["models"]["generator"] == chosen.id
    assert result["status"] == "DRAFT"
    for body in (
        {"models": {}},
        {"retrieval_profile": "uninstalled"},
        {"name": ""},
        {
            "models": {
                "embedder": chosen.id,
                "reranker": chosen.id,
                "generator": chosen.id,
            }
        },
    ):
        with pytest.raises(HTTPException) as error:
            local_ai.create_profile(body, session, hr)
        assert error.value.status_code == 422


def test_late_join_after_probation_requires_hr_review(session, hr):
    e = profile(session, hr, start_date="2020-01-10", probation_end_date="2020-04-09")
    journey = d.entities(session, "journey", e.id)[0]
    assert journey.status == "PAUSED" and journey.data["requires_hr_review"]
    assert d.run_due_scenarios(session) == 0
    with pytest.raises(HTTPException):
        d.journey_command(
            journey.id,
            {"command": "RESUME", "resume_policy": {"date_policy": "KEEP_DATES"}},
            session,
            hr,
        )
    assert journey.status == "PAUSED"
    d.journey_command(
        journey.id,
        {
            "command": "RESUME",
            "resume_policy": {"date_policy": "KEEP_DATES"},
            "reason": "HR проверил применимость позднего маршрута",
        },
        session,
        hr,
    )
    assert not journey.data["requires_hr_review"]
    d.update(e, employment_stage="ENDED")
    with pytest.raises(HTTPException):
        d.journey_command(journey.id, {"command": "ACTIVATE"}, session, hr)


def test_directory_and_calendars_do_not_cross_scope(session, hr):
    manager = User(
        id="limited-manager",
        email="limited-manager@test.local",
        name="Manager",
        role="MANAGER",
        scope={},
        password_hash="test",
        enabled=True,
    )
    other = User(
        id="foreign-directory",
        email="private-foreign@test.local",
        name="Other Manager",
        role="MANAGER",
        scope={},
        password_hash="test",
        enabled=True,
    )
    session.add_all([manager, other])
    session.flush()
    e = profile(session, hr, manager_id=manager.id)
    actor = Actor(manager.id, manager.name, manager.role, manager.scope)
    result = d.directory(session, actor)
    assert {u["id"] for u in result["users"]} == {manager.id}
    assert all("email" not in u or u["id"] == manager.id for u in result["users"])
    assert {site["id"] for site in result["sites"]} == {e.data["site_id"]}
    scoped = Actor("scoped-calendar-admin", "HR", "HR_ADMIN", {"site_ids": ["GrPP"]})
    foreign = d.create_calendar(
        {"name": "Foreign calendar", "site_id": "GTP"}, session, hr
    )
    foreign_id = json.loads(foreign.body)["id"]
    with pytest.raises(HTTPException):
        d.create_calendar_version(
            foreign_id, {"weekly_working_days": [1, 2, 3, 4, 5]}, session, scoped
        )
    with pytest.raises(HTTPException):
        d.create_site({"name": "Unapproved new site"}, session, scoped)
    with pytest.raises(HTTPException):
        d.legal_entity_create({"name": "Unapproved new entity"}, session, scoped)


def test_employee_conversation_access_is_fenced_by_episode_and_ended(session, hr):
    from korsik import chat

    e = profile(session, hr)
    actor = employee_actor(e)
    old = chat.conversation_for(session, e)
    oldid = old.id
    assert chat.can_conversation(session, actor, old)
    d.update(e, episode_id="new-episode")
    assert not chat.can_conversation(session, actor, old)
    assert chat.can_conversation(session, hr, old)
    d.update(e, employment_stage="ENDED")
    assert chat.conversations(hr, session)["items"] == []
    with pytest.raises(HTTPException):
        chat.conversation_for(session, e)
    assert chat.conversation_for(session, e, create=False) is None
    assert [c.id for c in d.entities(session, "conversation", e.id)] == [oldid]


def test_reenable_employee_account_rejects_competing_user_and_operator_reset(
    session, hr
):
    from korsik import auth_admin

    e = profile(session, hr)
    old = User(
        id="old-account",
        email="old-account@test.local",
        name="Old",
        role="EMPLOYEE",
        scope={},
        employee_id=e.id,
        password_hash="test",
        enabled=False,
    )
    active = User(
        id="new-account",
        email="new-account@test.local",
        name="New",
        role="EMPLOYEE",
        scope={},
        employee_id=e.id,
        password_hash="test",
        enabled=True,
    )
    manager = User(
        id="operator-must-not-reset",
        email="protected-manager@test.local",
        name="Manager",
        role="MANAGER",
        scope={},
        password_hash="test",
        enabled=True,
    )
    session.add_all([old, active, manager])
    session.commit()
    request = Request(
        {"type": "http", "headers": [(b"if-match", auth_admin.user_etag(old).encode())]}
    )
    with pytest.raises(HTTPException) as error:
        auth_admin.update_user(
            old.id,
            {"enabled": True, "reason": "Подтверждён повторный доступ"},
            request,
            session,
            hr,
        )
    assert error.value.status_code == 409 and old.enabled is False
    operator = Actor("technical-operator", "Operator", "PLATFORM_ADMIN", {"all": True})
    assert not auth_admin.accessible(session, operator, manager)
    with pytest.raises(HTTPException):
        auth_admin.issue_reset(
            manager.id, {"reason": "No business grant"}, session, operator
        )


def test_local_ai_validation_is_reloadable_and_pending_test_deduplicates(session, hr):
    from korsik import local_ai
    from fastapi import Response

    profile_row = save_entity(
        session,
        "local_ai_profile",
        {"configuration": {"models": {}, "generation_policy": "LOCAL_GENERATION"}},
        status="DRAFT",
    )
    session.commit()
    first = local_ai.test_profile(profile_row.id, {}, req(profile_row), session, hr)
    second = local_ai.test_profile(profile_row.id, {}, req(profile_row), session, hr)
    assert first["validation_id"] == second["validation_id"]
    response = Response()
    detail = local_ai.get_profile(profile_row.id, response, session, hr)
    assert detail["latest_validation_id"] == first["validation_id"]
    assert (
        len(detail["validations"]) == 1
        and detail["validations"][0]["status"] == "QUEUED"
    )
    assert response.headers["etag"] == etag(profile_row)
    assert (
        len([j for j in session.scalars(select(Job)) if j.kind == "LOCAL_AI_VALIDATE"])
        == 1
    )


def test_operation_owner_still_requires_current_role_and_scope_after_purge(session, hr):
    op = save_entity(
        session,
        "operation",
        {
            "type": "KNOWLEDGE_PURGE",
            "operation_scope": {"site_ids": ["GrPP"], "legal_entity_id": "legal-a"},
        },
        status="SUCCEEDED",
        owner_id=hr.id,
    )
    d.operation_authorize(session, hr, op)
    changed = Actor(
        hr.id,
        "Changed",
        "HR_ADMIN",
        {"site_ids": ["GrPP"], "legal_entity_ids": ["legal-b"]},
    )
    with pytest.raises(HTTPException):
        d.operation_authorize(session, changed, op)
    former = Actor(hr.id, "Former", "EMPLOYEE", {}, "employee-id")
    with pytest.raises(HTTPException):
        d.operation_authorize(session, former, op)


def test_weekly_digest_uses_only_reviewed_feedback_and_privacy_denominators(
    session, hr
):
    from types import SimpleNamespace

    start = datetime(2026, 10, 19, tzinfo=timezone.utc)
    end = start + timedelta(days=7)
    schedule = save_entity(
        session,
        "digest_schedule",
        {
            "enabled": True,
            "owner_epoch": 1,
            "scope": {"all": True},
            "recipient_user_ids": [hr.id],
            "channels": ["HR_PANEL"],
            "timezone": "Europe/Moscow",
        },
    )
    employees = [profile(session, hr, name=f"Digest fixture {i}") for i in range(6)]
    for i, e in enumerate(employees):
        feedback = save_entity(
            session,
            "feedback",
            {
                "employee_id": e.id,
                "text": "Raw personal text must not appear in aggregate",
            },
        )
        feedback.created_at = start + timedelta(days=1)
        save_entity(
            session,
            "feedback_analysis",
            {
                "feedback_id": feedback.id,
                "employee_id": e.id,
                "sentiment": "POSITIVE" if i < 5 else "NEGATIVE",
                "topics": ["Рабочее место"],
            },
            status="REVIEWED" if i < 5 else "PENDING_HR_REVIEW",
        )
    job = SimpleNamespace(
        payload={
            "schedule_id": schedule.id,
            "owner_epoch": 1,
            "period": "2026-10-19",
            "period_from": start.isoformat(),
            "period_to": end.isoformat(),
        }
    )
    d.materialize_weekly_digest(session, job)
    digest = d.entities(session, "weekly_digest", hr.id)[0]
    metrics = digest.data["metrics"]
    assert (
        metrics["feedback_received"] == 6
        and metrics["feedback_reviewed"] == 5
        and metrics["feedback_pending_review"] == 1
    )
    assert metrics["sentiment_distribution"] == [
        {
            "sentiment": "POSITIVE",
            "employee_count": 5,
            "denominator": 5,
            "suppressed": False,
            "method": "LATEST_HUMAN_REVIEWED_FEEDBACK_PER_EMPLOYEE",
        }
    ]
    assert (
        metrics["topics"][0]["employee_count"] == 5
        and metrics["timezone"] == "Europe/Moscow"
    )
    assert metrics["completion"]["rate"] is None
    assert "Raw personal text" not in json.dumps(digest.data)
    # The same template with one respondent cannot reveal the topic or label.
    single = save_entity(
        session,
        "digest_schedule",
        {**schedule.data, "scope": {"site_ids": ["isolated-fixture"]}},
    )
    d.update(employees[0], site_id="isolated-fixture")
    d.materialize_weekly_digest(
        session, SimpleNamespace(payload={**job.payload, "schedule_id": single.id})
    )
    small = d.entities(session, "weekly_digest", hr.id)[-1].data["metrics"]
    assert (
        small["sensitive_aggregates_suppressed"] and small["topics"][0]["topic"] is None
    )
    assert small["sentiment_distribution"][0]["sentiment"] is None


def test_head_talent_has_scoped_hr_drilldown_without_grant_escalation(session, hr):
    profile(session, hr)
    head = Actor("head", "Talent Head", "HEAD_TALENT", {"all": True})
    view = d.dashboard_data(session, head)
    assert view["metrics"]["employees"] == 1 and len(view["employees"]) == 1
    assert d.directory(session, head)["users"]
    assert all("email" not in user for user in d.directory(session, head)["users"])
    assert len(d.list_employees(session, head)["items"]) == 1
    assert len(d.journeys(session, head)["items"]) == 1
    assert d.validate_export_request(
        {"dataset": "ADAPTATION", "format": "CSV", "reason": "Personal report"}, head
    )["dataset"] == "ADAPTATION"
    foreign = Actor("head", "Talent Head", "HEAD_TALENT", {"site_ids": ["foreign"]})
    assert d.list_employees(session, foreign)["items"] == []


def test_generic_cancel_does_not_strand_purge_or_publish(session, hr):
    for kind in ("KNOWLEDGE_PURGE", "KNOWLEDGE_ROLLBACK", "KNOWLEDGE_PUBLISH"):
        op = save_entity(
            session,
            "operation",
            {"type": kind, "operation_scope": {"all": True}},
            status="PENDING",
            owner_id=hr.id,
        )
        with pytest.raises(HTTPException) as error:
            d.operation_cancel(
                op.id, {"reason": "Попытка общей отмены"}, req(op), session, hr
            )
        assert error.value.status_code == 409 and op.status == "PENDING"
        assert not op.data.get("cancel_requested")
    report = save_entity(
        session,
        "operation",
        {"type": "REPORT_EXPORT", "export_scope": {"all": True}},
        status="PENDING",
        owner_id=hr.id,
    )
    d.operation_cancel(
        report.id, {"reason": "Выгрузка больше не нужна"}, req(report), session, hr
    )
    assert report.status == "CANCELLED"


def test_operation_cancel_preserves_processing_job_and_cancels_queued(session, hr):
    op = save_entity(
        session,
        "operation",
        {"type": "REPORT_EXPORT", "export_scope": {"all": True}},
        status="RUNNING",
        owner_id=hr.id,
    )
    queued = Job(
        id="cancel-queued",
        kind="DOMAIN_REPORT_EXPORT",
        payload={"operation_id": op.id},
        unique_key="cancel-queued",
        status="PENDING",
        run_at=now(),
    )
    running = Job(
        id="cancel-running",
        kind="DOMAIN_REPORT_EXPORT",
        payload={"operation_id": op.id, "_lease_token": "current"},
        unique_key="cancel-running",
        status="PROCESSING",
        run_at=now(),
    )
    session.add_all([queued, running])
    session.commit()
    d.operation_cancel(
        op.id,
        {"reason": "Cancel without overwriting current worker"},
        req(op),
        session,
        hr,
    )
    assert op.status == "CANCELLED" and op.data["cancel_requested"]
    assert queued.status == "CANCELLED" and running.status == "PROCESSING"
    assert running.payload["_lease_token"] == "current"


def test_queued_ingest_cancel_marks_uploaded_revision_retryable(session, hr):
    document = save_entity(
        session, "document", {"scope": {"site_ids": ["GrPP"]}}, status="DRAFT"
    )
    revision = save_entity(
        session,
        "document_revision",
        {"source_path": "untouched-original.pdf"},
        status="UPLOADED",
        parent_id=document.id,
    )
    operation = save_entity(
        session,
        "operation",
        {"type": "KNOWLEDGE_INGEST", "revision_id": revision.id},
        status="PENDING",
        owner_id=hr.id,
    )
    queued = Job(
        id="queued-ingest-cancel",
        kind="KNOWLEDGE_INGEST",
        payload={"operation_id": operation.id, "revision_id": revision.id},
        unique_key="queued-ingest-cancel",
        status="PENDING",
        run_at=now(),
    )
    session.add(queued)
    session.commit()
    d.operation_cancel(
        operation.id, {"reason": "Отмена до начала OCR"}, req(operation), session, hr
    )
    assert operation.status == "CANCELLED" and queued.status == "CANCELLED"
    assert (
        revision.status == "CANCELLED"
        and revision.data["source_path"] == "untouched-original.pdf"
    )


def test_prepared_exports_recheck_current_dataset_privileges(
    session, hr, tmp_path, monkeypatch
):
    from types import SimpleNamespace
    import korsik.settings as settings

    monkeypatch.setattr(
        settings, "get_settings", lambda: SimpleNamespace(blob_root=str(tmp_path))
    )
    employee = profile(session, hr, manager_id=None)
    response = d.create_report_export(
        {"dataset": "SURVEYS", "format": "CSV", "reason": "HR report"}, session, hr
    )
    op = session.get(Entity, json.loads(response.body)["id"])
    job = session.scalar(
        select(Job).where(Job.payload["operation_id"].as_string() == op.id)
    )
    d.process_domain_job(session, job)
    session.commit()
    assert op.status == "SUCCEEDED"
    assert len(d.list_report_exports(session, hr)["items"]) == 1
    for role in ("MANAGER", "PLATFORM_ADMIN"):
        changed = Actor(hr.id, hr.name, role, {"all": True})
        with pytest.raises(HTTPException) as denied:
            d.report_export_content(op.id, session, changed)
        assert denied.value.status_code in (403, 404)
        if role != "PLATFORM_ADMIN":
            assert d.list_report_exports(session, changed)["items"] == []
    scoped = Actor(hr.id, hr.name, "HR_ADMIN", {"site_ids": [employee.data["site_id"]]})
    with pytest.raises(HTTPException):
        d.validate_export_request(
            {"dataset": "AUDIT", "format": "CSV", "reason": "Scoped audit"}, scoped
        )
    assert (
        d.validate_export_request(
            {"dataset": "AUDIT", "format": "CSV", "reason": "Global audit"}, hr
        )["dataset"]
        == "AUDIT"
    )


def test_export_pins_materialized_cohort_and_head_rechecks_scope(
    session, hr, tmp_path, monkeypatch
):
    from types import SimpleNamespace
    import korsik.settings as settings

    monkeypatch.setattr(
        settings, "get_settings", lambda: SimpleNamespace(blob_root=str(tmp_path))
    )
    first = profile(session, hr)
    response = d.create_report_export(
        {"dataset": "ADAPTATION", "format": "CSV", "reason": "Prepare current cohort"},
        session,
        hr,
    )
    op = session.get(Entity, json.loads(response.body)["id"])
    second = profile(session, hr, name="Added after request")
    job = session.scalar(
        select(Job).where(Job.payload["operation_id"].as_string() == op.id)
    )
    d.process_domain_job(session, job)
    session.commit()
    assert set(op.data["employee_ids"]) == {first.id, second.id}
    head = Actor(hr.id, hr.name, "HEAD_TALENT", {"all": True})
    head.scope = {"site_ids": ["foreign"]}
    with pytest.raises(HTTPException):
        d.report_export_content(op.id, session, head)
    head.scope = {"all": True}
    assert d.report_export_content(op.id, session, head).media_type.startswith(
        "text/csv"
    )


def test_head_reads_manager_task_summaries_only_in_hr_scope(session, hr):
    employee = profile(session, hr)
    d.create_manager_task(
        {"employee_id": employee.id, "summary": "Private HR task summary"}, session, hr
    )
    for grant in ({"all": True}, {"all": True, "employee_drilldown": True}):
        assert len(d.manager_tasks(session, Actor("head", "Head", "HEAD_TALENT", grant))["items"]) == 1
    assert d.manager_tasks(session, Actor("head", "Head", "HEAD_TALENT", {"site_ids": ["foreign"]}))["items"] == []
    assert len(d.manager_tasks(session, hr)["items"]) == 1


def test_employee_new_episode_cannot_read_former_private_domain_records(session, hr):
    employee = profile(session, hr)
    old_episode = employee.data["episode_id"]
    old_journey = d.entities(session, "journey", employee.id)[0]
    old_survey = run_for(session, employee)
    old_case = d.create_case_entity(session, employee.id, "TEST", "Former private case")
    new_episode = save_entity(
        session,
        "employment_episode",
        {"employee_id": employee.id, "stage": "ACTIVE"},
        parent_id=employee.id,
    )
    d.update(employee, episode_id=new_episode.id, employment_stage="ACTIVE")
    actor = employee_actor(employee)
    session.commit()
    assert old_episode != new_episode.id
    assert d.journeys(session, actor)["items"] == []
    assert d.employee_timeline(employee.id, session, actor)["items"] == []
    assert [
        e["id"] for e in d.employee_episodes(employee.id, session, actor)["items"]
    ] == [new_episode.id]
    assert d.employee_surveys(employee.id, session, actor)["items"] == []
    assert d.list_cases(session, actor)["items"] == []
    for command in (
        lambda: d.survey_get(old_survey.id, session, actor),
        lambda: d.journey_get(old_journey.id, session, actor),
        lambda: d.get_case(old_case.id, session, actor),
        lambda: d.case_events(old_case.id, session, actor),
    ):
        with pytest.raises(HTTPException) as denied:
            command()
        assert denied.value.status_code == 404
    assert len(d.employee_surveys(employee.id, session, hr)["items"]) == 1
    assert (
        json.loads(d.get_case(old_case.id, session, hr).body)["description"]
        == "Former private case"
    )


def test_journey_migration_requires_published_frozen_preview_and_cancels_removed_future_steps(
    session, hr
):
    employee = profile(session, hr)
    journey = d.entities(session, "journey", employee.id)[0]
    version = session.get(Entity, journey.data["version_id"])
    graph = copy.deepcopy(version.data["graph"])
    actions = d.entities(session, "scheduled_action", journey.id)
    removed = actions[-1]
    graph["steps"] = [
        step for step in graph["steps"] if step["step_key"] != removed.data["step_key"]
    ]
    draft = save_entity(
        session,
        "scenario_version",
        {"graph": graph},
        status="DRAFT",
        parent_id=version.parent_id,
    )
    with pytest.raises(HTTPException) as denied:
        d.journey_plan(journey.id, {"version_id": draft.id}, session, hr)
    assert denied.value.status_code == 409
    draft.status = "PUBLISHED"
    session.commit()
    preview = json.loads(
        d.journey_plan(journey.id, {"version_id": draft.id}, session, hr).body
    )
    with pytest.raises(HTTPException):
        d.journey_plan_apply(
            journey.id,
            {
                "plan_id": preview["id"],
                "preview_hash": "stale",
                "reason": "Version migration",
            },
            session,
            hr,
        )
    result = d.journey_plan_apply(
        journey.id,
        {
            "plan_id": preview["id"],
            "preview_hash": preview["preview_hash"],
            "reason": "Version migration",
        },
        session,
        hr,
    )
    assert json.loads(result.body)["version_id"] == draft.id
    assert removed.status == "CANCELLED"
    assert session.get(Entity, preview["id"]).status == "APPLIED"
    journey.status = "COMPLETED"
    with pytest.raises(HTTPException):
        d.journey_signal(journey.id, {"signal_code": "HR_START_APPROVED"}, session, hr)


def test_scoped_user_roster_conjoins_legal_and_site_and_redacts_specialist_email(
    session, hr
):
    from korsik.auth import users

    same = User(
        id="scope-same",
        email="same@example.invalid",
        name="Same grant",
        role="HR_SPECIALIST",
        scope={"site_ids": ["GrPP"], "legal_entity_ids": ["legal-a"]},
        password_hash="no-login",
    )
    other = User(
        id="scope-other",
        email="private@example.invalid",
        name="Other legal entity",
        role="HR_SPECIALIST",
        scope={"site_ids": ["GrPP"], "legal_entity_ids": ["legal-b"]},
        password_hash="no-login",
    )
    technical = User(
        id="scope-platform",
        email="operator@example.invalid",
        name="Technical operator",
        role="PLATFORM_ADMIN",
        scope={"all": True},
        password_hash="no-login",
    )
    session.add_all([same, other, technical])
    session.commit()
    for role in ("HR_ADMIN", "HR_SPECIALIST"):
        scoped = Actor(
            "scoped-reader",
            "Scoped reader",
            role,
            {"site_ids": ["GrPP"], "legal_entity_ids": ["legal-a"]},
        )
        result = users(scoped, session)["items"]
        assert {u["id"] for u in result} == {same.id}
        if role == "HR_SPECIALIST":
            assert "email" not in result[0]
    platform = Actor(technical.id, technical.name, technical.role, technical.scope)
    assert {u["id"] for u in users(platform, session)["items"]} == {technical.id}


def test_platform_audit_metadata_does_not_expose_business_reason(session, hr):
    from korsik.auth import audit_events
    from korsik.common import audit

    audit(
        session,
        hr,
        "knowledge.revision.reviewed",
        "opaque-id",
        {"reason": "Private quotation of corporate source", "action": "APPROVE"},
    )
    session.commit()
    platform = Actor("technical", "Technical", "PLATFORM_ADMIN", {"all": True})
    assert all(row["data"] == {} for row in audit_events(platform, session)["items"])
    record = audit_events(hr, session)["items"][0]
    assert record["reason"].startswith("Private")
    assert "reason" not in record["data"] and record["data"]["has_reason"] is True
    private = session.get(Entity, record["data"]["reason_ref"])
    assert private.kind == "audit_reason" and private.parent_id == record["id"]
    assert (
        private.data["entity_id"] == "opaque-id" and private.data["actor_id"] == hr.id
    )
    assert all(
        row["reason"] is None for row in audit_events(platform, session)["items"]
    )
    for denied in (
        Actor("head", "Head", "HEAD_TALENT", {"all": True}),
        Actor("auditor", "Audit", "AUDITOR", {"all": True}),
        Actor("local", "HR", "HR_ADMIN", {"site_ids": ["GrPP"]}),
    ):
        with pytest.raises(HTTPException):
            audit_events(denied, session)


def test_rehire_handoff_uses_current_conversation_and_old_capture_is_private(
    session, hr
):
    from korsik import chat

    employee = profile(session, hr)
    actor = employee_actor(employee)
    conv = chat.conversation_for(session, employee)
    old_case = chat.handoff(session, actor, conv, "Former employment handoff")
    old_case_id = old_case["id"]
    old_run = run_for(session, employee)
    d.submit_survey_answer(
        session, actor, old_run.id, {"question_id": "D14-Q1", "value": "no"}
    )
    d.update(conv, mode="BOT")
    cap = d.open_capture(
        session,
        actor,
        {
            "survey_run_id": old_run.id,
            "question_id": "D14-Q1",
            "capture_kind": "COMMENT",
        },
    )
    d.capture_draft(session, actor, cap.id, {"text": "Former confidential draft"})
    new_episode = save_entity(
        session,
        "employment_episode",
        {"employee_id": employee.id, "stage": "ACTIVE"},
        parent_id=employee.id,
    )
    d.update(employee, episode_id=new_episode.id, employment_stage="ACTIVE")
    conv.status = "CLOSED"
    current = chat.conversation_for(session, employee)
    result = chat.handoff(session, actor, current, "Current employment handoff")
    session.commit()
    assert result["id"] != old_case_id
    assert (
        result["episode_id"] == new_episode.id
        and result["conversation_id"] == current.id
    )
    for operation in (
        lambda: d.get_input_capture(cap.id, session, actor),
        lambda: d.capture_draft(session, actor, cap.id, {"text": "overwrite"}),
        lambda: d.confirm_capture(
            session, actor, cap.id, {"draft_fingerprint": cap.data["draft_fingerprint"]}
        ),
    ):
        with pytest.raises(HTTPException) as denied:
            operation()
        assert denied.value.status_code == 404
    with pytest.raises(HTTPException) as denied:
        d.claim_case(old_case_id, session, hr)
    assert denied.value.status_code == 409
    assert session.get(Entity, old_case_id).status == "OPEN"


def test_local_ai_mutations_require_deployment_wide_administration(session, hr):
    from korsik import local_ai

    scoped = Actor("local-hr", "Local HR", "HR_ADMIN", {"site_ids": ["GrPP"]})
    dummy_request = Request({"type": "http", "headers": []})
    for command in (
        lambda: local_ai.create_profile({}, session, scoped),
        lambda: local_ai.test_profile("missing", {}, dummy_request, session, scoped),
        lambda: local_ai.activate_profile(
            "missing", {}, dummy_request, session, scoped
        ),
    ):
        with pytest.raises(HTTPException) as denied:
            command()
        assert denied.value.status_code == 403
    local_ai.require_global_operator(hr)
    local_ai.require_global_operator(
        Actor("technical", "Technical", "PLATFORM_ADMIN", {})
    )


def test_export_list_preserves_safe_format_metadata_without_private_request(
    session, hr
):
    employee = profile(session, hr)
    response = d.create_report_export(
        {
            "dataset": "ADAPTATION",
            "format": "CSV",
            "filters": {"site_ids": [employee.data["site_id"]]},
            "reason": "Private report reason",
        },
        session,
        hr,
    )
    value = d.list_report_exports(session, hr)["items"][0]
    assert value["id"] == json.loads(response.body)["id"]
    assert value["dataset"] == "ADAPTATION" and value["format"] == "CSV"
    assert (
        "export_request" not in value
        and "artifact_path" not in value
        and "employee_ids" not in value
    )
    assert "Private report reason" not in json.dumps(value)


def test_episode_views_hide_hr_reasons_and_former_episode_from_new_employee(
    session, hr
):
    employee = profile(session, hr)
    old_id = employee.data["episode_id"]
    old = session.get(Entity, old_id)
    d.update(old, reason="Private hiring review", end_reason="Private HR end reason")
    manager = Actor("privacy-manager", "Manager", "MANAGER", {})
    d.update(employee, manager_id=manager.id)
    manager_value = json.loads(
        d.employment_episode_detail(old_id, session, manager).body
    )
    assert "reason" not in manager_value and "end_reason" not in manager_value
    assert (
        "reason" not in d.employee_episodes(employee.id, session, manager)["items"][0]
    )
    assert (
        json.loads(d.employment_episode_detail(old_id, session, hr).body)["end_reason"]
        == "Private HR end reason"
    )
    new = save_entity(
        session,
        "employment_episode",
        {"employee_id": employee.id, "stage": "ACTIVE"},
        parent_id=employee.id,
    )
    d.update(employee, episode_id=new.id, employment_stage="ACTIVE")
    with pytest.raises(HTTPException) as denied:
        d.employment_episode_detail(old_id, session, employee_actor(employee))
    assert denied.value.status_code == 404


def test_scoped_hr_cannot_reset_technical_operator_with_matching_site_labels(
    session, hr
):
    from korsik import auth_admin

    scope = {"site_ids": ["GrPP"], "legal_entity_ids": ["same-legal"]}
    operator = User(
        id="local-labelled-operator",
        name="Operator",
        email="operator-local@invalid.test",
        role="PLATFORM_ADMIN",
        scope=scope,
        password_hash="no-login",
        enabled=True,
    )
    session.add(operator)
    session.commit()
    scoped = Actor("scoped-hr", "Scoped HR", "HR_ADMIN", scope)
    assert not auth_admin.accessible(session, scoped, operator)
    with pytest.raises(HTTPException) as denied:
        auth_admin.issue_reset(
            operator.id,
            {"reason": "Cannot inherit global technical authority"},
            session,
            scoped,
        )
    assert denied.value.status_code == 404
    assert auth_admin.accessible(session, hr, operator)
