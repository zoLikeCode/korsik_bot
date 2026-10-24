"""Scoped knowledge administration and reproducible local regression checks.

Control expectations are append-only. A manual review never changes measured
results. Purge revokes access before asynchronous, retryable physical erasure.
"""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
import uuid
import httpx
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import select, text

from .auth import get_actor, user_actor
from .common import audit, check_etag, commit, enqueue, etag, get_entity, require_role, save_entity, serialize
from .db import get_db, now
from .models import Entity, IdempotencyRecord, Message, Outbox, TransportDelivery, User
from .parsing import digest
from . import knowledge as kb
from . import retrieval
from .settings import get_settings

router = APIRouter()
HR = ("HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT")


@router.get("/knowledge/maintenance-operations")
def maintenance_operations(db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, "HR_ADMIN")
    from .domain import operation_authorize, operation_view
    rows = db.scalars(select(Entity).where(Entity.kind == "operation", Entity.owner_id == actor.id, Entity.data["type"].as_string().in_(["KNOWLEDGE_PURGE", "KNOWLEDGE_ROLLBACK"])).order_by(Entity.updated_at.desc()).limit(500)).all()
    result = []
    for row in rows:
        if row.data.get("type") not in {"KNOWLEDGE_PURGE", "KNOWLEDGE_ROLLBACK"}:
            continue
        try:
            operation_authorize(db, actor, row)
        except HTTPException:
            continue
        result.append(operation_view(row))
        if len(result) == 100:
            break
    return {"items": result}


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PreviewProfile(ClosedModel):
    legal_entity_id: str = Field(min_length=1, max_length=120)
    site_id: str = Field(min_length=1, max_length=120)
    org_unit_id: str | None = Field(default=None, min_length=1, max_length=100)
    employment_type: Literal["OFFICE", "PRODUCTION", "OTHER"]
    employment_stage: Literal["PREBOARDING", "ACTIVE"]
    as_of: AwareDatetime


class Expectation(ClosedModel):
    mode: Literal["APPROVED_CARD", "EXTRACT", "CLARIFY", "HANDOFF", "LOCAL_GENERATED"]
    evidence_revision_ids: list[str] = Field(default_factory=list, max_length=30)
    evidence_block_ids: list[str] = Field(default_factory=list, max_length=100)
    instruction_revision_id: str | None = None
    must_include_literals: list[str] = Field(default_factory=list, max_length=30)
    must_exclude_literals: list[str] = Field(default_factory=list, max_length=30)
    critical: bool
    review_rubric: str = Field(min_length=10, max_length=4000)


class EvidenceDependency(ClosedModel):
    source_revision_id: str
    source_extraction_revision_id: str
    source_block_ids: list[str] = Field(min_length=1, max_length=50)
    source_block_sha256: dict[str, str]
    source_block_image_sha256: dict[str, str] = Field(default_factory=dict)
    rationale: str = Field(min_length=3, max_length=2000)


class ControlQuestionInput(ClosedModel):
    question: str = Field(min_length=3, max_length=2000)
    preview_profile: PreviewProfile
    expectation: Expectation
    source_dependencies: list[EvidenceDependency] = Field(default_factory=list, max_length=100)
    reason: str = Field(min_length=10, max_length=2000)


class ManualCheck(ClosedModel):
    check_id: str = Field(min_length=1, max_length=200)
    status: Literal["PASS", "FAIL"]
    note: str = Field(default="", max_length=4000)


class QaReview(ClosedModel):
    checks: list[ManualCheck] = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=3, max_length=2000)


class RollbackInput(ClosedModel):
    target_revision_id: str
    expected_snapshot_id: str
    reason: str = Field(min_length=3, max_length=4000)


class PurgeInput(ClosedModel):
    expected_snapshot_id: str
    retention_policy_id: str
    reason: str = Field(min_length=3, max_length=4000)


def _profile_allowed(db, actor, profile):
    if actor.role not in HR:
        return False
    if not profile.get("site_id") or not profile.get("legal_entity_id"):
        return False
    site = db.scalar(select(Entity).where(Entity.id == profile["site_id"], Entity.kind == "site").execution_options(populate_existing=True))
    if not site or site.kind != "site":
        return False
    if site.data.get("legal_entity_id") != profile["legal_entity_id"]:
        return False
    if profile.get("org_unit_id"):
        from .organization import _unit_read, visible_employees
        unit = db.scalar(select(Entity).where(Entity.id == profile["org_unit_id"], Entity.kind == "org_unit", Entity.status == "ACTIVE").execution_options(populate_existing=True))
        if (not unit or unit.data.get("legal_entity_id") != profile["legal_entity_id"]
                or unit.data.get("site_id") and unit.data["site_id"] != profile["site_id"]
                or not _unit_read(actor, unit, visible_employees(db, actor))):
            return False
    grant = actor.scope or {}
    if grant.get("all"):
        return True
    sites, legal = grant.get("site_ids", []), grant.get("legal_entity_ids", [])
    if sites and profile["site_id"] not in sites:
        return False
    if legal and profile["legal_entity_id"] not in legal:
        return False
    return bool(sites or legal)


def _control(db, actor, control_id, lock=False):
    require_role(actor, *HR)
    if lock:
        kb._knowledge_write_lock(db)
    control = get_entity(db, control_id, "knowledge_control_question", lock=lock)
    current = get_entity(db, control.data.get("current_version_id", ""), "knowledge_control_question_version")
    if current.status == "PURGED" or not _profile_allowed(db, actor, current.data.get("preview_profile", {})):
        raise HTTPException(404, "control_question_not_found")
    return control, current


def _version_view(control, version):
    return {**serialize(version), "control_question_id": control.id,
            "control_question_version_id": version.id, "etag": etag(control)}


def _validate_control(db, actor, payload):
    data = payload.model_dump(mode="json")
    if not _profile_allowed(db, actor, data["preview_profile"]):
        raise HTTPException(403, "preview_profile_outside_grant")
    expectation = data["expectation"]
    for field in ("evidence_revision_ids", "evidence_block_ids", "must_include_literals", "must_exclude_literals"):
        values = expectation[field]
        if len(values) != len(set(values)) or any(not value.strip() or len(value) > 2000 for value in values):
            raise HTTPException(422, "duplicate_or_empty_expectation:" + field)
    if set(expectation["must_include_literals"]) & set(expectation["must_exclude_literals"]):
        raise HTTPException(422, "contradictory_literal_expectation")
    if expectation["mode"] in {"CLARIFY", "HANDOFF"} and (expectation["evidence_revision_ids"] or expectation["evidence_block_ids"]):
        raise HTTPException(422, "abstention_expectation_cannot_require_citations")
    references = set(expectation["evidence_revision_ids"])
    if expectation.get("instruction_revision_id"):
        references.add(expectation["instruction_revision_id"])
    for dependency in data["source_dependencies"]:
        revision, _ = kb._revision(db, actor, dependency["source_revision_id"])
        references.add(revision.id)
        extraction = kb._extraction(db, revision, dependency["source_extraction_revision_id"])
        blocks = {b["id"]: b for b in extraction.data["blocks"]}
        ids = dependency["source_block_ids"]
        if len(ids) != len(set(ids)) or set(ids) != set(dependency["source_block_sha256"]):
            raise HTTPException(422, "dependency_hash_mapping_must_be_exact")
        for block_id in ids:
            block = blocks.get(block_id)
            if block is None or digest(block.get("text", "").encode()) != dependency["source_block_sha256"][block_id]:
                raise HTTPException(409, "dependency_text_changed")
            if block["kind"] == "FIGURE" and dependency["source_block_image_sha256"].get(block_id) != block.get("image_sha256"):
                raise HTTPException(409, "dependency_image_changed")
    all_blocks = set()
    for revision_id in references:
        revision, _ = kb._revision(db, actor, revision_id)
        all_blocks.update(b["id"] for b in kb._extraction(db, revision).data["blocks"])
    if not set(expectation["evidence_block_ids"]).issubset(all_blocks):
        raise HTTPException(422, "expected_blocks_must_belong_to_declared_sources")
    return data


@router.get("/knowledge/control-questions")
def list_control_questions(actor=Depends(get_actor), db=Depends(get_db)):
    require_role(actor, *HR)
    result = []
    for control in db.scalars(select(Entity).where(Entity.kind == "knowledge_control_question", Entity.status == "ACTIVE")):
        version = db.get(Entity, control.data.get("current_version_id"))
        if version and version.status != "PURGED" and _profile_allowed(db, actor, version.data.get("preview_profile", {})):
            result.append(_version_view(control, version))
    return {"items": result, "next_cursor": None}


@router.post("/knowledge/control-questions", status_code=201)
def create_control_question(body: ControlQuestionInput, response: Response, actor=Depends(get_actor), db=Depends(get_db)):
    require_role(actor, *HR)
    kb._knowledge_write_lock(db)
    data = _validate_control(db, actor, body)
    control = save_entity(db, "knowledge_control_question", {}, owner_id=actor.id)
    db.flush()
    version = save_entity(db, "knowledge_control_question_version", {**data, "version_no": 1,
                          "author_id": actor.id, "fingerprint": digest(data)}, parent_id=control.id, status="IMMUTABLE")
    db.flush()
    kb._update(control, current_version_id=version.id)
    audit(db, actor, "knowledge.control_question.created", control.id, {"version_id": version.id}, reason=body.reason)
    commit(db)
    response.headers["ETag"] = etag(control)
    return _version_view(control, version)


@router.get("/knowledge/control-questions/{control_question_id}")
def get_control_question(control_question_id: str, response: Response, actor=Depends(get_actor), db=Depends(get_db)):
    control, version = _control(db, actor, control_question_id)
    response.headers["ETag"] = etag(control)
    return _version_view(control, version)


@router.post("/knowledge/control-questions/{control_question_id}/versions")
def version_control_question(control_question_id: str, body: ControlQuestionInput, request: Request,
                             response: Response, actor=Depends(get_actor), db=Depends(get_db)):
    control, previous = _control(db, actor, control_question_id, lock=True)
    check_etag(request, control)
    data = _validate_control(db, actor, body)
    version = save_entity(db, "knowledge_control_question_version", {**data, "version_no": previous.data["version_no"] + 1,
                          "author_id": actor.id, "previous_version_id": previous.id, "fingerprint": digest(data)},
                          parent_id=control.id, status="IMMUTABLE")
    db.flush()
    kb._update(control, current_version_id=version.id)
    audit(db, actor, "knowledge.control_question.versioned", control.id, {"version_id": version.id}, reason=body.reason)
    commit(db)
    response.headers["ETag"] = etag(control)
    return _version_view(control, version)


@router.get("/knowledge/control-question-versions/{control_question_version_id}")
def get_control_question_version(control_question_version_id: str, actor=Depends(get_actor), db=Depends(get_db)):
    version = get_entity(db, control_question_version_id, "knowledge_control_question_version")
    control, _ = _control(db, actor, version.parent_id)
    if version.status == "PURGED" or not _profile_allowed(db, actor, version.data["preview_profile"]):
        raise HTTPException(404, "control_question_version_not_found")
    return _version_view(control, version)


def evaluate_control_suite(db, candidate, actor, checkpoint=None):
    """Run real local answers against one frozen candidate; never send messages.

    Synthetic employee/conversation entities exist only inside a rolled-back
    savepoint because the shared retrieval entry point resolves its own profile.
    No employment episode, identity binding, jobs or outbox is created.
    """
    require_role(actor, *HR)
    base = get_entity(db, candidate.data["base_snapshot_id"], "knowledge_snapshot")
    membership = dict(base.data.get("members", {}))
    for revision_id, pin in candidate.data["members"].items():
        membership[pin["document_id"]] = revision_id
    candidate_chunks = {cid for ids in candidate.data["chunk_ids_by_revision"].values() for cid in ids}
    changed = set(candidate.data["members"])
    results = []
    controls = list(db.scalars(select(Entity).where(Entity.kind == "knowledge_control_question", Entity.status == "ACTIVE")))
    for control in controls:
        if checkpoint:
            checkpoint(len(results))
        version = db.get(Entity, control.data.get("current_version_id"))
        if not version or version.status != "IMMUTABLE" or not _profile_allowed(db, actor, version.data["preview_profile"]):
            continue
        data, expectation = version.data, version.data["expectation"]
        # Additions can change ranking and answer routing even if a test's
        # original source was not edited. Run the entire scoped frozen suite.
        profile = data["preview_profile"]
        as_of = datetime.fromisoformat(profile["as_of"].replace("Z", "+00:00"))
        permitted = {}
        for doc_id, revision_id in membership.items():
            revision, doc = db.get(Entity, revision_id), db.get(Entity, doc_id)
            if doc and revision and kb._scope_access(actor, doc) and retrieval.scope_matches(doc.data.get("scope"), profile, db=db) and retrieval.is_effective(revision.data, as_of):
                permitted[revision_id] = (doc, revision)
        savepoint = db.begin_nested()
        try:
            employee = save_entity(db, "employee", {**profile, "synthetic_qa_only": True}, status="QA_ONLY")
            db.flush()
            conversation = save_entity(db, "conversation", {"employee_id": employee.id, "mode": "BOT", "synthetic_qa_only": True}, status="QA_ONLY")
            db.flush()
            actual = retrieval.answer_question(db, conversation, data["question"], _preview={"permitted": permitted,
                "changed_revision_ids": changed, "chunk_ids": candidate_chunks})
            actual = copy.deepcopy(actual)
        except Exception as exc:
            actual = {"mode": "ERROR", "text": "", "citations": [], "reason": type(exc).__name__}
        finally:
            savepoint.rollback()
        if checkpoint:
            checkpoint(len(results) + 1)
        cited_revisions = {c.get("revision_id") for c in actual.get("citations", [])}
        cited_blocks = {c.get("block_id") for c in actual.get("citations", [])}
        text = actual.get("text", "")
        checks = {"mode": actual.get("mode") == expectation["mode"],
                  "required_literals": all(v.casefold() in text.casefold() for v in expectation["must_include_literals"]),
                  "excluded_literals": not any(v.casefold() in text.casefold() for v in expectation["must_exclude_literals"]),
                  "revision_ids": not expectation["evidence_revision_ids"] or cited_revisions == set(expectation["evidence_revision_ids"]),
                  "block_ids": not expectation["evidence_block_ids"] or cited_blocks == set(expectation["evidence_block_ids"]),
                  "instruction_revision": not expectation.get("instruction_revision_id") or expectation["instruction_revision_id"] in cited_revisions,
                  "current_scope": cited_revisions.issubset(permitted)}
        if expectation["mode"] in {"HANDOFF", "CLARIFY"}:
            checks["no_citations_when_abstaining"] = not actual.get("citations")
        results.append({"control_question_id": control.id, "control_question_version_id": version.id,
                        "expectation_fingerprint": data["fingerprint"], "critical": expectation["critical"],
                        "passed": all(checks.values()), "checks": checks, "actual_mode": actual.get("mode"),
                        "actual_text": text, "citation_revision_ids": sorted(cited_revisions),
                        "citation_block_ids": sorted(cited_blocks), "manual_check_id": f"CONTROL:{version.id}",
                        "manual_review_status": "PENDING", "review_rubric": expectation["review_rubric"]})
    return {"status": "NO_CONTROL_QUESTIONS" if not results else "PASSED" if all(r["passed"] for r in results) else "FAILED",
            "critical_failures": sum(r["critical"] and not r["passed"] for r in results), "results": results,
            "manual_review_status": "PENDING" if results else "NOT_APPLICABLE", "external_judge_used": False,
            "fingerprint": digest(results), "semantic_quality": "REQUIRES_HUMAN_RUBRIC_REVIEW" if results else "NOT_MEASURED"}


@router.post("/knowledge/qa-artifacts/{qa_artifact_id}/review")
def review_qa(qa_artifact_id: str, body: QaReview, request: Request, response: Response,
              actor=Depends(get_actor), db=Depends(get_db)):
    require_role(actor, *HR)
    kb._knowledge_write_lock(db)
    qa = get_entity(db, qa_artifact_id, "knowledge_qa", lock=True)
    revision, _ = kb._revision(db, actor, qa.data["revision_id"], True)
    check_etag(request, qa)
    if revision.status == "PUBLISHED":
        raise HTTPException(409, "published_qa_is_immutable_create_a_new_revision")
    candidate = get_entity(db, qa.data["candidate_snapshot_id"], "knowledge_candidate")
    if qa.data["content_fingerprint"] != revision.data.get("content_fingerprint") or revision.data.get("candidate_snapshot_id") != candidate.id:
        raise HTTPException(409, "qa_content_or_candidate_changed")
    suite = qa.data.get("control_suite", {})
    if qa.status != "PASSED" or suite.get("critical_failures", 0) or any(not r.get("passed") and r.get("critical") for r in suite.get("results", [])):
        raise HTTPException(409, "machine_failure_cannot_be_overridden")
    required = {r["manual_check_id"] for r in suite.get("results", [])} or {"OCR_NUMBERS", "CITATIONS", "ANSWER_MEANING"}
    checks = [c.model_dump() for c in body.checks]
    if len({c["check_id"] for c in checks}) != len(checks) or {c["check_id"] for c in checks} != required:
        raise HTTPException(422, {"code": "manual_checks_must_cover_artifact", "required_check_ids": sorted(required)})
    kb._update(qa, manual_checks=checks, manual_review_status="PASS" if all(c["status"] == "PASS" for c in checks) else "FAIL",
               manual_reviewed_by=actor.id, manual_reviewed_at=now().isoformat(), manual_review_reason=body.reason,
               manual_review_fingerprint=digest({"qa_fingerprint": qa.data.get("fingerprint"), "checks": checks, "reason": body.reason}))
    if suite.get("results"):
        kb._update(qa, control_suite={**suite, "manual_review_status": qa.data["manual_review_status"]})
    audit(db, actor, "knowledge.qa.manual_reviewed", qa.id, {"outcome": qa.data["manual_review_status"]}, reason=body.reason)
    commit(db)
    response.headers["ETag"] = etag(qa)
    return serialize(qa)


@router.get("/knowledge/snapshots/{kb_snapshot_id}")
def get_snapshot(kb_snapshot_id: str, actor=Depends(get_actor), db=Depends(get_db)):
    require_role(actor, *HR)
    snapshot = get_entity(db, kb_snapshot_id, "knowledge_snapshot")
    members = {}
    for doc_id, revision_id in snapshot.data.get("members", {}).items():
        doc = db.get(Entity, doc_id)
        if doc and doc.status not in {"PURGE_PENDING", "PURGED"} and kb._scope_access(actor, doc):
            members[doc_id] = revision_id
    allowed_revisions = set(members.values())
    return {"id": snapshot.id, "kb_snapshot_id": snapshot.id, "parent_snapshot_id": snapshot.data.get("previous_snapshot_id"),
            "created_at": snapshot.created_at, "members": members, "scope_filtered": len(members) != len(snapshot.data.get("members", {})),
            "publications": [{"document_id": did, "revision_id": rid,
                "publication_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"korsik:publication:{snapshot.id}:{did}:{rid}"))} for did, rid in members.items()],
            "projections": {rid: p for rid, p in snapshot.data.get("projections", {}).items() if rid in allowed_revisions}}


def _internal_request(entity):
    return Request({"type": "http", "headers": [(b"if-match", etag(entity).encode())], "method": "POST", "path": "/internal-authoring"})


@router.post("/knowledge/documents/{document_id}/rollback", status_code=202)
def rollback_document(document_id: str, body: RollbackInput, request: Request, actor=Depends(get_actor), db=Depends(get_db)):
    require_role(actor, "HR_ADMIN")
    doc = kb._doc(db, actor, document_id, True)
    check_etag(request, doc)
    head = kb._head(db)
    if head.data["snapshot_id"] != body.expected_snapshot_id:
        raise HTTPException(409, "stale_snapshot")
    target, _ = kb._revision(db, actor, body.target_revision_id)
    if target.parent_id != doc.id or target.status != "PUBLISHED":
        raise HTTPException(409, "rollback_target_must_be_previously_published")
    if doc.data.get("active_revision_id") == target.id:
        raise HTTPException(409, "target_already_active")
    extraction = kb._extraction(db, target)
    if kb._blockers(extraction):
        raise HTTPException(409, "rollback_source_review_incomplete")
    original_qa = db.get(Entity, target.data.get("qa_artifact_id"))
    if not original_qa or original_qa.status != "PASSED" or original_qa.data.get("content_fingerprint") != target.data.get("content_fingerprint"):
        raise HTTPException(409, "rollback_target_qa_unavailable")
    if target.data.get("source_path"):
        path = Path(target.data["source_path"])
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != target.data.get("sha256"):
            raise HTTPException(409, "rollback_original_missing_or_changed")
    data = copy.deepcopy(target.data)
    for key in ("published_at", "published_by", "kb_snapshot_id", "candidate_snapshot_id", "qa_artifact_id", "review_fingerprint", "impact_review_id",
                "latest_qa_operation_id", "qa_operation_id", "latest_candidate_operation_id", "candidate_build_operation_id", "rollback_operation_id"):
        data.pop(key, None)
    data.update(rollback_of_revision_id=target.id, rollback_reason=body.reason,
                metadata_snapshot=kb._document_metadata_snapshot(doc),
                scope_fingerprint=digest(doc.data.get("scope", {})))
    clone = save_entity(db, "document_revision", data, status="REVIEW_REQUIRED", parent_id=doc.id, owner_id=actor.id)
    db.flush()
    kb._new_extraction(db, clone, extraction.data, actor)
    audit(db, actor, "knowledge.rollback.prepared", doc.id, {"target_revision_id": target.id, "new_revision_id": clone.id}, reason=body.reason)
    commit(db)
    candidate_operation = kb.build_candidate(clone.id, {"base_snapshot_id": body.expected_snapshot_id}, _internal_request(clone), actor, db)
    candidate_operation_id = candidate_operation.get("operation_id", candidate_operation["id"])
    operation = kb._operation(db, "KNOWLEDGE_ROLLBACK", {
        "revision_id": clone.id, "target_revision_id": target.id, "document_id": doc.id,
        "candidate_operation_id": candidate_operation_id, "expected_snapshot_id": body.expected_snapshot_id,
        "reason": body.reason, "stage": "WAITING_CANDIDATE", "published": False,
        "deadline_at": (now() + timedelta(hours=2)).isoformat()}, status="PENDING")
    operation.owner_id = actor.id
    kb._update(operation, operation_scope=copy.deepcopy(doc.data.get("scope", {})))
    kb._update(clone, rollback_operation_id=operation.id)
    kb._update(doc, latest_revision_id=clone.id)
    db.flush()
    enqueue(db, "KNOWLEDGE_ROLLBACK", {"operation_id": operation.id}, f"knowledge-rollback:{operation.id}:0")
    audit(db, actor, "knowledge.document.rollback_requested", doc.id, {"target_revision_id": target.id, "new_revision_id": clone.id}, reason=body.reason)
    commit(db)
    return serialize(operation)


def _dependent_documents(db, revision_ids):
    affected = set()
    changed = set(revision_ids)
    while True:
        additions = set()
        for revision in db.scalars(select(Entity).where(Entity.kind == "document_revision", Entity.status == "PUBLISHED")):
            if any(d.get("source_revision_id") in changed for d in revision.data.get("dependencies", [])):
                additions.add(revision.parent_id)
                changed.add(revision.id)
        if additions.issubset(affected):
            return affected
        affected.update(additions)


@router.delete("/knowledge/documents/{document_id}", status_code=202)
def purge_document(document_id: str, body: PurgeInput, request: Request, actor=Depends(get_actor), db=Depends(get_db)):
    require_role(actor, "HR_ADMIN")
    doc = kb._doc(db, actor, document_id, True)
    check_etag(request, doc)
    policy = get_entity(db, body.retention_policy_id, "retention_policy")
    if not policy.data.get("configured") or policy.status != "ACTIVE" or policy.data.get("data_class") not in {"KNOWLEDGE_DOCUMENT", "knowledge_document"}:
        raise HTTPException(409, "configured_knowledge_retention_policy_required")
    if policy.data.get("hold") or doc.data.get("legal_hold"):
        raise HTTPException(409, "legal_hold_prevents_purge")
    backup_days = policy.data.get("backup_retention_days")
    if type(backup_days) is not int or not 0 <= backup_days <= 36500:
        raise HTTPException(409, "backup_expiry_policy_must_be_configured")
    head = kb._head(db)
    if head.data["snapshot_id"] != body.expected_snapshot_id:
        raise HTTPException(409, "stale_snapshot")
    revisions = list(db.scalars(select(Entity).where(Entity.kind == "document_revision", Entity.parent_id == doc.id)))
    revision_ids = [r.id for r in revisions]
    dependents = _dependent_documents(db, revision_ids) - {doc.id}
    for dependent_id in dependents:
        dependent = get_entity(db, dependent_id, "document", lock=True)
        dependent.status = "ARCHIVED"
        kb._update(dependent, authz_epoch=dependent.data.get("authz_epoch", 1) + 1, withdrawal_reason="SOURCE_PURGE")
    doc.status = "PURGE_PENDING"
    kb._update(doc, source_revoked=True, active_revision_id=None, authz_epoch=doc.data.get("authz_epoch", 1) + 1)
    old = get_entity(db, head.data["snapshot_id"], "knowledge_snapshot")
    snapshot = save_entity(db, "knowledge_snapshot", {"members": {did: rid for did, rid in old.data.get("members", {}).items()
                           if did not in dependents | {doc.id}}, "previous_snapshot_id": old.id, "reason": "Knowledge purge revocation"}, status="PUBLISHED")
    db.flush()
    kb._update(head, snapshot_id=snapshot.id, authz_epoch=head.data.get("authz_epoch", 1) + 1)
    operation = kb._operation(db, "KNOWLEDGE_PURGE", {"document_id": doc.id, "kb_snapshot_id": snapshot.id,
        "retention_policy_id": policy.id, "backup_expiry_at": (now() + timedelta(days=backup_days)).isoformat()}, status="PENDING")
    operation.owner_id = actor.id
    kb._update(operation, operation_scope=copy.deepcopy(doc.data.get("scope", {})))
    db.flush()
    enqueue(db, "KNOWLEDGE_PURGE", {"document_id": doc.id, "revision_ids": revision_ids, "operation_id": operation.id,
            "retention_policy_id": policy.id, "policy_version": policy.data.get("policy_version", policy.version), "reason": body.reason},
            f"knowledge-purge:{doc.id}:{operation.id}")
    audit(db, actor, "knowledge.document.purge_requested", doc.id, {"retention_policy_id": policy.id, "dependent_documents_withdrawn": len(dependents)}, reason=body.reason)
    commit(db)
    return serialize(operation)


def _process_rollback_job(db, job):
    # Lock and verify the current executor before any stage transition. Keep the
    # Job → knowledge gate → operation/document fence through outer worker commit.
    kb._lock_lease_job(db, job, job.payload.get("_lease_token"))
    operation = get_entity(db, job.payload["operation_id"], "operation", lock=True)
    if operation.status in {"SUCCEEDED", "FAILED", "CANCELLED", "WAITING_REVIEW"}:
        return True
    user = db.get(User, operation.owner_id)
    if not user or not user.enabled or user.role != "HR_ADMIN":
        operation.status = "FAILED"
        kb._update(operation, error={"code": "rollback_authorization_revoked"}, stage="FAILED")
        return True
    actor = user_actor(user)
    try:
        revision, doc = kb._revision(db, actor, operation.data["revision_id"], True)
        if not operation.data.get("qa_operation_id"):
            candidate_operation = get_entity(db, operation.data["candidate_operation_id"], "operation")
            if candidate_operation.status in {"PENDING", "QUEUED", "RUNNING", "PROCESSING"}:
                if now() >= datetime.fromisoformat(operation.data["deadline_at"]):
                    raise HTTPException(409, "rollback_candidate_deadline_exceeded")
                attempt = int(job.payload.get("wait_attempt", 0)) + 1
                enqueue(db, "KNOWLEDGE_ROLLBACK", {"operation_id": operation.id, "wait_attempt": attempt},
                    f"knowledge-rollback:{operation.id}:{attempt}", run_at=now() + timedelta(seconds=15))
                return True
            if candidate_operation.status != "SUCCEEDED":
                raise HTTPException(409, "rollback_candidate_failed")
            impact = get_entity(db, revision.data["impact_review_id"], "knowledge_impact_review")
            if impact.status != "PASSED":
                operation.status = "WAITING_REVIEW"
                kb._update(operation, stage="IMPACT_REVIEW_REQUIRED", requires_manual_review=True)
                return True
            qa_operation = kb._queue_qa(revision.id, {}, _internal_request(revision), actor, db)
            kb._update(operation, qa_operation_id=qa_operation.id, stage="WAITING_QA")
        qa_operation = get_entity(db, operation.data["qa_operation_id"], "operation")
        if qa_operation.status in {"PENDING", "QUEUED", "RUNNING", "PROCESSING"}:
            if now() >= datetime.fromisoformat(operation.data["deadline_at"]):
                raise HTTPException(409, "rollback_qa_deadline_exceeded")
            attempt = int(job.payload.get("wait_attempt", 0)) + 1
            enqueue(db, "KNOWLEDGE_ROLLBACK", {"operation_id": operation.id, "wait_attempt": attempt},
                    f"knowledge-rollback:{operation.id}:{attempt}", run_at=now() + timedelta(seconds=15))
            return True
        if qa_operation.status != "SUCCEEDED":
            raise HTTPException(409, "rollback_current_qa_failed")
        qa = get_entity(db, revision.data.get("qa_artifact_id", ""), "knowledge_qa")
        if qa.status != "PASSED":
            raise HTTPException(409, "rollback_current_qa_failed")
        if qa.data.get("control_suite", {}).get("results") and qa.data.get("manual_review_status") != "PASS":
            operation.status = "WAITING_REVIEW"
            kb._update(operation, stage="MANUAL_REVIEW_REQUIRED", requires_manual_review=True, qa_artifact_id=qa.id)
            return True
        kb._review_revision(revision.id, {"action": "APPROVE", "reason": operation.data["reason"]},
                            _internal_request(revision), actor, db)
        snapshot = kb._publish(db, revision, doc, actor, operation.data["expected_snapshot_id"])
        operation.status = "SUCCEEDED"
        kb._update(operation, stage="PUBLISHED", kb_snapshot_id=snapshot.id, published=True, completed_at=now().isoformat())
        audit(db, actor, "knowledge.document.rolled_back", doc.id, {"revision_id": revision.id, "operation_id": operation.id}, reason=operation.data["reason"])
    except HTTPException as exc:
        operation.status = "FAILED"
        kb._update(operation, stage="FAILED", error={"code": exc.detail, "http_status": exc.status_code})
    db.flush()
    return True


def process_knowledge_admin_job(db, job):
    if job.kind == "KNOWLEDGE_ROLLBACK":
        return _process_rollback_job(db, job)
    if job.kind != "KNOWLEDGE_PURGE":
        return False
    # Keep the fresh Job row locked through all irreversible erasures and final
    # worker commit. A reclaimer cannot replace this executor while it erases.
    token = job.payload.get("_lease_token")
    db.refresh(job, with_for_update=True)
    if job.payload.get("_lease_token") != token:
        raise kb.IngestLeaseLost("Purge worker lease superseded")
    if token:
        until = job.lease_until
        if until and until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        if job.status != "PROCESSING" or not until or until <= now():
            raise kb.IngestLeaseLost("Purge worker lease expired")
    if db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(76810108)"))
    kb._knowledge_write_lock(db)
    operation = get_entity(db, job.payload["operation_id"], "operation", lock=True)
    doc = get_entity(db, job.payload["document_id"], "document", lock=True)
    if doc.status == "PURGED":
        return True
    if doc.status != "PURGE_PENDING" or not doc.data.get("source_revoked"):
        raise RuntimeError("purge_revocation_missing")
    policy = get_entity(db, job.payload["retention_policy_id"], "retention_policy", lock=True)
    if policy.status != "ACTIVE" or not policy.data.get("configured"):
        raise RuntimeError("purge_policy_no_longer_enabled")
    if policy.data.get("hold") or doc.data.get("legal_hold"):
        raise RuntimeError("legal_hold_added_after_purge_request")
    revision_ids = set(job.payload["revision_ids"])
    if retrieval.MODE != "CORE" and revision_ids:
        try:
            retrieval._request(retrieval.QDRANT + f"/collections/{retrieval.COLLECTION}/points/delete?wait=true",
                {"filter": {"must": [{"key": "revision_id", "match": {"any": sorted(revision_ids)}}]}}, timeout=60)
        except httpx.HTTPStatusError as exc:
            # An already-removed collection contains no remaining vectors.
            if exc.response.status_code != 404:
                raise
    # Restrict erasure to the configured knowledge tree; metadata cannot delete
    # an arbitrary operator path even if a database row has been corrupted.
    root = (kb.DATA / "knowledge").resolve()
    for revision_id in revision_ids:
        folder = (root / revision_id).resolve()
        if folder.parent != root:
            raise RuntimeError("unsafe_purge_path")
        if folder.exists():
            shutil.rmtree(folder)
    affected_answers, affected_assets = set(), set()
    for row in list(db.scalars(select(Entity))):
        refs = set()
        if row.kind == "asset" and row.data.get("knowledge_revision_id") in revision_ids:
            affected_assets.add(row.id)
            asset_path = Path(row.data.get("storage_path", "")).resolve()
            extract_roots = {(kb.DATA / "citation-extracts").resolve(), (Path(get_settings().blob_root) / "citation-extracts").resolve()}
            if any(asset_path.is_relative_to(allowed) for allowed in extract_roots):
                if asset_path.is_file():
                    asset_path.unlink()
            elif not asset_path.is_relative_to(root):
                raise RuntimeError("unsafe_citation_asset_purge_path")
            row.data = {"source_purged": True, "knowledge_document_id": doc.id}
            row.status = "PURGED"
        if row.kind == "answer":
            refs = {c.get("revision_id") for c in row.data.get("citations", [])}
        if refs & revision_ids:
            affected_answers.add(row.id)
            row.data = {"conversation_id": row.data.get("conversation_id"), "employee_id": row.data.get("employee_id"),
                        "text": "Источник удалён по политике хранения.", "citations": [], "source_purged": True}
            row.status = "PURGED"
        elif (row.kind in {"extraction", "knowledge_chunk"} and row.parent_id in revision_ids
              or row.kind in {"citation", "knowledge_qa", "knowledge_impact_review"} and row.data.get("revision_id") in revision_ids):
            row.data = {"source_purged": True, "document_id": doc.id}
            row.status = "PURGED"
        elif row.kind == "document_revision" and row.id in revision_ids:
            row.data = {"document_id": doc.id, "source_purged": True}
            row.status = "PURGED"
        elif row.kind == "knowledge_control_question_version" and (
                any(d.get("source_revision_id") in revision_ids for d in row.data.get("source_dependencies", []))
                or set(row.data.get("expectation", {}).get("evidence_revision_ids", [])) & revision_ids
                or row.data.get("expectation", {}).get("instruction_revision_id") in revision_ids):
            row.data = {"source_purged": True, "fingerprint": row.data.get("fingerprint")}
            row.status = "PURGED"
        elif row.kind in {"operation", "knowledge_candidate", "knowledge_feedback_analysis", "feedback_analysis"} and row.id != operation.id:
            encoded = json.dumps(row.data, ensure_ascii=False)
            if any(marker in encoded for marker in revision_ids):
                row.data = {"source_purged": True, "document_id": doc.id}
                row.status = "PURGED"
    affected_messages = set()
    for message in db.scalars(select(Message)):
        if (message.data.get("answer_id") in affected_answers
                or any(c.get("revision_id") in revision_ids for c in message.data.get("citations", []))
                or any(a in affected_assets for a in message.data.get("asset_ids", []))):
            affected_messages.add(message.id)
            message.text = "Источник удалён по политике хранения."
            message.data = {"source_purged": True}
    for outbox in db.scalars(select(Outbox)):
        if (outbox.data.get("answer_id") in affected_answers or outbox.data.get("message_id") in affected_messages
                or any(c.get("revision_id") in revision_ids for c in outbox.data.get("citations", []))
                or any((a.get("asset_id") if isinstance(a, dict) else a) in affected_assets for a in outbox.attachments)):
            outbox.text = ""
            outbox.attachments = []
            outbox.callback_buttons = []
            outbox.data = {"source_purged": True}
            outbox.status = "CANCELLED"
            delivery = db.scalar(select(TransportDelivery).where(TransportDelivery.outbox_id == outbox.id))
            if delivery:
                delivery.status = "CANCELLED"
    markers = revision_ids | {doc.id} | affected_answers | affected_assets
    for record in db.scalars(select(IdempotencyRecord)):
        encoded = json.dumps(record.response, ensure_ascii=False)
        if any(marker in encoded for marker in markers):
            record.response = {"detail": "source_purged"}
            record.status_code = 410
    tombstone = {"document_id": doc.id, "retention_policy_id": policy.id, "policy_version": job.payload["policy_version"],
                 "purged_at": now().isoformat(), "backup_expiry_at": operation.data.get("backup_expiry_at"),
                 "external_messenger_copies_recalled": False}
    save_entity(db, "knowledge_tombstone", tombstone, status="PURGED", parent_id=doc.id)
    doc.data = {**tombstone, "source_revoked": True}
    doc.status = "PURGED"
    operation.status = "SUCCEEDED"
    kb._update(operation, **tombstone, live_data_erased=True)
    db.flush()
    return True
