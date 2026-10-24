"""Operator/build-time artifact installer. Never invoked by runtime containers.

Use on an internet-connected preparation host; transfer the resulting directory
into the private contour. Pinned repositories, byte hashes and licenses travel
with the weights. Runtime verifies this manifest before loading.
"""
import argparse
import concurrent.futures
import hashlib
import json
from pathlib import Path
import urllib.request
import uuid

CATALOG = {
    "generator-4b": {"repository": "Qwen/Qwen3.5-4B", "revision": "851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a",
                     "weights_sha256_by_path": {
                         "model.safetensors-00001-of-00002.safetensors": "26a93f066e1916adb13453dae5a0c707c0fbc71299ed98779571a907b8e74c61",
                         "model.safetensors-00002-of-00002.safetensors": "cb544bd9bfae93dc59b0f22b292f5933573854a7f9b97835c67060d7d910e188"}},
    "generator-2b": {"repository": "Qwen/Qwen3.5-2B", "revision": "15852e8c16360a2fea060d615a32b45270f8a8fc",
                     "weights_sha256": "aa33250c4fc64891ddfaba3a314fd9542ea371843c387178b425fbcc5ed680b1"},
    "embedder": {"repository": "ibm-granite/granite-embedding-97m-multilingual-r2", "revision": "835ad14087e140460703cf0fae09f97d469d65c2",
                 "onnx_sha256": "68e592b160673d30250824c1116bc6ab33f70efb22b97c9e1d7ce1e69c1c9d70"},
    "reranker": {"repository": "Horizon-Labs/multilingual-reranker-small", "revision": "d35a2781eeb3de1d971ad328165277bdf5712e35",
                 "onnx_sha256": "dfc9f8faff4f8fdff033a20692d0b20807b1d3dd842106ebe01da315fa127f63"},
    "generator": {"repository": "Qwen/Qwen3.5-0.8B", "revision": "2fc06364715b967f1860aea9cf38778875588b17"},
}


def json_get(url):
    with urllib.request.urlopen(url, timeout=120) as response:
        return json.load(response)


def install(role, target):
    entry = CATALOG[role]
    repo, revision = entry["repository"], entry["revision"]
    info = json_get(f"https://huggingface.co/api/models/{repo}/revision/{revision}?blobs=true")
    selected = []
    for file in info["siblings"]:
        name = file["rfilename"]
        basename = Path(name).name
        common = "/" not in name and (name.endswith((".json", ".jinja", ".txt", ".model")) or basename.lower().startswith(("license", "licence", "notice", "readme")))
        weight = (name.endswith(".safetensors") and "/" not in name) if role.startswith("generator") else name == "onnx/model.onnx"
        if common or weight:
            selected.append(file)
    selected = list({file["rfilename"]: file for file in selected}.values())
    declared = info.get("cardData", {}).get("license", [])
    declared = [declared] if isinstance(declared, str) else declared
    if not declared:
        raise RuntimeError(f"Missing upstream license declaration in pinned repository {repo}")
    folder = Path(target) / role
    folder.mkdir(parents=True, exist_ok=True)
    records = []
    def download(file):
        upstream = file["rfilename"]
        relative = "model.onnx" if upstream == "onnx/model.onnx" else upstream
        destination = folder / relative
        temp = destination.with_suffix(destination.suffix + "." + uuid.uuid4().hex + ".partial")
        expected = (file.get("lfs") or {}).get("sha256")
        if upstream == "onnx/model.onnx":
            expected = entry["onnx_sha256"]
        elif upstream.endswith(".safetensors") and entry.get("weights_sha256"):
            expected = entry["weights_sha256"]
        elif upstream in entry.get("weights_sha256_by_path", {}):
            expected = entry["weights_sha256_by_path"][upstream]
        if expected and destination.is_file():
            old_hash = hashlib.sha256()
            with destination.open("rb") as old:
                while chunk := old.read(1024*1024):
                    old_hash.update(chunk)
            previous = old_hash.hexdigest()
            if previous == expected:
                return {"path": relative, "upstream_path": upstream, "bytes": destination.stat().st_size, "sha256": previous, "upstream_lfs_sha256": expected}
        hasher = hashlib.sha256()
        count = 0
        with urllib.request.urlopen(f"https://huggingface.co/{repo}/resolve/{revision}/{upstream}", timeout=120) as response, temp.open("wb") as out:
            while data := response.read(1024 * 1024):
                out.write(data)
                hasher.update(data)
                count += len(data)
        actual = hasher.hexdigest()
        if expected and actual != expected:
            temp.unlink(missing_ok=True)
            raise RuntimeError(f"Hash mismatch: {repo}/{upstream}")
        temp.replace(destination)
        return {"path": relative, "upstream_path": upstream, "bytes": count, "sha256": actual, "upstream_lfs_sha256": expected}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        records = list(pool.map(download, selected))
    if not any(file["rfilename"].lower().startswith(("license", "licence")) for file in selected):
        # Some official repositories declare the license in their pinned model
        # card without shipping its text. Preserve both declaration and full text.
        standards = {"apache-2.0": "Apache-2.0", "cdla-permissive-2.0": "CDLA-Permissive-2.0"}
        for license_id in declared:
            if license_id not in standards:
                raise RuntimeError("Unsupported declared model license " + license_id)
            url = "https://raw.githubusercontent.com/spdx/license-list-data/main/text/" + standards[license_id] + ".txt"
            raw = urllib.request.urlopen(url, timeout=60).read()
            name = "LICENSE-" + standards[license_id] + ".txt"
            (folder / name).write_bytes(raw)
            records.append({"path": name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "source_url": url})
    declaration = json.dumps({"repository": repo, "revision": revision, "declared_license": declared}, ensure_ascii=False).encode()
    (folder / "LICENSE-DECLARATION.json").write_bytes(declaration)
    records.append({"path": "LICENSE-DECLARATION.json", "bytes": len(declaration), "sha256": hashlib.sha256(declaration).hexdigest()})
    manifest = {"schema": "korsik-local-model-v1", "repository": repo, "revision": revision, "role": "generator" if role.startswith("generator") else role, "variant": role,
                "files": sorted(records, key=lambda r: r["path"]), "runtime_network_required": False}
    (folder / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    return {"role": role, "files": len(records), "bytes": sum(r["bytes"] for r in records)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=[*CATALOG, "all"], default="all")
    parser.add_argument("--target", required=True)
    args = parser.parse_args()
    for role in ["embedder", "reranker", "generator-4b"] if args.role == "all" else [args.role]:
        print(json.dumps(install(role, args.target)))
