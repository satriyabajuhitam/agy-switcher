"""Integration tests for Triad Failover and Disk-Only Swap Contract.

Validates the In-Process SDK Facade (scripts/agy_switcher/api.py) under failover conditions
originating from agent gateways (e.g. agy-telemote TurnRunner) adhering to ADR-0014:
- [FAILOVER-DET-004]: Safe soft-import of agy_switcher.api and auto_rotate_on_exhaustion invocation.
- [FAILOVER-DET-005]: Disk-Only Swap execution with reload_server=False preserving desktop processes and presence locks.

Contracts & Specifications:
- .scratch/triad-failover-integration/spec.md#L81-L97
- .scratch/triad-failover-integration/issues/01-sdk-failover-contract.md
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

# Guarantee scripts directory is in sys.path for in-process SDK dynamic imports
REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = str(REPO_ROOT / "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from agy_switcher.models import RotationResult
from agy_switcher.vault import AgySwitchEngine
import agy_switcher.api as switcher_api


def create_mock_oauth_token(email: str, access_token: str | None = None) -> dict[str, Any]:
    """Generates a valid mock OAuth token structure with base64 JWT id_token payload."""
    payload = json.dumps({"email": email, "sub": f"sub-{email}", "aud": "antigravity-test"}).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(payload).decode("utf-8").rstrip("=")
    token_str = access_token or f"ya29.mock-token-for-{email}"
    return {
        "token": {
            "access_token": token_str,
            "token_type": "Bearer",
            "refresh_token": f"1//refresh-{email}",
            "expiry": "2026-12-31T23:59:59Z",
        },
        "auth_method": "oauth",
        "id_token": f"header.{payload_b64}.signature",
    }


class TestSdkFailoverContract(unittest.TestCase):
    """Test suite validating the SDK failover contract and pure Disk-Only Swap behavior."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name)

        self.vault_dir = self.base_dir / ".antigravity-vault"
        self.gemini_dir = self.base_dir / ".gemini"
        self.legacy_root = self.base_dir / ".agy-cli-manager"
        self.proc_dir = self.base_dir / "proc"

        self.vault_dir.mkdir(parents=True, exist_ok=True)
        self.gemini_dir.mkdir(parents=True, exist_ok=True)
        self.proc_dir.mkdir(parents=True, exist_ok=True)

        self.engine = AgySwitchEngine(
            vault_dir=self.vault_dir,
            gemini_dir=self.gemini_dir,
            legacy_root=self.legacy_root,
            proc_dir=self.proc_dir,
        )

        # Scaffolding accounts
        self.primary_token_data = create_mock_oauth_token("primary@example.com", "ya29.primary-token-val")
        self.standby_token_data = create_mock_oauth_token("standby@example.com", "ya29.standby-token-val")

        self._setup_account("primary-acc", self.primary_token_data)
        self._setup_account("standby-acc", self.standby_token_data)
        self.engine.set_active_name("primary-acc")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _setup_account(self, name: str, token_data: dict[str, Any]) -> None:
        acc_dir = self.engine.accounts_dir / name
        acc_dir.mkdir(parents=True, exist_ok=True)
        (acc_dir / "token.json").write_text(json.dumps(token_data), encoding="utf-8")

    def _setup_simulated_process(
        self, pid: int, cmdline: bytes, linked_locks: list[Path] | None = None
    ) -> Path:
        """Sets up a realistic simulated process entry in proc_dir with open file descriptors."""
        pid_dir = self.proc_dir / str(pid)
        pid_dir.mkdir(parents=True, exist_ok=True)
        (pid_dir / "cmdline").write_bytes(cmdline)
        fd_dir = pid_dir / "fd"
        fd_dir.mkdir(parents=True, exist_ok=True)
        if linked_locks:
            for idx, lock_path in enumerate(linked_locks):
                fd_link = fd_dir / str(idx + 10)
                if not fd_link.exists():
                    os.symlink(lock_path, fd_link)
        return pid_dir

    def test_failover_det_004_safe_soft_import_and_rotation_success(self) -> None:
        """[FAILOVER-DET-004] TurnRunner soft-imports agy_switcher.api and invokes auto_rotate_on_exhaustion."""
        # Active account is exhausted (quota = 0), standby account has available quota (quota = 90)
        cache_data = {
            "timestamp": 1700000000.0,
            "accounts": {
                "primary-acc": {"gemini": {"quota": 0, "reset_time": 1700003600.0}},
                "standby-acc": {"gemini": {"quota": 90, "reset_time": 1700003600.0}},
            },
        }
        self.engine.cache_file.write_text(json.dumps(cache_data), encoding="utf-8")

        # Simulate TurnRunner catching quota exhaustion: soft-importing agy_switcher.api
        try:
            import agy_switcher.api as imported_api
            api_available = True
        except ImportError:
            imported_api = None  # type: ignore[assignment]
            api_available = False

        self.assertTrue(api_available, "Dynamic soft-import of agy_switcher.api must succeed")
        self.assertIsNotNone(imported_api)

        # Invoke auto_rotate_on_exhaustion with model family and reload_server=False
        result: RotationResult = imported_api.auto_rotate_on_exhaustion(
            model="gemini",
            reload_server=False,
            engine=self.engine,
        )

        self.assertIsInstance(result, RotationResult)
        self.assertTrue(result.success, f"Rotation expected to succeed, got: {result.message}")
        self.assertEqual(result.previous_account, "primary-acc")
        self.assertEqual(result.new_account, "standby-acc")
        self.assertEqual(result.active_account, "standby-acc")
        self.assertEqual(result.model, "gemini")
        self.assertFalse(result.reloaded_server)
        self.assertEqual(result.quota_remaining, 0.9)

        # Verify active pointer in vault changed
        self.assertEqual(self.engine.get_active_name(), "standby-acc")

        # Verify dual-destination atomic write: both CLI and Desktop token files updated
        cli_token_payload = json.loads(self.engine.cli_token_file.read_text(encoding="utf-8"))
        self.assertEqual(
            cli_token_payload["token"]["access_token"],
            self.standby_token_data["token"]["access_token"],
        )
        self.assertEqual(self.engine.cli_token_file.stat().st_mode & 0o777, 0o600)

        desktop_token_payload = json.loads(self.engine.desktop_token_file.read_text(encoding="utf-8"))
        self.assertEqual(
            desktop_token_payload["token"]["access_token"],
            self.standby_token_data["token"]["access_token"],
        )
        self.assertEqual(self.engine.desktop_token_file.stat().st_mode & 0o777, 0o600)

    def test_failover_det_004_granular_slug_gemini_flash(self) -> None:
        """[FAILOVER-DET-004] TurnRunner passes granular model slug 'gemini-3.8-flash' directly."""
        cache_data = {
            "timestamp": 1700000000.0,
            "accounts": {
                "primary-acc": {"gemini": {"quota": 0, "reset_time": 1700003600.0}},
                "standby-acc": {"gemini": {"quota": 88, "reset_time": 1700003600.0}},
            },
        }
        self.engine.cache_file.write_text(json.dumps(cache_data), encoding="utf-8")

        result = switcher_api.auto_rotate_on_exhaustion(
            model="gemini-3.8-flash",
            reload_server=False,
            engine=self.engine,
        )

        self.assertTrue(result.success)
        self.assertEqual(result.previous_account, "primary-acc")
        self.assertEqual(result.new_account, "standby-acc")
        self.assertEqual(result.quota_remaining, 0.88)
        self.assertFalse(result.reloaded_server)
        self.assertEqual(self.engine.get_active_name(), "standby-acc")

    def test_failover_det_005_disk_only_swap_preserves_desktop_processes_and_locks(self) -> None:
        """[FAILOVER-DET-005] Disk-Only Swap with reload_server=False preserves processes and presence locks."""
        # Presence locks active on desktop and CLI presence paths
        presence_dir = self.gemini_dir / "antigravity" / "presence"
        presence_dir.mkdir(parents=True, exist_ok=True)
        desktop_presence_lock = presence_dir / "active-turn.lock"
        desktop_lock_content = "pid: 45000, turn: turn-active-claude\n"
        desktop_presence_lock.write_text(desktop_lock_content, encoding="utf-8")

        cli_presence_dir = self.gemini_dir / "antigravity-cli" / "presence"
        cli_presence_dir.mkdir(parents=True, exist_ok=True)
        cli_presence_lock = cli_presence_dir / "cli-turn.lock"
        cli_lock_content = "pid: 45001, session: test-cli-session\n"
        cli_presence_lock.write_text(cli_lock_content, encoding="utf-8")

        # Simulate genuine running language_server process in proc_dir holding open fd to presence lock
        server_pid = 45000
        server_cmdline = b"/opt/antigravity/bin/language_server\x00--standalone\x00"
        server_proc_dir = self._setup_simulated_process(
            pid=server_pid,
            cmdline=server_cmdline,
            linked_locks=[desktop_presence_lock],
        )

        # Simulate CLI TurnRunner process in proc_dir holding open fd to CLI lock
        cli_pid = 45001
        cli_proc_dir = self._setup_simulated_process(
            pid=cli_pid,
            cmdline=b"antigravity-cli\x00turn-runner\x00",
            linked_locks=[cli_presence_lock],
        )

        # Telemetry: primary exhausted for claude, standby healthy
        cache_data = {
            "timestamp": 1700000000.0,
            "accounts": {
                "primary-acc": {"claude": {"quota": 0, "reset_time": 1700003600.0}},
                "standby-acc": {"claude": {"quota": 85, "reset_time": 1700003600.0}},
            },
        }
        self.engine.cache_file.write_text(json.dumps(cache_data), encoding="utf-8")

        # Verify supervisor detects active running process and presence lock before invocation
        self.assertEqual(self.engine.locate_language_server_pid(), server_pid)
        self.assertTrue(self.engine.is_server_busy(server_pid))

        # Spy on supervisor lifecycle and system signal calls to assert absolute non-invocation
        with patch.object(self.engine, "restart_language_server") as mock_engine_restart, \
             patch.object(self.engine.supervisor, "restart_language_server") as mock_super_restart, \
             patch.object(self.engine.supervisor, "_send_signal") as mock_send_signal, \
             patch.object(self.engine.supervisor, "verify_turn_safety") as mock_turn_safety, \
             patch("os.kill") as mock_os_kill:

            result = switcher_api.auto_rotate_on_exhaustion(
                model="claude",
                reload_server=False,
                engine=self.engine,
            )

            # Assert process supervisor hooks and OS signal dispatch were strictly bypassed
            mock_engine_restart.assert_not_called()
            mock_super_restart.assert_not_called()
            mock_send_signal.assert_not_called()
            mock_turn_safety.assert_not_called()
            mock_os_kill.assert_not_called()

        self.assertTrue(result.success)
        self.assertEqual(result.new_account, "standby-acc")
        self.assertFalse(result.reloaded_server)
        self.assertEqual(result.quota_remaining, 0.85)

        # Assert CLI token file was atomically swapped to standby credentials
        cli_token_payload = json.loads(self.engine.cli_token_file.read_text(encoding="utf-8"))
        self.assertEqual(
            cli_token_payload["token"]["access_token"],
            self.standby_token_data["token"]["access_token"],
        )
        self.assertEqual(self.engine.cli_token_file.stat().st_mode & 0o777, 0o600)

        # Assert Desktop token file was also atomically updated (dual-destination contract)
        desktop_token_payload = json.loads(self.engine.desktop_token_file.read_text(encoding="utf-8"))
        self.assertEqual(
            desktop_token_payload["token"]["access_token"],
            self.standby_token_data["token"]["access_token"],
        )
        self.assertEqual(self.engine.desktop_token_file.stat().st_mode & 0o777, 0o600)

        # Assert presence lock files were completely preserved and unaltered
        self.assertTrue(desktop_presence_lock.is_file())
        self.assertEqual(desktop_presence_lock.read_text(encoding="utf-8"), desktop_lock_content)
        self.assertTrue(cli_presence_lock.is_file())
        self.assertEqual(cli_presence_lock.read_text(encoding="utf-8"), cli_lock_content)

        # Assert simulated process entries in proc_dir are intact and unterminated
        self.assertTrue(server_proc_dir.is_dir())
        self.assertEqual((server_proc_dir / "cmdline").read_bytes(), server_cmdline)
        self.assertTrue(cli_proc_dir.is_dir())

    def test_failover_det_005_contrast_reload_server_true_blocked_by_active_turn(self) -> None:
        """[FAILOVER-DET-005] Adversarial contrast: reload_server=True is blocked during active turn.

        Confirms the essential rationale of ADR-0014: passing reload_server=True during an active
        turn safely halts without killing language_server or corrupting the turn, while reload_server=False
        succeeds cleanly via Disk-Only Swap.
        """
        presence_dir = self.gemini_dir / "antigravity" / "presence"
        presence_dir.mkdir(parents=True, exist_ok=True)
        desktop_lock = presence_dir / "active-turn.lock"
        desktop_lock.write_text("pid: 50000, active-task\n", encoding="utf-8")

        server_pid = 50000
        self._setup_simulated_process(
            pid=server_pid,
            cmdline=b"/opt/antigravity/bin/language_server\x00--standalone\x00",
            linked_locks=[desktop_lock],
        )

        cache_data = {
            "timestamp": 1700000000.0,
            "accounts": {
                "primary-acc": {"claude": {"quota": 0, "reset_time": 1700003600.0}},
                "standby-acc": {"claude": {"quota": 80, "reset_time": 1700003600.0}},
            },
        }
        self.engine.cache_file.write_text(json.dumps(cache_data), encoding="utf-8")

        # When reload_server=True is passed during an active turn, auto_rotate_on_exhaustion rejects the reload
        with patch("os.kill") as mock_os_kill:
            result = switcher_api.auto_rotate_on_exhaustion(
                model="claude",
                reload_server=True,
                engine=self.engine,
            )
            mock_os_kill.assert_not_called()

        self.assertFalse(result.success)
        self.assertIn("busy processing an agent turn", result.message)
        self.assertFalse(result.reloaded_server)
        self.assertEqual(result.active_account, "primary-acc")
        self.assertEqual(self.engine.get_active_name(), "primary-acc")

    def test_failover_det_005_granular_claude_slug_and_direct_switch_account(self) -> None:
        """[FAILOVER-DET-005] Model 'claude-sonnet-4-6' and direct switch_account adhere to Disk-Only Swap."""
        cache_data = {
            "timestamp": 1700000000.0,
            "accounts": {
                "primary-acc": {"claude": {"quota": 0, "reset_time": 1700003600.0}},
                "standby-acc": {"claude": {"quota": 95, "reset_time": 1700003600.0}},
            },
        }
        self.engine.cache_file.write_text(json.dumps(cache_data), encoding="utf-8")

        # Test auto_rotate_on_exhaustion with granular slug 'claude-sonnet-4-6'
        res = switcher_api.auto_rotate_on_exhaustion(
            model="claude-sonnet-4-6",
            reload_server=False,
            engine=self.engine,
        )
        self.assertTrue(res.success)
        self.assertEqual(res.new_account, "standby-acc")
        self.assertEqual(res.quota_remaining, 0.95)
        self.assertFalse(res.reloaded_server)

        # Test direct SDK switch_account defaults to Disk-Only Swap (reload_server=False)
        with patch.object(self.engine.supervisor, "restart_language_server") as mock_restart:
            swapped = switcher_api.switch_account(
                name="primary-acc",
                reload_server=False,
                engine=self.engine,
            )
            mock_restart.assert_not_called()

        self.assertTrue(swapped)
        self.assertEqual(self.engine.get_active_name(), "primary-acc")

    def test_failover_model_slug_normalization_tolerance(self) -> None:
        """SDK auto_rotate_on_exhaustion normalizes granular model slugs to canonical families."""
        cache_data = {
            "timestamp": 1700000000.0,
            "accounts": {
                "primary-acc": {
                    "gemini": {"quota": 0, "reset_time": 1700003600.0},
                    "claude": {"quota": 0, "reset_time": 1700003600.0},
                },
                "standby-acc": {
                    "gemini": {"quota": 75, "reset_time": 1700003600.0},
                    "claude": {"quota": 65, "reset_time": 1700003600.0},
                },
            },
        }
        self.engine.cache_file.write_text(json.dumps(cache_data), encoding="utf-8")

        # Passing specific slug 'gemini-3.8-flash' directly to auto_rotate_on_exhaustion
        result_gemini = switcher_api.auto_rotate_on_exhaustion(
            model="gemini-3.8-flash",
            reload_server=False,
            engine=self.engine,
        )
        self.assertTrue(result_gemini.success)
        self.assertEqual(result_gemini.new_account, "standby-acc")
        self.assertEqual(result_gemini.quota_remaining, 0.75)

    def test_failover_unsupported_model_family_returns_failure(self) -> None:
        """SDK auto_rotate_on_exhaustion gracefully returns success=False for unknown model families."""
        result = switcher_api.auto_rotate_on_exhaustion(
            model="unknown-vendor-llm",
            reload_server=False,
            engine=self.engine,
        )
        self.assertFalse(result.success)
        self.assertIsNone(result.new_account)
        self.assertIn("Unsupported model family", result.message)

    def test_failover_default_engine_lookup_isolated(self) -> None:
        """SDK auto_rotate_on_exhaustion operates cleanly when engine parameter is omitted."""
        cache_data = {
            "timestamp": 1700000000.0,
            "accounts": {
                "primary-acc": {"gemini": {"quota": 0, "reset_time": 1700003600.0}},
                "standby-acc": {"gemini": {"quota": 80, "reset_time": 1700003600.0}},
            },
        }
        self.engine.cache_file.write_text(json.dumps(cache_data), encoding="utf-8")

        # Patch AgySwitchEngine default factory to use our isolated test engine
        with patch("agy_switcher.api.AgySwitchEngine", return_value=self.engine):
            result = switcher_api.auto_rotate_on_exhaustion(model="gemini", reload_server=False)

        self.assertTrue(result.success)
        self.assertEqual(result.new_account, "standby-acc")
        self.assertEqual(result.previous_account, "primary-acc")

    def test_failover_all_accounts_exhausted_returns_failure(self) -> None:
        """SDK auto_rotate_on_exhaustion returns success=False when all accounts are exhausted."""
        cache_data = {
            "timestamp": 1700000000.0,
            "accounts": {
                "primary-acc": {"gemini": {"quota": 0, "reset_time": 1700003600.0}},
                "standby-acc": {"gemini": {"quota": 0, "reset_time": 1700003600.0}},
            },
        }
        self.engine.cache_file.write_text(json.dumps(cache_data), encoding="utf-8")

        result = switcher_api.auto_rotate_on_exhaustion(
            model="gemini",
            reload_server=False,
            engine=self.engine,
        )
        self.assertFalse(result.success)
        self.assertIsNone(result.new_account)
        self.assertEqual(result.previous_account, "primary-acc")
        self.assertIn("exhausted", result.message)

    def test_failover_no_active_account_initially(self) -> None:
        """SDK auto_rotate_on_exhaustion succeeds when active account pointer was previously unset."""
        if self.engine.active_file.is_file():
            self.engine.active_file.unlink()

        cache_data = {
            "timestamp": 1700000000.0,
            "accounts": {
                "primary-acc": {"gemini": {"quota": 0, "reset_time": 1700003600.0}},
                "standby-acc": {"gemini": {"quota": 90, "reset_time": 1700003600.0}},
            },
        }
        self.engine.cache_file.write_text(json.dumps(cache_data), encoding="utf-8")

        result = switcher_api.auto_rotate_on_exhaustion(
            model="gemini",
            reload_server=False,
            engine=self.engine,
        )
        self.assertTrue(result.success)
        self.assertIsNone(result.previous_account)
        self.assertEqual(result.new_account, "standby-acc")
        self.assertEqual(self.engine.get_active_name(), "standby-acc")

    def test_rotation_result_to_dict_contract(self) -> None:
        """RotationResult serializes cleanly to dict matching SDK presentation contract."""
        res = RotationResult(
            success=True,
            previous_account="acc-a",
            new_account="acc-b",
            model="gemini",
            message="Rotation OK",
            quota_remaining=0.75,
            reloaded_server=False,
            active_account="acc-b",
        )
        data = res.to_dict()
        self.assertEqual(
            data,
            {
                "success": True,
                "previous_account": "acc-a",
                "new_account": "acc-b",
                "model": "gemini",
                "message": "Rotation OK",
                "quota_remaining": 0.75,
                "reloaded_server": False,
                "active_account": "acc-b",
            },
        )


if __name__ == "__main__":
    unittest.main()
