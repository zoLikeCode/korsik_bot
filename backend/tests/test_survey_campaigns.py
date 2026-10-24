import json
from datetime import datetime, timedelta, timezone
import pytest
from pydantic import ValidationError
from sqlalchemy import select
from korsik import domain as d, survey_campaigns as sc
from korsik.auth import Actor
from korsik.models import Entity, Job, Outbox, User
from test_domain import session as session, hr as hr, profile, employee_actor, req


def payload():
    return dict(
        title="Самочувствие и поддержка",
        description="Чтобы улучшить адаптацию",
        purpose="WELLBEING",
        questions=[
            dict(
                question_key="support",
                prompt="Насколько вам доступна поддержка коллег?",
                type="LIKERT",
            ),
            dict(question_key="help", prompt="Что могло бы помочь?", type="TEXT"),
        ],
        reason="Подготовка добровольного опроса",
    )


def published(db, hr):
    response = sc.create_template(sc.TemplateCreate(**payload()), db, hr)
    template = db.get(Entity, json.loads(response.body)["id"])
    version = d.entities(db, "survey_template_version", template.id)[0]
    sc.submit_review(
        version.id, sc.Reason(reason="Проверка формулировок"), req(version), db, hr
    )
    sc.review_version(
        version.id,
        sc.Review(reason="Проверено добровольное участие", decision="APPROVE"),
        req(version),
        db,
        hr,
    )
    sc.publish_version(
        version.id,
        sc.Reason(reason="Разрешаю добровольный опрос"),
        req(version),
        db,
        hr,
    )
    return template, version


def campaign(db, hr, version, people, start=None):
    start = start or sc.now() + timedelta(minutes=1)
    body = sc.CampaignInput(
        name="Рабочее самочувствие",
        version_id=version.id,
        audience={"employee_ids": [p.id for p in people]},
        schedule={
            "starts_at": start,
            "ends_at": start + timedelta(days=7),
            "timezone": "Europe/Moscow",
        },
        reason="Обратная связь по адаптации",
    )
    preview = sc.preview_campaign(body, db, hr)
    body.preview_hash = preview["preview_hash"]
    r = sc.create_campaign(body, db, hr)
    return db.get(Entity, json.loads(r.body)["id"]), body


def execute(db, job):
    job.status = "PROCESSING"
    job.payload = {**job.payload, "_lease_token": "trusted-claim"}
    job.lease_until = sc.now() + timedelta(minutes=1)
    db.flush()
    assert sc.process_job(db, job)
    job.status = "COMPLETED"
    db.flush()


def open_campaign(db, hr, version, people, clock, monkeypatch):
    c, body = campaign(db, hr, version, people, start=clock[0] + timedelta(minutes=1))
    monkeypatch.setattr(sc, "now", lambda: clock[0])
    monkeypatch.setattr(d, "now", lambda: clock[0])
    assert sc.tick(db) == 0
    clock[0] += timedelta(minutes=1)
    sc.tick(db)
    sc.tick(db)
    jobs = list(db.scalars(select(Job).where(Job.kind == sc.DELIVER)))
    assert len(jobs) == len(people)
    for job in jobs:
        execute(db, job)
    return c, d.entities(db, "survey_run")


def test_version_review_publish_immutable_and_typed_validation(session, hr):
    _, v = published(session, hr)
    with pytest.raises(Exception) as e:
        sc.edit_version(v.id, sc.Draft(**payload()), req(v), session, hr)
    assert e.value.status_code == 409
    with pytest.raises(ValidationError):
        sc.Question(
            question_key="q",
            prompt="Выбор",
            type="SINGLE_CHOICE",
            options=[{"value": "a", "label": "Да"}],
        )
    with pytest.raises(ValidationError):
        sc.Draft(**{**payload(), "diagnosis": True})
    with pytest.raises(ValidationError):
        sc.Schedule(
            starts_at="2026-10-25T09:00:00",
            ends_at="2026-10-26T09:00:00",
            timezone="Europe/Moscow",
        )


def test_due_timezone_answers_finish_no_general_analysis_and_no_duplicate(
    session, hr, monkeypatch
):
    clock = [datetime(2026, 10, 24, 9, tzinfo=timezone.utc)]
    monkeypatch.setattr(sc, "now", lambda: clock[0])
    e = profile(session, hr)
    _, v = published(session, hr)
    c, runs = open_campaign(session, hr, v, [e], clock, monkeypatch)
    assert c.status == "ACTIVE" and len(runs) == 1
    run = runs[0]
    assert run.parent_id != d.entities(session, "journey", e.id)[0].id
    assert "не анонимный" in run.data["definition"]["questions"][0]["prompt"]["text_vy"]
    opening = next(
        o
        for o in session.scalars(select(Outbox))
        if o.data.get("survey_run_id") == run.id
    )
    opening.data = {**opening.data, "accepted_at": clock[0].isoformat()}
    d.on_outbox_accepted(session, opening)
    d.on_outbox_accepted(session, opening)
    reminders = list(session.scalars(select(Job).where(Job.kind == sc.REMIND)))
    assert len(reminders) == 2
    assert not list(
        session.scalars(select(Job).where(Job.kind == "DOMAIN_SURVEY_REMINDER"))
    )
    actor = employee_actor(e)
    d.submit_survey_answer(
        session,
        actor,
        run.id,
        {"question_id": "CUSTOM-support", "value": 4, "state_revision": 1},
    )
    d.submit_survey_answer(
        session,
        actor,
        run.id,
        {
            "question_id": "CUSTOM-help",
            "value": "Помогло бы знакомство с наставником",
            "state_revision": 2,
        },
    )
    d.finish_survey(session, actor, run.id)
    assert run.status == "COMPLETED" and len(run.data["answers"]) == 2
    assert not d.entities(session, "feedback")
    assert not list(session.scalars(select(Job).where(Job.kind == "FEEDBACK_ANALYSIS")))
    assert "CUSTOM-support" not in d.entities(session, "journey", e.id)[0].data.get(
        "answers", {}
    )
    sc.tick(session)
    assert len(d.entities(session, "survey_run")) == 1
    clock[0] += timedelta(days=1)
    for job in reminders:
        execute(session, job)
    assert not session.scalar(
        select(Outbox).where(Outbox.unique_key.like("custom-reminder-send:%"))
    )


def test_cancel_and_replan_do_not_replace_answers_or_send_stale_generation(
    session, hr, monkeypatch
):
    clock = [datetime(2026, 10, 24, 9, tzinfo=timezone.utc)]
    monkeypatch.setattr(sc, "now", lambda: clock[0])
    monkeypatch.setattr(d, "now", lambda: clock[0])
    e = profile(session, hr)
    _, v = published(session, hr)
    c, body = campaign(session, hr, v, [e])
    old_generation = c.data["generation"]
    body.schedule.starts_at += timedelta(hours=2)
    body.schedule.ends_at += timedelta(hours=2)
    body.preview_hash = sc.preview_campaign(body, session, hr)["preview_hash"]
    sc.replan_campaign(c.id, body, req(c), session, hr)
    assert c.data["generation"] == old_generation + 1
    old_row = next(
        r
        for r in d.entities(session, "survey_campaign_enrollment", c.id)
        if r.data["generation"] == old_generation
    )
    job = d.enqueue(
        session,
        sc.DELIVER,
        {"enrollment_id": old_row.id, "generation": old_generation},
        "stale-test",
    )
    execute(session, job)
    assert not d.entities(session, "survey_run")
    clock[0] = body.schedule.starts_at
    sc.tick(session)
    for job in session.scalars(
        select(Job).where(Job.kind == sc.DELIVER, Job.status == "PENDING")
    ):
        execute(session, job)
    run = d.entities(session, "survey_run")[0]
    d.submit_survey_answer(
        session,
        employee_actor(e),
        run.id,
        {"question_id": "CUSTOM-support", "value": 3},
    )
    before = run.data["answers"].copy()
    with pytest.raises(Exception) as error:
        sc.replan_campaign(c.id, body, req(c), session, hr)
    assert error.value.status_code == 409
    sc.cancel_campaign(
        c.id, sc.Reason(reason="Закрываем текущий сбор ответов"), req(c), session, hr
    )
    assert run.status == "CANCELLED" and run.data["answers"] == before
    assert sc.delivery_allowed(session, run)[0] is False


def test_scope_creator_revocation_episode_and_manager_privacy(session, hr, monkeypatch):
    clock = [datetime(2026, 10, 24, 9, tzinfo=timezone.utc)]
    monkeypatch.setattr(sc, "now", lambda: clock[0])
    monkeypatch.setattr(d, "now", lambda: clock[0])
    session.add(
        User(
            id="manager-a",
            email="manager@local",
            password_hash="not-login",
            name="Manager",
            role="MANAGER",
            scope={},
            enabled=True,
        )
    )
    session.flush()
    e = profile(session, hr, manager_id="manager-a")
    _, v = published(session, hr)
    c, runs = open_campaign(session, hr, v, [e], clock, monkeypatch)
    run = runs[0]
    manager = Actor("manager-a", "Manager", "MANAGER", {})
    assert sc.analytics(c.id, session, manager)["suppressed"]
    with pytest.raises(Exception) as error:
        sc.campaign_responses(c.id, session, manager)
    assert error.value.status_code == 403
    platform = Actor("platform", "Operator", "PLATFORM_ADMIN", {"all": True})
    with pytest.raises(Exception) as error:
        sc.templates(session, platform)
    assert error.value.status_code == 403
    user = session.get(User, hr.id)
    user.scope = {"site_ids": ["OTHER"]}
    assert sc.delivery_allowed(session, run) == (False, "CAMPAIGN_ACCESS_REVOKED")
    user.scope = {"all": True}
    e.data = {**e.data, "episode_id": "new-episode"}
    assert sc.delivery_allowed(session, run)[0] is False


def test_schedule_uses_offset_and_reminders_begin_after_receipt(
    session, hr, monkeypatch
):
    schedule = sc.Schedule(
        starts_at="2026-10-24T12:30:00+03:00",
        ends_at="2026-10-27T12:30:00+03:00",
        timezone="Europe/Moscow",
        reminder_hours=[2],
    )
    assert schedule.starts_at.isoformat() == "2026-10-24T09:30:00+00:00"
    with pytest.raises(ValidationError):
        sc.Schedule(**{**schedule.model_dump(), "timezone": "No/SuchZone"})
    clock = [datetime(2026, 10, 24, 9, tzinfo=timezone.utc)]
    monkeypatch.setattr(sc, "now", lambda: clock[0])
    monkeypatch.setattr(d, "now", lambda: clock[0])
    e = profile(session, hr)
    _, v = published(session, hr)
    c, runs = open_campaign(session, hr, v, [e], clock, monkeypatch)
    assert not session.scalar(select(Job).where(Job.kind == sc.REMIND))
    receipt = clock[0] + timedelta(hours=3)
    sc.on_opening_accepted(session, runs[0], receipt)
    jobs = list(session.scalars(select(Job).where(Job.kind == sc.REMIND)))
    assert min(d.aware(j.run_at) for j in jobs) == receipt + timedelta(hours=24)
    expired_job = d.enqueue(
        session, sc.REMIND, {"survey_run_id": runs[0].id, "number": 3}, "expired-lease"
    )
    expired_job.status = "PROCESSING"
    expired_job.payload = {**expired_job.payload, "_lease_token": "old"}
    expired_job.lease_until = clock[0] - timedelta(seconds=1)
    assert sc.process_job(session, expired_job)
    assert not session.scalar(
        select(Outbox).where(Outbox.unique_key.like("custom-reminder-send:%"))
    )


def test_reminder_real_delivery_optout_and_expiry_suppress(session, hr, monkeypatch):
    clock = [datetime(2026, 10, 24, 9, tzinfo=timezone.utc)]
    monkeypatch.setattr(sc, "now", lambda: clock[0])
    monkeypatch.setattr(d, "now", lambda: clock[0])
    e = profile(session, hr)
    _, v = published(session, hr)
    c, runs = open_campaign(session, hr, v, [e], clock, monkeypatch)
    run = runs[0]
    sc.on_opening_accepted(session, run, clock[0])
    jobs = list(
        session.scalars(select(Job).where(Job.kind == sc.REMIND).order_by(Job.run_at))
    )
    clock[0] += timedelta(hours=24)
    execute(session, jobs[0])
    execute(session, jobs[0])
    out = list(
        session.scalars(
            select(Outbox).where(Outbox.unique_key.like("custom-reminder-send:%"))
        )
    )
    assert len(out) == 1
    assert out[0].data["survey_run_id"] == run.id
    assert {b["payload"]["action"] for b in out[0].callback_buttons} == {
        "SURVEY_RESUME",
        "SURVEY_DECLINE",
    }
    from korsik.chat import conversation_for

    d.handle_domain_callback(
        session,
        conversation_for(session, e),
        {"action": "SURVEY_DECLINE", "survey_run_id": run.id},
    )
    execute(session, jobs[1])
    assert run.status == "DECLINED"
    assert (
        len(
            list(
                session.scalars(
                    select(Outbox).where(
                        Outbox.unique_key.like("custom-reminder-send:%")
                    )
                )
            )
        )
        == 1
    )
    clock[0] += timedelta(days=8)
    sc.tick(session)
    assert c.status == "COMPLETED"


def test_scoped_template_and_campaign_preview_cannot_expand_grant(session, hr):
    e = profile(session, hr)
    user = User(
        id="local-hr",
        email="local-hr@test",
        password_hash="no-login",
        name="Local HR",
        role="HR_ADMIN",
        scope={"site_ids": ["GrPP"]},
        enabled=True,
    )
    session.add(user)
    session.flush()
    actor = Actor(user.id, user.name, user.role, user.scope)
    t, v = published(session, actor)
    assert t.data["scope"]["site_ids"] == ["GrPP"]
    foreign = Actor("other", "Other", "HR_ADMIN", {"site_ids": ["GTP"]})
    assert not sc.templates(session, foreign)["items"]
    with pytest.raises(Exception) as error:
        sc.version_get(v.id, session, foreign)
    assert error.value.status_code == 404
    with pytest.raises(Exception) as error:
        sc.create_template(
            sc.TemplateCreate(**payload(), scope={"all": True}), session, actor
        )
    assert error.value.status_code == 403
    c, body = campaign(session, actor, v, [e])
    body.audience.site_ids = ["GTP"]
    with pytest.raises(Exception) as error:
        sc.preview_campaign(body, session, actor)
    assert error.value.status_code == 403
    body.audience.site_ids = []
    body.name = "Название изменилось после просмотра"
    with pytest.raises(Exception) as error:
        sc.create_campaign(body, session, actor)
    assert error.value.status_code == 409
    # Published content stays immutable across a separately edited new version.
    before = v.data.copy()
    sc.new_version(
        t.id, sc.Reason(reason="Готовим следующую версию"), req(t), session, actor
    )
    draft = next(
        row
        for row in d.entities(session, "survey_template_version", t.id)
        if row.status == "DRAFT"
    )
    sc.edit_version(
        draft.id,
        sc.Draft(**{**payload(), "title": "Следующий опрос"}),
        req(draft),
        session,
        actor,
    )
    assert v.data == before and c.data["version_id"] == v.id


def test_six_person_analytics_suppress_small_cells_and_never_expose_texts(
    session, hr, monkeypatch
):
    clock = [datetime(2026, 10, 24, 9, tzinfo=timezone.utc)]
    monkeypatch.setattr(sc, "now", lambda: clock[0])
    monkeypatch.setattr(d, "now", lambda: clock[0])
    session.add(
        User(
            id="manager-six",
            email="manager6@test",
            password_hash="no-login",
            name="Manager",
            role="MANAGER",
            scope={},
            enabled=True,
        )
    )
    session.flush()
    people = [
        profile(session, hr, name=f"Synthetic {i}", manager_id="manager-six")
        for i in range(6)
    ]
    _, v = published(session, hr)
    c, runs = open_campaign(session, hr, v, people, clock, monkeypatch)
    for index, run in enumerate(runs):
        e = session.get(Entity, run.data["employee_id"])
        d.submit_survey_answer(
            session,
            employee_actor(e),
            run.id,
            {"question_id": "CUSTOM-support", "value": 4},
        )
        if index < 5:
            d.submit_survey_answer(
                session,
                employee_actor(e),
                run.id,
                {"question_id": "CUSTOM-help", "value": "Private wellbeing response"},
            )
            d.finish_survey(session, employee_actor(e), run.id)
    manager = Actor("manager-six", "Manager", "MANAGER", {})
    report = sc.analytics(c.id, session, manager)
    assert (
        report["eligible"] == 6 and report["completed"] is None
    )  # lone noncompletion hidden
    assert report["questions"][0]["mean"] == 4
    assert report["individual_scores"] is False
    assert "Private wellbeing" not in json.dumps(report)
    assert "employee_id" not in json.dumps(report)
    raw = sc.campaign_responses(c.id, session, hr)
    assert len(raw["items"]) == 6
    assert "Private wellbeing" in json.dumps(raw)
    assert (
        raw["items"][0]["answers"]["CUSTOM-support"]["question_prompt"]
        == "Насколько вам доступна поддержка коллег?"
    )


def test_single_and_multiple_questions_use_existing_typed_answer_validation(
    session, hr, monkeypatch
):
    clock = [datetime(2026, 10, 24, 9, tzinfo=timezone.utc)]
    monkeypatch.setattr(sc, "now", lambda: clock[0])
    monkeypatch.setattr(d, "now", lambda: clock[0])
    p = payload()
    p["questions"] = [
        dict(
            question_key="single",
            prompt="К кому обращаетесь за помощью?",
            type="SINGLE_CHOICE",
            options=[
                {"value": "mentor", "label": "Наставник"},
                {"value": "team", "label": "Коллеги"},
            ],
        ),
        dict(
            question_key="multi",
            prompt="Что было полезным?",
            type="MULTI_CHOICE",
            options=[
                {"value": "tour", "label": "Экскурсия"},
                {"value": "meeting", "label": "Встреча"},
            ],
            max_selected=2,
        ),
    ]
    t = sc.create_template(sc.TemplateCreate(**p), session, hr)
    v = d.entities(session, "survey_template_version", json.loads(t.body)["id"])[0]
    sc.submit_review(v.id, sc.Reason(reason="Проверяем варианты"), req(v), session, hr)
    sc.review_version(
        v.id,
        sc.Review(reason="Варианты проверены", decision="APPROVE"),
        req(v),
        session,
        hr,
    )
    sc.publish_version(
        v.id, sc.Reason(reason="Публикуем рабочий опрос"), req(v), session, hr
    )
    e = profile(session, hr)
    _, runs = open_campaign(session, hr, v, [e], clock, monkeypatch)
    run = runs[0]
    actor = employee_actor(e)
    with pytest.raises(Exception) as error:
        d.submit_survey_answer(
            session,
            actor,
            run.id,
            {"question_id": "CUSTOM-single", "value": "invented"},
        )
    assert error.value.status_code == 422
    d.submit_survey_answer(
        session, actor, run.id, {"question_id": "CUSTOM-single", "value": "mentor"}
    )
    d.submit_survey_answer(
        session,
        actor,
        run.id,
        {"question_id": "CUSTOM-multi", "value": ["tour", "meeting"]},
    )
    d.finish_survey(session, actor, run.id)
    assert run.status == "COMPLETED"
    assert (
        sc._present_answers(run)["CUSTOM-multi"]["display_value"]
        == "Экскурсия, Встреча"
    )


def test_http_models_forbid_unknown_fields_and_require_current_etag(session, hr):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from korsik.auth import get_actor
    from korsik.db import get_db
    from korsik.common import etag

    app = FastAPI()
    app.include_router(sc.router, prefix="/api/v1")
    app.dependency_overrides[get_actor] = lambda: hr
    app.dependency_overrides[get_db] = lambda: session
    client = TestClient(app)
    response = client.post("/api/v1/survey-templates", json=payload())
    assert response.status_code == 201
    template_id = response.json()["id"]
    version_id = client.get(f"/api/v1/survey-templates/{template_id}").json()[
        "versions"
    ][0]["id"]
    path = f"/api/v1/survey-template-versions/{version_id}"
    version = session.get(Entity, version_id)
    old_tag = etag(version)
    assert client.patch(path, json=payload()).status_code == 428
    invalid = client.patch(
        path, json={**payload(), "anonymous": True}, headers={"If-Match": old_tag}
    )
    assert invalid.status_code == 422
    valid = client.patch(
        path,
        json={**payload(), "title": "Новая редакция вопросника"},
        headers={"If-Match": old_tag},
    )
    assert valid.status_code == 200 and valid.headers["etag"] != old_tag
    assert (
        client.patch(path, json=payload(), headers={"If-Match": old_tag}).status_code
        == 412
    )
    manager = Actor("manager", "Manager", "MANAGER", {})
    app.dependency_overrides[get_actor] = lambda: manager
    assert client.get("/api/v1/survey-templates").status_code == 403
    assert client.get(path).status_code == 403


def test_campaign_and_source_share_one_lane_with_durable_defer(
    session, hr, monkeypatch
):
    clock = [datetime(2026, 10, 24, 9, tzinfo=timezone.utc)]
    monkeypatch.setattr(sc, "now", lambda: clock[0])
    monkeypatch.setattr(d, "now", lambda: clock[0])
    e = profile(session, hr)
    _, v = published(session, hr)
    journey = d.entities(session, "journey", e.id)[0]
    step = next(
        s
        for s in d.get_entity(
            session, journey.data["version_id"], "scenario_version"
        ).data["graph"]["steps"]
        if s.get("survey", {}).get("survey_key") == "SUR-D3"
    )
    source = d.open_survey(session, journey, step, e)
    c, body = campaign(session, hr, v, [e])
    clock[0] = body.schedule.starts_at
    sc.tick(session)
    job = session.scalar(select(Job).where(Job.kind == sc.DELIVER))
    execute(session, job)
    assert len(d.entities(session, "survey_run")) == 1
    deferred = session.scalar(
        select(Job).where(Job.kind == sc.DELIVER, Job.status == "PENDING")
    )
    assert deferred and deferred.unique_key.startswith("custom-defer:")
    source.status = "DECLINED"
    clock[0] = d.aware(deferred.run_at)
    execute(session, deferred)
    custom = next(
        r for r in d.entities(session, "survey_run") if r.data.get("campaign_id")
    )
    step2 = next(
        s
        for s in d.get_entity(
            session, journey.data["version_id"], "scenario_version"
        ).data["graph"]["steps"]
        if s.get("survey", {}).get("survey_key") == "SUR-D14"
    )
    assert d.open_survey(session, journey, step2, e) is None
    assert (
        custom.status == "IN_PROGRESS" and len(d.entities(session, "survey_run")) == 2
    )


def test_revoked_campaign_creator_blocks_real_answers_and_capture_confirmation(
    session, hr, monkeypatch
):
    from korsik.chat import conversation_for

    clock = [datetime(2026, 10, 24, 9, tzinfo=timezone.utc)]
    monkeypatch.setattr(sc, "now", lambda: clock[0])
    monkeypatch.setattr(d, "now", lambda: clock[0])
    e = profile(session, hr)
    _, v = published(session, hr)
    c, runs = open_campaign(session, hr, v, [e], clock, monkeypatch)
    run = runs[0]
    actor = employee_actor(e)
    d.submit_survey_answer(
        session, actor, run.id, {"question_id": "CUSTOM-support", "value": 4}
    )
    cap = d.open_capture(
        session, actor, {"survey_run_id": run.id, "question_id": "CUSTOM-help"}
    )
    d.capture_draft(session, actor, cap.id, {"text": "Draft private response"})
    user = session.get(User, hr.id)
    user.enabled = False
    with pytest.raises(Exception) as error:
        d.submit_survey_answer(
            session,
            actor,
            run.id,
            {"question_id": "CUSTOM-help", "value": "Not accepted"},
        )
    assert error.value.status_code == 409
    with pytest.raises(Exception) as error:
        d.confirm_capture(
            session, actor, cap.id, {"draft_fingerprint": cap.data["draft_fingerprint"]}
        )
    assert error.value.status_code == 409
    assert "CUSTOM-help" not in run.data["answers"] and cap.status == "DRAFT"
    with pytest.raises(Exception) as error:
        d.handle_domain_callback(
            session,
            conversation_for(session, e),
            {
                "action": "SURVEY_ANSWER",
                "survey_run_id": run.id,
                "question_id": "CUSTOM-help",
                "value": "Not accepted",
            },
        )
    assert error.value.status_code == 409
    assert not d.entities(session, "feedback")


def test_campaign_cancellation_releases_capture_without_erasing_draft_or_answers(
    session, hr, monkeypatch
):
    clock = [datetime(2026, 10, 24, 9, tzinfo=timezone.utc)]
    monkeypatch.setattr(sc, "now", lambda: clock[0])
    monkeypatch.setattr(d, "now", lambda: clock[0])
    e = profile(session, hr)
    _, v = published(session, hr)
    c, runs = open_campaign(session, hr, v, [e], clock, monkeypatch)
    run = runs[0]
    actor = employee_actor(e)
    d.submit_survey_answer(
        session, actor, run.id, {"question_id": "CUSTOM-support", "value": 4}
    )
    cap = d.open_capture(
        session, actor, {"survey_run_id": run.id, "question_id": "CUSTOM-help"}
    )
    d.capture_draft(session, actor, cap.id, {"text": "Saved draft"})
    sc.cancel_campaign(
        c.id, sc.Reason(reason="Закрытие добровольной кампании"), req(c), session, hr
    )
    assert cap.status == "CANCELLED" and cap.data["draft"] == "Saved draft"
    assert run.data["answers"]["CUSTOM-support"]["value"] == 4
    assert run.status == "CANCELLED"
