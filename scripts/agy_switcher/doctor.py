"""5-Layer Health Diagnostics and Self-Healing Engine.

Layer 2 Domain Engine: Depends on Layer 0 (exceptions, models) and Layer 1 (supervisor).
Zero external dependencies. Pure Python 3.10+ standard library.
Strictly acyclic DAG: Does NOT import vault.py at the module level.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import shlex
import shutil
import stat
import time
from typing import Any, Callable

from agy_switcher.exceptions import InvalidTokenError
from agy_switcher.models import DiagnosticItem, DiagnosticReport
from agy_switcher.security import (
    _atomic_write_file,
    _atomic_write_json,
    extract_email_from_jwt,
    validate_token_dict,
    validate_token_file,
)
from agy_switcher.supervisor import ProcessSupervisor

DEFAULT_VAULT_DIR = Path.home() / ".antigravity-vault"
DEFAULT_GEMINI_DIR = Path.home() / ".gemini"


class DoctorEngine:
    """Unified 5-layer health diagnostics and self-healing engine."""

    def __init__(
        self,
        vault_dir: Path | str | None = None,
        accounts_dir: Path | str | None = None,
        cache_dir: Path | str | None = None,
        config_file: Path | str | None = None,
        desktop_token_file: Path | str | None = None,
        cli_token_file: Path | str | None = None,
        supervisor: ProcessSupervisor | None = None,
        engine_callbacks: dict[str, Any] | None = None,
        *,
        gemini_dir: Path | str | None = None,
        proc_dir: Path | str | None = None,
        hook_installer: Callable[[Path | None], Path] | None = None,
        sync_tokens_callback: Callable[[str], None] | None = None,
        get_active_account_callback: Callable[[], str | None] | None = None,
        init_vault_callback: Callable[[], None] | None = None,
        engine: Any | None = None,
    ) -> None:
        self.engine = engine

        # Positional arguments disambiguation
        if isinstance(config_file, ProcessSupervisor):
            # Legacy 4-positional call: DoctorEngine(vault_dir, gemini_dir, proc_dir, supervisor)
            resolved_supervisor = config_file
            resolved_config = None
            resolved_gemini = gemini_dir or accounts_dir
            resolved_proc = proc_dir or cache_dir
            resolved_accounts = None
            resolved_cache = None
        elif (
            accounts_dir is not None
            and cache_dir is not None
            and config_file is None
            and desktop_token_file is None
            and cli_token_file is None
            and (str(cache_dir) in ("/proc", "/proc/") or cache_dir == Path("/proc"))
        ):
            # Legacy 3-positional call with /proc: DoctorEngine(vault_dir, gemini_dir, proc_dir)
            resolved_supervisor = supervisor
            resolved_config = None
            resolved_gemini = gemini_dir or accounts_dir
            resolved_proc = proc_dir or cache_dir
            resolved_accounts = None
            resolved_cache = None
        else:
            # Canonical spec constructor: DoctorEngine(vault_dir, accounts_dir, cache_dir, config_file, ...)
            resolved_supervisor = supervisor
            resolved_config = config_file
            resolved_gemini = gemini_dir
            resolved_proc = proc_dir
            resolved_accounts = accounts_dir
            resolved_cache = cache_dir

        if engine is not None:
            self._vault_dir = getattr(engine, "vault_dir", Path(vault_dir or DEFAULT_VAULT_DIR))
            self._gemini_dir = getattr(engine, "gemini_dir", Path(resolved_gemini or DEFAULT_GEMINI_DIR))
            self._proc_dir = getattr(engine, "proc_dir", Path(resolved_proc or "/proc"))
            self._supervisor = resolved_supervisor or getattr(engine, "supervisor", None)
        else:
            self._vault_dir = Path(vault_dir or DEFAULT_VAULT_DIR).expanduser()
            self._gemini_dir = Path(resolved_gemini or DEFAULT_GEMINI_DIR).expanduser()
            self._proc_dir = Path(resolved_proc) if resolved_proc else Path("/proc")
            self._supervisor = resolved_supervisor

        self._accounts_dir = Path(resolved_accounts).expanduser() if resolved_accounts else None
        self._cache_dir = Path(resolved_cache).expanduser() if resolved_cache else None
        self._config_file = Path(resolved_config).expanduser() if resolved_config else None
        self._desktop_token_file = Path(desktop_token_file).expanduser() if desktop_token_file else None
        self._cli_token_file = Path(cli_token_file).expanduser() if cli_token_file else None


        if engine_callbacks:
            sync_tokens_callback = (
                sync_tokens_callback
                or engine_callbacks.get("sync_tokens")
                or engine_callbacks.get("sync_tokens_callback")
            )
            get_active_account_callback = (
                get_active_account_callback
                or engine_callbacks.get("get_active_account")
                or engine_callbacks.get("get_active_account_callback")
            )
            init_vault_callback = (
                init_vault_callback
                or engine_callbacks.get("init_vault")
                or engine_callbacks.get("init_vault_callback")
            )
            hook_installer = (
                hook_installer
                or engine_callbacks.get("install_hook")
                or engine_callbacks.get("hook_installer")
            )

        self.hook_installer = hook_installer
        self.sync_tokens_callback = sync_tokens_callback
        self.get_active_account_callback = get_active_account_callback
        self.init_vault_callback = init_vault_callback

    @property
    def vault_dir(self) -> Path:
        if self.engine is not None and hasattr(self.engine, "vault_dir"):
            return Path(self.engine.vault_dir)
        return self._vault_dir

    @vault_dir.setter
    def vault_dir(self, val: Path | str) -> None:
        self._vault_dir = Path(val)

    @property
    def gemini_dir(self) -> Path:
        if self.engine is not None and hasattr(self.engine, "gemini_dir"):
            return Path(self.engine.gemini_dir)
        return self._gemini_dir

    @gemini_dir.setter
    def gemini_dir(self, val: Path | str) -> None:
        self._gemini_dir = Path(val)

    @property
    def proc_dir(self) -> Path:
        if self.engine is not None and hasattr(self.engine, "proc_dir"):
            return Path(self.engine.proc_dir)
        return self._proc_dir

    @proc_dir.setter
    def proc_dir(self, val: Path | str) -> None:
        self._proc_dir = Path(val)

    @property
    def accounts_dir(self) -> Path:
        if self._accounts_dir is not None:
            return self._accounts_dir
        if self.engine is not None and hasattr(self.engine, "accounts_dir"):
            return Path(self.engine.accounts_dir)
        return self.vault_dir / "accounts"

    @accounts_dir.setter
    def accounts_dir(self, val: Path | str) -> None:
        self._accounts_dir = Path(val).expanduser()

    @property
    def active_file(self) -> Path:
        return self.vault_dir / "active"

    @property
    def cache_dir(self) -> Path:
        if self._cache_dir is not None:
            return self._cache_dir
        if self.engine is not None and hasattr(self.engine, "cache_dir"):
            return Path(self.engine.cache_dir)
        return self.vault_dir / "cache"

    @cache_dir.setter
    def cache_dir(self, val: Path | str) -> None:
        self._cache_dir = Path(val).expanduser()

    @property
    def config_file(self) -> Path:
        if self._config_file is not None:
            return self._config_file
        if self.engine is not None and hasattr(self.engine, "config_file"):
            return Path(self.engine.config_file)
        return self.vault_dir / "config.json"

    @config_file.setter
    def config_file(self, val: Path | str) -> None:
        self._config_file = Path(val).expanduser()

    @property
    def desktop_token_file(self) -> Path:
        if self._desktop_token_file is not None:
            return self._desktop_token_file
        return self.gemini_dir / "jetski-standalone-oauth-token"

    @desktop_token_file.setter
    def desktop_token_file(self, val: Path | str) -> None:
        self._desktop_token_file = Path(val).expanduser()

    @property
    def cli_token_file(self) -> Path:
        if self._cli_token_file is not None:
            return self._cli_token_file
        return self.gemini_dir / "antigravity-cli" / "antigravity-oauth-token"

    @cli_token_file.setter
    def cli_token_file(self, val: Path | str) -> None:
        self._cli_token_file = Path(val).expanduser()

    @property
    def supervisor(self) -> ProcessSupervisor:
        if self._supervisor is not None:
            return self._supervisor
        if self.engine is not None and hasattr(self.engine, "supervisor") and self.engine.supervisor is not None:
            return self.engine.supervisor
        return ProcessSupervisor(gemini_dir=self.gemini_dir, proc_dir=self.proc_dir)

    def get_active_name(self) -> str | None:
        """Returns the name of the currently active account, or None."""
        if self.get_active_account_callback is not None:
            return self.get_active_account_callback()
        if self.engine is not None and hasattr(self.engine, "get_active_name"):
            return self.engine.get_active_name()
        if self.active_file.is_file():
            try:
                content = self.active_file.read_text(encoding="utf-8").strip()
                return content or None
            except OSError:
                return None
        return None

    def init_vault(self) -> None:
        """Ensures vault directory and standard subdirectories exist with 0700 permissions."""
        if self.init_vault_callback is not None:
            self.init_vault_callback()
            return
        if self.engine is not None and hasattr(self.engine, "init_vault"):
            self.engine.init_vault()
            return

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

    def _sync_tokens_to_targets(self, payload_str: str) -> None:
        """Atomically emits token payload to Desktop and CLI target files."""
        if self.sync_tokens_callback is not None:
            self.sync_tokens_callback(payload_str)
            return
        if self.engine is not None and hasattr(self.engine, "_sync_tokens_to_targets"):
            self.engine._sync_tokens_to_targets(payload_str)
            return

        _atomic_write_file(self.desktop_token_file, payload_str, mode=0o600)
        _atomic_write_file(self.cli_token_file, payload_str, mode=0o600)

    def install_hook(self, hooks_file: Path | None = None) -> Path:
        """Installs the PreInvocation lifecycle hook into hooks.json via deep merge."""
        if self.hook_installer is not None:
            return self.hook_installer(hooks_file)
        if self.engine is not None and hasattr(self.engine, "install_hook"):
            return self.engine.install_hook(hooks_file)

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

        # Resolve launcher path
        launcher_path = Path(__file__).resolve().parent.parent / "agy_switch.py"
        hook_cmd = f"python3 {shlex.quote(str(launcher_path))} hook-pre-invocation"

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
        _atomic_write_json(target_hooks, existing)
        return target_hooks

    def extract_email_from_jwt(self, id_token: str) -> str | None:
        """Decodes JWT payload to extract user email."""
        return extract_email_from_jwt(id_token)

    def validate_token_dict(self, data: Any) -> dict[str, Any]:
        """Validates token schema dictionary."""
        return validate_token_dict(data)

    def validate_token_file(self, token_path: Path) -> dict[str, Any]:
        """Reads and validates token file from disk."""
        return validate_token_file(token_path)

    def _is_valid_expiry(self, raw_expiry: Any) -> bool:
        """Validates that expiry is a recognizable positive timestamp or ISO string."""
        if raw_expiry is None or isinstance(raw_expiry, bool):
            return False
        if isinstance(raw_expiry, (int, float)):
            return math.isfinite(raw_expiry) and raw_expiry > 0
        if isinstance(raw_expiry, str):
            cleaned = raw_expiry.strip()
            try:
                num = float(cleaned)
                return math.isfinite(num) and num > 0
            except ValueError:
                pass
            try:
                iso_str = cleaned.replace("Z", "+00:00")
                datetime.fromisoformat(iso_str)
                return True
            except Exception:
                return False
        return False

    def _is_token_expired(self, raw_expiry: Any) -> bool:
        """Determines if a token expiry representation is already in the past."""
        if raw_expiry is None or isinstance(raw_expiry, bool):
            return False
        if isinstance(raw_expiry, (int, float)):
            return raw_expiry <= time.time()
        if isinstance(raw_expiry, str):
            cleaned = raw_expiry.strip()
            try:
                num = float(cleaned)
                if math.isfinite(num):
                    return num <= time.time()
            except ValueError:
                pass
            try:
                iso_str = cleaned.replace("Z", "+00:00")
                dt = datetime.fromisoformat(iso_str)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt <= datetime.now(timezone.utc)
            except Exception:
                pass
        return False

    def _check_permissions(self) -> DiagnosticItem:
        """Layer 1: Verifies POSIX permissions (0700 for directories, 0600 for sensitive files)."""
        if not self.vault_dir.is_dir():
            return DiagnosticItem(
                category="permissions",
                name="Vault File & Directory Permissions",
                status="FAIL",
                message=f"Vault directory does not exist: {self.vault_dir}.",
                can_fix=True,
            )

        insecure_paths: list[str] = []

        dirs_to_check: list[Path] = [self.vault_dir]
        if self.accounts_dir.is_dir():
            dirs_to_check.append(self.accounts_dir)
            for acc in self.accounts_dir.iterdir():
                if acc.is_dir() and not acc.name.startswith("."):
                    dirs_to_check.append(acc)
        if self.cache_dir.is_dir():
            dirs_to_check.append(self.cache_dir)

        for d in dirs_to_check:
            try:
                mode = stat.S_IMODE(d.stat().st_mode)
                if mode != 0o700:
                    insecure_paths.append(f"{d.name}/ ({oct(mode)})")
            except OSError:
                pass

        files_to_check: list[Path] = []
        if self.accounts_dir.is_dir():
            for acc in self.accounts_dir.iterdir():
                if acc.is_dir() and not acc.name.startswith("."):
                    for f in acc.iterdir():
                        if f.is_file():
                            files_to_check.append(f)
        if self.active_file.is_file():
            files_to_check.append(self.active_file)
        if self.cache_dir.is_dir():
            for cf in self.cache_dir.iterdir():
                if cf.is_file():
                    files_to_check.append(cf)
        if self.config_file.is_file():
            files_to_check.append(self.config_file)
        watch_pid = self.vault_dir / "watch.pid"
        if (watch_pid.is_file() or watch_pid.is_symlink()) and watch_pid.exists():
            files_to_check.append(watch_pid)
        watch_log = self.vault_dir / "watch.log"
        if (watch_log.is_file() or watch_log.is_symlink()) and watch_log.exists():
            files_to_check.append(watch_log)

        for f in files_to_check:
            try:
                mode = stat.S_IMODE(f.stat().st_mode)
                if mode != 0o600:
                    insecure_paths.append(f"{f.name} ({oct(mode)})")
            except OSError:
                pass

        if insecure_paths:
            return DiagnosticItem(
                category="permissions",
                name="Vault File & Directory Permissions",
                status="FAIL",
                message=f"Insecure permissions detected on: {', '.join(insecure_paths)}.",
                can_fix=True,
            )

        return DiagnosticItem(
            category="permissions",
            name="Vault File & Directory Permissions",
            status="PASS",
            message="All directory (0700) and file (0600) permissions are secure.",
            can_fix=False,
        )

    def _check_token_integrity(self) -> DiagnosticItem:
        """Layer 2: Validates OAuth token JSON schema, extracts JWT identity, and detects expired tokens."""
        if not self.accounts_dir.is_dir():
            return DiagnosticItem(
                category="token_integrity",
                name="OAuth Token & JWT Identity Integrity",
                status="WARN",
                message="No accounts registered in vault.",
                can_fix=False,
            )

        accounts = sorted([p for p in self.accounts_dir.iterdir() if p.is_dir() and not p.name.startswith(".")])
        if not accounts:
            return DiagnosticItem(
                category="token_integrity",
                name="OAuth Token & JWT Identity Integrity",
                status="WARN",
                message="No accounts registered in vault.",
                can_fix=False,
            )

        active_account = self.get_active_name()
        corrupt_accounts: list[tuple[str, str]] = []
        expired_active: list[str] = []
        expired_secondary: list[str] = []

        for acc in accounts:
            acc_name = acc.name
            token_path = acc / "token.json"
            if not token_path.is_file():
                corrupt_accounts.append((acc_name, "token.json does not exist"))
                continue

            try:
                raw_text = token_path.read_text(encoding="utf-8")
                data = json.loads(raw_text)
                self.validate_token_dict(data)
            except Exception as e:
                corrupt_accounts.append((acc_name, str(e)))
                continue

            id_tok = data.get("id_token", "")
            email = self.extract_email_from_jwt(id_tok)
            if not email:
                corrupt_accounts.append((acc_name, "Invalid id_token JWT (unable to extract email)"))
                continue

            token_obj = data.get("token", {})
            raw_expiry = token_obj.get("expiry")
            if not self._is_valid_expiry(raw_expiry):
                corrupt_accounts.append((acc_name, "Invalid token expiry format (unparseable timestamp/date)"))
                continue

            if self._is_token_expired(raw_expiry):
                if active_account and acc_name == active_account.strip():
                    expired_active.append(acc_name)
                else:
                    expired_secondary.append(acc_name)

        if corrupt_accounts:
            err_details = [f"'{a}': {msg}" for a, msg in corrupt_accounts]
            return DiagnosticItem(
                category="token_integrity",
                name="OAuth Token & JWT Identity Integrity",
                status="FAIL",
                message=f"Corrupt or invalid token schema detected for: {', '.join(err_details)}.",
                can_fix=False,
            )

        if expired_active:
            return DiagnosticItem(
                category="token_integrity",
                name="OAuth Token & JWT Identity Integrity",
                status="FAIL",
                message=f"Active account '{expired_active[0]}' token is expired.",
                can_fix=False,
            )

        if expired_secondary:
            return DiagnosticItem(
                category="token_integrity",
                name="OAuth Token & JWT Identity Integrity",
                status="WARN",
                message=f"Secondary account token(s) expired: {', '.join(expired_secondary)}.",
                can_fix=False,
            )

        return DiagnosticItem(
            category="token_integrity",
            name="OAuth Token & JWT Identity Integrity",
            status="PASS",
            message=f"All {len(accounts)} account tokens valid and unexpired.",
            can_fix=False,
        )

    def _check_dual_sync(self) -> DiagnosticItem:
        """Layer 3: Compares semantic JSON credentials of Desktop and CLI tokens against active vault account."""
        active = self.get_active_name()
        if not active or not (self.accounts_dir / active).is_dir():
            return DiagnosticItem(
                category="dual_sync",
                name="Dual-Destination Credential Sync",
                status="FAIL",
                message="No valid active account configured in vault.",
                can_fix=False,
            )

        vault_token_path = self.accounts_dir / active / "token.json"
        if not vault_token_path.is_file():
            return DiagnosticItem(
                category="dual_sync",
                name="Dual-Destination Credential Sync",
                status="FAIL",
                message=f"Active account '{active}' token file is missing.",
                can_fix=False,
            )

        try:
            vault_data = json.loads(vault_token_path.read_text(encoding="utf-8"))
        except Exception:
            return DiagnosticItem(
                category="dual_sync",
                name="Dual-Destination Credential Sync",
                status="FAIL",
                message=f"Active account '{active}' token JSON is corrupt.",
                can_fix=False,
            )

        if not isinstance(vault_data, dict):
            return DiagnosticItem(
                category="dual_sync",
                name="Dual-Destination Credential Sync",
                status="FAIL",
                message=f"Active account '{active}' token JSON is corrupt (root is not a JSON object).",
                can_fix=False,
            )

        vault_tok = vault_data.get("token", {}) if isinstance(vault_data.get("token"), dict) else {}
        v_access = vault_tok.get("access_token")
        v_refresh = vault_tok.get("refresh_token")
        v_auth = vault_data.get("auth_method")

        if not v_access or not v_refresh or not v_auth:
            return DiagnosticItem(
                category="dual_sync",
                name="Dual-Destination Credential Sync",
                status="FAIL",
                message=f"Active account '{active}' token credentials are incomplete or missing.",
                can_fix=False,
            )

        desktop_file = self.desktop_token_file
        cli_file = self.cli_token_file

        desktop_exists = desktop_file.is_file()
        cli_exists = cli_file.is_file()

        if not desktop_exists or not cli_exists:
            missing: list[str] = []
            if not desktop_exists:
                missing.append("Desktop")
            if not cli_exists:
                missing.append("CLI")
            return DiagnosticItem(
                category="dual_sync",
                name="Dual-Destination Credential Sync",
                status="WARN",
                message=f"{' and '.join(missing)} token file missing from disk.",
                can_fix=True,
            )

        try:
            desktop_data = json.loads(desktop_file.read_text(encoding="utf-8"))
            cli_data = json.loads(cli_file.read_text(encoding="utf-8"))
        except Exception:
            return DiagnosticItem(
                category="dual_sync",
                name="Dual-Destination Credential Sync",
                status="FAIL",
                message="Desktop or CLI token file contains corrupt or malformed JSON.",
                can_fix=True,
            )

        if not isinstance(desktop_data, dict) or not isinstance(cli_data, dict):
            return DiagnosticItem(
                category="dual_sync",
                name="Dual-Destination Credential Sync",
                status="FAIL",
                message="Desktop or CLI token file contains corrupt or malformed JSON (not a JSON object).",
                can_fix=True,
            )

        d_tok = desktop_data.get("token", {}) if isinstance(desktop_data.get("token"), dict) else {}
        c_tok = cli_data.get("token", {}) if isinstance(cli_data.get("token"), dict) else {}

        d_match = (
            d_tok.get("access_token") == v_access
            and d_tok.get("refresh_token") == v_refresh
            and desktop_data.get("auth_method") == v_auth
        )
        c_match = (
            c_tok.get("access_token") == v_access
            and c_tok.get("refresh_token") == v_refresh
            and cli_data.get("auth_method") == v_auth
        )

        if d_match and c_match:
            return DiagnosticItem(
                category="dual_sync",
                name="Dual-Destination Credential Sync",
                status="PASS",
                message=f"Desktop and CLI tokens match active account '{active}'.",
                can_fix=False,
            )

        mismatched: list[str] = []
        if not d_match:
            mismatched.append("Desktop")
        if not c_match:
            mismatched.append("CLI")

        return DiagnosticItem(
            category="dual_sync",
            name="Dual-Destination Credential Sync",
            status="FAIL",
            message=f"{' and '.join(mismatched)} token credentials desynchronized from active account '{active}'.",
            can_fix=True,
        )

    def _check_lifecycle_hooks(self) -> DiagnosticItem:
        """Layer 4: Checks ~/.gemini/config/hooks.json for PreInvocation lifecycle hook."""
        hooks_file = self.gemini_dir / "config" / "hooks.json"
        if not hooks_file.is_file():
            return DiagnosticItem(
                category="hooks",
                name="Antigravity PreInvocation Hook",
                status="WARN",
                message="hooks.json configuration file not found.",
                can_fix=True,
            )

        content = hooks_file.read_text(encoding="utf-8").strip()
        if not content:
            return DiagnosticItem(
                category="hooks",
                name="Antigravity PreInvocation Hook",
                status="FAIL",
                message="Malformed hooks.json detected (0-byte file).",
                can_fix=True,
            )

        try:
            data = json.loads(content)
        except Exception:
            return DiagnosticItem(
                category="hooks",
                name="Antigravity PreInvocation Hook",
                status="FAIL",
                message="Malformed hooks.json detected (invalid JSON syntax).",
                can_fix=True,
            )

        if not isinstance(data, dict):
            return DiagnosticItem(
                category="hooks",
                name="Antigravity PreInvocation Hook",
                status="FAIL",
                message="Malformed hooks.json detected (root is not a JSON object).",
                can_fix=True,
            )

        entry = data.get("agy-switch-auto-rotate")
        if not isinstance(entry, dict) or not entry.get("enabled", False):
            return DiagnosticItem(
                category="hooks",
                name="Antigravity PreInvocation Hook",
                status="WARN",
                message="PreInvocation hook not configured or disabled in hooks.json.",
                can_fix=True,
            )

        pre_inv = entry.get("PreInvocation")
        if not isinstance(pre_inv, list) or not pre_inv:
            return DiagnosticItem(
                category="hooks",
                name="Antigravity PreInvocation Hook",
                status="WARN",
                message="PreInvocation hook list is missing or empty in hooks.json.",
                can_fix=True,
            )

        cmd_item = pre_inv[0]
        if not isinstance(cmd_item, dict):
            return DiagnosticItem(
                category="hooks",
                name="Antigravity PreInvocation Hook",
                status="WARN",
                message="PreInvocation hook definition is invalid (not a JSON object).",
                can_fix=True,
            )

        cmd_str = cmd_item.get("command")
        if not cmd_str or not isinstance(cmd_str, str) or not cmd_str.strip():
            return DiagnosticItem(
                category="hooks",
                name="Antigravity PreInvocation Hook",
                status="WARN",
                message="PreInvocation hook command is missing or empty.",
                can_fix=True,
            )

        try:
            parts = shlex.split(cmd_str)
        except ValueError:
            return DiagnosticItem(
                category="hooks",
                name="Antigravity PreInvocation Hook",
                status="WARN",
                message="PreInvocation hook command syntax is invalid (unbalanced quotes).",
                can_fix=True,
            )

        if not parts:
            return DiagnosticItem(
                category="hooks",
                name="Antigravity PreInvocation Hook",
                status="WARN",
                message="PreInvocation hook command is missing or empty.",
                can_fix=True,
            )

        target_path: Path | None = None
        for part in parts:
            if part.endswith(".py"):
                target_path = Path(part).expanduser()
                break

        if target_path is None:
            first = parts[0]
            if first in ("python", "python3", "bash", "sh") and len(parts) > 1 and not parts[1].startswith("-"):
                target_path = Path(parts[1]).expanduser()
            else:
                target_path = Path(first).expanduser()

        if not target_path.is_file():
            if "/" not in str(target_path):
                resolved = shutil.which(str(target_path))
                if resolved:
                    target_path = Path(resolved)

        if not target_path.is_file():
            return DiagnosticItem(
                category="hooks",
                name="Antigravity PreInvocation Hook",
                status="WARN",
                message=f"PreInvocation hook target script does not exist: {target_path}.",
                can_fix=True,
            )

        if not os.access(target_path, os.R_OK):
            return DiagnosticItem(
                category="hooks",
                name="Antigravity PreInvocation Hook",
                status="WARN",
                message=f"PreInvocation hook target script is not readable: {target_path}.",
                can_fix=True,
            )

        is_direct_script = bool(parts and parts[0] == str(target_path))
        needs_exec = (not str(target_path).endswith(".py")) or is_direct_script
        if needs_exec and not os.access(target_path, os.X_OK):
            return DiagnosticItem(
                category="hooks",
                name="Antigravity PreInvocation Hook",
                status="WARN",
                message=f"PreInvocation hook executable target is not executable: {target_path}.",
                can_fix=True,
            )

        return DiagnosticItem(
            category="hooks",
            name="Antigravity PreInvocation Hook",
            status="PASS",
            message="PreInvocation hook registered and executable.",
            can_fix=False,
        )

    def _check_process_health(self) -> DiagnosticItem:
        """Layer 5: Inspects language_server and watch daemon via /proc/<pid>/cmdline."""
        # Locate language_server
        if self.engine is not None and hasattr(self.engine, "locate_language_server_pid"):
            ls_pid = self.engine.locate_language_server_pid()
        else:
            ls_pid = self.supervisor.locate_language_server_pid()

        ls_running = ls_pid is not None

        if ls_running:
            if self.engine is not None and hasattr(self.engine, "is_server_busy"):
                ls_busy = self.engine.is_server_busy(ls_pid)
            else:
                ls_busy = self.supervisor.is_server_busy(ls_pid)
        else:
            ls_busy = False

        if ls_running:
            ls_msg = f"language_server running (PID {ls_pid}, {'busy' if ls_busy else 'idle'})"
        else:
            ls_msg = "language_server not running"

        watch_pid_file = self.vault_dir / "watch.pid"
        watch_stale = False
        watch_active = False
        watch_msg = "watcher daemon not running"

        if watch_pid_file.is_file() or watch_pid_file.is_symlink():
            if watch_pid_file.is_symlink() and not watch_pid_file.exists():
                watch_stale = True
                watch_msg = "stale watch daemon PID file detected (broken symlink)"
            else:
                try:
                    pid_content = watch_pid_file.read_text(encoding="utf-8").strip()
                    watch_pid = int(pid_content)
                except Exception:
                    watch_pid = -1

                if watch_pid <= 1 or watch_pid == os.getpid():
                    watch_stale = True
                    watch_msg = "stale watch daemon PID file detected"
                else:
                    cmdline_file = self.proc_dir / str(watch_pid) / "cmdline"
                    if not cmdline_file.is_file():
                        watch_stale = True
                        watch_msg = f"stale watch daemon PID {watch_pid} detected (process not running)"
                    else:
                        try:
                            raw = cmdline_file.read_bytes().replace(b"\x00", b" ").lower()
                            raw_tokens = raw.split()
                            is_watch = False
                            # Synchronized daemon cmdline matching: recognises watch.py, watcher.py, agy_switch.py, agy-switch
                            has_script = any(
                                tok.endswith(b"watch.py")
                                or tok.endswith(b"watcher.py")
                                or tok.endswith(b"agy_switch.py")
                                or tok.endswith(b"agy_switch")
                                or tok.endswith(b"agy-switch")
                                or tok in (
                                    b"watch.py",
                                    b"watcher.py",
                                    b"scripts.watch",
                                    b"agy_switcher.watcher",
                                    b"agy_switch",
                                    b"agy-switch",
                                )
                                for tok in raw_tokens
                            )
                            has_action = any(
                                tok in (b"run", b"daemon", b"watch")
                                for tok in raw_tokens[1:]
                            )
                            if has_script and has_action:
                                is_watch = True

                            if is_watch:
                                watch_active = True
                                watch_msg = f"watcher active (PID {watch_pid})"
                            else:
                                watch_stale = True
                                watch_msg = f"stale watch daemon PID {watch_pid} detected (recycled PID)"
                        except OSError:
                            watch_stale = True
                            watch_msg = f"stale watch daemon PID {watch_pid} detected"

        combined_msg = f"{ls_msg}; {watch_msg}."

        if watch_stale:
            return DiagnosticItem(
                category="process",
                name="Language Server & Watcher Daemon",
                status="WARN",
                message=combined_msg,
                can_fix=True,
            )
        elif not ls_running:
            return DiagnosticItem(
                category="process",
                name="Language Server & Watcher Daemon",
                status="WARN",
                message=combined_msg,
                can_fix=False,
            )
        else:
            return DiagnosticItem(
                category="process",
                name="Language Server & Watcher Daemon",
                status="PASS",
                message=combined_msg,
                can_fix=False,
            )

    def run_diagnostics(self) -> DiagnosticReport:
        """Performs 5-layer health check across vault, tokens, dual sync, hooks, and processes."""
        items: list[DiagnosticItem] = [
            self._check_permissions(),
            self._check_token_integrity(),
            self._check_dual_sync(),
            self._check_lifecycle_hooks(),
            self._check_process_health(),
        ]

        if any(item.status == "FAIL" for item in items):
            overall_status = "FAIL"
            exit_code = 2
        elif any(item.status == "WARN" for item in items):
            overall_status = "WARN"
            exit_code = 1
        else:
            overall_status = "PASS"
            exit_code = 0

        return DiagnosticReport(
            items=items,
            overall_status=overall_status,
            exit_code=exit_code,
        )

    # Canonical alias
    run_doctor = run_diagnostics

    def fix_diagnostics(self, report: DiagnosticReport) -> list[str]:
        """Applies non-destructive automatic repairs to all fixable anomalies in the report."""
        fixes: list[str] = []

        for item in report.items:
            if not item.can_fix or item.fixed:
                continue

            if item.category == "permissions":
                try:
                    self.init_vault()
                    chmod_errors: list[str] = []

                    def _safe_chmod(target: Path, mode: int) -> None:
                        try:
                            os.chmod(target, mode)
                        except OSError as e:
                            chmod_errors.append(f"{target.name}: {e}")

                    if self.vault_dir.is_dir():
                        _safe_chmod(self.vault_dir, 0o700)
                    if self.accounts_dir.is_dir():
                        _safe_chmod(self.accounts_dir, 0o700)
                        for acc_dir in self.accounts_dir.iterdir():
                            if acc_dir.is_dir():
                                _safe_chmod(acc_dir, 0o700)
                                for f in acc_dir.iterdir():
                                    if f.is_file():
                                        _safe_chmod(f, 0o600)
                    if self.active_file.is_file():
                        _safe_chmod(self.active_file, 0o600)
                    if self.cache_dir.is_dir():
                        _safe_chmod(self.cache_dir, 0o700)
                        for cf in self.cache_dir.iterdir():
                            if cf.is_file():
                                _safe_chmod(cf, 0o600)
                    if self.config_file.is_file():
                        _safe_chmod(self.config_file, 0o600)
                    watch_pid = self.vault_dir / "watch.pid"
                    if (watch_pid.is_file() or watch_pid.is_symlink()) and watch_pid.exists():
                        _safe_chmod(watch_pid, 0o600)
                    watch_log = self.vault_dir / "watch.log"
                    if (watch_log.is_file() or watch_log.is_symlink()) and watch_log.exists():
                        _safe_chmod(watch_log, 0o600)

                    if chmod_errors:
                        item.fixed = False
                        item.status = "FAIL"
                        item.fix_action = f"Failed to restore permissions: {'; '.join(chmod_errors)}"
                    else:
                        action_str = "Restored POSIX permissions (0700 for directories, 0600 for files)"
                        item.fixed = True
                        item.status = "PASS"
                        item.fix_action = action_str
                        fixes.append(action_str)
                except OSError as e:
                    item.fixed = False
                    item.status = "FAIL"
                    item.fix_action = f"Failed to restore permissions: {e}"

            elif item.category == "dual_sync":
                active = self.get_active_name()
                if active:
                    tok_file = self.accounts_dir / active / "token.json"
                    if tok_file.is_file():
                        try:
                            token_data = self.validate_token_file(tok_file)
                            payload_str = json.dumps(token_data, indent=2) + "\n"
                            self._sync_tokens_to_targets(payload_str)
                            action_str = f"Re-synced Desktop and CLI tokens from active vault account '{active}'"
                            item.fixed = True
                            item.status = "PASS"
                            item.fix_action = action_str
                            fixes.append(action_str)
                        except Exception:
                            pass

            elif item.category == "hooks":
                hooks_file = self.gemini_dir / "config" / "hooks.json"
                is_corrupt = False
                if hooks_file.is_file() or hooks_file.is_symlink():
                    try:
                        content = hooks_file.read_text(encoding="utf-8").strip()
                        if not content:
                            is_corrupt = True
                        else:
                            loaded = json.loads(content)
                            if not isinstance(loaded, dict):
                                is_corrupt = True
                    except Exception:
                        is_corrupt = True

                if item.status == "FAIL" or is_corrupt:
                    ts = int(time.time() * 1000)
                    bak_file = hooks_file.parent / f"hooks.json.corrupt.{ts}.bak"
                    try:
                        if hooks_file.exists():
                            shutil.copy2(hooks_file, bak_file)
                            hooks_file.unlink()
                    except OSError:
                        pass
                    self.install_hook(hooks_file)
                    action_str = f"Backed up corrupt hooks.json to {bak_file.name} and registered fresh PreInvocation hook"
                    item.fixed = True
                    item.status = "PASS"
                    item.fix_action = action_str
                    fixes.append(action_str)
                else:
                    self.install_hook(hooks_file)
                    action_str = "Registered PreInvocation hook in hooks.json"
                    item.fixed = True
                    item.status = "PASS"
                    item.fix_action = action_str
                    fixes.append(action_str)

            elif item.category == "process":
                watch_pid = self.vault_dir / "watch.pid"
                if watch_pid.is_file() or watch_pid.is_symlink():
                    try:
                        watch_pid.unlink()
                        action_str = "Unlinked stale watch.pid file"
                        item.fixed = True
                        item.status = "PASS"
                        item.fix_action = action_str
                        fixes.append(action_str)
                    except OSError:
                        pass

        # Recalculate report overall status and exit code
        has_failed = any(it.status == "FAIL" for it in report.items)
        has_warned = any(it.status == "WARN" for it in report.items)
        if has_failed:
            report.overall_status = "FAIL"
            report.exit_code = 2
        elif has_warned:
            report.overall_status = "WARN"
            report.exit_code = 1
        else:
            report.overall_status = "PASS"
            report.exit_code = 0

        return fixes

    # Canonical alias
    auto_heal = fix_diagnostics


def run_doctor(
    vault_dir: Path | str | None = None,
    gemini_dir: Path | str | None = None,
    proc_dir: Path | str | None = None,
    supervisor: ProcessSupervisor | None = None,
    **kwargs: Any,
) -> DiagnosticReport:
    """Convenience functional entry point for running diagnostics."""
    doctor = DoctorEngine(
        vault_dir=vault_dir,
        gemini_dir=gemini_dir,
        proc_dir=proc_dir,
        supervisor=supervisor,
        **kwargs,
    )
    return doctor.run_diagnostics()


def auto_heal(
    report: DiagnosticReport,
    vault_dir: Path | str | None = None,
    gemini_dir: Path | str | None = None,
    proc_dir: Path | str | None = None,
    supervisor: ProcessSupervisor | None = None,
    **kwargs: Any,
) -> list[str]:
    """Convenience functional entry point for repairing fixable diagnostic anomalies."""
    doctor = DoctorEngine(
        vault_dir=vault_dir,
        gemini_dir=gemini_dir,
        proc_dir=proc_dir,
        supervisor=supervisor,
        **kwargs,
    )
    return doctor.fix_diagnostics(report)


__all__ = [
    "DoctorEngine",
    "run_doctor",
    "auto_heal",
    "extract_email_from_jwt",
    "validate_token_dict",
    "validate_token_file",
]
