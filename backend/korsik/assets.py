"""Private assets; an uploaded UUID is never a publication permission."""

import hashlib, io, os, zipfile
from pathlib import Path
from sqlalchemy import select
from fastapi import APIRouter, UploadFile, File, Form, Depends, HTTPException
from fastapi.responses import FileResponse
from .auth import get_actor, Actor
from .db import get_db, new_id
from .models import Entity
from .common import (
    get_entity,
    save_entity,
    list_kind,
    require_role,
    audit,
    commit,
    actor_can_employee,
    employee_is_enabled,
)
from .settings import get_settings

router = APIRouter()


def asset_public(asset):
    return {
        "id": asset.id,
        "asset_id": asset.id,
        "filename": asset.data.get("filename"),
        "content_type": asset.data.get("content_type"),
        "size": asset.data.get("size"),
        "status": asset.status,
        "source_url": f"/api/v1/assets/{asset.id}/content",
    }


def can_asset(db, actor, asset):
    if actor.role == "EMPLOYEE":
        current = (
            db.scalar(
                select(Entity)
                .where(Entity.id == actor.employee_id, Entity.kind == "employee")
                .execution_options(populate_existing=True)
            ) if actor.employee_id else None
        )
        if not employee_is_enabled(current):
            return False
    if asset.status not in {"READY", "APPROVED"}:
        return False
    employee_id = asset.data.get("employee_id")
    if asset.data.get("knowledge_revision_id"):
        from .retrieval import published_corpus

        employee = db.get(Entity, employee_id)
        if not employee or asset.data["knowledge_revision_id"] not in published_corpus(
            db, employee
        ):
            return False
    if employee_id:
        if actor.role == "EMPLOYEE":
            employee = db.scalar(
                select(Entity)
                .where(Entity.id == employee_id, Entity.kind == "employee")
                .execution_options(populate_existing=True)
            )
            if not employee_is_enabled(employee) or employee.data.get("employment_stage") == "ENDED":
                return False
            if asset.data.get("episode_id") and asset.data[
                "episode_id"
            ] != employee.data.get("episode_id"):
                return False
            if asset.data.get("conversation_id"):
                from .chat import can_conversation

                conversation = db.get(Entity, asset.data["conversation_id"])
                if (
                    not conversation
                    or conversation.kind != "conversation"
                    or not can_conversation(db, actor, conversation)
                ):
                    return False
            elif not asset.data.get("episode_id") and not asset.data.get(
                "knowledge_revision_id"
            ):
                return False
        return actor_can_employee(db, actor, employee_id) and actor.role in {
            "EMPLOYEE",
            "HR_ADMIN",
            "HR_SPECIALIST",
            "HEAD_TALENT",
        }
    if actor.role in {"HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT"}:
        from .knowledge import _scope_access

        # Authorship does not retain access after an HR employee's scope changes.
        # Use the same site AND legal-entity checks as the knowledge library.
        scope_probe = Entity(
            kind="document",
            status="ACTIVE",
            data={"scope": asset.data.get("scope", {})},
        )
        return _scope_access(actor, scope_probe)
    if actor.role != "EMPLOYEE" or not actor.employee_id:
        return False
    employee = db.get(Entity, actor.employee_id)
    if not employee or employee.data.get("employment_stage") == "ENDED":
        return False
    from .retrieval import scope_matches

    if not scope_matches(asset.data.get("scope", {}), employee):
        return False
    from .chat import _eligible

    for journey in list_kind(db, "journey", parent_id=employee.id):
        if journey.status == "CANCELLED" or journey.data.get(
            "episode_id"
        ) != employee.data.get("episode_id"):
            continue
        version = db.get(
            Entity,
            journey.data.get("template_version_id")
            or journey.data.get("version_id")
            or journey.data.get("scenario_version_id", ""),
        )
        if not version:
            continue
        for step in version.data.get("graph", {}).get("steps", []):
            if asset.id in step.get("content", {}).get("asset_ids", []) and _eligible(
                step, employee
            ):
                return True
    return False


@router.post("/assets", status_code=201)
def upload_asset(
    file: UploadFile = File(...),
    purpose: str = Form("SCENARIO_MATERIAL"),
    actor: Actor = Depends(get_actor),
    db=Depends(get_db),
):
    require_role(actor, "HR_ADMIN", "HR_SPECIALIST")
    if not (
        actor.scope.get("all") is True
        or actor.scope.get("site_ids")
        or actor.scope.get("legal_entity_ids")
    ):
        raise HTTPException(403, detail="scope_not_granted")
    if purpose != "SCENARIO_MATERIAL":
        raise HTTPException(422, detail="Выберите назначение SCENARIO_MATERIAL")
    raw = file.file.read(50 * 1024 * 1024 + 1)
    if not raw or len(raw) > 50 * 1024 * 1024:
        raise HTTPException(413, detail="Размер файла должен быть от1Б до50МБ")
    name = Path(file.filename or "material").name[:255]
    suffix = Path(name).suffix.lower()
    mime = {
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".txt": "text/plain",
    }
    if suffix not in mime:
        raise HTTPException(415, detail="Поддерживаются PDF,PPTX,PNG,JPG,TXT")
    if suffix == ".pdf" and not raw.startswith(b"%PDF-"):
        raise HTTPException(422, detail="Неверный PDF")
    if suffix in {".png", ".jpg", ".jpeg"}:
        from PIL import Image

        try:
            Image.MAX_IMAGE_PIXELS = 30_000_000
            with Image.open(io.BytesIO(raw)) as image:
                image.verify()
        except Exception:
            raise HTTPException(
                422, detail="Повреждённое или слишком большое изображение"
            )
    if suffix == ".pptx":
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                if (
                    "[Content_Types].xml" not in z.namelist()
                    or len(z.infolist()) > 10000
                    or sum(f.file_size for f in z.infolist()) > 250 * 1024 * 1024
                ):
                    raise ValueError()
                if any(
                    ".." in Path(f.filename).parts or Path(f.filename).is_absolute()
                    for f in z.infolist()
                ):
                    raise ValueError()
        except Exception:
            raise HTTPException(422, detail="Неверный или слишком большой PPTX")
    if suffix == ".txt":
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(422, detail="Текст должен быть UTF-8")
    aid = new_id()
    root = Path(get_settings().blob_root) / "assets"
    root.mkdir(parents=True, exist_ok=True)
    path = root / aid
    path.write_bytes(raw)
    os.chmod(path, 0o600)
    asset = save_entity(
        db,
        "asset",
        {
            "filename": name,
            "content_type": mime[suffix],
            "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "storage_path": str(path),
            "purpose": purpose,
            "scope": {"employment_stages": ["PREBOARDING", "ACTIVE"], **actor.scope},
            "validated": True,
        },
        status="READY",
        owner_id=actor.id,
        id=aid,
    )
    audit(db, actor, "asset.upload", aid, {"bytes": len(raw), "purpose": purpose})
    commit(db)
    return asset_public(asset)


@router.get("/assets/{asset_id}")
def get_asset(asset_id: str, actor: Actor = Depends(get_actor), db=Depends(get_db)):
    asset = get_entity(db, asset_id, "asset")
    if not can_asset(db, actor, asset):
        raise HTTPException(404, detail="Материал не найден")
    return asset_public(asset)


@router.get("/assets/{asset_id}/content")
def asset_content(asset_id: str, actor: Actor = Depends(get_actor), db=Depends(get_db)):
    asset = get_entity(db, asset_id, "asset")
    if not can_asset(db, actor, asset):
        raise HTTPException(404, detail="Материал не найден")
    path = Path(asset.data.get("storage_path", ""))
    root = Path(get_settings().blob_root).resolve()
    if not path.is_file() or not path.resolve().is_relative_to(root):
        raise HTTPException(404, detail="Материал недоступен")
    return FileResponse(
        path,
        filename=asset.data.get("filename", "file"),
        media_type=asset.data.get("content_type", "application/octet-stream"),
    )


@router.get("/citations/{citation_id}")
def citation(citation_id: str, actor: Actor = Depends(get_actor), db=Depends(get_db)):
    c = get_entity(db, citation_id, "citation")
    _authorize_citation(db, actor, c)
    return {
        k: v for k, v in c.data.items() if k not in {"employee_id", "conversation_id"}
    }


def _authorize_citation(db, actor, c):
    if not actor_can_employee(
        db, actor, c.data.get("employee_id")
    ) or actor.role not in {"EMPLOYEE", "HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT"}:
        raise HTTPException(404, detail="Источник не найден")
    employee = get_entity(db, c.data["employee_id"], "employee")
    from .retrieval import published_corpus

    if c.data.get("revision_id") not in published_corpus(db, employee):
        raise HTTPException(410, detail="Источник отозван или недоступен")
    return employee


@router.get("/citations/{citation_id}/source")
def citation_source(
    citation_id: str, actor: Actor = Depends(get_actor), db=Depends(get_db)
):
    c = get_entity(db, citation_id, "citation")
    _authorize_citation(db, actor, c)
    revision = get_entity(db, c.data["revision_id"], "document_revision")
    # Exact revision; no redirect to a mutable latest file.
    from .knowledge import get_source, _page_preview_response

    if c.data.get("kind") == "VISUAL":
        extraction = get_entity(db, c.data.get("extraction_revision_id", ""), "extraction")
        return _page_preview_response(revision, extraction, int(c.data.get("page_no", 1)))
    return get_source(revision.id, actor, db)


def citation_attachment(db, employee, citation, original=False):
    """Private, exact revision/page material for MAX without public download links."""
    import uuid
    from .retrieval import published_corpus
    from .knowledge import DATA

    permitted = published_corpus(db, employee)
    rid = citation.get("revision_id")
    if rid not in permitted:
        return None
    doc, revision = permitted[rid]
    root = Path(DATA) / "knowledge" / rid
    ext = db.get(Entity, citation.get("extraction_revision_id"))
    path = None
    filename = None
    mime = None
    page_no = citation.get("page_no", 1)
    if not original and ext and ext.kind == "extraction" and ext.parent_id == rid:
        page = next(
            (p for p in ext.data.get("pages", []) if p["page_no"] == page_no), None
        )
        if page and page.get("preview_file"):
            candidate = root / "previews" / page["preview_file"]
            if candidate.is_file() and candidate.resolve().is_relative_to(
                (root / "previews").resolve()
            ):
                path = candidate
                filename = f"{doc.data['title'][:100]} — стр. {page_no}.png"
                mime = "image/png"
    if original:
        candidate = Path(revision.data.get("source_path", ""))
        if candidate.is_file() and candidate.resolve().is_relative_to(root.resolve()):
            path = candidate
            filename = (
                revision.data.get("filename") or doc.data["title"] + candidate.suffix
            )
            mime = {
                ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                ".pdf": "application/pdf",
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
            }.get(candidate.suffix.lower(), "text/plain")
    key = f"citation-asset:{employee.id}:{rid}:{citation.get('extraction_revision_id')}:{'original' if original else page_no}"
    aid = str(uuid.uuid5(uuid.NAMESPACE_URL, key))
    old = db.get(Entity, aid)
    if old:
        return old
    if not path and not original:
        text = citation.get("evidence_text", "")
        if not text:
            return None
        path = Path(get_settings().blob_root) / "citation-extracts" / aid
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{doc.data['title']}\nСтраница/раздел: {page_no}\n\n{text}")
        path.chmod(0o600)
        filename = f"Выдержка — {doc.data['title'][:100]} — {page_no}.txt"
        mime = "text/plain"
    if not path:
        return None
    return save_entity(
        db,
        "asset",
        {
            "employee_id": employee.id,
            "knowledge_document_id": doc.id,
            "knowledge_revision_id": rid,
            "extraction_revision_id": citation.get("extraction_revision_id"),
            "page_no": page_no,
            "purpose": "CITATION_SOURCE" if original else "CITATION_PAGE",
            "filename": filename,
            "content_type": mime,
            "storage_path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size": path.stat().st_size,
        },
        status="READY",
        parent_id=rid,
        owner_id=employee.id,
        id=aid,
    )


def citation_pages_for_answer(db, employee, answer):
    result = []
    seen = set()
    for citation in answer.data.get("citations", []):
        key = (citation.get("revision_id"), citation.get("page_no"))
        if key in seen:
            continue
        seen.add(key)
        asset = citation_attachment(db, employee, citation)
        if asset:
            result.append(asset.id)
        if len(result) >= 3:
            break
    return result


def queue_original_source(db, employee, conversation, payload, event_id):
    from .common import queue_message
    from .retrieval import answer_still_authorized

    answer = get_entity(db, payload.get("answer_id", ""), "answer")
    if answer.parent_id != conversation.id or not answer_still_authorized(
        db, conversation, answer.data
    ):
        raise HTTPException(410, "Источник уже отозван или недоступен")
    citation = next(
        (
            c
            for c in answer.data.get("citations", [])
            if c.get("revision_id") == payload.get("revision_id")
        ),
        None,
    )
    if not citation:
        raise HTTPException(404, "Источник не относится к этому ответу")
    asset = citation_attachment(db, employee, citation, original=True)
    if not asset:
        raise HTTPException(
            409, "Оригинал доступен в кабинете; сейчас не удалось подготовить вложение"
        )
    queue_message(
        db,
        "EMPLOYEE",
        employee.id,
        "Оригинал источника: "
        + citation.get("document_title", "Документ")
        + ". Подтверждающая страница: "
        + str(citation.get("page_no", "")),
        f"original-source:{event_id}",
        attachments=[asset.id],
        data={"answer_id": answer.id, "conversation_id": conversation.id},
    )
    return {"status": "QUEUED"}
