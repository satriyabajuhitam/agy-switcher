import io
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from agy_switch import (
    AgySwitchEngine,
    AgySwitchError,
    QuotaClient,
    RotationEngine,
    main,
    normalize_model_name,
)


class TestModelSteeringEngine(unittest.TestCase):
    """Unit tests for AgySwitchEngine model steering configuration and normalization."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.vault_dir = Path(self.tmp_dir.name) / "vault"
        self.gemini_dir = Path(self.tmp_dir.name) / "gemini"
        self.engine = AgySwitchEngine(
            vault_dir=self.vault_dir,
            gemini_dir=self.gemini_dir,
        )

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_normalize_model_name_valid(self):
        self.assertEqual(normalize_model_name("gemini"), "gemini")
        self.assertEqual(normalize_model_name("claude"), "claude")
        self.assertEqual(normalize_model_name("Gemini"), "gemini")
        self.assertEqual(normalize_model_name("CLAUDE"), "claude")
        self.assertEqual(normalize_model_name("gemini-3.8-pro"), "gemini")
        self.assertEqual(normalize_model_name("claude-3-5-sonnet"), "claude")
        self.assertEqual(normalize_model_name("claude-3-opus"), "claude")

    def test_normalize_model_name_unsupported(self):
        with self.assertRaises(AgySwitchError) as ctx:
            normalize_model_name("gpt-4")
        self.assertIn("Unsupported model 'gpt-4'. Allowed models: 'gemini', 'claude'.", str(ctx.exception))

        with self.assertRaises(AgySwitchError):
            normalize_model_name("")

        with self.assertRaises(AgySwitchError):
            normalize_model_name("gemini-claude")

    def test_get_default_model_fallback(self):
        """When config.json does not exist or has no default_model, defaults to 'gemini'."""
        self.assertEqual(self.engine.get_default_model(), "gemini")

    def test_set_and_get_default_model(self):
        """set_default_model persists setting and get_default_model retrieves it."""
        res = self.engine.set_default_model("claude")
        self.assertEqual(res.get("default_model"), "claude")
        self.assertEqual(self.engine.get_default_model(), "claude")

        # Verify config.json file content
        cfg = json.loads((self.vault_dir / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg.get("default_model"), "claude")

        # Update with slug normalization
        self.engine.set_default_model("gemini-3.8-pro")
        self.assertEqual(self.engine.get_default_model(), "gemini")

    def test_set_default_model_unsupported(self):
        """set_default_model rejects unsupported model with AgySwitchError."""
        with self.assertRaises(AgySwitchError) as ctx:
            self.engine.set_default_model("deepseek")
        self.assertIn("Unsupported model 'deepseek'. Allowed models: 'gemini', 'claude'.", str(ctx.exception))


class TestModelSteeringCLI(unittest.TestCase):
    """Integration and CLI tests for config default-model and switch --auto --model."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.vault_dir = Path(self.tmp_dir.name) / "vault"
        self.gemini_dir = Path(self.tmp_dir.name) / "gemini"
        self.engine = AgySwitchEngine(
            vault_dir=self.vault_dir,
            gemini_dir=self.gemini_dir,
        )
        # Mock account setup
        self.engine.list_accounts = MagicMock(return_value={
            "active": "acc1",
            "accounts": [
                {"name": "acc1", "is_active": True, "valid": True, "email": "acc1@example.com"},
                {"name": "acc2", "is_active": False, "valid": True, "email": "acc2@example.com"},
                {"name": "acc3", "is_active": False, "valid": True, "email": "acc3@example.com"},
            ]
        })
        self.engine.get_active_name = MagicMock(return_value="acc1")
        self.engine.switch_tokens = MagicMock(return_value={
            "active": "acc2",
            "email": "acc2@example.com",
            "server_restarted": False,
        })

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_cli_config_default_model_display(self):
        """agy-switch config default-model displays current default."""
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            code = main(["config", "default-model"], engine=self.engine)
        self.assertEqual(code, 0)
        self.assertIn("default-model is currently 'gemini'", buf.getvalue())

    def test_cli_config_default_model_set_valid(self):
        """agy-switch config default-model claude validates and persists."""
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            code = main(["config", "default-model", "claude"], engine=self.engine)
        self.assertEqual(code, 0)
        self.assertIn("default-model set to 'claude'", buf.getvalue())
        self.assertEqual(self.engine.get_default_model(), "claude")

    def test_cli_config_default_model_unsupported(self):
        """agy-switch config default-model invalid exits code 1 with error message."""
        buf = io.StringIO()
        with patch("sys.stderr", buf):
            code = main(["config", "default-model", "openai-gpt"], engine=self.engine)
        self.assertEqual(code, 1)
        self.assertIn("[agy-switch] Unsupported model 'openai-gpt'. Allowed models: 'gemini', 'claude'.", buf.getvalue())

    def test_cli_switch_auto_unsupported_model(self):
        """agy-switch switch --auto --model unsupported exits code 1 with error message."""
        buf = io.StringIO()
        with patch("sys.stderr", buf):
            code = main(["switch", "--auto", "--model", "llama-3"], engine=self.engine)
        self.assertEqual(code, 1)
        self.assertIn("[agy-switch] Unsupported model 'llama-3'. Allowed models: 'gemini', 'claude'.", buf.getvalue())

    @patch("agy_switch.fetch_all_accounts_quota")
    def test_resolution_hierarchy_cli_overrides_config(self, mock_fetch):
        """CLI --model overrides config.json default_model, which overrides fallback gemini."""
        self.engine.set_default_model("claude")
        mock_fetch.return_value = {
            "acc1": {"gemini": {"quota": 10, "reset_time": 1700000000}, "claude": {"quota": 80, "reset_time": 1700000000}},
            "acc2": {"gemini": {"quota": 90, "reset_time": 1700000000}, "claude": {"quota": 10, "reset_time": 1700000000}},
        }
        self.engine.list_accounts.return_value = {
            "active": "acc1",
            "accounts": [
                {"name": "acc1", "is_active": True, "valid": True},
                {"name": "acc2", "is_active": False, "valid": True},
            ]
        }

        # 1. Config says claude, but CLI explicitly requests --model gemini
        # acc1 has 10% gemini, acc2 has 90% gemini -> switches to acc2 for gemini
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            code = main(["switch", "--auto", "--model", "gemini"], engine=self.engine)
        self.assertEqual(code, 0)
        self.engine.switch_tokens.assert_called_with("acc2", restart_server=True, force=False)

    @patch("agy_switch.fetch_all_accounts_quota")
    def test_resolution_hierarchy_config_used_when_cli_flag_omitted(self, mock_fetch):
        """When CLI --model flag is omitted, default_model from config.json is used."""
        self.engine.set_default_model("claude")
        mock_fetch.return_value = {
            "acc1": {"gemini": {"quota": 90, "reset_time": 1700000000}, "claude": {"quota": 10, "reset_time": 1700000000}},
            "acc2": {"gemini": {"quota": 10, "reset_time": 1700000000}, "claude": {"quota": 80, "reset_time": 1700000000}},
        }
        self.engine.list_accounts.return_value = {
            "active": "acc1",
            "accounts": [
                {"name": "acc1", "is_active": True, "valid": True},
                {"name": "acc2", "is_active": False, "valid": True},
            ]
        }

        # Without --model, it should steer to Claude because config has default_model="claude"
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            code = main(["switch", "--auto"], engine=self.engine)
        self.assertEqual(code, 0)
        self.engine.switch_tokens.assert_called_with("acc2", restart_server=True, force=False)

    @patch("agy_switch.fetch_all_accounts_quota")
    def test_strict_quota_isolation_all_exhausted(self, mock_fetch):
        """If all accounts have verified 0% quota for target model, auto-switch aborts with code 1."""
        mock_fetch.return_value = {
            "acc1": {"gemini": {"quota": 90, "reset_time": 1700000000}, "claude": {"quota": 0, "reset_time": 1700000000}},
            "acc2": {"gemini": {"quota": 80, "reset_time": 1700000000}, "claude": {"quota": 0, "reset_time": 1700000000}},
        }
        self.engine.list_accounts.return_value = {
            "active": "acc1",
            "accounts": [
                {"name": "acc1", "is_active": True, "valid": True},
                {"name": "acc2", "is_active": False, "valid": True},
            ]
        }

        buf = io.StringIO()
        with patch("sys.stderr", buf):
            code = main(["switch", "--auto", "--model", "claude"], engine=self.engine)
        self.assertEqual(code, 1)
        self.assertIn("[agy-switch] All accounts exhausted for model 'claude'. Cannot auto-switch.", buf.getvalue())
        self.engine.switch_tokens.assert_not_called()

    @patch("agy_switch.fetch_all_accounts_quota")
    def test_quota_telemetry_absence_handling(self, mock_fetch):
        """If no accounts have telemetry data for target model, aborts with code 1."""
        mock_fetch.return_value = {
            "acc1": {"gemini": {"quota": 90, "reset_time": 1700000000}},
            "acc2": {"gemini": {"quota": 80, "reset_time": 1700000000}},
        }
        self.engine.list_accounts.return_value = {
            "active": "acc1",
            "accounts": [
                {"name": "acc1", "is_active": True, "valid": True},
                {"name": "acc2", "is_active": False, "valid": True},
            ]
        }

        buf = io.StringIO()
        with patch("sys.stderr", buf):
            code = main(["switch", "--auto", "--model", "claude"], engine=self.engine)
        self.assertEqual(code, 1)
        self.assertIn("[agy-switch] No accounts found with available quota telemetry for model 'claude'.", buf.getvalue())
        self.engine.switch_tokens.assert_not_called()

    @patch("agy_switch.fetch_all_accounts_quota")
    def test_model_aware_cooldown_claude_penalized_eligible_for_gemini(self, mock_fetch):
        """Account penalized for Claude exhaustion is still eligible for Gemini if Gemini quota > 15%."""
        now = time.time()
        # acc1 (active) is exhausted on Gemini (5%)
        # acc2 has 85% Gemini (> 15%), but is in cooldown for Claude
        mock_fetch.return_value = {
            "acc1": {"gemini": {"quota": 5, "reset_time": now + 3600}, "claude": {"quota": 0, "reset_time": now + 3600}},
            "acc2": {"gemini": {"quota": 85, "reset_time": now + 3600}, "claude": {"quota": 0, "reset_time": now + 3600}},
        }
        self.engine.list_accounts.return_value = {
            "active": "acc1",
            "accounts": [
                {"name": "acc1", "is_active": True, "valid": True},
                {"name": "acc2", "is_active": False, "valid": True},
            ]
        }

        # Write cooldown for acc2 (penalized for Claude)
        cooldown_data = {
            "acc2": {
                "cooldown_until": now + 1800,
                "reason": "RESOURCE_EXHAUSTED (code 429) for claude-3-5-sonnet",
                "exhausted_at": now,
            }
        }
        (self.vault_dir / "cache" / "cooldown.json").write_text(json.dumps(cooldown_data), encoding="utf-8")

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            code = main(["switch", "--auto", "--model", "gemini"], engine=self.engine)
        self.assertEqual(code, 0)
        self.engine.switch_tokens.assert_called_with("acc2", restart_server=True, force=False)

    @patch("agy_switch.fetch_all_accounts_quota")
    def test_model_aware_cooldown_gemini_penalized_disqualified_for_gemini(self, mock_fetch):
        """Account penalized for Gemini exhaustion is disqualified when switching for Gemini."""
        now = time.time()
        mock_fetch.return_value = {
            "acc1": {"gemini": {"quota": 0, "reset_time": now + 3600}},
            "acc2": {"gemini": {"quota": 85, "reset_time": now + 3600}},
        }
        self.engine.list_accounts.return_value = {
            "active": "acc1",
            "accounts": [
                {"name": "acc1", "is_active": True, "valid": True},
                {"name": "acc2", "is_active": False, "valid": True},
            ]
        }

        # Write cooldown for acc2 specifically for Gemini
        cooldown_data = {
            "acc2": {
                "cooldown_until": now + 1800,
                "reason": "RESOURCE_EXHAUSTED for gemini-3.8-pro",
                "exhausted_at": now,
            }
        }
        (self.vault_dir / "cache" / "cooldown.json").write_text(json.dumps(cooldown_data), encoding="utf-8")

        buf = io.StringIO()
        with patch("sys.stderr", buf):
            code = main(["switch", "--auto", "--model", "gemini"], engine=self.engine)
        self.assertEqual(code, 1)
        self.assertIn("All accounts exhausted for model 'gemini'", buf.getvalue())
        self.engine.switch_tokens.assert_not_called()

    @patch("agy_switch.fetch_all_accounts_quota")
    def test_model_aware_cooldown_unhealthy_gemini_disqualified(self, mock_fetch):
        """Account with Claude cooldown and unhealthy Gemini quota (<= 15%) is disqualified."""
        now = time.time()
        mock_fetch.return_value = {
            "acc1": {"gemini": {"quota": 0, "reset_time": now + 3600}, "claude": {"quota": 0, "reset_time": now + 3600}},
            "acc2": {"gemini": {"quota": 10, "reset_time": now + 3600}, "claude": {"quota": 0, "reset_time": now + 3600}},
        }
        self.engine.list_accounts.return_value = {
            "active": "acc1",
            "accounts": [
                {"name": "acc1", "is_active": True, "valid": True},
                {"name": "acc2", "is_active": False, "valid": True},
            ]
        }

        cooldown_data = {
            "acc2": {
                "cooldown_until": now + 1800,
                "reason": "RESOURCE_EXHAUSTED (code 429) for claude-3-5-sonnet",
                "exhausted_at": now,
            }
        }
        (self.vault_dir / "cache" / "cooldown.json").write_text(json.dumps(cooldown_data), encoding="utf-8")

        buf = io.StringIO()
        with patch("sys.stderr", buf):
            code = main(["switch", "--auto", "--model", "gemini"], engine=self.engine)
        self.assertEqual(code, 1)
        self.engine.switch_tokens.assert_not_called()

    @patch("agy_switch.fetch_all_accounts_quota")
    def test_gemini_penalized_with_claude_zero_remains_disqualified(self, mock_fetch):
        """Account penalized explicitly for Gemini remains disqualified even if Claude quota is 0%."""
        now = time.time()
        # acc2 has Gemini 85% and Claude 0%, but cooldown reason is explicitly for Gemini
        mock_fetch.return_value = {
            "acc1": {"gemini": {"quota": 0, "reset_time": now + 3600}},
            "acc2": {"gemini": {"quota": 85, "reset_time": now + 3600}, "claude": {"quota": 0, "reset_time": now + 3600}},
        }
        self.engine.list_accounts.return_value = {
            "active": "acc1",
            "accounts": [
                {"name": "acc1", "is_active": True, "valid": True},
                {"name": "acc2", "is_active": False, "valid": True},
            ]
        }
        cooldown_data = {
            "acc2": {
                "cooldown_until": now + 1800,
                "reason": "RESOURCE_EXHAUSTED for gemini-3.8-pro",
                "exhausted_at": now,
            }
        }
        (self.vault_dir / "cache" / "cooldown.json").write_text(json.dumps(cooldown_data), encoding="utf-8")

        buf = io.StringIO()
        with patch("sys.stderr", buf):
            code = main(["switch", "--auto", "--model", "gemini"], engine=self.engine)
        self.assertEqual(code, 1)
        self.assertIn("All accounts exhausted for model 'gemini'", buf.getvalue())
        self.engine.switch_tokens.assert_not_called()

    @patch("agy_switch.fetch_all_accounts_quota")
    def test_candidate_ranking_multi_account_claude(self, mock_fetch):
        """When multiple candidates are available for Claude, ranks by Claude quota."""
        now = time.time()
        mock_fetch.return_value = {
            "acc1": {"gemini": {"quota": 90, "reset_time": now + 3600}, "claude": {"quota": 5, "reset_time": now + 3600}},
            "acc2": {"gemini": {"quota": 10, "reset_time": now + 3600}, "claude": {"quota": 85, "reset_time": now + 3600}},
            "acc3": {"gemini": {"quota": 95, "reset_time": now + 3600}, "claude": {"quota": 50, "reset_time": now + 3600}},
        }
        self.engine.list_accounts.return_value = {
            "active": "acc1",
            "accounts": [
                {"name": "acc1", "is_active": True, "valid": True},
                {"name": "acc2", "is_active": False, "valid": True},
                {"name": "acc3", "is_active": False, "valid": True},
            ]
        }

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            code = main(["switch", "--auto", "--model", "claude"], engine=self.engine)
        self.assertEqual(code, 0)
        # Should select acc2 (85% Claude), not acc3 (which has higher Gemini 95%)
        self.engine.switch_tokens.assert_called_with("acc2", restart_server=True, force=False)

    @patch("agy_switch.fetch_all_accounts_quota")
    def test_active_account_optimal_for_claude(self, mock_fetch):
        """Active account with healthy Claude quota (> 15%) is retained as optimal."""
        now = time.time()
        mock_fetch.return_value = {
            "acc1": {"gemini": {"quota": 10, "reset_time": now + 3600}, "claude": {"quota": 90, "reset_time": now + 3600}},
            "acc2": {"gemini": {"quota": 90, "reset_time": now + 3600}, "claude": {"quota": 50, "reset_time": now + 3600}},
        }
        self.engine.list_accounts.return_value = {
            "active": "acc1",
            "accounts": [
                {"name": "acc1", "is_active": True, "valid": True},
                {"name": "acc2", "is_active": False, "valid": True},
            ]
        }

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            code = main(["switch", "--auto", "--model", "claude"], engine=self.engine)
        self.assertEqual(code, 0)
        self.assertIn("Active account 'acc1' is optimal. No switch needed.", buf.getvalue())
        self.engine.switch_tokens.assert_not_called()

    @patch("agy_switch.fetch_all_accounts_quota")
    def test_active_account_in_cooldown_not_retained(self, mock_fetch):
        """Active account listed in cooldown is not retained even if cached quota is > 15%."""
        now = time.time()
        mock_fetch.return_value = {
            "acc1": {"gemini": {"quota": 50, "reset_time": now + 3600}},
            "acc2": {"gemini": {"quota": 80, "reset_time": now + 3600}},
        }
        self.engine.list_accounts.return_value = {
            "active": "acc1",
            "accounts": [
                {"name": "acc1", "is_active": True, "valid": True},
                {"name": "acc2", "is_active": False, "valid": True},
            ]
        }
        cooldown_data = {
            "acc1": {
                "cooldown_until": now + 1800,
                "reason": "RESOURCE_EXHAUSTED",
                "exhausted_at": now,
            }
        }
        (self.vault_dir / "cache" / "cooldown.json").write_text(json.dumps(cooldown_data), encoding="utf-8")

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            code = main(["switch", "--auto", "--model", "gemini"], engine=self.engine)
        self.assertEqual(code, 0)
        self.engine.switch_tokens.assert_called_with("acc2", restart_server=True, force=False)

    def test_rotation_engine_evaluate_default_model_parameter(self):
        """RotationEngine respects instance model_name when target_model is not explicitly passed."""
        rot = RotationEngine(model_name="claude", active_account_id="acc1")
        accounts = [
            {"name": "acc1", "quota": {"gemini": {"quota": 90}, "claude": {"quota": 5}}},
            {"name": "acc2", "quota": {"gemini": {"quota": 10}, "claude": {"quota": 80}}},
        ]
        target = rot.evaluate(accounts, current_active="acc1")
        # Should evaluate Claude and select acc2 (not stay on acc1 for Gemini)
        self.assertEqual(target, "acc2")

    def test_legacy_flat_quota_not_used_for_claude(self):
        """Flat legacy quota dictionary is not considered valid telemetry for Claude."""
        rot = RotationEngine(model_name="claude")
        legacy_data = {"quota": 80, "reset_time": 1700000000}
        self.assertIsNone(rot.get_quota_status(legacy_data, model_key="claude"))
        # But valid for gemini
        self.assertIsNotNone(rot.get_quota_status(legacy_data, model_key="gemini"))

    def test_corrupt_config_json_fallback(self):
        """Corrupt config.json gracefully falls back to 'gemini'."""
        (self.vault_dir / "config.json").write_text("invalid json{{{", encoding="utf-8")
        self.assertEqual(self.engine.get_default_model(), "gemini")

    @patch("agy_switch.fetch_all_accounts_quota")

    def test_active_account_in_cooldown_no_standby_aborts_code_1(self, mock_fetch):
        """Active account in cooldown with positive quota aborts with code 1 when no eligible standby exists."""
        now = time.time()
        mock_fetch.return_value = {
            "acc1": {"gemini": {"quota": 50, "reset_time": now + 3600}},
        }
        self.engine.list_accounts.return_value = {
            "active": "acc1",
            "accounts": [
                {"name": "acc1", "is_active": True, "valid": True},
            ]
        }
        cooldown_data = {
            "acc1": {
                "cooldown_until": now + 1800,
                "reason": "RESOURCE_EXHAUSTED",
                "exhausted_at": now,
            }
        }
        (self.vault_dir / "cache" / "cooldown.json").write_text(json.dumps(cooldown_data), encoding="utf-8")

        buf = io.StringIO()
        with patch("sys.stderr", buf):
            code = main(["switch", "--auto", "--model", "gemini"], engine=self.engine)
        self.assertEqual(code, 1)
        self.assertNotIn("is optimal", buf.getvalue())
        self.assertIn("All accounts exhausted for model 'gemini'. Cannot auto-switch.", buf.getvalue())
        self.engine.switch_tokens.assert_not_called()

    def test_is_cooldown_penalized_for_model_helper(self):
        """Verifies _is_cooldown_penalized_for_model helper logic for Gemini and Claude."""
        rot = RotationEngine()
        # 1. fraction <= 0.15 is always penalized
        self.assertTrue(rot._is_cooldown_penalized_for_model("gemini", 0.10, {}, {}))
        self.assertTrue(rot._is_cooldown_penalized_for_model("claude", 0.10, {}, {}))

        # 2. Gemini target: Claude cooldown reason -> exempt (False)
        claude_rec = {"reason": "RESOURCE_EXHAUSTED for claude-3-5-sonnet"}
        self.assertFalse(rot._is_cooldown_penalized_for_model("gemini", 0.80, claude_rec, {}))

        # 3. Gemini target: Gemini cooldown reason -> penalized (True)
        gemini_rec = {"reason": "RESOURCE_EXHAUSTED for gemini-3.8-pro"}
        self.assertTrue(rot._is_cooldown_penalized_for_model("gemini", 0.80, gemini_rec, {}))

        # 4. Claude target: Gemini cooldown reason -> exempt (False)
        self.assertFalse(rot._is_cooldown_penalized_for_model("claude", 0.80, gemini_rec, {}))

        # 5. Claude target: Claude cooldown reason -> penalized (True)
        self.assertTrue(rot._is_cooldown_penalized_for_model("claude", 0.80, claude_rec, {}))


if __name__ == "__main__":
    unittest.main()

