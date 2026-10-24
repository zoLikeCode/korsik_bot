import json, logging, hashlib, secrets
from contextlib import asynccontextmanager
from urllib.parse import urlparse
import hmac
from html import escape
from fastapi import FastAPI, HTTPException, Depends
from fastapi.responses import JSONResponse, HTMLResponse
from starlette.middleware.base import BaseHTTPMiddleware
from sqlalchemy import select, text
from . import __version__
from .settings import get_settings, read_secret
from .db import SessionLocal, now
from .models import Entity, Job, IdempotencyRecord
from .auth import (
    router as auth_router,
    get_actor,
    session_user,
    user_actor,
    hash_token,
    Actor,
)
from .common import list_kind, require_role

log = logging.getLogger("korsik")


@asynccontextmanager
async def lifespan(app):
    s = get_settings()
    if s.testing and not s.database_url.startswith("sqlite"):
        raise RuntimeError("Test mode cannot use production PostgreSQL")
    if s.product_mode == "FULL" and not s.generation_enabled:
        raise RuntimeError("FULL requires generation_enabled")
    if not s.testing:
        read_secret(s.secret_key_file)
    yield


app = FastAPI(
    title="Корсик · рабочий API",
    version=__version__,
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url="/api/openapi.json",
)


@app.get("/api/docs", include_in_schema=False, response_class=HTMLResponse)
def local_api_catalogue():
    """A server-rendered API catalogue works with no JavaScript or internet."""
    schema = app.openapi()
    rows = []
    for path, operations in sorted(schema.get("paths", {}).items()):
        for method in (
            "get",
            "post",
            "put",
            "patch",
            "delete",
            "head",
            "options",
            "trace",
        ):
            operation = operations.get(method)
            if not isinstance(operation, dict):
                continue
            summary = operation.get("summary") or operation.get("operationId") or ""
            rows.append(
                "<tr><td><strong>"
                + escape(method.upper())
                + "</strong></td><td><code>"
                + escape(str(path))
                + "</code></td><td>"
                + escape(str(summary))
                + "</td></tr>"
            )
    title = str(schema.get("info", {}).get("title", "Корсик · API"))
    version = str(schema.get("info", {}).get("version", ""))
    serialized = escape(json.dumps(schema, ensure_ascii=False, indent=2))
    return HTMLResponse(
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>" + escape(title) + "</title></head><body><main>"
        "<h1>" + escape(title) + "</h1><p>Версия: " + escape(version) + "</p>"
        "<p>Локальный каталог фактических HTTP-маршрутов. Выполнение команд "
        "требует авторизации и предусмотренных API защит.</p>"
        '<p><a href="/api/openapi.json" download="korsik-openapi.json">Скачать OpenAPI JSON</a></p>'
        "<table><caption>Маршруты API</caption><thead><tr><th>Метод</th><th>Путь</th>"
        "<th>Описание</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
        "<details><summary>Полная схема OpenAPI</summary><pre>"
        + serialized
        + "</pre></details></main></body></html>"
    )


class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        request_id = secrets.token_hex(12)
        request.state.request_id = request_id
        path = request.url.path
        method = request.method.upper()
        is_mutation = method not in {"GET", "HEAD", "OPTIONS"}
        public = path in {
            "/healthz",
            "/readyz",
            "/api/v1/auth/login",
            "/api/v1/auth/max-miniapp",
            "/api/v1/auth/config",
            "/api/v1/auth/password-reset/request",
            "/api/v1/auth/password-reset/confirm",
        } or path.startswith("/internal/transport/")
        body = None
        idem_id = None
        idem_fingerprint = None
        if path.startswith("/api/v1/") and not public:
            with SessionLocal() as db:
                user, session = session_user(request, db)
                if user:
                    request.state.actor = user_actor(user)
                    if is_mutation:
                        supplied = request.headers.get("X-CSRF-Token", "")
                        if not supplied or not hmac.compare_digest(
                            hash_token(supplied), session.csrf_hash
                        ):
                            return JSONResponse(
                                {
                                    "detail": {
                                        "code": "csrf_invalid",
                                        "message": "Обновите страницу и повторите запрос",
                                    }
                                },
                                403,
                            )
                elif not (
                    get_settings().testing and get_actor in app.dependency_overrides
                ):
                    return JSONResponse(
                        {
                            "detail": {
                                "code": "authentication_required",
                                "message": "Войдите в кабинет",
                            }
                        },
                        401,
                    )
        if is_mutation:
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                return JSONResponse(
                    {
                        "detail": {
                            "code": "origin_mismatch",
                            "message": "Недопустимый источник запроса",
                        }
                    },
                    403,
                )
        # Persist replay results for non-streaming JSON commands. The command's
        # own SQL keys/ETags protect crashes between domain commit and this cache.
        if (
            is_mutation
            and path.startswith("/api/v1/")
            and path
            not in {
                "/api/v1/auth/login",
                "/api/v1/auth/max-miniapp",
                "/api/v1/auth/password-reset/request",
                "/api/v1/auth/password-reset/confirm",
            }
            and "application/json" in request.headers.get("content-type", "")
        ):
            key = request.headers.get("Idempotency-Key")
            if not key or len(key) > 128:
                return JSONResponse(
                    {
                        "detail": {
                            "code": "idempotency_key_required",
                            "message": "Требуется Idempotency-Key",
                        }
                    },
                    428,
                )
            body = await request.body()
            if len(body) > 2 * 1024 * 1024:
                return JSONResponse({"detail": "Запрос слишком большой"}, 413)
            actor = getattr(request.state, "actor", None)
            who = getattr(actor, "id", "test")
            idem_id = hashlib.sha256(
                f"{who}:{method}:{path}:{key}".encode()
            ).hexdigest()
            idem_fingerprint = hashlib.sha256(body).hexdigest()
            with SessionLocal() as db:
                old = db.get(IdempotencyRecord, idem_id)
                if old:
                    if old.fingerprint != idem_fingerprint:
                        return JSONResponse(
                            {
                                "detail": {
                                    "code": "idempotency_conflict",
                                    "message": "Ключ повторён с другим содержимым",
                                }
                            },
                            409,
                        )
                    if old.status_code == 102:
                        return JSONResponse(
                            {
                                "detail": {
                                    "code": "command_in_progress",
                                    "message": "Команда уже принята; обновите результат перед повтором",
                                }
                            },
                            409,
                        )
                    headers = {"X-Idempotent-Replay": "true"}
                    if isinstance(old.response, dict) and old.response.get("etag"):
                        headers["ETag"] = old.response["etag"]
                    return JSONResponse(
                        old.response, status_code=old.status_code, headers=headers
                    )
                try:
                    db.add(
                        IdempotencyRecord(
                            key=idem_id,
                            fingerprint=idem_fingerprint,
                            status_code=102,
                            response={"state": "PROCESSING"},
                        )
                    )
                    db.commit()
                except Exception:
                    db.rollback()
                    return JSONResponse(
                        {
                            "detail": {
                                "code": "command_in_progress",
                                "message": "Команда уже обрабатывается",
                            }
                        },
                        409,
                    )
        try:
            response = await call_next(request)
            if (
                idem_id
                and 200 <= response.status_code < 500
                and "application/json" in response.headers.get("content-type", "")
            ):
                chunks = [part async for part in response.body_iterator]
                raw = b"".join(chunks)
                if len(raw) < 2 * 1024 * 1024:
                    try:
                        value = json.loads(raw)
                        with SessionLocal() as db:
                            record = db.get(IdempotencyRecord, idem_id)
                            if record:
                                record.status_code = response.status_code
                                record.response = value
                            db.commit()
                    except Exception:
                        log.warning(
                            "idempotency_result_store_failed request=%s", request_id
                        )

                async def iterator():
                    yield raw

                response.body_iterator = iterator()
            response.headers["X-Request-Id"] = request_id
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "same-origin"
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Frame-Options"] = "SAMEORIGIN"
            return response
        except Exception as error:
            import traceback

            frames = [
                (f.name, f.lineno) for f in traceback.extract_tb(error.__traceback__)
            ]
            log.error(
                "request_failed id=%s path=%s type=%s frames=%s",
                request_id,
                path,
                type(error).__name__,
                frames,
            )
            return JSONResponse(
                {
                    "detail": {
                        "code": "internal_error",
                        "message": "Ошибка сервера. Повторите запрос",
                        "request_id": request_id,
                    }
                },
                500,
            )


app.add_middleware(SecurityMiddleware)


@app.get("/healthz")
def healthz():
    return {"status": "ok", "service": "korsik", "version": __version__}


@app.get("/readyz")
def readyz():
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
        return {"status": "ready", "database": "OK"}
    except Exception:
        return JSONResponse({"status": "not_ready"}, 503)


@app.get("/api/v1/system/status")
def system_status(actor: Actor = Depends(get_actor)):
    require_role(actor, "HR_ADMIN", "PLATFORM_ADMIN")
    with SessionLocal() as db:
        states = {
            e.data.get("service"): e.data for e in list_kind(db, "service_health")
        }
        head = next(iter(list_kind(db, "knowledge_head")), None)
        snapshot = db.get(Entity, head.data.get("snapshot_id")) if head else None
        generator = states.get("generator", {}).get("ready", False)
        corpus = bool(snapshot and snapshot.data.get("members"))
        from .local_ai import active_profile, generation_policy, active_identity_matches

        profile = active_profile(db)
        policy = generation_policy(db)
        mode = get_settings().product_mode
        ready = (
            "CORE_READY"
            if mode == "CORE"
            else (
                "SETUP_REQUIRED"
                if not corpus or policy != "LOCAL_GENERATION"
                else (
                    "FULL_READY"
                    if generator and active_identity_matches(db, states)
                    else "DEGRADED_RUNTIME"
                )
            )
        )
        failed = db.scalar(
            select(__import__("sqlalchemy").func.count(Job.id)).where(
                Job.status == "FAILED"
            )
        )
        return {
            "deployment_mode": mode,
            "product_readiness": ready,
            "generation_enabled": get_settings().generation_enabled,
            "services": states,
            "knowledge_snapshot_id": snapshot.id if snapshot else None,
            "failed_jobs": failed,
            "active_profile_id": profile.id if profile else None,
            "generation_policy": policy,
            "full_quality_acceptance": "NOT_YET_COMPLETED",
        }


@app.get("/api/v1/jobs")
def list_jobs(actor: Actor = Depends(get_actor), status: str | None = None):
    require_role(actor, "HR_ADMIN", "PLATFORM_ADMIN")
    if actor.role == "HR_ADMIN" and actor.scope.get("all") is not True:
        raise HTTPException(403, "Global administration scope is required")
    with SessionLocal() as db:
        q = select(Job).order_by(Job.created_at.desc()).limit(200)
        if status:
            q = q.where(Job.status == status)
        return {
            "items": [
                {
                    "id": j.id,
                    "kind": j.kind,
                    "status": j.status,
                    "run_at": j.run_at.isoformat(),
                    "attempts": j.attempts,
                    "error": j.error,
                }
                for j in db.scalars(q)
            ]
        }


@app.post("/api/v1/jobs/{job_id}/retry")
def retry_job(job_id: str, actor: Actor = Depends(get_actor)):
    require_role(actor, "HR_ADMIN", "PLATFORM_ADMIN")
    if actor.role == "HR_ADMIN" and actor.scope.get("all") is not True:
        raise HTTPException(403, "Global administration scope is required")
    with SessionLocal() as db:
        job = db.scalar(
            select(Job)
            .where(Job.id == job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if not job:
            raise HTTPException(404, detail="Задание не найдено")
        if job.status not in {"FAILED", "RETRY"}:
            raise HTTPException(409, detail="Задание нельзя повторить")
        job.status = "PENDING"
        job.run_at = now()
        job.error = None
        db.commit()
        return {"id": job.id, "status": job.status}


app.include_router(auth_router, prefix="/api/v1")
from .max_miniapp import router as max_miniapp_router
from .survey_campaigns import router as survey_campaign_router
from .miniapp_profiles import router as miniapp_profile_router
from .hr_accounts import router as hr_account_router
from .broadcasts import router as broadcast_router

app.include_router(max_miniapp_router, prefix="/api/v1")
app.include_router(survey_campaign_router, prefix="/api/v1")
app.include_router(miniapp_profile_router, prefix="/api/v1")
app.include_router(hr_account_router, prefix="/api/v1")
app.include_router(broadcast_router, prefix="/api/v1")
from .chat import router as chat_router

app.include_router(chat_router, prefix="/api/v1")
from .domain import router as domain_router

app.include_router(domain_router, prefix="/api/v1")
from .knowledge import router as knowledge_router

app.include_router(knowledge_router, prefix="/api/v1")
from .knowledge_simple import router as knowledge_simple_router
app.include_router(knowledge_simple_router, prefix="/api/v1")
from .basic_formulations import router as basic_formulations_router

app.include_router(basic_formulations_router, prefix="/api/v1")
from .organization import router as organization_router

app.include_router(organization_router, prefix="/api/v1")
from .assets import router as assets_router

app.include_router(assets_router, prefix="/api/v1")
# Transport service has its own key and no browser/session authority.
try:
    from .transport_api import router as transport_router

    app.include_router(transport_router)
except ImportError:
    if not get_settings().testing:
        raise

from .analysis import router as analysis_router

app.include_router(analysis_router, prefix="/api/v1")

from .local_ai import router as local_ai_router

app.include_router(local_ai_router, prefix="/api/v1")

from .auth_admin import router as auth_admin_router

app.include_router(auth_admin_router, prefix="/api/v1")

from .operations import router as operations_router

app.include_router(operations_router, prefix="/api/v1")

from .knowledge_admin import router as knowledge_admin_router

app.include_router(knowledge_admin_router, prefix="/api/v1")

from .knowledge_warnings import router as knowledge_warnings_router

app.include_router(knowledge_warnings_router, prefix="/api/v1")

from .scenario_media import router as scenario_media_router

app.include_router(scenario_media_router, prefix="/api/v1")
