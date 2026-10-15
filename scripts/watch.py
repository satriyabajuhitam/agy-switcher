#!/usr/bin/env python3
"""watch.py: Thin re-export shim launcher for Watcher Daemon.

Maintains 100% backward compatibility for direct execution, CLI dispatch,
and test mock patching. Delegates operational logic to agy_switcher.watcher.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

# Ensure physical script dir and repo root are in sys.path (resolving symlinks)
_real_script_dir = os.path.dirname(os.path.realpath(__file__))
if _real_script_dir not in sys.path:
    sys.path.insert(0, _real_script_dir)
_parent_dir = os.path.dirname(_real_script_dir)
if _parent_dir not in sys.path:
    sys.path.insert(0, _parent_dir)

from agy_switcher.exceptions import (
    AgySwitchError,
    DaemonError,
    DaemonAlreadyRunningError,
)
from agy_switcher.telemetry import QuotaClient
from agy_switcher.rotation import RotationEngine
try:
    from agy_switcher.vault import AgySwitchEngine
except ImportError:
    try:
        from agy_switch import AgySwitchEngine
    except ImportError:
        from scripts.agy_switch import AgySwitchEngine
from agy_switcher.watcher import (
    DEFAULT_VAULT_DIR,
    DEFAULT_GEMINI_DIR,
    DEFAULT_MAX_CHUNK_BYTES,
    _open_secure_log_file,
    append_secure_log_entry,
    PresenceDetector,
    CooldownManager,
    LogWatcher,
    DaemonManager,
    main,
)

__all__ = [
    "AgySwitchError",
    "DaemonError",
    "DaemonAlreadyRunningError",
    "QuotaClient",
    "RotationEngine",
    "AgySwitchEngine",
    "DEFAULT_VAULT_DIR",
    "DEFAULT_GEMINI_DIR",
    "DEFAULT_MAX_CHUNK_BYTES",
    "_open_secure_log_file",
    "append_secure_log_entry",
    "PresenceDetector",
    "CooldownManager",
    "LogWatcher",
    "DaemonManager",
    "main",
    "time",
    "shutil",
    "subprocess",
]

if __name__ == "__main__":
    sys.exit(main())
