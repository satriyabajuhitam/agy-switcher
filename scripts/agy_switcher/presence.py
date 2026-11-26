"""Presence Detector & Secure Log Opening.

Layer 2 Domain Engine: Discovers active Antigravity Desktop sessions and provides secure append log file descriptors.
Zero external dependencies. Pure Python 3.10+ standard library.
Strictly acyclic DAG: Does NOT import vault.py, rotation.py, doctor.py, hook.py, or cli.py.
"""

from __future__ import annotations

from datetime import datetime, timezone
import errno
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Callable, cast

from agy_switcher.security import (
    _atomic_write_file,
    parse_pending_reload_payload,
    read_json_dict_nofollow,
)

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
                return candidate / file_name if (is_default_vault or raw_path is None or file_name == "watch.log") else raw_path

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
        except OSError as dir_err:
            if dir_err.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)):
                raise


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
            except OSError as dir_err:
                if dir_err.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)):
                    raise
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
    except OSError as e:
        if e.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)):
            try:
                if sys.stderr is not None:
                    sys.stderr.write(f"[watch] Disk full (ENOSPC): Failed to write audit log entry to {path}: {e}\n")
                    sys.stderr.flush()
            except Exception:
                pass
            return
        try:
            if sys.stderr is not None:
                sys.stderr.write(f"[watch] Failed to write audit log entry to {path}: {e}\n")
                sys.stderr.flush()
        except Exception:
            pass
    except Exception as e:
        try:
            if sys.stderr is not None:
                sys.stderr.write(f"[watch] Failed to write audit log entry to {path}: {e}\n")
                sys.stderr.flush()
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



_EXHAUSTION_REGEX: re.Pattern[str] = re.compile(
    r"resource[_\s-]?exhausted"
    r"|\b(?:http(?:/\d+(?:\.\d+)?)?(?:_?(?:status|code))?|(?:grpc|response|api|http)_?(?:status|error)(?:_?code)?|error(?:_?code)?|code|status(?:_?code)?)\"?\s*(?:[:=]|\()?\s*\"?429\b"
    r"|\b429\b\"?\s*\)?\s*(?:[:\-]?\s*(?:quota\s+(?:exceeded|exhausted|reached)|resource\s*exhausted|rate[- ]?limit|too\s+many\s+requests)\b|:\s*quota\b)"
    r"|\b(?:too\s+many\s+requests|rate[- ]?limit(?:ed|\s+exceeded)?|quota\s+(?:exceeded|exhausted|reached))\s*[:\-]?\s*[\(\[]\s*429\s*[\)\]]"
    r"|Individual quota reached|weekly quota reached", re.IGNORECASE,
)


def _is_scalar_code(val: object) -> bool:
    return (isinstance(val, int) and not isinstance(val, bool)) or (isinstance(val, str) and val.strip().isdigit())


def _extract_text_fragments(value: object, max_items: int = 10000) -> list[str]:
    """Extracts unescaped text fragments and scalar status codes from nested JSON payloads.
    Guards against cyclic structures and excessive depth via visited tracking.
    """
    fragments: list[str] = []
    stack: list[object] = [value]
    visited: set[int] = set()
    items_processed = 0

    while stack and items_processed < max_items:
        current = stack.pop()
        items_processed += 1
        if isinstance(current, str):
            clean = current.strip()
            if clean:
                fragments.append(clean)
        elif _is_scalar_code(current):
            fragments.append(str(current).strip())
        elif isinstance(current, (list, tuple)):
            c_id = id(current)
            if c_id in visited:
                continue
            visited.add(c_id)
            stack.extend(reversed(current))
        elif isinstance(current, dict):
            c_id = id(current)
            if c_id in visited:
                continue
            visited.add(c_id)
            for k, v in reversed(list(current.items())):
                if isinstance(k, str) and _EXHAUSTION_REGEX.search(k):
                    fragments.append(k.strip())
                if isinstance(k, str) and _is_scalar_code(v):
                    stack.append(f"{k}: {str(v).strip()}")
                elif isinstance(v, (str, list, tuple, dict)) and v:
                    stack.append(v)
                elif _is_scalar_code(v):
                    stack.append(str(v).strip())
    return fragments


def read_latest_jsonl_record(target_path: Path) -> dict[str, Any] | None:
    """Reads and parses the trailing JSON object record from a JSONL transcript file."""
    try:
        size = target_path.stat().st_size
        if size == 0:
            return None
        max_read = 4 * 1024 * 1024
        chunk_size = min(size, 65536)
        with open(target_path, "rb") as f:
            while True:
                f.seek(max(0, size - chunk_size))
                data = f.read(min(chunk_size, size))
                last_line_str: str | None = None
                for line in reversed(data.splitlines()):
                    s = line.strip().decode("utf-8", errors="replace")
                    if s:
                        last_line_str = s
                        break
                if not last_line_str:
                    return None
                if not last_line_str.startswith("{") and chunk_size < size and chunk_size < max_read:
                    chunk_size = min(size, chunk_size * 4, max_read)
                    continue
                if not last_line_str.startswith("{"):
                    return None
                try:
                    parsed = json.loads(last_line_str)
                    return cast(dict[str, Any], parsed) if isinstance(parsed, dict) else None
                except json.JSONDecodeError:
                    if chunk_size < size and chunk_size < max_read:
                        chunk_size = min(size, chunk_size * 4, max_read)
                        continue
                    return None
    except OSError:
        return None


def safe_stderr_write(message: str) -> None:
    """Writes a diagnostic line to sys.stderr without raising on closed streams."""
    try:
        if sys.stderr is not None:
            sys.stderr.write(f"{message}\n")
            sys.stderr.flush()
    except Exception:
        pass


def write_watch_log_line(
    log_dir: Path | None,
    log_line: str,
    append_fn: Callable[[Path | str, str], None] = append_secure_log_entry,
) -> None:
    """Appends a formatted audit line to watch.log with mode 0600 and safe error handling."""
    try:
        if log_dir is None:
            return
        append_fn(log_dir / "watch.log", log_line)
    except OSError as e:
        msg = (
            f"[watch] Disk full (ENOSPC): Error appending audit log: {e}"
            if e.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC))
            else f"[watch] Error appending audit log: {e}"
        )
        safe_stderr_write(msg)
    except Exception as e:
        safe_stderr_write(f"[watch] Error appending audit log: {e}")


def format_audit_log_line(
    target_account: str,
    old_account: str | None = None,
    reason: str = "",
    reloaded: bool = False,
    clean_reason: str = "",
) -> str:
    """Formats a structured watcher account-switch audit line."""
    if not clean_reason and reason and isinstance(reason, str):
        r = reason.strip()
        while r.startswith("(") and r.endswith(")") and len(r) >= 2:
            r = r[1:-1].strip()
        clean_reason = r
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    log_reason = clean_reason or reason
    suffix = " with force-restart language_server" if reloaded else ""
    prefix = f"Switched from '{old_account}' to '{target_account}'" if old_account else f"Switched to '{target_account}'"
    return (
        f"{ts} [watch] {prefix}{suffix} (reason: {log_reason})\n"
        if (old_account or reloaded)
        else f"{ts} [watch] {prefix} ({log_reason})\n"
    )


def format_deferred_reload_audit_line(
    target_account: str,
    reason: str = "in_turn_deferred_reload",
) -> str:
    """Formats a structured deferred safe-turn reload audit line."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    clean_target = " ".join(target_account.split())
    clean_reason = " ".join(reason.split()) if isinstance(reason, str) and reason.strip() else "in_turn_deferred_reload"
    return (
        f"{ts} [watch] Deferred safe-turn reload for '{clean_target}' "
        f"with force-restart language_server (reason: {clean_reason})\n"
    )


def is_watch_process_cmdline(proc_dir: Path, pid: int, watch_script: Path | None) -> bool:
    """Validates /proc/<pid>/cmdline if accessible to prevent signaling arbitrary processes."""
    if not proc_dir.is_dir():
        return True
    cmdline_file = proc_dir / str(pid) / "cmdline"
    if not cmdline_file.is_file():
        return False
    try:
        raw = cmdline_file.read_bytes()
    except OSError:
        return False
    raw_args = [a for a in raw.split(b"\x00") if a] if raw else []
    if not raw_args:
        return False
    args = [a.decode("utf-8", errors="replace").strip() for a in raw_args]
    argv0 = os.path.basename(args[0]).lower()
    is_python = argv0.startswith(("python", "pypy"))
    is_agyswitch = "agy-switch" in argv0 or "agy_switch" in argv0
    if not (is_python or is_agyswitch):
        return False
    script_name = watch_script.name.lower() if watch_script else ""
    if is_python:
        target_script = None
        skip_next = False
        for arg in args[1:]:
            if skip_next:
                skip_next = False
            elif arg == "-m":
                target_script = "-m"
                break
            elif arg == "-c":
                return False
            elif arg.startswith("-"):
                if arg in ("-w", "-x"):
                    skip_next = True
                continue
            else:
                target_script = os.path.basename(arg).lower()
                break
        if not target_script:
            return False
        full_cmd = " ".join(args).lower()
        if target_script == "-m":
            return "watch" in full_cmd and "run" in full_cmd
        matches_script = (
            (bool(script_name) and target_script == script_name)
            or target_script in ("watch.py", "watcher.py", "agy_switch.py")
            or "agy-switch" in target_script
            or "agy_switch" in target_script
        )
        return matches_script and (
            "run" in full_cmd
            or "watch" in full_cmd
            or bool(script_name and script_name in full_cmd)
        )
    args_lower = [a.lower() for a in args]
    return "watch" in args_lower or "run" in args_lower


def extract_5h_rolling_reset(
    target_dict: dict[str, Any],
    now: float,
    parse_ts_fn: Callable[[Any], float],
) -> float | None:
    """Extracts valid 5-hour rolling bucket reset timestamp, rejecting weekly timestamps > 5 hours."""
    max_rolling_window = 18000.0
    max_daily_window = 86400.0
    raw_buckets = target_dict.get("buckets")

    def _is_weekly(identifier: str) -> bool:
        return any(x in identifier for x in ("weekly", "week", "7d", "wk"))

    def _is_5h_rolling(identifier: str) -> bool:
        return any(x in identifier for x in ("5h", "five_hour", "five_hours", "5_hour", "5-hour", "rolling_5h"))

    bucket_items: list[tuple[str, dict[str, Any]]] = []
    if isinstance(raw_buckets, dict):
        bucket_items = [(str(k).strip().lower(), b) for k, b in raw_buckets.items() if isinstance(b, dict)]
    elif isinstance(raw_buckets, list):
        bucket_items = [(str(b.get("bucketId", "")).strip().lower(), b) for b in raw_buckets if isinstance(b, dict)]
    if bucket_items:
        for ident, b in bucket_items:
            if _is_5h_rolling(ident):
                rt_val = parse_ts_fn(b.get("reset_time", b.get("resetTime")))
                if math.isfinite(rt_val) and rt_val - now > 0:
                    return min(rt_val, now + max_rolling_window)
        if all(_is_weekly(ident) for ident, _ in bucket_items):
            return None
        for ident, b in bucket_items:
            if not _is_weekly(ident):
                rt_val = parse_ts_fn(b.get("reset_time", b.get("resetTime")))
                if math.isfinite(rt_val) and 0 < rt_val - now <= max_daily_window:
                    return min(rt_val, now + max_rolling_window)
        if any(_is_weekly(ident) for ident, _ in bucket_items):
            return None
    if (top_rt := target_dict.get("reset_time", target_dict.get("resetTime"))) is not None:
        rt_val = parse_ts_fn(top_rt)
        if math.isfinite(rt_val) and 0 < rt_val - now <= max_daily_window:
            return min(rt_val, now + max_rolling_window)
    return None


__all__ = [
    "_resolve_vault_log_path",
    "_resolve_log_file_target",
    "_open_secure_log_file",
    "append_secure_log_entry",
    "_write_atomic_file_secure",
    "parse_pending_reload_payload",
    "read_json_dict_nofollow",
    "extract_5h_rolling_reset",
    "_EXHAUSTION_REGEX",
    "_is_scalar_code",
    "_extract_text_fragments",
    "read_latest_jsonl_record",
    "safe_stderr_write",
    "write_watch_log_line",
    "format_audit_log_line",
    "format_deferred_reload_audit_line",
    "is_watch_process_cmdline",
    "PresenceDetector",
    "DEFAULT_GEMINI_DIR",
]
