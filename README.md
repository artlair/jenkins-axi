# jenkins-axi

Agent-ergonomic Jenkins CLI, per the Agent eXperience Interface
(<https://axi.md/>): TOON output, stated zeros, structured errors on stdout
with the fix inline.

## Scope

**Monitor pipelines; start/restart/stop builds. Nothing else.**

The REST client exposes `get()` plus a `post()` whose endpoints are a fixed
whitelist in `client.py` (build, buildWithParameters, stop, replay). That is
the entire write surface. Config changes and destructive actions, create or
edit or delete jobs, config.xml, plugin/credential/node administration, the
script console, are excluded by construction, not by policy. The whitelist is
the reviewable boundary; an eyeball on four regexes covers it.

## Install

```
uv tool install git+https://github.com/artlair/jenkins-axi
```

## Setup

One-time: store the Jenkins URL, username, and API token in whatever Secret
Service backend the desktop has (GNOME Keyring, KeePassXC, ...), via
`secret-tool`, the freedesktop Secret Service CLI:

```
jenkins-axi setup --url http://jenkins.example.net:8080 --username youruser
# API token read from stdin (paste, then Enter)
```

Generate the API token from the Jenkins UI: your user → Configure → API Token
→ Add new Token (on Jenkins 2.346.x LTS it lives on the Configure page; the
separate per-user Security page came in a later release). The token is read from stdin, never argv, and stored through
`secret-tool` into the backend your session bus answers with. The CLI reads
it back with `secret-tool search --all service jenkins-axi`, so any Secret
Service provider works. Multiple servers: add entries with different `url`
attributes and select with `--url`.

## Usage

Run bare `jenkins-axi`: it prints live state (reachable, auth, queue depth)
and names its own subcommands. Per-subcommand `--help` is the authority on
flags, so they are not copied here.

Subcommands, noun-first:

```
setup           store url + username + token via secret-tool
auth status     reachable? secret found? auth ok?
job list        top-level jobs, or branches of a multibranch job
job view        one job's detail (kind, health, last success/failure, params)
build view      one build's status; while building: elapsed vs estimated
build console   a build's console output (--tail N default, --full)
build watch     poll a build until it finishes (read-only polling)
build start     trigger a build (--param k=v, repeatable)
build restart   re-run: same script+params via /replay/rebuild, or re-trigger with the build's original parameters
build stop      stop an in-progress build (honest no-op if completed)
queue list      the build queue, with the "why" per item
```

Job address: `project` or `project/branch` for multibranch jobs. Branch names
often contain slashes (`bugfix%2FDEV-1217` as Jenkins lists them) and may be
addressed raw (`Batman/bugfix/DEV-1217`); everything after the first `/` joins
into one branch name, percent-encoding is passed through.

## Development

```
uv sync --extra dev
uv run pytest
uv run black . && uv run ruff check .
```

Tests drive the real CLI as a subprocess against a fake Jenkins HTTP server
and a stub `secret-tool` on PATH; nothing touches a network or a secret
store.
