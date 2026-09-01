"""TOON encoding (https://toonformat.dev/), the AXI output format (§1).

Encoder prelude copied from the working fleet reference
(my-salt/formulas/herdr/files/herdr-axi.py), which cites AXI §7.2 quoting and
§8-§9 forms. Do not re-derive quoting from the spec: a wrapper that quotes
wrong produces output the agent misparses silently. One form the prelude has
no method for, the inline primitive array (`tags[3]:`), is two lines in
render.py.
"""

from __future__ import annotations

import re
import sys

# Exit codes are AXI's (§6): 0 success (including no-ops), 1 error, 2 usage.
EXIT_OK, EXIT_ERR, EXIT_USAGE = 0, 1, 2

_UNQUOTED_NUMERIC = re.compile(
    r"^[+-]?[0-9]+(?:\.[0-9]+)?(?:e[+-]?[0-9]+)?$", re.IGNORECASE
)
_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _escape(s: str) -> str:
    out = []
    for ch in s:
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif ord(ch) < 0x20:
            out.append("\\u%04x" % ord(ch))
        else:
            out.append(ch)
    return "".join(out)


def _needs_quote(s: str) -> bool:
    """TOON §7.2 encoder quoting rules, with comma as the active delimiter."""
    if s == "":
        return True
    if s != s.strip(" \t"):
        return True
    if s in ("true", "false", "null"):
        return True
    if _UNQUOTED_NUMERIC.match(s):
        return True
    if any(c in s for c in ':"\\[]{},'):
        return True
    if s[0] in "-#":
        return True
    return any(ord(c) < 0x20 for c in s)


def val(v) -> str:
    """Encode a single TOON primitive."""
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    return '"%s"' % _escape(s) if _needs_quote(s) else s


def field(name: str) -> str:
    """Encode a TOON key/field name (§7.3)."""
    return (
        name if re.match(r"^[A-Za-z_][A-Za-z0-9_.]*$", name) else '"%s"' % _escape(name)
    )


class Toon:
    """Accumulates TOON lines. Everything the agent consumes goes to stdout."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def kv(self, key: str, value, indent: int = 0) -> Toon:
        self.lines.append("%s%s: %s" % ("  " * indent, field(key), val(value)))
        return self

    def obj(self, key: str, pairs: dict, indent: int = 0) -> Toon:
        """A nested object (§8). Omitted entirely when empty."""
        if not pairs:
            return self
        self.lines.append("%s%s:" % ("  " * indent, field(key)))
        for k, v in pairs.items():
            self.kv(k, v, indent + 1)
        return self

    def table(
        self, key: str, fields: list[str], rows: list[dict], indent: int = 0
    ) -> Toon:
        """An array of objects in tabular form (§9.3), or `key: []` when empty
        , AXI §5 wants the zero stated, not implied by silence."""
        pad = "  " * indent
        if not rows:
            self.lines.append("%s%s: []" % (pad, field(key)))
            return self
        self.lines.append(
            "%s%s[%d]{%s}:"
            % (pad, field(key), len(rows), ",".join(field(f) for f in fields))
        )
        for row in rows:
            self.lines.append(
                "%s%s"
                % ("  " * (indent + 1), ",".join(val(row.get(f)) for f in fields))
            )
        return self

    def help(self, *hints: str) -> Toon:
        """Contextual next steps (AXI §9), in the shape the AXI spec prints
        them: a counted header over plain indented lines."""
        hints = tuple(h for h in hints if h)
        if not hints:
            return self
        self.lines.append("help[%d]:" % len(hints))
        for h in hints:
            self.lines.append("  " + h)
        return self

    def blank(self) -> Toon:
        self.lines.append("")
        return self

    def emit(self, code: int = EXIT_OK):
        sys.stdout.write("\n".join(self.lines).rstrip("\n") + "\n")
        sys.exit(code)


def fail(message: str, *hints: str, code: int = EXIT_ERR):
    """A structured error, on stdout, in the same format as success (AXI §6).

    Errors on stderr are invisible to an agent that only reads stdout; an error
    it cannot see becomes a silent wrong turn. Every failure here carries the
    command that fixes it, so the correction costs one turn, not three.
    """
    Toon().kv("error", message).help(*hints).emit(code)
