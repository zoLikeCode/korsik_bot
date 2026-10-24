"""Signed MAX fixtures, current binding/role and cookie/CSRF boundary tests."""
import hmac
import json
import uuid
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import quote, urlencode

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from korsik import auth, max_miniapp as mini
from korsik.common import save_entity
from korsik.db import get_db, make_engine, new_id, now
from korsik.models import Base, Entity, SessionToken, User

TOKEN = "TEST_ONLY_MAX_BOT_TOKEN_DO_NOT_DEPLOY"


def signed(uid=67890, *, issued=None, query=None, **user_fields):
    params = {"user": json.dumps({"id": uid, **user_fields}, ensure_ascii=False, separators=(",", ":")),
              "auth_date": str(int(now().timestamp()) if issued is None else issued), "query_id": query or str(uuid.uuid4())}
    # Test fixture signer is deliberately separate from production parsing and
    # validation. The immutable reference vector below additionally uses the
    # official algorithm independently evaluated by Node WebCrypto.
    message = "\n".join(f"{key}={params[key]}" for key in sorted(params))
    key = hmac.digest(b"WebAppData", TOKEN.encode(), "sha256")
    params["hash"] = hmac.digest(key, message.encode(), "sha256").hex()
    return urlencode(params, quote_via=quote)


def test_official_node_webcrypto_reference_vector_and_signature_tamper():
    # Reference fields are the published MAX validation example. This fixed
    # digest was obtained using official TypeScript/WebCrypto key/message order.
    raw = ('auth_date=1771409719&chat=%7B%22id%22%3A12345%2C%22type%22%3A%22DIALOG%22%7D&ip=192.168.0.1&query_id=4c0ab423-342b-4e45-aea4-2747dbc500cd&user=%7B%22id%22%3A67890%2C%22first_name%22%3A%22Max%22%2C%22last_name%22%3A%22User%22%2C%22username%22%3Anull%2C%22language_code%22%3A%22ru%22%2C%22photo_url%22%3Anull%7D&hash=600e7a12f0d96a642e269528c51a9f935c5f4165ed9b4f984cd054a207fb86bf')
    assert mini.validate_init_data(raw, TOKEN, timestamp=1771409800).max_user_id == "67890"
    with pytest.raises(HTTPException) as failure:
        mini.validate_init_data(raw.replace('67890', '67891'), TOKEN, timestamp=1771409800)
    assert failure.value.detail['code'] == 'max_init_data_invalid'


@pytest.fixture
def app_db(tmp_path, monkeypatch):
    engine = make_engine('sqlite:///:memory:'); Base.metadata.create_all(engine)
    db = sessionmaker(engine, expire_on_commit=False)()
    secret = tmp_path / 'bot-token'; secret.write_text(TOKEN)
    monkeypatch.setattr(mini, 'get_settings', lambda: SimpleNamespace(public_url='https://korsik.test', secure_cookies=True, max_token_file=str(secret), session_ttl_seconds=43200))
    app = FastAPI(); app.include_router(mini.router); app.include_router(auth.router)
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app, base_url='https://korsik.test')
    yield app, client, db
    db.close(); engine.dispose()


def principal(db, uid=67890, role='HR_ADMIN', status='ACTIVE'):
    user = User(id=new_id(), email=f'{new_id()}@example.test', name='Trusted account', password_hash='not-used', role=role, scope={'site_ids':['site-a']}, enabled=True)
    db.add(user); db.flush()
    binding = save_entity(db, 'principal_binding', {'channel':'MAX', 'max_user_id':str(uid), 'confirmed_at':now().isoformat()}, owner_id=user.id, status=status)
    db.commit(); return user, binding


def employee_binding(db, uid=67890, *, status='ACTIVE', stage='ACTIVE', with_user=False):
    employee = save_entity(db, 'employee', {'name':'Synthetic employee', 'employment_stage':stage,'episode_id':new_id()})
    binding = save_entity(db, 'max_binding', {'max_user_id':str(uid),'episode_id':employee.data['episode_id']}, parent_id=employee.id, status=status)
    employee.data = {**employee.data,'binding_id':binding.id}
    user = None
    if with_user:
        user = User(id=new_id(), email=f'{new_id()}@example.test', name='Existing employee', password_hash='not-used', role='EMPLOYEE', scope={}, employee_id=employee.id, enabled=True)
        db.add(user); db.flush(); employee.data={**employee.data,'user_id':user.id}
    db.commit(); return employee,binding,user


def test_signed_principal_current_role_session_and_secure_embedded_cookies(app_db):
    app, client, db = app_db
    user, binding = principal(db, role='MANAGER')
    result = client.post('/auth/max-miniapp', json={'init_data':signed(role='HR_ADMIN',username='administrator')})
    assert result.status_code == 200, result.text
    assert result.json()['actor']['role']=='MANAGER' and result.json()['actor']['id']==user.id
    assert result.headers['cache-control']=='no-store'
    cookies=result.headers.get_list('set-cookie')
    assert len(cookies)==2 and all('Secure' in v and 'SameSite=none' in v and 'Max-Age=3600' in v for v in cookies)
    assert 'HttpOnly' in cookies[0] and 'HttpOnly' not in cookies[1]
    session=client.get('/auth/session')
    assert session.status_code==200 and session.json()['csrf_token']==result.json()['csrf_token']
    stored=db.scalar(select(SessionToken)); assert stored.csrf_hash==auth.hash_token(result.json()['csrf_token'])
    metadata=db.scalar(select(Entity).where(Entity.kind=='max_miniapp_session'))
    assert len(metadata.id)<=36 and len(metadata.parent_id)<=36 and metadata.data['session_id']==stored.id
    assert all(result.json()['csrf_token'] not in str(e.data) for e in db.scalars(select(Entity)))
    user.role='HR_SPECIALIST'; user.scope={'site_ids':['site-b']};db.commit()
    current=client.get('/auth/session').json()['actor']
    assert current['role']=='HR_SPECIALIST' and current['scope']=={'site_ids':['site-b']}
    binding.status='REVOKED';db.commit()
    assert client.get('/auth/session').status_code==401


def test_same_browser_reload_allowed_but_stolen_launch_cannot_mint_session(app_db):
    app, client, db=app_db;principal(db)
    data={'init_data':signed()}
    first=client.post('/auth/max-miniapp',json=data);assert first.status_code==200
    assert client.post('/auth/max-miniapp',json=data).json()['csrf_token']==first.json()['csrf_token']
    attacker=TestClient(app,base_url='https://korsik.test')
    replay=attacker.post('/auth/max-miniapp',json=data)
    assert replay.status_code==409 and replay.json()['detail']['code']=='max_init_data_replayed'
    assert len(list(db.scalars(select(SessionToken))))==1


def test_verified_employee_binding_provisions_only_employee_and_ends_access(app_db):
    app,client,db=app_db
    employee,binding,_=employee_binding(db)
    result=client.post('/auth/max-miniapp',json={'init_data':signed(role='HR_ADMIN',first_name='Boss')})
    assert result.status_code==200,result.text
    actor=result.json()['actor'];assert actor['role']=='EMPLOYEE' and actor['employee_id']==employee.id and actor['scope']=={}
    user=db.get(User,actor['id']);assert user.email.endswith('@internal.invalid') and user.name==employee.data['name']
    assert user.password_hash not in result.text and TOKEN not in result.text
    assert employee.data['user_id']==user.id
    employee.data={**employee.data,'employment_stage':'ENDED'};db.commit()
    assert client.get('/auth/session').status_code==401


@pytest.mark.parametrize('status,stage',[('PENDING_VERIFICATION','ACTIVE'),('ACTIVE','ENDED')])
def test_pending_or_ended_employee_never_creates_or_enables_account(app_db,status,stage):
    app,client,db=app_db;employee_binding(db,status=status,stage=stage)
    result=client.post('/auth/max-miniapp',json={'init_data':signed()})
    assert result.status_code==403 and result.json()['detail']['code']=='max_account_not_linked'
    assert not list(db.scalars(select(User))) and not list(db.scalars(select(SessionToken)))


def test_disabled_existing_employee_is_not_replaced_or_reenabled(app_db):
    app,client,db=app_db;e,b,user=employee_binding(db,with_user=True);user.enabled=False;db.commit()
    result=client.post('/auth/max-miniapp',json={'init_data':signed()})
    assert result.status_code==403 and not user.enabled and len(list(db.scalars(select(User))))==1


def test_current_employee_pointer_ignores_only_disabled_historical_accounts(app_db):
    app, client, db = app_db
    employee, binding, current = employee_binding(db, with_user=True)
    historical = User(id=new_id(), email=f'{new_id()}@example.test', name='Historical employee',
                      password_hash='not-used', role='EMPLOYEE', scope={}, employee_id=employee.id, enabled=False)
    db.add(historical); db.commit()
    result = client.post('/auth/max-miniapp', json={'init_data': signed(role='HR_ADMIN')})
    assert result.status_code == 200, result.text
    assert result.json()['actor']['id'] == current.id and result.json()['actor']['role'] == 'EMPLOYEE'
    assert client.get('/auth/session').json()['actor']['id'] == current.id
    assert not historical.enabled and len(list(db.scalars(select(User)))) == 2
    assert employee.data['user_id'] == current.id and employee.data['binding_id'] == binding.id


@pytest.mark.parametrize('pointer_state', ['missing', 'disabled_history', 'foreign_card'])
def test_employee_pointer_must_identify_the_enabled_account_on_this_card(app_db, pointer_state):
    app, client, db = app_db
    employee, binding, current = employee_binding(db, with_user=True)
    historical = User(id=new_id(), email=f'{new_id()}@example.test', name='Historical employee',
                      password_hash='not-used', role='EMPLOYEE', scope={}, employee_id=employee.id, enabled=False)
    db.add(historical)
    pointer = new_id() if pointer_state == 'missing' else historical.id
    if pointer_state == 'foreign_card':
        foreign = User(id=new_id(), email=f'{new_id()}@example.test', name='Other card',
                       password_hash='not-used', role='EMPLOYEE', scope={}, employee_id=new_id(), enabled=True)
        db.add(foreign); pointer = foreign.id
    employee.data = {**employee.data, 'user_id': pointer}; db.commit()
    users_before = len(list(db.scalars(select(User))))
    result = client.post('/auth/max-miniapp', json={'init_data': signed()})
    assert result.status_code == 403 and result.json()['detail']['code'] == 'max_account_unavailable'
    assert not list(db.scalars(select(SessionToken)))
    assert len(list(db.scalars(select(User)))) == users_before
    assert not historical.enabled and employee.data['user_id'] == pointer


def test_trusted_pointer_cannot_choose_between_two_enabled_accounts(app_db):
    app, client, db = app_db
    employee, binding, current = employee_binding(db, with_user=True)
    second = User(id=new_id(), email=f'{new_id()}@example.test', name='Second enabled account',
                  password_hash='not-used', role='EMPLOYEE', scope={}, employee_id=employee.id, enabled=True)
    db.add(second); db.commit()
    result = client.post('/auth/max-miniapp', json={'init_data': signed()})
    assert result.status_code == 409 and result.json()['detail']['code'] == 'max_identity_ambiguous'
    assert not list(db.scalars(select(SessionToken))) and employee.data['user_id'] == current.id


def test_enabled_employee_pointer_with_wrong_role_never_authenticates(app_db):
    app, client, db = app_db
    employee, binding, current = employee_binding(db, with_user=True)
    current.role = 'MANAGER'; db.commit()
    result = client.post('/auth/max-miniapp', json={'init_data': signed(role='EMPLOYEE')})
    assert result.status_code == 403 and result.json()['detail']['code'] == 'max_account_unavailable'
    assert not list(db.scalars(select(SessionToken))) and current.role == 'MANAGER'


@pytest.mark.parametrize('historical_count', [1, 2])
def test_missing_pointer_does_not_provision_around_disabled_history(app_db, historical_count):
    app, client, db = app_db
    employee, binding, current = employee_binding(db, with_user=True)
    current.enabled = False
    employee.data = {key: value for key, value in employee.data.items() if key != 'user_id'}
    if historical_count == 2:
        db.add(User(id=new_id(), email=f'{new_id()}@example.test', name='Other disabled history',
                    password_hash='not-used', role='EMPLOYEE', scope={}, employee_id=employee.id, enabled=False))
    db.commit()
    result = client.post('/auth/max-miniapp', json={'init_data': signed()})
    assert result.status_code == (403 if historical_count == 1 else 409)
    assert not list(db.scalars(select(SessionToken)))
    assert len(list(db.scalars(select(User)))) == historical_count
    assert all(not user.enabled for user in db.scalars(select(User)))


def test_principal_and_employee_identity_conflict_does_not_choose_hr(app_db):
    app,client,db=app_db;principal(db);employee_binding(db)
    result=client.post('/auth/max-miniapp',json={'init_data':signed()})
    assert result.status_code==409 and result.json()['detail']['code']=='max_identity_ambiguous'
    assert not list(db.scalars(select(SessionToken)))


def test_existing_unrelated_browser_hr_cookie_does_not_override_signed_employee(app_db):
    app,client,db=app_db;hr,_=principal(db,uid=99999);e,b,user=employee_binding(db,with_user=True)
    csrf='old-csrf';raw='existing-hr-browser-session'
    db.add(SessionToken(id=auth.hash_token(raw),user_id=hr.id,csrf_hash=auth.hash_token(csrf),expires_at=now()+timedelta(hours=1)));db.commit()
    client.cookies.set('korsik_session',raw,domain='korsik.test',path='/')
    result=client.post('/auth/max-miniapp',json={'init_data':signed()})
    assert result.status_code==200 and result.json()['actor']['id']==user.id and result.json()['actor']['role']=='EMPLOYEE'
    assert client.get('/auth/session').json()['actor']['id']==user.id


@pytest.mark.parametrize('offset,code',[(-3601,'max_init_data_expired'),(90,'max_init_data_future')])
def test_launch_time_bounds_are_enforced_before_provisioning(app_db,offset,code):
    app,client,db=app_db;employee_binding(db)
    result=client.post('/auth/max-miniapp',json={'init_data':signed(issued=int(now().timestamp())+offset)})
    assert result.status_code==401 and result.json()['detail']['code']==code
    assert not list(db.scalars(select(User))) and not list(db.scalars(select(SessionToken)))


@pytest.mark.parametrize('mutation',['duplicate_hash','duplicate_user','malformed_percent','unsafe_object','url_uid'])
def test_ambiguous_or_unsigned_inputs_do_not_authenticate(app_db,mutation):
    app,client,db=app_db;principal(db)
    raw=signed()
    body={'init_data':raw}
    if mutation=='duplicate_hash':body['init_data']+='&hash='+'a'*64
    if mutation=='duplicate_user':body['init_data']+='&user=%7B%22id%22%3A1%7D'
    if mutation=='malformed_percent':body['init_data']=raw.replace('%7B','%ZZ',1)
    if mutation=='unsafe_object':body={'initDataUnsafe':{'user':{'id':67890,'role':'HR_ADMIN'}}}
    if mutation=='url_uid':body={'init_data':'user_id=67890&role=HR_ADMIN'}
    result=client.post('/auth/max-miniapp',json=body)
    assert result.status_code in {401,422} and not list(db.scalars(select(SessionToken)))


def test_origin_https_and_no_client_role_parameter(app_db,monkeypatch):
    app,client,db=app_db;principal(db)
    assert client.post('/auth/max-miniapp',json={'init_data':signed()},headers={'Origin':'https://evil.test'}).status_code==403
    assert client.post('/auth/max-miniapp',json={'init_data':signed(),'role':'HR_ADMIN'}).status_code==422
    settings=mini.get_settings();settings.public_url='http://korsik.test';settings.secure_cookies=False
    monkeypatch.setattr(mini, 'get_settings', lambda: settings)
    http=TestClient(app,base_url='http://korsik.test')
    assert http.post('/auth/max-miniapp',json={'init_data':signed()}).status_code==503


def test_real_middleware_miniapp_exchange_is_public_but_session_mutations_keep_csrf(app_db, monkeypatch):
    from korsik import main
    app,client,db=app_db;principal(db)
    factory=sessionmaker(db.get_bind(),expire_on_commit=False)
    old_overrides=dict(main.app.dependency_overrides)
    main.app.dependency_overrides[get_db]=lambda:db
    monkeypatch.setattr(main,'SessionLocal',factory)
    monkeypatch.setattr(main,'get_settings',lambda:SimpleNamespace(testing=True))
    try:
        real=TestClient(main.app,base_url='https://korsik.test')
        exchange=real.post('/api/v1/auth/max-miniapp',json={'init_data':signed()})
        assert exchange.status_code==200,exchange.text
        assert real.get('/api/v1/auth/session').status_code==200
        denied=real.post('/api/v1/auth/logout',json={},headers={'Idempotency-Key':str(uuid.uuid4())})
        assert denied.status_code==403 and denied.json()['detail']['code']=='csrf_invalid'
        accepted=real.post('/api/v1/auth/logout',json={},headers={'Idempotency-Key':str(uuid.uuid4()),'X-CSRF-Token':exchange.json()['csrf_token']})
        assert accepted.status_code==200 and real.get('/api/v1/auth/session').status_code==401
    finally:
        main.app.dependency_overrides.clear();main.app.dependency_overrides.update(old_overrides)
