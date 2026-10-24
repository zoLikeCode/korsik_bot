"""Activation rechecks present quality/deployment, not only an old PASSED row."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from korsik import local_ai, retrieval
from korsik.common import fingerprint, save_entity
from korsik.models import Audit, Entity
from test_domain import hr as hr, req, session as session


@pytest.fixture
def activation_fixture(session, hr, tmp_path, monkeypatch):
    settings = SimpleNamespace(
        product_mode="FULL",
        generation_enabled=True,
        model_manifest_dir=str(tmp_path),
        generator_model_subdir="generator",
    )
    paths = {}
    for role in ("embedder", "reranker", "generator", "generator-other"):
        path = tmp_path / role
        path.mkdir()
        encoded = json.dumps(
            {"repository": "synthetic-" + role, "revision": "test-only", "files": []}
        ).encode()
        (path / "manifest.json").write_bytes(encoded)
        if role.startswith("generator"):
            report = {
                "status": "PASS",
                "reviewer": "SYNTHETIC_ACTIVATION_GUARD_FIXTURE_NOT_MODEL_QUALITY",
                "artifact_fingerprint": hashlib.sha256(encoded).hexdigest(),
                "pipeline_source_sha256": hashlib.sha256(
                    Path(retrieval.__file__).read_bytes()
                ).hexdigest(),
                "generation_runtime_source_sha256": local_ai.pipeline_identities()[
                    "generation_runtime_source_fingerprint"
                ],
                "cases": [],
            }
            (path / "quality-report.json").write_text(json.dumps(report))
        paths[role] = path
    monkeypatch.setattr(local_ai, "get_settings", lambda: settings)
    result = local_ai.create_profile({"name": "Synthetic guard fixture"}, session, hr)
    row = session.get(Entity, result["id"])

    def passed_validation():
        save_entity(
            session,
            "local_ai_validation",
            {
                "configuration_fingerprint": fingerprint(row.data["configuration"]),
                "scope": "SYNTHETIC_ACTIVATION_GUARD_FIXTURE_NOT_REAL_SMOKE",
            },
            parent_id=row.id,
            status="PASSED",
        )
        session.commit()

    passed_validation()
    control = save_entity(
        session, "local_ai_control", {"active_profile_id": None, "epoch": 7}
    )
    session.commit()
    calls = []
    live = {}
    urls = {
        "embedder": retrieval.EMBEDDER,
        "reranker": retrieval.RERANKER,
        "generator": retrieval.GENERATOR,
    }
    for role, aid in row.data["configuration"]["models"].items():
        live[role] = {
            "status": "READY",
            "fingerprint": session.get(Entity, aid).data["fingerprint"],
        }
        if role == "generator":
            live[role]["renderer_fingerprint"] = local_ai.pipeline_identities()[
                "generation_runtime_source_fingerprint"
            ]

    def request(url, payload=None, method="POST", timeout=30):
        role = next(
            (role for role, endpoint in urls.items() if url == endpoint + "/healthz"),
            None,
        )
        assert (
            role and method == "GET" and payload is None
        ), "NO_INFERENCE_OR_EXTERNAL_REQUESTS_IN_FIXTURE"
        calls.append(role)
        value = live[role]
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(retrieval, "_request", request)
    return SimpleNamespace(
        settings=settings,
        paths=paths,
        profile=row,
        control=control,
        live=live,
        calls=calls,
        passed_validation=passed_validation,
    )


@pytest.mark.parametrize(
    "change,code",
    [
        ("quality_revoked", "current_semantic_review_required"),
        ("quality_stale", "current_semantic_review_required"),
        ("pipeline_changed", "pipeline_identity_changed"),
        ("deployment_changed", "deployment_model_mismatch"),
        ("generation_disabled", "generation_disabled"),
        ("live_weights_changed", "live_runtime_identity_mismatch"),
        ("live_renderer_changed", "live_runtime_identity_mismatch"),
        ("live_unready", "live_runtime_identity_mismatch"),
        ("live_unavailable", "live_runtime_unavailable"),
        ("live_malformed", "live_runtime_identity_mismatch"),
    ],
)
def test_old_passed_validation_cannot_override_current_activation_gate(
    session, hr, activation_fixture, change, code
):
    fixture = activation_fixture
    if change.startswith("quality_"):
        path = fixture.paths["generator"] / "quality-report.json"
        report = json.loads(path.read_text())
        if change == "quality_revoked":
            report["status"] = "FAIL"
        else:
            report["pipeline_source_sha256"] = "obsolete-source"
        path.write_text(json.dumps(report))
    elif change == "pipeline_changed":
        configuration = deepcopy(fixture.profile.data["configuration"])
        configuration["pipeline_identities"][
            "retrieval_module_source_fingerprint"
        ] = "obsolete-module"
        fixture.profile.data = {**fixture.profile.data, "configuration": configuration}
        fixture.passed_validation()  # Even a matching old validation cannot bless changed code.
    elif change == "deployment_changed":
        fixture.settings.generator_model_subdir = "generator-other"
    elif change == "generation_disabled":
        fixture.settings.generation_enabled = False
    elif change == "live_weights_changed":
        fixture.live["generator"]["fingerprint"] = "another-installed-runtime"
    elif change == "live_renderer_changed":
        fixture.live["generator"]["renderer_fingerprint"] = "another-runtime-code"
    elif change == "live_unready":
        fixture.live["generator"]["status"] = "LOADING"
    elif change == "live_unavailable":
        fixture.live["generator"] = RuntimeError(
            "Synthetic network boundary unavailable"
        )
    elif change == "live_malformed":
        fixture.live["generator"] = []
    with pytest.raises(HTTPException) as error:
        local_ai.activate_profile(
            fixture.profile.id, {}, req(fixture.profile), session, hr
        )
    assert error.value.status_code == 409 and error.value.detail["code"] == code
    assert fixture.profile.status == "DRAFT"
    assert fixture.control.data == {"active_profile_id": None, "epoch": 7}
    assert not session.scalar(
        select(Audit).where(Audit.action == "local_ai.profile.activate")
    )


def test_activation_requires_current_health_and_preserves_null_control_cas(
    session, hr, activation_fixture
):
    fixture = activation_fixture
    result = local_ai.activate_profile(
        fixture.profile.id, {}, req(fixture.profile), session, hr
    )
    assert result["status"] == "ACTIVE"
    assert set(fixture.calls) == {"embedder", "reranker", "generator"}
    assert fixture.control.data == {"active_profile_id": fixture.profile.id, "epoch": 8}


@pytest.mark.parametrize(
    "reason", [None, "", " ", "ab", 42, {}, "x" * 2001, " " + "x" * 2000]
)
def test_supplied_profile_reason_is_never_silently_dropped(
    session, hr, activation_fixture, reason
):
    fixture = activation_fixture
    for invoke in (
        lambda: local_ai.create_profile({"reason": reason}, session, hr),
        lambda: local_ai.activate_profile(
            fixture.profile.id, {"reason": reason}, req(fixture.profile), session, hr
        ),
    ):
        with pytest.raises(HTTPException) as error:
            invoke()
        assert error.value.status_code == 422
    assert fixture.calls == []
    assert fixture.control.data["active_profile_id"] is None
