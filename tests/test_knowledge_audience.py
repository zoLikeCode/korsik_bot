"""The simple document audience is a live organizational ACL inside HR grants."""
import pytest

from korsik.common import save_entity
from korsik import knowledge, retrieval
from test_knowledge import kb


def unit(db, key, legal="legal-a", site=None, parent=None, status="ACTIVE"):
    row = save_entity(db, "org_unit", {"title": key, "legal_entity_id": legal,
                      "site_id": site}, id=key, parent_id=parent, status=status)
    db.commit()
    return row


def sites(db):
    for key, legal in [("a", "legal-a"), ("b", "legal-b")]:
        save_entity(db, "site", {"legal_entity_id": legal}, id=key)
    db.commit()


def create(client, **body):
    return client.post("/knowledge/documents", json={"title": "Правила", **body})


def test_all_checkbox_clears_unit_filter_but_never_hr_grants(kb):
    client, db, actor = kb
    sites(db)
    actor.scope = {"site_ids": ["a"], "legal_entity_ids": ["legal-a"]}
    result = create(client, audience_all=True, scope={"org_unit_ids": ["old-selection"]})
    assert result.status_code == 201, result.text
    doc = result.json()
    assert doc["simple_workflow"] is True
    assert doc["scope"]["site_ids"] == ["a"]
    assert doc["scope"]["legal_entity_ids"] == ["legal-a"]
    assert not doc["scope"].get("org_unit_ids")
    assert retrieval.scope_matches(doc["scope"], {"site_id": "a", "legal_entity_id": "legal-a",
        "employment_stage": "PREBOARDING", "employment_type": "OFFICE"})
    assert not retrieval.scope_matches(doc["scope"], {"site_id": "b", "legal_entity_id": "legal-b",
        "employment_stage": "ACTIVE", "employment_type": "OFFICE"})


def test_selected_department_includes_current_descendant_not_other_department(kb):
    client, db, actor = kb
    sites(db)
    root = unit(db, "direction")
    child = unit(db, "team", parent=root.id, site="a")
    unit(db, "other-team", site="a")
    actor.scope = {"site_ids": ["a"]}
    result = create(client, audience_all=False, scope={"org_unit_ids": [root.id]})
    assert result.status_code == 201, result.text
    scope = result.json()["scope"]
    employee = save_entity(db, "employee", {"site_id": "a", "legal_entity_id": "legal-a",
        "org_unit_id": child.id, "employment_stage": "ACTIVE", "employment_type": "OFFICE"})
    db.commit()
    assert retrieval.scope_matches(scope, employee, db=db)
    employee.data = {**employee.data, "org_unit_id": "other-team"}
    db.commit()
    assert not retrieval.scope_matches(scope, employee, db=db)


@pytest.mark.parametrize("submitted", [None, [], ["missing"], ["archived"], ["unit", "unit"], [22], "unit"])
def test_picker_rejects_empty_missing_archived_or_malformed_units(kb, submitted):
    client, db, _ = kb
    unit(db, "unit")
    unit(db, "archived", status="ARCHIVED")
    result = create(client, audience_all=False, scope={"org_unit_ids": submitted})
    assert result.status_code == 422, result.text


@pytest.mark.parametrize("scope", [{"site_ids": ["b"]}, {"all": True}, {"sensitivity": "STANDARD"}])
def test_simple_picker_rejects_client_supplied_grant_fields(kb, scope):
    client, _, _ = kb
    assert create(client, audience_all=True, scope=scope).status_code == 422


def test_scope_and_current_hr_area_both_constrain_selected_unit(kb):
    client, db, actor = kb
    sites(db)
    unit(db, "outside", legal="legal-b", site="b")
    unit(db, "global-outside", legal="legal-b")
    actor.scope = {"site_ids": ["a"]}
    assert create(client, audience_all=False, scope={"org_unit_ids": ["outside"]}).status_code == 403
    assert create(client, audience_all=False, scope={"org_unit_ids": ["global-outside"]}).status_code == 422
    actor.scope = {}
    assert create(client, audience_all=True, scope={}).status_code == 403


def test_active_document_cannot_drop_unit_acl_or_expand_it_in_place(kb):
    client, db, _ = kb
    unit(db, "unit")
    unit(db, "other")
    result = create(client, audience_all=False, scope={"org_unit_ids": ["unit"]})
    doc = db.get(knowledge.Entity, result.json()["id"])
    doc.status = "ACTIVE"
    db.commit()
    current = client.get(f"/knowledge/documents/{doc.id}")
    for units in ([], ["unit", "other"]):
        scope = {**result.json()["scope"], "org_unit_ids": units}
        changed = client.patch(f"/knowledge/documents/{doc.id}", json={"scope": scope},
                               headers={"If-Match": current.headers["etag"]})
        assert changed.status_code == 409, changed.text
    assert knowledge._scope_subset({"org_unit_ids": ["unit"]}, {"org_unit_ids": ["unit", "other"]})
    assert not knowledge._scope_subset({}, {"org_unit_ids": ["unit"]})


def test_historical_missing_stage_is_active_only_for_scope_expansion_check():
    assert knowledge._scope_subset({"employment_stages": ["ACTIVE"]}, {})
    assert not knowledge._scope_subset({"employment_stages": ["PREBOARDING", "ACTIVE"]}, {})
    assert not knowledge._scope_subset({}, {"employment_stages": ["PREBOARDING"]})


def test_old_explicit_document_api_and_upload_stay_advanced_unless_opted_in(kb):
    client, db, _ = kb
    doc = create(client, scope={"site_ids": ["a"]}).json()
    assert "simple_workflow" not in doc
    upload = client.post(f"/knowledge/documents/{doc['id']}/revisions",
        files={"file": ("rules.txt", b"Rules for staff.", "text/plain")},
        data={"simple_workflow": "true"}, headers={"If-Match": doc["etag"]})
    assert upload.status_code == 202, upload.text
    assert upload.json()["revision"]["simple_workflow"] is True
