"""Supervise the temporary demo tunnel, including edge and public reachability.

Only validated HTTPS URLs are published. Recovery may change the hostname and
cannot repair an offline Vast host or make Quick Tunnel addresses permanent.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


def public_url(value):
    try:
        u = urlsplit(value)
        if (
            u.scheme == "https"
            and u.hostname
            and re.fullmatch(r"[a-z0-9-]+\.trycloudflare\.com", u.hostname)
            and u.port is None
            and not u.username
            and not u.password
            and u.path in {"", "/"}
            and not u.query
            and not u.fragment
        ):
            return "https://" + u.hostname
    except ValueError:
        pass
    return None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def probe_html(url, *, external=False):
    """Executed in a bounded child: libc DNS can outlast socket timeouts."""
    if external and public_url(url) != url.rstrip("/"):
        return False
    try:
        if external:
            socket.getaddrinfo(urlsplit(url).hostname, 443, type=socket.SOCK_STREAM)
        request = Request(
            url.rstrip("/") + "/",
            headers={
                "User-Agent": "Korsik-Tunnel-Health/1",
                "Cache-Control": "no-cache",
            },
        )
        with build_opener(NoRedirect).open(request, timeout=5) as response:
            body = response.read(262145)
            return (
                response.status == 200
                and len(body) <= 262144
                and "text/html" in response.headers.get("Content-Type", "")
                and "Корсик".encode() in body
                and b'id="root"' in body
            )
    except (OSError, ValueError, HTTPError, URLError):
        return False


def bounded_html_probe(url, *, external=False):
    try:
        result = subprocess.run(
            [
                sys.executable,
                __file__,
                "--probe-public" if external else "--probe-origin",
                url,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=8,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def edge_ready(url):
    try:
        with build_opener(NoRedirect).open(url, timeout=2) as response:
            return response.status == 200
    except (OSError, ValueError, HTTPError, URLError):
        return False


def atomic_write(path, value):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(value)
    temporary.chmod(0o644)
    temporary.replace(path)


@dataclass
class Config:
    state: Path = Path("/run/tunnel/public_url")
    origin: str = "http://web:8080"
    interval: float = 15
    startup_grace: float = 120
    failure_limit: int = 3
    restart_base: float = 5
    restart_max: float = 60
    metrics: str = "127.0.0.1:2000"

    @property
    def health(self):
        return self.state.with_name("health.json")


class Supervisor:
    def __init__(
        self,
        config,
        *,
        clock=time.monotonic,
        process_factory=subprocess.Popen,
        origin_probe=None,
        public_probe=None,
        ready_probe=None
    ):
        self.config, self.clock, self.process_factory = config, clock, process_factory
        self.origin_probe = origin_probe or (lambda: bounded_html_probe(config.origin))
        self.public_probe = public_probe or (
            lambda url: bounded_html_probe(url, external=True)
        )
        self.ready_probe = ready_probe or (
            lambda: edge_ready("http://" + config.metrics + "/ready")
        )
        self.stop_requested = threading.Event()
        self.lines = queue.Queue(maxsize=128)
        self.process = self.reader = None
        self.candidate = None
        self.failures = self.restarts = self.generation = 0
        self.started = self.next_start = 0
        self.ready_since = None
        config.state.parent.mkdir(parents=True, exist_ok=True)

    def state(self, status, reason=None):
        atomic_write(
            self.config.health,
            json.dumps(
                {
                    "status": status,
                    "reason": reason,
                    "generation": self.generation,
                    "url": self.candidate,
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                    "failure_count": self.failures,
                }
            ),
        )

    def withdraw(self, reason):
        self.config.state.unlink(missing_ok=True)
        self.ready_since = None
        self.state("UNAVAILABLE", reason)

    def start(self):
        self.config.state.unlink(missing_ok=True)
        self.candidate, self.failures, self.ready_since = None, 0, None
        self.generation += 1
        self.started = self.clock()
        self.state("STARTING")
        self.process = self.process_factory(
            [
                "/usr/local/bin/cloudflared",
                "tunnel",
                "--no-autoupdate",
                "--protocol",
                "http2",
                "--metrics",
                self.config.metrics,
                "--url",
                self.config.origin,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        process, generation = self.process, self.generation

        def read():
            for line in process.stdout:
                try:
                    self.lines.put_nowait((generation, line))
                except queue.Full:
                    pass

        if process.stdout is not None:
            self.reader = threading.Thread(target=read, daemon=True)
            self.reader.start()

    def accept_line(self, generation, line):
        # Old readers and ERR lines cannot overwrite the current allocated URL.
        if generation != self.generation or re.search(r"\bERR\b", line):
            return
        match = re.search(
            r"https://[a-z0-9-]+\.trycloudflare\.com(?![a-zA-Z0-9.-])", line
        )
        if match:
            self.candidate = public_url(match.group())

    def stop_child(self):
        process, self.process = self.process, None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        if self.reader:
            self.reader.join(timeout=1)
            self.reader = None
        if process.stdout is not None:
            process.stdout.close()

    def restart(self, reason):
        self.withdraw(reason)
        self.stop_child()
        delay = min(
            self.config.restart_max,
            self.config.restart_base * 2 ** min(self.restarts, 5),
        )
        self.restarts += 1
        self.next_start = self.clock() + delay
        print(
            "tunnel_restarting reason=" + reason + " backoff_seconds=" + str(delay),
            flush=True,
        )

    def step(self):
        if self.process is None:
            if self.clock() < self.next_start or self.stop_requested.is_set():
                return
            self.start()
        if self.process.poll() is not None:
            self.restart("CHILD_EXITED")
            return
        while True:
            try:
                generation, line = self.lines.get_nowait()
                self.accept_line(generation, line)
            except queue.Empty:
                break
        # Origin outages are not repaired by rotating a working external URL.
        origin_available = self.origin_probe()
        if self.stop_requested.is_set():
            return
        if not origin_available:
            self.failures = 0
            self.withdraw("ORIGIN_UNAVAILABLE")
            return
        reason = "EDGE_NOT_READY"
        ready = self.ready_probe()
        if ready and not self.candidate:
            reason = "WAITING_PUBLIC_ADDRESS"
        if ready and self.candidate:
            reachable = self.public_probe(self.candidate)
            if self.stop_requested.is_set():
                return
            if reachable:
                previous = (
                    self.config.state.read_text()
                    if self.config.state.exists()
                    else None
                )
                atomic_write(self.config.state, self.candidate)
                self.failures = 0
                if self.ready_since is None:
                    self.ready_since = self.clock()
                if self.clock() - self.ready_since >= 60:
                    self.restarts = 0
                self.state("READY")
                if previous != self.candidate:
                    print("tunnel_public_url=" + self.candidate, flush=True)
                return
            reason = "PUBLIC_DNS_HTTPS_UNAVAILABLE"
        self.withdraw(reason)
        if self.clock() - self.started >= self.config.startup_grace:
            self.failures += 1
            self.state("UNAVAILABLE", reason)
            if self.failures >= self.config.failure_limit:
                self.restart(reason)

    def request_stop(self, *_):
        self.stop_requested.set()

    def run(self):
        try:
            while not self.stop_requested.is_set():
                self.step()
                self.stop_requested.wait(self.config.interval)
        finally:
            self.config.state.unlink(missing_ok=True)
            self.stop_child()
            self.state("STOPPED")


def healthcheck(config):
    try:
        state = json.loads(config.health.read_text())
        age = (
            datetime.now(timezone.utc) - datetime.fromisoformat(state["checked_at"])
        ).total_seconds()
        return (
            state["status"] == "READY"
            and 0 <= age <= 90
            and public_url(config.state.read_text()) == state["url"]
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def main():
    config = Config(origin=os.getenv("TUNNEL_ORIGIN", "http://web:8080"))
    if len(sys.argv) == 3 and sys.argv[1] in {"--probe-public", "--probe-origin"}:
        return (
            0
            if probe_html(sys.argv[2], external=sys.argv[1] == "--probe-public")
            else 1
        )
    if sys.argv[1:] == ["--healthcheck"]:
        return 0 if healthcheck(config) else 1
    supervisor = Supervisor(config)
    signal.signal(signal.SIGTERM, supervisor.request_stop)
    signal.signal(signal.SIGINT, supervisor.request_stop)
    supervisor.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
