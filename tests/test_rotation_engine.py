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
        # > 15% should retain active account even if another account is higher -> returns None
        engine = RotationEngine(model_name="gemini-1.5-pro", active_account_id="acc1")
        accounts = [
            {"name": "acc1", "quota": self._make_quota(gemini_frac=0.16)},
            {"name": "acc2", "quota": self._make_quota(gemini_frac=1.0)},
        ]
        result = engine.evaluate(accounts, current_active="acc1", target_model="gemini", current_time=self.base_time)
        self.assertIsNone(result)

    def test_drain_window_retain(self):
        # < 15% but > 5% and reset in <= 30m -> retain active account -> returns None
        engine = RotationEngine(model_name="gemini-1.5-pro", active_account_id="acc1")
        accounts = [
            {"name": "acc1", "quota": self._make_quota(gemini_frac=0.06, gemini_reset=29)},
            {"name": "acc2", "quota": self._make_quota(gemini_frac=1.0, gemini_reset=120)},
        ]
        result = engine.evaluate(accounts, current_active="acc1", target_model="gemini", current_time=self.base_time)
        self.assertIsNone(result)

    def test_imminent_reset_priority(self):
        engine = RotationEngine(model_name="gemini-1.5-pro", active_account_id="acc1")
        accounts = [
            {"name": "acc1", "quota": self._make_quota(gemini_frac=0.05, gemini_reset=60)},
            {"name": "acc2", "quota": self._make_quota(gemini_frac=0.50, gemini_reset=120)},
            {"name": "acc3", "quota": self._make_quota(gemini_frac=0.11, gemini_reset=40)},
        ]
        result = engine.evaluate(accounts, current_active="acc1", target_model="gemini", current_time=self.base_time)
        self.assertEqual(result, "acc3")

    def test_imminent_reset_too_low(self):
        engine = RotationEngine(model_name="gemini-1.5-pro", active_account_id="acc1")
        accounts = [
            {"name": "acc1", "quota": self._make_quota(gemini_frac=0.05, gemini_reset=60)},
            {"name": "acc2", "quota": self._make_quota(gemini_frac=0.50, gemini_reset=120)},
            {"name": "acc3", "quota": self._make_quota(gemini_frac=0.09, gemini_reset=40)},
        ]
        result = engine.evaluate(accounts, current_active="acc1", target_model="gemini", current_time=self.base_time)
        self.assertEqual(result, "acc2")

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


if __name__ == "__main__":
    unittest.main()

