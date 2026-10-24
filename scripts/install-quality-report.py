#!/usr/bin/env python3
"""Operator explicitly imports the bundled, limited implementation review.

This verifies exact model bytes and retrieval source, and never turns an
unreviewed model into PASS. Company acceptance remains a separate procedure.
"""

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument(
        "--report",
        type=Path,
        default=root / "tests/model-quality/qwen-4b-release17.json",
    )
    args = parser.parse_args()
    bundle = args.bundle.resolve(strict=True)
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    review = json.loads(args.report.read_text())
    identity_spec = importlib.util.spec_from_file_location(
        "korsik_operator_rag_identity", root / "backend/korsik/rag_identity.py"
    )
    identity = importlib.util.module_from_spec(identity_spec)
    identity_spec.loader.exec_module(identity)
    cases = review.get("cases", [])
    reviewed_cases = (
        isinstance(cases, list)
        and len(cases) >= 20
        and all(
            isinstance(case, dict) and case.get("status") == "PASS" for case in cases
        )
        and all(isinstance(case.get("id"), str) and case["id"] for case in cases)
        and len({case["id"] for case in cases}) == len(cases)
        and review.get("measured", {}).get("cases") == len(cases)
    )
    if (
        review.get("status") != "PASS"
        or review.get("scope")
        not in {
            "GUARDED_APPLICATION_20_CASE_IMPLEMENTATION_REVIEW",
            "GUARDED_APPLICATION_EXTENDED_REGRESSION_REVIEW",
        }
        or not reviewed_cases
        or review.get("pipeline_identities") != identity.pipeline_identities()
        or review.get("artifact_fingerprint") != sha256(manifest_path)
        or review.get("repository") != manifest.get("repository")
        or review.get("revision") != manifest.get("revision")
        or review.get("pipeline_source_sha256")
        != sha256(root / "backend/korsik/retrieval.py")
        or review.get("generation_runtime_source_sha256")
        != sha256(root / "services/model_server/app.py")
    ):
        raise SystemExit(
            "Review does not match this artifact/pipeline; a new semantic review is required"
        )
    for record in manifest["files"]:
        path = (bundle / record["path"]).resolve(strict=True)
        if bundle not in path.parents or sha256(path) != record["sha256"]:
            raise SystemExit("Model file verification failed; review was not installed")
    destination = bundle / "quality-report.json"
    temporary = bundle / "quality-report.json.importing"
    shutil.copyfile(args.report, temporary)
    temporary.replace(destination)
    print(
        json.dumps(
            {
                "status": "IMPORTED",
                "scope": review["scope"],
                "company_acceptance": False,
            }
        )
    )


if __name__ == "__main__":
    main()
