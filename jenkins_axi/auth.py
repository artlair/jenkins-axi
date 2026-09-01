"""Auth: the Jenkins URL/username/API token, stored and resolved through the
freedesktop Secret Service via `secret-tool`.

The entry carries attributes `service=jenkins-axi`, `url=<base url>`,
`username=<jenkins user>`; the item's secret is the Jenkins API token. Any
Secret Service provider answers (GNOME Keyring on colleagues' desktops,
KeePassXC here), because `secret-tool` is the provider-agnostic CLI both
writing (setup) and reading (every command).

Parsing facts ported from the house-tested confluence-axi wrapper
(formulas/atlassian-axi/files/confluence-axi.sh, verified live against
libsecret 0.21.7 + KeePassXC):

  * `secret-tool search` writes the [path]/label/secret/created/modified/
    schema block to stdout but EVERY `attribute.<key> = <value>` line to
    STDERR (g_printerr in libsecret's tool/secret-tool.c), so both streams
    are captured and merged for one lookup.
  * a LOCKED vault makes the search BLOCK waiting on the unlock prompt
    (verified: it hangs rather than erroring), so every subprocess call runs
    under a hard timeout and fails closed with the unlock remedy; the
    timeout is an explicit parameter (production default 10s) so tests can
    pass a short one rather than hanging for real.
  * a missing match exits 0 with EMPTY output; unreachable service lands
    there too. All three collapse onto one lookup-failed message.
  * everything after the FIRST ` = ` separator is the value (tokens may
    contain ` = ` themselves); keys are whitespace-trimmed; empty values
    must not win, because KeePassXC always emits attribute.UserName first
    even when the Username field is empty (an empty UserName would shadow a
    later non-empty username attribute).

Salt never holds the secret, and neither does anything else: it lives in the
user's secret store, written once by `setup` reading stdin (never argv).
"""

from __future__ import annotations

import getpass
import subprocess
import sys
from dataclasses import dataclass
from urllib.parse import urlsplit

from jenkins_axi.errors import AxiError

ENTRY_SERVICE = "jenkins-axi"

# Production default for the hard timeout around every secret-tool call. A
# locked vault BLOCKS the subprocess waiting on the unlock prompt, so this is
# the failure-closed bound, not an optimization.
DEFAULT_SECRET_TIMEOUT = 10.0


@dataclass(frozen=True)
class Entry:
    """One stored server credential: everything a command needs to talk."""

    url: str
    username: str
    token: str

    @classmethod
    def from_pairs(cls, url: str, username: str, token: str) -> Entry:
        return cls(normalize_url(url), username, token)


def normalize_url(url: str) -> str:
    """Strip a trailing slash; require http(s) so a mangling user input fails
    here (at setup or resolve) rather than as a confusing 404 later."""
    url = url.strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise AxiError(f"not a valid Jenkins URL: {url!r}", "Use http(s)://host[:port]")
    return url


def secret_tool(*args: str, secret_input: str | None = None, timeout: float) -> str:
    """Run secret-tool, merging both streams (attributes land on stderr),
    under the hard fail-closed timeout."""
    try:
        proc = subprocess.run(
            ["secret-tool", *args],
            input=secret_input,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError as e:
        raise AxiError(
            "secret-tool not found: install libsecret (the freedesktop Secret Service CLI)",
        ) from e
    except subprocess.TimeoutExpired as e:
        raise AxiError(
            "Secret Service blocked the lookup past "
            f"{int(timeout)}s (vault locked, waiting on the unlock prompt?)",
            "Unlock the vault and re-run",
        ) from e
    # Merged streams are parse-safe: stray stderr diagnostics never equal a
    # bare secret/attribute key (verified in the confluence-axi wrapper).
    return proc.stdout + proc.stderr


def _parse_search(raw: str) -> list[dict[str, str]]:
    """Parse merged `secret-tool search --all` output into one dict per item.

    Items are separated by blank lines. Per key the first NON-EMPTY value
    wins: empty always-emitted-but-empty lines (KeePassXC's attribute.UserName)
    must not shadow a later non-empty one (attribute.username).
    """
    items: list[dict[str, str]] = []
    for block in raw.split("\n\n"):
        pairs: dict[str, str] = {}
        for line in block.splitlines():
            i = line.find(" = ")
            if i <= 0:
                continue
            key = line[:i].strip()
            value = line[i + 3 :]
            if value and key not in pairs:
                pairs[key] = value
        if pairs:
            items.append(pairs)
    return items


def _attribute(item: dict[str, str], name: str) -> str | None:
    """Read attribute.<name>, case-insensitively on the attribute part: we
    store lowercase via secret-tool store, while KeePassXC exposes the entry
    Username field as attribute.UserName."""
    prefix = "attribute."
    for key, value in item.items():
        if key.lower() == (prefix + name).lower():
            return value or None
    return None


def resolve(
    url_filter: str | None = None, timeout: float = DEFAULT_SECRET_TIMEOUT
) -> Entry:
    """Resolve the stored credential, failing closed with the remedy inline.

    Exactly one entry → use it. Several → fail listing their urls (the fix is
    `--url <one of them>`); a url_filter narrows the selection first.
    """
    raw = secret_tool("search", "--all", "service", ENTRY_SERVICE, timeout=timeout)
    items = _parse_search(raw)
    if not items:
        raise AxiError(
            "no Secret Service entry found for Jenkins",
            f"Run `jenkins-axi setup --url <url> --username <user>` "
            f"(expects attributes service={ENTRY_SERVICE}, url, username)",
        )
    entries = [
        item
        for item in items
        if url_filter is None or _attribute(item, "url") == normalize_url(url_filter)
    ]
    if len(entries) == 1:
        item = entries[0]
        url = _attribute(item, "url")
        username = _attribute(item, "username")
        token = item.get("secret")
        missing = [
            name
            for name, value in [("url", url), ("username", username), ("secret", token)]
            if not value
        ]
        if missing:
            raise AxiError(
                "Secret Service entry for Jenkins is missing: " + ", ".join(missing),
                "Re-create it with `jenkins-axi setup --url <url> --username <user>`",
            )
        return Entry.from_pairs(url, username, token)
    if not entries:
        known = sorted({u for item in items if (u := _attribute(item, "url"))})
        raise AxiError(
            f"no Secret Service entry with url {normalize_url(url_filter)!r} "
            f"(found: {', '.join(repr(u) for u in known)})",
            "Run `jenkins-axi setup --url <url> --username <user>` to add it, "
            "or `jenkins-axi --url <one of the found urls>`",
        )
    urls = sorted({u for item in entries if (u := _attribute(item, "url"))})
    raise AxiError(
        f"{len(entries)} Secret Service entries found (urls: {', '.join(repr(u) for u in urls)})",
        "Pass --url <one of the found urls> to select one",
    )


def setup(url: str, username: str, timeout: float = DEFAULT_SECRET_TIMEOUT) -> Entry:
    """Store url + username + token (read from stdin, never argv) as ONE entry.

    Writes through secret-tool, so the secret lands in whatever Secret Service
    backend the session bus answers with.
    """
    url = normalize_url(url)
    print("Jenkins API token (paste, then Enter):", file=sys.stderr)
    token = (
        getpass.getpass("") if sys.stdin.isatty() else sys.stdin.readline().rstrip("\n")
    )
    if not token.strip():
        raise AxiError("empty API token: nothing stored", "Re-run and paste the token")
    secret_tool(
        "store",
        "--label=Jenkins (jenkins-axi)",
        "service",
        ENTRY_SERVICE,
        "url",
        url,
        "username",
        username,
        secret_input=token,
        timeout=timeout,
    )
    return Entry(url, username, token)
