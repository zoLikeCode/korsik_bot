from copy import deepcopy
from pathlib import Path
import pytest
from fastapi import HTTPException
from korsik import domain as d, scenario_media as media
from korsik.auth import Actor
from korsik.common import save_entity
from test_domain import session, hr, profile


def asset(db, scope, mime='image/png', status='READY', purpose='SCENARIO_MATERIAL'):
    return save_entity(db, 'asset', {'filename':'image.png','content_type':mime,'scope':{'employment_stages':['PREBOARDING','ACTIVE'], **scope},'purpose':purpose},status=status)


def test_gallery_filters_ready_image_purpose_and_live_scope(session, hr):
    allowed = asset(session, {'site_ids':['GrPP']})
    asset(session, {'site_ids':['GTP']}); asset(session, {'site_ids':['GrPP']}, mime='application/pdf')
    asset(session, {'site_ids':['GrPP']}, status='FAILED'); asset(session, {'site_ids':['GrPP']}, purpose='PRIVATE')
    actor = Actor('head','Руководитель','HEAD_TALENT',{'site_ids':['GrPP']})
    result = media.images(actor,session)
    assert [r['id'] for r in result['items']] == [allowed.id]
    assert result['builtins'][0]['id'] == 'korsik'
    for role in ('EMPLOYEE','MANAGER'):
        with pytest.raises(HTTPException) as e: media.images(Actor('x','x',role,{}),session)
        assert e.value.status_code == 403


def test_real_builtin_is_validated_private_asset_not_arbitrary_remote_url(session, hr, tmp_path, monkeypatch):
    from korsik import assets
    from types import SimpleNamespace
    monkeypatch.setattr(assets, 'get_settings', lambda:SimpleNamespace(blob_root=str(tmp_path)))
    result = media.select_builtin({'id':'korsik'},hr,session)
    row = session.get(d.Entity,result['id'])
    assert row.status == 'READY' and row.data['validated'] and row.data['builtin'] == 'korsik'
    assert Path(row.data['storage_path']).read_bytes() == (Path(media.__file__).parent/'resources'/'korsik.png').read_bytes()
    assert media.select_builtin({'id':'korsik'},hr,session)['id'] == row.id
    with pytest.raises(HTTPException): media.select_builtin({'id':'https://example.org/x.png'},hr,session)


def test_author_and_employee_asset_checks_are_both_required(session, hr):
    employee = profile(session,hr,site_id='GrPP')
    other = asset(session,{'site_ids':['GTP']})
    definition={'steps':[{'content':{'asset_ids':[other.id]}}]}
    scoped = Actor('hr-scoped','HR','HR_SPECIALIST',{'site_ids':['GrPP']})
    with pytest.raises(HTTPException): media.validate_authored_materials(session,scoped,definition)
    # An all-scope HR can author it, but it still cannot reach an inapplicable employee.
    media.validate_authored_materials(session,hr,definition)
    with pytest.raises(HTTPException): media.validated_materials(session,employee,[other.id])
    own=asset(session,{'site_ids':['GrPP']})
    assert media.validated_materials(session,employee,[own.id,own.id]) == [own.id]


def test_legacy_message_materials_reach_actual_outbox_and_missing_material_pauses(session, hr, monkeypatch):
    from datetime import datetime, timezone
    from sqlalchemy import select
    from korsik.models import Outbox
    from test_simple_scenarios import program
    clock=datetime(2026,10,25,9,tzinfo=timezone.utc)
    monkeypatch.setattr(d,'now',lambda:clock)
    employee=profile(session,hr,start_date='2026-10-25')
    for old in d.entities(session,'journey',employee.id): old.status='CANCELLED'
    d.update(employee,max_binding_status='VERIFIED')
    image=asset(session,{'site_ids':['GrPP']})
    definition={'schema_version':'korsik-scenario-v2','algorithm_version':'calendar-v1','start_step_key':'greeting','steps':[
      {'step_key':'greeting','kind':'MESSAGE','sequence_no':1,'mandatory':False,'eligibility':{},'delivery_policy':{},
       'trigger':{'type':'ANCHOR_TIME','anchor':'START_DATE','offset_days':0,'day_basis':'CALENDAR','local_time':'10:00'},
       'content':{'text_ty':'Привет','text_vy':'Привет','asset_ids':[image.id]},'transitions':[]}]}
    scenario,_=program(session,definition)
    journey=d.enroll_employee(session,employee,scenario.id)
    d.run_due_scenarios(session)
    outbound=next(o for o in session.scalars(select(Outbox)) if (o.data or {}).get('journey_id')==journey.id)
    assert outbound.attachments == [image.id] and outbound.text == 'Привет'
    other=profile(session,hr,start_date='2026-10-25')
    for old in d.entities(session,'journey',other.id): old.status='CANCELLED'
    d.update(other,max_binding_status='VERIFIED')
    image.status='FAILED'
    blocked=d.enroll_employee(session,other,scenario.id)
    d.run_due_scenarios(session)
    assert blocked.status=='PAUSED' and blocked.data['review_reason']=='SCENARIO_ASSET_UNAVAILABLE'
    assert not [o for o in session.scalars(select(Outbox)) if (o.data or {}).get('journey_id')==blocked.id]
