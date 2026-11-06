"""In-Process SDK Facade for agy-switch.

Layer 4 Presentation module: Provides programmatic access for Python consumers
(e.g., agy-telemote TurnRunner) to inspect accounts, query quota telemetry,
and perform Disk-Only Swap failovers without subprocess execution overhead.

Zero external dependencies. Pure Python 3.10+ standard library.
Layer 4 DAG: May import Layer 0 (exceptions, models, security), Layer 1 (telemetry, supervisor),
and Layer 2 (vault, rotation).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agy_switcher.models import AccountInfo, RotationResult, FleetQuotaSummary
from agy_switcher.security import extract_email_from_jwt
from agy_switcher.vault import AgySwitchEngine


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


__all__ = [
    "get_active_account",
    "AccountInfo",
    "RotationResult",
    "FleetQuotaSummary",
]
