#!/usr/bin/env python3
"""Bounded host recovery of this Compose project's persistent services only.

No model calls, database credentials, pulls, builds, full-stack restarts or
container removals. Docker's restart policy does not recover unhealthy running
containers; this optional systemd timer covers that gap conservatively.
"""
from __future__ import annotations
import argparse
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

SERVICES = ("postgres", "qdrant", "api", "worker", "answer-worker", "ingest-worker", "scheduler", "encoder", "reranker", "generator", "parser", "web", "max-connector", "tunnel")
MODELS = {"encoder", "reranker", "generator", "parser"}
FAILURES = 3
COOLDOWN = 300
MAX_HOURLY = 2
MAX_ACTIONS = 2
INSPECT_FORMAT = ('{"id":{{json .Id}},"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
                  '"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
                  '"oneoff":{{json (index .Config.Labels "com.docker.compose.oneoff")}},'
                  '"status":{{json .State.Status}},"started_at":{{json .State.StartedAt}},'
                  '"health":{{if (index .State "Health")}}{{json (index (index .State "Health") "Status")}}{{else}}null{{end}}}')


class DockerError(RuntimeError):
    pass


def epoch(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, AttributeError):
        return None


def maintenance(state_dir):
    return any((state_dir / marker).exists() or (state_dir / marker).is_symlink() for marker in ("maintenance", "restore-in-progress"))


def plan(state, observations, timestamp, project="korsik", services=SERVICES):
    """Pure policy: reserve action budgets BEFORE executing any Docker change."""
    if timestamp < state.get("last_tick", 0):
        return [], {"status": "CLOCK_REGRESSION", "services": []}
    state["last_tick"] = timestamp
    data = state.setdefault("services", {})
    grouped = {service: [] for service in services}
    for row in observations:
        if (row.get("project") == project and row.get("service") in grouped
                and str(row.get("oneoff", "false")).casefold() != "true"
                and re.fullmatch(r"[a-f0-9]{12,64}", row.get("id", ""))):
            grouped[row["service"]].append(row)
    actions, summary = [], []
    for service in services:
        entry = data.setdefault(service, {})
        previous = entry.get("restart_timestamps", [])
        entry["restart_timestamps"] = [value for value in previous if timestamp - 3600 < value <= timestamp]
        rows = grouped[service]
        grace = 300 if service in MODELS else 180
        if len(rows) > 1:
            entry["failure_streak"] = 0
            summary.append({"service": service, "status": "AMBIGUOUS_CONTAINERS"})
            continue
        row = rows[0] if rows else None
        if row is None:
            entry.setdefault("missing_since", timestamp)
            age = timestamp - entry["missing_since"]
            failed = True
            status = "MISSING"
        else:
            signature = [row["id"], row.get("started_at")]
            if entry.get("incarnation") != signature:
                entry.update(incarnation=signature, first_seen=timestamp, failure_streak=0)
            entry.pop("missing_since", None)
            started = epoch(row.get("started_at"))
            # Unknown timestamps fail closed to the observed startup grace.
            age = timestamp - (started if started and 0 < started <= timestamp else entry["first_seen"])
            status = row.get("status", "unknown")
            healthy = status == "running" and row.get("health") in {None, "healthy"}
            failed = not healthy
            if status == "running" and row.get("health"):
                status = row["health"].upper()
        if not failed:
            entry["failure_streak"] = 0
            summary.append({"service": service, "status": "HEALTHY"})
            continue
        entry["failure_streak"] = int(entry.get("failure_streak", 0)) + 1
        reason = "STARTUP_GRACE" if age < grace else "FAILURE_STREAK" if entry["failure_streak"] < FAILURES else None
        if reason is None and timestamp - entry.get("last_action", -COOLDOWN) < COOLDOWN:
            reason = "COOLDOWN"
        if reason is None and len(entry["restart_timestamps"]) >= MAX_HOURLY:
            reason = "HOURLY_CAP"
        if reason is None and len(actions) >= MAX_ACTIONS:
            reason = "TICK_CAP"
        if reason is None:
            action = {"service": service, "action": "restart" if row else "create", "container_id": row["id"] if row else None, "started_at": row.get("started_at") if row else None}
            actions.append(action)
            entry["last_action"] = timestamp
            entry["restart_timestamps"].append(timestamp)
            entry["failure_streak"] = 0
            reason = "ACTION_RESERVED"
        summary.append({"service": service, "status": status, "decision": reason})
    return actions, {"status": "OBSERVED", "services": summary}


class Docker:
    def __init__(self, app_dir, env_file, project, services=SERVICES):
        self.app_dir, self.env_file, self.project, self.services = app_dir, env_file, project, tuple(services)

    def call(self, arguments, timeout=20):
        try:
            result = subprocess.run(["docker", *arguments], capture_output=True, text=True, timeout=timeout, cwd=self.app_dir)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DockerError("docker_command_unavailable") from exc
        if result.returncode:
            raise DockerError("docker_command_failed")
        return result.stdout

    def inspect(self):
        identifiers = self.call(["ps", "-a", "-q", "--filter", "label=com.docker.compose.project=" + self.project]).split()
        if not identifiers:
            return []
        if any(not re.fullmatch(r"[a-f0-9]{12,64}", value) for value in identifiers):
            raise DockerError("docker_invalid_inventory")
        rows = [json.loads(line) for line in self.call(["inspect", "--format", INSPECT_FORMAT, *identifiers]).splitlines() if line]
        if any(not isinstance(row, dict) or row.get("health") not in (None, "healthy", "starting", "unhealthy") for row in rows):
            raise DockerError("docker_invalid_health_observation")
        return rows

    def apply(self, action):
        if action["action"] == "restart":
            self.call(["restart", "--time", "45", action["container_id"]], timeout=70)
        else:
            self.call(["compose", "--project-name", self.project, "--env-file", str(self.env_file), "-f", str(self.app_dir / "compose.yaml"),
                       "up", "-d", "--no-build", "--no-deps", "--pull", "never", action["service"]], timeout=70)


def atomic_write(path, state):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".watchdog-", delete=False) as stream:
        temporary = Path(stream.name)
        os.chmod(temporary, 0o600)
        try:
            json.dump(state, stream, sort_keys=True)
            stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def tick(docker, state_dir, timestamp=None):
    if maintenance(state_dir):
        return {"status": "MAINTENANCE", "actions": []}
    path = state_dir / "watchdog-state.json"
    try:
        if path.exists() and (path.is_symlink() or path.stat().st_size > 1024 * 1024):
            raise ValueError("invalid_state_file")
        state = json.loads(path.read_text()) if path.exists() else {"schema_version": 1, "services": {}}
        if state.get("schema_version") != 1 or not isinstance(state.get("services"), dict):
            raise ValueError("invalid_state_schema")
    except (OSError, ValueError, TypeError):
        return {"status": "STATE_INVALID", "actions": []}
    if state.get("project", docker.project) != docker.project:
        return {"status": "STATE_PROJECT_MISMATCH", "actions": []}
    state["project"] = docker.project
    try:
        observations = docker.inspect()
    except (DockerError, ValueError, TypeError):
        return {"status": "DOCKER_UNAVAILABLE", "actions": []}
    try:
        actions, report = plan(state, observations, time.time() if timestamp is None else timestamp, docker.project, docker.services)
    except (ValueError, TypeError, AttributeError, OverflowError):
        return {"status": "STATE_INVALID", "actions": []}
    # Restart budget survives a crash after Docker accepted the restart.
    atomic_write(path, state)
    applied = []
    for action in actions:
        if maintenance(state_dir):
            applied.append({"service": action["service"], "result": "MAINTENANCE_SKIPPED"})
            break
        try:
            current = [row for row in docker.inspect() if row.get("project") == docker.project and row.get("service") == action["service"] and str(row.get("oneoff", "false")).casefold() != "true"]
            if action["action"] == "restart":
                if len(current) != 1 or current[0].get("id") != action["container_id"] or current[0].get("started_at") != action["started_at"]:
                    applied.append({"service": action["service"], "result": "INVENTORY_CHANGED"}); continue
                if current[0].get("status") == "running" and current[0].get("health") in {None, "healthy"}:
                    applied.append({"service": action["service"], "result": "RECOVERED_BEFORE_ACTION"}); continue
            elif current:
                applied.append({"service": action["service"], "result": "INVENTORY_CHANGED"}); continue
            if maintenance(state_dir):
                applied.append({"service": action["service"], "result": "MAINTENANCE_SKIPPED"}); break
            docker.apply(action)
            applied.append({"service": action["service"], "action": action["action"], "result": "REQUEST_ACCEPTED"})
        except (DockerError, ValueError, TypeError):
            applied.append({"service": action["service"], "result": "DOCKER_ACTION_FAILED"})
    report["actions"] = applied
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-dir", type=Path, default=Path("/opt/korsik/app"))
    parser.add_argument("--env-file", type=Path, default=Path("/opt/korsik/app/.env"))
    parser.add_argument("--state-dir", type=Path, default=Path("/opt/korsik/runtime"))
    parser.add_argument("--project", default="korsik")
    parser.add_argument("--allow-probe-project", action="store_true")
    parser.add_argument("--services", nargs="+", choices=SERVICES, default=SERVICES)
    args = parser.parse_args()
    if args.project != "korsik" and not (args.allow_probe_project and re.fullmatch(r"korsik-watchdog-probe-[a-z0-9-]{1,40}", args.project)):
        parser.error("Only korsik or an explicitly enabled isolated watchdog probe project is permitted")
    if not args.state_dir.is_dir() or args.state_dir.is_symlink():
        parser.error("Create a private state directory before running the watchdog")
    if not args.app_dir.is_dir() or not args.env_file.is_file():
        parser.error("Existing application directory and environment file are required")
    lock = os.open(args.state_dir / "watchdog.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"status": "LOCKED", "actions": []})); return
        report = tick(Docker(args.app_dir.resolve(), args.env_file.resolve(), args.project, tuple(dict.fromkeys(args.services))), args.state_dir)
        # Only metadata and fixed error codes are logged, never Docker stderr,
        # environment contents, model output, messages or employee identifiers.
        print(json.dumps(report, separators=(",", ":")))
    finally:
        os.close(lock)


if __name__ == "__main__":
    main()
