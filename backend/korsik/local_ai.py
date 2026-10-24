"""Offline model registry, measured validation and explicit activation after drain."""

import hashlib, json, math, uuid, time
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select, func, text
from .db import get_db, now
from .models import Entity
from .auth import get_actor
from .settings import get_settings
from .rag_identity import pipeline_identities
from .common import (
    require_role,
    get_entity,
    save_entity,
    list_kind,
    serialize,
    etag,
    check_etag,
    commit,
    audit,
    enqueue,
    fingerprint,
)

router = APIRouter()
ROLES = {"embedder": "encoder", "reranker": "reranker", "generator": "generator"}


def require_global_operator(actor):
    require_role(actor, "PLATFORM_ADMIN", "HR_ADMIN")
    if actor.role == "HR_ADMIN" and actor.scope.get("all") is not True:
        raise HTTPException(
            403,
            "Deployment-wide AI changes require global HR or platform administration",
        )


def validate_optional_reason(body):
    if "reason" in body:
        reason = body["reason"]
        if not isinstance(reason, str) or len(reason) > 2000 or len(reason.strip()) < 3:
            raise HTTPException(422, "Укажите причину от 3 до 2000 символов")


def register_models(db):
    root = Path(get_settings().model_manifest_dir)
    items = []
    selected_generator = getattr(
        get_settings(), "generator_model_subdir", "generator-4b"
    )
    bundles = [(role, root / role) for role in ("embedder", "reranker")]
    bundles.extend(
        ("generator", path) for path in sorted(root.glob("generator*")) if path.is_dir()
    )
    for role, bundle in bundles:
        path = bundle / "manifest.json"
        if not path.is_file():
            continue
        manifest = json.loads(path.read_text())
        fp = hashlib.sha256(path.read_bytes()).hexdigest()
        aid = str(uuid.uuid5(uuid.NAMESPACE_URL, "model:" + role + ":" + fp))
        artifact = db.get(Entity, aid)
        if not artifact:
            artifact = save_entity(
                db,
                "model_artifact",
                {
                    "role": role,
                    "repository": manifest.get("repository"),
                    "revision": manifest.get("revision"),
                    "fingerprint": fp,
                    "files": manifest.get("files", []),
                    "bundle_subdir": bundle.name,
                    "deployment_selected": role != "generator"
                    or bundle.name == selected_generator,
                    "local_only": True,
                },
                status="INSTALLED",
                id=aid,
            )
        selected = role != "generator" or bundle.name == selected_generator
        if (
            artifact.data.get("deployment_selected") != selected
            or artifact.data.get("bundle_subdir") != bundle.name
        ):
            artifact.data = {
                **artifact.data,
                "deployment_selected": selected,
                "bundle_subdir": bundle.name,
            }
        quality_path = bundle / "quality-report.json"
        quality = {"status": "NOT_EVALUATED", "scope": "NOT_COMPANY_ACCEPTANCE"}
        if quality_path.is_file():
            report = json.loads(quality_path.read_text())
            if report.get("artifact_fingerprint") == fp:
                review_current = role != "generator" or (
                    report.get("pipeline_source_sha256")
                    == hashlib.sha256(
                        Path(__file__).with_name("retrieval.py").read_bytes()
                    ).hexdigest()
                    and report.get("generation_runtime_source_sha256")
                    == pipeline_identities()["generation_runtime_source_fingerprint"]
                )
                report_status = report.get("status", "NOT_EVALUATED")
                quality = {
                    "status": (
                        "STALE_PIPELINE_REVIEW"
                        if report_status == "PASS" and not review_current
                        else report_status
                    ),
                    "report_status": report_status,
                    "pipeline_review_current": review_current,
                    "cases": len(report.get("cases", [])),
                    "reviewer": report.get("reviewer"),
                    "report_sha256": hashlib.sha256(
                        quality_path.read_bytes()
                    ).hexdigest(),
                    "scope": "IMPLEMENTATION_REVIEW_NOT_COMPANY_ACCEPTANCE",
                }
        if artifact.data.get("quality_evaluation") != quality:
            artifact.data = {**artifact.data, "quality_evaluation": quality}
        items.append(artifact)
    return items


def profile_public(row):
    return {
        **serialize(row),
        "profile_id": row.id,
        "configuration_fingerprint": fingerprint(row.data.get("configuration", {})),
    }


def active_profile(db):
    control = next(iter(list_kind(db, "local_ai_control")), None)
    active_id = control.data.get("active_profile_id") if control else None
    return db.get(Entity, active_id) if active_id else None


def generation_policy(db):
    profile = active_profile(db)
    return (
        profile.data.get("configuration", {}).get("generation_policy")
        if profile
        and profile.data.get("configuration", {}).get("pipeline_identities")
        == pipeline_identities()
        else None
    )


@router.get("/local-ai/models")
def models(db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, "PLATFORM_ADMIN", "HR_ADMIN")
    items = register_models(db)
    commit(db)
    return {"items": [serialize(a) for a in items], "runtime_downloads": False}


@router.get("/local-ai/models/{model_artifact_id}")
def model(model_artifact_id: str, db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, "PLATFORM_ADMIN", "HR_ADMIN")
    return serialize(get_entity(db, model_artifact_id, "model_artifact"))


@router.get("/local-ai/profiles")
def profiles(db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, "PLATFORM_ADMIN", "HR_ADMIN")
    return {"items": [profile_public(p) for p in list_kind(db, "local_ai_profile")]}


@router.post("/local-ai/profiles", status_code=201)
def create_profile(body: dict, db=Depends(get_db), actor=Depends(get_actor)):
    require_global_operator(actor)
    validate_optional_reason(body)
    installed = register_models(db)
    if set(body) - {
        "name",
        "models",
        "generation_policy",
        "retrieval_profile",
        "reason",
    }:
        raise HTTPException(422, "Unknown profile fields")
    policy = body.get("generation_policy", "LOCAL_GENERATION")
    if policy not in {"LOCAL_GENERATION", "EXTRACTIVE_ONLY"}:
        raise HTTPException(422, "Unsupported generation policy")
    if get_settings().product_mode == "FULL" and policy != "LOCAL_GENERATION":
        raise HTTPException(422, "FULL requires LOCAL_GENERATION")
    required = {"embedder", "reranker"} | (
        {"generator"} if policy == "LOCAL_GENERATION" else set()
    )
    if body.get("retrieval_profile", "cpu") != "cpu":
        raise HTTPException(
            422, "Only the installed CPU retrieval profile is supported"
        )
    if "name" in body and (
        not isinstance(body["name"], str)
        or not body["name"].strip()
        or len(body["name"]) > 160
    ):
        raise HTTPException(422, "Profile name must contain 1 to 160 characters")
    selected = (
        body["models"]
        if "models" in body
        else {
            m.data["role"]: m.id
            for m in installed
            if m.data["role"] in required and m.data.get("deployment_selected", True)
        }
    )
    if not isinstance(selected, dict) or set(selected) - set(ROLES):
        raise HTTPException(422, "Invalid model role mapping")
    if not required.issubset(selected):
        raise HTTPException(422, "Required offline model roles are missing")
    for role in selected:
        artifact = next(
            (
                m
                for m in installed
                if m.id == selected.get(role) and m.data["role"] == role
            ),
            None,
        )
        if not artifact:
            raise HTTPException(
                422, "Model is not in installed offline bundle: " + role
            )
    config = {
        "models": selected,
        "generation_policy": policy,
        "retrieval_profile": "cpu",
        "max_context_tokens": 4096,
        "max_input_tokens": 3840,
        "max_new_tokens": 256,
        "thinking": False,
        "runtime_egress": False,
        "pipeline_identities": pipeline_identities(),
    }
    row = save_entity(
        db,
        "local_ai_profile",
        {
            "name": str(body.get("name", "Локальный CPU + Qwen"))[:160],
            "configuration": config,
            "created_by": actor.id,
        },
        status="DRAFT",
        owner_id=actor.id,
    )
    audit(db, actor, "local_ai.profile.create", row.id, reason=body.get("reason"))
    commit(db)
    return profile_public(row)


@router.get("/local-ai/active-profile")
def get_active(db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, "HR_ADMIN", "PLATFORM_ADMIN")
    row = active_profile(db)
    return {
        "active_profile": profile_public(row) if row else None,
        "configuration_state": "ACTIVE" if row else "SETUP_REQUIRED",
        "deployment_mode": get_settings().product_mode,
    }


@router.get("/local-ai/profiles/{profile_id}")
def get_profile(
    profile_id: str, response: Response, db=Depends(get_db), actor=Depends(get_actor)
):
    require_role(actor, "HR_ADMIN", "PLATFORM_ADMIN")
    row = get_entity(db, profile_id, "local_ai_profile")
    response.headers["ETag"] = etag(row)
    validations = list_kind(db, "local_ai_validation", parent_id=row.id)
    return {
        **profile_public(row),
        "validations": [
            {
                "id": v.id,
                "validation_id": v.id,
                "status": v.status,
                "configuration_fingerprint": v.data.get("configuration_fingerprint"),
                "created_at": v.created_at.isoformat(),
            }
            for v in validations
        ],
        "latest_validation_id": validations[-1].id if validations else None,
    }


@router.post("/local-ai/profiles/{profile_id}/tests", status_code=202)
def test_profile(
    profile_id: str,
    body: dict,
    request: Request,
    db=Depends(get_db),
    actor=Depends(get_actor),
):
    require_global_operator(actor)
    profile = get_entity(db, profile_id, "local_ai_profile", lock=True)
    check_etag(request, profile)
    configuration_hash = fingerprint(profile.data["configuration"])
    pending = next(
        (
            v
            for v in reversed(
                list_kind(db, "local_ai_validation", parent_id=profile.id)
            )
            if v.status in {"QUEUED", "RUNNING"}
            and v.data.get("configuration_fingerprint") == configuration_hash
        ),
        None,
    )
    if pending:
        return {**serialize(pending), "validation_id": pending.id}
    validation = save_entity(
        db,
        "local_ai_validation",
        {
            "profile_id": profile.id,
            "configuration_fingerprint": fingerprint(profile.data["configuration"]),
            "checks": [],
            "scope": "RUNTIME_SMOKE_SYNTHETIC_NOT_CORPORATE_ACCEPTANCE",
        },
        status="QUEUED",
        parent_id=profile.id,
        owner_id=actor.id,
    )
    enqueue(
        db,
        "LOCAL_AI_VALIDATE",
        {"validation_id": validation.id},
        "local-ai-validation:" + validation.id,
    )
    commit(db)
    return {**serialize(validation), "validation_id": validation.id}


@router.get("/local-ai/validations/{validation_id}")
def get_validation(validation_id: str, db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, "HR_ADMIN", "PLATFORM_ADMIN")
    return serialize(get_entity(db, validation_id, "local_ai_validation"))


@router.post("/local-ai/profiles/{profile_id}/activate")
def activate_profile(
    profile_id: str,
    body: dict,
    request: Request,
    db=Depends(get_db),
    actor=Depends(get_actor),
):
    require_global_operator(actor)
    validate_optional_reason(body)
    # Acquire the global gate before a profile row: two concurrent switches may
    # otherwise each hold the other's old/target row while waiting on this gate.
    if db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(76810103)"))
    profile = get_entity(db, profile_id, "local_ai_profile", lock=True)
    check_etag(request, profile)
    validations = [
        v
        for v in list_kind(db, "local_ai_validation", parent_id=profile.id)
        if v.status == "PASSED"
        and v.data["configuration_fingerprint"]
        == fingerprint(profile.data["configuration"])
    ]
    if not validations:
        raise HTTPException(
            409, "Profile must pass real runtime smoke tests before activation"
        )
    pending = (
        db.scalar(
            select(func.count(Entity.id)).where(
                Entity.kind == "answer", Entity.status.in_(["QUEUED", "RUNNING"])
            )
        )
        or 0
    )
    if pending:
        raise HTTPException(409, {"code": "drain_required", "pending_answers": pending})
    assert_current_activation_target(db, profile)
    control = next(iter(list_kind(db, "local_ai_control")), None)
    if control:
        control = get_entity(db, control.id, lock=True)
    old = active_profile(db)
    if old and old.id != profile.id:
        old.status = "INACTIVE"
    profile.status = "ACTIVE"
    profile.data = {
        **profile.data,
        "activated_at": now().isoformat(),
        "validation_id": validations[-1].id,
    }
    if control:
        control.data = {
            "active_profile_id": profile.id,
            "epoch": int(control.data.get("epoch", 0)) + 1,
        }
    else:
        save_entity(
            db, "local_ai_control", {"active_profile_id": profile.id, "epoch": 1}
        )
    audit(
        db,
        actor,
        "local_ai.profile.activate",
        profile.id,
        {"validation_id": validations[-1].id},
        reason=body.get("reason"),
    )
    commit(db)
    return profile_public(profile)


def assert_current_activation_target(db, profile):
    """A previous PASSED smoke never overrides current code/quality/deployment."""
    from .retrieval import _request, EMBEDDER, RERANKER, GENERATOR

    config = profile.data.get("configuration", {})
    if config.get("pipeline_identities") != pipeline_identities():
        raise HTTPException(409, {"code": "pipeline_identity_changed"})
    policy = config.get("generation_policy")
    if policy not in {"LOCAL_GENERATION", "EXTRACTIVE_ONLY"}:
        raise HTTPException(409, {"code": "invalid_generation_policy"})
    settings = get_settings()
    if settings.product_mode == "FULL" and policy != "LOCAL_GENERATION":
        raise HTTPException(409, {"code": "full_requires_local_generation"})
    if policy == "LOCAL_GENERATION" and not settings.generation_enabled:
        raise HTTPException(409, {"code": "generation_disabled"})
    installed = register_models(db)
    selected = {
        row.data["role"]: row
        for row in installed
        if row.data.get("deployment_selected")
    }
    models = config.get("models")
    required = {"embedder", "reranker"} | (
        {"generator"} if policy == "LOCAL_GENERATION" else set()
    )
    if (
        not isinstance(models, dict)
        or not required.issubset(models)
        or set(models) - set(ROLES)
    ):
        raise HTTPException(409, {"code": "deployment_model_mismatch"})
    for role, artifact_id in models.items():
        current = selected.get(role)
        if not current or current.id != artifact_id or current.status != "INSTALLED":
            raise HTTPException(
                409, {"code": "deployment_model_mismatch", "role": role}
            )
    if policy == "LOCAL_GENERATION":
        quality = selected["generator"].data.get("quality_evaluation", {})
        if (
            quality.get("status") != "PASS"
            or quality.get("pipeline_review_current") is not True
        ):
            raise HTTPException(409, {"code": "current_semantic_review_required"})
    urls = {"embedder": EMBEDDER, "reranker": RERANKER, "generator": GENERATOR}
    states = {}
    for role in models:
        try:
            detail = _request(urls[role] + "/healthz", method="GET", timeout=5)
        except Exception:
            raise HTTPException(409, {"code": "live_runtime_unavailable", "role": role})
        if not isinstance(detail, dict):
            raise HTTPException(409, {"code": "live_runtime_identity_mismatch"})
        states[ROLES[role]] = {
            "ready": detail.get("status") == "READY",
            "checked_at": now().isoformat(),
            "detail": detail,
        }
    if not profile_identity_matches(db, profile, states):
        raise HTTPException(409, {"code": "live_runtime_identity_mismatch"})


def validate_runtime_job(db, job):
    if job.kind != "LOCAL_AI_VALIDATE":
        return False
    from .retrieval import _request, validate_generated, EMBEDDER, RERANKER, GENERATOR

    validation = get_entity(db, job.payload["validation_id"], "local_ai_validation")
    profile = get_entity(db, validation.parent_id, "local_ai_profile")
    config = profile.data["configuration"]
    checks = []

    def check(name, fn):
        started = time.monotonic()
        try:
            detail = fn()
            checks.append(
                {
                    "name": name,
                    "status": "PASS",
                    "seconds": round(time.monotonic() - started, 3),
                    "detail": detail,
                }
            )
        except Exception as error:
            checks.append(
                {"name": name, "status": "FAIL", "error_type": type(error).__name__}
            )

    def pipeline_identity():
        if config.get("pipeline_identities") != pipeline_identities():
            raise ValueError("Pipeline code changed; create and validate a new profile")
        return config["pipeline_identities"]

    check("pipeline_code_identity", pipeline_identity)
    urls = {"embedder": EMBEDDER, "reranker": RERANKER, "generator": GENERATOR}
    for role, aid in config["models"].items():

        def identity(role=role, aid=aid):
            result = _request(urls[role] + "/healthz", method="GET", timeout=5)
            artifact = get_entity(db, aid, "model_artifact")
            if result.get("fingerprint") != artifact.data["fingerprint"]:
                raise ValueError("Artifact fingerprint mismatch")
            if role == "generator" and result.get("renderer_fingerprint") != config.get(
                "pipeline_identities", {}
            ).get("generation_runtime_source_fingerprint"):
                raise ValueError("Generation runtime code fingerprint mismatch")
            return {
                "fingerprint": result["fingerprint"],
                "runtime": result.get("runtime"),
                **(
                    {"renderer_fingerprint": result.get("renderer_fingerprint")}
                    if role == "generator"
                    else {}
                ),
            }

        check(role + "_identity", identity)

    def embedding():
        r = _request(
            EMBEDDER + "/embed",
            {"texts": ["Правила адаптации сотрудника"], "purpose": "query"},
        )
        v = r["vectors"][0]
        assert (
            len(v) == 384
            and all(math.isfinite(x) for x in v)
            and abs(sum(x * x for x in v) - 1) < 0.01
        )
        return {"dimensions": len(v), "norm": sum(x * x for x in v) ** 0.5}

    check("russian_embedding", embedding)

    def reranking():
        r = _request(
            RERANKER + "/rerank",
            {
                "query": "Когда обед?",
                "documents": [
                    "Обед с 12:00 до 13:00.",
                    "Пароль меняют в личном кабинете.",
                ],
            },
        )
        assert r["scores"][0] > r["scores"][1]
        return {"ordering": "relevant_first"}

    check("russian_reranking", reranking)
    if config["generation_policy"] == "LOCAL_GENERATION":
        register_models(db)

        def semantic_report():
            artifact = get_entity(db, config["models"]["generator"], "model_artifact")
            quality = artifact.data.get("quality_evaluation", {})
            if quality.get("status") != "PASS":
                raise ValueError("Model has no passing implementation semantic review")
            return quality

        check("semantic_evaluation_report", semantic_report)
        cases = [
            ("Когда обед?", "Обед с 12:00 до 13:00.", ("12:00", "13:00")),
            (
                "К кому обратиться за пропуском?",
                "Для получения пропуска обратитесь в отдел кадров.",
                ("отдел", "кадр"),
            ),
            (
                "Когда нужно уведомить HR?",
                "Если изменился номер телефона, уведомите HR в течение 2 дней.",
                ("2",),
            ),
        ]
        for i, (q, text, expected) in enumerate(cases):

            def generation(q=q, text=text, expected=expected):
                r = _request(
                    GENERATOR + "/generate",
                    {
                        "question": q,
                        "evidence": [{"text": text, "title": "Тестовый документ"}],
                        "max_new_tokens": 96,
                    },
                    timeout=48,
                )
                valid, reason = validate_generated(r["text"], [{"text": text}])
                assert valid and all(t in r["text"].lower() for t in expected), (
                    reason,
                    r["text"],
                )
                return {
                    "output": r["text"],
                    "output_tokens": r.get("output_tokens"),
                    "seconds": r.get("seconds"),
                }

            check("grounded_generation_" + str(i + 1), generation)
    validation.data = {
        **validation.data,
        "checks": checks,
        "completed_at": now().isoformat(),
    }
    validation.status = (
        "PASSED" if all(c["status"] == "PASS" for c in checks) else "FAILED"
    )
    return True


def profile_identity_matches(db, profile, states):
    if not profile:
        return False
    from datetime import datetime
    from .db import aware

    config = profile.data.get("configuration", {})
    if config.get("pipeline_identities") != pipeline_identities():
        return False
    for role, aid in config.get("models", {}).items():
        state = states.get(ROLES.get(role, ""), {})
        artifact = db.get(Entity, aid)
        stamp = state.get("checked_at")
        if not artifact or not state.get("ready") or not stamp:
            return False
        if (now() - aware(datetime.fromisoformat(stamp))).total_seconds() > 180:
            return False
        if state.get("detail", {}).get("fingerprint") != artifact.data.get(
            "fingerprint"
        ):
            return False
        if role == "generator" and state.get("detail", {}).get(
            "renderer_fingerprint"
        ) != config["pipeline_identities"].get("generation_runtime_source_fingerprint"):
            return False
    return bool(config.get("models"))


def active_identity_matches(db, states):
    return profile_identity_matches(db, active_profile(db), states)
