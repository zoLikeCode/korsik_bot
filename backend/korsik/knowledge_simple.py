"""Opt-in automatic private preparation; only an explicit HR click publishes."""
from copy import deepcopy
import hashlib
import re
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from .auth import get_actor, user_actor
from .common import audit, check_etag, commit, enqueue, etag, get_entity, require_role, serialize
from .db import get_db, now
from .models import Entity, Job, User
from .parsing import digest

router = APIRouter()
KIND = "KNOWLEDGE_SIMPLE_PREPARE"
PENDING = {"PENDING", "QUEUED", "RUNNING", "PROCESSING", "RETRY"}


def normalize(payload):
    """Keep undecoded figures visible without treating them as evidence."""
    result = deepcopy(payload)
    for block in result.get("blocks", []):
        described = block.get("description") and block.get("description_image_sha256") == block.get("image_sha256")
        if block.get("kind") == "FIGURE" and not described and block.get("disposition") not in {"REFERENCE_ONLY", "EXCLUDE_NONCONTENT"}:
            block.update(disposition="REFERENCE_ONLY", disposition_origin="AUTO_REFERENCE_ONLY")
    return result


def quality_blockers(extraction):
    """Technical quality; do not fabricate per-page or per-answer HR reviews."""
    data = extraction.data
    blockers = []
    if not data.get("pages"):
        blockers.append("NO_PHYSICAL_PAGES")
    for page in data.get("pages", []):
        if page.get("status") != "PROCESSED":
            blockers.append(f"PAGE_{page['page_no']}_NOT_PROCESSED")
        blockers.extend(f"PAGE_{page['page_no']}:{issue}" for issue in page.get("issues", []) if issue not in {"FIGURE_DESCRIPTION_REQUIRED", "SMARTART_OR_CHART_MANUAL_REVIEW", "PPTX_SOURCE_REVIEW_REQUIRED"})
    searchable = False
    for block in data.get("blocks", []):
        if block.get("disposition") in {"REFERENCE_ONLY", "EXCLUDE_NONCONTENT"}:
            continue
        if block.get("issues"):
            blockers.extend(f"BLOCK_{block['id']}:{issue}" for issue in block['issues'])
        if block.get("retry_visual_review_required"):
            blockers.append(f"BLOCK_{block['id']}_VISUAL_REVIEW_REQUIRED")
        kind = block.get("kind")
        if kind == "TABLE":
            grid = block.get("grid") or []
            if not grid or any(not isinstance(row, list) or not row for row in grid) or len({len(row) for row in grid if isinstance(row, list)}) != 1:
                blockers.append(f"TABLE_{block['id']}_INVALID_GRID")
            else:
                searchable |= any(str(cell).strip() for row in grid for cell in row)
        elif kind == "FIGURE":
            if not block.get("description") or block.get("description_image_sha256") != block.get("image_sha256"):
                blockers.append(f"FIGURE_{block['id']}_UNREVIEWED")
            else:
                searchable = True
        elif kind == "TEXT":
            searchable |= bool(str(block.get("text", "")).strip())
    if not searchable:
        blockers.append("NO_SEARCHABLE_TEXT_ADD_IMAGE_DESCRIPTION")
    return sorted(set(blockers))


def schedule(db, revision, actor=None, trigger="content"):
    if not revision.data.get("simple_workflow") or revision.status not in {"REVIEW_REQUIRED", "FAILED"} or not revision.data.get("latest_extraction_revision_id"):
        return
    actor_id = actor.id if actor else revision.data.get("simple_actor_id") or revision.owner_id
    from . import knowledge as kb
    if actor_id and revision.data.get("simple_actor_id") != actor_id:
        kb._update(revision, simple_actor_id=actor_id)
    enqueue(db, KIND, {"revision_id": revision.id, "actor_id": actor_id,
        "extraction_id": revision.data["latest_extraction_revision_id"], "content_fingerprint": revision.data["content_fingerprint"]},
        f"simple-prepare:{revision.id}:{revision.data['latest_extraction_revision_id']}:{trigger}")


def _request(entity):
    return Request({"type": "http", "headers": [(b"if-match", etag(entity).encode())]})


def _operation(db, revision, field):
    identifier = revision.data.get(field)
    return db.get(Entity, identifier) if identifier else None


def _preparation_pin(db, revision, doc):
    from . import knowledge as kb
    return digest({"extraction": revision.data.get("latest_extraction_revision_id"), "content": revision.data.get("content_fingerprint"),
        "scope": doc.data.get("scope", {}), "category": doc.data.get("document_category"),
        "head": kb._head(db).data['snapshot_id'], "profile": kb._qa_profile_fingerprint(db), "controls": kb._control_versions_fingerprint(db)})


def process(db, job):
    from . import knowledge as kb
    kb._lock_lease_job(db, job, job.payload.get("_lease_token"))
    revision = get_entity(db, job.payload['revision_id'], 'document_revision', lock=True)
    if revision.status not in {"REVIEW_REQUIRED", "FAILED"} or not revision.data.get('simple_workflow'):
        return True
    if job.payload['extraction_id'] != revision.data.get('latest_extraction_revision_id') or job.payload['content_fingerprint'] != revision.data.get('content_fingerprint'):
        return True
    user = db.scalar(select(User).where(User.id == job.payload['actor_id']).execution_options(populate_existing=True))
    if not user or not user.enabled or user.role not in kb.EDIT_ROLES:
        kb._update(revision, simple_error="Ответственный HR больше не имеет доступа")
        return True
    actor = user_actor(user)
    try:
        revision, doc = kb._revision(db, actor, revision.id, True)
        extraction = kb._extraction(db, revision)
        blockers = quality_blockers(extraction)
        if blockers:
            kb._update(revision, simple_error=None)
            return True
        pin = _preparation_pin(db, revision, doc)
        if revision.data.get('simple_preparation_pin') != pin:
            kb._update(revision, simple_preparation_pin=pin, candidate_snapshot_id=None, qa_artifact_id=None,
                latest_candidate_operation_id=None, latest_qa_operation_id=None, simple_approval=None, simple_error=None)
        candidate = db.get(Entity, revision.data.get('candidate_snapshot_id')) if revision.data.get('candidate_snapshot_id') else None
        candidate_op = _operation(db, revision, 'latest_candidate_operation_id')
        if not candidate:
            if candidate_op and candidate_op.status in PENDING:
                return True
            if candidate_op and candidate_op.status in {'FAILED','CANCELLED'}:
                kb._update(revision, simple_error=str(candidate_op.data.get('error') or 'Не удалось подготовить поиск'))
                return True
            kb.build_candidate(revision.id, {}, _request(revision), actor, db)
            return True
        qa = db.get(Entity, revision.data.get('qa_artifact_id')) if revision.data.get('qa_artifact_id') else None
        qa_op = _operation(db, revision, 'latest_qa_operation_id')
        if not qa:
            if qa_op and qa_op.status in PENDING:
                return True
            if qa_op and qa_op.status in {'FAILED','CANCELLED'}:
                kb._update(revision, simple_error=str(qa_op.data.get('error') or 'Не пройдена проверка документа'))
                return True
            kb._queue_qa(revision.id, {}, _request(revision), actor, db)
        return True
    except HTTPException as error:
        kb._update(revision, simple_error=str(error.detail))
        return True


def approval_current(revision, extraction, qa, candidate):
    approval = revision.data.get('simple_approval') or {}
    return bool(revision.data.get('simple_workflow') and approval.get('extraction_id') == extraction.id
        and approval.get('content_fingerprint') == revision.data.get('content_fingerprint')
        and approval.get('qa_id') == qa.id and approval.get('qa_fingerprint') == digest(qa.data)
        and approval.get('candidate_id') == candidate.id)


def blocker_message(code):
    if code == "NO_SEARCHABLE_TEXT_ADD_IMAGE_DESCRIPTION":
        return "На странице нет текста для поиска. Добавьте описание изображения."
    if code == "NO_PHYSICAL_PAGES":
        return "Не удалось распознать страницы документа. Обновите файл."
    if code == "CONTROL_QUESTION_REGRESSION_FAILED":
        return "Контрольные ответы не прошли проверку. Исправьте документ или контрольные вопросы."
    match = re.match(r"PAGE_(\d+)", code)
    if match:
        return "Проверьте распознавание на странице " + match.group(1)
    if code.startswith("TABLE_"):
        return "Проверьте таблицу по оригиналу и исправьте распознанные ячейки."
    if code.startswith("FIGURE_"):
        return "Добавьте описание изображения или оставьте его только для просмотра."
    if code.startswith("BLOCK_"):
        return "Проверьте распознанный фрагмент по оригиналу."
    if not re.search(r"[А-Яа-яЁё]", code):
        return "Подготовка документа не завершена. Повторите подготовку или обновите файл."
    return code


def readiness(db, revision, doc):
    from . import knowledge as kb
    codes=[]
    processing=revision.status in {'UPLOADED','PROCESSING','PARSING','INDEXING','QUEUED'}
    requires_prepare = not revision.data.get('simple_workflow') and revision.status in {'REVIEW_REQUIRED','FAILED','APPROVED'}
    if requires_prepare: codes.append('Подготовьте документ к публикации')
    if doc.status in {'PURGE_PENDING','PURGED','TOMBSTONED'} or doc.data.get('source_revoked'):
        codes.append('Исходный документ больше недоступен')
    if revision.status in {'PUBLISHED','ARCHIVED','REJECTED','PURGE_PENDING','PURGED','CANCELLED'}:
        codes.append('Эта версия недоступна для публикации')
    extraction = kb._extraction(db, revision) if revision.data.get('latest_extraction_revision_id') else None
    if not extraction:
        codes.append('Дождитесь распознавания документа')
    else:
        codes.extend(quality_blockers(extraction))
    candidate = db.get(Entity,revision.data.get('candidate_snapshot_id')) if revision.data.get('candidate_snapshot_id') else None
    qa = db.get(Entity,revision.data.get('qa_artifact_id')) if revision.data.get('qa_artifact_id') else None
    operation_id=None
    preparing = db.scalar(select(Job).where(Job.kind==KIND, Job.status.in_(PENDING),
        Job.payload['revision_id'].as_string()==revision.id,
        Job.payload['extraction_id'].as_string()==revision.data.get('latest_extraction_revision_id')))
    processing = processing or preparing is not None
    for field in ['latest_ingest_operation_id','latest_candidate_operation_id','latest_qa_operation_id']:
        op=_operation(db,revision,field)
        if op and op.status in PENDING:
            processing=True; operation_id=op.id
    if not candidate or not qa:
        codes.append('Документ готовится к публикации' if processing else 'Проверка документа ещё не завершена')
    else:
        try:
            kb._assert_qa_current(db,qa)
            head=db.scalar(select(Entity).where(Entity.kind=='knowledge_head').execution_options(populate_existing=True))
            if not head or candidate.data['base_snapshot_id'] != head.data['snapshot_id']:
                codes.append('База знаний изменилась. Повторите подготовку документа'); requires_prepare=True
            pin = candidate.data.get('members',{}).get(revision.id,{})
            if not extraction or pin.get('extraction_revision_id') != extraction.id or pin.get('content_fingerprint') != revision.data.get('content_fingerprint'):
                codes.append('Содержимое изменилось. Повторите подготовку документа'); requires_prepare=True
            if pin.get('document_policy_fingerprint') != digest({'scope':doc.data.get('scope',{}),'category':doc.data.get('document_category')}):
                codes.append('Аудитория изменилась. Повторите подготовку документа'); requires_prepare=True
            if qa.status != 'PASSED' or qa.data.get('candidate_snapshot_id') != candidate.id or qa.data.get('content_fingerprint') != revision.data.get('content_fingerprint'):
                codes.extend(qa.data.get('blockers') or ['Проверка документа не пройдена'])
            if qa.data.get('manual_review_status') == 'FAIL': codes.append('Ручная проверка контрольных ответов не пройдена')
            impact=db.get(Entity,revision.data.get('impact_review_id')) if revision.data.get('impact_review_id') else None
            if not impact or impact.status != 'PASSED': codes.append('Изменение затрагивает связанные инструкции. Требуется проверка ответственным HR')
        except HTTPException as error:
            codes.append(str(error.detail)); requires_prepare=True
    if revision.data.get('simple_error'):
        codes.append(revision.data['simple_error']); requires_prepare=True
    if revision.data.get('simple_workflow') and not processing and (not candidate or not qa): requires_prepare=True
    return {'ready_to_publish':not codes and not processing,'processing':processing,'blockers':[{'message':blocker_message(message),'code':message} for message in dict.fromkeys(codes)],
        'status':revision.status,'operation_id':operation_id,'requires_prepare':requires_prepare}


@router.get('/knowledge/revisions/{revision_id}/simple-status')
def simple_status(revision_id: str, actor=Depends(get_actor), db=Depends(get_db)):
    from . import knowledge as kb
    revision,doc=kb._revision(db,actor,revision_id)
    return readiness(db,revision,doc)


@router.post('/knowledge/revisions/{revision_id}/simple-prepare',status_code=202)
def simple_prepare(revision_id: str, request: Request, response: Response, actor=Depends(get_actor), db=Depends(get_db)):
    from . import knowledge as kb
    actor = _current_actor(db, actor)
    revision,_=kb._revision(db,actor,revision_id,True)
    check_etag(request,revision)
    if revision.status == 'APPROVED':
        revision.status='REVIEW_REQUIRED'
        kb._update(revision,review_fingerprint=None,simple_approval=None)
    kb._mutable_revision(revision)
    extraction=kb._extraction(db,revision)
    kb._update(revision,simple_workflow=True,simple_actor_id=actor.id,simple_preparation_pin=None,simple_error=None)
    normalized=normalize(extraction.data)
    if normalized != extraction.data:
        kb._new_extraction(db,revision,normalized,actor)
    schedule(db,revision,actor,'explicit-'+str(revision.version))
    audit(db,actor,'knowledge.simple.preparation_requested',revision.id)
    commit(db);response.headers['ETag']=etag(revision)
    return {'revision':serialize(revision)}


def _current_actor(db, actor):
    from . import knowledge as kb
    kb._knowledge_write_lock(db)
    user=db.scalar(select(User).where(User.id==actor.id).with_for_update().execution_options(populate_existing=True))
    if not user or not user.enabled or user.role not in kb.EDIT_ROLES:
        raise HTTPException(403,'Полномочия ответственного HR изменились')
    return user_actor(user)


@router.post('/knowledge/revisions/{revision_id}/simple-publish')
def simple_publish(revision_id: str, request: Request, response: Response, actor=Depends(get_actor), db=Depends(get_db)):
    from . import knowledge as kb
    require_role(actor,'HR_ADMIN')
    actor=_current_actor(db,actor)
    require_role(actor,'HR_ADMIN')
    revision,doc=kb._revision(db,actor,revision_id,True)
    check_etag(request,revision)
    if not revision.data.get('simple_workflow'):
        raise HTTPException(409,'Подготовьте документ перед публикацией')
    status=readiness(db,revision,doc)
    if not status['ready_to_publish']:
        raise HTTPException(409,{'code':'document_not_ready','message':'; '.join(b['message'] for b in status['blockers'])})
    source=Path(revision.data.get('source_path',''))
    try:
        source_current = source.is_file() and hashlib.sha256(source.read_bytes()).hexdigest()==revision.data.get('sha256')
    except OSError:
        source_current = False
    if not source_current:
        raise HTTPException(409,'Исходный файл изменился или недоступен. Загрузите документ повторно')
    extraction=kb._extraction(db,revision)
    qa=get_entity(db,revision.data['qa_artifact_id'],'knowledge_qa')
    candidate=get_entity(db,revision.data['candidate_snapshot_id'],'knowledge_candidate')
    with db.begin_nested():
        approval={'extraction_id':extraction.id,'content_fingerprint':revision.data['content_fingerprint'],'qa_id':qa.id,
            'qa_fingerprint':digest(qa.data),'candidate_id':candidate.id,'actor_id':actor.id,'approved_at':now().isoformat(),'method':'EXPLICIT_WHOLE_REVISION_APPROVAL'}
        revision.status='APPROVED'
        kb._update(revision,simple_approval=approval,approved_by=actor.id,approved_at=approval['approved_at'],
            review_fingerprint=digest(approval),source_completeness='COMPLETE_WITHIN_DECLARED_SCOPE')
        snapshot=kb._publish(db,revision,doc,actor)
        audit(db,actor,'knowledge.simple.whole_revision_approved_and_published',revision.id,approval)
    commit(db);response.headers['ETag']=etag(revision)
    return {'revision':serialize(revision),'kb_snapshot_id':snapshot.id,'status':'PUBLISHED'}
