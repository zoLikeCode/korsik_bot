"""Rebuild a disposable vector projection from authoritative published SQL rows."""

from sqlalchemy import select

from .models import Entity
from .retrieval import index_chunks, selected_model_fingerprints


def reindex_published(db):
    head = db.scalar(select(Entity).where(Entity.kind == "knowledge_head"))
    if not head:
        return {"status": "EMPTY", "indexed": 0}
    snapshot_id = head.data["snapshot_id"]
    snapshot = db.get(Entity, snapshot_id)
    if not snapshot or snapshot.kind != "knowledge_snapshot":
        raise ValueError("published_snapshot_missing")
    revisions = []
    for document_id, revision_id in snapshot.data.get("members", {}).items():
        document, revision = db.get(Entity, document_id), db.get(Entity, revision_id)
        if (
            not document
            or document.status != "ACTIVE"
            or document.data.get("source_revoked")
        ):
            continue
        if document.data.get("active_revision_id") != revision_id:
            raise ValueError("published_snapshot_document_mismatch")
        if not revision or revision.status != "PUBLISHED":
            raise ValueError("published_revision_missing")
        revisions.append(revision_id)
    chunks = (
        db.scalars(
            select(Entity)
            .where(
                Entity.kind == "knowledge_chunk",
                Entity.parent_id.in_(revisions),
                Entity.status == "PUBLISHED",
            )
            .order_by(Entity.id)
        ).all()
        if revisions
        else []
    )
    # Upsert only: no live collection deletion or interval with an empty index.
    # Every retrieval still checks current SQL permissions and publication state.
    projection = index_chunks(db, chunks) if chunks else {"indexed": 0}
    selected = selected_model_fingerprints(db)
    if (
        projection.get("encoder_fingerprint")
        and selected.get("embedder") != projection["encoder_fingerprint"]
    ):
        raise ValueError("active_encoder_projection_mismatch")
    db.refresh(head)
    if head.data["snapshot_id"] != snapshot_id:
        raise ValueError("snapshot_changed_during_reindex_retry")
    return {
        "status": "OK",
        "snapshot_id": snapshot_id,
        "revisions": len(revisions),
        **projection,
    }
