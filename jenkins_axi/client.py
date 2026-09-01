"""The Jenkins REST client: `get()` plus a whitelisted `post()`.

This module is the reviewable scope boundary. `get()` answers anything
Jenkins answers read-only. `post()` takes one of the four `PostEndpoint`
members and builds the path itself; the final path is then checked against
that endpoint's anchored regex, so the only POSTs that can ever leave this
process are:

  build                POST /job/<job>/build
  buildWithParameters  POST /job/<job>/buildWithParameters (form-encoded params)
  stop                 POST /job/<job>/<n>/stop
  replay               POST /job/<job>/<n>/replay

Anything else, config.xml writes, doDelete, createItem, plugin/credential
administration, the script console, is excluded by construction, not by
policy. An eyeball on the four regexes below is an eyeball on the entire
write surface.

Failures raise `AxiError` (with the fixing commands as hints) or `NotFound`;
the thin CLI turns those into structured stdout errors. The library never
exits itself.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from enum import StrEnum

from jenkins_axi.errors import AuthFailed, AxiError, NotFound, Unreachable

DEFAULT_TIMEOUT = 15.0


class ProgrammingError(Exception):
    """A call outside the whitelisted endpoints (see client.post)."""


@dataclass(frozen=True)
class JobRef:
    """An addressed job: `project` or `project/branch` for multibranch.

    Branch names commonly CONTAIN slashes (Jenkins lists them pre-encoded,
    e.g. `bugfix%2FDEV-1217`), so addressing collapses everything after the
    first `/` into one branch name. Segments are URL-quoted with `%` left
    untouched, so Jenkins' own pre-encoded names pass through and raw
    spaces-unicode get quoted on the way out.
    """

    parts: tuple[str, ...]

    @classmethod
    def from_string(cls, spec: str) -> JobRef:
        parts = tuple(s for s in spec.split("/") if s)
        if not parts:
            raise AxiError(
                "empty job address", "Address a job as project or project/branch"
            )
        if len(parts) > 2:
            parts = (parts[0], "/".join(parts[1:]))
        return cls(parts)

    @classmethod
    def from_parts(cls, *parts: str) -> JobRef:
        return cls(parts)

    @property
    def display(self) -> str:
        return "/".join(part.replace("%2F", "/") for part in self.parts)

    def url_path(self, suffix: str = "") -> str:
        quoted = [urllib.parse.quote(part, safe="%") for part in self.parts]
        return "/job/" + "/job/".join(quoted) + suffix


class PostEndpoint(StrEnum):
    """The four POSTs jenkins-axi can ever issue. This enum plus the anchored
    regexes in PATH_PATTERNS are the entire write surface."""

    BUILD = "build"
    BUILD_WITH_PARAMS = "buildWithParameters"
    STOP = "stop"
    REPLAY = "replay"  # POSTed at /<n>/replay/rebuild (see post)


PATH_PATTERNS: dict[PostEndpoint, re.Pattern[str]] = {
    PostEndpoint.BUILD: re.compile(r"^/job/[^/]+(/job/[^/]+)?/build$"),
    PostEndpoint.BUILD_WITH_PARAMS: re.compile(
        r"^/job/[^/]+(/job/[^/]+)?/buildWithParameters$"
    ),
    PostEndpoint.STOP: re.compile(r"^/job/[^/]+(/job/[^/]+)?/[^/]+/stop$"),
    PostEndpoint.REPLAY: re.compile(r"^/job/[^/]+(/job/[^/]+)?/[^/]+/replay/rebuild$"),
}


@dataclass(frozen=True)
class Response:
    """One HTTP round-trip. `data` is parsed JSON when the body is JSON,
    else the raw text (console output answers as text)."""

    status: int
    headers: dict[str, str]
    text: str

    @property
    def data(self) -> dict | str:
        if re.search(r"application/json", self.headers.get("Content-Type", "")):
            return json.loads(self.text)
        return self.text


@dataclass(frozen=True)
class JenkinsClient:
    """Basic-auth client for one stored server. Only get()/post() exist."""

    base_url: str
    username: str
    token: str
    timeout: float = DEFAULT_TIMEOUT
    crumb_cache: dict = field(default_factory=dict)

    def auth_header(self) -> dict[str, str]:
        cred = base64.b64encode(f"{self.username}:{self.token}".encode()).decode()
        return {"Authorization": "Basic " + cred}

    def get(self, path: str, params: dict[str, str] | None = None) -> Response:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers=self.auth_header())
        return self.round_trip(request)

    def get_json(self, path: str, tree: str | None = None) -> Response:
        params = {"tree": tree} if tree else None
        return self.get(path, params)

    def round_trip(self, request: urllib.request.Request) -> Response:
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return Response(
                    status=response.status,
                    headers=dict(response.headers.items()),
                    text=response.read().decode("utf-8", "replace"),
                )
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code in (401, 403):
                raise AuthFailed(
                    f"auth failed (HTTP {e.code}): username/token mismatch or expired",
                    "Run `jenkins-axi auth status` to re-check",
                    "Re-run `jenkins-axi setup --url <url> --username <user>` with a fresh token",
                ) from e
            if e.code == 404:
                raise NotFound(f"HTTP 404 for {request.get_full_url()}") from e
            raise AxiError(
                f"HTTP {e.code} from Jenkins for {request.get_full_url()}: {first_line(body)[:400]}",
            ) from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise Unreachable(
                f"server unreachable: {first_line(str(e))[:400]}",
                "Check network/VPN reachability of the Jenkins host",
                "Run `jenkins-axi --url <url>` to try a different stored server",
            ) from e

    def post(
        self,
        endpoint: PostEndpoint,
        ref: JobRef,
        n: int | None = None,
        params: dict[str, str] | None = None,
    ) -> Response:
        """One of the four whitelisted POSTs. The path is built here and then
        checked against the endpoint's anchored pattern (the whitelist holds
        even against a caller that builds paths itself)."""
        if endpoint in (PostEndpoint.BUILD, PostEndpoint.BUILD_WITH_PARAMS):
            path = ref.url_path("/" + endpoint.value)
        elif endpoint is PostEndpoint.REPLAY:
            # workflow-cps ReplayAction: POST /replay renders the replay FORM
            # (no doIndex -> no trigger, no queue). The real trigger is
            # doRebuild at /replay/rebuild: replays the same script and
            # params. Verified against workflow-cps ReplayAction.
            path = ref.url_path(f"/{n}/replay/rebuild")
        else:
            if n is None:
                raise AxiError(f"{endpoint.value} needs a build number")
            path = ref.url_path(f"/{n}/{endpoint.value}")
        if not PATH_PATTERNS[endpoint].match(path):
            # Unreachable by construction; the check exists so a future edit
            # that breaks the whitelist fails loudly, not silently.
            raise ProgrammingError(
                f"POST path {path!r} is outside the whitelisted endpoints"
            )
        headers = self.auth_header()
        crumb = self.crumb()
        if crumb:
            headers[crumb[0]] = crumb[1]
        body = None
        if params:
            body = urllib.parse.urlencode(params).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = urllib.request.Request(
            self.base_url + path, data=body, headers=headers, method="POST"
        )
        return self.round_trip(request)

    def crumb(self) -> tuple[str, str] | None:
        """The CSRF crumb (field name, crumb value), fetched once per process
        and cached. Honest about what it buys: crumbs are session-bound and
        this client keeps no cookie jar, but Jenkins skips the crumb check
        for API-token-authenticated requests entirely (ApiCrumbExclusion), so
        the negotiation is belt-and-braces: carried when the server answers
        crumbIssuer, never allowed to block the POST when it does not."""
        if "crumb" not in self.crumb_cache:
            try:
                response = self.get_json("/crumbIssuer/api/json")
                data = response.data
                if (
                    isinstance(data, dict)
                    and data.get("crumb")
                    and data.get("crumbRequestField")
                ):
                    self.crumb_cache["crumb"] = (
                        data["crumbRequestField"],
                        data["crumb"],
                    )
            except AxiError, ValueError:
                pass
        return self.crumb_cache.get("crumb")

    def server_version(self) -> str | None:
        return self.get("/api/json").headers.get("X-Jenkins")


def first_line(text: str) -> str:
    return " ".join(text.split()) if text else ""
