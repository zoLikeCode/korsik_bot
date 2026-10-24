"""Scoped, versioned base questions shared by greetings and the knowledge UI."""

from copy import deepcopy
import hashlib
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, text

from .auth import get_actor
from .common import audit, commit, etag, list_kind, require_role, save_entity
from .db import get_db, new_id
from .models import Entity

router = APIRouter()
DEFAULT_ITEMS = [
    {
        "id": "food",
        "label": "Где пообедать?",
        "query_text": "Где можно пообедать на моей площадке?",
    },
    {
        "id": "confidentiality",
        "label": "Коммерческая тайна",
        "query_text": "Что относится к коммерческой тайне компании?",
    },
    {
        "id": "anticorruption",
        "label": "Антикоррупционная политика",
        "query_text": "Какие основные антикоррупционные правила действуют в компании?",
    },
    {
        "id": "quality",
        "label": "Качество и экология",
        "query_text": "Какие требования к качеству и экологии действуют в компании?",
    },
]


class Formulation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(
        default_factory=lambda: "p_" + uuid.uuid4().hex,
        min_length=1,
        max_length=120,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    label: str = Field(min_length=1, max_length=60)
    query_text: str = Field(min_length=1, max_length=2000)

    @field_validator("label", "query_text", mode="before")
    @classmethod
    def trimmed(cls, value):
        return value.strip() if isinstance(value, str) else value


class BasicFormulationsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[Formulation] = Field(min_length=1, max_length=8)

    @field_validator("items")
    @classmethod
    def unique(cls, items):
        if len({item.id for item in items}) != len(items):
            raise ValueError("ID формулировок должны быть уникальны")
        return items


def _scope(actor):
    scope = actor.scope or {}
    if scope.get("all") is True:
        return {"all": True}
    result = {
        key: sorted(set(scope.get(key) or []))
        for key in ("site_ids", "legal_entity_ids")
        if scope.get(key)
    }
    if not result:
        raise HTTPException(
            403,
            "Для настройки формулировок нужна область площадки или юридического лица",
        )
    return result


def _config_id(scope):
    key = json.dumps(scope, sort_keys=True, separators=(",", ":"))
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "korsik:basic-formulations:" + key))


def _default_revision(scope):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, _config_id(scope) + ":defaults-v2"))


def _view(row, scope, actor=None):
    return {
        "id": row.id if row else _config_id(scope),
        "menu_id": row.id if row else _config_id(scope),
        "menu_version_id": row.data["revision_id"] if row else _default_revision(scope),
        "scope": scope,
        "items": deepcopy(row.data["items"] if row else DEFAULT_ITEMS),
        "configured": row is not None,
        "can_edit": actor is None or actor.role in {"HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT"},
    }


@router.get("/knowledge/basic-formulations")
def get_basic_formulations(actor=Depends(get_actor), db=Depends(get_db)):
    require_role(actor, "HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT")
    scope = _scope(actor)
    row = db.get(Entity, _config_id(scope))
    return JSONResponse(
        _view(row, scope, actor),
        headers={"ETag": etag(row) if row else f'"{_config_id(scope)}:0"'},
    )


@router.put("/knowledge/basic-formulations")
def put_basic_formulations(
    body: BasicFormulationsInput,
    request: Request,
    actor=Depends(get_actor),
    db=Depends(get_db),
):
    require_role(actor, "HR_ADMIN", "HR_SPECIALIST")
    scope = _scope(actor)
    config_id = _config_id(scope)
    # Serialize first publication too: its deterministic ID has no row to lock yet.
    if db.bind.dialect.name == "postgresql":
        key = int.from_bytes(hashlib.sha256(config_id.encode()).digest()[:8], "big") % (
            2**63
        )
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
    row = db.scalar(
        select(Entity)
        .where(Entity.id == config_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    expected = etag(row) if row else f'"{config_id}:0"'
    supplied = request.headers.get("if-match")
    if not supplied:
        raise HTTPException(428, "Обновите формулировки перед сохранением")
    if supplied != expected:
        raise HTTPException(412, "Формулировки изменились. Обновите страницу")
    revision = new_id()
    data = {
        "scope": scope,
        "items": [item.model_dump() for item in body.items],
        "revision_id": revision,
    }
    if row:
        row.data = data
    else:
        row = save_entity(
            db, "basic_formulations", data, status="PUBLISHED", id=config_id
        )
    save_entity(
        db,
        "basic_formulations_revision",
        data,
        status="PUBLISHED",
        parent_id=config_id,
        id=revision,
    )
    audit(
        db,
        actor,
        "knowledge.basic_formulations.save",
        config_id,
        {"revision_id": revision, "count": len(body.items)},
    )
    commit(db)
    return JSONResponse(_view(row, scope, actor), headers={"ETag": etag(row)})


def _matches(scope, employee):
    if scope.get("all") is True:
        return True
    return bool(scope.get("site_ids") or scope.get("legal_entity_ids")) and all(
        not scope.get(key) or employee.data.get(field) in scope[key]
        for key, field in (
            ("site_ids", "site_id"),
            ("legal_entity_ids", "legal_entity_id"),
        )
    )


def basic_prompts_for(db, employee):
    rows = [
        row
        for row in list_kind(db, "basic_formulations", status="PUBLISHED")
        if _matches(row.data.get("scope", {}), employee)
    ]
    # A branch configuration takes precedence over the company default.
    rows.sort(
        key=lambda row: (
            not row.data["scope"].get("all", False),
            bool(row.data["scope"].get("site_ids")),
            row.updated_at,
        ),
        reverse=True,
    )
    row = rows[0] if rows else None
    scope = row.data["scope"] if row else {"all": True}
    result = _view(row, scope)
    result["items"] = [
        {
            **item,
            "prompt_key": item["id"],
            "action": {
                "kind": "RAG_QUERY",
                "menu_id": result["menu_id"],
                "menu_version_id": result["menu_version_id"],
                "prompt_key": item["id"],
            },
        }
        for item in result["items"]
    ]
    result["configuration_state"] = "BASIC_FORMULATIONS"
    return result


def simple_basic_prompt_buttons(db, employee):
    return [
        {"text": item["label"], "payload": item["action"]}
        for item in basic_prompts_for(db, employee)["items"]
    ]
