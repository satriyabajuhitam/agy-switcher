"""Telemetry engine, CloudCode QuotaClient, thread-safe cache, and defensive parsers.

Layer 1 Core Services module: Depends strictly on Layer 0 (exceptions, models)
and Python standard library. Zero external dependencies.
"""

from __future__ import annotations

import concurrent.futures
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from agy_switcher.exceptions import AgySwitchError

# Default OAuth Client Credentials for CloudCode
DEFAULT_CLIENT_ID = "1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com"
DEFAULT_CLIENT_SECRET = "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"

# Error classification patterns and phrases
AUTH_ERROR_CODE_PATTERN = re.compile(r"\b(401|403)\b")
HTTP_SERVER_ERROR_PATTERN = re.compile(r"\b(500|502|503|504)\b")
AUTH_PHRASES = (
    "no token",
    "token refresh rejected",
    "invalid_grant",
    "unauthorized",
    "revoked",
    "forbidden",
    "access_denied",
    "invalid_client",
)

# Global re-entrant lock protecting cache read-merge-write operations across threads
_CACHE_LOCK = threading.RLock()


def _sanitize_expires_in(raw_exp: Any, default: int = 3600) -> int:
    """Sanitizes an expires_in value to a positive integer, falling back to default."""
    if isinstance(raw_exp, bool):
        return default
    try:
        val = float(raw_exp)
        return int(val) if math.isfinite(val) and 0 < val <= 31536000 else default
    except (ValueError, TypeError, OverflowError):
        return default


def _has_auth_indicator(text: Any) -> bool:
    """Checks whether an error message contains auth/token corruption keywords or HTTP 401/403 status codes."""
    if text is None:
        return False
    lower = str(text).lower()
    if any(phrase in lower for phrase in AUTH_PHRASES):
        return True
    if AUTH_ERROR_CODE_PATTERN.search(lower):
        return True
    return False


def _parse_timestamp_defensive(val: Any) -> float:
    """Safely extracts a positive float UNIX epoch timestamp in UTC or returns float('inf')."""
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
            return float("inf")
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


def _parse_quota_defensive(val: Any) -> int | float:
    """Safely extracts a non-negative quota percentage or returns -1."""
    if val is None or isinstance(val, bool):
        return -1
    if isinstance(val, (int, float)):
        return val if math.isfinite(val) and val >= 0 else -1
    if isinstance(val, str):
        try:
            num = float(val.strip().rstrip("%"))
            if math.isfinite(num) and num >= 0:
                return num
        except ValueError:
            pass
    return -1


def normalize_model_name(model: str) -> str:
    """Validates and normalizes model name to 'gemini' or 'claude'.

    Tolerant of specific slugs like 'gemini-3.8-pro' or 'claude-3-5-sonnet'.
    Raises AgySwitchError for unsupported models.
    """
    if not model or not isinstance(model, str):
        raise AgySwitchError(f"Unsupported model '{model}'. Allowed models: 'gemini', 'claude'.")
    m = model.strip().lower()
    has_gemini = "gemini" in m
    has_claude = "claude" in m
    if has_gemini and not has_claude:
        return "gemini"
    elif has_claude and not has_gemini:
        return "claude"
    else:
        raise AgySwitchError(f"Unsupported model '{model}'. Allowed models: 'gemini', 'claude'.")


def _is_auth_error(error_val: Any, client: Any = None) -> bool:
    """Classifies whether an error represents auth/token corruption (401/403, revoked, no token)."""
    if error_val is None:
        return False
    if isinstance(error_val, dict):
        raw_type = error_val.get("error_type")
        if raw_type == "auth":
            return True
        code = error_val.get("code") or error_val.get("status_code")
        if isinstance(code, str) and code.isdigit():
            code = int(code)
        if isinstance(code, int):
            if code in (401, 403):
                return True
            if code in (500, 502, 503, 504):
                return False
        err_str = str(error_val.get("error", "")).lower()
        if raw_type == "http" and _has_auth_indicator(err_str):
            return True
        if isinstance(raw_type, str) and raw_type.strip() and raw_type.strip() != "auth":
            return False
    elif isinstance(error_val, Exception):
        if isinstance(error_val, urllib.error.HTTPError):
            if error_val.code in (401, 403):
                return True
            if error_val.code in (500, 502, 503, 504):
                return False
        if isinstance(error_val, (TimeoutError, ConnectionError)):
            return False
        if isinstance(error_val, urllib.error.URLError) and not isinstance(error_val, urllib.error.HTTPError):
            return False
        err_str = str(error_val).lower()
    elif isinstance(error_val, str):
        err_str = error_val.lower()
    else:
        err_str = str(error_val).lower()

    if _has_auth_indicator(err_str):
        return True

    if client is not None:
        c_type = getattr(client, "last_error_type", None)
        if isinstance(c_type, str) and c_type == "auth":
            return True
        c_err = getattr(client, "last_error", None)
        if isinstance(c_err, str) and _has_auth_indicator(c_err):
            return True

    return False


def _is_network_error(error_val: Any, client: Any = None) -> bool:
    """Classifies whether an error represents a transient network transport failure."""
    if error_val is None:
        return False
    if isinstance(error_val, urllib.error.HTTPError):
        # Only classify 5xx server errors as transient network/transport errors
        return error_val.code in (500, 502, 503, 504)
    if isinstance(error_val, (urllib.error.URLError, TimeoutError, ConnectionError)):
        return True
    if isinstance(error_val, dict):
        if error_val.get("error_type") == "network":
            return True
        if error_val.get("error_type") == "auth":
            return False
        if error_val.get("error_type") == "http":
            code = error_val.get("code") or error_val.get("status_code")
            if isinstance(code, int):
                return code in (500, 502, 503, 504)
            err_str = str(error_val.get("error", "")).lower()
            return bool(HTTP_SERVER_ERROR_PATTERN.search(err_str))
        err_str = str(error_val.get("error", "")).lower()
    elif isinstance(error_val, Exception):
        err_str = str(error_val).lower()
    elif isinstance(error_val, str):
        err_str = error_val.lower()
    else:
        err_str = str(error_val).lower()

    net_keywords = [
        "timeout", "timed out", "urlerror", "network", "unreachable",
        "connection", "reset by peer", "failed to connect", "socket",
        "dns", "authoritative",
    ]
    is_net_error = any(k in err_str for k in net_keywords) or bool(HTTP_SERVER_ERROR_PATTERN.search(err_str))
    if is_net_error:
        if not _has_auth_indicator(err_str):
            return True

    if client is not None:
        c_type = getattr(client, "last_error_type", None)
        if isinstance(c_type, str) and c_type == "network":
            return True
        if isinstance(c_type, str) and c_type == "http":
            c_err = getattr(client, "last_error", None)
            if isinstance(c_err, str) and bool(HTTP_SERVER_ERROR_PATTERN.search(c_err)):
                return True
        c_err = getattr(client, "last_error", None)
        if isinstance(c_err, str):
            c_err_lower = c_err.lower()
            c_is_net = any(k in c_err_lower for k in net_keywords) or bool(HTTP_SERVER_ERROR_PATTERN.search(c_err_lower))
            if c_is_net and not _has_auth_indicator(c_err_lower):
                return True

    return False


def _is_stale_exhausted_cache(
    cached_entry: Any,
    rot_engine: Any,
    model_key: str,
    now: float,
    ttl_seconds: float = 300.0,
    clock_skew_tolerance: float = 5.0,
) -> bool:
    """Returns True if cached_entry is non-dict, unparseable, or holds a 0% quota that is stale/expired."""
    if not isinstance(cached_entry, dict):
        return True
    if rot_engine is None or not hasattr(rot_engine, "get_quota_status"):
        return True
    status = rot_engine.get_quota_status(cached_entry, model_key=model_key)
    if status is None:
        return True
    if status.fraction > 0.0:
        return False
    cached_at_val = cached_entry.get("cached_at")
    try:
        c_at = float(cached_at_val)
        if not math.isfinite(c_at) or c_at <= 0 or isinstance(cached_at_val, bool):
            c_at = 0.0
    except (ValueError, TypeError, OverflowError):
        c_at = 0.0

    is_expired = (c_at <= 0.0) or (now - c_at > ttl_seconds) or (now - c_at < -clock_skew_tolerance)
    try:
        curr_dt = datetime.fromtimestamp(now, tz=timezone.utc)
        is_reset_elapsed = status.reset_time <= curr_dt
    except (ValueError, TypeError, OverflowError, OSError):
        is_reset_elapsed = False

    return is_expired or is_reset_elapsed


class QuotaClient:
    """Client for fetching and caching quota telemetry from Google CloudCode API."""

    def __init__(
        self,
        engine_or_client_id: Any = DEFAULT_CLIENT_ID,
        client_secret: str = DEFAULT_CLIENT_SECRET,
        timeout: float = 2.0,
        cache_dir: Path | None = None,
        engine: Any | None = None,
    ) -> None:
        target_engine = engine if engine is not None else (
            engine_or_client_id if hasattr(engine_or_client_id, "cache_dir") else None
        )
        if target_engine is not None:
            self.engine = target_engine
            self.cache_dir = cache_dir or getattr(target_engine, "cache_dir", None) or (Path.home() / ".antigravity-vault" / "cache")
            self.client_id = DEFAULT_CLIENT_ID
        else:
            self.engine = None
            self.client_id = engine_or_client_id
            self.cache_dir = cache_dir or (Path.home() / ".antigravity-vault" / "cache")
        self.client_secret = client_secret
        self.timeout = timeout
        self.token_endpoint = "https://oauth2.googleapis.com/token"
        self.quota_endpoint = "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary"
        self.cache_file = getattr(target_engine, "cache_file", None) or (self.cache_dir / "last_known_quota.json")
        self._local = threading.local()

    @property
    def last_error(self) -> str | None:
        return getattr(self._local, "last_error", None)

    @last_error.setter
    def last_error(self, val: str | None) -> None:
        self._local.last_error = val

    @property
    def last_error_type(self) -> str | None:
        return getattr(self._local, "last_error_type", None)

    @last_error_type.setter
    def last_error_type(self, val: str | None) -> None:
        self._local.last_error_type = val

    def refresh_access_token(self, refresh_token: str) -> dict[str, Any] | None:
        """Refreshes the OAuth access token."""
        if not refresh_token or not isinstance(refresh_token, str) or not refresh_token.strip():
            return None
        data = urllib.parse.urlencode({
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": refresh_token.strip(),
            "grant_type": "refresh_token",
        }).encode("utf-8")

        req = urllib.request.Request(self.token_endpoint, data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
                token_data = json.loads(body)
                if not isinstance(token_data, dict) or "error" in token_data:
                    return None
                raw_token = token_data.get("access_token")
                if not isinstance(raw_token, str) or not raw_token.strip():
                    return None
                raw_exp = token_data.get("expires_in", 3600)
                expires_in = _sanitize_expires_in(raw_exp, default=3600)
                return {
                    "access_token": raw_token.strip(),
                    "expires_in": expires_in,
                }
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as e:
            print(f"[QuotaClient] Failed to refresh token: {e}", file=sys.stderr)
            return None

    def parse_quota_response(self, raw_resp: dict[str, Any]) -> dict[str, Any]:
        """Parses CloudCode response into the standardized Hybrid Quota Schema."""
        if not isinstance(raw_resp, dict):
            return {}
        groups = raw_resp.get("groups")
        if not isinstance(groups, list) or not groups:
            return {}

        result: dict[str, Any] = {}
        for group in groups:
            if not isinstance(group, dict):
                continue
            group_name = group.get("displayName") or group.get("group", "")
            if not isinstance(group_name, str):
                continue

            group_name_lower = group_name.lower()
            if "gemini" in group_name_lower:
                key = "gemini"
            elif "claude" in group_name_lower:
                key = "claude"
            else:
                continue

            buckets_raw = group.get("buckets", [])
            if not isinstance(buckets_raw, list) or not buckets_raw:
                continue

            buckets: dict[str, dict[str, Any]] = {}
            parsed_buckets_list: list[tuple[int, float, int, dict[str, Any]]] = []

            for b in buckets_raw:
                if not isinstance(b, dict):
                    continue
                raw_bucket_id = str(b.get("bucketId") or "").strip().lower()
                if any(x in raw_bucket_id for x in ("five_hour", "five_hours", "5h", "rolling_5h", "5_hour", "5-hour")):
                    norm_id = "5h"
                elif any(x in raw_bucket_id for x in ("weekly", "week", "7d")):
                    norm_id = "weekly"
                else:
                    norm_id = re.sub(r"[^a-z0-9_]", "", raw_bucket_id)

                if not norm_id:
                    continue

                frac = b.get("remainingFraction")
                if frac is None:
                    frac_val = 0.0
                else:
                    try:
                        frac_val = float(frac)
                    except (ValueError, TypeError):
                        frac_val = 0.0
                frac_val = max(0.0, min(1.0, frac_val))

                quota_pct = int(round(frac_val * 100))

                reset_time_str = b.get("resetTime")
                reset_time = 0
                if isinstance(reset_time_str, (int, float)):
                    reset_time = max(0, int(reset_time_str))
                elif isinstance(reset_time_str, str) and reset_time_str:
                    if reset_time_str.isdigit():
                        reset_time = max(0, int(reset_time_str))
                    else:
                        try:
                            rt = reset_time_str.replace("Z", "+00:00")
                            dt = datetime.fromisoformat(rt)
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=timezone.utc)
                            reset_time = max(0, int(dt.timestamp()))
                        except Exception:
                            reset_time = 0

                bucket_info = {
                    "quota": quota_pct,
                    "reset_time": reset_time,
                }
                buckets[norm_id] = bucket_info
                parsed_buckets_list.append((quota_pct, frac_val, reset_time, bucket_info))

            if not parsed_buckets_list:
                continue

            # Deterministic 2-tier comparison for top-level worst-case values:
            # Primary: lowest remainingFraction ascending (raw unrounded float)
            # Secondary: earliest reset_time ascending (break ties by earliest reset timestamp)
            def _worst_key(item: tuple[int, float, int, dict[str, Any]]) -> tuple[float, float]:
                pct, _f_val, r_time, _ = item
                r_key = float(r_time) if r_time > 0 else float("inf")
                return (_f_val, r_key)

            worst_pct, worst_frac, worst_reset, _ = min(parsed_buckets_list, key=_worst_key)

            result[key] = {
                "quota": worst_pct,
                "reset_time": worst_reset,
                "cached": False,
                "buckets": buckets,
            }

        return result

    def fetch_quota(
        self,
        access_token: str | dict[str, Any],
        refresh_token: str | None = None,
        on_refresh: Any | None = None,
    ) -> dict[str, Any]:
        """Fetches quota from the internal CloudCode API."""
        if isinstance(access_token, dict):
            access_token = str(access_token.get("access_token", "")).strip()
        elif isinstance(access_token, str):
            access_token = access_token.strip()
        else:
            access_token = str(access_token or "").strip()

        self.last_error = None
        self.last_error_type = None

        if not access_token:
            return {}

        req = urllib.request.Request(
            self.quota_endpoint,
            data=b'{"project": ""}',
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "User-Agent": "Antigravity/2.5.5",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
                raw_data = json.loads(body)
                return self.parse_quota_response(raw_data)
        except urllib.error.HTTPError as e:
            if e.code == 401 and refresh_token:
                new_token_data = self.refresh_access_token(refresh_token)
                if new_token_data:
                    new_tok_str = new_token_data.get("access_token") if isinstance(new_token_data, dict) else str(new_token_data)
                    if new_tok_str and isinstance(new_tok_str, str) and new_tok_str.strip():
                        if on_refresh:
                            try:
                                on_refresh(new_token_data)
                            except Exception:
                                pass
                        return self.fetch_quota(new_tok_str.strip(), refresh_token=None)
                self.last_error = "Token refresh rejected"
                self.last_error_type = "auth"
            else:
                self.last_error = f"HTTP Error {e.code}: {e.reason}"
                self.last_error_type = "auth" if e.code in (401, 403) else "http"
            print(f"[QuotaClient] HTTP Error {e.code}: {e.reason}", file=sys.stderr)
            return {}
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as e:
            self.last_error = f"Network or Parse Error: {e}"
            self.last_error_type = "network" if isinstance(e, (urllib.error.URLError, TimeoutError)) else "parse"
            print(f"[QuotaClient] Network or Parse Error: {e}", file=sys.stderr)
            return {}

    def load_cache(self) -> dict[str, Any]:
        """Loads cached quota data from vault cache or legacy fallback."""
        cache_path = Path(self.cache_file)
        if cache_path.is_file():
            try:
                return json.loads(cache_path.read_text(encoding="utf-8"))
            except Exception:
                return {}
        legacy_cache = Path.home() / ".gemini" / "config" / "skills" / "agy-quota-checker" / "last_known_quota.json"
        if legacy_cache.is_file():
            try:
                return json.loads(legacy_cache.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def save_cache(
        self,
        data: dict[str, Any],
        merge: bool = True,
        evict_keys: list[str] | set[str] | None = None,
    ) -> None:
        """Atomically saves quota cache to vault cache directory protected by threading.RLock.

        Guarantees:
        1. Thread-safe execution under concurrent worker threads.
        2. Top-level 'timestamp' field is always present, defaulting to time.time()
           if absent, boolean, non-positive, or invalid in incoming data.
        3. When merge=True and data contains an 'accounts' dict, existing valid
           standby accounts and their respective 'cached_at' timestamps are preserved.
        4. For valid incoming accounts lacking 'cached_at', 'cached_at' is automatically
           set to root_ts, ensuring independent freshness tracking across all callers.
        5. Invalid or error-laden accounts in incoming data are filtered out
           without corrupting existing valid cache entries. Errored model sub-keys
           (e.g. claude: {"error": ...}) are stripped while preserving healthy models.
        6. Permissions: cache directory is 0700 and cache file is 0600.
        7. Cross-mount resilience: temporary files are created in cache_file.parent,
           preventing EXDEV errors when cache_file is customized.
        """
        with _CACHE_LOCK:
            cache_target_file = Path(self.cache_file)
            target_dir = cache_target_file.parent
            target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                os.chmod(target_dir, 0o700)
            except OSError:
                pass
            if self.cache_dir != target_dir:
                self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                try:
                    os.chmod(self.cache_dir, 0o700)
                except OSError:
                    pass

            if not isinstance(data, dict):
                data_to_write = {
                    "timestamp": time.time(),
                    "accounts": {},
                }
            else:
                # Determine root timestamp: default to time.time() if absent, bool, or invalid
                root_ts_val = data.get("timestamp")
                if root_ts_val is None or isinstance(root_ts_val, bool):
                    root_ts = time.time()
                elif isinstance(root_ts_val, (int, float)):
                    root_ts = float(root_ts_val) if math.isfinite(root_ts_val) and root_ts_val > 0 else time.time()
                else:
                    try:
                        parsed_ts = float(root_ts_val)
                        root_ts = parsed_ts if math.isfinite(parsed_ts) and parsed_ts > 0 else time.time()
                    except (ValueError, TypeError, OverflowError):
                        root_ts = time.time()

                def _sanitize_account_entry(acc_val: Any) -> dict[str, Any] | None:
                    if not isinstance(acc_val, dict) or not acc_val or acc_val.get("error"):
                        return None
                    entry = dict(acc_val)
                    # Strip errored or invalid model sub-keys (e.g. claude: {"error": ...})
                    for m_key in ("gemini", "claude"):
                        if m_key in entry:
                            m_val = entry[m_key]
                            if not isinstance(m_val, dict) or m_val.get("error"):
                                del entry[m_key]

                    # Strip any other sub-dict containing an error key
                    for k in list(entry.keys()):
                        if k not in ("cached_at", "timestamp"):
                            v = entry[k]
                            if isinstance(v, dict) and v.get("error"):
                                del entry[k]

                    # Must contain at least one valid model container or valid quota payload
                    has_model = isinstance(entry.get("gemini"), dict) or isinstance(entry.get("claude"), dict)
                    has_quota = "quota" in entry or "groups" in entry
                    if not (has_model or has_quota):
                        return None

                    # Normalize cached_at timestamp
                    cached_at_val = entry.get("cached_at")
                    if cached_at_val is None or isinstance(cached_at_val, bool):
                        entry["cached_at"] = root_ts
                    elif isinstance(cached_at_val, (int, float)):
                        if not math.isfinite(cached_at_val) or cached_at_val <= 0:
                            entry["cached_at"] = root_ts
                    else:
                        try:
                            p_cat = float(cached_at_val)
                            entry["cached_at"] = p_cat if math.isfinite(p_cat) and p_cat > 0 else root_ts
                        except (ValueError, TypeError, OverflowError):
                            entry["cached_at"] = root_ts
                    return entry

                def _extract_valid_accounts(accounts_map: Any) -> dict[str, Any]:
                    valid: dict[str, Any] = {}
                    if isinstance(accounts_map, dict):
                        for k, v in accounts_map.items():
                            if isinstance(k, str) and k.strip():
                                sanitized = _sanitize_account_entry(v)
                                if sanitized is not None:
                                    valid[k.strip()] = sanitized
                    return valid

                incoming_valid = _extract_valid_accounts(data.get("accounts"))

                if merge:
                    current = self.load_cache()
                    merged_accounts: dict[str, Any] = (
                        _extract_valid_accounts(current.get("accounts"))
                        if isinstance(current, dict)
                        else {}
                    )
                    for acc_name, incoming_acc in incoming_valid.items():
                        if acc_name in merged_accounts:
                            merged_entry = dict(merged_accounts[acc_name])
                            merged_entry.update(incoming_acc)
                            merged_accounts[acc_name] = merged_entry
                        else:
                            merged_accounts[acc_name] = incoming_acc
                    if evict_keys:
                        for ev in evict_keys:
                            if isinstance(ev, str) and ev.strip() in merged_accounts:
                                del merged_accounts[ev.strip()]
                    merged_data = dict(current) if isinstance(current, dict) else {}
                    for k, v in data.items():
                        if k != "accounts":
                            merged_data[k] = v
                    merged_data["accounts"] = merged_accounts
                    merged_data["timestamp"] = root_ts
                    data_to_write = merged_data
                else:
                    data_to_write = {k: v for k, v in data.items() if k != "accounts"}
                    if evict_keys:
                        for ev in evict_keys:
                            if isinstance(ev, str) and ev.strip() in incoming_valid:
                                del incoming_valid[ev.strip()]
                    data_to_write["accounts"] = incoming_valid
                    data_to_write["timestamp"] = root_ts

            tmp_file = None
            try:
                tmp_fd, tmp_file = tempfile.mkstemp(dir=target_dir, prefix="quota-cache-", suffix=".tmp")
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                    json.dump(data_to_write, f, indent=2)
                os.chmod(tmp_file, 0o600)
                os.replace(tmp_file, cache_target_file)
                try:
                    os.chmod(cache_target_file, 0o600)
                except OSError:
                    pass
                tmp_file = None
            except Exception as e:
                print(f"[QuotaClient] Failed to persist quota cache to {self.cache_file}: {e}", file=sys.stderr)
            finally:
                if tmp_file and os.path.exists(tmp_file):
                    try:
                        os.unlink(tmp_file)
                    except OSError:
                        pass


def fetch_account_quota(
    engine: Any,
    account: dict[str, Any] | str,
    client: QuotaClient | None = None,
) -> tuple[str, dict[str, Any]]:
    """Fetches quota for a single account from CloudCode API.

    Returns a tuple of (account_name, quota_dict).
    """
    if isinstance(account, dict):
        acc_name = account.get("name", "")
    else:
        acc_name = str(account)

    if not acc_name:
        return "", {"error": "Invalid account name", "error_type": "validation"}

    if client is None:
        client = QuotaClient(timeout=2.0, engine=engine)

    tokens = engine.get_account_tokens(acc_name) if hasattr(engine, "get_account_tokens") else None
    if not tokens or not tokens.get("access_token"):
        return acc_name, {"error": "No token", "error_type": "auth"}

    def _on_token_refreshed(new_token_data: dict[str, Any] | str) -> None:
        if isinstance(new_token_data, dict):
            access_tok = str(new_token_data.get("access_token", "")).strip()
            if not access_tok:
                return
            raw_exp = new_token_data.get("expires_in", 3600)
            exp_sec = _sanitize_expires_in(raw_exp, default=3600)
            expiry_iso = (datetime.now(timezone.utc) + timedelta(seconds=exp_sec)).strftime("%Y-%m-%dT%H:%M:%SZ")
            if hasattr(engine, "update_account_access_token"):
                engine.update_account_access_token(acc_name, access_tok, expiry=expiry_iso)
        else:
            if hasattr(engine, "update_account_access_token"):
                engine.update_account_access_token(acc_name, str(new_token_data))

    try:
        try:
            quota_res = client.fetch_quota(
                tokens["access_token"],
                refresh_token=tokens.get("refresh_token"),
                on_refresh=_on_token_refreshed,
            )
        except TypeError as te:
            if "on_refresh" in str(te):
                quota_res = client.fetch_quota(
                    tokens["access_token"],
                    refresh_token=tokens.get("refresh_token"),
                )
            else:
                raise
        if quota_res:
            return acc_name, quota_res
        raw_err = getattr(client, "last_error", None)
        err_msg = raw_err if isinstance(raw_err, str) and raw_err else "Fetch failed"
        raw_type = getattr(client, "last_error_type", None)
        if raw_type == "http" and bool(HTTP_SERVER_ERROR_PATTERN.search(err_msg)):
            err_type = "network"
        elif isinstance(raw_type, str) and raw_type:
            err_type = raw_type
        else:
            err_type = "auth" if _has_auth_indicator(err_msg) else "network"
        return acc_name, {"error": err_msg, "error_type": err_type}
    except Exception as e:
        err_str = str(e)
        err_type = "network" if _is_network_error(e) else ("auth" if _is_auth_error(e) else "general")
        return acc_name, {"error": err_str, "error_type": err_type}


def fetch_all_accounts_quota(
    engine: Any,
    accounts: list[dict[str, Any]],
    client: QuotaClient | None = None,
    max_workers: int = 5,
) -> dict[str, dict[str, Any]]:
    """Concurrently fetches quota for multiple accounts using ThreadPoolExecutor."""
    if client is None:
        client = QuotaClient(timeout=2.0, engine=engine)

    if not accounts:
        return {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(fetch_account_quota, engine, acc, client)
            for acc in accounts
        ]
        return dict(f.result() for f in concurrent.futures.as_completed(futures))


def sort_accounts_by_quota(
    accounts: list[dict[str, Any]] | None,
    target_model: str = "gemini",
) -> list[dict[str, Any]]:
    """Sorts accounts deterministically using a 3-tier tie-breaking key:

    1. Primary: -quota_val (highest available quota percentage first for target_model).
    2. Secondary: reset_time (earliest reset timestamp first; accounts with reset time <= 0 or None map to float("inf")).
    3. Tertiary: account name (alphabetical ascending for stable sorting).

    Defensively handles missing, non-dict, or error quota payloads without crashing.
    """
    if not accounts:
        return []

    try:
        model_key = normalize_model_name(target_model)
    except Exception:
        model_key = "gemini"

    def _sort_key(acc: dict[str, Any]) -> tuple[int | float, float, str]:
        if not isinstance(acc, dict):
            return (1, float("inf"), "")

        q_root = acc.get("quota") if isinstance(acc.get("quota"), dict) else {}
        q_data = q_root.get(model_key) if isinstance(q_root.get(model_key), dict) else {}

        quota_val: int | float = -1
        reset_time: float = float("inf")

        if isinstance(q_data, dict):
            quota_val = _parse_quota_defensive(q_data.get("quota"))
            reset_time = _parse_timestamp_defensive(q_data.get("reset_time"))

        name = str(acc.get("name") or "")
        return (-quota_val, reset_time, name)

    return sorted(accounts, key=_sort_key)


def run_quota_pipeline(
    engine: Any,
    data: dict[str, Any] | None,
    client: QuotaClient | None = None,
) -> dict[str, Any]:
    """Executes the standard quota telemetry pipeline:

    1. Fetches quotas concurrently for all accounts via fetch_all_accounts_quota.
    2. Enforces Cache Merge Invariant: merges newly fetched valid account data into last_known_quota.json.
    3. For accounts failing network fetch, loads last known cached telemetry if present and marks cached=True.
       Accounts without cache are marked as Error / Unreachable.
    4. Sorts accounts deterministically using sort_accounts_by_quota.
    """
    if not isinstance(data, dict):
        return {"active": None, "accounts": []}

    if client is None:
        client = QuotaClient(timeout=2.0, engine=engine)

    raw_accounts = data.get("accounts", [])
    if not isinstance(raw_accounts, list) or not raw_accounts:
        return data

    # Defensive copy of accounts to avoid in-place mutation of caller data
    accounts = [dict(acc) for acc in raw_accounts if isinstance(acc, dict)]

    # 1. Concurrently fetch quotas
    raw_quotas = fetch_all_accounts_quota(engine, accounts, client=client)

    # 2. Extract valid quotas and merge into cache
    valid_quotas: dict[str, Any] = {}
    for name, q in raw_quotas.items():
        if isinstance(q, dict) and not q.get("error") and q.get("gemini"):
            valid_quotas[name] = q

    if valid_quotas:
        client.save_cache({
            "timestamp": datetime.now(timezone.utc).timestamp(),
            "accounts": valid_quotas,
        }, merge=True)

    # Reload merged cache to get all historical + new entries
    cache_data = client.load_cache()
    cached_accounts = (
        cache_data.get("accounts", {})
        if isinstance(cache_data, dict) and isinstance(cache_data.get("accounts"), dict)
        else {}
    )

    # 3. Apply quota / cache fallback per account
    for acc in accounts:
        acc_name = acc.get("name", "")
        live_q = raw_quotas.get(acc_name)

        if isinstance(live_q, dict) and not live_q.get("error") and live_q.get("gemini"):
            # Live telemetry succeeded: sanitize to strictly {"gemini": gemini_copy}
            gemini_copy = dict(live_q["gemini"])
            gemini_copy["cached"] = False
            acc["quota"] = {"gemini": gemini_copy}
        else:
            # Live fetch failed or error returned: check cache
            cached_acc = cached_accounts.get(acc_name)
            has_valid_gemini_cache = (
                isinstance(cached_acc, dict)
                and isinstance(cached_acc.get("gemini"), dict)
                and not cached_acc["gemini"].get("error")
                and "quota" in cached_acc["gemini"]
            )
            has_legacy_cache = (
                isinstance(cached_acc, dict)
                and "quota" in cached_acc
                and not cached_acc.get("error")
            )

            if has_valid_gemini_cache:
                gemini_copy = dict(cached_acc["gemini"])
                gemini_copy["cached"] = True
                acc["quota"] = {"gemini": gemini_copy}
            elif has_legacy_cache:
                gemini_copy = {
                    "quota": cached_acc.get("quota", 0),
                    "reset_time": cached_acc.get("reset_time", 0),
                    "cached": True,
                    "buckets": {},
                }
                acc["quota"] = {"gemini": gemini_copy}
            else:
                # No cache available: mark as Unreachable
                acc["quota"] = {
                    "gemini": None,
                    "error": "Unreachable",
                }

    # 4. Sort accounts deterministically
    result = dict(data)
    result["accounts"] = sort_accounts_by_quota(accounts)
    return result
