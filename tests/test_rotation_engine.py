import unittest
from unittest.mock import patch
from datetime import datetime, timezone
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from rotation_engine import RotationEngine, QuotaStatus

class TestRotationEngine(unittest.TestCase):
    def setUp(self):
        # Base time for testing
        self.base_time = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)
        
    def _make_quota(self, gemini_frac=1.0, gemini_reset=None, claude_frac=1.0, claude_reset=None, gemini_weekly=None, claude_weekly=None):
        def format_time(minutes_offset):
            if minutes_offset is None:
                # Default 2 hours ahead
                minutes_offset = 120
            from datetime import timedelta
            t = self.base_time + timedelta(minutes=minutes_offset)
            return t.strftime("%Y-%m-%dT%H:%M:%SZ")

        groups = []
        if gemini_frac is not None:
            gem_reset_val = gemini_reset if gemini_reset is not None else 120
            buckets = [{"bucketId": "five_hour", "remainingFraction": gemini_frac, "resetTime": format_time(gem_reset_val)}]
            if gemini_weekly is not None:
                buckets.append({"bucketId": "weekly", "remainingFraction": gemini_weekly, "resetTime": format_time(gem_reset_val + 10000)})
            groups.append({"group": "Gemini Models", "buckets": buckets})
            
        if claude_frac is not None:
            claude_reset_val = claude_reset if claude_reset is not None else 120
            buckets = [{"bucketId": "five_hour", "remainingFraction": claude_frac, "resetTime": format_time(claude_reset_val)}]
            if claude_weekly is not None:
                buckets.append({"bucketId": "weekly", "remainingFraction": claude_weekly, "resetTime": format_time(claude_reset_val + 10000)})
            groups.append({"group": "Claude Models", "buckets": buckets})
            
        return {"groups": groups}

    def test_worst_case_evaluation(self):
        engine = RotationEngine(model_name="gemini-1.5-pro", active_account_id="acc1")
        accounts = {
            "acc1": self._make_quota(gemini_frac=0.8, gemini_weekly=0.1) # worst case is 0.1
        }
        accounts["acc2"] = self._make_quota(gemini_frac=0.9, gemini_weekly=0.9)
        
        result = engine.evaluate(accounts, current_time=self.base_time)
        self.assertEqual(result, "acc2")

    def test_cache_optimized_retain(self):
        # > 15% should retain active account even if another account is higher
        engine = RotationEngine(model_name="gemini-1.5-pro", active_account_id="acc1")
        accounts = {
            "acc1": self._make_quota(gemini_frac=0.16),
            "acc2": self._make_quota(gemini_frac=1.0)
        }
        result = engine.evaluate(accounts, current_time=self.base_time)
        self.assertEqual(result, "acc1")

    def test_drain_window_retain(self):
        # < 15% but > 5% and reset in <= 30m
        engine = RotationEngine(model_name="gemini-1.5-pro", active_account_id="acc1")
        accounts = {
            "acc1": self._make_quota(gemini_frac=0.06, gemini_reset=29),
            "acc2": self._make_quota(gemini_frac=1.0, gemini_reset=120)
        }
        result = engine.evaluate(accounts, current_time=self.base_time)
        self.assertEqual(result, "acc1")

    def test_imminent_reset_priority(self):
        engine = RotationEngine(model_name="gemini-1.5-pro", active_account_id="acc1")
        accounts = {
            "acc1": self._make_quota(gemini_frac=0.05, gemini_reset=60),
            "acc2": self._make_quota(gemini_frac=0.50, gemini_reset=120),
            "acc3": self._make_quota(gemini_frac=0.11, gemini_reset=40)
        }
        result = engine.evaluate(accounts, current_time=self.base_time)
        self.assertEqual(result, "acc3")

    def test_imminent_reset_too_low(self):
        engine = RotationEngine(model_name="gemini-1.5-pro", active_account_id="acc1")
        accounts = {
            "acc1": self._make_quota(gemini_frac=0.05, gemini_reset=60),
            "acc2": self._make_quota(gemini_frac=0.50, gemini_reset=120),
            "acc3": self._make_quota(gemini_frac=0.09, gemini_reset=40)
        }
        result = engine.evaluate(accounts, current_time=self.base_time)
        self.assertEqual(result, "acc2")

    def test_target_claude_model(self):
        engine = RotationEngine(model_name="claude-3-5-sonnet", active_account_id="acc1")
        accounts = {
            "acc1": self._make_quota(gemini_frac=1.0, claude_frac=0.10),
            "acc2": self._make_quota(gemini_frac=0.10, claude_frac=1.0)
        }
        result = engine.evaluate(accounts, current_time=self.base_time)
        self.assertEqual(result, "acc2")

    def test_no_valid_accounts(self):
        engine = RotationEngine(model_name="gemini-1.5-pro", active_account_id="acc1")
        accounts = {
            "acc1": {},
            "acc2": {}
        }
        result = engine.evaluate(accounts, current_time=self.base_time)
        self.assertEqual(result, "acc1")

    def test_reset_delta_calculated_once_per_account(self):
        """reset_delta must only be calculated once per account status during evaluate."""
        engine = RotationEngine(model_name="gemini-1.5-pro", active_account_id="acc1")
        accounts = {
            # fraction 0.06 (>0.05) checks drain window, but reset=35 (>30) so continues to step 3 loop
            "acc1": self._make_quota(gemini_frac=0.06, gemini_reset=35),
            "acc2": self._make_quota(gemini_frac=0.50, gemini_reset=120),
        }

        with patch.object(engine, "calculate_reset_delta", wraps=engine.calculate_reset_delta) as spy:
            result = engine.evaluate(accounts, current_time=self.base_time)

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


if __name__ == "__main__":
    unittest.main()

