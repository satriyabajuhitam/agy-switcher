#!/usr/bin/env python3
"""Comprehensive unit tests for Layer 2 Vault Engine and Layer 3 PreInvocation Hook modularization."""

import io
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from agy_switcher.exceptions import (
    AccountNotFoundError,
    ActiveTurnRunningError,
    AgySwitchError,
    InvalidTokenError,
)
from agy_switcher.doctor import DoctorEngine
from agy_switcher.supervisor import ProcessSupervisor
from agy_switcher.vault import (
    AgySwitchEngine,
    _atomic_write_file,
    _atomic_write_json,
    extract_email_from_jwt,
    validate_token_dict,
    validate_token_file,
)
from agy_switcher.hook import (
    handle_pre_invocation_hook,
    _is_stale_exhausted_cache,
    _resolve_symbol,
    _get_telemetry_fetchers,
    _get_quota_client_class,
    _get_rotation_engine_class,
)


def create_sample_token(email: str = "test@example.com") -> dict:
    import base64
    payload = json.dumps({"email": email, "sub": "12345", "aud": "test_aud"}).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(payload).decode("utf-8").rstrip("=")
    return {
        "token": {
            "access_token": f"ya29.sample_access_token_for_{email}",
            "token_type": "Bearer",
            "refresh_token": f"1//sample_refresh_token_for_{email}",
            "expiry": "2026-10-30T12:00:00Z",
        },
        "auth_method": "oauth",
        "id_token": f"header.{payload_b64}.signature",
    }


class TestVaultEngineModular(unittest.TestCase):
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

    def test_unified_facade_composition(self):
        """AgySwitchEngine must compose ProcessSupervisor and DoctorEngine."""
        self.assertIsInstance(self.engine.supervisor, ProcessSupervisor)
        self.assertIsInstance(self.engine.doctor, DoctorEngine)
        self.assertEqual(self.engine.supervisor.proc_dir, self.proc_dir)
        self.assertEqual(self.engine.supervisor.gemini_dir, self.gemini_dir)

    def test_posix_permissions_enforced(self):
        """Vault directories must have 0700 permissions and token files 0600."""
        self.assertEqual(stat.S_IMODE(self.vault_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.engine.accounts_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.engine.cache_dir.stat().st_mode), 0o700)

        tok_file = self.base_path / "test_tok.json"
        _atomic_write_json(tok_file, create_sample_token())
        self.assertEqual(stat.S_IMODE(tok_file.stat().st_mode), 0o600)

    def test_dual_destination_atomic_swap(self):
        """switch_tokens and switch_account must write atomically to desktop and cli token files with 0600."""
        # Add account
        tok_data = create_sample_token("user1@example.com")
        acc_dir = self.engine.accounts_dir / "user1"
        acc_dir.mkdir(parents=True, exist_ok=True)
        (acc_dir / "token.json").write_text(json.dumps(tok_data), encoding="utf-8")

        # Mock supervisor restart to avoid real process signals
        with patch.object(self.engine.supervisor, "restart_language_server", return_value=True):
            res = self.engine.switch_tokens("user1", restart_server=True)

        self.assertEqual(res["active"], "user1")
        self.assertEqual(res["email"], "user1@example.com")
        self.assertTrue(res["server_restarted"])

        desktop_file = self.engine.desktop_token_file
        cli_file = self.engine.cli_token_file
        self.assertTrue(desktop_file.is_file())
        self.assertTrue(cli_file.is_file())
        self.assertEqual(stat.S_IMODE(desktop_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(cli_file.stat().st_mode), 0o600)

        # Also test switch_account alias
        with patch.object(self.engine.supervisor, "restart_language_server", return_value=False):
            res_alias = self.engine.switch_account("user1", no_restart=True)
        self.assertEqual(res_alias["active"], "user1")
        self.assertFalse(res_alias["server_restarted"])

    def test_transparent_delegation_supervisor_and_doctor(self):
        """Engine must delegate supervisor and doctor methods transparently."""
        with patch.object(self.engine.supervisor, "locate_language_server_pid", return_value=4242):
            self.assertEqual(self.engine.locate_language_server_pid(), 4242)

        with patch.object(self.engine.supervisor, "is_server_busy", return_value=True):
            self.assertTrue(self.engine.is_server_busy(4242))

        with patch.object(self.engine.doctor, "run_diagnostics") as mock_diag:
            mock_diag.return_value = "report_mock"
            self.assertEqual(self.engine.run_diagnostics(), "report_mock")
            self.assertEqual(self.engine.run_doctor(), "report_mock")

        with patch.object(self.engine.doctor, "fix_diagnostics") as mock_fix:
            mock_fix.return_value = ["fixed_something"]
            self.assertEqual(self.engine.fix_diagnostics("report"), ["fixed_something"])
            self.assertEqual(self.engine.auto_heal("report"), ["fixed_something"])

        # Expiry checks
        self.assertFalse(self.engine._is_valid_expiry(True))
        self.assertFalse(self.engine._is_valid_expiry(False))
        self.assertTrue(self.engine._is_valid_expiry(1700000000))
        self.assertTrue(self.engine._is_valid_expiry("2026-10-15T00:00:00Z"))

    def test_install_hook_points_to_agy_switch_launcher(self):
        """install_hook must point to scripts/agy_switch.py launcher."""
        hooks_file = self.gemini_dir / "config" / "hooks.json"
        res_path = self.engine.install_hook(hooks_file=hooks_file)
        self.assertEqual(res_path, hooks_file)
        self.assertTrue(hooks_file.is_file())
        data = json.loads(hooks_file.read_text(encoding="utf-8"))
        self.assertIn("agy-switch-auto-rotate", data)
        cmd = data["agy-switch-auto-rotate"]["PreInvocation"][0]["command"]
        self.assertIn("agy_switch.py", cmd)
        self.assertIn("hook-pre-invocation", cmd)


class TestHookLifecycleModular(unittest.TestCase):
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

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_hook_auto_switch_disabled_returns_empty_json(self):
        """When auto-switch is disabled, hook must output {} and return 0."""
        self.engine.set_auto_switch(False)
        out = io.StringIO()
        with patch("sys.stdin", io.StringIO('{"modelName": "gemini-3.8-pro"}')):
            with patch("sys.stdout", out):
                ret = handle_pre_invocation_hook(self.engine)
        self.assertEqual(ret, 0)
        self.assertEqual(json.loads(out.getvalue()), {})

    def test_hook_dynamic_proxy_bridge_resolves_mock(self):
        """Dynamic Module Proxy Bridge must resolve mocks on agy_switch if patched."""
        import agy_switch
        self.engine.set_auto_switch(True)
        acc_dir = self.engine.accounts_dir / "acc1"
        acc_dir.mkdir(parents=True, exist_ok=True)
        (acc_dir / "token.json").write_text(json.dumps(create_sample_token("acc1@example.com")), encoding="utf-8")
        self.engine.set_active_name("acc1")

        mock_fetch = MagicMock(return_value=("acc1", {"gemini": {"quota": 80}}))
        with patch.object(agy_switch, "fetch_account_quota", mock_fetch):
            out = io.StringIO()
            with patch("sys.stdin", io.StringIO('{"modelName": "gemini-3.8-pro"}')):
                with patch("sys.stdout", out):
                    ret = handle_pre_invocation_hook(self.engine)
            self.assertEqual(ret, 0)
            mock_fetch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
