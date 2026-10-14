import argparse
import glob
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any

# Ensure script and repo directories are in sys.path
_script_dir = os.path.dirname(os.path.abspath(__file__))
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)
_parent_dir = os.path.dirname(_script_dir)
if _parent_dir not in sys.path:
    sys.path.insert(0, _parent_dir)

try:
    from agy_switch import (
        AgySwitchEngine,
        RotationEngine,
        QuotaClient,
        AgySwitchError,
    )
except ImportError:
    try:
        from scripts.agy_switch import (
            AgySwitchEngine,
            RotationEngine,
            QuotaClient,
            AgySwitchError,
        )
    except ImportError:
        class AgySwitchError(Exception):
            """Base exception fallback."""


DEFAULT_VAULT_DIR = Path.home() / ".antigravity-vault"
DEFAULT_GEMINI_DIR = Path.home() / ".gemini"


class DaemonError(AgySwitchError, RuntimeError):
    """Base exception for watcher daemon lifecycle operations."""


class DaemonAlreadyRunningError(DaemonError):
    """Raised when daemon start() is attempted while an active daemon is already running."""

    def __init__(self, pid: int, message: str | None = None) -> None:
        self.pid = pid
        msg = message or f"Watcher daemon is already running with PID {pid}."
        super().__init__(msg)


class PresenceDetector:
    """Discovers active Antigravity Desktop conversation session without historical session scans."""

    def __init__(
        self,
        gemini_dir: Path | str | None = None,
        proc_dir: Path | str | None = None,
    ):
        self.gemini_dir = Path(gemini_dir).expanduser() if gemini_dir else Path.home() / ".gemini"
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


class CooldownManager:
    """
    Manages account cooldown records with atomic file writes, mode 0600 permissions,
    auto-pruning of expired records, dynamic quota reset alignment, and legacy schema compatibility.
    """

    def __init__(self, cache_dir: Path | str, default_cooldown_seconds: int = 3600):
        self.cache_dir = Path(cache_dir).expanduser()
        self.cooldown_file = self.cache_dir / "cooldown.json"
        self.default_cooldown_seconds = default_cooldown_seconds

    @staticmethod
    def _parse_timestamp(val: Any) -> float:
        """Safely parses timestamp from float, int, or ISO-8601 string. Returns float('inf') on failure."""
        if val is None or isinstance(val, bool):
            return float("inf")
        if isinstance(val, (int, float)):
            return float(val) if math.isfinite(val) and val > 0 else float("inf")
        if isinstance(val, str):
            cleaned = val.strip()
            try:
                num = float(cleaned)
                if math.isfinite(num) and num > 0:
                    return num
            except ValueError:
                pass
            try:
                iso_str = cleaned.replace("Z", "+00:00")
                dt = datetime.fromisoformat(iso_str)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                ts = dt.timestamp()
                return ts if math.isfinite(ts) and ts > 0 else float("inf")
            except Exception:
                pass
        return float("inf")

    def _cooldown_file_exists(self) -> bool:
        if not hasattr(self.cooldown_file, "is_file"):
            return False
        try:
            return self.cooldown_file.is_file()
        except OSError:
            return False

    def _read_raw(self) -> tuple[dict | None, bool]:
        """
        Reads raw JSON content from cooldown.json.
        Returns:
            (records_dict, is_corrupt)
            - If file does not exist: ({}, False)
            - If valid JSON dict: (data, False)
            - If JSON is corrupted or root is not a dict: ({}, True) -> permits atomic self-healing
            - If transient OSError occurs: (None, False) -> prevents wiping disk file on transient I/O glitch
        """
        if not self._cooldown_file_exists():
            return {}, False

        try:
            text = self.cooldown_file.read_text(encoding="utf-8")
            if not text.strip():
                return {}, False
            data = json.loads(text)
            if not isinstance(data, dict):
                return {}, True
            return data, False
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}, True
        except OSError:
            return None, False
        except Exception:
            return None, False

    def _atomic_write(self, records: dict) -> None:
        """
        Atomically writes dictionary records to cooldown.json with strictly enforced 0600 mode.
        Ensures temporary files reside in the same directory and are replaced atomically.
        Cleans up temporary files in case of error.
        """
        cache_path = Path(self.cache_dir)
        cache_path.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(cache_path, 0o700)
        except OSError:
            pass

        temp_path = cache_path / f".cooldown.json.tmp.{os.getpid()}.{time.time_ns()}"
        payload = json.dumps(records, indent=2).encode("utf-8")
        fd = None
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            fd = os.open(temp_path, flags, 0o600)
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                pass

            with open(fd, "wb", closefd=True) as f:
                fd = None
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())

            os.replace(temp_path, self.cooldown_file)
            try:
                os.chmod(self.cooldown_file, 0o600)
            except OSError:
                pass
        except Exception:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
            raise

    def _parse_and_prune(self, raw_data: dict, now: float) -> tuple[dict[str, dict], bool]:
        """
        Normalizes structured records and legacy float records.
        Filters out expired records (cooldown_until <= now).
        Returns (active_records, modified_flag).
        """
        active_records: dict[str, dict] = {}
        modified = False

        if not isinstance(raw_data, dict):
            return {}, True

        for account_name, val in raw_data.items():
            if not isinstance(account_name, str) or not account_name.strip():
                modified = True
                continue

            cleaned_name = account_name.strip()
            if cleaned_name != account_name:
                modified = True

            # Legacy flat float/int timestamp
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                if not math.isfinite(val):
                    modified = True
                    continue
                exhausted_at = float(val)
                cooldown_until = exhausted_at + self.default_cooldown_seconds
                if cooldown_until > now:
                    active_records[cleaned_name] = {
                        "cooldown_until": cooldown_until,
                        "exhausted_at": exhausted_at,
                        "reason": "RESOURCE_EXHAUSTED",
                    }
                    # Legacy record was converted to rich format: mark modified so disk gets normalized
                    modified = True
                else:
                    modified = True
            elif isinstance(val, dict):
                needs_repair = False
                try:
                    exhausted_val = val.get("exhausted_at")
                    if exhausted_val is not None:
                        exhausted_at = float(exhausted_val)
                        if not math.isfinite(exhausted_at):
                            modified = True
                            continue
                    else:
                        exhausted_at = now
                        needs_repair = True

                    cooldown_val = val.get("cooldown_until")
                    if cooldown_val is not None:
                        cooldown_until = float(cooldown_val)
                        if not math.isfinite(cooldown_until):
                            modified = True
                            continue
                    else:
                        cooldown_until = exhausted_at + self.default_cooldown_seconds
                        needs_repair = True

                    reason_val = val.get("reason")
                    if reason_val is not None and isinstance(reason_val, str) and reason_val.strip():
                        reason = reason_val.strip()
                    else:
                        reason = "RESOURCE_EXHAUSTED"
                        needs_repair = True

                    if cooldown_until > now:
                        active_records[cleaned_name] = {
                            "cooldown_until": cooldown_until,
                            "exhausted_at": exhausted_at,
                            "reason": reason,
                        }
                        if needs_repair:
                            modified = True
                    else:
                        # Expired: pruned
                        modified = True
                except (ValueError, TypeError):
                    modified = True
            else:
                modified = True

        return active_records, modified

    def get_cooldowns(self) -> dict[str, dict]:
        """
        Loads cooldown records from cooldown.json.
        Returns dict: {account_name: {"cooldown_until": float, "exhausted_at": float, "reason": str}}
        Supports backward compatibility with legacy flat float values {account: timestamp}.
        Prunes expired records automatically from memory and file.
        """
        if not self._cooldown_file_exists():
            return {}

        raw_data, is_corrupt = self._read_raw()
        if raw_data is None:
            # Transient I/O error reading file: return empty dict in memory,
            # but NEVER overwrite or wipe cooldown.json on disk!
            return {}

        if is_corrupt:
            # File is explicitly corrupt JSON: heal file by resetting to empty dict atomically
            try:
                self._atomic_write({})
            except OSError:
                pass
            return {}

        now = time.time()
        active_records, modified = self._parse_and_prune(raw_data, now)
        if modified:
            try:
                self._atomic_write(active_records)
            except OSError:
                pass
        return active_records

    def is_in_cooldown(self, account_name: str) -> bool:
        """
        Returns True if account_name is currently in active cooldown (cooldown_until > now),
        False if expired or not recorded. Automatically prunes expired records.
        """
        if not account_name or not isinstance(account_name, str) or not account_name.strip():
            return False
        active_cooldowns = self.get_cooldowns()
        return account_name.strip() in active_cooldowns

    def set_cooldown(
        self,
        account_name: str,
        reset_time: float | int | str | None = None,
        reason: str = "RESOURCE_EXHAUSTED",
    ) -> dict:
        """
        Sets cooldown for an account and atomically writes to cooldown.json with mode 0600.
        Uses reset_time if provided and in the future (reset_time > now);
        otherwise defaults to now + default_cooldown_seconds.
        Automatically prunes expired records during update.
        """
        if not account_name or not isinstance(account_name, str) or not account_name.strip():
            raise ValueError("account_name is required and must be a non-empty string")

        account_name = account_name.strip()
        now = time.time()
        valid_reset = False
        if reset_time is not None and not isinstance(reset_time, bool):
            rt_val = self._parse_timestamp(reset_time)
            if math.isfinite(rt_val) and rt_val > now:
                cooldown_until = rt_val
                valid_reset = True

        if not valid_reset:
            cooldown_until = now + self.default_cooldown_seconds

        record = {
            "cooldown_until": cooldown_until,
            "exhausted_at": now,
            "reason": str(reason) if reason else "RESOURCE_EXHAUSTED",
        }

        raw_data, _ = self._read_raw() if self._cooldown_file_exists() else ({}, False)
        if raw_data is None:
            raw_data = {}

        active_records, _ = self._parse_and_prune(raw_data, now)
        active_records[account_name] = record

        self._atomic_write(active_records)
        return record

    def clear_cooldown(self, account_name: str | None = None) -> bool:
        """
        Clears cooldown for a specific account or all accounts if account_name is None.
        Returns True if records were modified/cleared, False if no change.
        """
        if not self._cooldown_file_exists():
            return False

        raw_data, _ = self._read_raw()
        if raw_data is None or not isinstance(raw_data, dict):
            return False

        now = time.time()
        active_records, _ = self._parse_and_prune(raw_data, now)

        if account_name is None:
            if not active_records:
                return False
            self._atomic_write({})
            return True
        else:
            clean_name = account_name.strip() if isinstance(account_name, str) else account_name
            if clean_name in active_records:
                del active_records[clean_name]
                self._atomic_write(active_records)
                return True
            return False

    def get_reset_time_from_cache(self, account_name: str, model_hint: str | None = None) -> float | None:
        """
        Helper to read quota reset_time for account_name from last_known_quota.json if present.
        If model_hint is provided:
            Evaluates ONLY the specified model's quota bucket. If reset_time <= now or unavailable,
            returns None (strictly preventing cross-model leakage per ADR 0001).
        If model_hint is None:
            Checks ("gemini", "claude") and returns the earliest future reset time.
        """
        if not isinstance(self.cache_dir, (str, Path, os.PathLike)):
            return None

        quota_file = Path(self.cache_dir) / "last_known_quota.json"
        if not quota_file.is_file():
            return None

        try:
            data = json.loads(quota_file.read_text(encoding="utf-8"))
        except Exception:
            return None

        accounts = data.get("accounts", data) if isinstance(data, dict) else {}
        if not isinstance(accounts, dict):
            return None

        clean_name = account_name.strip() if isinstance(account_name, str) else account_name
        acc_data = accounts.get(clean_name)
        if not isinstance(acc_data, dict):
            return None

        now = time.time()
        has_nested_models = any(isinstance(acc_data.get(k), dict) for k in ("gemini", "claude"))

        if not has_nested_models:
            # Flat cache schema support: e.g. {"quota": 0, "reset_time": 1791201600}
            if "reset_time" in acc_data:
                rt_val = self._parse_timestamp(acc_data["reset_time"])
                if math.isfinite(rt_val) and rt_val > now:
                    return rt_val
            return None

        if model_hint:
            model_key = model_hint.lower()
            m_data = acc_data.get(model_key)
            if isinstance(m_data, dict) and "reset_time" in m_data:
                rt_val = self._parse_timestamp(m_data["reset_time"])
                if math.isfinite(rt_val) and rt_val > now:
                    return rt_val
            # STRICT CONTRACT: If model_hint is specified, NEVER cross-contaminate with other models
            return None

        candidates: list[float] = []
        for m_key in ("gemini", "claude"):
            m_data = acc_data.get(m_key)
            if isinstance(m_data, dict) and "reset_time" in m_data:
                rt_val = self._parse_timestamp(m_data["reset_time"])
                if math.isfinite(rt_val) and rt_val > now:
                    candidates.append(rt_val)

        if candidates:
            return min(candidates)
        return None


class LogWatcher:
    def __init__(
        self,
        engine: AgySwitchEngine,
        cooldown_manager: CooldownManager | None = None,
        damping_seconds: float | list[str] = 15.0,
        log_dirs: list[str] | None = None,
        cooldown_minutes: int = 60,
        presence_detector: PresenceDetector | None = None,
    ):
        self.engine = engine
        if isinstance(damping_seconds, list):
            log_dirs = damping_seconds
            damping_seconds = 15.0
        self.damping_seconds = float(damping_seconds)
        self.last_switch_time: float = 0.0
        self.file_cursors: dict[str, int] = {}
        self._stagnant_sizes: dict[str, int] = {}
        self.active_session_id: str | None = None
        self.active_transcript_path: Path | None = None

        if presence_detector is not None:
            self.presence_detector = presence_detector
        else:
            gemini_dir = getattr(self.engine, "gemini_dir", None)
            self.presence_detector = PresenceDetector(gemini_dir=gemini_dir)

        if log_dirs is not None:
            self.log_dirs = log_dirs
        else:
            self.log_dirs = [
                str(self.presence_detector.cli_log_dir),
            ]
        self.cooldown_minutes = cooldown_minutes

        if cooldown_manager is not None:
            self.cooldown_manager = cooldown_manager
        else:
            cache_dir = getattr(self.engine, "cache_dir", None)
            if not isinstance(cache_dir, (str, Path)):
                vault_dir = getattr(self.engine, "vault_dir", None)
                if isinstance(vault_dir, (str, Path)):
                    cache_dir = Path(vault_dir) / "cache"
                else:
                    cache_dir = Path.home() / ".antigravity-vault" / "cache"
            self.cooldown_manager = CooldownManager(
                cache_dir=cache_dir,
                default_cooldown_seconds=cooldown_minutes * 60,
            )
        self.cooldown_file = getattr(
            self.cooldown_manager,
            "cooldown_file",
            getattr(self.engine, "cache_dir", Path.home()) / "cooldown.json"
        )

        self.exhaustion_regex = re.compile(
            r"RESOURCE_EXHAUSTED|\bcode\s*429\b|429\s+Too\s+Many\s+Requests|Individual quota reached|weekly quota reached",
            re.IGNORECASE
        )

    def is_exhausted_message(self, line: str) -> bool:
        if not line or not isinstance(line, str):
            return False
        return bool(self.exhaustion_regex.search(line))

    def infer_target_model(self, line: str = "", active_account: str | None = None, cache: dict | None = None) -> str:
        line_lower = line.lower() if line else ""
        if "claude" in line_lower or "anthropic" in line_lower:
            return "claude"
        if "gemini" in line_lower:
            return "gemini"

        # Check active account quota state if available
        if active_account and cache:
            accounts_cache = cache.get("accounts", cache) if isinstance(cache.get("accounts"), dict) else cache
            acc_quota = accounts_cache.get(active_account)
            if isinstance(acc_quota, dict):
                rot = RotationEngine(model_name="gemini", active_account_id=active_account)
                c_status = rot.get_quota_status(acc_quota, model_key="claude")
                g_status = rot.get_quota_status(acc_quota, model_key="gemini")

                c_frac = c_status.fraction if c_status is not None else None
                g_frac = g_status.fraction if g_status is not None else None

                if c_frac is not None and c_frac <= 0.0 and (g_frac is None or g_frac > 0.0):
                    return "claude"
                if g_frac is not None and g_frac <= 0.0 and (c_frac is None or c_frac > 0.0):
                    return "gemini"
                if c_frac is not None and g_frac is not None:
                    if c_frac < g_frac and c_frac <= 0.15:
                        return "claude"
                    if g_frac < c_frac and g_frac <= 0.15:
                        return "gemini"

        return "gemini"

    def process_line(self, line: str, is_jsonl: bool = False) -> bool:
        if not line or not isinstance(line, str):
            return False

        # Anti-thrashing damping window (15s): suppress rotation if within damping window
        now = time.time()
        if self.last_switch_time > 0:
            elapsed = now - self.last_switch_time
            if 0 <= elapsed < self.damping_seconds:
                return False
            elif elapsed < 0:
                self.last_switch_time = 0.0

        if not is_jsonl:
            if self.is_exhausted_message(line):
                print(f"[watch] Detected quota exhaustion: {line.strip()}", file=sys.stderr)
                return self.trigger_switch(line=line)
            return False

        # Structured JSON-First Event Filtering
        line_clean = line.strip()
        if not line_clean.startswith("{"):
            return False

        try:
            data = json.loads(line_clean)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return False

        if not isinstance(data, dict):
            return False

        status = data.get("status")
        msg_type = data.get("type")

        # IMMUNITY RULE: NEVER trigger rotation on DONE status or USER_INPUT type
        if status == "DONE" or msg_type == "USER_INPUT":
            return False

        # ONLY evaluate if status == "ERROR", type == "ERROR_MESSAGE", or non-empty error field
        is_error = (
            status == "ERROR"
            or msg_type == "ERROR_MESSAGE"
            or bool(data.get("error"))
        )
        if not is_error:
            return False

        # Extract text from content, message, and/or error payload
        error_parts: list[str] = []

        error_val = data.get("error")
        if error_val:
            if isinstance(error_val, str):
                error_parts.append(error_val)
            else:
                try:
                    error_parts.append(json.dumps(error_val))
                except Exception:
                    error_parts.append(str(error_val))

        content_val = data.get("content")
        if content_val and (status == "ERROR" or msg_type == "ERROR_MESSAGE"):
            if isinstance(content_val, str):
                error_parts.append(content_val)
            elif isinstance(content_val, list):
                for item in content_val:
                    if isinstance(item, str):
                        error_parts.append(item)
                    elif isinstance(item, dict):
                        if "text" in item:
                            error_parts.append(str(item["text"]))
                        else:
                            try:
                                error_parts.append(json.dumps(item))
                            except Exception:
                                error_parts.append(str(item))
                    else:
                        error_parts.append(str(item))
            elif isinstance(content_val, dict):
                try:
                    error_parts.append(json.dumps(content_val))
                except Exception:
                    error_parts.append(str(content_val))

        message_val = data.get("message")
        if message_val and (status == "ERROR" or msg_type == "ERROR_MESSAGE"):
            if isinstance(message_val, str):
                error_parts.append(message_val)
            else:
                try:
                    error_parts.append(json.dumps(message_val))
                except Exception:
                    error_parts.append(str(message_val))

        combined_text = " ".join(error_parts)
        if self.is_exhausted_message(combined_text):
            print(f"[watch] Detected structured JSON quota exhaustion: {combined_text.strip()[:160]}", file=sys.stderr)
            return self.trigger_switch(line=combined_text)

        return False

    def get_cooldowns(self) -> dict:
        return self.cooldown_manager.get_cooldowns()

    def set_cooldown(self, account_name: str, reset_time: float | None = None, reason: str = "RESOURCE_EXHAUSTED"):
        return self.cooldown_manager.set_cooldown(account_name, reset_time=reset_time, reason=reason)

    def _notify_desktop(self, target_account: str, reason: str = "") -> None:
        """Fires an asynchronous, non-blocking desktop notification via notify-send if installed."""
        notify_bin = shutil.which("notify-send")
        if notify_bin:
            title = "agy-switch"
            msg = f"Auto-rotated to '{target_account}' ({reason})"
            try:
                subprocess.Popen(
                    [notify_bin, title, msg],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass

        # Log event to watch.log
        vault_dir = getattr(self.engine, "vault_dir", None)
        if not isinstance(vault_dir, (str, Path)):
            vault_dir = Path.home() / ".antigravity-vault"
        log_file = Path(vault_dir) / "watch.log"
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                os.chmod(log_file.parent, 0o700)
            except OSError:
                pass

            if log_file.is_symlink():
                try:
                    log_file.unlink()
                except OSError:
                    pass

            open_flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
            log_fd = os.open(
                log_file,
                open_flags,
                0o600,
            )
            try:
                os.fchmod(log_fd, 0o600)
            except OSError:
                pass

            try:
                with open(log_fd, "a", encoding="utf-8", closefd=True) as f:
                    log_fd = None
                    ts = datetime.now(timezone.utc).isoformat()
                    f.write(f"[{ts}] Switched to '{target_account}' ({reason})\n")
                    f.flush()
            finally:
                if log_fd is not None:
                    try:
                        os.close(log_fd)
                    except OSError:
                        pass
        except Exception:
            pass

    def trigger_switch(self, line: str = "", model_hint: str | None = None) -> bool:
        # Check damping window
        now = time.time()
        if self.last_switch_time > 0:
            elapsed = now - self.last_switch_time
            if 0 <= elapsed < self.damping_seconds:
                print(
                    f"[watch] Damping window active ({elapsed:.1f}s < {self.damping_seconds}s). "
                    f"Suppressing rotation.",
                    file=sys.stderr,
                )
                return False
            elif elapsed < 0:
                self.last_switch_time = 0.0

        active_name = self.engine.get_active_name()
        if not active_name:
            print("[watch] No active account found. Cannot switch.", file=sys.stderr)
            return False

        # Read cache to get quotas first
        client = QuotaClient(self.engine)
        cache_data = client.load_cache()
        accounts_cache = cache_data.get("accounts", cache_data) if isinstance(cache_data.get("accounts"), dict) else cache_data

        # Dynamically infer model family FIRST
        resolved_model = model_hint or self.infer_target_model(line=line, active_account=active_name, cache=accounts_cache)

        # Retrieve reset time with resolved model hint to prevent cross-model leakage (ADR 0001)
        reset_time = None
        try:
            reset_time = self.cooldown_manager.get_reset_time_from_cache(active_name, model_hint=resolved_model)
        except Exception:
            pass

        # Parse specific exhaustion reason from line if possible
        reason = "RESOURCE_EXHAUSTED"
        if line:
            line_lower = line.lower()
            if "individual quota reached" in line_lower:
                reason = "Individual quota reached"
            elif "weekly quota reached" in line_lower:
                reason = "weekly quota reached"

        self.set_cooldown(active_name, reset_time=reset_time, reason=reason)
        cooldowns = self.get_cooldowns()

        accounts_data = self.engine.list_accounts()
        # Filter out accounts in cooldown in-memory (O(1) lookup avoiding N disk I/O queries)
        valid_accounts = []
        for acc in accounts_data.get("accounts", []):
            acc_name = acc["name"]
            if acc_name == active_name:
                continue
            if acc_name in cooldowns:
                continue
            if acc["valid"]:
                valid_accounts.append(acc)

        if not valid_accounts:
            print("[watch] All other valid accounts are in cooldown. Cannot switch.", file=sys.stderr)
            return False

        for acc in valid_accounts:
            acc["quota"] = accounts_cache.get(acc["name"], {})

        rot_engine = RotationEngine(model_name=resolved_model, active_account_id=active_name)
        target_account = rot_engine.evaluate(valid_accounts, current_active=active_name, target_model=resolved_model)

        if not target_account or target_account == active_name:
            # Fallback to the first available if rotation engine is strict
            target_account = valid_accounts[0]["name"]

        print(f"[watch] Switching from '{active_name}' to '{target_account}' mid-turn...", file=sys.stderr)
        try:
            self.engine.switch_tokens(target_account, restart_server=False, force=True)
            self.last_switch_time = time.time()
            print(f"[watch] Successfully switched to '{target_account}'.", file=sys.stderr)

            # Fire non-blocking desktop notification
            self._notify_desktop(target_account=target_account, reason=reason)

            # small sleep to avoid rapid re-triggering for the same error
            time.sleep(0.1)
            return True
        except Exception as e:
            print(f"[watch] Error switching accounts: {e}", file=sys.stderr)
            return False

    def scan_active_targets(self) -> list[str]:
        """
        Returns list of file paths to tail:
        1. Current active Desktop transcript (if active session discovered).
        2. CLI logs from ~/.gemini/antigravity-cli/log/*.log.
        Handles dynamic active session switching and cursor reset/cleanup.
        """
        current_session_id = self.presence_detector.get_active_session_id()
        if current_session_id != self.active_session_id:
            # Active session changed: clean up previous transcript cursor
            if self.active_transcript_path:
                prev_path_str = str(self.active_transcript_path)
                if prev_path_str in self.file_cursors:
                    del self.file_cursors[prev_path_str]
                self._stagnant_sizes.pop(prev_path_str, None)
            self.active_session_id = current_session_id
            self.active_transcript_path = self.presence_detector.get_active_transcript_path(session_id=current_session_id)

        # Clean up any stale jsonl transcript cursors not matching active transcript
        active_transcript_str = str(self.active_transcript_path) if self.active_transcript_path else None
        for path_key in list(self.file_cursors.keys()):
            if path_key.endswith(".jsonl") and path_key != active_transcript_str:
                del self.file_cursors[path_key]
                self._stagnant_sizes.pop(path_key, None)

        targets = list(self.presence_detector.get_watch_targets(as_str=True, session_id=current_session_id))

        # Check custom log_dirs if provided
        if getattr(self, "log_dirs", None):
            for log_dir in self.log_dirs:
                if not os.path.isdir(log_dir):
                    continue
                if hasattr(self.presence_detector, "cli_log_dir") and os.path.abspath(log_dir) == os.path.abspath(str(self.presence_detector.cli_log_dir)):
                    continue
                for f in glob.glob(os.path.join(log_dir, "*.log")):
                    if f not in targets:
                        targets.append(f)

        return targets

    def scan_new_files(self) -> list[str]:
        return self.scan_active_targets()

    def poll_once(self) -> int:
        """
        Polls active targets once for new lines and processes them.
        Returns the number of account switches triggered during this polling cycle.
        """
        current_targets = self.scan_active_targets()
        switches_triggered = 0

        # Initialize cursors for newly discovered files (start at end of file)
        for target in current_targets:
            if target not in self.file_cursors:
                try:
                    self.file_cursors[target] = os.path.getsize(target)
                except OSError:
                    self.file_cursors[target] = 0

        # Process lines for all active tracked files
        for target in list(self.file_cursors.keys()):
            if not os.path.isfile(target):
                del self.file_cursors[target]
                self._stagnant_sizes.pop(target, None)
                continue

            try:
                current_size = os.path.getsize(target)
            except OSError:
                continue

            cursor = self.file_cursors[target]
            if current_size < cursor:
                # File was rotated/truncated: reset cursor to beginning
                cursor = 0
                self.file_cursors[target] = 0
                self._stagnant_sizes.pop(target, None)

            # Avoid busy-reading stagnant files where trailing bytes have no newline
            if target in self._stagnant_sizes and current_size <= self._stagnant_sizes[target]:
                continue

            if current_size > cursor:
                try:
                    with open(target, "rb") as f:
                        f.seek(cursor)
                        chunk = f.read()

                    if not chunk:
                        continue

                    last_nl = chunk.rfind(b"\n")
                    if last_nl == -1:
                        # No complete line has arrived; hold cursor and track file size
                        self.file_cursors[target] = cursor
                        self._stagnant_sizes[target] = current_size
                        continue

                    self._stagnant_sizes.pop(target, None)
                    complete_bytes = chunk[:last_nl + 1]
                    raw_lines = complete_bytes.splitlines(keepends=True)

                    is_jsonl = target.endswith(".jsonl") or target.endswith("transcript.jsonl")
                    processed_bytes = 0
                    failover_occurred = False

                    for raw_line in raw_lines:
                        line_bytes_len = len(raw_line)
                        line_text = raw_line.decode("utf-8", errors="replace")
                        if self.process_line(line_text, is_jsonl=is_jsonl):
                            switches_triggered += 1
                            failover_occurred = True
                            processed_bytes += line_bytes_len
                            # Advance cursor precisely to the end of line K to preserve remaining lines
                            self.file_cursors[target] = cursor + processed_bytes
                            break
                        processed_bytes += line_bytes_len

                    if not failover_occurred:
                        self.file_cursors[target] = cursor + (last_nl + 1)

                except OSError:
                    continue

        return switches_triggered

    def watch_loop(self, poll_interval: float = 0.5, max_iterations: int | None = None):
        print("[watch] Daemon started, tailing logs for quota exhaustion...", file=sys.stderr)
        iterations = 0
        while True:
            try:
                self.poll_once()
                iterations += 1
                if max_iterations is not None and iterations >= max_iterations:
                    break
                time.sleep(poll_interval)
            except KeyboardInterrupt:
                print("\n[watch] Stopping daemon.", file=sys.stderr)
                break
            except Exception as e:
                print(f"[watch] Unhandled exception in loop: {e}", file=sys.stderr)
                time.sleep(5)


class DaemonManager:
    """
    Manages the detached background lifecycle of the watch daemon.
    Enforces atomic PID file updates, mode 0600 permissions, process validation via /proc,
    graceful SIGTERM termination with 5s SIGKILL escalation, and stale PID pruning.
    """

    SYSTEMD_TEMPLATE = """[Unit]
Description=Antigravity Account Switcher Log Watcher Daemon
After=network.target

[Service]
Type=simple
ExecStart=%h/.local/bin/agy-switch watch run
Restart=on-failure
RestartSec=5s
StandardOutput=append:%h/.antigravity-vault/watch.log
StandardError=append:%h/.antigravity-vault/watch.log

[Install]
WantedBy=default.target
"""

    def __init__(
        self,
        vault_dir: Path | str | None = None,
        watch_script: Path | str | None = None,
        proc_dir: Path | str | None = None,
        python_bin: str | None = None,
        gemini_dir: Path | str | None = None,
    ) -> None:
        self.vault_dir = Path(vault_dir).expanduser() if vault_dir else DEFAULT_VAULT_DIR
        self.pid_file = self.vault_dir / "watch.pid"
        self.log_file = self.vault_dir / "watch.log"
        self.proc_dir = Path(proc_dir) if proc_dir else Path("/proc")
        self.python_bin = python_bin or sys.executable
        self.watch_script = Path(watch_script).resolve() if watch_script else Path(__file__).resolve()
        self.gemini_dir = Path(gemini_dir).expanduser() if gemini_dir else DEFAULT_GEMINI_DIR

    def _clean_pid_file(self) -> None:
        """Safely unlinks PID file if it exists or is a symlink."""
        try:
            self.pid_file.unlink(missing_ok=True)
        except OSError:
            pass

    def _write_pid_file(self, pid: int) -> None:
        """Atomically writes PID to watch.pid with strictly enforced mode 0600."""
        self.vault_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.vault_dir, 0o700)
        except OSError:
            pass

        temp_path = self.vault_dir / f".watch.pid.tmp.{os.getpid()}.{time.time_ns()}"
        payload = f"{pid}\n".encode("utf-8")
        fd = None
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            fd = os.open(temp_path, flags, 0o600)
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                pass

            with open(fd, "wb", closefd=True) as f:
                fd = None
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())

            os.replace(temp_path, self.pid_file)
            try:
                os.chmod(self.pid_file, 0o600)
            except OSError:
                pass
        except Exception:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
            raise

    def _is_watch_cmdline(self, pid: int) -> bool:
        """Validates /proc/<pid>/cmdline if accessible to prevent killing arbitrary processes."""
        if not self.proc_dir.is_dir():
            return True

        cmdline_file = self.proc_dir / str(pid) / "cmdline"
        if not cmdline_file.is_file():
            return False

        try:
            raw = cmdline_file.read_bytes()
        except OSError:
            return False

        if not raw:
            return False

        raw_args = [a for a in raw.split(b"\x00") if a]
        if not raw_args:
            return False

        args = [a.decode("utf-8", errors="replace").strip() for a in raw_args]
        argv0 = os.path.basename(args[0]).lower()

        is_python = argv0.startswith("python") or argv0.startswith("pypy")
        is_agyswitch = "agy-switch" in argv0 or "agy_switch" in argv0

        if not (is_python or is_agyswitch):
            return False

        script_name = self.watch_script.name.lower() if self.watch_script else ""

        if is_python:
            target_script = None
            skip_next = False
            for arg in args[1:]:
                if skip_next:
                    skip_next = False
                    continue
                if arg == "-m":
                    target_script = "-m"
                    break
                if arg == "-c":
                    return False
                if arg.startswith("-"):
                    if arg in ("-w", "-x"):
                        skip_next = True
                    continue
                target_script = os.path.basename(arg).lower()
                break

            if not target_script:
                return False

            if target_script == "-m":
                full_cmd = " ".join(args).lower()
                return "watch" in full_cmd and "run" in full_cmd

            matches_script = False
            if bool(script_name) and target_script == script_name:
                matches_script = True
            elif target_script in ("watch.py", "agy_switch.py"):
                matches_script = True
            elif "agy-switch" in target_script or "agy_switch" in target_script:
                matches_script = True

            if not matches_script:
                return False

            full_cmd = " ".join(args).lower()
            return "run" in full_cmd or "watch" in full_cmd or bool(script_name and script_name in full_cmd)

        if is_agyswitch:
            args_lower = [a.lower() for a in args]
            return "watch" in args_lower or "run" in args_lower

        return False

    def is_process_alive(self, pid: int) -> bool:
        """Checks if PID exists, belongs to current user, and represents an active watch process."""
        if pid <= 1 or pid == os.getpid():
            return False

        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return False
        except OSError:
            return False

        return self._is_watch_cmdline(pid)

    def get_running_pid(self) -> int | None:
        """
        Reads and returns running daemon PID from watch.pid if active.
        Automatically unlinks dead, stale, or corrupted PID files.
        """
        if self.pid_file.is_symlink():
            self._clean_pid_file()
            return None

        if not self.pid_file.is_file():
            return None

        try:
            content = self.pid_file.read_text(encoding="utf-8").strip()
            if not content:
                self._clean_pid_file()
                return None
            pid = int(content)
            if pid <= 1:
                self._clean_pid_file()
                return None
        except (OSError, ValueError):
            self._clean_pid_file()
            return None

        if self.is_process_alive(pid):
            return pid
        else:
            self._clean_pid_file()
            return None

    def start(self, cooldown: int | None = None) -> int:
        """
        Spawns detached background process with start_new_session=True.
        Writes PID to watch.pid with mode 0600.
        Raises DaemonAlreadyRunningError if daemon is already active.
        """
        existing_pid = self.get_running_pid()
        if existing_pid is not None:
            raise DaemonAlreadyRunningError(existing_pid)

        self.vault_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.vault_dir, 0o700)
        except OSError:
            pass

        if self.log_file.is_symlink():
            try:
                self.log_file.unlink()
            except OSError:
                pass

        open_flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        log_fd = os.open(
            self.log_file,
            open_flags,
            0o600,
        )
        try:
            os.fchmod(log_fd, 0o600)
        except OSError:
            pass

        cmd = [self.python_bin, str(self.watch_script), "run"]
        if cooldown is not None:
            cmd.extend(["--cooldown", str(cooldown)])

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=log_fd,
                stderr=log_fd,
                start_new_session=True,
            )
        finally:
            try:
                os.close(log_fd)
            except OSError:
                pass

        self._last_proc = proc
        pid = proc.pid
        try:
            self._write_pid_file(pid)
        except Exception:
            try:
                proc.terminate()
                proc.wait(timeout=1.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            raise

        return pid

    def stop(self, timeout: float = 5.0) -> bool:
        """
        Gracefully terminates the running daemon with SIGTERM.
        Waits up to timeout seconds (default 5.0s). If still alive, escalates to SIGKILL.
        Cleans up watch.pid. Returns True if a process was terminated, False otherwise.
        """
        if getattr(self, "_last_proc", None) is not None:
            try:
                self._last_proc.poll()
            except Exception:
                pass

        pid = self.get_running_pid()
        if pid is None:
            return False

        try:
            os.kill(pid, signal.SIGTERM)
        except PermissionError as e:
            print(f"Error stopping daemon: Permission denied for PID {pid} ({e})", file=sys.stderr)
            return False
        except ProcessLookupError:
            self._clean_pid_file()
            return False
        except OSError as e:
            print(f"Error stopping daemon: {e}", file=sys.stderr)
            self._clean_pid_file()
            return False

        start_time = time.time()
        poll_interval = 0.05
        terminated = False
        while time.time() - start_time < timeout:
            if not self.is_process_alive(pid):
                terminated = True
                break
            time.sleep(poll_interval)

        if not terminated and self.is_process_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass

            kill_start = time.time()
            while time.time() - kill_start < 1.0:
                if not self.is_process_alive(pid):
                    break
                time.sleep(poll_interval)

        if getattr(self, "_last_proc", None) is not None and getattr(self._last_proc, "pid", None) == pid:
            try:
                self._last_proc.wait(timeout=0.2)
            except Exception:
                pass

        if os.name == "posix":
            try:
                os.waitpid(pid, os.WNOHANG)
            except (ChildProcessError, OSError):
                pass

        if self.is_process_alive(pid):
            return False

        self._clean_pid_file()
        return True

    def status(self) -> dict[str, Any]:
        """
        Returns status dictionary:
        {'running': bool, 'pid': int | None, 'log_file': str, 'targets': list[str]}.
        Automatically cleans up stale PID files.
        """
        pid = self.get_running_pid()
        if pid is not None:
            targets: list[str] = []
            try:
                detector = PresenceDetector(gemini_dir=self.gemini_dir, proc_dir=self.proc_dir)
                targets = list(detector.get_watch_targets(as_str=True))
            except Exception:
                targets = []

            return {
                "running": True,
                "pid": pid,
                "log_file": str(self.log_file),
                "targets": targets,
            }
        else:
            return {
                "running": False,
                "pid": None,
                "log_file": str(self.log_file),
                "targets": [],
            }

    def generate_systemd(self, target_path: Path | str | None = None) -> str:
        """
        Generates user systemd service definition and optionally writes it to target_path.
        """
        if target_path is not None:
            target = Path(target_path).expanduser()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(self.SYSTEMD_TEMPLATE, encoding="utf-8")

        return self.SYSTEMD_TEMPLATE


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="watch.py",
        description="Antigravity Account Switcher Log Watcher & Daemon",
    )
    parser.add_argument(
        "action",
        nargs="?",
        choices=["run", "start", "stop", "status"],
        default="run",
        help="Daemon action: run (default foreground), start (background daemon), stop, status",
    )
    parser.add_argument(
        "--generate-systemd",
        action="store_true",
        help="Output user systemd service definition",
    )
    parser.add_argument(
        "--install-systemd",
        action="store_true",
        help="Install systemd user service unit to ~/.config/systemd/user/agy-switch-watcher.service",
    )
    parser.add_argument(
        "--cooldown",
        type=int,
        default=60,
        help="Default cooldown minutes for exhausted accounts (default: 60)",
    )
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    engine = AgySwitchEngine()
    dm = DaemonManager(vault_dir=engine.vault_dir)

    if args.generate_systemd or args.install_systemd:
        target = (
            Path.home() / ".config" / "systemd" / "user" / "agy-switch-watcher.service"
            if args.install_systemd
            else None
        )
        unit = dm.generate_systemd(target_path=target)
        sys.stdout.write(unit)
        sys.stdout.flush()
        return 0

    if args.action == "start":
        try:
            pid = dm.start(cooldown=args.cooldown)
            print(f"[watch] Daemon started in background (PID: {pid}).")
            print(f"  - Log: {dm.log_file}")
            print(f"  - PID: {dm.pid_file}")
            return 0
        except DaemonAlreadyRunningError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        except Exception as e:
            print(f"Error starting daemon: {e}", file=sys.stderr)
            return 1

    elif args.action == "stop":
        try:
            stopped = dm.stop()
            if stopped:
                print("[watch] Daemon stopped successfully.")
            else:
                print("[watch] Daemon was not running.")
            return 0
        except Exception as e:
            print(f"Error stopping daemon: {e}", file=sys.stderr)
            return 1

    elif args.action == "status":
        st = dm.status()
        if st["running"]:
            print(f"[watch] Daemon is running (PID: {st['pid']}).")
            print(f"  - Log: {st['log_file']}")
            if st.get("targets"):
                print("  - Monitored targets:")
                for t in st["targets"]:
                    print(f"    * {t}")
        else:
            print("[watch] Daemon is stopped.")
            print(f"  - Log: {st['log_file']}")
        return 0

    elif args.action == "run":
        try:
            signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))
        except Exception:
            pass
        watcher = LogWatcher(engine, cooldown_minutes=args.cooldown)
        watcher.watch_loop()
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
