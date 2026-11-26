"""Layer 3 Workflow & Orchestration module: PreInvocation lifecycle hook.

Evaluates 3-tier quota status and triggers automatic account rotation mid-session.
Implements Dynamic Module Proxy Bridge for transparent mock test interception.

Zero external dependencies. Pure Python 3.10+ standard library.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sys
import time
from typing import Any, Callable, cast

from agy_switcher.exceptions import AgySwitchError
from agy_switcher.models import QuotaStatus
from agy_switcher.presence import _resolve_vault_log_path, append_secure_log_entry
from agy_switcher.telemetry import (
    HTTP_SERVER_ERROR_PATTERN,
    _has_auth_indicator,
    _is_auth_error,
    _is_network_error,
    _is_stale_exhausted_cache,
)
from agy_switcher.vault import AgySwitchEngine


def _resolve_symbol(symbol_name: str, fallback_factory: Callable[[], Any]) -> Any:
    """Resolves symbol from sys.modules['agy_switch'] if mocked/present; falls back to sub-package default."""
    mod = sys.modules.get("agy_switch")
    if mod and hasattr(mod, symbol_name):
        return getattr(mod, symbol_name)
    return fallback_factory()


def _get_telemetry_fetchers() -> tuple[Callable[..., Any], Callable[..., Any]]:
    """Resolves fetch_account_quota and fetch_all_accounts_quota dynamically."""
    mod = sys.modules.get("agy_switch")
    if mod and hasattr(mod, "fetch_account_quota") and hasattr(mod, "fetch_all_accounts_quota"):
        return mod.fetch_account_quota, mod.fetch_all_accounts_quota
    from agy_switcher.telemetry import fetch_account_quota, fetch_all_accounts_quota
    return fetch_account_quota, fetch_all_accounts_quota


def _get_quota_client_class() -> Any:
    return _resolve_symbol(
        "QuotaClient",
        lambda: __import__("agy_switcher.telemetry", fromlist=["QuotaClient"]).QuotaClient,
    )


def _get_rotation_engine_class() -> Any:
    return _resolve_symbol(
        "RotationEngine",
        lambda: __import__("agy_switcher.rotation", fromlist=["RotationEngine"]).RotationEngine,
    )


_DEFAULT_APPEND_SECURE_LOG_ENTRY = append_secure_log_entry


def _get_error_classifiers() -> tuple[Callable[..., Any], Callable[..., Any]]:
    mod = sys.modules.get("agy_switch")
    auth_fn = getattr(mod, "_is_auth_error", None) if mod else None
    net_fn = getattr(mod, "_is_network_error", None) if mod else None
    return (auth_fn or _is_auth_error), (net_fn or _is_network_error)


def _get_append_secure_log_entry() -> Callable[[Path | str, str], None]:
    if append_secure_log_entry is not _DEFAULT_APPEND_SECURE_LOG_ENTRY:
        return append_secure_log_entry
    mod = sys.modules.get("agy_switch")
    if mod is not None and "append_secure_log_entry" in mod.__dict__:
        candidate = mod.__dict__["append_secure_log_entry"]
        if callable(candidate):
            return cast(Callable[[Path | str, str], None], candidate)
    return append_secure_log_entry


def _extract_http_status(payload: object) -> int | None:
    """Extracts an integer HTTP status code from a quota or error payload dict."""
    if not isinstance(payload, dict):
        return None
    for key in ("code", "status_code", "http_status", "httpStatus"):
        val = payload.get(key)
        if isinstance(val, bool) or val is None:
            continue
        if isinstance(val, int):
            return val
        if isinstance(val, str) and val.strip().isdigit():
            return int(val.strip())
    return None


def _has_probe_error_payload(payload: object) -> bool:
    """Returns True if a probe quota dict represents an error payload rather than quota data."""
    if not isinstance(payload, dict):
        return False
    if payload.get("error"):
        return True
    if str(payload.get("error_type") or "").strip():
        return True
    http_status = _extract_http_status(payload)
    return http_status is not None and http_status >= 400


def _normalize_probe_error_payload(payload: object, probe_error: object = None) -> object:
    """Normalizes probe error payloads so http_status and error_type variants classify uniformly."""
    if isinstance(probe_error, Exception):
        return probe_error
    if isinstance(payload, dict):
        normalized = dict(payload)
        raw_type = normalized.get("error_type")
        if isinstance(raw_type, str):
            normalized["error_type"] = raw_type.strip().lower()
        if normalized.get("code") is None and normalized.get("status_code") is None:
            http_status = _extract_http_status(normalized)
            if http_status is not None:
                normalized["code"] = http_status
        return normalized
    return probe_error


def _extract_failover_context(
    active_name: str | None,
    active_quota: object,
    probe_error: object,
    client: object,
    is_active_auth_err: bool,
    rot_engine: Any,
    model_key: str,
    display_model: str,
    threshold: float,
) -> tuple[str, str, str]:
    """Derives (trigger, reason, error_type) for failover audit log and state persistence."""
    if is_active_auth_err:
        raw_err_msg = ""
        if isinstance(active_quota, dict) and active_quota.get("error"):
            raw_err_msg = str(active_quota.get("error"))
        elif isinstance(active_quota, dict) and _extract_http_status(active_quota) is not None:
            raw_err_msg = f"HTTP {_extract_http_status(active_quota)}"
        elif probe_error is not None:
            raw_err_msg = str(probe_error)
        elif getattr(client, "last_error", None):
            raw_err_msg = str(getattr(client, "last_error"))
        reason = " ".join(raw_err_msg.split()) or "Authentication failed"

        raw_err_type = ""
        if isinstance(active_quota, dict) and active_quota.get("error_type"):
            raw_err_type = str(active_quota.get("error_type"))
        elif getattr(client, "last_error_type", None):
            raw_err_type = str(getattr(client, "last_error_type"))
        clean_err_type = " ".join(raw_err_type.split()).lower()
        error_type = "auth" if clean_err_type in ("", "http") else clean_err_type
        return "auth_corruption", reason, error_type

    if active_name and isinstance(active_quota, dict) and not active_quota.get("error"):
        active_status = rot_engine.get_quota_status(active_quota, model_key=model_key)
        if active_status is not None:
            pct_str = active_status.remaining_percentage()
            thresh_pct = int(round(threshold * 100))
            return "quota_rotation", f"{display_model} quota at {pct_str}% (< {thresh_pct}%)", ""
        return "quota_rotation", f"{display_model} quota unavailable or below threshold", ""

    if probe_error is not None:
        clean_err = " ".join(str(probe_error).split())
        return "quota_rotation", f"Active probe fallback ({clean_err})", ""

    return "quota_rotation", f"{display_model} quota below threshold", ""


def _persist_failover_audit(
    engine: AgySwitchEngine,
    from_account: str | None,
    to_account: str,
    trigger: str,
    reason: str,
    error_type: str,
    timestamp: float,
) -> None:
    """Dual-persists failover audit trail to watch.log and cache/failover_state.json fail-safely."""
    clean_from = " ".join(from_account.split()) if isinstance(from_account, str) and from_account.strip() else ""
    clean_to = " ".join(to_account.split())
    clean_reason = " ".join(reason.split()) if reason else ""

    try:
        log_target = _resolve_vault_log_path(engine=engine)
        if log_target is not None:
            try:
                ts_dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
            except (ValueError, TypeError, OverflowError, OSError):
                ts_dt = datetime.now(timezone.utc)
            ts_str = ts_dt.strftime("%Y-%m-%d %H:%M:%S")
            prefix = (
                f"Hook rotated from '{clean_from}' to '{clean_to}'"
                if clean_from
                else f"Hook rotated to '{clean_to}'"
            )
            log_line = f"{ts_str} [hook] {prefix} (trigger: {trigger}, reason: {clean_reason})\n"
            _get_append_secure_log_entry()(log_target, log_line)
    except Exception as log_exc:
        print(f"[agy-switch hook] Warning: Failed to append audit log: {log_exc}", file=sys.stderr)

    try:
        record_last_failover = getattr(engine, "record_last_failover", None)
        if callable(record_last_failover):
            record_last_failover(
                from_account=clean_from,
                to_account=clean_to,
                trigger=trigger,
                reason=clean_reason,
                timestamp=timestamp,
            )
        if trigger == "auth_corruption" and clean_from:
            record_auth_error = getattr(engine, "record_auth_error", None)
            if callable(record_auth_error):
                record_auth_error(
                    clean_from,
                    error=clean_reason,
                    error_type=error_type or "auth",
                    timestamp=timestamp,
                )
    except Exception as state_exc:
        print(f"[agy-switch hook] Warning: Failed to persist failover state: {state_exc}", file=sys.stderr)


def handle_pre_invocation_hook(engine: AgySwitchEngine) -> int:
    """Evaluates 3-tier quota status and auto-rotates active account before tool invocation."""
    if not engine.get_auto_switch():
        sys.stdout.write(json.dumps({}) + "\n")
        sys.stdout.flush()
        return 0
    try:
        raw = sys.stdin.read()
        try:
            _payload = json.loads(raw) if raw and raw.strip() else {}
        except Exception:
            _payload = {}
        if not isinstance(_payload, dict):
            _payload = {}
        raw_model = _payload.get("modelName")
        if isinstance(raw_model, str) and raw_model.strip():
            model_name = raw_model.strip()
        else:
            model_name = "auto"
        if model_name.lower() in ("auto", ""):
            resolved_default = engine.get_default_model()
            if resolved_default and resolved_default != "gemini":
                model_name = resolved_default
        model_key = "claude" if "claude" in model_name.lower() else "gemini"
        display_model = "Gemini" if model_name.lower() in ("auto", "gemini", "") else model_name

        # Wire in QuotaClient, RotationEngine, and fetchers via Proxy Bridge
        try:
            fetch_account_quota, fetch_all_accounts_quota = _get_telemetry_fetchers()
            QuotaClientCls = _get_quota_client_class()
            RotationEngineCls = _get_rotation_engine_class()
            is_auth_err_fn, is_net_err_fn = _get_error_classifiers()

            threshold = engine.get_quota_threshold() if hasattr(engine, "get_quota_threshold") else 0.25
            tier1_threshold = min(1.0, threshold + 0.10)

            active_name = engine.get_active_name()
            client = QuotaClientCls(timeout=2.0, engine=engine)
            rot_engine = RotationEngineCls(model_name=model_name, active_account_id=active_name, threshold=threshold)

            cached_data = client.load_cache()
            accounts_cache = (
                cached_data.get("accounts", {})
                if isinstance(cached_data, dict) and isinstance(cached_data.get("accounts"), dict)
                else {}
            )
            active_cache = accounts_cache.get(active_name) if active_name else None
            now = time.time()

            # Tier 1 (Healthy Pass - 0ms Overhead)
            if active_name and isinstance(active_cache, dict):
                try:
                    cached_at_val = float(active_cache.get("cached_at", 0))
                except (ValueError, TypeError, OverflowError):
                    cached_at_val = 0.0

                is_fresh = math.isfinite(cached_at_val) and cached_at_val > 0 and (-5.0 <= (now - cached_at_val) <= 300.0)
                if is_fresh:
                    status = rot_engine.get_quota_status(active_cache, model_key=model_key)
                    if status is not None and status.fraction >= tier1_threshold:
                        # 0ms pass: strictly zero network calls and zero disk writes
                        sys.stdout.write(json.dumps({}) + "\n")
                        sys.stdout.flush()
                        return 0

            # Tier 2 (Single-Account Probe - ~300ms Overhead)
            probe_error = None
            active_quota = None
            if active_name:
                try:
                    _, active_quota = fetch_account_quota(engine, active_name, client=client)
                    if _has_probe_error_payload(active_quota):
                        assert isinstance(active_quota, dict)
                        if active_quota.get("error"):
                            probe_error = active_quota.get("error")
                        else:
                            http_status = _extract_http_status(active_quota)
                            probe_error = (
                                f"HTTP {http_status}"
                                if http_status is not None
                                else str(active_quota.get("error_type") or "Probe error")
                            )
                except Exception as probe_exc:
                    probe_error = probe_exc
                    active_quota = {"error": str(probe_exc)}

                if probe_error is not None:
                    # Active Probe Error Classification
                    err_obj = _normalize_probe_error_payload(active_quota, probe_error)
                    if is_auth_err_fn(err_obj, client=client):
                        # Auth/token corruption: do not fall back to cache; signal Tier 3 failover
                        print(f"[agy-switch hook] Active account '{active_name}' auth corruption: {probe_error}. Triggering Tier 3 failover.", file=sys.stderr)
                    elif is_net_err_fn(err_obj, client=client) or is_net_err_fn(probe_error, client=client):
                        # Transient transport failure: attempt fallback to cached quota
                        if isinstance(active_cache, dict):
                            cached_status = rot_engine.get_quota_status(active_cache, model_key=model_key)
                            if cached_status is not None and cached_status.fraction >= threshold:
                                # Retain active account silently without emitting false alarms
                                print(f"[agy-switch hook] Active account '{active_name}' network error: {probe_error}. Retaining active via cache ({cached_status.remaining_percentage()}%).", file=sys.stderr)
                                sys.stdout.write(json.dumps({}) + "\n")
                                sys.stdout.flush()
                                return 0
                        # If active cache is missing or low (< threshold), check if any standby account has cached quota
                        has_standby_cache = any(
                            acc_id != active_name and isinstance(q_data, dict) and rot_engine.has_available_quota(q_data)
                            for acc_id, q_data in accounts_cache.items()
                        )
                        if not has_standby_cache:
                            print(f"[agy-switch hook] Active account '{active_name}' network error: {probe_error}. Retaining active silently (no standby cache).", file=sys.stderr)
                            sys.stdout.write(json.dumps({}) + "\n")
                            sys.stdout.flush()
                            return 0
                else:
                    # Probe succeeded without error
                    if isinstance(active_quota, dict) and not _has_probe_error_payload(active_quota):
                        active_status = rot_engine.get_quota_status(active_quota, model_key=model_key)
                        if (
                            rot_engine.has_available_quota(active_quota)
                            and active_status is not None
                            and active_status.fraction >= threshold
                        ):
                            active_entry = dict(active_quota)
                            active_entry["cached_at"] = time.time()
                            client.save_cache({"accounts": {active_name: active_entry}}, merge=True)
                            print(f"[agy-switch hook] Tier 2 probe for '{active_name}' healthy ({active_status.remaining_percentage()}%). Retaining active account.", file=sys.stderr)
                            sys.stdout.write(json.dumps({}) + "\n")
                            sys.stdout.flush()
                            return 0

            # Tier 3 (Full Standby Probe & Rotation)
            accounts_data = engine.list_accounts().get("accounts", [])
            registered_accounts = [acc for acc in accounts_data if isinstance(acc, dict) and acc.get("name")]
            registered_names = {acc["name"] for acc in registered_accounts}
            standby_accounts = [acc for acc in registered_accounts if acc["name"] != active_name]
            raw_quotas = fetch_all_accounts_quota(engine, standby_accounts, client=client)

            combined_quotas = {k: v for k, v in accounts_cache.items() if k in registered_names}
            now = time.time()
            evict_keys: set[str] = set(accounts_cache.keys()) - registered_names
            terminal_auth_keys: set[str] = set()

            for s_acc in standby_accounts:
                acc_name = s_acc.get("name")
                if not acc_name:
                    continue
                q_data = raw_quotas.get(acc_name)
                if isinstance(q_data, dict) and not _has_probe_error_payload(q_data) and q_data:
                    entry = dict(q_data)
                    entry["cached_at"] = now
                    combined_quotas[acc_name] = entry
                elif q_data is not None and is_auth_err_fn(_normalize_probe_error_payload(q_data), client=client):
                    combined_quotas.pop(acc_name, None)
                    evict_keys.add(acc_name)
                    terminal_auth_keys.add(acc_name)
                else:
                    # Standby probe failed due to transport / network error (or no probe response)
                    cached_entry = combined_quotas.get(acc_name)
                    if _is_stale_exhausted_cache(cached_entry, rot_engine, model_key, now):
                        combined_quotas.pop(acc_name, None)

            # If active_quota from Tier 2 was successful, ensure it is in combined_quotas
            if active_name and isinstance(active_quota, dict) and not _has_probe_error_payload(active_quota) and active_quota:
                active_entry = dict(active_quota)
                active_entry["cached_at"] = now
                combined_quotas[active_name] = active_entry

            # If active account had auth corruption, remove it from combined_quotas so it does not fall back to stale cache
            err_obj = _normalize_probe_error_payload(active_quota, probe_error)
            has_active_err = probe_error is not None or _has_probe_error_payload(active_quota)
            is_active_auth_err = bool(active_name and has_active_err and is_auth_err_fn(err_obj, client=client))
            if is_active_auth_err and active_name:
                combined_quotas.pop(active_name, None)
                evict_keys.add(active_name)
                terminal_auth_keys.add(active_name)
            elif active_name and (probe_error is not None or (isinstance(active_quota, dict) and active_quota.get("error"))):
                # Active account probe encountered network error; check if cached data is stale 0%
                cached_active_entry = combined_quotas.get(active_name)
                if _is_stale_exhausted_cache(cached_active_entry, rot_engine, model_key, now):
                    combined_quotas.pop(active_name, None)

            # Persist fresh telemetry to cache
            client.save_cache({"accounts": combined_quotas}, merge=True, evict_keys=evict_keys)

            target_account = rot_engine.evaluate(combined_quotas, current_active=active_name, target_model=model_name)
            print(f"[agy-switch hook] Evaluated rotation for {model_name}: {active_name} -> {target_account or active_name}", file=sys.stderr)

            # Exhaustion Evaluation Invariant:
            total_registered = len(registered_accounts)
            evaluated_count = len([
                acc for acc in registered_accounts
                if (acc["name"] in combined_quotas or acc["name"] in terminal_auth_keys)
            ])
            verified_accounts = [
                acc for acc in registered_accounts
                if acc["name"] in combined_quotas
            ]
            verified_count = len(verified_accounts)
            has_verified_account = verified_count > 0
            verified_exhausted_count = 0
            for acc in verified_accounts:
                st = rot_engine.get_quota_status(combined_quotas[acc["name"]], model_key=model_key)
                if st is not None and st.is_exhausted():
                    verified_exhausted_count += 1

            all_exhausted = (
                total_registered > 0
                and evaluated_count == total_registered
                and has_verified_account
                and verified_exhausted_count == verified_count
            )

            if all_exhausted:
                sys.stdout.write(json.dumps({
                    "injectSteps": [
                        {
                            "ephemeralMessage": f"⚠️ All Antigravity accounts have exhausted their {display_model} quota."
                        }
                    ]
                }) + "\n")
                sys.stdout.flush()
                return 0

            if target_account and target_account != active_name:
                engine.switch_tokens(target_account, restart_server=False, force=True)

                trigger, reason, error_type = _extract_failover_context(
                    active_name=active_name,
                    active_quota=active_quota,
                    probe_error=probe_error,
                    client=client,
                    is_active_auth_err=bool(is_active_auth_err),
                    rot_engine=rot_engine,
                    model_key=model_key,
                    display_model=display_model,
                    threshold=threshold,
                )
                _persist_failover_audit(
                    engine=engine,
                    from_account=active_name,
                    to_account=target_account,
                    trigger=trigger,
                    reason=reason,
                    error_type=error_type,
                    timestamp=now,
                )

                status = rot_engine.get_quota_status(combined_quotas.get(target_account, {}), model_key=model_key)
                pct = status.remaining_percentage() if status is not None else "Unknown"
                if trigger == "auth_corruption" and active_name:
                    ephemeral_msg = (
                        f"🔄 Auto-rotated to **{target_account}** for {display_model} ({pct}% remaining) "
                        f"after auth failure on **{active_name}** ({reason}). "
                        f"Run `agy-switch login {active_name}` to re-authenticate."
                    )
                else:
                    ephemeral_msg = f"🔄 Auto-rotated to **{target_account}** for {display_model} ({pct}% remaining)."
                sys.stdout.write(json.dumps({
                    "injectSteps": [
                        {
                            "ephemeralMessage": ephemeral_msg
                        }
                    ]
                }) + "\n")
                sys.stdout.flush()
                return 0

        except Exception as qc_exc:
            print(f"[agy-switch hook] QuotaClient/RotationEngine error: {qc_exc}", file=sys.stderr)
    except Exception as exc:
        print(f"[agy-switch hook] Error parsing stdin: {exc}", file=sys.stderr)
    sys.stdout.write(json.dumps({}) + "\n")
    sys.stdout.flush()
    return 0


__all__ = [
    "handle_pre_invocation_hook",
    "_is_stale_exhausted_cache",
    "_resolve_symbol",
    "_get_telemetry_fetchers",
    "_get_quota_client_class",
    "_get_rotation_engine_class",
]
