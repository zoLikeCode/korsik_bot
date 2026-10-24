"""Signed one-use HR invites do not delegate admin rights or broaden scope."""
import hmac,json,uuid
from datetime import timedelta
from urllib.parse import parse_qs,urlparse,urlencode,quote
from types import SimpleNamespace
import pytest
from fastapi import HTTPException,FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from korsik import hr_accounts as accounts, max_miniapp as mini, domain as d, organization as org, auth
from korsik.auth import Actor,user_actor
from korsik.common import save_entity,require_role,etag
from korsik.db import now,get_db
from korsik.models import Entity,User,SessionToken
from test_domain import session as session,hr as hr,profile,req

TOKEN='SYNTHETIC-HR-TOKEN-DO-NOT-DEPLOY'


def head(db,scope=None):
    user=User(id='head27',name='Руководитель подбора',email='head27@example.test',role='HEAD_TALENT',scope=scope or {'site_ids':['GrPP']},password_hash='unused',enabled=True)
    db.add(user);db.commit();return user,user_actor(user)


def issued(db,actor,email='new-hr@example.test',**extra):
    result=accounts.issue_invitation(accounts.HRInvitation(name='Новый HR',email=email,**extra),db,actor)
    payload=parse_qs(urlparse(result['invite_url']).query)['startapp'][0]
    return db.get(Entity,result['id']),payload,result


def launch(payload,uid='70901'):
    return mini.VerifiedLaunch(uid,int(now().timestamp()),str(uuid.uuid4()),'synthetic',payload)


def denied(code,fn,*args,**kwargs):
    with pytest.raises(HTTPException) as caught:fn(*args,**kwargs)
    assert caught.value.status_code==code


def signed(payload,uid=70901,**forged):
    params={'auth_date':str(int(now().timestamp())),'query_id':str(uuid.uuid4()),'start_param':payload,
        'user':json.dumps({'id':uid,**forged},separators=(',',':'))}
    material='\n'.join(f'{k}={params[k]}' for k in sorted(params))
    params['hash']=hmac.digest(hmac.digest(b'WebAppData',TOKEN.encode(),'sha256'),material.encode(),'sha256').hex()
    return urlencode(params,quote_via=quote)


def test_head_can_create_scoped_staff_invite_but_not_generic_admin_or_platform(session,hr):
    user,actor=head(session)
    invitation,payload,response=issued(session,actor)
    assert response['invite_url'].startswith('https://max.ru/') and '?startapp=hr_' in response['invite_url']
    assert invitation.data['role']=='HR_SPECIALIST' and invitation.data['scope']==user.scope
    assert invitation.data['token_hash']!=payload[3:]
    assert 'token_hash' not in accounts.invitations(session,actor)['items'][0]
    assert 'invite_url' not in accounts.invitations(session,actor)['items'][0]
    denied(403,require_role,actor,'HR_ADMIN')
    denied(403,require_role,actor,'PLATFORM_ADMIN')
    require_role(actor,'HR_SPECIALIST')
    denied(403,accounts.issue_invitation,accounts.HRInvitation(name='X',email='x@example.test',scope={'all':True}),session,actor)
    denied(403,accounts.issue_invitation,accounts.HRInvitation(name='X',email='x@example.test'),session,Actor('special','HR','HR_SPECIALIST',{'all':True}))


def test_redeem_fixed_specialist_scope_and_regular_reopen_does_not_rebind(session,hr):
    user,actor=head(session)
    invitation,payload,_=issued(session,actor)
    created,binding,employee=accounts.redeem_invitation(session,launch(payload))
    session.commit()
    assert employee is None and created.role=='HR_SPECIALIST' and created.scope==user.scope
    assert created.employee_id is None and binding.kind=='principal_binding' and binding.owner_id==created.id
    assert invitation.status=='CONSUMED'
    assert accounts.redeem_invitation(session,launch(payload)) is None
    denied(409,accounts.redeem_invitation,session,launch(payload,'70902'))
    resolved,_,_=mini._resolve_identity(session,launch(payload))
    assert resolved.id==created.id
    created.enabled=False;session.commit()
    denied(403,mini._resolve_identity,session,launch(payload))


@pytest.mark.parametrize('change',['ROLE','SCOPE','DISABLED','EXPIRED','REVOKED'])
def test_pending_invitation_rechecks_issuer_expiry_and_revocation(session,hr,change):
    user,actor=head(session)
    invitation,payload,_=issued(session,actor)
    if change=='ROLE':user.role='MANAGER'
    elif change=='SCOPE':user.scope={'site_ids':['other']}
    elif change=='DISABLED':user.enabled=False
    elif change=='EXPIRED':d.update(invitation,expires_at=(now()-timedelta(seconds=1)).isoformat())
    else:invitation.status='REVOKED'
    session.commit()
    denied(409 if change in {'EXPIRED','REVOKED'} else 403,accounts.redeem_invitation,session,launch(payload))
    assert not session.scalar(select(User).where(User.email=='new-hr@example.test'))
    assert invitation.status!='CONSUMED'


def test_invitation_collision_never_promotes_existing_max_or_existing_email(session,hr):
    user,actor=head(session)
    invitation,payload,_=issued(session,actor)
    employee=profile(session,hr)
    save_entity(session,'max_binding',{'max_user_id':'70901','episode_id':employee.data['episode_id']},parent_id=employee.id)
    denied(409,accounts.redeem_invitation,session,launch(payload))
    assert invitation.status=='ISSUED' and user.role=='HEAD_TALENT'
    denied(409,accounts.issue_invitation,accounts.HRInvitation(name='Again',email=user.email),session,actor)


def test_invitation_revoke_is_scope_and_etag_guarded(session,hr):
    user,actor=head(session)
    invitation,payload,_=issued(session,actor)
    foreign=Actor('foreign','Foreign','HEAD_TALENT',{'site_ids':['other']})
    denied(404,accounts.revoke_invitation,invitation.id,req(invitation),session,foreign)
    stale=req(invitation)
    d.update(invitation,name='Changed');session.commit()
    denied(412,accounts.revoke_invitation,invitation.id,stale,session,actor)
    assert accounts.revoke_invitation(invitation.id,req(invitation),session,actor)['status']=='REVOKED'
    denied(409,accounts.redeem_invitation,session,launch(payload))


def test_signed_miniapp_invite_exchange_creates_session_atomically_and_ignores_user_role_claims(session,hr,tmp_path,monkeypatch):
    user,actor=head(session)
    invitation,payload,_=issued(session,actor)
    secret=tmp_path/'token';secret.write_text(TOKEN)
    monkeypatch.setattr(mini,'get_settings',lambda:SimpleNamespace(public_url='https://test.local',secure_cookies=True,max_token_file=str(secret),session_ttl_seconds=3600))
    app=FastAPI();app.include_router(mini.router);app.include_router(auth.router)
    app.dependency_overrides[get_db]=lambda:session
    with TestClient(app,base_url='https://test.local') as client:
        raw=signed(payload,role='PLATFORM_ADMIN',scope={'all':True})
        response=client.post('/auth/max-miniapp',json={'init_data':raw})
        assert response.status_code==200,response.text
        assert response.json()['actor']['role']=='HR_SPECIALIST' and response.json()['actor']['scope']==user.scope
        assert invitation.status=='CONSUMED' and session.scalar(select(SessionToken)) is not None
        assert client.get('/auth/session').status_code==200
        binding=session.get(Entity,invitation.data['binding_id']);binding.status='REVOKED';session.commit()
        assert client.get('/auth/session').status_code==401
    # A tampered unsigned payload cannot redeem the new invitation.
    row,other,_=issued(session,actor,email='second@example.test')
    denied(401,mini.validate_init_data,signed(other).replace('hr_','hr_X',1),TOKEN)
    assert row.status=='ISSUED'


def test_head_has_hr_author_and_org_scope_but_small_group_suppression_is_preserved(session,hr):
    user,actor=head(session)
    site=session.get(Entity,'GrPP')
    result=org.create_unit(org.UnitCreate(title='Новый отдел',legal_entity_id=site.data['legal_entity_id'],site_id=site.id),session,actor)
    assert result.status_code==201
    employee=profile(session,actor)
    assert employee.data.get('assigned_hr_id') is None # legacy explicit-create API stays compatible
    assert d.list_employees(session,actor)['items'][0]['id']==employee.id
    denied(403,org.create_unit,org.UnitCreate(title='Global',legal_entity_id=site.data['legal_entity_id']),session,actor)
    denied(403,auth.create_user,auth.UserInput(email='admin@example.test',name='Admin',password='password123456',role='HR_ADMIN',scope={'all':True}),actor,session)
