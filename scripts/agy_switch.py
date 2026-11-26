#!/usr/bin/env python3
"""agy-switch: High-reliability multi-account switcher for Antigravity 2.0 & CLI.

Thin re-export facade launcher providing 100% backward compatibility for all
legacy imports, dynamic test patches, and CLI executions.
Optimized fast-path dispatch for hook-pre-invocation via deferred lazy-imports.
"""

from __future__ import annotations

import importlib
import json
import math
import os
import sys
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any, Callable
    from agy_switcher.vault import AgySwitchEngine

# Ensure physical script dir and repo root are in sys.path (resolving symlinks)
_real_script_dir = os.path.dirname(os.path.realpath(__file__))
if _real_script_dir not in sys.path:
    sys.path.insert(0, _real_script_dir)
_parent_dir = os.path.dirname(_real_script_dir)
if _parent_dir not in sys.path:
    sys.path.insert(0, _parent_dir)


# Register bidirectional module aliases in sys.modules for backward compatibility and mock test interception
_curr_mod = sys.modules.get(__name__)
if _curr_mod is not None:
    sys.modules.setdefault("agy_switch", _curr_mod)
    sys.modules.setdefault("scripts.agy_switch", _curr_mod)


def _resolve_symbol(symbol_name: str, fallback_factory: Callable[[], Any]) -> Any:
    """Resolves symbol from sys.modules['agy_switch'], 'scripts.agy_switch', or caller module if mocked/present; falls back to sub-package default."""
    for mod_name in ("agy_switch", "scripts.agy_switch", __name__):
        mod = sys.modules.get(mod_name)
        if mod is not None and symbol_name in mod.__dict__:
            return mod.__dict__[symbol_name]
    for mod_name in ("agy_switch", "scripts.agy_switch", __name__):
        mod = sys.modules.get(mod_name)
        if mod is not None and hasattr(mod, symbol_name):
            return getattr(mod, symbol_name)
    return fallback_factory()


def hook_pre_invocation_dispatch(
    argv: list[str] | None = None,
    engine: AgySwitchEngine | None = None,
) -> int:
    """Fast-path dispatch for Antigravity PreInvocation hook (<40ms cold start).

    Defers eager imports of heavy modules (doctor, telemetry, formatters, urllib, argparse)
    until Tier 2 or Tier 3 failover is required.
    """
    # 1. Determine environment / vault configuration
    if engine is not None:
        vault_dir_str: str | None = str(getattr(engine, "_vault_dir", ""))
        if not vault_dir_str and hasattr(engine, "cache_file"):
            vault_dir_str = os.path.dirname(str(engine.cache_file))
    else:
        env_vault = os.environ.get("AGY_VAULT_DIR")
        if env_vault is not None and env_vault.strip():
            vault_dir_str = os.path.expanduser(env_vault.strip())
        else:
            vault_dir_str = os.path.expanduser("~/.antigravity-vault")

    # 2. Check auto-switch toggle & config
    auto_switch = False
    threshold = 0.25
    default_model = "gemini"

    if engine is not None and hasattr(engine, "get_auto_switch"):
        auto_switch = bool(engine.get_auto_switch())
        if hasattr(engine, "get_quota_threshold"):
            try:
                threshold = float(engine.get_quota_threshold())
            except (ValueError, TypeError):
                threshold = 0.25
        if hasattr(engine, "get_default_model"):
            try:
                default_model = str(engine.get_default_model())
            except Exception:
                default_model = "gemini"
    elif vault_dir_str:
        config_path = os.path.join(vault_dir_str, "config.json")
        if os.path.isfile(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as fp:
                    raw_cfg = fp.read()
                cfg_obj = json.loads(raw_cfg)
                if isinstance(cfg_obj, dict):
                    auto_switch = bool(cfg_obj.get("auto_switch", False))
                    raw_thresh = cfg_obj.get("quota_threshold")
                    if raw_thresh is not None:
                        threshold = float(raw_thresh)
                    raw_model = cfg_obj.get("default_model")
                    if raw_model and isinstance(raw_model, str):
                        default_model = raw_model.strip()
            except Exception:
                auto_switch = False
                threshold = 0.25
                default_model = "gemini"

    if not auto_switch:
        sys.stdout.write(json.dumps({}) + "\n")
        sys.stdout.flush()
        return 0

    # 3. Read and parse stdin payload
    try:
        raw_stdin = sys.stdin.read()
    except Exception:
        raw_stdin = ""

    try:
        stdin_payload = json.loads(raw_stdin) if raw_stdin and raw_stdin.strip() else {}
    except Exception:
        stdin_payload = {}

    if not isinstance(stdin_payload, dict):
        stdin_payload = {}

    raw_model_name = stdin_payload.get("modelName")
    if isinstance(raw_model_name, str) and raw_model_name.strip():
        model_name = raw_model_name.strip()
    else:
        model_name = "auto"

    if model_name.lower() in ("auto", ""):
        if default_model and default_model.lower() not in ("auto", "gemini", ""):
            model_name = default_model
        else:
            model_name = "gemini"

    model_key = "claude" if "claude" in model_name.lower() else "gemini"

    # 4. Determine active account name
    active_name: str | None = None
    if engine is not None and hasattr(engine, "get_active_name"):
        active_name = engine.get_active_name()
    elif vault_dir_str:
        active_path = os.path.join(vault_dir_str, "active")
        if os.path.isfile(active_path):
            try:
                with open(active_path, "r", encoding="utf-8") as fp:
                    active_name = fp.read().strip()
            except Exception:
                active_name = None

    # 5. Fast-path Tier 1 evaluation
    tier1_threshold = min(1.0, threshold + 0.10)
    tier1_passed = False
    corrupted_cache_warning: str | None = None

    cache_candidates: list[str] = []
    if engine is not None and getattr(engine, "cache_file", None):
        cache_candidates.append(str(engine.cache_file))
    if vault_dir_str:
        cache_candidates.extend([
            os.path.join(vault_dir_str, "quota_summary.json"),
            os.path.join(vault_dir_str, "cache", "last_known_quota.json"),
            os.path.join(vault_dir_str, "last_known_quota.json"),
        ])

    target_cache: str | None = None
    for candidate in cache_candidates:
        if os.path.isfile(candidate):
            target_cache = candidate
            break

    if target_cache is not None:
        try:
            with open(target_cache, "r", encoding="utf-8") as fp:
                cache_raw = fp.read()
            if not cache_raw.strip():
                raise ValueError("Cache file is empty")
            cached_data = json.loads(cache_raw)
            if not isinstance(cached_data, dict):
                raise ValueError(f"Cache root must be an object, got {type(cached_data).__name__}")

            # Validate accounts container schema
            accounts_val = cached_data.get("accounts")
            if accounts_val is not None and not isinstance(accounts_val, dict):
                raise ValueError(f"Cache 'accounts' must be an object, got {type(accounts_val).__name__}")

            accounts_cache = accounts_val if isinstance(accounts_val, dict) else cached_data

            if active_name:
                active_val = accounts_cache.get(active_name)
                if active_val is not None and not isinstance(active_val, dict):
                    raise ValueError(f"Cache entry for '{active_name}' must be an object, got {type(active_val).__name__}")
                active_cache = active_val

                if isinstance(active_cache, dict):
                    raw_cached_at = active_cache.get("cached_at")
                    if raw_cached_at is None or isinstance(raw_cached_at, (bool, str, list, dict)):
                        raise ValueError(f"Invalid 'cached_at' timestamp: {raw_cached_at!r}")
                    try:
                        cached_at_val = float(raw_cached_at)
                    except (ValueError, TypeError, OverflowError) as exc:
                        raise ValueError(f"Unparseable 'cached_at' timestamp: {exc}") from exc

                    if not math.isfinite(cached_at_val) or cached_at_val <= 0:
                        raise ValueError(f"Non-finite or non-positive 'cached_at' timestamp: {cached_at_val}")

                    candidate_m = active_cache.get(model_key)
                    if candidate_m is not None and not isinstance(candidate_m, dict):
                        raise ValueError(f"Model entry for '{model_key}' must be an object, got {type(candidate_m).__name__}")
                    if isinstance(candidate_m, dict):
                        raw_q = candidate_m.get("quota") if candidate_m.get("quota") is not None else candidate_m.get("fraction")
                        if raw_q is not None and not isinstance(raw_q, (bool, int, float, str)):
                            raise ValueError(f"Unexpected quota data type in model '{model_key}': {type(raw_q).__name__}")

                    now = time.time()
                    is_fresh = (-5.0 <= (now - cached_at_val) <= 300.0)
                    if is_fresh:
                        target_info: dict[str, Any] | None = None
                        candidate_m = active_cache.get(model_key)
                        if candidate_m is not None and not isinstance(candidate_m, dict):
                            raise ValueError(f"Model entry for '{model_key}' must be an object, got {type(candidate_m).__name__}")
                        if isinstance(candidate_m, dict):
                            target_info = candidate_m
                        elif (
                            ("quota" in active_cache or "fraction" in active_cache)
                            and not isinstance(active_cache.get("quota"), dict)
                            and model_key == "gemini"
                        ):
                            target_info = active_cache

                        if target_info is not None:
                            q_val = target_info.get("quota") if target_info.get("quota") is not None else target_info.get("fraction")
                            fraction = 0.0
                            if isinstance(q_val, bool):
                                fraction = 0.0
                            elif isinstance(q_val, (int, float)):
                                val_f = float(q_val)
                                if not math.isfinite(val_f):
                                    raise ValueError(f"Quota value is not finite: {val_f}")
                                fraction = val_f / 100.0 if val_f > 1.0 else max(0.0, val_f)
                            elif isinstance(q_val, str):
                                try:
                                    s_val = float(q_val.strip().rstrip("%"))
                                    if not math.isfinite(s_val):
                                        raise ValueError(f"Quota string is not finite: {s_val}")
                                    fraction = max(0.0, min(1.0, s_val / 100.0 if s_val > 1.0 else s_val))
                                except ValueError as exc:
                                    raise ValueError(f"Unparseable quota string: {q_val!r}") from exc
                            elif q_val is not None:
                                raise ValueError(f"Unexpected quota data type: {type(q_val).__name__}")

                            if fraction >= tier1_threshold:
                                tier1_passed = True
        except Exception as exc:
            corrupted_cache_warning = (
                f"[agy-switch hook] Warning: Corrupted or unparseable Tier 1 quota cache file '{target_cache}': {exc}\n"
            )
            tier1_passed = False

    if tier1_passed:
        # Tier 1 Cache Hit: 0ms overhead, strictly sub-40ms execution
        sys.stdout.write(json.dumps({}) + "\n")
        sys.stdout.flush()
        return 0

    # 6. Fallback to Tier 2 / 3 failover
    if corrupted_cache_warning:
        sys.stderr.write(corrupted_cache_warning)
        sys.stderr.flush()

    import io
    sys.stdin = io.StringIO(raw_stdin)

    engine_cls = _resolve_symbol(
        "AgySwitchEngine",
        lambda: importlib.import_module("agy_switcher.vault").AgySwitchEngine,
    )
    hook_fn = _resolve_symbol(
        "handle_pre_invocation_hook",
        lambda: importlib.import_module("agy_switcher.hook").handle_pre_invocation_hook,
    )
    try:
        engine_inst = engine if engine is not None else engine_cls()
        return int(hook_fn(engine_inst))
    except Exception as exc:
        sys.stderr.write(f"[agy-switch hook] Error during hook failover execution: {exc}\n")
        sys.stderr.flush()
        sys.stdout.write(json.dumps({}) + "\n")
        sys.stdout.flush()
        return 0


_LAZY_EXPORTS: dict[str, tuple[str, str | None]] = {
    # Non-exported domain helpers in agy_switcher
    "InvalidThresholdError": ("agy_switcher.exceptions", "InvalidThresholdError"),
    "_format_bucket_countdown": ("agy_switcher.formatters", "_format_bucket_countdown"),
    "_format_gemini_quota_summary": ("agy_switcher.formatters", "_format_gemini_quota_summary"),
    "_format_model_quota_summary": ("agy_switcher.formatters", "_format_model_quota_summary"),
    "_has_auth_indicator": ("agy_switcher.telemetry", "_has_auth_indicator"),
    # Standard library re-exports
    "argparse": ("argparse", None),
    "base64": ("base64", None),
    "dataclass": ("dataclasses", "dataclass"),
    "datetime": ("datetime", "datetime"),
    "timedelta": ("datetime", "timedelta"),
    "timezone": ("datetime", "timezone"),
    "re": ("re", None),
    "shlex": ("shlex", None),
    "shutil": ("shutil", None),
    "signal": ("signal", None),
    "stat": ("stat", None),
    "subprocess": ("subprocess", None),
    "tempfile": ("tempfile", None),
    "threading": ("threading", None),
    "urllib": ("urllib", None),
    "Path": ("pathlib", "Path"),
    "Any": ("typing", "Any"),
    "Callable": ("typing", "Callable"),
    "Optional": ("typing", "Optional"),
}

__all__ = [
    # Layer 0
    "DEFAULT_PENDING_RELOAD_TTL_SECONDS",
    "AgySwitchError",
    "InvalidTokenError",
    "AccountNotFoundError",
    "ActiveTurnRunningError",
    "DaemonError",
    "DaemonAlreadyRunningError",
    "InvalidThresholdError",
    "AccountAuthError",
    "DiagnosticItem",
    "DiagnosticReport",
    "QuotaStatus",
    "EvaluationCandidate",
    "AccountInfo",
    "RotationResult",
    "FleetQuotaSummary",
    "PendingReloadPayload",
    "TokenIdentity",
    "TargetSyncSnapshot",
    "TokenSyncStatus",
    "parse_pending_reload_payload",
    "read_latest_jsonl_record",
    # Layer 1
    "ProcessSupervisor",
    "format_countdown",
    "format_accounts_listicle",
    "format_accounts_table",
    "format_status_report",
    "format_diagnostic_report",
    "_format_bucket_countdown",
    "_format_gemini_quota_summary",
    "_format_model_quota_summary",
    "DEFAULT_CLOUDCODE_ENDPOINT",
    "DEFAULT_FALLBACK_CLOUDCODE_ENDPOINT",
    "QUOTA_SUMMARY_PATH",
    "DEFAULT_QUOTA_ENDPOINT",
    "DEFAULT_FALLBACK_QUOTA_ENDPOINT",
    "resolve_quota_endpoint",
    "QuotaClient",
    "probe_account_auth",
    "fetch_account_quota",
    "fetch_all_accounts_quota",
    "sort_accounts_by_quota",
    "normalize_model_name",
    "run_quota_pipeline",
    "AUTH_PHRASES",
    "AUTH_ERROR_CODE_PATTERN",
    "HTTP_SERVER_ERROR_PATTERN",
    "QUOTA_REGEX",
    "_has_auth_indicator",
    "_is_auth_error",
    "_is_network_error",
    "_is_stale_exhausted_cache",
    "_parse_timestamp_defensive",
    "_parse_quota_defensive",
    "_sanitize_expires_in",
    # Layer 2
    "AgySwitchEngine",
    "DEFAULT_VAULT_DIR",
    "DEFAULT_GEMINI_DIR",
    "DEFAULT_LEGACY_ROOT",
    "_atomic_write_file",
    "_atomic_write_json",
    "extract_email_from_jwt",
    "resolve_token_email",
    "validate_token_dict",
    "validate_token_file",
    "check_token_dict",
    "check_token_file",
    "RotationEngine",
    "DoctorEngine",
    "run_doctor",
    "auto_heal",
    "PresenceDetector",
    "CooldownManager",
    "LogWatcher",
    "DaemonManager",
    "_open_secure_log_file",
    "_resolve_vault_log_path",
    "append_secure_log_entry",
    # Layer 3
    "handle_pre_invocation_hook",
    # Layer 4
    "get_active_account",
    "auto_rotate_on_exhaustion",
    "switch_account",
    "get_fleet_quota_summary",
    "format_fleet_exhaustion_message",
    "format_fleet_exhaustion_notice",
    "build_parser",
    "handle_vault",
    "main",
    # Standard library re-exports for backward-compatibility with tests
    "argparse",
    "base64",
    "dataclass",
    "datetime",
    "json",
    "math",
    "os",
    "re",
    "shlex",
    "shutil",
    "signal",
    "stat",
    "subprocess",
    "sys",
    "tempfile",
    "threading",
    "time",
    "timedelta",
    "timezone",
    "urllib",
    "Path",
    "Any",
    "Callable",
    "Optional",
    # Fast-path seam
    "hook_pre_invocation_dispatch",
]


def __getattr__(name: str) -> Any:
    val: Any = None
    resolved = False

    if name in _LAZY_EXPORTS:
        mod_name, attr_name = _LAZY_EXPORTS[name]
        mod = importlib.import_module(mod_name)
        val = getattr(mod, attr_name) if attr_name else mod
        resolved = True

    if not resolved:
        try:
            import agy_switcher
            if hasattr(agy_switcher, name):
                val = getattr(agy_switcher, name)
                resolved = True
        except Exception:
            pass

    if not resolved:
        try:
            mod = importlib.import_module("agy_switcher.exceptions")
            if hasattr(mod, name):
                val = getattr(mod, name)
                resolved = True
        except Exception:
            pass

    if not resolved:
        for submod in (
            "agy_switcher.models",
            "agy_switcher.security",
            "agy_switcher.supervisor",
            "agy_switcher.formatters",
            "agy_switcher.telemetry",
            "agy_switcher.vault",
            "agy_switcher.rotation",
            "agy_switcher.doctor",
            "agy_switcher.watcher",
            "agy_switcher.hook",
            "agy_switcher.api",
            "agy_switcher.cli",
        ):
            try:
                mod = importlib.import_module(submod)
                if hasattr(mod, name):
                    val = getattr(mod, name)
                    resolved = True
                    break
            except Exception:
                pass

    if not resolved:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    globals()[name] = val
    for alias_name in ("agy_switch", "scripts.agy_switch"):
        alias_mod = sys.modules.get(alias_name)
        if alias_mod is not None:
            alias_mod.__dict__[name] = val
    return val


def __dir__() -> list[str]:
    return sorted(set(list(globals().keys()) + __all__))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "hook-pre-invocation":
        sys.exit(hook_pre_invocation_dispatch(sys.argv[1:]))
    import agy_switcher
    sys.exit(agy_switcher.main())
