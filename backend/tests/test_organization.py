import io
import json
from copy import deepcopy

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from PIL import Image
from starlette.requests import Request

from korsik import domain as d, organization as org
from korsik.auth import Actor
from korsik.common import save_entity, etag
from korsik.models import Entity, User
from test_domain import session as session, hr as hr, profile, req

LEGAL = "00000000-0000-4000-8000-000000000001"


def person(db, hr, name, **changes):
    return profile(db, hr, name=name, **changes)


def unit(db, hr, title="Отдел", **changes):
    response = org.create_unit(
        org.UnitCreate(title=title, legal_entity_id=LEGAL, **changes), db, hr
    )
    return db.get(Entity, json.loads(response.body)["id"])


def assign(db, hr, employee, **changes):
    return org.patch_employee(
        employee.id, org.EmployeePatch(**changes), req(employee), db, hr
    )


def detail(db, actor, employee):
    return json.loads(org.employee_detail(employee.id, db, actor).body)


def test_hierarchy_heads_navigation_safe_profiles_and_authority_unchanged(session, hr):
    chief = person(session, hr, "Директор")
    manager = person(session, hr, "Начальник отдела")
    lead = person(session, hr, "Руководитель команды")
    worker = person(session, hr, "Сотрудник")
    d.update(
        worker,
        email="private@example.local",
        phone="private-phone",
        max_user_id="secret-max",
        profile_bio="private bio",
        mood="private",
        manager_id="auth-manager",
    )
    original_roles = [(u.id, u.role, deepcopy(u.scope)) for u in session.query(User)]
    direction = unit(
        session, hr, "Управление", type="DIRECTION", head_employee_id=chief.id
    )
    department = unit(
        session, hr, "Отдел", parent_id=direction.id, head_employee_id=manager.id
    )
    team = unit(
        session,
        hr,
        "Команда",
        type="TEAM",
        parent_id=department.id,
        head_employee_id=lead.id,
    )
    assign(session, hr, chief, org_unit_id=direction.id)
    assign(session, hr, manager, org_unit_id=department.id)
    assign(session, hr, lead, org_unit_id=team.id)
    assign(session, hr, worker, org_unit_id=team.id)
    result = detail(session, hr, worker)
    assert result["manager_employee_id"] == lead.id
    assert [e["id"] for e in result["manager_chain"]] == [lead.id, manager.id, chief.id]
    assert worker.id in {e["id"] for e in detail(session, hr, lead)["direct_reports"]}
    assert result["org_unit_title"] == "Команда"
    assert not {
        "email",
        "phone",
        "max_user_id",
        "profile_bio",
        "mood",
        "manager_id",
    } & set(result)
    assert worker.data["manager_id"] == "auth-manager"
    assert [(u.id, u.role, u.scope) for u in session.query(User)] == original_roles


def test_explicit_manager_overrides_unit_and_null_clear_restores_effective_head(
    session, hr
):
    head = person(session, hr, "Начальник")
    mentor = person(session, hr, "Другой руководитель")
    worker = person(session, hr, "Сотрудник")
    team = unit(session, hr, head_employee_id=head.id)
    assign(session, hr, worker, org_unit_id=team.id, reports_to_employee_id=mentor.id)
    assert detail(session, hr, worker)["manager_employee_id"] == mentor.id
    assign(session, hr, worker, reports_to_employee_id=None)
    assert detail(session, hr, worker)["manager_employee_id"] == head.id
    assign(session, hr, worker, org_unit_id=None)
    assert detail(session, hr, worker)["manager_employee_id"] is None


def test_legacy_manager_user_employee_link_is_read_only_fallback(session, hr):
    manager = person(session, hr, "Руководитель")
    worker = person(session, hr, "Сотрудник")
    user = User(
        id="manager-user",
        email="manager@org.local",
        password_hash="unused",
        name="Руководитель",
        role="MANAGER",
        scope={"site_ids": ["GrPP"]},
        employee_id=manager.id,
    )
    session.add(user)
    d.update(worker, manager_id=user.id)
    session.commit()
    assert detail(session, hr, worker)["manager_employee_id"] == manager.id
    assign(session, hr, worker, reports_to_employee_id=None)
    assert worker.data["manager_id"] == user.id
    assert user.role == "MANAGER" and user.scope == {"site_ids": ["GrPP"]}


def test_unit_parent_cycle_and_self_parent_are_rejected_without_partial_save(
    session, hr
):
    root = unit(session, hr, "Управление")
    child = unit(session, hr, "Команда", parent_id=root.id)
    before = root.parent_id, etag(root)
    for parent_id in (root.id, child.id):
        with pytest.raises(HTTPException) as exc:
            org.patch_unit(
                root.id, org.UnitPatch(parent_id=parent_id), req(root), session, hr
            )
        assert exc.value.status_code == 409
        assert (root.parent_id, etag(root)) == before


def test_explicit_and_effective_head_reporting_cycles_are_rejected(session, hr):
    a = person(session, hr, "Первый")
    b = person(session, hr, "Второй")
    assign(session, hr, a, reports_to_employee_id=b.id)
    with pytest.raises(HTTPException) as exc:
        assign(session, hr, b, reports_to_employee_id=a.id)
    assert exc.value.status_code == 409
    assert b.data.get("reports_to_employee_id") is None
    assign(session, hr, a, reports_to_employee_id=None)
    team = unit(session, hr, head_employee_id=a.id)
    assign(session, hr, b, org_unit_id=team.id)
    with pytest.raises(HTTPException) as exc:
        assign(session, hr, a, reports_to_employee_id=b.id)
    assert exc.value.status_code == 409
    assert a.data.get("reports_to_employee_id") is None


def test_cycle_from_assigning_two_unit_heads_is_rejected(session, hr):
    a = person(session, hr, "Первый")
    b = person(session, hr, "Второй")
    x = unit(session, hr, "Первый отдел", head_employee_id=a.id)
    y = unit(session, hr, "Второй отдел", head_employee_id=b.id)
    assign(session, hr, a, org_unit_id=y.id)
    with pytest.raises(HTTPException) as exc:
        assign(session, hr, b, org_unit_id=x.id)
    assert exc.value.status_code == 409


def test_cross_legal_entity_and_incompatible_site_links_rejected(session, hr):
    other_legal = save_entity(session, "organization", {"name": "Другой контур"})
    foreign = person(session, hr, "Другой сотрудник")
    d.update(foreign, legal_entity_id=other_legal.id)
    local = person(session, hr, "Местный сотрудник")
    team = unit(session, hr, site_id="GTP")
    with pytest.raises(HTTPException) as exc:
        assign(session, hr, local, org_unit_id=team.id)
    assert exc.value.status_code == 422
    with pytest.raises(HTTPException) as exc:
        assign(session, hr, local, reports_to_employee_id=foreign.id)
    assert exc.value.status_code == 422
    with pytest.raises(HTTPException) as exc:
        unit(session, hr, head_employee_id=foreign.id)
    assert exc.value.status_code == 422
    with pytest.raises(HTTPException) as exc:
        org.create_unit(
            org.UnitCreate(
                title="Чужая площадка", legal_entity_id=other_legal.id, site_id="GrPP"
            ),
            session,
            hr,
        )
    assert exc.value.status_code == 422


def test_missing_ended_self_and_unseen_employee_links_rejected(session, hr):
    local = person(session, hr, "Сотрудник")
    ended = person(session, hr, "Уволенный")
    d.update(ended, employment_stage="ENDED")
    for target in ("missing", ended.id):
        with pytest.raises(HTTPException) as exc:
            assign(session, hr, local, reports_to_employee_id=target)
        assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        assign(session, hr, local, reports_to_employee_id=local.id)
    assert exc.value.status_code == 422
    unseen = person(session, hr, "На другой площадке", site_id="GTP")
    scoped = Actor("scoped-hr", "HR площадки", "HR_SPECIALIST", {"site_ids": ["GrPP"]})
    with pytest.raises(HTTPException) as exc:
        assign(session, scoped, local, reports_to_employee_id=unseen.id)
    assert exc.value.status_code == 404


def test_scope_directory_and_read_roles_do_not_grant_editing(session, hr):
    local = person(session, hr, "Свой сотрудник")
    colleague = person(session, hr, "Коллега")
    foreign = person(session, hr, "Чужое юрлицо")
    legal = save_entity(session, "organization", {"name": "Другая компания"})
    d.update(foreign, legal_entity_id=legal.id)
    team = unit(session, hr)
    for role in ("EMPLOYEE", "MANAGER"):
        actor = Actor("reader", "Читатель", role, {"all": True}, employee_id=local.id)
        result = org.organization(session, actor)
        assert result["can_edit"] is False
        if role == "EMPLOYEE":
            assert {e["id"] for e in result["employees"]} == {local.id, colleague.id}
            assert detail(session, actor, colleague)["id"] == colleague.id
            with pytest.raises(HTTPException) as exc:
                detail(session, actor, foreign)
            assert exc.value.status_code == 404
        with pytest.raises(HTTPException) as exc:
            assign(session, actor, local, org_unit_id=team.id)
        assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:
        org.organization(
            session, Actor("platform", "Администратор", "PLATFORM_ADMIN", {"all": True})
        )
    assert exc.value.status_code == 403


def test_site_hr_reads_parent_but_cannot_widen_or_edit_global_unit(session, hr):
    local = person(session, hr, "Сотрудник", site_id="GrPP")
    foreign_site = person(session, hr, "Другая площадка", site_id="GTP")
    parent = unit(session, hr, "Общее управление")
    scoped = Actor("scoped-hr", "HR площадки", "HR_SPECIALIST", {"site_ids": ["GrPP"]})
    result = org.organization(session, scoped)
    assert {e["id"] for e in result["employees"]} == {local.id}
    assert next(u for u in result["units"] if u["id"] == parent.id)["can_edit"] is False
    with pytest.raises(HTTPException) as exc:
        org.patch_unit(
            parent.id,
            org.UnitPatch(title="Новый заголовок"),
            req(parent),
            session,
            scoped,
        )
    assert exc.value.status_code == 404
    child = unit(session, scoped, "Местный отдел", parent_id=parent.id, site_id="GrPP")
    with pytest.raises(HTTPException) as exc:
        org.patch_unit(
            child.id, org.UnitPatch(site_id="GTP"), req(child), session, scoped
        )
    assert exc.value.status_code == 403


def test_stale_and_missing_etag_rejected_and_unknown_auth_fields_forbidden(session, hr):
    employee = person(session, hr, "Сотрудник")
    team = unit(session, hr)
    stale = req(employee)
    assign(session, hr, employee, org_unit_id=team.id)
    with pytest.raises(HTTPException) as exc:
        org.patch_employee(
            employee.id, org.EmployeePatch(org_unit_id=None), stale, session, hr
        )
    assert exc.value.status_code == 412
    with pytest.raises(HTTPException) as exc:
        org.patch_unit(
            team.id,
            org.UnitPatch(title="Новый отдел"),
            Request({"type": "http", "headers": []}),
            session,
            hr,
        )
    assert exc.value.status_code == 428
    with pytest.raises(ValidationError):
        org.EmployeePatch(manager_id="auth-manager", role="HR_ADMIN")


def test_archive_requires_empty_unit_and_preserves_employee_assignments(session, hr):
    employee = person(session, hr, "Сотрудник")
    team = unit(session, hr)
    assign(session, hr, employee, org_unit_id=team.id)
    with pytest.raises(HTTPException) as exc:
        org.patch_unit(
            team.id, org.UnitPatch(status="ARCHIVED"), req(team), session, hr
        )
    assert exc.value.status_code == 409
    assert employee.data["org_unit_id"] == team.id
    assign(session, hr, employee, org_unit_id=None)
    org.patch_unit(team.id, org.UnitPatch(status="ARCHIVED"), req(team), session, hr)
    assert team.status == "ARCHIVED"
    assert team.id not in {u["id"] for u in org.organization(session, hr)["units"]}


def test_profile_photo_is_private_actual_thumbnail_and_revoked_across_episodes(
    session, hr, tmp_path, monkeypatch
):
    from types import SimpleNamespace

    employee = person(session, hr, "Сотрудник")
    colleague = person(session, hr, "Коллега")
    image_path = tmp_path / "profile.png"
    Image.new("RGB", (800, 600), (80, 130, 190)).save(image_path)
    asset = save_entity(
        session,
        "asset",
        {
            "employee_id": employee.id,
            "episode_id": employee.data["episode_id"],
            "validated": True,
            "content_type": "image/png",
            "storage_path": str(image_path),
        },
        owner_id=employee.id,
        status="READY",
    )
    d.update(employee, profile_photo_asset_id=asset.id)
    monkeypatch.setattr(
        org, "get_settings", lambda: SimpleNamespace(blob_root=str(tmp_path))
    )
    assert org.employee_photo_url(session, hr, employee)
    response = org.employee_photo(employee.id, session, hr)
    with Image.open(io.BytesIO(response.body)) as image:
        assert image.size == (256, 192) and image.format == "JPEG"
    assert response.headers["cache-control"] == "private, no-store"
    actor = Actor("other-employee", "Коллега", "EMPLOYEE", {}, employee_id=colleague.id)
    assert org.employee_photo_url(session, actor, employee) is None
    with pytest.raises(HTTPException) as exc:
        org.employee_photo(employee.id, session, actor)
    assert exc.value.status_code == 404
    d.update(employee, episode_id="new-employment-episode")
    assert org.employee_photo_url(session, hr, employee) is None


def test_ended_head_does_not_block_unrelated_edits_and_can_be_repaired(session, hr):
    head = person(session, hr, "Бывший руководитель")
    employee = person(session, hr, "Сотрудник")
    team = unit(session, hr, "Отдел с бывшим руководителем", head_employee_id=head.id)
    assign(session, hr, employee, org_unit_id=team.id)
    d.update(head, employment_stage="ENDED")
    assert detail(session, hr, employee)["manager_employee_id"] is None
    unrelated = unit(session, hr, "Новое независимое подразделение")
    org.patch_unit(
        unrelated.id, org.UnitPatch(title="Новое название"), req(unrelated), session, hr
    )
    replacement = person(session, hr, "Новый руководитель")
    org.patch_unit(
        team.id, org.UnitPatch(head_employee_id=replacement.id), req(team), session, hr
    )
    assert detail(session, hr, employee)["manager_employee_id"] == replacement.id
    with pytest.raises(HTTPException) as exc:
        org.patch_unit(
            team.id, org.UnitPatch(head_employee_id=head.id), req(team), session, hr
        )
    assert exc.value.status_code == 404


def test_photo_helper_uses_current_authority_without_loading_whole_directory(
    session, hr, monkeypatch
):
    employee = person(session, hr, "Сотрудник")
    monkeypatch.setattr(
        org, "_all", lambda *_: pytest.fail("chat photo lookup must not scan directory")
    )
    assert org.employee_photo_url(session, hr, employee) is None


def test_unit_patch_stale_version_and_nonempty_children_or_head_block_archive(
    session, hr
):
    head = person(session, hr, "Руководитель")
    parent = unit(session, hr, "Управление")
    child = unit(session, hr, "Дочерний отдел", parent_id=parent.id)
    stale = req(parent)
    org.patch_unit(
        parent.id, org.UnitPatch(title="Новое управление"), req(parent), session, hr
    )
    with pytest.raises(HTTPException) as exc:
        org.patch_unit(
            parent.id, org.UnitPatch(title="Старая правка"), stale, session, hr
        )
    assert exc.value.status_code == 412
    with pytest.raises(HTTPException) as exc:
        org.patch_unit(
            parent.id, org.UnitPatch(status="ARCHIVED"), req(parent), session, hr
        )
    assert exc.value.status_code == 409
    org.patch_unit(
        child.id, org.UnitPatch(head_employee_id=head.id), req(child), session, hr
    )
    with pytest.raises(HTTPException) as exc:
        org.patch_unit(
            child.id, org.UnitPatch(status="ARCHIVED"), req(child), session, hr
        )
    assert exc.value.status_code == 409


def test_chat_list_and_detail_expose_only_current_authorized_profile_photo(session, hr):
    from fastapi import Response
    from korsik import chat

    employee = person(session, hr, "Сотрудник с фотографией")
    other = person(session, hr, "На другой площадке", site_id="GTP")
    asset = save_entity(
        session,
        "asset",
        {
            "employee_id": employee.id,
            "episode_id": employee.data["episode_id"],
            "validated": True,
            "content_type": "image/jpeg",
            "storage_path": "/not-public/a-real-profile-image.jpg",
        },
        owner_id=employee.id,
        status="READY",
    )
    d.update(employee, profile_photo_asset_id=asset.id)
    conv = chat.conversation_for(session, employee)
    url = f"/api/v1/organization/employees/{employee.id}/photo"
    rows = chat.conversations(hr, session)["items"]
    assert (
        next(row for row in rows if row["employee_id"] == employee.id)[
            "employee_photo_url"
        ]
        == url
    )
    assert (
        chat.conversation_detail(conv.id, Response(), hr, session)["employee_photo_url"]
        == url
    )
    scoped = Actor(
        "scoped-hr", "HR другой площадки", "HR_SPECIALIST", {"site_ids": ["GTP"]}
    )
    assert not any(
        row["employee_id"] == employee.id
        for row in chat.conversations(scoped, session)["items"]
    )
    with pytest.raises(HTTPException) as exc:
        chat.conversation_detail(conv.id, Response(), scoped, session)
    assert exc.value.status_code == 404
    asset.data = {**asset.data, "episode_id": "stale-employment-episode"}
    assert (
        chat.conversation_detail(conv.id, Response(), hr, session)["employee_photo_url"]
        is None
    )
    d.update(employee, employment_stage="ENDED")
    assert not any(
        row["employee_id"] == employee.id
        for row in chat.conversations(hr, session)["items"]
    )
    assert org.employee_photo_url(session, hr, employee) is None


def test_legal_transfer_redacts_stale_public_organization_links(session, hr):
    manager = person(session, hr, "Руководитель")
    employee = person(session, hr, "Сотрудник")
    team = unit(session, hr, head_employee_id=manager.id)
    assign(
        session, hr, employee, org_unit_id=team.id, reports_to_employee_id=manager.id
    )
    other = save_entity(session, "organization", {"name": "Другая организация"})
    d.update(employee, legal_entity_id=other.id)
    result = detail(session, hr, employee)
    assert result["org_unit_id"] is None
    assert result["reports_to_employee_id"] is None
    assert result["manager_employee_id"] is None
    d.update(manager, legal_entity_id=other.id)
    row = next(u for u in org.organization(session, hr)["units"] if u["id"] == team.id)
    assert row["head_employee_id"] is None


def test_scoped_unit_partial_patch_preserves_hidden_head_but_cannot_assign_unseen_id(
    session, hr
):
    head = person(session, hr, "Руководитель в центральном офисе", site_id="GTP")
    person(session, hr, "Сотрудник площадки", site_id="GrPP")
    team = unit(session, hr, "Отдел площадки", site_id="GrPP", head_employee_id=head.id)
    scoped = Actor("site-hr", "HR площадки", "HR_SPECIALIST", {"site_ids": ["GrPP"]})
    row = next(
        u for u in org.organization(session, scoped)["units"] if u["id"] == team.id
    )
    assert row["head_employee_id"] is None and row["can_edit"] is True
    response = org.patch_unit(
        team.id,
        org.UnitPatch(title="Новое название отдела"),
        req(team),
        session,
        scoped,
    )
    assert json.loads(response.body)["head_employee_id"] is None
    assert team.data["title"] == "Новое название отдела"
    assert team.data["head_employee_id"] == head.id
    with pytest.raises(HTTPException) as exc:
        org.patch_unit(
            team.id, org.UnitPatch(head_employee_id=head.id), req(team), session, scoped
        )
    assert exc.value.status_code == 404
    assert team.data["head_employee_id"] == head.id


def test_scoped_employee_partial_patch_preserves_hidden_explicit_manager(session, hr):
    manager = person(session, hr, "Руководитель другой площадки", site_id="GTP")
    employee = person(session, hr, "Сотрудник площадки", site_id="GrPP")
    team = unit(session, hr, "Местный отдел", site_id="GrPP")
    assign(session, hr, employee, reports_to_employee_id=manager.id)
    scoped = Actor("site-hr", "HR площадки", "HR_SPECIALIST", {"site_ids": ["GrPP"]})
    assert detail(session, scoped, employee)["reports_to_employee_id"] is None
    org.patch_employee(
        employee.id,
        org.EmployeePatch(org_unit_id=team.id),
        req(employee),
        session,
        scoped,
    )
    assert employee.data["reports_to_employee_id"] == manager.id
    assert employee.data["org_unit_id"] == team.id
