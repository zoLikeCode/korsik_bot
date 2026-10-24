"""Versioned knowledge lifecycle: upload -> extraction -> human review -> publish.

No upload becomes searchable automatically. Published revisions and extraction
snapshots remain immutable; employees resolve current scope at every read.
"""
from __future__ import annotations
import copy
import hashlib
import errno
import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select, text

from .auth import Actor, get_actor
from .common import audit, check_etag, commit, enqueue, etag, get_entity, require_role, save_entity, serialize
from .db import get_db, new_id, now
from .models import Entity
from .parsing import MAX_BYTES, digest, parse_document
from .retrieval import index_chunks, make_chunks, scope_matches, is_effective

router = APIRouter()
EDIT_ROLES = ("HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT")
REVIEW_ROLES = ("HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT")
DATA = Path(os.environ.get("DATA_DIR", os.environ.get("KORSIK_BLOB_ROOT", "/data")))
ALLOWED = {".pdf", ".pptx", ".txt", ".md", ".png", ".jpg", ".jpeg"}


def _update(entity, **values):
    entity.data = {**entity.data, **values}
    entity.version = (entity.version or 1) + 1
    entity.updated_at = now()


def _document_metadata_snapshot(doc):
    return copy.deepcopy({key: doc.data.get(key) for key in ("title", "scope", "document_category")})


def _effective_dates(start, end=None):
    start = start or now().isoformat()
    try:
        stamps = [datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None for value in (start, end)]
        stamps = [value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value for value in stamps]
        if stamps[1] and stamps[1] <= stamps[0]:
            raise ValueError("effective_to_not_after_start")
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(422, "invalid_effective_date_range") from exc
    return start, end or None


def _legal_scope(scope):
    ids = set(scope.get("legal_entity_ids") or [])
    single = scope.get("legal_entity_id")
    return ids & {single} if ids and single else ids or ({single} if single else set())


def _scope_access(actor, doc, employee=None, write=False):
    if actor.role == "EMPLOYEE":
        return bool(employee and doc.status == "ACTIVE" and scope_matches(doc.data.get("scope"), employee))
    if actor.role not in {"HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT"}:
        return False
    grant = actor.scope or {}
    if grant.get("all") is True:
        return True
    sites, legal = grant.get("site_ids", []), grant.get("legal_entity_ids", [])
    document_scope = doc.data.get("scope", {})
    document_sites = document_scope.get("site_ids", [])
    document_legal = _legal_scope(document_scope)
    if not document_sites and not document_legal:
        return bool(not write and doc.status == "ACTIVE" and document_scope.get("sensitivity", "STANDARD") == "STANDARD" and (sites or legal))
    if sites and (not document_sites or not set(document_sites).issubset(sites)):
        return False
    if legal and (not document_legal or not document_legal.issubset(legal)):
        return False
    # A site grant does not grant group-wide draft/edit access. Published standard
    # group materials may be read by HR, but never widened/edited on that grant.
    return bool(sites or legal)


def _validate_scope(scope):
    if not isinstance(scope, dict):
        raise HTTPException(422, "invalid_scope")
    result = copy.deepcopy(scope)
    allowed = {"legal_entity_id", "legal_entity_ids", "site_ids", "org_unit_ids", "audience_roles", "sensitivity", "employment_stages", "employment_types"}
    if set(result) - allowed:
        raise HTTPException(422, "unknown_scope_fields")
    defaults = {"site_ids": [], "audience_roles": ["EMPLOYEE"], "sensitivity": "STANDARD",
                "employment_stages": ["ACTIVE"], "employment_types": ["OFFICE", "PRODUCTION", "OTHER"]}
    for key, value in defaults.items():
        result.setdefault(key, value)
    enums = {"audience_roles": {"EMPLOYEE", "HR_SPECIALIST", "HR_ADMIN", "MANAGER", "HEAD_TALENT", "PLATFORM_ADMIN", "AUDITOR"},
             "employment_stages": {"PREBOARDING", "ACTIVE"}, "employment_types": {"OFFICE", "PRODUCTION", "OTHER"}}
    for key in ["site_ids", *enums, *(["legal_entity_ids"] if "legal_entity_ids" in result else []), *(["org_unit_ids"] if "org_unit_ids" in result else [])]:
        values = result[key]
        if not isinstance(values, list) or len(values) > 1000 or any(not isinstance(value, str) for value in values) or len(set(values)) != len(values):
            raise HTTPException(422, "invalid_scope_list:" + key)
        if key in enums and (not values or not set(values).issubset(enums[key])):
            raise HTTPException(422, "invalid_scope_values:" + key)
    if result["sensitivity"] not in {"STANDARD", "HR_RESTRICTED", "COMPLIANCE_RESTRICTED"}:
        raise HTTPException(422, "invalid_scope_sensitivity")
    if result.get("legal_entity_id") is not None and not isinstance(result["legal_entity_id"], str):
        raise HTTPException(422, "invalid_scope_legal_entity")
    if result.get("legal_entity_id") and result.get("legal_entity_ids") and result["legal_entity_id"] not in result["legal_entity_ids"]:
        raise HTTPException(422, "contradictory_legal_entity_scope")
    return result


def _validate_org_scope(db, actor, scope):
    """Unit choices narrow the actor's grants; they never replace those grants."""
    from .organization import _grant
    ids = scope.get("org_unit_ids") or []
    if not ids:
        return scope
    units = {u.id: u for u in db.scalars(select(Entity).where(
        Entity.kind == "org_unit", Entity.id.in_(ids), Entity.status == "ACTIVE"
    ).execution_options(populate_existing=True))}
    if len(units) != len(ids):
        raise HTTPException(422, "organization_unit_not_available")
    scope_legal = _legal_scope(scope)
    scope_sites = set(scope.get("site_ids") or [])
    sites = {s.id: s for s in db.scalars(select(Entity).where(
        Entity.kind == "site", Entity.id.in_(scope_sites)
    ))} if scope_sites else {}
    site_legal = {s.data.get("legal_entity_id") for s in sites.values()}
    for unit in units.values():
        legal, site = unit.data.get("legal_entity_id"), unit.data.get("site_id")
        if not legal or not _grant(actor.scope or {}, legal, site):
            raise HTTPException(403, "organization_unit_not_granted")
        if scope_legal and legal not in scope_legal:
            raise HTTPException(422, "organization_unit_outside_document_scope")
        if scope_sites and (site and site not in scope_sites or not site and legal not in site_legal):
            raise HTTPException(422, "organization_unit_outside_document_scope")
    return scope


def _simplified_scope(db, actor, body):
    """The simple picker exposes only departments, within the current HR area."""
    all_employees = body.get("audience_all")
    if not isinstance(all_employees, bool):
        raise HTTPException(422, "audience_all_must_be_boolean")
    submitted = body.get("scope") or {}
    if not isinstance(submitted, dict) or set(submitted) - {"org_unit_ids"}:
        raise HTTPException(422, "simple_audience_accepts_only_organization_units")
    selected = submitted.get("org_unit_ids") or []
    if not all_employees and not selected:
        raise HTTPException(422, "organization_unit_required")
    grant = actor.scope or {}
    scope = {"employment_stages": ["PREBOARDING", "ACTIVE"]}
    if grant.get("all") is not True:
        for key in ("site_ids", "legal_entity_ids"):
            if grant.get(key):
                scope[key] = copy.deepcopy(grant[key])
    if not all_employees:
        scope["org_unit_ids"] = selected
    return _validate_org_scope(db, actor, _validate_scope(scope))


def _knowledge_write_lock(db):
    # Short write sections only. Workers release this transaction before local
    # model/OCR HTTP calls; the job fence always precedes this advisory lock.
    if db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(76810109)"))


def _doc(db, actor, id, edit=False):
    if edit:
        _knowledge_write_lock(db)
    entity = get_entity(db, id, "document")
    if edit:
        db.refresh(entity)
    if entity.status in {"PURGE_PENDING", "PURGED", "TOMBSTONED"} or entity.data.get("source_revoked"):
        raise HTTPException(404, "document_not_found")
    if edit:
        require_role(actor, *EDIT_ROLES)
    employee = db.get(Entity, actor.employee_id) if getattr(actor, "employee_id", None) else None
    if not _scope_access(actor, entity, employee, write=edit):
        raise HTTPException(404, "document_not_found")
    return entity


def _revision(db, actor, id, edit=False):
    if edit:
        _knowledge_write_lock(db)
    revision = get_entity(db, id, "document_revision")
    if edit:
        db.refresh(revision)
    doc = _doc(db, actor, revision.parent_id, edit)
    if actor.role == "EMPLOYEE" and (revision.status != "PUBLISHED" or doc.data.get("active_revision_id") != revision.id or not is_effective(revision.data)):
        raise HTTPException(404, "revision_not_found")
    return revision, doc


def _result(entity, response=None):
    if response is not None:
        response.headers["ETag"] = etag(entity)
    return {**serialize(entity), **({"owner_id": entity.owner_id} if entity.kind == "document" else {})}


def _pages(items):
    return {"items": [_result(i) for i in items], "next_cursor": None, "total": len(items)}


def _head(db):
    _knowledge_write_lock(db)
    head = db.scalar(select(Entity).where(Entity.kind == "knowledge_head").with_for_update())
    if head:
        db.refresh(head, with_for_update=True)
    if not head:
        snapshot = save_entity(db, "knowledge_snapshot", {"members": {}, "reason": "Initial empty corpus"}, status="PUBLISHED")
        db.flush()
        head = save_entity(db, "knowledge_head", {"snapshot_id": snapshot.id, "authz_epoch": 1})
        db.flush()
    return head


def _extraction(db, revision, pin=None):
    current = revision.data.get("latest_extraction_revision_id")
    if not current:
        raise HTTPException(409, "extraction_not_ready")
    extraction = get_entity(db, pin or current, "extraction")
    if extraction.parent_id != revision.id:
        raise HTTPException(404, "extraction_not_found")
    return extraction


def _mutable_revision(revision):
    if revision.status not in {"REVIEW_REQUIRED", "FAILED"}:
        raise HTTPException(409, "revision_is_not_editable")


def _new_extraction(db, revision, payload, actor=None):
    if revision.data.get("simple_workflow"):
        from .knowledge_simple import normalize
        payload = normalize(payload)
    extraction = save_entity(db, "extraction", copy.deepcopy(payload), parent_id=revision.id, status="IMMUTABLE")
    db.flush()
    fingerprint = digest({"pages": payload.get("pages", []), "blocks": payload.get("blocks", [])})
    _update(revision, latest_extraction_revision_id=extraction.id, latest_checkpoint_extraction_revision_id=extraction.id,
            content_fingerprint=fingerprint, candidate_snapshot_id=None, qa_artifact_id=None,
            review_fingerprint=None, impact_review_id=None)
    if revision.status == "APPROVED":
        revision.status = "REVIEW_REQUIRED"
    if revision.data.get("simple_workflow"):
        _update(revision, simple_approval=None, simple_preparation_pin=None, simple_error=None,
            latest_candidate_operation_id=None, latest_qa_operation_id=None)
        from .knowledge_simple import schedule
        schedule(db, revision, actor, "extraction")
    return extraction


def _require_pin(body, revision, extraction):
    expected = body.get("expected_extraction_revision_id")
    if expected and expected != extraction.id:
        raise HTTPException(409, "stale_extraction")
    expected_content = body.get("expected_content_fingerprint")
    if expected_content and expected_content != revision.data.get("content_fingerprint"):
        raise HTTPException(409, "stale_content_fingerprint")


def _operation(db, kind, result=None, status="SUCCEEDED"):
    return save_entity(db, "operation", {"type": kind, "result": result or {}, **(result or {})}, status=status)


@router.get("/knowledge/documents")
def list_documents(state: str | None = None, q: str | None = None, actor: Actor = Depends(get_actor), db=Depends(get_db)):
    query = select(Entity).where(Entity.kind == "document")
    if state:
        query = query.where(Entity.status == state)
    employee = db.get(Entity, actor.employee_id) if getattr(actor, "employee_id", None) else None
    items = [d for d in db.scalars(query.order_by(Entity.updated_at.desc())).all()
             if _scope_access(actor, d, employee) and (not q or q.lower() in d.data.get("title", "").lower())]
    result = _pages(items)
    for item in result["items"]:
        latest = db.get(Entity, item.get("latest_revision_id")) if item.get("latest_revision_id") else None
        if latest:
            item.update(latest_revision_status=latest.status, latest_ingest_operation_id=latest.data.get("latest_ingest_operation_id"),
                processing_error=latest.data.get("processing_error"), page_count=latest.data.get("page_count"),
                latest_candidate_operation_id=latest.data.get("latest_candidate_operation_id"), latest_qa_operation_id=latest.data.get("latest_qa_operation_id"))
    return result


@router.post("/knowledge/documents", status_code=201)
def create_document(body: dict, response: Response, actor: Actor = Depends(get_actor), db=Depends(get_db)):
    require_role(actor, *EDIT_ROLES)
    title = str(body.get("title", "")).strip()
    if not title or len(title) > 500:
        raise HTTPException(422, "title_required")
    simplified = "audience_all" in body
    scope = _simplified_scope(db, actor, body) if simplified else _validate_org_scope(db, actor, _validate_scope(body.get("scope") or {}))
    category = body.get("document_category", "OTHER")
    if category not in {"REGULATION", "INSTRUCTION", "OVERVIEW", "MAP", "OTHER"}:
        raise HTTPException(422, "invalid_document_category")
    draft = Entity(kind="document", id=new_id(), data={"title": title, "scope": scope})
    if not _scope_access(actor, draft, write=True):
        raise HTTPException(403, "scope_not_granted")
    owner_id = actor.id
    if "owner_id" in body:
        from .knowledge_ownership import validate_document_owner
        owner_id = validate_document_owner(db, actor, draft, body["owner_id"])
    entity = save_entity(db, "document", {"title": title, "description": str(body.get("description", ""))[:4000],
                         "scope": scope, "document_category": category, "active_revision_id": None, "authz_epoch": 1,
                         **({"simple_workflow": True} if simplified else {})}, status="DRAFT", owner_id=owner_id)
    audit(db, actor, "knowledge.document.created", entity.id, {"owner_id": owner_id})
    commit(db)
    return _result(entity, response)


@router.get("/knowledge/documents/{document_id}")
def get_document(document_id: str, response: Response, actor: Actor = Depends(get_actor), db=Depends(get_db)):
    return _result(_doc(db, actor, document_id), response)


@router.patch("/knowledge/documents/{document_id}")
def update_document(document_id: str, body: dict, request: Request, response: Response, actor: Actor = Depends(get_actor), db=Depends(get_db)):
    doc = _doc(db, actor, document_id, True)
    check_etag(request, doc)
    allowed = {k: v for k, v in body.items() if k in {"title", "description", "scope", "document_category"}}
    if "scope" in allowed:
        allowed["scope"] = _validate_org_scope(db, actor, _validate_scope(allowed["scope"]))
        if doc.status == "ACTIVE" and not _scope_subset(allowed["scope"], doc.data.get("scope", {})):
            raise HTTPException(409, "scope_expansion_requires_archive_and_new_revision_review")
        probe = copy.copy(doc)
        probe.data = {**doc.data, "scope": allowed["scope"]}
        if not _scope_access(actor, probe, write=True):
            raise HTTPException(403, "scope_not_granted")
        allowed["authz_epoch"] = doc.data.get("authz_epoch", 1) + 1
    previous_owner_id = doc.owner_id
    if "owner_id" in body and body["owner_id"] != previous_owner_id:
        from .knowledge_ownership import validate_document_owner
        proposed = Entity(kind="document", id=doc.id, data={**doc.data, **allowed}, status=doc.status, owner_id=doc.owner_id)
        doc.owner_id = validate_document_owner(db, actor, proposed, body["owner_id"], reason=body.get("reason"), changing=True)
    elif "scope" in allowed:
        from .knowledge_ownership import validate_current_document_owner
        proposed = Entity(kind="document", id=doc.id, data={**doc.data, **allowed}, status=doc.status, owner_id=doc.owner_id)
        validate_current_document_owner(db, actor, proposed)
    _update(doc, **allowed)
    if "scope" in allowed or "document_category" in allowed:
        for draft in db.scalars(select(Entity).where(Entity.kind == "document_revision", Entity.parent_id == doc.id, Entity.status.in_(["APPROVED", "REVIEW_REQUIRED"]))):
            if draft.status == "APPROVED":
                draft.status = "REVIEW_REQUIRED"
            _update(draft, candidate_snapshot_id=None, qa_artifact_id=None, review_fingerprint=None, impact_review_id=None)
    audit(db, actor, "knowledge.document.updated", doc.id, {"fields": list(allowed), "previous_owner_id": previous_owner_id, "owner_id": doc.owner_id}, reason=body.get("reason"))
    commit(db)
    return _result(doc, response)


@router.get("/knowledge/documents/{document_id}/revisions")
def list_revisions(document_id: str, actor: Actor = Depends(get_actor), db=Depends(get_db)):
    _doc(db, actor, document_id, True)
    return _pages(db.scalars(select(Entity).where(Entity.kind == "document_revision", Entity.parent_id == document_id).order_by(Entity.created_at.desc())).all())


@router.post("/knowledge/documents/{document_id}/revisions", status_code=202)
async def upload_revision(document_id: str, request: Request, response: Response, file: UploadFile = File(...),
                          reason: str = Form("Загрузка документа"), effective_from: str = Form(""),
                          effective_to: str = Form(""), expected_sha256: str = Form(""),
                          force_new_version: bool = Form(False), simple_workflow: bool = Form(False),
                          actor: Actor = Depends(get_actor), db=Depends(get_db)):
    doc = _doc(db, actor, document_id, True)
    check_etag(request, doc)
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED:
        raise HTTPException(415, "unsupported_media_type")
    revision_id = new_id()
    folder = DATA / "knowledge" / revision_id
    source = folder / ("source" + suffix)
    h, count = hashlib.sha256(), 0
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with source.open("wb") as dest:
            while part := await file.read(1024 * 1024):
                count += len(part)
                if count > MAX_BYTES:
                    raise HTTPException(413, "file_limit_exceeded")
                h.update(part)
                dest.write(part)
        if expected_sha256 and h.hexdigest() != expected_sha256:
            raise HTTPException(422, "source_digest_mismatch")
        # MIME sniffing prevents renamed executables and corrupted file containers.
        header = source.read_bytes()[:8]
        if suffix == ".pdf" and not header.startswith(b"%PDF-") or suffix == ".pptx" and not header.startswith(b"PK"):
            raise HTTPException(415, "file_signature_mismatch")
        prior = db.scalars(select(Entity).where(Entity.kind == "document_revision", Entity.parent_id == doc.id)).all()
        duplicate = next((r for r in prior if not force_new_version and doc.status != "ARCHIVED"
                          and r.data.get("sha256") == h.hexdigest()
                          and r.data.get("scope_fingerprint") == digest(doc.data.get("scope", {})) and r.status != "REJECTED"
                          and (not effective_from or r.data.get("effective_from") == effective_from)
                          and (not effective_to or r.data.get("effective_to") == effective_to)), None)
        if duplicate:
            source.unlink(missing_ok=True)
            return {"revision": serialize(duplicate), "duplicate": True, "operation": None}
        effective_from, effective_to = _effective_dates(effective_from, effective_to)
        revision = save_entity(db, "document_revision", {"document_id": doc.id, "filename": Path(file.filename or "source").name,
                    "source_path": str(source), "sha256": h.hexdigest(), "bytes": count, "reason": reason[:2000],
                    "metadata_snapshot": _document_metadata_snapshot(doc),
                    "effective_from": effective_from, "effective_to": effective_to or None, "revision_kind": "FILE", "scope_fingerprint": digest(doc.data.get("scope", {})),
                    "latest_extraction_revision_id": None, "content_fingerprint": None,
                    **({"simple_workflow": True} if simple_workflow or doc.data.get("simple_workflow") else {})},
                    status="UPLOADED", parent_id=doc.id, owner_id=actor.id, id=revision_id)
        op = _operation(db, "KNOWLEDGE_INGEST", {"revision_id": revision.id}, "PENDING")
        _update(revision, operation_id=op.id, latest_ingest_operation_id=op.id)
        enqueue(db, "KNOWLEDGE_INGEST", {"revision_id": revision.id, "operation_id": op.id}, f"ingest:{revision.id}:1")
        _update(doc, latest_revision_id=revision.id)
        audit(db, actor, "knowledge.revision.uploaded", revision.id, {"sha256": h.hexdigest(), "bytes": count}, reason=reason)
        commit(db)
        response.headers["ETag"] = etag(doc)
        return {"revision": serialize(revision), "operation": serialize(op)}
    except OSError as exc:
        db.rollback()
        try:
            source.unlink(missing_ok=True)
            folder.rmdir()
        except OSError:
            pass
        if exc.errno in {errno.ENOSPC, errno.EDQUOT}:
            raise HTTPException(507,{"code":"storage_full","message":"Недостаточно места для нового документа. Действующая база знаний сохранена; обратитесь к администратору."}) from exc
        raise
    except Exception:
        db.rollback()
        if not db.get(Entity, revision_id):
            try:
                source.unlink(missing_ok=True)
                folder.rmdir()
            except OSError:
                pass
        raise


@router.post("/knowledge/documents/{document_id}/revisions/article", status_code=201)
def create_article(document_id: str, body: dict, request: Request, response: Response, actor: Actor = Depends(get_actor), db=Depends(get_db)):
    doc = _doc(db, actor, document_id, True)
    check_etag(request, doc)
    sections = body.get("sections", [])
    rendered_sections = []
    for section in sections:
        rendered_sections.append(str(section.get("heading", "")))
        rendered_sections.extend(str(p) for p in section.get("paragraphs", []))
        if section.get("table"):
            rendered_sections.append(" | ".join(section["table"].get("columns", [])))
            rendered_sections.extend(" | ".join(str(c) for c in row) for row in section["table"].get("rows", []))
        rendered_sections.extend(str(faq.get("question", "")) + "\n" + str(faq.get("answer", "")) for faq in section.get("faq", []))
    instruction = body.get("instruction") or {}
    text = str(instruction.get("answer_text") or body.get("text") or body.get("content") or body.get("body_markdown") or "\n\n".join(rendered_sections)).strip()
    if body.get("article_kind") == "APPROVED_INSTRUCTION" and (not instruction or not body.get("dependencies")):
        raise HTTPException(422, "instruction_and_evidence_dependencies_required")
    if not text or len(text) > 100000:
        raise HTTPException(422, "article_text_required")
    effective_from, effective_to = _effective_dates(body.get("effective_from"), body.get("effective_to"))
    revision = save_entity(db, "document_revision", {"document_id": doc.id, "revision_kind": "ARTICLE", "sha256": digest(text.encode()),
        "metadata_snapshot": _document_metadata_snapshot(doc),
        "effective_from": effective_from, "effective_to": effective_to,
        "reason": body.get("reason", "Инструкция HR"), "scope_fingerprint": digest(doc.data.get("scope", {})), "dependencies": body.get("dependencies", []), "article_kind": body.get("article_kind", "GENERAL"), "instruction": instruction, "origin_gap_id": body.get("origin_gap_id")},
        status="REVIEW_REQUIRED", parent_id=doc.id, owner_id=actor.id)
    db.flush()
    from .parsing import block
    _new_extraction(db, revision, {"pages": [{"page_no": 1, "status": "PROCESSED", "review_status": "PENDING", "issues": []}],
                   "blocks": [block("TEXT", text, 1, origin="HR_AUTHORED")]})
    _update(doc, latest_revision_id=revision.id)
    audit(db, actor, "knowledge.article.created", revision.id, reason=body.get("reason"))
    commit(db)
    return _result(revision, response)


@router.get("/knowledge/revisions/{revision_id}")
def get_revision(revision_id: str, response: Response, actor: Actor = Depends(get_actor), db=Depends(get_db)):
    revision, _ = _revision(db, actor, revision_id)
    return _result(revision, response)


@router.get("/knowledge/revisions/{revision_id}/extraction")
def get_extraction(revision_id: str, response: Response, extraction_revision_id: str | None = None,
                   page_from: int = 1, limit: int = 500, actor: Actor = Depends(get_actor), db=Depends(get_db)):
    revision, _ = _revision(db, actor, revision_id, True)
    extraction = _extraction(db, revision, extraction_revision_id)
    response.headers["ETag"] = etag(extraction)
    rows = [{**b, "block_id": b["id"], "type": b["kind"]} for b in extraction.data["blocks"] if b["page_no"] >= page_from]
    return {"extraction_revision_id": extraction.id, "items": rows[:min(limit, 2000)], "blocks": rows[:min(limit, 2000)],
            "pages": extraction.data["pages"], "next_cursor": None, "content_fingerprint": revision.data.get("content_fingerprint")}


@router.get("/knowledge/revisions/{revision_id}/compare")
def compare_revisions(revision_id: str, base_revision_id: str, extraction_revision_id: str | None = None,
                      base_extraction_revision_id: str | None = None, actor: Actor = Depends(get_actor), db=Depends(get_db)):
    """Bounded physical-page comparison; never infers legal or semantic impact."""
    require_role(actor, *EDIT_ROLES)
    def authorized(id):
        revision = db.scalar(select(Entity).where(Entity.id == id, Entity.kind == "document_revision").execution_options(populate_existing=True))
        document = db.scalar(select(Entity).where(Entity.id == revision.parent_id, Entity.kind == "document").execution_options(populate_existing=True)) if revision else None
        if not document or document.status in {"PURGE_PENDING", "PURGED", "TOMBSTONED"} or document.data.get("source_revoked") or not _scope_access(actor, document, write=True):
            raise HTTPException(404, "revision_not_found")
        return revision, document
    target, doc = authorized(revision_id)
    base, base_doc = authorized(base_revision_id)
    if base_doc.id != doc.id:
        raise HTTPException(422, "comparison_requires_same_document")
    before, after = _extraction(db, base, base_extraction_revision_id), _extraction(db, target, extraction_revision_id)
    if before.status != "IMMUTABLE" or after.status != "IMMUTABLE":
        raise HTTPException(409, "comparison_requires_immutable_extractions")
    return _revision_comparison(base, target, before, after, doc)


def _revision_comparison(base, target, before, after, doc):
    from difflib import SequenceMatcher
    import re
    limits = {"pages": 60, "blocks_per_extraction": 1000, "text_chars_per_page_side": 4000,
              "tables_per_page": 10, "figures_per_page": 20, "grid_rows": 100, "grid_columns": 30,
              "cell_chars": 300, "response_bytes": 524288}
    truncated = set()
    def clip(text, limit, reason):
        text = str(text or "")
        if len(text) > limit:
            truncated.add(reason)
        return text[:limit]
    def state(a, b):
        return "ADDED" if a is None else "REMOVED" if b is None else "UNCHANGED" if a == b else "CHANGED"
    def meta_value(value):
        if len(json.dumps(value, ensure_ascii=False)) > 2000:
            truncated.add("METADATA_VALUE_LIMIT")
            return json.dumps(value, ensure_ascii=False)[:2000]
        return value
    fields = ("filename", "sha256", "revision_kind", "article_kind", "effective_from", "effective_to", "scope_fingerprint")
    metadata = [{"field": key, "before": meta_value(base.data.get(key)), "after": meta_value(target.data.get(key)),
                 "status": state(base.data.get(key), target.data.get(key))} for key in fields
                if key in base.data or key in target.data]
    unavailable = []
    for key in ("title", "scope", "document_category"):
        snapshots = [row.data.get("metadata_snapshot") for row in (base, target)]
        known = [isinstance(snapshot, dict) and key in snapshot for snapshot in snapshots]
        values = [snapshot.get(key) if available else None for snapshot, available in zip(snapshots, known)]
        if not all(known):
            unavailable.append(key)
        metadata.append({"field": key, "before": meta_value(values[0]), "after": meta_value(values[1]),
                         "status": state(*values) if all(known) else "HISTORY_UNAVAILABLE", "historical_available": {"base": known[0], "target": known[1]}})
    def side(extraction):
        pages = {p["page_no"]: p for p in extraction.data.get("pages", [])}
        rows = extraction.data.get("blocks", [])
        if len(rows) > limits["blocks_per_extraction"]:
            truncated.add("BLOCK_COUNT_LIMIT")
        grouped = {}
        for block in rows[:limits["blocks_per_extraction"]]:
            grouped.setdefault(block["page_no"], []).append(block)
        for blocks in grouped.values():
            blocks.sort(key=lambda block: block.get("reading_order", 0))
        return pages, grouped
    old_pages, old_blocks = side(before)
    new_pages, new_blocks = side(after)
    numbers = sorted(set(old_pages) | set(new_pages))
    result = {"base": {"revision_id": base.id, "extraction_revision_id": before.id, "filename": base.data.get("filename")},
              "target": {"revision_id": target.id, "extraction_revision_id": after.id, "filename": target.data.get("filename")},
              "comparison": {"alignment": "PHYSICAL_PAGE_AND_KIND_READING_ORDER_POSITION", "semantic_diff": False,
                  "page_movement_detection": False, "random_block_ids_used_for_alignment": False},
              "metadata": metadata, "metadata_history_unavailable": unavailable,
              "current_document_metadata": {**{key: meta_value(value) for key, value in _document_metadata_snapshot(doc).items()}, "is_historical": False},
              "pages": [], "limits": limits,
              "summary": {"physical_pages_available": len(numbers), "pages_compared": 0, "pages_added": 0, "pages_removed": 0, "pages_changed": 0,
                  "text_pages_changed": 0, "tables_added": 0, "tables_removed": 0, "tables_changed": 0,
                  "figures_added": 0, "figures_removed": 0, "figures_changed": 0, "unavailable_pages": 0,
                  "metadata_fields_changed": sum(m["status"] in {"ADDED", "REMOVED", "CHANGED"} for m in metadata)},
              "limitations": ["Сопоставление по физической странице, типу и порядку. Перенос страницы/вставка блока может менять пары; это не смысловое или юридическое заключение.",
                  "Неизвлечённые страницы и ограничения объёма не подтверждают отсутствие изменений. Для решения сверяйте полные оригиналы и зависимости.",
                  "Текущие метаданные документа отделены от исторических снимков редакций; отсутствующая история не восстанавливается догадкой."]}
    budget = len(json.dumps(result, ensure_ascii=False).encode()) + 3000
    for number in numbers[:limits["pages"]]:
        a, b = old_pages.get(number), new_pages.get(number)
        old, new = old_blocks.get(number, []), new_blocks.get(number, [])
        complete = all(p is None or p.get("status") == "PROCESSED" for p in (a, b))
        text_a = "\n\n".join(row.get("text", "") for row in old if row.get("kind") == "TEXT") if a else None
        text_b = "\n\n".join(row.get("text", "") for row in new if row.get("kind") == "TEXT") if b else None
        shown_a, shown_b = clip(text_a, 4000, "TEXT_CHAR_LIMIT"), clip(text_b, 4000, "TEXT_CHAR_LIMIT")
        changes = []
        for tag, i, j, k, m in SequenceMatcher(None, shown_a.splitlines(), shown_b.splitlines(), autojunk=True).get_opcodes():
            if tag == "equal":
                continue
            old_lines, new_lines = shown_a.splitlines()[i:j], shown_b.splitlines()[k:m]
            combined = " ".join(old_lines + new_lines)
            changes.append({"kind": tag.upper(), "before": "\n".join(old_lines), "after": "\n".join(new_lines),
                            "contains_numbers": bool(re.search(r"\d", combined)), "contains_negation": bool(re.search(r"\b(?:не|нет|нельзя|запрещ[а-я]*)\b", combined.casefold()))})
        row = {"page_no": number, "status": state(a is not None or None, b is not None or None), "comparison_complete": complete,
               "base_page_status": a.get("status") if a else None, "target_page_status": b.get("status") if b else None,
               "before_render_sha256": a.get("render_sha256") if a else None, "after_render_sha256": b.get("render_sha256") if b else None,
               "text": {"status": state(text_a, text_b) if complete else "UNAVAILABLE", "before": shown_a, "after": shown_b,
                        "before_sha256": digest(text_a.encode()) if text_a is not None else None, "after_sha256": digest(text_b.encode()) if text_b is not None else None,
                        "truncated": len(text_a or "") > 4000 or len(text_b or "") > 4000, "changes": changes}, "tables": [], "figures": []}
        for kind, name in (("TABLE", "tables"), ("FIGURE", "figures")):
            sides = [[block for block in blocks if block.get("kind") == kind] for blocks in (old, new)]
            maximum = limits[name + "_per_page"]
            if max(map(len, sides)) > maximum:
                truncated.add(name.upper() + "_COUNT_LIMIT")
            for index in range(min(max(map(len, sides)), maximum)):
                pair = [blocks[index] if index < len(blocks) else None for blocks in sides]
                def material(block):
                    if block is None:
                        return None
                    common = {key: block.get(key) for key in ("bbox", "disposition")}
                    if kind == "FIGURE":
                        return {**common, **{key: block.get(key) for key in ("image_sha256", "description", "description_image_sha256")}}
                    return {**common, "grid": block.get("grid", []), "text": block.get("text", "")}
                materials = [material(block) for block in pair]
                item = {"position": index + 1, "status": state(*materials) if complete else "UNAVAILABLE"}
                for prefix, value in zip(("before", "after"), materials):
                    if kind == "FIGURE":
                        item[prefix + "_image_sha256"] = value.get("image_sha256") if value else None
                        item[prefix + "_description"] = clip(value.get("description"), 2000, "FIGURE_DESCRIPTION_LIMIT") if value else None
                    else:
                        grid = value.get("grid", []) if value else []
                        if len(grid) > limits["grid_rows"] or any(len(cells) > limits["grid_columns"] for cells in grid):
                            truncated.add("TABLE_GRID_LIMIT")
                        item[prefix + "_grid"] = [[clip(cell, limits["cell_chars"], "TABLE_CELL_LIMIT") for cell in cells[:limits["grid_columns"]]] for cells in grid[:limits["grid_rows"]]]
                        item[prefix + "_text"] = clip(value.get("text"), 2000, "TABLE_TEXT_LIMIT") if value else None
                item["alignment_ambiguous"] = len(sides[0]) != len(sides[1])
                row[name].append(item)
        if row["status"] == "UNCHANGED" and (row["text"]["status"] == "CHANGED" or any(item["status"] != "UNCHANGED" for name in ("tables", "figures") for item in row[name]) or row["before_render_sha256"] != row["after_render_sha256"]):
            row["status"] = "CHANGED"
        if not complete:
            row["status"] = "UNAVAILABLE"
        size = len(json.dumps(row, ensure_ascii=False).encode())
        if budget + size > limits["response_bytes"]:
            truncated.add("RESPONSE_BYTE_LIMIT")
            break
        budget += size
        result["pages"].append(row)
        summary = result["summary"]
        summary["pages_compared"] += 1
        for status in ("ADDED", "REMOVED", "CHANGED"):
            summary["pages_" + status.lower()] += row["status"] == status
        summary["text_pages_changed"] += row["text"]["status"] in {"ADDED", "REMOVED", "CHANGED"}
        summary["unavailable_pages"] += not complete
        for name in ("tables", "figures"):
            for status in ("ADDED", "REMOVED", "CHANGED"):
                summary[name + "_" + status.lower()] += sum(item["status"] == status for item in row[name])
    if len(numbers) > limits["pages"]:
        truncated.add("PHYSICAL_PAGE_LIMIT")
    result.update(truncated=bool(truncated), truncation_reasons=sorted(truncated))
    return result


@router.get("/knowledge/revisions/{revision_id}/pages")
def list_pages(revision_id: str, response: Response, extraction_revision_id: str | None = None, actor: Actor = Depends(get_actor), db=Depends(get_db)):
    revision, _ = _revision(db, actor, revision_id, True)
    extraction = _extraction(db, revision, extraction_revision_id)
    response.headers["ETag"] = etag(extraction)
    return {"extraction_revision_id": extraction.id, "items": [{**p, "preview_url": f"/api/v1/knowledge/revisions/{revision_id}/pages/{p['page_no']}/preview" if p.get("preview_file") else None} for p in extraction.data["pages"]], "next_cursor": None,
            "physical_page_count": len(extraction.data["pages"]), "reviewed_pages": sum(p.get("review_status") == "REVIEWED" for p in extraction.data["pages"])}


@router.get("/knowledge/revisions/{revision_id}/pages/{page_no}/preview")
def page_preview(revision_id: str, page_no: int, actor: Actor = Depends(get_actor), db=Depends(get_db)):
    revision, _ = _revision(db, actor, revision_id, True)
    extraction = _extraction(db, revision)
    return _page_preview_response(revision, extraction, page_no)


def _page_preview_response(revision, extraction, page_no):
    """File boundary only; caller must authorize the exact revision first."""
    if extraction.kind != "extraction" or extraction.parent_id != revision.id:
        raise HTTPException(404, "preview_not_found")
    page = next((p for p in extraction.data["pages"] if p["page_no"] == page_no), None)
    if not page or not page.get("preview_file"):
        raise HTTPException(404, "preview_unavailable_use_source")
    folder = DATA / "knowledge" / revision.id / "previews"
    path = (folder / page["preview_file"]).resolve()
    if not path.is_relative_to(folder.resolve()) or not path.exists():
        raise HTTPException(404, "preview_not_found")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.get("/knowledge/revisions/{revision_id}/source")
def get_source(revision_id: str, actor: Actor = Depends(get_actor), db=Depends(get_db)):
    revision, _ = _revision(db, actor, revision_id)
    source = revision.data.get("source_path")
    if not source or not Path(source).exists():
        raise HTTPException(404, "source_not_found")
    # source_path is generated at ingest, never accepted from request.
    return FileResponse(source, filename=revision.data.get("filename", "source"), media_type="application/pdf" if str(source).lower().endswith(".pdf") else None, content_disposition_type="inline" if str(source).lower().endswith(".pdf") else "attachment", headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.patch("/knowledge/revisions/{revision_id}/extraction/blocks/{block_id}")
def correct_block(revision_id: str, block_id: str, body: dict, request: Request, response: Response,
                  actor: Actor = Depends(get_actor), db=Depends(get_db)):
    revision, _ = _revision(db, actor, revision_id, True)
    _mutable_revision(revision)
    extraction = _extraction(db, revision)
    check_etag(request, extraction)
    _require_pin(body, revision, extraction)
    payload = copy.deepcopy(extraction.data)
    target = next((b for b in payload["blocks"] if b["id"] == block_id), None)
    if not target:
        raise HTTPException(404, "block_not_found")
    kind = body.get("patch_type", "TEXT")
    reason = str(body.get("reason", "")).strip()
    if len(reason) < 3:
        raise HTTPException(422, "correction_reason_required")
    if kind == "TEXT":
        if target["kind"] != "TEXT":
            raise HTTPException(422, "text_correction_requires_text_block")
        text = body.get("text", body.get("corrected_text"))
        if not isinstance(text, str) or not text.strip() or len(text) > 200000:
            raise HTTPException(422, "invalid_corrected_text")
        target["text"] = text
    elif kind == "TABLE":
        if target["kind"] != "TABLE":
            raise HTTPException(422, "table_correction_requires_table_block")
        grid = body.get("grid")
        if not grid or len(grid) > 500 or any(not isinstance(r, list) or len(r) > 100 for r in grid):
            raise HTTPException(422, "invalid_table_grid")
        target["grid"] = [[str(c)[:10000] for c in r] for r in grid]
        target["text"] = "\n".join(" | ".join(r) for r in target["grid"])
    elif kind == "FIGURE_DESCRIPTION":
        if target["kind"] != "FIGURE":
            raise HTTPException(422, "figure_required")
        description = str(body.get("description", "")).strip()
        if not description:
            raise HTTPException(422, "description_required")
        target["description"] = description[:50000]
        target["description_origin"] = "HR_AUTHORED"
        target["description_image_sha256"] = target.get("image_sha256")
        if revision.data.get("simple_workflow") and target.get("disposition_origin") == "AUTO_REFERENCE_ONLY":
            target["disposition"] = "INCLUDE"
            target["disposition_origin"] = "HUMAN_DESCRIPTION"
    elif kind == "DISPOSITION":
        disposition = body.get("disposition")
        if disposition not in {"INCLUDE", "REFERENCE_ONLY", "EXCLUDE_NONCONTENT"}:
            raise HTTPException(422, "invalid_disposition")
        if disposition == "EXCLUDE_NONCONTENT" and target.get("kind") in {"TABLE", "FIGURE"}:
            raise HTTPException(422, "critical_content_cannot_be_excluded")
        target["disposition"] = disposition
        if revision.data.get("simple_workflow"):
            target["disposition_origin"] = "HUMAN"
    elif kind == "READING_ORDER":
        ids = body.get("ordered_block_ids", [])
        same_page = [b for b in payload["blocks"] if b["page_no"] == target["page_no"]]
        if set(ids) != {b["id"] for b in same_page} or len(ids) != len(set(ids)):
            raise HTTPException(422, "reading_order_must_cover_page")
        positions = {id: n for n, id in enumerate(ids)}
        for b in same_page:
            b["reading_order"] = positions[b["id"]]
        payload["blocks"].sort(key=lambda b: (b["page_no"], b.get("reading_order", 0)))
    else:
        raise HTTPException(422, "unknown_patch_type")
    target["review_status"] = "REVIEWED"
    target["corrected_by"] = actor.id
    target["correction_reason"] = reason
    target["correction_origin"] = "HUMAN"
    if kind != "DISPOSITION":
        target["issues"] = []
    if revision.data.get("revision_kind") == "ARTICLE" and revision.data.get("article_kind") == "APPROVED_INSTRUCTION":
        canonical = "\n\n".join(b.get("text", "") for b in sorted(payload["blocks"], key=lambda b: (b["page_no"], b.get("reading_order", 0)))
            if b.get("kind") == "TEXT" and b.get("disposition") not in {"REFERENCE_ONLY", "EXCLUDE_NONCONTENT"}).strip()
        if not canonical:
            raise HTTPException(422, "approved_instruction_requires_answer_text")
        _update(revision, instruction={**revision.data.get("instruction", {}), "answer_text": canonical})
    new = _new_extraction(db, revision, payload, actor)
    audit(db, actor, "knowledge.extraction.corrected", revision.id, {"block_id": block_id, "type": kind, "reason": reason})
    commit(db)
    response.headers["ETag"] = etag(new)
    return {"revision": serialize(revision), "extraction_revision_id": new.id, "block": target}


@router.patch("/knowledge/revisions/{revision_id}/pages/{page_no}/review")
def review_page(revision_id: str, page_no: int, body: dict, request: Request, response: Response,
                actor: Actor = Depends(get_actor), db=Depends(get_db)):
    revision, _ = _revision(db, actor, revision_id, True)
    _mutable_revision(revision)
    extraction = _extraction(db, revision)
    check_etag(request, extraction)
    _require_pin(body, revision, extraction)
    payload = copy.deepcopy(extraction.data)
    page = next((p for p in payload["pages"] if p["page_no"] == page_no), None)
    if not page:
        raise HTTPException(404, "page_not_found")
    action = body.get("action", "REVIEW_CONTENT")
    reason = str(body.get("reason", "")).strip()
    if len(reason) < 3:
        raise HTTPException(422, "review_reason_required")
    blocks = [b for b in payload["blocks"] if b["page_no"] == page_no]
    if page["status"] != "PROCESSED":
        raise HTTPException(409, "failed_or_pending_page_cannot_be_reviewed")
    if action == "CONFIRM_BLANK":
        if any(b.get("text", "").strip() or b["kind"] in {"TABLE", "FIGURE"} for b in blocks):
            raise HTTPException(422, "meaningful_content_not_blank")
        page["blank_confirmed"] = True
        page["issues"] = []
    elif action == "EXCLUDE_NONCONTENT":
        if any(b.get("text", "").strip() or b["kind"] in {"TABLE", "FIGURE"} for b in blocks):
            raise HTTPException(422, "meaningful_content_cannot_be_excluded")
        page["disposition"] = "EXCLUDE_NONCONTENT"
        page["issues"] = []
    elif action != "REVIEW_CONTENT":
        raise HTTPException(422, "invalid_page_action")
    unresolved = []
    for b in blocks:
        if b["kind"] == "FIGURE" and b.get("disposition") != "REFERENCE_ONLY" and not b.get("description"):
            unresolved.append("FIGURE_DESCRIPTION_REQUIRED")
        if b["kind"] == "TABLE" and b.get("review_status") != "REVIEWED":
            unresolved.append("TABLE_STRUCTURE_REVIEW")
    # HR confirms the OCR/transcribed content against original. This does not
    # waive missing/failed/figure/table blockers.
    page["review_status"] = "REVIEWED"
    page["reviewed_by"] = actor.id
    page["review_reason"] = reason
    page["issues"] = sorted(set(unresolved + [x for x in page.get("issues", []) if x in {"NO_READABLE_TEXT", "SMARTART_OR_CHART_MANUAL_REVIEW"}]))
    for b in blocks:
        if b["kind"] == "TEXT":
            b["review_status"] = "REVIEWED"
            b["issues"] = []
    new = _new_extraction(db, revision, payload, actor)
    audit(db, actor, "knowledge.page.reviewed", revision.id, {"page_no": page_no, "action": action, "reason": reason})
    commit(db)
    response.headers["ETag"] = etag(new)
    return {"extraction_revision_id": new.id, "page": page, "revision": serialize(revision)}


def _blockers(extraction, described_figures=None):
    described_figures = set(described_figures or [])
    result = []
    if not extraction.data.get("pages"):
        result.append("NO_PHYSICAL_PAGES")
    for page in extraction.data.get("pages", []):
        if page.get("status") != "PROCESSED":
            result.append(f"PAGE_{page['page_no']}_NOT_PROCESSED")
        if page.get("review_status") != "REVIEWED":
            result.append(f"PAGE_{page['page_no']}_NOT_REVIEWED")
        if page.get("issues"):
            result.extend(f"PAGE_{page['page_no']}:{i}" for i in page["issues"] if i not in {"FIGURE_DESCRIPTION_REQUIRED", "SMARTART_OR_CHART_MANUAL_REVIEW"})
    for b in extraction.data.get("blocks", []):
        if b["kind"] == "FIGURE" and b.get("disposition") != "REFERENCE_ONLY" and b["id"] not in described_figures:
            if not b.get("description") or b.get("description_image_sha256") != b.get("image_sha256"):
                result.append(f"FIGURE_{b['id']}_UNREVIEWED")
        if b["kind"] == "TABLE" and b.get("review_status") != "REVIEWED":
            result.append(f"TABLE_{b['id']}_UNREVIEWED")
    return sorted(set(result))


def _scope_subset(child, parent):
    for name in ["site_ids", "org_unit_ids", "employment_stages", "employment_types", "audience_roles"]:
        p, c = parent.get(name), child.get(name)
        if name == "employment_stages":
            p = ["ACTIVE"] if p is None else p
            c = ["ACTIVE"] if c is None else c
        if p and (not c or not set(c).issubset(p)):
            return False
    p_legal, c_legal = _legal_scope(parent), _legal_scope(child)
    if p_legal and (not c_legal or not c_legal.issubset(p_legal)):
        return False
    levels = {"STANDARD": 0, "HR_RESTRICTED": 1, "COMPLIANCE_RESTRICTED": 2}
    return levels.get(child.get("sensitivity", "STANDARD"), 0) >= levels.get(parent.get("sensitivity", "STANDARD"), 0)


def _dependency_validation(db, members, published_members):
    """Verify exact evidence, image hashes, scope and a cycle-free version graph."""
    covered_figures, graph = {}, {}
    all_ids = set(members) | set(published_members.values())
    for rid in all_ids:
        revision = db.get(Entity, rid)
        if not revision or revision.kind != "document_revision":
            raise HTTPException(409, "dependency_revision_missing")
        graph[rid] = [d.get("source_revision_id") for d in revision.data.get("dependencies", [])]
    for rid, revision in members.items():
        dependent_doc = get_entity(db, revision.parent_id, "document")
        for dep in revision.data.get("dependencies", []):
            source_id = dep.get("source_revision_id")
            if source_id not in all_ids:
                raise HTTPException(409, "dependency_not_current_or_in_candidate")
            source = get_entity(db, source_id, "document_revision")
            source_doc = get_entity(db, source.parent_id, "document")
            if not _scope_subset(dependent_doc.data.get("scope", {}), source_doc.data.get("scope", {})):
                raise HTTPException(422, "dependent_scope_broader_than_evidence")
            source_extraction = _extraction(db, source, dep.get("source_extraction_revision_id"))
            if source_extraction.id != source.data.get("latest_extraction_revision_id"):
                raise HTTPException(409, "dependency_extraction_stale")
            blocks = {b["id"]: b for b in source_extraction.data["blocks"]}
            block_ids = dep.get("source_block_ids", [])
            hashes = dep.get("source_block_sha256", {})
            if not block_ids or set(block_ids) != set(hashes):
                raise HTTPException(422, "dependency_hashes_required_for_exact_block_set")
            for block_id in block_ids:
                b = blocks.get(block_id)
                if not b or hashes[block_id] != digest(b.get("text", "").encode()):
                    raise HTTPException(409, "dependency_block_hash_mismatch")
                if b.get("kind") == "FIGURE":
                    image_hash = dep.get("source_block_image_sha256", {}).get(block_id)
                    if image_hash != b.get("image_sha256") or not image_hash:
                        raise HTTPException(409, "dependency_image_hash_mismatch")
                    covered_figures.setdefault(source_id, {}).setdefault(block_id, []).append(rid)
    visiting, visited = set(), set()
    def visit(node):
        if node in visiting:
            raise HTTPException(422, "knowledge_dependency_cycle")
        if node in visited:
            return
        visiting.add(node)
        for child in graph.get(node, []):
            visit(child)
        visiting.remove(node)
        visited.add(node)
    for node in graph:
        visit(node)
    return covered_figures


def _candidate_members(db, candidate):
    return {rid: get_entity(db, rid, "document_revision") for rid in candidate.data.get("members", {candidate.data["revision_id"]: {}})}


def _candidate_pin(db, revision):
    doc = get_entity(db, revision.parent_id, "document")
    return {"document_id": revision.parent_id, "extraction_revision_id": revision.data.get("latest_extraction_revision_id"),
            "content_fingerprint": revision.data.get("content_fingerprint"),
            "document_policy_fingerprint": digest({"scope": doc.data.get("scope", {}), "category": doc.data.get("document_category")}),
            "revision_policy_fingerprint": digest({key: revision.data.get(key) for key in
                ("dependencies", "effective_from", "effective_to", "article_kind", "instruction")})}


@router.post("/knowledge/revisions/{revision_id}/candidate-build", status_code=202)
def build_candidate(revision_id: str, body: dict, request: Request, actor: Actor = Depends(get_actor), db=Depends(get_db)):
    revision, _ = _revision(db, actor, revision_id, True)
    check_etag(request, revision)
    _mutable_revision(revision)
    _require_pin(body, revision, _extraction(db, revision))
    head = _head(db)
    if body.get("base_snapshot_id") and body["base_snapshot_id"] != head.data["snapshot_id"]:
        raise HTTPException(409, "stale_base_snapshot")
    members = {revision.id: revision}
    documents = {revision.parent_id}
    extras = body.get("additional_revisions", [])
    if not isinstance(extras, list) or len(extras) > 100:
        raise HTTPException(422, "invalid_additional_revisions")
    for pin in extras:
        if not isinstance(pin, dict):
            raise HTTPException(422, "invalid_additional_revision_pin")
        extra, _ = _revision(db, actor, pin.get("revision_id"), True)
        if extra.id in members or extra.parent_id in documents:
            raise HTTPException(422, "duplicate_candidate_document")
        _mutable_revision(extra)
        _require_pin(pin, extra, _extraction(db, extra))
        members[extra.id] = extra
        documents.add(extra.parent_id)
    pins = {rid: _candidate_pin(db, member) for rid, member in members.items()}
    op = _operation(db, "KNOWLEDGE_CANDIDATE_BUILD", {"revision_id": revision.id, "member_ids": list(pins)}, "PENDING")
    payload = {"revision_id": revision.id, "operation_id": op.id, "members": pins, "base_snapshot_id": head.data["snapshot_id"],
        "actor_id": actor.id, "actor_fingerprint": digest({"role": actor.role, "scope": actor.scope}),
        "profile_fingerprint": _qa_profile_fingerprint(db)}
    for member in members.values():
        _update(member, latest_candidate_operation_id=op.id, candidate_build_operation_id=op.id,
            candidate_snapshot_id=None, impact_review_id=None, qa_artifact_id=None, review_fingerprint=None)
    enqueue(db, "KNOWLEDGE_CANDIDATE_BUILD", payload, f"knowledge-candidate:{op.id}")
    audit(db, actor, "knowledge.candidate.queued", revision.id, {"operation_id": op.id, "member_ids": list(pins)})
    commit(db)
    return serialize(op)


def _process_candidate_job(db, job):
    from .auth import user_actor
    from .models import User
    payload = copy.deepcopy(job.payload)
    lease = payload.get("_lease_token")
    op = get_entity(db, payload["operation_id"], "operation")
    if op.status in {"SUCCEEDED", "FAILED", "CANCELLED"}:
        return True
    members = {rid: get_entity(db, rid, "document_revision") for rid in payload["members"]}
    revision = members[payload["revision_id"]]
    documents = {member.parent_id for member in members.values()}
    actor = None
    def guard(stage="INDEXING", completed=0, retain_lock=False):
        nonlocal actor
        _lock_lease_job(db, job, lease)
        db.refresh(op, with_for_update=True)
        if op.status == "CANCELLED" or op.data.get("cancel_requested"):
            raise IngestCancelled("Candidate build cancelled")
        user = db.get(User, payload["actor_id"])
        if not user:
            raise HTTPException(409, "candidate_requester_no_longer_exists")
        db.refresh(user)
        actor = user_actor(user)
        if not user.enabled or actor.role not in EDIT_ROLES or digest({"role": actor.role, "scope": actor.scope}) != payload["actor_fingerprint"]:
            raise HTTPException(409, "candidate_requester_no_longer_authorized")
        if _qa_profile_fingerprint(db) != payload["profile_fingerprint"]:
            raise HTTPException(409, "candidate_model_profile_changed")
        head = db.scalar(select(Entity).where(Entity.kind == "knowledge_head").execution_options(populate_existing=True))
        if not head or head.data["snapshot_id"] != payload["base_snapshot_id"]:
            raise HTTPException(409, "stale_base_snapshot")
        for doc_id in sorted(documents):
            doc = get_entity(db, doc_id, "document", lock=True)
            db.refresh(doc, with_for_update=True)
        for rid, member in members.items():
            db.refresh(member)
            _revision(db, actor, rid, True)
            _mutable_revision(member)
            if member.data.get("latest_candidate_operation_id") != op.id or _candidate_pin(db, member) != payload["members"][rid]:
                raise HTTPException(409, "candidate_source_changed_or_replaced")
        until = job.lease_until
        if until and until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        job.lease_until = max(until or now(), now() + timedelta(seconds=300))
        op.status = "RUNNING"
        _update(op, progress_stage=stage, completed_members=completed, total_members=len(members), heartbeat_at=now().isoformat())
        if not retain_lock:
            db.commit()
    try:
        guard()
        base = get_entity(db, payload["base_snapshot_id"], "knowledge_snapshot")
        current_members = {doc_id: rid for doc_id, rid in base.data.get("members", {}).items() if doc_id not in documents}
        coverage = _dependency_validation(db, members, current_members)
        chunk_ids, projections, prepared = {}, {}, []
        for completed, (rid, member) in enumerate(members.items()):
            ext = _extraction(db, member)
            chunks = make_chunks(db, member, {**ext.data, "id": ext.id}, persist=False,
                checkpoint=lambda: guard("TOKENIZING", completed))
            projection = index_chunks(db, chunks, checkpoint=lambda: guard("INDEXING", completed)) if chunks else {"mode": "REFERENCE_ONLY", "indexed": 0}
            if projection.get("mode") == "DENSE_SPARSE":
                from .retrieval import selected_model_fingerprints
                expected_encoder = selected_model_fingerprints(db).get("embedder")
                if expected_encoder and projection.get("encoder_fingerprint") != expected_encoder:
                    raise HTTPException(409, "candidate_encoder_profile_mismatch")
            if not chunks and not coverage.get(rid):
                raise ValueError("no_searchable_content_or_authored_description")
            chunk_ids[rid] = [chunk.id for chunk in chunks]
            projections[rid] = projection
            prepared.extend(chunks)
            guard("INDEXING", completed + 1)
        # Only this fenced transaction materializes the candidate and its chunks.
        # Failed/cancelled attempts can leave unreferenced Qdrant points; published
        # corpus membership and exact DB chunk IDs exclude those from every query.
        guard("MATERIALIZING", len(members), retain_lock=True)
        coverage = _dependency_validation(db, members, current_members)
        db.add_all(prepared)
        pins = payload["members"]
        extraction = _extraction(db, revision)
        snapshot = save_entity(db, "knowledge_candidate", {"base_snapshot_id": payload["base_snapshot_id"], "revision_id": revision.id,
            "extraction_revision_id": extraction.id, "content_fingerprint": revision.data["content_fingerprint"],
            "members": pins, "chunk_ids_by_revision": chunk_ids, "projections": projections,
            "chunk_ids": sum(chunk_ids.values(), []), "covered_figures": coverage, "created_by": actor.id}, status="BUILT")
        impacted = []
        for old_doc_id, old_rid in base.data.get("members", {}).items():
            if old_doc_id in documents:
                continue
            old_revision = db.get(Entity, old_rid)
            if not old_revision:
                continue
            for dep in old_revision.data.get("dependencies", []):
                source = db.get(Entity, dep.get("source_revision_id"))
                if source and source.parent_id in documents:
                    impacted.append({"revision_id": old_rid, "document_id": old_doc_id, "state": "PENDING", "reason": "SOURCE_REPLACED"})
                    break
        impact = save_entity(db, "knowledge_impact_review", {"revision_id": revision.id, "candidate_snapshot_id": snapshot.id,
                     "content_fingerprint": revision.data["content_fingerprint"], "dependents": impacted,
                     "fingerprint": digest({"candidate": snapshot.data, "dependents": impacted})}, status="NEEDS_REVIEW" if impacted else "PASSED")
        db.flush()
        for member in members.values():
            _update(member, candidate_snapshot_id=snapshot.id, impact_review_id=impact.id, qa_artifact_id=None, review_fingerprint=None)
        for rid, member in members.items():
            _update(member, index_projection=projections[rid])
        result = {"candidate_snapshot_id": snapshot.id, "impact_review_id": impact.id, "members": pins, "projections": projections}
        op.status = "SUCCEEDED"
        _update(op, candidate_build_result=result, result={"candidate_build_result": result, "revision_id": revision.id}, progress_stage="COMPLETE")
        audit(db, actor, "knowledge.candidate.built", revision.id, {"member_ids": list(members), "operation_id": op.id})
        db.commit()
    except IngestLeaseLost:
        db.rollback()
    except Exception as exc:
        db.rollback()
        try:
            _lock_lease_job(db, job, lease)
        except IngestLeaseLost:
            db.rollback()
            return True
        db.refresh(op, with_for_update=True)
        if isinstance(exc, IngestCancelled) or op.status == "CANCELLED" or op.data.get("cancel_requested"):
            op.status = "CANCELLED"
            _update(op, cancellation_confirmed_at=now().isoformat())
        else:
            op.status = "FAILED"
            _update(op, error=str(getattr(exc, "detail", str(exc)))[:1000], retryable=True)
        db.commit()
    return True


@router.post("/knowledge/revisions/{revision_id}/qa-runs", status_code=202)
def run_qa(revision_id: str, body: dict, request: Request, actor: Actor = Depends(get_actor), db=Depends(get_db)):
    op = _queue_qa(revision_id, body, request, actor, db)
    commit(db)
    return serialize(op)


def _queue_qa(revision_id, body, request, actor, db):
    """Transactional rule implementation; caller owns the commit/lease fence."""
    revision, _ = _revision(db, actor, revision_id, True)
    check_etag(request, revision)
    extraction = _extraction(db, revision)
    _require_pin(body, revision, extraction)
    candidate = get_entity(db, body.get("candidate_snapshot_id") or revision.data.get("candidate_snapshot_id"), "knowledge_candidate")
    if revision.id not in candidate.data["members"] or revision.data.get("candidate_snapshot_id") != candidate.id:
        raise HTTPException(409, "revision_not_candidate_member")
    if revision.status not in {"REVIEW_REQUIRED", "FAILED", "APPROVED"}:
        raise HTTPException(409, "qa_revision_is_immutable_or_processing")
    op = _operation(db, "KNOWLEDGE_QA", {"revision_id": revision.id, "candidate_snapshot_id": candidate.id}, "PENDING")
    db.flush()
    pins = {"revision_id": revision.id, "candidate_snapshot_id": candidate.id, "operation_id": op.id,
            "candidate_fingerprint": digest(candidate.data), "control_fingerprint": _control_versions_fingerprint(db),
            "profile_fingerprint": _qa_profile_fingerprint(db), "actor_id": actor.id,
            "actor_fingerprint": digest({"role": actor.role, "scope": actor.scope})}
    _update(revision, latest_qa_operation_id=op.id, qa_operation_id=op.id, qa_artifact_id=None, review_fingerprint=None)
    revision.status = "REVIEW_REQUIRED"
    enqueue(db, "KNOWLEDGE_QA", pins, f"knowledge-qa:{op.id}")
    audit(db, actor, "knowledge.qa.queued", revision.id, {"operation_id": op.id, "candidate_snapshot_id": candidate.id})
    return op


def _control_versions_fingerprint(db):
    rows = db.scalars(select(Entity).where(Entity.kind == "knowledge_control_question", Entity.status == "ACTIVE").execution_options(populate_existing=True)).all()
    return digest(sorted((row.id, row.data.get("current_version_id")) for row in rows))


def _qa_profile_fingerprint(db):
    from .local_ai import active_profile
    from .rag_identity import pipeline_identities
    profile = active_profile(db)
    return digest({"profile": {"id": profile.id, "configuration": profile.data.get("configuration")} if profile else None,
                   "pipeline_identities": pipeline_identities()})


def _assert_qa_current(db,qa):
    if qa.data.get("control_versions_fingerprint") and qa.data["control_versions_fingerprint"] != _control_versions_fingerprint(db):
        raise HTTPException(409,"qa_control_versions_changed")
    if qa.data.get("model_profile_fingerprint") and qa.data["model_profile_fingerprint"] != _qa_profile_fingerprint(db):
        raise HTTPException(409,"qa_model_profile_changed")


def _perform_qa(db, revision, candidate, actor, checkpoint, control_fingerprint, profile_fingerprint):
    from .knowledge_admin import evaluate_control_suite
    control_suite = evaluate_control_suite(db, candidate, actor, checkpoint=checkpoint)
    checkpoint(len(control_suite["results"]), retain_lock=True)
    all_blockers, artifacts = [], []
    for rid, member in _candidate_members(db, candidate).items():
        ext = _extraction(db, member)
        pin = candidate.data["members"][rid]
        if pin["content_fingerprint"] != member.data["content_fingerprint"] or pin["extraction_revision_id"] != ext.id:
            raise HTTPException(409, "candidate_is_stale")
        if member.data.get("simple_workflow"):
            from .knowledge_simple import quality_blockers
            physical_blockers = quality_blockers(ext)
        else:
            physical_blockers = _blockers(ext, candidate.data.get("covered_figures", {}).get(rid, {}))
        blockers = list(physical_blockers)
        if control_suite["status"] == "FAILED":
            blockers.append("CONTROL_QUESTION_REGRESSION_FAILED")
        all_blockers.extend(blockers)
        qa = save_entity(db, "knowledge_qa", {"revision_id": rid, "candidate_snapshot_id": candidate.id,
                   "content_fingerprint": member.data["content_fingerprint"], "extraction_revision_id": ext.id,
                   "control_versions_fingerprint": control_fingerprint, "model_profile_fingerprint": profile_fingerprint,
                   "checks": {"physical_coverage": not physical_blockers, "control_questions": control_suite["status"] != "FAILED", "exact_evidence_links": True,
                              "index_ack": rid in candidate.data["projections"]},
                   "blockers": blockers, "control_suite": control_suite,
                   "manual_review_status": "PENDING" if control_suite["results"] else "NOT_APPLICABLE",
                   "model_quality_evaluation": control_suite.get("semantic_quality", "NOT_MEASURED"), "fingerprint": digest({"candidate": candidate.data, "member": rid, "control_suite": control_suite["fingerprint"]})},
                   status="PASSED" if not blockers else "FAILED")
        db.flush()
        _update(member, qa_artifact_id=qa.id)
        artifacts.append(qa)
    return {"qa_artifact_id": next(a.id for a in artifacts if a.data["revision_id"] == revision.id),
            "qa_results": [serialize(a) for a in artifacts], "status": "FAILED" if all_blockers else "PASSED"}


@router.get("/knowledge/qa-artifacts/{qa_artifact_id}")
def get_qa(qa_artifact_id: str, response: Response, actor: Actor = Depends(get_actor), db=Depends(get_db)):
    qa = get_entity(db, qa_artifact_id, "knowledge_qa")
    _revision(db, actor, qa.data["revision_id"], True)
    response.headers["ETag"] = etag(qa)
    return serialize(qa)


@router.post("/knowledge/revisions/{revision_id}/review")
def review_revision(revision_id: str, body: dict, request: Request, response: Response, actor: Actor = Depends(get_actor), db=Depends(get_db)):
    revision = _review_revision(revision_id, body, request, actor, db)
    commit(db)
    return _result(revision, response)


def _review_revision(revision_id, body, request, actor, db):
    """Transactional approval rules shared by HTTP and fenced rollback worker."""
    require_role(actor, *REVIEW_ROLES)
    revision, _ = _revision(db, actor, revision_id, True)
    check_etag(request, revision)
    if revision.status == "PUBLISHED":
        raise HTTPException(409, "published_revision_is_immutable")
    action = body.get("action")
    if len(str(body.get("reason", ""))) < 3:
        raise HTTPException(422, "review_reason_required")
    extraction = _extraction(db, revision)
    _require_pin(body, revision, extraction)
    if action == "APPROVE":
        candidate = get_entity(db, revision.data.get("candidate_snapshot_id"), "knowledge_candidate")
        impact = get_entity(db, revision.data.get("impact_review_id"), "knowledge_impact_review")
        if impact.status != "PASSED":
            raise HTTPException(409, "impact_review_required")
        pin = candidate.data["members"].get(revision.id, {})
        doc_policy = digest({"scope": _doc(db, actor, revision.parent_id).data.get("scope", {}), "category": _doc(db, actor, revision.parent_id).data.get("document_category")})
        if pin.get("document_policy_fingerprint") != doc_policy:
            raise HTTPException(409, "document_policy_changed_rebuild_candidate")
        covered = candidate.data.get("covered_figures", {}).get(revision.id, {})
        for figure, describing_revision_ids in covered.items():
            if not any(get_entity(db, rid, "document_revision").status == "APPROVED" for rid in describing_revision_ids):
                raise HTTPException(409, "approve_authored_description_before_source_file")
        blockers = _blockers(extraction, covered)
        if blockers:
            raise HTTPException(409, {"code": "source_review_incomplete", "blockers": blockers})
        qa_id = body.get("qa_artifact_id") or revision.data.get("qa_artifact_id")
        if not qa_id:
            raise HTTPException(409, "candidate_qa_required")
        qa = get_entity(db, qa_id, "knowledge_qa")
        _assert_qa_current(db,qa)
        if qa.status != "PASSED" or qa.data["content_fingerprint"] != revision.data["content_fingerprint"] or qa.data["candidate_snapshot_id"] != revision.data.get("candidate_snapshot_id"):
            raise HTTPException(409, "qa_is_failed_or_stale")
        if qa.data.get("control_suite", {}).get("results") and qa.data.get("manual_review_status") != "PASS":
            raise HTTPException(409, "control_question_manual_review_required")
        revision.status = "APPROVED"
        _update(revision, approved_by=actor.id, approved_at=now().isoformat(),
                review_fingerprint=digest({"content": revision.data["content_fingerprint"], "qa": qa.id, "reviewer": actor.id}),
                source_completeness="COMPLETE_WITHIN_DECLARED_SCOPE")
    elif action == "REQUEST_CHANGES":
        revision.status = "REVIEW_REQUIRED"
        _update(revision, review_fingerprint=None, qa_artifact_id=None, candidate_snapshot_id=None)
    elif action == "REJECT":
        revision.status = "REJECTED"
        _update(revision, review_fingerprint=None)
    else:
        raise HTTPException(422, "invalid_review_action")
    audit(db, actor, "knowledge.revision.reviewed", revision.id, {"action": action, "reason": body["reason"]})
    return revision


def _publish(db, revision, doc, actor=None, expected_snapshot=None, review_pins=None):
    if doc.status in {"PURGE_PENDING", "PURGED", "TOMBSTONED"} or doc.data.get("source_revoked"):
        raise HTTPException(409, "source_revoked")
    head = _head(db)
    if expected_snapshot and expected_snapshot != head.data["snapshot_id"]:
        raise HTTPException(409, "stale_snapshot")
    if revision.status != "APPROVED" or not revision.data.get("candidate_snapshot_id"):
        raise HTTPException(409, "approved_revision_required")
    candidate = get_entity(db, revision.data.get("candidate_snapshot_id"), "knowledge_candidate")
    if candidate.data["base_snapshot_id"] != head.data["snapshot_id"]:
        raise HTTPException(409, "candidate_is_stale_rebuild_required")
    members = _candidate_members(db, candidate)
    expected_members = set(members) - {revision.id}
    supplied = review_pins or []
    if {p.get("revision_id") for p in supplied} != expected_members or len(supplied) != len(expected_members):
        raise HTTPException(422, "publish_requires_exact_candidate_member_set")
    review_map = {p["revision_id"]: p.get("expected_review_fingerprint") for p in supplied}
    impact = get_entity(db, revision.data.get("impact_review_id"), "knowledge_impact_review")
    if impact.status != "PASSED" or impact.data["candidate_snapshot_id"] != candidate.id:
        raise HTTPException(409, "impact_review_required")
    for rid, member in members.items():
        if actor:
            _revision(db, actor, rid, True)
        if member.status != "APPROVED" or not member.data.get("review_fingerprint"):
            raise HTTPException(409, "all_candidate_members_must_be_approved")
        if not is_effective(member.data):
            raise HTTPException(409, "candidate_member_outside_effective_window")
        if rid in review_map and review_map[rid] != member.data["review_fingerprint"]:
            raise HTTPException(409, "stale_member_review")
        pin = candidate.data["members"][rid]
        ext = _extraction(db, member)
        if pin["content_fingerprint"] != member.data["content_fingerprint"] or pin["extraction_revision_id"] != ext.id:
            raise HTTPException(409, "candidate_member_changed")
        qa = get_entity(db, member.data.get("qa_artifact_id"), "knowledge_qa")
        from .knowledge_simple import approval_current, quality_blockers
        simple_approval = approval_current(member, ext, qa, candidate)
        if (quality_blockers(ext) if simple_approval else _blockers(ext, candidate.data.get("covered_figures", {}).get(rid, {}))):
            raise HTTPException(409, "source_review_incomplete")
        _assert_qa_current(db,qa)
        if qa.status != "PASSED" or qa.data["content_fingerprint"] != member.data["content_fingerprint"] or qa.data["candidate_snapshot_id"] != candidate.id:
            raise HTTPException(409, "member_qa_stale")
        if qa.data.get("manual_review_status") == "FAIL":
            raise HTTPException(409, "control_question_manual_review_not_passed")
        if qa.data.get("control_suite", {}).get("results") and qa.data.get("manual_review_status") != "PASS" and not simple_approval:
            raise HTTPException(409, "control_question_manual_review_required")
        member_doc = get_entity(db, member.parent_id, "document", lock=True)
        db.refresh(member_doc, with_for_update=True)
        if pin.get("document_policy_fingerprint") != digest({"scope": member_doc.data.get("scope", {}), "category": member_doc.data.get("document_category")}):
            raise HTTPException(409, "candidate_document_policy_changed")
        if member.data.get("candidate_snapshot_id") != candidate.id or member.data.get("impact_review_id") != impact.id:
            raise HTTPException(409, "candidate_review_mismatch")
    old = get_entity(db, head.data["snapshot_id"], "knowledge_snapshot")
    withdrawn = {d["document_id"] for d in impact.data.get("dependents", []) if d.get("state") == "WITHDRAW"}
    new_members = {did: rid for did, rid in old.data.get("members", {}).items() if did not in withdrawn}
    new_members.update({member.parent_id: rid for rid, member in members.items()})
    _dependency_validation(db, members, {did: rid for did, rid in new_members.items() if rid not in members})
    snapshot = save_entity(db, "knowledge_snapshot", {"members": new_members,
                 "previous_snapshot_id": old.id, "reason": "Atomic approved candidate publication", "projections": candidate.data["projections"],
                 "candidate_snapshot_id": candidate.id, "withdrawn_document_ids": sorted(withdrawn)}, status="PUBLISHED")
    db.flush()
    for rid, member in members.items():
        member.status = "PUBLISHED"
        _update(member, published_at=now().isoformat(), published_by=actor.id if actor else None, kb_snapshot_id=snapshot.id)
        member_doc = get_entity(db, member.parent_id, "document", lock=True)
        if member_doc.status in {"PURGE_PENDING", "PURGED", "TOMBSTONED"} or member_doc.data.get("source_revoked"):
            raise HTTPException(409, "candidate_member_source_revoked")
        member_doc.status = "ACTIVE"
        _update(member_doc, active_revision_id=rid, authz_epoch=member_doc.data.get("authz_epoch", 1) + 1)
        for chunk_id in candidate.data["chunk_ids_by_revision"][rid]:
            chunk = db.get(Entity, chunk_id)
            if chunk:
                chunk.status = "PUBLISHED"
    for did in withdrawn:
        if actor:
            _doc(db, actor, did, True)
        dependent_doc = get_entity(db, did, "document", lock=True)
        dependent_doc.status = "ARCHIVED"
        _update(dependent_doc, authz_epoch=dependent_doc.data.get("authz_epoch", 1) + 1, withdrawal_reason="SOURCE_REPLACED_PENDING_REVIEW")
    _update(head, snapshot_id=snapshot.id, authz_epoch=head.data.get("authz_epoch", 1) + 1)
    candidate.status = "PUBLISHED"
    rollback_ops = db.scalars(select(Entity).where(Entity.kind == "operation", Entity.status == "WAITING_REVIEW")).all()
    for operation in rollback_ops:
        if operation.data.get("type") == "KNOWLEDGE_ROLLBACK" and operation.data.get("revision_id") in members:
            operation.status = "SUCCEEDED"
            _update(operation, kb_snapshot_id=snapshot.id, published=True, result={"revision_id": operation.data["revision_id"], "kb_snapshot_id": snapshot.id, "published": True})
    return snapshot


@router.post("/knowledge/revisions/{revision_id}/publish", status_code=202)
def publish_revision(revision_id: str, body: dict, request: Request, actor: Actor = Depends(get_actor), db=Depends(get_db)):
    require_role(actor, "HR_ADMIN")
    revision, doc = _revision(db, actor, revision_id, True)
    check_etag(request, revision)
    if body.get("expected_review_fingerprint") and body["expected_review_fingerprint"] != revision.data.get("review_fingerprint"):
        raise HTTPException(409, "stale_review")
    effective = body.get("effective_at") or revision.data.get("effective_from") or now().isoformat()
    stamp = datetime.fromisoformat(effective.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    if stamp > now():
        if revision.status != "APPROVED":
            raise HTTPException(409, "approved_revision_required")
        from .models import Job
        key = f"publish:{revision.id}:{revision.data['review_fingerprint']}:{stamp.isoformat()}"
        existing = db.scalar(select(Job).where(Job.unique_key == key))
        if existing and existing.payload.get("operation_id"):
            return serialize(get_entity(db, existing.payload["operation_id"], "operation"))
        op = _operation(db, "KNOWLEDGE_PUBLISH", {"revision_id": revision.id}, "PENDING")
        op.owner_id = actor.id
        db.flush()
        enqueue(db, "KNOWLEDGE_PUBLISH", {"revision_id": revision.id, "operation_id": op.id,
                "actor_id": actor.id, "actor_fingerprint": digest({"role": actor.role, "scope": actor.scope}),
                "expected_review_fingerprint": revision.data["review_fingerprint"],
                "expected_snapshot_id": body.get("expected_snapshot_id") or _head(db).data["snapshot_id"],
                "additional_revision_reviews": body.get("additional_revision_reviews", [])}, key, run_at=stamp)
    else:
        snapshot = _publish(db, revision, doc, actor, body.get("expected_snapshot_id"), body.get("additional_revision_reviews", []))
        op = _operation(db, "KNOWLEDGE_PUBLISH", {"revision_id": revision.id, "kb_snapshot_id": snapshot.id})
    audit(db, actor, "knowledge.revision.publish_requested", revision.id, {"effective_at": effective}, reason=body.get("reason"))
    commit(db)
    return serialize(op)


@router.post("/knowledge/documents/{document_id}/archive")
def archive_document(document_id: str, body: dict, request: Request, actor: Actor = Depends(get_actor), db=Depends(get_db)):
    doc = _doc(db, actor, document_id, True)
    check_etag(request, doc)
    doc.status = "ARCHIVED"
    _update(doc, authz_epoch=doc.data.get("authz_epoch", 1) + 1)
    audit(db, actor, "knowledge.document.archived", doc.id, {"reason": body.get("reason")})
    commit(db)
    return serialize(doc)


def _scoped_snapshot(db, actor, snapshot):
    require_role(actor, *EDIT_ROLES)
    result = serialize(snapshot)
    members = snapshot.data.get("members", {})
    result["members"] = {doc_id: revision_id for doc_id, revision_id in members.items()
                         if (doc := db.get(Entity, doc_id)) is not None and _scope_access(actor, doc)}
    result["scope_filtered"] = len(result["members"]) != len(members)
    return result


@router.get("/knowledge/snapshots/current")
def current_snapshot(actor: Actor = Depends(get_actor), db=Depends(get_db)):
    require_role(actor, *EDIT_ROLES)
    head = _head(db)
    db.commit()
    snapshot = get_entity(db, head.data["snapshot_id"], "knowledge_snapshot")
    return _scoped_snapshot(db, actor, snapshot)


@router.post("/knowledge/revisions/{revision_id}/retry", status_code=202)
def retry_revision(revision_id: str, body: dict, request: Request, actor: Actor = Depends(get_actor), db=Depends(get_db)):
    revision, _ = _revision(db, actor, revision_id, True)
    check_etag(request, revision)
    if revision.status not in {"FAILED", "REVIEW_REQUIRED", "CANCELLED"} or not revision.data.get("source_path"):
        raise HTTPException(409, "retry_not_allowed")
    extraction = _extraction(db, revision) if revision.data.get("latest_extraction_revision_id") else None
    selected = body.get("page_numbers")
    options = body.get("page_options", {})
    if selected is not None:
        physical = {p["page_no"] for p in extraction.data["pages"]} if extraction else set()
        if not isinstance(selected, list) or not selected or len(selected) > 500 or any(type(n) is not int or n not in physical for n in selected):
            raise HTTPException(422, "invalid_retry_page_numbers")
        selected = sorted(set(selected))
        if len(str(body.get("reason", "")).strip()) < 3:
            raise HTTPException(422, "selected_page_retry_reason_required")
    if not isinstance(options, dict) or len(options) > 500:
        raise HTTPException(422, "invalid_page_options")
    for key, value in options.items():
        if not str(key).isdigit() or selected is None or int(key) not in selected or not isinstance(value, dict) or set(value) - {"rotation", "force_ocr"}:
            raise HTTPException(422, "invalid_page_options")
        if type(value.get("rotation", 0)) is not int or value.get("rotation", 0) not in {0, 90, 180, 270} or type(value.get("force_ocr", False)) is not bool:
            raise HTTPException(422, "invalid_page_options")
        if (value.get("rotation") or value.get("force_ocr")) and Path(revision.data["source_path"]).suffix.lower() not in {".pdf", ".png", ".jpg", ".jpeg"}:
            raise HTTPException(422, "ocr_rotation_requires_pdf_or_image")
    attempt = revision.data.get("attempt", 1) + 1
    _update(revision, attempt=attempt, candidate_snapshot_id=None, qa_artifact_id=None, review_fingerprint=None, impact_review_id=None)
    revision.status = "UPLOADED"
    op = _operation(db, "KNOWLEDGE_INGEST", {"revision_id": revision.id}, "PENDING")
    _update(revision, latest_ingest_operation_id=op.id)
    enqueue(db, "KNOWLEDGE_INGEST", {"revision_id": revision.id, "operation_id": op.id, "retry": True,
        "page_numbers": selected, "page_options": options}, f"ingest:{revision.id}:{attempt}")
    audit(db, actor, "knowledge.retry", revision.id, {"attempt": attempt, "page_numbers": selected, "page_options": options, "reason": body.get("reason")})
    commit(db)
    return serialize(op)


@router.get("/knowledge/revisions/{revision_id}/dependencies")
def dependencies(revision_id: str, actor: Actor = Depends(get_actor), db=Depends(get_db)):
    revision, _ = _revision(db, actor, revision_id, True)
    return {"items": revision.data.get("dependencies", []), "next_cursor": None}


@router.get("/knowledge/impact-reviews/{impact_review_id}")
def get_impact(impact_review_id: str, response: Response, actor: Actor = Depends(get_actor), db=Depends(get_db)):
    impact = get_entity(db, impact_review_id, "knowledge_impact_review")
    _revision(db, actor, impact.data["revision_id"], True)
    return _result(impact,response)


@router.post("/knowledge/revisions/{revision_id}/impact-reviews", status_code=202)
def create_impact(revision_id: str, body: dict, actor: Actor = Depends(get_actor), db=Depends(get_db)):
    revision, _ = _revision(db, actor, revision_id, True)
    if not revision.data.get("impact_review_id"):
        raise HTTPException(409, "candidate_build_required")
    return serialize(get_entity(db, revision.data["impact_review_id"], "knowledge_impact_review"))


class IngestCancelled(Exception):
    pass


class IngestLeaseLost(Exception):
    pass


class IngestSourceRevoked(Exception):
    pass


def _lock_lease_job(db, job, token):
    db.refresh(job, with_for_update=True)
    if job.payload.get("_lease_token") != token:
        raise IngestLeaseLost("Worker lease superseded")
    if token:
        until=job.lease_until
        if until and until.tzinfo is None:
            until=until.replace(tzinfo=timezone.utc)
        if job.status != "PROCESSING" or not until or until <= now():
            raise IngestLeaseLost("Worker lease expired")
    _knowledge_write_lock(db)


def _process_qa_job(db, job):
    from .auth import user_actor
    from .models import User
    payload = dict(job.payload)
    lease = payload.get("_lease_token")
    op = get_entity(db, payload["operation_id"], "operation")
    if op.status in {"SUCCEEDED", "FAILED", "CANCELLED"}:
        return True
    revision = get_entity(db, payload["revision_id"], "document_revision")
    candidate = get_entity(db, payload["candidate_snapshot_id"], "knowledge_candidate")
    user = db.get(User, payload["actor_id"])
    actor = user_actor(user) if user else None
    def guard(completed=0):
        nonlocal actor
        _lock_lease_job(db,job,lease)
        db.refresh(op, with_for_update=True)
        if op.status == "CANCELLED" or op.data.get("cancel_requested"):
            raise IngestCancelled("QA cancelled")
        current_user = db.get(User, payload["actor_id"])
        if not current_user:
            raise HTTPException(409, "qa_requester_no_longer_exists")
        db.refresh(current_user)
        if not current_user.enabled or current_user.role not in REVIEW_ROLES:
            raise HTTPException(409, "qa_requester_no_longer_authorized")
        actor = user_actor(current_user)
        if digest({"role": actor.role, "scope": actor.scope}) != payload["actor_fingerprint"]:
            raise HTTPException(409, "qa_requester_scope_changed")
        db.refresh(candidate)
        db.refresh(revision)
        if revision.data.get("candidate_snapshot_id") != candidate.id or revision.data.get("latest_qa_operation_id") != op.id or revision.status != "REVIEW_REQUIRED":
            raise HTTPException(409, "qa_candidate_replaced_or_revision_changed")
        if digest(candidate.data) != payload["candidate_fingerprint"] or candidate.status != "BUILT":
            raise HTTPException(409, "qa_candidate_changed")
        if _control_versions_fingerprint(db) != payload["control_fingerprint"] or _qa_profile_fingerprint(db) != payload["profile_fingerprint"]:
            raise HTTPException(409, "qa_controls_or_model_profile_changed")
        for rid in candidate.data["members"]:
            member = get_entity(db, rid, "document_revision")
            db.refresh(member)
            doc = get_entity(db, member.parent_id, "document", lock=True)
            db.refresh(doc, with_for_update=True)
            _revision(db, actor, rid, True)
            pin = candidate.data["members"][rid]
            if member.data.get("content_fingerprint") != pin["content_fingerprint"] or member.data.get("latest_extraction_revision_id") != pin["extraction_revision_id"]:
                raise HTTPException(409, "qa_source_changed")
        deadline = now() + timedelta(seconds=240)
        existing = job.lease_until
        if existing and existing.tzinfo is None:
            existing = existing.replace(tzinfo=timezone.utc)
        job.lease_until = max(existing, deadline) if existing else deadline
        _update(op, completed_control_questions=completed, progress_stage="CONTROL_QUESTIONS", heartbeat_at=now().isoformat())
    try:
        guard()
        op.status = "RUNNING"
        db.commit()
        def heartbeat(completed, retain_lock=False):
            guard(completed)
            if not retain_lock:
                db.commit()
        result = _perform_qa(db, revision, candidate, actor, heartbeat, payload["control_fingerprint"], payload["profile_fingerprint"])
        guard(len(result["qa_results"][0].get("control_suite", {}).get("results", [])))
        op.status = "SUCCEEDED" if result["status"] == "PASSED" else "FAILED"
        _update(op, **result, result={**result, "revision_id": revision.id}, progress_stage="COMPLETE", retryable=result["status"] != "PASSED")
        db.commit()
    except IngestLeaseLost:
        db.rollback()
    except IngestCancelled:
        db.rollback()
        try:
            _lock_lease_job(db,job,lease)
        except IngestLeaseLost:
            db.rollback()
            return True
        db.refresh(op, with_for_update=True)
        op.status = "CANCELLED"
        _update(op, cancellation_confirmed_at=now().isoformat())
        db.commit()
    except Exception as exc:
        db.rollback()
        try:
            _lock_lease_job(db,job,lease)
        except IngestLeaseLost:
            db.rollback()
            return True
        db.refresh(op, with_for_update=True)
        if op.status != "CANCELLED":
            op.status = "FAILED"
            _update(op, error=str(getattr(exc, "detail", str(exc)))[:1000], retryable=True)
            db.commit()
    return True


def _process_scheduled_publish_job(db, job):
    from .auth import user_actor
    from .models import User
    try:
        _lock_lease_job(db, job, job.payload.get("_lease_token"))
    except IngestLeaseLost:
        db.rollback()
        return True
    payload = dict(job.payload)
    op = get_entity(db, payload["operation_id"], "operation", lock=True) if payload.get("operation_id") else None
    if op and op.status in {"SUCCEEDED", "CANCELLED"}:
        return True
    revision = get_entity(db, payload["revision_id"], "document_revision", lock=True)
    doc = get_entity(db, revision.parent_id, "document", lock=True)
    if revision.status == "PUBLISHED":
        if op:
            op.status = "SUCCEEDED"
            _update(op, kb_snapshot_id=revision.data.get("kb_snapshot_id"), published=True,
                    result={"revision_id": revision.id, "kb_snapshot_id": revision.data.get("kb_snapshot_id"), "published": True})
        return True  # An old/replayed schedule cannot publish this revision twice.
    actor = None
    try:
        if op:
            op.status = "RUNNING"
        user = db.scalar(select(User).where(User.id == payload.get("actor_id", "")).execution_options(populate_existing=True))
        if not user or not user.enabled or user.role != "HR_ADMIN":
            raise HTTPException(409, "scheduled_publisher_no_longer_authorized")
        actor = user_actor(user)
        if payload.get("actor_fingerprint") != digest({"role": actor.role, "scope": actor.scope}):
            raise HTTPException(409, "scheduled_publisher_permissions_changed")
        if payload.get("expected_review_fingerprint") != revision.data.get("review_fingerprint"):
            raise HTTPException(409, "scheduled_review_changed")
        _revision(db, actor, revision.id, True)
        with db.begin_nested():
            snapshot = _publish(db, revision, doc, actor, payload.get("expected_snapshot_id"), payload.get("additional_revision_reviews", []))
        if op:
            op.status = "SUCCEEDED"
            _update(op, kb_snapshot_id=snapshot.id, published=True,
                    result={"revision_id": revision.id, "kb_snapshot_id": snapshot.id, "published": True}, error=None)
        audit(db, actor, "knowledge.revision.scheduled_published", revision.id, {"operation_id": op.id if op else None, "kb_snapshot_id": snapshot.id})
    except HTTPException as exc:
        if doc.status not in {"PURGE_PENDING", "PURGED", "TOMBSTONED"} and not doc.data.get("source_revoked"):
            _update(revision, scheduled_publish_error=str(exc.detail))
            revision.status = "REVIEW_REQUIRED"
        if op:
            op.status = "FAILED"
            _update(op, published=False, error=str(exc.detail), retryable=False)
    # The outer worker commits these writes together with the fresh Job fence.
    return True


def process_ingest_job(db, job):
    if job.kind == "KNOWLEDGE_SIMPLE_PREPARE":
        from .knowledge_simple import process
        return process(db, job)
    if job.kind in {"KNOWLEDGE_CANDIDATE_BUILD", "KNOWLEDGE_QA"}:
        expected_lease = job.payload.get("_lease_token")
        result = _process_candidate_job(db, job) if job.kind == "KNOWLEDGE_CANDIDATE_BUILD" else _process_qa_job(db, job)
        try:
            _lock_lease_job(db, job, expected_lease)
        except IngestLeaseLost:
            db.rollback()
            return result
        revision = get_entity(db, job.payload["revision_id"], "document_revision")
        from .knowledge_simple import schedule
        schedule(db, revision, trigger=job.kind + ":" + job.payload["operation_id"])
        db.commit()
        return result
    if job.kind == "KNOWLEDGE_PUBLISH":
        return _process_scheduled_publish_job(db, job)
    if job.kind not in {"KNOWLEDGE_INGEST", "DOCUMENT_INGEST", "INGEST_DOCUMENT"}:
        return False
    revision = get_entity(db, job.payload["revision_id"], "document_revision")
    source_doc = get_entity(db, revision.parent_id, "document")
    if source_doc.status in {"PURGE_PENDING", "PURGED", "TOMBSTONED"} or source_doc.data.get("source_revoked"):
        return True
    expected_lease = job.payload.get("_lease_token")
    attempt_directory = new_id()
    derivative_bytes = 0
    if revision.status in {"PUBLISHED", "APPROVED", "REJECTED"}:
        return True
    op = db.get(Entity, job.payload.get("operation_id")) if job.payload.get("operation_id") else None
    if op:
        db.refresh(op)
    prior = _extraction(db, revision) if revision.data.get("latest_extraction_revision_id") else None
    retained_pages, retained_blocks, manual_blocks = {}, [], []
    if job.payload.get("retry") and prior:
        selected = job.payload.get("page_numbers")
        retained_pages = {p["page_no"]: p for p in prior.data["pages"] if (p["page_no"] not in selected if selected is not None else p["status"] == "PROCESSED")}
        retained_blocks = [b for b in prior.data["blocks"] if b["page_no"] in retained_pages]
        manual_blocks = [b for b in prior.data["blocks"] if b["page_no"] not in retained_pages and
            (b.get("correction_origin") == "HUMAN" or b.get("corrected_by") or b.get("description_origin") == "HR_AUTHORED")]
    def assert_current_worker():
        _lock_lease_job(db,job,expected_lease)
        if op:
            db.refresh(op, with_for_update=True)
            if op.status == "CANCELLED" or op.data.get("cancel_requested"):
                raise IngestCancelled("Ingestion cancelled by operator")
        doc_guard = get_entity(db, revision.parent_id, "document", lock=True)
        db.refresh(doc_guard, with_for_update=True)
        if doc_guard.status in {"PURGE_PENDING", "PURGED", "TOMBSTONED"} or doc_guard.data.get("source_revoked"):
            raise IngestSourceRevoked("Source revoked")
        deadline=now()+timedelta(seconds=120)
        existing=job.lease_until
        if existing and existing.tzinfo is None:
            existing=existing.replace(tzinfo=timezone.utc)
        job.lease_until=max(existing,deadline) if existing else deadline
    def checkpoint(pages, blocks, previews=None):
        nonlocal derivative_bytes
        assert_current_worker()
        if previews:
            import base64
            import re
            output = DATA / "knowledge" / revision.id / "previews" / attempt_directory
            output.mkdir(parents=True, exist_ok=True)
            for filename, encoded in previews.items():
                if not re.fullmatch(r"page-\d{4}\.png", filename):
                    raise ValueError("invalid_parser_preview_name")
                raw = base64.b64decode(encoded, validate=True)
                derivative_bytes += len(raw)
                if len(raw) > 15 * 1024 * 1024 or derivative_bytes > 500 * 1024 * 1024:
                    raise ValueError("parser_derivative_limit")
                (output / filename).write_bytes(raw)
        pages = copy.deepcopy(pages)
        for page in pages:
            if page.get("preview_file"):
                page["preview_file"] = attempt_directory + "/" + Path(page["preview_file"]).name
        merged_pages = [copy.deepcopy(retained_pages.get(p["page_no"], p)) for p in pages]
        merged_blocks = [copy.deepcopy(b) for b in blocks if b["page_no"] not in retained_pages] + copy.deepcopy(retained_blocks)
        # Human corrections survive a selected-page OCR retry. Match logical
        # regions, never an OCR-generated UUID; a whole-page text correction
        # supersedes new whole-page OCR. Other machine regions remain reviewable.
        def same_region(old, fresh):
            if old["page_no"] != fresh["page_no"] or old["kind"] != fresh["kind"]:
                return False
            if old.get("kind") == "FIGURE" and old.get("image_sha256") == fresh.get("image_sha256"):
                return True
            a, b = old.get("bbox"), fresh.get("bbox")
            if not a or not b:
                return True
            intersection = max(0, min(a[2], b[2])-max(a[0], b[0])) * max(0, min(a[3], b[3])-max(a[1], b[1]))
            area = max((a[2]-a[0])*(a[3]-a[1]), .000001)
            return intersection / area > .5
        for old in manual_blocks:
            preserved = copy.deepcopy(old)
            merged_blocks = [b for b in merged_blocks if not same_region(preserved, b)]
            page = next((p for p in merged_pages if p["page_no"] == old["page_no"]), None)
            if page:
                page["review_status"] = "PENDING"
                page["issues"] = sorted(set(page.get("issues", []) + ["MANUAL_CORRECTIONS_PRESERVED_REVIEW"]))
                if old.get("kind") == "FIGURE" and page.get("render_sha256") and old.get("image_sha256") != page["render_sha256"]:
                    preserved.update(disposition="REFERENCE_ONLY", retry_visual_review_required=True,
                        retry_visual_reason="RENDER_CHANGED_HR_DESCRIPTION_PRESERVED_IN_HISTORY")
            merged_blocks.append(preserved)
        merged_blocks.sort(key=lambda b: (b["page_no"], b.get("reading_order", 0)))
        _new_extraction(db, revision, {"pages": merged_pages, "blocks": merged_blocks,
                        "source_sha256": revision.data["sha256"], "parser_version": "korsik-parser-1"})
        db.commit()  # durable first-failure checkpoint, not only final success
    try:
        assert_current_worker()
        revision.status = "PROCESSING"
        if op:
            op.status = "RUNNING"
        db.commit()
        result = _run_parser(revision.data["source_path"], DATA / "knowledge" / revision.id / "previews", checkpoint,
            **({"skip_pages": sorted(retained_pages)} if retained_pages else {}),
            **({"page_options": job.payload["page_options"]} if job.payload.get("page_options") else {}))
        assert_current_worker()
        current = _extraction(db, revision)
        failed = any(p["status"] == "FAILED" for p in current.data["pages"])
        revision.status = "FAILED" if failed else "REVIEW_REQUIRED"
        _update(revision, parser_version=result["parser_version"], processing_error=None,
                page_count=len(current.data["pages"]), block_count=len(current.data["blocks"]))
        if op:
            op.status = "FAILED" if failed else "SUCCEEDED"
            _update(op, revision_id=revision.id, extraction_revision_id=current.id)
    except (IngestLeaseLost, IngestSourceRevoked):
        db.rollback()
        return True
    except IngestCancelled:
        doc_guard=get_entity(db,revision.parent_id,"document",lock=True)
        db.refresh(doc_guard,with_for_update=True)
        if doc_guard.status in {"PURGE_PENDING","PURGED","TOMBSTONED"} or doc_guard.data.get("source_revoked"):
            db.rollback()
            return True
        revision.status = "CANCELLED"
        _update(revision, processing_error=None, cancellation_confirmed_at=now().isoformat())
        if op:
            op.status = "CANCELLED"
    except Exception as exc:
        # A parser error arriving after purge/lease replacement cannot resurrect
        # a tombstoned revision or overwrite the successor worker's checkpoint.
        try:
            assert_current_worker()
        except (IngestLeaseLost, IngestSourceRevoked):
            db.rollback()
            return True
        except IngestCancelled:
            doc_guard=get_entity(db,revision.parent_id,"document",lock=True)
            db.refresh(doc_guard,with_for_update=True)
            if doc_guard.status in {"PURGE_PENDING","PURGED","TOMBSTONED"} or doc_guard.data.get("source_revoked"):
                db.rollback()
                return True
            revision.status = "CANCELLED"
            if op:
                op.status = "CANCELLED"
            db.commit()
            return True
        revision.status = "FAILED"
        _update(revision, processing_error=f"{type(exc).__name__}: {str(exc)[:500]}")
        if op:
            op.status = "FAILED"
            _update(op, error=revision.data["processing_error"])
    if revision.status == "REVIEW_REQUIRED":
        from .knowledge_simple import schedule
        schedule(db, revision, trigger="ingest:" + str(job.payload.get("operation_id", revision.id)))
    db.commit()
    return True


@router.post("/knowledge/impact-reviews/{impact_review_id}/review")
def review_impact(impact_review_id: str, body: dict, request: Request, actor: Actor = Depends(get_actor), db=Depends(get_db)):
    require_role(actor, "HR_ADMIN")
    impact = get_entity(db, impact_review_id, "knowledge_impact_review")
    revision, _ = _revision(db, actor, impact.data["revision_id"], True)
    if revision.status == "PUBLISHED":
        raise HTTPException(409, "published_impact_review_is_immutable")
    check_etag(request, impact)
    decisions = body.get("decisions", [])
    pending = impact.data.get("dependents", [])
    if {d.get("revision_id") for d in decisions} != {d["revision_id"] for d in pending}:
        raise HTTPException(422, "exact_dependency_decisions_required")
    updated = []
    for dependency in pending:
        decision = next(d for d in decisions if d["revision_id"] == dependency["revision_id"])
        if decision.get("action") != "WITHDRAW" or len(str(decision.get("reason", ""))) < 3:
            raise HTTPException(422, "withdraw_with_reason_or_rebuild_with_replacement_required")
        _revision(db, actor, dependency["revision_id"], True)
        updated.append({**dependency, "state": "WITHDRAW", "reason": decision["reason"], "reviewed_by": actor.id})
    _update(impact, dependents=updated, reviewed_by=actor.id, fingerprint=digest({"candidate": impact.data["candidate_snapshot_id"], "dependents": updated}))
    impact.status = "PASSED"
    audit(db, actor, "knowledge.impact.reviewed", impact.id, {"withdraw_revision_ids": [d["revision_id"] for d in updated]},
        reason="; ".join(d["revision_id"] + ": " + d["reason"] for d in updated) or body.get("reason"))
    commit(db)
    return serialize(impact)


def _run_parser(source, previews, checkpoint, *, skip_pages=None, page_options=None):
    parser_url = os.environ.get("PARSER_URL", "").rstrip("/")
    import base64
    import tempfile
    if not parser_url:
        # Local test/operator route also writes into private scratch first.
        # Durable derivatives are fenced under the document lock in checkpoint.
        with tempfile.TemporaryDirectory(prefix="korsik-local-parse-") as scratch:
            sent = set()
            def local_checkpoint(pages, blocks):
                images = {}
                for page in pages:
                    name = page.get("preview_file")
                    if name and name not in sent:
                        images[name] = base64.b64encode((Path(scratch) / name).read_bytes()).decode()
                        sent.add(name)
                checkpoint(pages, blocks, images)
            return parse_document(source, scratch, local_checkpoint, skip_pages=skip_pages, page_options=page_options)
    import httpx
    complete = None
    with open(source, "rb") as file, httpx.Client(timeout=httpx.Timeout(660, connect=10), trust_env=False) as client:
        with client.stream("POST", parser_url + "/parse", files={"file": (Path(source).name, file, "application/octet-stream")}, data={"skip_pages": json.dumps(skip_pages or []), "page_options": json.dumps(page_options or {})}) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if len(line) > 40 * 1024 * 1024:
                    raise ValueError("parser_response_size_limit")
                event = json.loads(line)
                if event["type"] == "checkpoint":
                    checkpoint(event["pages"], event["blocks"], event.get("previews", {}))
                elif event["type"] == "complete":
                    complete = event["result"]
                elif event["type"] == "error":
                    raise ValueError(event["error"])
    if complete is None:
        raise ValueError("parser_stream_incomplete")
    return complete

