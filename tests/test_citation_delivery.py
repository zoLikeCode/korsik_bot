"""MAX evidence remains private and follows current publication permissions."""

from pathlib import Path
from types import SimpleNamespace
import pytest
from sqlalchemy.orm import sessionmaker
from PIL import Image
from korsik.db import make_engine
from korsik.models import Base
from korsik.common import save_entity
from korsik.auth import Actor
from korsik import assets, knowledge


@pytest.fixture
def data(tmp_path, monkeypatch):
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(engine, expire_on_commit=False)()
    monkeypatch.setattr(knowledge, "DATA", tmp_path)
    monkeypatch.setattr(
        assets, "get_settings", lambda: SimpleNamespace(blob_root=str(tmp_path))
    )
    employee = save_entity(
        db, "employee", {"site_id": "A", "employment_stage": "ACTIVE"}
    )
    doc = save_entity(
        db,
        "document",
        {
            "title": "Проверенный источник",
            "scope": {"site_ids": ["A"], "employment_stages": ["ACTIVE"]},
        },
        status="ACTIVE",
    )
    rev = save_entity(
        db,
        "document_revision",
        {"document_id": doc.id},
        status="PUBLISHED",
        parent_id=doc.id,
    )
    root = tmp_path / "knowledge" / rev.id
    preview = root / "previews" / "attempt-1" / "page-0001.png"
    preview.parent.mkdir(parents=True)
    Image.new("RGB", (32, 32), "white").save(preview)
    original = root / "source.pdf"
    original.write_bytes(b"%PDF-1.7\nfixture original")
    rev.data = {**rev.data, "source_path": str(original), "filename": "rules.pdf"}
    doc.data = {**doc.data, "active_revision_id": rev.id}
    ext = save_entity(
        db,
        "extraction",
        {"pages": [{"page_no": 1, "preview_file": "attempt-1/page-0001.png"}]},
        parent_id=rev.id,
        status="IMMUTABLE",
    )
    citation = {
        "revision_id": rev.id,
        "extraction_revision_id": ext.id,
        "document_id": doc.id,
        "document_title": doc.data["title"],
        "page_no": 1,
        "evidence_text": "Точная проверенная выдержка.",
    }
    actor = Actor(employee.id, "Employee", "EMPLOYEE", {}, employee.id)
    yield db, employee, doc, rev, ext, citation, actor, tmp_path
    db.close()
    engine.dispose()


def test_private_exact_page_and_original_are_deduplicated_then_revoked(data):
    db, employee, doc, rev, ext, citation, actor, root = data
    page = assets.citation_attachment(db, employee, citation)
    assert Path(page.data["storage_path"]).read_bytes().startswith(b"\x89PNG")
    assert "attempt-1" in page.data["storage_path"]
    assert assets.citation_attachment(db, employee, citation).id == page.id
    original = assets.citation_attachment(db, employee, citation, original=True)
    assert original.data["filename"] == "rules.pdf" and original.id != page.id
    assert assets.can_asset(db, actor, page)
    other = Actor("other", "Other", "EMPLOYEE", {}, "other")
    assert not assets.can_asset(db, other, page)
    doc.status = "ARCHIVED"
    assert not assets.can_asset(db, actor, page) and not assets.can_asset(
        db, actor, original
    )


def test_employee_visual_citation_source_http_uses_exact_authorized_extraction(data):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from korsik.auth import get_actor
    from korsik.db import get_db

    db, employee, doc, rev, ext, citation, actor, root = data
    row = save_entity(db, "citation", {**citation, "kind": "VISUAL", "employee_id": employee.id}, owner_id=employee.id)
    app = FastAPI()
    app.include_router(assets.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_actor] = lambda: actor
    client = TestClient(app)
    path = f"/citations/{row.id}/source"
    response = client.get(path)
    assert response.status_code == 200 and response.headers["content-type"] == "image/png"
    assert response.content == (root / "knowledge" / rev.id / "previews/attempt-1/page-0001.png").read_bytes()
    app.dependency_overrides[get_actor] = lambda: Actor("other", "Other", "EMPLOYEE", {}, "other")
    assert client.get(path).status_code == 404
    app.dependency_overrides[get_actor] = lambda: actor
    employee.data = {**employee.data, "site_id": "other"}
    assert client.get(path).status_code == 410
    employee.data = {**employee.data, "site_id": "A"}
    doc.data = {**doc.data, "source_revoked": True}
    assert client.get(path).status_code == 410


def test_missing_preview_yields_labelled_exact_extract_and_no_public_url(data):
    db, employee, doc, rev, ext, citation, actor, root = data
    ext.data = {"pages": [{"page_no": 1}]}
    asset = assets.citation_attachment(db, employee, citation)
    assert asset.data["content_type"] == "text/plain" and asset.data[
        "filename"
    ].startswith("Выдержка")
    assert citation["evidence_text"] in Path(asset.data["storage_path"]).read_text()
    assert assets.asset_public(asset)["source_url"].startswith("/api/v1/assets/")
    employee.data = {**employee.data, "employment_stage": "ENDED"}
    assert not assets.can_asset(db, actor, asset)


def test_private_photo_uses_current_employment_episode_even_with_known_asset_url(data):
    db, employee, doc, rev, ext, citation, actor, root = data
    employee.data = {**employee.data, "episode_id": "old-episode"}
    conv = save_entity(
        db, "conversation", {"employee_id": employee.id, "episode_id": "old-episode"}
    )
    photo = save_entity(
        db,
        "asset",
        {
            "employee_id": employee.id,
            "conversation_id": conv.id,
            "purpose": "EMPLOYEE_PHOTO",
        },
        status="READY",
    )
    assert assets.can_asset(db, actor, photo)
    employee.data = {**employee.data, "episode_id": "new-episode"}
    assert not assets.can_asset(db, actor, photo)
    hr = Actor("hr", "HR", "HR_ADMIN", {"all": True})
    assert assets.can_asset(db, hr, photo)


def test_material_access_rechecks_current_hr_site_and_legal_grants(data):
    db, *_ = data
    material = save_entity(
        db,
        "asset",
        {"scope": {"site_ids": ["A"], "legal_entity_ids": ["L1"]}},
        status="READY",
        owner_id="author",
    )
    author = Actor(
        "author", "HR", "HR_SPECIALIST", {"site_ids": ["A"], "legal_entity_ids": ["L1"]}
    )
    assert assets.can_asset(db, author, material)
    author.scope = {"site_ids": ["B"], "legal_entity_ids": ["L1"]}
    assert not assets.can_asset(db, author, material)
    author.scope = {"site_ids": ["A"], "legal_entity_ids": ["L2"]}
    assert not assets.can_asset(db, author, material)
    material.data = {"scope": {}}
    assert assets.can_asset(db, author, material)
    material.data = {"scope": {"sensitivity": "HR_RESTRICTED"}}
    assert not assets.can_asset(db, author, material)


def test_assigned_employee_only_hr_cannot_create_group_material(data):
    db, *_ = data
    from fastapi import HTTPException, UploadFile
    import io

    actor = Actor("assigned", "HR", "HR_SPECIALIST", {})
    with pytest.raises(HTTPException) as error:
        assets.upload_asset(
            file=UploadFile(filename="material.txt", file=io.BytesIO(b"material")),
            purpose="SCENARIO_MATERIAL",
            actor=actor,
            db=db,
        )
    assert error.value.status_code == 403


def test_scenario_reference_cannot_widen_material_employee_scope(data):
    db, employee, *rest = data
    actor = rest[-2]
    employee.data = {**employee.data, "episode_id": "current", "legal_entity_id": "L1"}
    material = save_entity(
        db,
        "asset",
        {"scope": {"site_ids": ["B"], "legal_entity_ids": ["L1"]}},
        status="READY",
    )
    version = save_entity(
        db,
        "scenario_version",
        {
            "graph": {
                "steps": [
                    {
                        "step_key": "asset-step",
                        "content": {"asset_ids": [material.id]},
                        "eligibility": {},
                    }
                ]
            }
        },
        status="PUBLISHED",
    )
    save_entity(
        db,
        "journey",
        {"episode_id": "current", "version_id": version.id},
        parent_id=employee.id,
        status="ACTIVE",
    )
    assert not assets.can_asset(db, actor, material)
    material.data = {"scope": {"site_ids": ["A"], "legal_entity_ids": ["L2"]}}
    assert not assets.can_asset(db, actor, material)
    material.data = {"scope": {"site_ids": ["A"], "legal_entity_ids": ["L1"]}}
    assert assets.can_asset(db, actor, material)


def test_max_presend_rechecks_attachment_scope_after_employee_transfer(data):
    db, employee, *_ = data
    from korsik.models import Outbox
    from korsik.transport_api import valid_outbox

    binding = save_entity(
        db,
        "max_binding",
        {"episode_id": "current", "max_user_id": "123456"},
        status="ACTIVE",
        parent_id=employee.id,
    )
    employee.data = {
        **employee.data,
        "episode_id": "current",
        "binding_id": binding.id,
        "max_binding_status": "VERIFIED",
    }
    material = save_entity(
        db,
        "asset",
        {"scope": {"site_ids": ["A"]}, "purpose": "SCENARIO_MATERIAL"},
        status="READY",
    )
    outbox = Outbox(
        recipient_kind="EMPLOYEE",
        recipient_id=employee.id,
        text="Материал",
        attachments=[{"asset_id": material.id}],
        data={"episode_id": "current"},
    )
    assert valid_outbox(db, outbox)[0] == {"user_id": 123456}
    employee.data = {**employee.data, "site_id": "B"}
    assert valid_outbox(db, outbox) == (None, "ATTACHMENT_SCOPE_REVOKED")
