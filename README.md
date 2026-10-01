# jenkins-axi

Agent-ergonomic Jenkins CLI per the Agent eXperience Interface
(<https://axi.md/>): token-efficient TOON output, stated zeros, structured
errors on stdout with the fix inline.

## Scope

Monitor pipelines; start, restart, and stop builds. Nothing else. The only
write endpoints are a fixed whitelist in `client.py` (build,
buildWithParameters, stop, replay), so job config, credentials, plugins,
nodes, and the script console are out of reach by construction.

## Install

```
uv tool install git+https://github.com/artlair/jenkins-axi
```

## Setup

Generate an API token in the Jenkins UI (your user -> Configure -> API
Token), then run once:

```
jenkins-axi setup --url http://jenkins.example.net:8080 --username youruser
# paste the token at the prompt (stdin, never argv)
```

The URL, username, and token are stored via `secret-tool` in whatever
Secret Service backend the desktop provides (GNOME Keyring, KeePassXC, ...).
Re-running setup for a url replaces its entry; to remove one:
`secret-tool clear service jenkins-axi url <url>`. For multiple servers, run
setup per server and select with `--url`.

## Usage

Run bare `jenkins-axi` first: it prints live state (reachable, auth, queue
depth) and lists its subcommands. `jenkins-axi <cmd> --help` is the
authority on flags.

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

Job addressing: `project`, or `project/branch` for multibranch jobs.
Everything after the first `/` joins into one branch name, so slashes in
branch names work as-is (`Batman/bugfix/DEV-1217`). `last` is the default
build; a build number works too.

## Development

```
uv sync --extra dev
uv run pytest
uv run black . && uv run ruff check .
```

Tests drive the real CLI as a subprocess against a fake Jenkins HTTP server
and a stub `secret-tool` on PATH; nothing touches a network or a secret
store.
