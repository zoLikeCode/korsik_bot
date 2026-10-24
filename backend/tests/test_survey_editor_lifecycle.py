"""The compact editor approves one exact survey version with an explicit click."""
from copy import deepcopy
import json

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from korsik import domain as d, survey_campaigns as sc
from korsik.auth import Actor
from korsik.models import Audit, Entity, Job, Outbox
from test_domain import session as session, hr as hr, req, profile
from test_survey_campaigns import payload, campaign


def draft(db, actor):
    result = sc.create_template(sc.TemplateCreate(**payload()), db, actor)
    template = db.get(Entity, json.loads(result.body)["id"])
    return template, d.entities(db, "survey_template_version", template.id)[0]


@pytest.mark.parametrize("role", ["HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT"])
def test_explicit_publish_approves_exact_draft_for_scoped_hr(session, hr, role):
    actor = Actor(hr.id, "Responsible HR", role, {"site_ids": ["GrPP"]})
    template, version = draft(session, actor)
    content = deepcopy(version.data)
    before_jobs = len(list(session.scalars(select(Job))))
    before_messages = len(list(session.scalars(select(Outbox))))
    result = sc.publish_version(version.id, sc.Reason(reason="Публикую проверенные мной вопросы"), req(version), session, actor)
    assert result.status_code == 200 and version.status == "PUBLISHED"
    assert template.data["published_version_id"] == version.id
    assert all(version.data[key] == value for key, value in content.items())
    assert version.data["publication_approval"] == {
        "actor_id": actor.id,
        "method": "EXPLICIT_PUBLISH",
        "from_status": "DRAFT",
        "content_fingerprint": sc.fingerprint(content),
    }
    assert "reviewed_by" not in version.data
    event = session.scalar(select(Audit).where(Audit.action == "survey.version.publish", Audit.entity_id == version.id))
    assert event.data["approval_method"] == "EXPLICIT_PUBLISH"
    assert len(list(session.scalars(select(Job)))) == before_jobs
    assert len(list(session.scalars(select(Outbox)))) == before_messages
    assert "добровольное" in sc._definition(version)["questions"][0]["prompt"]["text_vy"]


@pytest.mark.parametrize("role", ["EMPLOYEE", "MANAGER", "PLATFORM_ADMIN"])
def test_non_hr_cannot_publish_survey(session, hr, role):
    _, version = draft(session, hr)
    actor = Actor("other", "Other", role, {"all": True})
    with pytest.raises(HTTPException) as error:
        sc.publish_version(version.id, sc.Reason(reason="Пытаюсь опубликовать"), req(version), session, actor)
    assert error.value.status_code == 403 and version.status == "DRAFT"


def test_publish_rechecks_scope_and_displayed_version_etag(session, hr):
    actor = Actor(hr.id, "HR", "HR_SPECIALIST", {"site_ids": ["GrPP"]})
    template, version = draft(session, actor)
    stale = req(version)
    sc.edit_version(version.id, sc.Draft(**{**payload(), "title": "Новая формулировка опроса"}), req(version), session, actor)
    with pytest.raises(HTTPException) as error:
        sc.publish_version(version.id, sc.Reason(reason="Публикация увиденной версии"), stale, session, actor)
    assert error.value.status_code == 412 and template.data["published_version_id"] is None
    foreign = Actor("foreign", "HR", "HEAD_TALENT", {"site_ids": ["GTP"]})
    with pytest.raises(HTTPException) as error:
        sc.publish_version(version.id, sc.Reason(reason="Публикация"), req(version), session, foreign)
    assert error.value.status_code == 404 and version.status == "DRAFT"


def test_new_draft_copies_selected_old_version_without_replacing_live_snapshot(session, hr):
    template, first = draft(session, hr)
    sc.publish_version(first.id, sc.Reason(reason="Публикация первой версии"), req(first), session, hr)
    employee = profile(session, hr)
    existing, _ = campaign(session, hr, first, [employee])
    first_data = deepcopy(first.data)
    created = sc.new_version(template.id, sc.VersionCreate(reason="Новая версия", base_version_id=first.id), req(template), session, hr)
    second = session.get(Entity, json.loads(created.body)["id"])
    sc.edit_version(second.id, sc.Draft(**{**payload(), "title": "Вторая опубликованная версия"}), req(second), session, hr)
    sc.publish_version(second.id, sc.Reason(reason="Публикация второй версии"), req(second), session, hr)
    third_result = sc.new_version(template.id, sc.VersionCreate(reason="Копия выбранной первой версии", base_version_id=first.id), req(template), session, hr)
    third = session.get(Entity, json.loads(third_result.body)["id"])
    assert third.status == "DRAFT" and third.data["title"] == first_data["title"]
    assert third.data["questions"] == first_data["questions"]
    assert first.data == first_data and first.status == "PUBLISHED"
    assert template.data["published_version_id"] == second.id
    assert existing.data["version_id"] == first.id
    with pytest.raises(HTTPException) as error:
        sc.new_version(template.id, sc.VersionCreate(reason="Повторный черновик", base_version_id=second.id), req(template), session, hr)
    assert error.value.status_code == 409


def test_foreign_template_base_and_repeat_publication_are_rejected(session, hr):
    template, version = draft(session, hr)
    foreign_template, foreign = draft(session, hr)
    sc.publish_version(version.id, sc.Reason(reason="Публикация"), req(version), session, hr)
    with pytest.raises(HTTPException) as error:
        sc.new_version(template.id, sc.VersionCreate(reason="Чужая исходная версия", base_version_id=foreign.id), req(template), session, hr)
    assert error.value.status_code == 404
    with pytest.raises(HTTPException) as error:
        sc.publish_version(version.id, sc.Reason(reason="Повторная публикация"), req(version), session, hr)
    assert error.value.status_code == 409
    assert foreign_template.data["published_version_id"] is None


def test_results_can_be_opened_only_for_selected_accessible_template(session, hr):
    first, v1 = draft(session, hr)
    second, v2 = draft(session, hr)
    for version in [v1, v2]:
        sc.publish_version(version.id, sc.Reason(reason="Публикация"), req(version), session, hr)
    employee = profile(session, hr)
    expected, _ = campaign(session, hr, v1, [employee])
    campaign(session, hr, v2, [employee])
    rows = sc.campaigns(session, hr, template_id=first.id)["items"]
    assert [row["id"] for row in rows] == [expected.id]
    assert all(row["template_id"] != second.id for row in rows)
