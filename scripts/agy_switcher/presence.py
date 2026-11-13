"""Presence Detector & Secure Log Opening.

Layer 2 Domain Engine: Discovers active Antigravity Desktop sessions and provides secure append log file descriptors.
Zero external dependencies. Pure Python 3.10+ standard library.
Strictly acyclic DAG: Does NOT import vault.py, rotation.py, doctor.py, hook.py, or cli.py.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
import time
from typing import Any

from agy_switcher.security import _atomic_write_file

DEFAULT_GEMINI_DIR = Path.home() / ".gemini"
DEFAULT_VAULT_DIR = Path.home() / ".antigravity-vault"


def _is_mock_object(obj: object) -> bool:
    """Returns True if object is an instance of unittest.mock Mock or MagicMock."""
    if obj is None:
        return False
    cls = type(obj)
    mod = getattr(cls, "__module__", "")
    name = cls.__name__
    return (
        "unittest.mock" in mod
        or name in ("Mock", "MagicMock", "NonCallableMagicMock", "NonCallableMock", "AsyncMock")
        or hasattr(obj, "_mock_return_value")
    )


def _is_test_runner() -> bool:
    return (
        "unittest" in sys.modules
        or "pytest" in sys.modules
        or "PYTEST_CURRENT_TEST" in os.environ
    )


def _is_prod_vault_path(path: Path | str) -> bool:
    try:
        p = Path(path).expanduser()
    except Exception:
        return False
    try:
        default_resolved = DEFAULT_VAULT_DIR.resolve()
    except Exception:
        default_resolved = DEFAULT_VAULT_DIR
    try:
        resolved = p.resolve()
    except Exception:
        resolved = p
    return (
        resolved == default_resolved
        or resolved == (default_resolved / p.name)
        or default_resolved in resolved.parents
        or str(p).startswith(str(DEFAULT_VAULT_DIR))
        or str(p).startswith("~/.antigravity-vault")
    )


def _resolve_vault_log_path(
    log_file: Path | str | None = None,
    engine: object | None = None,
) -> Path | None:
    """Safely resolves isolated vault log path, rejecting invalid or production paths under test runners.

    - If AGY_VAULT_DIR is set in the environment:
        * If non-empty and non-whitespace: validates candidate path. Under test runners,
          returns None if pointing to production vault; otherwise returns target log path.
        * If empty "" or whitespace "   ": rejected as invalid vault path without falling back
          to DEFAULT_VAULT_DIR. Under test runners, suppresses writes (returns None) unless
          engine specifies an isolated sandbox vault_dir. Outside test runners, falls back to DEFAULT_VAULT_DIR.
    - If AGY_VAULT_DIR is unset:
        * Checks engine.vault_dir if provided. Under test runners, returns None if pointing to prod.
        * Under test runners, suppresses writes for default vault paths (returns None).
        * Outside test runners, falls back to DEFAULT_VAULT_DIR.
    """
    try:
        if log_file is not None and str(log_file).strip() and str(log_file).strip() != ".":
            raw_path = Path(str(log_file).strip()).expanduser()
            is_default_vault = _is_prod_vault_path(raw_path)
            file_name = raw_path.name
        else:
            raw_path = None
            is_default_vault = True
            file_name = "watch.log"

        env_vault = os.environ.get("AGY_VAULT_DIR")
        if env_vault is not None:
            cleaned = env_vault.strip()
            if cleaned:
                candidate = Path(cleaned).expanduser()
                if _is_test_runner() and _is_prod_vault_path(candidate):
                    return None
                return candidate / file_name if (is_default_vault or raw_path is None) else raw_path

            # Empty or whitespace AGY_VAULT_DIR: reject without falling back to DEFAULT_VAULT_DIR
            if engine is not None:
                v = getattr(engine, "vault_dir", None)
                if v is not None and not _is_mock_object(v) and isinstance(v, (str, Path)) and str(v).strip():
                    candidate = Path(str(v).strip()).expanduser()
                    if not (_is_test_runner() and _is_prod_vault_path(candidate)):
                        return candidate / file_name if (is_default_vault or raw_path is None) else raw_path
            if not is_default_vault and raw_path is not None:
                return raw_path
            if _is_test_runner():
                return None
            return DEFAULT_VAULT_DIR / file_name if (is_default_vault or raw_path is None) else raw_path

        # AGY_VAULT_DIR is unset
        if engine is not None:
            v = getattr(engine, "vault_dir", None)
            if v is not None and not _is_mock_object(v) and isinstance(v, (str, Path)) and str(v).strip():
                candidate = Path(str(v).strip()).expanduser()
                if _is_test_runner() and _is_prod_vault_path(candidate):
                    return None
                return candidate / file_name if (is_default_vault or raw_path is None) else raw_path

        if not is_default_vault and raw_path is not None:
            return raw_path
        if is_default_vault and _is_test_runner():
            return None
        return DEFAULT_VAULT_DIR / file_name if (is_default_vault or raw_path is None) else raw_path
    except Exception:
        return None if _is_test_runner() else (DEFAULT_VAULT_DIR / "watch.log")


def _resolve_log_file_target(log_file: Path | str) -> Path | None:
    if not str(log_file).strip() or str(log_file).strip() == ".":
        return None if _is_test_runner() else (DEFAULT_VAULT_DIR / "watch.log")
    return _resolve_vault_log_path(log_file=log_file)


def _open_secure_log_file(log_file: Path | str) -> int:
    """Safely open an append-only log file with 0600 permissions and O_NOFOLLOW protection.

    If log_file is a pre-existing symlink, it is unlinked first to avoid writing through symlinks.
    Ensures parent directories exist with mode 0700.
    Returns the opened file descriptor.
    """
    path = Path(log_file)
    if not str(path).strip() or str(path).strip() == ".":
        raise ValueError("Cannot open empty or current directory path as log file")
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
    target_path = _resolve_log_file_target(log_file)
    if target_path is None:
        return
    path = target_path
    try:
        if path.parent and not path.parent.is_dir():
            try:
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.chmod(path.parent, 0o700)
            except OSError:
                pass
        opened_fd = _open_secure_log_file(path)
        log_fd: int | None = opened_fd
        try:
            with open(opened_fd, "a", encoding="utf-8", closefd=True) as f:
                log_fd = None
                f.write(message)
                f.flush()
        finally:
            if log_fd is not None:
                try:
                    os.close(log_fd)
                except OSError:
                    pass
    except Exception as e:
        try:
            sys.stderr.write(f"[watch] Failed to write audit log entry to {path}: {e}\n")
        except Exception:
            pass


def _write_atomic_file_secure(target_file: Path | str, payload: bytes | str) -> None:
    """Atomically writes payload to target_file with mode 0600 and ensures mode 0700 parent."""
    _atomic_write_file(target_file, payload, mode=0o600)


class PresenceDetector:
    """Discovers active Antigravity session across Desktop and CLI environments."""

    def __init__(
        self,
        gemini_dir: Path | str | None = None,
        proc_dir: Path | str | None = None,
    ):
        self.gemini_dir = Path(gemini_dir).expanduser() if gemini_dir else DEFAULT_GEMINI_DIR
        self.presence_dir = self.gemini_dir / "antigravity" / "presence"
        self.brain_dir = self.gemini_dir / "antigravity" / "brain"
        self.cli_presence_dir = self.gemini_dir / "antigravity-cli" / "presence"
        self.cli_brain_dir = self.gemini_dir / "antigravity-cli" / "brain"
        self.cli_log_dir = self.gemini_dir / "antigravity-cli" / "log"
        self.proc_dir = Path(proc_dir) if proc_dir else Path("/proc")

    def _discover_from_proc(self) -> str | None:
        """
        Tier 1: Scans proc_dir/<pid>/fd/* of running language_server or antigravity processes
        to find file descriptors referencing presence/*.lock across Desktop and CLI presence directories.
        Returns the active session ID (or newest if multiple), or None if not found or inaccessible.
        """
        if not self.proc_dir.is_dir():
            return None

        candidates: list[tuple[float, str, str]] = []
        presence_dirs = [self.presence_dir, self.cli_presence_dir]

        try:
            for pid_entry in self.proc_dir.iterdir():
                if not pid_entry.name.isdigit():
                    continue

                cmdline_file = pid_entry / "cmdline"
                try:
                    cmdline = cmdline_file.read_bytes().replace(b"\x00", b" ")
                except OSError:
                    continue

                if b"language_server" not in cmdline and b"antigravity" not in cmdline:
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

                        if not target.startswith("/"):
                            continue

                        if target.endswith(" (deleted)"):
                            target = target[:-10]

                        target_path = Path(target)
                        if target_path.suffix != ".lock":
                            continue

                        session_id = target_path.stem
                        if not session_id or session_id.startswith("."):
                            continue

                        matched_dir: Path | None = None
                        for p_dir in presence_dirs:
                            try:
                                if (
                                    target_path.parent.resolve() == p_dir.resolve()
                                    or str(target_path.parent) == str(p_dir)
                                ):
                                    matched_dir = p_dir
                                    break
                            except OSError:
                                if str(target_path.parent) == str(p_dir):
                                    matched_dir = p_dir
                                    break

                        if matched_dir is None:
                            continue

                        real_lock_file = matched_dir / target_path.name
                        try:
                            if real_lock_file.is_dir():
                                continue
                            if (
                                real_lock_file.is_symlink()
                                and real_lock_file.resolve().parent != matched_dir.resolve()
                            ):
                                continue
                        except OSError:
                            pass

                        mtime = 0.0
                        try:
                            if real_lock_file.is_file():
                                mtime = real_lock_file.stat().st_mtime
                        except OSError:
                            pass

                        candidates.append((mtime, session_id, str(matched_dir / target_path.name)))
                except OSError:
                    continue
        except OSError:
            return None

        if not candidates:
            return None

        candidates.sort(key=lambda x: (-x[0], x[1], x[2]))
        return candidates[0][1]

    def _discover_from_presence_locks(self) -> str | None:
        """
        Tier 2: Falls back to inspecting presence/*.lock across both presence_dir
        and cli_presence_dir, selecting the candidate with the newest mtime.
        """
        candidates: list[tuple[float, str, str]] = []
        presence_dirs = [self.presence_dir, self.cli_presence_dir]

        for p_dir in presence_dirs:
            try:
                if not p_dir.is_dir():
                    continue
                for lock_file in p_dir.iterdir():
                    try:
                        if not lock_file.is_file() or lock_file.suffix != ".lock":
                            continue
                        session_id = lock_file.stem
                        if not session_id or session_id.startswith("."):
                            continue
                        if lock_file.is_symlink():
                            try:
                                if lock_file.resolve().parent != p_dir.resolve():
                                    continue
                            except OSError:
                                continue
                        mtime = lock_file.stat().st_mtime
                        candidates.append((mtime, session_id, str(lock_file)))
                    except OSError:
                        continue
            except OSError:
                continue

        if not candidates:
            return None

        candidates.sort(key=lambda x: (-x[0], x[1], x[2]))
        return candidates[0][1]

    def get_active_session_id(self) -> str | None:
        """
        Discovers the active Antigravity session ID across Desktop and CLI environments.
        Tier 1: Scans /proc/*/fd for process file descriptors pointing to presence/*.lock.
        Tier 2: Falls back to newest .lock file mtime across presence directories.
        Returns UUID or session identifier string, or None if no active session found.
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
        resolved from Desktop brain (~/.gemini/antigravity/brain/<id>) or
        CLI brain (~/.gemini/antigravity-cli/brain/<id>).
        If both transcript files exist, returns the file with the newest mtime.
        If only one exists, returns that file.
        If neither file exists, checks for existing session directories under Desktop and CLI
        brain and returns the expected transcript path (preferring newest directory mtime).
        Returns None if no active session is found or neither session directory exists.
        """
        if session_id is None:
            session_id = self.get_active_session_id()
        if not session_id:
            return None

        desk_session_dir = self.brain_dir / session_id
        cli_session_dir = self.cli_brain_dir / session_id

        desk_transcript = desk_session_dir / ".system_generated" / "logs" / "transcript.jsonl"
        cli_transcript = cli_session_dir / ".system_generated" / "logs" / "transcript.jsonl"

        def _is_file(p: Path) -> bool:
            try:
                return p.is_file()
            except OSError:
                return False

        def _is_dir(p: Path) -> bool:
            try:
                return p.is_dir()
            except OSError:
                return False

        def _mtime(p: Path) -> float:
            try:
                return p.stat().st_mtime
            except OSError:
                return 0.0

        desk_file_exists = _is_file(desk_transcript)
        cli_file_exists = _is_file(cli_transcript)

        if desk_file_exists and cli_file_exists:
            return cli_transcript if _mtime(cli_transcript) > _mtime(desk_transcript) else desk_transcript
        elif desk_file_exists:
            return desk_transcript
        elif cli_file_exists:
            return cli_transcript

        desk_dir_exists = _is_dir(desk_session_dir)
        cli_dir_exists = _is_dir(cli_session_dir)

        if desk_dir_exists and cli_dir_exists:
            return cli_transcript if _mtime(cli_session_dir) > _mtime(desk_session_dir) else desk_transcript
        elif desk_dir_exists:
            return desk_transcript
        elif cli_dir_exists:
            return cli_transcript

        return None

    def get_watch_targets(self, as_str: bool = False, session_id: str | None = None) -> list[Path] | list[str]:
        """
        Returns list of active log targets to watch:
        - Active transcript.jsonl if resolved and existing on disk.
        Strictly bounds watch targets to transcript.jsonl, omitting raw CLI log files.
        Yields empty list [] when no active transcript exists.
        """
        active_transcript = self.get_active_transcript_path(session_id=session_id)
        if active_transcript is not None:
            try:
                if active_transcript.is_file():
                    if as_str:
                        return [str(active_transcript)]
                    return [active_transcript]
            except OSError:
                pass

        return []



__all__ = [
    "_resolve_vault_log_path",
    "_resolve_log_file_target",
    "_open_secure_log_file",
    "append_secure_log_entry",
    "_write_atomic_file_secure",
    "PresenceDetector",
    "DEFAULT_GEMINI_DIR",
]
