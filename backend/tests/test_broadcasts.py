"""Synthetic MAX broadcasts: no connector/network/model requests."""

from datetime import datetime, timedelta, timezone
import uuid

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from korsik import broadcasts as b, domain as d, transport_api as tx
from korsik.auth import Actor
from korsik.common import queue_message, save_entity
from korsik.models import Entity, Outbox, TransportDelivery, User
from test_domain import session, hr, profile
from test_max import secret_settings


@pytest.fixture
def clock(monkeypatch):
    value = [datetime(2026, 10, 25, 9, tzinfo=timezone.utc)]
    monkeypatch.setattr(b, "now", lambda: value[0])
    return value


def person(db, hr, name="Иван", **changes):
    employee = profile(db, hr, name=name, **changes)
    binding = save_entity(
        db,
        "max_binding",
        {
            "episode_id": employee.data["episode_id"],
            "max_user_id": str(9000 + len(d.entities(db, "max_binding"))),
            "display_name": name + " MAX",
        },
        status="ACTIVE",
        parent_id=employee.id,
    )
    d.update(employee, binding_id=binding.id, max_binding_status="VERIFIED")
    return employee, binding


def body(ids=(), units=(), **changes):
    payload = dict(
        client_request_id=uuid.uuid4(),
        text="Общее сообщение",
        recipient_ids=list(ids),
        org_unit_ids=list(units),
        scheduled_date="2026-10-25",
        scheduled_time="12:05",
        timezone="Europe/Moscow",
    )
    payload.update(changes)
    return b.BroadcastInput(**payload)


def send_later(db, hr, clock, people):
    result = b.create_broadcast(body([p.id for p in people]), hr, db)
    clock[0] += timedelta(minutes=6)
    b.tick(db)
    return result, db.get(Entity, result["id"])


def outs(db, mailing):
    return list(
        db.scalars(
            select(Outbox).where(Outbox.data["broadcast_id"].as_string() == mailing.id)
        )
    )


def test_creation_is_idempotent_and_schedule_never_sends_early(session, hr, clock):
    employee, _ = person(session, hr)
    request = body([employee.id])
    first = b.create_broadcast(request, hr, session)
    repeated = b.create_broadcast(request, hr, session)
    assert repeated["id"] == first["id"] and repeated["status_label"] == "В ожидании"
    b.tick(session)
    mailing = session.get(Entity, first["id"])
    assert not outs(session, mailing)
    clock[0] += timedelta(minutes=6)
    b.tick(session)
    b.tick(session)
    assert len(outs(session, mailing)) == 1 and mailing.status == "SENDING"
    assert b.broadcast_detail(mailing.id, hr, session)["counts"]["accepted"] == 0
    changed = request.model_copy(update={"text": "Другой текст"})
    with pytest.raises(HTTPException) as error:
        b.create_broadcast(changed, hr, session)
    assert error.value.status_code == 409


def test_actual_receipts_are_required_and_partial_failure_is_never_completed(
    session, hr, clock, secret_settings
):
    first, _ = person(session, hr, "Первый")
    second, _ = person(session, hr, "Второй")
    _, mailing = send_later(session, hr, clock, [first, second])
    tx.prepare_deliveries(session)
    deliveries = list(
        session.scalars(
            select(TransportDelivery).where(
                TransportDelivery.outbox_id.in_([o.id for o in outs(session, mailing)])
            )
        )
    )
    first_claim = tx.claim_delivery(deliveries[0].id, session)
    tx.delivery_receipt(
        deliveries[0].id,
        tx.DeliveryReceipt(
            attempt_id=first_claim["attempt_id"],
            status="ACCEPTED",
            platform_message_id="synthetic-1",
        ),
        session,
    )
    result = b.broadcast_detail(mailing.id, hr, session)
    assert (
        result["status"] == "WAITING"
        and result["counts"]["accepted"] == 1
        and result["counts"]["pending"] == 1
    )
    second_claim = tx.claim_delivery(deliveries[1].id, session)
    tx.delivery_receipt(
        deliveries[1].id,
        tx.DeliveryReceipt(
            attempt_id=second_claim["attempt_id"],
            status="REJECTED",
            error_code="SYNTHETIC_REJECTED",
        ),
        session,
    )
    b.tick(session)
    result = b.broadcast_detail(mailing.id, hr, session)
    assert result["status_label"] == "В ожидании" and result["counts"]["failed"] == 1
    assert any(row["error"] for row in result["recipients"])


def test_all_accepted_receipts_complete_exactly_once(
    session, hr, clock, secret_settings
):
    employee, _ = person(session, hr)
    _, mailing = send_later(session, hr, clock, [employee])
    tx.prepare_deliveries(session)
    delivery = session.scalar(
        select(TransportDelivery).where(
            TransportDelivery.outbox_id == outs(session, mailing)[0].id
        )
    )
    claim = tx.claim_delivery(delivery.id, session)
    receipt = tx.DeliveryReceipt(
        attempt_id=claim["attempt_id"],
        status="ACCEPTED",
        platform_message_id="synthetic",
    )
    tx.delivery_receipt(delivery.id, receipt, session)
    tx.delivery_receipt(delivery.id, receipt, session)
    b.tick(session)
    b.tick(session)
    assert b.broadcast_detail(mailing.id, hr, session)["status_label"] == "Исполнено"
    assert len(outs(session, mailing)) == 1


def test_unavailable_explicit_recipients_and_unseen_people_are_rejected(
    session, hr, clock
):
    employee, binding = person(session, hr)
    binding.status = "REVOKED"
    with pytest.raises(HTTPException) as error:
        b.create_broadcast(body([employee.id]), hr, session)
    assert error.value.status_code == 422
    with pytest.raises(HTTPException) as error:
        b.create_broadcast(body(["not-visible"]), hr, session)
    assert error.value.status_code == 404
    assert d.entities(session, "broadcast") == []


@pytest.mark.parametrize("role", ["EMPLOYEE", "MANAGER", "PLATFORM_ADMIN"])
def test_non_hr_roles_cannot_read_or_schedule_broadcasts(session, hr, clock, role):
    actor = Actor("no", "Forbidden", role, {"all": True})
    for action in (
        lambda: b.recipients(actor, session),
        lambda: b.list_broadcasts(actor, session),
        lambda: b.create_broadcast(body(["anything"]), actor, session),
    ):
        with pytest.raises(HTTPException) as error:
            action()
        assert error.value.status_code == 403


@pytest.mark.parametrize("role", ["HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT"])
def test_allowed_roles_are_live_and_scope_is_checked(session, hr, clock, role):
    user = session.get(User, hr.id)
    user.role = role
    actor = Actor(hr.id, "HR", role, {"all": True})
    employee, _ = person(session, hr)
    result = b.create_broadcast(body([employee.id]), actor, session)
    user.scope = {"site_ids": ["GTP"]}
    assert b.list_broadcasts(actor, session)["items"] == []
    with pytest.raises(HTTPException):
        b.broadcast_detail(result["id"], actor, session)
    clock[0] += timedelta(minutes=6)
    b.tick(session)
    row = d.entities(session, "broadcast_recipient", result["id"])[0]
    assert row.status == "SUPPRESSED" and row.data["reason"] == "AUTHOR_SCOPE_REVOKED"


def test_org_descendants_union_snapshot_skips_and_later_membership_fence(
    session, hr, clock
):
    first, _ = person(session, hr, "Участник")
    second, binding = person(session, hr, "Без MAX")
    legal = first.data["legal_entity_id"]
    parent = save_entity(
        session,
        "org_unit",
        {"title": "Направление", "type": "DIRECTION", "legal_entity_id": legal},
        status="ACTIVE",
    )
    child = save_entity(
        session,
        "org_unit",
        {"title": "Команда", "type": "TEAM", "legal_entity_id": legal},
        status="ACTIVE",
        parent_id=parent.id,
    )
    d.update(first, org_unit_id=child.id)
    d.update(second, org_unit_id=child.id)
    binding.status = "REVOKED"
    result = b.create_broadcast(body([first.id], [parent.id, child.id]), hr, session)
    assert result["counts"] == {
        "total": 2,
        "eligible": 1,
        "accepted": 0,
        "skipped": 1,
        "failed": 0,
        "unknown": 0,
        "pending": 1,
    }
    late, _ = person(session, hr, "Позже")
    d.update(late, org_unit_id=child.id)
    clock[0] += timedelta(minutes=6)
    b.tick(session)
    mailing = session.get(Entity, result["id"])
    assert (
        len(outs(session, mailing)) == 1
        and outs(session, mailing)[0].recipient_id == first.id
    )
    # Explicit selection still applies when a person is also in a group.
    d.update(first, org_unit_id=None)
    assert tx.valid_outbox(session, outs(session, mailing)[0])[0] is not None
    group_only = b.create_broadcast(
        body(units=[parent.id], scheduled_time="12:20"), hr, session
    )
    d.update(late, org_unit_id=None)
    clock[0] += timedelta(minutes=20)
    b.tick(session)
    row = next(
        r
        for r in d.entities(session, "broadcast_recipient", group_only["id"])
        if r.data["employee_id"] == late.id
    )
    assert row.status == "SUPPRESSED" and row.data["reason"] == "ORG_MEMBERSHIP_CHANGED"


@pytest.mark.parametrize(
    "change,reason",
    [
        ("off", "EMPLOYEE_DISABLED"),
        ("delete", "EMPLOYEE_DISABLED"),
        ("episode", "STALE_EPISODE"),
        ("binding", "BINDING_CHANGED"),
        ("role", "AUTHOR_SCOPE_REVOKED"),
    ],
)
def test_delivery_rechecks_live_fences_after_queueing(
    session, hr, clock, change, reason
):
    employee, binding = person(session, hr)
    _, mailing = send_later(session, hr, clock, [employee])
    out = outs(session, mailing)[0]
    assert tx.valid_outbox(session, out)[0] is not None
    if change == "off":
        d.update(employee, enabled=False)
    elif change == "delete":
        employee.status = "DELETED"
    elif change == "episode":
        d.update(employee, episode_id="new-episode")
        binding.data = {**binding.data, "episode_id": "new-episode"}
    elif change == "binding":
        new = save_entity(
            session,
            "max_binding",
            {**binding.data},
            parent_id=employee.id,
            status="ACTIVE",
        )
        d.update(employee, binding_id=new.id)
    else:
        session.get(User, hr.id).role = "MANAGER"
    denied = tx.valid_outbox(session, out)
    assert denied[0] is None and denied[1] == reason


def test_inserted_address_is_plain_text_without_max_format(
    session, hr, clock, secret_settings
):
    recipient, _ = person(session, hr, "Получатель")
    mentioned, binding = person(session, hr, "Имя <важно>")
    request = body([recipient.id])
    request.text = f"Привет <script> {{" + "{mention:" + mentioned.id + "}} & всем"
    result = b.create_broadcast(request, hr, session)
    clock[0] += timedelta(minutes=6)
    b.tick(session)
    mailing = session.get(Entity, result["id"])
    tx.prepare_deliveries(session)
    out = outs(session, mailing)[0]
    delivery = session.scalar(
        select(TransportDelivery).where(TransportDelivery.outbox_id == out.id)
    )
    claim = tx.claim_delivery(delivery.id, session)
    assert "format" not in claim["body"]
    assert "max://" not in claim["body"]["text"]
    assert claim["body"]["text"] == "Привет <script> Имя <важно> & всем"
    ordinary = queue_message(
        session, "EMPLOYEE", recipient.id, "Обычный <текст>", "ordinary-broadcast-test"
    )
    tx.prepare_deliveries(session)
    delivery = session.scalar(
        select(TransportDelivery).where(TransportDelivery.outbox_id == ordinary.id)
    )
    normal = tx.claim_delivery(delivery.id, session)
    assert (
        normal["body"]["text"] == "Обычный <текст>" and "format" not in normal["body"]
    )
    d.update(mentioned, enabled=False)
    assert b.delivery_allowed(session, out) == (False, "MENTION_UNAVAILABLE")


def test_timezone_and_dst_are_explicit_and_past_is_rejected(session, hr, clock):
    employee, _ = person(session, hr)
    request = body([employee.id])
    request.timezone = "Asia/Anadyr"
    request.scheduled_time = "22:00"
    assert (
        b.create_broadcast(request, hr, session)["scheduled_at"]
        == "2026-10-25T10:00:00+00:00"
    )
    request = body([employee.id])
    request.scheduled_time = "11:59"
    with pytest.raises(HTTPException):
        b.create_broadcast(request, hr, session)
    for day, value in [("2027-03-14", "02:30"), ("2027-11-07", "01:30")]:
        request = body([employee.id])
        request.timezone = "America/New_York"
        request.scheduled_date = datetime.fromisoformat(day).date()
        request.scheduled_time = value
        with pytest.raises(HTTPException) as error:
            b.create_broadcast(request, hr, session)
        assert error.value.status_code == 422


def test_bounded_batch_and_no_silent_recipient_truncation(
    session, hr, clock, monkeypatch
):
    people = [person(session, hr, str(i))[0] for i in range(3)]
    monkeypatch.setattr(b, "BATCH", 2)
    result = b.create_broadcast(body([p.id for p in people]), hr, session)
    mailing = session.get(Entity, result["id"])
    clock[0] += timedelta(minutes=6)
    b.tick(session)
    assert len(outs(session, mailing)) == 2
    b.tick(session)
    b.tick(session)
    assert len(outs(session, mailing)) == 3
    monkeypatch.setattr(b, "MAX_RECIPIENTS", 2)
    with pytest.raises(HTTPException):
        b.create_broadcast(body([p.id for p in people]), hr, session)
    assert len(d.entities(session, "broadcast")) == 1


def test_unknown_delivery_never_retries_or_claims_completion(
    session, hr, clock, secret_settings
):
    employee, _ = person(session, hr)
    _, mailing = send_later(session, hr, clock, [employee])
    tx.prepare_deliveries(session)
    delivery = session.scalar(
        select(TransportDelivery).where(
            TransportDelivery.outbox_id == outs(session, mailing)[0].id
        )
    )
    claim = tx.claim_delivery(delivery.id, session)
    tx.delivery_receipt(
        delivery.id,
        tx.DeliveryReceipt(
            attempt_id=claim["attempt_id"],
            status="UNKNOWN",
            error_code="NETWORK_UNCERTAIN",
        ),
        session,
    )
    b.tick(session)
    b.tick(session)
    result = b.broadcast_detail(mailing.id, hr, session)
    assert (
        result["status_label"] == "В ожидании"
        and result["counts"]["failed"] == 0
        and result["counts"]["unknown"] == 1
        and result["counts"]["pending"] == 1
        and result["counts"]["accepted"] == 0
    )
    assert len(outs(session, mailing)) == 1
    with pytest.raises(HTTPException):
        tx.claim_delivery(delivery.id, session)


def test_group_skips_disabled_members_without_silently_adding_them_later(
    session, hr, clock
):
    active, _ = person(session, hr, "Активный")
    disabled, _ = person(session, hr, "Выключенный", enabled=False)
    unit = save_entity(
        session,
        "org_unit",
        {
            "title": "Команда",
            "type": "TEAM",
            "legal_entity_id": active.data["legal_entity_id"],
        },
        status="ACTIVE",
    )
    d.update(active, org_unit_id=unit.id)
    d.update(disabled, org_unit_id=unit.id, enabled=False)
    result = b.create_broadcast(body(units=[unit.id]), hr, session)
    assert result["counts"]["skipped"] == 1 and result["counts"]["eligible"] == 1
    d.update(disabled, enabled=True)
    clock[0] += timedelta(minutes=6)
    b.tick(session)
    mailing = session.get(Entity, result["id"])
    assert [row.recipient_id for row in outs(session, mailing)] == [active.id]


def test_unseen_org_or_mention_never_creates_a_partial_broadcast(session, hr, clock):
    employee, _ = person(session, hr)
    for request in (
        body([employee.id], units=["unseen-unit"]),
        body([employee.id], text="{{mention:unknown-employee}}"),
    ):
        with pytest.raises(HTTPException):
            b.create_broadcast(request, hr, session)
    assert not d.entities(session, "broadcast")
    assert not list(
        session.scalars(select(Outbox).where(Outbox.unique_key.like("broadcast:%")))
    )


def test_deleted_recipient_history_is_masked_only_while_current_scope_still_grants_it(
    session, hr, clock
):
    employee, _ = person(session, hr, "Имя, которое надо скрыть")
    result = b.create_broadcast(body([employee.id]), hr, session)
    employee.status = "DELETED"
    d.update(employee, enabled=False)
    clock[0] += timedelta(minutes=6)
    b.tick(session)
    mailing = session.get(Entity, result["id"])
    history = b.broadcast_detail(mailing.id, hr, session)
    assert history["recipients"][0]["name"] == "Сотрудник удалён"
    assert history["counts"]["failed"] == 1 and history["status"] == "WAITING"
    assert not outs(session, mailing)
    assert all(
        row["id"] != employee.id for row in b.recipients(hr, session)["employees"]
    )
    user = session.get(User, hr.id)
    user.scope = {"site_ids": ["GTP"]}
    with pytest.raises(HTTPException) as denied:
        b.broadcast_detail(mailing.id, hr, session)
    assert denied.value.status_code == 404
    assert b.list_broadcasts(hr, session)["items"] == []


def test_binding_revocation_from_another_transaction_overrides_cached_active_binding(
    session, hr, clock
):
    from sqlalchemy.orm import Session

    employee, binding = person(session, hr)
    _, mailing = send_later(session, hr, clock, [employee])
    out = outs(session, mailing)[0]
    session.commit()
    assert binding.status == "ACTIVE"
    with Session(bind=session.bind, expire_on_commit=False) as other:
        revoked = other.get(Entity, binding.id)
        revoked.status = "REVOKED"
        other.commit()
    assert (
        binding.status == "ACTIVE"
    )  # prove the caller really holds a stale identity-map row
    allowed, reason = tx.valid_outbox(session, out)
    assert allowed is None and reason == "NO_BINDING"
    assert binding.status == "REVOKED"


def test_address_insertion_does_not_require_mentioned_max_account(session, hr, clock):
    recipient, _ = person(session, hr, "Получатель")
    mentioned, binding = person(session, hr, "Полное имя")
    d.update(
        mentioned,
        preferred_name="Короткое обращение",
        binding_id=None,
        max_binding_status="NOT_CONNECTED",
    )
    request = body(
        [recipient.id], text="Здравствуйте, {{mention:" + mentioned.id + "}}!"
    )
    result = b.create_broadcast(request, hr, session)
    mailing = session.get(Entity, result["id"])
    assert mailing.data["display_text"] == "Здравствуйте, Короткое обращение!"
    assert "max_user_id" not in mailing.data["mentions"][0]
    assert "binding_id" not in mailing.data["mentions"][0]


def test_recipient_directory_uses_current_unit_and_preferred_name(session, hr, clock):
    employee, _ = person(session, hr, "Полное имя")
    unit = save_entity(
        session,
        "org_unit",
        {"title": "Текущий отдел", "type": "DEPARTMENT", "legal_entity_id": "le-one"},
        status="ACTIVE",
    )
    d.update(
        employee,
        preferred_name="Обращение",
        org_unit_id=unit.id,
        department="Старый отдел",
    )
    projected = b._projection(session, employee, {unit.id: unit})
    assert projected["department"] == "Текущий отдел"
    assert projected["preferred_name"] == "Обращение"
