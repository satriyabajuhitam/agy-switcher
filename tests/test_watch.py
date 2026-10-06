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

if __name__ == '__main__':
    unittest.main()
