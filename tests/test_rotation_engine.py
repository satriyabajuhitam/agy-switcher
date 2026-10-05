import unittest
from datetime import datetime, timezone
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from rotation_engine import RotationEngine

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

if __name__ == "__main__":
    unittest.main()
