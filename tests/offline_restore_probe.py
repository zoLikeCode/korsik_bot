"""Runtime proof inside a separately restored, internet-isolated Compose project."""

from io import BytesIO
import json
from pathlib import Path
import socket
import time
import httpx
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import select
from starlette.requests import Request
from korsik.auth import user_actor
from korsik.common import etag
from korsik.db import SessionLocal, new_id
from korsik.domain import dashboard_data
from korsik.models import Entity, User, Job
from korsik.analysis import analyze_text
from korsik.recovery import reindex_published
from korsik.retrieval import answer_question, COLLECTION
from korsik.settings import get_settings

settings = get_settings()
if not settings.database_name.startswith("korsik_offline_"):
    raise RuntimeError("ISOLATED_OFFLINE_DATABASE_REQUIRED")

report = {
    "schema": "korsik-offline-restore-v1",
    "mode": settings.product_mode,
    "database": settings.database_name,
    "external_MAX_sends": 0,
    "checks": [],
    "max_transport": "NOT_STARTED_NO_OUTGOING_MESSAGES",
}


def record(name, valid, detail=None):
    report["checks"].append(
        {"name": name, "status": "PASS" if valid else "FAIL", "detail": detail}
    )


for host in ("huggingface.co", "api.openai.com"):
    try:
        socket.getaddrinfo(host, 443)
        record("egress_dns_" + host, False)
    except socket.gaierror:
        record("egress_dns_" + host, True)

for address, port in (("1.1.1.1", 443), ("8.8.8.8", 53)):
    try:
        with socket.create_connection((address, port), timeout=2):
            record("egress_tcp_" + address, False)
    except OSError:
        record("egress_tcp_" + address, True)

with httpx.Client(timeout=240, trust_env=False) as client:
    roles = ["encoder", "reranker", "parser"]
    if settings.product_mode == "FULL":
        roles.append("generator")
    for role in roles:
        value = client.get(f"http://{role}:8000/healthz").json()
        record(
            role + "_cold_start",
            value.get("status") == "READY",
            {
                k: value.get(k)
                for k in (
                    "status",
                    "fingerprint",
                    "artifact_fingerprint",
                    "runtime",
                    "profile",
                )
            },
        )
    image = Image.new("RGB", (2400, 1400), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 60)
    draw.text(
        (100, 180), "Отпуск согласовать с руководителем.", fill="black", font=font
    )
    draw.text((100, 290), "Заявление передать в отдел кадров.", fill="black", font=font)
    pdf = BytesIO()
    image.save(pdf, "PDF", resolution=150)
    text, errors, complete = [], [], False
    started = time.monotonic()
    with client.stream(
        "POST",
        "http://parser:8000/parse",
        files={"file": ("synthetic-scan.pdf", pdf.getvalue(), "application/pdf")},
    ) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            item = json.loads(line)
            if item["type"] == "checkpoint":
                text.extend(b.get("text", "") for b in item.get("blocks", []))
            elif item["type"] == "complete":
                complete = True
            elif item["type"] == "error":
                errors.append(item.get("error"))
    record(
        "offline_russian_image_only_pdf_ocr",
        complete and not errors and "отпуск" in " ".join(text).lower(),
        {
            "seconds": round(time.monotonic() - started, 3),
            "errors": len(errors),
            "fixture": "SYNTHETIC_NOT_COMPANY_OCR_ACCEPTANCE",
        },
    )

with SessionLocal() as db:
    if settings.product_mode == "FULL":
        # A backup can pin an earlier installed decoder. Rehearse the native
        # profile test/activation workflow against the cold local services;
        # never manufacture a PASSED validation row or bypass drain/ETag.
        from korsik import local_ai, worker, scheduler

        admin = db.scalar(
            select(User).where(
                User.role == "HR_ADMIN",
                User.enabled.is_(True),
                User.scope["all"].as_boolean().is_(True),
            )
        )
        assert admin, "Global HR administrator missing from restored SQL"
        actor = user_actor(admin)
        scheduler.tick(db)
        db.commit()  # normal deadline watchdog drains expired restored answers
        installed = local_ai.register_models(db)
        models = {
            item.data["role"]: item.id
            for item in installed
            if item.data.get("deployment_selected")
        }
        profile = local_ai.create_profile(
            {
                "name": "Isolated cold restore runtime profile",
                "models": models,
                "generation_policy": "LOCAL_GENERATION",
                "retrieval_profile": "cpu",
                "reason": "Isolated recovery rehearsal; synthetic smoke, no company acceptance",
            },
            db,
            actor,
        )
        entity = db.get(Entity, profile["id"])
        request = Request(
            {"type": "http", "headers": [(b"if-match", etag(entity).encode())]}
        )
        validation = local_ai.test_profile(entity.id, {}, request, db, actor)
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            db.rollback()
            current = db.get(Entity, validation["validation_id"])
            if current.status not in {"QUEUED", "RUNNING"}:
                break
            # Actual claim/lease/handler/commit path, inside the disposable DB.
            if not worker.run_once(ingest=True):
                time.sleep(0.2)
        db.rollback()
        current = db.get(Entity, validation["validation_id"])
        entity = db.get(Entity, entity.id)
        if current.status == "PASSED":
            request = Request(
                {"type": "http", "headers": [(b"if-match", etag(entity).encode())]}
            )
            local_ai.activate_profile(
                entity.id,
                {"reason": "Isolated recovery rehearsal after real local validation"},
                request,
                db,
                actor,
            )
        validation_job = db.scalar(
            select(Job).where(
                Job.kind == "LOCAL_AI_VALIDATE",
                Job.payload["validation_id"].as_string() == current.id,
            )
        )
        record(
            "cold_local_profile_runtime_test_and_activation",
            current.status == "PASSED"
            and entity.status == "ACTIVE"
            and validation_job is not None
            and validation_job.status == "COMPLETED",
            {
                "validation_status": current.status,
                "profile_status": entity.status,
                "model_roles": sorted(models),
                "checks": current.data.get("checks", []),
                "orchestration": "NATIVE_ENDPOINT_FUNCTIONS_AND_ACTUAL_WORKER_RUN_ONCE",
                "worker_partition": "ingest",
                "validation_job_status": (
                    validation_job.status if validation_job else None
                ),
                "validation_job_attempts": (
                    validation_job.attempts if validation_job else None
                ),
                "scope": "SYNTHETIC_RUNTIME_SMOKE_NOT_COMPANY_ACCEPTANCE",
            },
        )
    result = reindex_published(db)
    record(
        (
            "restore_vector_projection_from_sql"
            if settings.product_mode == "FULL"
            else "restore_sql_lexical_projection"
        ),
        result["status"] == "OK"
        and result["indexed"] > 0
        and result.get("mode")
        == ("DENSE_SPARSE" if settings.product_mode == "FULL" else "LEXICAL_ONLY"),
        result,
    )
    if settings.product_mode == "FULL":
        with httpx.Client(timeout=30, trust_env=False) as client:
            response = client.post(
                settings.qdrant_url + "/collections/" + COLLECTION + "/points/count",
                json={"exact": True},
            )
            response.raise_for_status()
            points = response.json()["result"]["count"]
        record(
            "actual_restored_qdrant_point_count",
            points >= result["indexed"] > 0,
            {
                "points": points,
                "indexed_source_chunks": result["indexed"],
                "fresh_isolated_qdrant_volume": True,
            },
        )
    employee = db.scalar(
        select(Entity).where(
            Entity.kind == "employee",
            Entity.data["external_employee_id"].as_string() == "DEMO-KORSIK-001",
        )
    )
    assert employee, "synthetic demo employee missing from restored SQL"
    conv = Entity(id=new_id(), kind="conversation", data={"employee_id": employee.id})
    answer = answer_question(
        db, conv, "Как передавать пароли к конфиденциальной информации?"
    )
    record(
        "offline_" + settings.product_mode.lower() + "_answer_from_restored_corpus",
        answer["mode"]
        in (
            {"LOCAL_GENERATED", "EXTRACT", "APPROVED_CARD"}
            if settings.product_mode == "FULL"
            else {"EXTRACT", "APPROVED_CARD"}
        )
        and answer.get("reason")
        not in {
            "LOCAL_GENERATOR_UNAVAILABLE",
            "LOCAL_RETRIEVAL_UNAVAILABLE",
            "DEGRADED_PROFILE_MISMATCH",
            "GENERATION_BUDGET_EXCEEDED",
            "INCOMPLETE_GENERATION",
        }
        and bool(answer.get("citations")),
        {
            "mode": answer["mode"],
            "citations": len(answer.get("citations", [])),
            "reason": answer.get("reason"),
            "scope": "RESTORED_SOURCE_ANSWER_NOT_FREE_GENERATION_QUALITY_ACCEPTANCE",
            "generation_policy": "Normative source may require literal EXTRACT; actual decoder execution is checked separately by native runtime validation",
        },
    )
    analysis = analyze_text(
        "Коллеги помогли быстро освоиться, но пропуск выдали с задержкой."
    )
    record(
        "offline_feedback_analysis",
        analysis.get("status") == "PENDING_HR_REVIEW"
        and bool(analysis.get("model_fingerprint")),
        {
            "status": analysis.get("status"),
            "training_data_kind": analysis.get("training_data_kind"),
            "quality": "Synthetic baseline; HR decision required",
        },
    )
    admin = db.scalar(
        select(User).where(
            User.role == "HR_ADMIN",
            User.enabled.is_(True),
            User.scope["all"].as_boolean().is_(True),
        )
    )
    assert admin, "Global HR administrator missing from restored SQL"
    dashboard = dashboard_data(db, user_actor(admin))
    record(
        "offline_dashboard_from_restored_sql",
        isinstance(dashboard, dict) and bool(dashboard),
        {"computed": True},
    )
    db.rollback()

report["status"] = (
    "PASS" if all(c["status"] == "PASS" for c in report["checks"]) else "FAIL"
)
Path("/operator/offline-restore-report.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2)
)
print(json.dumps(report, ensure_ascii=False), flush=True)
