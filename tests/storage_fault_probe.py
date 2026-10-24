"""Actual ENOSPC on a tiny disposable tmpfs; never fills a live data volume."""

import asyncio
from io import BytesIO
import json
import os
from pathlib import Path
from fastapi import HTTPException, Request, Response, UploadFile
from sqlalchemy import select, func
from korsik import knowledge as kb
from korsik.auth import user_actor
from korsik.db import SessionLocal
from korsik.models import Entity, User
from korsik.settings import get_settings

database = get_settings().database_name
if not database.startswith("korsik_offline_"):
    raise RuntimeError("ISOLATED_OFFLINE_DATABASE_REQUIRED")
root = Path("/fault")
if (
    not root.is_dir()
    or os.environ.get("KORSIK_STORAGE_FAULT_ALLOWED") != "ISOLATED_TMPFS_ONLY"
):
    raise RuntimeError("EXPLICIT_DISPOSABLE_TMPFS_REQUIRED")
mounts = [
    line.split(" - ", 1)
    for line in Path("/proc/self/mountinfo").read_text().splitlines()
]
if not any(
    len(parts) == 2
    and parts[0].split()[4] == "/fault"
    and parts[1].split()[0] == "tmpfs"
    for parts in mounts
):
    raise RuntimeError("FAULT_MOUNT_MUST_BE_TMPFS")
filesystem_bytes = os.statvfs(root).f_blocks * os.statvfs(root).f_frsize
if not 0 < filesystem_bytes <= 2 * 1024 * 1024:
    raise RuntimeError("FAULT_TMPFS_MUST_BE_AT_MOST_2_MIB")
original_data = kb.DATA
kb.DATA = root
with SessionLocal() as db:
    admin = db.scalar(
        select(User).where(User.role == "HR_ADMIN", User.enabled.is_(True))
    )
    actor = user_actor(admin)
    doc = kb.create_document(
        {
            "title": "Synthetic ENOSPC test",
            "document_category": "REGULATION",
            "scope": {"site_ids": ["GrPP"], "employment_stages": ["ACTIVE"]},
        },
        Response(),
        actor,
        db,
    )
    head = db.scalar(select(Entity).where(Entity.kind == "knowledge_head"))
    snapshot_id = head.data["snapshot_id"]
    filler = root / "fill.tmp"
    written = 0
    with filler.open("wb", buffering=0) as output:
        try:
            while True:
                output.write(b"x" * 4096)
                written += 4096
        except OSError as exc:
            if exc.errno != 28:
                raise
    request = Request(
        {"type": "http", "headers": [(b"if-match", doc["etag"].encode())]}
    )

    async def upload():
        return await kb.upload_revision(
            doc["id"],
            request,
            Response(),
            UploadFile(filename="synthetic.txt", file=BytesIO(b"a" * 50000)),
            reason="Synthetic disk-full proof",
            effective_from="",
            effective_to="",
            expected_sha256="",
            force_new_version=False,
            actor=actor,
            db=db,
        )

    status = None
    try:
        asyncio.run(upload())
    except HTTPException as exc:
        status = exc.status_code
    db.rollback()
    db.refresh(head)
    failed_revision_count = db.scalar(
        select(func.count(Entity.id)).where(
            Entity.kind == "document_revision", Entity.parent_id == doc["id"]
        )
    )
    preserved = head.data["snapshot_id"] == snapshot_id and failed_revision_count == 0
    filler.unlink(missing_ok=True)
    result = asyncio.run(upload())
    db.refresh(head)
    recovered = (
        bool(result.get("revision", {}).get("id"))
        and head.data["snapshot_id"] == snapshot_id
    )
    report = {
        "schema": "korsik-actual-enospc-v1",
        "database": database,
        "filesystem": "1MiB disposable tmpfs /fault",
        "verified_tmpfs_capacity_bytes": filesystem_bytes,
        "filled_bytes_before_real_ENOSPC": written,
        "controlled_http_status": status,
        "published_snapshot_preserved": preserved,
        "partial_revision_not_persisted": failed_revision_count == 0,
        "retry_after_space_release_succeeds": recovered,
        "external_MAX_sends": 0,
        "status": "PASS" if status == 507 and preserved and recovered else "FAIL",
    }
    Path("/operator/storage-fault-report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)
    if report["status"] != "PASS":
        raise SystemExit(1)
kb.DATA = original_data
