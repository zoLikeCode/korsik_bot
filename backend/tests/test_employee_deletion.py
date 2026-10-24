"""Recoverable employment-card deletion: no HTTP/MAX/model requests."""
from copy import deepcopy
from datetime import timedelta
import json

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from korsik import auth, assets, chat, domain as d, max_events as mx, organization as org
from korsik import rag_identity, simple_scenarios as simple, transport_api as tx
from korsik.auth import Actor
from korsik.common import actor_can_employee, employee_is_enabled, etag, save_entity
from korsik.db import now
from korsik.models import Audit, Entity, Outbox, SessionToken, TransportDelivery, User
from test_domain import session as session, hr as hr, profile, req, employee_actor
from test_employee_administration import invite, redeem, scoped_hr
from test_employee_off_security import account, session_for, request as session_request
from test_max import secret_settings as secret_settings
from test_simple_scenarios import clock as clock, no_global_buttons as no_global_buttons, active, queued, accept, program


def remove(db, actor, employee):
    return d.delete_employee(employee.id, req(employee), db, actor)


def restore(db, actor, employee):
    return d.restore_employee(employee.id, req(employee), db, actor)


def denied(code, function, *args, **kwargs):
    with pytest.raises(HTTPException) as caught:
        function(*args, **kwargs)
    assert caught.value.status_code == code


def test_delete_preserves_history_and_hides_directory_card_chat_and_analytics(session, hr):
    employee = profile(session, hr)
    conversation = chat.conversation_for(session, employee)
    historical = save_entity(session, "survey_run", {"employee_id": employee.id,
        "episode_id": employee.data["episode_id"], "answers": {"q": "private"}},
        parent_id=employee.id, status="COMPLETED")
    episode = session.get(Entity, employee.data["episode_id"])
    history = deepcopy(historical.data)
    response = remove(session, hr, employee)
    result = json.loads(response.body)
    assert response.status_code == 200 and response.headers["etag"] == etag(employee)
    assert result["status"] == "DELETED" and result["enabled"] is False
    assert employee.data["deleted_by"] == hr.id and employee.data["deleted_episode_id"] == episode.id
    assert session.get(Entity, historical.id).data == history
    assert session.get(Entity, episode.id) is episode
    assert session.get(Entity, conversation.id) is conversation
    assert d.list_employees(session, hr)["items"] == []
    assert d.filtered_employees(session, hr) == []
    assert org.organization(session, hr)["employees"] == []
    assert chat.conversations(hr, session)["items"] == []
    denied(404, d.get_employee, employee.id, session, hr)
    denied(404, org.employee_detail, employee.id, session, hr)
    denied(404, chat.require_conversation, session, hr, conversation.id)
    audits = list(session.scalars(select(Audit).where(Audit.entity_id == employee.id)))
    assert any(a.action == "employee.delete" for a in audits)


def test_deletion_updates_pagination_totals_and_clamps_last_page(session, hr):
    employees = [profile(session, hr, name=f"Delete {index}") for index in range(11)]
    assert d.list_employees(session, hr, page=2, page_size=10)["total"] == 11
    remove(session, hr, employees[-1])
    result = d.list_employees(session, hr, page=2, page_size=10)
    assert (result["total"], result["pages"], result["page"]) == (10, 1, 1)
    assert employees[-1].id not in {e["id"] for e in result["items"]}
    assert d.list_employees(session, hr, page=1, search="Delete 10")["total"] == 0


def test_delete_requires_scoped_hr_and_current_etag(session, hr):
    employee = profile(session, hr)
    for role in ["EMPLOYEE", "MANAGER", "PLATFORM_ADMIN"]:
        denied(403, d.delete_employee, employee.id, req(employee), session,
               Actor("actor", role, role, {"all": True}, employee_id=employee.id))
    outside = scoped_hr(session, "another-site")
    denied(404, d.delete_employee, employee.id, req(employee), session, outside)
    denied(404, d.delete_employee, employee.id, req(employee), session,
           Actor("head", "Head", "HEAD_TALENT", {"site_ids": ["another-site"]}))
    denied(428, d.delete_employee, employee.id, Request({"type": "http", "headers": []}), session, hr)
    stale = req(employee)
    d.update(employee, name="Changed"); session.commit()
    denied(412, d.delete_employee, employee.id, stale, session, hr)
    assert employee.status != "DELETED" and employee_is_enabled(employee)
    inside = Actor(hr.id, "Scoped HR", "HR_SPECIALIST", {"site_ids": ["GrPP"]})
    assert remove(session, inside, employee).status_code == 200
    another = profile(session, hr)
    head = Actor(hr.id, "Scoped Head", "HEAD_TALENT", {"site_ids": ["GrPP"]})
    assert remove(session, head, another).status_code == 200
    assert another.data["deleted_by"] == head.id and not employee_is_enabled(another)


def test_delete_revokes_employee_and_manager_sessions_but_preserves_independent_hr(session, hr):
    employee = profile(session, hr)
    employee_user = account(session, employee)
    manager = account(session, employee, "MANAGER")
    admin = session.get(User, hr.id)
    admin.employee_id = employee.id
    records = []
    for index, user in enumerate([employee_user, manager, admin]):
        raw, token = session_for(session, user)
        binding = save_entity(session, "principal_binding", {"max_user_id": str(500 + index)}, owner_id=user.id)
        records.append((user, raw, token, binding))
    session.commit()
    remove(session, hr, employee)
    for user, raw, token, binding in records[:2]:
        assert session.get(SessionToken, token.id) is None
        assert auth.session_user(session_request(raw), session) == (None, None)
        assert binding.status == "REVOKED" and user.enabled is True
    _, raw, token, binding = records[-1]
    assert session.get(SessionToken, token.id) is token and binding.status == "ACTIVE"
    assert auth.session_user(session_request(raw), session)[0].id == admin.id
    assert admin.enabled and admin.role == "HR_ADMIN" and admin.scope == {"all": True}
    assert not actor_can_employee(session, hr, employee.id)


def test_delete_revokes_issued_invites_active_binding_and_employee_callbacks(session, hr, secret_settings):
    employee = profile(session, hr)
    issued, token = invite(session, hr, employee)
    binding = save_entity(session, "max_binding", {"episode_id": employee.data["episode_id"],
        "max_user_id": "80101"}, parent_id=employee.id)
    callback = save_entity(session, "callback_action", {"recipient_kind": "EMPLOYEE", "recipient_id": employee.id})
    d.update(employee, binding_id=binding.id, max_binding_status="VERIFIED")
    remove(session, hr, employee)
    assert issued.status == binding.status == callback.status == "REVOKED"
    assert employee.data["binding_id"] is None and employee.data["max_binding_status"] == "REVOKED"
    assert mx.current_employee(session, 80101) is None
    denied(404, chat.issue_employee_invite, employee.id, chat.EmployeeInvitation(), hr, session)
    redeem(session, token, 80101)
    assert employee.status == "DELETED" and binding.status == "REVOKED"


def test_delete_after_trusted_redeem_revokes_binding_without_changing_max_identity(session, hr, secret_settings):
    employee = profile(session, hr)
    _, token = invite(session, hr, employee)
    redeem(session, token, 80202)
    binding = session.get(Entity, employee.data["binding_id"])
    assert binding.status == "ACTIVE"
    remove(session, hr, employee)
    assert binding.status == "REVOKED" and binding.data["max_user_id"] == "80202"
    assert mx.current_employee(session, 80202) is None


def test_delete_suppresses_unsent_pauses_only_access_owned_journeys_and_cancels_answers(session, hr, clock):
    employee, _, _, journey = active(session, hr, clock)
    already_paused = save_entity(session, "journey", {"episode_id": employee.data["episode_id"]}, parent_id=employee.id, status="PAUSED")
    assert simple.run_due(session, journey, employee, clock[0]) == 1
    outbound = queued(session, journey)[-1]
    delivery = TransportDelivery(outbox_id=outbound.id, status="PENDING")
    session.add(delivery)
    answer = save_entity(session, "answer", {"employee_id": employee.id}, status="QUEUED")
    capture = save_entity(session, "input_capture", {}, parent_id=employee.id, status="READY_TO_CONFIRM")
    session.commit()
    remove(session, hr, employee)
    assert outbound.status == delivery.status == "SUPPRESSED"
    assert journey.status == "PAUSED" and journey.data["access_suspended"]
    assert already_paused.status == "PAUSED" and not already_paused.data.get("access_suspended")
    assert answer.status == "CANCELLED" and capture.status == "CANCELLED"
    assert simple.run_due(session, journey, employee, clock[0] + timedelta(days=7)) == 0
    assert d.enroll_employee(session, employee) is None
    other_scenario, _ = program(session)
    before_journeys = len(d.entities(session, "journey", employee.id))
    assert simple.enroll(session, employee, other_scenario) is None
    assert len(d.entities(session, "journey", employee.id)) == before_journeys


def test_delete_retains_unknown_delivery_and_presend_rejects_existing_sending_lease(session, hr):
    employee = profile(session, hr)
    unknown = Outbox(recipient_kind="EMPLOYEE", recipient_id=employee.id, text="Unknown",
        unique_key="delete-unknown", status="UNKNOWN_DELIVERY", data={})
    sending = Outbox(recipient_kind="EMPLOYEE", recipient_id=employee.id, text="Sending",
        unique_key="delete-sending", status="SENDING", data={})
    session.add_all([unknown, sending]); session.flush()
    delivery = TransportDelivery(outbox_id=sending.id, status="SENDING", attempt_id="delete-attempt",
        attempts=1, lease_until=now() + timedelta(minutes=2))
    session.add(delivery); session.commit()
    remove(session, hr, employee)
    assert unknown.status == "UNKNOWN_DELIVERY" and sending.status == "SENDING"
    denied(409, tx.validate_delivery_attempt, delivery.id, tx.DeliveryValidation(attempt_id="delete-attempt"), session)
    assert delivery.status == sending.status == "SUPPRESSED"
    assert tx.valid_outbox(session, unknown) == (None, "EMPLOYEE_DISABLED")


def test_restore_is_scoped_etag_protected_and_off_without_reactivating_old_tokens(session, hr, secret_settings):
    employee = profile(session, hr)
    invitation, token = invite(session, hr, employee)
    original_episode = employee.data["episode_id"]
    remove(session, hr, employee)
    deleted_request = req(employee)
    outside = Actor("outside", "Outside", "HR_SPECIALIST", {"site_ids": ["another"]})
    denied(404, d.restore_employee, employee.id, deleted_request, session, outside)
    denied(403, d.restore_employee, employee.id, deleted_request, session, employee_actor(employee))
    response = restore(session, hr, employee)
    assert response.status_code == 200 and employee.status == "ACTIVE"
    assert employee.data["enabled"] is False and employee.data["episode_id"] == original_episode
    assert len(d.list_employees(session, hr)["items"]) == 1
    assert invitation.status == "REVOKED" and mx.current_employee(session, 80303) is None
    redeem(session, token, 80303)
    assert invitation.status == "REVOKED" and employee.data["max_binding_status"] == "REVOKED"
    denied(412, d.restore_employee, employee.id, deleted_request, session, hr)
    assert any(a.action == "employee.restore" for a in session.scalars(select(Audit)))


def test_restore_does_not_reopen_ended_episode_or_changed_episode(session, hr):
    ended = profile(session, hr)
    d.update(ended, employment_stage="ENDED")
    episode = session.get(Entity, ended.data["episode_id"]); episode.status = "ENDED"
    ended.status = "ENDED"; session.commit()
    remove(session, hr, ended); restore(session, hr, ended)
    assert ended.status == "ENDED" and ended.data["employment_stage"] == "ENDED"
    assert episode.status == "ENDED" and ended.data["enabled"] is False
    denied(409, d.employee_administration, ended.id, {"enabled": True}, req(ended), session, hr)
    other = profile(session, hr)
    remove(session, hr, other)
    d.update(other, episode_id="different-episode"); session.commit()
    denied(409, d.restore_employee, other.id, req(other), session, hr)
    assert other.status == "DELETED"


def test_restore_keeps_waiting_answers_and_does_not_generate_missed_messages(session, hr, clock):
    employee, _, _, journey = active(session, hr, clock)
    simple.run_due(session, journey, employee, clock[0]); accept(session, queued(session, journey)[-1], clock)
    before_answers = deepcopy(journey.data.get("answers", {}))
    before_messages = len(queued(session, journey))
    remove(session, hr, employee)
    clock[0] += timedelta(days=7)
    restore(session, hr, employee)
    assert journey.status == "PAUSED" and journey.data["waiting_step_key"] == "hello"
    assert journey.data.get("answers", {}) == before_answers
    assert simple.run_due(session, journey, employee, clock[0]) == 0
    assert len(queued(session, journey)) == before_messages


def test_delete_cancels_future_stage_operation_without_ending_episode(session, hr):
    employee = profile(session, hr)
    episode = session.get(Entity, employee.data["episode_id"])
    operation = save_entity(session, "operation", {"type": "EMPLOYMENT_STAGE_CHANGE"},
        parent_id=episode.id, status="SCHEDULED")
    d.update(episode, stage_scheduled_operation_id=operation.id)
    stage = employee.data["employment_stage"]
    remove(session, hr, employee)
    assert operation.status == "CANCELLED" and operation.data["reason_code"] == "EMPLOYEE_DELETED"
    assert episode.data["stage_scheduled_operation_id"] is None
    assert employee.data["employment_stage"] == stage and episode.status != "ENDED"


def test_stale_mutation_refreshes_deleted_row_and_status_fences_forged_enabled(session, hr):
    employee = profile(session, hr)
    factory = sessionmaker(bind=session.bind, expire_on_commit=False)
    with factory() as cached:
        stale_employee = cached.get(Entity, employee.id)
        stale_request = req(stale_employee)
        assert actor_can_employee(cached, hr, employee.id)
        remove(session, hr, employee)
        denied(404, d.employee_administration, employee.id, {"enabled": True}, stale_request, cached, hr)
        assert not actor_can_employee(cached, hr, employee.id)
        assert actor_can_employee(cached, hr, employee.id, include_deleted=True)
    d.update(employee, enabled=True); session.commit()
    assert not employee_is_enabled(employee)
    assert org.organization(session, hr)["employees"] == []


def test_delete_does_not_change_rag_pipeline_identities_or_private_asset_history(session, hr):
    employee = profile(session, hr)
    asset = save_entity(session, "asset", {"employee_id": employee.id, "episode_id": employee.data["episode_id"]},
        status="READY", owner_id=employee.id)
    identity = rag_identity.pipeline_identities()
    assert assets.can_asset(session, hr, asset)
    remove(session, hr, employee)
    assert rag_identity.pipeline_identities() == identity
    assert session.get(Entity, asset.id).status == "READY"
    assert not assets.can_asset(session, hr, asset)
    assert not assets.can_asset(session, employee_actor(employee), asset)


def test_old_queue_remains_revoked_after_restore_on_and_new_binding_while_new_queue_is_allowed(session, hr):
    employee = profile(session, hr)
    old = Outbox(recipient_kind="EMPLOYEE", recipient_id=employee.id, text="Old queue",
        unique_key="delete-old-cutoff", status="PENDING", data={}, created_at=now() - timedelta(minutes=1))
    session.add(old); session.commit()
    remove(session, hr, employee)
    # Represents an occupied queue row skipped by the deletion transaction.
    old.status = "QUEUED"
    restore(session, hr, employee)
    d.employee_administration(employee.id, {"enabled": True}, req(employee), session, hr)
    binding = save_entity(session, "max_binding", {"episode_id": employee.data["episode_id"],
        "max_user_id": "80404"}, parent_id=employee.id)
    d.update(employee, binding_id=binding.id, max_binding_status="VERIFIED")
    fresh = Outbox(recipient_kind="EMPLOYEE", recipient_id=employee.id, text="Fresh queue",
        unique_key="delete-new-cutoff", status="PENDING", data={}, created_at=now() + timedelta(seconds=1))
    session.add(fresh); session.commit()
    assert tx.valid_outbox(session, old) == (None, "EMPLOYEE_DELETED")
    assert tx.valid_outbox(session, fresh) == ({"user_id": 80404}, None)
    assert employee.data["enabled"] is True and binding.status == "ACTIVE"


@pytest.mark.parametrize("corrupt", [None, "bad timestamp", 123, {}, True])
def test_malformed_persisted_deletion_cutoff_fails_closed(session, hr, corrupt):
    employee = profile(session, hr)
    d.update(employee, deleted_at=corrupt)
    session.commit()
    outbound = Outbox(recipient_kind="EMPLOYEE", recipient_id=employee.id,
        text="Never authorized", data={}, created_at=now())
    assert tx.valid_outbox(session, outbound) == (None, "EMPLOYEE_DELETED")
