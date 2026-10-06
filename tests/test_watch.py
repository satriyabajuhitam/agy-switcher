import unittest
from unittest.mock import MagicMock, patch, mock_open
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.watch import LogWatcher

class TestLogWatcher(unittest.TestCase):
    def setUp(self):
        self.engine_mock = MagicMock()
        self.watcher = LogWatcher(engine=self.engine_mock, log_dirs=["/fake/log/dir"], cooldown_minutes=60)

    def test_regex_matching_resource_exhausted(self):
        line = "Error: RESOURCE_EXHAUSTED (code 429)"
        self.assertTrue(self.watcher.is_exhausted_message(line))

    def test_regex_matching_individual_quota(self):
        line = "Individual quota reached for this account"
        self.assertTrue(self.watcher.is_exhausted_message(line))

    def test_regex_matching_weekly_quota(self):
        line = "weekly quota reached, try again later"
        self.assertTrue(self.watcher.is_exhausted_message(line))

    def test_regex_not_matching_normal_log(self):
        line = "INFO: User logged in successfully"
        self.assertFalse(self.watcher.is_exhausted_message(line))

    @patch("scripts.watch.time.sleep", return_value=None)
    def test_trigger_switch_on_exhaustion(self, sleep_mock):
        line = "RESOURCE_EXHAUSTED (code 429)"
        
        self.engine_mock.get_active_name.return_value = 'acc1'
        self.engine_mock.list_accounts.return_value = {
            "active": "acc1",
            "accounts": [
                {"name": "acc1", "valid": True, "is_active": True},
                {"name": "acc2", "valid": True, "is_active": False},
            ]
        }
        
        with patch("scripts.watch.RotationEngine") as rot_engine_class_mock:
            rot_engine_mock = MagicMock()
            rot_engine_class_mock.return_value = rot_engine_mock
            rot_engine_mock.evaluate.return_value = 'acc2'
            
            self.watcher.process_line(line)
            
            self.engine_mock.switch_tokens.assert_called_once_with('acc2', restart_server=False, force=True)

    def test_infer_target_model_claude_keywords(self):
        self.assertEqual(
            self.watcher.infer_target_model("RESOURCE_EXHAUSTED (code 429) for claude-3-5-sonnet"),
            "claude"
        )
        self.assertEqual(
            self.watcher.infer_target_model("Anthropic rate limit: Individual quota reached"),
            "claude"
        )
        self.assertEqual(
            self.watcher.infer_target_model("Error: claude limit reached"),
            "claude"
        )

    def test_infer_target_model_gemini_keywords(self):
        self.assertEqual(
            self.watcher.infer_target_model("RESOURCE_EXHAUSTED (code 429) for gemini-1.5-pro"),
            "gemini"
        )
        self.assertEqual(
            self.watcher.infer_target_model("Gemini quota exhausted"),
            "gemini"
        )

    def test_infer_target_model_from_active_account_cache(self):
        cache_claude_depleted = {
            "acc1": {
                "claude": {"quota": 0, "reset_time": 0},
                "gemini": {"quota": 90, "reset_time": 0},
            }
        }
        self.assertEqual(
            self.watcher.infer_target_model(
                "RESOURCE_EXHAUSTED (code 429)",
                active_account="acc1",
                cache=cache_claude_depleted
            ),
            "claude"
        )

        cache_gemini_depleted = {
            "acc1": {
                "claude": {"quota": 80, "reset_time": 0},
                "gemini": {"quota": 0, "reset_time": 0},
            }
        }
        self.assertEqual(
            self.watcher.infer_target_model(
                "RESOURCE_EXHAUSTED (code 429)",
                active_account="acc1",
                cache=cache_gemini_depleted
            ),
            "gemini"
        )

        # Default fallback when no specific depletion or equal
        self.assertEqual(
            self.watcher.infer_target_model("RESOURCE_EXHAUSTED (code 429)"),
            "gemini"
        )

    @patch("scripts.watch.time.sleep", return_value=None)
    def test_trigger_switch_passes_claude_to_rotation_engine(self, sleep_mock):
        line = "RESOURCE_EXHAUSTED (code 429) for claude-3-5-sonnet"
        self.engine_mock.get_active_name.return_value = 'acc1'
        self.engine_mock.list_accounts.return_value = {
            "active": "acc1",
            "accounts": [
                {"name": "acc1", "valid": True, "is_active": True},
                {"name": "acc2", "valid": True, "is_active": False},
            ]
        }

        with patch("scripts.watch.RotationEngine") as rot_engine_class_mock:
            rot_engine_mock = MagicMock()
            rot_engine_class_mock.return_value = rot_engine_mock
            rot_engine_mock.evaluate.return_value = 'acc2'

            self.watcher.process_line(line)

            rot_engine_class_mock.assert_called_once_with(model_name="claude", active_account_id="acc1")
            rot_engine_mock.evaluate.assert_called_once()
            _, kwargs = rot_engine_mock.evaluate.call_args
            self.assertEqual(kwargs.get("target_model"), "claude")

    @patch("scripts.watch.time.sleep", return_value=None)
    @patch("scripts.watch.QuotaClient")
    def test_trigger_switch_uses_quota_client_load_cache(self, mock_quota_client_cls, sleep_mock):
        line = "RESOURCE_EXHAUSTED (code 429)"
        self.engine_mock.get_active_name.return_value = 'acc1'
        self.engine_mock.list_accounts.return_value = {
            "active": "acc1",
            "accounts": [
                {"name": "acc1", "valid": True, "is_active": True},
                {"name": "acc2", "valid": True, "is_active": False},
            ]
        }
        mock_qc_instance = MagicMock()
        mock_qc_instance.load_cache.return_value = {}
        mock_quota_client_cls.return_value = mock_qc_instance

        self.watcher.process_line(line)

        mock_quota_client_cls.assert_called_once_with(self.engine_mock)
        mock_qc_instance.load_cache.assert_called_once()

    def test_scan_new_files_only_scans_log(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            file_log1 = os.path.join(tmpdir, "a.log")
            file_txt = os.path.join(tmpdir, "b.txt")
            file_log2 = os.path.join(tmpdir, "c.log")
            file_json = os.path.join(tmpdir, "d.json")

            for f in (file_log1, file_txt, file_log2, file_json):
                with open(f, "w") as fp:
                    fp.write("hello")

            watcher = LogWatcher(engine=self.engine_mock, log_dirs=[tmpdir])
            scanned = watcher.scan_new_files()

            self.assertIn(file_log1, scanned)
            self.assertIn(file_log2, scanned)
            self.assertNotIn(file_txt, scanned)
            self.assertNotIn(file_json, scanned)

    @patch("scripts.watch.time.sleep", return_value=None)
    def test_failover_selects_claude_quota_when_claude_exhausted(self, sleep_mock):
        line = "RESOURCE_EXHAUSTED (code 429) for claude-3-5-sonnet"
        self.engine_mock.get_active_name.return_value = 'acc1'
        self.engine_mock.list_accounts.return_value = {
            "active": "acc1",
            "accounts": [
                {"name": "acc1", "valid": True, "is_active": True},
                {"name": "acc2", "valid": True, "is_active": False},
                {"name": "acc3", "valid": True, "is_active": False},
            ]
        }
        mock_cache = {
            "acc1": {
                "claude": {"quota": 0, "reset_time": 0},
                "gemini": {"quota": 100, "reset_time": 0},
            },
            # acc2 has 0% claude, 100% gemini
            "acc2": {
                "claude": {"quota": 0, "reset_time": 0},
                "gemini": {"quota": 100, "reset_time": 0},
            },
            # acc3 has 80% claude, 20% gemini
            "acc3": {
                "claude": {"quota": 80, "reset_time": 0},
                "gemini": {"quota": 20, "reset_time": 0},
            },
        }
        with patch("scripts.watch.QuotaClient") as mock_qc_cls:
            mock_qc = MagicMock()
            mock_qc.load_cache.return_value = mock_cache
            mock_qc_cls.return_value = mock_qc

            self.watcher.process_line(line)

            # Because claude was exhausted, acc3 (80% claude) should be selected, not acc2 (0% claude)
            self.engine_mock.switch_tokens.assert_called_once_with('acc3', restart_server=False, force=True)


if __name__ == '__main__':
    unittest.main()

