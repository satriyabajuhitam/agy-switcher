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

from agy_switcher.exceptions import AccountNotFoundError, ActiveTurnRunningError
from agy_switcher.models import AccountInfo, RotationResult, FleetQuotaSummary
from agy_switcher.rotation import RotationEngine
from agy_switcher.security import extract_email_from_jwt
from agy_switcher.telemetry import normalize_model_name
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


def auto_rotate_on_exhaustion(
    model: str = "gemini",
    reload_server: bool = False,
    engine: AgySwitchEngine | None = None,
) -> RotationResult:
    """Rotates to the optimal account with remaining quota for target model using Disk-Only Swap."""
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
        previous_account = engine.get_active_name()
        if previous_account:
            previous_account = previous_account.strip()
        if not previous_account:
            previous_account = None
    except Exception:
        previous_account = None

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

    if previous_account:
        cooldowns[previous_account] = {"reason": "RESOURCE_EXHAUSTED", "model": model_key}

    rot_engine = RotationEngine(
        vault_dir=engine.vault_dir,
        active_account_id=previous_account,
        target_model=model_key,
    )
    target_account = rot_engine.evaluate(
        valid_accounts,
        current_active=previous_account,
        target_model=model_key,
        cooldowns=cooldowns,
    )

    if not target_account or target_account == previous_account:
        return RotationResult(
            success=False,
            previous_account=previous_account,
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
                previous_account=previous_account,
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
            previous_account=previous_account,
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

    return RotationResult(
        success=True,
        previous_account=previous_account,
        new_account=target_account,
        model=model,
        message=f"Successfully rotated to '{target_account}' for model '{model}'.",
        quota_remaining=quota_rem,
        reloaded_server=server_reloaded,
        active_account=target_account,
    )


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


__all__ = [
    "get_active_account",
    "auto_rotate_on_exhaustion",
    "switch_account",
    "AccountInfo",
    "RotationResult",
    "FleetQuotaSummary",
]
