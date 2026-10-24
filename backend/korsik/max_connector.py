"""MAX transport: durable inbox, narrow delivery API, no HR/domain SQL privileges."""

import asyncio, hashlib, hmac, json, logging, os, socket, ipaddress, time
from pathlib import Path
from urllib.parse import urlsplit
from contextlib import asynccontextmanager
import httpx
from fastapi import FastAPI, Request, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from .db import SessionLocal, now
from .models import TransportInbox, TransportDelivery, TransportCursor
from .settings import get_settings, read_secret

log = logging.getLogger("korsik.max")
API = get_settings().max_api_url.rstrip("/")
CORE = os.getenv("CORE_URL", "http://api:8000")
ALLOWED_TYPES = {
    "message_created",
    "message_callback",
    "bot_started",
    "bot_stopped",
    "dialog_removed",
}


def token():
    return read_secret(get_settings().max_token_file)


def core_headers():
    return {"X-Korsik-Transport-Key": read_secret(get_settings().internal_secret_file)}


def cursor_get(key):
    with SessionLocal() as db:
        c = db.get(TransportCursor, key)
        return dict(c.value) if c else {}


def cursor_put(key, value):
    with SessionLocal() as db:
        c = db.get(TransportCursor, key)
        if c:
            c.value = value
        else:
            db.add(TransportCursor(id=key, value=value))
        db.commit()


def update_id(event):
    # Stable across webhook retry and polling; transient staging metadata excluded.
    raw = json.dumps(
        {k: v for k, v in event.items() if not k.startswith("_")},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def safe_cdn_url(url):
    u = urlsplit(url)
    if u.scheme != "https" or u.username or u.password or u.port not in (None, 443):
        return False
    hostname = (u.hostname or "").lower()
    if not any(
        hostname == d or hostname.endswith("." + d)
        for d in ("max.ru", "oneme.ru", "okcdn.ru", "mycdn.me", "userapi.com")
    ):
        return False
    try:
        addresses = {
            r[4][0] for r in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        }
        return bool(addresses) and all(
            ipaddress.ip_address(a).is_global for a in addresses
        )
    except OSError:
        return False


async def stage_photos(event):
    attachments = ((event.get("message") or {}).get("body") or {}).get(
        "attachments"
    ) or []
    staged = []
    for attachment in attachments[:1]:
        if attachment.get("type") not in {"image", "photo"}:
            continue
        url = (attachment.get("payload") or {}).get("url")
        if not isinstance(url, str) or not await asyncio.to_thread(safe_cdn_url, url):
            continue
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
                async with client.stream("GET", url) as response:
                    if response.status_code != 200:
                        continue
                    data = bytearray()
                    async for part in response.aiter_bytes():
                        data.extend(part)
                        if len(data) > 10 * 1024 * 1024:
                            raise ValueError("image_too_large")
            from PIL import Image
            import io

            with Image.open(io.BytesIO(data)) as image:
                if image.width * image.height > 32_000_000 or image.format not in {
                    "JPEG",
                    "PNG",
                    "WEBP",
                }:
                    continue
                image.verify()
            digest = hashlib.sha256(data).hexdigest()
            sid = hashlib.sha256((update_id(event) + digest).encode()).hexdigest()
            root = Path(os.getenv("INBOUND_DIR", "/inbound"))
            root.mkdir(parents=True, exist_ok=True)
            path = root / sid
            if not path.exists():
                temporary = path.with_suffix(".tmp")
                temporary.write_bytes(data)
                temporary.chmod(0o640)
                temporary.replace(path)
            staged.append({"id": sid, "sha256": digest})
        except Exception as error:
            log.warning("incoming_photo_failed type=%s", type(error).__name__)
    return staged


async def persist_event(event):
    if not isinstance(event, dict) or event.get("update_type") not in ALLOWED_TYPES:
        return None
    eid = update_id(event)
    with SessionLocal() as db:
        if db.get(TransportInbox, eid):
            return eid
    payload = {k: v for k, v in event.items() if not k.startswith("_")}
    try:
        with SessionLocal() as db:
            needs_media = any(
                a.get("type") in {"image", "photo"}
                for a in (
                    ((event.get("message") or {}).get("body") or {}).get("attachments")
                    or []
                )
            )
            db.add(
                TransportInbox(
                    id=eid,
                    update_type=event["update_type"],
                    payload=payload,
                    status="MEDIA_PENDING" if needs_media else "PENDING",
                )
            )
            db.commit()
    except IntegrityError:
        pass
    return eid


async def media_loop():
    while True:
        try:
            with SessionLocal() as db:
                row = db.scalar(
                    select(TransportInbox)
                    .where(TransportInbox.status == "MEDIA_PENDING")
                    .order_by(TransportInbox.created_at)
                    .limit(1)
                )
                eid = row.id if row else None
                event = dict(row.payload) if row else None
            if not eid:
                await asyncio.sleep(0.5)
                continue
            staged = await stage_photos(event)
            with SessionLocal() as db:
                row = db.get(TransportInbox, eid)
                if row and row.status == "MEDIA_PENDING":
                    row.payload = {**row.payload, "_staged_attachments": staged}
                    row.status = "PENDING"
                    db.commit()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log.warning("media_stage_failed type=%s", type(error).__name__)
            await asyncio.sleep(2)


async def acknowledge_callback(event):
    callback = (event.get("callback") or {}).get("callback_id")
    if not callback:
        return
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                API + "/answers",
                params={"callback_id": callback},
                headers={"Authorization": token()},
                json={},
            )
    except Exception:
        pass  # Ack is cosmetic; domain operation is durable in inbox.


async def register_webhook(client, url):
    target = url.rstrip("/") + "/hooks/max"
    old = cursor_get("webhook-registration")
    # Delete only the URL this installation previously registered.
    if old.get("url") and old["url"] != target:
        r = await client.delete(
            API + "/subscriptions",
            params={"url": old["url"]},
            headers={"Authorization": token()},
        )
        if r.status_code not in {200, 404}:
            raise RuntimeError("previous_webhook_remove_failed")
    r = await client.post(
        API + "/subscriptions",
        headers={"Authorization": token()},
        json={
            "url": target,
            "update_types": sorted(ALLOWED_TYPES),
            "secret": read_secret(get_settings().max_webhook_secret_file),
        },
    )
    r.raise_for_status()
    value = r.json()
    if value.get("success") is False:
        raise RuntimeError("webhook_registration_rejected")
    cursor_put(
        "webhook-registration",
        {"url": target, "registered_at": now().isoformat(), "status": "REGISTERED"},
    )
    log.info("webhook_registered hostname=%s", urlsplit(url).hostname)


async def incoming_loop():
    mode = get_settings().max_mode
    if mode == "disabled":
        return
    async with httpx.AsyncClient(timeout=40) as client:
        previous = None
        last_checked = 0
        while True:
            try:
                if cursor_get("max-control").get("enabled") is False:
                    await asyncio.sleep(5)
                    continue
                if mode == "webhook":
                    path = Path(os.getenv("TUNNEL_URL_FILE", "/run/tunnel/public_url"))
                    url = os.getenv("MAX_PUBLIC_URL", "").strip() or (
                        path.read_text().strip() if path.is_file() else ""
                    )
                    u = urlsplit(url)
                    if (
                        u.scheme != "https"
                        or u.port is not None
                        or not u.hostname
                        or (
                            not os.getenv("MAX_PUBLIC_URL")
                            and not u.hostname.endswith(".trycloudflare.com")
                        )
                    ):
                        await asyncio.sleep(2)
                        continue
                    if url != previous or time.monotonic() - last_checked > 300:
                        await register_webhook(client, url)
                        previous = url
                        last_checked = time.monotonic()
                    await asyncio.sleep(5)
                elif mode == "polling":
                    subscriptions = await client.get(
                        API + "/subscriptions", headers={"Authorization": token()}
                    )
                    subscriptions.raise_for_status()
                    if subscriptions.json().get("subscriptions"):
                        raise RuntimeError("polling_forbidden_with_active_webhook")
                    state = cursor_get("max-poll-marker")
                    params = {
                        "timeout": 25,
                        "limit": 100,
                        "types": ",".join(sorted(ALLOWED_TYPES)),
                    }
                    if state.get("marker") is not None:
                        params["marker"] = state["marker"]
                    response = await client.get(
                        API + "/updates",
                        params=params,
                        headers={"Authorization": token()},
                    )
                    response.raise_for_status()
                    value = response.json()
                    for event in value.get("updates", []):
                        await persist_event(event)
                        await acknowledge_callback(event)
                    # Only advance after every event has committed to inbox.
                    cursor_put(
                        "max-poll-marker",
                        {
                            "marker": value.get("marker"),
                            "committed_at": now().isoformat(),
                        },
                    )
                else:
                    raise RuntimeError("unknown_max_mode")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.error("max_incoming_failed type=%s", type(error).__name__)
                await asyncio.sleep(10)


async def upload_asset(client, delivery, asset):
    aid = asset["asset_id"]
    key = "asset-token:" + aid
    cached = cursor_get(key)
    if cached.get("payload"):
        return {"type": cached["type"], "payload": cached["payload"]}
    result = await client.get(
        CORE + f"/internal/transport/deliveries/{delivery['delivery_id']}/assets/{aid}",
        headers=core_headers(),
        timeout=30,
    )
    result.raise_for_status()
    kind = "image" if asset["content_type"].startswith("image/") else "file"
    r = await client.post(
        API + "/uploads",
        params={"type": kind},
        headers={"Authorization": token()},
        timeout=10,
    )
    r.raise_for_status()
    allocation = r.json()
    url = allocation.get("url", "")
    if not await asyncio.to_thread(safe_cdn_url, url):
        raise RuntimeError("upload_host_not_allowed")
    r = await client.post(
        url,
        files={"data": (asset["filename"], result.content, asset["content_type"])},
        timeout=45,
    )
    r.raise_for_status()
    uploaded = r.json()
    if kind == "image" and uploaded.get("photos"):
        payload = {"photos": uploaded["photos"]}
    elif uploaded.get("token") or allocation.get("token"):
        payload = {"token": uploaded.get("token") or allocation["token"]}
    else:
        raise RuntimeError("upload_token_missing")
    cursor_put(key, {"type": kind, "payload": payload})
    return {"type": kind, "payload": payload}


async def receipt(client, delivery, status, mid=None, error=None):
    body = {
        "attempt_id": delivery["attempt_id"],
        "status": status,
        "platform_message_id": mid,
        "error_code": error,
    }
    # Retrying the receipt is safe; it does not resend the external message.
    for attempt in range(5):
        try:
            r = await client.post(
                CORE
                + f"/internal/transport/deliveries/{delivery['delivery_id']}/receipt",
                headers=core_headers(),
                json=body,
                timeout=5,
            )
            if r.status_code in {200, 409}:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(min(5, attempt + 1))


async def integration_test_loop():
    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            try:
                with SessionLocal() as db:
                    rows = db.scalars(
                        select(TransportCursor).where(
                            TransportCursor.id.like("integration-test:%")
                        )
                    ).all()
                    pending = [
                        (r.id, dict(r.value))
                        for r in rows
                        if r.value.get("status") == "PENDING"
                    ]
                for key, value in pending:
                    try:
                        response = await client.get(
                            API + "/me", headers={"Authorization": token()}
                        )
                        response.raise_for_status()
                        data = response.json()
                        result = {
                            **value,
                            "status": "PASSED",
                            "bot_id": data.get("user_id"),
                            "username": data.get("username"),
                            "checked_at": now().isoformat(),
                        }
                    except Exception as error:
                        result = {
                            **value,
                            "status": "FAILED",
                            "error_type": type(error).__name__,
                            "checked_at": now().isoformat(),
                        }
                    cursor_put(key, result)
                await asyncio.sleep(3)
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(5)


async def outgoing_loop():
    if get_settings().max_mode == "disabled":
        return
    last_sent = {}
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        while True:
            try:
                if cursor_get("max-control").get("enabled") is False:
                    await asyncio.sleep(5)
                    continue
                with SessionLocal() as db:
                    did = db.scalar(
                        select(TransportDelivery.id)
                        .where(
                            TransportDelivery.status.in_(["PENDING", "RETRY"]),
                            TransportDelivery.not_before <= now(),
                        )
                        .order_by(TransportDelivery.created_at)
                        .limit(1)
                    )
                if not did:
                    await asyncio.sleep(0.4)
                    continue
                r = await client.post(
                    CORE + f"/internal/transport/deliveries/{did}/claim",
                    headers=core_headers(),
                    timeout=10,
                )
                if r.status_code == 409:
                    await asyncio.sleep(0.2)
                    continue
                r.raise_for_status()
                delivery = r.json()
                body = delivery["body"]
                recipient = delivery["recipient"]
                key = str(recipient)
                # One worker and 0.6s/chat respects MAX's 2 messages/s/chat limit.
                await asyncio.sleep(
                    max(0, 0.6 - (time.monotonic() - last_sent.get(key, 0)))
                )
                try:
                    for asset in delivery.get("assets", []):
                        body.setdefault("attachments", []).append(
                            await upload_asset(client, delivery, asset)
                        )
                except Exception as error:
                    await receipt(
                        client,
                        delivery,
                        "RETRY",
                        error="attachment_" + type(error).__name__,
                    )
                    continue
                gate = await client.post(
                    CORE
                    + f"/internal/transport/deliveries/{delivery['delivery_id']}/validate",
                    headers=core_headers(),
                    json={"attempt_id": delivery["attempt_id"]},
                    timeout=5,
                )
                if gate.status_code != 200:
                    continue
                try:
                    r = await client.post(
                        API + "/messages",
                        params={**recipient, "disable_link_preview": "true"},
                        headers={"Authorization": token()},
                        json=body,
                        timeout=25,
                    )
                    last_sent[key] = time.monotonic()
                    if r.status_code in {429}:
                        await receipt(client, delivery, "RETRY", error="MAX_RATE_LIMIT")
                        continue
                    if r.status_code >= 500:
                        await receipt(
                            client, delivery, "UNKNOWN", error="MAX_SERVER_AMBIGUOUS"
                        )
                        continue
                    value = r.json()
                    if r.status_code >= 400 or value.get("success") is False:
                        code = str(value.get("code", "MAX_REJECTED"))[:80]
                        await receipt(
                            client,
                            delivery,
                            "RETRY" if code == "attachment.not.ready" else "REJECTED",
                            error=code,
                        )
                        continue
                    mid = (
                        ((value.get("message") or {}).get("body") or {}).get("mid")
                        or (value.get("message") or {}).get("mid")
                        or value.get("mid")
                    )
                    await receipt(
                        client, delivery, "ACCEPTED", mid=str(mid) if mid else None
                    )
                except (httpx.TimeoutException, httpx.NetworkError):
                    await receipt(
                        client, delivery, "UNKNOWN", error="MAX_NETWORK_AMBIGUOUS"
                    )
                except Exception as error:
                    await receipt(
                        client, delivery, "UNKNOWN", error=type(error).__name__
                    )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.error("max_outgoing_failed type=%s", type(error).__name__)
                await asyncio.sleep(3)


@asynccontextmanager
async def lifespan(app):
    tasks = [
        asyncio.create_task(incoming_loop()),
        asyncio.create_task(outgoing_loop()),
        asyncio.create_task(media_loop()),
        asyncio.create_task(integration_test_loop()),
    ]
    app.state.background_tasks = tasks
    yield
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(docs_url=None, openapi_url=None, lifespan=lifespan)


@app.get("/healthz")
def health():
    tasks = getattr(app.state, "background_tasks", [])
    if len(tasks) != 4 or any(task.done() for task in tasks):
        raise HTTPException(503, "background_task_unavailable")
    return {"status": "ok", "mode": get_settings().max_mode}


@app.post("/api/v1/integrations/max/webhook")
@app.post("/hooks/max")
async def webhook(request: Request):
    expected = read_secret(get_settings().max_webhook_secret_file)
    if not hmac.compare_digest(
        expected, request.headers.get("X-Max-Bot-Api-Secret", "")
    ):
        raise HTTPException(403, "forbidden")
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 1024 * 1024:
            raise HTTPException(413, "too_large")
    try:
        event = json.loads(data)
    except ValueError:
        raise HTTPException(400, "invalid_json")
    await persist_event(event)
    asyncio.create_task(acknowledge_callback(event))
    return {"ok": True}
