"""Organization ACLs must follow live membership, not cached/indexed claims."""
from copy import deepcopy

import pytest
from sqlalchemy.orm import sessionmaker

from korsik import organization_scope, retrieval
from korsik.common import save_entity
from korsik.db import make_engine
from korsik.models import Base, Entity
from korsik.organization_scope import org_unit_ancestors


@pytest.fixture
def organization_acl(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'organization-acl.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    db = factory()

    def unit(title, parent=None, legal="company", site=None):
        return save_entity(db, "org_unit", {"title": title, "legal_entity_id": legal,
                           "site_id": site}, parent_id=parent.id if parent else None)

    root = unit("Дирекция")
    other = unit("Другая дирекция")
    department = unit("Отдел", root)
    team = unit("Команда", department, site="site-a")
    employee = save_entity(db, "employee", {"legal_entity_id": "company", "site_id": "site-a",
                           "org_unit_id": team.id, "employment_stage": "ACTIVE", "employment_type": "OFFICE"})
    conversation = save_entity(db, "conversation", {"employee_id": employee.id})
    db.commit()
    yield db, factory, employee, conversation, root, department, team, other
    db.close()
    engine.dispose()


def scope(unit, **extra):
    return {"org_unit_ids": [unit.id], **extra}


def update_elsewhere(factory, row, **data):
    with factory() as other:
        current = other.get(Entity, row.id)
        current.data = {**current.data, **data}
        other.commit()


def document(db, organization=None, **extra):
    doc = save_entity(db, "document", {"title": "Правила", "scope": {
        "legal_entity_ids": ["company"], "site_ids": ["site-a"],
        **(scope(organization) if organization else {}), **extra}})
    revision = save_entity(db, "document_revision", {"effective_from": "2020-01-01T00:00:00+00:00",
                           "content_fingerprint": "fixture"}, status="PUBLISHED", parent_id=doc.id)
    doc.data = {**doc.data, "active_revision_id": revision.id}
    db.flush()
    return doc, revision


def test_parent_scope_includes_descendants_without_bypassing_tenant_or_site(organization_acl):
    db, _, employee, _, root, department, team, other = organization_acl
    assert org_unit_ancestors(db, employee) == {root.id, department.id, team.id}
    for unit in [root, department, team]:
        assert retrieval.scope_matches(scope(unit), employee)  # object_session fallback
    assert not retrieval.scope_matches(scope(other), employee)
    assert not retrieval.scope_matches(scope(root, site_ids=["site-b"]), employee)
    assert not retrieval.scope_matches(scope(root, legal_entity_ids=["elsewhere"]), employee)
    # An empty organization restriction removes only that restriction.
    assert retrieval.scope_matches({"org_unit_ids": [], "site_ids": ["site-a"]}, employee)
    assert not retrieval.scope_matches({"org_unit_ids": [], "site_ids": ["site-b"], "all": True}, employee)


def test_membership_move_and_reparent_are_immediate_in_stale_session(organization_acl):
    db, factory, employee, _, root, department, team, other = organization_acl
    assert retrieval.scope_matches(scope(root), employee)
    # Both employee and units are still cached in db's identity map.
    with factory() as editor:
        moved_team = editor.get(Entity, team.id)
        moved_team.parent_id = other.id
        editor.commit()
    assert not retrieval.scope_matches(scope(root), employee)
    assert retrieval.scope_matches(scope(other), employee)
    update_elsewhere(factory, employee, org_unit_id=department.id)
    assert retrieval.scope_matches(scope(root), employee)
    assert not retrieval.scope_matches(scope(other), employee)
    assert employee.data["org_unit_id"] == department.id


def test_dict_identity_uses_current_database_membership_not_copied_attributes(organization_acl):
    db, factory, employee, _, root, _, team, other = organization_acl
    snapshot = {**deepcopy(employee.data), "employee_id": employee.id,
                "org_unit_ancestors": [other.id], "org_unit_ids": [other.id]}
    update_elsewhere(factory, employee, org_unit_id=other.id)
    assert retrieval.scope_matches(scope(other), snapshot, db=db)
    assert not retrieval.scope_matches(scope(root), snapshot, db=db)
    assert not retrieval.scope_matches(scope(team), snapshot, db=db)
    assert not retrieval.scope_matches(scope(other), {**snapshot, "employee_id": "missing"}, db=db)


def test_anonymous_profile_requires_db_for_ancestors_and_ignores_fabricated_claims(organization_acl):
    db, _, employee, _, root, _, team, other = organization_acl
    profile = {**deepcopy(employee.data), "org_unit_ancestors": [root.id, other.id],
               "org_unit_ids": [other.id], "ancestor_ids": [root.id]}
    assert retrieval.scope_matches(scope(team), profile)
    assert not retrieval.scope_matches(scope(root), profile)
    assert not retrieval.scope_matches(scope(other), profile)
    # Explicit DB enables a trusted HR preview profile to use the actual tree.
    assert retrieval.scope_matches(scope(root), profile, db=db)
    assert not retrieval.scope_matches(scope(other), profile, db=db)


def test_archival_revokes_unit_and_breaks_ancestor_chain(organization_acl):
    db, factory, employee, _, root, department, team, _ = organization_acl
    with factory() as editor:
        editor.get(Entity, department.id).status = "ARCHIVED"
        editor.commit()
    assert retrieval.scope_matches(scope(team), employee)
    assert not retrieval.scope_matches(scope(department), employee)
    assert not retrieval.scope_matches(scope(root), employee)
    with factory() as editor:
        editor.get(Entity, team.id).status = "ARCHIVED"
        editor.commit()
    assert org_unit_ancestors(db, employee) == set()
    assert not retrieval.scope_matches(scope(team), employee)


@pytest.mark.parametrize("changes", [{"legal_entity_id": "other-company"}, {"site_id": "site-b"}])
def test_corrupt_outside_legal_or_site_membership_never_grants(organization_acl, changes):
    db, factory, employee, _, root, _, team, _ = organization_acl
    update_elsewhere(factory, team, **changes)
    assert org_unit_ancestors(db, employee) == set()
    assert not retrieval.scope_matches(scope(team), employee)
    assert not retrieval.scope_matches(scope(root), employee)


def test_cross_legal_parent_stops_chain_and_cycle_or_depth_fails_closed(organization_acl, monkeypatch):
    db, factory, employee, _, root, department, team, _ = organization_acl
    update_elsewhere(factory, root, legal_entity_id="other-company")
    assert org_unit_ancestors(db, employee) == {team.id, department.id}
    assert not retrieval.scope_matches(scope(root), employee)
    update_elsewhere(factory, root, legal_entity_id="company")
    with factory() as editor:
        editor.get(Entity, root.id).parent_id = team.id
        editor.commit()
    assert org_unit_ancestors(db, employee) == set()
    with factory() as editor:
        editor.get(Entity, root.id).parent_id = None
        editor.commit()
    monkeypatch.setattr(organization_scope, "MAX_ORGANIZATION_DEPTH", 2)
    assert org_unit_ancestors(db, employee) == set()


def test_corpus_follows_live_scope_changes_without_index_rebuild(organization_acl, monkeypatch):
    db, factory, employee, _, root, _, team, other = organization_acl
    doc, revision = document(db, root)
    _, other_revision = document(db, other)
    db.commit()
    monkeypatch.setattr(retrieval, "MODE", "CORE")
    monkeypatch.setattr(retrieval, "_request", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
    chunks = retrieval.make_chunks(db, revision, {"id": "extraction", "blocks": [
        {"id": "block", "kind": "TEXT", "page_no": 1, "text": "Обед с двенадцати до тринадцати часов."}]}, persist=False)
    assert chunks[0].data["org_unit_ids"] == [root.id]
    assert set(retrieval.published_corpus(db, employee)) == {revision.id}
    update_elsewhere(factory, employee, org_unit_id=other.id)
    assert set(retrieval.published_corpus(db, employee)) == {other_revision.id}
    # Indexed metadata still says root; it cannot override the authoritative ACL.
    assert chunks[0].data["org_unit_ids"] == [root.id]
    update_elsewhere(factory, doc, scope=scope(other))
    assert set(retrieval.published_corpus(db, employee)) == {revision.id, other_revision.id}


def test_disabled_employee_cannot_get_corpus_history_answer_or_preview(organization_acl, monkeypatch):
    db, factory, employee, conversation, root, _, _, _ = organization_acl
    _, revision = document(db)
    db.commit()
    snapshot = {**deepcopy(employee.data), "employee_id": employee.id}
    permitted = retrieval.published_corpus(db, employee)
    assert revision.id in permitted
    assert retrieval.answer_still_authorized(db, conversation, {"citations": []})
    update_elsewhere(factory, employee, enabled=False)
    assert not retrieval.scope_matches({"all": True}, employee)
    assert not retrieval.scope_matches(scope(root), snapshot, db=db)
    assert not retrieval.scope_matches({}, {**snapshot, "enabled": False})
    assert retrieval.published_corpus(db, employee) == {}
    assert retrieval.published_corpus(db, snapshot) == {}
    assert retrieval.safe_history(db, employee, [{"role": "USER", "text": "Private"}]) == []
    assert not retrieval.answer_still_authorized(db, conversation, {"citations": []})
    assert not retrieval.answer_still_authorized(db, conversation, {"citations": [{"revision_id": revision.id}]})
    assert retrieval.search(db, employee, "Обед", _preview={"permitted": permitted})[0] == []
    monkeypatch.setattr(retrieval, "search", lambda *a, **k: pytest.fail("OFF must be rejected before retrieval"))
    answer = retrieval.answer_question(db, conversation, "Обед")
    assert answer["reason"] == "EMPLOYEE_DISABLED" and not answer["citations"]
    update_elsewhere(factory, employee, enabled=True)
    assert retrieval.scope_matches({}, employee)  # No cached OFF or ancestor grant.


def test_corpus_reads_current_tree_once_independent_of_document_count(organization_acl):
    from sqlalchemy import event

    db, _, employee, _, root, _, _, _ = organization_acl
    for _ in range(25):
        document(db, root)
    db.commit()
    statements = []

    def selected(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(db.bind, "before_cursor_execute", selected)
    try:
        assert len(retrieval.published_corpus(db, employee)) == 25
    finally:
        event.remove(db.bind, "before_cursor_execute", selected)
    # Employee, document list, one fresh membership/path and revision list.
    # More documents must not cause another employee/tree query per document.
    assert len(statements) <= 5
