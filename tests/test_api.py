"""Unit and integration tests for In-Process SDK Facade (scripts/agy_switcher/api.py).

Contracts & Specifications:
- .scratch/packaging-and-sdk/spec.md#L220-L245
- .scratch/packaging-and-sdk/issues/02-sdk-models-and-active-account-facade.md
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
import sys
import tempfile
from typing import Any
import unittest
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from agy_switcher.exceptions import AccountNotFoundError, ActiveTurnRunningError, InvalidTokenError
from agy_switcher.models import AccountInfo, RotationResult, FleetQuotaSummary
from agy_switcher.security import extract_email_from_jwt
from agy_switcher.vault import AgySwitchEngine
import agy_switcher.api as api


def create_sample_token(email: str = "test@example.com") -> dict[str, Any]:
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


class TestSDKFacade(unittest.TestCase):
    """Tests for in-process SDK facade functions and models."""

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

    def test_get_active_account_populated(self) -> None:
        """[REQ-2-SCN-001] GIVEN configured active account acc1, get_active_account returns populated AccountInfo."""
        acc1_dir = self.engine.accounts_dir / "acc1"
        acc1_dir.mkdir(parents=True, exist_ok=True)
        acc1_token = acc1_dir / "token.json"
        acc1_token.write_text(json.dumps(create_sample_token("acc1@example.com")), encoding="utf-8")

        acc2_dir = self.engine.accounts_dir / "acc2"
        acc2_dir.mkdir(parents=True, exist_ok=True)
        acc2_token = acc2_dir / "token.json"
        acc2_token.write_text(json.dumps(create_sample_token("acc2@example.com")), encoding="utf-8")

        self.engine.set_active_name("acc1")

        account_info = api.get_active_account(engine=self.engine)

        self.assertIsInstance(account_info, AccountInfo)
        self.assertEqual(account_info.name, "acc1")
        self.assertEqual(account_info.email, "acc1@example.com")
        self.assertTrue(account_info.is_active)
        self.assertEqual(account_info.status, "active")
        self.assertIsNotNone(account_info.token_path)
        assert account_info.token_path is not None
        self.assertEqual(Path(account_info.token_path), acc1_token)

        data = account_info.to_dict()
        self.assertEqual(data["name"], "acc1")
        self.assertEqual(data["email"], "acc1@example.com")
        self.assertTrue(data["is_active"])
        self.assertEqual(data["status"], "active")
        self.assertEqual(data["token_path"], str(acc1_token))

    def test_get_active_account_empty_vault(self) -> None:
        """[REQ-2-SCN-002] GIVEN an empty vault with no active account, get_active_account returns is_active=False."""
        account_info = api.get_active_account(engine=self.engine)

        self.assertIsInstance(account_info, AccountInfo)
        self.assertFalse(account_info.is_active)
        self.assertEqual(account_info.name, "")
        self.assertIsNone(account_info.email)
        self.assertEqual(account_info.status, "none")
        self.assertIsNone(account_info.token_path)

        data = account_info.to_dict()
        self.assertFalse(data["is_active"])
        self.assertEqual(data["name"], "")
        self.assertIsNone(data["email"])
        self.assertEqual(data["status"], "none")

    def test_get_active_account_dangling_pointer(self) -> None:
        """[REQ-2-EDGE-001] Dangling active pointer file resolved safely."""
        self.engine.set_active_name("deleted_acc")

        account_info = api.get_active_account(engine=self.engine)

        self.assertIsInstance(account_info, AccountInfo)
        self.assertFalse(account_info.is_active)
        self.assertEqual(account_info.name, "")
        self.assertIsNone(account_info.email)
        self.assertEqual(account_info.status, "none")

    def test_get_active_account_corrupt_token(self) -> None:
        """[REQ-2-ERR-003] Corrupt token file does not raise unhandled JSONDecodeError."""
        corrupt_dir = self.engine.accounts_dir / "corrupt_acc"
        corrupt_dir.mkdir(parents=True, exist_ok=True)
        corrupt_token = corrupt_dir / "token.json"
        corrupt_token.write_text("{malformed: json invalid binary \x00\xff", encoding="utf-8")

        self.engine.set_active_name("corrupt_acc")

        account_info = api.get_active_account(engine=self.engine)

        self.assertIsInstance(account_info, AccountInfo)
        self.assertEqual(account_info.name, "corrupt_acc")
        self.assertFalse(account_info.is_active)
        self.assertEqual(account_info.status, "corrupt")
        self.assertIsNone(account_info.email)

    def test_get_active_account_default_engine(self) -> None:
        """api.get_active_account uses default AgySwitchEngine when none provided."""
        with patch("agy_switcher.api.AgySwitchEngine", return_value=self.engine):
            self.engine.set_active_name("")
            account_info = api.get_active_account()
            self.assertIsInstance(account_info, AccountInfo)
            self.assertFalse(account_info.is_active)

    def test_get_active_account_whitespace_padding(self) -> None:
        """Active account file with leading/trailing whitespace resolves to trimmed account."""
        acc_dir = self.engine.accounts_dir / "spaced_acc"
        acc_dir.mkdir(parents=True, exist_ok=True)
        token_file = acc_dir / "token.json"
        token_file.write_text(json.dumps(create_sample_token("spaced@example.com")), encoding="utf-8")

        self.engine.active_file.write_text("   spaced_acc \n\t", encoding="utf-8")

        account_info = api.get_active_account(engine=self.engine)
        self.assertEqual(account_info.name, "spaced_acc")
        self.assertEqual(account_info.email, "spaced@example.com")
        self.assertTrue(account_info.is_active)
        self.assertEqual(account_info.status, "active")

    def test_get_active_account_path_traversal_dotdot(self) -> None:
        """Active file containing '..' is rejected as invalid rather than escaping vault."""
        self.engine.active_file.write_text("..\n", encoding="utf-8")

        account_info = api.get_active_account(engine=self.engine)
        self.assertFalse(account_info.is_active)
        self.assertEqual(account_info.name, "")
        self.assertIsNone(account_info.email)
        self.assertEqual(account_info.status, "none")
        self.assertIsNone(account_info.token_path)

    def test_get_active_account_path_traversal_subpath(self) -> None:
        """Active file containing directory separators or relative escape is rejected."""
        self.engine.active_file.write_text("../../etc\n", encoding="utf-8")

        account_info = api.get_active_account(engine=self.engine)
        self.assertFalse(account_info.is_active)
        self.assertEqual(account_info.name, "")
        self.assertIsNone(account_info.email)
        self.assertEqual(account_info.status, "none")

        self.engine.active_file.write_text("acc/sub\n", encoding="utf-8")
        account_info_sub = api.get_active_account(engine=self.engine)
        self.assertFalse(account_info_sub.is_active)
        self.assertEqual(account_info_sub.name, "")

    def test_get_active_account_missing_token_file(self) -> None:
        """Account folder exists in vault but token.json is absent, reporting corrupt."""
        acc_dir = self.engine.accounts_dir / "no_token_acc"
        acc_dir.mkdir(parents=True, exist_ok=True)
        self.engine.set_active_name("no_token_acc")

        account_info = api.get_active_account(engine=self.engine)
        self.assertEqual(account_info.name, "no_token_acc")
        self.assertFalse(account_info.is_active)
        self.assertEqual(account_info.status, "corrupt")
        self.assertIsNone(account_info.email)
        self.assertEqual(Path(account_info.token_path), acc_dir / "token.json")  # type: ignore[arg-type]

    def test_get_active_account_token_is_directory(self) -> None:
        """token.json path is a directory instead of a regular file."""
        acc_dir = self.engine.accounts_dir / "dir_token_acc"
        acc_dir.mkdir(parents=True, exist_ok=True)
        (acc_dir / "token.json").mkdir(parents=True, exist_ok=True)
        self.engine.set_active_name("dir_token_acc")

        account_info = api.get_active_account(engine=self.engine)
        self.assertEqual(account_info.name, "dir_token_acc")
        self.assertFalse(account_info.is_active)
        self.assertEqual(account_info.status, "corrupt")

    def test_get_active_account_token_missing_required_keys(self) -> None:
        """token.json contains valid JSON but fails token schema validation."""
        acc_dir = self.engine.accounts_dir / "partial_acc"
        acc_dir.mkdir(parents=True, exist_ok=True)
        (acc_dir / "token.json").write_text(json.dumps({"token": {"access_token": "foo"}}), encoding="utf-8")
        self.engine.set_active_name("partial_acc")

        account_info = api.get_active_account(engine=self.engine)
        self.assertEqual(account_info.name, "partial_acc")
        self.assertFalse(account_info.is_active)
        self.assertEqual(account_info.status, "corrupt")

    def test_get_active_account_jwt_without_email_claim(self) -> None:
        """Valid token dict and id_token JWT with no email claim yields email=None."""
        acc_dir = self.engine.accounts_dir / "no_email_acc"
        acc_dir.mkdir(parents=True, exist_ok=True)

        payload = json.dumps({"sub": "no_email_user", "aud": "test_aud"}).encode("utf-8")
        payload_b64 = base64.urlsafe_b64encode(payload).decode("utf-8").rstrip("=")
        token_data = {
            "token": {
                "access_token": "ya29.sample_token",
                "token_type": "Bearer",
                "refresh_token": "1//sample_refresh",
                "expiry": "2026-09-30T12:00:00Z",
            },
            "auth_method": "oauth",
            "id_token": f"header.{payload_b64}.signature",
        }
        (acc_dir / "token.json").write_text(json.dumps(token_data), encoding="utf-8")
        self.engine.set_active_name("no_email_acc")

        account_info = api.get_active_account(engine=self.engine)
        self.assertEqual(account_info.name, "no_email_acc")
        self.assertTrue(account_info.is_active)
        self.assertEqual(account_info.status, "active")
        self.assertIsNone(account_info.email)

    def test_get_active_account_engine_exception_leak_protection(self) -> None:
        """Underlying engine failure does not leak unhandled exceptions to caller."""
        with patch.object(self.engine, "get_active_name", side_effect=OSError("Hardware I/O error")):
            account_info = api.get_active_account(engine=self.engine)
            self.assertFalse(account_info.is_active)
            self.assertEqual(account_info.name, "")
            self.assertEqual(account_info.status, "none")

    def test_auto_rotate_on_exhaustion_success_disk_only_swap(self) -> None:
        """[REQ-2-SCN-003] auto_rotate_on_exhaustion executes Disk-Only Swap without restarting server."""
        acc1_dir = self.engine.accounts_dir / "acc1"
        acc1_dir.mkdir(parents=True, exist_ok=True)
        acc1_token = acc1_dir / "token.json"
        acc1_token_data = create_sample_token("acc1@example.com")
        acc1_token.write_text(json.dumps(acc1_token_data), encoding="utf-8")

        acc2_dir = self.engine.accounts_dir / "acc2"
        acc2_dir.mkdir(parents=True, exist_ok=True)
        acc2_token = acc2_dir / "token.json"
        acc2_token_data = create_sample_token("acc2@example.com")
        acc2_token.write_text(json.dumps(acc2_token_data), encoding="utf-8")

        self.engine.set_active_name("acc1")

        presence_lock = self.gemini_dir / "antigravity" / "presence" / "active-turn.lock"
        presence_lock.parent.mkdir(parents=True, exist_ok=True)
        presence_lock.write_text("active-turn-12345\n", encoding="utf-8")

        cache_data = {
            "timestamp": 1700000000,
            "accounts": {
                "acc1": {"gemini": {"quota": 0, "reset_time": 1700003600}},
                "acc2": {"gemini": {"quota": 80, "reset_time": 1700003600}},
            },
        }
        self.engine.cache_file.write_text(json.dumps(cache_data), encoding="utf-8")

        with patch.object(self.engine, "restart_language_server") as mock_restart:
            result = api.auto_rotate_on_exhaustion(model="gemini", reload_server=False, engine=self.engine)
            mock_restart.assert_not_called()

        self.assertTrue(result.success)
        self.assertEqual(result.previous_account, "acc1")
        self.assertEqual(result.new_account, "acc2")
        self.assertEqual(result.active_account, "acc2")
        self.assertEqual(result.model, "gemini")
        self.assertFalse(result.reloaded_server)
        self.assertEqual(result.quota_remaining, 0.8)

        self.assertEqual(self.engine.get_active_name(), "acc2")

        cli_token = json.loads(self.engine.cli_token_file.read_text(encoding="utf-8"))
        self.assertEqual(cli_token["token"]["access_token"], acc2_token_data["token"]["access_token"])

        desktop_token = json.loads(self.engine.desktop_token_file.read_text(encoding="utf-8"))
        self.assertEqual(desktop_token["token"]["access_token"], acc2_token_data["token"]["access_token"])

    def test_auto_rotate_on_exhaustion_all_exhausted(self) -> None:
        """[REQ-2-SCN-004] auto_rotate_on_exhaustion returns success=False when all accounts have 0% quota."""
        acc1_dir = self.engine.accounts_dir / "acc1"
        acc1_dir.mkdir(parents=True, exist_ok=True)
        (acc1_dir / "token.json").write_text(json.dumps(create_sample_token("acc1@example.com")), encoding="utf-8")

        acc2_dir = self.engine.accounts_dir / "acc2"
        acc2_dir.mkdir(parents=True, exist_ok=True)
        (acc2_dir / "token.json").write_text(json.dumps(create_sample_token("acc2@example.com")), encoding="utf-8")

        self.engine.set_active_name("acc1")

        cache_data = {
            "timestamp": 1700000000,
            "accounts": {
                "acc1": {"gemini": {"quota": 0, "reset_time": 1700003600}},
                "acc2": {"gemini": {"quota": 0, "reset_time": 1700003600}},
            },
        }
        self.engine.cache_file.write_text(json.dumps(cache_data), encoding="utf-8")

        result = api.auto_rotate_on_exhaustion(model="gemini", engine=self.engine)

        self.assertFalse(result.success)
        self.assertIsNone(result.new_account)
        self.assertIn("All accounts exhausted for model 'gemini'", result.message)
        self.assertEqual(self.engine.get_active_name(), "acc1")

    def test_auto_rotate_on_exhaustion_model_steering_claude(self) -> None:
        """[REQ-2-SCN-005] auto_rotate_on_exhaustion steers rotation using model-specific telemetry."""
        acc1_dir = self.engine.accounts_dir / "acc1"
        acc1_dir.mkdir(parents=True, exist_ok=True)
        (acc1_dir / "token.json").write_text(json.dumps(create_sample_token("acc1@example.com")), encoding="utf-8")

        acc2_dir = self.engine.accounts_dir / "acc2"
        acc2_dir.mkdir(parents=True, exist_ok=True)
        (acc2_dir / "token.json").write_text(json.dumps(create_sample_token("acc2@example.com")), encoding="utf-8")

        acc3_dir = self.engine.accounts_dir / "acc3"
        acc3_dir.mkdir(parents=True, exist_ok=True)
        (acc3_dir / "token.json").write_text(json.dumps(create_sample_token("acc3@example.com")), encoding="utf-8")

        self.engine.set_active_name("acc1")

        cache_data = {
            "timestamp": 1700000000,
            "accounts": {
                "acc1": {"claude": {"quota": 0}, "gemini": {"quota": 100}},
                "acc2": {"claude": {"quota": 100}, "gemini": {"quota": 10}},
                "acc3": {"claude": {"quota": 50}, "gemini": {"quota": 90}},
            },
        }
        self.engine.cache_file.write_text(json.dumps(cache_data), encoding="utf-8")

        result = api.auto_rotate_on_exhaustion(model="claude", engine=self.engine)

        self.assertTrue(result.success)
        self.assertEqual(result.new_account, "acc2")
        self.assertEqual(result.model, "claude")
        self.assertEqual(self.engine.get_active_name(), "acc2")

    def test_auto_rotate_on_exhaustion_unsupported_model(self) -> None:
        """[REQ-2-ERR-004] auto_rotate_on_exhaustion handles unsupported model gracefully."""
        result = api.auto_rotate_on_exhaustion(model="unsupported-xyz-99", engine=self.engine)

        self.assertFalse(result.success)
        self.assertIsNone(result.previous_account)
        self.assertIsNone(result.new_account)
        self.assertEqual(result.model, "unsupported-xyz-99")
        self.assertIn("Unsupported model family 'unsupported-xyz-99'", result.message)

    def test_auto_rotate_on_exhaustion_default_parameters(self) -> None:
        """auto_rotate_on_exhaustion defaults to model='gemini' and reload_server=False."""
        acc1_dir = self.engine.accounts_dir / "acc1"
        acc1_dir.mkdir(parents=True, exist_ok=True)
        (acc1_dir / "token.json").write_text(json.dumps(create_sample_token("acc1@example.com")), encoding="utf-8")

        acc2_dir = self.engine.accounts_dir / "acc2"
        acc2_dir.mkdir(parents=True, exist_ok=True)
        (acc2_dir / "token.json").write_text(json.dumps(create_sample_token("acc2@example.com")), encoding="utf-8")

        self.engine.set_active_name("acc1")

        cache_data = {
            "timestamp": 1700000000,
            "accounts": {
                "acc1": {"gemini": {"quota": 0}},
                "acc2": {"gemini": {"quota": 90}},
            },
        }
        self.engine.cache_file.write_text(json.dumps(cache_data), encoding="utf-8")

        with patch.object(self.engine, "restart_language_server") as mock_restart:
            result = api.auto_rotate_on_exhaustion(engine=self.engine)
            mock_restart.assert_not_called()

        self.assertTrue(result.success)
        self.assertEqual(result.model, "gemini")
        self.assertEqual(result.new_account, "acc2")
        self.assertFalse(result.reloaded_server)

    def test_auto_rotate_on_exhaustion_reload_server_busy_preserves_disk_tokens(self) -> None:
        """reload_server=True during active turn aborts safely without mutating disk tokens."""
        acc1_dir = self.engine.accounts_dir / "acc1"
        acc1_dir.mkdir(parents=True, exist_ok=True)
        acc1_token = acc1_dir / "token.json"
        acc1_token_data = create_sample_token("acc1@example.com")
        acc1_token.write_text(json.dumps(acc1_token_data), encoding="utf-8")

        acc2_dir = self.engine.accounts_dir / "acc2"
        acc2_dir.mkdir(parents=True, exist_ok=True)
        (acc2_dir / "token.json").write_text(json.dumps(create_sample_token("acc2@example.com")), encoding="utf-8")

        self.engine.switch_tokens("acc1", restart_server=False)

        cache_data = {
            "timestamp": 1700000000,
            "accounts": {
                "acc1": {"gemini": {"quota": 0}},
                "acc2": {"gemini": {"quota": 90}},
            },
        }
        self.engine.cache_file.write_text(json.dumps(cache_data), encoding="utf-8")

        with patch.object(self.engine, "locate_language_server_pid", return_value=9999), \
             patch.object(self.engine, "is_server_busy", return_value=True), \
             patch.object(self.engine, "restart_language_server") as mock_restart:
            result = api.auto_rotate_on_exhaustion(model="gemini", reload_server=True, engine=self.engine)
            mock_restart.assert_not_called()

        self.assertFalse(result.success)
        self.assertIsNone(result.new_account)
        self.assertIn("busy processing an agent turn", result.message)
        self.assertEqual(self.engine.get_active_name(), "acc1")

        desktop_data = json.loads(self.engine.desktop_token_file.read_text(encoding="utf-8"))
        cli_data = json.loads(self.engine.cli_token_file.read_text(encoding="utf-8"))
        self.assertEqual(extract_email_from_jwt(desktop_data["id_token"]), "acc1@example.com")
        self.assertEqual(extract_email_from_jwt(cli_data["id_token"]), "acc1@example.com")

    def test_auto_rotate_on_exhaustion_reload_server_idle_reloads_successfully(self) -> None:
        """reload_server=True reloads language server when server is idle."""
        acc1_dir = self.engine.accounts_dir / "acc1"
        acc1_dir.mkdir(parents=True, exist_ok=True)
        (acc1_dir / "token.json").write_text(json.dumps(create_sample_token("acc1@example.com")), encoding="utf-8")

        acc2_dir = self.engine.accounts_dir / "acc2"
        acc2_dir.mkdir(parents=True, exist_ok=True)
        (acc2_dir / "token.json").write_text(json.dumps(create_sample_token("acc2@example.com")), encoding="utf-8")

        self.engine.switch_tokens("acc1", restart_server=False)

        cache_data = {
            "timestamp": 1700000000,
            "accounts": {
                "acc1": {"gemini": {"quota": 0}},
                "acc2": {"gemini": {"quota": 90}},
            },
        }
        self.engine.cache_file.write_text(json.dumps(cache_data), encoding="utf-8")

        with patch.object(self.engine, "locate_language_server_pid", return_value=8888), \
             patch.object(self.engine, "is_server_busy", return_value=False), \
             patch.object(self.engine, "restart_language_server", return_value=True) as mock_restart:
            result = api.auto_rotate_on_exhaustion(model="gemini", reload_server=True, engine=self.engine)
            mock_restart.assert_called_once_with(force=False)

        self.assertTrue(result.success)
        self.assertEqual(result.new_account, "acc2")
        self.assertTrue(result.reloaded_server)
        self.assertEqual(self.engine.get_active_name(), "acc2")

    def test_switch_account_default_disk_only_swap(self) -> None:
        """[REQ-2-SCN-006] Disk-Only Swap swaps tokens without checking language server or presence locks."""
        acc1_dir = self.engine.accounts_dir / "acc1"
        acc1_dir.mkdir(parents=True, exist_ok=True)
        acc1_token = acc1_dir / "token.json"
        acc1_token.write_text(json.dumps(create_sample_token("acc1@example.com")), encoding="utf-8")

        acc2_dir = self.engine.accounts_dir / "acc2"
        acc2_dir.mkdir(parents=True, exist_ok=True)
        acc2_token = acc2_dir / "token.json"
        acc2_token.write_text(json.dumps(create_sample_token("acc2@example.com")), encoding="utf-8")

        self.engine.switch_tokens("acc1", restart_server=False)
        self.assertEqual(self.engine.get_active_name(), "acc1")

        with patch.object(self.engine, "locate_language_server_pid") as mock_locate, \
             patch.object(self.engine, "is_server_busy") as mock_busy, \
             patch.object(self.engine, "restart_language_server") as mock_restart:
            res = api.switch_account("acc2", reload_server=False, engine=self.engine)

            self.assertTrue(res)
            self.assertEqual(self.engine.get_active_name(), "acc2")
            mock_locate.assert_not_called()
            mock_busy.assert_not_called()
            mock_restart.assert_not_called()

            desktop_data = json.loads(self.engine.desktop_token_file.read_text(encoding="utf-8"))
            cli_data = json.loads(self.engine.cli_token_file.read_text(encoding="utf-8"))
            self.assertEqual(extract_email_from_jwt(desktop_data["id_token"]), "acc2@example.com")
            self.assertEqual(extract_email_from_jwt(cli_data["id_token"]), "acc2@example.com")

    def test_switch_account_default_reload_server_arg_is_false(self) -> None:
        """switch_account defaults to reload_server=False when omitted."""
        acc1_dir = self.engine.accounts_dir / "acc1"
        acc1_dir.mkdir(parents=True, exist_ok=True)
        (acc1_dir / "token.json").write_text(json.dumps(create_sample_token("acc1@example.com")), encoding="utf-8")
        self.engine.set_active_name("acc1")

        acc2_dir = self.engine.accounts_dir / "acc2"
        acc2_dir.mkdir(parents=True, exist_ok=True)
        (acc2_dir / "token.json").write_text(json.dumps(create_sample_token("acc2@example.com")), encoding="utf-8")

        with patch.object(self.engine, "locate_language_server_pid") as mock_locate, \
             patch.object(self.engine, "is_server_busy") as mock_busy, \
             patch.object(self.engine, "restart_language_server") as mock_restart:
            res = api.switch_account("acc2", engine=self.engine)
            self.assertTrue(res)
            self.assertEqual(self.engine.get_active_name(), "acc2")
            mock_locate.assert_not_called()
            mock_busy.assert_not_called()
            mock_restart.assert_not_called()

    def test_switch_account_busy_server_raises_active_turn_running_error(self) -> None:
        """[REQ-2-ERR-001] reload_server=True raises ActiveTurnRunningError during active turn and preserves disk tokens."""
        acc1_dir = self.engine.accounts_dir / "acc1"
        acc1_dir.mkdir(parents=True, exist_ok=True)
        (acc1_dir / "token.json").write_text(json.dumps(create_sample_token("acc1@example.com")), encoding="utf-8")

        acc2_dir = self.engine.accounts_dir / "acc2"
        acc2_dir.mkdir(parents=True, exist_ok=True)
        (acc2_dir / "token.json").write_text(json.dumps(create_sample_token("acc2@example.com")), encoding="utf-8")

        self.engine.switch_tokens("acc1", restart_server=False)
        self.assertEqual(self.engine.get_active_name(), "acc1")

        with patch.object(self.engine, "locate_language_server_pid", return_value=9999), \
             patch.object(self.engine, "is_server_busy", return_value=True):
            with self.assertRaises(ActiveTurnRunningError):
                api.switch_account("acc2", reload_server=True, engine=self.engine)

            self.assertEqual(self.engine.get_active_name(), "acc1")
            desktop_data = json.loads(self.engine.desktop_token_file.read_text(encoding="utf-8"))
            cli_data = json.loads(self.engine.cli_token_file.read_text(encoding="utf-8"))
            self.assertEqual(extract_email_from_jwt(desktop_data["id_token"]), "acc1@example.com")
            self.assertEqual(extract_email_from_jwt(cli_data["id_token"]), "acc1@example.com")

    def test_switch_account_presence_lock_raises_active_turn_running_error(self) -> None:
        """[REQ-2-ERR-001] Open presence lock raises ActiveTurnRunningError when reload_server=True."""
        acc1_dir = self.engine.accounts_dir / "acc1"
        acc1_dir.mkdir(parents=True, exist_ok=True)
        (acc1_dir / "token.json").write_text(json.dumps(create_sample_token("acc1@example.com")), encoding="utf-8")

        acc2_dir = self.engine.accounts_dir / "acc2"
        acc2_dir.mkdir(parents=True, exist_ok=True)
        (acc2_dir / "token.json").write_text(json.dumps(create_sample_token("acc2@example.com")), encoding="utf-8")

        self.engine.switch_tokens("acc1", restart_server=False)

        presence_dir = self.gemini_dir / "antigravity" / "presence"
        presence_dir.mkdir(parents=True, exist_ok=True)
        lock_file = presence_dir / "session.lock"
        lock_file.touch()

        pid_dir = self.proc_dir / "7777" / "fd"
        pid_dir.mkdir(parents=True, exist_ok=True)
        (pid_dir / "1").symlink_to(lock_file)

        with self.assertRaises(ActiveTurnRunningError):
            api.switch_account("acc2", reload_server=True, engine=self.engine)

        self.assertEqual(self.engine.get_active_name(), "acc1")

    def test_switch_account_idle_server_reloads_successfully(self) -> None:
        """reload_server=True reloads language server when server is idle and swaps tokens."""
        acc1_dir = self.engine.accounts_dir / "acc1"
        acc1_dir.mkdir(parents=True, exist_ok=True)
        (acc1_dir / "token.json").write_text(json.dumps(create_sample_token("acc1@example.com")), encoding="utf-8")

        acc2_dir = self.engine.accounts_dir / "acc2"
        acc2_dir.mkdir(parents=True, exist_ok=True)
        (acc2_dir / "token.json").write_text(json.dumps(create_sample_token("acc2@example.com")), encoding="utf-8")

        self.engine.switch_tokens("acc1", restart_server=False)

        with patch.object(self.engine, "locate_language_server_pid", return_value=8888), \
             patch.object(self.engine, "is_server_busy", return_value=False), \
             patch.object(self.engine, "restart_language_server", return_value=True) as mock_restart:
            res = api.switch_account("acc2", reload_server=True, engine=self.engine)
            self.assertTrue(res)
            self.assertEqual(self.engine.get_active_name(), "acc2")
            mock_restart.assert_called_once_with(force=False)

    def test_switch_account_non_existent_account_raises_account_not_found(self) -> None:
        """[REQ-2-ERR-002] Non-existent account raises AccountNotFoundError with expected message."""
        with self.assertRaises(AccountNotFoundError) as ctx:
            api.switch_account("ghost", engine=self.engine)

        self.assertIn("Account 'ghost' not found in vault", str(ctx.exception))

    def test_switch_account_empty_or_invalid_name_raises_account_not_found(self) -> None:
        """Empty or whitespace name raises AccountNotFoundError."""
        with self.assertRaises(AccountNotFoundError):
            api.switch_account("", engine=self.engine)

    def test_switch_account_default_engine(self) -> None:
        """api.switch_account uses default AgySwitchEngine when none provided."""
        acc_dir = self.engine.accounts_dir / "default_eng_acc"
        acc_dir.mkdir(parents=True, exist_ok=True)
        (acc_dir / "token.json").write_text(json.dumps(create_sample_token("eng@example.com")), encoding="utf-8")

        with patch("agy_switcher.api.AgySwitchEngine", return_value=self.engine):
            res = api.switch_account("default_eng_acc")
            self.assertTrue(res)
            self.assertEqual(self.engine.get_active_name(), "default_eng_acc")

    def test_switch_account_non_existent_with_reload_server_true_and_busy_server(self) -> None:
        """Non-existent account raises AccountNotFoundError before turn busy checks."""
        with patch.object(self.engine, "locate_language_server_pid", return_value=9999), \
             patch.object(self.engine, "is_server_busy", return_value=True):
            with self.assertRaises(AccountNotFoundError) as ctx:
                api.switch_account("ghost", reload_server=True, engine=self.engine)
            self.assertIn("Account 'ghost' not found in vault", str(ctx.exception))

    def test_switch_account_path_traversal_rejected(self) -> None:
        """Path traversal target raises AccountNotFoundError without escaping vault."""
        with self.assertRaises(AccountNotFoundError):
            api.switch_account("../etc", engine=self.engine)
        with self.assertRaises(AccountNotFoundError):
            api.switch_account("..", engine=self.engine)

    def test_switch_account_corrupt_token_file(self) -> None:
        """Account folder with corrupt token.json raises InvalidTokenError."""
        bad_dir = self.engine.accounts_dir / "bad_token_acc"
        bad_dir.mkdir(parents=True, exist_ok=True)
        (bad_dir / "token.json").write_text("{malformed: json", encoding="utf-8")

        with self.assertRaises(InvalidTokenError):
            api.switch_account("bad_token_acc", engine=self.engine)

    def test_switch_account_reload_server_when_no_server_running(self) -> None:
        """reload_server=True succeeds and returns True when language server is not running."""
        acc_dir = self.engine.accounts_dir / "no_srv_acc"
        acc_dir.mkdir(parents=True, exist_ok=True)
        (acc_dir / "token.json").write_text(json.dumps(create_sample_token("nosrv@example.com")), encoding="utf-8")

        with patch.object(self.engine, "locate_language_server_pid", return_value=None), \
             patch.object(self.engine, "is_server_busy", return_value=False):
            res = api.switch_account("no_srv_acc", reload_server=True, engine=self.engine)
            self.assertTrue(res)
            self.assertEqual(self.engine.get_active_name(), "no_srv_acc")

    def test_get_fleet_quota_summary_populated(self) -> None:
        """[REQ-2-SCN-008] get_fleet_quota_summary returns populated FleetQuotaSummary with cached telemetry."""
        acc1_dir = self.engine.accounts_dir / "acc1"
        acc1_dir.mkdir(parents=True, exist_ok=True)
        (acc1_dir / "token.json").write_text(json.dumps(create_sample_token("acc1@example.com")), encoding="utf-8")

        acc2_dir = self.engine.accounts_dir / "acc2"
        acc2_dir.mkdir(parents=True, exist_ok=True)
        (acc2_dir / "token.json").write_text(json.dumps(create_sample_token("acc2@example.com")), encoding="utf-8")

        self.engine.set_active_name("acc1")

        cache_ts = 1700000000.0
        cache_data = {
            "timestamp": cache_ts,
            "accounts": {
                "acc1": {
                    "cached_at": cache_ts,
                    "gemini": {"quota": 80, "reset_time": 1700003600},
                    "claude": {"quota": 60, "reset_time": 1700003600},
                },
                "acc2": {
                    "cached_at": cache_ts,
                    "gemini": {"quota": 45, "reset_time": 1700003600},
                    "claude": {"quota": 90, "reset_time": 1700003600},
                },
            },
        }
        self.engine.cache_dir.mkdir(parents=True, exist_ok=True)
        self.engine.cache_file.write_text(json.dumps(cache_data), encoding="utf-8")

        summary = api.get_fleet_quota_summary(model="all", engine=self.engine)

        self.assertIsInstance(summary, FleetQuotaSummary)
        self.assertEqual(summary.total_accounts, 2)
        self.assertEqual(summary.active_account, "acc1")
        self.assertEqual(summary.model, "all")
        self.assertEqual(summary.timestamp, cache_ts)
        self.assertIn("acc1", summary.accounts)
        self.assertIn("acc2", summary.accounts)
        self.assertEqual(summary.accounts["acc1"]["gemini"]["quota"], 80)
        self.assertEqual(summary.accounts["acc2"]["claude"]["quota"], 90)

    def test_get_fleet_quota_summary_model_filtering(self) -> None:
        """[REQ-2-SCN-008] get_fleet_quota_summary with model='gemini' extracts gemini quota details."""
        acc1_dir = self.engine.accounts_dir / "acc1"
        acc1_dir.mkdir(parents=True, exist_ok=True)
        (acc1_dir / "token.json").write_text(json.dumps(create_sample_token("acc1@example.com")), encoding="utf-8")

        self.engine.set_active_name("acc1")

        cache_ts = 1700000500.0
        cache_data = {
            "timestamp": cache_ts,
            "accounts": {
                "acc1": {
                    "cached_at": cache_ts,
                    "gemini": {"quota": 75, "reset_time": 1700003600},
                    "claude": {"quota": 20, "reset_time": 1700003600},
                },
            },
        }
        self.engine.cache_dir.mkdir(parents=True, exist_ok=True)
        self.engine.cache_file.write_text(json.dumps(cache_data), encoding="utf-8")

        summary = api.get_fleet_quota_summary(model="gemini", engine=self.engine)

        self.assertIsInstance(summary, FleetQuotaSummary)
        self.assertEqual(summary.total_accounts, 1)
        self.assertEqual(summary.active_account, "acc1")
        self.assertEqual(summary.model, "gemini")
        self.assertEqual(summary.timestamp, cache_ts)
        self.assertIn("acc1", summary.accounts)
        # Verify both direct quota key and gemini container key are populated
        self.assertEqual(summary.accounts["acc1"]["quota"], 75)
        self.assertEqual(summary.accounts["acc1"]["gemini"]["quota"], 75)

    def test_get_fleet_quota_summary_network_outage_fallback(self) -> None:
        """[REQ-2-EDGE-002] Telemetry network outage falls back to cached quota with timestamp preserved."""
        acc1_dir = self.engine.accounts_dir / "acc1"
        acc1_dir.mkdir(parents=True, exist_ok=True)
        (acc1_dir / "token.json").write_text(json.dumps(create_sample_token("acc1@example.com")), encoding="utf-8")

        self.engine.set_active_name("acc1")

        cache_ts = 1699999999.0
        cache_data = {
            "timestamp": cache_ts,
            "accounts": {
                "acc1": {
                    "cached_at": cache_ts,
                    "gemini": {"quota": 65, "reset_time": 1700003600},
                },
            },
        }
        self.engine.cache_dir.mkdir(parents=True, exist_ok=True)
        self.engine.cache_file.write_text(json.dumps(cache_data), encoding="utf-8")

        import email.message
        import urllib.error
        hdrs = email.message.Message()
        with patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError("https://daily-cloudcode-pa.googleapis.com", 503, "Service Unavailable", hdrs, None)):
            summary = api.get_fleet_quota_summary(model="gemini", engine=self.engine)

        self.assertIsInstance(summary, FleetQuotaSummary)
        self.assertEqual(summary.total_accounts, 1)
        self.assertEqual(summary.active_account, "acc1")
        self.assertEqual(summary.timestamp, cache_ts)
        self.assertEqual(summary.accounts["acc1"]["quota"], 65)

    def test_get_fleet_quota_summary_empty_vault_and_missing_cache(self) -> None:
        """get_fleet_quota_summary handles empty vault and missing cache file cleanly."""
        summary = api.get_fleet_quota_summary(engine=self.engine)
        self.assertIsInstance(summary, FleetQuotaSummary)
        self.assertEqual(summary.total_accounts, 0)
        self.assertIsNone(summary.active_account)
        self.assertEqual(summary.accounts, {})
        self.assertEqual(summary.model, "all")
        self.assertGreater(summary.timestamp, 0.0)

    def test_get_fleet_quota_summary_corrupted_cache(self) -> None:
        """get_fleet_quota_summary handles corrupted JSON cache without crashing."""
        acc1_dir = self.engine.accounts_dir / "acc1"
        acc1_dir.mkdir(parents=True, exist_ok=True)
        (acc1_dir / "token.json").write_text(json.dumps(create_sample_token("acc1@example.com")), encoding="utf-8")
        self.engine.set_active_name("acc1")

        self.engine.cache_dir.mkdir(parents=True, exist_ok=True)
        self.engine.cache_file.write_text("CORRUPTED NOT JSON {{{", encoding="utf-8")

        summary = api.get_fleet_quota_summary(engine=self.engine)
        self.assertIsInstance(summary, FleetQuotaSummary)
        self.assertEqual(summary.total_accounts, 1)
        self.assertEqual(summary.active_account, "acc1")
        self.assertEqual(summary.accounts, {"acc1": {}})
        self.assertGreater(summary.timestamp, 0.0)

    def test_get_fleet_quota_summary_dangling_active_pointer(self) -> None:
        """Dangling active pointer file resolved safely to active_account=None."""
        acc1_dir = self.engine.accounts_dir / "acc1"
        acc1_dir.mkdir(parents=True, exist_ok=True)
        (acc1_dir / "token.json").write_text(json.dumps(create_sample_token("acc1@example.com")), encoding="utf-8")

        self.engine.active_file.write_text("deleted_acc\n", encoding="utf-8")

        summary = api.get_fleet_quota_summary(engine=self.engine)
        self.assertIsNone(summary.active_account)
        self.assertEqual(summary.total_accounts, 1)

    def test_get_fleet_quota_summary_path_traversal_active_pointer(self) -> None:
        """Path traversal string in active file safely rejected to active_account=None."""
        self.engine.active_file.write_text("../../etc\n", encoding="utf-8")

        summary = api.get_fleet_quota_summary(engine=self.engine)
        self.assertIsNone(summary.active_account)

    def test_get_fleet_quota_summary_model_filtering_excludes_other_models(self) -> None:
        """Model filtering for claude does not leak gemini quota when claude is absent."""
        acc1_dir = self.engine.accounts_dir / "acc1"
        acc1_dir.mkdir(parents=True, exist_ok=True)
        (acc1_dir / "token.json").write_text(json.dumps(create_sample_token("acc1@example.com")), encoding="utf-8")

        cache_data = {
            "timestamp": 1700000000.0,
            "accounts": {
                "acc1": {
                    "cached_at": 1700000000.0,
                    "gemini": {"quota": 80, "reset_time": 1700003600},
                },
            },
        }
        self.engine.cache_dir.mkdir(parents=True, exist_ok=True)
        self.engine.cache_file.write_text(json.dumps(cache_data), encoding="utf-8")

        summary = api.get_fleet_quota_summary(model="claude", engine=self.engine)
        self.assertIn("acc1", summary.accounts)
        self.assertNotIn("gemini", summary.accounts["acc1"])
        self.assertNotIn("quota", summary.accounts["acc1"])
        self.assertEqual(summary.model, "claude")

    def test_get_fleet_quota_summary_per_account_cache_fallback(self) -> None:
        """Per-account directory last_known_quota.json is used when central cache is missing entry."""
        acc1_dir = self.engine.accounts_dir / "acc1"
        acc1_dir.mkdir(parents=True, exist_ok=True)
        (acc1_dir / "token.json").write_text(json.dumps(create_sample_token("acc1@example.com")), encoding="utf-8")

        per_acc_cache = acc1_dir / "last_known_quota.json"
        per_acc_cache.write_text(
            json.dumps({"gemini": {"quota": 95, "reset_time": 1700003600}}),
            encoding="utf-8",
        )

        summary = api.get_fleet_quota_summary(model="gemini", engine=self.engine)
        self.assertIn("acc1", summary.accounts)
        self.assertEqual(summary.accounts["acc1"]["gemini"]["quota"], 95)

    def test_get_fleet_quota_summary_cloudcode_raw_groups(self) -> None:
        """CloudCode raw groups schema parsed properly during model-filtered fleet summary."""
        acc1_dir = self.engine.accounts_dir / "acc1"
        acc1_dir.mkdir(parents=True, exist_ok=True)
        (acc1_dir / "token.json").write_text(json.dumps(create_sample_token("acc1@example.com")), encoding="utf-8")

        cache_data = {
            "timestamp": 1700000000.0,
            "accounts": {
                "acc1": {
                    "cached_at": 1700000000.0,
                    "groups": [
                        {
                            "displayName": "Gemini 1.5 Pro",
                            "buckets": [
                                {
                                    "remainingFraction": 0.65,
                                    "resetTime": "2026-11-06T12:00:00Z",
                                }
                            ],
                        }
                    ],
                },
            },
        }
        self.engine.cache_dir.mkdir(parents=True, exist_ok=True)
        self.engine.cache_file.write_text(json.dumps(cache_data), encoding="utf-8")

        summary = api.get_fleet_quota_summary(model="gemini", engine=self.engine)
        self.assertIn("acc1", summary.accounts)
        self.assertEqual(summary.accounts["acc1"]["quota"], 65)

    def test_get_fleet_quota_summary_empty_or_none_model_defaults_all(self) -> None:
        """None or whitespace model defaults cleanly to 'all'."""
        summary1 = api.get_fleet_quota_summary(model=None, engine=self.engine)  # type: ignore[arg-type]
        self.assertEqual(summary1.model, "all")

        summary2 = api.get_fleet_quota_summary(model="   ", engine=self.engine)
        self.assertEqual(summary2.model, "all")

    def test_get_fleet_quota_summary_nan_or_inf_timestamp_resilience(self) -> None:
        """NaN or infinite cache timestamp gracefully falls back to current POSIX time."""
        import math
        cache_data = {"timestamp": float("nan"), "accounts": {}}
        self.engine.cache_dir.mkdir(parents=True, exist_ok=True)
        self.engine.cache_file.write_text(json.dumps(cache_data), encoding="utf-8")

        summary = api.get_fleet_quota_summary(engine=self.engine)
        self.assertTrue(math.isfinite(summary.timestamp))
        self.assertGreater(summary.timestamp, 0.0)

    def test_top_level_package_exports_and_parity(self) -> None:
        """[REQ-2-SCN-007] Top-level agy_switcher package and agy_switch launcher re-export all api symbols with 100% parity."""
        import agy_switcher

        expected_symbols = [
            "get_active_account",
            "auto_rotate_on_exhaustion",
            "switch_account",
            "get_fleet_quota_summary",
            "AccountInfo",
            "RotationResult",
            "FleetQuotaSummary",
        ]

        # Verify api module has all symbols
        for sym in expected_symbols:
            self.assertTrue(hasattr(api, sym), f"api module missing symbol: {sym}")
            self.assertIn(sym, api.__all__, f"api.__all__ missing symbol: {sym}")

        # Verify agy_switcher package re-exports
        self.assertIs(agy_switcher.AccountInfo, AccountInfo)
        self.assertIs(agy_switcher.RotationResult, RotationResult)
        self.assertIs(agy_switcher.FleetQuotaSummary, FleetQuotaSummary)
        self.assertIs(agy_switcher.get_active_account, api.get_active_account)
        self.assertIs(agy_switcher.auto_rotate_on_exhaustion, api.auto_rotate_on_exhaustion)
        self.assertIs(agy_switcher.switch_account, api.switch_account)
        self.assertIs(agy_switcher.get_fleet_quota_summary, api.get_fleet_quota_summary)
        for sym in expected_symbols:
            self.assertIn(sym, agy_switcher.__all__, f"agy_switcher.__all__ missing symbol: {sym}")

        # Verify agy_switch launcher re-exports
        import agy_switch
        self.assertIs(agy_switch.AccountInfo, AccountInfo)
        self.assertIs(agy_switch.RotationResult, RotationResult)
        self.assertIs(agy_switch.FleetQuotaSummary, FleetQuotaSummary)
        self.assertIs(agy_switch.get_active_account, api.get_active_account)
        self.assertIs(agy_switch.auto_rotate_on_exhaustion, api.auto_rotate_on_exhaustion)
        self.assertIs(agy_switch.switch_account, api.switch_account)
        self.assertIs(agy_switch.get_fleet_quota_summary, api.get_fleet_quota_summary)
        for sym in expected_symbols:
            self.assertIn(sym, agy_switch.__all__, f"agy_switch.__all__ missing symbol: {sym}")


class TestSDKModels(unittest.TestCase):
    """Tests for SDK dataclasses in agy_switcher.models."""

    def test_account_info_defaults_and_dict(self) -> None:
        info = AccountInfo(name="my-account")
        self.assertEqual(info.name, "my-account")
        self.assertIsNone(info.email)
        self.assertFalse(info.is_active)
        self.assertEqual(info.status, "standby")
        self.assertIsNone(info.token_path)

        d = info.to_dict()
        self.assertEqual(d["name"], "my-account")
        self.assertEqual(d["status"], "standby")

    def test_rotation_result_model(self) -> None:
        result = RotationResult(
            success=True,
            previous_account="acc1",
            new_account="acc2",
            model="gemini",
            message="Rotation successful",
            quota_remaining=0.85,
            reloaded_server=False,
        )
        self.assertTrue(result.success)
        self.assertEqual(result.previous_account, "acc1")
        self.assertEqual(result.new_account, "acc2")
        self.assertEqual(result.model, "gemini")
        self.assertFalse(result.reloaded_server)

        d = result.to_dict()
        self.assertEqual(d["success"], True)
        self.assertEqual(d["quota_remaining"], 0.85)

    def test_rotation_result_post_init_preserves_none_new_account_on_failure(self) -> None:
        """When success=False, new_account remains None even if active_account is provided."""
        result = RotationResult(
            success=False,
            previous_account="acc1",
            active_account="acc1",
            new_account=None,
            model="gemini",
            message="Exhausted",
        )
        self.assertFalse(result.success)
        self.assertIsNone(result.new_account)
        self.assertEqual(result.active_account, "acc1")
        self.assertEqual(result.previous_account, "acc1")

    def test_rotation_result_post_init_sync_active_account(self) -> None:
        """When success=True and only active_account or new_account is passed, both synchronize."""
        res1 = RotationResult(success=True, active_account="acc2")
        self.assertEqual(res1.active_account, "acc2")
        self.assertEqual(res1.new_account, "acc2")

        res2 = RotationResult(success=True, new_account="acc3")
        self.assertEqual(res2.active_account, "acc3")
        self.assertEqual(res2.new_account, "acc3")

    def test_fleet_quota_summary_model(self) -> None:
        summary = FleetQuotaSummary(
            total_accounts=2,
            active_account="acc1",
            accounts={"acc1": {"quota": 1.0}, "acc2": {"quota": 0.5}},
            model="all",
            timestamp=1700000000.0,
        )
        self.assertEqual(summary.total_accounts, 2)
        self.assertEqual(summary.active_account, "acc1")
        d = summary.to_dict()
        self.assertEqual(d["total_accounts"], 2)
        self.assertEqual(d["model"], "all")
        # Ensure accounts dictionary copy isolation
        d["accounts"]["acc1"]["quota"] = 0.0
        self.assertEqual(summary.accounts["acc1"]["quota"], 0.0)

    def test_account_info_to_dict_token_path_edge_cases(self) -> None:
        """token_path variations (None, empty Path, valid Path, string) serialize cleanly."""
        info_empty_path = AccountInfo(name="acc", token_path=Path(""))
        self.assertIsNone(info_empty_path.to_dict()["token_path"])

        info_none = AccountInfo(name="acc", token_path=None)
        self.assertIsNone(info_none.to_dict()["token_path"])

        info_str = AccountInfo(name="acc", token_path="/var/token.json")
        self.assertEqual(info_str.to_dict()["token_path"], "/var/token.json")

    def test_list_accounts_and_get_accounts(self) -> None:
        """Verifies list_accounts and get_accounts return expected metadata and models."""
        from agy_switcher.api import list_accounts, get_accounts

        mock_engine = MagicMock()
        mock_engine.list_accounts.return_value = {
            "active": "acc1",
            "accounts": [
                {"name": "acc1", "email": "acc1@test.com", "valid": True, "is_active": True},
                {"name": "acc2", "email": "acc2@test.com", "valid": True, "is_active": False},
            ],
        }

        listed = list_accounts(engine=mock_engine)
        self.assertEqual(listed["active"], "acc1")
        self.assertEqual(len(listed["accounts"]), 2)

        acc_objs = get_accounts(engine=mock_engine)
        self.assertEqual(len(acc_objs), 2)
        self.assertEqual(acc_objs[0].name, "acc1")
        self.assertTrue(acc_objs[0].is_active)
        self.assertEqual(acc_objs[0].status, "active")
        self.assertEqual(acc_objs[1].name, "acc2")
        self.assertFalse(acc_objs[1].is_active)
        self.assertEqual(acc_objs[1].status, "standby")



if __name__ == "__main__":
    unittest.main()


