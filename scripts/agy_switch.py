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
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.vault_dir, 0o700)
        except OSError:
            pass
        self.accounts_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

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

        # 1. Update Antigravity 2.0 (WSL / Desktop) token
        jetski_token_path = self.gemini_dir / "jetski-standalone-oauth-token"
        self._atomic_write_json(jetski_token_path, token_data)

        # 2. Update Antigravity CLI token
        cli_token_path = self.gemini_dir / "antigravity-cli" / "antigravity-oauth-token"
        self._atomic_write_json(cli_token_path, token_data)

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

    def load_quota_cache(self) -> dict[str, Any]:
        """Loads cached quota data from vault cache or legacy quota checker fallback."""
        if hasattr(self, "cache_file") and self.cache_file.is_file():
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

    def save_quota_cache(self, data: dict[str, Any]) -> None:
        """Atomically saves quota cache to vault cache directory."""
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            tmp_fd, tmp_file = tempfile.mkstemp(dir=self.cache_dir, prefix="quota-cache-", suffix=".tmp")
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_file, self.cache_file)
        except Exception:
            pass

    def probe_account_quota(
        self,
        name: str,
        token_path: Path,
        agy_binary: str,
        timeout: int = 45,
    ) -> dict[str, Any]:
        """Probes real-time quota of a single account in an isolated temporary sandbox."""
        result: dict[str, Any] = {
            "account": name,
            "gemini_5h": None,
            "gemini_5h_reset": None,
            "gemini_weekly": None,
            "gemini_weekly_reset": None,
            "claude_5h": None,
            "claude_5h_reset": None,
            "claude_weekly": None,
            "claude_weekly_reset": None,
            "raw_error": None,
            "is_fallback": False,
        }

        with tempfile.TemporaryDirectory(prefix=f"agy-quota-{name}-") as tmpdir:
            tmp_home = Path(tmpdir)
            cli_dir = tmp_home / ".gemini" / "antigravity-cli"
            cli_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(token_path, cli_dir / "antigravity-oauth-token")
            shutil.copy2(token_path, tmp_home / ".gemini" / "jetski-standalone-oauth-token")
            try:
                (cli_dir / "antigravity-oauth-token").chmod(0o600)
                (tmp_home / ".gemini" / "jetski-standalone-oauth-token").chmod(0o600)
            except OSError:
                pass

            env = os.environ.copy()
            env["HOME"] = str(tmp_home)
            env["PATH"] = f"{Path.home()}/.local/bin:{env.get('PATH', '/bin:/usr/bin:/usr/local/bin')}"

            try:
                proc = subprocess.run(
                    [agy_binary, "-p", "/usage"],
                    cwd=str(tmp_home),
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
                output = "\n".join(part for part in (proc.stdout, proc.stderr) if part).strip()

                for match in QUOTA_REGEX.finditer(output):
                    model_type = match.group(1).strip()
                    limit_type = match.group(2).strip()
                    pct = int(match.group(3))
                    reset_time = (match.group(4) or "").strip()

                    if model_type == "Gemini Models":
                        if limit_type == "Five Hour Limit Remaining":
                            result["gemini_5h"] = pct
                            result["gemini_5h_reset"] = reset_time
                        elif limit_type == "Weekly Limit Remaining":
                            result["gemini_weekly"] = pct
                            result["gemini_weekly_reset"] = reset_time
                    elif model_type == "Claude and GPT models":
                        if limit_type == "Five Hour Limit Remaining":
                            result["claude_5h"] = pct
                            result["claude_5h_reset"] = reset_time
                        elif limit_type == "Weekly Limit Remaining":
                            result["claude_weekly"] = pct
                            result["claude_weekly_reset"] = reset_time

                if proc.returncode != 0 and all(
                    result[k] is None for k in ["gemini_5h", "gemini_weekly", "claude_5h", "claude_weekly"]
                ):
                    result["raw_error"] = f"Exit code {proc.returncode}: {output[:200]}"
            except subprocess.TimeoutExpired:
                result["raw_error"] = f"Timed out after {timeout}s"
            except Exception as e:
                result["raw_error"] = str(e)

        return result

    def get_quota_overview(
        self,
        agy_binary: str | None = None,
        timeout: int = 45,
    ) -> dict[str, Any]:
        """Probes real-time quotas across all vault accounts and returns sorted rankings and recommendations."""
        resolved_bin: str | None = None
        binary_error: str | None = None
        try:
            resolved_bin = self.resolve_agy_binary(agy_binary)
        except AgySwitchError as e:
            binary_error = str(e)

        active_account = self.get_active_name()
        listed = self.list_accounts().get("accounts", [])
        cache = self.load_quota_cache()

        results: list[dict[str, Any]] = []

        for item in listed:
            name = item["name"]
            token_path = self.accounts_dir / name / "token.json"
            if not token_path.is_file():
                continue

            if resolved_bin:
                probe_data = self.probe_account_quota(name, token_path, resolved_bin, timeout=timeout)
            else:
                probe_data = {
                    "account": name,
                    "gemini_5h": None,
                    "gemini_5h_reset": None,
                    "gemini_weekly": None,
                    "gemini_weekly_reset": None,
                    "claude_5h": None,
                    "claude_5h_reset": None,
                    "claude_weekly": None,
                    "claude_weekly_reset": None,
                    "raw_error": binary_error,
                    "is_fallback": False,
                }

            # Update cache if probe succeeded
            if probe_data["gemini_5h"] is not None and probe_data["gemini_weekly"] is not None:
                cache[name] = {
                    "gemini_5h": probe_data["gemini_5h"],
                    "gemini_5h_reset": probe_data["gemini_5h_reset"],
                    "gemini_weekly": probe_data["gemini_weekly"],
                    "gemini_weekly_reset": probe_data["gemini_weekly_reset"],
                    "claude_5h": probe_data["claude_5h"],
                    "claude_5h_reset": probe_data["claude_5h_reset"],
                    "claude_weekly": probe_data["claude_weekly"],
                    "claude_weekly_reset": probe_data["claude_weekly_reset"],
                }
            elif name in cache:
                cached = cache[name]
                probe_data["gemini_5h"] = cached.get("gemini_5h")
                probe_data["gemini_5h_reset"] = cached.get("gemini_5h_reset")
                probe_data["gemini_weekly"] = cached.get("gemini_weekly")
                probe_data["gemini_weekly_reset"] = cached.get("gemini_weekly_reset")
                probe_data["claude_5h"] = cached.get("claude_5h")
                probe_data["claude_5h_reset"] = cached.get("claude_5h_reset")
                probe_data["claude_weekly"] = cached.get("claude_weekly")
                probe_data["claude_weekly_reset"] = cached.get("claude_weekly_reset")
                probe_data["is_fallback"] = True

            probe_data["is_active"] = (name == active_account)
            probe_data["status"] = "Active" if probe_data["is_active"] else "Standby"
            probe_data["email"] = item.get("email") or "-"
            results.append(probe_data)

        self.save_quota_cache(cache)

        # Sort accounts: highest Gemini weekly limit first, then highest Gemini 5-hour limit
        results.sort(
            key=lambda r: (
                r["gemini_weekly"] if r["gemini_weekly"] is not None else -1,
                r["gemini_5h"] if r["gemini_5h"] is not None else -1,
            ),
            reverse=True,
        )

        recommendation: str | None = None
        if not results:
            recommendation = "No accounts found in vault."
        else:
            top = results[0]
            top_weekly = top.get("gemini_weekly")
            if top_weekly is None or top_weekly == 0:
                recommendation = "All accounts have exhausted or unavailable quotas."
            elif not top["is_active"]:
                recommendation = f"Switch to '{top['account']}' ({top['email']}) with {top_weekly}% Gemini Weekly quota remaining."
            else:
                recommendation = f"Current active account '{top['account']}' has the highest remaining quota ({top_weekly}% Gemini Weekly)."

        return {
            "active": active_account,
            "accounts": results,
            "recommendation": recommendation,
        }


def format_countdown(dt: datetime, now: datetime | None = None) -> str:
    """Formats time difference into human-readable countdown in Indonesian (e.g. ~3 jam 20 menit lagi)."""
    if now is None:
        now = datetime.now().astimezone()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    diff = dt - now
    sec = int(diff.total_seconds())
    if sec <= 0:
        return "baru reset"
    if diff.days > 0:
        hours = diff.seconds // 3600
        return f"~{diff.days} hari {hours} jam lagi"
    if diff.seconds >= 3600:
        hours = diff.seconds // 3600
        mins = (diff.seconds % 3600) // 60
        return f"~{hours} jam {mins} menit lagi"
    mins = diff.seconds // 60
    return f"~{mins} menit lagi"


def fmt_quota_detail(
    pct: int | None,
    reset_str: str | None,
    is_weekly: bool = False,
    now: datetime | None = None,
) -> str:
    """Formats quota percentage with warning emoji and localized countdown."""
    if pct is None:
        return "N/A"
    icon = ""
    if pct == 0:
        icon = " 🔴"
    elif pct <= 20:
        icon = " ⚠️"
    if not reset_str:
        return f"**{pct}%**{icon}"

    try:
        dt = datetime.fromisoformat(reset_str.replace("Z", "+00:00")).astimezone()
        if now is None:
            now = datetime.now().astimezone()
        cd = format_countdown(dt, now)
        tz_name = dt.strftime("%Z") or "WIB"
        time_str = dt.strftime(f"%H:%M {tz_name}") if not is_weekly else dt.strftime(f"%d %b %H:%M {tz_name}")
        return f"**{pct}%**{icon} *(Reset: {time_str}, {cd})*"
    except Exception:
        return f"**{pct}%**{icon}"


def format_quota_table(data: dict[str, Any]) -> str:
    """Formats quota overview into a compact, human-readable terminal table."""
    accounts = data.get("accounts", [])
    if not accounts:
        return "No accounts registered in vault (~/.antigravity-vault/accounts)."

    active_account = data.get("active")
    recommendation = data.get("recommendation")

    max_acc_len = max(max((len(a["account"]) for a in accounts), default=7), 7)
    max_email_len = max(max((len(str(a.get("email") or "")) for a in accounts), default=5), 18)

    email_col_width = min(max_email_len, 32)
    acc_col_width = min(max_acc_len, 16)

    header = (
        f"   {'ACCOUNT':<{acc_col_width}} "
        f"{'EMAIL':<{email_col_width}} "
        f"{'STATUS':<7} "
        f"{'GEMINI 5H':<10} "
        f"{'GEMINI WK':<10} "
        f"{'CLAUDE 5H':<10} "
        f"{'CLAUDE WK':<10}"
    )
    separator = "-" * len(header)
    lines = [
        "Antigravity Quota Overview (~/.antigravity-vault):",
        separator,
        header,
        separator,
    ]

    has_fallback = False
    for a in accounts:
        marker = "* " if a.get("is_active") else "  "
        acc_name = a["account"]
        if len(acc_name) > acc_col_width:
            acc_name = acc_name[: acc_col_width - 2] + ".."
        email = str(a.get("email") or "-")
        if len(email) > email_col_width:
            email = email[: email_col_width - 3] + "..."

        status = a.get("status") or ("Active" if a.get("is_active") else "Standby")

        is_fb = a.get("is_fallback", False)
        if is_fb:
            has_fallback = True

        def _fmt_pct(val: int | None) -> str:
            if val is None:
                return "N/A"
            suffix = "*" if is_fb else ""
            return f"{val}%{suffix}"

        g5 = _fmt_pct(a.get("gemini_5h"))
        gw = _fmt_pct(a.get("gemini_weekly"))
        c5 = _fmt_pct(a.get("claude_5h"))
        cw = _fmt_pct(a.get("claude_weekly"))

        lines.append(
            f"{marker} {acc_name:<{acc_col_width}} "
            f"{email:<{email_col_width}} "
            f"{status:<7} "
            f"{g5:<10} "
            f"{gw:<10} "
            f"{c5:<10} "
            f"{cw:<10}"
        )

    lines.append(separator)
    if active_account:
        lines.append(f"Active Account : {active_account}")
    if recommendation:
        lines.append(f"Recommendation : {recommendation}")
    if has_fallback:
        lines.append("* Note: Asterisk (*) indicates cached fallback metrics.")
    return "\n".join(lines)


def format_quota_listicle(data: dict[str, Any]) -> str:
    """Formats quota overview into a clean numbered listicle for mobile/chat."""
    accounts = data.get("accounts", [])
    if not accounts:
        return "Tidak ada akun terdaftar di vault Antigravity."

    now = datetime.now().astimezone()
    lines = ["Berikut status sisa kuota akun Antigravity saat ini:\n"]

    for idx, r in enumerate(accounts, 1):
        status_tag = "*(Aktif)*" if r.get("is_active") else "*(Standby)*"
        if r.get("is_fallback"):
            status_tag += " *(Cache)*"
        lines.append(f"{idx}. **`{r['account']}`** — {status_tag}")
        lines.append(f"   * **Email:** `{r.get('email') or '-'}`")
        lines.append(f"   * **Status:** {'Active' if r.get('is_active') else 'Standby'}")
        lines.append("   * **Gemini Models:**")
        lines.append(f"     * **Limit Mingguan:** {fmt_quota_detail(r.get('gemini_weekly'), r.get('gemini_weekly_reset'), is_weekly=True, now=now)}")
        lines.append(f"     * **Limit 5-Jam:** {fmt_quota_detail(r.get('gemini_5h'), r.get('gemini_5h_reset'), is_weekly=False, now=now)}")
        lines.append("   * **Claude & GPT Models:**")
        lines.append(f"     * **Limit Mingguan:** {fmt_quota_detail(r.get('claude_weekly'), r.get('claude_weekly_reset'), is_weekly=True, now=now)}")
        lines.append(f"     * **Limit 5-Jam:** {fmt_quota_detail(r.get('claude_5h'), r.get('claude_5h_reset'), is_weekly=False, now=now)}")
        lines.append("")

    rec = data.get("recommendation")
    if rec:
        lines.append(f"💡 **Rekomendasi:** {rec}")

    return "\n".join(lines)


def format_accounts_table(result: dict[str, Any]) -> str:
    """Formats accounts list as a clean, human-readable terminal table."""
    accounts = result.get("accounts", [])
    if not accounts:
        return "No accounts registered in vault (~/.antigravity-vault/accounts)."

    lines = [
        "Antigravity Accounts (~/.antigravity-vault):",
        "-------------------------------------------------------------",
        f"{'':2} {'ACCOUNT':<20} {'EMAIL':<30} {'STATUS'}",
        "-------------------------------------------------------------",
    ]

    for acc in accounts:
        marker = "* " if acc["is_active"] else "  "
        status = "Active" if acc["is_active"] else "Standby"
        if not acc["valid"]:
            status += " (Invalid Token)"
        email_str = acc["email"] or "-"
        lines.append(f"{marker}{acc['name']:<20} {email_str:<30} {status}")

    lines.append("-------------------------------------------------------------")
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


def main(argv: list[str] | None = None, engine: AgySwitchEngine | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agy-switch",
        description="High-reliability account switcher for Antigravity 2.0 & CLI",
    )
    subparsers = parser.add_subparsers(dest="command")

    # Command: list
    list_parser = subparsers.add_parser("list", help="List registered accounts and show active account")
    list_parser.add_argument("--quota", action="store_true", help="Include real-time quota overview")
    list_parser.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    list_parser.add_argument("--format", choices=["table", "list"], default="table", help="Output format: table (default) or list (listicle)")
    list_parser.add_argument("--timeout", type=int, default=45, help="Probe timeout in seconds per account (default: 45)")

    # Command: quota
    quota_parser = subparsers.add_parser("quota", help="Inspect real-time quota usage across all accounts")
    quota_parser.add_argument("--agy-binary", help="Path to agy binary (default: auto-detected)")
    quota_parser.add_argument("--timeout", type=int, default=45, help="Probe timeout in seconds per account (default: 45)")
    quota_parser.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    quota_parser.add_argument("--format", choices=["table", "list"], default="table", help="Output format: table (default) or list (listicle)")

    # Command: status
    subparsers.add_parser("status", help="Show system status, active account, and server health")

    # Command: switch
    switch_parser = subparsers.add_parser("switch", help="Switch active account across Antigravity 2.0 and CLI")
    switch_parser.add_argument("account", help="Name of the account to switch to")
    switch_parser.add_argument("--no-restart", action="store_true", help="Do not restart language_server process")
    switch_parser.add_argument("--force", action="store_true", help="Force switch even if active turns are detected")

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

    if args.command == "quota" or (args.command == "list" and getattr(args, "quota", False)):
        agy_bin = getattr(args, "agy_binary", None)
        timeout = getattr(args, "timeout", 45)
        overview = engine.get_quota_overview(agy_binary=agy_bin, timeout=timeout)
        if getattr(args, "json", False):
            print(json.dumps(overview, indent=2))
        elif getattr(args, "format", "table") == "list":
            print(format_quota_listicle(overview))
        else:
            print(format_quota_table(overview))
        return 0

    if args.command == "list" or not args.command:
        data = engine.list_accounts()
        if getattr(args, "json", False):
            print(json.dumps(data, indent=2))
        else:
            print(format_accounts_table(data))
        return 0

    if args.command == "status":
        status_info = engine.get_status()
        print(format_status_report(status_info))
        return 0

    if args.command == "switch":
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

    if args.command == "hook-pre-invocation":
        try:
            raw = sys.stdin.read()
            _payload = json.loads(raw) if raw.strip() else {}
            
            # Wire in QuotaClient
            try:
                from quota_client import QuotaClient
                active_name = engine.get_active_name()
                if active_name:
                    token_path = engine.accounts_dir / active_name / "token.json"
                    if token_path.is_file():
                        token_data = engine.validate_token_file(token_path)
                        access_token = token_data.get("token", {}).get("access_token")
                        refresh_token = token_data.get("token", {}).get("refresh_token")
                        if access_token:
                            client = QuotaClient(timeout=2.0)
                            quota_res = client.fetch_quota(access_token, refresh_token=refresh_token)
                            print(f"[agy-switch hook] Quota for {active_name}: {json.dumps(quota_res)}", file=sys.stderr)
            except Exception as qc_exc:
                print(f"[agy-switch hook] QuotaClient error: {qc_exc}", file=sys.stderr)
        except Exception as exc:
            print(f"[agy-switch hook] Error parsing stdin: {exc}", file=sys.stderr)
        sys.stdout.write(json.dumps({}) + "\n")
        sys.stdout.flush()
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())

