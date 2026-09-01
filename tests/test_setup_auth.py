"""Setup and auth resolution, E2E: the secret never on argv, stdin-only
storage, fail-closed paths (locked vault, missing entry, multi-entry)."""

from __future__ import annotations

from conftest import search_block


def test_setup_stores_via_secret_tool_stdin(harness, fake_jenkins):
    done = harness.run(
        "setup", "--url", fake_jenkins.url, "--username", "alec", input="s3cr3t-token\n"
    )
    assert done.returncode == 0
    assert "stored: true" in done.stdout
    assert fake_jenkins.url in done.stdout
    lines = harness.stored_args()
    args_line = next(l for l in lines if l[0] == "ARGS")
    stdin_line = next(l for l in lines if l[0] == "STDIN")
    # The stored attributes reach secret-tool; the secret does NOT touch argv.
    assert "service\tjenkins-axi" in "\t".join(args_line)
    assert "url\t" + fake_jenkins.url in "\t".join(args_line)
    assert "username\talec" in "\t".join(args_line)
    assert "s3cr3t-token" not in " ".join(args_line)
    assert stdin_line[1] == "s3cr3t-token"


def test_setup_empty_token_fails(harness):
    done = harness.run(
        "setup", "--url", "http://127.0.0.1:1", "--username", "alec", input="\n"
    )
    assert done.returncode == 1
    assert "empty API token" in done.stdout


def test_setup_bad_url_fails(harness):
    done = harness.run(
        "setup", "--url", "ftp://nope", "--username", "alec", input="t\n"
    )
    assert done.returncode == 1
    assert "not a valid Jenkins URL" in done.stdout


def test_basic_auth_uses_stored_credential(harness, fake_jenkins):
    fake_jenkins.set_route_json("/api/json", {"jobs": []})
    fake_jenkins.set_basic_auth("alec", "testtoken123")
    done = harness.run("job", "list")
    assert done.returncode == 0
    method, _path, auth, _body = fake_jenkins.requests[0]
    assert method == "GET"
    assert auth == fake_jenkins.rec.basic_auth


def test_missing_entry_fails_with_remedy(harness):
    done = harness.run("job", "list", extra_env={"JENKINS_AXI_TEST_SEARCH_BLOCK": ""})
    assert done.returncode == 1
    assert "no Secret Service entry found" in done.stdout
    assert "jenkins-axi setup" in done.stdout


def test_multi_entry_requires_url(harness, fake_jenkins):
    other = search_block("http://other.example.net:8080", secret="other")
    done = harness.run(
        "job",
        "list",
        extra_env={
            "JENKINS_AXI_TEST_SEARCH_BLOCK": search_block(fake_jenkins.url)
            + "\n\n"
            + other
        },
    )
    assert done.returncode == 1
    assert "2 Secret Service entries found" in done.stdout
    assert "--url" in done.stdout


def test_multi_entry_url_selects(harness, fake_jenkins):
    other = search_block("http://other.example.net:8080", secret="other")
    block = search_block(fake_jenkins.url) + "\n\n" + other
    fake_jenkins.set_route_json("/api/json", {"jobs": []})
    done = harness.run(
        "job",
        "list",
        "--url",
        fake_jenkins.url,
        extra_env={"JENKINS_AXI_TEST_SEARCH_BLOCK": block},
    )
    assert done.returncode == 0


def test_url_select_fails_with_found_urls(harness, fake_jenkins):
    done = harness.run(
        "job",
        "list",
        "--url",
        "http://absent.example.net",
        extra_env={"JENKINS_AXI_TEST_SEARCH_BLOCK": search_block(fake_jenkins.url)},
    )
    assert done.returncode == 1
    assert "no Secret Service entry with url" in done.stdout
    assert fake_jenkins.url in done.stdout


def test_locked_vault_fails_closed_fast(harness, fake_jenkins):
    """A locked vault BLOCKS the real secret-tool waiting on the unlock prompt
    (verified live against KeePassXC); the sleeping stub stands in for it, and
    the fail-closed timeout must land fast with the unlock remedy."""
    done = harness.run(
        "job",
        "list",
        extra_env={
            "JENKINS_AXI_TEST_SEARCH_BLOCK": search_block(fake_jenkins.url),
            "JENKINS_AXI_TEST_SECRET_TOOL_SLEEP": "3",
            "JENKINS_AXI_SECRET_TIMEOUT": "0.2",
        },
        timeout=10,
    )
    assert done.returncode == 1
    assert "vault locked" in done.stdout
    assert "Unlock" in done.stdout


def test_missing_username_attribute_is_reported(harness, fake_jenkins):
    block = search_block(fake_jenkins.url).replace(
        "attribute.username = alec", "attribute.username = "
    )
    done = harness.run(
        "job", "list", extra_env={"JENKINS_AXI_TEST_SEARCH_BLOCK": block}
    )
    assert done.returncode == 1
    assert "missing: username" in done.stdout
