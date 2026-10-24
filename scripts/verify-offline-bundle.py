#!/usr/bin/env python3
"""Read-only, stdlib verification of a trusted Korsik offline delivery.

Does not extract archives, load Docker images, contact the network or run models.
An unsigned manifest establishes integrity against its trusted copy, not origin.
Source includes supplied internal policy examples; it is not a public release.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys
import tarfile
import time


ARCHIVES = ("images.tar.gz", "models.tar.gz", "source.tar.gz")
IMAGE_TAGS = (
    "korsik-app:local", "korsik-web:local", "korsik-tunnel:local",
    "korsik-models:cpu", "korsik-generator:local", "korsik-parser:local",
    "korsik-parser:structure", "postgres:17-alpine", "qdrant/qdrant:v1.20.0",
)
HISTORICAL_REPORT_NAMES = (
    "qwen-4b-comparison.json", "qwen-4b-pipeline20-accepted.json",
)
SOURCE_PRIVATE_DIRS = {
    ".git", ".venv", "node_modules", "dist", "__pycache__", ".pytest_cache",
    ".ruff_cache", "data", "models", "secrets", ".secrets", ".ssh",
    "backups", "private", "private-backup",
}
SOURCE_PRIVATE_SUFFIXES = {
    ".db", ".sqlite", ".sqlite3", ".dump", ".pgdump", ".bak", ".backup",
    ".log", ".pyc", ".tsbuildinfo", ".pem", ".key",
}
PRIVATE_KEY = re.compile(
    rb"-----BEGIN (?:OPENSSH |RSA |EC |DSA |ENCRYPTED )?PRIVATE KEY-----"
)
CREDENTIAL = re.compile(
    rb"(?i)(?:authorization|api[_-]?key|access[_-]?token|password|secret)"
    rb"\s*[:=]\s*[\"']([A-Za-z0-9_+/=\-]{32,})[\"']"
)
SHA = re.compile(r"[0-9a-f]{64}")
JSON_LIMIT = 16 * 1024 * 1024
CHUNK = 1024 * 1024


class VerificationError(Exception):
    """Only non-content error codes leave this verifier."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def require(condition, code):
    if not condition:
        raise VerificationError(code)


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def safe_path(name, *, allow_root=False):
    require(isinstance(name, str) and "\x00" not in name, "UNSAFE_TAR_PATH")
    require(len(name) <= 4096 and "\\" not in name, "UNSAFE_TAR_PATH")
    path = PurePosixPath(name)
    require(not path.is_absolute() and not re.match(r"^[A-Za-z]:", name),
            "UNSAFE_TAR_PATH")
    require(".." not in path.parts, "UNSAFE_TAR_PATH")
    value = path.as_posix()
    require(value != "." or allow_root, "UNSAFE_EMPTY_PATH")
    return value


def linked_path(member, name):
    """Allow a contained Docker-save link, including safe parent references."""
    raw = member.linkname
    require(raw and "\x00" not in raw and "\\" not in raw
            and not raw.startswith("/") and not re.match(r"^[A-Za-z]:", raw),
            "UNSAFE_TAR_LINK")
    parts = [] if member.islnk() else list(PurePosixPath(name).parent.parts)
    for part in PurePosixPath(raw).parts:
        if part == "..":
            require(bool(parts), "UNSAFE_TAR_LINK")
            parts.pop()
        elif part != ".":
            parts.append(part)
    return safe_path("/".join(parts))


def source_name_allowed(name):
    parts = PurePosixPath(name).parts
    base = parts[-1].lower()
    require(not any(p.lower() in SOURCE_PRIVATE_DIRS for p in parts),
            "PRIVATE_SOURCE_DIRECTORY")
    require(not base.startswith(".env") or base == ".env.example", "PRIVATE_ENV_FILE")
    require(PurePosixPath(base).suffix not in SOURCE_PRIVATE_SUFFIXES,
            "PRIVATE_SOURCE_FILE")
    require(not re.search(r"\.(?:db|sqlite3?)-(?:wal|shm|journal)$", base)
            and base not in {"id_rsa", "id_ed25519", "id_ecdsa", ".ds_store"}
            and not base.startswith(".coverage"), "PRIVATE_SOURCE_FILE")


def scan_archive(path, role):
    """One sequential tar pass; retain hashes, never weight/layer contents."""
    members, documents, links = {}, {}, {}
    total = 0
    try:
        with tarfile.open(path, "r|gz") as archive:
            for member in archive:
                name = safe_path(member.name, allow_root=member.isdir())
                if name == "." and member.isdir():
                    continue
                require(name not in members, "DUPLICATE_TAR_MEMBER")
                require(len(members) < 200000, "TOO_MANY_TAR_MEMBERS")
                require(not member.mode & 0o6000, "PRIVILEGED_TAR_MODE")
                if member.isdir():
                    members[name] = {"type": "directory"}
                    continue
                if member.issym() or member.islnk():
                    require(role == "images", "SOURCE_OR_MODEL_LINK_FORBIDDEN")
                    links[name] = linked_path(member, name)
                    members[name] = {"type": "link"}
                    continue
                require(member.isfile(), "SPECIAL_TAR_MEMBER_FORBIDDEN")
                require(0 <= member.size <= 64 * 1024**3, "TAR_MEMBER_TOO_LARGE")
                total += member.size
                require(total <= 500 * 1024**3, "TAR_CONTENT_TOO_LARGE")
                if role == "source":
                    source_name_allowed(name)
                stream = archive.extractfile(member)
                require(stream is not None, "UNREADABLE_TAR_MEMBER")
                digest, read_bytes, tail = hashlib.sha256(), 0, b""
                keep = role == "images" and member.size <= JSON_LIMIT
                keep = keep or (role == "models" and (
                    name.endswith("/manifest.json") or name == "manifest.json"
                    or name.endswith("/LICENSE-INVENTORY.json")
                    or name.endswith("/LICENSE-DECLARATION.json")
                    or name.endswith("/quality-report.json")
                    or PurePosixPath(name).name in HISTORICAL_REPORT_NAMES))
                require(not keep or member.size <= JSON_LIMIT, "METADATA_TOO_LARGE")
                content = bytearray() if keep else None
                while chunk := stream.read(CHUNK):
                    digest.update(chunk)
                    read_bytes += len(chunk)
                    if role == "source":
                        if read_bytes == len(chunk):
                            require(not chunk.startswith(b"SQLite format 3\x00"),
                                    "SQLITE_CONTENT_IN_SOURCE")
                        window = tail + chunk
                        require(not PRIVATE_KEY.search(window), "PRIVATE_KEY_IN_SOURCE")
                        require(not CREDENTIAL.search(window), "LITERAL_CREDENTIAL_IN_SOURCE")
                        tail = window[-4096:]
                    if content is not None:
                        content.extend(chunk)
                require(read_bytes == member.size, "TRUNCATED_TAR_MEMBER")
                members[name] = {"type": "file", "bytes": read_bytes,
                                 "sha256": digest.hexdigest()}
                if content is not None:
                    try:
                        value = json.loads(content)
                    except (ValueError, UnicodeDecodeError):
                        require(role == "images", "INVALID_MODEL_METADATA_JSON")
                    else:
                        documents[name] = value
        for name, target in links.items():
            seen = {name}
            while target in links:
                require(target not in seen, "CYCLIC_TAR_LINK")
                seen.add(target)
                target = links[target]
            require(target in members, "DANGLING_TAR_LINK")
        # Even a contained link cannot be an ancestor of another archive member.
        for name in members:
            require(not any(parent.as_posix() in links
                            for parent in PurePosixPath(name).parents),
                    "TAR_MEMBER_BELOW_LINK")
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise VerificationError("UNREADABLE_ARCHIVE") from exc
    return members, documents, {"members": len(members), "bytes": total,
                                "safe_links": len(links)}


def regular(members, name):
    name = safe_path(name)
    require(name in members and members[name]["type"] == "file", "MISSING_REQUIRED_FILE")
    return members[name]


def verify_images(manifest, members, documents):
    expected, requested = {}, {}
    image_records = manifest.get("images", [])
    require(isinstance(image_records, list) and image_records, "INVALID_IMAGE_RECORDS")
    explicit_requests = any("requested_tag" in image for image in image_records
                            if isinstance(image, dict))
    for image in image_records:
        require(isinstance(image, dict), "INVALID_IMAGE_RECORD")
        image_id = image.get("id", "")
        require(image_id.startswith("sha256:") and SHA.fullmatch(image_id[7:]),
                "INVALID_IMAGE_ID")
        tags = image.get("tags", [])
        require(isinstance(tags, list) and all(isinstance(tag, str) for tag in tags),
                "INVALID_IMAGE_TAGS")
        if explicit_requests:
            tag = image.get("requested_tag")
            require(tag in IMAGE_TAGS and tag in tags and tag not in requested,
                    "INVALID_REQUESTED_IMAGE_TAG")
            requested[tag] = image_id
        for tag in tags:
            require(tag not in expected or expected[tag] == image_id, "AMBIGUOUS_IMAGE_TAG")
            expected[tag] = image_id
    if explicit_requests:
        require(set(requested) == set(IMAGE_TAGS), "MISSING_REQUESTED_IMAGE_TAG")
    else:
        # Legacy format2 exports had only inspect aliases. New exports are explicit.
        require(all(tag in expected for tag in IMAGE_TAGS), "MISSING_EXPECTED_IMAGE_TAG")
        requested = {tag: expected[tag] for tag in IMAGE_TAGS}
    entries = documents.get("manifest.json")
    require(isinstance(entries, list) and entries, "MISSING_DOCKER_SAVE_MANIFEST")
    actual, config_count = {}, 0
    for entry in entries:
        config_name = safe_path(entry.get("Config", ""))
        config_record = regular(members, config_name)
        image_id = "sha256:" + config_record["sha256"]
        require(config_name in documents, "INVALID_IMAGE_CONFIG_JSON")
        config = documents[config_name]
        require(isinstance(config, dict) and config.get("architecture") == "amd64"
                and config.get("os") == "linux", "WRONG_IMAGE_PLATFORM")
        # Do not display any image environment value, even in a failure report.
        for value in config.get("config", {}).get("Env", []):
            key, _, payload = value.partition("=")
            require(not payload or not re.search(r"(?i)(TOKEN|PASSWORD|SECRET|API_KEY)$", key),
                    "CREDENTIAL_ENV_EMBEDDED_IN_IMAGE")
        for layer in entry.get("Layers", []):
            regular(members, layer)
        for tag in entry.get("RepoTags") or []:
            require(tag in expected and expected[tag] == image_id,
                    "DOCKER_TAG_CONFIG_ID_MISMATCH")
            require(tag not in actual or actual[tag] == image_id, "AMBIGUOUS_DOCKER_TAG")
            actual[tag] = image_id
        config_count += 1
    require(all(actual.get(tag) == requested[tag] for tag in IMAGE_TAGS),
            "MISSING_SAVED_CANONICAL_IMAGE")
    for name, record in members.items():
        if record["type"] == "file" and re.fullmatch(r"blobs/sha256/[0-9a-f]{64}", name):
            require(record["sha256"] == name.rsplit("/", 1)[1], "OCI_BLOB_HASH_MISMATCH")
    return {"status": "PASS", "canonical_tags_verified": len(IMAGE_TAGS),
            "canonical_image_ids": {tag: actual[tag] for tag in IMAGE_TAGS},
            "configs_verified": config_count, "platform": "linux/amd64",
            "requested_tag_metadata": ("EXPLICIT_REQUESTED_TAGS" if explicit_requests
                                       else "LEGACY_INSPECT_ALIASES"),
            "layer_validation": "REFERENCED_MEMBERS_AND_OCI_BLOB_HASHES; NO_DIFF_ID_REPLAY",
            "metadata_only_aliases": sorted(set(expected) - set(actual))}


def record_list(document):
    records = document.get("files", [])
    if isinstance(records, dict):
        records = [{"path": name, **(value if isinstance(value, dict)
                    else {"sha256": value})} for name, value in records.items()]
    require(isinstance(records, list) and records, "EMPTY_MODEL_INVENTORY")
    return records


def verify_inventory(name, inventory, members):
    base = PurePosixPath(name).parent
    paths = set()
    for record in record_list(inventory):
        relative = safe_path(record.get("path", ""))
        target = (base / relative).as_posix()
        require(target not in paths, "DUPLICATE_MODEL_INVENTORY_MEMBER")
        actual = regular(members, target)
        require(SHA.fullmatch(record.get("sha256", ""))
                and actual["sha256"] == record["sha256"], "MODEL_MEMBER_HASH_MISMATCH")
        require("bytes" in record and actual["bytes"] == record["bytes"],
                "MODEL_MEMBER_SIZE_MISMATCH")
        paths.add(target)
    return paths


def verify_models(members, documents, source_members, generator_subdir):
    manifests = {name: value for name, value in documents.items()
                 if name.endswith("/manifest.json") or name == "manifest.json"}
    required = ("embedder/manifest.json", "reranker/manifest.json",
                generator_subdir + "/manifest.json",
                "docling/docling-project--docling-layout-heron/manifest.json",
                "docling/docling-project--docling-models/manifest.json")
    require(all(name in manifests for name in required), "REQUIRED_MODEL_MANIFEST_MISSING")
    covered, inventories = set(manifests), {}
    for name, value in manifests.items():
        require(isinstance(value, dict) and value.get("repository") and value.get("revision"),
                "UNPINNED_MODEL_MANIFEST")
        covered.update(verify_inventory(name, value, members))
    for name, value in documents.items():
        if name.endswith("/LICENSE-INVENTORY.json"):
            require(isinstance(value, dict), "INVALID_LICENSE_INVENTORY")
            paths = verify_inventory(name, value, members)
            require(any("LICENSE" in PurePosixPath(p).name
                        and not p.endswith(".json") for p in paths), "LICENSE_TEXT_MISSING")
            require(any(p.endswith("/LICENSE-DECLARATION.json") for p in paths),
                    "LICENSE_DECLARATION_MISSING")
            covered.add(name)
            covered.update(paths)
            inventories[name] = value
    for role in ("embedder", "reranker"):
        inventory_name = "licenses/" + role + "/LICENSE-INVENTORY.json"
        own_licensed = any(p.startswith(role + "/") and "license" in p.lower()
                           and not p.endswith(".json") for p in covered)
        require(own_licensed or inventory_name in inventories, "CPU_MODEL_LICENSE_MISSING")
        if inventory_name in inventories:
            sidecar = inventories[inventory_name]
            artifact = manifests[role + "/manifest.json"]
            require(sidecar.get("repository") == artifact["repository"]
                    and sidecar.get("revision") == artifact["revision"],
                    "LICENSE_ARTIFACT_PIN_MISMATCH")
    for name in manifests:
        base = str(PurePosixPath(name).parent) + "/"
        if base.startswith("generator") or base.startswith("docling/"):
            require(any(p.startswith(base) and "license" in p.lower()
                        and not p.endswith(".json") for p in covered), "MODEL_LICENSE_MISSING")
    selected_name = generator_subdir + "/quality-report.json"
    selected = documents.get(selected_name)
    require(isinstance(selected, dict) and selected.get("status") == "PASS",
            "CURRENT_GENERATOR_QUALITY_REVIEW_MISSING")
    selected_manifest = manifests[generator_subdir + "/manifest.json"]
    require(selected.get("artifact_fingerprint")
            == regular(members, generator_subdir + "/manifest.json")["sha256"]
            and selected.get("repository") == selected_manifest["repository"]
            and selected.get("revision") == selected_manifest["revision"],
            "QUALITY_REVIEW_MODEL_MISMATCH")
    for field, name in (
        ("pipeline_source_sha256", "backend/korsik/retrieval.py"),
        ("generation_runtime_source_sha256", "services/model_server/app.py"),
    ):
        require(selected.get(field) == regular(source_members, name)["sha256"],
                "QUALITY_REVIEW_SOURCE_MISMATCH")
    selected_review_sha = regular(members, selected_name)["sha256"]
    bundled_reviews = sorted(name for name, record in source_members.items()
                             if name.startswith("tests/model-quality/") and name.endswith(".json")
                             and record["type"] == "file" and record["sha256"] == selected_review_sha)
    require(bool(bundled_reviews), "BUNDLED_QUALITY_REPORT_MISMATCH")
    historical_reports = []
    for basename in HISTORICAL_REPORT_NAMES:
        model_path = generator_subdir + "/" + basename
        if model_path not in members:
            continue
        source_path = "tests/model-quality/" + basename
        require(source_path in source_members and source_members[source_path]["type"] == "file",
                "HISTORICAL_REPORT_SOURCE_MISSING")
        actual, counterpart = regular(members, model_path), source_members[source_path]
        require(actual["sha256"] == counterpart["sha256"] and actual["bytes"] == counterpart["bytes"],
                "HISTORICAL_REPORT_SOURCE_MISMATCH")
        require(isinstance(documents.get(model_path), (dict, list)), "INVALID_HISTORICAL_REPORT_JSON")
        covered.add(model_path)
        historical_reports.append({"path": model_path, "bytes": actual["bytes"], "sha256": actual["sha256"],
                                   "source": source_path, "status": "HISTORICAL_NOT_CURRENT_QUALITY"})
    for name in members:
        if name.endswith("/quality-report.json"):
            covered.add(name)  # Optional FAIL reports remain explicit historical evidence.
    extras = {name for name, value in members.items()
              if value["type"] == "file" and name not in covered}
    require(not extras, "UNINVENTORIED_MODEL_FILES")
    return {"status": "PASS", "model_manifests_verified": len(manifests),
            "license_sidecars_verified": len(inventories),
            "selected_generator": generator_subdir,
            "quality_review_scope": selected.get("scope"),
            "selected_quality_report_sha256": selected_review_sha,
            "matching_source_quality_reports": bundled_reviews,
            "historical_report_inventory": historical_reports,
            "current_source_bound_quality_report": True}


def verify_bundle(bundle, generator_subdir="generator-4b"):
    started = time.monotonic()
    report = {"schema": "korsik-offline-bundle-verification-v1",
              "checked_at_utc": datetime.now(timezone.utc).isoformat(),
              "status": "FAIL", "checks": {}, "secrets_printed": False,
              "archive_extraction": False, "docker_load": False,
              "runtime_or_network_calls": False,
              "scope": "DELIVERY_INTEGRITY_AND_CONTENT_INVENTORY_NOT_RUNTIME_ACCEPTANCE",
              "source_classification": "INTERNAL_SUPPLIED_POLICY_EXAMPLES_AND_SYNTHETIC_QA",
              "manifest_authenticity": "TRUSTED_OPERATOR_COPY_REQUIRED_UNSIGNED_MANIFEST"}
    try:
        require(bundle.is_dir() and not bundle.is_symlink(), "INVALID_BUNDLE_DIRECTORY")
        manifest_path = bundle / "manifest.json"
        require(manifest_path.is_file() and not manifest_path.is_symlink()
                and manifest_path.stat().st_size <= JSON_LIMIT, "INVALID_BUNDLE_MANIFEST")
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
        report["manifest_sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
        require(manifest.get("format") == 2 and manifest.get("secrets_included") is False
                and manifest.get("runtime_downloads_required") is False,
                "INVALID_BUNDLE_DECLARATIONS")
        entries = manifest.get("files", [])
        require(isinstance(entries, list) and len(entries) == len(ARCHIVES),
                "INVALID_ARCHIVE_LIST")
        records = {entry["file"]: entry for entry in entries}
        require(set(records) == set(ARCHIVES), "INVALID_ARCHIVE_LIST")
        report["archives"] = []
        for name in ARCHIVES:
            path = bundle / name
            expected = records[name]
            require(path.is_file() and not path.is_symlink(), "ARCHIVE_MISSING")
            require(path.stat().st_size == expected.get("bytes"), "ARCHIVE_SIZE_MISMATCH")
            require(SHA.fullmatch(expected.get("sha256", ""))
                    and sha256_file(path) == expected["sha256"], "ARCHIVE_SHA256_MISMATCH")
            report["archives"].append({"file": name, "bytes": path.stat().st_size,
                                       "sha256": expected["sha256"]})
        report["checks"]["archive_sha256_and_size"] = {"status": "PASS", "archives": 3}
        source_members, _, source_stats = scan_archive(bundle / "source.tar.gz", "source")
        report["checks"]["source"] = {"status": "PASS", **source_stats,
            "credential_markers_found": 0, "not_public_sanitized": True}
        image_members, image_docs, image_stats = scan_archive(bundle / "images.tar.gz", "images")
        report["checks"]["images"] = {**verify_images(manifest, image_members, image_docs),
                                     **image_stats}
        report["canonical_image_ids"] = report["checks"]["images"]["canonical_image_ids"]
        model_members, model_docs, model_stats = scan_archive(bundle / "models.tar.gz", "models")
        report["checks"]["models"] = {**verify_models(model_members, model_docs,
                                                     source_members, generator_subdir),
                                     **model_stats}
        report["status"] = "PASS"
    except VerificationError as exc:
        report["failure_code"] = exc.code
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        report["failure_code"] = "MALFORMED_OR_UNREADABLE_BUNDLE"
    report["seconds"] = round(time.monotonic() - started, 3)
    return report


def bind_release_identity(report, manifest_bytes, runtime_images, quality_report_sha256):
    """Bind an actual PASS to the exact exported manifest and measured deployment.

    Integrity verification and runtime acceptance remain separate. This joins
    their identities so a valid older export cannot support a newer release.
    """
    require(report.get("status") == "PASS", "BUNDLE_REPORT_NOT_PASS")
    require(hashlib.sha256(manifest_bytes).hexdigest() == report.get("manifest_sha256"),
            "RELEASE_MANIFEST_REPORT_MISMATCH")
    manifest = json.loads(manifest_bytes)
    archives = {entry["file"]: {key: entry[key] for key in ("file", "bytes", "sha256")}
                for entry in manifest.get("files", [])}
    measured = {entry["file"]: entry for entry in report.get("archives", [])}
    require(set(archives) == set(ARCHIVES) and archives == measured,
            "RELEASE_ARCHIVE_REPORT_MISMATCH")
    canonical = report.get("canonical_image_ids", {})
    exported = {entry.get("requested_tag"): entry.get("id")
                for entry in manifest.get("images", [])}
    require(set(canonical) == set(IMAGE_TAGS) and canonical == exported,
            "RELEASE_IMAGE_MANIFEST_MISMATCH")
    roles = {"api": "korsik-app:local", "web": "korsik-web:local",
             "encoder": "korsik-models:cpu", "reranker": "korsik-models:cpu",
             "generator": "korsik-generator:local", "parser": "korsik-parser:structure"}
    require(all(runtime_images.get(role) == canonical[tag] for role, tag in roles.items()),
            "RELEASE_RUNTIME_IMAGE_MISMATCH")
    for role in ("worker", "answer-worker", "ingest-worker", "scheduler"):
        if role in runtime_images:
            require(runtime_images[role] == canonical["korsik-app:local"],
                    "RELEASE_WORKER_IMAGE_MISMATCH")
    reviewed = report.get("checks", {}).get("models", {}).get("selected_quality_report_sha256")
    require(isinstance(quality_report_sha256, str) and SHA.fullmatch(quality_report_sha256)
            and reviewed == quality_report_sha256, "RELEASE_QUALITY_REPORT_MISMATCH")
    return {"manifest_sha256": report["manifest_sha256"],
            "archives": [archives[name] for name in ARCHIVES],
            "canonical_image_ids": canonical,
            "runtime_image_bindings": {role: runtime_images[role] for role in roles},
            "selected_quality_report_sha256": reviewed}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--generator-subdir", default="generator-4b")
    parser.add_argument("--report", type=Path,
                        help="Write actual verification results here; never a fabricated template")
    args = parser.parse_args()
    try:
        safe_path(args.generator_subdir)
        require("/" not in args.generator_subdir, "INVALID_GENERATOR_SUBDIR")
    except VerificationError as exc:
        parser.error(exc.code)
    report = verify_bundle(args.bundle, args.generator_subdir)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.report.with_name(args.report.name + ".writing")
        with temporary.open("w") as target:
            json.dump(report, target, ensure_ascii=False, indent=2)
            target.write("\n")
        temporary.replace(args.report)
    print(json.dumps({"status": report["status"], "failure_code": report.get("failure_code"),
                      "seconds": report["seconds"], "checks_completed": len(report["checks"])}))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
