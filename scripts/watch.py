import os
import time
import re
import glob
import json
import math
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any

try:
    from agy_switch import AgySwitchEngine, RotationEngine, QuotaClient
except ImportError:
    from scripts.agy_switch import AgySwitchEngine, RotationEngine, QuotaClient


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

    def get_active_transcript_path(self) -> Path | None:
        """
        Returns Path to brain/<active-id>/.system_generated/logs/transcript.jsonl
        when the session directory exists, or None.
        """
        session_id = self.get_active_session_id()
        if not session_id:
            return None

        session_dir = self.brain_dir / session_id
        if not session_dir.is_dir():
            return None

        return session_dir / ".system_generated" / "logs" / "transcript.jsonl"

    def get_watch_targets(self, as_str: bool = False) -> list[Path] | list[str]:
        """
        Returns list of active log targets to watch:
        - Active transcript.jsonl if resolved and existing on disk.
        - Existing CLI logs in antigravity-cli/log/*.log.
        Omits non-existent paths.
        """
        targets: list[Path] = []
        active_transcript = self.get_active_transcript_path()
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
        if isinstance(cache_dir, (str, Path, os.PathLike)):
            self.cache_dir = Path(cache_dir).expanduser()
            self.cooldown_file = self.cache_dir / "cooldown.json"
        else:
            self.cache_dir = cache_dir
            self.cooldown_file = getattr(cache_dir, "cooldown_file", None) or (
                cache_dir / "cooldown.json" if hasattr(cache_dir, "__truediv__") else None
            )
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
        if not isinstance(self.cache_dir, (str, Path, os.PathLike)):
            # Test mock fallback: if cache_dir is a test MagicMock rather than filesystem path, handle gracefully
            try:
                if hasattr(self.cooldown_file, "write_text"):
                    self.cooldown_file.write_text(json.dumps(records, indent=2))
            except Exception:
                pass
            return

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
        log_dirs: list[str] = None,
        cooldown_minutes: int = 60,
    ):
        self.engine = engine
        self.log_dirs = log_dirs or [
            str(self.engine.gemini_dir / "antigravity-cli" / "log")
            if hasattr(self.engine, "gemini_dir") and isinstance(self.engine.gemini_dir, (str, Path))
            else str(Path.home() / ".gemini" / "antigravity-cli" / "log"),
            str(self.engine.gemini_dir / "antigravity" / "log")
            if hasattr(self.engine, "gemini_dir") and isinstance(self.engine.gemini_dir, (str, Path))
            else str(Path.home() / ".gemini" / "antigravity" / "log"),
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
        self.cooldown_file = getattr(self.cooldown_manager, "cooldown_file", getattr(self.engine, "cache_dir", Path.home()) / "cooldown.json")
        
        self.exhaustion_regex = re.compile(
            r"RESOURCE_EXHAUSTED \(\s*code\s*429\s*\)|Individual quota reached|weekly quota reached",
            re.IGNORECASE
        )
        
        self.file_cursors = {}

    def is_exhausted_message(self, line: str) -> bool:
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

    def process_line(self, line: str) -> bool:
        if self.is_exhausted_message(line):
            print(f"[watch] Detected quota exhaustion: {line.strip()}", file=sys.stderr)
            return self.trigger_switch(line=line)
        return False

    def get_cooldowns(self) -> dict:
        return self.cooldown_manager.get_cooldowns()

    def set_cooldown(self, account_name: str, reset_time: float | None = None, reason: str = "RESOURCE_EXHAUSTED"):
        return self.cooldown_manager.set_cooldown(account_name, reset_time=reset_time, reason=reason)

    def trigger_switch(self, line: str = "", model_hint: str | None = None) -> bool:
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
            print(f"[watch] Successfully switched to '{target_account}'.", file=sys.stderr)
            # small sleep to avoid rapid re-triggering for the same error
            time.sleep(2)
            return True
        except Exception as e:
            print(f"[watch] Error switching accounts: {e}", file=sys.stderr)
            return False

    def scan_new_files(self) -> list[str]:
        log_files = []
        for log_dir in self.log_dirs:
            if not os.path.isdir(log_dir):
                continue
            log_files.extend(glob.glob(os.path.join(log_dir, "*.log")))
        return log_files

    def watch_loop(self):
        print("[watch] Daemon started, tailing logs for quota exhaustion...", file=sys.stderr)
        while True:
            try:
                current_files = self.scan_new_files()
                for file_path in current_files:
                    if file_path not in self.file_cursors:
                        # Start tailing from the end of new files
                        self.file_cursors[file_path] = os.path.getsize(file_path)

                for file_path in list(self.file_cursors.keys()):
                    if not os.path.isfile(file_path):
                        del self.file_cursors[file_path]
                        continue

                    current_size = os.path.getsize(file_path)
                    cursor = self.file_cursors[file_path]

                    if current_size < cursor:
                        # File was rotated/truncated
                        cursor = 0

                    if current_size > cursor:
                        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                            f.seek(cursor)
                            lines = f.readlines()
                            self.file_cursors[file_path] = f.tell()
                            
                            for line in lines:
                                if self.process_line(line):
                                    # If a failover switch was triggered, break out of this batch
                                    # to prevent cascading failovers from duplicate burst error lines.
                                    break
                                
                time.sleep(0.5)
            except KeyboardInterrupt:
                print("\n[watch] Stopping daemon.", file=sys.stderr)
                break
            except Exception as e:
                print(f"[watch] Unhandled exception in loop: {e}", file=sys.stderr)
                time.sleep(5)

if __name__ == "__main__":
    engine = AgySwitchEngine()
    watcher = LogWatcher(engine)
    watcher.watch_loop()
