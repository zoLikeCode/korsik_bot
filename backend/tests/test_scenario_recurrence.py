"""Actual durable recurrence, no MAX network or wall-clock sleeps."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
import pytest
from korsik import domain as d, simple_scenarios as simple
from korsik.scenario_recurrence import ensure, next_occurrence
from korsik.models import Outbox
from test_domain import session, hr, profile, req
from test_simple_scenarios import graph, program, clock, queued, accept


def recurring():
    result = graph()
    result['start_policy'] = {'condition': 'RECURRING', 'every_days': 2, 'time': '10:00', 'timezone': 'Europe/Moscow'}
    return result


def setup(db, hr, clock):
    employee = profile(db, hr)
    d.update(employee, max_binding_status='VERIFIED')
    scenario, version = program(db, recurring())
    d.update(version, activation_at=clock[0].isoformat())
    return employee, scenario, version


@pytest.mark.parametrize('days', [0, 366, True, '2'])
def test_invalid_recurrence_intervals(days):
    definition = recurring(); definition['start_policy']['every_days'] = days
    assert not simple.validate(definition)['valid']


def test_next_local_calendar_slot_and_simulation():
    definition = recurring()
    before = datetime(2026, 10, 25, 6, 59, tzinfo=timezone.utc)
    assert next_occurrence(definition['start_policy'], before).isoformat() == '2026-10-25T07:00:00+00:00'
    assert next_occurrence(definition['start_policy'], before + timedelta(minutes=1)).isoformat() == '2026-10-27T07:00:00+00:00'
    result = simple.simulate(definition, {}, clock=before)
    assert result['timeline'][0]['due_at'] == '2026-10-25T07:00:00+00:00'
    assert result['outbound_messages_created'] == 0


def test_every_occurrence_is_new_run_and_same_tick_is_idempotent(session, hr, clock):
    employee, scenario, version = setup(session, hr, clock)
    first = ensure(session, employee, scenario, version)
    assert first.data['occurrence_at'] == '2026-10-26T07:00:00+00:00'
    assert ensure(session, employee, scenario, version).id == first.id
    assert simple.run_due(session, first, employee, clock[0]) == 0
    clock[0] = d.aware(first.data['occurrence_at'])
    assert ensure(session, employee, scenario, version).id == first.id
    simple.run_due(session, first, employee, clock[0])
    accept(session, queued(session, first)[0], clock)
    assert first.data['waiting_step_key'] == 'hello'
    second = ensure(session, employee, scenario, version)
    assert second.id != first.id and first.status in {'ACTIVE', 'SCHEDULED'}
    clock[0] = d.aware(second.data['occurrence_at'])
    assert ensure(session, employee, scenario, version).id == second.id
    assert first.status == 'CANCELLED'
    assert first.data['cancellation_reason'] == 'NEXT_RECURRING_OCCURRENCE'
    assert second.data['answers'] == {}
    assert simple.run_due(session, second, employee, clock[0]) == 1
    assert ensure(session, employee, scenario, version).id != second.id  # Only one next future reservation.
    assert len([o for o in session.scalars(select(Outbox)) if (o.data or {}).get('journey_id') == second.id]) == 1


def test_long_downtime_skips_old_slots_and_episode_and_off_are_fenced(session, hr, clock):
    employee, scenario, version = setup(session, hr, clock)
    old = ensure(session, employee, scenario, version)
    unrelated = d.entities(session, 'journey', employee.id)[0]
    unrelated_status = unrelated.status
    clock[0] += timedelta(days=80)
    latest = ensure(session, employee, scenario, version)
    assert (clock[0] - d.aware(latest.data['occurrence_at'])).total_seconds() < 2*86400
    assert old.status == 'CANCELLED' and unrelated.status == unrelated_status
    assert len([j for j in d.entities(session, 'journey', employee.id) if j.data.get('recurrence_id')]) == 2
    d.update(employee, enabled=False)
    assert ensure(session, employee, scenario, version) is None
    d.update(employee, enabled=True, episode_id='new-employment')
    new = ensure(session, employee, scenario, version)
    assert new.data['episode_id'] == 'new-employment'
    assert new.data['recurrence_id'] != latest.data['recurrence_id']
    assert simple.run_due(session, latest, employee, clock[0]) == 0


def test_unknown_previous_delivery_blocks_new_occurrence_without_replay(session, hr, clock):
    employee, scenario, version = setup(session, hr, clock)
    first = ensure(session, employee, scenario, version)
    clock[0] = d.aware(first.data['occurrence_at'])
    ensure(session, employee, scenario, version)
    simple.run_due(session, first, employee, clock[0])
    outbound = queued(session, first)[0]; outbound.status = 'UNKNOWN_DELIVERY'
    following = ensure(session, employee, scenario, version)
    clock[0] = d.aware(following.data['occurrence_at'])
    assert ensure(session, employee, scenario, version) is None
    assert following.status == 'PAUSED' and first.status != 'CANCELLED'
    assert following.data['review_reason'] == 'DELIVERY_RECONCILIATION_REQUIRED'
    assert simple.run_due(session, following, employee, clock[0]) == 0


def test_settings_title_and_calendar_save_atomically_with_original_etag(session, hr):
    import json
    from fastapi import HTTPException
    from test_scenario_lifecycle import draft
    _, version=draft(session,hr)
    original=req(version)
    definition=recurring()
    response=d.put_scenario_version(version.id,{'name':'Новая редакция','graph':definition},original,session,hr)
    result=json.loads(response.body)
    assert result['name']=='Новая редакция' and result['graph']['start_policy']['condition']=='RECURRING'
    with pytest.raises(HTTPException) as e:
        d.put_scenario_version(version.id,{'name':'Потерянная правка','graph':graph()},original,session,hr)
    assert e.value.status_code==412
    assert version.data['name']=='Новая редакция' and version.data['graph']['start_policy']['every_days']==2
