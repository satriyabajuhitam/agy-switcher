#!/usr/bin/env python3
"""agy-switch: High-reliability account switcher for Antigravity 2.0 & CLI.

Pure Python 3 standard library. Zero external dependencies.
"""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# Import safety: prepend script directory to sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


DEFAULT_VAULT_DIR = Path.home() / ".antigravity-vault"
DEFAULT_GEMINI_DIR = Path.home() / ".gemini"
DEFAULT_LEGACY_ROOT = Path.home() / ".agy-cli-manager"

QUOTA_REGEX = re.compile(
    r"^(Gemini Models|Claude and GPT models)\s+"
    r"(Weekly Limit Remaining|Five Hour Limit Remaining)\s+"
    r"(\d+)%\s*"
    r"([0-9T:\-Z.]+)?",
    re.MULTILINE,
)


class AgySwitchError(Exception):
    """Base exception for agy-switch operations."""


class InvalidTokenError(AgySwitchError):
    """Raised when an OAuth token fails structural or schema validation."""


class AccountNotFoundError(AgySwitchError):
    """Raised when a requested account does not exist in the vault."""


class ActiveTurnRunningError(AgySwitchError):
    """Raised when language_server is busy and switch is attempted without --force."""


class AgySwitchEngine:
    """Core engine managing account vaulting, auto-migration, and listing."""

    def __init__(
        self,
        vault_dir: Path | None = None,
        gemini_dir: Path | None = None,
        legacy_root: Path | None = None,
    ) -> None:
        self.vault_dir = Path(vault_dir or DEFAULT_VAULT_DIR).expanduser()
        self.gemini_dir = Path(gemini_dir or DEFAULT_GEMINI_DIR).expanduser()
        self.legacy_root = Path(legacy_root or DEFAULT_LEGACY_ROOT).expanduser()
        self.accounts_dir = self.vault_dir / "accounts"
        self.active_file = self.vault_dir / "active"
        self.cache_dir = self.vault_dir / "cache"
        self.cache_file = self.cache_dir / "last_known_quota.json"
        self.config_file = self.vault_dir / "config.json"

        self.init_vault()

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
        if not isinstance(data, dict):
            raise InvalidTokenError("Token payload must be a JSON object.")

        token_obj = data.get("token")
        if not isinstance(token_obj, dict):
            raise InvalidTokenError("Missing required 'token' object in payload.")

        required_token_keys = ("access_token", "refresh_token", "token_type", "expiry")
        missing_keys = [k for k in required_token_keys if not token_obj.get(k)]
        if missing_keys:
            raise InvalidTokenError(f"Missing required token fields: {', '.join(missing_keys)}")

        if not data.get("auth_method"):
            raise InvalidTokenError("Missing required 'auth_method' field.")

        id_token = data.get("id_token")
        if not id_token or not isinstance(id_token, str):
            raise InvalidTokenError("Missing or invalid 'id_token' JWT field.")

        return data

    def validate_token_file(self, token_path: Path) -> dict[str, Any]:
        """Reads and validates a token file from disk."""
        if not token_path.is_file():
            raise InvalidTokenError(f"Token file does not exist: {token_path}")
        try:
            raw = token_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as e:
            raise InvalidTokenError(f"Failed to read/parse token JSON: {e}") from e

        return self.validate_token_dict(data)

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
        try:
            parts = id_token.split(".")
            if len(parts) >= 2:
                payload = parts[1]
                payload += "=" * (-len(payload) % 4)
                decoded = json.loads(base64.urlsafe_b64decode(payload.encode("utf-8")))
                return decoded.get("email")
        except Exception:
            return None
        return None

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
        jetski_token = self.gemini_dir / "jetski-standalone-oauth-token"
        cli_token = self.gemini_dir / "antigravity-cli" / "antigravity-oauth-token"

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
        target_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp_fd, tmp_file = tempfile.mkstemp(dir=target_path.parent, prefix=f"{target_path.name}.tmp-")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.chmod(tmp_file, mode)
            os.replace(tmp_file, target_path)
            tmp_file = None
        finally:
            if tmp_file and os.path.exists(tmp_file):
                try:
                    os.unlink(tmp_file)
                except OSError:
                    pass

    def _atomic_write_json(self, target_path: Path, payload: dict[str, Any]) -> None:
        """Writes JSON to a target path atomically with 0600 permissions."""
        self._atomic_write_file(target_path, json.dumps(payload, indent=2) + "\n", mode=0o600)

    def _sync_tokens_to_targets(self, token_payload: str) -> None:
        """Atomically emits token payload to both Desktop and CLI target files."""
        jetski_token_path = self.gemini_dir / "jetski-standalone-oauth-token"
        self._atomic_write_file(jetski_token_path, token_payload, mode=0o600)

        cli_token_path = self.gemini_dir / "antigravity-cli" / "antigravity-oauth-token"
        self._atomic_write_file(cli_token_path, token_payload, mode=0o600)

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

        script_abs_path = str(Path(__file__).resolve())
        hook_cmd = f"python3 {script_abs_path} hook-pre-invocation"

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

    def list_accounts(self) -> dict[str, Any]:
        """Lists all accounts registered in the vault and marks the active account."""
        active = self.get_active_name()
        accounts: list[dict[str, Any]] = []

        if self.accounts_dir.is_dir():
            for acc_path in sorted(self.accounts_dir.iterdir()):
                if not acc_path.is_dir():
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
            raise AccountNotFoundError(
                f"Account '{name}' not found in vault ({self.accounts_dir}). "
                f"Available accounts: {', '.join(sorted(p.name for p in self.accounts_dir.iterdir() if p.is_dir())) or 'None'}"
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
        """Finds the running language_server PID by scanning /proc or pgrep."""
        # 1. Scan /proc for exact binary command line
        proc_path = Path("/proc")
        if proc_path.is_dir():
            try:
                for pid_dir in proc_path.iterdir():
                    if not pid_dir.name.isdigit():
                        continue
                    cmdline_file = pid_dir / "cmdline"
                    try:
                        cmdline = cmdline_file.read_bytes().replace(b"\x00", b" ")
                        if b"language_server" in cmdline and b"--standalone" in cmdline:
                            return int(pid_dir.name)
                    except (OSError, ValueError):
                        continue
            except OSError:
                pass

        # 2. Fallback to pgrep
        try:
            res = subprocess.run(
                ["pgrep", "-f", "language_server.*--standalone"],
                capture_output=True,
                text=True,
                check=False,
            )
            lines = res.stdout.strip().split()
            if lines and lines[0].isdigit():
                return int(lines[0])
        except Exception:
            pass

        return None

    def is_server_busy(self, pid: int | None = None) -> bool:
        """Checks if language_server has active child processes or presence locks."""
        # 1. Check presence locks in ~/.gemini
        presence_dirs = [
            self.gemini_dir / "antigravity" / "presence",
            self.gemini_dir / "antigravity-cli" / "presence",
        ]
        for p_dir in presence_dirs:
            if p_dir.is_dir():
                try:
                    lock_files = [f for f in p_dir.iterdir() if f.is_file() and f.suffix == ".lock"]
                    if lock_files:
                        return True
                except OSError:
                    pass

        # 2. Check child processes of pid if provided
        if pid is not None:
            proc_path = Path("/proc")
            if proc_path.is_dir():
                try:
                    for p_dir in proc_path.iterdir():
                        if not p_dir.name.isdigit():
                            continue
                        stat_file = p_dir / "stat"
                        try:
                            content = stat_file.read_text(encoding="utf-8")
                            parts = content.split()
                            if len(parts) >= 4 and parts[3] == str(pid):
                                return True
                        except (OSError, IndexError):
                            continue
                except OSError:
                    pass

        return False

    def _send_signal(self, pid: int, sig: int = signal.SIGTERM) -> None:
        """Dispatches signal to target process."""
        os.kill(pid, sig)

    def _poll_process_exit(self, pid: int, timeout: float = 3.0) -> bool:
        """Polls until process with given pid has terminated."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
                time.sleep(0.1)
            except OSError:
                return True
        return False

    def restart_language_server(self, force: bool = False) -> bool:
        """Restarts language_server gracefully via SIGTERM so WSL Relay auto-respawns it."""
        pid = self.locate_language_server_pid()
        if pid is None:
            return False

        if self.is_server_busy(pid) and not force:
            raise ActiveTurnRunningError(
                "Antigravity is currently busy processing an agent turn or background task. "
                "Use --force to switch and restart anyway."
            )

        self._send_signal(pid, signal.SIGTERM)
        return self._poll_process_exit(pid, timeout=3.0)

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


def _sanitize_expires_in(raw_exp: Any, default: int = 3600) -> int:
    """Sanitizes an expires_in value to a positive integer, falling back to default."""
    if isinstance(raw_exp, bool):
        return default
    try:
        val = float(raw_exp)
        return int(val) if math.isfinite(val) and 0 < val <= 31536000 else default
    except (ValueError, TypeError, OverflowError):
        return default


AUTH_ERROR_CODE_PATTERN = re.compile(r"\b(401|403)\b")
HTTP_SERVER_ERROR_PATTERN = re.compile(r"\b(500|502|503|504)\b")
AUTH_PHRASES = (
    "no token",
    "token refresh rejected",
    "invalid_grant",
    "unauthorized",
    "revoked",
    "forbidden",
    "access_denied",
    "invalid_client",
)


def _has_auth_indicator(text: Any) -> bool:
    """Checks whether an error message contains auth/token corruption keywords or HTTP 401/403 status codes."""
    if text is None:
        return False
    lower = str(text).lower()
    if any(phrase in lower for phrase in AUTH_PHRASES):
        return True
    if AUTH_ERROR_CODE_PATTERN.search(lower):
        return True
    return False


class QuotaClient:
    def __init__(
        self,
        engine_or_client_id: Any = "1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com",
        client_secret: str = "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",
        timeout: float = 2.0,
        cache_dir: Path | None = None,
        engine: Any | None = None,
    ) -> None:
        target_engine = engine if engine is not None else (engine_or_client_id if hasattr(engine_or_client_id, "cache_dir") else None)
        if target_engine is not None:
            self.engine = target_engine
            self.cache_dir = cache_dir or getattr(target_engine, "cache_dir", None) or (Path.home() / ".antigravity-vault" / "cache")
            self.client_id = "1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com"
        else:
            self.engine = None
            self.client_id = engine_or_client_id
            self.cache_dir = cache_dir or (Path.home() / ".antigravity-vault" / "cache")
        self.client_secret = client_secret
        self.timeout = timeout
        self.token_endpoint = "https://oauth2.googleapis.com/token"
        self.quota_endpoint = "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary"
        self.cache_file = getattr(target_engine, "cache_file", None) or (self.cache_dir / "last_known_quota.json")
        self._local = threading.local()

    @property
    def last_error(self) -> str | None:
        return getattr(self._local, "last_error", None)

    @last_error.setter
    def last_error(self, val: str | None) -> None:
        self._local.last_error = val

    @property
    def last_error_type(self) -> str | None:
        return getattr(self._local, "last_error_type", None)

    @last_error_type.setter
    def last_error_type(self, val: str | None) -> None:
        self._local.last_error_type = val

    def refresh_access_token(self, refresh_token: str) -> dict[str, Any] | None:
        """Refreshes the OAuth access token."""
        if not refresh_token or not isinstance(refresh_token, str) or not refresh_token.strip():
            return None
        data = urllib.parse.urlencode({
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": refresh_token.strip(),
            "grant_type": "refresh_token",
        }).encode("utf-8")

        req = urllib.request.Request(self.token_endpoint, data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
                token_data = json.loads(body)
                if not isinstance(token_data, dict) or "error" in token_data:
                    return None
                raw_token = token_data.get("access_token")
                if not isinstance(raw_token, str) or not raw_token.strip():
                    return None
                raw_exp = token_data.get("expires_in", 3600)
                expires_in = _sanitize_expires_in(raw_exp, default=3600)
                return {
                    "access_token": raw_token.strip(),
                    "expires_in": expires_in,
                }
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as e:
            print(f"[QuotaClient] Failed to refresh token: {e}", file=sys.stderr)
            return None

    def parse_quota_response(self, raw_resp: dict[str, Any]) -> dict[str, Any]:
        """Parses CloudCode response into the standardized Hybrid Quota Schema."""
        if not isinstance(raw_resp, dict):
            return {}
        groups = raw_resp.get("groups")
        if not isinstance(groups, list) or not groups:
            return {}

        result: dict[str, Any] = {}
        for group in groups:
            if not isinstance(group, dict):
                continue
            group_name = group.get("displayName") or group.get("group", "")
            if not isinstance(group_name, str):
                continue

            group_name_lower = group_name.lower()
            if "gemini" in group_name_lower:
                key = "gemini"
            elif "claude" in group_name_lower:
                key = "claude"
            else:
                continue

            buckets_raw = group.get("buckets", [])
            if not isinstance(buckets_raw, list) or not buckets_raw:
                continue

            buckets: dict[str, dict[str, Any]] = {}
            parsed_buckets_list: list[tuple[int, float, int, dict[str, Any]]] = []

            for b in buckets_raw:
                if not isinstance(b, dict):
                    continue
                raw_bucket_id = str(b.get("bucketId") or "").strip().lower()
                if any(x in raw_bucket_id for x in ("five_hour", "five_hours", "5h", "rolling_5h", "5_hour", "5-hour")):
                    norm_id = "5h"
                elif any(x in raw_bucket_id for x in ("weekly", "week", "7d")):
                    norm_id = "weekly"
                else:
                    norm_id = re.sub(r"[^a-z0-9_]", "", raw_bucket_id)

                if not norm_id:
                    continue

                frac = b.get("remainingFraction")
                if frac is None:
                    frac_val = 0.0
                else:
                    try:
                        frac_val = float(frac)
                    except (ValueError, TypeError):
                        frac_val = 0.0
                frac_val = max(0.0, min(1.0, frac_val))

                quota_pct = int(round(frac_val * 100))

                reset_time_str = b.get("resetTime")
                reset_time = 0
                if isinstance(reset_time_str, (int, float)):
                    reset_time = max(0, int(reset_time_str))
                elif isinstance(reset_time_str, str) and reset_time_str:
                    if reset_time_str.isdigit():
                        reset_time = max(0, int(reset_time_str))
                    else:
                        try:
                            rt = reset_time_str.replace("Z", "+00:00")
                            dt = datetime.fromisoformat(rt)
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=timezone.utc)
                            reset_time = max(0, int(dt.timestamp()))
                        except Exception:
                            reset_time = 0

                bucket_info = {
                    "quota": quota_pct,
                    "reset_time": reset_time,
                }
                buckets[norm_id] = bucket_info
                parsed_buckets_list.append((quota_pct, frac_val, reset_time, bucket_info))

            if not parsed_buckets_list:
                continue

            # Deterministic 2-tier comparison for top-level worst-case values:
            # Primary: lowest remainingFraction ascending (raw unrounded float)
            # Secondary: earliest reset_time ascending (break ties by earliest reset timestamp)
            def _worst_key(item: tuple[int, float, int, dict[str, Any]]) -> tuple[float, float]:
                pct, _f_val, r_time, _ = item
                r_key = float(r_time) if r_time > 0 else float("inf")
                return (_f_val, r_key)

            worst_pct, worst_frac, worst_reset, _ = min(parsed_buckets_list, key=_worst_key)

            result[key] = {
                "quota": worst_pct,
                "reset_time": worst_reset,
                "cached": False,
                "buckets": buckets,
            }

        return result

    def fetch_quota(
        self,
        access_token: str,
        refresh_token: str | None = None,
        on_refresh: Any | None = None,
    ) -> dict[str, Any]:
        """Fetches quota from the internal CloudCode API."""
        if isinstance(access_token, dict):
            access_token = str(access_token.get("access_token", "")).strip()
        elif isinstance(access_token, str):
            access_token = access_token.strip()
        else:
            access_token = str(access_token or "").strip()

        self.last_error = None
        self.last_error_type = None

        if not access_token:
            return {}

        req = urllib.request.Request(
            self.quota_endpoint,
            data=b'{"project": ""}',
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "User-Agent": "Antigravity/2.5.5",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
                raw_data = json.loads(body)
                return self.parse_quota_response(raw_data)
        except urllib.error.HTTPError as e:
            if e.code == 401 and refresh_token:
                new_token_data = self.refresh_access_token(refresh_token)
                if new_token_data:
                    new_tok_str = new_token_data.get("access_token") if isinstance(new_token_data, dict) else str(new_token_data)
                    if new_tok_str and isinstance(new_tok_str, str) and new_tok_str.strip():
                        if on_refresh:
                            try:
                                on_refresh(new_token_data)
                            except Exception:
                                pass
                        return self.fetch_quota(new_tok_str.strip(), refresh_token=None)
                self.last_error = "Token refresh rejected"
                self.last_error_type = "auth"
            else:
                self.last_error = f"HTTP Error {e.code}: {e.reason}"
                self.last_error_type = "auth" if e.code in (401, 403) else "http"
            print(f"[QuotaClient] HTTP Error {e.code}: {e.reason}", file=sys.stderr)
            return {}
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as e:
            self.last_error = f"Network or Parse Error: {e}"
            self.last_error_type = "network" if isinstance(e, (urllib.error.URLError, TimeoutError)) else "parse"
            print(f"[QuotaClient] Network or Parse Error: {e}", file=sys.stderr)
            return {}

    def load_cache(self) -> dict[str, Any]:
        """Loads cached quota data from vault cache or legacy fallback."""
        cache_path = Path(self.cache_file)
        if cache_path.is_file():
            try:
                return json.loads(cache_path.read_text(encoding="utf-8"))
            except Exception:
                return {}
        legacy_cache = Path.home() / ".gemini" / "config" / "skills" / "agy-quota-checker" / "last_known_quota.json"
        if legacy_cache.is_file():
            try:
                return json.loads(legacy_cache.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def save_cache(
        self,
        data: dict[str, Any],
        merge: bool = True,
        evict_keys: list[str] | set[str] | None = None,
    ) -> None:
        """Atomically saves quota cache to vault cache directory.

        Guarantees:
        1. Top-level 'timestamp' field is always present, defaulting to time.time()
           if absent, boolean, non-positive, or invalid in incoming data.
        2. When merge=True and data contains an 'accounts' dict, existing valid
           standby accounts and their respective 'cached_at' timestamps are preserved.
        3. For valid incoming accounts lacking 'cached_at', 'cached_at' is automatically
           set to root_ts, ensuring independent freshness tracking across all callers.
        4. Invalid or error-laden accounts in incoming data are filtered out
           without corrupting existing valid cache entries. Errored model sub-keys
           (e.g. claude: {"error": ...}) are stripped while preserving healthy models.
        5. Permissions: cache directory is 0700 and cache file is 0600.
        6. Cross-mount resilience: temporary files are created in cache_file.parent,
           preventing EXDEV errors when cache_file is customized.
        """
        cache_target_file = Path(self.cache_file)
        target_dir = cache_target_file.parent
        target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(target_dir, 0o700)
        except OSError:
            pass
        if self.cache_dir != target_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                os.chmod(self.cache_dir, 0o700)
            except OSError:
                pass

        if not isinstance(data, dict):
            data_to_write = {
                "timestamp": time.time(),
                "accounts": {},
            }
        else:
            # Determine root timestamp: default to time.time() if absent, bool, or invalid
            root_ts_val = data.get("timestamp")
            if root_ts_val is None or isinstance(root_ts_val, bool):
                root_ts = time.time()
            elif isinstance(root_ts_val, (int, float)):
                root_ts = float(root_ts_val) if math.isfinite(root_ts_val) and root_ts_val > 0 else time.time()
            else:
                try:
                    parsed_ts = float(root_ts_val)
                    root_ts = parsed_ts if math.isfinite(parsed_ts) and parsed_ts > 0 else time.time()
                except (ValueError, TypeError, OverflowError):
                    root_ts = time.time()

            def _sanitize_account_entry(acc_val: Any) -> dict[str, Any] | None:
                if not isinstance(acc_val, dict) or not acc_val or acc_val.get("error"):
                    return None
                entry = dict(acc_val)
                # Strip errored or invalid model sub-keys (e.g. claude: {"error": ...})
                for m_key in ("gemini", "claude"):
                    if m_key in entry:
                        m_val = entry[m_key]
                        if not isinstance(m_val, dict) or m_val.get("error"):
                            del entry[m_key]

                # Strip any other sub-dict containing an error key
                for k in list(entry.keys()):
                    if k not in ("cached_at", "timestamp"):
                        v = entry[k]
                        if isinstance(v, dict) and v.get("error"):
                            del entry[k]

                # Must contain at least one valid model container or valid quota payload
                has_model = isinstance(entry.get("gemini"), dict) or isinstance(entry.get("claude"), dict)
                has_quota = "quota" in entry or "groups" in entry
                if not (has_model or has_quota):
                    return None

                # Normalize cached_at timestamp
                cached_at_val = entry.get("cached_at")
                if cached_at_val is None or isinstance(cached_at_val, bool):
                    entry["cached_at"] = root_ts
                elif isinstance(cached_at_val, (int, float)):
                    if not math.isfinite(cached_at_val) or cached_at_val <= 0:
                        entry["cached_at"] = root_ts
                else:
                    try:
                        p_cat = float(cached_at_val)
                        entry["cached_at"] = p_cat if math.isfinite(p_cat) and p_cat > 0 else root_ts
                    except (ValueError, TypeError, OverflowError):
                        entry["cached_at"] = root_ts
                return entry

            def _extract_valid_accounts(accounts_map: Any) -> dict[str, Any]:
                valid: dict[str, Any] = {}
                if isinstance(accounts_map, dict):
                    for k, v in accounts_map.items():
                        if isinstance(k, str) and k.strip():
                            sanitized = _sanitize_account_entry(v)
                            if sanitized is not None:
                                valid[k.strip()] = sanitized
                return valid

            incoming_valid = _extract_valid_accounts(data.get("accounts"))

            if merge:
                current = self.load_cache()
                merged_accounts: dict[str, Any] = (
                    _extract_valid_accounts(current.get("accounts"))
                    if isinstance(current, dict)
                    else {}
                )
                for acc_name, incoming_acc in incoming_valid.items():
                    if acc_name in merged_accounts:
                        merged_entry = dict(merged_accounts[acc_name])
                        merged_entry.update(incoming_acc)
                        merged_accounts[acc_name] = merged_entry
                    else:
                        merged_accounts[acc_name] = incoming_acc
                if evict_keys:
                    for ev in evict_keys:
                        if isinstance(ev, str) and ev.strip() in merged_accounts:
                            del merged_accounts[ev.strip()]
                merged_data = dict(current) if isinstance(current, dict) else {}
                for k, v in data.items():
                    if k != "accounts":
                        merged_data[k] = v
                merged_data["accounts"] = merged_accounts
                merged_data["timestamp"] = root_ts
                data_to_write = merged_data
            else:
                data_to_write = {k: v for k, v in data.items() if k != "accounts"}
                if evict_keys:
                    for ev in evict_keys:
                        if isinstance(ev, str) and ev.strip() in incoming_valid:
                            del incoming_valid[ev.strip()]
                data_to_write["accounts"] = incoming_valid
                data_to_write["timestamp"] = root_ts

        tmp_file = None
        try:
            tmp_fd, tmp_file = tempfile.mkstemp(dir=target_dir, prefix="quota-cache-", suffix=".tmp")
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(data_to_write, f, indent=2)
            os.chmod(tmp_file, 0o600)
            os.replace(tmp_file, cache_target_file)
            try:
                os.chmod(cache_target_file, 0o600)
            except OSError:
                pass
            tmp_file = None
        except Exception as e:
            print(f"[QuotaClient] Failed to persist quota cache to {self.cache_file}: {e}", file=sys.stderr)
        finally:
            if tmp_file and os.path.exists(tmp_file):
                try:
                    os.unlink(tmp_file)
                except OSError:
                    pass


def fetch_account_quota(
    engine: AgySwitchEngine,
    account: dict[str, Any] | str,
    client: QuotaClient | None = None,
) -> tuple[str, dict[str, Any]]:
    """Fetches quota for a single account from CloudCode API.

    Returns a tuple of (account_name, quota_dict).
    """
    if isinstance(account, dict):
        acc_name = account.get("name", "")
    else:
        acc_name = str(account)

    if not acc_name:
        return "", {"error": "Invalid account name", "error_type": "validation"}

    if client is None:
        client = QuotaClient(timeout=2.0, engine=engine)

    tokens = engine.get_account_tokens(acc_name)
    if not tokens or not tokens.get("access_token"):
        return acc_name, {"error": "No token", "error_type": "auth"}

    def _on_token_refreshed(new_token_data: dict[str, Any] | str) -> None:
        if isinstance(new_token_data, dict):
            access_tok = str(new_token_data.get("access_token", "")).strip()
            if not access_tok:
                return
            raw_exp = new_token_data.get("expires_in", 3600)
            exp_sec = _sanitize_expires_in(raw_exp, default=3600)
            expiry_iso = (datetime.now(timezone.utc) + timedelta(seconds=exp_sec)).strftime("%Y-%m-%dT%H:%M:%SZ")
            engine.update_account_access_token(acc_name, access_tok, expiry=expiry_iso)
        else:
            engine.update_account_access_token(acc_name, str(new_token_data))

    try:
        try:
            quota_res = client.fetch_quota(
                tokens["access_token"],
                refresh_token=tokens.get("refresh_token"),
                on_refresh=_on_token_refreshed,
            )
        except TypeError as te:
            if "on_refresh" in str(te):
                quota_res = client.fetch_quota(
                    tokens["access_token"],
                    refresh_token=tokens.get("refresh_token"),
                )
            else:
                raise
        if quota_res:
            return acc_name, quota_res
        raw_err = getattr(client, "last_error", None)
        err_msg = raw_err if isinstance(raw_err, str) and raw_err else "Fetch failed"
        raw_type = getattr(client, "last_error_type", None)
        if raw_type == "http" and bool(HTTP_SERVER_ERROR_PATTERN.search(err_msg)):
            err_type = "network"
        elif isinstance(raw_type, str) and raw_type:
            err_type = raw_type
        else:
            err_type = "auth" if _has_auth_indicator(err_msg) else "network"
        return acc_name, {"error": err_msg, "error_type": err_type}
    except Exception as e:
        err_str = str(e)
        err_type = "network" if _is_network_error(e) else ("auth" if _is_auth_error(e) else "general")
        return acc_name, {"error": err_str, "error_type": err_type}


def fetch_all_accounts_quota(
    engine: AgySwitchEngine,
    accounts: list[dict[str, Any]],
    client: QuotaClient | None = None,
    max_workers: int = 5,
) -> dict[str, dict[str, Any]]:
    """Concurrently fetches quota for multiple accounts using ThreadPoolExecutor."""
    import concurrent.futures

    if client is None:
        client = QuotaClient(timeout=2.0, engine=engine)

    if not accounts:
        return {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(fetch_account_quota, engine, acc, client)
            for acc in accounts
        ]
        return dict(f.result() for f in concurrent.futures.as_completed(futures))


def _parse_timestamp_defensive(val: Any) -> float:
    """Safely extracts a positive float UNIX epoch timestamp in UTC or returns float('inf')."""
    if val is None or isinstance(val, bool):
        return float("inf")
    if isinstance(val, (int, float)):
        return float(val) if math.isfinite(val) and val > 0 else float("inf")
    if isinstance(val, str):
        cleaned = val.strip()
        try:
            num = float(cleaned)
            if math.isfinite(num) and num > 0:
                return num
            return float("inf")
        except ValueError:
            pass
        try:
            iso_str = cleaned.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            ts = dt.timestamp()
            return ts if math.isfinite(ts) and ts > 0 else float("inf")
        except Exception:
            pass
    return float("inf")


def _parse_quota_defensive(val: Any) -> int | float:
    """Safely extracts a non-negative quota percentage or returns -1."""
    if val is None or isinstance(val, bool):
        return -1
    if isinstance(val, (int, float)):
        return val if math.isfinite(val) and val >= 0 else -1
    if isinstance(val, str):
        try:
            num = float(val.strip().rstrip("%"))
            if math.isfinite(num) and num >= 0:
                return num
        except ValueError:
            pass
    return -1


def format_countdown(reset_timestamp: int | float | str | None, current_timestamp: float | None = None) -> str:
    """Converts a UNIX epoch reset timestamp into a relative human-readable countdown string.

    Rules:
    - If reset_timestamp is None, non-numeric/unparseable, or <= 0: returns "-"
    - If delta <= 0: returns "expired"
    - If delta < 3600: returns f"{max(1, delta // 60)}m" (e.g. "45m", "1m")
    - If 3600 <= delta < 86400: returns f"{h}h {m}m" if m > 0 else f"{h}h" (e.g. "2h 15m", "2h")
    - If delta >= 86400: returns f"{d}d {h}h" if h > 0 else f"{d}d" (e.g. "4d 2h", "3d")
    """
    ts = _parse_timestamp_defensive(reset_timestamp)
    if math.isinf(ts) or ts <= 0:
        return "-"

    curr = current_timestamp if current_timestamp is not None else time.time()
    if isinstance(curr, bool) or not isinstance(curr, (int, float)) or not math.isfinite(curr):
        curr = time.time()

    delta = int(ts - curr)

    if delta <= 0:
        return "expired"

    if delta < 3600:
        minutes = max(1, delta // 60)
        return f"{minutes}m"

    if delta < 86400:
        h = delta // 3600
        m = (delta % 3600) // 60
        return f"{h}h {m}m" if m > 0 else f"{h}h"

    d = delta // 86400
    h = (delta % 86400) // 3600
    return f"{d}d {h}h" if h > 0 else f"{d}d"


def sort_accounts_by_quota(accounts: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Sorts accounts deterministically using a 3-tier tie-breaking key:

    1. Primary: -quota_val (highest available Gemini quota percentage first).
    2. Secondary: reset_time (earliest reset timestamp first; accounts with reset time <= 0 or None map to float("inf")).
    3. Tertiary: account name (alphabetical ascending for stable sorting).

    Defensively handles missing, non-dict, or error quota payloads without crashing.
    """
    if not accounts:
        return []

    def _sort_key(acc: dict[str, Any]) -> tuple[int | float, float, str]:
        if not isinstance(acc, dict):
            return (1, float("inf"), "")

        q_root = acc.get("quota") if isinstance(acc.get("quota"), dict) else {}
        q_data = q_root.get("gemini") if isinstance(q_root.get("gemini"), dict) else {}

        quota_val: int | float = -1
        reset_time: float = float("inf")

        if isinstance(q_data, dict):
            quota_val = _parse_quota_defensive(q_data.get("quota"))
            reset_time = _parse_timestamp_defensive(q_data.get("reset_time"))

        name = str(acc.get("name") or "")
        return (-quota_val, reset_time, name)

    return sorted(accounts, key=_sort_key)


def run_quota_pipeline(
    engine: AgySwitchEngine,
    data: dict[str, Any] | None,
    client: QuotaClient | None = None,
) -> dict[str, Any]:
    """Executes the standard quota telemetry pipeline:

    1. Fetches quotas concurrently for all accounts via fetch_all_accounts_quota.
    2. Enforces Cache Merge Invariant: merges newly fetched valid account data into last_known_quota.json.
    3. For accounts failing network fetch, loads last known cached telemetry if present and marks cached=True.
       Accounts without cache are marked as Error / Unreachable.
    4. Sorts accounts deterministically using sort_accounts_by_quota.
    """
    if not isinstance(data, dict):
        return {"active": None, "accounts": []}

    if client is None:
        client = QuotaClient(timeout=2.0, engine=engine)

    raw_accounts = data.get("accounts", [])
    if not isinstance(raw_accounts, list) or not raw_accounts:
        return data

    # Defensive copy of accounts to avoid in-place mutation of caller data
    accounts = [dict(acc) for acc in raw_accounts if isinstance(acc, dict)]

    # 1. Concurrently fetch quotas
    raw_quotas = fetch_all_accounts_quota(engine, accounts, client=client)

    # 2. Extract valid quotas and merge into cache
    valid_quotas: dict[str, Any] = {}
    for name, q in raw_quotas.items():
        if isinstance(q, dict) and not q.get("error") and q.get("gemini"):
            valid_quotas[name] = q

    if valid_quotas:
        client.save_cache({
            "timestamp": datetime.now(timezone.utc).timestamp(),
            "accounts": valid_quotas,
        }, merge=True)

    # Reload merged cache to get all historical + new entries
    cache_data = client.load_cache()
    cached_accounts = (
        cache_data.get("accounts", {})
        if isinstance(cache_data, dict) and isinstance(cache_data.get("accounts"), dict)
        else {}
    )

    # 3. Apply quota / cache fallback per account
    for acc in accounts:
        acc_name = acc.get("name", "")
        live_q = raw_quotas.get(acc_name)

        if isinstance(live_q, dict) and not live_q.get("error") and live_q.get("gemini"):
            # Live telemetry succeeded: sanitize to strictly {"gemini": gemini_copy}
            gemini_copy = dict(live_q["gemini"])
            gemini_copy["cached"] = False
            acc["quota"] = {"gemini": gemini_copy}
        else:
            # Live fetch failed or error returned: check cache
            cached_acc = cached_accounts.get(acc_name)
            has_valid_gemini_cache = (
                isinstance(cached_acc, dict)
                and isinstance(cached_acc.get("gemini"), dict)
                and not cached_acc["gemini"].get("error")
                and "quota" in cached_acc["gemini"]
            )
            has_legacy_cache = (
                isinstance(cached_acc, dict)
                and "quota" in cached_acc
                and not cached_acc.get("error")
            )

            if has_valid_gemini_cache:
                gemini_copy = dict(cached_acc["gemini"])
                gemini_copy["cached"] = True
                acc["quota"] = {"gemini": gemini_copy}
            elif has_legacy_cache:
                gemini_copy = {
                    "quota": cached_acc.get("quota", 0),
                    "reset_time": cached_acc.get("reset_time", 0),
                    "cached": True,
                    "buckets": {},
                }
                acc["quota"] = {"gemini": gemini_copy}
            else:
                # No cache available: mark as Unreachable
                acc["quota"] = {
                    "gemini": None,
                    "error": "Unreachable",
                }

    # 4. Sort accounts deterministically
    result = dict(data)
    result["accounts"] = sort_accounts_by_quota(accounts)
    return result


def _format_bucket_countdown(reset_ts: Any, current_timestamp: float | None = None) -> str:
    """Helper to format countdown badge for bucket summaries without broken strings."""
    cd = format_countdown(reset_ts, current_timestamp=current_timestamp)
    if cd == "-":
        return ""
    if cd == "expired":
        return " (expired)"
    return f" (in {cd})"


def _format_gemini_quota_summary(acc: dict[str, Any], current_timestamp: float | None = None) -> str:
    """Extracts and formats Gemini quota summary for display across table and listicle views."""
    if not isinstance(acc, dict):
        return "Error / Unreachable"

    q_root = acc.get("quota") if isinstance(acc.get("quota"), dict) else {}
    gemini_data = q_root.get("gemini") if isinstance(q_root.get("gemini"), dict) else None

    if q_root.get("error") or gemini_data is None or (isinstance(gemini_data, dict) and gemini_data.get("error")):
        return "Error / Unreachable"

    if not isinstance(gemini_data, dict):
        return "Error / Unreachable"

    raw_cached = gemini_data.get("cached")
    is_cached = (
        raw_cached.strip().lower() in ("true", "1")
        if isinstance(raw_cached, str)
        else bool(raw_cached)
    )

    raw_quota = gemini_data.get("quota")
    parsed_q = _parse_quota_defensive(raw_quota)
    quota = int(round(parsed_q)) if parsed_q >= 0 else 0

    if is_cached:
        return f"{quota}% (Cached)"

    buckets = gemini_data.get("buckets") if isinstance(gemini_data.get("buckets"), dict) else {}
    b_5h = buckets.get("5h") if isinstance(buckets.get("5h"), dict) else {}
    b_wk = buckets.get("weekly") if isinstance(buckets.get("weekly"), dict) else (buckets.get("wk") if isinstance(buckets.get("wk"), dict) else {})

    def _render_bucket(tag: str, b_data: dict[str, Any]) -> str:
        raw_b = b_data.get("quota")
        parsed_b = _parse_quota_defensive(raw_b)
        q_b = int(round(parsed_b)) if parsed_b >= 0 else quota
        ts_b = b_data.get("reset_time")
        cd_b = _format_bucket_countdown(ts_b, current_timestamp=current_timestamp)
        return f"{tag}: {q_b}%{cd_b}"

    parts: list[str] = []
    if b_5h:
        parts.append(_render_bucket("5h", b_5h))
    if b_wk:
        parts.append(_render_bucket("wk", b_wk))

    if parts:
        return f"{quota}% [{' | '.join(parts)}]"
    return f"{quota}%"


def format_accounts_listicle(
    result: dict[str, Any] | None,
    show_quota: bool = True,
    current_timestamp: float | None = None,
) -> str:
    """Formats accounts list as a compact, single-line bulleted listicle for LLMs and narrow terminals."""
    if not isinstance(result, dict):
        return "No accounts registered in vault (~/.antigravity-vault/accounts)."

    accounts = result.get("accounts", [])
    if not isinstance(accounts, list) or not accounts:
        return "No accounts registered in vault (~/.antigravity-vault/accounts)."

    lines: list[str] = []
    for acc in accounts:
        if not isinstance(acc, dict):
            continue
        marker = "• [*]" if acc.get("is_active") else "• [ ]"
        email_str = str(acc.get("email") or "").strip() or "-"
        name = str(acc.get("name") or "").strip()
        prefix = f"{marker} {name} ({email_str})"

        if show_quota:
            quota_summary = _format_gemini_quota_summary(acc, current_timestamp=current_timestamp)
            lines.append(f"{prefix} — Gemini: {quota_summary}")
        else:
            status = "Active" if acc.get("is_active") else "Standby"
            if not acc.get("valid", True):
                status += " (Invalid)"
            lines.append(f"{prefix} [{status}]")

    active_acc = result.get("active")
    if active_acc:
        lines.append(f"Active Account: {active_acc}")
    else:
        lines.append("No account currently active. Run: agy-switch switch <account>")

    return "\n".join(lines)


def format_accounts_table(
    result: dict[str, Any] | None,
    show_quota: bool = False,
    current_timestamp: float | None = None,
) -> str:
    """Formats accounts list as a clean, human-readable terminal table focusing exclusively on Gemini."""
    if not isinstance(result, dict):
        return "No accounts registered in vault (~/.antigravity-vault/accounts)."

    accounts = result.get("accounts", [])
    if not isinstance(accounts, list) or not accounts:
        return "No accounts registered in vault (~/.antigravity-vault/accounts)."

    header = f"{'':2} {'ACCOUNT':<20} {'EMAIL':<30} {'STATUS':<15}"
    if show_quota:
        header += " GEMINI QUOTA"

    lines = [
        "Antigravity Accounts (~/.antigravity-vault):",
        "-" * len(header),
        header,
        "-" * len(header),
    ]

    for acc in accounts:
        if not isinstance(acc, dict):
            continue
        marker = "* " if acc.get("is_active") else "  "
        status = "Active" if acc.get("is_active") else "Standby"
        if not acc.get("valid", True):
            status += " (Invalid)"
        email_str = str(acc.get("email") or "").strip() or "-"
        line = f"{marker}{str(acc.get('name') or ''):<20} {email_str:<30} {status:<15}"

        if show_quota:
            quota_summary = _format_gemini_quota_summary(acc, current_timestamp=current_timestamp)
            line += f" {quota_summary}"

        lines.append(line)

    lines.append("-" * len(header))
    active_acc = result.get("active")
    if active_acc:
        lines.append(f"Active Account: {active_acc}")
    else:
        lines.append("No account currently active. Run: agy-switch switch <account>")

    return "\n".join(lines)


def format_status_report(status: dict[str, Any]) -> str:
    """Formats system and server status into a clean terminal report."""
    lines = [
        "Antigravity System Status:",
        "-------------------------------------------------------------",
    ]
    active = status.get("active")
    email = status.get("email") or "unknown"
    if active:
        lines.append(f"Active Account    : {active} ({email})")
        if "cached_quota" in status and isinstance(status["cached_quota"], dict):
            q_data = status["cached_quota"]
            if isinstance(q_data.get("gemini"), dict):
                lines.append(f"Cached Quota      : {q_data['gemini'].get('quota', 0)}% (Gemini)")
            elif isinstance(q_data.get("claude"), dict):
                lines.append(f"Cached Quota      : {q_data['claude'].get('quota', 0)}% (Claude)")
    else:
        lines.append("Active Account    : None (No account active)")

    if status.get("server_running"):
        pid = status.get("server_pid")
        lines.append(f"Language Server   : Running (PID: {pid})")
        if status.get("server_busy"):
            lines.append("Server State      : Busy (Active task running - requires --force to restart)")
        else:
            lines.append("Server State      : Idle (Safe to restart)")
    else:
        lines.append("Language Server   : Stopped / Not Found")

    lines.append("-------------------------------------------------------------")
    return "\n".join(lines)


from dataclasses import dataclass
from typing import Optional

@dataclass
class QuotaStatus:
    fraction: float
    reset_time: datetime

    def get_reset_delta_minutes(self, current_time: datetime) -> float:
        """Calculates reset delta in minutes relative to current_time."""
        return (self.reset_time - current_time).total_seconds() / 60.0

    def is_exhausted(self) -> bool:
        """Returns True if the quota is exhausted (<= 0% remaining)."""
        return self.fraction <= 0.0

    def has_available_quota(self) -> bool:
        """Returns True if there is positive quota remaining (> 0%)."""
        return self.fraction > 0.0

    def remaining_percentage(self) -> int:
        """Returns remaining quota as an integer percentage (0-100)."""
        return int(self.fraction * 100)

@dataclass
class EvaluationCandidate:
    account_id: str
    status: QuotaStatus
    reset_delta: float

class RotationEngine:
    def __init__(self, model_name: str | None = "gemini", active_account_id: str | None = None):
        self.model_name = model_name or "gemini"
        self.active_account_id = active_account_id
        
        model_lower = (self.model_name or "gemini").lower()
        if "claude" in model_lower:
            self.target_group_prefix = "Claude"
            self.target_model_key = "claude"
        else:
            self.target_group_prefix = "Gemini"
            self.target_model_key = "gemini"

    def _parse_time(self, t_val: Any) -> datetime:
        if not t_val:
            return datetime.max.replace(tzinfo=timezone.utc)
        if isinstance(t_val, datetime):
            return t_val if t_val.tzinfo else t_val.replace(tzinfo=timezone.utc)
        if isinstance(t_val, (int, float)):
            try:
                return datetime.fromtimestamp(t_val, tz=timezone.utc)
            except Exception:
                return datetime.max.replace(tzinfo=timezone.utc)
        if isinstance(t_val, str):
            if t_val.isdigit():
                try:
                    return datetime.fromtimestamp(int(t_val), tz=timezone.utc)
                except Exception:
                    pass
            t_str = t_val
            if t_str.endswith("Z"):
                t_str = t_str[:-1] + "+00:00"
            try:
                dt = datetime.fromisoformat(t_str)
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except Exception:
                return datetime.max.replace(tzinfo=timezone.utc)
        return datetime.max.replace(tzinfo=timezone.utc)

    def get_quota_status(self, quota_data: dict, model_key: str | None = None) -> Optional[QuotaStatus]:
        if not quota_data or not isinstance(quota_data, dict):
            return None

        m_key = model_key or self.target_model_key
        
        # 1. Check last_known_quota.json cache schema: {"gemini": {"quota": X, "reset_time": Y}}
        target_info = None
        if m_key in quota_data and isinstance(quota_data[m_key], dict):
            target_info = quota_data[m_key]
        elif "quota" in quota_data and not isinstance(quota_data["quota"], dict):
            target_info = quota_data

        if target_info is not None and isinstance(target_info, dict):
            q_val = target_info.get("quota")
            r_val = target_info.get("reset_time")
            if q_val is not None:
                if isinstance(q_val, int):
                    fraction = max(0.0, min(1.0, float(q_val) / 100.0))
                elif isinstance(q_val, float):
                    if q_val > 1.0:
                        fraction = float(q_val) / 100.0
                    elif q_val >= 0.0:
                        fraction = float(q_val)
                    else:
                        fraction = 0.0
                else:
                    fraction = 0.0
                reset_time = self._parse_time(r_val)
                return QuotaStatus(fraction=fraction, reset_time=reset_time)

        # 2. Check CloudCode raw groups schema: {"groups": [{"group": "...", "buckets": [...]}]}
        if "groups" in quota_data:
            target_prefix = "Claude" if "claude" in m_key.lower() else "Gemini"
            target_group = None
            for group in quota_data.get("groups", []):
                g_name = group.get("displayName") or group.get("group", "")
                if g_name.startswith(target_prefix):
                    target_group = group
                    break

            if target_group and "buckets" in target_group:
                lowest_fraction = 1.0
                earliest_reset = datetime.max.replace(tzinfo=timezone.utc)
                for bucket in target_group["buckets"]:
                    frac = bucket.get("remainingFraction", 1.0)
                    reset_time = self._parse_time(bucket.get("resetTime"))
                    if frac < lowest_fraction:
                        lowest_fraction = frac
                        earliest_reset = reset_time
                    elif frac == lowest_fraction and reset_time < earliest_reset:
                        earliest_reset = reset_time
                return QuotaStatus(fraction=lowest_fraction, reset_time=earliest_reset)

        return None

    def is_exhausted(self, quota_data: dict) -> bool:
        """Returns True if the relevant quota is completely exhausted (0% remaining)."""
        status = self.get_quota_status(quota_data)
        if status is None:
            return False
        return status.is_exhausted()

    def has_available_quota(self, quota_data: dict) -> bool:
        """Returns True if the relevant quota has positive remaining quota."""
        status = self.get_quota_status(quota_data)
        if status is None:
            return False
        return status.has_available_quota()

    def calculate_reset_delta(self, reset_time: datetime, current_time: datetime) -> float:
        """Calculates reset delta in minutes."""
        return QuotaStatus(fraction=0.0, reset_time=reset_time).get_reset_delta_minutes(current_time)

    def evaluate(
        self,
        accounts: list[dict] | dict,
        current_active: str | None = None,
        target_model: str = "gemini",
        current_time: datetime | None = None,
    ) -> str | None:
        """Evaluates accounts and returns the optimal account to switch to, or None if current_active is optimal."""
        if current_time is None:
            current_time = datetime.now(timezone.utc)
        elif current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)

        if not target_model:
            target_model = self.model_name or "gemini"
        model_key = "claude" if "claude" in target_model.lower() else "gemini"

        # Normalize accounts into a list of (account_id, quota_dict)
        items: list[tuple[str, dict]] = []
        if isinstance(accounts, dict):
            if "accounts" in accounts and isinstance(accounts["accounts"], dict):
                raw_dict = accounts["accounts"]
            else:
                raw_dict = accounts
            for acc_id, q_data in raw_dict.items():
                items.append((acc_id, q_data if isinstance(q_data, dict) else {}))
        elif isinstance(accounts, list):
            for acc in accounts:
                if not isinstance(acc, dict):
                    continue
                acc_name = acc.get("name") or acc.get("account") or acc.get("id")
                if not acc_name:
                    continue
                if current_active is None and acc.get("is_active"):
                    current_active = acc_name
                if "quota" in acc and isinstance(acc["quota"], dict):
                    q_data = acc["quota"]
                else:
                    q_data = acc
                items.append((acc_name, q_data))

        if current_active is None:
            current_active = self.active_account_id

        evaluated: dict[str, QuotaStatus] = {}
        reset_deltas: dict[str, float] = {}

        for acc_id, q_data in items:
            status = self.get_quota_status(q_data, model_key=model_key)
            if status is not None:
                evaluated[acc_id] = status
                reset_deltas[acc_id] = self.calculate_reset_delta(status.reset_time, current_time)

        if not evaluated:
            return None

        active_status = evaluated.get(current_active) if current_active else None

        # 1. cache_optimized heuristic: retain current active if fraction > 0.15 (> 15%)
        if active_status is not None and active_status.fraction > 0.15:
            return None

        # 2. drain_window heuristic: retain current active if fraction > 0.05 and reset in <= 30m
        if active_status is not None and active_status.fraction > 0.05:
            reset_delta = reset_deltas.get(current_active, float("inf"))
            if 0 <= reset_delta <= 30:
                return None

        # 3. Candidate evaluation
        # Active account is explicitly excluded from being selected as the fallback.
        # Candidates must strictly have positive quota (fraction > 0.0).
        candidates: list[EvaluationCandidate] = []
        for acc_id, status in evaluated.items():
            if acc_id == current_active:
                continue
            if status.fraction <= 0.0 or not status.has_available_quota():
                continue
            candidates.append(
                EvaluationCandidate(
                    account_id=acc_id,
                    status=status,
                    reset_delta=reset_deltas.get(acc_id, float("inf")),
                )
            )

        if not candidates:
            return None

        # Imminent reset priority: accounts nearing reset (reset_delta <= 45m)
        # with healthy quota (fraction >= 0.15).
        # Imminent accounts with very low quota (< 15%) do not qualify for imminent priority
        # so they do not starve accounts with higher / 100% quota.
        imminent_candidates = [
            c for c in candidates
            if 0 <= c.reset_delta <= 45 and c.status.fraction >= 0.15
        ]

        if imminent_candidates:
            # Sort by highest fraction first, tie-break by earliest reset delta
            imminent_candidates.sort(key=lambda c: (-c.status.fraction, c.reset_delta))
            return imminent_candidates[0].account_id

        # Prioritize candidate with highest absolute quota, tie-break by earliest reset delta
        candidates.sort(key=lambda c: (-c.status.fraction, c.reset_delta))
        return candidates[0].account_id


def _is_auth_error(error_val: Any, client: Any = None) -> bool:
    """Classifies whether an error represents auth/token corruption (401/403, revoked, no token)."""
    if error_val is None:
        return False
    if isinstance(error_val, dict):
        raw_type = error_val.get("error_type")
        if raw_type == "auth":
            return True
        code = error_val.get("code") or error_val.get("status_code")
        if isinstance(code, str) and code.isdigit():
            code = int(code)
        if isinstance(code, int):
            if code in (401, 403):
                return True
            if code in (500, 502, 503, 504):
                return False
        err_str = str(error_val.get("error", "")).lower()
        if raw_type == "http" and _has_auth_indicator(err_str):
            return True
        if isinstance(raw_type, str) and raw_type.strip() and raw_type.strip() != "auth":
            return False
    elif isinstance(error_val, Exception):
        if isinstance(error_val, urllib.error.HTTPError):
            if error_val.code in (401, 403):
                return True
            if error_val.code in (500, 502, 503, 504):
                return False
        if isinstance(error_val, (TimeoutError, ConnectionError)):
            return False
        if isinstance(error_val, urllib.error.URLError) and not isinstance(error_val, urllib.error.HTTPError):
            return False
        err_str = str(error_val).lower()
    elif isinstance(error_val, str):
        err_str = error_val.lower()
    else:
        err_str = str(error_val).lower()

    if _has_auth_indicator(err_str):
        return True

    if client is not None:
        c_type = getattr(client, "last_error_type", None)
        if isinstance(c_type, str) and c_type == "auth":
            return True
        c_err = getattr(client, "last_error", None)
        if isinstance(c_err, str) and _has_auth_indicator(c_err):
            return True

    return False


def _is_network_error(error_val: Any, client: Any = None) -> bool:
    """Classifies whether an error represents a transient network transport failure."""
    if error_val is None:
        return False
    if isinstance(error_val, urllib.error.HTTPError):
        # Only classify 5xx server errors as transient network/transport errors
        return error_val.code in (500, 502, 503, 504)
    if isinstance(error_val, (urllib.error.URLError, TimeoutError, ConnectionError)):
        return True
    if isinstance(error_val, dict):
        if error_val.get("error_type") == "network":
            return True
        if error_val.get("error_type") == "auth":
            return False
        if error_val.get("error_type") == "http":
            code = error_val.get("code") or error_val.get("status_code")
            if isinstance(code, int):
                return code in (500, 502, 503, 504)
            err_str = str(error_val.get("error", "")).lower()
            return bool(HTTP_SERVER_ERROR_PATTERN.search(err_str))
        err_str = str(error_val.get("error", "")).lower()
    elif isinstance(error_val, Exception):
        err_str = str(error_val).lower()
    elif isinstance(error_val, str):
        err_str = error_val.lower()
    else:
        err_str = str(error_val).lower()

    net_keywords = [
        "timeout", "timed out", "urlerror", "network", "unreachable",
        "connection", "reset by peer", "failed to connect", "socket",
        "dns", "authoritative",
    ]
    is_net_error = any(k in err_str for k in net_keywords) or bool(HTTP_SERVER_ERROR_PATTERN.search(err_str))
    if is_net_error:
        if not _has_auth_indicator(err_str):
            return True

    if client is not None:
        c_type = getattr(client, "last_error_type", None)
        if isinstance(c_type, str) and c_type == "network":
            return True
        if isinstance(c_type, str) and c_type == "http":
            c_err = getattr(client, "last_error", None)
            if isinstance(c_err, str) and bool(HTTP_SERVER_ERROR_PATTERN.search(c_err)):
                return True
        c_err = getattr(client, "last_error", None)
        if isinstance(c_err, str):
            c_err_lower = c_err.lower()
            c_is_net = any(k in c_err_lower for k in net_keywords) or bool(HTTP_SERVER_ERROR_PATTERN.search(c_err_lower))
            if c_is_net and not _has_auth_indicator(c_err_lower):
                return True

    return False


def _is_stale_exhausted_cache(
    cached_entry: Any,
    rot_engine: RotationEngine,
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
        model_key = "claude" if "claude" in model_name.lower() else "gemini"
        display_model = "Gemini" if model_name.lower() in ("auto", "gemini", "") else model_name

        # Wire in QuotaClient and RotationEngine
        try:
            active_name = engine.get_active_name()
            client = QuotaClient(timeout=2.0, engine=engine)
            rot_engine = RotationEngine(model_name=model_name, active_account_id=active_name)

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
                    if _is_auth_error(err_obj, client=client):
                        # Auth/token corruption: do not fall back to cache; signal Tier 3 failover
                        print(f"[agy-switch hook] Active account '{active_name}' auth corruption: {probe_error}. Triggering Tier 3 failover.", file=sys.stderr)
                    elif _is_network_error(err_obj, client=client) or _is_network_error(probe_error, client=client):
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
                elif q_data is not None and _is_auth_error(q_data):
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
                and _is_auth_error(err_obj, client=client)
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


def main(argv: list[str] | None = None, engine: AgySwitchEngine | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agy-switch",
        description="High-reliability account switcher for Antigravity 2.0 & CLI",
    )
    subparsers = parser.add_subparsers(dest="command")

    # Command: list
    list_parser = subparsers.add_parser("list", help="List registered accounts and show active account")
    list_parser.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    list_parser.add_argument("--format", choices=["table", "list"], default="table", help="Output format: table (default) or list (listicle)")
    list_parser.add_argument("--quota", action="store_true", help="Fetch and display quota concurrently")

    # Command: quota
    quota_parser = subparsers.add_parser("quota", help="Fetch and display current quota rankings across all accounts")
    quota_parser.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    quota_parser.add_argument("--format", choices=["table", "list"], default="table", help="Output format: table (default) or list (listicle)")

    # Command: status
    subparsers.add_parser("status", help="Show system status, active account, and server health")

    # Command: switch
    switch_parser = subparsers.add_parser("switch", help="Switch active account across Antigravity 2.0 and CLI")
    switch_parser.add_argument("account", nargs="?", help="Name of the account to switch to")
    switch_parser.add_argument("--no-restart", action="store_true", help="Do not restart language_server process")
    switch_parser.add_argument("--force", action="store_true", help="Force switch even if active turns are detected")
    switch_parser.add_argument("--auto", action="store_true", help="Auto-switch to optimal account")

    # Command: add
    add_parser = subparsers.add_parser("add", help="Register a new account from token JSON file")
    add_parser.add_argument("name", help="Account name/alias to register")
    add_parser.add_argument("--token-file", required=True, type=Path, help="Path to token JSON file")

    # Command: remove
    remove_parser = subparsers.add_parser("remove", help="Remove an account from vault")
    remove_parser.add_argument("name", help="Account name to remove")
    remove_parser.add_argument("--force", action="store_true", help="Force removal even if active")

    # Command: migrate
    subparsers.add_parser("migrate", help="Import legacy accounts from ~/.agy-cli-manager")

    # Command: login
    login_parser = subparsers.add_parser("login", help="Interactive Google OAuth login in an isolated sandbox")
    login_parser.add_argument("name", help="Account name/alias to assign in the vault")
    login_parser.add_argument("--agy-binary", help="Path to agy binary (default: auto-detected)")
    login_parser.add_argument("--timeout", type=int, default=600, help="Timeout in seconds (default: 600)")
    login_parser.add_argument("--force", action="store_true", help="Overwrite existing account without confirmation prompt")

    # Command: config
    config_parser = subparsers.add_parser("config", help="Manage vault configuration")
    config_subparsers = config_parser.add_subparsers(dest="config_command")
    auto_switch_parser = config_subparsers.add_parser("auto-switch", help="Configure auto-switch toggle (on/off)")
    auto_switch_parser.add_argument("state", nargs="?", choices=["on", "off"], help="State (on/off)")

    # Command: hook
    hook_parser = subparsers.add_parser("hook", help="Manage Antigravity lifecycle hooks")
    hook_subparsers = hook_parser.add_subparsers(dest="hook_command")
    hook_subparsers.add_parser("install", help="Install PreInvocation lifecycle hook into hooks.json")

    # Command: hook-pre-invocation
    subparsers.add_parser("hook-pre-invocation", help="Antigravity PreInvocation lifecycle hook handler")

    # Command: watch
    watch_parser = subparsers.add_parser(
        "watch",
        help="Manage background daemon to tail logs and auto-switch on quota exhaustion",
    )
    watch_parser.add_argument(
        "watch_action",
        nargs="?",
        choices=["run", "start", "stop", "status"],
        default="run",
        help="Action to perform: run (default foreground), start (background daemon), stop, status",
    )
    watch_parser.add_argument(
        "--generate-systemd",
        action="store_true",
        help="Output systemd user service unit definition",
    )
    watch_parser.add_argument(
        "--install-systemd",
        action="store_true",
        help="Install systemd user service unit to ~/.config/systemd/user/agy-switch-watcher.service",
    )
    watch_parser.add_argument(
        "--cooldown",
        type=int,
        default=60,
        help="Default cooldown minutes for exhausted accounts (default: 60)",
    )

    # Command: internal-probe
    subparsers.add_parser("internal-probe", help=argparse.SUPPRESS)

    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    if engine is None:
        engine = AgySwitchEngine()

    # Perform auto-migration if needed on startup
    migrated = engine.auto_migrate_if_needed()
    if migrated and len(migrated) > 0 and args.command != "migrate":
        if args.command == "hook-pre-invocation" or getattr(args, "json", False):
            print(f"[agy-switch] Auto-migrated {len(migrated)} account(s) from agy-cli-manager: {', '.join(migrated)}", file=sys.stderr)
        else:
            print(f"[agy-switch] Auto-migrated {len(migrated)} account(s) from agy-cli-manager: {', '.join(migrated)}")

    if args.command in ("quota", "list") or not args.command:
        data = engine.list_accounts()
        show_quota = True if args.command == "quota" else getattr(args, "quota", False)
        if show_quota:
            data = run_quota_pipeline(engine, data)

        if getattr(args, "json", False):
            print(json.dumps(data, indent=2))
        elif getattr(args, "format", "table") == "list":
            print(format_accounts_listicle(data, show_quota=show_quota))
        else:
            print(format_accounts_table(data, show_quota=show_quota))
        return 0

    if args.command == "status":
        status_info = engine.get_status()
        
        client = QuotaClient(timeout=2.0, engine=engine)
        cache_data = client.load_cache()
        
        now = datetime.now(timezone.utc).timestamp()
        cache_age = now - cache_data.get("timestamp", 0)
        
        if cache_age > 60:
            script_abs_path = str(Path(__file__).resolve())
            subprocess.Popen([sys.executable, script_abs_path, "internal-probe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
        if "accounts" in cache_data and status_info.get("active"):
            active_name = status_info["active"]
            q_data = cache_data["accounts"].get(active_name, {})
            status_info["cached_quota"] = q_data
            
        print(format_status_report(status_info))
        return 0

    if args.command == "internal-probe":
        accounts_data = engine.list_accounts().get("accounts", [])
        client = QuotaClient(timeout=2.0, engine=engine)
        raw_quotas = fetch_all_accounts_quota(engine, accounts_data, client=client)
        valid_quotas = {k: v for k, v in raw_quotas.items() if "error" not in v and v}
                    
        client.save_cache({
            "timestamp": datetime.now(timezone.utc).timestamp(),
            "accounts": valid_quotas
        })
        return 0

    if args.command == "switch":
        if getattr(args, "auto", False):
            active_name = engine.get_active_name()
            accounts_data = engine.list_accounts().get("accounts", [])
            
            client = QuotaClient(timeout=2.0, engine=engine)
            raw_quotas = fetch_all_accounts_quota(engine, accounts_data, client=client)
            valid_quotas = {k: v for k, v in raw_quotas.items() if "error" not in v and v}
            
            if not valid_quotas:
                print(f"[agy-switch] No valid quota info available to auto-switch.")
                return 1

            client.save_cache({
                "timestamp": datetime.now(timezone.utc).timestamp(),
                "accounts": valid_quotas
            })
            
            for acc in accounts_data:
                acc["quota"] = valid_quotas.get(acc["name"], {})
            
            rot_engine = RotationEngine(model_name="gemini", active_account_id=active_name)
            target_account = rot_engine.evaluate(accounts_data, current_active=active_name, target_model="gemini")
            
            if target_account is None or target_account == active_name:
                print(f"[agy-switch] Active account '{active_name}' is optimal. No switch needed.")
                return 0
                
            args.account = target_account
            
        elif getattr(args, "account", None) is None:
            print("Error: account name must be provided if --auto is not used.", file=sys.stderr)
            return 1

        try:
            res = engine.switch_tokens(
                args.account,
                restart_server=not args.no_restart,
                force=args.force,
            )
            print(f"[agy-switch] Successfully switched active account to '{res['active']}' ({res.get('email') or 'unknown'}).")
            print("  - Updated: ~/.gemini/jetski-standalone-oauth-token (Antigravity 2.0)")
            print("  - Updated: ~/.gemini/antigravity-cli/antigravity-oauth-token (Antigravity CLI)")
            if res.get("server_restarted"):
                print("  - Server : Restarted language_server (new credentials loaded)")
            elif not args.no_restart:
                print("  - Server : language_server not running or no restart needed")
            return 0
        except ActiveTurnRunningError as e:
            print(f"Warning: {e}", file=sys.stderr)
            return 2
        except AgySwitchError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    if args.command == "add":
        try:
            res = engine.add_account(args.name, args.token_file)
            print(f"[agy-switch] Account '{res['name']}' registered successfully.")
            if res.get("email"):
                print(f"  - Email: {res['email']}")
            print(f"  - Vault: {engine.accounts_dir / res['name'] / 'token.json'}")
            return 0
        except AgySwitchError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    if args.command == "login":
        try:
            res = engine.login_account(
                args.name,
                agy_binary=args.agy_binary,
                timeout=args.timeout,
                force=args.force,
            )
            email_info = f" ({res['email']})" if res.get("email") else ""
            print(f"[agy-switch] Account '{res['name']}'{email_info} successfully logged in and saved to vault.")
            print(f"  - Status: Standby (Run 'agy-switch switch {res['name']}' to activate)")
            print(f"  - Vault : {engine.accounts_dir / res['name'] / 'token.json'}")
            return 0
        except AgySwitchError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    if args.command == "remove":
        try:
            engine.remove_account(args.name, force=args.force)
            print(f"[agy-switch] Account '{args.name}' removed successfully from vault.")
            return 0
        except AgySwitchError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    if args.command == "migrate":
        if migrated:
            print(f"[agy-switch] Successfully migrated {len(migrated)} account(s): {', '.join(migrated)}")
        else:
            print("[agy-switch] No new accounts to migrate from legacy ~/.agy-cli-manager.")
        return 0

    if args.command == "config":
        if getattr(args, "config_command", None) == "auto-switch":
            state = getattr(args, "state", None)
            if state == "on":
                engine.set_auto_switch(True)
                print("[agy-switch] auto-switch enabled.")
            elif state == "off":
                engine.set_auto_switch(False)
                print("[agy-switch] auto-switch disabled.")
            else:
                current = engine.get_auto_switch()
                print(f"[agy-switch] auto-switch is currently {'on' if current else 'off'}.")
            return 0
        cfg = engine.load_config()
        print(json.dumps(cfg, indent=2))
        return 0

    if args.command == "hook":
        if getattr(args, "hook_command", None) == "install":
            hooks_path = engine.install_hook()
            print(f"[agy-switch] PreInvocation hook registered successfully in {hooks_path}.")
            return 0
        hook_parser.print_help()
        return 0

    if args.command == "watch":
        try:
            import watch
        except ImportError:
            from scripts import watch

        dm = watch.DaemonManager(
            vault_dir=engine.vault_dir,
            proc_dir=getattr(engine, "proc_dir", None),
            gemini_dir=getattr(engine, "gemini_dir", None),
        )

        if getattr(args, "generate_systemd", False) or getattr(args, "install_systemd", False):
            target = (
                Path.home() / ".config" / "systemd" / "user" / "agy-switch-watcher.service"
                if getattr(args, "install_systemd", False)
                else None
            )
            unit_content = dm.generate_systemd(target_path=target)
            sys.stdout.write(unit_content)
            sys.stdout.flush()
            if target:
                print(f"[agy-switch watch] Installed systemd unit to {target}", file=sys.stderr)
            return 0

        action = getattr(args, "watch_action", None) or "run"

        if action == "start":
            try:
                cooldown = getattr(args, "cooldown", 60)
                pid = dm.start(cooldown=cooldown)
                print(f"[agy-switch watch] Daemon started in background (PID: {pid}).")
                print(f"  - Log: {dm.log_file}")
                print(f"  - PID: {dm.pid_file}")
                return 0
            except watch.DaemonAlreadyRunningError as e:
                print(f"Error: {e}", file=sys.stderr)
                return 1
            except Exception as e:
                print(f"Error starting daemon: {e}", file=sys.stderr)
                return 1

        elif action == "stop":
            try:
                stopped = dm.stop()
                if stopped:
                    print("[agy-switch watch] Daemon stopped successfully.")
                else:
                    print("[agy-switch watch] No active daemon was running.")
                return 0
            except Exception as e:
                print(f"Error stopping daemon: {e}", file=sys.stderr)
                return 1

        elif action == "status":
            try:
                st = dm.status()
                if st["running"]:
                    print(f"[agy-switch watch] Daemon is running (PID: {st['pid']}).")
                    print(f"  - Log: {st['log_file']}")
                    if st.get("targets"):
                        print("  - Monitored targets:")
                        for t in st["targets"]:
                            print(f"    * {t}")
                else:
                    print("[agy-switch watch] Daemon is stopped.")
                    print(f"  - Log: {st['log_file']}")
                return 0
            except Exception as e:
                print(f"Error checking daemon status: {e}", file=sys.stderr)
                return 1

        elif action == "run":
            cooldown_mins = getattr(args, "cooldown", 60)
            try:
                signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))
            except Exception:
                pass
            try:
                watcher = watch.LogWatcher(engine, cooldown_minutes=cooldown_mins)
                watcher.watch_loop()
                return 0
            except KeyboardInterrupt:
                return 0

    if args.command == "hook-pre-invocation":
        return handle_pre_invocation_hook(engine)

    return 0


if __name__ == "__main__":
    sys.exit(main())

