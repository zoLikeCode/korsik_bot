"""Personal/current-manager/direct-team profiles and narrow real-image ACL."""
from types import SimpleNamespace
import io
from datetime import timedelta
import pytest
from fastapi import HTTPException
from PIL import Image
from sqlalchemy import update
from korsik import miniapp_profiles as profiles, domain as d, organization as org
from korsik.auth import Actor
from korsik.common import save_entity
from korsik.models import Entity, User
from test_domain import session as session, hr as hr, profile


def expect_error(code, fn, *args, **kwargs):
    with pytest.raises(HTTPException) as caught:fn(*args, **kwargs)
    assert caught.value.status_code == code


def photo(db, employee, tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, 'get_settings', lambda: SimpleNamespace(blob_root=str(tmp_path)))
    path=tmp_path/(employee.id+'.png');Image.new('RGB',(20,30),(30,100,180)).save(path)
    asset=save_entity(db,'asset',{'episode_id':employee.data['episode_id'],'storage_path':str(path),
        'validated':True,'content_type':'image/png'},owner_id=employee.id,status='READY')
    d.update(employee,profile_photo_asset_id=asset.id)
    db.commit();return asset


def manager_fixture(db, hr):
    leader=profile(db,hr,name='Руководитель',email='leader@example.test')
    user=User(id='manager27',name='Руководитель',email='manager@example.test',role='MANAGER',scope={},
        employee_id=leader.id,password_hash='unused',enabled=True)
    db.add(user);db.commit()
    actor=Actor(user.id,user.name,'MANAGER',{},leader.id,user.email)
    worker=profile(db,hr,name='Подчинённый',email='worker@example.test')
    d.update(worker,reports_to_employee_id=leader.id)
    db.commit();return leader,user,actor,worker


def test_employee_own_profile_manager_and_dates_are_real_and_private_fields_omitted(session,hr):
    leader,user,manager,employee=manager_fixture(session,hr)
    d.update(employee,profile_bio='PRIVATE',personal_notes='PRIVATE',department='Тестовый отдел')
    actor=Actor('self','Сотрудник','EMPLOYEE',{},employee.id,'login@internal.invalid')
    data=profiles.my_profile(session,actor)
    assert data['employee']['email']=='worker@example.test'
    assert data['employee']['start_date']==employee.data['start_date']
    assert data['employee']['probation_end_date']==employee.data['probation_end_date']
    assert data['employee']['department']=='Тестовый отдел'
    assert data['manager']['id']==leader.id and data['direct_reports']==[]
    assert 'PRIVATE' not in str(data) and 'scope' not in data['user']


def test_manager_current_direct_team_uses_org_graph_and_truthful_adaptation(session,hr):
    leader,user,actor,worker=manager_fixture(session,hr)
    other=profile(session,hr,name='Не прямой подчинённый')
    d.update(other,reports_to_employee_id=worker.id)
    for journey in d.entities(session,'journey',worker.id):journey.status='COMPLETED'
    active=save_entity(session,'journey',{'episode_id':worker.data['episode_id']},parent_id=worker.id,status='PAUSED')
    data=profiles.my_profile(session,actor)
    assert [r['id'] for r in data['direct_reports']]==[worker.id]
    assert data['direct_reports'][0]['is_adapting'] is True
    active.status='COMPLETED';session.flush()
    assert profiles.my_profile(session,actor)['direct_reports'][0]['is_adapting'] is False
    active.status='ACTIVE';d.update(active,episode_id='previous')
    assert profiles.my_profile(session,actor)['direct_reports'][0]['is_adapting'] is False


def test_manager_without_employment_card_uses_real_user_and_legacy_direct_reports(session,hr):
    user=User(id='standalone',name='Без карточки',email='real@example.test',role='MANAGER',scope={},password_hash='unused',enabled=True)
    session.add(user);session.commit()
    employee=profile(session,hr,manager_id=user.id)
    actor=Actor(user.id,user.name,user.role,{},None,user.email)
    data=profiles.my_profile(session,actor)
    assert data['employee'] is None and data['user']['email']=='real@example.test'
    assert [r['id'] for r in data['direct_reports']]==[employee.id]
    assert 'start_date' not in data['user'] and 'profile_photo_url' not in data['user']


def test_unit_head_manager_relationship_and_scope_restrictions(session,hr):
    leader,user,actor,worker=manager_fixture(session,hr)
    unit=save_entity(session,'org_unit',{'title':'Группа','type':'TEAM','legal_entity_id':leader.data['legal_entity_id'],
        'head_employee_id':leader.id,'site_id':'GrPP'})
    d.update(worker,reports_to_employee_id=None,org_unit_id=unit.id)
    assert profiles.my_profile(session,actor)['direct_reports'][0]['id']==worker.id
    restricted=Actor(actor.id,actor.name,'MANAGER',{'site_ids':['other']},leader.id)
    assert profiles.my_profile(session,restricted)['direct_reports']==[]
    d.update(worker,enabled=False)
    assert profiles.my_profile(session,actor)['direct_reports']==[]


def test_real_photo_self_manager_direct_report_allowed_but_unrelated_or_stale_denied(session,hr,tmp_path,monkeypatch):
    leader,user,actor,worker=manager_fixture(session,hr)
    photo(session,leader,tmp_path,monkeypatch)
    asset=photo(session,worker,tmp_path,monkeypatch)
    employee_actor=Actor('employee','Employee','EMPLOYEE',{},worker.id)
    assert profiles.profile_photo(worker.id,session,employee_actor).media_type=='image/jpeg'
    assert profiles.profile_photo(leader.id,session,employee_actor).body[:2]==b'\xff\xd8'
    assert profiles.profile_photo(worker.id,session,actor).body[:2]==b'\xff\xd8'
    assert profiles.profile_photo(leader.id,session,actor).status_code==200
    unrelated=profile(session,hr)
    photo(session,unrelated,tmp_path,monkeypatch)
    expect_error(404,profiles.profile_photo,unrelated.id,session,employee_actor)
    expect_error(404,profiles.profile_photo,unrelated.id,session,actor)
    d.update(asset,episode_id='old-episode')
    expect_error(404,profiles.profile_photo,worker.id,session,actor)
    assert profiles.my_profile(session,actor)['direct_reports'][0]['profile_photo_url'] is None


def test_photo_relationship_move_and_off_are_checked_on_each_request(session,hr,tmp_path,monkeypatch):
    leader,user,actor,worker=manager_fixture(session,hr)
    photo(session,worker,tmp_path,monkeypatch)
    assert profiles.profile_photo(worker.id,session,actor).status_code==200
    d.update(worker,reports_to_employee_id=None,manager_id=None)
    expect_error(404,profiles.profile_photo,worker.id,session,actor)
    d.update(leader,enabled=False)
    expect_error(403,profiles.my_profile,session,actor)
    employee_actor=Actor('employee','Employee','EMPLOYEE',{},worker.id)
    worker.status='DELETED';session.flush()
    expect_error(403,profiles.my_profile,session,employee_actor)
    expect_error(403,profiles.my_profile,session,hr)


def test_explicit_unavailable_manager_or_unit_head_does_not_resurrect_legacy_manager(session,hr):
    leader,user,actor,worker=manager_fixture(session,hr)
    d.update(worker,manager_id=user.id,reports_to_employee_id='missing-explicit-manager')
    employee_actor=Actor('self','Self','EMPLOYEE',{},worker.id)
    assert profiles.my_profile(session,employee_actor)['manager'] is None
    unit=save_entity(session,'org_unit',{'title':'Team','legal_entity_id':worker.data['legal_entity_id'],
        'head_employee_id':'missing-unit-head','site_id':'GrPP'})
    d.update(worker,reports_to_employee_id=None,org_unit_id=unit.id)
    assert profiles.my_profile(session,employee_actor)['manager'] is None


def test_photo_rejects_conflicting_employee_marker_even_when_owner_matches(session,hr,tmp_path,monkeypatch):
    leader,user,actor,worker=manager_fixture(session,hr)
    asset=photo(session,worker,tmp_path,monkeypatch)
    d.update(asset,employee_id=leader.id)
    expect_error(404,profiles.profile_photo,worker.id,session,actor)


def test_cached_photo_asset_rechecks_current_validation_before_serving(session,hr,tmp_path,monkeypatch):
    leader,user,actor,worker=manager_fixture(session,hr)
    asset=photo(session,worker,tmp_path,monkeypatch)
    assert profiles.profile_photo(worker.id,session,actor).status_code==200
    session.execute(update(Entity).where(Entity.id==asset.id).values(status='REJECTED'),
                    execution_options={'synchronize_session':False})
    assert asset.status=='READY'  # a previously cached ORM object is deliberately stale
    expect_error(404,profiles.profile_photo,worker.id,session,actor)
