"""Operator cannot activate a stale, incomplete or different-artifact review."""

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/install-quality-report.py"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture(tmp_path):
    bundle = tmp_path / "model"
    bundle.mkdir()
    weights = bundle / "weights.bin"
    weights.write_bytes(b"synthetic fixture; no model or network")
    manifest = {
        "repository": "example/synthetic",
        "revision": "pinned-test-revision",
        "files": [{"path": weights.name, "sha256": sha(weights)}],
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    spec = importlib.util.spec_from_file_location(
        "review_test_identity", ROOT / "backend/korsik/rag_identity.py"
    )
    identity = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(identity)
    report = {
        "status": "PASS",
        "scope": "GUARDED_APPLICATION_EXTENDED_REGRESSION_REVIEW",
        "artifact_fingerprint": sha(bundle / "manifest.json"),
        "repository": manifest["repository"],
        "revision": manifest["revision"],
        "pipeline_source_sha256": sha(ROOT / "backend/korsik/retrieval.py"),
        "generation_runtime_source_sha256": sha(ROOT / "services/model_server/app.py"),
        "pipeline_identities": identity.pipeline_identities(),
        "measured": {"cases": 29},
        "cases": [{"id": str(i), "status": "PASS"} for i in range(29)],
    }
    return bundle, report


def invoke(tmp_path, bundle, report):
    path = tmp_path / "review.json"
    path.write_text(json.dumps(report))
    return (
        subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--bundle",
                str(bundle),
                "--report",
                str(path),
            ],
            capture_output=True,
            text=True,
        ),
        path,
    )


def test_source_bound_review_imported_without_network(tmp_path):
    bundle, report = fixture(tmp_path)
    result, path = invoke(tmp_path, bundle, report)
    assert result.returncode == 0, result.stderr
    assert (bundle / "quality-report.json").read_bytes() == path.read_bytes()
    assert not (bundle / "quality-report.json.importing").exists()


@pytest.mark.parametrize(
    "problem",
    [
        "pending_case",
        "duplicate_case",
        "stale_pipeline",
        "different_renderer",
        "failed_review",
        "changed_weights",
    ],
)
def test_invalid_review_preserves_previous_file(tmp_path, problem):
    bundle, report = fixture(tmp_path)
    destination = bundle / "quality-report.json"
    destination.write_bytes(b"previous review preserved")
    if problem == "pending_case":
        report["cases"][4]["status"] = "PENDING"
    elif problem == "duplicate_case":
        report["cases"][-1]["id"] = report["cases"][0]["id"]
    elif problem == "stale_pipeline":
        report["pipeline_identities"]["retrieval_policy_fingerprint"] = "wrong-source"
    elif problem == "different_renderer":
        report["generation_runtime_source_sha256"] = "different-renderer"
    elif problem == "failed_review":
        report["status"] = "FAIL"
    elif problem == "changed_weights":
        (bundle / "weights.bin").write_bytes(b"changed after evaluation")
    result, _ = invoke(tmp_path, bundle, report)
    assert result.returncode != 0
    assert destination.read_bytes() == b"previous review preserved"
