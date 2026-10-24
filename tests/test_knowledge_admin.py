"""Real CORE regression, immutable expectations and safe knowledge retirement."""
import copy
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from korsik import knowledge, knowledge_admin, retrieval
from korsik.auth import Actor, get_actor
from korsik.common import etag, save_entity
from korsik.db import get_db, make_engine, now
from korsik.models import Base, Entity, IdempotencyRecord, Job, Message, Outbox, TransportDelivery, User
from test_knowledge import action, create_text, page_review, publish


@pytest.fixture
def admin(tmp_path, monkeypatch):
    engine = make_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    db = sessionmaker(engine, expire_on_commit=False)()
    actor = Actor('admin', 'HR', 'HR_ADMIN', {'all': True})
    db.add(User(id=actor.id,email='admin@kb.test',password_hash='not-used',name=actor.name,role=actor.role,scope=actor.scope,enabled=True))
    app = FastAPI()
    app.include_router(knowledge.router)
    app.include_router(knowledge_admin.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_actor] = lambda: actor
    monkeypatch.setattr(knowledge, 'DATA', tmp_path)
    monkeypatch.setattr(retrieval, 'MODE', 'CORE')
    # CORE must answer locally; any accidentally introduced network call fails.
    monkeypatch.setattr(retrieval, '_request', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('network_forbidden')))
    db.add(Entity(id='a', kind='site', data={'legal_entity_id': 'legal-a'}, status='ACTIVE'))
    db.add(Entity(id='b', kind='site', data={'legal_entity_id': 'legal-b'}, status='ACTIVE'))
    db.commit()
    yield TestClient(app), db, actor
    db.close()
    engine.dispose()


def question(revision_id=None):
    return {'question': 'Обед в столовой', 'preview_profile': {
        'legal_entity_id': 'legal-a', 'site_id': 'a', 'employment_type': 'OFFICE',
        # The source is uploaded immediately before this question. A historical
        # fixed timestamp eventually precedes its effective_from and tests an
        # empty corpus instead of the intended current-scope CORE regression.
        'employment_stage': 'ACTIVE', 'as_of': now().isoformat()},
        'expectation': {'mode': 'EXTRACT', 'evidence_revision_ids': [revision_id] if revision_id else [],
            'critical': True, 'must_include_literals': ['12:00'], 'must_exclude_literals': ['14:00'],
            'review_rubric': 'Проверить время обеда и точное соответствие источнику.'},
        'reason': 'Контроль точного времени обеда'}


def candidate(client, db):
    doc, revision = create_text(client, db, 'Обед в столовой с 12:00 до 13:00.')
    page_review(client, revision['id'])
    built = action(client, revision['id'], 'candidate-build')
    assert built.status_code == 202, built.text
    row = db.get(Entity, built.json()['candidate_build_result']['candidate_snapshot_id'])
    return doc, revision, row


def test_control_versions_are_immutable_scoped_and_compare_and_swap(admin):
    client, db, actor = admin
    _, revision, _ = candidate(client, db)
    payload = question(revision['id'])
    created = client.post('/knowledge/control-questions', json=payload)
    assert created.status_code == 201, created.text
    first = copy.deepcopy(created.json())
    original = copy.deepcopy(db.get(Entity, first['id']).data)
    payload['question'] = 'Обед в столовой — часы работы'
    path = f"/knowledge/control-questions/{first['control_question_id']}/versions"
    assert client.post(path, json=payload).status_code == 428
    second = client.post(path, json=payload, headers={'If-Match': created.headers['etag']})
    assert second.status_code == 200, second.text
    assert second.json()['version_no'] == 2
    assert db.get(Entity, first['id']).data == original
    assert client.post(path, json=payload, headers={'If-Match': created.headers['etag']}).status_code == 412
    actor.scope, actor.role = {'site_ids': ['b']}, 'HR_SPECIALIST'
    assert client.get('/knowledge/control-questions').json()['items'] == []
    assert client.get(f"/knowledge/control-questions/{first['control_question_id']}").status_code == 404
    assert client.post('/knowledge/control-questions', json=payload).status_code == 403
    actor.role, actor.scope = 'PLATFORM_ADMIN', {'all': True}
    assert client.get('/knowledge/control-questions').status_code == 403


def test_control_suite_runs_real_local_retrieval_without_leaving_employee_or_outbox(admin):
    client, db, actor = admin
    _, revision, built = candidate(client, db)
    created = client.post('/knowledge/control-questions', json=question(revision['id']))
    assert created.status_code == 201, created.text
    employees_before = list(db.scalars(select(Entity.id).where(Entity.kind == 'employee')))
    result = knowledge_admin.evaluate_control_suite(db, built, actor)
    assert result['status'] == 'PASSED', json.dumps(result['results'], ensure_ascii=False)
    assert result['results'][0]['actual_mode'] == 'EXTRACT'
    assert result['results'][0]['citation_revision_ids'] == [revision['id']]
    assert result['manual_review_status'] == 'PENDING'
    assert result['semantic_quality'] == 'REQUIRES_HUMAN_RUBRIC_REVIEW'
    assert list(db.scalars(select(Entity.id).where(Entity.kind == 'employee'))) == employees_before
    assert not list(db.scalars(select(Outbox)))
    assert not list(db.scalars(select(Entity).where(Entity.kind == 'conversation')))
    payload = question(revision['id'])
    payload['expectation']['must_include_literals'] = ['17:59']
    old = created.json()
    assert client.post(f"/knowledge/control-questions/{old['control_question_id']}/versions", json=payload,
                       headers={'If-Match': created.headers['etag']}).status_code == 200
    failed = knowledge_admin.evaluate_control_suite(db, built, actor)
    assert failed['status'] == 'FAILED'
    assert failed['critical_failures'] == 1
    assert failed['results'][0]['checks']['required_literals'] is False


def test_manual_review_cannot_override_machine_failure_and_requires_all_checks(admin):
    client, db, actor = admin
    _, revision, built = candidate(client, db)
    action(client, revision['id'], 'qa-runs')
    rev = db.get(Entity, revision['id'])
    qa = db.get(Entity, rev.data['qa_artifact_id'])
    original_checks = copy.deepcopy(qa.data['checks'])
    path = f'/knowledge/qa-artifacts/{qa.id}/review'
    incomplete = {'checks': [{'check_id': 'OCR_NUMBERS', 'status': 'PASS'}], 'reason': 'Проверено с оригиналом'}
    assert client.post(path, json=incomplete, headers={'If-Match': etag(qa)}).status_code == 422
    payload = {'checks': [{'check_id': k, 'status': 'PASS'} for k in ['OCR_NUMBERS', 'CITATIONS', 'ANSWER_MEANING']], 'reason': 'Сверено по оригиналу'}
    reviewed = client.post(path, json=payload, headers={'If-Match': etag(qa)})
    assert reviewed.status_code == 200, reviewed.text
    assert qa.data['checks'] == original_checks
    assert qa.data['manual_review_status'] == 'PASS'
    qa.status = 'FAILED'
    db.commit()
    assert client.post(path, json=payload, headers={'If-Match': etag(qa)}).status_code == 409


def test_new_document_addition_runs_existing_scoped_control_questions(admin):
    client, db, actor = admin
    _, old = create_text(client, db, 'Обед в столовой с 12:00 до 13:00.')
    publish(client, old['id'])
    created = client.post('/knowledge/control-questions', json=question(old['id']))
    assert created.status_code == 201
    _, fresh = create_text(client, db, 'Правила парковки автомобилей на территории.')
    page_review(client, fresh['id'])
    built = action(client, fresh['id'], 'candidate-build').json()
    candidate = db.get(Entity, built['candidate_build_result']['candidate_snapshot_id'])
    result = knowledge_admin.evaluate_control_suite(db, candidate, actor)
    assert len(result['results']) == 1
    assert result['results'][0]['control_question_version_id'] == created.json()['id']
    assert result['results'][0]['passed'], json.dumps(result['results'], ensure_ascii=False)


def test_snapshot_history_is_filtered_by_current_grant_and_revocation(admin):
    client, db, actor = admin
    first, r1 = create_text(client, db, site='a')
    publish(client, r1['id'])
    second, r2 = create_text(client, db, site='b')
    publish(client, r2['id'])
    current = knowledge._head(db).data['snapshot_id']
    snapshot = db.get(Entity, current)
    knowledge._update(snapshot, projections={r1['id']: {'collection': 'allowed'}, r2['id']: {'collection': 'hidden'}})
    db.commit()
    actor.scope, actor.role = {'site_ids': ['a']}, 'HR_SPECIALIST'
    view = client.get(f'/knowledge/snapshots/{current}').json()
    assert view['members'] == {first['id']: r1['id']}
    assert view['projections'] == {r1['id']: {'collection': 'allowed'}}
    assert second['id'] not in json.dumps(view)
    row = db.get(Entity, first['id'])
    row.status = 'PURGE_PENDING'
    db.commit()
    assert client.get(f'/knowledge/snapshots/{current}').json()['members'] == {}


def test_rollback_rebuilds_a_new_revision_and_keeps_old_publications_immutable(admin, monkeypatch):
    client, db, actor = admin
    def fenced_stage(job):
        # An inner commit would release the executor fence before the stage is
        # durable. Run real queue/approval rules while rejecting any commit.
        with monkeypatch.context() as patch:
            patch.setattr(db, "commit", lambda: (_ for _ in ()).throw(AssertionError("rollback_inner_commit")))
            return knowledge_admin.process_knowledge_admin_job(db, job)
    doc, first = create_text(client, db, 'Обед с 12:00 до 13:00.')
    publish(client, first['id'])
    first_data = copy.deepcopy(db.get(Entity, first['id']).data)
    latest_doc = client.get(f"/knowledge/documents/{doc['id']}").json()
    second_response = client.post(f"/knowledge/documents/{doc['id']}/revisions", files={'file': ('second.txt', 'Обед с 13:00 до 14:00.'.encode(), 'text/plain')}, data={'reason': 'Изменён график'}, headers={'If-Match': latest_doc['etag']})
    assert second_response.status_code == 202
    second = second_response.json()['revision']
    job = db.scalar(select(Job).where(Job.kind == 'KNOWLEDGE_INGEST').order_by(Job.created_at.desc()))
    knowledge.process_ingest_job(db, job)
    publish(client, second['id'])
    body = {'target_revision_id': first['id'], 'expected_snapshot_id': knowledge._head(db).data['snapshot_id'], 'reason': 'Возврат утверждённого графика'}
    current_doc = client.get(f"/knowledge/documents/{doc['id']}")
    result = client.post(f"/knowledge/documents/{doc['id']}/rollback", json=body, headers={'If-Match': current_doc.headers['etag']})
    assert result.status_code == 202, result.text
    new_revision_id = result.json()['revision_id']
    assert result.json()['status'] == 'PENDING'
    assert db.get(Entity, doc['id']).data['active_revision_id'] == second['id']
    rollback_job = db.scalar(select(Job).where(Job.kind == 'KNOWLEDGE_ROLLBACK').order_by(Job.created_at.desc()))
    assert fenced_stage(rollback_job)
    db.commit()
    assert db.get(Entity, result.json()['id']).status == 'PENDING'
    candidate_job = db.scalar(select(Job).where(Job.kind == 'KNOWLEDGE_CANDIDATE_BUILD').order_by(Job.created_at.desc()))
    assert knowledge.process_ingest_job(db, candidate_job)
    db.commit()
    waiting_job = db.scalar(select(Job).where(Job.kind == 'KNOWLEDGE_ROLLBACK').order_by(Job.created_at.desc()))
    assert fenced_stage(waiting_job)
    db.commit()
    operation = db.get(Entity, result.json()['id'])
    assert operation.data['stage'] == 'WAITING_QA'
    assert operation.data['qa_operation_id']
    qa_job = db.scalar(select(Job).where(Job.kind == 'KNOWLEDGE_QA').order_by(Job.created_at.desc()))
    assert knowledge.process_ingest_job(db, qa_job)
    db.commit()
    waiting_job = db.scalar(select(Job).where(Job.kind == 'KNOWLEDGE_ROLLBACK').order_by(Job.created_at.desc()))
    assert fenced_stage(waiting_job)
    db.commit()
    assert db.get(Entity, result.json()['id']).status == 'SUCCEEDED'
    assert new_revision_id not in {first['id'], second['id']}
    assert db.get(Entity, first['id']).data == first_data
    assert db.get(Entity, doc['id']).data['active_revision_id'] == new_revision_id
    assert db.get(Entity, new_revision_id).data['rollback_of_revision_id'] == first['id']
    assert db.get(Entity, new_revision_id).data['metadata_snapshot'] == knowledge._document_metadata_snapshot(db.get(Entity, doc['id']))
    assert db.get(Entity, new_revision_id).data['scope_fingerprint'] == knowledge.digest(db.get(Entity, doc['id']).data.get('scope', {}))
    assert db.get(Entity, new_revision_id).status == 'PUBLISHED'


def test_purge_requires_explicit_policy_cas_then_erases_sources_and_cached_answers(admin):
    client, db, actor = admin
    doc, revision = create_text(client, db)
    publish(client, revision['id'])
    policy = save_entity(db, 'retention_policy', {'configured': True, 'data_class': 'KNOWLEDGE_DOCUMENT', 'backup_retention_days': 7})
    answer = save_entity(db, 'answer', {'text': 'Частный ответ', 'citations': [{'revision_id': revision['id']}], 'conversation_id': 'conv'}, status='COMPLETED')
    db.flush()
    message = Message(conversation_id='conv', seq=1, author_type='BOT', text='Частный ответ', data={'answer_id': answer.id})
    db.add(message)
    db.flush()
    outbox = Outbox(recipient_kind='EMPLOYEE', recipient_id='employee', unique_key='source-answer', text='Частный ответ', data={'message_id': message.id})
    db.add(outbox)
    db.flush()
    delivery = TransportDelivery(outbox_id=outbox.id)
    db.add(delivery)
    extract_file = knowledge.DATA / 'citation-extracts' / 'generated-citation'
    extract_file.parent.mkdir(parents=True)
    extract_file.write_text('Частная выдержка из источника')
    asset = save_entity(db, 'asset', {'knowledge_revision_id': revision['id'], 'purpose': 'CITATION_PAGE', 'storage_path': str(extract_file)}, status='READY', parent_id=revision['id'])
    db.flush()
    attachment_delivery = Outbox(recipient_kind='EMPLOYEE', recipient_id='employee', unique_key='source-attachment', text='Источник', attachments=[asset.id], data={})
    db.add(attachment_delivery)
    cached = IdempotencyRecord(key='idempotency', fingerprint='abc', status_code=200, response={'revision_id': revision['id'], 'text': 'Частный ответ'})
    db.add(cached)
    db.commit()
    path = f"/knowledge/documents/{doc['id']}"
    latest = client.get(path)
    payload = {'expected_snapshot_id': 'stale', 'retention_policy_id': policy.id, 'reason': 'Закончился срок хранения'}
    assert client.request('DELETE', path, json=payload, headers={'If-Match': latest.headers['etag']}).status_code == 409
    payload['expected_snapshot_id'] = knowledge._head(db).data['snapshot_id']
    result = client.request('DELETE', path, json=payload, headers={'If-Match': latest.headers['etag']})
    assert result.status_code == 202, result.text
    assert db.get(Entity, doc['id']).status == 'PURGE_PENDING'
    assert client.get(f"/knowledge/revisions/{revision['id']}/source").status_code == 404
    assert doc['id'] not in db.get(Entity, knowledge._head(db).data['snapshot_id']).data['members']
    folder = knowledge.DATA / 'knowledge' / revision['id']
    assert folder.exists()
    job = db.scalar(select(Job).where(Job.kind == 'KNOWLEDGE_PURGE'))
    assert knowledge_admin.process_knowledge_admin_job(db, job)
    db.commit()
    assert not folder.exists()
    assert not extract_file.exists()
    assert asset.status == 'PURGED' and attachment_delivery.status == 'CANCELLED'
    assert db.get(Entity, doc['id']).status == 'PURGED'
    assert db.get(Entity, revision['id']).data['source_purged']
    assert answer.data['citations'] == [] and answer.status == 'PURGED'
    assert message.text == 'Источник удалён по политике хранения.'
    assert outbox.status == delivery.status == 'CANCELLED'
    assert outbox.text == '' and cached.status_code == 410
    assert knowledge_admin.process_knowledge_admin_job(db, job)
    assert len(list(db.scalars(select(Entity).where(Entity.kind == 'knowledge_tombstone')))) == 1
    assert db.get(Entity, result.json()['id']).data['external_messenger_copies_recalled'] is False


def test_purge_never_erases_held_documents_or_unconfigured_retention(admin):
    client, db, actor = admin
    doc, revision = create_text(client, db)
    publish(client, revision['id'])
    policy = save_entity(db, 'retention_policy', {'configured': False, 'data_class': 'KNOWLEDGE_DOCUMENT', 'backup_retention_days': 0})
    db.commit()
    payload = {'expected_snapshot_id': knowledge._head(db).data['snapshot_id'], 'retention_policy_id': policy.id, 'reason': 'Истёк срок хранения'}
    headers = {'If-Match': etag(db.get(Entity, doc['id']))}
    assert client.request('DELETE', f"/knowledge/documents/{doc['id']}", json=payload, headers=headers).status_code == 409
    knowledge._update(policy, configured=True, hold=True)
    db.commit()
    assert client.request('DELETE', f"/knowledge/documents/{doc['id']}", json=payload, headers=headers).status_code == 409
    assert (knowledge.DATA / 'knowledge' / revision['id']).exists()


def test_maintenance_operations_resume_with_owner_and_current_scope_only(admin):
    client, db, actor = admin
    own = save_entity(db, 'operation', {'type': 'KNOWLEDGE_PURGE', 'operation_scope': {'site_ids': ['a']}, 'document_id': 'removed-document'}, status='SUCCEEDED', owner_id=actor.id)
    save_entity(db, 'operation', {'type': 'KNOWLEDGE_PURGE', 'operation_scope': {'site_ids': ['a']}}, status='PENDING', owner_id='another-admin')
    db.commit()
    response = client.get('/knowledge/maintenance-operations')
    assert response.status_code == 200, response.text
    assert [row['id'] for row in response.json()['items']] == [own.id]
    actor.scope = {'site_ids': ['b']}
    assert client.get('/knowledge/maintenance-operations').json()['items'] == []
    actor.role = 'PLATFORM_ADMIN'
    assert client.get('/knowledge/maintenance-operations').status_code == 403


@pytest.mark.parametrize('failure', ['expired', 'cancelled', 'superseded', 'policy_hold'])
def test_purge_executor_rechecks_lease_and_policy_before_irreversible_erasure(admin, monkeypatch, failure):
    from datetime import timedelta
    from korsik.db import now
    client, db, actor = admin
    doc, revision = create_text(client, db)
    publish(client, revision['id'])
    policy = save_entity(db, 'retention_policy', {'configured': True, 'data_class': 'KNOWLEDGE_DOCUMENT', 'backup_retention_days': 7, 'hold': False}, status='ACTIVE')
    db.commit()
    current = client.get(f"/knowledge/documents/{doc['id']}")
    result = client.request('DELETE', f"/knowledge/documents/{doc['id']}", json={'expected_snapshot_id': knowledge._head(db).data['snapshot_id'], 'retention_policy_id': policy.id, 'reason': 'Тест протокола удаления'}, headers={'If-Match': current.headers['etag']})
    assert result.status_code == 202, result.text
    folder = knowledge.DATA / 'knowledge' / revision['id']
    job = db.scalar(select(Job).where(Job.kind == 'KNOWLEDGE_PURGE'))
    job.payload = {**job.payload, '_lease_token': 'current-executor'}
    job.status = 'CANCELLED' if failure == 'cancelled' else 'PROCESSING'
    job.lease_until = now() + timedelta(seconds=-1 if failure == 'expired' else 600)
    if failure == 'policy_hold':
        policy.data = {**policy.data, 'hold': True}
    db.commit()
    if failure == 'superseded':
        original_refresh = db.refresh
        def supersede(entity, *args, **kwargs):
            if entity is job:
                job.payload = {**job.payload, '_lease_token': 'new-executor'}
                db.flush()
            return original_refresh(entity, *args, **kwargs)
        monkeypatch.setattr(db, 'refresh', supersede)
    with pytest.raises(RuntimeError if failure == 'policy_hold' else knowledge.IngestLeaseLost):
        knowledge_admin.process_knowledge_admin_job(db, job)
    assert folder.is_dir()
    assert db.get(Entity, doc['id']).status == 'PURGE_PENDING'
    assert db.get(Entity, result.json()['id']).status == 'PENDING'


@pytest.mark.parametrize('failure', ['expired', 'cancelled', 'superseded'])
def test_rollback_executor_requires_current_lease_before_stage_transition(admin, monkeypatch, failure):
    from datetime import timedelta
    from korsik.db import now
    client, db, actor = admin
    operation = save_entity(db, 'operation', {'type': 'KNOWLEDGE_ROLLBACK', 'stage': 'WAITING_CANDIDATE'}, status='PENDING', owner_id=actor.id)
    db.flush()
    job = Job(kind='KNOWLEDGE_ROLLBACK', payload={'operation_id': operation.id, '_lease_token': 'current-executor'}, unique_key='rollback-fence', status='CANCELLED' if failure == 'cancelled' else 'PROCESSING', lease_until=now() + timedelta(seconds=-1 if failure == 'expired' else 600))
    db.add(job)
    db.commit()
    if failure == 'superseded':
        original_refresh = db.refresh
        def supersede(entity, *args, **kwargs):
            if entity is job:
                job.payload = {**job.payload, '_lease_token': 'new-executor'}
                db.flush()
            return original_refresh(entity, *args, **kwargs)
        monkeypatch.setattr(db, 'refresh', supersede)
    with pytest.raises(knowledge.IngestLeaseLost):
        knowledge_admin.process_knowledge_admin_job(db, job)
    assert operation.status == 'PENDING'
    assert operation.data['stage'] == 'WAITING_CANDIDATE'

