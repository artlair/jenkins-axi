"""jenkins-axi exception types.

`AxiError` carries optional hints that main() turns into the structured
stdout error (AXI §6). The subtypes let auth status (and anything else that
cares) separate the failure causes honestly:

  * `NotFound`, HTTP 404: "no such job" vs "job exists, no builds" is
    decided by the caller, which knows what path it asked for.
  * `Unreachable`, the server did not answer at all (network/VPN).
  * `AuthFailed`, the server answered but rejected the credential.
"""

from __future__ import annotations


class AxiError(Exception):
    """A user-facing failure. `hints` name the fixing commands."""

    def __init__(self, message: str, *hints: str) -> None:
        super().__init__(message)
        self.message = message
        self.hints = hints


class NotFound(AxiError):
    """HTTP 404 from the server."""


class Unreachable(AxiError):
    """The server did not answer (connection error, timeout)."""


class AuthFailed(AxiError):
    """The server answered but rejected the credentials (401/403)."""
