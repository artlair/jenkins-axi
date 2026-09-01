"""Data orchestration: the operations jenkins-axi offers, as pure functions
taking a JenkinsClient. cli.py is thin wiring over these; tests target the
operations through their seams, not the argparse entrypoint.

Build addressing (`spec`): `last` resolves to Jenkins' own /lastBuild path,
an integer to /<n>/. Every operation answers the build that lastBuild names,
for a pipeline that is the most recent build regardless of completion, so
lastBuild.building is the honest "is something running" signal.

Failure semantics: a 404 on /lastBuild can mean "no such job" or "job exists,
no builds yet"; build_info does the second GET on failure and says which,
because "job exists, no builds yet" and "no such job" read as each other.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from jenkins_axi.client import JenkinsClient, JobRef, PostEndpoint, Response
from jenkins_axi.errors import AxiError, NotFound

SPEC_LAST = "last"


@dataclass(frozen=True)
class JobSummary:
    """A top-level list row: name, kind, health color. Deliberately minimal
    (AXI §2); `--fields` style widening lives in widening the tree query."""

    name: str
    kind: str
    color: str | None

    @classmethod
    def from_json(cls, data: dict) -> JobSummary:
        return cls(
            name=data["name"],
            kind=kind_of(data.get("_class", "")),
            color=data.get("color"),
        )


@dataclass(frozen=True)
class BranchSummary:
    """A branch of a multibranch job (or the one build of a plain job):
    the monitoring core. status derives RUNNING from lastBuild.building."""

    name: str
    number: int | None
    building: bool
    result: str | None
    timestamp_ms: int | None
    duration_ms: int | None
    estimated_ms: int | None

    @classmethod
    def from_json(cls, name: str, last: dict | None) -> BranchSummary:
        return cls(
            name=name,
            number=last.get("number") if last else None,
            building=bool(last.get("building")) if last else False,
            result=last.get("result") if last else None,
            timestamp_ms=last.get("timestamp") if last else None,
            duration_ms=last.get("duration") if last else None,
            estimated_ms=last.get("estimatedDuration") if last else None,
        )

    def status(self, now_ms: int) -> str:
        if self.building:
            elapsed = max(0, now_ms - (self.timestamp_ms or 0))
            return "RUNNING " + progress(elapsed, self.estimated_ms)
        return self.result or "no builds"


@dataclass(frozen=True)
class JobDetail:
    name: str
    kind: str
    description: str | None
    health: str | None
    last_success: tuple[int, str, int] | None  # (number, result, timestamp_ms)
    last_failure: tuple[int, str, int] | None
    params: dict[str, str]  # name -> type

    @classmethod
    def from_json(cls, data: dict) -> JobDetail:
        builds = {
            key: build_tuple(data.get(key))
            for key in ("lastSuccessfulBuild", "lastFailedBuild", "lastCompletedBuild")
        }
        params = {
            definition["name"]: definition.get("type", "?")
            for prop in data.get("property", [])
            for definition in prop.get("parameterDefinitions", [])
        }
        health = next(iter(data.get("healthReport") or []), None)
        return cls(
            name=data.get("displayName") or data.get("name", "?"),
            kind=kind_of(data.get("_class", "")),
            description=data.get("description"),
            health=health.get("description") if health else None,
            last_success=builds["lastSuccessfulBuild"],
            last_failure=builds["lastFailedBuild"],
            params=params,
        )


@dataclass(frozen=True)
class BuildInfo:
    number: int
    building: bool
    result: str | None
    timestamp_ms: int | None
    duration_ms: int | None
    estimated_ms: int | None
    params: dict[str, str]  # original parameters (name -> value), for restart
    param_count: int  # total parameter defs; null-masked (passwords) included

    @classmethod
    def from_json(cls, data: dict) -> BuildInfo:
        all_params = [
            p
            for action in data.get("actions") or []
            for p in (action.get("parameters") or [])
        ]
        params = {
            p["name"]: str(p["value"]) for p in all_params if p.get("value") is not None
        }
        param_count = len(all_params)  # null-masked (passwords) included
        return cls(
            number=data["number"],
            building=bool(data.get("building")),
            result=data.get("result"),
            timestamp_ms=data.get("timestamp"),
            duration_ms=data.get("duration"),
            estimated_ms=data.get("estimatedDuration"),
            params=params,
            param_count=param_count,
        )

    def elapsed_ms(self, now_ms: int) -> int:
        """While building, duration_ms stays 0: elapsed is now - timestamp.
        Completed builds report elapsed as the final duration."""
        if self.building:
            return max(0, now_ms - self.timestamp_ms)
        return self.duration_ms or 0


@dataclass(frozen=True)
class QueueItem:
    id: int
    why: str | None
    job: str | None
    in_queue_ms: int | None
    blocked: bool
    executable: int | None  # build number when it already started

    @classmethod
    def from_json(cls, data: dict) -> QueueItem:
        task = data.get("task") or {}
        executable = data.get("executable") or {}
        return cls(
            id=data["id"],
            why=data.get("why"),
            job=task.get("fullName") or task.get("name"),
            in_queue_ms=data.get("inQueueSince"),
            blocked=bool(data.get("blocked")),
            executable=executable.get("number"),
        )


@dataclass(frozen=True)
class StartResult:
    ref: JobRef
    queue_id: int | None
    item: QueueItem | None
    message: str


@dataclass(frozen=True)
class RestartResult:
    ref: JobRef
    original_number: int
    queue_id: int | None
    item: QueueItem | None
    path: str  # "rebuilt (POSTed /replay/rebuild)" | "re-triggered..." modes


@dataclass(frozen=True)
class StopResult:
    ref: JobRef
    number: int
    stopped: bool
    result: str | None
    detail: str


NESTED_KINDS = {"multibranch", "folder"}


def kind_of(underclass: str) -> str:
    """Map a Jenkins _class to the short kind. Unknown classes answer with
    the bare underclass (honest, and still grep-able)."""
    mapping = {
        "WorkflowMultiBranchProject": "multibranch",
        "WorkflowJob": "pipeline",
        "FreeStyleProject": "freestyle",
        "Folder": "folder",
        "Hudson": "server",
    }
    bare = underclass.rsplit(".", 1)[-1]
    return mapping.get(bare, bare)


def root(client: JenkinsClient) -> tuple[dict, str | None]:
    # tree=jobs[name]: names only, so the ambient-status round-trip stays tiny
    # (the no-tree root response carries every job's full config, enormous).
    response = client.get_json("/api/json", tree="jobs[name]")
    data = as_dict(response)
    return data, response.headers.get("X-Jenkins")


def queue_total(client: JenkinsClient) -> int:
    # No range on the tree query: the count IS the total (AXI §4).
    response = client.get_json("/queue/api/json", tree="items[id]")
    return len(as_dict(response).get("items", []))


def list_jobs(client: JenkinsClient, limit: int) -> list[JobSummary]:
    response = client.get_json(
        "/api/json", tree=f"jobs[name,_class,color]{{0,{limit}}}"
    )
    data = as_dict(response)
    return [JobSummary.from_json(j) for j in data.get("jobs", [])]


def list_jobs_count(client: JenkinsClient) -> int:
    """The server-side count, unranged names-only: the ranged page's len is
    the page size, and printing it as `total` lies to the agent (fresh-eyes
    #6: a 120-job server would answer total: 50, read as "there are 50")."""
    return len(as_dict(client.get_json("/api/json", tree="jobs[name]")).get("jobs", []))


def list_branches_count(client: JenkinsClient, ref: JobRef) -> int:
    """Server-side branch count for a multibranch project (see list_jobs_count)."""
    return len(
        as_dict(client.get_json(ref.url_path("/api/json"), tree="jobs[name]")).get(
            "jobs", []
        )
    )


def list_branches(
    client: JenkinsClient, ref: JobRef, limit: int, now_ms: int
) -> list[BranchSummary]:
    tree = f"{BRANCH_TREE}{{0,{limit}}}"
    try:
        response = client.get_json(ref.url_path("/api/json"), tree=tree)
    except NotFound:
        raise missing_job(ref) from None
    data = as_dict(response)
    if data.get("jobs") is None:
        # Plain (non-nested) job asked for as a branch list: nothing to drill.
        raise AxiError(
            f"{ref.display} is not a nested job (no branches to list)",
            f"Run `jenkins-axi job view {ref.display}`",
            f"Run `jenkins-axi build view {ref.display} last`",
        )
    return [
        BranchSummary.from_json(branch["name"], branch.get("lastBuild"))
        for branch in data["jobs"]
    ]


def job_detail(client: JenkinsClient, ref: JobRef) -> JobDetail:
    try:
        response = client.get_json(ref.url_path("/api/json"), tree=JOB_TREE)
    except NotFound:
        raise missing_job(ref) from None
    return JobDetail.from_json(as_dict(response))


def build_info(
    client: JenkinsClient, ref: JobRef, spec: str, now_ms: int | None = None
) -> BuildInfo:
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    if spec == SPEC_LAST:
        suffix = "/lastBuild/api/json"
    elif re.fullmatch(r"\d+", spec):
        suffix = f"/{spec}/api/json"
    else:
        raise AxiError(
            f"invalid build spec {spec!r}",
            "Use `last` or a build number, e.g. `jenkins-axi build view <job> 7`",
        )
    try:
        response = client.get_json(ref.url_path(suffix), tree=BUILD_TREE)
        return BuildInfo.from_json(as_dict(response))
    except NotFound:
        # The 404 line can mean four things; ask the job itself and answer
        # honestly. One extra GET on failure only.
        raise missing_build(client, ref, spec) from None


def console_text(client: JenkinsClient, ref: JobRef, spec: str) -> str:
    if spec == SPEC_LAST:
        suffix = "/lastBuild/consoleText"
    elif re.fullmatch(r"\d+", spec):
        suffix = f"/{spec}/consoleText"
    else:
        raise AxiError(
            f"invalid build spec {spec!r}",
            "Use `last` or a build number, e.g. `jenkins-axi build console <job> 7`",
        )
    try:
        return client.get(ref.url_path(suffix)).text
    except NotFound:
        raise missing_build(client, ref, spec) from None


def queue_items(client: JenkinsClient, limit: int) -> list[QueueItem]:
    response = client.get_json(
        "/queue/api/json",
        tree=f"items[id,why,inQueueSince,blocked,task[name,fullName]]{{0,{limit}}}",
    )
    data = as_dict(response)
    return [QueueItem.from_json(i) for i in data.get("items", [])]


def start_build(
    client: JenkinsClient, ref: JobRef, params: dict[str, str] | None = None
) -> StartResult:
    """Trigger a build. A parameterized job without --param fails with the
    param list inline; --param on a non-parameterized job fails honestly."""
    if params:
        response = client.post(PostEndpoint.BUILD_WITH_PARAMS, ref, params=params)
    else:
        # The probe needs /api/json: the bare job path answers text/html on a
        # real server, which as_dict silently flattens to {} and every job
        # would read non-parameterized (fresh-eyes #4).
        definitions = client.get_json(
            ref.url_path("/api/json"), tree="property[parameterDefinitions[name]]"
        )
        data = as_dict(definitions)
        if kind_of(data.get("_class", "")) in NESTED_KINDS:
            raise AxiError(
                f"{ref.display} is a {kind_of(data.get("_class", ""))} project: "
                "POSTing /build there re-indexes branches, it does not start a build",
                f"Run `jenkins-axi job list {ref.display}` to list its branches",
                f"Address a build target as {ref.display}/<branch>",
            )
        if data.get("property"):
            required = [
                definition["name"]
                for prop in data["property"]
                for definition in prop.get("parameterDefinitions", [])
            ]
            raise AxiError(
                f"{ref.display} is parameterized, params required: "
                + ", ".join(required),
                f"Run `jenkins-axi job view {ref.display}` to see each param's type",
                f"Re-run with --param k=v (repeatable), e.g. "
                f"`jenkins-axi build start {ref.display} --param k=v`",
            )
        response = client.post(PostEndpoint.BUILD, ref)
    queue_id = queue_id_from(response)
    item = queue_item(client, queue_id) if queue_id else None
    if item and item.executable is not None:
        message = f"started as build #{item.executable}"
    else:
        message = "queued" + (f" ({item.why})" if item and item.why else "")
    return StartResult(ref=ref, queue_id=queue_id, item=item, message=message)


def restart_build(client: JenkinsClient, ref: JobRef, spec: str) -> RestartResult:
    """Re-run an existing build with ONE POST: the replay endpoint for
    workflow builds, otherwise re-trigger with the original parameters (or
    plain). Deliberately no param overrides on restart: keep the re-run
    predictable."""
    info = build_info(client, ref, spec)
    try:
        response = client.post(PostEndpoint.REPLAY, ref, n=info.number)
        # workflow-cps doRebuild: replays the same script and params, answers
        # a 302 back to the build page. NOT the form-rendering /replay index,
        # whose 200 means "rendered the form" and queued nothing.
        path = "rebuilt (POSTed /replay/rebuild)"
    except NotFound:
        # ONLY a definite miss falls back (404 = no doRebuild here, so
        # definitely not triggered; a freestyle build has no ReplayAction).
        # Unreachable/AuthFailed/5xx/409 must NOT: an ambiguous failure may
        # already have queued a build, and re-triggering that would
        # double-fire (round-2 repro: 500 -> two POSTs, success reported).
        if info.params:
            response = client.post(
                PostEndpoint.BUILD_WITH_PARAMS, ref, params=info.params
            )
            path = "re-triggered with original params"
        else:
            response = client.post(PostEndpoint.BUILD, ref)
            path = "re-triggered"
    queue_id = queue_id_from(response)
    item = queue_item(client, queue_id) if queue_id else None
    # Password/credential parameters are null-masked by the API and dropped
    # from `params`: say so rather than implying a faithful re-run.
    detail = path
    if path != "rebuilt (POSTed /replay/rebuild)" and info.param_count > len(
        info.params
    ):
        masked = info.param_count - len(info.params)
        detail += (
            f" ({masked} of {info.param_count} params null-masked, defaults applied)"
        )
    return RestartResult(
        ref=ref, original_number=info.number, queue_id=queue_id, item=item, path=detail
    )


def stop_build(client: JenkinsClient, ref: JobRef, spec: str) -> StopResult:
    """Stop an in-progress build. GET first: stopping a completed build is a
    no-op, reported honestly rather than as a silent 302 redirect."""
    info = build_info(client, ref, spec)
    if not info.building:
        return StopResult(
            ref=ref,
            number=info.number,
            stopped=False,
            result=info.result,
            detail=f"build #{info.number} is not running, nothing to stop",
        )
    client.post(PostEndpoint.STOP, ref, n=info.number)
    return StopResult(
        ref=ref,
        number=info.number,
        stopped=True,
        result=None,
        detail=f"stop POSTed for build #{info.number}",
    )


def queue_item(client: JenkinsClient, queue_id: int) -> QueueItem | None:
    """Fetch one queue item; 404s honestly mean it already executed."""
    try:
        return QueueItem.from_json(
            as_dict(client.get_json(f"/queue/item/{queue_id}/api/json"))
        )
    except NotFound:
        return None


def missing_job(ref: JobRef) -> AxiError:
    """An honest 404 for a job address (the raw 404 line reads as each
    other)."""
    return AxiError(
        f"no such job or folder: {ref.display}",
        "Run `jenkins-axi job list` to see jobs",
        "Multibranch branches are addressed as project/branch",
    )


def missing_build(client: JenkinsClient, ref: JobRef, spec: str) -> AxiError:
    """An honest 404 for a build request: ask the job API itself so "job
    exists, no builds yet" never reads as "no such job" (or the reverse) -
    and a multibranch/folder target gets the drill-down fix, because "has
    no builds yet" would hint `build start <project>`, which re-indexes
    branches."""
    try:
        probe = client.get_json(
            ref.url_path("/api/json"), tree="lastBuild[number],_class"
        )
    except NotFound:
        return AxiError(
            f"no such job or folder: {ref.display}",
            "Run `jenkins-axi job list` to see jobs",
            "Multibranch branches are addressed as project/branch",
        )
    if kind_of(as_dict(probe).get("_class", "")) in NESTED_KINDS:
        return AxiError(
            f"{ref.display} is a {kind_of(as_dict(probe).get("_class", ""))} project: "
            "no project-level builds; address project/branch",
            f"Run `jenkins-axi job list {ref.display}` to list its branches",
        )
    if spec == SPEC_LAST:
        return AxiError(
            f"{ref.display} has no builds yet",
            f"Run `jenkins-axi build start {ref.display}` to trigger one",
        )
    return AxiError(
        f"no such build #{spec} on {ref.display}",
        f"Run `jenkins-axi build view {ref.display} last` for the latest build",
    )


def queue_id_from(response: Response) -> int | None:
    """Jenkins answers a build POST with 201 + Location .../queue/item/123/."""
    match = re.search(r"/queue/item/(\d+)", response.headers.get("Location", ""))
    return int(match.group(1)) if match else None


def as_dict(response: Response) -> dict:
    data = response.data
    return data if isinstance(data, dict) else {}


def build_tuple(build: dict | None) -> tuple[int, str, int] | None:
    if not build:
        return None
    return (build.get("number"), build.get("result"), build.get("timestamp"))


def progress(elapsed_ms: int, estimated_ms: int | None) -> str:
    """'62% (8m20s elapsed, ~14m30s estimated, 57%)'. An unknown estimate
    stays honest ('estimate unknown') instead of inventing a number (AXI §4).
    """
    elapsed = format_ms(elapsed_ms)
    if not estimated_ms:
        return f"({elapsed} elapsed, estimate unknown)"
    pct = min(999, int(100 * elapsed_ms / estimated_ms))
    return f"({elapsed} elapsed, ~{format_ms(estimated_ms)} estimated, {pct}%)"


def format_ms(ms: int) -> str:
    """Compact duration: '50s', '2m14s', '3h02m'."""
    secs = max(0, int(ms / 1000))
    if secs < 90:
        return f"{secs}s"
    mins, rem = divmod(secs, 60)
    if mins < 90:
        return f"{mins}m{rem:02d}s"
    hours, mins = divmod(mins, 60)
    return f"{hours}h{mins:02d}m"


BRANCH_TREE = "jobs[name,lastBuild[number,building,estimatedDuration,timestamp,result]]"
BUILD_TREE = "number,building,result,timestamp,duration,estimatedDuration,actions[parameters[name,value]]"
JOB_TREE = (
    "displayName,_class,description,healthReport[score,description],"
    "lastSuccessfulBuild[number,result,timestamp],lastFailedBuild[number,result,timestamp],"
    "lastBuild[number,building,estimatedDuration,timestamp,result],"
    "property[parameterDefinitions[name,type]]"
)
