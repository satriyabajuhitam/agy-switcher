"""Domain exception hierarchy for agy-switch.

Layer 0 Foundation module: Zero internal dependencies.
Pure Python 3.10+ standard library.
"""

from __future__ import annotations


class AgySwitchError(Exception):
    """Base exception for agy-switch operations."""


class InvalidTokenError(AgySwitchError):
    """Raised when an OAuth token fails structural or schema validation."""


class AccountNotFoundError(AgySwitchError):
    """Raised when a requested account does not exist in the vault."""


class ActiveTurnRunningError(AgySwitchError):
    """Raised when language_server is busy and switch/restart is attempted without --force."""


class DaemonError(AgySwitchError, RuntimeError):
    """Base exception for watcher daemon lifecycle operations."""


class DaemonAlreadyRunningError(DaemonError):
    """Raised when daemon start() is attempted while an active daemon is already running."""

    def __init__(self, pid: int, message: str | None = None) -> None:
        self.pid = pid
        msg = message or f"Watcher daemon is already running with PID {pid}."
        super().__init__(msg)


class InvalidThresholdError(AgySwitchError):
    """Raised when an invalid quota threshold value is specified."""


__all__ = [
    "AgySwitchError",
    "InvalidTokenError",
    "AccountNotFoundError",
    "ActiveTurnRunningError",
    "DaemonError",
    "DaemonAlreadyRunningError",
    "InvalidThresholdError",
]
