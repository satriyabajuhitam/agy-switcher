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
import concurrent.futures
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
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


class TestSoftImportAndConcurrency(unittest.TestCase):
    """Integration test suite validating soft-import fallback and concurrency safety (ADR-0014).

    Validates:
    - [FAILOVER-ERR-001]: Graceful fallback when agy-switcher is not installed in Python runtime.
    - [FAILOVER-EDGE-004]: Concurrent failover rotations serialize safely under atomic disk swap
      without race conditions, without corrupted token files, and without double-hopping.
    """

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

        # Scaffolding accounts: primary-acc (active), standby-acc (target 1), standby-acc-2 (target 2)
        self.primary_token_data = create_mock_oauth_token("primary@example.com", "ya29.primary-token-val")
        self.standby_token_data = create_mock_oauth_token("standby@example.com", "ya29.standby-token-val")
        self.standby2_token_data = create_mock_oauth_token("standby2@example.com", "ya29.standby2-token-val")

        self._setup_account("primary-acc", self.primary_token_data)
        self._setup_account("standby-acc", self.standby_token_data)
        self._setup_account("standby-acc-2", self.standby2_token_data)

        self.engine.set_active_name("primary-acc")

        # Quota cache telemetry: primary-acc exhausted (quota=0), standby-acc healthy (quota=90), standby-acc-2 (quota=85)
        cache_data = {
            "timestamp": 1700000000.0,
            "accounts": {
                "primary-acc": {"gemini": {"quota": 0, "reset_time": 1700003600.0}},
                "standby-acc": {"gemini": {"quota": 90, "reset_time": 1700003600.0}},
                "standby-acc-2": {"gemini": {"quota": 85, "reset_time": 1700003600.0}},
            },
        }
        self.engine.cache_file.write_text(json.dumps(cache_data), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _setup_account(self, name: str, token_data: dict[str, Any]) -> None:
        acc_dir = self.engine.accounts_dir / name
        acc_dir.mkdir(parents=True, exist_ok=True)
        (acc_dir / "token.json").write_text(json.dumps(token_data), encoding="utf-8")

    def test_failover_err_001_soft_import_fallback_when_agy_switcher_not_installed(self) -> None:
        """[FAILOVER-ERR-001] Graceful fallback when agy_switcher is not installed in runtime."""
        # Simulate missing agy_switcher.api module in Python runtime
        with patch.dict(sys.modules, {"agy_switcher.api": None, "agy_switcher": None}):
            # Attempt soft-import rotation via helper
            success, result, error_msg = switcher_api.handle_soft_import_rotation(
                model="gemini",
                reload_server=False,
                engine=self.engine,
            )

        self.assertFalse(success)
        self.assertIsNone(result)
        # Verify message explains quota exhaustion and manual re-login or installing agy-switcher is required
        self.assertIn("quota", error_msg.lower())
        self.assertIn("exhausted", error_msg.lower())
        self.assertIn("manual re-login or installing 'agy-switcher' is required", error_msg.lower())

    def test_failover_err_001_turn_runner_catches_import_error_gracefully(self) -> None:
        """[FAILOVER-ERR-001] TurnRunner catches ImportError gracefully and emits guidance message."""
        progress_sink_messages: list[str] = []

        class SimulatedTurnRunner:
            def handle_exhaustion_turn(self, error_text: str) -> dict[str, Any]:
                lower = error_text.lower()
                if "resource_exhausted" in lower or "resourceexhausted" in lower or "quota" in lower:
                    try:
                        # Soft-import agy_switcher.api dynamically
                        import agy_switcher.api as dynamic_api
                        rot = dynamic_api.auto_rotate_on_exhaustion(model="gemini", reload_server=False)
                        return {"success": rot.success, "message": rot.message}
                    except (ImportError, ModuleNotFoundError):
                        msg = (
                            "Quota limit reached (RESOURCE_EXHAUSTED). "
                            "Manual re-login or installing 'agy-switcher' is required."
                        )
                        progress_sink_messages.append(msg)
                        return {"success": False, "message": msg}
                return {"success": False, "message": "Unknown error"}

        runner = SimulatedTurnRunner()

        # Simulate environment where agy_switcher is absent
        with patch.dict(sys.modules, {"agy_switcher.api": None, "agy_switcher": None}):
            res = runner.handle_exhaustion_turn("rpc error: code = ResourceExhausted desc = Quota limit reached")

        self.assertFalse(res["success"])
        self.assertEqual(len(progress_sink_messages), 1)
        self.assertIn("manual re-login or installing 'agy-switcher' is required", progress_sink_messages[0].lower())

    def test_failover_edge_004_concurrent_failover_rotations_serialize_safely(self) -> None:
        """[FAILOVER-EDGE-004] Two concurrent turns rotate simultaneously without race or double-hop."""
        start_event = threading.Event()

        def concurrent_rotate() -> RotationResult:
            start_event.wait(timeout=5.0)
            return switcher_api.auto_rotate_on_exhaustion(
                model="gemini",
                reload_server=False,
                engine=self.engine,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            fut1 = executor.submit(concurrent_rotate)
            fut2 = executor.submit(concurrent_rotate)
            # Release both threads simultaneously to create genuine concurrent entry
            start_event.set()
            res1 = fut1.result(timeout=10.0)
            res2 = fut2.result(timeout=10.0)

        # Both turns must succeed
        self.assertTrue(res1.success, f"Turn 1 rotation failed: {res1.message}")
        self.assertTrue(res2.success, f"Turn 2 rotation failed: {res2.message}")

        # Active account must have cleanly transitioned to standby-acc
        active_account = self.engine.get_active_name()
        self.assertEqual(active_account, "standby-acc")

        # Crucial Anti-Double-Hop Invariant: active account must NOT have double-hopped to standby-acc-2
        self.assertNotEqual(active_account, "standby-acc-2")
        self.assertEqual(res1.active_account, "standby-acc")
        self.assertEqual(res2.active_account, "standby-acc")

        # Verify token files on disk are intact, valid JSON, 0600 permissions, containing standby-acc tokens
        cli_payload = json.loads(self.engine.cli_token_file.read_text(encoding="utf-8"))
        self.assertEqual(
            cli_payload["token"]["access_token"],
            self.standby_token_data["token"]["access_token"],
        )
        self.assertEqual(self.engine.cli_token_file.stat().st_mode & 0o777, 0o600)

        desktop_payload = json.loads(self.engine.desktop_token_file.read_text(encoding="utf-8"))
        self.assertEqual(
            desktop_payload["token"]["access_token"],
            self.standby_token_data["token"]["access_token"],
        )
        self.assertEqual(self.engine.desktop_token_file.stat().st_mode & 0o777, 0o600)

    def test_failover_edge_004_multi_worker_concurrency_stress(self) -> None:
        """[FAILOVER-EDGE-004] High concurrency stress: 8 workers failover simultaneously without corruption."""
        worker_count = 8
        barrier = threading.Barrier(worker_count)

        def worker_task() -> RotationResult:
            barrier.wait(timeout=5.0)
            return switcher_api.auto_rotate_on_exhaustion(
                model="gemini",
                reload_server=False,
                engine=self.engine,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(worker_task) for _ in range(worker_count)]
            results = [f.result(timeout=10.0) for f in futures]

        for idx, res in enumerate(results):
            self.assertTrue(res.success, f"Worker {idx} failed: {res.message}")
            self.assertEqual(res.active_account, "standby-acc")

        self.assertEqual(self.engine.get_active_name(), "standby-acc")
        self.assertNotEqual(self.engine.get_active_name(), "standby-acc-2")

        # Verify atomic disk swap did not leave corrupt files or partial writes
        cli_payload = json.loads(self.engine.cli_token_file.read_text(encoding="utf-8"))
        self.assertEqual(
            cli_payload["token"]["access_token"],
            self.standby_token_data["token"]["access_token"],
        )

    def test_failover_edge_004_explicit_current_account_prevents_double_hop(self) -> None:
        """[FAILOVER-EDGE-004] Passing current_account explicitly prevents double-hop across threads."""
        start_event = threading.Event()

        def rotate_from_primary() -> RotationResult:
            start_event.wait(timeout=5.0)
            return switcher_api.auto_rotate_on_exhaustion(
                model="gemini",
                reload_server=False,
                engine=self.engine,
                current_account="primary-acc",
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(rotate_from_primary)
            f2 = executor.submit(rotate_from_primary)
            start_event.set()
            r1 = f1.result(timeout=10.0)
            r2 = f2.result(timeout=10.0)

        self.assertTrue(r1.success)
        self.assertTrue(r2.success)
        self.assertEqual(self.engine.get_active_name(), "standby-acc")
        self.assertNotEqual(self.engine.get_active_name(), "standby-acc-2")

    def test_failover_edge_004_heterogeneous_model_concurrency_race(self) -> None:
        """[FAILOVER-EDGE-004] Concurrent rotations for different models isolate and do not corrupt state."""
        cache_data = {
            "timestamp": 1700000000.0,
            "accounts": {
                "primary-acc": {
                    "claude": {"quota": 0, "reset_time": 1700003600.0},
                    "gemini": {"quota": 0, "reset_time": 1700003600.0},
                },
                "standby-acc": {
                    "claude": {"quota": 90, "reset_time": 1700003600.0},
                    "gemini": {"quota": 0, "reset_time": 1700003600.0},
                },
                "standby-acc-2": {
                    "claude": {"quota": 85, "reset_time": 1700003600.0},
                    "gemini": {"quota": 85, "reset_time": 1700003600.0},
                },
            },
        }
        self.engine.cache_file.write_text(json.dumps(cache_data), encoding="utf-8")

        start_event = threading.Event()

        def rotate_claude() -> RotationResult:
            start_event.wait(timeout=5.0)
            return switcher_api.auto_rotate_on_exhaustion(
                model="claude",
                reload_server=False,
                engine=self.engine,
                current_account="primary-acc",
            )

        def rotate_gemini() -> RotationResult:
            start_event.wait(timeout=5.0)
            return switcher_api.auto_rotate_on_exhaustion(
                model="gemini",
                reload_server=False,
                engine=self.engine,
                current_account="primary-acc",
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            fut_c = executor.submit(rotate_claude)
            fut_g = executor.submit(rotate_gemini)
            start_event.set()
            res_c = fut_c.result(timeout=10.0)
            res_g = fut_g.result(timeout=10.0)

        # Both must succeed
        self.assertTrue(res_c.success)
        self.assertTrue(res_g.success)

        # Claude rotated to an account with claude quota (standby-acc or standby-acc-2)
        self.assertEqual(res_c.model, "claude")
        self.assertIn(res_c.new_account, ["standby-acc", "standby-acc-2"])

        # Gemini MUST NOT be stranded on standby-acc (which has 0 gemini quota)!
        self.assertEqual(res_g.model, "gemini")
        self.assertEqual(res_g.new_account, "standby-acc-2")

        # The active account in vault must be an account valid for both (standby-acc-2)
        self.assertEqual(self.engine.get_active_name(), "standby-acc-2")

    def test_failover_edge_004_corrupt_standby_account_bypassed_cleanly(self) -> None:
        """[FAILOVER-EDGE-004] Corrupted token file in standby account is bypassed in favor of valid account."""
        # Corrupt standby-acc token.json
        (self.engine.accounts_dir / "standby-acc" / "token.json").write_text("{corrupt-json", encoding="utf-8")

        res = switcher_api.auto_rotate_on_exhaustion(
            model="gemini",
            reload_server=False,
            engine=self.engine,
            current_account="primary-acc",
        )

        self.assertTrue(res.success)
        self.assertEqual(res.new_account, "standby-acc-2")
        self.assertEqual(self.engine.get_active_name(), "standby-acc-2")

    def test_failover_err_001_soft_import_missing_attribute_fallback(self) -> None:
        """[FAILOVER-ERR-001] Missing auto_rotate_on_exhaustion attribute in imported module triggers clean fallback."""
        from unittest.mock import MagicMock
        mock_mod = MagicMock(spec=[])
        with patch.dict(sys.modules, {"agy_switcher.api": mock_mod}):
            success, result, msg = switcher_api.handle_soft_import_rotation(
                model="gemini",
                reload_server=False,
                engine=self.engine,
            )
        self.assertFalse(success)
        self.assertIsNone(result)
        self.assertIn("manual re-login or installing 'agy-switcher' is required", msg.lower())

    def test_failover_edge_004_all_accounts_exhausted_with_explicit_current_account(self) -> None:
        """[FAILOVER-EDGE-004] Rotation fails gracefully when all accounts exhausted, preserving current_account in result."""
        cache_data = {
            "timestamp": 1700000000.0,
            "accounts": {
                "primary-acc": {"gemini": {"quota": 0, "reset_time": 1700003600.0}},
                "standby-acc": {"gemini": {"quota": 0, "reset_time": 1700003600.0}},
                "standby-acc-2": {"gemini": {"quota": 0, "reset_time": 1700003600.0}},
            },
        }
        self.engine.cache_file.write_text(json.dumps(cache_data), encoding="utf-8")

        res = switcher_api.auto_rotate_on_exhaustion(
            model="gemini",
            reload_server=False,
            engine=self.engine,
            current_account="primary-acc",
        )
        self.assertFalse(res.success)
        self.assertEqual(res.previous_account, "primary-acc")
        self.assertIsNone(res.new_account)
        self.assertIn("exhausted", res.message)



class TestMultiModelTelemetry(unittest.TestCase):
    """Integration test suite validating multi-model telemetry and missing timestamp handling (ADR-0014).

    Validates:
    - [PANIC-EDGE-001]: GIVEN both 'gemini' and 'claude' model quotas are exhausted across all accounts,
      WHEN get_fleet_quota_summary(model="all") is queried during a Panic Event,
      THEN the summary calculates reset countdowns independently for both families
      and displays the relevant reset time for the currently requested turn model.
    - [PANIC-EDGE-002]: GIVEN CloudCode quota API returns 0% quota without providing a specific reset
      countdown timestamp, WHEN the Fleet Exhaustion message is synthesized,
      THEN the formatter indicates that quota is exhausted and will refresh on the standard rolling
      window schedule, and does not display 'None' or negative countdown durations.
    """

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

        self.acc1_token = create_mock_oauth_token("acc1@example.com", "ya29.acc1-token")
        self.acc2_token = create_mock_oauth_token("acc2@example.com", "ya29.acc2-token")
        self._setup_account("acc1", self.acc1_token)
        self._setup_account("acc2", self.acc2_token)
        self.engine.set_active_name("acc1")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _setup_account(self, name: str, token_data: dict[str, Any]) -> None:
        acc_dir = self.engine.accounts_dir / name
        acc_dir.mkdir(parents=True, exist_ok=True)
        (acc_dir / "token.json").write_text(json.dumps(token_data), encoding="utf-8")

    def test_panic_edge_001_multi_model_concurrent_exhaustion_independent_resets(self) -> None:
        """[PANIC-EDGE-001] Multiple model families exhausted concurrently in fleet summary.

        GIVEN both 'gemini' and 'claude' model quotas are exhausted across all accounts,
        WHEN get_fleet_quota_summary(model="all") is queried during a Panic Event,
        THEN the summary calculates reset countdowns independently for both families
        AND displays the relevant reset time for the currently requested turn model.
        """
        curr_ts = 1730902200.0  # reference timestamp
        gemini_reset = curr_ts + 1200.0   # +20 min -> 14:30 UTC
        claude_reset = curr_ts + 6600.0   # +110 min (1h 50m) -> 16:00 UTC

        cache_data = {
            "timestamp": curr_ts,
            "accounts": {
                "acc1": {
                    "groups": [
                        {
                            "displayName": "Gemini 1.5 Pro",
                            "buckets": [
                                {
                                    "remainingFraction": 0.0,
                                    "resetTime": "2024-11-06T14:30:00Z",
                                }
                            ],
                        },
                        {
                            "displayName": "Claude 3.5 Sonnet",
                            "buckets": [
                                {
                                    "remainingFraction": 0.0,
                                    "resetTime": "2024-11-06T16:00:00Z",
                                }
                            ],
                        },
                    ],
                },
                "acc2": {
                    "gemini": {"quota": 0, "reset_time": gemini_reset},
                    "claude": {"quota": 0, "reset_time": claude_reset},
                },
            },
        }
        self.engine.cache_dir.mkdir(parents=True, exist_ok=True)
        self.engine.cache_file.write_text(json.dumps(cache_data), encoding="utf-8")

        summary = switcher_api.get_fleet_quota_summary(model="all", engine=self.engine)
        self.assertIsInstance(summary, switcher_api.FleetQuotaSummary)
        self.assertEqual(summary.model, "all")
        self.assertEqual(summary.total_accounts, 2)

        # Both accounts must contain independently evaluated quota and reset times for both families
        for acc_name in ("acc1", "acc2"):
            acc_data = summary.accounts[acc_name]
            self.assertIn("gemini", acc_data)
            self.assertIn("claude", acc_data)
            self.assertEqual(acc_data["gemini"]["quota"], 0)
            self.assertEqual(acc_data["claude"]["quota"], 0)
            self.assertIsNotNone(acc_data["gemini"]["reset_time"])
            self.assertIsNotNone(acc_data["claude"]["reset_time"])
            # Gemini and Claude have independent reset times
            self.assertNotEqual(acc_data["gemini"]["reset_time"], acc_data["claude"]["reset_time"])

        # Formatting user failure notice displays the relevant reset time for the currently requested turn model
        msg_gemini = switcher_api.format_fleet_exhaustion_message(summary, model="gemini", current_time=curr_ts)
        self.assertIn("14:30", msg_gemini)
        self.assertIn("20 menit lagi", msg_gemini)
        self.assertNotIn("16:00", msg_gemini)

        msg_claude = switcher_api.format_fleet_exhaustion_message(summary, model="claude", current_time=curr_ts)
        self.assertIn("16:00", msg_claude)
        self.assertIn("1 jam 50 menit lagi", msg_claude)
        self.assertNotIn("14:30", msg_claude)

    def test_panic_edge_002_missing_or_indefinite_reset_timestamp_handling(self) -> None:
        """[PANIC-EDGE-002] All accounts exhausted with indefinite or missing reset timestamps in telemetry.

        GIVEN CloudCode quota API returns 0% quota without providing a specific reset countdown timestamp,
        WHEN the Fleet Exhaustion message is synthesized,
        THEN the formatter indicates that quota is exhausted and will refresh on the standard rolling window schedule,
        AND does not display 'None' or negative countdown durations.
        """
        curr_ts = 1730902200.0

        # Scenario 2a: CloudCode returns raw groups with 0% quota and no resetTime
        cache_data_groups = {
            "timestamp": curr_ts,
            "accounts": {
                "acc1": {
                    "groups": [
                        {
                            "displayName": "Gemini 1.5 Pro",
                            "buckets": [
                                {
                                    "remainingFraction": 0.0,
                                    # resetTime is omitted / not provided
                                }
                            ],
                        }
                    ],
                },
                "acc2": {
                    "groups": [
                        {
                            "displayName": "Gemini 1.5 Flash",
                            "buckets": [
                                {
                                    "remainingFraction": 0.0,
                                    "resetTime": None,  # Explicitly None
                                }
                            ],
                        }
                    ],
                },
            },
        }
        self.engine.cache_dir.mkdir(parents=True, exist_ok=True)
        self.engine.cache_file.write_text(json.dumps(cache_data_groups), encoding="utf-8")

        summary = switcher_api.get_fleet_quota_summary(model="gemini", engine=self.engine)
        for acc_name in ("acc1", "acc2"):
            acc_data = summary.accounts[acc_name]
            self.assertEqual(acc_data["quota"], 0)
            self.assertIsNone(acc_data["reset_time"], f"{acc_name} reset_time should be None, got {acc_data['reset_time']!r}")

        msg = switcher_api.format_fleet_exhaustion_message(summary, model="gemini", current_time=curr_ts)
        self.assertNotIn("None", msg)
        self.assertNotIn("-", msg.replace("⚠️", ""))  # No negative numbers
        self.assertEqual(
            msg,
            "⚠️ Seluruh kuota akun sedang habis. Silakan coba kembali dalam beberapa saat atau periksa status dengan /quota.",
        )

        # Scenario 2b: Multi-model (model="all") where both gemini and claude have missing reset timestamps
        cache_data_multi_indef = {
            "timestamp": curr_ts,
            "accounts": {
                "acc1": {
                    "gemini": {"quota": 0, "reset_time": None},
                    "claude": {"quota": 0, "reset_time": None},
                },
                "acc2": {
                    "gemini": {"quota": 0, "reset_time": ""},
                    "claude": {"quota": 0, "reset_time": None},
                },
            },
        }
        self.engine.cache_file.write_text(json.dumps(cache_data_multi_indef), encoding="utf-8")
        summary_all = switcher_api.get_fleet_quota_summary(model="all", engine=self.engine)
        self.assertIsNone(summary_all.accounts["acc1"]["gemini"]["reset_time"])
        self.assertIsNone(summary_all.accounts["acc1"]["claude"]["reset_time"])

        msg_gemini_indef = switcher_api.format_fleet_exhaustion_message(summary_all, model="gemini", current_time=curr_ts)
        self.assertNotIn("None", msg_gemini_indef)
        self.assertEqual(
            msg_gemini_indef,
            "⚠️ Seluruh kuota akun sedang habis. Silakan coba kembali dalam beberapa saat atau periksa status dengan /quota.",
        )

        # Scenario 2c: Expired timestamps in the past do not produce negative countdown durations
        cache_data_expired = {
            "timestamp": curr_ts,
            "accounts": {
                "acc1": {"gemini": {"quota": 0, "reset_time": curr_ts - 500.0}},
            },
        }
        self.engine.cache_file.write_text(json.dumps(cache_data_expired), encoding="utf-8")
        summary_expired = switcher_api.get_fleet_quota_summary(model="gemini", engine=self.engine)
        msg_expired = switcher_api.format_fleet_exhaustion_message(summary_expired, model="gemini", current_time=curr_ts)
        self.assertNotIn("None", msg_expired)
        self.assertNotIn("-", msg_expired.replace("⚠️", ""))
        self.assertEqual(
            msg_expired,
            "⚠️ Seluruh kuota akun sedang habis. Silakan coba kembali dalam beberapa saat atau periksa status dengan /quota.",
        )


if __name__ == "__main__":
    unittest.main()

