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
import unittest
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from agy_switcher.models import AccountInfo, RotationResult, FleetQuotaSummary
from agy_switcher.vault import AgySwitchEngine
import agy_switcher.api as api


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

    def test_top_level_package_exports_and_parity(self) -> None:
        """Top-level agy_switcher package re-exports models and api facade correctly."""
        import agy_switcher

        self.assertIs(agy_switcher.AccountInfo, AccountInfo)
        self.assertIs(agy_switcher.RotationResult, RotationResult)
        self.assertIs(agy_switcher.FleetQuotaSummary, FleetQuotaSummary)
        self.assertIs(agy_switcher.get_active_account, api.get_active_account)


if __name__ == "__main__":
    unittest.main()
