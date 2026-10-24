"""Boundary tests on the real middleware, session store and command replay."""

import uuid
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select, func
from korsik.db import make_engine, get_db
from korsik.models import Base, User
from korsik.auth import ph
from korsik import main


def test_scoped_hr_cannot_trigger_or_inspect_global_worker_jobs():
    from korsik.auth import Actor
    from fastapi import HTTPException

    actor = Actor("local", "Local HR", "HR_ADMIN", {"site_ids": ["A"]})
    for action in (
        lambda: main.list_jobs(actor=actor),
        lambda: main.retry_job("opaque-job", actor=actor),
    ):
        with pytest.raises(HTTPException) as error:
            action()
        assert error.value.status_code == 403


@pytest.fixture
def api(monkeypatch):
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as db:
        db.add(
            User(
                id="admin",
                email="admin@test.local",
                name="Admin",
                role="HR_ADMIN",
                scope={"all": True},
                password_hash=ph.hash("Test-only-password-123"),
            )
        )
        db.commit()

    def dependency():
        with factory() as db:
            yield db

    main.app.dependency_overrides[get_db] = dependency
    monkeypatch.setattr(main, "SessionLocal", factory)
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: SimpleNamespace(
            testing=True,
            database_url="sqlite://",
            product_mode="CORE",
            generation_enabled=False,
        ),
    )
    with TestClient(main.app) as client:
        yield client, factory
    main.app.dependency_overrides.clear()
    engine.dispose()


def login(client):
    r = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@test.local", "password": "Test-only-password-123"},
    )
    assert r.status_code == 200, r.text
    return r.json()["csrf_token"]


def headers(csrf, key=None):
    return {"X-CSRF-Token": csrf, "Idempotency-Key": key or str(uuid.uuid4())}


def test_site_admin_cannot_pause_global_max_or_schedule_global_probe(api):
    client, factory = api
    csrf = login(client)
    target = client.get("/api/v1/integrations").json()["items"][0]
    with factory() as db:
        admin = db.get(User, "admin")
        admin.scope = {"site_ids": ["A"]}
        db.commit()
    endpoint = "/api/v1/integrations/" + target["id"]
    body = {"enabled": False, "reason": "Synthetic transport pause"}
    assert (
        client.patch(
            endpoint, json=body, headers={**headers(csrf), "If-Match": target["etag"]}
        ).status_code
        == 403
    )
    assert (
        client.post(endpoint + "/tests", json={}, headers=headers(csrf)).status_code
        == 403
    )
    assert client.get(endpoint).json()["enabled"] is True
    with factory() as db:
        admin = db.get(User, "admin")
        admin.role = "PLATFORM_ADMIN"
        db.commit()
    assert (
        client.patch(
            endpoint, json=body, headers={**headers(csrf), "If-Match": target["etag"]}
        ).status_code
        == 200
    )


def test_retention_policy_requires_real_explicit_approval_and_global_hr_scope(api):
    client, factory = api
    csrf = login(client)
    current = client.get("/api/v1/retention/knowledge-policy")
    assert current.status_code == 200 and current.json()["status"] == "UNCONFIGURED"
    body = {
        "enabled": True,
        "backup_retention_days": 30,
        "hold": False,
        "approval_reference": "Synthetic test policy, not company approval",
        "approval_confirmed": False,
        "reason": "Synthetic policy test",
    }

    def put(payload=body, tag=current.headers["etag"]):
        h = headers(csrf)
        if tag:
            h["If-Match"] = tag
        return client.put("/api/v1/retention/knowledge-policy", json=payload, headers=h)

    assert put().status_code == 422
    body["approval_confirmed"] = True
    assert put(tag=None).status_code == 428
    saved = put()
    assert saved.status_code == 200 and saved.json()["enabled"]
    assert saved.json()["automatic_business_deletion"] is False
    assert (
        client.get("/api/v1/retention/policies").json()["configured_business_retention"]
        is True
    )
    assert put().status_code == 412
    assert (
        put({**body, "backup_retention_days": True}, saved.headers["etag"]).status_code
        == 422
    )
    with factory() as db:
        actor = db.get(User, "admin")
        actor.scope = {"site_ids": ["A"]}
        db.commit()
    assert put(tag=saved.headers["etag"]).status_code == 403
    with factory() as db:
        actor = db.get(User, "admin")
        actor.role = "PLATFORM_ADMIN"
        actor.scope = {"all": True}
        db.commit()
    assert put(tag=saved.headers["etag"]).status_code == 403


def test_csrf_origin_and_password_secrecy(api):
    client, factory = api
    assert client.get("/api/v1/users").status_code == 401
    assert client.get("/api/v1/auth/config").json()["modes"] == ["LOCAL"]
    csrf = login(client)
    body = {
        "email": "manager@test.local",
        "name": "Manager",
        "role": "MANAGER",
        "scope": {},
        "password": "Manager-password-123",
    }
    assert (
        client.post(
            "/api/v1/users", json=body, headers={"Idempotency-Key": "abc"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/users",
            json=body,
            headers={**headers(csrf), "Origin": "https://wrong.example"},
        ).status_code
        == 403
    )
    r = client.post("/api/v1/users", json=body, headers=headers(csrf))
    assert r.status_code == 201, r.text
    assert (
        "password" not in r.text
        and "password_hash" not in client.get("/api/v1/users").text
    )


def test_idempotent_command_and_conflicting_payload(api):
    client, factory = api
    csrf = login(client)
    h = headers(csrf, "stable-command")
    body = {
        "email": "manager@test.local",
        "name": "Manager",
        "role": "MANAGER",
        "scope": {},
        "password": "Manager-password-123",
    }
    first = client.post("/api/v1/users", json=body, headers=h)
    second = client.post("/api/v1/users", json=body, headers=h)
    assert (
        first.status_code == 201
        and second.status_code == 201
        and first.json() == second.json()
    )
    assert second.headers["X-Idempotent-Replay"] == "true"
    assert (
        client.post(
            "/api/v1/users", json={**body, "name": "Changed"}, headers=h
        ).status_code
        == 409
    )
    with factory() as db:
        assert (
            db.scalar(
                select(func.count(User.id)).where(User.email == "manager@test.local")
            )
            == 1
        )


def test_disabled_account_loses_existing_session(api):
    client, factory = api
    login(client)
    with factory() as db:
        user = db.get(User, "admin")
        user.enabled = False
        db.commit()
    assert client.get("/api/v1/users").status_code == 401


def test_one_use_offline_password_reset_revokes_sessions(api):
    client, factory = api
    csrf = login(client)
    r = client.post(
        "/api/v1/users/admin/password-reset",
        json={"reason": "Личность подтверждена"},
        headers=headers(csrf),
    )
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    reset = client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": token, "new_password": "New-password-test-123"},
    )
    assert reset.status_code == 200, reset.text
    assert client.get("/api/v1/auth/session").status_code == 401
    assert (
        client.post(
            "/api/v1/auth/password-reset/confirm",
            json={"token": token, "new_password": "Other-password-test-123"},
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/v1/auth/login",
            json={"email": "admin@test.local", "password": "New-password-test-123"},
        ).status_code
        == 200
    )
