"""Presence Detector & Secure Log Opening.

Layer 2 Domain Engine: Discovers active Antigravity Desktop sessions and provides secure append log file descriptors.
Zero external dependencies. Pure Python 3.10+ standard library.
Strictly acyclic DAG: Does NOT import vault.py, rotation.py, doctor.py, hook.py, or cli.py.
"""

from __future__ import annotations

import os
from pathlib import Path
import time
from typing import Any

from agy_switcher.security import _atomic_write_file

DEFAULT_GEMINI_DIR = Path.home() / ".gemini"


def _open_secure_log_file(log_file: Path | str) -> int:
    """Safely open an append-only log file with 0600 permissions and O_NOFOLLOW protection.

    If log_file is a pre-existing symlink, it is unlinked first to avoid writing through symlinks.
    Ensures parent directories exist with mode 0700.
    Returns the opened file descriptor.
    """
    path = Path(log_file)
    if path.parent and not path.parent.is_dir():
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path.parent, 0o700)
        except OSError:
            pass

    if path.is_symlink():
        try:
            path.unlink()
        except OSError:
            pass

    open_flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    log_fd = os.open(path, open_flags, 0o600)
    try:
        os.fchmod(log_fd, 0o600)
    except OSError:
        pass
    return log_fd


def append_secure_log_entry(log_file: Path | str, message: str) -> None:
    """Safely append a message entry to a log file with 0600 permissions and mode 0700 parent."""
    path = Path(log_file)
    try:
        if path.parent and not path.parent.is_dir():
            try:
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.chmod(path.parent, 0o700)
            except OSError:
                pass
        log_fd = _open_secure_log_file(path)
        try:
            with open(log_fd, "a", encoding="utf-8", closefd=True) as f:
                log_fd = None
                f.write(message)
                f.flush()
        finally:
            if log_fd is not None:
                try:
                    os.close(log_fd)
                except OSError:
                    pass
    except Exception:
        pass


def _write_atomic_file_secure(target_file: Path | str, payload: bytes | str) -> None:
    """Atomically writes payload to target_file with mode 0600 and ensures mode 0700 parent."""
    _atomic_write_file(target_file, payload, mode=0o600)


class PresenceDetector:
    """Discovers active Antigravity Desktop conversation session without historical session scans."""

    def __init__(
        self,
        gemini_dir: Path | str | None = None,
        proc_dir: Path | str | None = None,
    ):
        self.gemini_dir = Path(gemini_dir).expanduser() if gemini_dir else DEFAULT_GEMINI_DIR
        self.presence_dir = self.gemini_dir / "antigravity" / "presence"
        self.brain_dir = self.gemini_dir / "antigravity" / "brain"
        self.cli_log_dir = self.gemini_dir / "antigravity-cli" / "log"
        self.proc_dir = Path(proc_dir) if proc_dir else Path("/proc")

    def _discover_from_proc(self) -> str | None:
        """
        Tier 1: Scans proc_dir/<pid>/fd/* of running language_server processes
        to find file descriptors referencing presence/<uuid>.lock.
        Returns the active session ID (or newest if multiple), or None if not found or inaccessible.
        """
        if not self.proc_dir.is_dir():
            return None

        candidates: list[tuple[float, str]] = []

        try:
            for pid_entry in self.proc_dir.iterdir():
                if not pid_entry.name.isdigit():
                    continue

                cmdline_file = pid_entry / "cmdline"
                try:
                    cmdline = cmdline_file.read_bytes().replace(b"\x00", b" ")
                except OSError:
                    continue

                if b"language_server" not in cmdline:
                    continue

                fd_dir = pid_entry / "fd"
                if not fd_dir.is_dir():
                    continue

                try:
                    for fd_entry in fd_dir.iterdir():
                        try:
                            target = os.readlink(fd_entry)
                        except OSError:
                            continue

                        if target.endswith(" (deleted)"):
                            target = target[:-10]

                        target_path = Path(target)
                        if target_path.suffix != ".lock":
                            continue

                        try:
                            target_parent_resolved = target_path.parent.resolve()
                            presence_resolved = self.presence_dir.resolve()
                            is_in_presence = (
                                target_parent_resolved == presence_resolved
                                or str(target_path.parent) == str(self.presence_dir)
                            )
                        except OSError:
                            is_in_presence = str(target_path.parent) == str(self.presence_dir)

                        if not is_in_presence:
                            continue

                        session_id = target_path.stem
                        if not session_id:
                            continue

                        mtime = 0.0
                        try:
                            real_lock_file = self.presence_dir / target_path.name
                            if real_lock_file.is_file():
                                mtime = real_lock_file.stat().st_mtime
                        except OSError:
                            pass

                        candidates.append((mtime, session_id))
                except OSError:
                    continue
        except OSError:
            return None

        if not candidates:
            return None

        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    def _discover_from_presence_locks(self) -> str | None:
        """
        Tier 2: Falls back to inspecting presence/*.lock and selects the file
        with the newest mtime.
        """
        if not self.presence_dir.is_dir():
            return None

        candidates: list[tuple[float, str]] = []
        try:
            for lock_file in self.presence_dir.iterdir():
                if not lock_file.is_file() or lock_file.suffix != ".lock":
                    continue
                try:
                    mtime = lock_file.stat().st_mtime
                    candidates.append((mtime, lock_file.stem))
                except OSError:
                    continue
        except OSError:
            return None

        if not candidates:
            return None

        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    def get_active_session_id(self) -> str | None:
        """
        Discovers the active Antigravity Desktop conversation ID.
        Tier 1: Scans /proc/*/fd for language_server file descriptors pointing to presence/*.lock.
        Tier 2: Falls back to newest .lock file mtime in presence_dir.
        Returns UUID string or None if no active session found.
        """
        try:
            session_id = self._discover_from_proc()
        except OSError:
            session_id = None

        if session_id:
            return session_id

        try:
            return self._discover_from_presence_locks()
        except OSError:
            return None

    def get_active_transcript_path(self, session_id: str | None = None) -> Path | None:
        """
        Returns Path to brain/<active-id>/.system_generated/logs/transcript.jsonl
        when the session directory exists, or None.
        If session_id is provided, bypasses calling get_active_session_id() again.
        """
        if session_id is None:
            session_id = self.get_active_session_id()
        if not session_id:
            return None

        session_dir = self.brain_dir / session_id
        if not session_dir.is_dir():
            return None

        return session_dir / ".system_generated" / "logs" / "transcript.jsonl"

    def get_watch_targets(self, as_str: bool = False, session_id: str | None = None) -> list[Path] | list[str]:
        """
        Returns list of active log targets to watch:
        - Active transcript.jsonl if resolved and existing on disk.
        - Existing CLI logs in antigravity-cli/log/*.log.
        Omits non-existent paths.
        """
        targets: list[Path] = []
        active_transcript = self.get_active_transcript_path(session_id=session_id)
        if active_transcript is not None and active_transcript.is_file():
            targets.append(active_transcript)

        if self.cli_log_dir.is_dir():
            try:
                for cli_log in sorted(self.cli_log_dir.glob("*.log")):
                    if cli_log.is_file():
                        targets.append(cli_log)
            except OSError:
                pass

        if as_str:
            return [str(p) for p in targets]
        return targets


__all__ = [
    "_open_secure_log_file",
    "append_secure_log_entry",
    "_write_atomic_file_secure",
    "PresenceDetector",
    "DEFAULT_GEMINI_DIR",
]
