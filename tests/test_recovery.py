import pytest
from sqlalchemy.orm import Session
from korsik import recovery
from korsik.common import can_delegate_scope, save_entity
from korsik.db import make_engine
from korsik.models import Base


def test_reindex_only_authoritative_published_chunks_and_checks_snapshot(monkeypatch):
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        doc = save_entity(db, "document", {}, status="ACTIVE")
        rev = save_entity(
            db, "document_revision", {}, status="PUBLISHED", parent_id=doc.id
        )
        doc.data = {"active_revision_id": rev.id}
        snapshot = save_entity(
            db, "knowledge_snapshot", {"members": {doc.id: rev.id}}, status="PUBLISHED"
        )
        head = save_entity(db, "knowledge_head", {"snapshot_id": snapshot.id})
        keep = save_entity(
            db,
            "knowledge_chunk",
            {"text": "internal"},
            status="PUBLISHED",
            parent_id=rev.id,
        )
        save_entity(
            db,
            "knowledge_chunk",
            {"text": "draft"},
            status="CANDIDATE",
            parent_id=rev.id,
        )
        save_entity(
            db, "knowledge_chunk", {"text": "old"}, status="PUBLISHED", parent_id="old"
        )
        db.commit()
        calls = []
        monkeypatch.setattr(
            recovery,
            "index_chunks",
            lambda _, chunks: calls.extend(c.id for c in chunks)
            or {"indexed": len(chunks), "encoder_fingerprint": "pinned"},
        )
        monkeypatch.setattr(
            recovery, "selected_model_fingerprints", lambda _: {"embedder": "pinned"}
        )
        result = recovery.reindex_published(db)
        assert result["indexed"] == 1 and calls == [keep.id]
        monkeypatch.setattr(
            recovery, "selected_model_fingerprints", lambda _: {"embedder": "other"}
        )
        with pytest.raises(ValueError, match="active_encoder_projection_mismatch"):
            recovery.reindex_published(db)

        def changed(_, chunks):
            head.data = {"snapshot_id": "new"}
            db.flush()
            return {"indexed": 1}

        monkeypatch.setattr(recovery, "index_chunks", changed)
        with pytest.raises(ValueError, match="snapshot_changed"):
            recovery.reindex_published(db)
    engine.dispose()


def test_drilldown_grant_cannot_be_escalated():
    assert not can_delegate_scope(
        {"site_ids": ["A"]}, {"site_ids": ["A"], "employee_drilldown": True}
    )
    assert not can_delegate_scope({"all": True}, {"employee_drilldown": "yes"})
    assert can_delegate_scope(
        {"site_ids": ["A"], "employee_drilldown": True},
        {"site_ids": ["A"], "employee_drilldown": True},
    )
    assert not can_delegate_scope(
        {"site_ids": ["A"], "employee_drilldown": True},
        {"site_ids": ["B"], "employee_drilldown": True},
    )
