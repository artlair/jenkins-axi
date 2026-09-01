"""Job listing and viewing, E2E: stated totals, stated zeros, honest
404-vs-no-builds semantics."""

from __future__ import annotations

import json

BRANCHES = {
    "jobs": [
        {
            "_class": "org.jenkinsci.plugins.workflow.job.WorkflowJob",
            "name": "bugfix%2FDEV-1217",
            "lastBuild": {
                "number": 58,
                "building": False,
                "estimatedDuration": 192544,
                "result": "SUCCESS",
                "timestamp": 1785737579711,
            },
        },
        {
            "_class": "org.jenkinsci.plugins.workflow.job.WorkflowJob",
            "name": "feature%2Fwip",
            "lastBuild": None,
        },
    ]
}


def test_job_list_states_total_and_zero(harness, fake_jenkins):
    fake_jenkins.set_route_json(
        "/api/json", {"jobs": [{"name": "Alfred"}, {"name": "atlas"}]}
    )
    done = harness.run("job", "list")
    assert done.returncode == 0
    assert "total: 2" in done.stdout
    assert "jobs[2]{name,kind,color}:" in done.stdout
    assert "Alfred," in done.stdout


def test_job_list_zero_is_stated(harness, fake_jenkins):
    fake_jenkins.set_route_json("/api/json", {"jobs": []})
    done = harness.run("job", "list")
    assert done.returncode == 0
    assert "total: 0" in done.stdout
    assert "jobs: []" in done.stdout


def test_job_list_branches(harness, fake_jenkins):
    fake_jenkins.set_route_json("/job/Batman/api/json", BRANCHES)
    done = harness.run("job", "list", "Batman")
    assert done.returncode == 0
    assert "total: 2" in done.stdout
    # Branch names pass through percent-encoded, display decoded.
    assert "bugfix%2FDEV-1217" in done.stdout
    assert "SUCCESS" in done.stdout
    # Branch with no builds states the zero in its own row.
    assert "no builds" in done.stdout


def test_job_list_branches_running_filter(harness, fake_jenkins):
    running = json.loads(json.dumps(BRANCHES))
    running["jobs"][0]["lastBuild"]["building"] = True
    fake_jenkins.set_route_json("/job/Batman/api/json", running)
    done = harness.run("job", "list", "Batman", "--running")
    assert done.returncode == 0
    assert "RUNNING" in done.stdout
    assert "bugfix%2FDEV-1217" in done.stdout
    assert "feature%2Fwip" not in done.stdout


def test_job_list_plain_job_is_not_nested(harness, fake_jenkins):
    fake_jenkins.set_route_json(
        "/job/plain/api/json",
        {"_class": "org.jenkinsci.plugins.workflow.job.WorkflowJob"},
    )
    done = harness.run("job", "list", "plain")
    assert done.returncode == 1
    assert "not a nested job" in done.stdout


def test_job_view_404_is_honest(harness, fake_jenkins):
    done = harness.run("job", "view", "absent")
    assert done.returncode == 1
    assert "no such job or folder: absent" in done.stdout
    assert "job list" in done.stdout


def test_job_view_detail(harness, fake_jenkins):
    fake_jenkins.set_route_json(
        "/job/atlas/api/json",
        {
            "displayName": "atlas",
            "_class": "org.jenkinsci.plugins.workflow.job.WorkflowJob",
            "description": "the atlas pipeline",
            "healthReport": [{"score": 100, "description": "Stable"}],
            "lastSuccessfulBuild": {
                "number": 5,
                "result": "SUCCESS",
                "timestamp": 1785737579711,
            },
            "lastFailedBuild": None,
            "property": [
                {
                    "parameterDefinitions": [
                        {"name": "deployment-id", "type": "StringParameterDefinition"}
                    ]
                }
            ],
        },
    )
    done = harness.run("job", "view", "atlas")
    assert done.returncode == 0
    assert "kind: pipeline" in done.stdout
    assert "health: Stable" in done.stdout
    assert 'last_success: "#5 SUCCESS' in done.stdout
    assert "last_failure: never" in done.stdout
    assert "deployment-id" in done.stdout
