"""Isolated parser: bounded upload, NDJSON checkpoints, no database credentials."""
import base64
import errno
import os
from contextlib import asynccontextmanager
import json
import queue
import tempfile
import threading
from pathlib import Path
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from parsing import MAX_BYTES, parse_document, load_structural_parser

@asynccontextmanager
async def lifespan(app):
    if os.environ.get("PARSER_PROFILE") == "cpu_structure":
        load_structural_parser()
    yield

app = FastAPI(title="Korsik isolated offline parser", lifespan=lifespan)
BUSY = threading.Semaphore(1)


@app.get("/healthz")
def health():
    import shutil
    from parsing import STRUCTURAL_INFO
    return {"status": "READY" if shutil.which("tesseract") else "UNAVAILABLE", "parser": "korsik-parser-1", "network_required": False,
            "profile": os.environ.get("PARSER_PROFILE", "cpu_basic"), "structure": STRUCTURAL_INFO}


@app.post("/parse")
async def parse(file: UploadFile = File(...), skip_pages: str = Form("[]"), page_options: str = Form("{}")):
    try:
        retained = json.loads(skip_pages)
        if not isinstance(retained, list) or len(retained) > 500 or any(type(n) is not int or not 1 <= n <= 500 for n in retained):
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(422, "invalid_retained_pages")
    try:
        options = json.loads(page_options)
        if not isinstance(options, dict) or len(options) > 500:
            raise ValueError()
        for key, value in options.items():
            if not key.isdigit() or not 1 <= int(key) <= 500 or not isinstance(value, dict) or set(value) - {"rotation", "force_ocr"}:
                raise ValueError()
            if type(value.get("rotation", 0)) is not int or value.get("rotation", 0) not in {0, 90, 180, 270} or type(value.get("force_ocr", False)) is not bool:
                raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(422, "invalid_page_options")
    if not BUSY.acquire(blocking=False):
        raise HTTPException(429, "parser_busy")
    try:
        td = tempfile.TemporaryDirectory(prefix="korsik-parser-", dir="/tmp")
    except Exception as exc:
        # Admission must be returned even when temporary storage cannot create
        # its first directory; otherwise one full disk bricks this process.
        BUSY.release()
        if isinstance(exc, OSError) and exc.errno in {errno.ENOSPC, errno.EDQUOT}:
            raise HTTPException(507, "parser_storage_full") from exc
        raise
    folder = Path(td.name)
    source = folder / ("source" + Path(file.filename or "").suffix.lower())
    count = 0
    try:
        with source.open("wb") as out:
            while part := await file.read(1024 * 1024):
                count += len(part)
                if count > MAX_BYTES:
                    raise HTTPException(413, "source_too_large")
                out.write(part)
    except Exception:
        td.cleanup()
        BUSY.release()
        raise
    events = queue.Queue(maxsize=2)
    previews = set()
    stop = threading.Event()
    def emit(item):
        if stop.is_set():
            raise RuntimeError("parser_client_disconnected")
        raw = json.dumps(item, ensure_ascii=False)
        if len(raw.encode()) > 30 * 1024 * 1024:
            raise RuntimeError("canonical_checkpoint_size_limit")
        while not stop.is_set():
            try:
                events.put(raw + "\n", timeout=1)
                return
            except queue.Full:
                continue
        raise RuntimeError("parser_client_disconnected")
    def checkpoint(pages, blocks):
        images = {}
        for page in pages:
            name = page.get("preview_file")
            if name and name not in previews:
                data = (folder / "previews" / Path(name).name).read_bytes()
                if len(data) > 15 * 1024 * 1024:
                    raise ValueError("preview_size_limit")
                images[name] = base64.b64encode(data).decode()
                previews.add(name)
        emit({"type": "checkpoint", "pages": pages, "blocks": blocks, "previews": images})
    def worker():
        try:
            result = parse_document(source, folder / "previews", checkpoint, skip_pages=retained, page_options=options)
            emit({"type": "complete", "result": {k: v for k, v in result.items() if k not in {"pages", "blocks"}}})
        except Exception as exc:
            try:
                emit({"type": "error", "error": f"{type(exc).__name__}: {str(exc)[:500]}"})
            except Exception:
                pass
        finally:
            while not stop.is_set():
                try:
                    events.put(None, timeout=1)
                    break
                except queue.Full:
                    pass
            BUSY.release()
    threading.Thread(target=worker, daemon=True).start()
    def stream():
        try:
            while True:
                value = events.get()
                if value is None:
                    break
                yield value
        finally:
            stop.set()
            td.cleanup()
    return StreamingResponse(stream(), media_type="application/x-ndjson", headers={"Cache-Control": "no-store"})
