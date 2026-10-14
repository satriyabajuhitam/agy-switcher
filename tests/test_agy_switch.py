#!/usr/bin/env python3
"""Tests for Ticket 01: Vault Foundation & Auto-Migration (agy-switch)."""

import json
import os
import stat
import subprocess
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path
import sys

# Add scripts directory to sys.path so agy_switch can be imported
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from agy_switch import (
    AgySwitchEngine,
    AgySwitchError,
    InvalidTokenError,
    AccountNotFoundError,
    ActiveTurnRunningError,
    format_countdown,
    sort_accounts_by_quota,
    format_accounts_listicle,
    format_accounts_table,
    _parse_timestamp_defensive,
    fetch_account_quota,
    QuotaClient,
    main,
)


import base64


def create_sample_token(email: str = "test@example.com") -> dict:
    payload = json.dumps({"email": email, "sub": "12345", "aud": "test_aud"}).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(payload).decode("utf-8").rstrip("=")
    return {
        "token": {
            "access_token": f"ya29.sample_access_token_for_{email}",
            "token_type": "Bearer",
            "refresh_token": f"1//sample_refresh_token_for_{email}",
            "expiry": "2026-09-30T12:00:00Z",
        },
        "auth_method": "oauth",
        "id_token": f"header.{payload_b64}.signature",
    }


class TestVaultFoundationAndMigration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)
        self.vault_dir = self.base_path / ".antigravity-vault"
        self.gemini_dir = self.base_path / ".gemini"
        self.legacy_root = self.base_path / ".agy-cli-manager"
        self._orig_popen = subprocess.Popen

    def tearDown(self):
        subprocess.Popen = self._orig_popen
        self.temp_dir.cleanup()

    def test_vault_directories_created(self):
        """Engine should automatically create ~/.antigravity-vault/accounts and cache if missing with 0o700."""
        self.assertFalse(self.vault_dir.exists())
        engine = AgySwitchEngine(vault_dir=self.vault_dir, gemini_dir=self.gemini_dir)
        self.assertTrue(self.vault_dir.is_dir())
        self.assertTrue((self.vault_dir / "accounts").is_dir())
        self.assertTrue((self.vault_dir / "cache").is_dir())
        
        # Verify permissions
        self.assertEqual(stat.S_IMODE(self.vault_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.vault_dir / "accounts").stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.vault_dir / "cache").stat().st_mode), 0o700)

    def test_vault_directories_mkdir_passes_mode_700(self):
        """init_vault() must pass mode=0o700 directly to mkdir to close TOCTOU window."""
        mkdir_calls = []
        orig_mkdir = Path.mkdir

        def tracking_mkdir(path_self, *args, **kwargs):
            mkdir_calls.append((path_self, kwargs.get("mode")))
            return orig_mkdir(path_self, *args, **kwargs)

        with patch.object(Path, "mkdir", side_effect=tracking_mkdir, autospec=True):
            test_vdir = self.base_path / "test_vault_mode"
            engine = AgySwitchEngine(vault_dir=test_vdir, gemini_dir=self.gemini_dir)

        accounts_dir = test_vdir / "accounts"
        cache_dir = test_vdir / "cache"

        # Check that accounts_dir and cache_dir mkdir were called with mode=0o700
        accounts_called_mode = [mode for p, mode in mkdir_calls if p == accounts_dir]
        cache_called_mode = [mode for p, mode in mkdir_calls if p == cache_dir]

        self.assertTrue(accounts_called_mode, "accounts_dir.mkdir was not called")
        self.assertEqual(accounts_called_mode[0], 0o700)
        self.assertTrue(cache_called_mode, "cache_dir.mkdir was not called")
        self.assertEqual(cache_called_mode[0], 0o700)

    def test_get_account_tokens(self):
        """get_account_tokens encapsulates token extraction from token.json."""
        engine = AgySwitchEngine(vault_dir=self.vault_dir, gemini_dir=self.gemini_dir)

        # 1. Non-existent account
        self.assertIsNone(engine.get_account_tokens("non_existent"))

        # 2. Valid account
        acc_dir = self.vault_dir / "accounts" / "valid_user"
        acc_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        sample = create_sample_token("valid_user@example.com")
        sample["token"]["access_token"] = "mock_access_123"
        sample["token"]["refresh_token"] = "mock_refresh_456"
        (acc_dir / "token.json").write_text(json.dumps(sample), encoding="utf-8")

        tokens = engine.get_account_tokens("valid_user")
        self.assertIsNotNone(tokens)
        self.assertEqual(tokens.get("access_token"), "mock_access_123")
        self.assertEqual(tokens.get("refresh_token"), "mock_refresh_456")

        # 3. Invalid token file
        invalid_dir = self.vault_dir / "accounts" / "invalid_user"
        invalid_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        (invalid_dir / "token.json").write_text("{bad json", encoding="utf-8")
        self.assertIsNone(engine.get_account_tokens("invalid_user"))

    def test_auto_migration_discovers_and_copies_accounts(self):
        """Engine should auto-migrate accounts from legacy agy-cli-manager accounts folder."""
        # Create legacy accounts
        accounts = ["credible", "quickstart", "honeymoney", "satriyabajuhitam"]
        for acc in accounts:
            token_dir = self.legacy_root / "accounts" / acc / ".gemini" / "antigravity-cli"
            token_dir.mkdir(parents=True, exist_ok=True)
            token_path = token_dir / "antigravity-oauth-token"
            token_path.write_text(json.dumps(create_sample_token(f"{acc}@gmail.com")), encoding="utf-8")

        engine = AgySwitchEngine(
            vault_dir=self.vault_dir,
            gemini_dir=self.gemini_dir,
            legacy_root=self.legacy_root,
        )
        migrated = engine.auto_migrate_if_needed()

        self.assertEqual(sorted(migrated), sorted(accounts))

        # Verify all accounts copied to vault with token.json
        for acc in accounts:
            target_token = self.vault_dir / "accounts" / acc / "token.json"
            self.assertTrue(target_token.is_file())
            # Verify file mode is 0600
            mode = stat.S_IMODE(target_token.stat().st_mode)
            self.assertEqual(mode, 0o600)
            data = json.loads(target_token.read_text(encoding="utf-8"))
            self.assertEqual(data["auth_method"], "oauth")
            self.assertIn("access_token", data["token"])

    def test_auto_migration_skips_when_vault_already_populated(self):
        """Auto-migration should not overwrite existing accounts if vault is not empty."""
        acc_dir = self.vault_dir / "accounts" / "existing_user"
        acc_dir.mkdir(parents=True, exist_ok=True)
        (acc_dir / "token.json").write_text(json.dumps(create_sample_token("existing@example.com")))

        # Create legacy accounts
        legacy_acc = self.legacy_root / "accounts" / "legacy_user" / ".gemini" / "antigravity-cli"
        legacy_acc.mkdir(parents=True, exist_ok=True)
        (legacy_acc / "antigravity-oauth-token").write_text(json.dumps(create_sample_token("legacy@example.com")))

        engine = AgySwitchEngine(
            vault_dir=self.vault_dir,
            gemini_dir=self.gemini_dir,
            legacy_root=self.legacy_root,
        )
        migrated = engine.auto_migrate_if_needed()
        self.assertEqual(migrated, [])
        self.assertFalse((self.vault_dir / "accounts" / "legacy_user").exists())

    def test_token_validation_accepts_valid_and_rejects_invalid(self):
        """Token validation must strictly verify required keys."""
        engine = AgySwitchEngine(vault_dir=self.vault_dir, gemini_dir=self.gemini_dir)

        # Valid token
        valid = create_sample_token("valid@example.com")
        validated = engine.validate_token_dict(valid)
        self.assertEqual(validated["auth_method"], "oauth")

        # Invalid: missing token object
        with self.assertRaises(InvalidTokenError):
            engine.validate_token_dict({"auth_method": "oauth"})

        # Invalid: missing access_token in token object
        with self.assertRaises(InvalidTokenError):
            engine.validate_token_dict({"token": {"refresh_token": "abc"}, "auth_method": "oauth"})

        # Invalid: missing id_token
        with self.assertRaises(InvalidTokenError):
            engine.validate_token_dict({
                "token": {
                    "access_token": "a",
                    "refresh_token": "b",
                    "token_type": "Bearer",
                    "expiry": "x",
                },
                "auth_method": "oauth",
            })

    def test_list_accounts_marks_active_account(self):
        """list_accounts() must return all accounts and mark the active account properly."""
        engine = AgySwitchEngine(vault_dir=self.vault_dir, gemini_dir=self.gemini_dir)
        for name in ["alpha", "beta", "gamma"]:
            acc_dir = self.vault_dir / "accounts" / name
            acc_dir.mkdir(parents=True, exist_ok=True)
            (acc_dir / "token.json").write_text(json.dumps(create_sample_token(f"{name}@test.com")))

        # No active marker yet
        res1 = engine.list_accounts()
        self.assertIsNone(res1["active"])
        self.assertEqual(len(res1["accounts"]), 3)
        for item in res1["accounts"]:
            self.assertFalse(item["is_active"])

        # Set active marker to beta
        engine.set_active_name("beta")
        res2 = engine.list_accounts()
        self.assertEqual(res2["active"], "beta")
        beta_info = next(a for a in res2["accounts"] if a["name"] == "beta")
        self.assertTrue(beta_info["is_active"])
        alpha_info = next(a for a in res2["accounts"] if a["name"] == "alpha")
        self.assertFalse(alpha_info["is_active"])


class TestAtomicDualSwapper(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)
        self.vault_dir = self.base_path / ".antigravity-vault"
        self.gemini_dir = self.base_path / ".gemini"
        self.legacy_root = self.base_path / ".agy-cli-manager"

        self.engine = AgySwitchEngine(
            vault_dir=self.vault_dir,
            gemini_dir=self.gemini_dir,
            legacy_root=self.legacy_root,
        )

        # Seed two accounts in vault
        for name in ["credible", "quickstart"]:
            acc_dir = self.vault_dir / "accounts" / name
            acc_dir.mkdir(parents=True, exist_ok=True)
            (acc_dir / "token.json").write_text(json.dumps(create_sample_token(f"{name}@example.com")))

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_switch_tokens_updates_both_targets_atomically(self):
        """Switching to an account must atomically update both jetski and CLI token files."""
        res = self.engine.switch_tokens("quickstart", restart_server=False)
        self.assertEqual(res["active"], "quickstart")
        self.assertEqual(self.engine.get_active_name(), "quickstart")

        # 1. Check Antigravity 2.0 token
        jetski_token = self.gemini_dir / "jetski-standalone-oauth-token"
        self.assertTrue(jetski_token.is_file())
        self.assertEqual(stat.S_IMODE(jetski_token.stat().st_mode), 0o600)
        jetski_data = json.loads(jetski_token.read_text(encoding="utf-8"))
        self.assertIn("quickstart", jetski_data["token"]["access_token"])

        # 2. Check Antigravity CLI token
        cli_token = self.gemini_dir / "antigravity-cli" / "antigravity-oauth-token"
        self.assertTrue(cli_token.is_file())
        self.assertEqual(stat.S_IMODE(cli_token.stat().st_mode), 0o600)
        cli_data = json.loads(cli_token.read_text(encoding="utf-8"))
        self.assertIn("quickstart", cli_data["token"]["access_token"])

    def test_switch_tokens_explicitly_calls_os_replace_for_both_targets(self):
        """switch_tokens must explicitly invoke os.replace for both target credential files."""
        with patch("os.replace", wraps=os.replace) as mock_replace:
            self.engine.switch_tokens("quickstart", restart_server=False)

            replaced_targets = [str(call.args[1]) for call in mock_replace.call_args_list if len(call.args) >= 2]
            jetski_target = str(self.gemini_dir / "jetski-standalone-oauth-token")
            cli_target = str(self.gemini_dir / "antigravity-cli" / "antigravity-oauth-token")

            self.assertIn(jetski_target, replaced_targets, "os.replace was not called for jetski token target")
            self.assertIn(cli_target, replaced_targets, "os.replace was not called for cli token target")

    def test_switch_tokens_nonexistent_account_raises_error(self):
        """Switching to non-existent account must raise AccountNotFoundError."""
        with self.assertRaises(AccountNotFoundError):
            self.engine.switch_tokens("does_not_exist", restart_server=False)

    def test_switch_tokens_corrupted_token_raises_invalid_token(self):
        """If account token is corrupted, error is raised and destinations remain untouched."""
        bad_acc = self.vault_dir / "accounts" / "corrupted"
        bad_acc.mkdir(parents=True, exist_ok=True)
        (bad_acc / "token.json").write_text("NOT_VALID_JSON")

        with self.assertRaises(InvalidTokenError):
            self.engine.switch_tokens("corrupted", restart_server=False)

        jetski_token = self.gemini_dir / "jetski-standalone-oauth-token"
        self.assertFalse(jetski_token.exists())

    def test_switch_updates_legacy_state_json_when_present(self):
        """If ~/.agy-cli-manager/state.json exists, its active field must be synced."""
        self.legacy_root.mkdir(parents=True, exist_ok=True)
        legacy_state_file = self.legacy_root / "state.json"
        legacy_state_file.write_text(json.dumps({
            "active": "credible",
            "accounts": {"credible": {}, "quickstart": {}},
        }))

        self.engine.switch_tokens("quickstart", restart_server=False)

        # Verify legacy state is synced
        updated_state = json.loads(legacy_state_file.read_text(encoding="utf-8"))
        self.assertEqual(updated_state["active"], "quickstart")


class TestProcessManagementAndSafety(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)
        self.vault_dir = self.base_path / ".antigravity-vault"
        self.gemini_dir = self.base_path / ".gemini"

        self.engine = AgySwitchEngine(
            vault_dir=self.vault_dir,
            gemini_dir=self.gemini_dir,
        )

        # Seed account in vault
        acc_dir = self.vault_dir / "accounts" / "quickstart"
        acc_dir.mkdir(parents=True, exist_ok=True)
        (acc_dir / "token.json").write_text(json.dumps(create_sample_token("quickstart@example.com")))

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_is_server_busy_detects_presence_locks(self):
        """Presence lock files in gemini directory indicate an active agent session."""
        lock_dir = self.gemini_dir / "antigravity" / "presence"
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_file = lock_dir / "active_turn.lock"
        lock_file.write_text("lock")

        self.assertTrue(self.engine.is_server_busy(pid=12345))

    def test_switch_blocked_when_busy_without_force(self):
        """When active tasks exist and force=False, switch_tokens must raise ActiveTurnRunningError."""
        self.engine.locate_language_server_pid = lambda: 99999  # Mock running PID
        self.engine.is_server_busy = lambda pid: True          # Mock busy

        with self.assertRaises(ActiveTurnRunningError):
            self.engine.switch_tokens("quickstart", restart_server=True, force=False)

    def test_switch_succeeds_when_busy_with_force(self):
        """When force=True, switch_tokens overrides busy detection and restarts server."""
        self.engine.locate_language_server_pid = lambda: 99999
        self.engine.is_server_busy = lambda pid: True
        killed = []
        self.engine._send_signal = lambda pid, sig: killed.append((pid, sig))
        self.engine._poll_process_exit = lambda pid, timeout: True

        res = self.engine.switch_tokens("quickstart", restart_server=True, force=True)
        self.assertEqual(res["active"], "quickstart")
        self.assertTrue(res["server_restarted"])
        self.assertEqual(len(killed), 1)
        self.assertEqual(killed[0][0], 99999)

    def test_get_status_returns_complete_report(self):
        """get_status() must return active account, server running status, PID, and busy state."""
        self.engine.set_active_name("quickstart")
        self.engine.locate_language_server_pid = lambda: 12345
        self.engine.is_server_busy = lambda pid: False

        status = self.engine.get_status()
        self.assertEqual(status["active"], "quickstart")
        self.assertTrue(status["server_running"])
        self.assertEqual(status["server_pid"], 12345)
        self.assertFalse(status["server_busy"])


class TestAccountManagementCLI(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)
        self.vault_dir = self.base_path / ".antigravity-vault"
        self.gemini_dir = self.base_path / ".gemini"
        self.legacy_root = self.base_path / ".agy-cli-manager"

        self.engine = AgySwitchEngine(
            vault_dir=self.vault_dir,
            gemini_dir=self.gemini_dir,
            legacy_root=self.legacy_root,
        )

        acc_dir = self.vault_dir / "accounts" / "active_acc"
        acc_dir.mkdir(parents=True, exist_ok=True)
        (acc_dir / "token.json").write_text(json.dumps(create_sample_token("active@example.com")))
        self.engine.set_active_name("active_acc")

        acc_dir2 = self.vault_dir / "accounts" / "standby_acc"
        acc_dir2.mkdir(parents=True, exist_ok=True)
        (acc_dir2 / "token.json").write_text(json.dumps(create_sample_token("standby@example.com")))

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_add_account_valid_token(self):
        """add_account() stores new valid account with 0600 mode."""
        token_file = self.base_path / "new_token.json"
        token_file.write_text(json.dumps(create_sample_token("new@example.com")))

        self.engine.add_account("newbie", token_file)
        target = self.vault_dir / "accounts" / "newbie" / "token.json"
        self.assertTrue(target.is_file())
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_add_account_invalid_token_raises_error(self):
        """add_account() raises InvalidTokenError on corrupt payload."""
        token_file = self.base_path / "bad_token.json"
        token_file.write_text("invalid json")

        with self.assertRaises(InvalidTokenError):
            self.engine.add_account("bad", token_file)

    def test_remove_standby_account_succeeds(self):
        """remove_account() removes non-active account folder."""
        self.engine.remove_account("standby_acc")
        self.assertFalse((self.vault_dir / "accounts" / "standby_acc").exists())

    def test_remove_active_account_fails_without_force(self):
        """remove_account() refuses to delete active account without force."""
        with self.assertRaises(AgySwitchError):
            self.engine.remove_account("active_acc", force=False)

    def test_remove_active_account_succeeds_with_force(self):
        """remove_account() deletes active account with force and clears active file."""
        self.engine.remove_account("active_acc", force=True)
        self.assertFalse((self.vault_dir / "accounts" / "active_acc").exists())
        self.assertIsNone(self.engine.get_active_name())

    def test_cli_help(self):
        """Executing --help exits with code 0."""
        with self.assertRaises(SystemExit) as cm:
            main(["--help"])
        self.assertEqual(cm.exception.code, 0)

    def test_cli_list_and_status(self):
        """Executing list and status subcommands return exit code 0."""
        self.assertEqual(main(["list"], engine=self.engine), 0)
        self.assertEqual(main(["status"], engine=self.engine), 0)

    def test_cli_add_and_remove(self):
        """Executing add and remove via CLI parses arguments and executes correctly."""
        token_file = self.base_path / "cli_token.json"
        token_file.write_text(json.dumps(create_sample_token("cli@example.com")))

        # Add valid account
        ret = main(["add", "cliusr", "--token-file", str(token_file)], engine=self.engine)
        self.assertEqual(ret, 0)
        self.assertTrue((self.vault_dir / "accounts" / "cliusr").is_dir())

        # Remove active account without force -> 1
        ret_err = main(["remove", "active_acc"], engine=self.engine)
        self.assertEqual(ret_err, 1)

        # Remove active account with force -> 0
        ret_ok = main(["remove", "active_acc", "--force"], engine=self.engine)
        self.assertEqual(ret_ok, 0)

    def test_cli_migrate(self):
        """Executing migrate via CLI runs migration and returns 0."""
        ret = main(["migrate"], engine=self.engine)
        self.assertEqual(ret, 0)


class TestInteractiveLogin(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)
        self.vault_dir = self.base_path / ".antigravity-vault"
        self.gemini_dir = self.base_path / ".gemini"
        self._orig_popen = subprocess.Popen
        self.engine = AgySwitchEngine(
            vault_dir=self.vault_dir,
            gemini_dir=self.gemini_dir,
        )

    def tearDown(self):
        subprocess.Popen = self._orig_popen
        self.temp_dir.cleanup()

    def _mock_popen_with_token(self, token_payload: dict):
        class MockProc:
            def __init__(self, temp_home: Path):
                self.temp_home = temp_home

            def poll(self):
                token_target = self.temp_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
                token_target.parent.mkdir(parents=True, exist_ok=True)
                token_target.write_text(json.dumps(token_payload), encoding="utf-8")
                return 0

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        return lambda args, **kwargs: MockProc(Path(kwargs["env"]["HOME"]))

    def test_login_account_success(self):
        """login_account() isolates execution, captures token, and stores it in Standby."""
        import subprocess

        self.engine.resolve_agy_binary = lambda override=None: "/bin/mock_agy"
        sample_token = create_sample_token("logged_in@example.com")
        subprocess.Popen = self._mock_popen_with_token(sample_token)

        res = self.engine.login_account("newbie")
        self.assertEqual(res["name"], "newbie")
        self.assertEqual(res["email"], "logged_in@example.com")
        self.assertEqual(res["status"], "standby")

        # Verify token saved with 0600
        saved_token = self.vault_dir / "accounts" / "newbie" / "token.json"
        self.assertTrue(saved_token.is_file())
        self.assertEqual(stat.S_IMODE(saved_token.stat().st_mode), 0o600)

        # Active account must NOT be changed
        self.assertIsNone(self.engine.get_active_name())

    def test_login_account_collision_prompt_aborted(self):
        """login_account() aborts if account already exists and user declines overwrite."""
        # Pre-seed account
        acc_dir = self.vault_dir / "accounts" / "existing"
        acc_dir.mkdir(parents=True, exist_ok=True)
        (acc_dir / "token.json").write_text(json.dumps(create_sample_token("original@example.com")))

        with self.assertRaises(AgySwitchError) as ctx:
            self.engine.login_account("existing", prompt_fn=lambda p: "n")
        self.assertIn("Overwrite aborted", str(ctx.exception))

    def test_login_account_collision_force_overwrite(self):
        """login_account() with force=True overwrites existing account without prompt."""
        import subprocess

        acc_dir = self.vault_dir / "accounts" / "existing"
        acc_dir.mkdir(parents=True, exist_ok=True)
        (acc_dir / "token.json").write_text(json.dumps(create_sample_token("old@example.com")))

        self.engine.resolve_agy_binary = lambda override=None: "/bin/mock_agy"
        new_token = create_sample_token("new@example.com")
        subprocess.Popen = self._mock_popen_with_token(new_token)

        res = self.engine.login_account("existing", force=True)
        self.assertEqual(res["email"], "new@example.com")

    def test_login_account_missing_token_raises_error(self):
        """login_account() raises AgySwitchError if agy terminates without creating token."""
        import subprocess

        class MockEmptyProc:
            def poll(self):
                return 0

        self.engine.resolve_agy_binary = lambda override=None: "/bin/mock_agy"
        subprocess.Popen = lambda args, **kwargs: MockEmptyProc()

        with self.assertRaises(AgySwitchError) as ctx:
            self.engine.login_account("failed_login")
        self.assertIn("did not produce a usable auth token", str(ctx.exception))

    def test_cli_login_dispatch(self):
        """main() handles 'login' command cleanly."""
        self.engine.login_account = lambda name, **kwargs: {
            "name": name,
            "email": "cli@example.com",
            "status": "standby",
        }
        ret = main(["login", "clilogin", "--force"], engine=self.engine)
        self.assertEqual(ret, 0)


class TestFormatCountdown(unittest.TestCase):
    """Unit tests for format_countdown relative countdown engine."""

    def test_none_zero_negative_timestamps(self):
        """None, 0, or negative reset timestamps return '-'."""
        self.assertEqual(format_countdown(None), "-")
        self.assertEqual(format_countdown(0), "-")
        self.assertEqual(format_countdown(-1), "-")
        self.assertEqual(format_countdown(-1000), "-")

    def test_non_numeric_timestamp_defensive(self):
        """Non-numeric input returns '-' defensively."""
        self.assertEqual(format_countdown("invalid"), "-")  # type: ignore
        self.assertEqual(format_countdown([]), "-")  # type: ignore

    def test_expired_countdown(self):
        """delta <= 0 returns 'expired'."""
        curr = 1000.0
        self.assertEqual(format_countdown(1000, current_timestamp=curr), "expired")
        self.assertEqual(format_countdown(999, current_timestamp=curr), "expired")
        self.assertEqual(format_countdown(500, current_timestamp=curr), "expired")

    def test_minutes_formatting(self):
        """delta < 3600 returns f'{max(1, delta // 60)}m'."""
        curr = 1000.0
        # 10s -> max(1, 0) = 1m
        self.assertEqual(format_countdown(1010, current_timestamp=curr), "1m")
        # 59s -> 1m
        self.assertEqual(format_countdown(1059, current_timestamp=curr), "1m")
        # 60s -> 1m
        self.assertEqual(format_countdown(1060, current_timestamp=curr), "1m")
        # 119s -> 1m
        self.assertEqual(format_countdown(1119, current_timestamp=curr), "1m")
        # 120s -> 2m
        self.assertEqual(format_countdown(1120, current_timestamp=curr), "2m")
        # 45 minutes = 2700s
        self.assertEqual(format_countdown(1000 + 2700, current_timestamp=curr), "45m")
        # 59m 59s = 3599s
        self.assertEqual(format_countdown(1000 + 3599, current_timestamp=curr), "59m")

    def test_hours_formatting(self):
        """3600 <= delta < 86400 formats as f'{h}h {m}m' if m > 0 else f'{h}h'."""
        curr = 1000.0
        # Exactly 1 hour (3600s)
        self.assertEqual(format_countdown(1000 + 3600, current_timestamp=curr), "1h")
        # Exactly 2 hours (7200s)
        self.assertEqual(format_countdown(1000 + 7200, current_timestamp=curr), "2h")
        # 2 hours 1 minute (7260s)
        self.assertEqual(format_countdown(1000 + 7260, current_timestamp=curr), "2h 1m")
        # 2 hours 15 minutes (8100s)
        self.assertEqual(format_countdown(1000 + 8100, current_timestamp=curr), "2h 15m")
        # 23 hours 59 minutes (86340s)
        self.assertEqual(format_countdown(1000 + 86340, current_timestamp=curr), "23h 59m")

    def test_days_formatting(self):
        """delta >= 86400 formats as f'{d}d {h}h' if h > 0 else f'{d}d'."""
        curr = 1000.0
        # Exactly 1 day (86400s)
        self.assertEqual(format_countdown(1000 + 86400, current_timestamp=curr), "1d")
        # 1 day 4 hours (86400 + 14400 = 100800s)
        self.assertEqual(format_countdown(1000 + 100800, current_timestamp=curr), "1d 4h")
        # Exactly 3 days (259200s)
        self.assertEqual(format_countdown(1000 + 259200, current_timestamp=curr), "3d")
        # 4 days 2 hours (4 * 86400 + 7200 = 352800s)
        self.assertEqual(format_countdown(1000 + 352800, current_timestamp=curr), "4d 2h")

    def test_default_current_timestamp_uses_time(self):
        """Omitting current_timestamp uses real time without error."""
        future_ts = int(time.time() + 120)
        res = format_countdown(future_ts)
        self.assertIn(res, ["1m", "2m"])

    def test_format_countdown_adversarial_bool_nan_inf(self):
        """format_countdown gracefully handles booleans, NaN, and Inf without throwing."""
        self.assertEqual(format_countdown(True), "-")
        self.assertEqual(format_countdown(False), "-")
        self.assertEqual(format_countdown(float("nan")), "-")
        self.assertEqual(format_countdown(float("inf")), "-")
        # Current timestamp as NaN or bool should not crash
        res = format_countdown(int(time.time() + 100), current_timestamp=float("nan"))
        self.assertIn(res, ["1m", "2m"])
        res_bool = format_countdown(int(time.time() + 100), current_timestamp=True)  # type: ignore
        self.assertIn(res_bool, ["1m", "2m"])

    def test_format_countdown_string_and_iso_timestamps(self):
        """String timestamps (numeric or ISO 8601) are parsed cleanly."""
        curr = 1000.0
        # Numeric string
        self.assertEqual(format_countdown("1120", current_timestamp=curr), "2m")
        # ISO string: 2 minutes after dt_curr
        from datetime import datetime, timezone
        dt_curr = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(
            format_countdown("2026-10-14T12:02:00Z", current_timestamp=dt_curr),
            "2m",
        )


class TestSortAccountsByQuota(unittest.TestCase):
    """Unit tests for sort_accounts_by_quota 3-tier deterministic sorting."""

    def test_sort_by_quota_descending(self):
        """Primary tier: Accounts with higher available quota appear first."""
        accounts = [
            {"name": "acc-low", "quota": {"gemini": {"quota": 40, "reset_time": 1000}}},
            {"name": "acc-high", "quota": {"gemini": {"quota": 95, "reset_time": 2000}}},
            {"name": "acc-mid", "quota": {"gemini": {"quota": 80, "reset_time": 1500}}},
        ]
        sorted_accs = sort_accounts_by_quota(accounts)
        names = [a["name"] for a in sorted_accs]
        self.assertEqual(names, ["acc-high", "acc-mid", "acc-low"])

    def test_sort_break_quota_tie_by_earliest_reset_time(self):
        """Secondary tier: Identical quota broken by earliest reset timestamp."""
        accounts = [
            {"name": "acc-later", "quota": {"gemini": {"quota": 80, "reset_time": 2000}}},
            {"name": "acc-earliest", "quota": {"gemini": {"quota": 80, "reset_time": 1000}}},
            {"name": "acc-middle", "quota": {"gemini": {"quota": 80, "reset_time": 1500}}},
        ]
        sorted_accs = sort_accounts_by_quota(accounts)
        names = [a["name"] for a in sorted_accs]
        self.assertEqual(names, ["acc-earliest", "acc-middle", "acc-later"])

    def test_sort_break_reset_tie_by_name_alphabetical(self):
        """Tertiary tier: Identical quota and reset time broken alphabetically by name."""
        accounts = [
            {"name": "charlie", "quota": {"gemini": {"quota": 80, "reset_time": 1000}}},
            {"name": "alice", "quota": {"gemini": {"quota": 80, "reset_time": 1000}}},
            {"name": "bob", "quota": {"gemini": {"quota": 80, "reset_time": 1000}}},
        ]
        sorted_accs = sort_accounts_by_quota(accounts)
        names = [a["name"] for a in sorted_accs]
        self.assertEqual(names, ["alice", "bob", "charlie"])

    def test_sort_safely_handles_null_error_missing_quotas(self):
        """Accounts with None quota, error dicts, or missing gemini sort safely to the bottom."""
        accounts = [
            {"name": "null-quota", "quota": None},
            {"name": "healthy-50", "quota": {"gemini": {"quota": 50, "reset_time": 1000}}},
            {"name": "error-quota", "quota": {"error": "Unreachable"}},
            {"name": "empty-quota", "quota": {}},
            {"name": "gemini-none", "quota": {"gemini": None}},
            {"name": "zero-quota", "quota": {"gemini": {"quota": 0, "reset_time": 1000}}},
        ]
        sorted_accs = sort_accounts_by_quota(accounts)
        names = [a["name"] for a in sorted_accs]

        # healthy-50 (quota 50) first, zero-quota (quota 0) second
        self.assertEqual(names[0], "healthy-50")
        self.assertEqual(names[1], "zero-quota")

        # Remaining (quota -1) all sorted to bottom alphabetically
        bottom_names = names[2:]
        self.assertEqual(
            bottom_names,
            sorted(["null-quota", "error-quota", "empty-quota", "gemini-none"]),
        )

    def test_sort_handles_missing_or_nonpositive_reset_time(self):
        """None, 0, or negative reset_time treated as float('inf') in tie-breaking."""
        accounts = [
            {"name": "acc-none-reset", "quota": {"gemini": {"quota": 80, "reset_time": None}}},
            {"name": "acc-valid-reset", "quota": {"gemini": {"quota": 80, "reset_time": 1000}}},
            {"name": "acc-zero-reset", "quota": {"gemini": {"quota": 80, "reset_time": 0}}},
            {"name": "acc-neg-reset", "quota": {"gemini": {"quota": 80, "reset_time": -100}}},
        ]
        sorted_accs = sort_accounts_by_quota(accounts)
        # acc-valid-reset should be first because 1000 < inf
        self.assertEqual(sorted_accs[0]["name"], "acc-valid-reset")
        # Remaining tied at inf, sorted alphabetically by name
        inf_names = [a["name"] for a in sorted_accs[1:]]
        self.assertEqual(
            inf_names,
            sorted(["acc-none-reset", "acc-zero-reset", "acc-neg-reset"]),
        )

    def test_sort_empty_list_and_non_mutating(self):
        """sort_accounts_by_quota returns a new list and handles empty list gracefully."""
        self.assertEqual(sort_accounts_by_quota([]), [])
        self.assertEqual(sort_accounts_by_quota(None), [])

        orig = [{"name": "b", "quota": {}}, {"name": "a", "quota": {}}]
        orig_copy = list(orig)
        res = sort_accounts_by_quota(orig)
        self.assertEqual(orig, orig_copy)
        self.assertIsNot(res, orig)

    def test_sort_handles_string_quotas_and_timestamps(self):
        """String quotas and timestamps (numeric or ISO) are parsed gracefully."""
        accounts = [
            {"name": "acc-low", "quota": {"gemini": {"quota": "40%", "reset_time": "1791201000"}}},
            {"name": "acc-high", "quota": {"gemini": {"quota": "95", "reset_time": "2026-10-14T12:00:00Z"}}},
        ]
        res = sort_accounts_by_quota(accounts)
        self.assertEqual(res[0]["name"], "acc-high")
        self.assertEqual(res[1]["name"], "acc-low")

    def test_sort_defensive_against_non_dict_items(self):
        """Non-dict items or items without name do not crash sorting."""
        accounts = [
            {"quota": {"gemini": {"quota": 10, "reset_time": 500}}},
            "not-a-dict",  # type: ignore
            None,  # type: ignore
        ]
        # Should not raise exception
        res = sort_accounts_by_quota(accounts)
        self.assertEqual(len(res), 3)

    def test_sort_adversarial_bool_nan_inf(self):
        """Adversarial values like True/False, NaN, and Inf are handled gracefully."""
        accounts = [
            {"name": "bool-quota", "quota": {"gemini": {"quota": True, "reset_time": 1000}}},
            {"name": "nan-quota", "quota": {"gemini": {"quota": float("nan"), "reset_time": 1000}}},
            {"name": "neg-quota", "quota": {"gemini": {"quota": -10, "reset_time": 1000}}},
            {"name": "normal-quota", "quota": {"gemini": {"quota": 50, "reset_time": 1000}}},
            {"name": "bool-reset", "quota": {"gemini": {"quota": 50, "reset_time": True}}},
            {"name": "nan-reset", "quota": {"gemini": {"quota": 50, "reset_time": float("nan")}}},
        ]
        res = sort_accounts_by_quota(accounts)
        # normal-quota and the two 50% accounts come first
        top_names = [a["name"] for a in res[:3]]
        self.assertIn("normal-quota", top_names)
        self.assertIn("bool-reset", top_names)
        self.assertIn("nan-reset", top_names)
        # normal-quota has reset_time 1000, while bool-reset and nan-reset have reset_time inf
        self.assertEqual(res[0]["name"], "normal-quota")



class TestFormatAccountsListicle(unittest.TestCase):
    """Unit tests for format_accounts_listicle formatter."""

    def test_empty_accounts_returns_notice(self):
        """Empty accounts list returns 'No accounts registered in vault (~/.antigravity-vault/accounts).'"""
        self.assertEqual(
            format_accounts_listicle({"active": None, "accounts": []}),
            "No accounts registered in vault (~/.antigravity-vault/accounts).",
        )
        self.assertEqual(
            format_accounts_listicle({}),
            "No accounts registered in vault (~/.antigravity-vault/accounts).",
        )

    def test_listicle_without_quota(self):
        """show_quota=False renders compact bullet items with [Active] / [Standby] tags."""
        data = {
            "active": "acc-active",
            "accounts": [
                {"name": "acc-active", "email": "active@example.com", "is_active": True, "valid": True},
                {"name": "acc-standby", "email": "standby@example.com", "is_active": False, "valid": True},
                {"name": "acc-invalid", "email": None, "is_active": False, "valid": False},
            ],
        }
        res = format_accounts_listicle(data, show_quota=False)
        lines = res.splitlines()
        self.assertEqual(len(lines), 4)
        self.assertEqual(lines[0], "• [*] acc-active (active@example.com) [Active]")
        self.assertEqual(lines[1], "• [ ] acc-standby (standby@example.com) [Standby]")
        self.assertEqual(lines[2], "• [ ] acc-invalid (-) [Standby (Invalid)]")
        self.assertEqual(lines[3], "Active Account: acc-active")

    def test_listicle_with_live_quota(self):
        """show_quota=True renders bullet items with worst-case percentage, bucket breakdown, and relative countdowns."""
        curr = 1000.0
        data = {
            "active": "acc1",
            "accounts": [
                {
                    "name": "acc1",
                    "email": "acc1@example.com",
                    "is_active": True,
                    "valid": True,
                    "quota": {
                        "gemini": {
                            "quota": 80,
                            "reset_time": curr + 7200,  # 2h
                            "cached": False,
                            "buckets": {
                                "5h": {"quota": 80, "reset_time": curr + 7200},  # in 2h
                                "weekly": {"quota": 95, "reset_time": curr + 352800},  # in 4d 2h
                            },
                        }
                    },
                },
                {
                    "name": "acc2",
                    "email": "acc2@example.com",
                    "is_active": False,
                    "valid": True,
                    "quota": {
                        "gemini": {
                            "quota": 50,
                            "reset_time": curr + 2700,
                            "cached": False,
                            "buckets": {
                                "5h": {"quota": 50, "reset_time": curr + 2700},  # in 45m
                                "weekly": {"quota": 70, "reset_time": curr + 86400},  # in 1d
                            },
                        }
                    },
                },
            ],
        }
        res = format_accounts_listicle(data, show_quota=True, current_timestamp=curr)
        lines = res.splitlines()
        self.assertEqual(lines[0], "• [*] acc1 (acc1@example.com) — Gemini: 80% [5h: 80% (in 2h) | wk: 95% (in 4d 2h)]")
        self.assertEqual(lines[1], "• [ ] acc2 (acc2@example.com) — Gemini: 50% [5h: 50% (in 45m) | wk: 70% (in 1d)]")
        self.assertEqual(lines[2], "Active Account: acc1")

    def test_listicle_cached_quota(self):
        """Cached quota displays '(Cached)' tag without raw bucket confusion."""
        data = {
            "active": "acc1",
            "accounts": [
                {
                    "name": "acc-cached",
                    "email": "cached@example.com",
                    "is_active": False,
                    "valid": True,
                    "quota": {
                        "gemini": {
                            "quota": 40,
                            "reset_time": 2000,
                            "cached": True,
                            "buckets": {
                                "5h": {"quota": 40, "reset_time": 2000},
                                "weekly": {"quota": 80, "reset_time": 5000},
                            },
                        }
                    },
                }
            ],
        }
        res = format_accounts_listicle(data, show_quota=True)
        lines = res.splitlines()
        self.assertEqual(lines[0], "• [ ] acc-cached (cached@example.com) — Gemini: 40% (Cached)")
        self.assertNotIn("5h:", lines[0])
        self.assertNotIn("wk:", lines[0])

    def test_listicle_error_and_unreachable_accounts(self):
        """Unreachable, missing, or error accounts display 'Error / Unreachable'."""
        data = {
            "active": None,
            "accounts": [
                {"name": "err-acc", "email": "err@example.com", "is_active": False, "valid": True, "quota": {"error": "Timeout"}},
                {"name": "none-gemini", "email": None, "is_active": False, "valid": True, "quota": {"gemini": None}},
                {"name": "none-quota", "email": "nq@example.com", "is_active": False, "valid": True, "quota": None},
                {"name": "gemini-err", "email": "ge@example.com", "is_active": False, "valid": True, "quota": {"gemini": {"error": "Failed"}}},
            ],
        }
        res = format_accounts_listicle(data, show_quota=True)
        lines = res.splitlines()
        self.assertEqual(lines[0], "• [ ] err-acc (err@example.com) — Gemini: Error / Unreachable")
        self.assertEqual(lines[1], "• [ ] none-gemini (-) — Gemini: Error / Unreachable")
        self.assertEqual(lines[2], "• [ ] none-quota (nq@example.com) — Gemini: Error / Unreachable")
        self.assertEqual(lines[3], "• [ ] gemini-err (ge@example.com) — Gemini: Error / Unreachable")
        self.assertEqual(lines[4], "No account currently active. Run: agy-switch switch <account>")

    def test_listicle_countdown_edge_cases(self):
        """Expired countdowns render '(expired)' (never '(in expired)'), and missing/0 timestamps omit countdown (never '(in -)' or '(-) ')."""
        curr = 1000.0
        data = {
            "active": "acc1",
            "accounts": [
                {
                    "name": "acc-expired",
                    "email": "exp@example.com",
                    "is_active": True,
                    "valid": True,
                    "quota": {
                        "gemini": {
                            "quota": 0,
                            "reset_time": curr - 10,
                            "cached": False,
                            "buckets": {
                                "5h": {"quota": 0, "reset_time": curr - 10},
                                "weekly": {"quota": 90, "reset_time": curr + 3600},
                            },
                        }
                    },
                },
                {
                    "name": "acc-no-ts",
                    "email": "nots@example.com",
                    "is_active": False,
                    "valid": True,
                    "quota": {
                        "gemini": {
                            "quota": 100,
                            "reset_time": 0,
                            "cached": False,
                            "buckets": {
                                "5h": {"quota": 100, "reset_time": 0},
                                "weekly": {"quota": 100, "reset_time": None},
                            },
                        }
                    },
                },
            ],
        }
        res = format_accounts_listicle(data, show_quota=True, current_timestamp=curr)
        lines = res.splitlines()
        self.assertEqual(lines[0], "• [*] acc-expired (exp@example.com) — Gemini: 0% [5h: 0% (expired) | wk: 90% (in 1h)]")
        self.assertNotIn("(in expired)", lines[0])
        self.assertEqual(lines[1], "• [ ] acc-no-ts (nots@example.com) — Gemini: 100% [5h: 100% | wk: 100%]")
        self.assertNotIn("(in -)", lines[1])
        self.assertNotIn("(-)", lines[1][len("• [ ] acc-no-ts (nots@example.com) — Gemini: 100%"):])

    def test_adversarial_inputs_listicle(self):
        """None result, non-list accounts, and non-dict account entries do not crash format_accounts_listicle."""
        self.assertEqual(
            format_accounts_listicle(None),
            "No accounts registered in vault (~/.antigravity-vault/accounts).",
        )
        self.assertEqual(
            format_accounts_listicle({"accounts": "invalid"}),  # type: ignore
            "No accounts registered in vault (~/.antigravity-vault/accounts).",
        )
        data = {
            "active": "ok",
            "accounts": [
                None,  # type: ignore
                "not-a-dict",  # type: ignore
                {
                    "name": "ok",
                    "email": "  ",
                    "is_active": True,
                    "valid": True,
                    "quota": {
                        "gemini": {
                            "quota": "85.4%",
                            "cached": "true",
                        }
                    },
                },
            ],
        }
        res = format_accounts_listicle(data, show_quota=True)
        self.assertIn("• [*] ok (-) — Gemini: 85% (Cached)", res)
        self.assertIn("Active Account: ok", res)

    def test_format_partial_bucket_5h_only(self):
        """Account with only a 5h bucket does not fabricate a synthetic wk quota in listicle or table views."""
        data = {
            "active": "acc1",
            "accounts": [
                {
                    "name": "acc1",
                    "email": "acc1@example.com",
                    "is_active": True,
                    "valid": True,
                    "quota": {
                        "gemini": {
                            "quota": 80,
                            "reset_time": 1791201600,
                            "cached": False,
                            "buckets": {
                                "5h": {"quota": 80, "reset_time": 1791201600},
                            },
                        }
                    },
                }
            ],
        }
        res = format_accounts_listicle(data, show_quota=True, current_timestamp=1791194400.0)
        self.assertIn("80% [5h: 80% (in 2h)]", res)
        self.assertNotIn("wk:", res)

        table_res = format_accounts_table(data, show_quota=True, current_timestamp=1791194400.0)
        self.assertIn("80% [5h: 80% (in 2h)]", table_res)
        self.assertNotIn("wk:", table_res)

    def test_parse_timestamp_defensive_utc_and_clamping(self):
        """Naive ISO strings evaluate in UTC, and negative/zero/non-finite timestamps clamp to float('inf')."""
        ts_naive = _parse_timestamp_defensive("2026-10-05T12:00:00")
        self.assertEqual(ts_naive, 1791201600.0)

        ts_z = _parse_timestamp_defensive("2026-10-05T12:00:00Z")
        self.assertEqual(ts_z, 1791201600.0)

        self.assertEqual(_parse_timestamp_defensive(-100), float("inf"))
        self.assertEqual(_parse_timestamp_defensive(-0.5), float("inf"))
        self.assertEqual(_parse_timestamp_defensive(0), float("inf"))
        self.assertEqual(_parse_timestamp_defensive(0.0), float("inf"))
        self.assertEqual(_parse_timestamp_defensive("-500"), float("inf"))
        self.assertEqual(_parse_timestamp_defensive("0"), float("inf"))
        self.assertEqual(_parse_timestamp_defensive("1969-12-31T23:59:59Z"), float("inf"))

        self.assertEqual(_parse_timestamp_defensive(float("nan")), float("inf"))
        self.assertEqual(_parse_timestamp_defensive(float("inf")), float("inf"))
        self.assertEqual(_parse_timestamp_defensive(float("-inf")), float("inf"))

        self.assertEqual(_parse_timestamp_defensive(None), float("inf"))
        self.assertEqual(_parse_timestamp_defensive(True), float("inf"))
        self.assertEqual(_parse_timestamp_defensive(False), float("inf"))
        self.assertEqual(_parse_timestamp_defensive("corrupted"), float("inf"))


class TestFormatAccountsTable(unittest.TestCase):
    """Unit tests for format_accounts_table formatter."""

    def test_empty_accounts_returns_notice(self):
        """Empty accounts list returns 'No accounts registered in vault (~/.antigravity-vault/accounts).'"""
        self.assertEqual(
            format_accounts_table({"active": None, "accounts": []}),
            "No accounts registered in vault (~/.antigravity-vault/accounts).",
        )
        self.assertEqual(
            format_accounts_table({}),
            "No accounts registered in vault (~/.antigravity-vault/accounts).",
        )

    def test_table_without_quota(self):
        """show_quota=False displays ACCOUNT, EMAIL, STATUS without GEMINI QUOTA."""
        data = {
            "active": "acc-active",
            "accounts": [
                {"name": "acc-active", "email": "active@example.com", "is_active": True, "valid": True},
                {"name": "acc-standby", "email": "standby@example.com", "is_active": False, "valid": True},
            ],
        }
        res = format_accounts_table(data, show_quota=False)
        self.assertIn("ACCOUNT", res)
        self.assertIn("EMAIL", res)
        self.assertIn("STATUS", res)
        self.assertNotIn("GEMINI QUOTA", res)
        self.assertNotIn("CLAUDE", res)
        self.assertIn("* acc-active", res)
        self.assertIn("  acc-standby", res)
        self.assertIn("Active Account: acc-active", res)

    def test_table_with_gemini_quota(self):
        """show_quota=True adds 'GEMINI QUOTA' column with live breakdown, cached tags, and error handling."""
        curr = 1000.0
        data = {
            "active": "acc-live",
            "accounts": [
                {
                    "name": "acc-live",
                    "email": "live@example.com",
                    "is_active": True,
                    "valid": True,
                    "quota": {
                        "gemini": {
                            "quota": 80,
                            "reset_time": curr + 7200,
                            "cached": False,
                            "buckets": {
                                "5h": {"quota": 80, "reset_time": curr + 7200},
                                "weekly": {"quota": 95, "reset_time": curr + 352800},
                            },
                        }
                    },
                },
                {
                    "name": "acc-cached",
                    "email": "cached@example.com",
                    "is_active": False,
                    "valid": True,
                    "quota": {
                        "gemini": {
                            "quota": 40,
                            "reset_time": curr + 100,
                            "cached": True,
                        }
                    },
                },
                {
                    "name": "acc-unreachable",
                    "email": "unreach@example.com",
                    "is_active": False,
                    "valid": True,
                    "quota": {
                        "error": "Failed to connect",
                    },
                },
            ],
        }
        res = format_accounts_table(data, show_quota=True, current_timestamp=curr)
        self.assertIn("GEMINI QUOTA", res)
        self.assertNotIn("STATUS          QUOTA", res)  # Must be GEMINI QUOTA, not bare QUOTA
        self.assertIn("80% [5h: 80% (in 2h) | wk: 95% (in 4d 2h)]", res)
        self.assertIn("40% (Cached)", res)
        self.assertIn("Error / Unreachable", res)
        # Verify divider length matches header
        lines = res.splitlines()
        header_line = lines[2]
        divider_top = lines[1]
        divider_bottom = lines[3]
        self.assertEqual(len(divider_top), len(header_line))
        self.assertEqual(len(divider_bottom), len(header_line))

    def test_table_claude_omitted(self):
        """Claude telemetry is never rendered in table columns even if present in payload."""
        data = {
            "active": "acc1",
            "accounts": [
                {
                    "name": "acc1",
                    "email": "acc1@example.com",
                    "is_active": True,
                    "valid": True,
                    "quota": {
                        "claude": {"quota": 90, "reset_time": 1000},
                        "gemini": {"quota": 70, "cached": True},
                    },
                }
            ],
        }
        res = format_accounts_table(data, show_quota=True)
        self.assertNotIn("(C)", res)
        self.assertNotIn("Claude", res)
        self.assertIn("70% (Cached)", res)

    def test_adversarial_inputs_table(self):
        """None result, non-list accounts, and non-dict account entries do not crash format_accounts_table."""
        self.assertEqual(
            format_accounts_table(None),
            "No accounts registered in vault (~/.antigravity-vault/accounts).",
        )
        self.assertEqual(
            format_accounts_table({"accounts": "invalid"}),  # type: ignore
            "No accounts registered in vault (~/.antigravity-vault/accounts).",
        )
        data = {
            "active": "ok",
            "accounts": [
                None,  # type: ignore
                "not-a-dict",  # type: ignore
                {"name": "ok", "email": "  ", "is_active": True, "valid": True},
            ],
        }
        res = format_accounts_table(data, show_quota=False)
        self.assertIn("* ok", res)
        self.assertIn("-", res)  # whitespace email falls back to '-'


class TestTokenExpiryAndUnpacking(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)
        self.vault_dir = self.base_path / ".antigravity-vault"
        self.gemini_dir = self.base_path / ".gemini"
        self.engine = AgySwitchEngine(vault_dir=self.vault_dir, gemini_dir=self.gemini_dir)

        # Setup active account
        self.acc_active_dir = self.vault_dir / "accounts" / "active_acc"
        self.acc_active_dir.mkdir(parents=True, exist_ok=True)
        self.active_token_file = self.acc_active_dir / "token.json"
        self.active_token_payload = create_sample_token("active@example.com")
        self.active_token_payload["token"]["access_token"] = "ya29.initial_active_tok"
        self.active_token_payload["token"]["expiry"] = "2026-10-14T00:00:00Z"
        self.active_token_file.write_text(json.dumps(self.active_token_payload, indent=2), encoding="utf-8")
        os.chmod(self.active_token_file, 0o600)
        self.engine.set_active_name("active_acc")

        # Setup standby account
        self.acc_standby_dir = self.vault_dir / "accounts" / "standby_acc"
        self.acc_standby_dir.mkdir(parents=True, exist_ok=True)
        self.standby_token_file = self.acc_standby_dir / "token.json"
        self.standby_token_payload = create_sample_token("standby@example.com")
        self.standby_token_payload["token"]["access_token"] = "ya29.initial_standby_tok"
        self.standby_token_payload["token"]["expiry"] = "2026-10-14T00:00:00Z"
        self.standby_token_file.write_text(json.dumps(self.standby_token_payload, indent=2), encoding="utf-8")
        os.chmod(self.standby_token_file, 0o600)

        # Sync active targets initially
        self.engine.switch_tokens("active_acc", restart_server=False, force=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_update_account_access_token_with_expiry(self):
        """update_account_access_token updates access_token and expiry with 0600 mode."""
        new_expiry = "2026-10-14T12:34:56Z"
        res = self.engine.update_account_access_token("active_acc", "ya29.new_tok", expiry=new_expiry)
        self.assertTrue(res)

        data = json.loads(self.active_token_file.read_text(encoding="utf-8"))
        self.assertEqual(data["token"]["access_token"], "ya29.new_tok")
        self.assertEqual(data["token"]["expiry"], new_expiry)

        # File permissions check
        mode = stat.S_IMODE(self.active_token_file.stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_update_account_access_token_preserves_expiry_when_none(self):
        """Calling update_account_access_token without expiry preserves existing expiry."""
        orig_expiry = self.active_token_payload["token"]["expiry"]
        res = self.engine.update_account_access_token("active_acc", "ya29.newer_tok")
        self.assertTrue(res)

        data = json.loads(self.active_token_file.read_text(encoding="utf-8"))
        self.assertEqual(data["token"]["access_token"], "ya29.newer_tok")
        self.assertEqual(data["token"]["expiry"], orig_expiry)

    def test_update_account_access_token_syncs_to_desktop_and_cli_if_active(self):
        """Updating active account syncs updated access_token and expiry to Desktop & CLI token files."""
        new_expiry = "2026-10-14T15:00:00Z"
        res = self.engine.update_account_access_token("active_acc", "ya29.synced_tok", expiry=new_expiry)
        self.assertTrue(res)

        jetski_path = self.gemini_dir / "jetski-standalone-oauth-token"
        cli_path = self.gemini_dir / "antigravity-cli" / "antigravity-oauth-token"

        self.assertTrue(jetski_path.is_file())
        self.assertTrue(cli_path.is_file())
        self.assertEqual(stat.S_IMODE(jetski_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(cli_path.stat().st_mode), 0o600)

        jetski_data = json.loads(jetski_path.read_text(encoding="utf-8"))
        cli_data = json.loads(cli_path.read_text(encoding="utf-8"))

        self.assertEqual(jetski_data["token"]["access_token"], "ya29.synced_tok")
        self.assertEqual(jetski_data["token"]["expiry"], new_expiry)
        self.assertEqual(cli_data["token"]["access_token"], "ya29.synced_tok")
        self.assertEqual(cli_data["token"]["expiry"], new_expiry)

    def test_update_account_access_token_does_not_sync_if_standby(self):
        """Updating standby account does not overwrite active Desktop & CLI files."""
        res = self.engine.update_account_access_token("standby_acc", "ya29.standby_updated", expiry="2026-10-14T18:00:00Z")
        self.assertTrue(res)

        jetski_path = self.gemini_dir / "jetski-standalone-oauth-token"
        cli_path = self.gemini_dir / "antigravity-cli" / "antigravity-oauth-token"

        jetski_data = json.loads(jetski_path.read_text(encoding="utf-8"))
        cli_data = json.loads(cli_path.read_text(encoding="utf-8"))

        self.assertEqual(jetski_data["token"]["access_token"], "ya29.initial_active_tok")
        self.assertEqual(cli_data["token"]["access_token"], "ya29.initial_active_tok")

    def test_update_account_access_token_invalid_or_whitespace_name(self):
        """update_account_access_token handles invalid names and strips whitespace."""
        self.assertFalse(self.engine.update_account_access_token("", "ya29.valid_tok"))
        self.assertFalse(self.engine.update_account_access_token("   ", "ya29.valid_tok"))
        self.assertFalse(self.engine.update_account_access_token(None, "ya29.valid_tok"))  # type: ignore

        # Stripped active name correctly updates and syncs
        res = self.engine.update_account_access_token("  active_acc  ", "ya29.stripped_name_tok", expiry="2026-10-14T20:00:00Z")
        self.assertTrue(res)
        jetski_path = self.gemini_dir / "jetski-standalone-oauth-token"
        jetski_data = json.loads(jetski_path.read_text(encoding="utf-8"))
        self.assertEqual(jetski_data["token"]["access_token"], "ya29.stripped_name_tok")

    def test_fetch_account_quota_on_refresh_calculates_utc_iso_expiry(self):
        """fetch_account_quota computes UTC ISO 8601 ending in Z from expires_in and saves it, syncing targets."""
        mock_client = MagicMock(spec=QuotaClient)

        def fake_fetch_quota(access_tok, refresh_token=None, on_refresh=None):
            if on_refresh:
                on_refresh({"access_token": "ya29.auto_refreshed", "expires_in": 7200})
            return {"gemini": {"quota": 90, "reset_time": 12345, "cached": False, "buckets": {}}}

        mock_client.fetch_quota.side_effect = fake_fetch_quota

        with patch("agy_switch.datetime") as mock_dt:
            from datetime import datetime as real_datetime, timezone as real_tz
            fixed_now = real_datetime(2026, 10, 14, 10, 0, 0, tzinfo=real_tz.utc)
            mock_dt.now.return_value = fixed_now
            mock_dt.fromisoformat = real_datetime.fromisoformat

            name, q = fetch_account_quota(self.engine, "active_acc", client=mock_client)
            self.assertEqual(name, "active_acc")
            self.assertIn("gemini", q)

        # 7200 seconds = 2 hours -> 2026-10-14T12:00:00Z
        data = json.loads(self.active_token_file.read_text(encoding="utf-8"))
        self.assertEqual(data["token"]["access_token"], "ya29.auto_refreshed")
        self.assertEqual(data["token"]["expiry"], "2026-10-14T12:00:00Z")

        # Verify dual targets were also synced upon active account refresh with mode 0600
        jetski_path = self.gemini_dir / "jetski-standalone-oauth-token"
        cli_path = self.gemini_dir / "antigravity-cli" / "antigravity-oauth-token"
        self.assertEqual(stat.S_IMODE(jetski_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(cli_path.stat().st_mode), 0o600)
        jetski_data = json.loads(jetski_path.read_text(encoding="utf-8"))
        cli_data = json.loads(cli_path.read_text(encoding="utf-8"))
        self.assertEqual(jetski_data["token"]["access_token"], "ya29.auto_refreshed")
        self.assertEqual(jetski_data["token"]["expiry"], "2026-10-14T12:00:00Z")
        self.assertEqual(cli_data["token"]["access_token"], "ya29.auto_refreshed")
        self.assertEqual(cli_data["token"]["expiry"], "2026-10-14T12:00:00Z")

    def test_fetch_account_quota_refresh_revoked_fails_gracefully(self):
        """fetch_account_quota returns Fetch failed error dict when token refresh fails/revoked."""
        mock_client = MagicMock(spec=QuotaClient)
        # Client returns empty dict on revoked refresh
        mock_client.fetch_quota.return_value = {}
        mock_client.last_error = "Fetch failed"
        mock_client.last_error_type = "auth"

        name, q = fetch_account_quota(self.engine, "active_acc", client=mock_client)
        self.assertEqual(name, "active_acc")
        self.assertEqual(q.get("error"), "Fetch failed")
        self.assertEqual(q.get("error_type"), "auth")

    def test_update_account_access_token_empty_token_rejected(self):
        """update_account_access_token rejects empty or whitespace access tokens."""
        self.assertFalse(self.engine.update_account_access_token("active_acc", ""))
        self.assertFalse(self.engine.update_account_access_token("active_acc", "   "))
        self.assertFalse(self.engine.update_account_access_token("active_acc", None))  # type: ignore

    def test_fetch_account_quota_boundary_and_float_expires_in(self):
        """fetch_account_quota handles float strings, non-finite values, and booleans safely."""
        test_cases = [
            ("1800.0", "2026-10-14T10:30:00Z"),
            ("inf", "2026-10-14T11:00:00Z"),  # defaults to 3600
            (True, "2026-10-14T11:00:00Z"),   # defaults to 3600
            (-10, "2026-10-14T11:00:00Z"),    # defaults to 3600
        ]
        for exp_val, expected_expiry in test_cases:
            mock_client = MagicMock(spec=QuotaClient)

            def fake_fetch_quota(access_tok, refresh_token=None, on_refresh=None):
                if on_refresh:
                    on_refresh({"access_token": "ya29.test_tok", "expires_in": exp_val})
                return {"gemini": {"quota": 90, "reset_time": 12345, "cached": False, "buckets": {}}}

            mock_client.fetch_quota.side_effect = fake_fetch_quota

            with patch("agy_switch.datetime") as mock_dt:
                from datetime import datetime as real_datetime, timezone as real_tz
                fixed_now = real_datetime(2026, 10, 14, 10, 0, 0, tzinfo=real_tz.utc)
                mock_dt.now.return_value = fixed_now
                mock_dt.fromisoformat = real_datetime.fromisoformat

                fetch_account_quota(self.engine, "active_acc", client=mock_client)

            data = json.loads(self.active_token_file.read_text(encoding="utf-8"))
            self.assertEqual(data["token"]["access_token"], "ya29.test_tok")
            self.assertEqual(data["token"]["expiry"], expected_expiry, f"Failed for expires_in={exp_val}")


if __name__ == "__main__":
    unittest.main()



