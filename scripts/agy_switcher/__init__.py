"""agy_switcher: Internal modular sub-package for Antigravity Multi-Account Switcher.

Zero external dependencies. Pure Python 3.10+ standard library.
Uses PEP 562 lazy attribute loading to maintain pure submodule import isolation.
"""

from __future__ import annotations

import importlib
from typing import Any

__version__ = "2.0.0"

_EXPORTS: dict[str, str] = {
    # Layer 0: Exceptions
    "AgySwitchError": "agy_switcher.exceptions",
    "InvalidTokenError": "agy_switcher.exceptions",
    "AccountNotFoundError": "agy_switcher.exceptions",
    "ActiveTurnRunningError": "agy_switcher.exceptions",
    "DaemonError": "agy_switcher.exceptions",
    "DaemonAlreadyRunningError": "agy_switcher.exceptions",
    # Layer 0: Models
    "DiagnosticItem": "agy_switcher.models",
    "DiagnosticReport": "agy_switcher.models",
    "QuotaStatus": "agy_switcher.models",
    "EvaluationCandidate": "agy_switcher.models",
    # Layer 0: Security
    "_atomic_write_file": "agy_switcher.security",
    "_atomic_write_json": "agy_switcher.security",
    "extract_email_from_jwt": "agy_switcher.security",
    "validate_token_dict": "agy_switcher.security",
    "validate_token_file": "agy_switcher.security",
    "check_token_dict": "agy_switcher.security",
    "check_token_file": "agy_switcher.security",
    # Layer 1: Supervisor
    "ProcessSupervisor": "agy_switcher.supervisor",
    # Layer 1: Formatters
    "format_countdown": "agy_switcher.formatters",
    "format_accounts_listicle": "agy_switcher.formatters",
    "format_accounts_table": "agy_switcher.formatters",
    "format_status_report": "agy_switcher.formatters",
    "format_diagnostic_report": "agy_switcher.formatters",
    # Layer 1: Telemetry
    "QuotaClient": "agy_switcher.telemetry",
    "fetch_account_quota": "agy_switcher.telemetry",
    "fetch_all_accounts_quota": "agy_switcher.telemetry",
    "sort_accounts_by_quota": "agy_switcher.telemetry",
    "normalize_model_name": "agy_switcher.telemetry",
    "run_quota_pipeline": "agy_switcher.telemetry",
    "AUTH_PHRASES": "agy_switcher.telemetry",
    "AUTH_ERROR_CODE_PATTERN": "agy_switcher.telemetry",
    "HTTP_SERVER_ERROR_PATTERN": "agy_switcher.telemetry",
    "QUOTA_REGEX": "agy_switcher.telemetry",
    "_is_auth_error": "agy_switcher.telemetry",
    "_is_network_error": "agy_switcher.telemetry",
    "_is_stale_exhausted_cache": "agy_switcher.telemetry",
    "_parse_timestamp_defensive": "agy_switcher.telemetry",
    "_parse_quota_defensive": "agy_switcher.telemetry",
    "_sanitize_expires_in": "agy_switcher.telemetry",
    # Layer 2: Vault
    "AgySwitchEngine": "agy_switcher.vault",
    "DEFAULT_VAULT_DIR": "agy_switcher.vault",
    "DEFAULT_GEMINI_DIR": "agy_switcher.vault",
    "DEFAULT_LEGACY_ROOT": "agy_switcher.vault",

    # Layer 2: Rotation
    "RotationEngine": "agy_switcher.rotation",
    # Layer 2: Doctor
    "DoctorEngine": "agy_switcher.doctor",
    "run_doctor": "agy_switcher.doctor",
    "auto_heal": "agy_switcher.doctor",
    # Layer 2: Watcher & Presence
    "_open_secure_log_file": "agy_switcher.presence",
    "append_secure_log_entry": "agy_switcher.presence",
    "PresenceDetector": "agy_switcher.presence",
    "CooldownManager": "agy_switcher.watcher",
    "LogWatcher": "agy_switcher.watcher",
    "DaemonManager": "agy_switcher.watcher",
    # Layer 3: Hook
    "handle_pre_invocation_hook": "agy_switcher.hook",
    # Layer 4: CLI
    "build_parser": "agy_switcher.cli",
    "main": "agy_switcher.cli",
}

__all__ = list(_EXPORTS.keys())


def __getattr__(name: str) -> Any:
    if name in _EXPORTS:
        module_path = _EXPORTS[name]
        mod = importlib.import_module(module_path)
        val = getattr(mod, name)
        globals()[name] = val
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(list(globals().keys()) + __all__)
