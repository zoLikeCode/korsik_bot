"""Candidate-image integration checks; all records are synthetic and disposable.

The operator runner creates a schema-only PostgreSQL clone and mounts no MAX
credential, production blob storage, or model directory. No transport process
runs against this database. Local SQLite execution is permitted for preparation.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
import uuid

from fastapi import HTTPException
from sqlalchemy import select, text
from starlette.requests import Request

from korsik import domain as d, simple_scenarios as ss, survey_campaigns as sc
from korsik import basic_formulations as bf, transport_api as tx
from korsik import max_events as me
from korsik.auth import Actor
from korsik.common import etag
from korsik.db import SessionLocal, engine
from korsik.models import Entity, Job, Outbox, User
from korsik.seed import default_graph as legacy_graph, seed_defaults
from korsik.settings import get_settings

REPORT = Path(os.environ.get('KORSIK_RELEASE21_PG_REPORT', '/tmp/release21-integration.json'))
CHECK_DATABASE = os.environ.get('KORSIK_RELEASE21_CHECK_DATABASE')
DATABASE_NAME = engine.url.database
assert get_settings().max_mode == 'disabled', 'MAX_must_be_disabled_for_checks'
assert not Path(get_settings().max_token_file).exists(), 'MAX_token_must_not_be_mounted'
assert not get_settings().generation_enabled, 'model_generation_must_be_disabled'
if engine.dialect.name == 'postgresql':
    assert CHECK_DATABASE and DATABASE_NAME == CHECK_DATABASE and DATABASE_NAME.startswith('korsik_r21_check_'), 'isolated_database_required'
    with engine.connect() as connection:
        assert connection.execute(text('SELECT current_database()')).scalar_one() == CHECK_DATABASE
else:
    assert engine.dialect.name == 'sqlite' and CHECK_DATABASE == 'LOCAL_SQLITE_PREPARATION', 'explicit_local_preparation_required'


def unpack(response, db):
    return db.get(Entity, json.loads(response.body)['id'])


def request(entity, supplied=None):
    return Request({'type': 'http', 'headers': [(b'if-match', (supplied or etag(entity)).encode())]})


def transition(key):
    return {'target_step_key': key, 'condition': {'kind': 'ALWAYS'}}


def message(key, text, targets=(), trigger=None, **extra):
    return {'step_key': key, 'kind': 'MESSAGE',
            'trigger': trigger or {'type': 'AFTER_PREVIOUS'},
            'content': {'text_ty': text, 'text_vy': text, **extra},
            'transitions': [transition(target) for target in targets]}


def answer(key, label, target):
    return {'step_key': key, 'kind': 'ANSWER',
            'answer': {'label_ty': label, 'label_vy': label},
            'transitions': [transition(target)]}


def graph(steps=None, start='welcome', condition='ON_BOT_START', **policy):
    return {'schema_version': 'korsik-scenario-v2', 'algorithm_version': 'calendar-v1',
            'editor_mode': 'SIMPLE', 'start_step_key': start,
            'start_policy': {'condition': condition, 'timezone': 'Europe/Moscow', **policy},
            'resume_policy': {'reminder_hours': 24},
            'steps': steps or [message('welcome', 'Синтетическое приветствие', ['end']),
                               {'step_key': 'end', 'kind': 'END', 'transitions': []}],
            'editor_layout': {}}


def choices_graph():
    return graph([
        message('welcome', 'Как продолжим?', ['yes', 'no'], custom_prompts=[
            {'id': 'lunch', 'label': 'Где обедать?', 'query_text': 'Где можно пообедать?'}]),
        answer('yes', 'Продолжить', 'next'), answer('no', 'Позже', 'end'),
        message('next', 'Выбран только этот путь', ['end']),
        {'step_key': 'end', 'kind': 'END', 'transitions': []},
    ])


class SimpleScenarioIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with SessionLocal() as db:
            seed_defaults(db)
            db.commit()

    def setUp(self):
        self.db = SessionLocal()
        self.clock = datetime(2026, 10, 24, 9, tzinfo=timezone.utc)
        self.clock_patch = patch.object(d, 'now', lambda: self.clock)
        self.clock_patch.start()
        self.survey_clock_patch = patch.object(sc, 'now', lambda: self.clock)
        self.survey_clock_patch.start()
        self.suffix = uuid.uuid4().hex[:10]
        user = User(id=str(uuid.uuid4()), email='r21-' + self.suffix + '@test.local',
                    name='Синтетический HR release21', password_hash='not-a-login-hash',
                    role='HR_ADMIN', scope={'all': True}, enabled=True)
        self.db.add(user)
        self.db.commit()
        self.hr = Actor(id=user.id, name=user.name, role=user.role, scope=user.scope)
        self.people = []

    def tearDown(self):
        self.db.rollback()
        for employee_id in self.people:
            employee = self.db.get(Entity, employee_id)
            if employee:
                d.update(employee, employment_stage='ENDED')
        self.db.commit()
        self.db.close()
        self.survey_clock_patch.stop()
        self.clock_patch.stop()

    def published(self, definition=None):
        scenario = unpack(d.create_scenario({'name': 'release21-' + self.suffix}, self.db, self.hr), self.db)
        draft = unpack(d.create_scenario_version(scenario.id, {'graph': definition or graph()}, self.db, self.hr), self.db)
        validation = d.validate_scenario(draft.id, self.db, self.hr)
        self.assertTrue(validation['valid'], validation)
        d.review_scenario(draft.id, {}, self.db, self.hr)
        d.publish_scenario(draft.id, {'reason': 'Синтетическая изолированная проверка'}, self.db, self.hr)
        return scenario, draft

    def employee(self, scenario, verified=True, start_date='2026-10-24'):
        employee = unpack(d.create_employee({'name': 'Синтетический сотрудник ' + self.suffix,
            'site_id': 'GrPP', 'start_date': start_date, 'scenario_id': scenario.id,
            'employment_type': 'OFFICE', 'timezone': 'Europe/Moscow'}, self.db, self.hr), self.db)
        self.people.append(employee.id)
        d.update(employee, employment_stage='ACTIVE', max_binding_status='VERIFIED' if verified else 'UNBOUND')
        self.db.flush()
        journey = next(row for row in d.entities(self.db, 'journey', employee.id) if row.data['scenario_id'] == scenario.id)
        return employee, journey

    def start(self, journey, employee):
        d.update(journey, started_at=self.clock.isoformat())
        ss.plan(self.db, journey, employee)

    def outbox(self, employee, journey=None):
        return [row for row in self.db.scalars(select(Outbox).order_by(Outbox.created_at, Outbox.id))
                if row.recipient_id == employee.id and (journey is None or row.data.get('journey_id') == journey.id)]

    def receipt(self, outbound):
        # Advance the virtual clock so a later message has a later insertion
        # timestamp rather than relying on UUID ordering for equal timestamps.
        self.clock += timedelta(microseconds=1)
        outbound.status = 'ACCEPTED'
        outbound.data = {**outbound.data, 'accepted_at': self.clock.isoformat()}
        d.on_outbox_accepted(self.db, outbound)
        self.db.flush()

    def waiting(self):
        scenario, version = self.published(choices_graph())
        employee, journey = self.employee(scenario)
        self.start(journey, employee)
        d.run_due_scenarios(self.db)
        opening = self.outbox(employee, journey)[0]
        self.receipt(opening)
        return employee, journey, version, opening

    def choice(self, journey, answer_key='yes'):
        return {'action': 'SCENARIO_CHOICE', 'journey_id': journey.id,
                'step_key': 'welcome', 'answer_key': answer_key}

    def test_01_graph_choices_reachable_only_after_selection(self):
        definition = choices_graph()
        self.assertTrue(ss.validate(definition)['valid'])
        self.assertEqual(ss.selected_path(definition, {}), ['welcome'])
        self.assertEqual(ss.selected_path(definition, {'choice:welcome': {'value': 'yes'}}), ['welcome', 'yes', 'next', 'end'])
        self.assertEqual(ss.selected_path(definition, {'choice:welcome': {'value': 'no'}}), ['welcome', 'no', 'end'])
        for mutate in ('cycle', 'mixed', 'previous-time', 'legacy-kind'):
            invalid = deepcopy(definition)
            if mutate == 'cycle':
                invalid['steps'][3]['transitions'] = [transition('welcome')]
            elif mutate == 'mixed':
                invalid['steps'][0]['transitions'].append(transition('end'))
            elif mutate == 'previous-time':
                invalid['steps'][0]['trigger']['local_time'] = '10:00'
            else:
                invalid['steps'][0]['kind'] = 'HR_TASK'
            self.assertFalse(ss.validate(invalid)['valid'], mutate)

    def test_02_simulation_no_outbox_and_published_version_immutable(self):
        scenario, version = self.published(choices_graph())
        before = len(list(self.db.scalars(select(Outbox))))
        result = d.simulate_scenario(version.id, {'employee': {'start_date': '2026-10-24', 'site_id': 'GrPP'}}, self.db, self.hr)
        self.assertTrue(result['simulation_only'])
        self.assertEqual(result['outbound_messages_created'], 0)
        self.assertEqual(len(list(self.db.scalars(select(Outbox)))), before)
        with self.assertRaises(HTTPException) as context:
            d.put_scenario_version(version.id, {'graph': graph()}, request(version), self.db, self.hr)
        self.assertEqual(context.exception.status_code, 409)
        self.assertEqual(scenario.data['published_version_id'], version.id)

    def test_03_receipt_required_for_immediate_next_message(self):
        definition = graph([message('welcome', 'Первое', ['next']),
                            message('next', 'Второе', ['end']), {'step_key': 'end', 'kind': 'END', 'transitions': []}])
        scenario, _ = self.published(definition)
        employee, journey = self.employee(scenario)
        self.start(journey, employee)
        d.run_due_scenarios(self.db)
        d.run_due_scenarios(self.db)
        self.assertEqual([row.text for row in self.outbox(employee, journey)], ['Первое'])
        first = self.outbox(employee, journey)[0]
        self.receipt(first)
        self.receipt(first)
        d.run_due_scenarios(self.db)
        self.assertEqual([row.text for row in self.outbox(employee, journey)], ['Первое', 'Второе'])
        self.receipt(self.outbox(employee, journey)[1])
        d.run_due_scenarios(self.db)
        self.assertEqual(journey.status, 'COMPLETED')

    def test_04_choice_buttons_gate_and_custom_prompt_does_not_advance(self):
        employee, journey, _, opening = self.waiting()
        choice_buttons = [button for button in opening.callback_buttons if button.get('payload', {}).get('action') == 'SCENARIO_CHOICE']
        self.assertEqual([button['text'] for button in choice_buttons], ['Продолжить', 'Позже'])
        self.assertEqual(journey.data['waiting_step_key'], 'welcome')
        result = ss.resolve_formulation(self.db, employee, {'journey_id': journey.id, 'step_key': 'welcome', 'prompt_id': 'lunch'})
        self.assertEqual(result, 'Где можно пообедать?')
        with self.assertRaises(HTTPException) as context:
            ss.choose(self.db, employee, self.choice(journey, 'not-connected'))
        self.assertEqual(context.exception.status_code, 422)
        d.run_due_scenarios(self.db)
        self.assertEqual(len(self.outbox(employee, journey)), 1)
        self.assertFalse(ss.choose(self.db, employee, self.choice(journey))['duplicate'])
        self.assertTrue(ss.choose(self.db, employee, self.choice(journey))['duplicate'])
        with self.assertRaises(HTTPException) as context:
            ss.choose(self.db, employee, self.choice(journey, 'no'))
        self.assertEqual(context.exception.status_code, 409)
        d.run_due_scenarios(self.db)
        self.assertEqual([row.text for row in self.outbox(employee, journey)], ['Как продолжим?', 'Выбран только этот путь'])
        self.assertNotIn('no', journey.data.get('completed_steps', []))

    def test_05_choice_rejects_undelivered_and_cross_employee(self):
        scenario, _ = self.published(choices_graph())
        employee, journey = self.employee(scenario)
        self.start(journey, employee)
        d.run_due_scenarios(self.db)
        with self.assertRaises(HTTPException) as context:
            ss.choose(self.db, employee, self.choice(journey))
        self.assertEqual(context.exception.status_code, 409)
        self.receipt(self.outbox(employee, journey)[0])
        other, _ = self.employee(scenario)
        with self.assertRaises(HTTPException) as context:
            ss.choose(self.db, other, self.choice(journey))
        self.assertEqual(context.exception.status_code, 404)

    @unittest.skipUnless(engine.dialect.name == 'postgresql', 'PostgreSQL row-lock concurrency check')
    def test_06_concurrent_same_choice_has_one_effect(self):
        employee, journey, _, _ = self.waiting()
        employee_id, journey_id = employee.id, journey.id
        payload = self.choice(journey)
        self.db.commit()
        barrier = threading.Barrier(2)
        results, failures = [], []
        def choose():
            try:
                with SessionLocal() as db:
                    person = db.get(Entity, employee_id)
                    barrier.wait(timeout=10)
                    result = ss.choose(db, person, payload)
                    db.commit()
                    results.append(result['duplicate'])
            except BaseException as exc:
                failures.append(type(exc).__name__ + ':' + str(exc))
        threads = [threading.Thread(target=choose) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertFalse(any(thread.is_alive() for thread in threads), 'concurrent_callback_deadlock')
        self.assertEqual(failures, [])
        self.assertEqual(sorted(results), [False, True])
        self.db.expire_all()
        journey = self.db.get(Entity, journey_id)
        self.assertEqual(journey.data['completed_steps'].count('welcome'), 1)
        d.run_due_scenarios(self.db)
        self.assertEqual(len(self.outbox(employee, journey)), 2)

    def test_07_last_step_reminder_deduplicates_pending_delivery(self):
        employee, journey, _, _ = self.waiting()
        self.clock += timedelta(hours=24)
        d.run_due_scenarios(self.db)
        reminders = [row for row in self.outbox(employee, journey) if row.data.get('simple_resume')]
        self.assertEqual(len(reminders), 1)
        self.assertEqual(reminders[0].text, 'Как продолжим?')
        self.assertEqual(reminders[0].data['step_key'], 'welcome')
        for delivery_state in ('PENDING', 'QUEUED', 'SENDING', 'UNKNOWN_DELIVERY'):
            reminders[0].status = delivery_state
            self.clock += timedelta(hours=25)
            d.run_due_scenarios(self.db)
            self.assertEqual(len([row for row in self.outbox(employee, journey) if row.data.get('simple_resume')]), 1, delivery_state)
        self.receipt(reminders[0])
        self.assertEqual(journey.data['waiting_step_key'], 'welcome')
        d.run_due_scenarios(self.db)
        self.assertEqual(len([row for row in self.outbox(employee, journey) if row.data.get('simple_resume')]), 2)
        self.assertEqual(journey.data['completed_steps'], [])

    def test_08_on_bot_start_waits_for_launch_and_is_idempotent(self):
        scenario, _ = self.published()
        employee, journey = self.employee(scenario)
        d.run_due_scenarios(self.db)
        self.assertEqual(self.outbox(employee, journey), [])
        ss.notify_bot_started(self.db, employee)
        started_at = journey.data['started_at']
        ss.notify_bot_started(self.db, employee)
        self.assertEqual(journey.data['started_at'], started_at)
        self.assertEqual(len([row for row in d.entities(self.db, 'journey', employee.id) if row.data['scenario_id'] == scenario.id]), 1)
        d.run_due_scenarios(self.db)
        self.assertEqual(len(self.outbox(employee, journey)), 1)

    def test_09_start_date_midnight_and_scheduled_offset_timezone(self):
        definition = graph(condition='START_DATE')
        launch = ss.launch_at(definition, {'start_date': '2026-10-24'}, {}, self.clock)
        self.assertEqual(launch, datetime(2026, 10, 23, 21, tzinfo=timezone.utc))
        definition = graph(condition='SCHEDULED', date='2026-10-24', time='12:00')
        definition['steps'][0]['trigger'] = {'type': 'ANCHOR_TIME', 'offset_days': 1, 'local_time': '10:30'}
        self.assertEqual(ss.launch_at(definition, {}, {}, self.clock), self.clock)
        self.assertEqual(ss.due_for(definition, definition['steps'][0], {}, {}, self.clock), datetime(2026, 10, 25, 7, 30, tzinfo=timezone.utc))
        scenario, _ = self.published(graph(condition='SCHEDULED', date='2026-10-24', time='12:00'))
        employee, journey = self.employee(scenario)
        self.clock -= timedelta(minutes=1)
        d.run_due_scenarios(self.db)
        self.assertEqual(self.outbox(employee, journey), [])
        self.clock += timedelta(minutes=1)
        d.run_due_scenarios(self.db)
        self.assertEqual(len(self.outbox(employee, journey)), 1)

    def test_10_unverified_and_ended_employee_cannot_advance(self):
        scenario, _ = self.published(choices_graph())
        employee, journey = self.employee(scenario, verified=False)
        self.start(journey, employee)
        d.run_due_scenarios(self.db)
        self.assertEqual(self.outbox(employee, journey), [])
        d.update(employee, max_binding_status='VERIFIED')
        d.run_due_scenarios(self.db)
        self.receipt(self.outbox(employee, journey)[0])
        d.update(employee, employment_stage='ENDED')
        self.clock += timedelta(days=2)
        d.run_due_scenarios(self.db)
        self.assertEqual(journey.status, 'CANCELLED')
        with self.assertRaises(HTTPException) as context:
            ss.choose(self.db, employee, self.choice(journey))
        self.assertEqual(context.exception.status_code, 404)
        self.assertEqual(len(self.outbox(employee, journey)), 1)

    def survey_template(self):
        body = sc.TemplateCreate(title='Синтетический опрос release21', description='Проверка последовательности', purpose='WELLBEING',
            questions=[{'question_key': 'support', 'prompt': 'Оцените поддержку', 'type': 'LIKERT'},
                       {'question_key': 'continue', 'prompt': 'Продолжить?', 'type': 'SINGLE_CHOICE',
                        'options': [{'value': 'yes', 'label': 'Да'}, {'value': 'no', 'label': 'Нет'}]}],
            reason='Изолированный интеграционный тест')
        template = unpack(sc.create_template(body, self.db, self.hr), self.db)
        version = d.entities(self.db, 'survey_template_version', template.id)[0]
        sc.submit_review(version.id, sc.Reason(reason='Проверка формулировок'), request(version), self.db, self.hr)
        sc.review_version(version.id, sc.Review(reason='Проверено добровольное участие', decision='APPROVE'), request(version), self.db, self.hr)
        sc.publish_version(version.id, sc.Reason(reason='Публикация изолированного теста'), request(version), self.db, self.hr)
        return template, version

    def test_11_survey_reference_snapshot_all_questions_and_resume(self):
        _, template_version = self.survey_template()
        definition = graph([{'step_key': 'survey', 'kind': 'SURVEY',
            'survey_template_version_id': template_version.id, 'trigger': {'type': 'AFTER_PREVIOUS'},
            'transitions': [transition('end')]}, {'step_key': 'end', 'kind': 'END', 'transitions': []}], start='survey')
        scenario, version = self.published(definition)
        snapshot = deepcopy(version.data['graph']['steps'][0]['survey'])
        self.assertEqual(len(snapshot['questions']), 2)
        self.assertTrue(all(question['question_key'].startswith('survey:') for question in snapshot['questions']))
        employee, journey = self.employee(scenario)
        self.start(journey, employee)
        d.run_due_scenarios(self.db)
        run = d.entities(self.db, 'survey_run', journey.id)[0]
        self.assertEqual(run.data['definition'], snapshot)
        opening = next(row for row in self.outbox(employee) if row.data.get('survey_run_id') == run.id)
        self.receipt(opening)
        action = next(row for row in d.entities(self.db, 'scheduled_action', journey.id) if row.data['step_key'] == 'survey')
        self.assertEqual(action.status, 'WAITING_SURVEY')
        self.assertFalse(list(self.db.scalars(select(Job).where(Job.kind == 'DOMAIN_SURVEY_REMINDER', Job.payload['survey_run_id'].as_string() == run.id))))
        actor = Actor(id=employee.id, name=employee.data['name'], role='EMPLOYEE', scope={}, employee_id=employee.id)
        first, second = snapshot['questions']
        d.submit_survey_answer(self.db, actor, run.id, {'question_id': first['question_key'], 'value': 4, 'state_revision': 1})
        self.assertEqual(run.data['current_question_key'], second['question_key'])
        self.clock += timedelta(hours=24)
        d.run_due_scenarios(self.db)
        reminders = [row for row in self.outbox(employee, journey) if row.data.get('simple_resume')]
        self.assertEqual(len(reminders), 1)
        self.assertEqual(reminders[0].callback_buttons[0]['payload']['survey_run_id'], run.id)
        self.assertEqual(run.data['current_question_key'], second['question_key'])
        d.submit_survey_answer(self.db, actor, run.id, {'question_id': second['question_key'], 'value': 'yes', 'state_revision': 2})
        d.finish_survey(self.db, actor, run.id)
        d.run_due_scenarios(self.db)
        self.assertEqual(run.status, 'COMPLETED')
        self.assertEqual(journey.status, 'COMPLETED')
        self.assertEqual(len(run.data['answers']), 2)

    def test_12_draft_survey_reference_rejected_and_source_snapshot_stable(self):
        template, template_version = self.survey_template()
        definition = graph([{'step_key': 'survey', 'kind': 'SURVEY',
            'survey_template_version_id': template_version.id, 'trigger': {'type': 'AFTER_PREVIOUS'},
            'transitions': []}], start='survey')
        scenario, version = self.published(definition)
        pinned = deepcopy(version.data['graph']['steps'][0]['survey'])
        # A separate mutable source record cannot rewrite the already stored
        # scenario snapshot. This is confined to the disposable synthetic DB.
        d.update(template_version, title='Изменённый тестовый источник')
        self.assertEqual(version.data['graph']['steps'][0]['survey'], pinned)
        draft_source = d.save_entity(self.db, 'survey_template_version', deepcopy(template_version.data), status='DRAFT', parent_id=template.id)
        bad = deepcopy(definition)
        bad['steps'][0]['survey_template_version_id'] = draft_source.id
        with self.assertRaises(HTTPException) as context:
            d.create_scenario_version(scenario.id, {'graph': bad}, self.db, self.hr)
        self.assertEqual(context.exception.status_code, 422)

    def test_13_legacy_route_still_valid_and_not_converted(self):
        definition = legacy_graph()
        self.assertNotEqual(definition.get('editor_mode'), 'SIMPLE')
        self.assertTrue(d.validate_graph(definition)['valid'])
        self.assertTrue(any(step.get('mandatory') for step in definition['steps']))

    def test_14_basic_formulations_on_greeting_and_stale_etag(self):
        current = bf.get_basic_formulations(actor=self.hr, db=self.db)
        old_etag = current.headers['etag']
        supplied = Request({'type': 'http', 'headers': [(b'if-match', old_etag.encode())]})
        body = bf.BasicFormulationsInput(items=[{
            'id': 'r21_basic', 'label': 'Базовый вопрос release21',
            'query_text': 'Как найти внутреннюю инструкцию?',
        }])
        saved = bf.put_basic_formulations(body, supplied, actor=self.hr, db=self.db)
        self.assertNotEqual(saved.headers['etag'], old_etag)
        with self.assertRaises(HTTPException) as context:
            bf.put_basic_formulations(body, supplied, actor=self.hr, db=self.db)
        self.assertEqual(context.exception.status_code, 412)
        scenario, _ = self.published(choices_graph())
        employee, journey = self.employee(scenario)
        self.start(journey, employee)
        d.run_due_scenarios(self.db)
        labels = [button['text'] for button in self.outbox(employee, journey)[0].callback_buttons]
        self.assertIn('Базовый вопрос release21', labels)
        self.assertIn('Где обедать?', labels)
        self.assertIn('Продолжить', labels)

    def test_15_stale_resume_outbox_fenced_after_employee_advances(self):
        employee, journey, _, _ = self.waiting()
        binding = d.save_entity(self.db, 'max_binding', {
            'max_user_id': '555000', 'episode_id': employee.data['episode_id'],
        }, status='ACTIVE', parent_id=employee.id)
        d.update(employee, binding_id=binding.id)
        self.clock += timedelta(hours=24)
        d.run_due_scenarios(self.db)
        reminder = next(row for row in self.outbox(employee, journey) if row.data.get('simple_resume'))
        self.assertEqual(tx.valid_outbox(self.db, reminder), ({'user_id': 555000}, None))
        ss.choose(self.db, employee, self.choice(journey))
        self.assertEqual(tx.valid_outbox(self.db, reminder)[1], 'SCENARIO_ALREADY_ADVANCED')

    def test_16_message_material_attaches_and_revocation_pauses_before_send(self):
        material = d.save_entity(self.db, 'asset', {
            'purpose': 'SCENARIO_MATERIAL', 'filename': 'synthetic.txt',
            'content_type': 'text/plain', 'scope': {'site_ids': ['GrPP'],
                'employment_stages': ['PREBOARDING', 'ACTIVE']},
        }, status='READY')
        definition = graph()
        definition['steps'][0]['content']['asset_ids'] = [material.id]
        scenario, _ = self.published(definition)
        employee, journey = self.employee(scenario)
        self.start(journey, employee)
        d.run_due_scenarios(self.db)
        self.assertEqual(self.outbox(employee, journey)[0].attachments, [material.id])
        material.status = 'REVOKED'
        other, other_journey = self.employee(scenario)
        self.start(other_journey, other)
        d.run_due_scenarios(self.db)
        self.assertEqual(self.outbox(other, other_journey), [])
        self.assertEqual(other_journey.status, 'PAUSED')
        self.assertEqual(other_journey.data['review_reason'], 'SCENARIO_ASSET_UNAVAILABLE')

    def test_17_undeliverable_authored_and_rendered_text_prevented(self):
        invalid = graph()
        invalid['steps'][0]['content'] = {'text_ty': 'x' * 3901}
        self.assertFalse(ss.validate(invalid)['valid'])
        definition = graph()
        definition['steps'][0]['content'] = {'text_ty': 'x' * 3880 + '{{name}}'}
        self.assertTrue(ss.validate(definition)['valid'])
        scenario, _ = self.published(definition)
        employee, journey = self.employee(scenario)
        self.start(journey, employee)
        d.run_due_scenarios(self.db)
        self.assertEqual(self.outbox(employee, journey), [])
        self.assertEqual(journey.status, 'PAUSED')
        self.assertEqual(journey.data['review_reason'], 'MESSAGE_LENGTH_EXCEEDED')

    def test_18_real_signed_callback_decodes_and_routes_only_its_answer(self):
        from korsik.chat import conversation_for
        employee, journey, _, opening = self.waiting()
        binding = d.save_entity(self.db, 'max_binding', {
            'max_user_id': '555001', 'episode_id': employee.data['episode_id'],
        }, status='ACTIVE', parent_id=employee.id)
        d.update(employee, binding_id=binding.id)
        conversation = conversation_for(self.db, employee)
        with patch.object(tx, 'read_secret', return_value='synthetic-release21-signing-key'), patch.object(me, 'read_secret', return_value='synthetic-release21-signing-key'):
            keyboard = tx.signed_buttons(self.db, opening)
            signed = next(button['payload'] for row in keyboard for button in row if button['text'] == 'Продолжить')
            self.assertLess(len(signed), 128)
            decoded = me.callback_payload(self.db, signed, '555001', employee)
            self.assertEqual(decoded, self.choice(journey))
            forged = signed[:-1] + ('1' if signed[-1] == '0' else '0')
            with self.assertRaises(HTTPException) as context:
                me.callback_payload(self.db, forged, '555001', employee)
            self.assertEqual(context.exception.status_code, 403)
            me.handle_callback(self.db, employee, conversation, decoded, 'synthetic-release21-' + self.suffix)
        d.run_due_scenarios(self.db)
        self.assertEqual([row.text for row in self.outbox(employee, journey)], ['Как продолжим?', 'Выбран только этот путь'])


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(SimpleScenarioIntegration)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {'schema': 'korsik-release21-integration-v1', 'status': 'PASS' if result.wasSuccessful() else 'FAILED',
              'database_name': DATABASE_NAME, 'dialect': engine.dialect.name,
              'checks_count': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
              'skipped': [{'test': str(test), 'reason': reason} for test, reason in result.skipped],
              'MAX_messages_sent': 0, 'model_requests': 0, 'synthetic_records_only': True,
              'observed_at': datetime.now(timezone.utc).isoformat()}
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report), flush=True)
    raise SystemExit(0 if result.wasSuccessful() else 1)
