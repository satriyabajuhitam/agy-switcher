#!/usr/bin/env python3
"""Tests for Ticket 03: 5-Layer Self-Healing Diagnostics Core Engine (agy-switch)."""

import base64
import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import time
import unittest

# Ensure scripts directory is in sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from agy_switch import (
    AgySwitchEngine,
    DiagnosticItem,
    DiagnosticReport,
    format_diagnostic_report,
    main,
)


def create_sample_token(
    email: str = "test@example.com",
    expiry_offset_hours: int = 24,
    access_token: str | None = None,
    refresh_token: str | None = None,
) -> dict:
    payload = json.dumps({"email": email, "sub": "12345", "aud": "test_aud"}).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(payload).decode("utf-8").rstrip("=")
    exp_dt = datetime.now(timezone.utc) + timedelta(hours=expiry_offset_hours)
    exp_str = exp_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "token": {
            "access_token": access_token or f"ya29.sample_access_token_for_{email}",
            "token_type": "Bearer",
            "refresh_token": refresh_token or f"1//sample_refresh_token_for_{email}",
            "expiry": exp_str,
        },
        "auth_method": "oauth",
        "id_token": f"header.{payload_b64}.signature",
    }


class TestDiagnosticsCoreEngine(unittest.TestCase):
    def setUp(self):
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

    def tearDown(self):
        self.temp_dir.cleanup()

    def _setup_pristine_environment(self) -> str:
        """Sets up a completely healthy, pristine 5-layer environment."""
        # 1. Accounts in vault
        acc_name = "acc_primary"
        acc_dir = self.vault_dir / "accounts" / acc_name
        acc_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(acc_dir, 0o700)

        token_data = create_sample_token(email="primary@example.com")
        token_path = acc_dir / "token.json"
        token_path.write_text(json.dumps(token_data, indent=2), encoding="utf-8")
        os.chmod(token_path, 0o600)

        # Set active account
        self.engine.set_active_name(acc_name)
        os.chmod(self.engine.active_file, 0o600)

        # Cache file
        self.engine.cache_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.engine.cache_dir, 0o700)
        self.engine.cache_file.write_text("{}", encoding="utf-8")
        os.chmod(self.engine.cache_file, 0o600)

        # 2. Dual destination sync
        desktop_token = self.gemini_dir / "jetski-standalone-oauth-token"
        desktop_token.parent.mkdir(parents=True, exist_ok=True)
        desktop_token.write_text(json.dumps(token_data), encoding="utf-8")
        os.chmod(desktop_token, 0o600)

        cli_token = self.gemini_dir / "antigravity-cli" / "antigravity-oauth-token"
        cli_token.parent.mkdir(parents=True, exist_ok=True)
        cli_token.write_text(json.dumps(token_data, indent=4), encoding="utf-8")
        os.chmod(cli_token, 0o600)

        # 3. Lifecycle hook
        self.engine.install_hook()

        # 4. Mock process: language_server
        ls_pid_dir = self.proc_dir / "101"
        ls_pid_dir.mkdir(parents=True, exist_ok=True)
        (ls_pid_dir / "cmdline").write_bytes(b"/usr/bin/language_server\x00--standalone\x00")
        (ls_pid_dir / "stat").write_text("101 (language_server) S 1 101 101 0 0 0 0 0 0 0 0 0")

        # 5. Mock process: watch daemon
        watch_pid_dir = self.proc_dir / "202"
        watch_pid_dir.mkdir(parents=True, exist_ok=True)
        (watch_pid_dir / "cmdline").write_bytes(b"python3\x00scripts/watch.py\x00run\x00")
        (watch_pid_dir / "stat").write_text("202 (python3) S 1 202 202 0 0 0 0 0 0 0 0 0")

        watch_pid_file = self.vault_dir / "watch.pid"
        watch_pid_file.write_text("202\n", encoding="utf-8")
        os.chmod(watch_pid_file, 0o600)

        # Enforce all permissions
        os.chmod(self.vault_dir, 0o700)
        os.chmod(self.vault_dir / "accounts", 0o700)

        return acc_name

    # ---------------------------------------------------------
    # Dataclasses & Data Structures Tests
    # ---------------------------------------------------------
    def test_diagnostic_item_and_report_dataclasses(self):
        """Validates DiagnosticItem and DiagnosticReport schema and to_dict()."""
        item = DiagnosticItem(
            category="permissions",
            name="Vault File & Directory Permissions",
            status="PASS",
            message="All secure.",
            can_fix=False,
        )
        self.assertFalse(item.fixed)
        self.assertIsNone(item.fix_action)

        item_dict = item.to_dict()
        self.assertEqual(item_dict["category"], "permissions")
        self.assertEqual(item_dict["name"], "Vault File & Directory Permissions")
        self.assertEqual(item_dict["status"], "PASS")
        self.assertEqual(item_dict["message"], "All secure.")
        self.assertFalse(item_dict["can_fix"])
        self.assertFalse(item_dict["fixed"])
        self.assertIsNone(item_dict["fix_action"])

        report = DiagnosticReport(
            items=[item],
            overall_status="PASS",
            exit_code=0,
        )
        rep_dict = report.to_dict()
        self.assertEqual(rep_dict["overall_status"], "PASS")
        self.assertEqual(rep_dict["exit_code"], 0)
        self.assertEqual(rep_dict["summary"]["total"], 1)
        self.assertEqual(rep_dict["summary"]["passed"], 1)
        self.assertEqual(rep_dict["summary"]["warned"], 0)
        self.assertEqual(rep_dict["summary"]["failed"], 0)
        self.assertEqual(len(rep_dict["checks"]), 1)

    def test_ingestion_seam_proc_dir(self):
        """Validates that AgySwitchEngine accepts proc_dir and defaults to /proc."""
        engine_default = AgySwitchEngine(vault_dir=self.vault_dir)
        self.assertEqual(engine_default.proc_dir, Path("/proc"))

        mock_proc = self.base_path / "custom_proc"
        engine_custom = AgySwitchEngine(vault_dir=self.vault_dir, proc_dir=mock_proc)
        self.assertEqual(engine_custom.proc_dir, mock_proc)

    # ---------------------------------------------------------
    # Pristine 5-Layer Integration Test
    # ---------------------------------------------------------
    def test_run_diagnostics_pristine_environment(self):
        """Verifies that all 5 layers PASS under pristine conditions."""
        self._setup_pristine_environment()

        report = self.engine.run_diagnostics()

        self.assertEqual(report.overall_status, "PASS")
        self.assertEqual(report.exit_code, 0)
        self.assertEqual(len(report.items), 5)
        categories = [item.category for item in report.items]
        self.assertEqual(
            categories,
            ["permissions", "token_integrity", "dual_sync", "hooks", "process"],
        )
        for item in report.items:
            self.assertEqual(item.status, "PASS", f"Item {item.category} did not pass: {item.message}")
            self.assertFalse(item.can_fix)

    # ---------------------------------------------------------
    # Layer 1: Permissions
    # ---------------------------------------------------------
    def test_layer1_permissions_leak_detection_and_fix(self):
        """Detects 0777/0666 permission leaks across files/dirs and repairs with fix_diagnostics."""
        self._setup_pristine_environment()

        # Corrupt permissions
        os.chmod(self.vault_dir, 0o777)
        acc_dir = self.vault_dir / "accounts" / "acc_primary"
        os.chmod(acc_dir, 0o755)
        os.chmod(acc_dir / "token.json", 0o666)
        os.chmod(self.engine.active_file, 0o644)
        os.chmod(self.engine.cache_file, 0o666)
        os.chmod(self.vault_dir / "watch.pid", 0o644)

        report = self.engine.run_diagnostics()
        perm_item = next(it for it in report.items if it.category == "permissions")
        self.assertEqual(perm_item.status, "FAIL")
        self.assertTrue(perm_item.can_fix)
        self.assertIn("Insecure permissions", perm_item.message)

        # Fix diagnostics
        fixes = self.engine.fix_diagnostics(report)
        self.assertTrue(len(fixes) > 0)
        self.assertTrue(perm_item.fixed)
        self.assertIsNotNone(perm_item.fix_action)

        # Verify permissions restored
        self.assertEqual(stat.S_IMODE(self.vault_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(acc_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((acc_dir / "token.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.engine.active_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.engine.cache_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.vault_dir / "watch.pid").stat().st_mode), 0o600)

        # Post-fix report should be PASS
        post_report = self.engine.run_diagnostics()
        post_perm = next(it for it in post_report.items if it.category == "permissions")
        self.assertEqual(post_perm.status, "PASS")

    # ---------------------------------------------------------
    # Layer 2: Token & Identity Integrity
    # ---------------------------------------------------------
    def test_layer2_missing_or_corrupt_token_json(self):
        """Detects corrupt/malformed token.json as FAIL and can_fix=False."""
        self._setup_pristine_environment()
        acc_token = self.vault_dir / "accounts" / "acc_primary" / "token.json"
        acc_token.write_text("{not valid json", encoding="utf-8")

        report = self.engine.run_diagnostics()
        token_item = next(it for it in report.items if it.category == "token_integrity")
        self.assertEqual(token_item.status, "FAIL")
        self.assertFalse(token_item.can_fix)
        self.assertIn("corrupt or invalid", token_item.message.lower())

    def test_layer2_missing_schema_keys(self):
        """Detects missing required schema keys (e.g. refresh_token) as FAIL and can_fix=False."""
        self._setup_pristine_environment()
        token_data = create_sample_token()
        del token_data["token"]["refresh_token"]
        acc_token = self.vault_dir / "accounts" / "acc_primary" / "token.json"
        acc_token.write_text(json.dumps(token_data), encoding="utf-8")

        report = self.engine.run_diagnostics()
        token_item = next(it for it in report.items if it.category == "token_integrity")
        self.assertEqual(token_item.status, "FAIL")
        self.assertFalse(token_item.can_fix)

    def test_layer2_corrupt_jwt_id_token(self):
        """Detects unparseable JWT id_token (no email) as FAIL and can_fix=False."""
        self._setup_pristine_environment()
        token_data = create_sample_token()
        token_data["id_token"] = "invalid.jwt.token"
        acc_token = self.vault_dir / "accounts" / "acc_primary" / "token.json"
        acc_token.write_text(json.dumps(token_data), encoding="utf-8")

        report = self.engine.run_diagnostics()
        token_item = next(it for it in report.items if it.category == "token_integrity")
        self.assertEqual(token_item.status, "FAIL")
        self.assertFalse(token_item.can_fix)

    def test_layer2_expired_active_token(self):
        """Expired active account token triggers FAIL with can_fix=False."""
        self._setup_pristine_environment()
        token_data = create_sample_token(expiry_offset_hours=-2)
        acc_token = self.vault_dir / "accounts" / "acc_primary" / "token.json"
        acc_token.write_text(json.dumps(token_data), encoding="utf-8")

        report = self.engine.run_diagnostics()
        token_item = next(it for it in report.items if it.category == "token_integrity")
        self.assertEqual(token_item.status, "FAIL")
        self.assertFalse(token_item.can_fix)
        self.assertIn("expired", token_item.message.lower())

    def test_layer2_expired_secondary_token(self):
        """Expired non-active secondary account token triggers WARN (not FAIL) with can_fix=False."""
        self._setup_pristine_environment()
        # Add secondary account with expired token
        sec_dir = self.vault_dir / "accounts" / "acc_secondary"
        sec_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(sec_dir, 0o700)
        sec_token = create_sample_token(email="sec@example.com", expiry_offset_hours=-2)
        (sec_dir / "token.json").write_text(json.dumps(sec_token), encoding="utf-8")
        os.chmod(sec_dir / "token.json", 0o600)

        report = self.engine.run_diagnostics()
        token_item = next(it for it in report.items if it.category == "token_integrity")
        self.assertEqual(token_item.status, "WARN")
        self.assertFalse(token_item.can_fix)
        self.assertIn("acc_secondary", token_item.message)

    def test_layer2_no_accounts_in_vault(self):
        """Empty vault accounts dir returns WARN with can_fix=False."""
        report = self.engine.run_diagnostics()
        token_item = next(it for it in report.items if it.category == "token_integrity")
        self.assertEqual(token_item.status, "WARN")
        self.assertFalse(token_item.can_fix)

    # ---------------------------------------------------------
    # Layer 3: Semantic Dual-Destination Sync
    # ---------------------------------------------------------
    def test_layer3_semantic_equality_formatting_differences(self):
        """Different whitespace and key ordering between desktop/cli and vault token does NOT trigger failure."""
        self._setup_pristine_environment()
        vault_token = self.vault_dir / "accounts" / "acc_primary" / "token.json"
        data = json.loads(vault_token.read_text(encoding="utf-8"))

        desktop_token = self.gemini_dir / "jetski-standalone-oauth-token"
        cli_token = self.gemini_dir / "antigravity-cli" / "antigravity-oauth-token"

        # Re-order keys and change formatting
        desktop_data = {
            "auth_method": data["auth_method"],
            "id_token": data["id_token"],
            "token": {
                "expiry": data["token"]["expiry"],
                "refresh_token": data["token"]["refresh_token"],
                "token_type": data["token"]["token_type"],
                "access_token": data["token"]["access_token"],
            },
        }
        desktop_token.write_text(json.dumps(desktop_data, indent=8) + "\n\n", encoding="utf-8")
        cli_token.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")

        report = self.engine.run_diagnostics()
        sync_item = next(it for it in report.items if it.category == "dual_sync")
        self.assertEqual(sync_item.status, "PASS")

    def test_layer3_missing_target_file_warn_and_fix(self):
        """Missing desktop or CLI token file triggers WARN (can_fix=True) and fixes safely."""
        self._setup_pristine_environment()
        desktop_token = self.gemini_dir / "jetski-standalone-oauth-token"
        desktop_token.unlink()

        report = self.engine.run_diagnostics()
        sync_item = next(it for it in report.items if it.category == "dual_sync")
        self.assertEqual(sync_item.status, "WARN")
        self.assertTrue(sync_item.can_fix)

        fixes = self.engine.fix_diagnostics(report)
        self.assertTrue(len(fixes) > 0)
        self.assertTrue(desktop_token.is_file())
        self.assertTrue(sync_item.fixed)

    def test_layer3_token_payload_mismatch_and_atomic_fix(self):
        """Mismatched access_token triggers FAIL (can_fix=True) and re-syncs atomically without touching language_server."""
        self._setup_pristine_environment()
        cli_token = self.gemini_dir / "antigravity-cli" / "antigravity-oauth-token"
        bad_token = create_sample_token(access_token="ya29.old_desynced_token")
        cli_token.write_text(json.dumps(bad_token), encoding="utf-8")

        report = self.engine.run_diagnostics()
        sync_item = next(it for it in report.items if it.category == "dual_sync")
        self.assertEqual(sync_item.status, "FAIL")
        self.assertTrue(sync_item.can_fix)

        # Run fix
        fixes = self.engine.fix_diagnostics(report)
        self.assertTrue(len(fixes) > 0)
        self.assertTrue(sync_item.fixed)

        # Verify CLI token updated to match active vault account
        vault_data = json.loads((self.vault_dir / "accounts" / "acc_primary" / "token.json").read_text(encoding="utf-8"))
        fixed_cli_data = json.loads(cli_token.read_text(encoding="utf-8"))
        self.assertEqual(fixed_cli_data["token"]["access_token"], vault_data["token"]["access_token"])

    # ---------------------------------------------------------
    # Layer 4: Lifecycle Hook
    # ---------------------------------------------------------
    def test_layer4_missing_hooks_json(self):
        """Missing hooks.json triggers WARN with can_fix=True and installs hook on fix."""
        self._setup_pristine_environment()
        hooks_file = self.gemini_dir / "config" / "hooks.json"
        hooks_file.unlink()

        report = self.engine.run_diagnostics()
        hook_item = next(it for it in report.items if it.category == "hooks")
        self.assertEqual(hook_item.status, "WARN")
        self.assertTrue(hook_item.can_fix)

        fixes = self.engine.fix_diagnostics(report)
        self.assertTrue(len(fixes) > 0)
        self.assertTrue(hooks_file.is_file())
        self.assertTrue(hook_item.fixed)

    def test_layer4_malformed_0byte_hooks_json_and_backup_fix(self):
        """0-byte hooks.json triggers FAIL (can_fix=True), creates .bak and writes clean hooks.json."""
        self._setup_pristine_environment()
        hooks_file = self.gemini_dir / "config" / "hooks.json"
        hooks_file.write_text("", encoding="utf-8")

        report = self.engine.run_diagnostics()
        hook_item = next(it for it in report.items if it.category == "hooks")
        self.assertEqual(hook_item.status, "FAIL")
        self.assertTrue(hook_item.can_fix)
        self.assertIn("Malformed hooks.json", hook_item.message)

        fixes = self.engine.fix_diagnostics(report)
        self.assertTrue(len(fixes) > 0)
        self.assertTrue(hook_item.fixed)

        # Check .bak file created
        bak_files = list(hooks_file.parent.glob("hooks.json.corrupt.*.bak"))
        self.assertEqual(len(bak_files), 1)

        # Check clean hooks.json has PreInvocation
        data = json.loads(hooks_file.read_text(encoding="utf-8"))
        self.assertIn("agy-switch-auto-rotate", data)

    def test_layer4_corrupted_json_hooks_file(self):
        """Corrupted JSON syntax triggers FAIL and creates .bak on fix."""
        self._setup_pristine_environment()
        hooks_file = self.gemini_dir / "config" / "hooks.json"
        hooks_file.write_text("{ corrupt json: [", encoding="utf-8")

        report = self.engine.run_diagnostics()
        hook_item = next(it for it in report.items if it.category == "hooks")
        self.assertEqual(hook_item.status, "FAIL")
        self.assertTrue(hook_item.can_fix)

        self.engine.fix_diagnostics(report)
        bak_files = list(hooks_file.parent.glob("hooks.json.corrupt.*.bak"))
        self.assertEqual(len(bak_files), 1)
        self.assertEqual(bak_files[0].read_text(encoding="utf-8"), "{ corrupt json: [")

    def test_layer4_hook_missing_preinvocation_entry(self):
        """hooks.json without agy-switch-auto-rotate triggers WARN with can_fix=True."""
        self._setup_pristine_environment()
        hooks_file = self.gemini_dir / "config" / "hooks.json"
        hooks_file.write_text(json.dumps({"other-tool": {"enabled": True}}), encoding="utf-8")

        report = self.engine.run_diagnostics()
        hook_item = next(it for it in report.items if it.category == "hooks")
        self.assertEqual(hook_item.status, "WARN")
        self.assertTrue(hook_item.can_fix)

        self.engine.fix_diagnostics(report)
        data = json.loads(hooks_file.read_text(encoding="utf-8"))
        self.assertIn("agy-switch-auto-rotate", data)
        self.assertIn("other-tool", data)

    # ---------------------------------------------------------
    # Layer 5: Process Health & PID Verification
    # ---------------------------------------------------------
    def test_layer5_language_server_idle_and_busy(self):
        """Detects language_server PID and distinguishes idle vs busy status."""
        self._setup_pristine_environment()

        # Pristine setup has idle language server (PID 101)
        report = self.engine.run_diagnostics()
        proc_item = next(it for it in report.items if it.category == "process")
        self.assertEqual(proc_item.status, "PASS")
        self.assertIn("PID 101", proc_item.message)
        self.assertIn("idle", proc_item.message)

        # Simulate presence lock in ~/.gemini/antigravity/presence
        presence_dir = self.gemini_dir / "antigravity" / "presence"
        presence_dir.mkdir(parents=True, exist_ok=True)
        (presence_dir / "turn.lock").write_text("lock", encoding="utf-8")

        report_busy = self.engine.run_diagnostics()
        proc_busy = next(it for it in report_busy.items if it.category == "process")
        self.assertEqual(proc_busy.status, "PASS")
        self.assertIn("busy", proc_busy.message)

    def test_layer5_language_server_not_running(self):
        """Missing language_server process triggers WARN with can_fix=False."""
        self._setup_pristine_environment()
        # Remove language_server from mock proc_dir
        shutil.rmtree(self.proc_dir / "101")

        report = self.engine.run_diagnostics()
        proc_item = next(it for it in report.items if it.category == "process")
        self.assertEqual(proc_item.status, "WARN")
        self.assertFalse(proc_item.can_fix)
        self.assertIn("language_server not running", proc_item.message.lower())

    def test_layer5_stale_watch_pid_dead_process(self):
        """watch.pid referencing a dead PID is marked WARN with can_fix=True, unlinked on fix."""
        self._setup_pristine_environment()
        # Remove watch process dir 202
        shutil.rmtree(self.proc_dir / "202")

        report = self.engine.run_diagnostics()
        proc_item = next(it for it in report.items if it.category == "process")
        self.assertEqual(proc_item.status, "WARN")
        self.assertTrue(proc_item.can_fix)
        self.assertIn("stale", proc_item.message.lower())

        fixes = self.engine.fix_diagnostics(report)
        self.assertTrue(len(fixes) > 0)
        self.assertFalse((self.vault_dir / "watch.pid").exists())
        self.assertTrue(proc_item.fixed)

    def test_layer5_recycled_watch_pid_unrelated_process(self):
        """watch.pid referencing a recycled PID (unrelated cmdline) is marked stale and unlinked on fix."""
        self._setup_pristine_environment()
        # Change PID 202 cmdline to unrelated binary
        (self.proc_dir / "202" / "cmdline").write_bytes(b"/bin/sleep\x001000\x00")

        report = self.engine.run_diagnostics()
        proc_item = next(it for it in report.items if it.category == "process")
        self.assertEqual(proc_item.status, "WARN")
        self.assertTrue(proc_item.can_fix)
        self.assertIn("stale", proc_item.message.lower())

        self.engine.fix_diagnostics(report)
        self.assertFalse((self.vault_dir / "watch.pid").exists())

    def test_layer2_token_integrity_ignores_dotfiles_and_temp_dirs(self):
        """Hidden directories starting with '.' (like .tmp-rename-*) are ignored in _check_token_integrity."""
        self._setup_pristine_environment()
        tmp_dir = self.vault_dir / "accounts" / ".tmp-rename-12345"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(tmp_dir, 0o700)

        report = self.engine.run_diagnostics()
        token_item = next(it for it in report.items if it.category == "token_integrity")
        self.assertEqual(token_item.status, "PASS")
        self.assertNotIn(".tmp-rename", token_item.message)

    def test_layer4_hook_command_with_shlex_quotes_and_spaces(self):
        """Hook commands with quoted paths containing spaces are safely parsed via shlex."""
        self._setup_pristine_environment()
        custom_dir = self.base_path / "custom path with spaces"
        custom_dir.mkdir(parents=True, exist_ok=True)
        custom_script = custom_dir / "my_hook.py"
        custom_script.write_text("#!/usr/bin/env python3\nprint('hook')\n", encoding="utf-8")
        os.chmod(custom_script, 0o755)

        hooks_file = self.gemini_dir / "config" / "hooks.json"
        hooks_data = {
            "agy-switch-auto-rotate": {
                "enabled": True,
                "PreInvocation": [
                    {
                        "type": "command",
                        "command": f'python3 "{custom_script}" hook-pre-invocation',
                        "timeout": 5,
                    }
                ],
            }
        }
        hooks_file.write_text(json.dumps(hooks_data), encoding="utf-8")

        report = self.engine.run_diagnostics()
        hook_item = next(it for it in report.items if it.category == "hooks")
        self.assertEqual(hook_item.status, "PASS")

    def test_layer4_hook_executable_wrapper_without_py_extension(self):
        """Executable wrapper without .py extension is recognized and validated."""
        self._setup_pristine_environment()
        bin_dir = self.base_path / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        wrapper = bin_dir / "hook-wrapper"
        wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.chmod(wrapper, 0o755)

        hooks_file = self.gemini_dir / "config" / "hooks.json"
        hooks_data = {
            "agy-switch-auto-rotate": {
                "enabled": True,
                "PreInvocation": [
                    {
                        "type": "command",
                        "command": f'"{wrapper}" hook-pre-invocation',
                        "timeout": 5,
                    }
                ],
            }
        }
        hooks_file.write_text(json.dumps(hooks_data), encoding="utf-8")

        report = self.engine.run_diagnostics()
        hook_item = next(it for it in report.items if it.category == "hooks")
        self.assertEqual(hook_item.status, "PASS")

        # Negative test 1: wrapper exists but is not executable (0644)
        os.chmod(wrapper, 0o644)
        rep_unexec = self.engine.run_diagnostics()
        item_unexec = next(it for it in rep_unexec.items if it.category == "hooks")
        self.assertEqual(item_unexec.status, "WARN")
        self.assertIn("not executable", item_unexec.message)

        # Negative test 2: wrapper does not exist
        wrapper.unlink()
        rep_missing = self.engine.run_diagnostics()
        item_missing = next(it for it in rep_missing.items if it.category == "hooks")
        self.assertEqual(item_missing.status, "WARN")
        self.assertIn("does not exist", item_missing.message)

    def test_layer1_chmod_failure_records_error_and_does_not_mark_fixed(self):
        """If os.chmod raises OSError during fix_diagnostics, item.fixed remains False."""
        self._setup_pristine_environment()
        os.chmod(self.vault_dir, 0o777)

        report = self.engine.run_diagnostics()
        perm_item = next(it for it in report.items if it.category == "permissions")
        self.assertEqual(perm_item.status, "FAIL")
        self.assertTrue(perm_item.can_fix)

        def mock_chmod(path, mode):
            raise OSError("Simulated permission denied error")

        from unittest.mock import patch
        with patch("os.chmod", side_effect=mock_chmod):
            fixes = self.engine.fix_diagnostics(report)

        self.assertFalse(perm_item.fixed)
        self.assertNotEqual(perm_item.status, "PASS")
        self.assertIn("Failed", str(perm_item.fix_action))
        self.assertEqual(len(fixes), 0)

    def test_is_valid_expiry_boolean_typeguard(self):
        """Explicitly verifies that boolean values are rejected as valid expiry."""
        self.assertFalse(self.engine._is_valid_expiry(True))
        self.assertFalse(self.engine._is_valid_expiry(False))
        self.assertTrue(self.engine._is_valid_expiry(1700000000))
        self.assertTrue(self.engine._is_valid_expiry("2026-10-14T00:00:00Z"))

    # ---------------------------------------------------------
    # Overall Status & Auto-Healing Integration
    # ---------------------------------------------------------
    def test_overall_status_and_exit_code_calculation(self):
        """Validates overall_status and exit_code calculation for PASS (0), WARN (1), FAIL (2)."""
        self._setup_pristine_environment()

        # 1. Pristine -> PASS (0)
        rep_pass = self.engine.run_diagnostics()
        self.assertEqual(rep_pass.overall_status, "PASS")
        self.assertEqual(rep_pass.exit_code, 0)

        # 2. Only warning (e.g. language server not running) -> WARN (1)
        shutil.rmtree(self.proc_dir / "101")
        rep_warn = self.engine.run_diagnostics()
        self.assertEqual(rep_warn.overall_status, "WARN")
        self.assertEqual(rep_warn.exit_code, 1)

        # 3. Critical failure (e.g. permissions leak) -> FAIL (2)
        os.chmod(self.vault_dir, 0o777)
        rep_fail = self.engine.run_diagnostics()
        self.assertEqual(rep_fail.overall_status, "FAIL")
        self.assertEqual(rep_fail.exit_code, 2)

    def test_fix_diagnostics_with_unfixable_item_retains_status(self):
        """If an unfixable issue exists (e.g. corrupt token), report retains FAIL exit_code 2 after fix."""
        self._setup_pristine_environment()
        # Permissions leak (fixable) + Corrupt token (unfixable)
        os.chmod(self.vault_dir, 0o777)
        acc_token = self.vault_dir / "accounts" / "acc_primary" / "token.json"
        acc_token.write_text("{corrupt", encoding="utf-8")

        report = self.engine.run_diagnostics()
        self.assertEqual(report.overall_status, "FAIL")
        self.assertEqual(report.exit_code, 2)

        fixes = self.engine.fix_diagnostics(report)
        self.assertTrue(len(fixes) > 0)
        # Permissions should be fixed
        self.assertEqual(stat.S_IMODE(self.vault_dir.stat().st_mode), 0o700)
        # Token is still unfixable, overall status must remain FAIL
        self.assertEqual(report.overall_status, "FAIL")
        self.assertEqual(report.exit_code, 2)

    def test_layer5_recycled_watch_pid_with_run_in_cmdline(self):
        """watch.pid referencing a recycled PID running commands like 'cargo run' or 'npm run' must be rejected as stale."""
        self._setup_pristine_environment()
        # Set cmdline of PID 202 to cargo run
        (self.proc_dir / "202" / "cmdline").write_bytes(b"cargo\x00run\x00")

        report = self.engine.run_diagnostics()
        proc_item = next(it for it in report.items if it.category == "process")
        self.assertEqual(proc_item.status, "WARN")
        self.assertTrue(proc_item.can_fix)
        self.assertIn("recycled pid", proc_item.message.lower())

        fixes = self.engine.fix_diagnostics(report)
        self.assertTrue(len(fixes) > 0)
        self.assertFalse((self.vault_dir / "watch.pid").exists())

    def test_layer5_child_process_stat_with_spaces_in_comm(self):
        """is_server_busy handles Linux /proc/<pid>/stat with spaces in comm name (e.g. 'Web Content')."""
        self._setup_pristine_environment()
        # Mock child process 303 whose comm is '(Web Content)' and ppid is 101 (language_server)
        child_dir = self.proc_dir / "303"
        child_dir.mkdir(parents=True, exist_ok=True)
        (child_dir / "cmdline").write_bytes(b"web_content\x00")
        (child_dir / "stat").write_text("303 (Web Content) S 101 303 0 0 0 0 0 0 0 0 0")

        self.assertTrue(self.engine.is_server_busy(101))

    def test_layer4_corrupt_non_dict_hooks_json_backed_up_on_fix(self):
        """hooks.json containing a non-dict JSON array [1, 2, 3] is marked FAIL and backed up to .bak on fix."""
        self._setup_pristine_environment()
        hooks_file = self.gemini_dir / "config" / "hooks.json"
        hooks_file.write_text("[1, 2, 3]", encoding="utf-8")

        report = self.engine.run_diagnostics()
        hook_item = next(it for it in report.items if it.category == "hooks")
        self.assertEqual(hook_item.status, "FAIL")
        self.assertTrue(hook_item.can_fix)

        fixes = self.engine.fix_diagnostics(report)
        self.assertTrue(len(fixes) > 0)
        bak_files = list(hooks_file.parent.glob("hooks.json.corrupt.*.bak"))
        self.assertEqual(len(bak_files), 1)
        self.assertEqual(bak_files[0].read_text(encoding="utf-8"), "[1, 2, 3]")

    def test_layer4_invalid_preinv_command_definition(self):
        """PreInvocation entry in hooks.json that is not a dict or has empty command is flagged WARN."""
        self._setup_pristine_environment()
        hooks_file = self.gemini_dir / "config" / "hooks.json"
        hooks_file.write_text(json.dumps({
            "agy-switch-auto-rotate": {
                "enabled": True,
                "PreInvocation": ["not-a-dict"],
            }
        }), encoding="utf-8")

        report = self.engine.run_diagnostics()
        hook_item = next(it for it in report.items if it.category == "hooks")
        self.assertEqual(hook_item.status, "WARN")
        self.assertTrue(hook_item.can_fix)

        fixes = self.engine.fix_diagnostics(report)
        self.assertTrue(len(fixes) > 0)
        self.assertTrue(hook_item.fixed)

    def test_layer3_malformed_non_dict_json_tokens(self):
        """Desktop or CLI token containing non-dict JSON (e.g. [1, 2]) is marked FAIL without AttributeError and fixed."""
        self._setup_pristine_environment()
        desktop_token = self.gemini_dir / "jetski-standalone-oauth-token"
        desktop_token.write_text("[1, 2, 3]", encoding="utf-8")

        report = self.engine.run_diagnostics()
        sync_item = next(it for it in report.items if it.category == "dual_sync")
        self.assertEqual(sync_item.status, "FAIL")
        self.assertTrue(sync_item.can_fix)

        fixes = self.engine.fix_diagnostics(report)
        self.assertTrue(len(fixes) > 0)
        fixed_data = json.loads(desktop_token.read_text(encoding="utf-8"))
        self.assertIsInstance(fixed_data, dict)

    def test_layer3_active_token_missing_credentials_fails(self):
        """Active vault account token missing access_token or refresh_token is marked FAIL with can_fix=False."""
        self._setup_pristine_environment()
        token_path = self.vault_dir / "accounts" / "acc_primary" / "token.json"
        token_path.write_text(json.dumps({"token": {}, "auth_method": "oauth"}), encoding="utf-8")

        report = self.engine.run_diagnostics()
        sync_item = next(it for it in report.items if it.category == "dual_sync")
        self.assertEqual(sync_item.status, "FAIL")
        self.assertFalse(sync_item.can_fix)

    def test_layer1_missing_vault_directory(self):
        """Missing vault directory is flagged as FAIL with can_fix=True and restored by fix_diagnostics."""
        shutil.rmtree(self.vault_dir)

        report = self.engine.run_diagnostics()
        perm_item = next(it for it in report.items if it.category == "permissions")
        self.assertEqual(perm_item.status, "FAIL")
        self.assertTrue(perm_item.can_fix)

        fixes = self.engine.fix_diagnostics(report)
        self.assertTrue(len(fixes) > 0)
        self.assertTrue(self.vault_dir.is_dir())
        self.assertEqual(stat.S_IMODE(self.vault_dir.stat().st_mode), 0o700)

    def test_layer2_invalid_unparseable_expiry(self):
        """Token with unparseable garbage expiry is marked FAIL (can_fix=False)."""
        self._setup_pristine_environment()
        token_data = create_sample_token()
        token_data["token"]["expiry"] = "garbage_not_a_date_or_timestamp"
        token_path = self.vault_dir / "accounts" / "acc_primary" / "token.json"
        token_path.write_text(json.dumps(token_data), encoding="utf-8")

        report = self.engine.run_diagnostics()
        token_item = next(it for it in report.items if it.category == "token_integrity")
        self.assertEqual(token_item.status, "FAIL")
        self.assertFalse(token_item.can_fix)

    def test_layer5_broken_symlink_watch_pid(self):
        """watch.pid that is a broken symlink is detected as stale (can_fix=True) and unlinked on fix."""
        self._setup_pristine_environment()
        watch_pid_file = self.vault_dir / "watch.pid"
        watch_pid_file.unlink()
        watch_pid_file.symlink_to(self.base_path / "nonexistent_target")

        report = self.engine.run_diagnostics()
        proc_item = next(it for it in report.items if it.category == "process")
        self.assertEqual(proc_item.status, "WARN")
        self.assertTrue(proc_item.can_fix)

        fixes = self.engine.fix_diagnostics(report)
        self.assertTrue(len(fixes) > 0)
        self.assertFalse(watch_pid_file.exists() or watch_pid_file.is_symlink())

    def test_post_fix_to_dict_summary_consistency(self):
        """After fix_diagnostics repairs items, report.to_dict() summary is consistent with overall_status PASS."""
        self._setup_pristine_environment()
        # Corrupt permissions
        os.chmod(self.vault_dir, 0o777)

        report = self.engine.run_diagnostics()
        self.assertEqual(report.overall_status, "FAIL")
        self.assertEqual(report.exit_code, 2)

        self.engine.fix_diagnostics(report)
        rep_dict = report.to_dict()
        self.assertEqual(rep_dict["overall_status"], "PASS")
        self.assertEqual(rep_dict["exit_code"], 0)
        self.assertEqual(rep_dict["summary"]["failed"], 0)
        self.assertEqual(rep_dict["summary"]["warned"], 0)
        self.assertEqual(rep_dict["summary"]["passed"], rep_dict["summary"]["total"])



class TestDoctorCLIIntegration(unittest.TestCase):
    def setUp(self):
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

    def tearDown(self):
        self.temp_dir.cleanup()

    def _setup_pristine_environment(self) -> str:
        acc_name = "acc_primary"
        acc_dir = self.vault_dir / "accounts" / acc_name
        acc_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(acc_dir, 0o700)

        token_data = create_sample_token(email="primary@example.com")
        token_path = acc_dir / "token.json"
        token_path.write_text(json.dumps(token_data, indent=2), encoding="utf-8")
        os.chmod(token_path, 0o600)

        self.engine.set_active_name(acc_name)
        os.chmod(self.engine.active_file, 0o600)

        self.engine.cache_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.engine.cache_dir, 0o700)
        self.engine.cache_file.write_text("{}", encoding="utf-8")
        os.chmod(self.engine.cache_file, 0o600)

        desktop_token = self.gemini_dir / "jetski-standalone-oauth-token"
        desktop_token.parent.mkdir(parents=True, exist_ok=True)
        desktop_token.write_text(json.dumps(token_data), encoding="utf-8")
        os.chmod(desktop_token, 0o600)

        cli_token = self.gemini_dir / "antigravity-cli" / "antigravity-oauth-token"
        cli_token.parent.mkdir(parents=True, exist_ok=True)
        cli_token.write_text(json.dumps(token_data, indent=4), encoding="utf-8")
        os.chmod(cli_token, 0o600)

        self.engine.install_hook()

        ls_pid_dir = self.proc_dir / "101"
        ls_pid_dir.mkdir(parents=True, exist_ok=True)
        (ls_pid_dir / "cmdline").write_bytes(b"/usr/bin/language_server\x00--standalone\x00")
        (ls_pid_dir / "stat").write_text("101 (language_server) S 1 101 101 0 0 0 0 0 0 0 0 0")

        watch_pid_dir = self.proc_dir / "202"
        watch_pid_dir.mkdir(parents=True, exist_ok=True)
        (watch_pid_dir / "cmdline").write_bytes(b"python3\x00scripts/watch.py\x00run\x00")
        (watch_pid_dir / "stat").write_text("202 (python3) S 1 202 202 0 0 0 0 0 0 0 0 0")

        watch_pid_file = self.vault_dir / "watch.pid"
        watch_pid_file.write_text("202\n", encoding="utf-8")
        os.chmod(watch_pid_file, 0o600)

        os.chmod(self.vault_dir, 0o700)
        os.chmod(self.vault_dir / "accounts", 0o700)

        return acc_name

    def test_cli_doctor_registered_with_flags(self):
        """Verifies subcommand 'doctor' is registered and accepts --fix and --json flags."""
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                main(["doctor", "--help"], engine=self.engine)
            except SystemExit:
                pass
        help_text = stdout.getvalue()
        self.assertIn("--fix", help_text)
        self.assertIn("--json", help_text)

    def test_cli_doctor_pristine_terminal_listicle(self):
        """Verifies pristine environment returns exit code 0 and renders numbered listicle with [PASS]."""
        self._setup_pristine_environment()
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["doctor"], engine=self.engine)

        self.assertEqual(code, 0)
        out = stdout.getvalue()
        self.assertIn("1. [PASS]", out)
        self.assertIn("2. [PASS]", out)
        self.assertIn("3. [PASS]", out)
        self.assertIn("4. [PASS]", out)
        self.assertIn("5. [PASS]", out)
        self.assertIn("Diagnostic Summary", out)
        self.assertIn("PASS", out)
        self.assertEqual(stderr.getvalue(), "")

    def test_cli_doctor_pure_json_stdout_pristine(self):
        """Verifies pure JSON stdout guarantee: valid JSON conforming to schema on stdout, exit code 0."""
        self._setup_pristine_environment()
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["doctor", "--json"], engine=self.engine)

        self.assertEqual(code, 0)
        out = stdout.getvalue().strip()
        data = json.loads(out)
        self.assertEqual(data["overall_status"], "PASS")
        self.assertEqual(data["exit_code"], 0)
        self.assertEqual(data["summary"]["total"], 5)
        self.assertEqual(data["summary"]["passed"], 5)
        self.assertEqual(data["summary"]["warned"], 0)
        self.assertEqual(data["summary"]["failed"], 0)
        self.assertEqual(len(data["checks"]), 5)
        self.assertEqual(stderr.getvalue(), "")

    def test_cli_doctor_exit_code_warn(self):
        """Verifies non-critical warning (e.g. language_server stopped) exits with code 1."""
        self._setup_pristine_environment()
        shutil.rmtree(self.proc_dir / "101")

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["doctor"], engine=self.engine)

        self.assertEqual(code, 1)
        out = stdout.getvalue()
        self.assertIn("[WARN]", out)
        self.assertIn("WARN", out)

    def test_cli_doctor_exit_code_fail(self):
        """Verifies critical failure (e.g. permissions leak) exits with code 2."""
        self._setup_pristine_environment()
        os.chmod(self.vault_dir, 0o777)

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["doctor"], engine=self.engine)

        self.assertEqual(code, 2)
        out = stdout.getvalue()
        self.assertIn("[FAIL]", out)
        self.assertIn("FAIL", out)

    def test_cli_doctor_fix_workflow_terminal(self):
        """Verifies --fix workflow: runs repairs, reports applied fixes, re-runs diagnostics, exits with code 0."""
        self._setup_pristine_environment()
        os.chmod(self.vault_dir, 0o777)
        acc_dir = self.vault_dir / "accounts" / "acc_primary"
        os.chmod(acc_dir, 0o755)

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["doctor", "--fix"], engine=self.engine)

        self.assertEqual(code, 0)
        out = stdout.getvalue()
        self.assertIn("Applied", out)
        self.assertIn("Restored POSIX permissions", out)
        self.assertIn("[PASS]", out)
        self.assertEqual(stat.S_IMODE(self.vault_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(acc_dir.stat().st_mode), 0o700)

    def test_cli_doctor_fix_workflow_json(self):
        """Verifies --fix with --json emits pure JSON on stdout with post-repair status and exit code 0."""
        self._setup_pristine_environment()
        os.chmod(self.vault_dir, 0o777)

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["doctor", "--fix", "--json"], engine=self.engine)

        self.assertEqual(code, 0)
        out = stdout.getvalue().strip()
        data = json.loads(out)
        self.assertEqual(data["overall_status"], "PASS")
        self.assertEqual(data["exit_code"], 0)
        self.assertEqual(data["summary"]["passed"], 5)
        self.assertEqual(stat.S_IMODE(self.vault_dir.stat().st_mode), 0o700)

    def test_cli_doctor_unfixable_item_notifies_login(self):
        """Verifies unfixable items (corrupt token) explicitly notify user to execute agy-switch login <alias>."""
        self._setup_pristine_environment()
        token_path = self.vault_dir / "accounts" / "acc_primary" / "token.json"
        token_path.write_text("{bad json", encoding="utf-8")

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["doctor"], engine=self.engine)

        self.assertEqual(code, 2)
        out = stdout.getvalue()
        self.assertIn("[FAIL]", out)
        self.assertIn("agy-switch login", out)

        stdout_fix = io.StringIO()
        stderr_fix = io.StringIO()
        with contextlib.redirect_stdout(stdout_fix), contextlib.redirect_stderr(stderr_fix):
            code_fix = main(["doctor", "--fix"], engine=self.engine)

        self.assertEqual(code_fix, 2)
        out_fix = stdout_fix.getvalue()
        self.assertIn("agy-switch login", out_fix)

    def test_cli_doctor_pure_json_stdout_isolation_with_auto_migration(self):
        """Verifies auto-migration notices are routed to stderr when --json is passed to doctor."""
        legacy_acc = self.legacy_root / "accounts" / "legacy_user"
        legacy_token_dir = legacy_acc / ".gemini" / "antigravity-cli"
        legacy_token_dir.mkdir(parents=True, exist_ok=True)
        leg_tok = create_sample_token(email="legacy@example.com")
        (legacy_token_dir / "antigravity-oauth-token").write_text(json.dumps(leg_tok), encoding="utf-8")
        (self.legacy_root / "active").write_text("legacy_user\n", encoding="utf-8")

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["doctor", "--json"], engine=self.engine)

        self.assertIn(code, (0, 1, 2))
        out = stdout.getvalue().strip()
        data = json.loads(out)
        self.assertIsInstance(data, dict)
        err = stderr.getvalue()
        self.assertIn("Auto-migrated", err)

    def test_process_health_ignores_unrelated_watch_command(self):
        """Unrelated processes with 'watch' and 'run' in cmdline (like 'npm run watch') do not report active watcher."""
        self._setup_pristine_environment()
        watch_pid_dir = self.proc_dir / "202"
        # Overwrite cmdline with third-party command
        (watch_pid_dir / "cmdline").write_bytes(b"node\x00/usr/bin/npm\x00run\x00watch\x00")
        (watch_pid_dir / "stat").write_text("202 (npm) S 1 202 202 0 0 0 0 0 0 0 0 0")

        report = self.engine.run_diagnostics()
        proc_item = next(it for it in report.items if it.category == "process")
        self.assertEqual(proc_item.status, "WARN")
        self.assertTrue(proc_item.can_fix)
        self.assertIn("recycled PID", proc_item.message)
        self.assertNotIn("watcher active", proc_item.message)

    def test_is_server_busy_ignores_zombie_processes(self):
        """Defunct zombie child processes (state == 'Z') are ignored in is_server_busy."""
        self._setup_pristine_environment()
        # Add zombie child process of language_server (PID 101)
        zombie_pid_dir = self.proc_dir / "303"
        zombie_pid_dir.mkdir(parents=True, exist_ok=True)
        # stat format: pid (comm) state ppid ...
        (zombie_pid_dir / "stat").write_text("303 (defunct_worker) Z 101 303 0 0 0 0 0 0 0 0 0")

        # language_server has only zombie child -> busy must be False
        self.assertFalse(self.engine.is_server_busy(101))

        # Add active child process
        active_pid_dir = self.proc_dir / "304"
        active_pid_dir.mkdir(parents=True, exist_ok=True)
        (active_pid_dir / "stat").write_text("304 (active_worker) S 101 304 0 0 0 0 0 0 0 0 0")

        # Now busy must be True
        self.assertTrue(self.engine.is_server_busy(101))


if __name__ == "__main__":
    unittest.main()
