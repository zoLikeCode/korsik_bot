#!/usr/bin/env python3
"""Host operator evidence: deployed images, isolation, HTTPS and secret exposure.

Reads provisioned secrets to compare bytes, never prints their values. Run from
the deployed checkout, outside the application container, after a rollout.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import urllib.request


def command(*args, check=True):
    return subprocess.run(args, check=check, capture_output=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--secret-dir", required=True)
    parser.add_argument("--public-url", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    os.chdir(Path(__file__).resolve().parents[1])
    report = {
        "probe": "DEPLOYED_ISOLATION_TLS_SECRET_EXPOSURE",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "checks": [],
        "limitations": "Targeted checks; not a penetration test or company acceptance",
    }

    def record(name, ok, detail=None):
        report["checks"].append(
            {"name": name, "status": "PASS" if ok else "FAIL", "detail": detail}
        )

    config = json.loads(
        command("docker", "compose", "config", "--format", "json").stdout
    )
    project = config["name"]
    services = config["services"]
    inspected = {}
    for service in services:
        ids = (
            command("docker", "compose", "ps", "-a", "-q", service)
            .stdout.decode()
            .split()
        )
        if ids:
            inspected[service] = json.loads(
                command("docker", "inspect", ids[0]).stdout
            )[0]
    report["images"] = {name: obj["Image"] for name, obj in inspected.items()}
    expected = {
        "api",
        "web",
        "worker",
        "ingest-worker",
        "answer-worker",
        "scheduler",
        "max-connector",
        "postgres",
        "qdrant",
        "encoder",
        "reranker",
        "generator",
        "parser",
    }
    record(
        "services_running",
        all(inspected.get(s, {}).get("State", {}).get("Running") for s in expected),
    )
    record(
        "healthchecks",
        all(
            obj["State"].get("Health", {}).get("Status", "healthy") == "healthy"
            for name, obj in inspected.items()
            if name in expected
        ),
    )
    for name in (
        "backend",
        "models",
        "parsing",
        "frontend",
        "transport_db",
        "transport_control",
    ):
        network = json.loads(
            command("docker", "network", "inspect", f"{project}_{name}").stdout
        )[0]
        record("internal_network_" + name, network["Internal"] is True)
    private = {
        "postgres",
        "qdrant",
        "encoder",
        "reranker",
        "generator",
        "parser",
        "api",
    }
    record(
        "data_and_ai_have_no_host_ports",
        all(
            not obj["HostConfig"].get("PortBindings")
            for name, obj in inspected.items()
            if name in private
        ),
    )
    record(
        "app_nonroot_readonly",
        all(
            inspected[name]["HostConfig"]["ReadonlyRootfs"]
            and inspected[name]["Config"]["User"] == "10001:10001"
            for name in (
                "api",
                "worker",
                "ingest-worker",
                "answer-worker",
                "scheduler",
                "max-connector",
            )
        ),
    )
    # Tests actual route availability, including raw IP, rather than env alone.
    network_probe = """import json,socket
result={}
for name in ('huggingface.co','api.openai.com'):
 try: socket.getaddrinfo(name,443);result[name]=False
 except OSError: result[name]=True
try:
 s=socket.create_connection(('1.1.1.1',443),timeout=1);s.close();result['raw_ip']=False
except OSError: result['raw_ip']=True
print(json.dumps(result))
"""
    for name in ("api", "encoder", "reranker", "generator", "parser"):
        result = json.loads(
            command(
                "docker", "compose", "exec", "-T", name, "python", "-c", network_probe
            ).stdout
        )
        record("external_egress_blocked_" + name, all(result.values()), result)
    try:
        with urllib.request.urlopen(
            args.public_url.rstrip("/") + "/", timeout=20
        ) as response:
            record(
                "trusted_https",
                response.status == 200 and response.url.startswith("https://"),
            )
    except Exception as error:
        record("trusted_https", False, type(error).__name__)
    values = {
        p.name: p.read_bytes().strip()
        for p in Path(args.secret_dir).iterdir()
        if p.is_file()
    }
    values = {name: value for name, value in values.items() if len(value) >= 16}
    excluded = {
        ".git",
        ".venv",
        "node_modules",
        "dist",
        "data",
        "models",
        "secrets",
        ".secrets",
        "__pycache__",
    }
    hits = []

    def scan(label, data):
        for name, value in values.items():
            if value in data:
                hits.append({"location": label, "secret_name": name})

    for path in Path.cwd().rglob("*"):
        if not path.is_file() or any(
            part in excluded for part in path.relative_to(Path.cwd()).parts
        ):
            continue
        if path.stat().st_size <= 32 * 1024 * 1024:
            scan(str(path.relative_to(Path.cwd())), path.read_bytes())
    for name, obj in inspected.items():
        scan("container_env:" + name, json.dumps(obj["Config"].get("Env", [])).encode())
        logs = command("docker", "logs", "--tail", "3000", obj["Id"], check=False)
        scan("container_logs:" + name, logs.stdout + logs.stderr)
    with tempfile.TemporaryDirectory(prefix="korsik-bundle-check-") as temporary:
        command(
            "docker", "cp", inspected["web"]["Id"] + ":/usr/share/nginx/html", temporary
        )
        for path in Path(temporary).rglob("*"):
            if path.is_file():
                scan(
                    "deployed_frontend:" + str(path.relative_to(temporary)),
                    path.read_bytes(),
                )
    record(
        "no_provisioned_secret_in_source_bundle_env_or_recent_logs",
        not hits,
        {"matches": hits},
    )
    report["host_backend_fingerprint"] = hashlib.sha256(
        b"".join(
            path.as_posix().encode() + hashlib.sha256(path.read_bytes()).digest()
            for path in sorted(Path("backend/korsik").glob("*.py"))
        )
    ).hexdigest()
    runtime_fingerprint = """import hashlib,pathlib
root=pathlib.Path('/srv/korsik')
print(hashlib.sha256(b''.join(('backend/korsik/'+p.name).encode()+hashlib.sha256(p.read_bytes()).digest() for p in sorted(root.glob('*.py')))).hexdigest())
"""
    report["runtime_backend_fingerprint"] = (
        command(
            "docker",
            "compose",
            "exec",
            "-T",
            "api",
            "python",
            "-c",
            runtime_fingerprint,
        )
        .stdout.decode()
        .strip()
    )
    record(
        "host_backend_matches_running_image",
        report["host_backend_fingerprint"] == report["runtime_backend_fingerprint"],
    )
    for service, local_file, runtime_file in (
        ("api", "services/model_server/app.py", "/srv/runtime/model-server-app.py"),
        ("encoder", "services/model_server/app.py", "/srv/app.py"),
        ("reranker", "services/model_server/app.py", "/srv/app.py"),
        ("generator", "services/model_server/app.py", "/srv/app.py"),
        ("parser", "backend/korsik/parsing.py", "/srv/parsing.py"),
        ("parser", "services/model_server/parser_app.py", "/srv/parser_app.py"),
    ):
        expected_hash = hashlib.sha256(Path(local_file).read_bytes()).hexdigest()
        actual_hash = (
            command(
                "docker",
                "compose",
                "exec",
                "-T",
                service,
                "python",
                "-c",
                "import hashlib,pathlib;print(hashlib.sha256(pathlib.Path('"
                + runtime_file
                + "').read_bytes()).hexdigest())",
            )
            .stdout.decode()
            .strip()
        )
        record(
            "runtime_source_matches_" + service + "_" + Path(local_file).name,
            actual_hash == expected_hash,
        )
    report["status"] = (
        "PASS" if all(c["status"] == "PASS" for c in report["checks"]) else "FAIL"
    )
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({"status": report["status"], "checks": len(report["checks"])}))
    raise SystemExit(0 if report["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
