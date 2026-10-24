"""Calendar recurrence for SIMPLE journeys; caller commits and sends no network I/O."""
from datetime import timedelta
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo
from sqlalchemy import select
from . import domain as d
from .models import Outbox, TransportDelivery
from .common import employee_is_enabled
from .scenario_engine import local_datetime


def next_occurrence(policy, after):
    zone = policy.get("timezone", "Europe/Moscow")
    day = after.astimezone(ZoneInfo(zone)).date()
    candidate = local_datetime(day, policy["time"], zone)
    if candidate <= after:
        candidate = local_datetime(day + timedelta(days=policy["every_days"]), policy["time"], zone)
    return candidate


def _retire(db, journey, clock):
    # Cancel this run only. Other surveys and other scenarios retain their captures.
    journey.status = "CANCELLED"
    d.update(journey, schedule_revision=journey.data.get("schedule_revision", 1) + 1,
             cancellation_reason="NEXT_RECURRING_OCCURRENCE", cancelled_at=clock.isoformat())
    target_ids = set()
    for kind in ("scheduled_action", "survey_run", "scenario_question"):
        for row in d.entities(db, kind, journey.id):
            target_ids.add(row.id)
            if row.status not in {"COMPLETED", "ACCEPTED", "ANSWERED", "DECLINED", "CANCELLED"}:
                row.status = "CANCELLED"
    for kind in ("input_capture", "scenario_free_answer"):
        for row in d.entities(db, kind, journey.parent_id):
            owns_capture = (row.data.get("journey_id") == journey.id
                or row.data.get("survey_run_id") in target_ids
                or row.data.get("scenario_question_id") in target_ids)
            if owns_capture and row.status in {"OPEN", "DRAFT"}:
                row.status = "CANCELLED"
    for outbound in db.scalars(select(Outbox).where(Outbox.recipient_id == journey.parent_id)):
        if (outbound.data or {}).get("journey_id") != journey.id:
            continue
        if outbound.status in {"PENDING", "READY", "RETRY", "QUEUED"}:
            outbound.status = "CANCELLED"
            for delivery in db.scalars(select(TransportDelivery).where(TransportDelivery.outbox_id == outbound.id,
                    TransportDelivery.status.in_(["PENDING", "RETRY"]))):
                delivery.status = "CANCELLED"
                delivery.error_code = "STALE_SCHEDULE"


def ensure(db, employee, scenario, version, clock=None):
    """Lock order matches enrollment. A clock jump creates at most one latest run."""
    from . import simple_scenarios as simple
    clock = clock or d.now()
    employee = d.get_entity(db, employee.id, "employee", lock=True)
    scenario = d.get_entity(db, scenario.id, "scenario", lock=True)
    if not employee_is_enabled(employee) or employee.data.get("employment_stage") == "ENDED" or scenario.status == "ARCHIVED":
        return None
    if scenario.data.get("published_version_id") != version.id or not d._scenario_applies(scenario, employee):
        return None
    episode = employee.data.get("episode_id")
    key = str(uuid5(NAMESPACE_URL, f"korsik:recurrence:{employee.id}:{episode}:{scenario.id}"))
    state = db.scalar(select(d.Entity).where(d.Entity.id == key).with_for_update().execution_options(populate_existing=True))
    policy = version.data["graph"]["start_policy"]
    if state is None or state.data.get("version_id") != version.id:
        due = next_occurrence(policy, clock)
        data = {"employee_id": employee.id, "episode_id": episode, "scenario_id": scenario.id,
                "version_id": version.id, "next_at": due.isoformat()}
        if state:
            d.update(state, **data)
        else:
            state = d.save_entity(db, "scenario_recurrence", data, parent_id=employee.id, id=key)
    due = d.aware(state.data["next_at"])
    if due <= clock:
        zone = policy.get("timezone", "Europe/Moscow")
        elapsed_days = (clock.astimezone(ZoneInfo(zone)).date() - due.astimezone(ZoneInfo(zone)).date()).days
        jumps = max(0, elapsed_days // policy["every_days"])
        latest = local_datetime(due.astimezone(ZoneInfo(zone)).date() + timedelta(days=jumps * policy["every_days"]), policy["time"], zone)
        if latest > clock:
            jumps -= 1
            latest = local_datetime(latest.astimezone(ZoneInfo(zone)).date() - timedelta(days=policy["every_days"]), policy["time"], zone)
        due = latest
    jid = str(uuid5(NAMESPACE_URL, f"korsik:recurring-run:{key}:{version.id}:{due.isoformat()}"))
    journey = db.scalar(select(d.Entity).where(d.Entity.id == jid).execution_options(populate_existing=True))
    if due <= clock:
        previous = [j for j in d.entities(db, "journey", employee.id)
                    if j.id != jid and j.data.get("recurrence_id") == key and j.status in {"SCHEDULED", "ACTIVE", "PAUSED"}]
        ids = {j.id for j in previous}
        uncertain = any(o.status in {"SENDING", "UNKNOWN", "UNKNOWN_DELIVERY"}
            and (o.data or {}).get("journey_id") in ids for o in db.scalars(select(Outbox).where(Outbox.recipient_id == employee.id)))
        if uncertain:
            d.update(state, review_reason="DELIVERY_RECONCILIATION_REQUIRED", blocked_at=clock.isoformat())
            if journey and journey.status == "SCHEDULED":
                journey.status = "PAUSED"
                d.update(journey, requires_hr_review=True, review_reason="DELIVERY_RECONCILIATION_REQUIRED")
            return None
        for old in previous:
            _retire(db, old, clock)
        if journey and journey.status == "PAUSED" and journey.data.get("review_reason") == "DELIVERY_RECONCILIATION_REQUIRED":
            journey.status = "SCHEDULED"
            d.update(journey, requires_hr_review=False, review_reason=None)
    if journey is None:
        journey = d.save_entity(db, "journey", {"employee_id": employee.id, "episode_id": episode,
                "scenario_id": scenario.id, "version_id": version.id, "recurrence_id": key,
                "occurrence_at": due.isoformat(), "schedule_revision": 1, "signals": {}, "events": {},
                "answers": {}, "completed_steps": [], "last_step_key": None, "waiting_step_key": None},
                status="SCHEDULED", parent_id=employee.id, id=jid)
        simple.plan(db, journey, employee)
    if due <= clock:
        following = local_datetime(due.astimezone(ZoneInfo(policy.get("timezone", "Europe/Moscow"))).date()
                    + timedelta(days=policy["every_days"]), policy["time"], policy.get("timezone", "Europe/Moscow"))
        d.update(state, next_at=following.isoformat(), last_occurrence_at=due.isoformat(), review_reason=None)
    return journey
