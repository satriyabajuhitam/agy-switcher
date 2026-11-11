"""In-Process SDK Facade for agy-switch.

Layer 4 Presentation module: Provides programmatic access for Python consumers
(e.g., agy-telemote TurnRunner) to inspect accounts, query quota telemetry,
and perform Disk-Only Swap failovers without subprocess execution overhead.

Zero external dependencies. Pure Python 3.10+ standard library.
Layer 4 DAG: May import Layer 0 (exceptions, models, security), Layer 1 (telemetry, supervisor),
and Layer 2 (vault, rotation).
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
import json
import math
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any

from agy_switcher.exceptions import AccountNotFoundError, ActiveTurnRunningError, AgySwitchError
from agy_switcher.formatters import (
    format_fleet_exhaustion_message,
    format_fleet_exhaustion_notice,
)
from agy_switcher.models import AccountInfo, RotationResult, FleetQuotaSummary
from agy_switcher.rotation import RotationEngine
from agy_switcher.security import extract_email_from_jwt
from agy_switcher.telemetry import normalize_model_name
from agy_switcher.vault import AgySwitchEngine


def _sanitize_reset_time_and_delta(val: object, current_ts: float) -> tuple[object, int | None]:
    """Sanitizes reset_time and computes remaining minutes relative to current_ts.

    Returns (sanitized_reset_time, remaining_minutes or None).
    If reset_time is missing, None, non-numeric unparseable, or <= 0, returns (None, None).
    If reset_time is in the past (expired), returns (val_f, None).
    """
    if val is None or isinstance(val, bool):
        return (None, None)

    if isinstance(val, (int, float)):
        if not math.isfinite(val) or val <= 0 or val > 32503680000.0:
            return (None, None)
        val_f = float(val)
        delta_seconds = val_f - current_ts
        if delta_seconds > 0:
            return (val_f, max(1, int(round(delta_seconds / 60))))
        return (val_f, None)

    if isinstance(val, datetime):
        dt = val.astimezone(timezone.utc) if val.tzinfo else val.replace(tzinfo=timezone.utc)
        if dt.year >= 9000:
            return (None, None)
        val_f = dt.timestamp()
        if not math.isfinite(val_f) or val_f <= 0 or val_f > 32503680000.0:
            return (None, None)
        delta_seconds = val_f - current_ts
        if delta_seconds > 0:
            return (val_f, max(1, int(round(delta_seconds / 60))))
        return (val_f, None)

    if isinstance(val, str):
        cleaned = val.strip()
        if not cleaned or cleaned.lower() in ("none", "null", "-", "undefined"):
            return (None, None)
        if "t" in cleaned.lower():
            try:
                dt = datetime.fromisoformat(cleaned.replace("Z", "+00:00")).astimezone(timezone.utc)
                if dt.year >= 9000:
                    return (None, None)
                val_f = dt.timestamp()
                if not math.isfinite(val_f) or val_f <= 0 or val_f > 32503680000.0:
                    return (None, None)
                delta_seconds = val_f - current_ts
                if delta_seconds > 0:
                    return (val_f, max(1, int(round(delta_seconds / 60))))
                return (val_f, None)
            except Exception:
                pass
        try:
            val_f = float(cleaned)
            if not math.isfinite(val_f) or val_f <= 0 or val_f > 32503680000.0:
                return (None, None)
            delta_seconds = val_f - current_ts
            if delta_seconds > 0:
                return (val_f, max(1, int(round(delta_seconds / 60))))
            return (val_f, None)
        except ValueError:
            pass

        # Check "14:30" or "14:30 UTC"
        time_match = re.search(r"\b(\d{1,2}):(\d{2})\b", cleaned)
        if time_match:
            try:
                th = int(time_match.group(1))
                tm = int(time_match.group(2))
                if 0 <= th <= 23 and 0 <= tm <= 59:
                    curr_dt = datetime.fromtimestamp(current_ts, tz=timezone.utc)
                    reset_dt = curr_dt.replace(hour=th, minute=tm, second=0, microsecond=0)
                    if reset_dt < curr_dt:
                        reset_dt += timedelta(days=1)
                    delta_seconds = (reset_dt - curr_dt).total_seconds()
                    delta_m = max(1, int(round(delta_seconds / 60))) if delta_seconds > 0 else None
                    return (cleaned, delta_m)
            except Exception:
                pass

    return (None, None)


_ROTATION_LOCK = threading.RLock()
_LAST_ROTATION_REGISTRY: dict[tuple[str, str], dict[str, Any]] = {}

FALLBACK_MISSING_SWITCHER_MESSAGE = (
    "Quota limit reached (RESOURCE_EXHAUSTED). "
    "Manual re-login or installing 'agy-switcher' is required."
)


def get_active_account(engine: AgySwitchEngine | None = None) -> AccountInfo:
    """Returns the currently active account metadata, or empty AccountInfo if none configured."""
    try:
        if engine is None:
            engine = AgySwitchEngine()
        active_name = engine.get_active_name()
    except Exception:
        return AccountInfo(name="", email=None, is_active=False, token_path=None, status="none")

    if not active_name or not active_name.strip():
        return AccountInfo(name="", email=None, is_active=False, token_path=None, status="none")

    clean_name = active_name.strip()
    if "/" in clean_name or "\\" in clean_name or ".." in clean_name or clean_name in (".", ".."):
        return AccountInfo(name="", email=None, is_active=False, token_path=None, status="none")

    try:
        acc_dir = engine.accounts_dir / clean_name
        if (
            not acc_dir.is_dir()
            or acc_dir.is_symlink()
            or acc_dir.resolve().parent != engine.accounts_dir.resolve()
        ):
            # Dangling active pointer, missing directory, or path traversal attempt
            return AccountInfo(name="", email=None, is_active=False, token_path=None, status="none")
    except Exception:
        return AccountInfo(name="", email=None, is_active=False, token_path=None, status="none")

    token_path = acc_dir / "token.json"
    try:
        if not token_path.is_file():
            return AccountInfo(name=clean_name, email=None, is_active=False, token_path=token_path, status="corrupt")

        raw = token_path.read_text(encoding="utf-8")
        token_data = json.loads(raw)
        engine.validate_token_dict(token_data)
        raw_email = extract_email_from_jwt(token_data.get("id_token", ""))
        email = raw_email if (raw_email and raw_email.strip()) else None
        return AccountInfo(
            name=clean_name,
            email=email,
            is_active=True,
            token_path=token_path,
            status="active",
        )
    except Exception:
        return AccountInfo(
            name=clean_name,
            email=None,
            is_active=False,
            token_path=token_path,
            status="corrupt",
        )


def handle_soft_import_rotation(
    model: str = "gemini",
    reload_server: bool = False,
    engine: AgySwitchEngine | None = None,
    current_account: str | None = None,
) -> tuple[bool, RotationResult | None, str]:
    """Gracefully executes rotation via soft-import, handling missing agy-switcher without crashes.

    Encapsulates dynamic import of agy_switcher.api. If agy_switcher is absent from the
    runtime environment (ImportError / ModuleNotFoundError), catches the error cleanly
    and returns actionable fallback guidance.

    Returns:
        tuple[bool, RotationResult | None, str]: (success, rotation_result, message)
    """
    try:
        import importlib
        switcher_api = importlib.import_module("agy_switcher.api")
        if not hasattr(switcher_api, "auto_rotate_on_exhaustion"):
            return (False, None, FALLBACK_MISSING_SWITCHER_MESSAGE)
        res = switcher_api.auto_rotate_on_exhaustion(
            model=model,
            reload_server=reload_server,
            engine=engine,
            current_account=current_account,
        )
        return (res.success, res, res.message)
    except (ImportError, ModuleNotFoundError):
        return (False, None, FALLBACK_MISSING_SWITCHER_MESSAGE)
    except Exception as exc:
        return (False, None, f"Rotation error: {exc}")


def auto_rotate_on_exhaustion(
    model: str = "gemini",
    reload_server: bool = False,
    engine: AgySwitchEngine | None = None,
    current_account: str | None = None,
) -> RotationResult:
    """Rotates to the optimal account with remaining quota for target model using Disk-Only Swap."""
    if model and isinstance(model, str) and any(k in model.lower() for k in ("claude", "anthropic")):
        raise AgySwitchError(
            f"Non-Gemini model '{model}' is retired and unsupported. Allowed model is strictly 'gemini'."
        )
    try:
        model_key = normalize_model_name(model)
    except Exception:
        return RotationResult(
            success=False,
            previous_account=None,
            new_account=None,
            model=model,
            message=f"Unsupported model family '{model}'.",
            quota_remaining=None,
            reloaded_server=False,
            active_account=None,
        )

    with _ROTATION_LOCK:
        if engine is None:
            try:
                engine = AgySwitchEngine()
            except Exception as exc:
                return RotationResult(
                    success=False,
                    previous_account=None,
                    new_account=None,
                    model=model,
                    message=f"Failed to initialize vault engine: {exc}",
                    quota_remaining=None,
                    reloaded_server=False,
                    active_account=None,
                )

        try:
            vault_key = str(engine.vault_dir.resolve())
        except Exception:
            vault_key = str(id(engine))

        reg_key = (vault_key, model_key)

        try:
            active_account = engine.get_active_name()
            if active_account:
                active_account = active_account.strip()
            if not active_account:
                active_account = None
        except Exception:
            active_account = None

        now = time.time()
        # Clean up stale registry entries older than 60 seconds
        for k in list(_LAST_ROTATION_REGISTRY.keys()):
            if now - float(_LAST_ROTATION_REGISTRY[k].get("timestamp", 0.0)) > 60.0:
                _LAST_ROTATION_REGISTRY.pop(k, None)

        last_rot = _LAST_ROTATION_REGISTRY.get(reg_key)

        # Anti-double-hopping check:
        # If a recent rotation for this vault and model succeeded within a short debounce window (2.0s)
        # and the vault's active account matches the newly rotated account:
        if (
            last_rot is not None
            and (now - float(last_rot.get("timestamp", 0.0)) < 2.0)
            and last_rot.get("model") == model_key
            and isinstance(last_rot.get("result"), RotationResult)
            and last_rot["result"].success
            and active_account == last_rot.get("new_account")
        ):
            if current_account is None or current_account == last_rot.get("previous_account"):
                return last_rot["result"]

        # Explicit current_account guard: if the caller's turn ran on current_account, but the active
        # account has already transitioned away:
        if current_account and active_account and active_account != current_account:
            # Case A: A recent rotation for this specific model already rotated to active_account
            if (
                last_rot is not None
                and last_rot.get("model") == model_key
                and last_rot.get("new_account") == active_account
                and isinstance(last_rot.get("result"), RotationResult)
                and last_rot["result"].success
            ):
                if last_rot.get("previous_account") == current_account:
                    return last_rot["result"]
                res = last_rot["result"]
                return RotationResult(
                    success=True,
                    previous_account=current_account,
                    new_account=active_account,
                    model=model,
                    message=f"Account already rotated from '{current_account}' to '{active_account}' for model '{model}'.",
                    quota_remaining=res.quota_remaining,
                    reloaded_server=False,
                    active_account=active_account,
                )

            # Case B: active_account was changed (e.g. by another model rotation).
            # Inspect whether active_account actually has available quota for this target model.
            rot_engine_probe = RotationEngine(
                vault_dir=engine.vault_dir,
                active_account_id=active_account,
                target_model=model_key,
            )
            probe_cache = rot_engine_probe._load_quota_cache()
            probe_accounts_map = (
                probe_cache.get("accounts", {})
                if isinstance(probe_cache, dict) and isinstance(probe_cache.get("accounts"), dict)
                else (probe_cache if isinstance(probe_cache, dict) else {})
            )
            active_acc_quota = probe_accounts_map.get(active_account, {}) if isinstance(probe_accounts_map, dict) else {}
            active_status = rot_engine_probe.get_quota_status(active_acc_quota, model_key=model_key)
            active_quota_rem = active_status.fraction if active_status is not None else None

            # If active_account has available quota (> 0.0) for target model, retain it safely
            if active_quota_rem is not None and active_quota_rem > 0:
                return RotationResult(
                    success=True,
                    previous_account=current_account,
                    new_account=active_account,
                    model=model,
                    message=f"Account already transitioned from '{current_account}' to '{active_account}'.",
                    quota_remaining=active_quota_rem,
                    reloaded_server=False,
                    active_account=active_account,
                )
            # If active_account is also exhausted for model_key, fall through to rotate to a healthy account!

        previous_account = active_account

        try:
            accounts_data = engine.list_accounts()
        except Exception:
            accounts_data = {"active": previous_account, "accounts": []}

        accounts_list = accounts_data.get("accounts") or []
        valid_accounts = [acc for acc in accounts_list if isinstance(acc, dict) and acc.get("valid", True)]

        try:
            cooldowns = dict(engine.get_cooldowns())
        except Exception:
            cooldowns = {}

        if current_account:
            cooldowns[current_account] = {"reason": "RESOURCE_EXHAUSTED", "model": model_key}
        if active_account:
            if active_account != current_account:
                cooldowns[active_account] = {"reason": "RESOURCE_EXHAUSTED", "model": model_key}
        elif previous_account:
            cooldowns[previous_account] = {"reason": "RESOURCE_EXHAUSTED", "model": model_key}

        rot_engine = RotationEngine(
            vault_dir=engine.vault_dir,
            active_account_id=previous_account,
            target_model=model_key,
        )
        try:
            target_account = rot_engine.evaluate(
                valid_accounts,
                current_active=previous_account,
                target_model=model_key,
                cooldowns=cooldowns,
            )
        except Exception as exc:
            return RotationResult(
                success=False,
                previous_account=current_account or previous_account,
                new_account=None,
                model=model,
                message=f"Rotation evaluation failed: {exc}",
                quota_remaining=None,
                reloaded_server=False,
                active_account=previous_account,
            )

        if not target_account or target_account == previous_account:
            return RotationResult(
                success=False,
                previous_account=current_account or previous_account,
                new_account=None,
                model=model,
                message=f"All accounts exhausted for model '{model}'.",
                quota_remaining=None,
                reloaded_server=False,
                active_account=None,
            )

        if reload_server:
            pid = engine.locate_language_server_pid()
            if engine.is_server_busy(pid):
                return RotationResult(
                    success=False,
                    previous_account=current_account or previous_account,
                    new_account=None,
                    model=model,
                    message=(
                        "Antigravity is currently busy processing an agent turn or background task. "
                        "Cannot reload language server during active turn."
                    ),
                    quota_remaining=None,
                    reloaded_server=False,
                    active_account=previous_account,
                )

        try:
            switch_res = engine.switch_tokens(target_account, restart_server=reload_server, force=False)
            server_reloaded = bool(switch_res.get("server_restarted", False)) if reload_server else False
        except Exception as exc:
            return RotationResult(
                success=False,
                previous_account=current_account or previous_account,
                new_account=None,
                model=model,
                message=f"Failed to switch account to '{target_account}': {exc}",
                quota_remaining=None,
                reloaded_server=False,
                active_account=previous_account,
            )

        target_quota_cache = rot_engine._load_quota_cache()
        if isinstance(target_quota_cache, dict):
            target_accounts_map = (
                target_quota_cache.get("accounts", {})
                if isinstance(target_quota_cache.get("accounts"), dict)
                else target_quota_cache
            )
        else:
            target_accounts_map = {}

        target_acc_quota = target_accounts_map.get(target_account, {}) if isinstance(target_accounts_map, dict) else {}
        target_status = rot_engine.get_quota_status(target_acc_quota, model_key=model_key)
        quota_rem = target_status.fraction if target_status is not None else None

        rotation_res = RotationResult(
            success=True,
            previous_account=current_account or previous_account,
            new_account=target_account,
            model=model,
            message=f"Successfully rotated to '{target_account}' for model '{model}'.",
            quota_remaining=quota_rem,
            reloaded_server=server_reloaded,
            active_account=target_account,
        )

        _LAST_ROTATION_REGISTRY[reg_key] = {
            "timestamp": now,
            "model": model_key,
            "previous_account": current_account or previous_account,
            "new_account": target_account,
            "result": rotation_res,
        }

        return rotation_res


def switch_account(
    name: str,
    reload_server: bool = False,
    engine: AgySwitchEngine | None = None,
) -> bool:
    """Switches active credentials to target account. Defaults to Disk-Only Swap (reload_server=False).

    Args:
        name: Name/alias of the target account in vault.
        reload_server: If True, restarts language_server after switching tokens.
                       Enforces presence safety checks and raises ActiveTurnRunningError
                       if a turn is active. Defaults to False (Disk-Only Swap).
        engine: Optional AgySwitchEngine instance for dependency injection.

    Returns:
        True if the switch completed successfully.

    Raises:
        AccountNotFoundError: If the requested account does not exist in the vault.
        ActiveTurnRunningError: If reload_server is True and a turn/presence lock is active.
        InvalidTokenError: If the account token is malformed.
    """
    if engine is None:
        engine = AgySwitchEngine()

    if (
        not name
        or not isinstance(name, str)
        or not name.strip()
        or "/" in name
        or "\\" in name
        or ".." in name
        or name.strip() in (".", "..")
    ):
        raise AccountNotFoundError(f"Account '{name}' not found in vault ({engine.accounts_dir}).")

    clean_name = name.strip()
    acc_dir = engine.accounts_dir / clean_name
    token_file = acc_dir / "token.json"

    try:
        if (
            not acc_dir.is_dir()
            or acc_dir.is_symlink()
            or not token_file.is_file()
            or acc_dir.resolve().parent != engine.accounts_dir.resolve()
        ):
            available = sorted(
                p.name for p in engine.accounts_dir.iterdir()
                if p.is_dir() and not p.name.startswith(".")
            ) if engine.accounts_dir.is_dir() else []
            raise AccountNotFoundError(
                f"Account '{name}' not found in vault ({engine.accounts_dir}). "
                f"Available accounts: {', '.join(available) or 'None'}"
            )
    except OSError:
        raise AccountNotFoundError(f"Account '{name}' not found in vault ({engine.accounts_dir}).")

    if reload_server:
        pid = engine.locate_language_server_pid()
        if engine.is_server_busy(pid):
            raise ActiveTurnRunningError(
                "Antigravity is currently busy processing an agent turn or background task. "
                "Cannot reload language server during active turn."
            )

    engine.switch_tokens(name=clean_name, restart_server=reload_server, force=False)
    return True


def list_accounts(engine: AgySwitchEngine | None = None) -> dict[str, object]:
    """Returns all registered vault accounts and active account pointer."""
    if engine is None:
        try:
            engine = AgySwitchEngine()
        except Exception:
            return {"active": None, "accounts": []}
    return engine.list_accounts()


def get_accounts(engine: AgySwitchEngine | None = None) -> list[AccountInfo]:
    """Returns a list of all registered vault accounts as AccountInfo objects."""
    res = list_accounts(engine=engine)
    active_name = res.get("active")
    raw_accounts = res.get("accounts", [])
    result: list[AccountInfo] = []
    if isinstance(raw_accounts, list):
        for acc in raw_accounts:
            if isinstance(acc, dict):
                name = str(acc.get("name", "")).strip()
                if not name:
                    continue
                email = acc.get("email")
                is_act = bool(acc.get("is_active", False) or (name and name == active_name))
                token_path = acc.get("token_path")
                status = "active" if is_act else "standby"
                result.append(
                    AccountInfo(
                        name=name,
                        email=email,
                        is_active=is_act,
                        token_path=token_path,
                        status=status,
                    )
                )
            elif isinstance(acc, str) and acc.strip():
                name = acc.strip()
                is_act = (name == active_name)
                result.append(
                    AccountInfo(
                        name=name,
                        is_active=is_act,
                        status="active" if is_act else "standby",
                    )
                )
    return result


def get_fleet_quota_summary(
    model: str = "all",
    engine: AgySwitchEngine | None = None,
    refresh: bool = False,
) -> FleetQuotaSummary:
    """Returns aggregated quota telemetry for all registered vault accounts.

    Queries cached quota telemetry without blocking and handles offline state gracefully.
    Preserves cache timestamp from last_known_quota.json.

    Args:
        model: Model family to filter ("all", "gemini", "claude"). Defaults to "all".
        engine: Optional AgySwitchEngine instance for dependency injection.
        refresh: If True, attempts to trigger background quota fetch before reading cache.

    Returns:
        FleetQuotaSummary dataclass containing fleet capacity and quota distributions.
    """
    resolved_model = model.strip() if (isinstance(model, str) and model.strip()) else "all"
    if resolved_model.lower() == "all":
        target_model_key: str | None = None
        effective_model = "all"
    else:
        try:
            target_model_key = normalize_model_name(resolved_model)
            effective_model = target_model_key
        except Exception:
            target_model_key = resolved_model.lower()
            effective_model = resolved_model

    if engine is None:
        try:
            engine = AgySwitchEngine()
        except Exception:
            return FleetQuotaSummary(
                total_accounts=0,
                active_account=None,
                accounts={},
                model=effective_model,
                timestamp=time.time(),
            )

    active_name: str | None = None
    try:
        raw_active = engine.get_active_name()
        if raw_active and isinstance(raw_active, str) and raw_active.strip():
            clean_active = raw_active.strip()
            if not ("/" in clean_active or "\\" in clean_active or ".." in clean_active or clean_active in (".", "..")):
                acc_dir = engine.accounts_dir / clean_active
                if (
                    acc_dir.is_dir()
                    and not acc_dir.is_symlink()
                    and acc_dir.resolve().parent == engine.accounts_dir.resolve()
                ):
                    active_name = clean_active
    except Exception:
        active_name = None

    registered_names: list[str] = []
    try:
        accounts_data = engine.list_accounts()
        for acc in accounts_data.get("accounts", []):
            if isinstance(acc, dict) and "name" in acc:
                name_str = str(acc["name"]).strip()
                if name_str and name_str not in registered_names:
                    registered_names.append(name_str)
    except Exception:
        registered_names = []

    if refresh:
        try:
            from agy_switcher.telemetry import run_quota_pipeline
            target_m = target_model_key if target_model_key else "gemini"
            run_quota_pipeline(engine=engine, data=engine.list_accounts(), target_model=target_m)
        except Exception:
            pass

    cache_data: dict[str, Any] = {}
    cache_file = getattr(engine, "cache_file", None)
    if not cache_file and hasattr(engine, "vault_dir"):
        cache_file = engine.vault_dir / "cache" / "last_known_quota.json"

    if cache_file and isinstance(cache_file, Path) and cache_file.is_file():
        try:
            raw_content = cache_file.read_text(encoding="utf-8")
            if not raw_content.strip():
                print(f"[agy_switcher.api] Warning: Quota cache file {cache_file} is empty", file=sys.stderr)
                cache_data = {}
            else:
                parsed = json.loads(raw_content)
                if isinstance(parsed, dict):
                    cache_data = parsed
                else:
                    print(f"[agy_switcher.api] Warning: Quota cache file {cache_file} does not contain a JSON object", file=sys.stderr)
                    cache_data = {}
        except Exception as exc:
            print(f"[agy_switcher.api] Warning: Failed to parse quota cache file {cache_file} (corrupt JSON): {exc}", file=sys.stderr)
            cache_data = {}

    if not cache_data:
        try:
            from agy_switcher.telemetry import QuotaClient
            client = QuotaClient(engine=engine)
            loaded = client.load_cache()
            if isinstance(loaded, dict):
                cache_data = loaded
        except Exception:
            pass

    raw_ts = cache_data.get("timestamp") if isinstance(cache_data, dict) else None
    timestamp: float
    if isinstance(raw_ts, (int, float)) and not isinstance(raw_ts, bool):
        timestamp = float(raw_ts) if (math.isfinite(raw_ts) and raw_ts > 0) else time.time()
    else:
        try:
            parsed_ts = float(raw_ts) if raw_ts is not None else 0.0
            timestamp = parsed_ts if (math.isfinite(parsed_ts) and parsed_ts > 0) else time.time()
        except (ValueError, TypeError):
            timestamp = time.time()

    if isinstance(cache_data.get("accounts"), dict):
        cached_accounts_map = cache_data["accounts"]
    else:
        cached_accounts_map = {
            k: v for k, v in cache_data.items()
            if k not in ("timestamp", "cached_at") and isinstance(v, dict)
        }

    all_names = list(registered_names)
    for k in cached_accounts_map.keys():
        if isinstance(k, str) and k.strip() and k not in all_names:
            all_names.append(k)
    total_accounts = len(all_names)

    fleet_accounts: dict[str, dict[str, Any]] = {}
    for acc_name in all_names:
        cached_entry = cached_accounts_map.get(acc_name)
        if not isinstance(cached_entry, dict) or not cached_entry:
            # Check per-account directory cache fallback
            try:
                per_acc_cache = engine.accounts_dir / acc_name / "last_known_quota.json"
                if per_acc_cache.is_file():
                    loaded_per_acc = json.loads(per_acc_cache.read_text(encoding="utf-8"))
                    if isinstance(loaded_per_acc, dict):
                        cached_entry = loaded_per_acc
            except Exception:
                pass

        if not isinstance(cached_entry, dict):
            fleet_accounts[acc_name] = {}
            continue

        if target_model_key is None:
            acc_dict = dict(cached_entry)
            for m_key in ("gemini", "claude"):
                if m_key in cached_entry and isinstance(cached_entry[m_key], dict):
                    m_payload = dict(cached_entry[m_key])
                    sanitized_r, rem_m = _sanitize_reset_time_and_delta(m_payload.get("reset_time"), current_ts=timestamp)
                    m_payload["reset_time"] = sanitized_r
                    if rem_m is not None and "remaining_minutes" not in m_payload:
                        m_payload["remaining_minutes"] = rem_m
                    acc_dict[m_key] = m_payload
                elif "groups" in cached_entry and isinstance(cached_entry["groups"], list):
                    try:
                        rot = RotationEngine(vault_dir=engine.vault_dir, target_model=m_key)
                        st = rot.get_quota_status(cached_entry, model_key=m_key)
                        if st is not None:
                            is_valid = st.reset_time and st.reset_time.year < 9000
                            r_ts = st.reset_time.timestamp() if is_valid else None
                            rem_m = max(1, int(round((r_ts - timestamp) / 60))) if (is_valid and r_ts is not None and r_ts > timestamp) else None
                            m_payload = {
                                "quota": st.remaining_percentage(),
                                "reset_time": r_ts,
                            }
                            if rem_m is not None:
                                m_payload["remaining_minutes"] = rem_m
                            acc_dict[m_key] = m_payload
                    except Exception:
                        pass
                elif "quota" in cached_entry and not isinstance(cached_entry["quota"], dict) and m_key == "gemini":
                    m_payload = dict(cached_entry)
                    sanitized_r, rem_m = _sanitize_reset_time_and_delta(m_payload.get("reset_time"), current_ts=timestamp)
                    m_payload["reset_time"] = sanitized_r
                    if rem_m is not None and "remaining_minutes" not in m_payload:
                        m_payload["remaining_minutes"] = rem_m
                    acc_dict["gemini"] = m_payload
            fleet_accounts[acc_name] = acc_dict
        else:
            acc_dict = {}
            if target_model_key in cached_entry and isinstance(cached_entry[target_model_key], dict):
                model_payload = dict(cached_entry[target_model_key])
                sanitized_r, rem_m = _sanitize_reset_time_and_delta(model_payload.get("reset_time"), current_ts=timestamp)
                model_payload["reset_time"] = sanitized_r
                if rem_m is not None and "remaining_minutes" not in model_payload:
                    model_payload["remaining_minutes"] = rem_m
                acc_dict.update(model_payload)
                acc_dict[target_model_key] = model_payload
            elif "quota" in cached_entry and not isinstance(cached_entry["quota"], dict) and target_model_key == "gemini":
                sanitized_r, rem_m = _sanitize_reset_time_and_delta(cached_entry.get("reset_time"), current_ts=timestamp)
                model_payload = {
                    "quota": cached_entry.get("quota"),
                    "reset_time": sanitized_r,
                }
                if rem_m is not None:
                    model_payload["remaining_minutes"] = rem_m
                acc_dict.update(model_payload)
                acc_dict["gemini"] = model_payload
            elif "groups" in cached_entry and isinstance(cached_entry["groups"], list):
                try:
                    rot = RotationEngine(vault_dir=engine.vault_dir, target_model=target_model_key)
                    st = rot.get_quota_status(cached_entry, model_key=target_model_key)
                    if st is not None:
                        is_valid = st.reset_time and st.reset_time.year < 9000
                        r_ts = st.reset_time.timestamp() if is_valid else None
                        rem_m = max(1, int(round((r_ts - timestamp) / 60))) if (is_valid and r_ts is not None and r_ts > timestamp) else None
                        payload = {
                            "quota": st.remaining_percentage(),
                            "reset_time": r_ts,
                        }
                        if rem_m is not None:
                            payload["remaining_minutes"] = rem_m
                        acc_dict.update(payload)
                        acc_dict[target_model_key] = payload
                except Exception:
                    pass
            else:
                acc_dict = {}

            if "cached_at" in cached_entry and "cached_at" not in acc_dict:
                acc_dict["cached_at"] = cached_entry["cached_at"]

            if target_model_key == "gemini":
                acc_dict.pop("claude", None)
                if isinstance(acc_dict.get("gemini"), dict):
                    acc_dict["gemini"].pop("claude", None)

            fleet_accounts[acc_name] = acc_dict

    return FleetQuotaSummary(
        total_accounts=total_accounts,
        active_account=active_name,
        accounts=fleet_accounts,
        model=effective_model,
        timestamp=timestamp,
    )


__all__ = [
    "get_active_account",
    "auto_rotate_on_exhaustion",
    "switch_account",
    "list_accounts",
    "get_accounts",
    "get_fleet_quota_summary",
    "normalize_model_name",
    "handle_soft_import_rotation",
    "format_fleet_exhaustion_message",
    "format_fleet_exhaustion_notice",
    "FALLBACK_MISSING_SWITCHER_MESSAGE",
    "AccountInfo",
    "RotationResult",
    "FleetQuotaSummary",
]

