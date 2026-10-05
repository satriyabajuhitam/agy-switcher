#!/usr/bin/env python3
"""Tests for Ticket 01: Vault Foundation & Auto-Migration (agy-switch)."""

import json
import os
import stat
import subprocess
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()



