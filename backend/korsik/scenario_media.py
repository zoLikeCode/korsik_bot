"""Scoped image gallery; built-in selection creates an ordinary validated private asset."""
import hashlib
import io
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, UploadFile
from .auth import Actor, get_actor
from .db import get_db
from .common import require_role, list_kind
from .assets import asset_public, can_asset, upload_asset

router = APIRouter()


def validated_materials(db, employee, ids):
    from .models import Entity
    from .retrieval import scope_matches
    if not isinstance(ids, list) or len(ids) > 10 or any(not isinstance(i, str) for i in ids):
        raise HTTPException(422, "Не больше 10 материалов")
    for id in ids:
        asset = db.get(Entity, id)
        if not asset or asset.kind != "asset" or asset.status not in {"READY", "APPROVED"} or asset.data.get("purpose") != "SCENARIO_MATERIAL" or not scope_matches(asset.data.get("scope", {}), employee):
            raise HTTPException(409, "Материал недоступен сотруднику")
    return list(dict.fromkeys(ids))


def validate_authored_materials(db, actor, graph):
    from .models import Entity
    for step in graph.get("steps", []):
        if not isinstance(step, dict):
            continue
        content = step.get("content")
        if not isinstance(content, dict):
            continue
        ids = content.get("asset_ids", [])
        if not isinstance(ids, list) or len(ids) > 10 or any(not isinstance(i, str) for i in ids):
            raise HTTPException(422, "Не больше 10 материалов в сообщении")
        for id in ids:
            asset = db.get(Entity, id)
            if not asset or asset.kind != "asset" or asset.data.get("purpose") != "SCENARIO_MATERIAL" or not can_asset(db, actor, asset):
                raise HTTPException(404, "Материал не найден")


@router.get("/scenario-materials/images")
def images(actor: Actor = Depends(get_actor), db=Depends(get_db)):
    require_role(actor, "HR_ADMIN", "HR_SPECIALIST")
    rows = [asset_public(asset) for asset in list_kind(db, "asset")
            if asset.data.get("purpose") == "SCENARIO_MATERIAL"
            and asset.data.get("content_type") in {"image/png", "image/jpeg"}
            and can_asset(db, actor, asset)]
    return {"items": rows, "builtins": [{"id": "korsik", "filename": "Корсик", "source_url": "/brand/korsik.png"}]}


@router.post("/scenario-materials/builtin", status_code=201)
def select_builtin(payload: dict, actor: Actor = Depends(get_actor), db=Depends(get_db)):
    require_role(actor, "HR_ADMIN", "HR_SPECIALIST")
    if payload != {"id": "korsik"}:
        raise HTTPException(422, "Неизвестное изображение")
    raw = (Path(__file__).parent / "resources" / "korsik.png").read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    # Existing same-scope import is reusable; upload validates actual image bytes.
    for asset in list_kind(db, "asset"):
        if asset.data.get("builtin") == "korsik" and asset.data.get("sha256") == digest and asset.data.get("scope") == {"employment_stages": ["PREBOARDING", "ACTIVE"], **actor.scope} and can_asset(db, actor, asset):
            return asset_public(asset)
    result = upload_asset(UploadFile(filename="Корсик.png", file=io.BytesIO(raw)), "SCENARIO_MATERIAL", actor, db)
    from .models import Entity
    from .domain import update
    from .common import commit
    asset = db.get(Entity, result["id"])
    update(asset, builtin="korsik")
    commit(db)
    return result
