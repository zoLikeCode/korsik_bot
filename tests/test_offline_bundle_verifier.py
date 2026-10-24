"""Tiny synthetic archives verify the operator CLI, never delivery acceptance."""

import hashlib
import copy
import gzip
import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import subprocess
import unittest


SPEC = importlib.util.spec_from_file_location(
    "bundle_verifier", Path(__file__).parents[1] / "scripts/verify-offline-bundle.py"
)
VERIFIER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFIER)


def encoded(value):
    return json.dumps(value, sort_keys=True).encode()


def checksum(value):
    return hashlib.sha256(value).hexdigest()


class SyntheticBundle:
    def __init__(self, root):
        self.root = root
        self.files = {"source": {}, "images": {}, "models": {}}
        self.links = {"source": {}, "images": {}, "models": {}}
        self.files["source"] = {
            ".env.example": b"SECRET_DIR=./secrets\n",
            "backend/korsik/retrieval.py": b"synthetic retrieval source\n",
            "services/model_server/app.py": b"synthetic renderer source\n",
            "seed/examples/policy.pptx": b"synthetic supplied policy fixture",
        }
        self.image_records = []
        docker_entries = []
        for number, tag in enumerate(VERIFIER.IMAGE_TAGS):
            config = encoded({"architecture": "amd64", "os": "linux",
                              "config": {"Env": ["FIXTURE_ID=" + str(number)]}})
            name = checksum(config) + ".json"
            self.files["images"][name] = config
            docker_entries.append({"Config": name, "RepoTags": [tag], "Layers": []})
            self.image_records.append({"requested_tag": tag, "id": "sha256:" + checksum(config),
                                       "tags": [tag], "digests": []})
        self.files["images"]["manifest.json"] = encoded(docker_entries)
        model_roots = ("embedder", "reranker", "generator-4b",
                       "docling/docling-project--docling-layout-heron",
                       "docling/docling-project--docling-models")
        for base in model_roots:
            values = {"model.bin": b"tiny synthetic weight fixture"}
            if base not in {"embedder", "reranker"}:
                values["LICENSE.txt"] = b"Synthetic full license fixture"
            records = []
            for name, content in values.items():
                self.files["models"][base + "/" + name] = content
                records.append({"path": name, "bytes": len(content), "sha256": checksum(content)})
            manifest = {"repository": "fixture/" + base, "revision": "pinned-revision",
                        "files": records}
            self.files["models"][base + "/manifest.json"] = encoded(manifest)
        for role in ("embedder", "reranker"):
            records = []
            base = "licenses/" + role
            for name, content in {"LICENSE.txt": b"Synthetic full license fixture",
                                  "LICENSE-DECLARATION.json": encoded({"license": "fixture"})}.items():
                self.files["models"][base + "/" + name] = content
                records.append({"path": name, "bytes": len(content), "sha256": checksum(content)})
            self.files["models"][base + "/LICENSE-INVENTORY.json"] = encoded({
                "repository": "fixture/" + role, "revision": "pinned-revision", "files": records})
        quality = {"status": "PASS", "scope": "SYNTHETIC_UNIT_FIXTURE_NOT_REAL_QUALITY",
                   "repository": "fixture/generator-4b", "revision": "pinned-revision",
                   "artifact_fingerprint": checksum(self.files["models"]["generator-4b/manifest.json"]),
                   "pipeline_source_sha256": checksum(self.files["source"]["backend/korsik/retrieval.py"]),
                   "generation_runtime_source_sha256": checksum(self.files["source"]["services/model_server/app.py"])}
        self.files["models"]["generator-4b/quality-report.json"] = encoded(quality)
        self.files["source"]["tests/model-quality/qwen-4b-release10.json"] = encoded(quality)

    def write(self):
        records = []
        for role, values in self.files.items():
            path = self.root / (role + ".tar.gz")
            with tarfile.open(path, "w:gz") as archive:
                for name, data in values.items():
                    member = tarfile.TarInfo(name)
                    member.size = len(data)
                    member.mode = 0o644
                    archive.addfile(member, io.BytesIO(data))
                for name, target in self.links[role].items():
                    member = tarfile.TarInfo(name)
                    member.type = tarfile.SYMTYPE
                    member.linkname = target
                    member.mode = 0o777
                    archive.addfile(member)
            records.append({"file": path.name, "bytes": path.stat().st_size,
                            "sha256": VERIFIER.sha256_file(path)})
        (self.root / "manifest.json").write_text(json.dumps({
            "format": 2, "files": records, "images": self.image_records,
            "secrets_included": False, "runtime_downloads_required": False}))


class OfflineBundleVerifierTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fixture = SyntheticBundle(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def result(self):
        self.fixture.write()
        return VERIFIER.verify_bundle(self.root)

    def test_complete_small_bundle_verifies_without_runtime_or_extraction(self):
        result = self.result()
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(result["checks"]["images"]["canonical_tags_verified"], 9)
        self.assertEqual(result["checks"]["images"]["requested_tag_metadata"],
                         "EXPLICIT_REQUESTED_TAGS")
        self.assertEqual(result["checks"]["models"]["model_manifests_verified"], 5)
        self.assertEqual(result["checks"]["models"]["license_sidecars_verified"], 2)
        self.assertFalse(result["docker_load"])
        self.assertFalse(result["archive_extraction"])
        self.assertFalse(result["runtime_or_network_calls"])
        self.assertEqual(result["manifest_sha256"], checksum((self.root / "manifest.json").read_bytes()))
        self.assertEqual({item["file"] for item in result["archives"]}, set(VERIFIER.ARCHIVES))
        self.assertEqual(result["canonical_image_ids"], {item["requested_tag"]: item["id"] for item in self.fixture.image_records})

    def test_release_binds_exact_export_manifest_runtime_images_and_quality(self):
        result = self.result()
        canonical = result["canonical_image_ids"]
        runtime = {"api": canonical["korsik-app:local"], "web": canonical["korsik-web:local"],
                   "encoder": canonical["korsik-models:cpu"], "reranker": canonical["korsik-models:cpu"],
                   "generator": canonical["korsik-generator:local"], "parser": canonical["korsik-parser:structure"],
                   "answer-worker": canonical["korsik-app:local"]}
        manifest = (self.root / "manifest.json").read_bytes()
        quality = checksum(self.fixture.files["models"]["generator-4b/quality-report.json"])
        identity = VERIFIER.bind_release_identity(result, manifest, runtime, quality)
        self.assertEqual(identity["runtime_image_bindings"]["api"], runtime["api"])
        self.assertEqual(identity["manifest_sha256"], checksum(manifest))
        with self.assertRaises(VERIFIER.VerificationError) as stale:
            VERIFIER.bind_release_identity(result, manifest + b"\n", runtime, quality)
        self.assertEqual(stale.exception.code, "RELEASE_MANIFEST_REPORT_MISMATCH")
        for role in runtime:
            with self.subTest(role=role):
                wrong = {**runtime, role: "sha256:" + "0" * 64}
                with self.assertRaises(VERIFIER.VerificationError) as mismatch:
                    VERIFIER.bind_release_identity(result, manifest, wrong, quality)
                expected = "RELEASE_WORKER_IMAGE_MISMATCH" if role == "answer-worker" else "RELEASE_RUNTIME_IMAGE_MISMATCH"
                self.assertEqual(mismatch.exception.code, expected)
        wrong_archive = copy.deepcopy(result)
        wrong_archive["archives"][0]["bytes"] += 1
        with self.assertRaises(VERIFIER.VerificationError) as archive:
            VERIFIER.bind_release_identity(wrong_archive, manifest, runtime, quality)
        self.assertEqual(archive.exception.code, "RELEASE_ARCHIVE_REPORT_MISMATCH")
        with self.assertRaises(VERIFIER.VerificationError) as wrong_quality:
            VERIFIER.bind_release_identity(result, manifest, runtime, "0" * 64)
        self.assertEqual(wrong_quality.exception.code, "RELEASE_QUALITY_REPORT_MISMATCH")
        wrong_image = copy.deepcopy(result)
        wrong_image["canonical_image_ids"]["korsik-app:local"] = "sha256:" + "0" * 64
        with self.assertRaises(VERIFIER.VerificationError) as image:
            VERIFIER.bind_release_identity(wrong_image, manifest, runtime, quality)
        self.assertEqual(image.exception.code, "RELEASE_IMAGE_MANIFEST_MISMATCH")

    def test_compressed_archive_corruption_is_rejected_before_content(self):
        self.fixture.write()
        path = self.root / "models.tar.gz"
        value = bytearray(path.read_bytes())
        value[len(value) // 2] ^= 1
        path.write_bytes(value)
        result = VERIFIER.verify_bundle(self.root)
        self.assertEqual(result["failure_code"], "ARCHIVE_SHA256_MISMATCH")

    def test_containment_and_unsafe_links_reject_archive_escape(self):
        self.fixture.files["models"]["../outside"] = b"escape"
        self.assertEqual(self.result()["failure_code"], "UNSAFE_TAR_PATH")
        del self.fixture.files["models"]["../outside"]
        self.fixture.links["images"]["outside"] = "../../outside"
        self.assertEqual(self.result()["failure_code"], "UNSAFE_TAR_LINK")

    def test_only_contained_docker_links_are_allowed_and_no_descendants(self):
        config = next(name for name in self.fixture.files["images"] if name != "manifest.json")
        self.fixture.links["images"]["config-alias"] = config
        self.assertEqual(self.result()["status"], "PASS")
        self.fixture.files["images"]["config-alias/child"] = b"escape through ancestor"
        self.assertEqual(self.result()["failure_code"], "TAR_MEMBER_BELOW_LINK")

    def test_source_private_files_and_disguised_content_never_pass(self):
        cases = [
            (".env", b"TOKEN=fixture", "PRIVATE_ENV_FILE"),
            ("private-backup/archive.txt", b"fixture", "PRIVATE_SOURCE_DIRECTORY"),
            ("state.db-wal", b"fixture", "PRIVATE_SOURCE_FILE"),
            ("innocent.txt", b"SQLite format 3\x00fixture", "SQLITE_CONTENT_IN_SOURCE"),
            ("innocent.txt", b"-----BEGIN " + b"PRIVATE KEY-----", "PRIVATE_KEY_IN_SOURCE"),
            ("innocent.txt", b'password="' + b"0123456789abcdef" * 3 + b'"',
             "LITERAL_CREDENTIAL_IN_SOURCE"),
        ]
        for name, content, code in cases:
            with self.subTest(code=code):
                self.fixture.files["source"][name] = content
                result = self.result()
                self.assertEqual(result["failure_code"], code)
                self.assertNotIn(content.decode(errors="replace"), json.dumps(result))
                del self.fixture.files["source"][name]

    def test_image_config_hash_and_tag_mapping_are_checked(self):
        entries = json.loads(self.fixture.files["images"]["manifest.json"])
        config = entries[0]["Config"]
        value = json.loads(self.fixture.files["images"][config])
        value["config"]["Env"] = ["FIXTURE_ID=altered"]
        self.fixture.files["images"][config] = encoded(value)
        self.assertEqual(self.result()["failure_code"], "DOCKER_TAG_CONFIG_ID_MISMATCH")

    def test_inspect_aliases_need_not_all_be_saved_but_requested_tags_are_exact(self):
        self.fixture.image_records[0]["tags"].append("korsik-app:release-alias")
        result = self.result()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["checks"]["images"]["metadata_only_aliases"],
                         ["korsik-app:release-alias"])
        self.fixture.image_records[0]["requested_tag"] = "korsik-web:local"
        self.assertEqual(self.result()["failure_code"], "INVALID_REQUESTED_IMAGE_TAG")

    def test_mixed_requested_tag_metadata_is_rejected(self):
        del self.fixture.image_records[0]["requested_tag"]
        self.assertEqual(self.result()["failure_code"], "INVALID_REQUESTED_IMAGE_TAG")

    def test_compressed_oci_layers_and_blob_configs_use_content_digest_not_diff_id(self):
        raw_layer = io.BytesIO()
        with tarfile.open(fileobj=raw_layer, mode="w") as layer_archive:
            content = b"fixture app file"
            member = tarfile.TarInfo("srv/app.txt")
            member.size = len(content)
            layer_archive.addfile(member, io.BytesIO(content))
        raw = raw_layer.getvalue()
        compressed = gzip.compress(raw)
        layer_name = "blobs/sha256/" + checksum(compressed)
        self.assertNotEqual(checksum(raw), checksum(compressed))
        self.fixture.files["images"][layer_name] = compressed
        entries = json.loads(self.fixture.files["images"]["manifest.json"])
        for index, entry in enumerate(entries):
            previous_name = entry["Config"]
            config = json.loads(self.fixture.files["images"].pop(previous_name))
            config["rootfs"] = {"type": "layers", "diff_ids": ["sha256:" + checksum(raw)]}
            value = encoded(config)
            name = "blobs/sha256/" + checksum(value)
            self.fixture.files["images"][name] = value
            self.fixture.image_records[index]["id"] = "sha256:" + checksum(value)
            entry.update(Config=name, Layers=[layer_name])
        self.fixture.files["images"]["manifest.json"] = encoded(entries)
        self.assertEqual(self.result()["status"], "PASS")
        self.fixture.files["images"][layer_name] = compressed + b"altered"
        self.assertEqual(self.result()["failure_code"], "OCI_BLOB_HASH_MISMATCH")

    def test_wrong_platform_is_not_a_usable_delivery(self):
        entries = json.loads(self.fixture.files["images"]["manifest.json"])
        config = entries[0]["Config"]
        value = json.loads(self.fixture.files["images"][config])
        value["architecture"] = "arm64"
        self.fixture.files["images"][config] = encoded(value)
        self.assertEqual(self.result()["failure_code"], "WRONG_IMAGE_PLATFORM")

    def test_model_hash_and_undeclared_files_are_verified(self):
        original = self.fixture.files["models"]["embedder/model.bin"]
        self.fixture.files["models"]["embedder/model.bin"] = b"same archive correct, model wrong"
        self.assertEqual(self.result()["failure_code"], "MODEL_MEMBER_HASH_MISMATCH")
        self.fixture.files["models"]["embedder/model.bin"] = original
        self.fixture.files["models"]["unexpected.partial"] = b"unfinished"
        self.assertEqual(self.result()["failure_code"], "UNINVENTORIED_MODEL_FILES")

    def test_quality_report_must_pin_the_delivered_source(self):
        self.fixture.files["source"]["backend/korsik/retrieval.py"] += b"changed"
        self.assertEqual(self.result()["failure_code"], "QUALITY_REVIEW_SOURCE_MISMATCH")

    def test_current_quality_report_is_matched_by_exact_content_not_historical_name(self):
        old_name = "tests/model-quality/qwen-4b-release10.json"
        current = self.fixture.files["source"][old_name]
        historical = json.loads(current)
        historical["status"] = "FAIL"
        self.fixture.files["source"][old_name] = encoded(historical)
        self.fixture.files["source"]["tests/model-quality/qwen-4b-release12.json"] = current
        result = self.result()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["checks"]["models"]["matching_source_quality_reports"],
                         ["tests/model-quality/qwen-4b-release12.json"])
        del self.fixture.files["source"]["tests/model-quality/qwen-4b-release12.json"]
        self.assertEqual(self.result()["failure_code"], "BUNDLED_QUALITY_REPORT_MISMATCH")

    def test_license_sidecar_cannot_belong_to_another_revision(self):
        name = "licenses/embedder/LICENSE-INVENTORY.json"
        inventory = json.loads(self.fixture.files["models"][name])
        inventory["revision"] = "another-revision"
        self.fixture.files["models"][name] = encoded(inventory)
        self.assertEqual(self.result()["failure_code"], "LICENSE_ARTIFACT_PIN_MISMATCH")

    def test_required_nested_docling_manifest_cannot_be_silently_missing(self):
        del self.fixture.files["models"]["docling/docling-project--docling-models/manifest.json"]
        self.assertEqual(self.result()["failure_code"], "REQUIRED_MODEL_MANIFEST_MISSING")

    def test_only_exact_historical_reports_match_bundled_source_and_stay_historical(self):
        for basename in VERIFIER.HISTORICAL_REPORT_NAMES:
            content = encoded({"status": "FAIL", "scope": "SYNTHETIC_HISTORICAL_REPORT"})
            self.fixture.files["models"]["generator-4b/" + basename] = content
            self.fixture.files["source"]["tests/model-quality/" + basename] = content
        result = self.result()
        self.assertEqual(result["status"], "PASS", result)
        inventory = result["checks"]["models"]["historical_report_inventory"]
        self.assertEqual(len(inventory), 2)
        for record in inventory:
            self.assertEqual(record["status"], "HISTORICAL_NOT_CURRENT_QUALITY")
            content = self.fixture.files["models"][record["path"]]
            self.assertEqual(record["sha256"], checksum(content))
            self.assertEqual(record["bytes"], len(content))
            self.assertEqual(self.fixture.files["source"][record["source"]], content)

    def test_historical_report_requires_exact_source_counterpart(self):
        basename = VERIFIER.HISTORICAL_REPORT_NAMES[0]
        content = encoded({"status": "PASS", "scope": "HISTORICAL_NOT_CURRENT"})
        self.fixture.files["models"]["generator-4b/" + basename] = content
        self.assertEqual(self.result()["failure_code"], "HISTORICAL_REPORT_SOURCE_MISSING")
        self.fixture.files["source"]["tests/model-quality/" + basename] = content + b"\n"
        self.assertEqual(self.result()["failure_code"], "HISTORICAL_REPORT_SOURCE_MISMATCH")
        self.fixture.files["source"]["tests/model-quality/" + basename] = content.replace(b"PASS", b"FAIL")
        self.assertEqual(self.result()["failure_code"], "HISTORICAL_REPORT_SOURCE_MISMATCH")

    def test_arbitrary_or_other_directory_historical_json_is_not_whitelisted(self):
        content = encoded({"status": "FAIL"})
        for model_path, source_path in [
            ("generator-4b/notes.json", "tests/model-quality/notes.json"),
            ("generator-2b/qwen-4b-comparison.json", "tests/model-quality/qwen-4b-comparison.json"),
            ("generator-4b/nested/qwen-4b-comparison.json", "tests/model-quality/qwen-4b-comparison.json"),
        ]:
            with self.subTest(path=model_path):
                self.fixture.files["models"][model_path] = content
                self.fixture.files["source"][source_path] = content
                self.assertEqual(self.result()["failure_code"], "UNINVENTORIED_MODEL_FILES")
                del self.fixture.files["models"][model_path]
                del self.fixture.files["source"][source_path]


class ExportOutputSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.models = self.root / "models"
        self.models.mkdir()
        self.marker = self.root / "docker-was-called"
        binary = self.root / "bin"
        binary.mkdir()
        docker = binary / "docker"
        docker.write_text('#!/bin/sh\nprintf called > "$KORSIK_TEST_DOCKER_MARKER"\nexit 73\n')
        docker.chmod(0o755)
        self.environment = {**os.environ, "MODEL_DIR": str(self.models),
                            "PATH": str(binary) + os.pathsep + os.environ["PATH"],
                            "KORSIK_TEST_DOCKER_MARKER": str(self.marker)}
        self.script = Path(__file__).parents[1] / "scripts/export-offline-bundle.sh"

    def tearDown(self):
        self.temporary.cleanup()

    def run_export(self, output):
        return subprocess.run(["bash", str(self.script), str(output)], env=self.environment,
                              capture_output=True, text=True, timeout=10)

    def test_model_directory_and_descendants_are_rejected_before_any_archive(self):
        for output in (self.models, self.models / "new-delivery"):
            with self.subTest(output=output.name):
                result = self.run_export(output)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("outside pinned models", result.stderr)
                self.assertFalse(self.marker.exists())
        self.assertFalse((self.models / "new-delivery").exists())

    def test_existing_output_is_never_overwritten_or_removed(self):
        output = self.root / "existing"
        output.mkdir()
        sentinel = output / "keep.txt"
        sentinel.write_text("keep-existing-output")
        result = self.run_export(output)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be fresh", result.stderr)
        self.assertEqual(sentinel.read_text(), "keep-existing-output")
        self.assertFalse(self.marker.exists())

    def test_dangling_output_symlink_is_rejected_without_creating_its_target(self):
        target = self.root / "target"
        output = self.root / "link"
        output.symlink_to(target, target_is_directory=True)
        result = self.run_export(output)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(target.exists())
        self.assertFalse(self.marker.exists())

    def test_fresh_external_output_passes_guard_without_real_docker_execution(self):
        output = self.root / "new-delivery"
        result = self.run_export(output)
        self.assertEqual(result.returncode, 73)  # Explicit fake-Docker boundary.
        self.assertTrue(self.marker.exists())
        self.assertTrue(output.is_dir())
        self.assertEqual(output.stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
