"""Explicit account lifecycle, first setup and scoped Mini App role changes."""
from copy import deepcopy
from types import SimpleNamespace
import json
import pytest
from fastapi import FastAPI, Depends, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request
from korsik import auth, auth_admin as admin, hr_accounts as accounts, organization as org, domain as d, max_miniapp as mini, transport_api as tx
from korsik.auth import Actor, user_actor
from korsik.common import save_entity, etag
from korsik.db import make_engine, get_db, now
from korsik.models import Base, Entity, User, SessionToken, Outbox
from test_domain import session as session, hr as hr, profile, req
from test_hr_accounts import head, issued, launch, denied
from test_employee_off_security import session_for, request as cookie_request


@pytest.fixture
def empty_session():
    engine = make_engine('sqlite:///:memory:'); Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        yield db
    engine.dispose()


def test_bootstrap_is_fresh_only_hidden_head_and_repeated_bootstrap_never_resets(empty_session):
    db = empty_session; auth.bootstrap_admin(db)
    user = db.get(User, auth.SETUP_USER_ID)
    assert user.email == 'admin' and user.role == 'HEAD_TALENT' and user.scope == {'all': True}
    assert auth.ph.verify(user.password_hash, 'admin') and auth.setup_change_required(db, user)
    assert auth.users(user_actor(user), db)['items'] == []
    assert d.directory_user_rows(db, user_actor(user)) == []
    assert accounts.accounts(db, user_actor(user))['items'] == []
    denied(404, admin.target, db, user_actor(user), user.id)
    user.email = 'changed-login'; user.password_hash = auth.ph.hash('changed-password-123')
    db.get(Entity, auth.SETUP_MARKER_ID).data = {'credentials_change_required': False}; db.commit()
    old = user.password_hash; auth.bootstrap_admin(db)
    assert user.email == 'changed-login' and user.password_hash == old and not auth.setup_change_required(db, user)
    assert len(list(db.scalars(select(User)))) == 1


@pytest.mark.parametrize('existing', ['USER', 'CONFIG'])
def test_bootstrap_never_adds_known_credentials_to_existing_installation(empty_session, existing):
    db = empty_session
    if existing == 'USER':
        db.add(User(id='existing', name='Existing', email='existing@example.test', role='HR_SPECIALIST',
                    scope={'site_ids': ['a']}, enabled=True, password_hash='keep-this-hash'))
    else:
        save_entity(db, 'organization', {'name': 'Already configured'})
    db.commit(); auth.bootstrap_admin(db)
    assert db.get(User, auth.SETUP_USER_ID) is None
    if existing == 'USER':
        assert db.get(User, 'existing').password_hash == 'keep-this-hash'


def test_setup_login_requires_both_changes_preserves_current_session_and_unlocks_only_after_change(empty_session, monkeypatch):
    db = empty_session; auth.bootstrap_admin(db)
    monkeypatch.setattr(auth, 'get_settings', lambda: SimpleNamespace(secure_cookies=False, session_ttl_seconds=3600))
    app = FastAPI(); app.include_router(auth.router); app.include_router(admin.router)
    app.dependency_overrides[get_db] = lambda: db
    @app.get('/business')
    def business(actor=Depends(auth.get_actor)): return {'role': actor.role}
    with TestClient(app) as client:
        result = client.post('/auth/login', json={'email': 'admin', 'password': 'admin'})
        assert result.status_code == 200 and result.json()['actor']['credentials_change_required']
        assert client.get('/business').status_code == 403
        assert client.get('/auth/session').status_code == 200
        assert client.get('/auth/credentials').json() == {'login': 'admin', 'credentials_change_required': True}
        user = db.get(User, auth.SETUP_USER_ID); _, other = session_for(db, user)
        body = {'login': 'operator28', 'new_password': 'Long-new-password-28', 'current_password': 'wrong'}
        assert client.patch('/auth/credentials', json=body).status_code == 403
        assert client.patch('/auth/credentials', json={**body, 'current_password': 'admin', 'login': 'admin'}).status_code == 422
        changed = client.patch('/auth/credentials', json={**body, 'current_password': 'admin'})
        assert changed.status_code == 200 and changed.json()['actor']['credentials_change_required'] is False
        assert db.get(SessionToken, other.id) is None
        assert client.get('/business').json() == {'role': 'HEAD_TALENT'}
        assert client.get('/auth/session').json()['actor']['login'] == 'operator28'
        assert auth.ph.verify(user.password_hash, body['new_password'])
        auth.bootstrap_admin(db)
        assert user.email == 'operator28' and len(list(db.scalars(select(User)))) == 1


def test_regular_self_credentials_require_password_unique_login_and_revoke_other_sessions(session, hr):
    user = session.get(User, hr.id); user.password_hash = auth.ph.hash('old-password-28'); session.commit()
    raw, current = session_for(session, user); _, other = session_for(session, user)
    request = cookie_request(raw)
    foreign = User(id='other-login', name='Other', email='taken@example.test', role='MANAGER', scope={}, password_hash='unused', enabled=True)
    session.add(foreign); session.commit()
    body = {'login': 'taken@example.test', 'current_password': 'old-password-28', 'new_password': ' new-password-with-spaces '}
    denied(409, admin.change_own_credentials, admin.OwnCredentials(**body), request, session, hr)
    result = admin.change_own_credentials(admin.OwnCredentials(**{**body, 'login': ' New-Login28 '}), request, session, hr)
    assert result['actor']['login'] == 'new-login28' and user.role == 'HR_ADMIN' and user.scope == {'all': True}
    assert session.get(SessionToken, current.id) and session.get(SessionToken, other.id) is None
    assert auth.ph.verify(user.password_hash, body['new_password'])


@pytest.mark.parametrize('role', ['HR_ADMIN', 'HR_SPECIALIST', 'MANAGER', 'EMPLOYEE', 'PLATFORM_ADMIN'])
def test_only_head_can_delete_hr_accounts(session, hr, role):
    denied(403, accounts.delete_account, 'anything', Request({'type':'http','headers':[]}), session,
           Actor(hr.id, 'Actor', role, {'all': True}))


def test_scoped_hr_deletion_revokes_access_not_employee_history_and_cannot_be_reenabled_by_generic_admin(session, hr):
    issuer, actor = head(session)
    invitation, payload, _ = issued(session, actor)
    user, binding, _ = accounts.redeem_invitation(session, launch(payload)); session.commit()
    raw, token = session_for(session, user)
    employee = profile(session, hr); history = deepcopy(employee.data)
    pending = save_entity(session, 'employee_invite', {'issued_by': user.id}, owner_id=user.id, parent_id=employee.id, status='ISSUED')
    reset = save_entity(session, 'password_reset', {}, parent_id=user.id, status='ISSUED')
    callback = save_entity(session, 'callback_action', {'recipient_kind':'USER','recipient_id':user.id})
    outbound = Outbox(recipient_kind='USER',recipient_id=user.id,text='Pending',unique_key='delete-hr28',data={}); session.add(outbound);session.commit()
    request = Request({'type':'http','headers':[(b'if-match',admin.user_etag(user).encode())]})
    assert accounts.delete_account(user.id, request, session, actor).status_code == 200
    assert user.enabled is False and user.role == 'HR_SPECIALIST' and user.scope == issuer.scope
    assert {binding.status,invitation.status,pending.status,reset.status,callback.status} == {'REVOKED'}
    assert session.get(SessionToken, token.id) is None and auth.session_user(cookie_request(raw),session) == (None,None)
    assert tx.valid_outbox(session,outbound)[1] == 'USER_DISABLED'
    assert employee.data == history and accounts.accounts(session,actor)['items'] == []
    assert all(u['id'] != user.id for u in auth.users(hr,session)['items'])
    denied(404,admin.update_user,user.id,{'enabled':True,'reason':'Try restoring'},request,session,hr)
    denied(409,accounts.redeem_invitation,session,launch(payload))


def test_hr_delete_scope_current_role_etag_and_setup_self_guards(session,hr):
    issuer, actor = head(session); invitation,payload,_ = issued(session,actor)
    user,_,_ = accounts.redeem_invitation(session,launch(payload));session.commit()
    request = Request({'type':'http','headers':[(b'if-match',admin.user_etag(user).encode())]})
    denied(428,accounts.delete_account,user.id,Request({'type':'http','headers':[]}),session,actor)
    user.name='Updated';session.commit();denied(412,accounts.delete_account,user.id,request,session,actor)
    issuer.scope={'site_ids':['other']};session.commit()
    denied(404,accounts.delete_account,user.id,request,session,actor)
    issuer.scope=actor.scope;session.commit()
    denied(409,accounts.delete_account,issuer.id,request,session,actor)
    denied(409,accounts.delete_account,auth.SETUP_USER_ID,request,session,actor)
    denied(404,accounts.delete_account,hr.id,request,session,actor)
    issuer.role='HR_ADMIN';session.commit();denied(403,accounts.delete_account,user.id,request,session,actor)


def test_explicit_org_manager_role_preserves_max_episode_scope_and_answers_then_demotes(session,hr):
    employee=profile(session,hr); old=deepcopy(employee.data)
    user=User(id='role28',name='Employee',email='role28@example.test',role='EMPLOYEE',scope={},employee_id=employee.id,password_hash='unused',enabled=True)
    session.add(user);session.flush()
    binding=save_entity(session,'max_binding',{'max_user_id':'72801','episode_id':employee.data['episode_id']},parent_id=employee.id)
    d.update(employee,user_id=user.id,binding_id=binding.id);session.commit()
    _, token=session_for(session,user)
    response=org.change_access_role(employee.id,org.AccessRole(role='MANAGER'),req(employee),session,hr)
    assert json.loads(response.body)['access_role']=='MANAGER' and user.scope=={} and user.enabled
    assert session.get(SessionToken,token.id) is None
    assert employee.data['episode_id']==old['episode_id'] and employee.data['binding_id']==binding.id
    assert employee.data.get('manager_id')==old.get('manager_id') and binding.status=='ACTIVE'
    assert mini._resolve_identity(session,launch('', '72801'))[0].role=='MANAGER'
    org.change_access_role(employee.id,org.AccessRole(role='EMPLOYEE'),req(employee),session,hr)
    assert mini._resolve_identity(session,launch('', '72801'))[0].role=='EMPLOYEE'
    org.change_access_role(employee.id,org.AccessRole(role='MANAGER'),req(employee),session,hr)
    d.update(employee,miniapp_role_access={'role':'MANAGER','user_id':user.id,'episode_id':'old'})
    denied(403,mini._resolve_identity,session,launch('', '72801'))


def test_org_role_can_be_prepared_before_max_but_never_changes_hr_or_reenables_account(session,hr):
    employee=profile(session,hr)
    org.change_access_role(employee.id,org.AccessRole(role='MANAGER'),req(employee),session,hr)
    user=session.get(User,employee.data['user_id'])
    assert user.role=='MANAGER' and user.scope=={} and user.employee_id==employee.id
    user.enabled=False;session.commit()
    org.change_access_role(employee.id,org.AccessRole(role='EMPLOYEE'),req(employee),session,hr)
    assert user.enabled is False
    user.role='HR_SPECIALIST';session.commit()
    denied(409,org.change_access_role,employee.id,org.AccessRole(role='MANAGER'),req(employee),session,hr)


def test_org_role_requires_fresh_scoped_hr_etag_and_live_employee(session,hr):
    employee=profile(session,hr); stale=req(employee)
    d.update(employee,name='Updated');session.commit()
    denied(412,org.change_access_role,employee.id,org.AccessRole(role='MANAGER'),stale,session,hr)
    for role in ['EMPLOYEE','MANAGER','PLATFORM_ADMIN']:
        denied(403,org.change_access_role,employee.id,org.AccessRole(role='MANAGER'),req(employee),session,Actor(hr.id,'Actor',role,{'all':True}))
    owner=session.get(User,hr.id);owner.scope={'site_ids':['other']};session.commit()
    denied(404,org.change_access_role,employee.id,org.AccessRole(role='MANAGER'),req(employee),session,hr)
    owner.scope={'all':True};d.update(employee,enabled=False);session.commit()
    denied(409,org.change_access_role,employee.id,org.AccessRole(role='MANAGER'),req(employee),session,hr)


def test_explicit_demotion_keeps_verified_principal_max_identity_without_granting_arbitrary_employee_principals(session,hr):
    employee=profile(session,hr)
    user=User(id='principal28',name='Manager',email='principal28@example.test',role='MANAGER',scope={},employee_id=employee.id,password_hash='unused',enabled=True)
    session.add(user);session.flush();d.update(employee,user_id=user.id)
    binding=save_entity(session,'principal_binding',{'channel':'MAX','max_user_id':'72802','confirmed_at':now().isoformat()},owner_id=user.id);session.commit()
    org.change_access_role(employee.id,org.AccessRole(role='EMPLOYEE'),req(employee),session,hr)
    assert mini._resolve_identity(session,launch('', '72802'))[0].id==user.id and binding.status=='ACTIVE'
    d.update(employee,miniapp_role_access={})
    denied(403,mini._resolve_identity,session,launch('', '72802'))
