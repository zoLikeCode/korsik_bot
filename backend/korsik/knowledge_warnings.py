"""Advisory duplicate/applicability review; never asserts a legal contradiction."""

from datetime import datetime, timezone
from difflib import SequenceMatcher
import re
from fastapi import APIRouter, Depends
from sqlalchemy import select
from .auth import get_actor
from .common import require_role
from .db import get_db
from .models import Entity
from .knowledge import _doc, _scope_access

router = APIRouter()


def normalized_title(value):
    return " ".join(
        re.findall(r"[a-zа-я0-9]+", str(value).casefold().replace("ё", "е"))
    )


def applicability_overlaps(first, second):
    for plural in (
        "site_ids",
        "legal_entity_ids",
        "employment_types",
        "employment_stages",
        "audience_roles",
    ):
        a, b = set(first.get(plural, [])), set(second.get(plural, []))
        if plural == "legal_entity_ids":
            a |= {first["legal_entity_id"]} if first.get("legal_entity_id") else set()
            b |= {second["legal_entity_id"]} if second.get("legal_entity_id") else set()
        if a and b and not a.intersection(b):
            return False
    return True


def period_overlaps(first, second):
    def bound(data, name, default):
        value = data.get(name)
        if not value:
            return default
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed

    low, high = datetime.min.replace(tzinfo=timezone.utc), datetime.max.replace(
        tzinfo=timezone.utc
    )
    try:
        return max(
            bound(first, "effective_from", low), bound(second, "effective_from", low)
        ) < min(bound(first, "effective_to", high), bound(second, "effective_to", high))
    except (ValueError, TypeError, AttributeError):
        return None


@router.get("/knowledge/documents/{document_id}/warnings")
def document_warnings(document_id: str, actor=Depends(get_actor), db=Depends(get_db)):
    require_role(actor, "HR_ADMIN", "HR_SPECIALIST")
    current = _doc(db, actor, document_id)
    documents = db.scalars(
        select(Entity).where(
            Entity.kind == "document", Entity.status.in_(["DRAFT", "ACTIVE"])
        )
    ).all()
    visible = {
        doc.id: doc
        for doc in documents
        if not doc.data.get("source_revoked") and _scope_access(actor, doc)
    }
    visible[current.id] = current
    revisions = db.scalars(
        select(Entity).where(
            Entity.kind == "document_revision",
            Entity.parent_id.in_(visible),
            Entity.status.not_in(["REJECTED", "PURGED", "TOMBSTONED"]),
        )
    ).all()
    by_doc = {}
    for revision in revisions:
        by_doc.setdefault(revision.parent_id, []).append(revision)

    def representative(doc):
        rows = by_doc.get(doc.id, [])
        pinned = doc.data.get("latest_revision_id") or doc.data.get(
            "active_revision_id"
        )
        return next(
            (row.data for row in rows if row.id == pinned),
            rows[-1].data if rows else {},
        )

    current_title = normalized_title(current.data.get("title"))
    current_hashes = {row.data.get("sha256") for row in by_doc.get(current.id, [])} - {
        None,
        "",
    }
    items = []
    for doc in sorted(visible.values(), key=lambda row: row.id):
        if doc.id == current.id:
            continue
        title = normalized_title(doc.data.get("title"))
        similarity = (
            SequenceMatcher(None, current_title, title, autojunk=False).ratio()
            if current_title and title
            else 0
        )
        overlap = applicability_overlaps(
            current.data.get("scope", {}), doc.data.get("scope", {})
        )
        periods = period_overlaps(representative(current), representative(doc))
        hashes = {row.data.get("sha256") for row in by_doc.get(doc.id, [])} - {None, ""}
        codes = []
        if current_hashes.intersection(hashes):
            codes.append("EXACT_FILE_DUPLICATE")
        if similarity >= 0.82:
            codes.append("SIMILAR_TITLE")
            if overlap and periods is not False:
                codes.append("POTENTIAL_APPLICABILITY_CONFLICT")
        for code in codes:
            items.append(
                {
                    "code": code,
                    "document_id": doc.id,
                    "title": doc.data.get("title"),
                    "status": doc.status,
                    "title_similarity": round(similarity, 3),
                    "scope_overlap": overlap,
                    "period_overlap": periods,
                }
            )
    return {
        "items": items,
        "advisory_only": True,
        "explanation": "Сходство названий и областей не доказывает противоречие. Сравните документы перед публикацией.",
    }
