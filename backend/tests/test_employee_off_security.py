"""OFF fences apply to current requests/delivery, with HR management retained."""
from datetime import timedelta
from uuid import uuid4
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request
from sqlalchemy import select

from korsik import auth, assets, domain as d, max_miniapp as mini, organization as org, survey_campaigns as sc, transport_api as tx
from korsik.auth import Actor
from korsik.common import actor_can_employee, employee_is_enabled
from korsik.db import get_db, now
from korsik.models import Entity, Outbox, SessionToken, User
from test_domain import session as session, hr as hr, profile, employee_actor
from test_survey_campaigns import published, campaign


def account(db, employee, role="EMPLOYEE"):
    user = User(id=str(uuid4()), email=f"off-{uuid4().hex}@test.local", name="OFF security QA", password_hash="synthetic", role=role, scope={"all": True} if role.startswith("HR_") else {}, employee_id=employee.id, enabled=True)
    db.add(user); db.flush()
    return user


def disable(db, employee, value=False):
    d.update(employee, enabled=value)
    db.commit()


def request(raw):
    return Request({"type": "http", "headers": [(b"cookie", f"korsik_session={raw}".encode())]})


def session_for(db, user):
    raw = uuid4().hex
    row = SessionToken(id=auth.hash_token(raw), user_id=user.id, csrf_hash="synthetic", expires_at=now() + timedelta(hours=1))
    db.add(row); db.commit()
    return raw, row


def test_existing_password_session_and_cached_actor_recheck_off(session, hr):
    employee = profile(session, hr)
    user = account(session, employee)
    raw, token = session_for(session, user)
    assert auth.session_user(request(raw), session)[0].id == user.id
    disable(session, employee)
    assert auth.session_user(request(raw), session) == (None, None)
    assert mini.miniapp_session_is_current(session, token, user) is False
    cached = request(raw); cached.state.actor = auth.user_actor(user)
    with pytest.raises(HTTPException) as caught:
        auth.get_actor(cached, session)
    assert caught.value.status_code == 401
    disable(session, employee, True)
    assert auth.session_user(request(raw), session)[0].id == user.id


def test_http_login_and_session_off_fence_but_hr_still_enters(session, hr, monkeypatch):
    employee = profile(session, hr)
    user = account(session, employee)
    manager = account(session, employee, "MANAGER")
    admin = account(session, employee, "HR_ADMIN")
    session.commit()
    monkeypatch.setattr(auth, "ph", SimpleNamespace(verify=lambda *_: True))
    app = FastAPI(); app.include_router(auth.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app) as client:
        assert client.post("/api/v1/auth/login", json={"email": user.email, "password": "synthetic"}).status_code == 200
        assert client.get("/api/v1/auth/session").status_code == 200
        disable(session, employee)
        assert client.get("/api/v1/auth/session").status_code == 401
        for blocked in (user, manager):
            assert client.post("/api/v1/auth/login", json={"email": blocked.email, "password": "synthetic"}).status_code == 401
        assert client.post("/api/v1/auth/login", json={"email": admin.email, "password": "synthetic"}).status_code == 200
        assert client.get("/api/v1/auth/session").status_code == 200
    assert actor_can_employee(session, auth.user_actor(admin), employee.id)


def test_bound_miniapp_identity_off_cannot_create_account_or_session(session, hr):
    employee = profile(session, hr)
    binding = d.save_entity(session, "max_binding", {"max_user_id": "900051", "episode_id": employee.data["episode_id"]}, parent_id=employee.id)
    d.update(employee, binding_id=binding.id, max_binding_status="VERIFIED")
    session.commit()
    assert mini._employee_for_binding(session, binding).id == employee.id
    disable(session, employee)
    before = list(session.scalars(select(User)))
    with pytest.raises(HTTPException) as caught:
        mini._resolve_identity(session, mini.VerifiedLaunch("900051", int(now().timestamp()), "synthetic", "synthetic"))
    assert caught.value.status_code == 403
    assert len(list(session.scalars(select(User)))) == len(before)
    assert binding.status == "ACTIVE"


def test_miniapp_principal_manager_linked_to_off_employee_rejected_hr_retained(session, hr):
    employee = profile(session, hr)
    manager = account(session, employee, "MANAGER")
    admin = account(session, employee, "HR_ADMIN")
    for index, user in enumerate((manager, admin)):
        d.save_entity(session, "principal_binding", {"channel": "MAX", "confirmed_at": now().isoformat(), "max_user_id": str(900060 + index)}, owner_id=user.id)
    disable(session, employee)
    with pytest.raises(HTTPException) as caught:
        mini._resolve_identity(session, mini.VerifiedLaunch("900060", int(now().timestamp()), "synthetic", "synthetic"))
    assert caught.value.status_code == 403
    resolved, _, _ = mini._resolve_identity(session, mini.VerifiedLaunch("900061", int(now().timestamp()), "synthetic", "synthetic"))
    assert resolved.id == admin.id


def test_off_transport_rejects_employee_and_linked_user_while_hr_notifications_allowed(session, hr):
    employee = profile(session, hr)
    manager = account(session, employee, "MANAGER")
    admin = account(session, employee, "HR_ADMIN")
    for index, user in enumerate((manager, admin)):
        d.save_entity(session, "principal_binding", {"max_user_id": str(900070 + index)}, owner_id=user.id)
    disable(session, employee)
    direct = Outbox(recipient_kind="EMPLOYEE", recipient_id=employee.id, data={})
    assert tx.valid_outbox(session, direct) == (None, "EMPLOYEE_DISABLED")
    manager_notice = Outbox(recipient_kind="USER", recipient_id=manager.id, data={})
    assert tx.valid_outbox(session, manager_notice) == (None, "EMPLOYEE_DISABLED")
    admin_notice = Outbox(recipient_kind="USER", recipient_id=admin.id, data={})
    assert tx.valid_outbox(session, admin_notice) == ({"user_id": 900071}, None)


def test_off_directory_and_private_assets_hidden_hr_card_access_retained(session, hr):
    employee = profile(session, hr)
    d.update(employee, employment_stage="ACTIVE")
    asset = d.save_entity(session, "asset", {"employee_id": employee.id, "episode_id": employee.data["episode_id"], "purpose": "PROFILE", "validated": True, "content_type": "image/png"}, status="READY", owner_id=employee.id)
    d.update(employee, profile_photo_asset_id=asset.id)
    assert org._current(employee)
    assert assets.can_asset(session, employee_actor(employee), asset)
    disable(session, employee)
    assert not org._current(employee)
    assert not assets.can_asset(session, employee_actor(employee), asset)
    assert org._photo_asset(session, hr, employee) is None
    assert actor_can_employee(session, hr, employee.id)
    import json
    off_detail = org.employee_detail(employee.id, session, hr)
    assert off_detail.status_code == 200
    assert json.loads(off_detail.body)["can_edit"] is False
    with pytest.raises(HTTPException) as caught:
        org.employee_detail(employee.id, session, employee_actor(employee))
    assert caught.value.status_code in {403, 404}
    outside = Actor("outside", "Other HR", "HR_ADMIN", {"site_ids": ["outside-site"]})
    with pytest.raises(HTTPException) as caught:
        org.employee_detail(employee.id, session, outside)
    assert caught.value.status_code == 404
    assert assets.can_asset(session, hr, asset)


def test_off_survey_discovery_and_delivery_revoked_without_destroying_run(session, hr):
    employee = profile(session, hr)
    _, version = published(session, hr)
    program, _ = campaign(session, hr, version, [employee])
    run = d.save_entity(session, "survey_run", {"employee_id": employee.id, "episode_id": employee.data["episode_id"], "campaign_id": program.id, "campaign_generation": program.data["generation"]})
    session.flush()
    assert sc._audience_matches(session, program.data["audience"], employee)
    disable(session, employee)
    assert not sc._audience_matches(session, program.data["audience"], employee)
    assert sc.delivery_allowed(session, run)[0] is False
    assert sc.my_surveys(session, employee_actor(employee)) == {"items": []}
    assert session.get(Entity, run.id) is not None


def test_missing_enabled_flag_stays_compatible_and_hr_management_not_restricted(session, hr):
    employee = profile(session, hr)
    assert "enabled" not in employee.data or employee.data["enabled"] is True
    assert employee_is_enabled(employee)
    assert auth.employee_account_enabled(session, account(session, employee))
    disable(session, employee)
    assert actor_can_employee(session, hr, employee.id)
    assert not actor_can_employee(session, employee_actor(employee), employee.id)


def test_transport_refreshes_preloaded_employee_after_off_commit(session, hr):
    from sqlalchemy import update
    employee = profile(session, hr)
    previous = {**employee.data}
    session.execute(update(Entity).where(Entity.id == employee.id).values(data={**previous, "enabled": False}).execution_options(synchronize_session=False))
    session.commit()
    assert employee.data.get("enabled", True) is True  # deliberately stale identity map
    outbound = Outbox(recipient_kind="EMPLOYEE", recipient_id=employee.id, data={})
    assert tx.valid_outbox(session, outbound) == (None, "EMPLOYEE_DISABLED")
