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
    # Layer 1: Supervisor
    "ProcessSupervisor": "agy_switcher.supervisor",
    # Layer 1: Formatters
    "format_countdown": "agy_switcher.formatters",
    "format_accounts_listicle": "agy_switcher.formatters",
    "format_accounts_table": "agy_switcher.formatters",
    "format_status_report": "agy_switcher.formatters",
    "format_diagnostic_report": "agy_switcher.formatters",
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
