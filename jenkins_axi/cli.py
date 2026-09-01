"""jenkins-axi CLI.

Thin wiring per the house style: argparse dispatch -> api operations -> TOON
render. The E2E suite (tests/) drives THIS entrypoint as a subprocess against
fakes for the whole external world, per the house "test the logic, not the
entrypoint" ask: the logic lives in api/client/auth, exercised here through
the real wiring.
Subcommands are noun-first like the other axi CLIs (auth status, job list,
build view), which argparse models as nested subparsers. Flags come AFTER the
subcommand; --url retargets to another stored server.

Errors: api/client/auth raise AxiError (with the fixing commands as hints);
main() turns those into the structured stdout error (AXI §6) and exits 1.
Usage errors (no subcommand, unknown flag) exit 2. Bare invocation prints
live state, not help (AXI §8); --help on every subcommand is the flag
authority (AXI §10).
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from jenkins_axi import api, auth, render
from jenkins_axi.client import DEFAULT_TIMEOUT, JenkinsClient, JobRef
from jenkins_axi.errors import AuthFailed, AxiError, Unreachable
from jenkins_axi.toon import EXIT_OK, EXIT_USAGE, Toon, fail

DESCRIPTION = "Monitor Jenkins pipelines; start/restart/stop builds"

BARE_HINTS = (
    "jenkins-axi job list",
    "jenkins-axi job view <project>",
    "jenkins-axi build watch <project/branch> last",
    "jenkins-axi build console <project/branch> last",
)


def secret_timeout() -> float:
    """Fail-closed bound around every secret-tool call (a locked vault BLOCKS
    rather than erroring). Overridable via env so tests can pass a short one."""
    raw = os.environ.get("JENKINS_AXI_SECRET_TIMEOUT")
    return float(raw) if raw else auth.DEFAULT_SECRET_TIMEOUT


def client_for(args) -> JenkinsClient:
    """Resolve the stored credential (failing closed with the remedy inline)
    and build the client for it. The leaf options are default=SUPPRESS'd
    (fresh-eyes #5), so getattr with the root's values."""
    url = getattr(args, "url", None)
    timeout = getattr(args, "timeout", DEFAULT_TIMEOUT)
    entry = auth.resolve(url, timeout=secret_timeout())
    return JenkinsClient(entry.url, entry.username, entry.token, timeout=timeout)


def now_ms() -> int:
    return int(time.time() * 1000)


def when(timestamp_ms: int | None, now_ms: int) -> str:
    return render.when(timestamp_ms, now_ms)


def bin_path() -> str:
    path = os.path.abspath(sys.argv[0])
    home = os.path.expanduser("~")
    return "~" + path[len(home) :] if path.startswith(home) else path


def cmd_status(args) -> int:
    entry = auth.resolve(getattr(args, "url", None), timeout=secret_timeout())
    client = JenkinsClient(
        entry.url,
        entry.username,
        entry.token,
        timeout=getattr(args, "timeout", DEFAULT_TIMEOUT),
    )
    try:
        data, server = api.root(client)
        reachable, auth_ok = True, True
    except Unreachable:
        data, server = {}, None
        reachable, auth_ok = False, False
    except AuthFailed:
        data, server = {}, None
        reachable, auth_ok = True, False
    t = (
        Toon()
        .kv("bin", bin_path())
        .kv("description", DESCRIPTION)
        .kv("url", entry.url)
        .kv("reachable", reachable)
        .kv("auth", "ok" if auth_ok else "failed (check username/token)")
    )
    if server:
        t.kv("server", f"Jenkins {server}")
    if reachable and auth_ok:
        t.kv("jobs_total", len(data.get("jobs") or []))
        t.kv("queue_total", api.queue_total(client))
    t.blank().help(*BARE_HINTS)
    t.emit()
    return EXIT_OK


def cmd_setup(args) -> int:
    entry = auth.setup(args.url, args.username, timeout=secret_timeout())
    Toon().kv("stored", True).kv("url", entry.url).kv("username", entry.username).help(
        "jenkins-axi auth status",
    ).emit()
    return EXIT_OK


def cmd_job_list(args) -> int:
    client = client_for(args)
    now = now_ms()
    if args.name:
        ref = JobRef.from_string(args.name)
        branches = api.list_branches(client, ref, args.limit, now)
        total = api.list_branches_count(client, ref)
        # The --running filter runs over the fetched page only: shown keeps
        # meaning the PAGE (so truncated = page < total stays truthful) and
        # matched says how many of those matched. Filtering FIRST (round-2
        # review) made truncated: true fire on an untruncated list.
        rows = [
            {
                "name": b.name,
                "build": b.number,
                "status": b.status(now),
                "when": when(b.timestamp_ms, now),
            }
            for b in branches
        ]
        matched = None
        if args.running:
            rows = [row for row in rows if str(row["status"]).startswith("RUNNING")]
            matched = len(rows)
        t = (
            Toon()
            .kv("job", ref.display)
            .kv("total", total)  # the server-side count, not the page size
            .kv("shown", len(branches))
            .kv("truncated", len(branches) < total)
        )
        if matched is not None:
            t.kv("matched", matched)
        t.table("branches", ["name", "build", "status", "when"], rows)
        t.help(
            f"jenkins-axi build watch {ref.display}/<branch> last",
            f"jenkins-axi job view {ref.display}",
        ).emit()
        return EXIT_OK
    jobs = api.list_jobs(client, args.limit)
    total = api.list_jobs_count(client)
    t = (
        Toon()
        .kv("total", total)  # the server-side count, not the page size
        .kv("shown", len(jobs))
        .kv("truncated", len(jobs) < total)
        .table(
            "jobs",
            ["name", "kind", "color"],
            [{"name": j.name, "kind": j.kind, "color": j.color} for j in jobs],
        )
    )
    t.help("jenkins-axi job list <project> (branches of a multibranch job)").emit()
    return EXIT_OK


def build_ref(build: tuple[int, str, int] | None, now_ms: int) -> str:
    if not build:
        return "never"
    number, result, timestamp = build
    return f"#{number} {result} {when(timestamp, now_ms)}"


def cmd_job_view(args) -> int:
    client, ref = client_for(args), JobRef.from_string(args.name)
    now = now_ms()
    detail = api.job_detail(client, ref)
    t = (
        Toon()
        .kv("name", detail.name)
        .kv("job", ref.display)
        .kv("kind", detail.kind)
        .kv("description", detail.description)
        .kv("health", detail.health)
        .kv("last_success", build_ref(detail.last_success, now))
        .kv("last_failure", build_ref(detail.last_failure, now))
    )
    if detail.params:
        t.obj("params", detail.params)
    t.help(
        f"jenkins-axi build view {ref.display} last",
        f"jenkins-axi build console {ref.display} last",
    ).emit()
    return EXIT_OK


def spec_args(args) -> tuple[JenkinsClient, JobRef]:
    return client_for(args), JobRef.from_string(args.job)


def cmd_build_view(args) -> int:
    client, ref = spec_args(args)
    now = now_ms()
    info = api.build_info(client, ref, args.build, now_ms=now)
    t = Toon().kv("job", ref.display).kv("build", info.number)
    if info.building:
        t.kv(
            "status", "RUNNING " + api.progress(info.elapsed_ms(now), info.estimated_ms)
        )
    else:
        t.kv("status", info.result or "no result")
        t.kv("when", when(info.timestamp_ms, now))
        t.kv("duration", api.format_ms(info.duration_ms or 0))
    if info.params:
        t.obj("params", info.params)
    t.help(
        f"jenkins-axi build console {ref.display} {info.number}",
        f"jenkins-axi build stop {ref.display} {info.number}",
    ).emit()
    return EXIT_OK


def cmd_build_console(args) -> int:
    client, ref = spec_args(args)
    if args.tail < 1:
        raise AxiError(
            f"invalid --tail {args.tail}",
            "Tail is the number of trailing lines, minimum 1; use --full for everything",
        )
    text = api.console_text(client, ref, args.build)
    lines = text.splitlines()
    if len(lines) > args.tail and not args.full:
        total = len(lines)
        print(f"(truncated, {total} lines total, use --full)")
        lines = lines[-args.tail :]
    print("\n".join(lines))
    return EXIT_OK


def cmd_build_watch(args) -> int:
    client, ref = spec_args(args)
    info = api.build_info(client, ref, args.build, now_ms=now_ms())
    deadline = time.monotonic() + args.max
    while info.building:
        elapsed = info.elapsed_ms(now_ms())
        print(
            f"t+{api.format_ms(elapsed)}: RUNNING {api.progress(elapsed, info.estimated_ms)}",
            flush=True,
        )
        if time.monotonic() > deadline:
            fail(
                f"watch cap of {int(args.max)}s reached: build #{info.number} still running",
                f"Re-run `jenkins-axi build watch {ref.display} {info.number}`",
                f"Or poll: `jenkins-axi build view {ref.display} {info.number}`",
            )
        time.sleep(args.interval)
        info = api.build_info(client, ref, str(info.number), now_ms=now_ms())
    Toon().kv("job", ref.display).kv("build", info.number).kv(
        "status", info.result or "no result"
    ).kv("when", when(info.timestamp_ms, now_ms())).kv(
        "duration", api.format_ms(info.duration_ms or 0)
    ).help(
        f"jenkins-axi build console {ref.display} {info.number}",
        f"jenkins-axi build restart {ref.display} {info.number}",
    ).emit()
    return EXIT_OK


def param_dict(raw_params: list[str]) -> dict[str, str]:
    params: dict[str, str] = {}
    for raw in raw_params:
        if "=" not in raw:
            raise AxiError(
                f"invalid --param {raw!r}",
                "Params are k=v (repeatable), e.g. --param deployment-id=testec001",
            )
        key, value = raw.split("=", 1)
        params[key] = value
    return params


def cmd_build_start(args) -> int:
    client, ref = spec_args(args)
    result = api.start_build(client, ref, param_dict(args.param or []))
    t = Toon().kv("job", ref.display).kv("action", "start")
    if result.queue_id:
        t.kv("queue", result.queue_id)
    t.kv("message", result.message)
    t.help(f"jenkins-axi build watch {ref.display} last").emit()
    return EXIT_OK


def cmd_build_restart(args) -> int:
    client, ref = spec_args(args)
    result = api.restart_build(client, ref, args.build)
    t = (
        Toon()
        .kv("job", ref.display)
        .kv("action", "restart")
        .kv("original_build", result.original_number)
        .kv("path", result.path)
    )
    if result.queue_id:
        t.kv("queue", result.queue_id)
    t.help(f"jenkins-axi build watch {ref.display} last").emit()
    return EXIT_OK


def cmd_build_stop(args) -> int:
    client, ref = spec_args(args)
    result = api.stop_build(client, ref, args.build)
    t = (
        Toon()
        .kv("job", ref.display)
        .kv("build", result.number)
        .kv("stopped", result.stopped)
        .kv("detail", result.detail)
    )
    t.help(f"jenkins-axi build view {ref.display} {result.number}").emit()
    return EXIT_OK


def cmd_queue_list(args) -> int:
    client = client_for(args)
    now = now_ms()
    items = api.queue_items(client, args.limit)
    total = api.queue_total(client)
    Toon().kv("total", total).kv("shown", len(items)).kv(
        "truncated", len(items) < total
    ).table(
        "items",
        ["why", "job", "queued_for"],
        [
            {
                "why": item.why,
                "job": item.job,
                "queued_for": (
                    api.format_ms(now - item.in_queue_ms) if item.in_queue_ms else "0s"
                ),
            }
            for item in items
        ],
    ).help(
        "jenkins-axi job list"
    ).emit()
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    # default=SUPPRESS on the leaf copy: argparse copies the subparser
    # namespace OVER the root's, so a leaf default (None / 15.0) would
    # CLOBBER a root-parsed `--url http://x job list` (fresh-eyes #5). With
    # SUPPRESS the attribute is only set when the flag is actually passed.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--url", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument(
        "--timeout", type=float, default=argparse.SUPPRESS, help=argparse.SUPPRESS
    )

    parser = argparse.ArgumentParser(
        prog="jenkins-axi",
        description=DESCRIPTION + ". Bare invocation prints live state.",
    )
    parser.add_argument(
        "--url", help="select a stored server by url (default: the only entry)"
    )
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT, help=argparse.SUPPRESS
    )
    sub = parser.add_subparsers(dest="cmd")

    p_setup = sub.add_parser(
        "setup",
        help="store url + username + API token via secret-tool (token read from stdin)",
    )
    p_setup.add_argument("--url", required=True)
    p_setup.add_argument("--username", required=True)
    p_setup.add_argument(
        "--timeout", type=float, default=argparse.SUPPRESS, help=argparse.SUPPRESS
    )

    p_auth = sub.add_parser("auth", help="reachable? secret found? auth ok?")
    p_auth.add_subparsers(dest="sub").add_parser(
        "status", parents=[common], help="reachable? secret found? auth ok?"
    )

    p_job = sub.add_parser("job", help="list or view jobs")
    job_sub = p_job.add_subparsers(dest="sub")
    p_jobs = job_sub.add_parser(
        "list", parents=[common], help="top-level jobs, or branches of one job"
    )
    p_jobs.add_argument(
        "name", nargs="?", help="omit for all jobs; or a job to list its branches"
    )
    p_jobs.add_argument("--limit", type=int, default=50)
    p_jobs.add_argument(
        "--running", action="store_true", help="only branches with a running build"
    )
    p_jobv = job_sub.add_parser("view", parents=[common], help="one job's detail")
    p_jobv.add_argument("name")

    p_build = sub.add_parser(
        "build", help="view, watch, console, start, restart, stop builds"
    )
    build_sub = p_build.add_subparsers(dest="sub")
    p_bv = build_sub.add_parser("view", parents=[common], help="one build's status")
    p_bv.add_argument("job")
    p_bv.add_argument(
        "build", nargs="?", default="last", help="build number, or `last`"
    )
    p_bc = build_sub.add_parser(
        "console", parents=[common], help="a build's console output"
    )
    p_bc.add_argument("job")
    p_bc.add_argument("build", nargs="?", default="last")
    p_bc.add_argument(
        "--tail", type=int, default=200, help="last N lines (default 200)"
    )
    p_bc.add_argument("--full", action="store_true", help="no truncation")
    p_bw = build_sub.add_parser(
        "watch", parents=[common], help="poll a build until it finishes"
    )
    p_bw.add_argument("job")
    p_bw.add_argument("build", nargs="?", default="last")
    p_bw.add_argument(
        "--interval", type=float, default=5.0, help="poll seconds (default 5)"
    )
    p_bw.add_argument(
        "--max", type=float, default=1800.0, help="watch cap seconds (default 1800)"
    )
    p_bs = build_sub.add_parser(
        "start", parents=[common], help="trigger a build (--param k=v, repeatable)"
    )
    p_bs.add_argument("job")
    p_bs.add_argument("--param", action="append", help="k=v (repeatable)")
    p_br = build_sub.add_parser(
        "restart",
        parents=[common],
        help="re-run an existing build (replay or re-trigger)",
    )
    p_br.add_argument("job")
    p_br.add_argument("build", nargs="?", default="last")
    p_bstop = build_sub.add_parser(
        "stop",
        parents=[common],
        help="stop an in-progress build (honest no-op if completed)",
    )
    p_bstop.add_argument("job")
    p_bstop.add_argument("build", nargs="?", default="last")

    p_queue = sub.add_parser("queue", help="the build queue, with the why per item")
    p_ql = p_queue.add_subparsers(dest="sub").add_parser(
        "list", parents=[common], help="the build queue, with the why per item"
    )
    p_ql.add_argument("--limit", type=int, default=50)

    return parser


HANDLERS = {
    ("setup", None): cmd_setup,
    ("auth", "status"): cmd_status,
    ("job", "list"): cmd_job_list,
    ("job", "view"): cmd_job_view,
    ("build", "view"): cmd_build_view,
    ("build", "console"): cmd_build_console,
    ("build", "watch"): cmd_build_watch,
    ("build", "start"): cmd_build_start,
    ("build", "restart"): cmd_build_restart,
    ("build", "stop"): cmd_build_stop,
    ("queue", "list"): cmd_queue_list,
}


def dispatch(args) -> int:
    if not args.cmd:
        return cmd_status(args)
    sub = getattr(args, "sub", None)  # leaves without subcommands (setup) lack it
    if (args.cmd, sub) not in HANDLERS:
        # A noun alone ("jenkins-axi job"): usage error, AXI-shaped, exit 2.
        Toon().kv("error", f"no subcommand given for `{args.cmd}`").help(
            f"Run `jenkins-axi {args.cmd} --help` for subcommands and flags",
        ).emit(EXIT_USAGE)
    return HANDLERS[(args.cmd, sub)](args)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return dispatch(args)
    except AxiError as e:
        fail(e.message, *e.hints)
    except KeyboardInterrupt:
        fail("interrupted")
    except Exception as e:  # noqa: BLE001
        # The one boundary where a broad catch is right: a traceback on
        # stderr is invisible to an agent reading stdout, so even an
        # unexpected error must land on stdout in the same shape.
        fail(f"internal: {type(e).__name__}: {e}")


if __name__ == "__main__":
    sys.exit(main())
