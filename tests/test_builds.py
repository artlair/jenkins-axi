"""Build operations, E2E: view math, console truncation with the size hint,
and the write surface — exactly the four whitelisted POSTs, refused-none
honestly, with the crumb attached."""

from __future__ import annotations


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
    fake_jenkins.set_route_json(
        "/job/atlas/job/feature%2Fwip/lastBuild/api/json", build
    )
    fake_jenkins.set_route_json("/job/atlas/job/feature%2Fwip/7/api/json", build)


def test_build_view_running_reports_elapsed_vs_estimate(harness, fake_jenkins):
    import time

    now_ms = int(time.time() * 1000)
    route_build(
        fake_jenkins, building=True, timestamp_ms=now_ms - 5_000, estimated_ms=10_000
    )
    done = harness.run("build", "view", "atlas/feature/wip")
    assert done.returncode == 0
    assert "RUNNING" in done.stdout
    assert "5s elapsed" in done.stdout
    assert "~10s estimated" in done.stdout
    assert "50%" in done.stdout


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
        "/job/atlas",
        {"property": [{"parameterDefinitions": [{"name": "deployment-id"}]}]},
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
    fake_jenkins.set_route_json("/job/plain", {"property": []})
    done = harness.run("build", "start", "plain")
    assert done.returncode == 0
    path, _body = fake_jenkins.posts()[0]
    assert path == "/job/plain/build"
    assert "queued" in done.stdout


def test_build_stop_running_posts_stop(harness, fake_jenkins):
    import time

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


def test_build_restart_replays(harness, fake_jenkins):

    route_build(
        fake_jenkins, building=False, timestamp_ms=0, estimated_ms=1, params={"a": "b"}
    )
    done = harness.run("build", "restart", "atlas/feature/wip", "7")
    assert done.returncode == 0
    posts = fake_jenkins.posts()
    assert posts[0][0] == "/job/atlas/job/feature%2Fwip/7/replay"
    assert len(posts) == 1  # exactly ONE restart POST
    assert "path: replayed" in done.stdout


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
    assert posts[0][0].endswith("/7/replay")
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
