#!/usr/bin/env python3
"""agy-switch: High-reliability account switcher for Antigravity 2.0 & CLI.

Pure Python 3 standard library. Zero external dependencies.
"""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
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

    def _atomic_write_json(self, target_path: Path, payload: dict[str, Any]) -> None:
        """Writes JSON to a target path atomically with 0600 permissions."""
        target_path.parent.mkdir(parents=True, exist_ok=True)
        temp_file = target_path.with_suffix(f".tmp.{os.getpid()}")
        try:
            content = json.dumps(payload, indent=2) + "\n"
            temp_file.write_text(content, encoding="utf-8")
            os.chmod(temp_file, 0o600)
            os.replace(temp_file, target_path)
        finally:
            if temp_file.exists():
                temp_file.unlink(missing_ok=True)

    def update_account_access_token(self, name: str, access_token: str) -> bool:
        """Updates the access token in vault and syncs to active targets if current."""
        token_path = self.accounts_dir / name / "token.json"
        if not token_path.is_file():
            return False
        try:
            raw = token_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if "token" not in data or not isinstance(data["token"], dict):
                return False
            data["token"]["access_token"] = access_token
            self._atomic_write_json(token_path, data)

            if name == self.get_active_name():
                token_payload = json.dumps(data, indent=2) + "\n"
                jetski_token_path = self.gemini_dir / "jetski-standalone-oauth-token"
                temp_jetski = jetski_token_path.with_suffix(f".tmp.{os.getpid()}")
                try:
                    temp_jetski.write_text(token_payload, encoding="utf-8")
                    os.chmod(temp_jetski, 0o600)
                    os.replace(temp_jetski, jetski_token_path)
                finally:
                    if temp_jetski.exists():
                        temp_jetski.unlink(missing_ok=True)

                cli_token_path = self.gemini_dir / "antigravity-cli" / "antigravity-oauth-token"
                temp_cli = cli_token_path.with_suffix(f".tmp.{os.getpid()}")
                try:
                    temp_cli.write_text(token_payload, encoding="utf-8")
                    os.chmod(temp_cli, 0o600)
                    os.replace(temp_cli, cli_token_path)
                finally:
                    if temp_cli.exists():
                        temp_cli.unlink(missing_ok=True)
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
        temp_file = self.active_file.with_suffix(f".tmp.{os.getpid()}")
        try:
            temp_file.write_text(f"{name.strip()}\n", encoding="utf-8")
            os.chmod(temp_file, 0o600)
            os.replace(temp_file, self.active_file)
        finally:
            if temp_file.exists():
                temp_file.unlink(missing_ok=True)

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

        # 1. Update Antigravity 2.0 (WSL / Desktop) token
        jetski_token_path = self.gemini_dir / "jetski-standalone-oauth-token"
        jetski_token_path.parent.mkdir(parents=True, exist_ok=True)
        temp_jetski = jetski_token_path.with_suffix(f".tmp.{os.getpid()}")
        try:
            temp_jetski.write_text(token_payload, encoding="utf-8")
            os.chmod(temp_jetski, 0o600)
            os.replace(temp_jetski, jetski_token_path)
        finally:
            if temp_jetski.exists():
                temp_jetski.unlink(missing_ok=True)

        # 2. Update Antigravity CLI token
        cli_token_path = self.gemini_dir / "antigravity-cli" / "antigravity-oauth-token"
        cli_token_path.parent.mkdir(parents=True, exist_ok=True)
        temp_cli = cli_token_path.with_suffix(f".tmp.{os.getpid()}")
        try:
            temp_cli.write_text(token_payload, encoding="utf-8")
            os.chmod(temp_cli, 0o600)
            os.replace(temp_cli, cli_token_path)
        finally:
            if temp_cli.exists():
                temp_cli.unlink(missing_ok=True)

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

    def refresh_access_token(self, refresh_token: str) -> str | None:
        """Refreshes the OAuth access token."""
        data = urllib.parse.urlencode({
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }).encode("utf-8")

        req = urllib.request.Request(self.token_endpoint, data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
                token_data = json.loads(body)
                return token_data.get("access_token")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            print(f"[QuotaClient] Failed to refresh token: {e}", file=sys.stderr)
            return None

    def parse_quota_response(self, raw_resp: dict[str, Any]) -> dict[str, Any]:
        """Parses CloudCode response into the standardized cache schema."""
        result = {}
        for group in raw_resp.get("groups", []):
            group_name = group.get("displayName") or group.get("group", "")
            if "Gemini" in group_name:
                key = "gemini"
            elif "Claude" in group_name:
                key = "claude"
            else:
                continue

            buckets = group.get("buckets", [])
            if not buckets:
                continue
            
            worst_bucket = min(buckets, key=lambda b: b.get("remainingFraction", 1.0))
            quota_pct = int(worst_bucket.get("remainingFraction", 1.0) * 100)
            reset_time_str = worst_bucket.get("resetTime")
            reset_time = 0
            if reset_time_str:
                try:
                    rt = reset_time_str.replace("Z", "+00:00")
                    # Using datetime.fromisoformat with "+00:00" allows python 3.10 parsing
                    reset_time = int(datetime.fromisoformat(rt).timestamp())
                except Exception:
                    pass
            
            result[key] = {
                "quota": quota_pct,
                "reset_time": reset_time,
            }
        return result

    def fetch_quota(
        self,
        access_token: str,
        refresh_token: str | None = None,
        on_refresh: Any | None = None,
    ) -> dict[str, Any]:
        """Fetches quota from the internal CloudCode API."""
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
                new_access_token = self.refresh_access_token(refresh_token)
                if new_access_token:
                    if on_refresh:
                        try:
                            on_refresh(new_access_token)
                        except Exception:
                            pass
                    return self.fetch_quota(new_access_token, refresh_token=None)
            print(f"[QuotaClient] HTTP Error {e.code}: {e.reason}", file=sys.stderr)
            return {}
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            print(f"[QuotaClient] Network or Parse Error: {e}", file=sys.stderr)
            return {}

    def load_cache(self) -> dict[str, Any]:
        """Loads cached quota data from vault cache or legacy fallback."""
        if self.cache_file.is_file():
            try:
                return json.loads(self.cache_file.read_text(encoding="utf-8"))
            except Exception:
                return {}
        legacy_cache = Path.home() / ".gemini" / "config" / "skills" / "agy-quota-checker" / "last_known_quota.json"
        if legacy_cache.is_file():
            try:
                return json.loads(legacy_cache.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def save_cache(self, data: dict[str, Any]) -> None:
        """Atomically saves quota cache to vault cache directory."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.cache_dir, 0o700)
        except OSError:
            pass
        
        try:
            tmp_fd, tmp_file = tempfile.mkstemp(dir=self.cache_dir, prefix="quota-cache-", suffix=".tmp")
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.chmod(tmp_file, 0o600)
            os.replace(tmp_file, self.cache_file)
        except Exception:
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
        return "", {"error": "Invalid account name"}

    if client is None:
        client = QuotaClient(timeout=2.0)

    tokens = engine.get_account_tokens(acc_name)
    if not tokens or not tokens.get("access_token"):
        return acc_name, {"error": "No token"}

    def _on_token_refreshed(new_tok: str) -> None:
        engine.update_account_access_token(acc_name, new_tok)

    try:
        try:
            quota_res = client.fetch_quota(
                tokens["access_token"],
                refresh_token=tokens.get("refresh_token"),
                on_refresh=_on_token_refreshed,
            )
        except TypeError:
            quota_res = client.fetch_quota(
                tokens["access_token"],
                refresh_token=tokens.get("refresh_token"),
            )
        return acc_name, quota_res if quota_res else {"error": "Fetch failed"}
    except Exception as e:
        return acc_name, {"error": str(e)}


def fetch_all_accounts_quota(
    engine: AgySwitchEngine,
    accounts: list[dict[str, Any]],
    client: QuotaClient | None = None,
    max_workers: int = 5,
) -> dict[str, dict[str, Any]]:
    """Concurrently fetches quota for multiple accounts using ThreadPoolExecutor."""
    import concurrent.futures

    if client is None:
        client = QuotaClient(timeout=2.0)

    if not accounts:
        return {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(fetch_account_quota, engine, acc, client)
            for acc in accounts
        ]
        return dict(f.result() for f in concurrent.futures.as_completed(futures))


def format_accounts_table(result: dict[str, Any], show_quota: bool = False) -> str:
    """Formats accounts list as a clean, human-readable terminal table."""
    accounts = result.get("accounts", [])
    if not accounts:
        return "No accounts registered in vault (~/.antigravity-vault/accounts)."

    header = f"{'':2} {'ACCOUNT':<20} {'EMAIL':<30} {'STATUS':<15}"
    if show_quota:
        header += " QUOTA"
        
    lines = [
        "Antigravity Accounts (~/.antigravity-vault):",
        "-" * len(header),
        header,
        "-" * len(header),
    ]

    for acc in accounts:
        marker = "* " if acc["is_active"] else "  "
        status = "Active" if acc["is_active"] else "Standby"
        if not acc["valid"]:
            status += " (Invalid)"
        email_str = acc["email"] or "-"
        line = f"{marker}{acc['name']:<20} {email_str:<30} {status:<15}"
        
        if show_quota:
            q_data = acc.get("quota", {})
            if "error" in q_data:
                line += " Error"
            elif "gemini" in q_data:
                line += f" {q_data['gemini'].get('quota', 0)}%"
            elif "claude" in q_data:
                line += f" {q_data['claude'].get('quota', 0)}% (C)"
            else:
                line += " 0%"
                
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
        if "cached_quota" in status:
            q_data = status["cached_quota"]
            if "gemini" in q_data:
                lines.append(f"Cached Quota      : {q_data['gemini'].get('quota', 0)}% (Gemini)")
            elif "claude" in q_data:
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
                if isinstance(q_val, (int, float)):
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


def handle_pre_invocation_hook(engine: AgySwitchEngine) -> int:
    if not engine.get_auto_switch():
        sys.stdout.write(json.dumps({}) + "\n")
        sys.stdout.flush()
        return 0
    try:
        raw = sys.stdin.read()
        _payload = json.loads(raw) if raw.strip() else {}
        model_name = _payload.get("modelName", "gemini-1.5-pro")
        
        # Wire in QuotaClient and RotationEngine
        try:
            
            active_name = engine.get_active_name()
            accounts_data = engine.list_accounts().get("accounts", [])
            
            client = QuotaClient(timeout=2.0)
            raw_quotas = fetch_all_accounts_quota(engine, accounts_data, client=client)
            quotas = {k: v for k, v in raw_quotas.items() if "error" not in v and v}
                            
            rot_engine = RotationEngine(model_name=model_name, active_account_id=active_name)
            target_account = rot_engine.evaluate(quotas, current_active=active_name, target_model=model_name)
            
            print(f"[agy-switch hook] Evaluated rotation for {model_name}: {active_name} -> {target_account or active_name}", file=sys.stderr)
            
            # Check if all network quotas failed or all accounts are exhausted
            all_exhausted = (
                not quotas
                or not any(rot_engine.has_available_quota(q) for q in quotas.values())
            )
            
            if all_exhausted:
                sys.stdout.write(json.dumps({
                    "injectSteps": [
                        {
                            "ephemeralMessage": f"⚠️ All Antigravity accounts have exhausted their {model_name} quota."
                        }
                    ]
                }) + "\n")
                sys.stdout.flush()
                return 0
                
            if target_account and target_account != active_name:
                engine.switch_tokens(target_account, restart_server=False, force=True)
                
                status = rot_engine.get_quota_status(quotas.get(target_account, {}))
                pct = status.remaining_percentage() if status is not None else "Unknown"
                sys.stdout.write(json.dumps({
                    "injectSteps": [
                        {
                            "ephemeralMessage": f"🔄 Auto-rotated to **{target_account}** for {model_name} ({pct}% remaining)."
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
    subparsers.add_parser("watch", help="Run background daemon to tail logs and auto-switch on quota exhaustion")

    # Command: internal-probe
    subparsers.add_parser("internal-probe", help=argparse.SUPPRESS)

    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    if engine is None:
        engine = AgySwitchEngine()

    # Perform auto-migration if needed on startup
    migrated = engine.auto_migrate_if_needed()
    if migrated and args.command != "migrate":
        if args.command == "hook-pre-invocation":
            print(f"[agy-switch] Auto-migrated {len(migrated)} account(s) from agy-cli-manager: {', '.join(migrated)}", file=sys.stderr)
        else:
            print(f"[agy-switch] Auto-migrated {len(migrated)} account(s) from agy-cli-manager: {', '.join(migrated)}")

    if args.command == "list" or not args.command:
        data = engine.list_accounts()
        if getattr(args, "quota", False):
            client = QuotaClient(timeout=2.0)
            quotas = fetch_all_accounts_quota(engine, data.get("accounts", []), client=client)
            for acc in data.get("accounts", []):
                acc["quota"] = quotas.get(acc["name"], {})
                
        if getattr(args, "json", False):
            print(json.dumps(data, indent=2))
        else:
            print(format_accounts_table(data, show_quota=getattr(args, "quota", False)))
        return 0

    if args.command == "status":
        status_info = engine.get_status()
        
        client = QuotaClient(timeout=2.0)
        cache_data = client.load_cache()
        
        now = datetime.now(timezone.utc).timestamp()
        cache_age = now - cache_data.get("timestamp", 0)
        
        if cache_age > 60:
            script_abs_path = str(Path(__file__).resolve())
            import subprocess
            subprocess.Popen([sys.executable, script_abs_path, "internal-probe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
        if "accounts" in cache_data and status_info.get("active"):
            active_name = status_info["active"]
            q_data = cache_data["accounts"].get(active_name, {})
            status_info["cached_quota"] = q_data
            
        print(format_status_report(status_info))
        return 0

    if args.command == "internal-probe":
        accounts_data = engine.list_accounts().get("accounts", [])
        client = QuotaClient(timeout=2.0)
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
            
            client = QuotaClient(timeout=2.0)
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
            watcher = watch.LogWatcher(engine)
            watcher.watch_loop()
            return 0
        except KeyboardInterrupt:
            return 0

    if args.command == "hook-pre-invocation":
        return handle_pre_invocation_hook(engine)

    return 0


if __name__ == "__main__":
    sys.exit(main())

