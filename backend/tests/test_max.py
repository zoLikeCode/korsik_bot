"""MAX protocol/state integration; network calls are deliberately absent."""

import hashlib
import io
from types import SimpleNamespace
from urllib.parse import urlparse, parse_qs
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from PIL import Image
from test_domain import session as session, hr as hr, profile, req
from korsik import domain as d, chat, max_events as mx, transport_api as tx, retrieval
from korsik.common import save_entity, queue_message
from korsik.db import new_id
from korsik.models import Entity, Outbox, TransportInbox, TransportDelivery, Job


@pytest.fixture
def secret_settings(tmp_path, monkeypatch):
    key = tmp_path / "session_key"
    key.write_text("test-secret-key-for-local-signature-only")
    value = SimpleNamespace(secret_key_file=str(key), blob_root=str(tmp_path / "blobs"))
    monkeypatch.setattr(mx, "get_settings", lambda: value)
    monkeypatch.setattr(tx, "get_settings", lambda: value)
    return value


def inbox(db, payload):
    event = TransportInbox(
        id=hashlib.sha256(new_id().encode()).hexdigest(),
        update_type=payload["update_type"],
        payload=payload,
        status="ENQUEUED",
    )
    db.add(event)
    db.flush()
    return event


def process(db, event):
    mx.process_max_update(db, SimpleNamespace(payload={"inbox_id": event.id}))
    db.commit()


def test_invitation_uses_configured_deployment_bot(session, hr, monkeypatch):
    from korsik.settings import Settings
    from pydantic import ValidationError

    settings = Settings(max_bot_url="https://max.ru/company_adapter_bot/")
    assert settings.max_bot_url == "https://max.ru/company_adapter_bot"
    for invalid in (
        "http://max.ru/bot",
        "https://example.org/bot",
        "https://max.ru/bot?start=other",
        "https://token@max.ru/bot",
    ):
        with pytest.raises(ValidationError):
            Settings(max_bot_url=invalid)
    monkeypatch.setattr(chat, "get_settings", lambda: settings)
    employee = profile(session, hr)
    invite = chat.issue_employee_invite(
        employee.id, chat.EmployeeInvitation(), hr, session
    )
    assert invite["url"].startswith("https://max.ru/company_adapter_bot?start=join_")


def bind(db, hr, employee, uid=12345):
    invitation = chat.issue_employee_invite(
        employee.id, chat.EmployeeInvitation(), hr, db
    )
    token = parse_qs(urlparse(invitation["url"]).query)["start"][0]
    event = inbox(
        db,
        {
            "update_type": "bot_started",
            "user": {"user_id": uid, "first_name": "MAX User", "is_bot": False},
            "payload": token,
            "chat_id": 100,
        },
    )
    process(db, event)
    binding = db.get(Entity, employee.data["pending_binding_id"])
    chat.approve_employee_binding(
        binding.id, chat.BindingApprove(), req(binding), hr, db
    )
    return binding, chat.conversation_for(db, employee)


def callback_event(db, uid, raw):
    return inbox(
        db,
        {
            "update_type": "message_callback",
            "callback": {"user": {"user_id": uid, "is_bot": False}, "payload": raw},
            "message": {
                "recipient": {"chat_type": "dialog", "chat_id": 100},
                "body": {"text": ""},
            },
        },
    )


def test_hr_invitation_automatically_verifies_then_same_identity_works(
    session, hr, secret_settings
):
    e = profile(session, hr)
    invitation = chat.issue_employee_invite(
        e.id, chat.EmployeeInvitation(), hr, session
    )
    payload = parse_qs(urlparse(invitation["url"]).query)["start"][0]
    event = inbox(
        session,
        {
            "update_type": "bot_started",
            "user": {"user_id": 9001, "first_name": "Display name"},
            "payload": payload,
        },
    )
    process(session, event)
    binding = session.get(Entity, e.data["pending_binding_id"])
    assert binding.status == "ACTIVE"
    assert binding.data["verified_method"] == "HR_ISSUED_INVITATION"
    assert e.data["max_binding_status"] == "VERIFIED"
    assert mx.current_employee(session, 9001).id == e.id
    assert all(
        "HR должен подтвердить" not in out.text
        for out in session.scalars(select(Outbox)).all()
    )
    before = len(d.entities(session, "max_binding"))
    process(session, event)
    assert len(d.entities(session, "max_binding")) == before
    assert mx.current_employee(session, 9001).id == e.id
    conv = chat.conversation_for(session, e)
    assert conv.data["employee_id"] == e.id
    assert binding.data["max_user_id"] == "9001"
    assert binding.status == "ACTIVE"


def test_signed_callback_typed_answer_and_duplicate_event(session, hr, secret_settings):
    e = profile(session, hr)
    binding, conv = bind(session, hr, e)
    j = d.entities(session, "journey", e.id)[0]
    step = next(
        s
        for s in d.get_entity(session, j.data["version_id"], "scenario_version").data[
            "graph"
        ]["steps"]
        if s.get("survey", {}).get("survey_key") == "SUR-D3"
    )
    run = d.open_survey(session, j, step, e)
    session.commit()
    out = session.scalar(
        select(Outbox).where(Outbox.unique_key.like(f"survey:{run.id}:q:%"))
    )
    keyboard = tx.signed_buttons(session, out)
    raw = keyboard[0][0]["payload"]
    event = callback_event(session, 12345, raw)
    process(session, event)
    assert run.data["current_question_key"] == "D3-Q2"
    assert run.data["answers"]["D3-Q1"]["value"] == "A"
    process(session, event)
    assert len(run.data["answers"]) == 1
    old_event = callback_event(session, 12345, raw)
    process(session, old_event)
    assert len(run.data["answers"]) == 1
    assert session.scalar(
        select(Outbox).where(Outbox.unique_key == f"event-rejected:{old_event.id}")
    )


def test_forged_other_identity_and_stale_episode_callback_rejected(
    session, hr, secret_settings
):
    e = profile(session, hr)
    binding, conv = bind(session, hr, e)
    out = queue_message(
        session,
        "EMPLOYEE",
        e.id,
        "Question",
        "test-signature",
        buttons=[{"text": "Open", "payload": {"action": "MY_STAGE"}}],
    )
    raw = tx.signed_buttons(session, out)[0][0]["payload"]
    with pytest.raises(HTTPException) as forged:
        mx.callback_payload(
            session, raw[:-1] + ("0" if raw[-1] != "0" else "1"), 12345, e
        )
    assert forged.value.status_code == 403
    with pytest.raises(HTTPException):
        mx.callback_payload(session, raw, 999, e)
    other = profile(session, hr, name="Other")
    bind(session, hr, other, uid=222)
    with pytest.raises(HTTPException):
        mx.callback_payload(session, raw, 222, other)
    episode = new_id()
    d.update(e, episode_id=episode)
    binding.data = {**binding.data, "episode_id": episode}
    with pytest.raises(HTTPException) as stale:
        mx.callback_payload(session, raw, 12345, e)
    assert stale.value.status_code == 409


def test_presend_human_current_binding_and_source_acl(
    session, hr, secret_settings, monkeypatch
):
    e = profile(session, hr)
    binding, conv = bind(session, hr, e)
    out = d.emit(session, e.id, "Scheduled message", unique_key="test-human")
    assert tx.valid_outbox(session, out)[0]
    d.update(conv, mode="HUMAN")
    assert tx.valid_outbox(session, out)[1] == "HUMAN_SUPPRESSION"
    d.update(conv, mode="BOT")
    binding.status = "REVOKED"
    assert tx.valid_outbox(session, out)[0] is None
    binding.status = "ACTIVE"
    answer = save_entity(
        session,
        "answer",
        {"employee_id": e.id, "citations": [{"revision_id": "rev"}]},
        status="COMPLETED",
        parent_id=conv.id,
    )
    aout = queue_message(
        session,
        "EMPLOYEE",
        e.id,
        "Source answer",
        "test-source",
        data={"conversation_id": conv.id, "answer_id": answer.id},
    )
    monkeypatch.setattr(retrieval, "answer_still_authorized", lambda db, c, a: False)
    assert tx.valid_outbox(session, aout)[1] == "SOURCE_REVOKED"


def test_claim_then_last_validate_revokes_before_send(session, hr, secret_settings):
    e = profile(session, hr)
    binding, conv = bind(session, hr, e)
    out = d.emit(session, e.id, "Safe message", unique_key="validate-test")
    session.commit()
    tx.prepare_deliveries(session)
    session.commit()
    delivery = session.scalar(
        select(TransportDelivery).where(TransportDelivery.outbox_id == out.id)
    )
    claim = tx.claim_delivery(delivery.id, session)
    assert (
        tx.validate_delivery_attempt(
            delivery.id, tx.DeliveryValidation(attempt_id=claim["attempt_id"]), session
        )["status"]
        == "VALID"
    )
    binding.status = "REVOKED"
    session.commit()
    with pytest.raises(HTTPException):
        tx.validate_delivery_attempt(
            delivery.id, tx.DeliveryValidation(attempt_id=claim["attempt_id"]), session
        )
    assert delivery.status == "SUPPRESSED"
    assert out.status == "SUPPRESSED"


def test_accepted_receipt_is_once_and_unknown_is_not_retried(
    session, hr, secret_settings
):
    e = profile(session, hr)
    binding, conv = bind(session, hr, e)
    out = d.emit(session, e.id, "Accepted one", "", unique_key="accepted-test")
    session.commit()
    tx.prepare_deliveries(session)
    session.commit()
    delivery = session.scalar(
        select(TransportDelivery).where(TransportDelivery.outbox_id == out.id)
    )
    claim = tx.claim_delivery(delivery.id, session)
    receipt = tx.DeliveryReceipt(
        attempt_id=claim["attempt_id"], status="ACCEPTED", platform_message_id="max-mid"
    )
    tx.delivery_receipt(delivery.id, receipt, session)
    tx.delivery_receipt(delivery.id, receipt, session)
    assert out.status == "ACCEPTED"
    assert out.data["accepted_at"]
    assert (
        len(list(session.scalars(select(Job).where(Job.kind == "OUTBOX_ACCEPTED"))))
        == 1
    )
    second = d.emit(session, e.id, "Unknown", "", unique_key="unknown-test")
    session.commit()
    tx.prepare_deliveries(session)
    session.commit()
    unknown = session.scalar(
        select(TransportDelivery).where(TransportDelivery.outbox_id == second.id)
    )
    claim = tx.claim_delivery(unknown.id, session)
    tx.delivery_receipt(
        unknown.id,
        tx.DeliveryReceipt(attempt_id=claim["attempt_id"], status="UNKNOWN"),
        session,
    )
    tx.delivery_receipt(
        unknown.id,
        tx.DeliveryReceipt(attempt_id=claim["attempt_id"], status="RETRY"),
        session,
    )
    assert unknown.status == "UNKNOWN"
    assert second.status == "UNKNOWN_DELIVERY"
    with pytest.raises(HTTPException):
        tx.claim_delivery(unknown.id, session)


def test_direct_unverified_notice_requires_same_trusted_event_sender(
    session, secret_settings
):
    event = inbox(session, {"update_type": "bot_started", "user": {"user_id": 555}})
    out = mx.direct_notice(session, 555, "Technical only", event.id)
    assert tx.valid_outbox(session, out)[0] == {"user_id": 555}
    out.recipient_id = "556"
    assert tx.valid_outbox(session, out)[1] == "UNVERIFIED_DIRECT_RECIPIENT"


def test_image_staging_rejects_symlinks_and_hash_mismatch(
    session, hr, secret_settings, tmp_path, monkeypatch
):
    e = profile(session, hr)
    conv = chat.conversation_for(session, e)
    inbound = tmp_path / "inbound"
    inbound.mkdir()
    monkeypatch.setenv("INBOUND_DIR", str(inbound))
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(buf, format="PNG")
    raw = buf.getvalue()
    sid = hashlib.sha256(raw).hexdigest()
    path = inbound / sid
    path.write_bytes(raw)
    assert (
        mx.import_staged_image(session, e, conv, {"id": sid, "sha256": "wrong"}) is None
    )
    asset_id = mx.import_staged_image(session, e, conv, {"id": sid, "sha256": sid})
    asset = session.get(Entity, asset_id)
    assert asset.owner_id == e.id and asset.data["validated"]
    path.unlink()
    outside = tmp_path / "outside.png"
    outside.write_bytes(raw)
    path.symlink_to(outside)
    assert mx.import_staged_image(session, e, conv, {"id": sid, "sha256": sid}) is None


def test_offboard_and_rehire_separate_history_and_require_new_binding(
    session, hr, secret_settings
):
    from datetime import timedelta
    from korsik.models import User, SessionToken
    from korsik.db import now

    e = profile(session, hr)
    binding, old_conv = bind(session, hr, e, 75501)
    old_episode = session.get(Entity, e.data["episode_id"])
    old_journey = d.entities(session, "journey", e.id)[0]
    d.update(old_journey, answers={"historical": {"value": "keep"}})
    user = User(
        id="offboard-user",
        email="offboard@test.local",
        name="Test Employee",
        role="EMPLOYEE",
        scope={},
        employee_id=e.id,
        password_hash="test",
        enabled=True,
    )
    session.add(user)
    session.flush()
    session.add(
        SessionToken(
            id="offboard-session",
            user_id=user.id,
            csrf_hash="hash",
            expires_at=now() + timedelta(days=1),
        )
    )
    invitation = save_entity(
        session,
        "employee_invite",
        {"episode_id": old_episode.id},
        status="ISSUED",
        parent_id=e.id,
    )
    out = d.emit(
        session, e.id, "Pending old-episode message", unique_key="offboard-old-pending"
    )
    session.commit()
    preview = d.employee_change_preview(
        e.id,
        {
            "action": "OFFBOARD",
            "expected_episode_id": old_episode.id,
            "expected_state_version": old_episode.version,
            "reason": "Увольнение подтверждено",
        },
        session,
        hr,
    )
    d.employee_apply_change(
        e.id,
        {
            "preview_id": preview["preview_id"],
            "preview_hash": preview["preview_hash"],
            "reason": "Увольнение подтверждено",
        },
        session,
        hr,
    )
    assert old_episode.status == "ENDED" and old_episode.data["ended_at"]
    assert binding.status == "REVOKED" and invitation.status == "REVOKED"
    assert old_conv.status == "CLOSED" and out.status == "SUPPRESSED"
    assert (
        user.enabled is False and session.get(SessionToken, "offboard-session") is None
    )
    assert old_journey.data["answers"]["historical"]["value"] == "keep"
    assert mx.current_employee(session, 75501) is None
    next_date = (now().date() + timedelta(days=10)).isoformat()
    preview = d.employee_change_preview(
        e.id,
        {
            "action": "REHIRE",
            "new_start_date": next_date,
            "new_site_id": "GTP",
            "expected_episode_id": old_episode.id,
            "expected_state_version": old_episode.version,
            "reason": "Повторный найм подтверждён",
        },
        session,
        hr,
    )
    d.employee_rehire(
        e.id,
        {
            "preview_id": preview["preview_id"],
            "preview_hash": preview["preview_hash"],
            "reason": "Повторный найм подтверждён",
        },
        session,
        hr,
    )
    assert e.data["episode_id"] != old_episode.id
    assert (
        e.data["max_binding_status"] == "UNBOUND"
        and e.data["employment_stage"] == "PREBOARDING"
    )
    current = session.get(Entity, e.data["episode_id"])
    assert current.data["previous_episode_id"] == old_episode.id
    assert old_episode.data["start_date"] != next_date
    new_journey = next(
        j
        for j in d.entities(session, "journey", e.id)
        if j.data["episode_id"] == current.id
    )
    assert new_journey.data["answers"] == {} and old_journey.data["answers"]
    assert chat.conversation_for(session, e).id != old_conv.id
    assert mx.current_employee(session, 75501) is None
    assert not user.enabled


def test_profile_publication_requires_preview_and_current_recipient_grant(
    session, hr, secret_settings
):
    from korsik.models import User

    e = profile(session, hr)
    manager = User(
        id="profile-manager",
        email="manager@test.local",
        name="Manager",
        role="MANAGER",
        scope={},
        password_hash="test",
        enabled=True,
    )
    session.add(manager)
    session.flush()
    d.update(
        e,
        manager_id=manager.id,
        profile_bio="Работаю с полимерными материалами.",
        profile_publication_status="PENDING_HR_APPROVAL",
    )
    photo = save_entity(
        session,
        "asset",
        {
            "content_type": "image/png",
            "validated": True,
            "sha256": "synthetic-photo-sha",
        },
        status="READY",
        owner_id=e.id,
    )
    d.update(e, profile_photo_asset_id=photo.id)
    save_entity(
        session,
        "principal_binding",
        {"max_user_id": "75502"},
        status="ACTIVE",
        owner_id=manager.id,
    )
    session.commit()
    preview = d.profile_publication_preview(
        e.id,
        {
            "recipient_user_ids": [manager.id],
            "reason": "Представить сотрудника руководителю",
        },
        session,
        hr,
    )
    assert preview["sends_messages"] is False
    assert not any(
        o.data.get("notification_type") == "PROFILE_PUBLICATION"
        for o in session.scalars(select(Outbox))
    )
    approved = d.profile_publication_approve(
        e.id,
        {
            "preview_id": preview["preview_id"],
            "preview_hash": preview["preview_hash"],
            "reason": "Разрешение HR на указанную аудиторию",
        },
        session,
        hr,
    )
    repeated = d.profile_publication_approve(
        e.id,
        {
            "preview_id": preview["preview_id"],
            "preview_hash": preview["preview_hash"],
            "reason": "Повтор",
        },
        session,
        hr,
    )
    assert repeated["id"] == approved["id"] and len(approved["deliveries"]) == 1
    out = session.get(Outbox, approved["deliveries"][0]["outbox_id"])
    assert out.attachments == [{"asset_id": photo.id}]
    assert tx.valid_outbox(session, out)[0] == {"user_id": 75502}
    d.update(e, manager_id="new-manager")
    assert tx.valid_outbox(session, out)[1] == "ASSIGNMENT_REVOKED"
    d.update(e, manager_id=manager.id)
    d.profile_publication_reject(
        e.id, {"reason": "Отозвано разрешение на распространение"}, session, hr
    )
    assert out.status == "SUPPRESSED"
    assert session.get(Entity, approved["id"]).status == "REVOKED"
    assert tx.valid_outbox(session, out)[1] == "PROFILE_PUBLICATION_REVOKED"


def test_user_notification_rechecks_role_and_cohort_assignment(
    session, hr, secret_settings
):
    from korsik.models import User

    e = profile(session, hr)
    manager = User(
        id="digest-manager",
        email="digest-manager@test.local",
        name="Manager",
        role="MANAGER",
        scope={},
        password_hash="test",
        enabled=True,
    )
    session.add(manager)
    session.flush()
    d.update(e, manager_id=manager.id)
    save_entity(
        session,
        "principal_binding",
        {"max_user_id": "75503"},
        status="ACTIVE",
        owner_id=manager.id,
    )
    out = d.notify_user(
        session,
        manager.id,
        "Cohort digest",
        "test-cohort-grant",
        data={"required_scope": {"all": True}, "employee_ids": [e.id]},
    )
    assert tx.valid_outbox(session, out)[0]
    d.update(e, manager_id="reassigned")
    assert tx.valid_outbox(session, out)[1] == "ASSIGNMENT_REVOKED"
    manager.role = "EMPLOYEE"
    assert tx.valid_outbox(session, out)[1] == "ROLE_REVOKED"


def test_legacy_food_and_hr_commands_are_explicit_service_actions(
    session, hr, secret_settings
):
    e = profile(session, hr)
    binding, conv = bind(session, hr, e, 75504)
    for text in ("/Food", "/HR"):
        event = inbox(
            session,
            {
                "update_type": "message_created",
                "message": {
                    "sender": {"user_id": 75504},
                    "recipient": {"chat_type": "dialog", "chat_id": 100},
                    "body": {"text": text},
                },
            },
        )
        process(session, event)
    turns = d.entities(session, "conversation_turn", conv.id)
    assert len(turns) == 1 and "пообедать" in turns[0].data["input"]["text"]
    assert conv.data["mode"] == "WAITING_HR"
    mx.service_menu(session, e, conv, "test-menu")
    out = next(
        o for o in session.scalars(select(Outbox)) if o.unique_key == "menu:test-menu"
    )
    actions = {b["payload"].get("action") for b in out.callback_buttons}
    assert {"HELP", "FOOD", "MY_STAGE", "CONTINUE_SURVEY"}.issubset(actions)
    assert not actions.intersection({"CONTACT_HR", "CONTACT_RECRUITER", "HANDOFF"})


def test_verified_recruiter_contact_is_explicit_and_revocable(
    session, hr, secret_settings
):
    from starlette.requests import Request

    e = profile(session, hr, assigned_hr_id=hr.id)
    binding, conv = bind(session, hr, e, 75505)
    assert not d.verified_hr_contact(session, e)["available"]
    empty = Request({"type": "http", "headers": []})
    for url in (
        "https://evil.invalid/profile",
        "https://max.ru:bad-port/u/hr",
        "https://user:password@max.ru/u/hr",
    ):
        with pytest.raises(HTTPException):
            d.hr_contact_put(
                hr.id,
                {"url": url, "verified": True, "reason": "Known channel"},
                empty,
                session,
                hr,
            )
    result = d.hr_contact_put(
        hr.id,
        {
            "url": "https://max.ru/u/synthetic-recruiter",
            "verified": True,
            "reason": "Личность и ссылка подтверждены по известному каналу",
        },
        empty,
        session,
        hr,
    )
    contact = session.get(Entity, __import__("json").loads(result.body)["id"])
    mx.handle_callback(
        session, e, conv, {"action": "CONTACT_RECRUITER"}, "contact-test"
    )
    out = next(
        o
        for o in session.scalars(select(Outbox))
        if o.unique_key == "recruiter-contact:contact-test"
    )
    assert tx.valid_outbox(session, out)[0]
    assert out.callback_buttons[0]["url"] == "https://max.ru/u/synthetic-recruiter"
    d.hr_contact_revoke(hr.id, req(contact), session, hr)
    assert tx.valid_outbox(session, out)[1] == "RECRUITER_CONTACT_REVOKED"
    mx.handle_callback(
        session, e, conv, {"action": "CONTACT_RECRUITER"}, "contact-unavailable"
    )
    assert conv.data["mode"] == "WAITING_HR"


def test_presend_rejects_old_episode_context_even_with_new_current_binding(
    session, hr, secret_settings
):
    employee = profile(session, hr)
    binding, old_conversation = bind(session, hr, employee)
    old_episode = employee.data["episode_id"]
    old_outbox = queue_message(
        session,
        "EMPLOYEE",
        employee.id,
        "Former private reply",
        "old-private-reply",
        data={"conversation_id": old_conversation.id},
    )
    episode_pinned = d.emit(
        session, employee.id, "Former domain message", unique_key="old-domain-message"
    )
    new_episode = save_entity(
        session,
        "employment_episode",
        {"employee_id": employee.id, "stage": "ACTIVE"},
        parent_id=employee.id,
    )
    old_conversation.status = "CLOSED"
    d.update(employee, episode_id=new_episode.id, employment_stage="ACTIVE")
    d.update(binding, episode_id=new_episode.id)
    current = chat.conversation_for(session, employee)
    assert employee.data["episode_id"] != old_episode
    assert tx.valid_outbox(session, old_outbox)[1] == "STALE_CONVERSATION"
    assert tx.valid_outbox(session, episode_pinned)[1] == "STALE_EPISODE"
    out = queue_message(
        session,
        "EMPLOYEE",
        employee.id,
        "Current reply",
        "current-private-reply",
        data={"conversation_id": current.id},
    )
    assert tx.valid_outbox(session, out)[0]
    with pytest.raises(HTTPException) as denied:
        chat.handoff(session, hr, old_conversation, "Do not reopen old private episode")
    assert denied.value.status_code == 409


def test_delegated_task_reminder_rechecks_exact_assignment(
    session, hr, secret_settings
):
    from korsik.models import User
    from datetime import timedelta
    from korsik.db import now

    managers = [
        User(
            id="delegate-manager-" + str(i),
            email=f"delegate{i}@invalid.test",
            name=f"Manager {i}",
            role="MANAGER",
            scope={},
            password_hash="no-login",
            enabled=True,
        )
        for i in (1, 2)
    ]
    session.add_all(managers)
    session.commit()
    employee = profile(session, hr, manager_id=managers[0].id)
    response = d.create_manager_task(
        {
            "employee_id": employee.id,
            "assigned_manager_id": managers[1].id,
            "due_at": (now() - timedelta(minutes=1)).isoformat(),
        },
        session,
        hr,
    )
    import json

    task = session.get(Entity, json.loads(response.body)["id"])
    save_entity(
        session,
        "principal_binding",
        {"max_user_id": 4488},
        status="ACTIVE",
        owner_id=managers[1].id,
    )
    d.scan_deadlines(session)
    out = next(
        o
        for o in session.scalars(select(Outbox))
        if o.data.get("manager_task_id") == task.id
    )
    assert out.recipient_id == managers[1].id and tx.valid_outbox(session, out)[0]
    d.update_task(
        task.id,
        {
            "assigned_manager_id": managers[0].id,
            "reason": "Substitute assignment ended",
        },
        req(task),
        session,
        hr,
    )
    assert tx.valid_outbox(session, out)[1] == "TASK_ASSIGNMENT_REVOKED"


def source_question(db, employee, answer_binding):
    journey = d.entities(db, "journey", employee.id)[0]
    version = d.get_entity(db, journey.data["version_id"], "scenario_version")
    step = next(
        s
        for s in version.data["graph"]["steps"]
        if s.get("question", {}).get("answer_binding") == answer_binding
    )
    return save_entity(
        db,
        "scenario_question",
        {
            "employee_id": employee.id,
            "episode_id": employee.data["episode_id"],
            "journey_id": journey.id,
            "step_key": step["step_key"],
            "scenario_version_id": version.id,
            "question": step["question"],
        },
        status="OPEN",
        parent_id=journey.id,
    )


def signed_domain_callback(db, employee, payload):
    out = queue_message(
        db,
        "EMPLOYEE",
        employee.id,
        "Synthetic callback",
        "domain-regression:" + new_id(),
        buttons=[{"text": "Continue", "payload": payload}],
    )
    return tx.signed_buttons(db, out)[0][0]["payload"]


def assert_no_rag_dispatch(db, conversation):
    assert not d.entities(db, "conversation_turn", conversation.id)
    assert not d.entities(db, "answer", conversation.id)
    assert not list(db.scalars(select(Job).where(Job.kind == "ANSWER")))


def test_scenario_answer_definition_and_repeated_signed_callback_never_dispatch_rag(
    session, hr, secret_settings
):
    employee = profile(session, hr)
    _, conv = bind(session, hr, employee)
    question = source_question(session, employee, "ADDRESS_FORM")
    raw = signed_domain_callback(
        session,
        employee,
        {
            "action": "SCENARIO_ANSWER",
            "scenario_question_id": question.id,
            "question_id": question.data["question"]["question_key"],
            "value": "TY",
        },
    )
    event = callback_event(session, 12345, raw)
    process(session, event)
    assert question.status == "COMPLETED"
    assert question.data["answer"]["value"] == "TY"
    assert employee.data["address_form"] == "TY"
    assert isinstance(question.data["question"], dict)
    assert_no_rag_dispatch(session, conv)
    process(session, event)
    process(session, callback_event(session, 12345, raw))
    assert_no_rag_dispatch(session, conv)


def test_survey_domain_responses_do_not_become_chat_questions(
    session, hr, secret_settings
):
    employee = profile(session, hr)
    _, conv = bind(session, hr, employee)
    journey = d.entities(session, "journey", employee.id)[0]
    version = d.get_entity(session, journey.data["version_id"], "scenario_version")
    step = next(
        s
        for s in version.data["graph"]["steps"]
        if s.get("survey", {}).get("survey_key") == "SUR-D3"
    )
    run = d.open_survey(session, journey, step, employee)
    payloads = [
        {
            "action": "SURVEY_ANSWER",
            "survey_run_id": run.id,
            "question_id": "D3-Q1",
            "value": "A",
            "state_revision": 1,
        },
        {"action": "SURVEY_PAUSE", "survey_run_id": run.id},
        {"action": "SURVEY_RESUME", "survey_run_id": run.id},
    ]
    for payload in payloads:
        raw = signed_domain_callback(session, employee, payload)
        process(session, callback_event(session, 12345, raw))
        assert_no_rag_dispatch(session, conv)
    assert run.status == "IN_PROGRESS"
    assert run.data["current_question_key"] == "D3-Q2"
    assert run.data["answers"]["D3-Q1"]["value"] == "A"


def test_capture_to_question_dispatches_human_text_once_across_callback_events(
    session, hr, secret_settings
):
    employee = profile(session, hr)
    _, conv = bind(session, hr, employee)
    question = source_question(session, employee, "PREFERRED_NAME")
    cap = d.open_capture(
        session,
        mx.actor_for(employee),
        {
            "scenario_question_id": question.id,
            "question_id": question.data["question"]["question_key"],
        },
    )
    text = "Какова миссия компании ПОЛИПЛАСТИК?"
    d.capture_draft(session, mx.actor_for(employee), cap.id, {"text": text})
    raw = signed_domain_callback(
        session,
        employee,
        {
            "action": "CAPTURE_TO_QUESTION",
            "capture_id": cap.id,
        },
    )
    event = callback_event(session, 12345, raw)
    process(session, event)
    process(session, event)  # platform event redelivery
    process(session, callback_event(session, 12345, raw))  # second button click
    turns = d.entities(session, "conversation_turn", conv.id)
    answers = d.entities(session, "answer", conv.id)
    jobs = list(session.scalars(select(Job).where(Job.kind == "ANSWER")))
    assert cap.status == "CANCELLED"
    assert len(turns) == len(answers) == len(jobs) == 1
    assert turns[0].data["input"] == {"kind": "TEXT", "text": text}
    assert answers[0].data["question"] == text
    assert question.status == "OPEN"  # It was a question, not a saved name.


@pytest.mark.parametrize("draft", [None, "", "  ", {"type": "TEXT"}, ["definition"], 7])
def test_capture_conversion_rejects_missing_or_non_string_draft_without_rag(
    session, hr, secret_settings, draft
):
    employee = profile(session, hr)
    _, conv = bind(session, hr, employee)
    question = source_question(session, employee, "PREFERRED_NAME")
    cap = d.open_capture(
        session,
        mx.actor_for(employee),
        {
            "scenario_question_id": question.id,
            "question_id": question.data["question"]["question_key"],
        },
    )
    # Explicit malformed/stale persisted draft fixture; ordinary capture_draft
    # already forbids these values. The adapter must not stringify metadata.
    cap.data = {**cap.data, "draft": draft}
    raw = signed_domain_callback(
        session,
        employee,
        {
            "action": "CAPTURE_TO_QUESTION",
            "capture_id": cap.id,
        },
    )
    event = callback_event(session, 12345, raw)
    process(session, event)
    assert cap.status == "CANCELLED"
    assert_no_rag_dispatch(session, conv)
    assert not session.scalar(
        select(Outbox).where(Outbox.unique_key == "event-rejected:" + event.id)
    )


def test_plain_max_text_from_verified_identity_still_dispatches_one_answer(
    session, hr, secret_settings
):
    employee = profile(session, hr)
    _, conv = bind(session, hr, employee)
    text = "Какова миссия компании ПОЛИПЛАСТИК?"
    event = inbox(
        session,
        {
            "update_type": "message_created",
            "message": {
                "sender": {"user_id": 12345, "is_bot": False},
                "recipient": {"chat_type": "dialog", "chat_id": 100},
                "body": {"text": text},
            },
        },
    )
    process(session, event)
    process(session, event)
    turns = d.entities(session, "conversation_turn", conv.id)
    answers = d.entities(session, "answer", conv.id)
    assert len(turns) == len(answers) == 1
    assert turns[0].data["input"] == {"kind": "TEXT", "text": text}
    assert answers[0].data["question"] == text
    assert len(list(session.scalars(select(Job).where(Job.kind == "ANSWER")))) == 1


def test_photo_capture_attachment_identifier_is_not_a_human_question(
    session, hr, secret_settings
):
    employee = profile(session, hr)
    _, conv = bind(session, hr, employee)
    journey = d.entities(session, "journey", employee.id)[0]
    version = d.get_entity(session, journey.data["version_id"], "scenario_version")
    step = next(
        s
        for s in version.data["graph"]["steps"]
        if s.get("question", {}).get("question_key") == "PROFILE-PHOTO"
    )
    question = save_entity(
        session,
        "scenario_question",
        {
            "employee_id": employee.id,
            "episode_id": employee.data["episode_id"],
            "journey_id": journey.id,
            "step_key": step["step_key"],
            "scenario_version_id": version.id,
            "question": step["question"],
        },
        status="OPEN",
        parent_id=journey.id,
    )
    cap = d.open_capture(
        session,
        mx.actor_for(employee),
        {
            "scenario_question_id": question.id,
            "question_id": question.data["question"]["question_key"],
        },
    )
    # A staged upload ID is a string, but is not text written by the employee.
    d.capture_draft(
        session, mx.actor_for(employee), cap.id, {"attachment_id": new_id()}
    )
    assert cap.data["draft_type"] == "ATTACHMENT"
    raw = signed_domain_callback(
        session,
        employee,
        {
            "action": "CAPTURE_TO_QUESTION",
            "capture_id": cap.id,
        },
    )
    process(session, callback_event(session, 12345, raw))
    assert_no_rag_dispatch(session, conv)
    assert question.status == "OPEN"
