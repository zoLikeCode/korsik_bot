"""No provider/MAX calls: test live-child failures and public-state withdrawal."""

import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

PATH = Path(__file__).resolve().parents[1] / "services/tunnel/run.py"
spec = importlib.util.spec_from_file_location("korsik_tunnel_supervisor", PATH)
tunnel = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = tunnel
spec.loader.exec_module(tunnel)


class Child:
    stdout = None

    def __init__(self, stubborn=False):
        self.returncode = None
        self.terminated = self.killed = self.waited = 0
        self.stubborn = stubborn

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated += 1
        if not self.stubborn:
            self.returncode = 0

    def kill(self):
        self.killed += 1
        self.returncode = -9

    def wait(self, timeout):
        self.waited += 1
        if self.returncode is None:
            raise subprocess.TimeoutExpired("synthetic-child", timeout)
        return self.returncode


@pytest.fixture
def fixture(tmp_path):
    probes = SimpleNamespace(origin=True, ready=True, public=True)
    clock = SimpleNamespace(now=0)
    children, commands = [], []

    def start(command, **kwargs):
        child = Child()
        children.append(child)
        commands.append(command)
        return child

    config = tunnel.Config(state=tmp_path / "public_url", startup_grace=0)
    sup = tunnel.Supervisor(
        config,
        clock=lambda: clock.now,
        process_factory=start,
        origin_probe=lambda: probes.origin,
        ready_probe=lambda: probes.ready,
        public_probe=lambda url: probes.public,
    )
    sup.step()
    sup.accept_line(sup.generation, "| https://first.trycloudflare.com |")
    sup.step()
    assert config.state.read_text() == "https://first.trycloudflare.com"
    yield sup, probes, clock, children, commands
    sup.request_stop()
    sup.run()


def tick(sup, clock, count=1):
    for _ in range(count):
        clock.now += 15
        sup.step()


def test_alive_child_loses_edge_ready_and_is_reaped_before_restart(fixture):
    sup, probes, clock, children, commands = fixture
    probes.ready = False
    tick(sup, clock)
    assert not sup.config.state.exists()
    assert children[0].poll() is None
    tick(sup, clock, 2)
    assert children[0].terminated == children[0].waited == 1
    assert sup.process is None and sup.next_start > clock.now
    tick(sup, clock)
    assert len(children) == 2 and sup.generation == 2
    assert "--metrics" in commands[0] and "127.0.0.1:2000" in commands[0]
    sup.accept_line(1, "| https://stale.trycloudflare.com |")
    assert sup.candidate is None


def test_public_dns_failure_restarts_a_locally_connected_child(fixture, monkeypatch):
    sup, _, clock, children, _ = fixture

    def dns_failure(*args, **kwargs):
        raise socket.gaierror("synthetic DNS failure")

    monkeypatch.setattr(tunnel.socket, "getaddrinfo", dns_failure)
    sup.public_probe = lambda url: tunnel.probe_html(url, external=True)
    tick(sup, clock, 3)
    assert children[0].terminated == 1 and children[0].waited == 1
    assert not sup.config.state.exists()
    assert (
        json.loads(sup.config.health.read_text())["reason"]
        == "PUBLIC_DNS_HTTPS_UNAVAILABLE"
    )


def test_origin_outage_does_not_churn_the_tunnel(fixture):
    sup, probes, clock, children, _ = fixture
    probes.origin = probes.ready = False
    tick(sup, clock, 10)
    assert len(children) == 1 and children[0].poll() is None
    assert children[0].terminated == 0 and sup.failures == 0
    assert not sup.config.state.exists()
    assert json.loads(sup.config.health.read_text())["reason"] == "ORIGIN_UNAVAILABLE"
    probes.origin = probes.ready = True
    tick(sup, clock)
    assert sup.config.state.read_text() == "https://first.trycloudflare.com"


def test_pending_startup_never_publishes_unvalidated_address(tmp_path):
    clock = SimpleNamespace(now=0)
    child = Child()
    config = tunnel.Config(state=tmp_path / "public_url", startup_grace=120)
    sup = tunnel.Supervisor(
        config,
        clock=lambda: clock.now,
        process_factory=lambda *args, **kwargs: child,
        origin_probe=lambda: True,
        ready_probe=lambda: False,
        public_probe=lambda url: False,
    )
    sup.step()
    sup.accept_line(sup.generation, "https://allocated.trycloudflare.com")
    tick(sup, clock, 7)
    assert child.terminated == 0 and not config.state.exists() and sup.failures == 0
    tick(sup, clock, 3)
    assert child.terminated == 1


def test_atomic_replace_never_exposes_partial_url(tmp_path, monkeypatch):
    destination = tmp_path / "public_url"
    destination.write_text("https://old.trycloudflare.com")
    original = Path.replace
    observations = []

    def observe(path, target):
        observations.append((destination.read_text(), path.read_text()))
        return original(path, target)

    monkeypatch.setattr(Path, "replace", observe)
    tunnel.atomic_write(destination, "https://new.trycloudflare.com")
    assert observations == [
        ("https://old.trycloudflare.com", "https://new.trycloudflare.com")
    ]
    assert destination.read_text() == "https://new.trycloudflare.com"
    assert destination.stat().st_mode & 0o777 == 0o644


def test_sigterm_stops_reaps_and_withdraws_without_restarting(fixture):
    sup, _, _, children, _ = fixture
    children[0].stubborn = True
    sup.request_stop(15, None)
    sup.run()
    assert len(children) == 1
    assert children[0].terminated == children[0].killed == 1
    assert children[0].waited == 2
    assert not sup.config.state.exists()
    assert json.loads(sup.config.health.read_text())["status"] == "STOPPED"


def test_shutdown_during_probe_cannot_publish_late_ready(fixture):
    sup, _, clock, _, _ = fixture
    sup.config.state.unlink()

    def stop_during_probe(url):
        sup.request_stop()
        return True

    sup.public_probe = stop_during_probe
    tick(sup, clock)
    assert not sup.config.state.exists()


def test_error_or_foreign_hostname_cannot_replace_candidate(fixture):
    sup, _, _, _, _ = fixture
    sup.accept_line(sup.generation, "ERR reconnect https://wrong.trycloudflare.com")
    sup.accept_line(sup.generation, "https://wrong.trycloudflare.com.attacker.invalid")
    assert sup.candidate == "https://first.trycloudflare.com"
    for value in [
        "http://x.trycloudflare.com",
        "https://x.trycloudflare.com:443",
        "https://name@x.trycloudflare.com",
        "https://x.trycloudflare.com/path",
        "https://x.trycloudflare.com?key=secret",
    ]:
        assert tunnel.public_url(value) is None


def test_health_rejects_stale_or_withdrawn_state(fixture):
    sup, probes, clock, _, _ = fixture
    assert tunnel.healthcheck(sup.config)
    value = json.loads(sup.config.health.read_text())
    value["checked_at"] = "2000-01-01T00:00:00+00:00"
    sup.config.health.write_text(json.dumps(value))
    assert not tunnel.healthcheck(sup.config)
    probes.public = False
    tick(sup, clock)
    assert not tunnel.healthcheck(sup.config)


def test_dns_and_http_helper_has_a_hard_process_deadline(monkeypatch):
    observed = []

    def stalled(command, **kwargs):
        observed.append(kwargs)
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(tunnel.subprocess, "run", stalled)
    assert not tunnel.bounded_html_probe("https://x.trycloudflare.com", external=True)
    assert observed[0]["timeout"] == 8
    assert observed[0]["stdout"] == subprocess.DEVNULL


def test_real_origin_probe_rejects_redirects_and_unbranded_html():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/redirect/":
                self.send_response(302)
                self.send_header("Location", "/")
                self.end_headers()
                return
            self.send_response(530 if self.path == "/unavailable/" else 200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            body = (
                '<title>Корсик</title><div id="root"></div>'
                if self.path in {"/", "/unavailable/"}
                else "<h1>Another service</h1>"
            )
            self.wfile.write(body.encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:" + str(server.server_port)
    try:
        assert tunnel.probe_html(base)
        assert not tunnel.probe_html(base + "/other")
        assert not tunnel.probe_html(base + "/redirect")
        assert not tunnel.probe_html(base + "/unavailable")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
