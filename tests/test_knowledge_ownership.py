"""Atomic owner assignment API, scope is independent of ownership."""

from fastapi import HTTPException
from korsik.auth import user_actor
from korsik.models import User, Entity, Audit
from korsik.knowledge_ownership import validate_document_owner
from sqlalchemy import select
import pytest
from test_knowledge import kb as kb


def add_hr(db, identifier, scope, enabled=True, role="HR_SPECIALIST"):
    user = User(
        id=identifier,
        email=identifier + "@owner.test",
        name=identifier,
        role=role,
        scope=scope,
        enabled=enabled,
        password_hash="no-login",
    )
    db.add(user)
    db.commit()
    return user


def test_owner_api_cas_private_reason_and_current_scope(kb):
    client, db, actor = kb
    first = add_hr(db, "owner-a", {"site_ids": ["a"], "legal_entity_ids": ["legal-a"]})
    second = add_hr(db, "owner-b", {"site_ids": ["a"], "legal_entity_ids": ["legal-a"]})
    created = client.post(
        "/knowledge/documents",
        json={
            "title": "Scoped document",
            "scope": {"site_ids": ["a"], "legal_entity_ids": ["legal-a"]},
            "owner_id": first.id,
        },
    )
    assert created.status_code == 201, created.text
    doc = created.json()
    assert doc["owner_id"] == first.id
    changed = client.patch(
        "/knowledge/documents/" + doc["id"],
        json={"owner_id": second.id, "reason": "Передача ответственности"},
        headers={"If-Match": created.headers["etag"]},
    )
    assert (
        changed.status_code == 200 and changed.json()["owner_id"] == second.id
    ), changed.text
    stale = client.patch(
        "/knowledge/documents/" + doc["id"],
        json={"owner_id": first.id, "reason": "Concurrent stale change"},
        headers={"If-Match": created.headers["etag"]},
    )
    assert stale.status_code == 412
    records = db.scalars(select(Audit).where(Audit.entity_id == doc["id"])).all()
    event = next(row for row in records if row.data.get("owner_id") == second.id)
    assert (
        event.data.get("previous_owner_id") == first.id and "reason" not in event.data
    )
    assert (
        db.get(Entity, event.data["reason_ref"]).data["reason"]
        == "Передача ответственности"
    )
    # Becoming an owner does not retain access after the grant is revoked.
    second.scope = {"site_ids": ["b"], "legal_entity_ids": ["legal-b"]}
    db.commit()
    actor.id, actor.role, actor.scope = second.id, second.role, second.scope
    assert client.get("/knowledge/documents/" + doc["id"]).status_code == 404


def test_owner_eligibility_enabled_hr_and_proposed_scope(kb):
    client, db, actor = kb
    a = add_hr(db, "regional-a", {"site_ids": ["a"], "legal_entity_ids": ["legal-a"]})
    b = add_hr(db, "regional-b", {"site_ids": ["b"], "legal_entity_ids": ["legal-b"]})
    disabled = add_hr(db, "disabled-hr", {"all": True}, enabled=False)
    manager = add_hr(db, "manager", {"all": True}, role="MANAGER")
    global_doc = client.post(
        "/knowledge/documents", json={"title": "Group", "scope": {}, "owner_id": a.id}
    )
    assert global_doc.status_code == 422
    created = client.post(
        "/knowledge/documents",
        json={
            "title": "Draft",
            "scope": {"site_ids": ["a"], "legal_entity_ids": ["legal-a"]},
            "owner_id": a.id,
        },
    )
    assert created.status_code == 201
    path = "/knowledge/documents/" + created.json()["id"]
    for invalid in (disabled.id, manager.id, b.id, None):
        result = client.patch(
            path,
            json={"owner_id": invalid, "reason": "Check eligibility"},
            headers={"If-Match": client.get(path).headers["etag"]},
        )
        assert result.status_code == 422, result.text
        db.rollback()
    scope_only = client.patch(
        path,
        json={
            "scope": {"site_ids": ["b"], "legal_entity_ids": ["legal-b"]},
            "reason": "Нельзя оставить ответственного вне области",
        },
        headers={"If-Match": client.get(path).headers["etag"]},
    )
    assert scope_only.status_code == 422, scope_only.text
    db.rollback()
    unchanged = client.get(path).json()
    assert unchanged["owner_id"] == a.id and unchanged["scope"]["site_ids"] == ["a"]
    result = client.patch(
        path,
        json={
            "scope": {"site_ids": ["b"], "legal_entity_ids": ["legal-b"]},
            "owner_id": b.id,
            "reason": "Новая область и ответственный",
        },
        headers={"If-Match": client.get(path).headers["etag"]},
    )
    assert result.status_code == 200 and result.json()["owner_id"] == b.id, result.text
    assert result.json()["scope"]["site_ids"] == ["b"]


def test_owner_change_requires_admin_reason_and_no_role_bypass(kb):
    client, db, actor = kb
    hr = add_hr(db, "local-editor", {"site_ids": ["a"]})
    doc = client.post(
        "/knowledge/documents", json={"title": "Local", "scope": {"site_ids": ["a"]}}
    ).json()
    entity = db.get(Entity, doc["id"])
    for reason in (None, " "):
        with pytest.raises(HTTPException) as missing:
            validate_document_owner(db, actor, entity, hr.id, reason, changing=True)
        assert missing.value.status_code == 422
    local_actor = user_actor(hr)
    with pytest.raises(HTTPException) as denied:
        validate_document_owner(
            db, local_actor, entity, actor.id, "Cannot reassign", changing=True
        )
    assert denied.value.status_code == 403


def test_specialist_scope_edit_preserves_eligible_other_owner(kb):
    client, db, actor = kb
    editor = add_hr(db, "two-site-editor", {"site_ids": ["a", "b"]})
    response = client.post(
        "/knowledge/documents",
        json={"title": "Narrowing", "scope": {"site_ids": ["a", "b"]}},
    )
    assert response.status_code == 201, response.text
    original_owner = actor.id
    actor.id, actor.role, actor.scope = editor.id, editor.role, editor.scope
    result = client.patch(
        "/knowledge/documents/" + response.json()["id"],
        json={"scope": {"site_ids": ["a"]}, "reason": "Точнее область документа"},
        headers={"If-Match": response.headers["etag"]},
    )
    assert result.status_code == 200, result.text
    assert result.json()["owner_id"] == original_owner
