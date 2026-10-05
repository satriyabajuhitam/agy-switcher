import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure the scripts directory is in the path
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'scripts')))

from agy_switch import main, AgySwitchEngine, QuotaClient, RotationEngine

class TestCLI(unittest.TestCase):

    def setUp(self):
        self.engine = MagicMock(spec=AgySwitchEngine)
        self.engine.list_accounts.return_value = {
            "active": "acc1",
            "accounts": [
                {"name": "acc1", "is_active": True, "valid": True, "email": "a@b.com"},
                {"name": "acc2", "is_active": False, "valid": True, "email": "c@d.com"}
            ]
        }
        self.engine.get_active_name.return_value = "acc1"
        self.engine.get_account_tokens.return_value = {"access_token": "token1", "refresh_token": "refresh1"}
        self.engine.get_status.return_value = {
            "active": "acc1",
            "email": "a@b.com",
            "server_running": False,
            "server_pid": None,
            "server_busy": False
        }

    @patch('agy_switch.QuotaClient')
    def test_list_quota(self, mock_qc_class):
        mock_qc = mock_qc_class.return_value
        mock_qc.fetch_quota.return_value = {"gemini": {"quota": 85, "reset_time": 1700000000}}

        with patch('sys.stdout') as mock_stdout:
            exit_code = main(["list", "--quota"], engine=self.engine)
            self.assertEqual(exit_code, 0)
            
        # Verify it uses threaded execution by checking fetch_quota was called multiple times
        self.assertEqual(mock_qc.fetch_quota.call_count, 2)

    @patch('agy_switch.datetime')
    @patch('agy_switch.QuotaClient')
    @patch('subprocess.Popen')
    def test_status_cache_expired(self, mock_popen, mock_qc_class, mock_datetime):
        mock_qc = mock_qc_class.return_value
        # Cache is 100 seconds old
        mock_qc.load_cache.return_value = {"timestamp": 1000, "accounts": {"acc1": {"gemini": {"quota": 85}}}}
        from datetime import timezone
        mock_datetime.now.return_value.timestamp.return_value = 1101
        mock_datetime.timezone = timezone
        
        with patch('sys.stdout'):
            exit_code = main(["status"], engine=self.engine)
            self.assertEqual(exit_code, 0)
        
        # Verify background probe was launched
        mock_popen.assert_called_once()
        args = mock_popen.call_args[0][0]
        self.assertIn("internal-probe", args)

    @patch('agy_switch.datetime')
    @patch('agy_switch.QuotaClient')
    @patch('subprocess.Popen')
    def test_status_cache_valid(self, mock_popen, mock_qc_class, mock_datetime):
        mock_qc = mock_qc_class.return_value
        # Cache is 30 seconds old
        mock_qc.load_cache.return_value = {"timestamp": 1000, "accounts": {"acc1": {"gemini": {"quota": 85}}}}
        from datetime import timezone
        mock_datetime.now.return_value.timestamp.return_value = 1030
        mock_datetime.timezone = timezone
        
        with patch('sys.stdout'):
            exit_code = main(["status"], engine=self.engine)
            self.assertEqual(exit_code, 0)
        
        # Verify no probe
        mock_popen.assert_not_called()

    @patch('agy_switch.RotationEngine')
    @patch('agy_switch.QuotaClient')
    def test_switch_auto_optimal(self, mock_qc_class, mock_re_class):
        mock_qc = mock_qc_class.return_value
        mock_qc.fetch_quota.return_value = {"gemini": {"quota": 85}}
        
        mock_re = mock_re_class.return_value
        mock_re.evaluate.return_value = "acc1" # Current active is optimal
        
        with patch('sys.stdout') as mock_stdout:
            exit_code = main(["switch", "--auto"], engine=self.engine)
            self.assertEqual(exit_code, 0)
        
        # Verify os.replace/switch_tokens wasn't called because it's optimal
        self.engine.switch_tokens.assert_not_called()

    @patch('agy_switch.RotationEngine')
    @patch('agy_switch.QuotaClient')
    def test_switch_auto_rotate(self, mock_qc_class, mock_re_class):
        mock_qc = mock_qc_class.return_value
        mock_qc.fetch_quota.return_value = {"gemini": {"quota": 10}}
        
        mock_re = mock_re_class.return_value
        mock_re.evaluate.return_value = "acc2" # Needs rotation
        
        with patch('sys.stdout'):
            exit_code = main(["switch", "--auto"], engine=self.engine)
            self.assertEqual(exit_code, 0)
        
        # Verify rotation
        self.engine.switch_tokens.assert_called_once_with("acc2", restart_server=True, force=False)

if __name__ == '__main__':
    unittest.main()
