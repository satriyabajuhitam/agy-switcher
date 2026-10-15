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
from typing import Any, Callable

from agy_switcher.exceptions import AgySwitchError
from agy_switcher.models import QuotaStatus
from agy_switcher.telemetry import (
    HTTP_SERVER_ERROR_PATTERN,
    _has_auth_indicator,
    _is_auth_error,
    _is_network_error,
)
from agy_switcher.vault import AgySwitchEngine


def _resolve_symbol(symbol_name: str, fallback_factory: Callable[[], Any]) -> Any:
    """Resolves symbol from sys.modules['agy_switch'] if mocked/present; falls back to sub-package default."""
    mod = sys.modules.get("agy_switch")
    if mod and hasattr(mod, symbol_name):
        return getattr(mod, symbol_name)
    return fallback_factory()


def _get_telemetry_fetchers() -> tuple[Callable, Callable]:
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


def _get_error_classifiers() -> tuple[Callable, Callable]:
    mod = sys.modules.get("agy_switch")
    auth_fn = getattr(mod, "_is_auth_error", None) if mod else None
    net_fn = getattr(mod, "_is_network_error", None) if mod else None
    return (auth_fn or _is_auth_error), (net_fn or _is_network_error)


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

            active_name = engine.get_active_name()
            client = QuotaClientCls(timeout=2.0, engine=engine)
            rot_engine = RotationEngineCls(model_name=model_name, active_account_id=active_name)

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
                    if status is not None and status.fraction >= 0.25:
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
                    if isinstance(active_quota, dict) and active_quota.get("error"):
                        probe_error = active_quota.get("error")
                except Exception as probe_exc:
                    probe_error = probe_exc
                    active_quota = {"error": str(probe_exc)}

                if probe_error is not None:
                    # Active Probe Error Classification
                    err_obj = active_quota if isinstance(active_quota, dict) else probe_error
                    if is_auth_err_fn(err_obj, client=client):
                        # Auth/token corruption: do not fall back to cache; signal Tier 3 failover
                        print(f"[agy-switch hook] Active account '{active_name}' auth corruption: {probe_error}. Triggering Tier 3 failover.", file=sys.stderr)
                    elif is_net_err_fn(err_obj, client=client) or is_net_err_fn(probe_error, client=client):
                        # Transient transport failure: attempt fallback to cached quota
                        if isinstance(active_cache, dict):
                            cached_status = rot_engine.get_quota_status(active_cache, model_key=model_key)
                            if cached_status is not None and cached_status.fraction >= 0.15:
                                # Retain active account silently without emitting false alarms
                                print(f"[agy-switch hook] Active account '{active_name}' network error: {probe_error}. Retaining active via cache ({cached_status.remaining_percentage()}%).", file=sys.stderr)
                                sys.stdout.write(json.dumps({}) + "\n")
                                sys.stdout.flush()
                                return 0
                        # If active cache is missing or low (< 15%), check if any standby account has cached quota
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
                    if isinstance(active_quota, dict) and not active_quota.get("error"):
                        active_status = rot_engine.get_quota_status(active_quota, model_key=model_key)
                        if (
                            rot_engine.has_available_quota(active_quota)
                            and active_status is not None
                            and active_status.fraction >= 0.15
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
                if isinstance(q_data, dict) and "error" not in q_data and q_data:
                    entry = dict(q_data)
                    entry["cached_at"] = now
                    combined_quotas[acc_name] = entry
                elif q_data is not None and is_auth_err_fn(q_data):
                    combined_quotas.pop(acc_name, None)
                    evict_keys.add(acc_name)
                    terminal_auth_keys.add(acc_name)
                else:
                    # Standby probe failed due to transport / network error (or no probe response)
                    cached_entry = combined_quotas.get(acc_name)
                    if _is_stale_exhausted_cache(cached_entry, rot_engine, model_key, now):
                        combined_quotas.pop(acc_name, None)

            # If active_quota from Tier 2 was successful, ensure it is in combined_quotas
            if active_name and isinstance(active_quota, dict) and not active_quota.get("error") and active_quota:
                active_entry = dict(active_quota)
                active_entry["cached_at"] = now
                combined_quotas[active_name] = active_entry

            # If active account had auth corruption, remove it from combined_quotas so it does not fall back to stale cache
            err_obj = active_quota if isinstance(active_quota, dict) else probe_error
            is_active_auth_err = (
                active_name
                and (probe_error is not None or (isinstance(active_quota, dict) and active_quota.get("error")))
                and is_auth_err_fn(err_obj, client=client)
            )
            if is_active_auth_err:
                combined_quotas.pop(active_name, None)
                evict_keys.add(active_name)
                terminal_auth_keys.add(active_name)
            elif active_name and (probe_error is not None or (isinstance(active_quota, dict) and active_quota.get("error"))):
                # Active account probe encountered network error; check if cached data is stale 0%
                active_entry = combined_quotas.get(active_name)
                if _is_stale_exhausted_cache(active_entry, rot_engine, model_key, now):
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

                status = rot_engine.get_quota_status(combined_quotas.get(target_account, {}), model_key=model_key)
                pct = status.remaining_percentage() if status is not None else "Unknown"
                sys.stdout.write(json.dumps({
                    "injectSteps": [
                        {
                            "ephemeralMessage": f"🔄 Auto-rotated to **{target_account}** for {display_model} ({pct}% remaining)."
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
