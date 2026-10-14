#!/usr/bin/env python3
"""Comprehensive unit and integration tests for full-state account alias migration (agy-switch rename).

Contracts:
- .scratch/doctor-vault-mgmt/spec.md
- docs/adr/0008-unified-doctor-model-steering-and-vault-management.md
- .scratch/doctor-vault-mgmt/issues/01-account-alias-migration.md
"""

from __future__ import annotations

import base64
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Ensure scripts directory is in sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from agy_switch import (
    AgySwitchEngine,
    AgySwitchError,
    AccountNotFoundError,
    ActiveTurnRunningError,
    main,
)


def create_sample_token(email: str = "test@example.com") -> dict:
    """Creates a valid sample OAuth token dict for testing."""
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


class TestAccountAliasMigration(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)
        self.vault_dir = self.base_path / ".antigravity-vault"
        self.gemini_dir = self.base_path / ".gemini"
        self.legacy_root = self.base_path / ".agy-cli-manager"
        self.proc_dir = self.base_path / "proc"
        self.proc_dir.mkdir(parents=True, exist_ok=True)

        self.engine = AgySwitchEngine(
            vault_dir=self.vault_dir,
            gemini_dir=self.gemini_dir,
            legacy_root=self.legacy_root,
            proc_dir=self.proc_dir,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _create_account(self, name: str, email: str | None = None) -> Path:
        """Helper to create an account in the vault with valid token."""
        acc_dir = self.engine.accounts_dir / name
        acc_dir.mkdir(parents=True, exist_ok=True)
        token_data = create_sample_token(email=email or f"{name}@example.com")
        token_file = acc_dir / "token.json"
        self.engine._atomic_write_json(token_file, token_data)
        return acc_dir

    def _setup_cache(self, quotas: dict[str, dict]) -> Path:
        """Helper to set up last_known_quota.json."""
        cache_data = {
            "timestamp": 1728900000.0,
            "accounts": quotas,
        }
        self.engine._atomic_write_json(self.engine.cache_file, cache_data)
        return self.engine.cache_file

    def _setup_cooldown(self, cooldowns: dict[str, float | dict]) -> Path:
        """Helper to set up cooldown.json."""
        self.engine._atomic_write_json(self.engine.cooldown_file, cooldowns)
        return self.engine.cooldown_file

    # --- 1. Basic Inactive Account Rename ---

    def test_rename_inactive_account_success(self) -> None:
        """Renaming an inactive account updates directory name and preserves token."""
        self._create_account("acc1")
        self._create_account("acc2")
        self.engine.set_active_name("acc2")

        res = self.engine.rename_account("acc1", "acc1_renamed")

        self.assertEqual(
            res,
            {
                "old_alias": "acc1",
                "new_alias": "acc1_renamed",
                "is_active": False,
                "cache_migrated": False,
                "cooldown_migrated": False,
            },
        )
        self.assertFalse((self.engine.accounts_dir / "acc1").exists())
        new_token_file = self.engine.accounts_dir / "acc1_renamed" / "token.json"
        self.assertTrue(new_token_file.is_file())
        self.assertEqual(self.engine.get_active_name(), "acc2")

    # --- 2. Active Account Rename & Pointer Update ---

    def test_rename_active_account_updates_pointer(self) -> None:
        """Renaming the active account updates the active pointer file atomically."""
        self._create_account("acc1")
        self.engine.set_active_name("acc1")

        res = self.engine.rename_account("acc1", "acc_active_new")

        self.assertEqual(
            res,
            {
                "old_alias": "acc1",
                "new_alias": "acc_active_new",
                "is_active": True,
                "cache_migrated": False,
                "cooldown_migrated": False,
            },
        )
        self.assertFalse((self.engine.accounts_dir / "acc1").exists())
        self.assertTrue((self.engine.accounts_dir / "acc_active_new").is_dir())
        self.assertEqual(self.engine.get_active_name(), "acc_active_new")
        self.assertEqual(self.engine.active_file.read_text(encoding="utf-8").strip(), "acc_active_new")

    def test_rename_updates_legacy_state_if_present(self) -> None:
        """If legacy ~/.agy-cli-manager/state.json exists with active account, it is updated."""
        self._create_account("acc1")
        self.engine.set_active_name("acc1")
        legacy_state = self.legacy_root / "state.json"
        self.engine._atomic_write_json(legacy_state, {"active": "acc1", "other": "val"})

        self.engine.rename_account("acc1", "acc_legacy_renamed")

        loaded = json.loads(legacy_state.read_text(encoding="utf-8"))
        self.assertEqual(loaded.get("active"), "acc_legacy_renamed")
        self.assertEqual(loaded.get("other"), "val")

    # --- 3. Whitespace Normalization ---

    def test_rename_whitespace_normalization(self) -> None:
        """Aliases with leading/trailing whitespace are stripped and normalized."""
        self._create_account("acc_spaces")
        res = self.engine.rename_account("  acc_spaces  ", "  acc_clean_spaces  ")
        self.assertEqual(res["old_alias"], "acc_spaces")
        self.assertEqual(res["new_alias"], "acc_clean_spaces")
        self.assertTrue((self.engine.accounts_dir / "acc_clean_spaces").is_dir())

    # --- 4. Rejection Rules ---

    def test_rejection_identical_source_and_target(self) -> None:
        """Rejects old_alias == new_alias with explicit exact error message."""
        self._create_account("acc1")
        with self.assertRaises(AgySwitchError) as ctx:
            self.engine.rename_account("acc1", "acc1")
        self.assertEqual(str(ctx.exception), "Source and target account alias must be different.")

    def test_rejection_identical_source_and_target_whitespace(self) -> None:
        """Rejects old_alias == new_alias even when padded with whitespace."""
        self._create_account("acc1")
        with self.assertRaises(AgySwitchError) as ctx:
            self.engine.rename_account("acc1", "  acc1  ")
        self.assertEqual(str(ctx.exception), "Source and target account alias must be different.")

    def test_rejection_old_alias_not_found(self) -> None:
        """Rejects non-existent old_alias with AccountNotFoundError."""
        with self.assertRaises(AccountNotFoundError) as ctx:
            self.engine.rename_account("missing_acc", "target_acc")
        self.assertIn("missing_acc", str(ctx.exception))

    def test_rejection_new_alias_already_exists(self) -> None:
        """Rejects renaming if target alias already exists in the vault."""
        self._create_account("acc1")
        self._create_account("acc2")
        with self.assertRaises(AgySwitchError) as ctx:
            self.engine.rename_account("acc1", "acc2")
        self.assertIn("already exists", str(ctx.exception))

    # --- 5. Slug Sanitization & Validation ---

    def test_slug_validation_empty_and_whitespace(self) -> None:
        """Rejects empty strings and whitespace-only targets."""
        self._create_account("acc1")
        with self.assertRaises(AgySwitchError):
            self.engine.rename_account("acc1", "")
        with self.assertRaises(AgySwitchError):
            self.engine.rename_account("acc1", "   ")
        with self.assertRaises(AgySwitchError):
            self.engine.rename_account("", "target")

    def test_slug_validation_invalid_characters(self) -> None:
        """Rejects spaces, slashes, backslashes, and special symbols."""
        self._create_account("acc1")
        invalid_slugs = [
            "name with spaces",
            "name/with/slash",
            "name\\with\\backslash",
            "name@host",
            "name#hash",
            "name$dollar",
            "name!excl",
            "name:colon",
            "name;semi",
        ]
        for bad_slug in invalid_slugs:
            with self.subTest(slug=bad_slug):
                with self.assertRaises(AgySwitchError):
                    self.engine.rename_account("acc1", bad_slug)

    def test_slug_validation_path_traversal(self) -> None:
        """Rejects relative path traversal elements (.., .)."""
        self._create_account("acc1")
        traversals = ["..", ".", "../etc", "acc/..", "acc..bar", "..acc", "acc.."]
        for bad in traversals:
            with self.subTest(traversal=bad):
                with self.assertRaises(AgySwitchError):
                    self.engine.rename_account("acc1", bad)

    def test_slug_validation_length_bounds(self) -> None:
        """Accepts length 1–64, rejects > 64 chars."""
        self._create_account("acc1")
        max_valid = "a" * 64
        res = self.engine.rename_account("acc1", max_valid)
        self.assertEqual(res["new_alias"], max_valid)

        too_long = "b" * 65
        with self.assertRaises(AgySwitchError):
            self.engine.rename_account(max_valid, too_long)

    def test_slug_validation_allowed_character_set(self) -> None:
        """Accepts alphanumeric, dots, underscores, and hyphens."""
        self._create_account("old_name")
        valid_slug = "My-Account_1.0.beta"
        res = self.engine.rename_account("old_name", valid_slug)
        self.assertEqual(res["new_alias"], valid_slug)
        self.assertTrue((self.engine.accounts_dir / valid_slug).is_dir())

    # --- 6. Telemetry Cache Migration ---

    def test_telemetry_cache_key_migration(self) -> None:
        """Migrates key in last_known_quota.json preserving quota payloads."""
        self._create_account("acc1")
        self._create_account("acc2")
        quota_payload = {
            "gemini": {"quota": 85, "reset_time": 1728950000},
            "claude": {"quota": 60, "reset_time": 1728960000},
        }
        self._setup_cache({"acc1": quota_payload, "acc2": {"gemini": {"quota": 100}}})

        res = self.engine.rename_account("acc1", "acc1_new")
        self.assertTrue(res["cache_migrated"])

        loaded = json.loads(self.engine.cache_file.read_text(encoding="utf-8"))
        self.assertNotIn("acc1", loaded["accounts"])
        self.assertIn("acc1_new", loaded["accounts"])
        self.assertEqual(loaded["accounts"]["acc1_new"], quota_payload)
        self.assertIn("acc2", loaded["accounts"])
        # Ensure file permissions remain 0600
        file_mode = stat.S_IMODE(self.engine.cache_file.stat().st_mode)
        self.assertEqual(file_mode, 0o600)

    # --- 7. Cooldown Cache Migration ---

    def test_cooldown_cache_key_migration_float(self) -> None:
        """Migrates key in cooldown.json for float timestamp values."""
        self._create_account("acc1")
        cooldown_ts = 1728903600.0
        self._setup_cooldown({"acc1": cooldown_ts, "acc_other": 1728900000.0})

        res = self.engine.rename_account("acc1", "acc1_new")
        self.assertTrue(res["cooldown_migrated"])

        loaded = json.loads(self.engine.cooldown_file.read_text(encoding="utf-8"))
        self.assertNotIn("acc1", loaded)
        self.assertIn("acc1_new", loaded)
        self.assertEqual(loaded["acc1_new"], cooldown_ts)
        self.assertIn("acc_other", loaded)
        file_mode = stat.S_IMODE(self.engine.cooldown_file.stat().st_mode)
        self.assertEqual(file_mode, 0o600)

    def test_cooldown_cache_key_migration_dict(self) -> None:
        """Migrates key in cooldown.json for structured dictionary records."""
        self._create_account("acc1")
        record = {"exhausted_at": 1728900000.0, "cooldown_until": 1728903600.0, "model": "gemini"}
        self._setup_cooldown({"acc1": record})

        res = self.engine.rename_account("acc1", "acc1_new")
        self.assertTrue(res["cooldown_migrated"])

        loaded = json.loads(self.engine.cooldown_file.read_text(encoding="utf-8"))
        self.assertEqual(loaded.get("acc1_new"), record)

    # --- 8. Non-Destructive Grace on Missing/Corrupt Files ---

    def test_non_destructive_grace_when_cache_files_missing(self) -> None:
        """Missing last_known_quota.json or cooldown.json does not abort rename."""
        self._create_account("acc1")
        if self.engine.cache_file.exists():
            self.engine.cache_file.unlink()
        if self.engine.cooldown_file.exists():
            self.engine.cooldown_file.unlink()

        res = self.engine.rename_account("acc1", "acc1_new")
        self.assertEqual(res["cache_migrated"], False)
        self.assertEqual(res["cooldown_migrated"], False)
        self.assertTrue((self.engine.accounts_dir / "acc1_new").is_dir())

    def test_non_destructive_grace_when_cache_files_corrupt(self) -> None:
        """Corrupt last_known_quota.json or cooldown.json emits warning without crashing."""
        self._create_account("acc1")
        self.engine.cache_file.write_text("{malformed json", encoding="utf-8")
        self.engine.cooldown_file.write_text("{malformed json", encoding="utf-8")

        stderr_buf = io.StringIO()
        with mock.patch("sys.stderr", stderr_buf):
            res = self.engine.rename_account("acc1", "acc1_new")

        self.assertEqual(res["cache_migrated"], False)
        self.assertEqual(res["cooldown_migrated"], False)
        self.assertTrue((self.engine.accounts_dir / "acc1_new").is_dir())
        self.assertIn("Warning", stderr_buf.getvalue())

    # --- 9. Pre-Flight Write Validation ---

    def test_preflight_fails_when_active_file_not_writable(self) -> None:
        """Pre-flight check fails if active_file exists and is not writable."""
        self._create_account("acc1")
        self.engine.set_active_name("acc1")
        os.chmod(self.engine.active_file, 0o400)

        try:
            with self.assertRaises(AgySwitchError) as ctx:
                self.engine.rename_account("acc1", "acc1_new")
            self.assertIn("active", str(ctx.exception).lower())
            # Ensure filesystem directory rename was NOT executed
            self.assertTrue((self.engine.accounts_dir / "acc1").is_dir())
            self.assertFalse((self.engine.accounts_dir / "acc1_new").exists())
        finally:
            os.chmod(self.engine.active_file, 0o600)

    def test_preflight_fails_when_cache_file_not_writable(self) -> None:
        """Pre-flight check fails if cache_file exists and is not writable."""
        self._create_account("acc1")
        self._setup_cache({"acc1": {}})
        os.chmod(self.engine.cache_file, 0o400)

        try:
            with self.assertRaises(AgySwitchError) as ctx:
                self.engine.rename_account("acc1", "acc1_new")
            self.assertIn("cache", str(ctx.exception).lower())
            self.assertTrue((self.engine.accounts_dir / "acc1").is_dir())
            self.assertFalse((self.engine.accounts_dir / "acc1_new").exists())
        finally:
            os.chmod(self.engine.cache_file, 0o600)

    def test_preflight_fails_when_cooldown_file_not_writable(self) -> None:
        """Pre-flight check fails if cooldown_file exists and is not writable."""
        self._create_account("acc1")
        self._setup_cooldown({"acc1": 1728900000.0})
        os.chmod(self.engine.cooldown_file, 0o400)

        try:
            with self.assertRaises(AgySwitchError) as ctx:
                self.engine.rename_account("acc1", "acc1_new")
            self.assertIn("cooldown", str(ctx.exception).lower())
            self.assertTrue((self.engine.accounts_dir / "acc1").is_dir())
            self.assertFalse((self.engine.accounts_dir / "acc1_new").exists())
        finally:
            os.chmod(self.engine.cooldown_file, 0o600)

    def test_preflight_fails_when_accounts_dir_not_writable(self) -> None:
        """Pre-flight check fails if accounts directory is not writable."""
        self._create_account("acc1")
        with mock.patch("os.access") as mock_access:
            def access_side_effect(path, mode):
                if str(path) == str(self.engine.accounts_dir):
                    return False
                return True
            mock_access.side_effect = access_side_effect

            with self.assertRaises(AgySwitchError) as ctx:
                self.engine.rename_account("acc1", "acc1_new")
            self.assertIn("accounts directory", str(ctx.exception).lower())

    # --- 10. Transactional Rollback Guarantee ---

    def test_rollback_on_active_pointer_failure(self) -> None:
        """If updating active_file fails after directory rename, rollback restores original directory."""
        self._create_account("acc1")
        self.engine.set_active_name("acc1")

        with mock.patch.object(self.engine, "set_active_name", side_effect=OSError("Disk write failed")):
            with self.assertRaises(AgySwitchError) as ctx:
                self.engine.rename_account("acc1", "acc1_target")
            self.assertIn("Failed to update active account pointer", str(ctx.exception))

        # Rollback guarantee verification
        self.assertTrue((self.engine.accounts_dir / "acc1").is_dir())
        self.assertFalse((self.engine.accounts_dir / "acc1_target").exists())
        self.assertEqual(self.engine.get_active_name(), "acc1")

    # --- 11. Active Turn Safety Guard ---

    def test_active_turn_safety_blocks_busy_active_account(self) -> None:
        """Renaming active account while language_server is busy raises ActiveTurnRunningError."""
        self._create_account("acc1")
        self.engine.set_active_name("acc1")

        with mock.patch.object(self.engine, "is_server_busy", return_value=True):
            with self.assertRaises(ActiveTurnRunningError) as ctx:
                self.engine.rename_account("acc1", "acc1_new", force=False)
            self.assertIn("busy", str(ctx.exception))

        # Directory remains unchanged
        self.assertTrue((self.engine.accounts_dir / "acc1").is_dir())
        self.assertFalse((self.engine.accounts_dir / "acc1_new").exists())

    def test_active_turn_safety_bypassed_with_force(self) -> None:
        """ActiveTurnRunningError is bypassed when force=True is provided."""
        self._create_account("acc1")
        self.engine.set_active_name("acc1")

        with mock.patch.object(self.engine, "is_server_busy", return_value=True):
            res = self.engine.rename_account("acc1", "acc1_new", force=True)

        self.assertEqual(res["new_alias"], "acc1_new")
        self.assertTrue((self.engine.accounts_dir / "acc1_new").is_dir())
        self.assertEqual(self.engine.get_active_name(), "acc1_new")

    def test_active_turn_safety_allows_inactive_rename_even_if_busy(self) -> None:
        """Renaming an inactive account succeeds even if language_server is busy."""
        self._create_account("acc1")
        self._create_account("acc2")
        self.engine.set_active_name("acc2")

        with mock.patch.object(self.engine, "is_server_busy", return_value=True):
            res = self.engine.rename_account("acc1", "acc1_new", force=False)

        self.assertEqual(res["new_alias"], "acc1_new")
        self.assertTrue((self.engine.accounts_dir / "acc1_new").is_dir())

    # --- 12. Case Collision / Case Sensitivity ---

    def test_case_change_rename(self) -> None:
        """Renaming from acc1 to ACC1 works cleanly and safely."""
        self._create_account("acc1")
        res = self.engine.rename_account("acc1", "ACC1")
        self.assertEqual(res["new_alias"], "ACC1")
        self.assertTrue((self.engine.accounts_dir / "ACC1").is_dir())

    def test_case_collision_rejects_distinct_existing_account(self) -> None:
        """If target casing already exists as distinct account on case-sensitive OS, reject."""
        self._create_account("acc1")
        self._create_account("ACC1")
        with self.assertRaises(AgySwitchError) as ctx:
            self.engine.rename_account("acc1", "ACC1")
        self.assertIn("already exists", str(ctx.exception))

    # --- 13. CLI Invocations (main) ---

    def test_cli_rename_success(self) -> None:
        """CLI agy-switch rename <old> <new> succeeds with exit code 0."""
        self._create_account("acc1")
        self.engine.set_active_name("acc1")
        self._setup_cache({"acc1": {}})
        self._setup_cooldown({"acc1": 1728900000.0})

        stdout_buf = io.StringIO()
        with mock.patch("sys.stdout", stdout_buf):
            code = main(["rename", "acc1", "acc1_renamed"], engine=self.engine)

        self.assertEqual(code, 0)
        output = stdout_buf.getvalue()
        self.assertIn("Successfully renamed account 'acc1' to 'acc1_renamed'", output)
        self.assertIn("Active Pointer", output)
        self.assertIn("Telemetry Cache", output)
        self.assertIn("Cooldown Cache", output)

    def test_cli_rename_identical_source_and_target_exit_code_1(self) -> None:
        """CLI rejects identical source and target alias with exit code 1 and exact message."""
        self._create_account("acc1")
        stderr_buf = io.StringIO()
        with mock.patch("sys.stderr", stderr_buf):
            code = main(["rename", "acc1", "acc1"], engine=self.engine)

        self.assertEqual(code, 1)
        self.assertIn("[agy-switch] Source and target account alias must be different.", stderr_buf.getvalue())

    def test_cli_rename_not_found_exit_code_1(self) -> None:
        """CLI rejects missing account with exit code 1."""
        stderr_buf = io.StringIO()
        with mock.patch("sys.stderr", stderr_buf):
            code = main(["rename", "nonexistent", "new_alias"], engine=self.engine)

        self.assertEqual(code, 1)
        self.assertIn("not found in vault", stderr_buf.getvalue())

    def test_cli_rename_target_exists_exit_code_1(self) -> None:
        """CLI rejects existing target alias with exit code 1."""
        self._create_account("acc1")
        self._create_account("acc2")
        stderr_buf = io.StringIO()
        with mock.patch("sys.stderr", stderr_buf):
            code = main(["rename", "acc1", "acc2"], engine=self.engine)

        self.assertEqual(code, 1)
        self.assertIn("already exists", stderr_buf.getvalue())

    def test_cli_rename_active_busy_without_force_exit_code_2(self) -> None:
        """CLI rejects rename when active account is busy without --force with exit code 2."""
        self._create_account("acc1")
        self.engine.set_active_name("acc1")

        with mock.patch.object(self.engine, "is_server_busy", return_value=True):
            stderr_buf = io.StringIO()
            with mock.patch("sys.stderr", stderr_buf):
                code = main(["rename", "acc1", "acc1_target"], engine=self.engine)

        self.assertEqual(code, 2)
        self.assertIn("busy", stderr_buf.getvalue())

    def test_cli_rename_active_busy_with_force_exit_code_0(self) -> None:
        """CLI allows rename when active account is busy if --force is supplied."""
        self._create_account("acc1")
        self.engine.set_active_name("acc1")

        with mock.patch.object(self.engine, "is_server_busy", return_value=True):
            stdout_buf = io.StringIO()
            with mock.patch("sys.stdout", stdout_buf):
                code = main(["rename", "acc1", "acc1_target", "--force"], engine=self.engine)

        self.assertEqual(code, 0)
        self.assertTrue((self.engine.accounts_dir / "acc1_target").is_dir())

    # --- 14. Advanced Security & Edge Case Validations ---

    def test_slug_validation_old_alias_path_traversal_attack(self) -> None:
        """Old alias with path traversal or relative elements is rejected and does not mutate filesystem."""
        self._create_account("acc1")
        bad_sources = ["../cache", "/etc", "../../tmp", "acc/..", "..", "."]
        for bad in bad_sources:
            with self.subTest(bad_old=bad):
                with self.assertRaises(AgySwitchError):
                    self.engine.rename_account(bad, "legit_target")
        # Ensure cache directory is intact and not moved
        self.assertTrue(self.engine.cache_dir.is_dir())
        self.assertTrue((self.engine.accounts_dir / "acc1").is_dir())
        self.assertFalse((self.engine.accounts_dir / "legit_target").exists())

    def test_slug_validation_old_alias_invalid_characters(self) -> None:
        """Old alias with spaces or invalid characters is rejected."""
        bad_sources = ["bad name", "bad/slash", "bad\\backslash", "bad:colon"]
        for bad in bad_sources:
            with self.subTest(bad_old=bad):
                with self.assertRaises(AgySwitchError):
                    self.engine.rename_account(bad, "new_acc")

    def test_type_validation_non_string_arguments(self) -> None:
        """Non-string arguments for old_alias or new_alias raise AgySwitchError."""
        self._create_account("acc1")
        with self.assertRaises(AgySwitchError):
            self.engine.rename_account(None, "valid_name")  # type: ignore
        with self.assertRaises(AgySwitchError):
            self.engine.rename_account("acc1", None)  # type: ignore
        with self.assertRaises(AgySwitchError):
            self.engine.rename_account(12345, "valid_name")  # type: ignore
        with self.assertRaises(AgySwitchError):
            self.engine.rename_account("acc1", 67890)  # type: ignore

    def test_preflight_fails_when_vault_dir_not_writable(self) -> None:
        """Preflight fails if vault directory (parent of active pointer) is not writable."""
        self._create_account("acc1")
        with mock.patch("os.access") as mock_access:
            def access_side_effect(path, mode):
                if str(path) == str(self.engine.vault_dir):
                    return False
                return True
            mock_access.side_effect = access_side_effect

            with self.assertRaises(AgySwitchError) as ctx:
                self.engine.rename_account("acc1", "acc_new")
            self.assertIn("not writable", str(ctx.exception).lower())

    def test_preflight_fails_when_cache_dir_not_writable(self) -> None:
        """Preflight fails if cache directory (parent of cache and cooldown files) is not writable."""
        self._create_account("acc1")
        with mock.patch("os.access") as mock_access:
            def access_side_effect(path, mode):
                if str(path) == str(self.engine.cache_dir):
                    return False
                return True
            mock_access.side_effect = access_side_effect

            with self.assertRaises(AgySwitchError) as ctx:
                self.engine.rename_account("acc1", "acc_new")
            self.assertIn("not writable", str(ctx.exception).lower())

    def test_case_rename_intermediate_failure_restores_old_dir(self) -> None:
        """If second step of case-only rename fails, intermediate dir is restored to original dir."""
        self._create_account("acc_case")
        real_rename = Path.rename

        def fail_on_dst_rename(path_self, target):
            # Fail only when renaming from .tmp-rename-... to ACC_CASE (not when rolling back to acc_case)
            if ".tmp-rename-" in str(path_self) and "ACC_CASE" in str(target):
                raise OSError("Simulated second rename failure")
            return real_rename(path_self, target)

        with mock.patch.object(Path, "rename", side_effect=fail_on_dst_rename, autospec=True):
            with self.assertRaises(AgySwitchError) as ctx:
                self.engine.rename_account("acc_case", "ACC_CASE")
            self.assertIn("Failed to rename account directory", str(ctx.exception))

        # Original account directory restored
        self.assertTrue((self.engine.accounts_dir / "acc_case").is_dir())
        self.assertFalse((self.engine.accounts_dir / "ACC_CASE").exists())

    def test_target_is_broken_symlink_rejected(self) -> None:
        """If target alias is a broken symlink, it is rejected as already existing."""
        self._create_account("acc1")
        broken_link = self.engine.accounts_dir / "acc_broken"
        try:
            broken_link.symlink_to(self.engine.accounts_dir / "nonexistent_target")
        except OSError:
            self.skipTest("Symlinks not supported on filesystem")

        with self.assertRaises(AgySwitchError) as ctx:
            self.engine.rename_account("acc1", "acc_broken")
        self.assertIn("already exists", str(ctx.exception))

    def test_cache_migration_with_account_named_accounts(self) -> None:
        """An account named 'accounts' does not destroy the root 'accounts' dictionary."""
        self._create_account("accounts")
        quota_payload = {"gemini": {"quota": 90}}
        self._setup_cache({"acc_other": {"gemini": {"quota": 50}}, "accounts": quota_payload})

        res = self.engine.rename_account("accounts", "acc_safe")
        self.assertTrue(res["cache_migrated"])

        loaded = json.loads(self.engine.cache_file.read_text(encoding="utf-8"))
        self.assertIn("accounts", loaded)
        self.assertIsInstance(loaded["accounts"], dict)
        self.assertIn("acc_safe", loaded["accounts"])
        self.assertEqual(loaded["accounts"]["acc_safe"], quota_payload)
        self.assertIn("acc_other", loaded["accounts"])

    def test_cache_migration_legacy_top_level_dict(self) -> None:
        """Legacy flat cache without 'accounts' envelope is properly migrated."""
        self._create_account("acc_legacy")
        flat_cache = {
            "timestamp": 1728900000.0,
            "acc_legacy": {"quota": 77},
            "other_acc": {"quota": 88},
        }
        self.engine._atomic_write_json(self.engine.cache_file, flat_cache)

        res = self.engine.rename_account("acc_legacy", "acc_migrated")
        self.assertTrue(res["cache_migrated"])

        loaded = json.loads(self.engine.cache_file.read_text(encoding="utf-8"))
        self.assertNotIn("acc_legacy", loaded)
        self.assertIn("acc_migrated", loaded)
        self.assertEqual(loaded["acc_migrated"], {"quota": 77})
        self.assertIn("other_acc", loaded)

    def test_rollback_failure_reports_both_errors(self) -> None:
        """When directory rollback also fails, exception reports both initial and rollback failure."""
        self._create_account("acc1")
        self.engine.set_active_name("acc1")

        real_rename = Path.rename
        call_count = 0

        def rename_side_effect(path_self, target):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return real_rename(path_self, target)
            raise OSError("Rollback rename disk error")

        with mock.patch.object(self.engine, "set_active_name", side_effect=OSError("Active pointer disk error")):
            with mock.patch.object(Path, "rename", side_effect=rename_side_effect, autospec=True):
                with self.assertRaises(AgySwitchError) as ctx:
                    self.engine.rename_account("acc1", "acc_fail")
                msg = str(ctx.exception)
                self.assertIn("Active pointer disk error", msg)
                self.assertIn("Rollback also failed", msg)

    def test_list_accounts_ignores_hidden_and_temp_directories(self) -> None:
        """list_accounts() ignores hidden dot-directories and temporary files."""
        self._create_account("acc_real")
        dot_dir = self.engine.accounts_dir / ".tmp-rename-123"
        dot_dir.mkdir(parents=True, exist_ok=True)
        (dot_dir / "token.json").write_text("{}", encoding="utf-8")

        res = self.engine.list_accounts()
        account_names = [a["name"] for a in res["accounts"]]
        self.assertIn("acc_real", account_names)
        self.assertNotIn(".tmp-rename-123", account_names)


if __name__ == "__main__":
    unittest.main()
