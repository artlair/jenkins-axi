"""Queue listing: stated zeros and the why per item."""

from __future__ import annotations


def test_queue_list_zero_is_stated(harness, fake_jenkins):
    fake_jenkins.set_route_json("/queue/api/json", {"items": []})
    done = harness.run("queue", "list")
    assert done.returncode == 0
    assert "total: 0" in done.stdout
    assert "items: []" in done.stdout


def test_queue_list_rows(harness, fake_jenkins):
    import time

    now_ms = int(time.time() * 1000)
    fake_jenkins.set_route_json(
        "/queue/api/json",
        {
            "items": [
                {
                    "id": 123,
                    "why": "Waiting for next available executor",
                    "blocked": False,
                    "inQueueSince": now_ms - 60_000,
                    "task": {"name": "atlas", "fullName": "atlas » feature/wip"},
                }
            ]
        },
    )
    done = harness.run("queue", "list")
    assert done.returncode == 0
    assert "total: 1" in done.stdout
    assert "Waiting for next available executor" in done.stdout
    # Tabular rows are bare values, not key-repeats:
    assert "Waiting for next available executor,atlas" in done.stdout
