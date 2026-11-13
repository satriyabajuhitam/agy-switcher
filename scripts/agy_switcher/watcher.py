"""Log Watcher & Detached Daemon Engine.

Layer 2 Domain Engine: Depends on Layer 0 (exceptions, models) and Layer 1 (telemetry, supervisor, formatters).
Zero external dependencies. Pure Python 3.10+ standard library.
Strictly acyclic DAG: Does NOT import vault.py, rotation.py, doctor.py, hook.py, or cli.py at the module level.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import glob
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import signal
import ssl
import subprocess
import sys

_real_popen = subprocess.Popen
import threading
import time
from typing import Any, Callable, Mapping, TypeVar, cast
import urllib.error
import urllib.request

from agy_switcher.exceptions import (
    AgySwitchError,
    DaemonError,
    DaemonAlreadyRunningError,
)
from agy_switcher.supervisor import ProcessSupervisor
from agy_switcher.telemetry import (
    QuotaClient as _RealQuotaClient,
    fetch_account_quota as _real_fetch_account_quota,
)

RotationEngine: type[object] | None = None
QuotaClient: type[object] | None = None
fetch_account_quota: Callable[..., tuple[str, Mapping[str, object] | None]] | None = None
AgySwitchEngine: type[object] | None = None

logger = logging.getLogger(__name__)

DEFAULT_VAULT_DIR = Path.home() / ".antigravity-vault"
DEFAULT_MAX_CHUNK_BYTES: int = 4 * 1024 * 1024  # 4 MB read bound
DEFAULT_DAMPING_SECONDS: float = 60.0
SYSTEMD_ACTIVE_ERROR_MSG: str = (
    "Error: agy-switch-watcher.service is actively running via systemd. "
    "Manage the service via systemd ('systemctl --user stop agy-switch-watcher.service') "
    "or run 'agy-switch watch stop' before starting."
)
_EXHAUSTION_REGEX: re.Pattern[str] = re.compile(
    r"resource[_\s-]?exhausted"
    r"|\b(?:http(?:/\d+(?:\.\d+)?)?(?:_?(?:status|code))?|(?:grpc|response|api|http)_?(?:status|error)(?:_?code)?|error(?:_?code)?|code|status(?:_?code)?)\"?\s*(?:[:=]|\()?\s*\"?429\b"
    r"|\b429\b\"?\s*\)?\s*(?:[:\-]?\s*(?:quota\s+(?:exceeded|exhausted|reached)|resource\s*exhausted|rate[- ]?limit|too\s+many\s+requests)\b|:\s*quota\b)"
    r"|\b(?:too\s+many\s+requests|rate[- ]?limit(?:ed|\s+exceeded)?|quota\s+(?:exceeded|exhausted|reached))\s*[:\-]?\s*[\(\[]\s*429\s*[\)\]]"
    r"|Individual quota reached|weekly quota reached", re.IGNORECASE,
)
_ERROR_CODE_KEYS: tuple[str, ...] = ("code", "status_code", "statusCode", "error_code", "errorCode", "http_status", "httpStatus")

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

# Re-exported from presence.py (Layer 2)
from agy_switcher.presence import (
    DEFAULT_GEMINI_DIR,
    PresenceDetector,
    _is_mock_object,
    _is_prod_vault_path,
    _is_test_runner,
    _open_secure_log_file,
    _write_atomic_file_secure,
    append_secure_log_entry,
)

_T = TypeVar("_T")

def _resolve_symbol(symbol_name: str, fallback_factory: Callable[[], _T]) -> _T:
    """Resolves symbol from sys.modules if mocked/present; falls back to sub-package default."""
    for mod_name in ("agy_switcher.watcher", "scripts.watch", "watch", "agy_switch", "scripts.agy_switch"):
        mod = sys.modules.get(mod_name)
        if mod and getattr(mod, symbol_name, None) is not None:
            return cast(_T, getattr(mod, symbol_name))
    return fallback_factory()

def _get_rotation_engine_class() -> Any:
    return _resolve_symbol("RotationEngine", lambda: __import__("agy_switcher.rotation", fromlist=["RotationEngine"]).RotationEngine)
def _get_quota_client_class() -> Any:
    return _resolve_symbol("QuotaClient", lambda: _RealQuotaClient)
def _get_fetch_account_quota() -> Callable[..., tuple[str, Mapping[str, object] | None]] | None:
    return _resolve_symbol("fetch_account_quota", lambda: _real_fetch_account_quota)
def _get_engine_class() -> Any:
    def _fallback() -> Any:
        for m in ("agy_switcher.vault", "agy_switch", "scripts.agy_switch"):
            try:
                return __import__(m, fromlist=["AgySwitchEngine"]).AgySwitchEngine
            except ImportError:
                pass
    return _resolve_symbol("AgySwitchEngine", _fallback)
def _get_shutil() -> Any:
    return _resolve_symbol("shutil", lambda: shutil)
def _get_subprocess() -> Any:
    return _resolve_symbol("subprocess", lambda: subprocess)
def _get_time() -> Any:
    return _resolve_symbol("time", lambda: time)

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
                dt = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
                ts = dt.replace(tzinfo=timezone.utc).timestamp() if dt.tzinfo is None else dt.timestamp()
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

    def _read_raw(self) -> tuple[dict[str, Any] | None, bool]:
        """Reads raw JSON content from cooldown.json returning (records_dict, is_corrupt)."""
        if not self._cooldown_file_exists():
            return {}, False
        try:
            text = self.cooldown_file.read_text(encoding="utf-8")
            if not text.strip():
                return {}, False
            data = json.loads(text)
            return (data, False) if isinstance(data, dict) else ({}, True)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}, True
        except Exception:
            return None, False

    def _atomic_write(self, records: dict[str, Any]) -> None:
        """Atomically writes dictionary records to cooldown.json with strictly enforced 0600 mode."""
        payload = json.dumps(records, indent=2).encode("utf-8")
        _write_atomic_file_secure(self.cooldown_file, payload)

    def _parse_and_prune(self, raw_data: dict[str, Any], now: float) -> tuple[dict[str, dict[str, Any]], bool]:
        """Normalizes records, prunes expired entries, returns (active_records, modified_flag)."""
        active_records: dict[str, dict[str, Any]] = {}
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
                try:
                    e_raw, c_raw = val.get("exhausted_at"), val.get("cooldown_until")
                    needs_repair = (e_raw is None) or (c_raw is None) or not val.get("reason")
                    exhausted_at = now if e_raw is None else float(e_raw)
                    cooldown_until = (exhausted_at + self.default_cooldown_seconds) if c_raw is None else float(c_raw)
                    if not (math.isfinite(exhausted_at) and math.isfinite(cooldown_until)):
                        modified = True
                        continue
                    reason = str(val.get("reason") or "RESOURCE_EXHAUSTED").strip() or "RESOURCE_EXHAUSTED"
                    if cooldown_until > now:
                        active_records[cleaned_name] = {"cooldown_until": cooldown_until, "exhausted_at": exhausted_at, "reason": reason}
                        if needs_repair:
                            modified = True
                    else:
                        modified = True
                except (ValueError, TypeError):
                    modified = True
            else:
                modified = True
        return active_records, modified

    def get_cooldowns(self) -> dict[str, dict[str, Any]]:
        """Loads and normalizes active cooldown records, automatically pruning expired ones."""
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
        """Returns True if account_name is currently in active cooldown."""
        if not account_name or not isinstance(account_name, str) or not account_name.strip():
            return False
        active_cooldowns = self.get_cooldowns()
        return account_name.strip() in active_cooldowns

    def set_cooldown(
        self,
        account_name: str,
        reset_time: float | int | str | None = None,
        reason: str = "RESOURCE_EXHAUSTED",
    ) -> dict[str, Any]:
        """Sets account cooldown and atomically persists to cooldown.json with mode 0600."""
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
            if not raw_data:
                return False
            self._atomic_write({})
            return True
        else:
            clean_name = account_name.strip() if isinstance(account_name, str) else account_name
            if clean_name in raw_data or clean_name in active_records:
                active_records.pop(clean_name, None)
                self._atomic_write(active_records)
                return True
            return False

    def _extract_5h_rolling_reset(self, target_dict: dict[str, Any], now: float) -> float | None:
        """Extracts valid 5-hour rolling bucket reset timestamp, rejecting weekly timestamps > 5 hours.
        Adheres to ADR 0016 / [COOLDOWN-CLAMP-001] / [COOLDOWN-CLAMP-003].
        """
        max_rolling_window = 18000.0  # 5 hours
        max_daily_window = 86400.0    # 24 hours (multi-day boundary)
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
                    rt_val = self._parse_timestamp(b.get("reset_time", b.get("resetTime")))
                    if math.isfinite(rt_val) and rt_val - now > 0:
                        return min(rt_val, now + max_rolling_window)
            if all(_is_weekly(ident) for ident, _ in bucket_items):
                return None
            for ident, b in bucket_items:
                if not _is_weekly(ident):
                    rt_val = self._parse_timestamp(b.get("reset_time", b.get("resetTime")))
                    if math.isfinite(rt_val) and 0 < rt_val - now <= max_daily_window:
                        return min(rt_val, now + max_rolling_window)
            if any(_is_weekly(ident) for ident, _ in bucket_items):
                return None

        if (top_rt := target_dict.get("reset_time", target_dict.get("resetTime"))) is not None:
            rt_val = self._parse_timestamp(top_rt)
            if math.isfinite(rt_val) and 0 < rt_val - now <= max_daily_window:
                return min(rt_val, now + max_rolling_window)

        return None

    def get_reset_time_from_cache(self, account_name: str, model_hint: str | None = None) -> float | None:
        """Reads quota reset_time for account_name from last_known_quota.json, clamping to 5h rolling bucket."""
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
            return self._extract_5h_rolling_reset(acc_data, now)
        if model_hint:
            model_key = model_hint.lower()
            m_data = acc_data.get(model_key)
            if isinstance(m_data, dict):
                return self._extract_5h_rolling_reset(m_data, now)
            # STRICT CONTRACT: If model_hint is specified, NEVER cross-contaminate with other models
            return None
        candidates: list[float] = []
        for m_key in ("gemini", "claude"):
            m_data = acc_data.get(m_key)
            if isinstance(m_data, dict):
                rt_val = self._extract_5h_rolling_reset(m_data, now)
                if rt_val is not None:
                    candidates.append(rt_val)
        if candidates:
            return min(candidates)
        return None

class LogWatcher:
    def __init__(
        self,
        engine: Any,
        cooldown_manager: CooldownManager | None = None,
        damping_seconds: float | list[str] | None = DEFAULT_DAMPING_SECONDS,
        log_dirs: list[str] | None = None,
        cooldown_minutes: int = 60,
        presence_detector: PresenceDetector | None = None,
        max_chunk_bytes: int = DEFAULT_MAX_CHUNK_BYTES,
        supervisor: ProcessSupervisor | None = None,
        daemon_manager: DaemonManager | None = None,
    ):
        self.engine = engine
        if damping_seconds is None:
            damping_seconds = DEFAULT_DAMPING_SECONDS
        elif isinstance(damping_seconds, list):
            log_dirs = damping_seconds
            damping_seconds = DEFAULT_DAMPING_SECONDS
        self.damping_seconds = float(damping_seconds)
        self.last_switch_time: float = 0.0
        self.file_cursors: dict[str, int] = {}
        self._stagnant_sizes: dict[str, int] = {}
        self.active_session_id: str | None = None
        self.active_transcript_path: Path | None = None
        self.max_chunk_bytes = int(max_chunk_bytes)
        self.presence_detector = presence_detector or PresenceDetector(gemini_dir=getattr(self.engine, "gemini_dir", None))
        self.log_dirs = log_dirs if log_dirs is not None else [str(self.presence_detector.cli_log_dir)]
        self.cooldown_minutes = cooldown_minutes
        if cooldown_manager is not None:
            self.cooldown_manager = cooldown_manager
        else:
            cache_dir = getattr(self.engine, "cache_dir", None)
            if not isinstance(cache_dir, (str, Path)) or _is_mock_object(cache_dir):
                vault_dir = getattr(self.engine, "vault_dir", None)
                if isinstance(vault_dir, (str, Path)) and not _is_mock_object(vault_dir):
                    cache_dir = Path(vault_dir) / "cache"
                elif (env_v := os.environ.get("AGY_VAULT_DIR", "").strip()):
                    cache_dir = Path(env_v).expanduser() / "cache"
                else:
                    cache_dir = Path.home() / ".antigravity-vault" / "cache"
            self.cooldown_manager = CooldownManager(cache_dir=cache_dir, default_cooldown_seconds=cooldown_minutes * 60)
        self.cooldown_file = getattr(self.cooldown_manager, "cooldown_file", getattr(self.engine, "cache_dir", Path.home()) / "cooldown.json")
        self.exhaustion_regex = _EXHAUSTION_REGEX
        if supervisor is not None:
            self.supervisor = supervisor
        elif hasattr(self.engine, "supervisor") and self.engine.supervisor is not None:
            self.supervisor = self.engine.supervisor
        else:
            self.supervisor = ProcessSupervisor(
                gemini_dir=getattr(self.engine, "gemini_dir", None),
                proc_dir=getattr(self.presence_detector, "proc_dir", None),
            )
        self._last_transcript_record: dict[str, Any] | None = None
        try:
            sid = self.presence_detector.get_active_session_id()
            if sid:
                self.active_session_id = sid
                self.active_transcript_path = self.presence_detector.get_active_transcript_path(session_id=sid)
        except Exception:
            pass
        if daemon_manager is not None:
            self.daemon_manager = daemon_manager
        else:
            self.daemon_manager = DaemonManager(vault_dir=getattr(self.engine, "vault_dir", None))

    @property
    def _lock_fd(self) -> int | None:
        return self.daemon_manager._lock_fd

    def is_exhausted_message(self, line: str) -> bool:
        return bool(line and isinstance(line, str) and self.exhaustion_regex.search(line))

    def get_quota_threshold(self, default: float = 0.25) -> float:
        """Retrieves configured quota threshold fraction from engine, defaulting safely."""
        if hasattr(self, "engine") and self.engine is not None:
            try:
                thresh_fn = getattr(self.engine, "get_quota_threshold", getattr(self.engine, "quota_threshold", None))
                if thresh_fn is not None:
                    raw_val = thresh_fn() if callable(thresh_fn) else thresh_fn
                    if isinstance(raw_val, (int, float, str)) and not isinstance(raw_val, bool):
                        val = float(raw_val) / 100.0 if float(raw_val) > 1.0 else float(raw_val)
                        if 0.0 < val <= 1.0:
                            return val
            except Exception:
                pass
        return default

    def infer_target_model(self, line: str = "", active_account: str | None = None, cache: dict[str, Any] | None = None) -> str:
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
                rot_cls = _get_rotation_engine_class()
                rot = rot_cls(model_name="gemini", active_account_id=active_account)
                c_status = rot.get_quota_status(acc_quota, model_key="claude")
                g_status = rot.get_quota_status(acc_quota, model_key="gemini")
                c_frac = float(c_status.fraction) if (c_status is not None and hasattr(c_status, "fraction") and isinstance(c_status.fraction, (int, float)) and not isinstance(c_status.fraction, bool)) else None
                g_frac = float(g_status.fraction) if (g_status is not None and hasattr(g_status, "fraction") and isinstance(g_status.fraction, (int, float)) and not isinstance(g_status.fraction, bool)) else None
                if c_frac is not None and c_frac <= 0.0 and (g_frac is None or g_frac > 0.0):
                    return "claude"
                if g_frac is not None and g_frac <= 0.0 and (c_frac is None or c_frac > 0.0):
                    return "gemini"
                if c_frac is not None and g_frac is not None and c_frac < g_frac and c_frac <= 0.25:
                    return "claude"
                if g_frac is not None and c_frac is not None and g_frac < c_frac and g_frac <= 0.25:
                    return "gemini"
        return "gemini"

    @staticmethod
    def _is_switch_failed(res: object, target_account: str) -> bool:
        """Evaluates whether a switch_tokens() return payload indicates failure."""
        if not res:
            return True
        if isinstance(res, dict):
            return (
                res.get("success") is False
                or bool(res.get("error"))
                or (not res.get("active") and not res.get("success"))
                or (bool(res.get("active")) and res.get("active") != target_account and not res.get("success"))
            )
        return False

    def _is_damping_active(self, now: float | None = None, *, log_suppression: bool = False) -> bool:
        """Evaluates the unified damping window against self.last_switch_time."""
        if self.last_switch_time <= 0:
            return False
        current_time = time.time() if now is None else now
        elapsed = current_time - self.last_switch_time
        if 0 <= elapsed < self.damping_seconds:
            if log_suppression:
                print(
                    f"[watch] Damping window active ({elapsed:.1f}s < {self.damping_seconds}s). "
                    f"Suppressing rotation.",
                    file=sys.stderr,
                )
            return True
        if elapsed < 0:
            self.last_switch_time = 0.0
        return False

    def process_line(self, line: str, is_jsonl: bool = False, file_path: str | None = None) -> bool:
        if not line or not isinstance(line, str):
            return False
        # Anti-thrashing damping window: suppress rotation if within damping window
        if self._is_damping_active():
            return False
        # Structured JSON-First Event Filtering
        line_clean = line.strip()
        if not line_clean.startswith("{"):
            return False
        try:
            data: object = json.loads(line_clean)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return False
        if not isinstance(data, dict):
            return False
        data_dict = cast(dict[str, Any], data)
        if is_jsonl:
            self._last_transcript_record = data_dict
        status: object = data.get("status")
        msg_type: object = data.get("type")
        norm_status = status.strip().upper() if isinstance(status, str) else ""
        norm_type = msg_type.strip().upper() if isinstance(msg_type, str) else ""
        if norm_status == "DONE" or norm_type == "USER_INPUT":
            return False
        error_val: object = data.get("error")
        has_error_field = bool(error_val) and not (isinstance(error_val, str) and not error_val.strip())
        is_error_envelope = norm_status in ("ERROR", "ERROR_MESSAGE") or norm_type in ("ERROR", "ERROR_MESSAGE")
        if not (is_error_envelope or has_error_field):
            return False
        error_parts: list[str] = []
        if has_error_field:
            if _is_scalar_code(error_val):
                error_parts.append(f"error: {str(error_val).strip()}")
            else:
                error_parts.extend(_extract_text_fragments(error_val))
        for code_key in _ERROR_CODE_KEYS:
            code_val: object = data.get(code_key)
            if _is_scalar_code(code_val):
                error_parts.append(f"{code_key}: {str(code_val).strip()}")
        if is_error_envelope:
            for field in ("content", "message", "details", "detail", "reason"):
                field_val: object = data.get(field)
                if field_val:
                    error_parts.extend(_extract_text_fragments(field_val))
        combined_text = " ".join(error_parts).encode("utf-8", errors="replace").decode("utf-8")
        if self.is_exhausted_message(combined_text):
            print(f"[watch] Detected structured JSON quota exhaustion: {combined_text.strip()[:160]}", file=sys.stderr)
            return self._dispatch_exhaustion(line=combined_text, file_path=file_path)
        return False

    def _dispatch_exhaustion(
        self,
        line: str = "",
        model_hint: str | None = None,
        file_path: str | None = None,
    ) -> bool:
        """Dispatches exhaustion handling, preserving overrides/mocks on trigger_switch."""
        if getattr(self.trigger_switch, "__func__", None) is not LogWatcher.trigger_switch:
            try:
                return self.trigger_switch(line=line, model_hint=model_hint, file_path=file_path)
            except TypeError:
                return self.trigger_switch(line=line)
        return self.handle_exhaustion(line=line, model_hint=model_hint, file_path=file_path)

    def get_cooldowns(self) -> dict[str, dict[str, Any]]:
        if hasattr(self, "cooldown_manager") and self.cooldown_manager is not None:
            try:
                return self.cooldown_manager.get_cooldowns()
            except Exception:
                return {}
        return {}

    def is_in_cooldown(self, account_name: str) -> bool:
        return self.cooldown_manager.is_in_cooldown(account_name)

    def set_cooldown(
        self,
        account_name: str,
        reset_time: float | int | str | None = None,
        reason: str = "RESOURCE_EXHAUSTED",
        model_hint: str | None = None,
    ) -> dict[str, object]:
        """Sets cooldown for an account, clamping duration strictly to 5h rolling bucket or 30m fallback.
        Explicitly rejects multi-day weekly reset timestamps (ADR 0016).
        """
        now = time.time()
        max_cd, fallback_cd, max_daily = 18000.0, 1800.0, 86400.0

        def _get_cached() -> float:
            try:
                crt = self.cooldown_manager.get_reset_time_from_cache(account_name, model_hint=model_hint)
                if crt is not None and math.isfinite(crt) and crt > now:
                    return min(crt, now + max_cd)
            except Exception:
                pass
            return now + fallback_cd

        if reset_time is not None and not isinstance(reset_time, bool) and math.isfinite(parsed_rt := self.cooldown_manager._parse_timestamp(reset_time)):
            reset_delta = parsed_rt - now
            is_weekly = "weekly" in str(reason).lower() or reset_delta > max_daily
            effective_reset = (now + fallback_cd) if reset_delta <= 0 else (_get_cached() if is_weekly else min(parsed_rt, now + max_cd))
        else:
            effective_reset = _get_cached()

        return self.cooldown_manager.set_cooldown(
            account_name, reset_time=effective_reset, reason=reason
        )

    def _resolve_cache_dir(self) -> Path | None:
        for obj, attr in ((getattr(self, "cooldown_manager", None), "cache_dir"), (self.engine, "cache_dir")):
            if isinstance(d := getattr(obj, attr, None), (str, Path)):
                return Path(d)
        return Path(vd) / "cache" if isinstance(vd := getattr(self.engine, "vault_dir", None), (str, Path)) else None

    def _load_cached_quota_data(self) -> tuple[dict[str, Any], Any]:
        """Loads cached quota dictionary and client instance if available."""
        cache_data: dict[str, Any] = {}
        client = None
        try:
            client = _get_quota_client_class()(self.engine)
            if isinstance(loaded := client.load_cache(), dict):
                cache_data = loaded
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            logger.warning("[watch] Quota cache load network error: %s", e)
        except Exception as e:
            logger.debug("[watch] Quota cache load failure: %s", e)
        if not cache_data and (c_dir := self._resolve_cache_dir()):
            try:
                if (q_path := c_dir / "last_known_quota.json").is_file():
                    cache_data = json.loads(q_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return cache_data, client

    def _advance_cursors_to_eof(self) -> None:
        """Advances active file cursors to EOF and clears stagnant size tracking."""
        for target_file in list(self.file_cursors.keys()):
            try:
                if os.path.isfile(target_file):
                    self.file_cursors[target_file] = os.path.getsize(target_file)
                else:
                    self.file_cursors.pop(target_file, None)
            except (OSError, ValueError, TypeError):
                self.file_cursors.pop(target_file, None)
        self._stagnant_sizes.clear()

    def _is_account_cooldown_penalized(self, acc_name: str, cooldowns: dict[str, Any], accounts_cache: dict[str, Any] | None = None) -> bool:
        """Determines if an account in cooldown is penalized or granted immunity (ADR 0016 / COOLDOWN-IMMUNITY)."""
        if not isinstance(cooldowns, dict) or not acc_name:
            return False
        clean_name = acc_name.strip() if isinstance(acc_name, str) else ""
        if not clean_name or clean_name not in cooldowns:
            return False

        now = time.time()
        rec = cooldowns.get(clean_name)
        cd_until = rec.get("cooldown_until") if isinstance(rec, dict) else rec
        if isinstance(cd_until, (int, float)) and cd_until <= now:
            return False

        # Account is in active cooldown. Probe for Telemetry-Aware Cooldown Immunity (>= 70%)
        quota_frac: float | None = None
        if isinstance(accounts_cache, Mapping):
            acc_map = accounts_cache.get("accounts", accounts_cache) if isinstance(accounts_cache.get("accounts"), Mapping) else accounts_cache
            quota_frac = self._probe_active_quota(clean_name, acc_map, model_name="gemini")

        if quota_frac is None and (c_dir := self._resolve_cache_dir()) and (q_file := c_dir / "last_known_quota.json").is_file():
            try:
                raw = json.loads(q_file.read_text(encoding="utf-8"))
                acc_map = raw.get("accounts", raw) if isinstance(raw, dict) and isinstance(raw.get("accounts"), Mapping) else raw
                quota_frac = self._probe_active_quota(clean_name, acc_map, model_name="gemini") if isinstance(acc_map, Mapping) else None
            except Exception:
                pass

        if quota_frac is None and hasattr(self, "engine") and (fetch_acc_fn := _get_fetch_account_quota()):
            try:
                _, live_data = fetch_acc_fn(self.engine, clean_name)
                if isinstance(live_data, Mapping) and not live_data.get("error"):
                    quota_frac = self._probe_active_quota(clean_name, {clean_name: live_data}, model_name="gemini")
            except Exception:
                pass

        return True if quota_frac is None else (round(quota_frac, 6) < 0.70)

    def _notify_desktop(self, target_account: str, old_account: str | None = None, reason: str = "", reloaded: bool = False) -> None:
        """Fires an asynchronous desktop notification via notify-send and appends audit log."""
        if reloaded:
            _get_time().sleep(1.0)
        clean_reason = ""
        if reason and isinstance(reason, str):
            r = reason.strip()
            while r.startswith("(") and r.endswith(")") and len(r) >= 2:
                r = r[1:-1].strip()
            clean_reason = r
        shutil_mod = _get_shutil()
        notify_bin = shutil_mod.which("notify-send")
        if notify_bin:
            try:
                msg = f"🔄 Antigravity switched to {target_account}" + (" & reloaded language_server" if reloaded else "")
                if clean_reason:
                    msg += f" ({clean_reason})"
                _get_subprocess().Popen([notify_bin, "agy-switch", msg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
        self._append_audit_log(target_account=target_account, old_account=old_account, reason=reason, reloaded=reloaded, clean_reason=clean_reason)

    def _resolve_audit_log_dir(self) -> Path | None:
        """Dynamically resolves isolated vault directory for audit logging without caching."""
        if (env_vault := os.environ.get("AGY_VAULT_DIR")) and env_vault.strip():
            candidate = Path(env_vault.strip()).expanduser()
            return None if _is_test_runner() and _is_prod_vault_path(candidate) else candidate
        if (engine := getattr(self, "engine", None)) is not None:
            v = getattr(engine, "vault_dir", None)
            if v is not None and not _is_mock_object(v) and isinstance(v, (str, Path)) and str(v).strip():
                candidate = Path(str(v).strip()).expanduser()
                return None if _is_test_runner() and _is_prod_vault_path(candidate) else candidate
        return None if _is_test_runner() else DEFAULT_VAULT_DIR

    def _append_audit_log(
        self,
        target_account: str,
        old_account: str | None = None,
        reason: str = "",
        reloaded: bool = False,
        clean_reason: str = "",
    ) -> None:
        """Appends structured audit entry to watch.log with mode 0600."""
        try:
            if not clean_reason and reason and isinstance(reason, str):
                r = reason.strip()
                while r.startswith("(") and r.endswith(")") and len(r) >= 2:
                    r = r[1:-1].strip()
                clean_reason = r
            if (log_dir := self._resolve_audit_log_dir()) is None:
                return
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            log_reason = clean_reason or reason
            suffix = " with force-restart language_server" if reloaded else ""
            prefix = f"Switched from '{old_account}' to '{target_account}'" if old_account else f"Switched to '{target_account}'"
            log_line = f"{ts} [watch] {prefix}{suffix} (reason: {log_reason})\n" if (old_account or reloaded) else f"{ts} [watch] {prefix} ({log_reason})\n"
            append_secure_log_entry(log_dir / "watch.log", log_line)
        except Exception as e:
            try:
                sys.stderr.write(f"[watch] Error appending audit log: {e}\n")
            except Exception:
                pass

    def _dispatch_http_post(self, url: str, data: bytes, label: str) -> threading.Thread | None:
        def _send() -> None:
            try:
                req = urllib.request.Request(
                    url, data=data, headers={"Content-Type": "application/json", "User-Agent": "agy-switcher-watcher/1.0"}, method="POST"
                )
                with urllib.request.urlopen(req, timeout=3.0) as resp:
                    resp.read()
            except (ssl.SSLError, urllib.error.URLError, ValueError, ConnectionError, OSError, Exception) as e:
                getattr(e, "close", lambda: None)()
                logger.warning("[watch] %s alert dispatch failed: %s", label, e)
                print(f"[watch] {label} alert dispatch failed: {e}", file=sys.stderr)
        try:
            t = threading.Thread(target=_send, name=f"watcher-{label.lower()}-alert", daemon=True)
            t.start()
            return t
        except Exception as e:
            logger.warning("[watch] Failed to spawn %s alert thread: %s", label, e)
            print(f"[watch] Failed to spawn {label} alert thread: {e}", file=sys.stderr)
            return None

    def dispatch_alert(
        self, previous_account: str, new_account: str, model_hint: str | None = None, timestamp: str | None = None
    ) -> threading.Thread | None:
        """Dispatches non-blocking alerts (e.g. Telegram Bot, Webhook) upon rotation completion."""
        try:
            raw_bot, raw_chat = os.environ.get("TELEGRAM_ALERT_BOT_TOKEN", "").strip(), os.environ.get("TELEGRAM_ALERT_CHAT_ID", "").strip()
            webhook_url = os.environ.get("WATCHER_WEBHOOK_URL", "").strip() or None
            bot_token, chat_id = raw_bot or None, raw_chat or None
            if not bot_token and not webhook_url:
                return None
            ts = timestamp or datetime.now(timezone.utc).isoformat()
            t_thread = None
            if bot_token and chat_id:
                msg = f"🔄 Antigravity Auto-Rotation Alert\n• Previous: {previous_account}\n• Active: {new_account}\n• Model: {model_hint or 'default'}\n• Timestamp: {ts}"
                payload = json.dumps({"chat_id": chat_id, "text": msg}).encode("utf-8")
                t_thread = self._dispatch_http_post(f"https://api.telegram.org/bot{bot_token}/sendMessage", payload, "Telegram")
            w_thread = self.dispatch_webhook(
                previous_account=previous_account, new_account=new_account, model_hint=model_hint, timestamp=ts, webhook_url=webhook_url
            ) if webhook_url else None
            return t_thread or w_thread
        except (ssl.SSLError, urllib.error.URLError, ValueError, ConnectionError, OSError, Exception) as e:
            logger.warning("[watch] Alert dispatch failed: %s", e)
            print(f"[watch] Alert dispatch failed: {e}", file=sys.stderr)
            return None

    def dispatch_webhook(
        self, previous_account: str = "", new_account: str = "", model_hint: str | None = None,
        timestamp: str | None = None, webhook_url: str | None = None, **kwargs: Any,
    ) -> threading.Thread | None:
        """Dispatches non-blocking JSON alert to configured webhook URL upon rotation completion."""
        target_url = (webhook_url or kwargs.get("url") or kwargs.get("endpoint") or "").strip()
        prev_acc, new_acc = previous_account or "", new_account or ""
        if (not target_url) and isinstance(prev_acc, str) and prev_acc.startswith(("http://", "https://")):
            target_url, prev_acc, new_acc = prev_acc.strip(), new_acc, model_hint or ""
        target_url = target_url or os.environ.get("WATCHER_WEBHOOK_URL", "").strip()
        if not target_url:
            return None
        raw_ts = timestamp or kwargs.get("ts")
        ts = raw_ts.isoformat() if isinstance(raw_ts, datetime) else (str(raw_ts) if raw_ts is not None else datetime.now(timezone.utc).isoformat())
        payload = {"event": "auto_rotation", "previous_account": str(prev_acc), "new_account": str(new_acc), "timestamp": ts}
        return self._dispatch_http_post(target_url, json.dumps(payload).encode("utf-8"), "Webhook")

    def _probe_active_quota(self, active_account: str, accounts_cache: Mapping[str, object] | None, model_name: str = "gemini") -> float | None:
        """Executes the Pre-Switch Quota Probe for the active account.
        Returns the quota fraction (0.0 to 1.0) if verified from cache or telemetry,
        or None if unverified / unparseable (triggering safe rotation fallback).
        """
        if not accounts_cache or not isinstance(accounts_cache, Mapping):
            return None
        acc_quota = accounts_cache.get(active_account)
        if acc_quota is None:
            return None

        def _parse_val(v: object) -> float | None:
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                val = float(v)
                return val / 100.0 if val > 1.0 else max(0.0, val)
            if isinstance(v, str):
                try:
                    s_val = float(v.strip().rstrip("%"))
                    return s_val / 100.0 if s_val > 1.0 else max(0.0, s_val)
                except ValueError:
                    pass
            return None

        if (pv := _parse_val(acc_quota)) is not None:
            return pv
        if not isinstance(acc_quota, Mapping):
            return None
        try:
            rot = _get_rotation_engine_class()(model_name=model_name, active_account_id=active_account)
            st = rot.get_quota_status(acc_quota, model_key=model_name)
            if st is not None and hasattr(st, "fraction") and isinstance(rf := st.fraction, (int, float)) and not isinstance(rf, bool):
                return float(rf)
        except Exception:
            pass
        cand = acc_quota.get(model_name)
        tgt: Mapping[str, object] = cand if isinstance(cand, Mapping) else acc_quota
        for v in (cand, *(tgt.get(k) for k in ("fraction", "remaining_fraction", "remainingFraction", "quota"))):
            if (pv := _parse_val(v)) is not None:
                return pv
        return None

    def trigger_switch(self, line: str = "", model_hint: str | None = None, file_path: str | None = None) -> bool:
        """Backward-compatible alias forwarding to handle_exhaustion."""
        return self.handle_exhaustion(line=line, model_hint=model_hint, file_path=file_path)

    def handle_exhaustion(self, line: str = "", model_hint: str | None = None, file_path: str | None = None) -> bool:
        now = time.time()
        if self._is_damping_active(now, log_suppression=True):
            return False
        active_name = self.engine.get_active_name()
        if not active_name:
            print("[watch] No active account found. Cannot switch.", file=sys.stderr)
            return False
        cache_data, client = self._load_cached_quota_data()
        accounts_cache = cache_data.get("accounts", cache_data) if isinstance(cache_data.get("accounts"), dict) else cache_data
        resolved_model = model_hint or self.infer_target_model(line=line, active_account=active_name, cache=accounts_cache)
        probe_model = resolved_model if resolved_model in ("gemini", "claude") else "gemini"
        active_quota_fraction = self._probe_active_quota(
            active_name, accounts_cache if isinstance(accounts_cache, Mapping) else None, model_name=probe_model
        )
        is_cache_stale = False
        if isinstance(accounts_cache, Mapping) and isinstance(accounts_cache.get(active_name), Mapping):
            acc_entry = accounts_cache[active_name]
            if acc_entry.get("stale") is True:
                is_cache_stale = True
            elif acc_entry.get("cached_at") is not None and not isinstance(acc_entry.get("cached_at"), bool):
                try:
                    c_at = float(acc_entry["cached_at"])
                    is_cache_stale = (now - c_at > 300.0) or (now - c_at < -5.0)
                except (ValueError, TypeError, OverflowError):
                    pass
        if active_quota_fraction is None or is_cache_stale:
            try:
                fetch_acc_fn = _get_fetch_account_quota()
                if fetch_acc_fn is not None:
                    _, live_data = fetch_acc_fn(self.engine, active_name, client=client)
                    if isinstance(live_data, Mapping) and not live_data.get("error"):
                        active_quota_fraction = self._probe_active_quota(active_name, {active_name: live_data}, model_name=probe_model)
                    else:
                        active_quota_fraction = None
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                logger.warning("[watch] Pre-Switch Quota Probe live telemetry network error for '%s': %s", active_name, e)
                print(f"[watch] Pre-Switch Quota Probe live telemetry network error for '{active_name}': {e}. Treating quota as unverified.", file=sys.stderr)
                active_quota_fraction = None
            except Exception as e:
                logger.debug("[watch] Pre-Switch Quota Probe live telemetry failure for '%s': %s", active_name, e)
                active_quota_fraction = None

        threshold = self.get_quota_threshold(default=0.25)
        if active_quota_fraction is not None and round(active_quota_fraction, 6) > round(threshold, 6):
            print(
                f"[watch] Pre-Switch Quota Probe: Active account '{active_name}' has healthy quota "
                f"({active_quota_fraction * 100:.1f}% > {threshold * 100:.1f}%). Suppressing rotation (transient RPC anomaly).",
                file=sys.stderr,
            )
            return False
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
        self.set_cooldown(active_name, reset_time=reset_time, reason=reason, model_hint=resolved_model)
        cooldowns = self.get_cooldowns()
        accounts_data = self.engine.list_accounts()
        accounts_list = accounts_data.get("accounts") or []
        # Filter out accounts in cooldown in-memory (O(1) lookup avoiding N disk I/O queries)
        valid_accounts = []
        for acc in accounts_list:
            acc_name = acc.get("name") if isinstance(acc, dict) else None
            if not acc_name or acc_name == active_name:
                continue
            if self._is_account_cooldown_penalized(acc_name, cooldowns, accounts_cache):
                continue
            if acc.get("valid"):
                valid_accounts.append(acc)
        if not valid_accounts:
            print("[watch] All other valid accounts are in cooldown. Cannot switch.", file=sys.stderr)
            return False
        for acc in valid_accounts:
            acc_name = acc.get("name")
            acc["quota"] = accounts_cache[acc_name] if isinstance(accounts_cache, Mapping) and acc_name in accounts_cache else acc.get("quota", {})
        rot_cls = _get_rotation_engine_class()
        rot_engine = rot_cls(model_name=resolved_model, active_account_id=active_name)
        penalized_cooldowns = {
            acc: cd for acc, cd in cooldowns.items()
            if self._is_account_cooldown_penalized(acc, cooldowns, accounts_cache)
        }
        target_account = rot_engine.evaluate(
            valid_accounts, current_active=active_name, target_model=resolved_model, cooldowns=penalized_cooldowns
        )
        valid_names = {acc["name"] for acc in valid_accounts}
        if not target_account or target_account == active_name or target_account not in valid_names:
            print("[watch] All other valid accounts are exhausted or in cooldown. Cannot switch.", file=sys.stderr)
            return False
        print(f"[watch] Switching from '{active_name}' to '{target_account}' mid-turn...", file=sys.stderr)
        try:
            res = self.engine.switch_tokens(target_account, restart_server=True, force=True)
            if self._is_switch_failed(res, target_account):
                err_msg = res.get("error", "unsuccessful") if isinstance(res, dict) else f"switch_tokens returned {res}"
                print(f"[watch] Failed to switch accounts to '{target_account}': {err_msg}", file=sys.stderr)
                return False
            self.last_switch_time = time.time()
            print(f"[watch] Successfully switched to '{target_account}'.", file=sys.stderr)
            if file_path and (file_path_str := str(file_path)) not in self.file_cursors:
                self.file_cursors[file_path_str] = 0
            self._advance_cursors_to_eof()
            # Fire non-blocking desktop notification (encapsulates 1.0s settling delay when reloaded=True)
            server_restarted = res.get("server_restarted", False) if isinstance(res, dict) else False
            self._notify_desktop(target_account=target_account, old_account=active_name, reason="quota 429", reloaded=server_restarted)
            ts = datetime.now(timezone.utc).isoformat()
            self.dispatch_alert(previous_account=active_name, new_account=target_account, model_hint=resolved_model, timestamp=ts)
            return True
        except Exception as e:
            print(f"[watch] Error switching accounts: {e}", file=sys.stderr)
            return False

    def scan_active_targets(self) -> list[str]:
        """
        Returns list of file paths to tail:
        1. Current active transcript across Desktop and CLI environments (if active session discovered).
        2. CLI logs from ~/.gemini/antigravity-cli/log/*.log.
        Handles dynamic active session switching, cursor cleanup, and target deduplication.
        """
        try:
            current_session_id = self.presence_detector.get_active_session_id()
        except Exception:
            current_session_id = None
        if current_session_id != self.active_session_id:
            if self.active_transcript_path:
                self.file_cursors.pop(str(self.active_transcript_path), None)
                self._stagnant_sizes.pop(str(self.active_transcript_path), None)
            self.active_session_id = current_session_id
            self.active_transcript_path = None
            if current_session_id:
                try:
                    self.active_transcript_path = self.presence_detector.get_active_transcript_path(session_id=current_session_id)
                except Exception:
                    self.active_transcript_path = None
        active_transcript_str = str(self.active_transcript_path) if self.active_transcript_path else None
        for path_key in list(self.file_cursors.keys()):
            if path_key.endswith(".jsonl") and path_key != active_transcript_str:
                del self.file_cursors[path_key]
                self._stagnant_sizes.pop(path_key, None)
        try:
            targets = [str(t) for t in self.presence_detector.get_watch_targets(as_str=True, session_id=current_session_id)]
        except Exception:
            targets = []
        if getattr(self, "log_dirs", None):
            for log_dir in self.log_dirs:
                if os.path.isdir(log_dir) and (not hasattr(self.presence_detector, "cli_log_dir") or os.path.abspath(log_dir) != os.path.abspath(str(self.presence_detector.cli_log_dir))):
                    targets.extend(glob.glob(os.path.join(log_dir, "*.log")))
        return list(dict.fromkeys(targets))

    def scan_new_files(self) -> list[str]:
        return self.scan_active_targets()

    def _get_latest_transcript_record(
        self, transcript_path: str | Path | None = None
    ) -> dict[str, Any] | None:
        """Reads and parses the latest JSON record from the active transcript file."""
        target_path: Path | None = None
        if transcript_path is not None:
            target_path = Path(transcript_path)
            if not target_path.is_file():
                return None
        else:
            if self.active_transcript_path and self.active_transcript_path.is_file():
                target_path = self.active_transcript_path
            elif hasattr(self, "presence_detector") and self.presence_detector is not None:
                try:
                    p = self.presence_detector.get_active_transcript_path(session_id=self.active_session_id)
                    if p and p.is_file():
                        target_path = p
                        self.active_transcript_path = p
                except Exception:
                    target_path = None

        if not target_path or not target_path.is_file():
            return None

        try:
            size = target_path.stat().st_size
            if size == 0:
                return None

            # Bounded expanding backward window to accommodate large prompts/responses (>64KB)
            max_read = 4 * 1024 * 1024
            chunk_size = min(size, 65536)

            with open(target_path, "rb") as f:
                while True:
                    f.seek(max(0, size - chunk_size))
                    data = f.read(min(chunk_size, size))
                    lines = data.splitlines()

                    last_line_str: str | None = None
                    for line in reversed(lines):
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
                        if isinstance(parsed, dict):
                            return cast(dict[str, Any], parsed)
                        return None
                    except json.JSONDecodeError:
                        if chunk_size < size and chunk_size < max_read:
                            chunk_size = min(size, chunk_size * 4, max_read)
                            continue
                        return None
        except OSError:
            return None

    def _is_server_busy(self) -> bool:
        """Checks if language_server has active presence locks or busy child tasks."""
        if getattr(self, "supervisor", None) is not None:
            try:
                pid = getattr(self.supervisor, "locate_language_server_pid", lambda: None)()
                return bool(self.supervisor.is_server_busy(pid if isinstance(pid, int) else None))
            except Exception:
                pass
        if hasattr(self.engine, "is_server_busy"):
            try:
                return bool(self.engine.is_server_busy())
            except TypeError:
                return bool(self.engine.is_server_busy(None))
            except Exception:
                pass
        return False

    def _is_turn_quiescent(self, transcript_path: str | Path | None = None) -> bool:
        """Determines if the system is at a verified Safe Turn Boundary via dual inspection."""
        try:
            if getattr(self, "supervisor", None) is None and not hasattr(self.engine, "is_server_busy"):
                return False

            record = self._get_latest_transcript_record(transcript_path)
            if not record:
                return False

            status = str(record.get("status", "")).strip().upper()
            msg_type = str(record.get("type", "")).strip().upper()
            if (
                status in ("IN_PROGRESS", "RUNNING", "STREAMING")
                or bool(record.get("tool_calls"))
                or (status != "DONE" and msg_type != "USER_INPUT")
                or self._is_server_busy()
            ):
                return False
            return True
        except Exception:
            return False

    def _probe_standby_candidates(
        self,
        active_account: str,
        accounts_cache: Mapping[str, object] | None = None,
        model_name: str = "gemini",
        accounts_list: list[Any] | None = None,
    ) -> list[tuple[str, float]]:
        """Probes standby accounts for proactive rotation eligibility (>= 70% quota)."""
        if accounts_list is None:
            raw: object = getattr(self.engine, "list_accounts", lambda: {})()
            accounts_list = raw.get("accounts") if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
        accounts_list = accounts_list or []
        cooldowns = self.get_cooldowns() if hasattr(self, "get_cooldowns") else {}

        candidates: list[tuple[str, float]] = []
        for acc in accounts_list:
            acc_name = acc.get("name") if isinstance(acc, dict) else getattr(acc, "name", None)
            if not acc_name or acc_name == active_account:
                continue
            if self._is_account_cooldown_penalized(acc_name, cooldowns, accounts_cache if isinstance(accounts_cache, dict) else None):
                continue
            if not (acc.get("valid", True) if isinstance(acc, dict) else getattr(acc, "valid", True)):
                continue

            acc_q = accounts_cache.get(acc_name, {}) if isinstance(accounts_cache, Mapping) else {}
            q_frac = self._probe_active_quota(acc_name, {acc_name: acc_q}, model_name=model_name)
            if q_frac is None and hasattr(self, "engine") and (fetch_acc_fn := _get_fetch_account_quota()):
                try:
                    _, standby_live = fetch_acc_fn(self.engine, acc_name)
                    if isinstance(standby_live, Mapping) and not standby_live.get("error"):
                        q_frac = self._probe_active_quota(acc_name, {acc_name: standby_live}, model_name=model_name)
                except Exception:
                    pass

            if q_frac is not None and round(q_frac, 6) >= 0.70:
                candidates.append((acc_name, q_frac))

        candidates.sort(key=lambda item: item[1], reverse=True)
        return candidates

    def _probe_account_reset_delta(
        self,
        account_name: str,
        accounts_cache: Mapping[str, object] | None = None,
        model_name: str = "gemini",
        now: float | None = None,
    ) -> float | None:
        """Extracts the reset delta in minutes relative to current time for account_name."""
        now = time.time() if now is None else now
        curr_dt = datetime.fromtimestamp(now, tz=timezone.utc)
        if isinstance(accounts_cache, Mapping) and isinstance(acc_entry := accounts_cache.get(account_name), Mapping):
            for src in (acc_entry.get(model_name), acc_entry):
                if isinstance(src, Mapping):
                    for k in ("reset_delta", "remaining_minutes", "delta_minutes", "minutes_remaining"):
                        if (val := src.get(k)) is not None and not isinstance(val, bool):
                            try:
                                if math.isfinite(f_val := float(val)):
                                    return f_val
                            except ValueError:
                                pass
            try:
                rot = _get_rotation_engine_class()(model_name=model_name, active_account_id=account_name)
                st = rot.get_quota_status(acc_entry, model_key=model_name, current_time=curr_dt)
                if hasattr(st, "reset_time") and hasattr(st, "get_reset_delta_minutes") and st.reset_time != datetime.max.replace(tzinfo=timezone.utc):
                    if math.isfinite(delta := float(st.get_reset_delta_minutes(curr_dt))):
                        return delta
            except Exception:
                pass
            for src in (acc_entry.get(model_name), acc_entry):
                if isinstance(src, Mapping) and (rt_raw := src.get("reset_time", src.get("resetTime"))) is not None and not isinstance(rt_raw, bool):
                    pts = CooldownManager._parse_timestamp(rt_raw)
                    if math.isfinite(pts) and pts != float("inf"):
                        return (pts - now) / 60.0
        if hasattr(self, "cooldown_manager") and self.cooldown_manager is not None:
            try:
                if (rt_val := self.cooldown_manager.get_reset_time_from_cache(account_name, model_hint=model_name)) is not None and math.isfinite(rt_val):
                    return (rt_val - now) / 60.0
            except Exception:
                pass
        return None

    def _evaluate_proactive_rotation(self) -> bool:
        """Evaluates proactive rotation eligibility at a Safe Turn Boundary."""
        try:
            thresh_attr = getattr(self.engine, "get_quota_threshold", getattr(self.engine, "quota_threshold", None))
            if thresh_attr is None:
                return False
            threshold = self.get_quota_threshold(default=0.20)

            if threshold <= 0.0:
                return False

            now = time.time()
            if self._is_damping_active(now) or not self._is_turn_quiescent():
                return False

            get_active_fn = getattr(self.engine, "get_active_name", None)
            if not callable(get_active_fn) or not isinstance(active_name := get_active_fn(), str) or not active_name:
                return False

            cache_data, client = self._load_cached_quota_data()
            accounts_cache = cache_data.get("accounts", cache_data) if isinstance(cache_data.get("accounts"), dict) else cache_data
            active_quota_fraction = self._probe_active_quota(
                active_name, accounts_cache if isinstance(accounts_cache, Mapping) else None, model_name="gemini"
            )

            live_data_map: Mapping[str, object] | None = None
            if active_quota_fraction is None:
                try:
                    if fetch_acc_fn := _get_fetch_account_quota():
                        _, live_data = fetch_acc_fn(self.engine, active_name, client=client)
                        if isinstance(live_data, Mapping) and not live_data.get("error"):
                            live_data_map = {active_name: live_data}
                            active_quota_fraction = self._probe_active_quota(
                                active_name, live_data_map, model_name="gemini"
                            )
                except (urllib.error.URLError, TimeoutError, OSError) as e:
                    logger.warning("[watch] Proactive quota probe live telemetry network error for '%s': %s", active_name, e)
                    print(f"[watch] Proactive quota probe live telemetry network error for '{active_name}': {e}. Retaining active account.", file=sys.stderr)
                    return False
                except Exception as e:
                    logger.debug("[watch] Proactive quota probe live telemetry failure for '%s': %s", active_name, e)
                    return False

            if active_quota_fraction is None or round(active_quota_fraction, 6) > round(threshold, 6):
                return False

            probe_cache = live_data_map if live_data_map is not None else (accounts_cache if isinstance(accounts_cache, Mapping) else None)
            reset_delta = self._probe_account_reset_delta(active_name, probe_cache, model_name="gemini", now=now)
            if reset_delta is not None and 0.0 <= reset_delta <= 30.0:
                if round(active_quota_fraction, 6) > 0.05:
                    print(f"[watch] Drain window active: '{active_name}' reset in {reset_delta:.1f}m (<= 30.0m) with {active_quota_fraction * 100:.1f}% quota (> 5.0%). Retaining active account.", file=sys.stderr)
                    return False
                print(f"[watch] Drain window hard floor override: '{active_name}' has {active_quota_fraction * 100:.1f}% quota (<= 5.0%) inside drain window. Forcing rotation.", file=sys.stderr)

            candidates = self._probe_standby_candidates(active_name, accounts_cache if isinstance(accounts_cache, Mapping) else None, model_name="gemini")
            if not candidates:
                return False

            best_standby, best_quota = candidates[0]
            print(f"[watch] Proactive threshold failover: Active account '{active_name}' quota ({active_quota_fraction * 100:.1f}% <= {threshold * 100:.1f}%) at Safe Turn Boundary. Rotating to '{best_standby}' ({best_quota * 100:.1f}% quota).", file=sys.stderr)
            try:
                res = self.engine.switch_tokens(best_standby, restart_server=True, force=True)
                if self._is_switch_failed(res, best_standby):
                    err_msg = res.get("error", "unsuccessful") if isinstance(res, dict) else f"switch_tokens returned {res}"
                    print(f"[watch] Failed to proactively switch accounts to '{best_standby}': {err_msg}", file=sys.stderr)
                    return False
                self.last_switch_time = time.time()
                print(f"[watch] Successfully proactively switched to '{best_standby}'.", file=sys.stderr)
                if not self.active_transcript_path and getattr(self, "presence_detector", None) is not None:
                    try:
                        sid = self.active_session_id or getattr(self.presence_detector, "get_active_session_id", lambda: None)()
                        if (p := self.presence_detector.get_active_transcript_path(session_id=sid)) and p.is_file():
                            self.active_transcript_path = p
                    except Exception:
                        pass
                if self.active_transcript_path and (trans_str := str(self.active_transcript_path)) not in self.file_cursors:
                    self.file_cursors[trans_str] = 0
                self._advance_cursors_to_eof()
                return True
            except Exception as e:
                print(f"[watch] Error during proactive account rotation: {e}", file=sys.stderr)
                return False
        except Exception as e:
            print(f"[watch] Error evaluating proactive rotation: {e}", file=sys.stderr)
            return False

    def poll_once(self) -> int:
        """
        Polls active targets once for new lines and processes them.
        Returns the number of account switches triggered during this polling cycle.
        """
        try:
            current_targets = self.scan_active_targets()
        except Exception:
            current_targets = []
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
            try:
                if not os.path.isfile(target):
                    del self.file_cursors[target]
                    self._stagnant_sizes.pop(target, None)
                    continue
            except OSError:
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
                        chunk = f.read(self.max_chunk_bytes)
                    if not chunk:
                        continue
                    last_nl = chunk.rfind(b"\n")
                    if last_nl == -1:
                        if len(chunk) >= self.max_chunk_bytes:
                            # Oversized unterminated line exceeds read bound; advance cursor so tailing cannot wedge.
                            self.file_cursors[target] = cursor + len(chunk)
                            self._stagnant_sizes.pop(target, None)
                            continue
                        # No complete line has arrived; hold cursor and track file size
                        self.file_cursors[target] = cursor
                        self._stagnant_sizes[target] = current_size
                        continue
                    complete_bytes = chunk[:last_nl + 1]
                    raw_lines = complete_bytes.splitlines(keepends=True)
                    is_jsonl = target.endswith(".jsonl") or target.endswith("transcript.jsonl")
                    failover_occurred = False
                    for raw_line in raw_lines:
                        line_text = raw_line.decode("utf-8", errors="replace")
                        if self.process_line(line_text, is_jsonl=is_jsonl, file_path=target):
                            switches_triggered += 1
                            failover_occurred = True
                            break
                    if failover_occurred:
                        # Do NOT overwrite cursor; handle_exhaustion has already seeked all targets to EOF
                        break
                    self.file_cursors[target] = cursor + (last_nl + 1)
                    if len(chunk) > last_nl + 1 and (cursor + len(chunk) >= current_size):
                        self._stagnant_sizes[target] = current_size
                    else:
                        self._stagnant_sizes.pop(target, None)
                except OSError:
                    continue
        if switches_triggered == 0:
            try:
                if self._evaluate_proactive_rotation():
                    switches_triggered += 1
            except Exception as e:
                print(f"[watch] Error evaluating proactive rotation: {e}", file=sys.stderr)
        return switches_triggered

    def watch_loop(self, poll_interval: float = 0.5, max_iterations: int | None = None) -> None:
        if self.daemon_manager.is_systemd_active():
            print(SYSTEMD_ACTIVE_ERROR_MSG, file=sys.stderr)
            sys.exit(1)
        try:
            self.daemon_manager._acquire_lock()
        except (BlockingIOError, IOError):
            print("Error: Another watcher daemon is already running.", file=sys.stderr)
            sys.exit(1)
        print("[watch] Daemon started, tailing logs for quota exhaustion...", file=sys.stderr)
        iterations = 0
        try:
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
        finally:
            self.daemon_manager._release_lock()

class DaemonManager:
    """Manages detached background lifecycle of the watch daemon with atomic PID and lock enforcement."""

    SYSTEMD_TEMPLATE = (
        "[Unit]\nDescription=Antigravity Account Switcher Log Watcher Daemon\nAfter=network.target\n\n"
        "[Service]\nType=simple\nExecStart=%h/.local/bin/agy-switch watch run\nRestart=on-failure\nRestartSec=5s\nStandardOutput=append:%h/.antigravity-vault/watch.log\nStandardError=append:%h/.antigravity-vault/watch.log\n\n"
        "[Install]\nWantedBy=default.target\n"
    )
    SYSTEMD_SERVICE_NAME = "agy-switch-watcher.service"

    def __init__(
        self,
        vault_dir: Path | str | None = None,
        watch_script: Path | str | None = None,
        proc_dir: Path | str | None = None,
        python_bin: str | None = None,
        gemini_dir: Path | str | None = None,
    ) -> None:
        if vault_dir is not None and not _is_mock_object(vault_dir) and isinstance(vault_dir, (str, Path)):
            self.vault_dir = Path(vault_dir).expanduser()
        elif (env_vault := os.environ.get("AGY_VAULT_DIR")) and env_vault.strip():
            self.vault_dir = Path(env_vault.strip()).expanduser()
        else:
            self.vault_dir = DEFAULT_VAULT_DIR
        self.pid_file = self.vault_dir / "watch.pid"
        self.log_file = self.vault_dir / "watch.log"
        self.lock_file = self.vault_dir / "watch.lock"
        self._lock_fd: int | None = None
        self._start_lock = threading.RLock()
        self.proc_dir = Path(proc_dir) if proc_dir else Path("/proc")
        self.python_bin = python_bin or sys.executable
        _canonical_watch = (Path(__file__).resolve().parent.parent / "watch.py").resolve()
        self.watch_script = Path(watch_script).resolve() if watch_script else _canonical_watch
        self.gemini_dir = Path(gemini_dir).expanduser() if gemini_dir else DEFAULT_GEMINI_DIR

    def is_systemd_active(self, service_name: str = "agy-switch-watcher.service") -> bool:
        """Detects whether the specified user-level systemd service is actively running."""
        if not shutil.which("systemctl"):
            return False
        current_popen = subprocess.Popen
        try:
            if current_popen is not _real_popen:
                setattr(subprocess, "Popen", _real_popen)
            res = subprocess.run(
                ["systemctl", "--user", "is-active", service_name],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=5.0,
            )
            return res.returncode == 0
        except (subprocess.SubprocessError, OSError, Exception):
            return False
        finally:
            if current_popen is not _real_popen:
                setattr(subprocess, "Popen", current_popen)

    def _resolve_lock_path(self) -> Path:
        """Resolves watch.lock path, rejecting symlinks and handling permission errors."""
        if self.vault_dir.exists() and not os.access(self.vault_dir, os.W_OK):
            print(f"Error: Permission denied accessing vault directory '{self.vault_dir}': Directory is not writeable.", file=sys.stderr)
            sys.exit(1)
        try:
            self.vault_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            try: os.chmod(self.vault_dir, 0o700)
            except OSError: pass
        except (PermissionError, OSError) as e:
            print(f"Error: Permission denied accessing vault directory '{self.vault_dir}': {e}", file=sys.stderr)
            sys.exit(1)
        lock_path = self.lock_file
        try:
            if lock_path.is_symlink():
                try: lock_path.unlink()
                except OSError: pass
                print(f"Error: watch.lock at '{lock_path}' is a symlink. Symlink traversal is rejected.", file=sys.stderr)
                sys.exit(1)
        except (PermissionError, OSError) as e:
            print(f"Error: Permission denied accessing lock file '{lock_path}': {e}", file=sys.stderr)
            sys.exit(1)
        return lock_path

    def acquire_lock(self) -> int:
        """Acquires exclusive POSIX file lock on watch.lock, exiting on permission/symlink errors."""
        if self._lock_fd is not None:
            return self._lock_fd
        lock_path = self._resolve_lock_path()
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(str(lock_path), flags, 0o600)
        except (PermissionError, OSError) as e:
            if getattr(e, "errno", None) == 40 or "symlink" in str(e).lower():
                try: lock_path.unlink()
                except OSError: pass
                print(f"Error: watch.lock at '{lock_path}' is a symlink. Symlink traversal is rejected.", file=sys.stderr)
                sys.exit(1)
            print(f"Error: Permission denied accessing lock file '{lock_path}': {e}", file=sys.stderr)
            sys.exit(1)
        try: os.chmod(lock_path, 0o600)
        except OSError: pass
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as e:
            try: os.close(fd)
            except OSError: pass
            if isinstance(e, BlockingIOError) or getattr(e, "errno", None) in (11, 35):
                raise
            print(f"Error: Permission denied locking '{lock_path}': {e}", file=sys.stderr)
            sys.exit(1)
        self._lock_fd = fd
        return fd

    def _acquire_lock(self) -> int:
        return self.acquire_lock()

    def release_lock(self) -> None:
        self._release_lock()

    def _release_lock(self) -> None:
        """Releases the exclusive lock and closes the retained file descriptor."""
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                os.close(self._lock_fd)
            except OSError:
                pass
            self._lock_fd = None

    def _clean_pid_file(self) -> None:
        """Safely unlinks PID file if it exists or is a symlink."""
        try:
            self.pid_file.unlink(missing_ok=True)
        except OSError:
            pass

    def _write_pid_file(self, pid: int) -> None:
        """Atomically writes PID to watch.pid with strictly enforced mode 0600."""
        _write_atomic_file_secure(self.pid_file, f"{pid}\n".encode("utf-8"))

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
        raw_args = [a for a in raw.split(b"\x00") if a] if raw else []
        if not raw_args:
            return False
        args = [a.decode("utf-8", errors="replace").strip() for a in raw_args]
        argv0 = os.path.basename(args[0]).lower()
        is_python = argv0.startswith(("python", "pypy"))
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
                or "agy-switch" in target_script or "agy_switch" in target_script
            )
            return matches_script and ("run" in full_cmd or "watch" in full_cmd or bool(script_name and script_name in full_cmd))
        args_lower = [a.lower() for a in args]
        return "watch" in args_lower or "run" in args_lower

    def is_process_alive(self, pid: int) -> bool:
        """Checks if PID exists, belongs to current user, and represents an active watch process."""
        if pid <= 1 or pid == os.getpid():
            return False
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError, OSError):
            return False
        return self._is_watch_cmdline(pid)

    def get_running_pid(self) -> int | None:
        """Reads and returns running daemon PID from watch.pid if active. Unlinks dead/stale PID files."""
        if self.pid_file.is_symlink() or not self.pid_file.is_file():
            if self.pid_file.is_symlink(): self._clean_pid_file()
            return None
        try:
            pid = int(self.pid_file.read_text(encoding="utf-8").strip())
            if pid > 1 and self.is_process_alive(pid):
                return pid
        except (OSError, ValueError):
            pass
        self._clean_pid_file()
        return None

    def start(self, cooldown: int | None = None) -> int:
        """Spawns detached background process with start_new_session=True and mode 0600 PID file."""
        if self.is_systemd_active():
            print(SYSTEMD_ACTIVE_ERROR_MSG, file=sys.stderr)
            sys.exit(1)
        with self._start_lock:
            if self._lock_fd is not None:
                existing_pid = self.get_running_pid()
                if existing_pid is not None:
                    raise DaemonAlreadyRunningError(existing_pid)
                print("Error: Another watcher daemon is already running.", file=sys.stderr)
                sys.exit(1)
            try:
                self._acquire_lock()
            except (BlockingIOError, IOError):
                print("Error: Another watcher daemon is already running.", file=sys.stderr)
                sys.exit(1)
            try:
                existing_pid = self.get_running_pid()
                if existing_pid is not None:
                    raise DaemonAlreadyRunningError(existing_pid)
                self.vault_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                try: os.chmod(self.vault_dir, 0o700)
                except OSError: pass
                log_fd = _open_secure_log_file(self.log_file)
                cmd = [self.python_bin, str(self.watch_script), "run"]
                if cooldown is not None:
                    cmd.extend(["--cooldown", str(cooldown)])
                try:
                    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log_fd, stderr=log_fd, start_new_session=True)
                finally:
                    try: os.close(log_fd)
                    except OSError: pass
                self._last_proc = proc
                pid = proc.pid
                try:
                    self._write_pid_file(pid)
                except Exception:
                    try:
                        proc.terminate()
                        proc.wait(timeout=1.0)
                    except Exception:
                        proc.kill()
                    raise
                return pid
            except Exception:
                self._release_lock()
                raise

    def stop(self, timeout: float = 5.0) -> bool:
        """Gracefully terminates running daemon with SIGTERM, escalating to SIGKILL on timeout."""
        if getattr(self, "_last_proc", None) is not None:
            try: self._last_proc.poll()
            except Exception: pass
        pid = self.get_running_pid()
        if pid is None:
            return False
        try:
            os.kill(pid, signal.SIGTERM)
        except PermissionError as e:
            print(f"Error stopping daemon: Permission denied for PID {pid} ({e})", file=sys.stderr)
            return False
        except (ProcessLookupError, OSError) as e:
            if not isinstance(e, ProcessLookupError):
                print(f"Error stopping daemon: {e}", file=sys.stderr)
            self._clean_pid_file()
            self._release_lock()
            return False
        start_time = time.time()
        poll_interval = 0.05
        terminated = False
        while time.time() - start_time < timeout:
            if not self.is_process_alive(pid):
                terminated = True
                break
            _get_time().sleep(poll_interval)
        if not terminated and self.is_process_alive(pid):
            try: os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError): pass
            kill_start = time.time()
            while time.time() - kill_start < 1.0:
                if not self.is_process_alive(pid):
                    break
                _get_time().sleep(poll_interval)
        if getattr(self, "_last_proc", None) is not None and getattr(self._last_proc, "pid", None) == pid:
            try: self._last_proc.wait(timeout=0.2)
            except Exception: pass
        if os.name == "posix":
            try: os.waitpid(pid, os.WNOHANG)
            except (ChildProcessError, OSError): pass
        if self.is_process_alive(pid):
            return False
        self._clean_pid_file()
        self._release_lock()
        return True

    def status(self) -> dict[str, Any]:
        """Returns status dictionary: {'running': bool, 'pid': int | None, 'log_file': str, 'targets': list[str]}."""
        pid = self.get_running_pid()
        targets: list[str] = []
        if pid is not None:
            try:
                targets = [str(t) for t in PresenceDetector(gemini_dir=self.gemini_dir, proc_dir=self.proc_dir).get_watch_targets(as_str=True)]
            except Exception:
                pass
        return {"running": pid is not None, "pid": pid, "log_file": str(self.log_file), "targets": targets}

    def generate_systemd(self, target_path: Path | str | None = None) -> str:
        """Generates user systemd service definition and optionally writes it to target_path."""
        if target_path is not None:
            target = Path(target_path).expanduser()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(self.SYSTEMD_TEMPLATE, encoding="utf-8")
        return self.SYSTEMD_TEMPLATE

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="watch.py", description="Antigravity Account Switcher Log Watcher & Daemon")
    parser.add_argument("action", nargs="?", choices=["run", "start", "stop", "status"], default="run", help="Daemon action")
    parser.add_argument("--generate-systemd", action="store_true", help="Output user systemd service definition")
    parser.add_argument("--install-systemd", action="store_true", help="Install systemd user service unit")
    parser.add_argument("--cooldown", type=int, default=60, help="Default cooldown minutes")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    engine = _get_engine_class()()
    dm = DaemonManager(vault_dir=engine.vault_dir)
    if args.generate_systemd or args.install_systemd:
        target = Path.home() / ".config" / "systemd" / "user" / "agy-switch-watcher.service" if args.install_systemd else None
        unit = dm.generate_systemd(target_path=target)
        sys.stdout.write(unit)
        sys.stdout.flush()
        return 0
    if args.action == "start":
        try:
            pid = dm.start(cooldown=args.cooldown)
            print(f"[watch] Daemon started in background (PID: {pid}).\n  - Log: {dm.log_file}\n  - PID: {dm.pid_file}")
            return 0
        except DaemonAlreadyRunningError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        except Exception as e:
            print(f"Error starting daemon: {e}", file=sys.stderr)
            return 1
    if args.action == "stop":
        try:
            stopped = dm.stop()
            print("[watch] Daemon stopped successfully." if stopped else "[watch] Daemon was not running.")
            return 0
        except Exception as e:
            print(f"Error stopping daemon: {e}", file=sys.stderr)
            return 1
    if args.action == "status":
        st = dm.status()
        if st["running"]:
            print(f"[watch] Daemon is running (PID: {st['pid']}).\n  - Log: {st['log_file']}")
            if st.get("targets"):
                print("  - Monitored targets:")
                for t in st["targets"]:
                    print(f"    * {t}")
        else:
            print(f"[watch] Daemon is stopped.\n  - Log: {st['log_file']}")
        return 0
    if args.action == "run":
        try:
            signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))
        except Exception:
            pass
        LogWatcher(engine, cooldown_minutes=args.cooldown).watch_loop()
        return 0
    return 0

__all__ = [
    "AgySwitchError", "DaemonError", "DaemonAlreadyRunningError", "QuotaClient",
    "fetch_account_quota", "RotationEngine", "AgySwitchEngine", "DEFAULT_VAULT_DIR",
    "DEFAULT_GEMINI_DIR", "DEFAULT_MAX_CHUNK_BYTES", "DEFAULT_DAMPING_SECONDS",
    "SYSTEMD_ACTIVE_ERROR_MSG", "_open_secure_log_file", "append_secure_log_entry",
    "PresenceDetector", "CooldownManager", "LogWatcher", "DaemonManager",
    "main", "time", "shutil", "subprocess",
]

if __name__ == "__main__":
    sys.exit(main())
