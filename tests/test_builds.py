"""Build operations, E2E: view math, console truncation with the size hint,
and the write surface (exactly the four whitelisted POSTs, refused-none
honestly), with the crumb premise tested (attached AND absent)."""

from __future__ import annotations

import re
import time


def route_build(
    fake_jenkins,
    building: bool,
    timestamp_ms: int,
    estimated_ms: int,
    params: dict | None = None,
):
    actions = []
    if params is not None:
        actions.append(
            {"parameters": [{"name": k, "value": v} for k, v in params.items()]}
        )
    build = {
        "number": 7,
        "building": building,
        "result": None if building else "SUCCESS",
        "timestamp": timestamp_ms,
        "duration": 0 if building else 501_000,
        "estimatedDuration": estimated_ms,
        "actions": actions,
    }
    # The job route matters: the 404-disambiguation flow asks the job API
    # itself when /lastBuild 404s, and "no such job" must not shadow "no
    # such build" here.
    fake_jenkins.set_route_json("/job/atlas/job/feature%2Fwip/api/json", {})
    # The job page exists too (with the trailing slash urllib resolves for
    # ../..): the real doRebuild 302-back is followed by urllib as a GET.
    fake_jenkins.set_route(
        "/job/atlas/job/feature%2Fwip/", "{}", content_type="application/json"
    )
    fake_jenkins.set_route_json(
        "/job/atlas/job/feature%2Fwip/lastBuild/api/json", build
    )
    fake_jenkins.set_route_json("/job/atlas/job/feature%2Fwip/7/api/json", build)


def test_build_view_running_reports_elapsed_vs_estimate(harness, fake_jenkins):
    now_ms = int(time.time() * 1000)
    route_build(
        fake_jenkins, building=True, timestamp_ms=now_ms - 30_000, estimated_ms=60_000
    )
    done = harness.run("build", "view", "atlas/feature/wip")
    assert done.returncode == 0
    assert "RUNNING" in done.stdout
    assert "~60s estimated" in done.stdout
    # Wall-clock derived, so elapsed carries the CLI's own startup time on top
    # of the 30s the fake reports, and a loaded CI host can add whole seconds
    # of it. So the assertions bind to what the CLI itself printed, not to a
    # host-speed assumption: elapsed only has to cover the fake's 30s and stay
    # under the 90s formatting cliff, and the percent must equal
    # int(100 * elapsed_ms / 60_000) for the elapsed_ms inside the printed
    # second, [elapsed*1000, (elapsed+1)*1000).
    elapsed = int(re.search(r"\((\d+)s elapsed", done.stdout).group(1))
    assert 30 <= elapsed < 90  # formatted under 90s: stays in seconds
    percent = int(re.search(r", (\d+)%\)", done.stdout).group(1))
    assert 100 * elapsed * 1000 // 60_000 <= percent
    assert percent <= ((elapsed + 1) * 100_000 - 1) // 60_000


def test_build_view_completed_reports_duration(harness, fake_jenkins):
    route_build(fake_jenkins, building=False, timestamp_ms=0, estimated_ms=0)
    done = harness.run("build", "view", "atlas/feature/wip")
    assert done.returncode == 0
    assert "status: SUCCESS" in done.stdout
    assert "duration: 8m21s" in done.stdout


def test_build_console_truncates_with_hint(harness, fake_jenkins):
    text = "\n".join(f"line {i}" for i in range(300))
    fake_jenkins.set_route(
        "/job/atlas/job/feature%2Fwip/lastBuild/consoleText",
        text,
        content_type="text/plain",
    )
    done = harness.run("build", "console", "atlas/feature/wip")
    assert done.returncode == 0
    assert "(truncated, 300 lines total, use --full)" in done.stdout
    assert len(done.stdout.splitlines()) == 201  # hint + 200 lines
    assert "line 299" in done.stdout
    assert "line 99" not in done.stdout


def test_build_console_full(harness, fake_jenkins):
    text = "\n".join(f"line {i}" for i in range(300))
    fake_jenkins.set_route(
        "/job/atlas/job/feature%2Fwip/lastBuild/consoleText",
        text,
        content_type="text/plain",
    )
    done = harness.run("build", "console", "atlas/feature/wip", "--full")
    assert done.returncode == 0
    assert "line 0" in done.stdout
    assert "(truncated" not in done.stdout


def test_build_start_parameterized_requires_params(harness, fake_jenkins):
    fake_jenkins.set_route_json(
        "/job/atlas/api/json",
        {
            "_class": "org.jenkinsci.plugins.workflow.job.WorkflowJob",
            "property": [{"parameterDefinitions": [{"name": "deployment-id"}]}],
        },
    )
    done = harness.run("build", "start", "atlas")
    assert done.returncode == 1
    assert "parameterized, params required: deployment-id" in done.stdout
    assert fake_jenkins.posts() == []


def test_build_start_posts_with_params(harness, fake_jenkins):
    done = harness.run(
        "build",
        "start",
        "atlas",
        "--param",
        "deployment-id=testec001",
        "--param",
        "k=v",
    )
    assert done.returncode == 0
    posts = fake_jenkins.posts()
    assert len(posts) == 1
    path, body = posts[0]
    assert path == "/job/atlas/buildWithParameters"
    assert b"deployment-id=testec001" in body
    assert "queue: 123" in done.stdout


def test_build_start_plain_posts_build(harness, fake_jenkins):
    fake_jenkins.set_route_json(
        "/job/plain/api/json",
        {"_class": "org.jenkinsci.plugins.workflow.job.WorkflowJob", "property": []},
    )
    done = harness.run("build", "start", "plain")
    assert done.returncode == 0
    path, _body = fake_jenkins.posts()[0]
    assert path == "/job/plain/build"
    assert "queued" in done.stdout


def test_post_still_succeeds_without_crumb_issuer(harness, fake_jenkins):
    # crumbIssuer 404 (disabled/old): the POST must still go out, crumb-less.
    fake_jenkins.rec.crumb = None
    fake_jenkins.set_route_json(
        "/job/plain/api/json",
        {"_class": "org.jenkinsci.plugins.workflow.job.WorkflowJob", "property": []},
    )
    done = harness.run("build", "start", "plain")
    assert done.returncode == 0
    assert fake_jenkins.posts()[0][0] == "/job/plain/build"
    assert any(crumb is None for (_m, _p, _a, crumb, _b) in fake_jenkins.requests)


def test_multibranch_target_is_not_a_build_target(harness, fake_jenkins):
    fake_jenkins.set_route_json(
        "/job/atlas/api/json",
        {
            "_class": "org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject",
            "lastBuild": None,
        },
    )
    done = harness.run("build", "start", "atlas")
    assert done.returncode == 1
    assert "multibranch project" in done.stdout
    assert "re-indexes branches" in done.stdout
    assert fake_jenkins.posts() == []


def test_multibranch_target_view_says_address_project_branch(harness, fake_jenkins):
    fake_jenkins.set_route_json(
        "/job/atlas/api/json",
        {
            "_class": "org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject"
        },
    )
    done = harness.run("build", "view", "atlas", "last")
    assert done.returncode == 1
    assert "no project-level builds" in done.stdout
    assert "job list atlas" in done.stdout


def test_build_stop_running_posts_stop(harness, fake_jenkins):
    route_build(
        fake_jenkins,
        building=True,
        timestamp_ms=int(time.time() * 1000),
        estimated_ms=60_000,
    )
    done = harness.run("build", "stop", "atlas/feature/wip")
    assert done.returncode == 0
    assert "stopped: true" in done.stdout
    posts = fake_jenkins.posts()
    assert len(posts) == 1
    assert posts[0][0] == "/job/atlas/job/feature%2Fwip/7/stop"


def test_build_stop_completed_is_honest_noop(harness, fake_jenkins):
    route_build(fake_jenkins, building=False, timestamp_ms=0, estimated_ms=0)
    done = harness.run("build", "stop", "atlas/feature/wip")
    assert done.returncode == 0
    assert "stopped: false" in done.stdout
    assert "not running" in done.stdout
    assert fake_jenkins.posts() == []


def test_build_stop_no_such_build(harness, fake_jenkins):
    fake_jenkins.set_route_json("/job/atlas/job/feature%2Fwip/api/json", {})
    done = harness.run("build", "stop", "atlas/feature/wip", "99")
    assert done.returncode == 1
    assert "no such build #99" in done.stdout
    assert fake_jenkins.posts() == []


def test_build_stop_no_builds_yet(harness, fake_jenkins):
    fake_jenkins.set_route_json(
        "/job/atlas/job/feature%2Fwip/api/json", {"lastBuild": None}
    )
    done = harness.run("build", "stop", "atlas/feature/wip")
    assert done.returncode == 1
    assert "has no builds yet" in done.stdout
    assert fake_jenkins.posts() == []


def test_build_restart_replays_with_crumb(harness, fake_jenkins):
    route_build(
        fake_jenkins, building=False, timestamp_ms=0, estimated_ms=1, params={"a": "b"}
    )
    done = harness.run("build", "restart", "atlas/feature/wip", "7")
    assert done.returncode == 0
    posts = fake_jenkins.posts()
    assert posts[0][0] == "/job/atlas/job/feature%2Fwip/7/replay/rebuild"
    assert len(posts) == 1  # exactly ONE restart POST
    # doRebuild answers a 302 back to the build page: no queue id to report,
    # so the output says what it did, not an invented queue line (this claim
    # survived round 1 only because the fake answered 201 + queue Location;
    # the fake now answers the real 302, so this asserts the honest shape).
    assert "path: rebuilt (POSTed /replay/rebuild)" in done.stdout
    assert "queue:" not in done.stdout
    # The crumb premise, tested: the POST carries the negotiated crumb.
    assert any(crumb == "c0ffee" for (_m, _p, _a, crumb, _b) in fake_jenkins.requests)


def test_build_restart_ambiguous_failure_surfaces_it(harness, fake_jenkins):
    """A 500 on rebuild is ambiguous (the build may have queued): exactly one
    POST, exit 1, no fallback."""
    route_build(
        fake_jenkins, building=False, timestamp_ms=0, estimated_ms=1, params={"a": "b"}
    )
    fake_jenkins.rec.replay_status = 500
    done = harness.run("build", "restart", "atlas/feature/wip", "7")
    assert done.returncode == 1
    assert "HTTP 500" in done.stdout
    assert len(fake_jenkins.posts()) == 1  # no blind fallback


def test_build_restart_falls_back_to_original_params(harness, fake_jenkins):

    route_build(
        fake_jenkins,
        building=False,
        timestamp_ms=0,
        estimated_ms=1,
        params={"deployment-id": "e1"},
    )
    fake_jenkins.rec.replay_status = 404
    done = harness.run("build", "restart", "atlas/feature/wip", "7")
    assert done.returncode == 0
    posts = fake_jenkins.posts()
    assert posts[0][0].endswith("/7/replay/rebuild")
    path, body = posts[1]
    assert path == "/job/atlas/job/feature%2Fwip/buildWithParameters"
    assert b"deployment-id=e1" in body
    assert "path: re-triggered with original params" in done.stdout


def test_build_restart_falls_back_to_plain(harness, fake_jenkins):

    route_build(
        fake_jenkins, building=False, timestamp_ms=0, estimated_ms=1, params=None
    )
    fake_jenkins.rec.replay_status = 404
    done = harness.run("build", "restart", "atlas/feature/wip")
    assert done.returncode == 0
    assert fake_jenkins.posts()[1][0] == "/job/atlas/job/feature%2Fwip/build"
    assert "path: re-triggered" in done.stdout


def test_build_start_invalid_param_shape(harness, fake_jenkins):
    done = harness.run("build", "start", "atlas", "--param", "noequals")
    assert done.returncode == 1
    assert "invalid --param" in done.stdout
    assert fake_jenkins.posts() == []


def test_build_console_tail_zero_is_rejected(harness, fake_jenkins):
    text = "\n".join(f"line {i}" for i in range(50))
    fake_jenkins.set_route(
        "/job/atlas/job/feature%2Fwip/lastBuild/consoleText",
        text,
        content_type="text/plain",
    )
    done = harness.run("build", "console", "atlas/feature/wip", "--tail", "0")
    assert done.returncode == 1
    assert "invalid --tail 0" in done.stdout
    assert "--full" in done.stdout
