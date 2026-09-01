"""End-to-end test harness.

These tests drive the real `jenkins-axi` CLI as a subprocess, real config
resolution, real HTTP round-trips, real argparse dispatch, real subprocess
secret-tool, with the whole external world faked so we can observe exactly
what a user would experience:

  * a fake Jenkins HTTP server standing in for the real server, which records
    every request (method, path, Authorization header, body) so "it sent
    basic auth with the stored token" and "it POSTed the stop endpoint" are
    assertions on recorded requests;
  * a stub `secret-tool` binary on PATH, so "the token was read from stdin,
    never argv" is an assertion on the stub's log, and "a locked vault
    BLOCKS the lookup" is a sleeping stub under our fail-closed timeout.

Nothing here touches a network beyond localhost or a real secret store.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

# The installed console script (pyproject `jenkins-axi = "jenkins_axi.cli:main"`).
# Resolved from the real PATH before we shadow it with the stub bin dir.
JENKINS_AXI_BIN = shutil.which("jenkins-axi")


# --- fake Jenkins HTTP server ---------------------------------------------


@dataclass
class Recorder:
    """Shared state between the HTTP handler and the test."""

    routes: dict = field(
        default_factory=dict
    )  # bare path -> (status, body, content_type)
    requests: list = field(
        default_factory=list
    )  # (method, bare path, auth header, body)
    basic_auth: str | None = None  # expected Authorization header; None = no auth check
    post_status: int = 201
    post_location: str | None = "http://127.0.0.1:1/queue/item/123/"
    replay_status: int = 302
    crumb: dict | None = field(
        default_factory=lambda: {
            "crumb": "c0ffee",
            "crumbRequestField": "Jenkins-Crumb",
        }
    )


def make_handler(rec: Recorder):
    class Handler(BaseHTTPRequestHandler):
        def _bare_path(self) -> str:
            return self.path.split("?")[0]

        def _record(self, method: str, body: bytes | None = None):
            rec.requests.append(
                (
                    method,
                    self._bare_path(),
                    self.headers.get("Authorization"),
                    self.headers.get("Jenkins-Crumb"),
                    body,
                )
            )
            if (
                rec.basic_auth is not None
                and self.headers.get("Authorization") != rec.basic_auth
            ):
                return 401, "{}"
            return None, None

        def _send(self, status: int, body: str, content_type: str = "application/json"):
            payload = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            if rec.post_location and status == 201:
                self.send_header("Location", rec.post_location)
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            status, body = self._record("GET")
            if status:
                self._send(status, body)
                return
            path = self._bare_path()
            if path == "/crumbIssuer/api/json":
                if rec.crumb is None:
                    self._send(404, "{}")
                else:
                    self._send(200, json.dumps(rec.crumb))
                return
            route = rec.routes.get(path)
            if route is None:
                self._send(404, "<h2>HTTP ERROR 404</h2>", "text/html")
                return
            self._send(route[0], route[1], route[2])

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(n) if n else None
            status, err = self._record("POST", body)
            if status:
                self._send(status, err)
                return
            # Real endpoint: workflow-cps doRebuild (the /replay index is
            # the replay FORM, which a POST merely renders).
            if self._bare_path().endswith("/replay/rebuild"):
                if rec.replay_status == 302:
                    # Real doRebuild: queues the build, answers a 302 back to
                    # the build page (NOT a 201 + queue Location; that is
                    # buildWithParameters). urllib follows it as a GET of the
                    # build page, which routes like any GET.
                    self.send_response(302)
                    self.send_header("Location", "../..")
                    self.end_headers()
                else:
                    self._send(rec.replay_status, "{}")
                return
            self._send(rec.post_status, "{}")

        def log_message(self, *a):  # silence
            pass

    return Handler


@dataclass
class FakeJenkins:
    server: HTTPServer
    rec: Recorder

    @property
    def url(self) -> str:
        _host, port = self.server.server_address
        return f"http://127.0.0.1:{port}"

    def set_route(
        self,
        path: str,
        body: str,
        status: int = 200,
        content_type: str = "application/json",
    ):
        self.rec.routes[path] = (status, body, content_type)

    def set_route_json(self, path: str, data: dict, status: int = 200):
        self.set_route(path, json.dumps(data), status, "application/json")

    def set_basic_auth(self, username: str, token: str):
        self.rec.basic_auth = (
            "Basic " + base64.b64encode(f"{username}:{token}".encode()).decode()
        )

    @property
    def requests(self) -> list:
        return self.rec.requests

    def posts(self) -> list[tuple[str, bytes | None]]:
        return [
            (path, body)
            for (method, path, _auth, _crumb, body) in self.requests
            if method == "POST"
        ]


@pytest.fixture
def fake_jenkins():
    rec = Recorder()
    server = HTTPServer(("127.0.0.1", 0), make_handler(rec))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield FakeJenkins(server, rec)
    finally:
        server.shutdown()


# --- stub secret-tool ------------------------------------------------------

# Mirrors the real libsecret split: the [path]/label/secret block on stdout,
# EVERY attribute.<key> line on STDERR (g_printerr in libsecret's
# tool/secret-tool.c). Reads stdin ONLY for `store` (the secret; searching
# must never block on an inherited stdin).
SECRET_TOOL_STUB = """\
#!/usr/bin/env python3
import os, sys, time
argv = sys.argv[1:]
with open(os.environ["JENKINS_AXI_TEST_SECRET_TOOL_LOG"], "a") as f:
    f.write("ARGS\\t" + "\\t".join(argv) + "\\n")
    if "store" in argv:
        f.write("STDIN\\t" + sys.stdin.read() + "\\n")
sleep = float(os.environ.get("JENKINS_AXI_TEST_SECRET_TOOL_SLEEP", "0"))
if sleep:
    time.sleep(sleep)
for line in os.environ.get("JENKINS_AXI_TEST_SEARCH_BLOCK", "").splitlines():
    stream = sys.stderr if line.startswith("attribute.") else sys.stdout
    stream.write(line + "\\n")
    stream.flush()
"""


def write_stub(bindir: Path, name: str, body: str) -> None:
    p = bindir / name
    p.write_text(body)
    p.chmod(0o755)


def search_block(url: str, username: str = "alec", secret: str = "testtoken123") -> str:
    """One `secret-tool search --all` item in the REAL libsecret layout
    (verified live + against tool/secret-tool.c 0.21.7): a bracketed header
    line starts each item, then label/secret/created/modified/schema on
    stdout and EVERY attribute line on stderr, and there are NO blank lines
    between items. Multi-item blocks are simply concatenated. The empty
    attribute lines KeePassXC always emits (UserName, URL) are included so
    empty-match shadowing is exercised, not assumed away."""
    return (
        f"[b017136cf4324982b3cb5dbe58173868]\n"
        "label = Jenkins (jenkins-axi)\n"
        f"secret = {secret}\n"
        "created = 2026-09-01 10:00:00\n"
        "modified = 2026-09-01 10:00:00\n"
        "schema = org.freedesktop.Secret.Generic\n"
        "attribute.service = jenkins-axi\n"
        f"attribute.url = {url}\n"
        "attribute.URL = \n"
        "attribute.UserName = \n"
        f"attribute.username = {username}\n"
        f"attribute.Uuid = b017136cf4324982b3cb5dbe58173868\n"
        # Every g_print line ends with a newline, INCLUDING the last item's,
        # so concatenating items keeps each header on its own line.
    )


# --- the harness object driven by tests -----------------------------------


@dataclass
class Harness:
    root: Path
    bindir: Path
    store_log: Path
    entry_url: str

    def run(
        self,
        *args: str,
        extra_env: dict | None = None,
        input: str | None = None,
        stderr: int | None = None,
        timeout: float = 30,
    ) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PATH"] = f"{self.bindir}{os.pathsep}{env.get('PATH', '')}"
        env["JENKINS_AXI_TEST_SECRET_TOOL_LOG"] = str(self.store_log)
        env["JENKINS_AXI_TEST_SEARCH_BLOCK"] = search_block(self.entry_url)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [JENKINS_AXI_BIN, *args],
            env=env,
            input=input,
            stdout=subprocess.PIPE,
            stderr=stderr if stderr is not None else subprocess.PIPE,
            text=True,
            check=False,
            timeout=timeout,
        )

    def stored_args(self) -> list[list[str]]:
        if not self.store_log.exists():
            return []
        return [line.split("\t") for line in self.store_log.read_text().splitlines()]


@pytest.fixture
def harness(tmp_path, fake_jenkins):
    if JENKINS_AXI_BIN is None:
        pytest.skip("`jenkins-axi` console script not installed; run under `uv run`")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    write_stub(bindir, "secret-tool", SECRET_TOOL_STUB)
    return Harness(
        root=tmp_path,
        bindir=bindir,
        store_log=tmp_path / "store.log",
        entry_url=fake_jenkins.url,
    )
