"""Ambient status: bare invocation prints live state, not help (AXI §8);
errors must survive with stderr discarded (AXI §6); usage errors exit 2."""

from __future__ import annotations

import subprocess

from conftest import search_block


def test_bare_prints_live_state(harness, fake_jenkins):
    fake_jenkins.set_route_json(
        "/api/json", {"jobs": [{"name": "Alfred"}, {"name": "atlas"}]}
    )
    fake_jenkins.set_route_json("/queue/api/json", {"items": []})
    done = harness.run()
    assert done.returncode == 0
    assert "bin: " in done.stdout
    assert "description: Monitor Jenkins" in done.stdout
    assert (
        f'url: "{fake_jenkins.url}"' in done.stdout
    )  # TOON quotes: value contains ":"
    assert "auth: ok" in done.stdout
    assert "jobs_total: 2" in done.stdout
    assert "queue_total: 0" in done.stdout
    assert "help[" in done.stdout
    assert "jenkins-axi job list" in done.stdout


def test_absent_server_version_is_omitted_not_invented(harness, fake_jenkins):
    fake_jenkins.set_route_json("/api/json", {"jobs": []})
    fake_jenkins.set_route_json("/queue/api/json", {"items": []})
    done = harness.run()
    assert done.returncode == 0
    assert "server:" not in done.stdout


def test_unreachable_fails_structured(harness, fake_jenkins):
    closed = search_block("http://127.0.0.1:1")  # port 1: closed on localhost
    done = harness.run(
        "job", "list", extra_env={"JENKINS_AXI_TEST_SEARCH_BLOCK": closed}
    )
    assert done.returncode == 1
    assert "server unreachable" in done.stdout


def test_error_survives_stderr_discard(harness, fake_jenkins):
    """House check from the AXI reference: `tool <cmd> 2>/dev/null` still
    carries the structured error on stdout."""
    closed = search_block("http://127.0.0.1:1")
    done = harness.run(
        "job",
        "list",
        extra_env={"JENKINS_AXI_TEST_SEARCH_BLOCK": closed},
        stderr=subprocess.DEVNULL,
    )
    assert done.returncode == 1
    assert "server unreachable" in done.stdout


def test_auth_failed_is_structured(harness, fake_jenkins):
    fake_jenkins.set_basic_auth("alec", "RIGHT-token")
    fake_jenkins.set_route_json("/api/json", {"jobs": []})
    done = harness.run("job", "list")
    assert done.returncode == 1
    assert "auth failed" in done.stdout
    assert "auth status" in done.stdout


def test_unknown_flag_exits_2(harness):
    done = harness.run("job", "list", "--bogus")
    assert done.returncode == 2


def test_noun_alone_exits_2_axi_shaped(harness):
    done = harness.run("job")
    assert done.returncode == 2
    assert "error: no subcommand given" in done.stdout
