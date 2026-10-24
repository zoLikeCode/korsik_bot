"""HR QA profiles use verified organizational membership and live access."""
import copy
import json

import pytest
from pydantic import ValidationError

from korsik import knowledge_admin
from korsik.common import save_entity
from korsik.models import Entity
from test_knowledge_admin import admin as admin, candidate, question


def unit(db, **changes):
    return save_entity(db, "org_unit", {"title": "Подразделение", "legal_entity_id": "legal-a", "site_id": "a", **changes})


def test_preview_profile_unit_is_optional_but_never_accepts_ancestor_claims():
    data = question()["preview_profile"]
    assert knowledge_admin.PreviewProfile(**data).org_unit_id is None
    assert knowledge_admin.PreviewProfile(**data, org_unit_id="team").org_unit_id == "team"
    for extra in [{"org_unit_id": ""}, {"org_unit_ancestors": ["team"]}, {"org_unit_ids": ["team"]}]:
        with pytest.raises(ValidationError):
            knowledge_admin.PreviewProfile(**data, **extra)


@pytest.mark.parametrize("invalid", ["missing", "wrong_kind", "archived", "legal", "site"])
def test_control_creation_rejects_invalid_unit_even_for_all_scope_hr(admin, invalid):
    client, db, _ = admin
    selected = unit(db)
    if invalid == "wrong_kind":
        selected.kind = "asset"
    elif invalid == "archived":
        selected.status = "ARCHIVED"
    elif invalid in {"legal", "site"}:
        selected.data = {**selected.data, "legal_entity_id" if invalid == "legal" else "site_id": "elsewhere"}
    db.commit()
    payload = question()
    payload["preview_profile"]["org_unit_id"] = "missing-unit" if invalid == "missing" else selected.id
    response = client.post("/knowledge/control-questions", json=payload)
    assert response.status_code == 403, response.text


def test_scoped_hr_can_choose_current_hq_or_own_site_units_but_cannot_cross_grant(admin):
    client, db, actor = admin
    hq = unit(db, site_id=None)
    local = unit(db)
    actor.role, actor.scope = "HR_SPECIALIST", {"site_ids": ["a"]}
    for selected in [hq, local]:
        payload = question()
        payload["preview_profile"]["org_unit_id"] = selected.id
        response = client.post("/knowledge/control-questions", json=payload)
        assert response.status_code == 201, response.text
        assert response.json()["preview_profile"]["org_unit_id"] == selected.id
    payload["preview_profile"].update(site_id="b", legal_entity_id="legal-b")
    assert client.post("/knowledge/control-questions", json=payload).status_code == 403


def test_control_suite_uses_db_descendants_for_org_restricted_evidence(admin):
    client, db, actor = admin
    doc, revision, built = candidate(client, db)
    parent = unit(db, site_id=None)
    child = unit(db)
    child.parent_id = parent.id
    document = db.get(Entity, doc["id"])
    document.data = {**document.data, "scope": {**document.data["scope"], "org_unit_ids": [parent.id]}}
    db.commit()
    payload = question(revision["id"])
    payload["preview_profile"]["org_unit_id"] = child.id
    created = client.post("/knowledge/control-questions", json=payload)
    assert created.status_code == 201, created.text
    result = knowledge_admin.evaluate_control_suite(db, built, actor)
    assert result["status"] == "PASSED", json.dumps(result, ensure_ascii=False)
    assert result["results"][0]["citation_revision_ids"] == [revision["id"]]
    # Moving the selected unit out of the parent's subtree revokes evidence
    # immediately, without mutating the immutable control expectation.
    before = copy.deepcopy(db.get(Entity, created.json()["id"]).data)
    child.parent_id = None
    db.commit()
    result = knowledge_admin.evaluate_control_suite(db, built, actor)
    assert result["status"] == "FAILED"
    assert result["results"][0]["citation_revision_ids"] == []
    assert db.get(Entity, created.json()["id"]).data == before
    child.status = "ARCHIVED"
    db.commit()
    assert client.get("/knowledge/control-questions").json()["items"] == []
    assert knowledge_admin.evaluate_control_suite(db, built, actor)["results"] == []
