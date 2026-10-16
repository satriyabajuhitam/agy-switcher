"""Layer 2 Domain Engine: Vault Engine & Unified Facade.

Composes ProcessSupervisor (Layer 1) and DoctorEngine (Layer 2) to provide
atomic vault CRUD, dual-destination token swaps, account aliases, config persistence,
and transparent supervisor/diagnostic delegation.

Zero external dependencies. Pure Python 3.10+ standard library.
Strictly acyclic DAG: Depends on Layer 0 (exceptions, models) and Layer 1 (supervisor, telemetry).
Does not import Layer 3 (hook) or Layer 4 (cli).
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any

from agy_switcher.exceptions import (
    AccountNotFoundError,
    ActiveTurnRunningError,
    AgySwitchError,
    InvalidThresholdError,
    InvalidTokenError,
)
from agy_switcher.models import DiagnosticItem, DiagnosticReport
from agy_switcher.security import (
    _atomic_write_file,
    _atomic_write_json,
    extract_email_from_jwt,
    validate_token_dict,
    validate_token_file,
)
from agy_switcher.supervisor import ProcessSupervisor
from agy_switcher.telemetry import (
    _parse_timestamp_defensive,
    normalize_model_name,
)
from agy_switcher.doctor import DoctorEngine

DEFAULT_VAULT_DIR = Path.home() / ".antigravity-vault"
DEFAULT_GEMINI_DIR = Path.home() / ".gemini"
DEFAULT_LEGACY_ROOT = Path.home() / ".agy-cli-manager"


class AgySwitchEngine:

    """Unified Facade managing account vaulting, auto-migration, dual-destination token swaps, and diagnostics."""

    def __init__(
        self,
        vault_dir: Path | str | None = None,
        gemini_dir: Path | str | None = None,
        legacy_root: Path | str | None = None,
        proc_dir: Path | str | None = None,
        cli_dir: Path | str | None = None,
    ) -> None:
        self._vault_dir = Path(vault_dir or DEFAULT_VAULT_DIR).expanduser()
        self._gemini_dir = Path(gemini_dir or DEFAULT_GEMINI_DIR).expanduser()
        self.legacy_root = Path(legacy_root or DEFAULT_LEGACY_ROOT).expanduser()
        self._proc_dir = Path(proc_dir) if proc_dir else Path("/proc")
        self.cli_dir = Path(cli_dir).expanduser() if cli_dir else (self._gemini_dir / "antigravity-cli")
        self.accounts_dir = self._vault_dir / "accounts"
        self.active_file = self._vault_dir / "active"
        self.cache_dir = self._vault_dir / "cache"
        self.cache_file = self.cache_dir / "last_known_quota.json"
        self.cooldown_file = self.cache_dir / "cooldown.json"
        self.config_file = self._vault_dir / "config.json"
        self.desktop_token_file = self._gemini_dir / "jetski-standalone-oauth-token"
        self.cli_token_file = self.cli_dir / "antigravity-oauth-token"

        self.supervisor = ProcessSupervisor(gemini_dir=self._gemini_dir, proc_dir=self._proc_dir)
        self.doctor = DoctorEngine(engine=self)

        self.init_vault()

    @property
    def vault_dir(self) -> Path:
        return self._vault_dir

    @vault_dir.setter
    def vault_dir(self, val: Path | str) -> None:
        self._vault_dir = Path(val).expanduser()
        self.accounts_dir = self._vault_dir / "accounts"
        self.active_file = self._vault_dir / "active"
        self.cache_dir = self._vault_dir / "cache"
        self.cache_file = self.cache_dir / "last_known_quota.json"
        self.cooldown_file = self.cache_dir / "cooldown.json"
        self.config_file = self._vault_dir / "config.json"

    @property
    def gemini_dir(self) -> Path:
        return self._gemini_dir

    @gemini_dir.setter
    def gemini_dir(self, val: Path | str) -> None:
        self._gemini_dir = Path(val).expanduser()
        if hasattr(self, "supervisor") and self.supervisor is not None:
            self.supervisor.gemini_dir = self._gemini_dir
        self.desktop_token_file = self._gemini_dir / "jetski-standalone-oauth-token"
        if not hasattr(self, "cli_dir") or not self.cli_dir:
            self.cli_token_file = self._gemini_dir / "antigravity-cli" / "antigravity-oauth-token"

    @property
    def proc_dir(self) -> Path:
        return self._proc_dir

    @proc_dir.setter
    def proc_dir(self, val: Path | str) -> None:
        self._proc_dir = Path(val) if val else Path("/proc")
        if hasattr(self, "supervisor") and self.supervisor is not None:
            self.supervisor.proc_dir = self._proc_dir

    def init_vault(self) -> None:
        """Ensures ~/.antigravity-vault/ and required subdirectories exist."""
        self.vault_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(self.vault_dir, 0o700)
        except OSError:
            pass
        self.accounts_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(self.accounts_dir, 0o700)
        except OSError:
            pass
        self.cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(self.cache_dir, 0o700)
        except OSError:
            pass

    def validate_token_dict(self, data: Any) -> dict[str, Any]:
        """Validates that data complies with Google OAuth token JSON schema."""
        return validate_token_dict(data)

    def validate_token_file(self, token_path: Path) -> dict[str, Any]:
        """Reads and validates a token file from disk."""
        return validate_token_file(token_path)

    def get_account_tokens(self, name: str) -> dict[str, str | None] | None:
        """Retrieves and returns access_token and refresh_token for a given account.

        Returns a dict with 'access_token' and 'refresh_token', or None if account
        or token file does not exist or is invalid.
        """
        token_path = self.accounts_dir / name / "token.json"
        if not token_path.is_file():
            return None
        try:
            token_data = self.validate_token_file(token_path)
            token_payload = token_data.get("token", {})
            return {
                "access_token": token_payload.get("access_token"),
                "refresh_token": token_payload.get("refresh_token"),
            }
        except InvalidTokenError:
            return None

    def extract_email_from_jwt(self, id_token: str) -> str | None:
        """Decodes JWT payload to extract user email without external dependencies."""
        return extract_email_from_jwt(id_token)

    def auto_migrate_if_needed(self, legacy_root: Path | None = None) -> list[str]:
        """Imports accounts from legacy agy-cli-manager if vault is empty."""
        root = Path(legacy_root or self.legacy_root)
        legacy_accounts_dir = root / "accounts"

        if not legacy_accounts_dir.is_dir():
            return []

        # Only auto-migrate if vault has no accounts yet
        existing_accounts = [p for p in self.accounts_dir.iterdir() if p.is_dir()]
        if existing_accounts:
            return []

        migrated: list[str] = []
        if legacy_accounts_dir.is_dir():
            for account_path in sorted(legacy_accounts_dir.iterdir()):
                if not account_path.is_dir():
                    continue
                name = account_path.name
                legacy_token = account_path / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
                if not legacy_token.is_file():
                    continue

                try:
                    token_data = self.validate_token_file(legacy_token)
                except InvalidTokenError:
                    continue

                target_acc_dir = self.accounts_dir / name
                target_acc_dir.mkdir(parents=True, exist_ok=True)
                target_token = target_acc_dir / "token.json"

                # Write atomically with 0600 permissions
                self._atomic_write_json(target_token, token_data)
                migrated.append(name)

            # Sync active account from legacy state.json if present
            legacy_state_file = root / "state.json"
            if legacy_state_file.is_file() and migrated:
                try:
                    state_data = json.loads(legacy_state_file.read_text(encoding="utf-8"))
                    legacy_active = state_data.get("active")
                    if legacy_active in migrated:
                        self.set_active_name(legacy_active)
                except Exception:
                    pass

        # If still no accounts migrated and vault is empty, harvest live tokens from ~/.gemini
        if not migrated and not [p for p in self.accounts_dir.iterdir() if p.is_dir()]:
            live_account = self.harvest_live_tokens_if_needed()
            if live_account:
                migrated.append(live_account)

        return migrated

    def harvest_live_tokens_if_needed(self) -> str | None:
        """If vault has no accounts and live tokens exist in ~/.gemini, imports them as active account."""
        existing_accounts = [p for p in self.accounts_dir.iterdir() if p.is_dir()]
        if existing_accounts:
            return None

        # Check Desktop Antigravity 2.0 token first, then CLI token
        jetski_token = self.desktop_token_file
        cli_token = self.cli_token_file

        source_token = None
        if jetski_token.is_file():
            source_token = jetski_token
        elif cli_token.is_file():
            source_token = cli_token

        if source_token is None:
            return None

        try:
            token_data = self.validate_token_file(source_token)
        except InvalidTokenError:
            return None

        email = self.extract_email_from_jwt(token_data.get("id_token", ""))
        account_name = email.split("@")[0] if email and "@" in email else "primary"
        target_dir = self.accounts_dir / account_name
        target_dir.mkdir(parents=True, exist_ok=True)
        self._atomic_write_json(target_dir / "token.json", token_data)
        self.set_active_name(account_name)
        return account_name

    def _atomic_write_file(self, target_path: Path, content: str, mode: int = 0o600) -> None:
        """Writes content to a target path atomically with specified permissions."""
        _atomic_write_file(target_path, content, mode=mode)

    def _atomic_write_json(self, target_path: Path, payload: dict[str, Any]) -> None:
        """Writes JSON to a target path atomically with 0600 permissions."""
        _atomic_write_json(target_path, payload)

    def _sync_tokens_to_targets(self, token_payload: str) -> None:
        """Atomically emits token payload to both Desktop and CLI target files."""
        self._atomic_write_file(self.desktop_token_file, token_payload, mode=0o600)
        self._atomic_write_file(self.cli_token_file, token_payload, mode=0o600)

    def update_account_access_token(self, name: str, access_token: str, expiry: str | None = None) -> bool:
        """Updates the access token in vault and syncs to active targets if current."""
        if not name or not isinstance(name, str) or not name.strip():
            return False
        name = name.strip()
        if not access_token or not isinstance(access_token, str) or not access_token.strip():
            return False
        token_path = self.accounts_dir / name / "token.json"
        if not token_path.is_file():
            return False
        try:
            raw = token_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if "token" not in data or not isinstance(data["token"], dict):
                return False
            data["token"]["access_token"] = access_token.strip()
            if expiry is not None:
                if isinstance(expiry, str) and expiry.strip():
                    data["token"]["expiry"] = expiry.strip()
            self._atomic_write_json(token_path, data)

            if name == self.get_active_name():
                token_payload = json.dumps(data, indent=2) + "\n"
                self._sync_tokens_to_targets(token_payload)
            return True
        except Exception:
            return False

    def load_config(self) -> dict[str, Any]:
        """Loads vault configuration from ~/.antigravity-vault/config.json."""
        if self.config_file.is_file():
            try:
                return json.loads(self.config_file.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def save_config(self, config: dict[str, Any]) -> None:
        """Saves vault configuration atomically with 0600 permissions."""
        self._atomic_write_json(self.config_file, config)

    def get_auto_switch(self) -> bool:
        """Returns whether auto-switch is enabled in vault config."""
        cfg = self.load_config()
        return bool(cfg.get("auto_switch", False))

    def set_auto_switch(self, enabled: bool) -> dict[str, Any]:
        """Sets auto-switch state and saves to config.json."""
        cfg = self.load_config()
        cfg["auto_switch"] = bool(enabled)
        self.save_config(cfg)
        return cfg

    def get_default_model(self) -> str:
        """Returns configured default model from config.json, defaulting to 'gemini'."""
        cfg = self.load_config()
        raw = cfg.get("default_model")
        if raw and isinstance(raw, str):
            try:
                return normalize_model_name(raw)
            except AgySwitchError:
                return "gemini"
        return "gemini"

    def set_default_model(self, model: str) -> dict[str, Any]:
        """Persists validated default model ('gemini' or 'claude') to config.json."""
        normalized = normalize_model_name(model)
        cfg = self.load_config()
        cfg["default_model"] = normalized
        self.save_config(cfg)
        return cfg

    def get_quota_threshold(self) -> float:
        """Returns configured quota threshold fraction, defaulting to 0.25 (25%)."""
        cfg = self.load_config()
        raw = cfg.get("quota_threshold")
        if raw is not None:
            try:
                val = float(raw)
                if val > 1.0:
                    val = val / 100.0
                if 0.0 < val <= 1.0:
                    return val
            except (ValueError, TypeError):
                pass
        return 0.25

    def set_quota_threshold(self, threshold: float | int | str) -> dict[str, Any]:
        """Persists validated quota threshold (percentage or fraction) to config.json."""
        try:
            val = float(threshold)
        except (ValueError, TypeError):
            raise InvalidThresholdError(f"Invalid threshold '{threshold}'. Must be a number between 1 and 100.")

        if val > 1.0:
            fraction = val / 100.0
        else:
            fraction = val

        if not (0.0 < fraction <= 1.0):
            raise InvalidThresholdError(f"Threshold must be between 1% (0.01) and 100% (1.0). Got: {threshold}")

        cfg = self.load_config()
        cfg["quota_threshold"] = round(fraction, 4)
        self.save_config(cfg)
        return cfg

    def get_cooldowns(self) -> dict[str, Any]:
        """Returns currently active cooldown records from cache/cooldown.json."""
        cooldown_file = self.cache_dir / "cooldown.json"
        if not cooldown_file.is_file():
            return {}
        try:
            raw = json.loads(cooldown_file.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return {}
            now = time.time()
            active = {}
            for acc, rec in raw.items():
                until = 0.0
                if isinstance(rec, dict):
                    until = _parse_timestamp_defensive(rec.get("cooldown_until"))
                elif isinstance(rec, (int, float, str)):
                    until = _parse_timestamp_defensive(rec)
                if math.isfinite(until) and until > now:
                    active[acc] = rec
            return active
        except Exception:
            return {}

    def install_hook(self, hooks_file: Path | None = None) -> Path:
        """Installs the PreInvocation lifecycle hook into hooks.json via deep JSON merge."""
        target_hooks = Path(hooks_file or (self.gemini_dir / "config" / "hooks.json")).expanduser()
        target_hooks.parent.mkdir(parents=True, exist_ok=True)

        existing: dict[str, Any] = {}
        if target_hooks.is_file():
            try:
                content = target_hooks.read_text(encoding="utf-8").strip()
                if content:
                    loaded = json.loads(content)
                    if isinstance(loaded, dict):
                        existing = loaded
            except Exception:
                existing = {}

        launcher_script = Path(__file__).resolve()
        if launcher_script.name != "agy_switch.py":
            candidate = launcher_script.parent.parent / "agy_switch.py"
            if candidate.is_file():
                launcher_script = candidate
        hook_cmd = f"python3 {shlex.quote(str(launcher_script))} hook-pre-invocation"

        hook_def = {
            "enabled": True,
            "PreInvocation": [
                {
                    "type": "command",
                    "command": hook_cmd,
                    "timeout": 5,
                }
            ],
        }

        existing["agy-switch-auto-rotate"] = hook_def
        self._atomic_write_json(target_hooks, existing)
        return target_hooks

    def get_active_name(self) -> str | None:
        """Returns the name of the currently active account, or None."""
        if not self.active_file.is_file():
            return None
        try:
            name = self.active_file.read_text(encoding="utf-8").strip()
            return name if name else None
        except OSError:
            return None

    def set_active_name(self, name: str) -> None:
        """Sets the active account name in the vault."""
        self._atomic_write_file(self.active_file, f"{name.strip()}\n", mode=0o600)

    def get_active_account(self) -> str | None:
        """Alias for get_active_name."""
        return self.get_active_name()

    def set_active_account(self, name: str) -> None:
        """Alias for set_active_name."""
        self.set_active_name(name)

    def list_accounts(self) -> dict[str, Any]:
        """Lists all accounts registered in the vault and marks the active account."""
        active = self.get_active_name()
        accounts: list[dict[str, Any]] = []

        if self.accounts_dir.is_dir():
            for acc_path in sorted(self.accounts_dir.iterdir()):
                if not acc_path.is_dir() or acc_path.name.startswith("."):
                    continue
                name = acc_path.name
                token_file = acc_path / "token.json"
                email = None
                valid = False

                if token_file.is_file():
                    try:
                        data = self.validate_token_file(token_file)
                        email = self.extract_email_from_jwt(data.get("id_token", ""))
                        valid = True
                    except InvalidTokenError:
                        pass

                accounts.append({
                    "name": name,
                    "email": email,
                    "valid": valid,
                    "is_active": (name == active),
                })

        return {
            "active": active,
            "accounts": accounts,
        }

    def switch_tokens(
        self,
        name: str,
        restart_server: bool = True,
        force: bool = False,
    ) -> dict[str, Any]:
        """Atomically switches active tokens across both Antigravity 2.0 and CLI surfaces."""
        acc_dir = self.accounts_dir / name
        token_file = acc_dir / "token.json"

        if not acc_dir.is_dir() or not token_file.is_file():
            available = sorted(
                p.name for p in self.accounts_dir.iterdir()
                if p.is_dir() and not p.name.startswith(".")
            ) if self.accounts_dir.is_dir() else []
            raise AccountNotFoundError(
                f"Account '{name}' not found in vault ({self.accounts_dir}). "
                f"Available accounts: {', '.join(available) or 'None'}"
            )

        # Validate token payload before touching live target files
        token_data = self.validate_token_file(token_file)
        token_payload = json.dumps(token_data, indent=2) + "\n"

        # 1 & 2. Update Antigravity 2.0 (WSL / Desktop) and CLI tokens atomically
        self._sync_tokens_to_targets(token_payload)

        # 3. Update vault active marker
        self.set_active_name(name)

        # 4. Backward-compatibility bridge: Update ~/.agy-cli-manager/state.json if it exists
        legacy_state_file = self.legacy_root / "state.json"
        if legacy_state_file.is_file():
            try:
                state_data = json.loads(legacy_state_file.read_text(encoding="utf-8"))
                if isinstance(state_data, dict):
                    state_data["active"] = name
                    self._atomic_write_json(legacy_state_file, state_data)
            except Exception:
                pass

        email = self.extract_email_from_jwt(token_data.get("id_token", ""))

        server_restarted = False
        if restart_server:
            server_restarted = self.restart_language_server(force=force)

        return {
            "active": name,
            "email": email,
            "server_restarted": server_restarted,
        }

    def switch_account(
        self,
        name: str,
        force: bool = False,
        no_restart: bool = False,
    ) -> dict[str, Any]:
        """Unified Facade alias for switch_tokens matching specification."""
        return self.switch_tokens(name=name, restart_server=not no_restart, force=force)

    def add_account(self, name: str, token_path: Path) -> dict[str, Any]:
        """Validates and registers a new account in the vault."""
        clean_name = name.strip()
        if not clean_name:
            raise AgySwitchError("Account name cannot be empty.")

        token_file = Path(token_path)
        token_data = self.validate_token_file(token_file)

        acc_dir = self.accounts_dir / clean_name
        acc_dir.mkdir(parents=True, exist_ok=True)
        dest_token = acc_dir / "token.json"
        self._atomic_write_json(dest_token, token_data)

        email = self.extract_email_from_jwt(token_data.get("id_token", ""))
        return {"name": clean_name, "email": email}

    def remove_account(self, name: str, force: bool = False) -> None:
        """Removes an account from the vault (cannot remove active account without --force)."""
        clean_name = name.strip()
        acc_dir = self.accounts_dir / clean_name
        if not acc_dir.is_dir():
            raise AccountNotFoundError(f"Account '{clean_name}' not found in vault.")

        active = self.get_active_name()
        if active == clean_name:
            if not force:
                raise AgySwitchError(f"Cannot remove currently active account '{clean_name}' without --force.")
            self.active_file.unlink(missing_ok=True)

        shutil.rmtree(acc_dir)

    @staticmethod
    def validate_account_alias(alias: str) -> str:
        """Validates that an account alias matches ^[a-zA-Z0-9._-]+$ with length 1–64,

        rejecting spaces, slashes, and path traversal elements ('..', '.').
        """
        if not isinstance(alias, str):
            raise AgySwitchError("Account alias must be a string.")
        clean = alias.strip()
        if not clean:
            raise AgySwitchError("Account alias cannot be empty.")
        if len(clean) > 64:
            raise AgySwitchError(f"Account alias too long ({len(clean)} > 64 chars): '{clean}'")
        if not re.match(r"^[a-zA-Z0-9._-]+$", clean):
            raise AgySwitchError(
                f"Invalid account alias '{clean}'. Allowed characters: letters, digits, '.', '_', '-'"
            )
        if ".." in clean or clean in (".", ".."):
            raise AgySwitchError(f"Account alias contains invalid path traversal element: '{clean}'")
        return clean

    def _validate_preflight_writes(self) -> None:
        """Verifies write access on accounts_dir, active_file, cache_file, and cooldown_file before mutating."""
        if not os.access(self.accounts_dir, os.W_OK | os.X_OK):
            raise AgySwitchError(f"Pre-flight check failed: accounts directory '{self.accounts_dir}' is not writable.")

        targets = [
            (self.active_file, "active pointer file"),
            (self.cache_file, "cache file"),
            (self.cooldown_file, "cooldown file"),
        ]
        for path, desc in targets:
            parent = path.parent
            if not (parent.exists() and os.access(parent, os.W_OK | os.X_OK)):
                raise AgySwitchError(
                    f"Pre-flight check failed: directory for {desc} '{parent}' is not writable."
                )
            if path.exists() and not os.access(path, os.W_OK):
                raise AgySwitchError(f"Pre-flight check failed: {desc} '{path}' is not writable.")

    def rename_account(
        self,
        old_alias: str,
        new_alias: str,
        force: bool = False,
    ) -> dict[str, Any]:
        """Atomically renames account, updates active pointer with rollback guarantee, and migrates cache/cooldown.

        Returns:
            dict with structure:
            {
                "old_alias": str,
                "new_alias": str,
                "is_active": bool,
                "cache_migrated": bool,
                "cooldown_migrated": bool
            }
        """
        if not isinstance(old_alias, str):
            raise AgySwitchError("Account alias must be a string.")
        clean_old = old_alias.strip()
        if not clean_old:
            raise AgySwitchError("Account alias cannot be empty.")

        if not isinstance(new_alias, str):
            raise AgySwitchError("Account alias must be a string.")
        clean_new = new_alias.strip()
        if not clean_new:
            raise AgySwitchError("Account alias cannot be empty.")

        if clean_old == clean_new:
            raise AgySwitchError("Source and target account alias must be different.")

        clean_old = self.validate_account_alias(clean_old)
        clean_new = self.validate_account_alias(clean_new)

        old_dir = self.accounts_dir / clean_old
        if (
            not old_dir.is_dir()
            or old_dir.is_symlink()
            or old_dir.resolve().parent != self.accounts_dir.resolve()
        ):
            available = sorted(
                p.name for p in self.accounts_dir.iterdir()
                if p.is_dir() and not p.name.startswith(".")
            )
            raise AccountNotFoundError(
                f"Account '{clean_old}' not found in vault ({self.accounts_dir}). "
                f"Available accounts: {', '.join(available) or 'None'}"
            )

        new_dir = self.accounts_dir / clean_new
        is_case_only_rename = (clean_old.lower() == clean_new.lower())

        if new_dir.exists() or new_dir.is_symlink():
            if is_case_only_rename:
                try:
                    if not old_dir.samefile(new_dir):
                        raise AgySwitchError(f"Account '{clean_new}' already exists in vault.")
                except OSError:
                    pass
            else:
                raise AgySwitchError(f"Account '{clean_new}' already exists in vault.")

        active_name = self.get_active_name()
        is_active = (active_name is not None and active_name.strip() == clean_old)

        if is_active:
            pid = self.locate_language_server_pid()
            if self.is_server_busy(pid) and not force:
                raise ActiveTurnRunningError(
                    f"Antigravity is currently busy processing an agent turn or background task. "
                    f"Cannot rename active account '{clean_old}'. Use --force to proceed anyway."
                )

        # Pre-flight write checks
        self._validate_preflight_writes()

        # Step 1: Directory rename (using temporary intermediate directory if case-only rename)
        if is_case_only_rename:
            tmp_rename = self.accounts_dir / f".tmp-rename-{os.getpid()}-{os.urandom(6).hex()}"
            old_dir.rename(tmp_rename)
            try:
                tmp_rename.rename(new_dir)
            except Exception as e:
                try:
                    tmp_rename.rename(old_dir)
                except Exception as rb_exc:
                    raise AgySwitchError(
                        f"Failed to rename account directory: {e}. Rollback also failed: {rb_exc}"
                    ) from e
                raise AgySwitchError(f"Failed to rename account directory: {e}") from e
        else:
            try:
                old_dir.rename(new_dir)
            except Exception as e:
                raise AgySwitchError(f"Failed to rename account directory: {e}") from e

        # Step 2: Update active pointer if active, with rollback guarantee
        if is_active:
            try:
                self.set_active_name(clean_new)
            except Exception as exc:
                # Rollback directory rename
                try:
                    if is_case_only_rename:
                        tmp_rb = self.accounts_dir / f".tmp-rename-rb-{os.getpid()}-{os.urandom(6).hex()}"
                        new_dir.rename(tmp_rb)
                        tmp_rb.rename(old_dir)
                    else:
                        new_dir.rename(old_dir)
                except Exception as rb_exc:
                    raise AgySwitchError(
                        f"Failed to update active account pointer: {exc}. Rollback also failed: {rb_exc}"
                    ) from exc
                # Also ensure active pointer points to old account if partially touched
                try:
                    if self.get_active_name() != clean_old:
                        self.set_active_name(clean_old)
                except Exception:
                    pass
                raise AgySwitchError(f"Failed to update active account pointer: {exc}") from exc

            # Backward-compatibility bridge: Update ~/.agy-cli-manager/state.json if it exists
            legacy_state_file = self.legacy_root / "state.json"
            if legacy_state_file.is_file():
                try:
                    state_data = json.loads(legacy_state_file.read_text(encoding="utf-8"))
                    if isinstance(state_data, dict) and state_data.get("active") == clean_old:
                        state_data["active"] = clean_new
                        self._atomic_write_json(legacy_state_file, state_data)
                except Exception:
                    pass

        # Step 3: Migrate telemetry cache key
        cache_migrated = False
        if self.cache_file.is_file():
            try:
                raw_cache = self.cache_file.read_text(encoding="utf-8")
                cache_data = json.loads(raw_cache)
                if isinstance(cache_data, dict):
                    migrated = False
                    accounts_map = cache_data.get("accounts")
                    if isinstance(accounts_map, dict) and clean_old in accounts_map:
                        accounts_map[clean_new] = accounts_map.pop(clean_old)
                        migrated = True
                    # Legacy top-level account map fallback (ignoring metadata keys)
                    if clean_old not in ("accounts", "timestamp") and clean_old in cache_data:
                        cache_data[clean_new] = cache_data.pop(clean_old)
                        migrated = True
                    if migrated:
                        self._atomic_write_json(self.cache_file, cache_data)
                        cache_migrated = True
            except json.JSONDecodeError as exc:
                print(f"[agy-switch] Warning: Malformed cache file '{self.cache_file}': {exc}", file=sys.stderr)
            except Exception as exc:
                print(f"[agy-switch] Warning: Failed to migrate cache key: {exc}", file=sys.stderr)

        # Step 4: Migrate cooldown key
        cooldown_migrated = False
        if self.cooldown_file.is_file():
            try:
                raw_cd = self.cooldown_file.read_text(encoding="utf-8")
                cd_data = json.loads(raw_cd)
                if isinstance(cd_data, dict) and clean_old in cd_data:
                    cd_data[clean_new] = cd_data.pop(clean_old)
                    self._atomic_write_json(self.cooldown_file, cd_data)
                    cooldown_migrated = True
            except json.JSONDecodeError as exc:
                print(f"[agy-switch] Warning: Malformed cooldown file '{self.cooldown_file}': {exc}", file=sys.stderr)
            except Exception as exc:
                print(f"[agy-switch] Warning: Failed to migrate cooldown key: {exc}", file=sys.stderr)

        return {
            "old_alias": clean_old,
            "new_alias": clean_new,
            "is_active": is_active,
            "cache_migrated": cache_migrated,
            "cooldown_migrated": cooldown_migrated,
        }

    def resolve_agy_binary(self, override_bin: str | None = None) -> str:
        """Finds the agy CLI binary path."""
        if override_bin and override_bin.strip():
            candidate = Path(override_bin.strip()).expanduser()
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
            raise AgySwitchError(f"Specified agy binary not found or not executable: {override_bin}")

        # 1. Check AGY_BINARY environment variable
        env_bin = os.getenv("AGY_BINARY", "").strip()
        if env_bin:
            candidate = Path(env_bin).expanduser()
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)

        # 2. Check standard user local bin ~/.local/bin/agy
        default_local = Path.home() / ".local" / "bin" / "agy"
        if default_local.is_file() and os.access(default_local, os.X_OK):
            return str(default_local)

        # 3. Check system PATH
        path_bin = shutil.which("agy")
        if path_bin:
            return path_bin

        raise AgySwitchError("Antigravity CLI binary ('agy') not found in ~/.local/bin/agy or system PATH.")

    def login_account(
        self,
        name: str,
        agy_binary: str | None = None,
        timeout: int = 600,
        force: bool = False,
        prompt_fn: Any | None = None,
    ) -> dict[str, Any]:
        """Authenticates a new account via isolated agy CLI sandbox and saves to vault as Standby."""
        clean_name = name.strip()
        if not clean_name:
            raise AgySwitchError("Account name cannot be empty.")

        acc_dir = self.accounts_dir / clean_name
        if acc_dir.is_dir() and not force:
            prompt_msg = f"Account '{clean_name}' already exists in vault. Overwrite? [y/N]: "
            if prompt_fn is not None:
                resp = prompt_fn(prompt_msg)
            else:
                try:
                    resp = input(prompt_msg)
                except (EOFError, OSError):
                    resp = "n"
            if str(resp).strip().lower() not in {"y", "yes"}:
                raise AgySwitchError(f"Account '{clean_name}' already exists in vault. Overwrite aborted.")

        resolved_binary = self.resolve_agy_binary(agy_binary)

        with tempfile.TemporaryDirectory(prefix="agy-login-") as temp_dir_str:
            temp_home = Path(temp_dir_str)
            gemini_login_dir = temp_home / ".gemini"
            gemini_login_dir.mkdir(parents=True, exist_ok=True)

            env = os.environ.copy()
            env["HOME"] = str(temp_home)
            env["PATH"] = f"{Path.home()}/.local/bin:{env.get('PATH', '/bin:/usr/bin:/usr/local/bin')}"

            try:
                proc = subprocess.Popen(
                    [resolved_binary],
                    stdin=sys.stdin,
                    stdout=sys.stdout,
                    stderr=sys.stderr,
                    cwd=str(temp_home),
                    env=env,
                )
            except Exception as exc:
                raise AgySwitchError(f"Failed to spawn agy login process: {exc}") from exc

            start_time = time.time()
            try:
                while proc.poll() is None:
                    if time.time() - start_time > timeout:
                        proc.terminate()
                        try:
                            proc.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                        raise AgySwitchError(f"Login timed out after {timeout} seconds.")
                    time.sleep(0.3)
            except KeyboardInterrupt:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                raise AgySwitchError("Login process interrupted by user.")

            token_file = temp_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
            if not token_file.is_file():
                jetski_token = temp_home / ".gemini" / "jetski-standalone-oauth-token"
                if jetski_token.is_file():
                    token_file = jetski_token
                else:
                    raise AgySwitchError("agy login did not produce a usable auth token profile.")

            token_data = self.validate_token_file(token_file)
            acc_dir.mkdir(parents=True, exist_ok=True)
            self._atomic_write_json(acc_dir / "token.json", token_data)
            email = self.extract_email_from_jwt(token_data.get("id_token", ""))

            return {
                "name": clean_name,
                "email": email,
                "status": "standby",
            }

    def locate_language_server_pid(self) -> int | None:
        """Finds running language_server PID by scanning /proc or pgrep."""
        return self.supervisor.locate_language_server_pid()

    def is_server_busy(self, pid: int | None = None) -> bool:
        """Checks if language_server has active child processes or presence locks."""
        return self.supervisor.is_server_busy(pid)

    def _send_signal(self, pid: int, sig: int = signal.SIGTERM) -> None:
        """Dispatches signal to target process."""
        self.supervisor._send_signal(pid, sig)

    def _poll_process_exit(self, pid: int, timeout: float = 3.0) -> bool:
        """Polls until process with given pid has terminated."""
        return self.supervisor._poll_process_exit(pid, timeout=timeout)

    def restart_language_server(self, force: bool = False) -> bool:
        """Restarts language_server gracefully via SIGTERM so WSL Relay auto-respawns it.

        Delegates directly to ProcessSupervisor while transparently bridging any
        instance-level monkey-patched mocks on this engine instance.
        """
        bridge_attrs = ("_send_signal", "_poll_process_exit", "locate_language_server_pid", "is_server_busy")
        saved = {}
        had_instance_attr = set()
        for attr in bridge_attrs:
            if attr in self.__dict__:
                if attr in self.supervisor.__dict__:
                    had_instance_attr.add(attr)
                    saved[attr] = self.supervisor.__dict__[attr]
                setattr(self.supervisor, attr, getattr(self, attr))
        try:
            return self.supervisor.restart_language_server(force=force)
        finally:
            for attr in bridge_attrs:
                if attr in self.__dict__:
                    if attr in had_instance_attr:
                        setattr(self.supervisor, attr, saved[attr])
                    else:
                        self.supervisor.__dict__.pop(attr, None)

    def get_status(self) -> dict[str, Any]:
        """Returns health and status of the vault and running language_server."""
        active = self.get_active_name()
        pid = self.locate_language_server_pid()
        running = pid is not None
        busy = self.is_server_busy(pid) if running else False

        email = None
        if active:
            token_file = self.accounts_dir / active / "token.json"
            if token_file.is_file():
                try:
                    data = self.validate_token_file(token_file)
                    email = self.extract_email_from_jwt(data.get("id_token", ""))
                except Exception:
                    pass

        return {
            "active": active,
            "email": email,
            "server_running": running,
            "server_pid": pid,
            "server_busy": busy,
        }

    def _is_valid_expiry(self, raw_expiry: Any) -> bool:
        """Validates that expiry is a recognizable positive timestamp or ISO string."""
        return self.doctor._is_valid_expiry(raw_expiry)

    def _is_token_expired(self, raw_expiry: Any) -> bool:
        """Determines if a token expiry representation is already in the past."""
        return self.doctor._is_token_expired(raw_expiry)

    def run_diagnostics(self) -> DiagnosticReport:
        """Runs the 5-layer diagnostic suite via DoctorEngine."""
        return self.doctor.run_diagnostics()

    def run_doctor(self) -> DiagnosticReport:
        """Canonical alias for run_diagnostics."""
        return self.run_diagnostics()

    def fix_diagnostics(self, report: DiagnosticReport) -> list[str]:
        """Executes automated repairs for fixable diagnostic failures via DoctorEngine."""
        return self.doctor.fix_diagnostics(report)

    def auto_heal(self, report: DiagnosticReport) -> list[str]:
        """Canonical alias for fix_diagnostics."""
        return self.fix_diagnostics(report)


__all__ = [
    "DEFAULT_VAULT_DIR",
    "DEFAULT_GEMINI_DIR",
    "DEFAULT_LEGACY_ROOT",
    "_atomic_write_file",
    "_atomic_write_json",
    "extract_email_from_jwt",
    "validate_token_dict",
    "validate_token_file",
    "AgySwitchEngine",
]
