#!/usr/bin/env python3
"""agy-switch: High-reliability multi-account switcher for Antigravity 2.0 & CLI.

Thin re-export facade launcher providing 100% backward compatibility for all
legacy imports, dynamic test patches, and CLI executions.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Optional
import urllib.error
import urllib.parse
import urllib.request

# Ensure physical script dir and repo root are in sys.path (resolving symlinks)
_real_script_dir = os.path.dirname(os.path.realpath(__file__))
if _real_script_dir not in sys.path:
    sys.path.insert(0, _real_script_dir)
_parent_dir = os.path.dirname(_real_script_dir)
if _parent_dir not in sys.path:
    sys.path.insert(0, _parent_dir)

# Layer 0: Exceptions & Models
from agy_switcher.exceptions import (
    AccountNotFoundError,
    ActiveTurnRunningError,
    AgySwitchError,
    DaemonAlreadyRunningError,
    DaemonError,
    InvalidTokenError,
)
from agy_switcher.models import (
    DiagnosticItem,
    DiagnosticReport,
    EvaluationCandidate,
    QuotaStatus,
)

# Layer 1: Services, Formatters & Telemetry
from agy_switcher.supervisor import ProcessSupervisor
from agy_switcher.formatters import (
    format_accounts_listicle,
    format_accounts_table,
    format_countdown,
    format_diagnostic_report,
    format_status_report,
    _format_bucket_countdown,
    _format_gemini_quota_summary,
)
from agy_switcher.telemetry import (
    AUTH_ERROR_CODE_PATTERN,
    AUTH_PHRASES,
    HTTP_SERVER_ERROR_PATTERN,
    QUOTA_REGEX,
    QuotaClient,
    _has_auth_indicator,
    _is_auth_error,
    _is_network_error,
    _is_stale_exhausted_cache,
    _parse_quota_defensive,
    _parse_timestamp_defensive,
    _sanitize_expires_in,
    fetch_account_quota,
    fetch_all_accounts_quota,
    normalize_model_name,
    run_quota_pipeline,
    sort_accounts_by_quota,
)

# Layer 2: Vault, Rotation, Doctor & Watcher
from agy_switcher.vault import (
    DEFAULT_GEMINI_DIR,
    DEFAULT_LEGACY_ROOT,
    DEFAULT_VAULT_DIR,
    AgySwitchEngine,
    _atomic_write_file,
    _atomic_write_json,
    extract_email_from_jwt,
    validate_token_dict,
    validate_token_file,
)
from agy_switcher.rotation import RotationEngine
from agy_switcher.doctor import (
    DoctorEngine,
    auto_heal,
    run_doctor,
)
from agy_switcher.watcher import (
    CooldownManager,
    DaemonManager,
    LogWatcher,
    PresenceDetector,
    _open_secure_log_file,
)

# Layer 3: Hook Orchestration
from agy_switcher.hook import handle_pre_invocation_hook

# Layer 4: CLI Presentation & Dispatch
from agy_switcher.cli import build_parser, main

__all__ = [
    # Layer 0
    "AgySwitchError",
    "InvalidTokenError",
    "AccountNotFoundError",
    "ActiveTurnRunningError",
    "DaemonError",
    "DaemonAlreadyRunningError",
    "DiagnosticItem",
    "DiagnosticReport",
    "QuotaStatus",
    "EvaluationCandidate",
    # Layer 1
    "ProcessSupervisor",
    "format_countdown",
    "format_accounts_listicle",
    "format_accounts_table",
    "format_status_report",
    "format_diagnostic_report",
    "_format_bucket_countdown",
    "_format_gemini_quota_summary",
    "QuotaClient",
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
    "validate_token_dict",
    "validate_token_file",
    "RotationEngine",
    "DoctorEngine",
    "run_doctor",
    "auto_heal",
    "PresenceDetector",
    "CooldownManager",
    "LogWatcher",
    "DaemonManager",
    "_open_secure_log_file",
    # Layer 3
    "handle_pre_invocation_hook",
    # Layer 4
    "build_parser",
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
]


if __name__ == "__main__":
    sys.exit(main())
