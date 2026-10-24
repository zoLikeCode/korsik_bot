"""Trusted HR stage scheduling; the model and messenger cannot confirm hiring."""

from fastapi import HTTPException
from types import SimpleNamespace
from sqlalchemy import select
from .auth import user_actor
from .common import (
    assert_employee,
    audit,
    check_etag,
    enqueue,
    fingerprint,
    get_entity,
    require_role,
    save_entity,
)
from .db import now
from .models import Entity, Job, User

TYPE = "EMPLOYMENT_STAGE_CHANGE"
JOB = "DOMAIN_EMPLOYMENT_STAGE_CHANGE"


def _pin(actor):
    return fingerprint(
        {"role": actor.role, "scope": actor.scope, "employee_id": actor.employee_id}
    )


def _current_actor(db, actor_id):
    user = db.scalar(
        select(User)
        .where(User.id == actor_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return (
        user_actor(user)
        if user and user.enabled and user.role in {"HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT"}
        else None
    )


def _locked_target(db, episode_id):
    episode = get_entity(db, episode_id, "employment_episode")
    employee = get_entity(db, episode.parent_id, "employee", lock=True)
    episode = get_entity(db, episode_id, "employment_episode", lock=True)
    return employee, episode


def _finish_cancel(db, op, reason_code, actor_id=None, reason=None):
    from . import domain as d

    op.status = "CANCELLED"
    d.update(
        op,
        reason_code=reason_code,
        cancelled_at=now().isoformat(),
        cancelled_by=actor_id,
    )
    d.event(
        db, "operation_event", op.id, "CANCELLED", actor_id, reason_code=reason_code
    )
    audit(
        db,
        SimpleNamespace(id=actor_id),
        "episode.stage.cancelled",
        op.id,
        {"reason_code": reason_code},
        reason=reason,
    )


def cancel_pending(db, employee, reason_code, actor=None, reason=None):
    """Called while changing a card. Never lock a Job after the employee lock."""
    from . import domain as d

    episode_id = employee.data.get("episode_id")
    if not episode_id:
        return
    employee, episode = _locked_target(db, episode_id)
    for op in db.scalars(
        select(Entity)
        .where(
            Entity.kind == "operation",
            Entity.parent_id == episode_id,
            Entity.status == "SCHEDULED",
        )
        .order_by(Entity.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ):
        if op.data.get("type") == TYPE:
            _finish_cancel(db, op, reason_code, actor.id if actor else None, reason)
    d.update(
        episode,
        stage_schedule_epoch=episode.data.get("stage_schedule_epoch", 0) + 1,
        stage_scheduled_operation_id=None,
    )


def schedule(db, actor, employee, episode, payload, effective):
    from . import domain as d

    require_role(actor, *d.HR)
    if "expected_episode_id" not in payload or "expected_state_version" not in payload:
        raise HTTPException(
            422, "Для отложенной стадии укажите ожидаемый период и версию"
        )
    current_actor = _current_actor(db, actor.id)
    if not current_actor or _pin(current_actor) != _pin(actor):
        raise HTTPException(409, "Права автора изменились; обновите страницу")
    assert_employee(db, current_actor, employee.id)
    cancel_pending(
        db, employee, "SUPERSEDED_BY_NEW_STAGE_COMMAND", actor, payload["reason"]
    )
    op = save_entity(
        db,
        "operation",
        {
            "type": TYPE,
            "employee_id": employee.id,
            "episode_id": episode.id,
            "target_stage": payload["stage"],
            "previous_stage": employee.data.get("employment_stage"),
            "effective_at": effective.isoformat(),
            "actor_id": actor.id,
            "actor_pin": _pin(current_actor),
            "stage_schedule_epoch": episode.data["stage_schedule_epoch"],
            "reason": payload["reason"],
        },
        status="SCHEDULED",
        parent_id=episode.id,
        owner_id=actor.id,
    )
    db.flush()
    d.update(episode, stage_scheduled_operation_id=op.id)
    db.flush()
    d.update(op, expected_episode_version=episode.version)
    enqueue(
        db, JOB, {"operation_id": op.id}, f"episode-stage:{op.id}", run_at=effective
    )
    d.event(
        db,
        "operation_event",
        op.id,
        "SCHEDULED",
        actor.id,
        effective_at=effective.isoformat(),
        target_stage=payload["stage"],
    )
    audit(
        db,
        actor,
        "episode.stage.scheduled",
        episode.id,
        {
            "operation_id": op.id,
            "stage": payload["stage"],
            "effective_at": effective.isoformat(),
        },
        reason=payload["reason"],
    )
    return op


def apply_stage(
    db, actor, employee, episode, stage, effective, reason, operation_id=None
):
    from . import domain as d

    d.update(employee, employment_stage=stage, stage_effective_at=effective.isoformat())
    d.sync_employment_episode(db, employee)
    d.update(episode, stage_effective_at=effective.isoformat())
    for journey in d.entities(db, "journey", employee.id):
        if journey.data.get("episode_id") == episode.id and journey.status not in {
            "COMPLETED",
            "CANCELLED",
        }:
            if stage == "ACTIVE" and journey.status == "SCHEDULED":
                journey.status = "ACTIVE"
            d.replan_journey(db, journey, employee, bump_revision=True)
    d.event(
        db,
        "employment_episode_event",
        episode.id,
        "STAGE_CHANGED",
        actor.id,
        stage=stage,
        effective_at=effective.isoformat(),
        reason=reason,
        operation_id=operation_id,
    )
    audit(
        db,
        actor,
        "episode.stage",
        episode.id,
        {"operation_id": operation_id, "stage": stage},
        reason=reason,
    )


def cancel(db, actor, operation, payload, request):
    from . import domain as d

    require_role(actor, *d.HR)
    employee, episode = _locked_target(db, operation.data["episode_id"])
    current_actor = _current_actor(db, actor.id)
    if not current_actor or _pin(current_actor) != _pin(actor):
        raise HTTPException(403, "Права автора изменились")
    assert_employee(db, current_actor, employee.id)
    operation = get_entity(db, operation.id, "operation", lock=True)
    check_etag(request, operation)
    reason = payload.get("reason")
    if not isinstance(reason, str) or len(reason.strip()) < 3:
        raise HTTPException(422, "Укажите причину отмены не короче 3 символов")
    if operation.status != "SCHEDULED":
        raise HTTPException(409, "Эту операцию уже нельзя отменить")
    _finish_cancel(db, operation, "HR_CANCELLED", actor.id, reason)
    if episode.data.get("stage_scheduled_operation_id") == operation.id:
        d.update(
            episode,
            stage_schedule_epoch=episode.data.get("stage_schedule_epoch", 0) + 1,
            stage_scheduled_operation_id=None,
        )
    return operation


def process(db, job):
    from . import domain as d

    if job.kind != JOB:
        return False
    lease_token = job.payload.get("_lease_token")
    job = db.scalar(
        select(Job)
        .where(Job.id == job.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if job.payload.get("_lease_token") != lease_token:
        return True
    if job.status != "PROCESSING":
        raise RuntimeError("STAGE_LEASE_NOT_PROCESSING")
    if not job.lease_until or d.aware(job.lease_until) <= now():
        raise RuntimeError("STAGE_LEASE_EXPIRED")
    op = get_entity(db, job.payload["operation_id"], "operation")
    employee, episode = _locked_target(db, op.data["episode_id"])
    actor = _current_actor(db, op.data["actor_id"])
    op = get_entity(db, op.id, "operation", lock=True)
    if op.status != "SCHEDULED":
        return True
    effective = d.aware(op.data["effective_at"])
    if effective > now():
        raise RuntimeError("STAGE_JOB_CLAIMED_BEFORE_DUE")
    reason_code = None
    if (
        employee.data.get("episode_id") != episode.id
        or employee.data.get("employment_stage") == "ENDED"
        or episode.status == "ENDED"
    ):
        reason_code = "EMPLOYMENT_EPISODE_ENDED_OR_REPLACED"
    elif not actor or _pin(actor) != op.data["actor_pin"]:
        reason_code = "AUTHOR_ACCESS_CHANGED"
    else:
        try:
            assert_employee(db, actor, employee.id)
        except HTTPException:
            reason_code = "AUTHOR_EMPLOYEE_SCOPE_REVOKED"
    if not reason_code and (
        episode.version != op.data["expected_episode_version"]
        or episode.data.get("stage_schedule_epoch") != op.data["stage_schedule_epoch"]
        or episode.data.get("stage_scheduled_operation_id") != op.id
        or employee.data.get("employment_stage") != op.data["previous_stage"]
    ):
        reason_code = "STALE_EPISODE_OR_STAGE_COMMAND"
    if reason_code:
        # Automatic due-time rejection is a system decision. The original HR
        # remains actor_id on the request, not a falsely attributed canceller.
        _finish_cancel(db, op, reason_code)
    else:
        apply_stage(
            db,
            actor,
            employee,
            episode,
            op.data["target_stage"],
            effective,
            op.data["reason"],
            op.id,
        )
        op.status = "SUCCEEDED"
        d.update(
            op,
            applied_at=now().isoformat(),
            result={"episode_id": episode.id, "stage": op.data["target_stage"]},
        )
        d.event(db, "operation_event", op.id, "SUCCEEDED", actor.id)
    if episode.data.get("stage_scheduled_operation_id") == op.id:
        d.update(episode, stage_scheduled_operation_id=None)
    return True
