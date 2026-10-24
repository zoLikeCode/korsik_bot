"""Current organization membership for employee-facing authorization.

An organization path is server data, never a claim supplied by an employee or
an indexed chunk. Re-read it for each authorization decision so a move,
reparenting or revocation takes effect without reindexing knowledge.
"""
from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import inspect, select
from sqlalchemy.orm import object_session

from .models import Entity

MAX_ORGANIZATION_DEPTH = 512


def employee_session(employee, db=None):
    """Resolve only a caller-supplied Session or the Entity's owning Session."""
    return db if db is not None else object_session(employee) if isinstance(employee, Entity) else None


def current_employee(db, employee):
    """Refresh persisted identities; anonymous dicts remain trusted QA profiles.

    Dictionaries may contain an employee id for server-side delivery checks.
    If present, its current database row wins over copied attributes. A missing
    or wrong-kind identity does not fall back to the supplied snapshot.
    """
    if employee is None:
        return None
    db = employee_session(employee, db)
    if isinstance(employee, Entity):
        if employee.kind != "employee":
            return None
        state = inspect(employee)
        identity = employee.id if state.identity is not None else None
    elif isinstance(employee, Mapping):
        identity = employee.get("employee_id") or employee.get("id")
    else:
        return None
    if db is not None and identity:
        return db.scalar(
            select(Entity)
            .where(Entity.id == identity, Entity.kind == "employee")
            .execution_options(populate_existing=True)
        )
    return employee


def employee_data(employee):
    return employee.data if isinstance(employee, Entity) else employee if isinstance(employee, Mapping) else {}


def org_unit_ancestors(db, employee):
    """Return the current unit and its valid ACTIVE ancestors, fail closed.

    A unit in another legal entity or a site-specific unit on another site
    cannot grant access. An invalid/archived parent stops the chain. Cycles or
    excessive depth invalidate the whole path rather than grant cyclic rights.
    With no Session only direct membership is usable; fabricated ancestor
    attributes are deliberately ignored.
    """
    db = employee_session(employee, db)
    employee = current_employee(db, employee)
    data = employee_data(employee)
    if not data or data.get("enabled", True) is False:
        return set()
    key = data.get("org_unit_id")
    if not isinstance(key, str) or not key:
        return set()
    if db is None:
        return {key}
    legal = data.get("legal_entity_id")
    if not legal:
        return set()
    units = {
        row.id: row
        for row in db.scalars(
            select(Entity)
            .where(Entity.kind == "org_unit", Entity.status == "ACTIVE",
                   Entity.data["legal_entity_id"].as_string() == legal)
            .execution_options(populate_existing=True)
        )
        if not row.data.get("site_id") or row.data["site_id"] == data.get("site_id")
    }
    path = set()
    while key:
        if key in path or len(path) >= MAX_ORGANIZATION_DEPTH:
            return set()
        unit = units.get(key)
        if unit is None:
            break
        path.add(key)
        key = unit.parent_id
    return path
