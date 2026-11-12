import unittest
from unittest.mock import patch
from datetime import datetime, timezone
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from agy_switch import (
    RotationEngine,
    QuotaStatus,
    fetch_account_quota,
    fetch_all_accounts_quota,
)

class TestRotationEngine(unittest.TestCase):
    def setUp(self):
        # Base time for testing
        self.base_time = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)
        
    def _make_quota(self, gemini_frac=1.0, gemini_reset=None, claude_frac=1.0, claude_reset=None, gemini_weekly=None, claude_weekly=None):
        def format_timestamp(minutes_offset):
            if minutes_offset is None:
                minutes_offset = 120
            from datetime import timedelta
            t = self.base_time + timedelta(minutes=minutes_offset)
            return int(t.timestamp())

        data = {}
        if gemini_frac is not None:
            frac = gemini_frac
            if gemini_weekly is not None and gemini_weekly < frac:
                frac = gemini_weekly
            gem_reset_val = gemini_reset if gemini_reset is not None else 120
            data["gemini"] = {
                "quota": int(round(frac * 100)),
                "reset_time": format_timestamp(gem_reset_val),
            }
            
        if claude_frac is not None:
            frac = claude_frac
            if claude_weekly is not None and claude_weekly < frac:
                frac = claude_weekly
            claude_reset_val = claude_reset if claude_reset is not None else 120
            data["claude"] = {
                "quota": int(round(frac * 100)),
                "reset_time": format_timestamp(claude_reset_val),
            }
            
        return data

    def test_worst_case_evaluation(self):
        engine = RotationEngine(model_name="gemini-1.5-pro", active_account_id="acc1")
        accounts = [
            {"name": "acc1", "quota": self._make_quota(gemini_frac=0.8, gemini_weekly=0.1)}, # worst case is 10%
            {"name": "acc2", "quota": self._make_quota(gemini_frac=0.9, gemini_weekly=0.9)}, # 90%
        ]
        
        result = engine.evaluate(accounts, current_active="acc1", target_model="gemini", current_time=self.base_time)
        self.assertEqual(result, "acc2")

    def test_cache_optimized_retain(self):
        # > 25% should retain active account even if another account is higher -> returns None
        engine = RotationEngine(model_name="gemini-1.5-pro", active_account_id="acc1")
        accounts = [
            {"name": "acc1", "quota": self._make_quota(gemini_frac=0.26)},
            {"name": "acc2", "quota": self._make_quota(gemini_frac=1.0)},
        ]
        result = engine.evaluate(accounts, current_active="acc1", target_model="gemini", current_time=self.base_time)
        self.assertIsNone(result)

    def test_drain_window_retain(self):
        # < 25% but > 5% and reset in <= 30m -> retain active account -> returns None
        engine = RotationEngine(model_name="gemini-1.5-pro", active_account_id="acc1")
        accounts = [
            {"name": "acc1", "quota": self._make_quota(gemini_frac=0.06, gemini_reset=29)},
            {"name": "acc2", "quota": self._make_quota(gemini_frac=1.0, gemini_reset=120)},
        ]
        result = engine.evaluate(accounts, current_active="acc1", target_model="gemini", current_time=self.base_time)
        self.assertIsNone(result)

    def test_drain_window_preservation_active_retained(self):
        """[WATCH-PROACTIVE-003]: Active account with 12% quota and reset in 18m is retained."""
        engine = RotationEngine(model_name="gemini-1.5-pro", active_account_id="primary-acc", threshold=0.20)
        accounts = [
            {"name": "primary-acc", "quota": self._make_quota(gemini_frac=0.12, gemini_reset=18)},
            {"name": "standby-acc", "quota": self._make_quota(gemini_frac=0.95, gemini_reset=120)},
        ]
        result = engine.evaluate(accounts, current_active="primary-acc", target_model="gemini", current_time=self.base_time)
        self.assertIsNone(result)

    def test_drain_window_hard_floor_override_rotates(self):
        """[WATCH-PROACTIVE-EDGE-002]: Active account with 4% quota inside drain window overrides retention and rotates."""
        engine = RotationEngine(model_name="gemini-1.5-pro", active_account_id="primary-acc", threshold=0.20)
        accounts = [
            {"name": "primary-acc", "quota": self._make_quota(gemini_frac=0.04, gemini_reset=10)},
            {"name": "standby-acc", "quota": self._make_quota(gemini_frac=0.95, gemini_reset=120)},
        ]
        result = engine.evaluate(accounts, current_active="primary-acc", target_model="gemini", current_time=self.base_time)
        self.assertEqual(result, "standby-acc")

    def test_drain_window_exact_5_percent_hard_floor_override(self):
        """Active account with exact 5% quota overrides drain window retention and rotates."""
        engine = RotationEngine(model_name="gemini-1.5-pro", active_account_id="primary-acc", threshold=0.20)
        accounts = [
            {"name": "primary-acc", "quota": self._make_quota(gemini_frac=0.05, gemini_reset=10)},
            {"name": "standby-acc", "quota": self._make_quota(gemini_frac=0.95, gemini_reset=120)},
        ]
        result = engine.evaluate(accounts, current_active="primary-acc", target_model="gemini", current_time=self.base_time)
        self.assertEqual(result, "standby-acc")

    def test_drain_window_exact_30_minutes_boundary(self):
        """Active account with 12% quota and reset in exact 30.0m is retained."""
        engine = RotationEngine(model_name="gemini-1.5-pro", active_account_id="primary-acc", threshold=0.20)
        accounts = [
            {"name": "primary-acc", "quota": self._make_quota(gemini_frac=0.12, gemini_reset=30)},
            {"name": "standby-acc", "quota": self._make_quota(gemini_frac=0.95, gemini_reset=120)},
        ]
        result = engine.evaluate(accounts, current_active="primary-acc", target_model="gemini", current_time=self.base_time)
        self.assertIsNone(result)

    def test_drain_window_outside_30_minutes_boundary(self):
        """Active account with 12% quota and reset in 31m (> 30m) is rotated."""
        engine = RotationEngine(model_name="gemini-1.5-pro", active_account_id="primary-acc", threshold=0.20)
        accounts = [
            {"name": "primary-acc", "quota": self._make_quota(gemini_frac=0.12, gemini_reset=31)},
            {"name": "standby-acc", "quota": self._make_quota(gemini_frac=0.95, gemini_reset=120)},
        ]
        result = engine.evaluate(accounts, current_active="primary-acc", target_model="gemini", current_time=self.base_time)
        self.assertEqual(result, "standby-acc")

    def test_imminent_reset_priority(self):
        """Standby account nearing reset with healthy quota (>= 25%) is prioritized."""
        engine = RotationEngine(model_name="gemini-1.5-pro", active_account_id="acc1")
        accounts = [
            {"name": "acc1", "quota": self._make_quota(gemini_frac=0.05, gemini_reset=60)},
            {"name": "acc2", "quota": self._make_quota(gemini_frac=0.50, gemini_reset=120)},
            {"name": "acc3", "quota": self._make_quota(gemini_frac=0.30, gemini_reset=40)},
        ]
        result = engine.evaluate(accounts, current_active="acc1", target_model="gemini", current_time=self.base_time)
        self.assertEqual(result, "acc3")

    def test_accounts_100_percent_override_11_percent_imminent(self):
        """Accounts with 100% quota correctly override 11% imminent-reset accounts."""
        engine = RotationEngine(model_name="gemini-1.5-pro", active_account_id="acc1")
        accounts = [
            {"name": "acc1", "quota": self._make_quota(gemini_frac=0.05, gemini_reset=60)},
            {"name": "acc2", "quota": self._make_quota(gemini_frac=1.00, gemini_reset=120)},
            {"name": "acc3", "quota": self._make_quota(gemini_frac=0.11, gemini_reset=40)},
        ]
        result = engine.evaluate(accounts, current_active="acc1", target_model="gemini", current_time=self.base_time)
        self.assertEqual(result, "acc2")

    def test_imminent_reset_too_low(self):
        """Imminent accounts with < 15% quota do not get priority over higher quota accounts."""
        engine = RotationEngine(model_name="gemini-1.5-pro", active_account_id="acc1")
        accounts = [
            {"name": "acc1", "quota": self._make_quota(gemini_frac=0.05, gemini_reset=60)},
            {"name": "acc2", "quota": self._make_quota(gemini_frac=0.50, gemini_reset=120)},
            {"name": "acc3", "quota": self._make_quota(gemini_frac=0.11, gemini_reset=40)},
        ]
        result = engine.evaluate(accounts, current_active="acc1", target_model="gemini", current_time=self.base_time)
        self.assertEqual(result, "acc2")

    def test_candidates_with_zero_quota_ignored(self):
        """Candidates with 0% quota must be ignored and never selected as fallback."""
        engine = RotationEngine(model_name="gemini-1.5-pro", active_account_id="acc1")
        accounts = [
            {"name": "acc1", "quota": self._make_quota(gemini_frac=0.05, gemini_reset=60)},
            {"name": "acc2", "quota": self._make_quota(gemini_frac=0.00, gemini_reset=120)},
            {"name": "acc3", "quota": self._make_quota(gemini_frac=0.00, gemini_reset=30)},
        ]
        result = engine.evaluate(accounts, current_active="acc1", target_model="gemini", current_time=self.base_time)
        self.assertIsNone(result)

        accounts.append({"name": "acc4", "quota": self._make_quota(gemini_frac=0.30, gemini_reset=120)})
        result = engine.evaluate(accounts, current_active="acc1", target_model="gemini", current_time=self.base_time)
        self.assertEqual(result, "acc4")

    def test_active_account_excluded_from_fallback(self):
        """Active account is explicitly excluded from being selected as the fallback."""
        engine = RotationEngine(model_name="gemini-1.5-pro", active_account_id="acc1")
        accounts = [
            {"name": "acc1", "quota": self._make_quota(gemini_frac=0.12, gemini_reset=60)},
            {"name": "acc2", "quota": self._make_quota(gemini_frac=0.10, gemini_reset=120)},
        ]
        result = engine.evaluate(accounts, current_active="acc1", target_model="gemini", current_time=self.base_time)
        self.assertEqual(result, "acc2")

    def test_all_standby_below_15_picks_highest_absolute_quota(self):
        """When all standby accounts have < 15% quota, pick the highest absolute quota."""
        engine = RotationEngine(model_name="gemini-1.5-pro", active_account_id="acc1")
        accounts = [
            {"name": "acc1", "quota": self._make_quota(gemini_frac=0.02, gemini_reset=60)},
            {"name": "acc2", "quota": self._make_quota(gemini_frac=0.08, gemini_reset=120)},
            {"name": "acc3", "quota": self._make_quota(gemini_frac=0.12, gemini_reset=40)},
        ]
        result = engine.evaluate(accounts, current_active="acc1", target_model="gemini", current_time=self.base_time)
        self.assertEqual(result, "acc3")

    def test_tie_break_by_earliest_reset_delta(self):
        """When two candidate accounts have equal highest quota, tie-break by earliest reset time."""
        engine = RotationEngine(model_name="gemini-1.5-pro", active_account_id="acc1")
        accounts = [
            {"name": "acc1", "quota": self._make_quota(gemini_frac=0.05, gemini_reset=60)},
            {"name": "acc2", "quota": self._make_quota(gemini_frac=1.00, gemini_reset=120)},
            {"name": "acc3", "quota": self._make_quota(gemini_frac=1.00, gemini_reset=50)},
        ]
        result = engine.evaluate(accounts, current_active="acc1", target_model="gemini", current_time=self.base_time)
        self.assertEqual(result, "acc3")

    def test_target_claude_model(self):
        engine = RotationEngine(model_name="claude-3-5-sonnet", active_account_id="acc1")
        accounts = [
            {"name": "acc1", "quota": self._make_quota(gemini_frac=1.0, claude_frac=0.10)},
            {"name": "acc2", "quota": self._make_quota(gemini_frac=0.10, claude_frac=1.0)},
        ]
        result = engine.evaluate(accounts, current_active="acc1", target_model="claude", current_time=self.base_time)
        self.assertEqual(result, "acc2")

    def test_no_valid_accounts(self):
        engine = RotationEngine(model_name="gemini-1.5-pro", active_account_id="acc1")
        accounts = [
            {"name": "acc1", "quota": {}},
            {"name": "acc2", "quota": {}},
        ]
        result = engine.evaluate(accounts, current_active="acc1", target_model="gemini", current_time=self.base_time)
        self.assertIsNone(result)

    def test_reset_delta_calculated_once_per_account(self):
        """reset_delta must only be calculated once per account status during evaluate."""
        engine = RotationEngine(model_name="gemini-1.5-pro", active_account_id="acc1")
        accounts = [
            # fraction 0.06 (>0.05) checks drain window, but reset=35 (>30) so continues to step 3 loop
            {"name": "acc1", "quota": self._make_quota(gemini_frac=0.06, gemini_reset=35)},
            {"name": "acc2", "quota": self._make_quota(gemini_frac=0.50, gemini_reset=120)},
        ]

        with patch.object(engine, "calculate_reset_delta", wraps=engine.calculate_reset_delta) as spy:
            result = engine.evaluate(accounts, current_active="acc1", target_model="gemini", current_time=self.base_time)

        self.assertEqual(result, "acc2")
        self.assertEqual(spy.call_count, 2, f"Expected exactly 2 reset_delta calculations (1 per account), got {spy.call_count}")

    def test_quota_status_encapsulates_reset_delta_minutes(self):
        """QuotaStatus must encapsulate its own reset delta calculation in minutes."""
        from datetime import timedelta
        reset_time = self.base_time + timedelta(minutes=45)
        status = QuotaStatus(fraction=0.5, reset_time=reset_time)
        delta = status.get_reset_delta_minutes(self.base_time)
        self.assertAlmostEqual(delta, 45.0)

    def test_quota_status_semantic_methods(self):
        """QuotaStatus provides semantic helper methods instead of primitive fraction access."""
        from datetime import timedelta
        reset_time = self.base_time + timedelta(minutes=30)
        
        status_full = QuotaStatus(fraction=1.0, reset_time=reset_time)
        self.assertFalse(status_full.is_exhausted())
        self.assertTrue(status_full.has_available_quota())
        self.assertEqual(status_full.remaining_percentage(), 100)

        status_zero = QuotaStatus(fraction=0.0, reset_time=reset_time)
        self.assertTrue(status_zero.is_exhausted())
        self.assertFalse(status_zero.has_available_quota())
        self.assertEqual(status_zero.remaining_percentage(), 0)

        status_partial = QuotaStatus(fraction=0.456, reset_time=reset_time)
        self.assertFalse(status_partial.is_exhausted())
        self.assertTrue(status_partial.has_available_quota())
        self.assertEqual(status_partial.remaining_percentage(), 45)

    def test_rotation_engine_has_available_quota(self):
        """RotationEngine provides has_available_quota encapsulating status retrieval."""
        engine = RotationEngine(model_name="gemini-1.5-pro", active_account_id="acc1")
        has_quota_data = self._make_quota(gemini_frac=0.5)
        zero_quota_data = self._make_quota(gemini_frac=0.0)
        empty_data = {}

        self.assertTrue(engine.has_available_quota(has_quota_data))
        self.assertFalse(engine.has_available_quota(zero_quota_data))
        self.assertFalse(engine.has_available_quota(empty_data))

    def test_evaluate_with_dict_accounts(self):
        """RotationEngine.evaluate supports dict mapping of account_id -> quota dict."""
        engine = RotationEngine(model_name="gemini-1.5-pro", active_account_id="acc1")
        accounts = {
            "acc1": self._make_quota(gemini_frac=0.10),
            "acc2": self._make_quota(gemini_frac=0.90),
        }
        result = engine.evaluate(accounts, current_active="acc1", target_model="gemini", current_time=self.base_time)
        self.assertEqual(result, "acc2")

    def test_evaluate_schema_from_cache_file(self):
        """RotationEngine.evaluate supports full last_known_quota.json cache dictionary."""
        engine = RotationEngine(model_name="gemini-1.5-pro", active_account_id="acc1")
        cache_data = {
            "timestamp": int(self.base_time.timestamp()),
            "accounts": {
                "acc1": self._make_quota(gemini_frac=0.80),
                "acc2": self._make_quota(gemini_frac=0.20),
            }
        }
        # acc1 is optimal (>15% and active), evaluate returns None
        result = engine.evaluate(cache_data, current_active="acc1", target_model="gemini", current_time=self.base_time)
        self.assertIsNone(result)

    def test_fetch_account_quota_helpers(self):
        """fetch_account_quota and fetch_all_accounts_quota extract clean quota dicts."""
        from unittest.mock import MagicMock
        mock_engine = MagicMock()
        mock_engine.get_account_tokens.side_effect = lambda name: {
            "acc1": {"access_token": "token1", "refresh_token": "r1"},
            "acc2": {"access_token": "token2", "refresh_token": "r2"},
            "no_token": None,
        }.get(name)

        mock_client = MagicMock()
        mock_client.fetch_quota.side_effect = lambda tok, refresh_token=None: {
            "token1": {"gemini": {"quota": 80, "reset_time": 1700000000}},
            "token2": {"gemini": {"quota": 90, "reset_time": 1700000000}},
        }.get(tok, {})

        # Test single fetch
        name, q = fetch_account_quota(mock_engine, {"name": "acc1"}, client=mock_client)
        self.assertEqual(name, "acc1")
        self.assertEqual(q["gemini"]["quota"], 80)

        name_err, q_err = fetch_account_quota(mock_engine, {"name": "no_token"}, client=mock_client)
        self.assertEqual(name_err, "no_token")
        self.assertIn("error", q_err)

        # Test concurrent batch fetch
        accounts = [{"name": "acc1"}, {"name": "acc2"}, {"name": "no_token"}]
        all_quotas = fetch_all_accounts_quota(mock_engine, accounts, client=mock_client)
        self.assertEqual(len(all_quotas), 3)
        self.assertIn("gemini", all_quotas["acc1"])
        self.assertIn("gemini", all_quotas["acc2"])
        self.assertIn("error", all_quotas["no_token"])

    def test_quota_status_integer_percentage_one(self):
        """Integer percentage 1 (1%) is parsed as fraction 0.01, not 1.0 (100%)."""
        engine = RotationEngine(model_name="gemini-1.5-pro", active_account_id="acc1")
        quota_data = {"gemini": {"quota": 1, "reset_time": 1700000000}}
        status = engine.get_quota_status(quota_data, model_key="gemini")
        self.assertAlmostEqual(status.fraction, 0.01)
        self.assertFalse(status.is_exhausted())

        # Test integer 0 (0%)
        quota_data_zero = {"gemini": {"quota": 0, "reset_time": 1700000000}}
        status_zero = engine.get_quota_status(quota_data_zero, model_key="gemini")
        self.assertEqual(status_zero.fraction, 0.0)
        self.assertTrue(status_zero.is_exhausted())

    def test_evaluate_list_of_strings_with_nested_cache(self):
        """RotationEngine.evaluate(['acc1', 'acc2'], active='acc1') resolves nested 'accounts' cache dict."""
        import tempfile
        import json
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_dir = Path(tmpdir)
            cache_dir = vault_dir / "cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_payload = {
                "timestamp": int(self.base_time.timestamp()),
                "accounts": {
                    "acc1": {
                        "gemini": {"quota": 10, "reset_time": int((self.base_time).timestamp()) + 3600}
                    },
                    "acc2": {
                        "gemini": {"quota": 90, "reset_time": int((self.base_time).timestamp()) + 3600}
                    },
                },
            }
            (cache_dir / "last_known_quota.json").write_text(json.dumps(cache_payload), encoding="utf-8")

            engine = RotationEngine(vault_dir=vault_dir, model_name="gemini")
            best = engine.evaluate(["acc1", "acc2"], active="acc1", current_time=self.base_time)
            self.assertEqual(best, "acc2")

    def test_init_model_vs_local_directory(self):
        """RotationEngine('gemini') does not misidentify model name as local directory path even if dir exists."""
        import tempfile
        import os
        with tempfile.TemporaryDirectory() as tmpdir:
            old_cwd = os.getcwd()
            try:
                os.chdir(tmpdir)
                os.mkdir("gemini")
                os.mkdir("gemini-3.8-pro")

                # Must resolve to model 'gemini', not vault_dir
                engine_gemini = RotationEngine("gemini")
                self.assertEqual(engine_gemini.model_name, "gemini")
                self.assertEqual(engine_gemini.target_model_key, "gemini")
                self.assertIsNone(engine_gemini.vault_dir)

                # Specific slug must resolve to model
                engine_slug = RotationEngine("gemini-3.8-pro")
                self.assertEqual(engine_slug.model_name, "gemini-3.8-pro")
                self.assertEqual(engine_slug.target_model_key, "gemini")
                self.assertIsNone(engine_slug.vault_dir)

                # Explicit relative path must still resolve to vault_dir
                engine_path = RotationEngine("./claude")
                self.assertIsNotNone(engine_path.vault_dir)
                self.assertEqual(engine_path.vault_dir, Path("./claude").expanduser())
            finally:
                os.chdir(old_cwd)

    def test_evaluate_dict_with_accounts_list(self):
        """RotationEngine.evaluate with standard dict {'active': ..., 'accounts': [...]} evaluates correctly."""
        engine = RotationEngine(model_name="gemini")
        data = {
            "active": "acc1",
            "accounts": [
                {"name": "acc1", "quota": {"gemini": {"quota": 10, "reset_time": int(self.base_time.timestamp()) + 3600}}},
                {"name": "acc2", "quota": {"gemini": {"quota": 90, "reset_time": int(self.base_time.timestamp()) + 3600}}},
            ],
        }
        best = engine.evaluate(data, current_time=self.base_time)
        self.assertEqual(best, "acc2")

    def test_evaluate_list_of_dicts_without_quota_field_using_cache(self):
        """RotationEngine.evaluate([{'name': 'acc1'}, {'name': 'acc2'}]) falls back to cached quota."""
        import tempfile
        import json
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_dir = Path(tmpdir)
            cache_dir = vault_dir / "cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_payload = {
                "timestamp": int(self.base_time.timestamp()),
                "accounts": {
                    "acc1": {"gemini": {"quota": 10, "reset_time": int(self.base_time.timestamp()) + 3600}},
                    "acc2": {"gemini": {"quota": 90, "reset_time": int(self.base_time.timestamp()) + 3600}},
                },
            }
            (cache_dir / "last_known_quota.json").write_text(json.dumps(cache_payload), encoding="utf-8")

            engine = RotationEngine(vault_dir=vault_dir, model_name="gemini")
            best = engine.evaluate([{"name": "acc1"}, {"name": "acc2"}], active="acc1", current_time=self.base_time)
            self.assertEqual(best, "acc2")

    def test_evaluate_mixed_list_str_and_dict(self):
        """RotationEngine.evaluate(['acc1', {'name': 'acc2', ...}]) handles mixed list seamlessly."""
        import tempfile
        import json
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_dir = Path(tmpdir)
            cache_dir = vault_dir / "cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_payload = {
                "timestamp": int(self.base_time.timestamp()),
                "accounts": {
                    "acc1": {"gemini": {"quota": 10, "reset_time": int(self.base_time.timestamp()) + 3600}},
                },
            }
            (cache_dir / "last_known_quota.json").write_text(json.dumps(cache_payload), encoding="utf-8")

            engine = RotationEngine(vault_dir=vault_dir, model_name="gemini")
            mixed = ["acc1", {"name": "acc2", "quota": {"gemini": {"quota": 95, "reset_time": int(self.base_time.timestamp()) + 3600}}}]
            best = engine.evaluate(mixed, active="acc1", current_time=self.base_time)
            self.assertEqual(best, "acc2")

    def test_evaluate_gemini_exclusive_quota_gemini_003(self):
        """[QUOTA-GEMINI-003] RotationEngine evaluates standby accounts based strictly on Gemini quota.

        GIVEN active account 'acc1' with 0% Gemini quota (and 100% non-Gemini quota),
              standby account 'acc2' with 80% Gemini quota (and 10% non-Gemini quota),
              standby account 'acc3' with 40% Gemini quota (and 95% non-Gemini quota)
        WHEN RotationEngine(model_name="gemini").evaluate() evaluates candidate accounts
        THEN it selects 'acc2' based on highest available Gemini quota,
             and completely ignores non-Gemini model state.
        """
        accounts = [
            {
                "name": "acc1",
                "quota": {
                    "gemini": {"quota": 0, "reset_time": 1700003600},
                    "claude": {"quota": 100, "reset_time": 1700003600},
                },
            },
            {
                "name": "acc2",
                "quota": {
                    "gemini": {"quota": 80, "reset_time": 1700003600},
                    "claude": {"quota": 10, "reset_time": 1700003600},
                },
            },
            {
                "name": "acc3",
                "quota": {
                    "gemini": {"quota": 40, "reset_time": 1700003600},
                    "claude": {"quota": 95, "reset_time": 1700003600},
                },
            },
        ]
        for model_variant in ("gemini", "GEMINI", "  GeMiNi  "):
            engine = RotationEngine(model_name=model_variant, active_account_id="acc1")
            self.assertEqual(engine.target_model_key, "gemini")
            result = engine.evaluate(accounts, current_active="acc1", current_time=self.base_time)
            self.assertEqual(result, "acc2")

        accounts_map = {acc["name"]: acc["quota"] for acc in accounts}
        engine = RotationEngine(model_name="gemini", active_account_id="acc1")
        self.assertEqual(
            engine.evaluate(accounts_map, current_active="acc1", target_model="  GeMiNi  ", current_time=self.base_time),
            "acc2",
        )

    def test_evaluate_gemini_exclusive_failover_edge_005(self):
        """[FAILOVER-EDGE-005] Specific Gemini model slugs normalize cleanly to 'gemini' and evaluate exclusively the Gemini quota bucket.

        GIVEN a turn running with specific Gemini model slugs ("gemini-3.8-flash", "gemini-3.8-pro", "gemini-2.5-flash")
        WHEN RotationEngine processes the model parameter via __init__, evaluate, or get_quota_status
        THEN each model slug is normalized cleanly to "gemini",
             and rotation heuristics evaluate exclusively the Gemini quota bucket.
        """
        from agy_switcher.telemetry import normalize_model_name

        slugs = (
            "gemini-3.8-flash",
            "gemini-3.8-pro",
            "gemini-2.5-flash",
            "  GeMiNi-3.8-PrO  ",
            "GEMINI-2.5-FLASH",
            " \t gemini-3.8-flash \n ",
        )
        accounts = [
            {"name": "acc1", "quota": {"gemini": {"quota": 0, "reset_time": 1700003600}, "claude": {"quota": 100}}},
            {"name": "acc2", "quota": {"gemini": {"quota": 85, "reset_time": 1700003600}, "claude": {"quota": 5}}},
            {"name": "acc3", "quota": {"gemini": {"quota": 30, "reset_time": 1700003600}, "claude": {"quota": 99}}},
        ]

        for slug in slugs:
            # 1. normalize_model_name verification
            self.assertEqual(normalize_model_name(slug), "gemini")

            # 2. RotationEngine initialized with slug (kwarg and positional) normalizes target_model_key to 'gemini'
            engine = RotationEngine(model_name=slug, active_account_id="acc1")
            self.assertEqual(engine.target_model_key, "gemini")
            pos_engine = RotationEngine(slug, "acc1")
            self.assertEqual(pos_engine.target_model_key, "gemini")

            # 3. get_quota_status with slug resolves strictly to the 'gemini' quota bucket
            q_data = {
                "gemini": {"quota": 75, "reset_time": 1700003600},
                "claude": {"quota": 5, "reset_time": 1700003600},
            }
            status = engine.get_quota_status(q_data, model_key=slug)
            self.assertIsNotNone(status)
            assert status is not None
            self.assertEqual(status.remaining_percentage(), 75)

            # 4. evaluate() selects highest Gemini standby account
            res = engine.evaluate(accounts, current_active="acc1", current_time=self.base_time)
            self.assertEqual(res, "acc2")

            # 5. evaluate() with explicit target_model=slug normalizes and evaluates Gemini bucket
            default_engine = RotationEngine(model_name="gemini", active_account_id="acc1")
            res_explicit = default_engine.evaluate(
                accounts, current_active="acc1", target_model=slug, current_time=self.base_time
            )
            self.assertEqual(res_explicit, "acc2")

    def test_evaluate_gemini_exclusive_no_cross_model_leakage_or_claude_only(self):
        """Generic cooldowns never inspect non-Gemini quota state, and Claude-only payloads are ignored for Gemini."""
        engine = RotationEngine(model_name="gemini", active_account_id="acc1")
        future_reset = int(self.base_time.timestamp()) + 3600

        # 1. Generic cooldown (no model/reason naming claude) must NOT inspect q_data['claude'] for exemption
        acc2_quota = {
            "gemini": {"quota": 80, "reset_time": future_reset},
            "claude": {"quota": 0, "reset_time": future_reset},
        }
        generic_cooldown = {"reason": "RESOURCE_EXHAUSTED"}
        self.assertTrue(
            engine.is_cooldown_penalized_for_model("gemini", 0.80, generic_cooldown, acc2_quota),
            "Generic cooldown must remain penalized without leaking claude quota state from q_data",
        )
        accounts_with_cooldown = [
            {"name": "acc1", "quota": {"gemini": {"quota": 0, "reset_time": future_reset}}},
            {"name": "acc2", "quota": acc2_quota},
        ]
        self.assertIsNone(
            engine.evaluate(
                accounts_with_cooldown,
                current_active="acc1",
                cooldowns={"acc2": generic_cooldown},
                current_time=self.base_time,
            )
        )

        # 2. Accounts with ONLY 'claude' quota are ignored during Gemini evaluation (list, dict, and groups forms)
        claude_only_list = [
            {"name": "acc1", "quota": {"gemini": {"quota": 0, "reset_time": future_reset}}},
            {"name": "acc2", "quota": {"claude": {"quota": 100, "reset_time": future_reset}}},
        ]
        self.assertIsNone(engine.evaluate(claude_only_list, current_active="acc1", current_time=self.base_time))

        claude_only_with_gemini_standby = [
            {"name": "acc1", "quota": {"gemini": {"quota": 0, "reset_time": future_reset}}},
            {"name": "acc2", "quota": {"claude": {"quota": 100, "reset_time": future_reset}}},
            {"name": "acc3", "quota": {"gemini": {"quota": 35, "reset_time": future_reset}}},
        ]
        self.assertEqual(
            engine.evaluate(claude_only_with_gemini_standby, current_active="acc1", current_time=self.base_time),
            "acc3",
        )

        claude_only_dict = {
            "acc1": {"gemini": {"quota": 0, "reset_time": future_reset}},
            "acc2": {"claude": {"quota": 100, "reset_time": future_reset}},
        }
        self.assertIsNone(engine.evaluate(claude_only_dict, current_active="acc1", current_time=self.base_time))

        claude_only_groups = {
            "acc1": {"gemini": {"quota": 0, "reset_time": future_reset}},
            "acc2": {
                "groups": [
                    {
                        "group": "Claude and GPT models",
                        "buckets": [{"remainingFraction": 1.0, "resetTime": future_reset}],
                    }
                ]
            },
        }
        self.assertIsNone(engine.evaluate(claude_only_groups, current_active="acc1", current_time=self.base_time))

        # 3. Vault cache with Claude-only entry for standby account is ignored during Gemini evaluation
        import tempfile
        import json
        with tempfile.TemporaryDirectory() as tmpdir:
            v_dir = Path(tmpdir)
            cache_dir = v_dir / "cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
            (cache_dir / "last_known_quota.json").write_text(
                json.dumps({
                    "timestamp": int(self.base_time.timestamp()),
                    "accounts": {
                        "acc1": {"gemini": {"quota": 0, "reset_time": future_reset}},
                        "acc2": {"claude": {"quota": 100, "reset_time": future_reset}},
                    },
                }),
                encoding="utf-8",
            )
            cached_engine = RotationEngine(vault_dir=v_dir, model_name="gemini", active_account_id="acc1")
            self.assertIsNone(cached_engine.evaluate(["acc1", "acc2"], current_active="acc1", current_time=self.base_time))
            self.assertIsNone(
                cached_engine.evaluate(
                    [{"name": "acc1"}, {"name": "acc2"}],
                    current_active="acc1",
                    current_time=self.base_time,
                )
            )

    def test_evaluate_gemini_exclusive_auto_rotate_granular_slugs(self):
        """[FAILOVER-EDGE-005] auto_rotate_on_exhaustion normalizes granular slugs cleanly to 'gemini'."""
        import tempfile
        import json
        from agy_switcher.api import auto_rotate_on_exhaustion
        from agy_switcher.vault import AgySwitchEngine

        slugs = ("gemini-3.8-flash", "gemini-3.8-pro", "gemini-2.5-flash", "  GeMiNi-3.8-PrO  ")
        with tempfile.TemporaryDirectory() as tmpdir:
            base_path = Path(tmpdir)
            v_dir = base_path / ".antigravity-vault"
            proc_dir = base_path / "proc"
            proc_dir.mkdir(parents=True, exist_ok=True)
            engine = AgySwitchEngine(
                vault_dir=v_dir,
                gemini_dir=base_path / ".gemini",
                legacy_root=base_path / ".agy-cli-manager",
                proc_dir=proc_dir,
            )

            for acc in ("primary-acc", "standby-acc"):
                acc_dir = v_dir / "accounts" / acc
                acc_dir.mkdir(parents=True, exist_ok=True)
                (acc_dir / "token.json").write_text(
                    json.dumps({
                        "token": {
                            "access_token": f"ya29.sample_{acc}",
                            "refresh_token": f"1//sample_refresh_{acc}",
                            "token_type": "Bearer",
                            "expiry": "2030-01-01T00:00:00Z",
                        },
                        "auth_method": "oauth",
                        "id_token": "header.eyJlbWFpbCI6ICJ0ZXN0QGV4YW1wbGUuY29tIn0.sig",
                    }),
                    encoding="utf-8",
                )
            engine.set_active_name("primary-acc")

            cache_dir = v_dir / "cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_payload = {
                "timestamp": int(self.base_time.timestamp()),
                "accounts": {
                    "primary-acc": {"gemini": {"quota": 0, "reset_time": int(self.base_time.timestamp()) + 3600}},
                    "standby-acc": {"gemini": {"quota": 88, "reset_time": int(self.base_time.timestamp()) + 3600}},
                },
            }
            (cache_dir / "last_known_quota.json").write_text(json.dumps(cache_payload), encoding="utf-8")

            for slug in slugs:
                engine.set_active_name("primary-acc")
                res = auto_rotate_on_exhaustion(model=slug, reload_server=False, engine=engine)
                self.assertTrue(res.success, f"auto_rotate_on_exhaustion failed: {res.message}")
                self.assertEqual(res.new_account, "standby-acc")
                self.assertEqual(res.quota_remaining, 0.88)
                self.assertIn("ya29.sample_standby-acc", engine.cli_token_file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()


