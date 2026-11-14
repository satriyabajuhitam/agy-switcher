import errno
import fcntl
import io
import json
import socket
import threading
import unittest
from unittest.mock import MagicMock, patch, mock_open
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from scripts.watch import (
    LogWatcher,
    PresenceDetector,
    DaemonManager,
    DEFAULT_VAULT_DIR,
    main as watch_main,
    _open_secure_log_file,
    _resolve_vault_log_path,
    append_secure_log_entry,
)
from agy_switcher.supervisor import ProcessSupervisor
from agy_switcher.models import AccountInfo

class TestLogWatcher(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.engine_mock = MagicMock()
        self.engine_mock.vault_dir = Path(self.tmpdir)
        self.watcher = LogWatcher(engine=self.engine_mock, log_dirs=["/fake/log/dir"], cooldown_minutes=60)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

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
        line = json.dumps({"status": "ERROR", "content": "RESOURCE_EXHAUSTED (code 429)"})
        
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
            
            self.engine_mock.switch_tokens.assert_called_once_with('acc2', restart_server=True, force=True)

    @patch("scripts.watch.time.sleep", return_value=None)
    def test_trigger_switch_aborts_when_all_exhausted(self, sleep_mock):
        line = json.dumps({"status": "ERROR", "content": "RESOURCE_EXHAUSTED (code 429)"})
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
            rot_engine_mock.evaluate.return_value = None

            switched = self.watcher.process_line(line)
            self.assertFalse(switched)
            self.engine_mock.switch_tokens.assert_not_called()

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
        line = json.dumps({"status": "ERROR", "content": "RESOURCE_EXHAUSTED (code 429) for claude-3-5-sonnet"})
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
        line = json.dumps({"status": "ERROR", "content": "RESOURCE_EXHAUSTED (code 429)"})
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
        line = json.dumps({"status": "ERROR", "content": "RESOURCE_EXHAUSTED (code 429) for claude-3-5-sonnet"})
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
            self.engine_mock.switch_tokens.assert_called_once_with('acc3', restart_server=True, force=True)


    def test_process_line_returns_boolean(self):
        # Non-exhausted returns False
        self.assertFalse(self.watcher.process_line("Normal log line"))

        # Exhausted with switch returns True
        exhausted_line = json.dumps({"status": "ERROR", "content": "RESOURCE_EXHAUSTED (code 429)"})
        with patch.object(self.watcher, "trigger_switch", return_value=True):
            self.assertTrue(self.watcher.process_line(exhausted_line))

        # Exhausted with failed switch returns False
        with patch.object(self.watcher, "trigger_switch", return_value=False):
            self.assertFalse(self.watcher.process_line(exhausted_line))

    def test_watch_batch_burst_lines_breaks_on_switch(self):
        """Verify that when a batch of lines contains multiple 429 errors, only the first triggers a switch."""
        switch_count = 0
        def fake_trigger_switch(line="", **kwargs):
            nonlocal switch_count
            switch_count += 1
            return True

        self.watcher.trigger_switch = fake_trigger_switch

        batch_lines = [
            json.dumps({"status": "ERROR", "content": "RESOURCE_EXHAUSTED (code 429) line 1"}) + "\n",
            json.dumps({"status": "ERROR", "content": "RESOURCE_EXHAUSTED (code 429) line 2"}) + "\n",
            json.dumps({"status": "ERROR", "content": "RESOURCE_EXHAUSTED (code 429) line 3"}) + "\n",
        ]

        # Simulate batch processing in watch_loop
        for line in batch_lines:
            if self.watcher.process_line(line):
                break

        # Only one switch should have occurred
        self.assertEqual(switch_count, 1)

    @patch("scripts.watch.time.sleep", return_value=None)
    def test_handle_exhaustion_advances_all_tracked_cursors_to_eof_and_resets_stagnant(self, mock_sleep):
        """Post-switch cursor advance seeks ALL tracked files in file_cursors to EOF and pops stagnant sizes."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            f1 = os.path.join(tmpdir, "desktop_transcript.jsonl")
            f2 = os.path.join(tmpdir, "cli_output.log")
            with open(f1, "w", encoding="utf-8") as fp1:
                fp1.write('{"status": "ERROR", "content": "RESOURCE_EXHAUSTED (code 429)"}\n')
            with open(f2, "w", encoding="utf-8") as fp2:
                fp2.write("normal log entry before exhaustion\n")

            self.watcher.file_cursors = {f1: 0, f2: 10}
            self.watcher._stagnant_sizes = {f1: 10, f2: 15}

            self.engine_mock.get_active_name.return_value = 'acc1'
            self.engine_mock.list_accounts.return_value = {
                "active": "acc1",
                "accounts": [
                    {"name": "acc1", "valid": True, "is_active": True},
                    {"name": "acc2", "valid": True, "is_active": False},
                ]
            }

            with patch("scripts.watch.RotationEngine") as rot_cls_mock:
                rot_mock = MagicMock()
                rot_mock.evaluate.return_value = 'acc2'
                rot_cls_mock.return_value = rot_mock

                success = self.watcher.handle_exhaustion(line="RESOURCE_EXHAUSTED (code 429)", file_path=f1)
                self.assertTrue(success)
                self.engine_mock.switch_tokens.assert_called_once_with('acc2', restart_server=True, force=True)

                # Both cursors must be seeked to their file sizes (EOF)
                self.assertEqual(self.watcher.file_cursors[f1], os.path.getsize(f1))
                self.assertEqual(self.watcher.file_cursors[f2], os.path.getsize(f2))
                # Stagnant sizes must be popped/reset
                self.assertEqual(len(self.watcher._stagnant_sizes), 0)

    @patch("scripts.watch.time.sleep", return_value=None)
    def test_handle_exhaustion_defensively_handles_unlinked_file(self, mock_sleep):
        """If a tracked file is unlinked during cursor advance, handle_exhaustion pops it without raising OSError."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            f_alive = os.path.join(tmpdir, "alive.log")
            f_dead = os.path.join(tmpdir, "deleted.log")
            with open(f_alive, "w", encoding="utf-8") as fp:
                fp.write("alive file content\n")

            self.watcher.file_cursors = {f_alive: 0, f_dead: 10}
            self.watcher._stagnant_sizes = {f_alive: 5, f_dead: 10}

            self.engine_mock.get_active_name.return_value = 'acc1'
            self.engine_mock.list_accounts.return_value = {
                "active": "acc1",
                "accounts": [
                    {"name": "acc1", "valid": True, "is_active": True},
                    {"name": "acc2", "valid": True, "is_active": False},
                ]
            }

            with patch("scripts.watch.RotationEngine") as rot_cls_mock:
                rot_mock = MagicMock()
                rot_mock.evaluate.return_value = 'acc2'
                rot_cls_mock.return_value = rot_mock

                success = self.watcher.handle_exhaustion(line="RESOURCE_EXHAUSTED (code 429)")
                self.assertTrue(success)

                # f_alive seeked to EOF, f_dead popped safely
                self.assertEqual(self.watcher.file_cursors[f_alive], os.path.getsize(f_alive))
                self.assertNotIn(f_dead, self.watcher.file_cursors)
                self.assertNotIn(f_dead, self.watcher._stagnant_sizes)

    @patch("scripts.watch.time.sleep", return_value=None)
    def test_poll_once_bypasses_cursor_overwrite_on_failover(self, mock_sleep):
        """poll_once breaks without overwriting file_cursors[target] when failover occurs."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = os.path.join(tmpdir, "test.log")
            line1 = json.dumps({"type": "info", "content": "normal startup"}) + "\n"
            line2 = json.dumps({"status": "ERROR", "content": "RESOURCE_EXHAUSTED (code 429)"}) + "\n"
            line3 = json.dumps({"type": "info", "content": "trailing content"}) + "\n"
            with open(test_file, "w", encoding="utf-8") as fp:
                fp.write(line1 + line2 + line3)

            watcher = LogWatcher(engine=self.engine_mock, log_dirs=[tmpdir])
            # Set cursor at beginning
            watcher.file_cursors[test_file] = 0

            # Mock scan_active_targets to return our test file
            watcher.scan_active_targets = lambda: [test_file]

            self.engine_mock.get_active_name.return_value = 'acc1'
            self.engine_mock.list_accounts.return_value = {
                "active": "acc1",
                "accounts": [
                    {"name": "acc1", "valid": True, "is_active": True},
                    {"name": "acc2", "valid": True, "is_active": False},
                ]
            }

            with patch("scripts.watch.RotationEngine") as rot_cls_mock:
                rot_mock = MagicMock()
                rot_mock.evaluate.return_value = 'acc2'
                rot_cls_mock.return_value = rot_mock

                switches = watcher.poll_once()
                self.assertEqual(switches, 1)

                # The cursor should NOT be at line 2 boundary (len(line1) + len(line2));
                # it MUST remain at EOF (os.path.getsize(test_file)) as set by handle_exhaustion!
                full_size = os.path.getsize(test_file)
                self.assertEqual(watcher.file_cursors[test_file], full_size)

                # Subsequent poll_once without writes should find nothing to process
                switches_after = watcher.poll_once()
                self.assertEqual(switches_after, 0)

    @patch("scripts.watch.time.sleep", return_value=None)
    def test_damping_interval_suppresses_subsequent_exhaustion(self, mock_sleep):
        """Damping window suppresses subsequent rotation within damping_seconds without mutating attribute."""
        self.watcher.damping_seconds = 15.0
        self.engine_mock.get_active_name.return_value = 'acc1'
        self.engine_mock.list_accounts.return_value = {
            "active": "acc1",
            "accounts": [
                {"name": "acc1", "valid": True, "is_active": True},
                {"name": "acc2", "valid": True, "is_active": False},
            ]
        }

        with patch("scripts.watch.RotationEngine") as rot_cls_mock:
            rot_mock = MagicMock()
            rot_mock.evaluate.return_value = 'acc2'
            rot_cls_mock.return_value = rot_mock

            exhausted_line = json.dumps({"status": "ERROR", "content": "RESOURCE_EXHAUSTED (code 429)"})
            with patch("time.time", return_value=100.0):
                switched = self.watcher.process_line(exhausted_line)
                self.assertTrue(switched)
                self.assertEqual(self.watcher.last_switch_time, 100.0)
                self.assertEqual(self.watcher.damping_seconds, 15.0)

            # Inside damping window (100.0 + 5.0 < 100.0 + 15.0) -> suppressed
            with patch("time.time", return_value=105.0):
                switched_suppressed = self.watcher.process_line(exhausted_line)
                self.assertFalse(switched_suppressed)
                # Switch count should still be 1
                self.assertEqual(self.engine_mock.switch_tokens.call_count, 1)
                self.assertEqual(self.watcher.damping_seconds, 15.0)

            # Outside damping window (100.0 + 16.0 > 100.0 + 15.0) -> allowed
            with patch("time.time", return_value=116.0):
                switched_allowed = self.watcher.process_line(exhausted_line)
                self.assertTrue(switched_allowed)
                self.assertEqual(self.engine_mock.switch_tokens.call_count, 2)
                self.assertEqual(self.watcher.last_switch_time, 116.0)
                self.assertEqual(self.watcher.damping_seconds, 15.0)

    def test_trigger_switch_alias_forwards_to_handle_exhaustion(self):
        """trigger_switch remains a public method alias forwarding directly to handle_exhaustion."""
        with patch.object(self.watcher, "handle_exhaustion", return_value=True) as mock_handle:
            res = self.watcher.trigger_switch(
                line="test 429",
                model_hint="claude",
                file_path="/tmp/test.log",
            )
            self.assertTrue(res)
            mock_handle.assert_called_once_with(
                line="test 429",
                model_hint="claude",
                file_path="/tmp/test.log",
            )

    @patch("scripts.watch.time.sleep", return_value=None)
    def test_handle_exhaustion_switch_failure_does_not_advance_cursors_or_damp(self, mock_sleep):
        """When switch_tokens fails or returns failure, handle_exhaustion returns False without cursor advance or damping."""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmpdir:
            f1 = os.path.join(tmpdir, "test.log")
            with open(f1, "w", encoding="utf-8") as fp:
                fp.write("some initial error lines\n")

            self.watcher.file_cursors = {f1: 0}
            self.watcher._stagnant_sizes = {f1: 5}
            self.watcher.last_switch_time = 0.0

            self.engine_mock.get_active_name.return_value = 'acc1'
            self.engine_mock.list_accounts.return_value = {
                "active": "acc1",
                "accounts": [
                    {"name": "acc1", "valid": True, "is_active": True},
                    {"name": "acc2", "valid": True, "is_active": False},
                ]
            }

            with patch("scripts.watch.RotationEngine") as rot_cls_mock, \
                 patch.object(self.watcher, "_notify_desktop") as mock_notify:
                rot_mock = MagicMock()
                rot_mock.evaluate.return_value = 'acc2'
                rot_cls_mock.return_value = rot_mock

                # Case A: switch_tokens returns failure dict
                self.engine_mock.switch_tokens.return_value = {"success": False, "error": "Presence lock active"}
                res = self.watcher.handle_exhaustion(line="RESOURCE_EXHAUSTED (code 429)", file_path=f1)

                self.assertFalse(res)
                # Cursor must NOT be advanced to EOF (remains 0)
                self.assertEqual(self.watcher.file_cursors[f1], 0)
                # Stagnant sizes must NOT be cleared
                self.assertIn(f1, self.watcher._stagnant_sizes)
                # Damping time must NOT be updated
                self.assertEqual(self.watcher.last_switch_time, 0.0)
                # Notification must NOT be sent
                mock_notify.assert_not_called()

                # Case B: switch_tokens returns False
                self.engine_mock.switch_tokens.return_value = False
                res_false = self.watcher.handle_exhaustion(line="RESOURCE_EXHAUSTED (code 429)", file_path=f1)
                self.assertFalse(res_false)
                self.assertEqual(self.watcher.file_cursors[f1], 0)
                self.assertEqual(self.watcher.last_switch_time, 0.0)
                mock_notify.assert_not_called()

    @patch("scripts.watch.time.sleep", return_value=None)
    def test_handle_exhaustion_switch_exception_does_not_advance_cursors_or_damp(self, mock_sleep):
        """When switch_tokens raises an exception, handle_exhaustion catches it and returns False."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            f1 = os.path.join(tmpdir, "test.log")
            with open(f1, "w", encoding="utf-8") as fp:
                fp.write("log data\n")

            self.watcher.file_cursors = {f1: 0}
            self.watcher._stagnant_sizes = {f1: 3}
            self.watcher.last_switch_time = 0.0

            self.engine_mock.get_active_name.return_value = 'acc1'
            self.engine_mock.list_accounts.return_value = {
                "active": "acc1",
                "accounts": [
                    {"name": "acc1", "valid": True, "is_active": True},
                    {"name": "acc2", "valid": True, "is_active": False},
                ]
            }

            with patch("scripts.watch.RotationEngine") as rot_cls_mock, \
                 patch.object(self.watcher, "_notify_desktop") as mock_notify:
                rot_mock = MagicMock()
                rot_mock.evaluate.return_value = 'acc2'
                rot_cls_mock.return_value = rot_mock

                self.engine_mock.switch_tokens.side_effect = OSError("Disk write error")
                res = self.watcher.handle_exhaustion(line="RESOURCE_EXHAUSTED (code 429)", file_path=f1)

                self.assertFalse(res)
                self.assertEqual(self.watcher.file_cursors[f1], 0)
                self.assertIn(f1, self.watcher._stagnant_sizes)
                self.assertEqual(self.watcher.last_switch_time, 0.0)
                mock_notify.assert_not_called()

    def test_handle_exhaustion_observes_settling_delay_on_server_restart(self):
        """Settling delay of 1.0s is executed exactly once during handle_exhaustion failover with server reload."""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmpdir:
            f1 = os.path.join(tmpdir, "test.log")
            with open(f1, "w", encoding="utf-8") as fp:
                fp.write("log data\n")

            self.watcher.file_cursors = {f1: 0}
            self.engine_mock.get_active_name.return_value = 'acc1'
            self.engine_mock.list_accounts.return_value = {
                "active": "acc1",
                "accounts": [
                    {"name": "acc1", "valid": True, "is_active": True},
                    {"name": "acc2", "valid": True, "is_active": False},
                ]
            }

            with patch("scripts.watch.RotationEngine") as rot_cls_mock, \
                 patch("scripts.watch.time.sleep") as mock_sleep, \
                 patch("scripts.watch.shutil.which", return_value="/usr/bin/notify-send"), \
                 patch("scripts.watch.subprocess.Popen") as mock_popen, \
                 patch("agy_switcher.watcher.append_secure_log_entry"):
                rot_mock = MagicMock()
                rot_mock.evaluate.return_value = 'acc2'
                rot_cls_mock.return_value = rot_mock

                # Subtest A: server_restarted = True -> sleep(1.0) called exactly once
                self.engine_mock.switch_tokens.return_value = {
                    "active": "acc2",
                    "server_restarted": True,
                }
                res = self.watcher.handle_exhaustion(line="RESOURCE_EXHAUSTED (code 429)", file_path=f1)
                self.assertTrue(res)
                mock_sleep.assert_called_once_with(1.0)
                self.assertEqual(mock_sleep.call_count, 1)

                # Verify clean notification format dispatched via notify-send
                mock_popen.assert_called_once()
                call_args = mock_popen.call_args[0][0]
                self.assertEqual(call_args[0], "/usr/bin/notify-send")
                self.assertEqual(call_args[1], "agy-switch")
                self.assertEqual(call_args[2], "🔄 Antigravity switched to acc2 & reloaded language_server (quota 429)")
                self.assertNotIn("(reloading language_server...)", call_args[2])
                self.assertNotIn(") (", call_args[2])

                # Subtest B: server_restarted = False -> no settling delay
                mock_sleep.reset_mock()
                mock_popen.reset_mock()
                self.watcher.last_switch_time = 0.0
                self.engine_mock.switch_tokens.return_value = {
                    "active": "acc2",
                    "server_restarted": False,
                }
                res_b = self.watcher.handle_exhaustion(line="RESOURCE_EXHAUSTED (code 429)", file_path=f1)
                self.assertTrue(res_b)
                mock_sleep.assert_not_called()

                mock_popen.assert_called_once()
                call_args_b = mock_popen.call_args[0][0]
                self.assertEqual(call_args_b[2], "🔄 Antigravity switched to acc2 (quota 429)")
                self.assertNotIn("& reloaded", call_args_b[2])
                self.assertNotIn(") (", call_args_b[2])

    @patch("scripts.watch.time.sleep", return_value=None)
    def test_handle_exhaustion_accepts_path_object_file_path(self, mock_sleep):
        """handle_exhaustion normalizes Path objects passed as file_path and advances cursor to EOF."""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir) / "path_test.log"
            p.write_text("sample content\n", encoding="utf-8")

            self.watcher.file_cursors = {}
            self.watcher._stagnant_sizes = {"unrelated.log": 99}

            self.engine_mock.get_active_name.return_value = 'acc1'
            self.engine_mock.list_accounts.return_value = {
                "active": "acc1",
                "accounts": [
                    {"name": "acc1", "valid": True, "is_active": True},
                    {"name": "acc2", "valid": True, "is_active": False},
                ]
            }

            with patch("scripts.watch.RotationEngine") as rot_cls_mock:
                rot_mock = MagicMock()
                rot_mock.evaluate.return_value = 'acc2'
                rot_cls_mock.return_value = rot_mock

                res = self.watcher.handle_exhaustion(line="RESOURCE_EXHAUSTED (code 429)", file_path=p)
                self.assertTrue(res)

                # Normalized string key in file_cursors at EOF
                self.assertIn(str(p), self.watcher.file_cursors)
                self.assertEqual(self.watcher.file_cursors[str(p)], p.stat().st_size)
                # Entire stagnant_sizes dictionary cleared
                self.assertEqual(len(self.watcher._stagnant_sizes), 0)

    def test_notify_desktop_reloaded_true_invokes_settling_delay_and_fires_reload_notification(self):
        """When reloaded=True, _notify_desktop invokes 1.0s settling delay and sends explicit reload message without double parentheses."""
        with patch("scripts.watch.shutil.which", return_value="/usr/bin/notify-send"), \
             patch("scripts.watch.time.sleep") as mock_sleep, \
             patch("scripts.watch.subprocess.Popen") as mock_popen, \
             patch("agy_switcher.watcher.append_secure_log_entry"):

            self.watcher._notify_desktop(
                target_account="acc2",
                old_account="acc1",
                reason="quota 429",
                reloaded=True,
            )

            # Settling delay of 1.0s MUST be invoked exactly once
            mock_sleep.assert_called_once_with(1.0)

            # notify-send must be dispatched
            mock_popen.assert_called_once()
            call_args = mock_popen.call_args[0][0]
            self.assertEqual(call_args[0], "/usr/bin/notify-send")
            # Summary is agy-switch
            self.assertEqual(call_args[1], "agy-switch")
            # Message receives clean reload notification and reason following ADR 0010
            self.assertEqual(call_args[2], "🔄 Antigravity switched to acc2 & reloaded language_server (quota 429)")
            self.assertNotIn("(reloading language_server...)", call_args[2])
            self.assertNotIn(") (", call_args[2])

    def test_notify_desktop_reloaded_false_does_not_invoke_settling_delay(self):
        """When reloaded=False, _notify_desktop does not invoke 1.0s settling delay and sends standard message."""
        with patch("scripts.watch.shutil.which", return_value="/usr/bin/notify-send"), \
             patch("scripts.watch.time.sleep") as mock_sleep, \
             patch("scripts.watch.subprocess.Popen") as mock_popen, \
             patch("agy_switcher.watcher.append_secure_log_entry"):

            self.watcher._notify_desktop(
                target_account="acc2",
                old_account="acc1",
                reason="routine rotation",
                reloaded=False,
            )

            # No 1.0s settling delay when not reloaded
            mock_sleep.assert_not_called()

            # notify-send must be dispatched without reload notification
            mock_popen.assert_called_once()
            call_args = mock_popen.call_args[0][0]
            self.assertEqual(call_args[0], "/usr/bin/notify-send")
            self.assertEqual(call_args[2], "🔄 Antigravity switched to acc2 (routine rotation)")
            self.assertNotIn("reloaded language_server", call_args[2])
            self.assertNotIn(") (", call_args[2])

    def test_notify_desktop_formatting_edge_cases(self):
        """Verify clean message formatting without redundant parentheses or empty suffixes across edge cases."""
        with patch("scripts.watch.shutil.which", return_value="/usr/bin/notify-send"), \
             patch("scripts.watch.time.sleep", return_value=None), \
             patch("scripts.watch.subprocess.Popen") as mock_popen, \
             patch("agy_switcher.watcher.append_secure_log_entry"):

            # Case A: reloaded=True without reason
            self.watcher._notify_desktop(target_account="acc2", reloaded=True)
            self.assertEqual(mock_popen.call_args[0][0][2], "🔄 Antigravity switched to acc2 & reloaded language_server")

            # Case B: reloaded=False without reason
            mock_popen.reset_mock()
            self.watcher._notify_desktop(target_account="acc2", reloaded=False)
            self.assertEqual(mock_popen.call_args[0][0][2], "🔄 Antigravity switched to acc2")

            # Case C: reason already wrapped in parentheses
            mock_popen.reset_mock()
            self.watcher._notify_desktop(target_account="acc2", reason="(quota 429)", reloaded=True)
            self.assertEqual(mock_popen.call_args[0][0][2], "🔄 Antigravity switched to acc2 & reloaded language_server (quota 429)")
            self.assertNotIn(") (", mock_popen.call_args[0][0][2])
            self.assertNotIn("((", mock_popen.call_args[0][0][2])

    def test_notify_desktop_structured_audit_log_reloaded_format_and_mode_0600(self):
        """Structured audit log line is written to watch.log with mode 0600 upon reloaded switch."""
        import tempfile
        from pathlib import Path
        import re

        with tempfile.TemporaryDirectory() as tmpdir:
            vault_path = Path(tmpdir)
            self.watcher.engine.vault_dir = vault_path

            with patch("scripts.watch.shutil.which", return_value=None), \
                 patch("scripts.watch.time.sleep", return_value=None):

                self.watcher._notify_desktop(
                    target_account="acc2",
                    old_account="acc1",
                    reason="quota 429",
                    reloaded=True,
                )

            watch_log = vault_path / "watch.log"
            self.assertTrue(watch_log.is_file())
            # Enforce 0600 mode
            self.assertEqual(watch_log.stat().st_mode & 0o777, 0o600)

            content = watch_log.read_text(encoding="utf-8")
            pattern = (
                r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} "
                r"\[watch\] Switched from 'acc1' to 'acc2' with force-restart language_server \(reason: quota 429\)\n$"
            )
            self.assertRegex(content, pattern)

    def test_notify_desktop_structured_audit_log_not_reloaded_format(self):
        """Structured audit log line for non-reloaded rotation is appended correctly."""
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            vault_path = Path(tmpdir)
            self.watcher.engine.vault_dir = vault_path

            with patch("scripts.watch.shutil.which", return_value=None), \
                 patch("scripts.watch.time.sleep", return_value=None):

                self.watcher._notify_desktop(
                    target_account="acc2",
                    old_account="acc1",
                    reason="cache rotation",
                    reloaded=False,
                )

            watch_log = vault_path / "watch.log"
            self.assertTrue(watch_log.is_file())
            content = watch_log.read_text(encoding="utf-8")
            pattern = (
                r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} "
                r"\[watch\] Switched from 'acc1' to 'acc2' \(reason: cache rotation\)\n$"
            )
            self.assertRegex(content, pattern)

    def test_notify_desktop_structured_audit_log_uniform_watch_prefix_all_branches(self):
        """Structured audit log line uniformly includes [watch] across all branches including fallback."""
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            vault_path = Path(tmpdir)
            self.watcher.engine.vault_dir = vault_path

            with patch("scripts.watch.shutil.which", return_value=None), \
                 patch("scripts.watch.time.sleep", return_value=None):

                # Branch 1: old_account is None, reloaded is False (fallback branch)
                self.watcher._notify_desktop(
                    target_account="acc2",
                    old_account=None,
                    reason="manual switch",
                    reloaded=False,
                )

                # Branch 2: old_account is None, reloaded is True
                self.watcher._notify_desktop(
                    target_account="acc3",
                    old_account=None,
                    reason="manual reload",
                    reloaded=True,
                )

            watch_log = vault_path / "watch.log"
            self.assertTrue(watch_log.is_file())
            lines = watch_log.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 2)

            for line in lines:
                self.assertRegex(line, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \[watch\] ")

            self.assertIn("[watch] Switched to 'acc2' (manual switch)", lines[0])
            self.assertIn("[watch] Switched to 'acc3' with force-restart language_server (reason: manual reload)", lines[1])

    def test_notify_desktop_handles_missing_notify_send_gracefully(self):
        """When notify-send binary is missing, _notify_desktop still writes the audit log cleanly."""
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            vault_path = Path(tmpdir)
            self.watcher.engine.vault_dir = vault_path

            with patch("scripts.watch.shutil.which", return_value=None), \
                 patch("scripts.watch.time.sleep", return_value=None), \
                 patch("scripts.watch.subprocess.Popen") as mock_popen:

                self.watcher._notify_desktop(
                    target_account="acc2",
                    old_account="acc1",
                    reason="quota 429",
                    reloaded=True,
                )

                mock_popen.assert_not_called()

            watch_log = vault_path / "watch.log"
            self.assertTrue(watch_log.is_file())
            self.assertIn("Switched from 'acc1' to 'acc2' with force-restart language_server", watch_log.read_text(encoding="utf-8"))

    @patch("scripts.watch.time.sleep", return_value=None)
    def test_pre_switch_quota_probe_healthy_and_depleted(self, mock_sleep):
        """[PRE-PROBE-001] & [PRE-PROBE-002]: LogWatcher.handle_exhaustion executes Pre-Switch Quota Probe.
        - Healthy Gemini quota (>25%) suppresses rotation and cooldown application (transient RPC anomaly).
        - Depleted Gemini quota (<=25%) authorizes cooldown and rotation to standby account with available quota.
        """
        cache_dir = Path(self.tmpdir) / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / "last_known_quota.json"
        self.engine_mock.cache_dir = cache_dir
        self.engine_mock.cache_file = cache_file

        self.engine_mock.get_active_name.return_value = "primary-acc"
        self.engine_mock.list_accounts.return_value = {
            "active": "primary-acc",
            "accounts": [
                {"name": "primary-acc", "valid": True, "is_active": True},
                {"name": "standby-acc", "valid": True, "is_active": False},
            ],
        }

        exhaustion_line = json.dumps({"status": "ERROR", "content": "RESOURCE_EXHAUSTED (code 429)"})

        # --- [PRE-PROBE-001]: Healthy quota (> 25%) suppresses rotation ---
        cache_file.write_text(
            json.dumps({
                "accounts": {
                    "primary-acc": {"gemini": {"quota": 85}},
                    "standby-acc": {"gemini": {"quota": 90}},
                }
            }),
            encoding="utf-8",
        )

        res_healthy = self.watcher.handle_exhaustion(line=exhaustion_line)
        self.assertFalse(res_healthy)
        self.engine_mock.switch_tokens.assert_not_called()
        self.assertNotIn("primary-acc", self.watcher.get_cooldowns())
        self.assertFalse(self.watcher.is_in_cooldown("primary-acc"))

        # Also verify float fraction representation (0.85 > 0.25)
        cache_file.write_text(
            json.dumps({
                "accounts": {
                    "primary-acc": {"gemini": {"fraction": 0.85}},
                    "standby-acc": {"gemini": {"fraction": 0.90}},
                }
            }),
            encoding="utf-8",
        )
        res_healthy_fraction = self.watcher.handle_exhaustion(line=exhaustion_line)
        self.assertFalse(res_healthy_fraction)
        self.engine_mock.switch_tokens.assert_not_called()
        self.assertNotIn("primary-acc", self.watcher.get_cooldowns())

        # --- [PRE-PROBE-002]: Depleted quota (<= 25%) authorizes rotation ---
        cache_file.write_text(
            json.dumps({
                "accounts": {
                    "primary-acc": {"gemini": {"fraction": 0.10}},
                    "standby-acc": {"gemini": {"fraction": 0.90}},
                }
            }),
            encoding="utf-8",
        )
        self.engine_mock.switch_tokens.return_value = {"success": True, "active": "standby-acc"}

        res_depleted = self.watcher.handle_exhaustion(line=exhaustion_line)
        self.assertTrue(res_depleted)
        self.assertIn("primary-acc", self.watcher.get_cooldowns())
        self.engine_mock.switch_tokens.assert_called_once_with("standby-acc", restart_server=True, force=True)

        # --- Adversarial Edge Cases ---
        # 1. Exact boundary at 0.25 (<= 0.25 is not healthy, authorizes rotation)
        self.watcher.cooldown_manager.clear_cooldown()
        self.watcher.last_switch_time = 0.0
        self.engine_mock.switch_tokens.reset_mock()
        cache_file.write_text(
            json.dumps({
                "accounts": {
                    "primary-acc": {"gemini": {"fraction": 0.25}},
                    "standby-acc": {"gemini": {"fraction": 0.90}},
                }
            }),
            encoding="utf-8",
        )
        res_boundary = self.watcher.handle_exhaustion(line=exhaustion_line)
        self.assertTrue(res_boundary)
        self.assertIn("primary-acc", self.watcher.get_cooldowns())
        self.engine_mock.switch_tokens.assert_called_once_with("standby-acc", restart_server=True, force=True)

        # 2. Healthy boundary just above threshold (0.26 > 0.25 suppresses rotation)
        self.watcher.cooldown_manager.clear_cooldown()
        self.watcher.last_switch_time = 0.0
        self.engine_mock.switch_tokens.reset_mock()
        cache_file.write_text(
            json.dumps({
                "accounts": {
                    "primary-acc": {"gemini": {"fraction": 0.26}},
                    "standby-acc": {"gemini": {"fraction": 0.90}},
                }
            }),
            encoding="utf-8",
        )
        res_above = self.watcher.handle_exhaustion(line=exhaustion_line)
        self.assertFalse(res_above)
        self.engine_mock.switch_tokens.assert_not_called()
        self.assertNotIn("primary-acc", self.watcher.get_cooldowns())

        # 3. Missing active quota in cache falls back safely to rotation when standby available
        self.watcher.cooldown_manager.clear_cooldown()
        self.watcher.last_switch_time = 0.0
        self.engine_mock.switch_tokens.reset_mock()
        cache_file.write_text(
            json.dumps({
                "accounts": {
                    "standby-acc": {"gemini": {"quota": 90}},
                }
            }),
            encoding="utf-8",
        )
        res_missing = self.watcher.handle_exhaustion(line=exhaustion_line)
        self.assertTrue(res_missing)
        self.assertIn("primary-acc", self.watcher.get_cooldowns())
        self.engine_mock.switch_tokens.assert_called_once_with("standby-acc", restart_server=True, force=True)

        # 4. Active damping window suppresses rotation prior to quota probe
        self.watcher.cooldown_manager.clear_cooldown()
        self.watcher.last_switch_time = time.time() - 5.0
        self.watcher.damping_seconds = 15.0
        self.engine_mock.switch_tokens.reset_mock()
        res_damped = self.watcher.handle_exhaustion(line=exhaustion_line)
        self.assertFalse(res_damped)
        self.engine_mock.switch_tokens.assert_not_called()
        self.assertNotIn("primary-acc", self.watcher.get_cooldowns())

    @patch("scripts.watch.time.sleep", return_value=None)
    def test_pre_switch_probe_boundary_and_cache_missing(self, mock_sleep):
        """[PRE-PROBE-EDGE-001] & [PRE-PROBE-ERR-001]: Pre-Switch Quota Probe boundaries and IO fallback.
        - [PRE-PROBE-EDGE-001]: Exactly 25.0% Gemini quota (fraction = 0.25) evaluates as depleted (<= 25%),
          authorizing account rotation to preserve agent operation before zero-token hard cutoff.
        - [PRE-PROBE-ERR-001]: Missing or unparseable last_known_quota.json treats active account quota as
          unverified rather than assuming healthy, and falls back to standard exhaustion rotation so the
          agent turn is not permanently blocked.
        """
        cache_dir = Path(self.tmpdir) / "cache_boundary"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / "last_known_quota.json"
        self.engine_mock.cache_dir = cache_dir
        self.engine_mock.cache_file = cache_file

        self.engine_mock.get_active_name.return_value = "primary-acc"
        self.engine_mock.list_accounts.return_value = {
            "active": "primary-acc",
            "accounts": [
                {"name": "primary-acc", "valid": True, "is_active": True},
                {"name": "standby-acc", "valid": True, "is_active": False},
            ],
        }
        self.engine_mock.switch_tokens.return_value = {"success": True, "active": "standby-acc"}
        exhaustion_line = json.dumps({"status": "ERROR", "content": "RESOURCE_EXHAUSTED (code 429)"})

        # --- [PRE-PROBE-EDGE-001]: Exact boundary at fraction = 0.25 authorizes rotation ---
        self.watcher.cooldown_manager.clear_cooldown()
        self.watcher.last_switch_time = 0.0
        self.engine_mock.switch_tokens.reset_mock()
        cache_file.write_text(
            json.dumps({
                "accounts": {
                    "primary-acc": {"gemini": {"fraction": 0.25}},
                    "standby-acc": {"gemini": {"fraction": 0.90}},
                }
            }),
            encoding="utf-8",
        )
        res_boundary = self.watcher.handle_exhaustion(line=exhaustion_line)
        self.assertTrue(res_boundary)
        self.assertIn("primary-acc", self.watcher.get_cooldowns())
        self.engine_mock.switch_tokens.assert_called_once_with("standby-acc", restart_server=True, force=True)

        # Also verify integer percentage quota = 25 (25% <= 25% boundary)
        self.watcher.cooldown_manager.clear_cooldown()
        self.watcher.last_switch_time = 0.0
        self.engine_mock.switch_tokens.reset_mock()
        cache_file.write_text(
            json.dumps({
                "accounts": {
                    "primary-acc": {"gemini": {"quota": 25}},
                    "standby-acc": {"gemini": {"quota": 90}},
                }
            }),
            encoding="utf-8",
        )
        res_boundary_int = self.watcher.handle_exhaustion(line=exhaustion_line)
        self.assertTrue(res_boundary_int)
        self.assertIn("primary-acc", self.watcher.get_cooldowns())
        self.engine_mock.switch_tokens.assert_called_once_with("standby-acc", restart_server=True, force=True)

        # --- [PRE-PROBE-ERR-001]: Missing last_known_quota.json falls back safely to rotation ---
        self.watcher.cooldown_manager.clear_cooldown()
        self.watcher.last_switch_time = 0.0
        self.engine_mock.switch_tokens.reset_mock()
        cache_file.unlink(missing_ok=True)

        res_missing = self.watcher.handle_exhaustion(line=exhaustion_line)
        self.assertTrue(res_missing)
        self.assertIn("primary-acc", self.watcher.get_cooldowns())
        self.engine_mock.switch_tokens.assert_called_once_with("standby-acc", restart_server=True, force=True)

        # --- [PRE-PROBE-ERR-001]: Unparseable/corrupted last_known_quota.json falls back safely to rotation ---
        self.watcher.cooldown_manager.clear_cooldown()
        self.watcher.last_switch_time = 0.0
        self.engine_mock.switch_tokens.reset_mock()
        cache_file.write_text("{ unparseable corrupted json", encoding="utf-8")

        res_corrupt = self.watcher.handle_exhaustion(line=exhaustion_line)
        self.assertTrue(res_corrupt)
        self.assertIn("primary-acc", self.watcher.get_cooldowns())
        self.engine_mock.switch_tokens.assert_called_once_with("standby-acc", restart_server=True, force=True)

    @patch("scripts.watch.time.sleep", return_value=None)
    def test_truncation_resets_cursor_and_clock_skew(self, mock_sleep):
        """[WATCH-BOUND-EDGE-003] and [PRE-PROBE-EDGE-002]:
        1. [WATCH-BOUND-EDGE-003]: File truncation during transcript tailing resets cursor to 0 and resumes tailing.
        2. [PRE-PROBE-EDGE-002]: Negative elapsed time from backwards clock adjustment resets last_switch_time safely.
        """
        # [WATCH-BOUND-EDGE-003]: Verify file truncation (current_size < cursor) resets cursor to 0
        with self.subTest("WATCH-BOUND-EDGE-003: file truncation resets cursor and resumes tailing"):
            with tempfile.TemporaryDirectory() as watch_tmpdir:
                transcript_path = os.path.join(watch_tmpdir, "transcript.jsonl")
                initial_line = json.dumps({"type": "turn_start", "content": "padding" * 20}) + "\n"
                initial_content = initial_line * (4096 // len(initial_line) + 2)
                with open(transcript_path, "w", encoding="utf-8") as fp:
                    fp.write(initial_content)
                initial_size = os.path.getsize(transcript_path)
                self.assertGreaterEqual(initial_size, 4096)

                watcher = LogWatcher(engine=self.engine_mock, log_dirs=[watch_tmpdir])
                watcher.active_transcript_path = Path(transcript_path)
                watcher.presence_detector.get_watch_targets = lambda **kw: [transcript_path]
                watcher.file_cursors[transcript_path] = 4096

                # Antigravity truncates the file to a smaller size with new records
                new_record = json.dumps({"type": "user", "content": "hello after rotation"}) + "\n"
                with open(transcript_path, "w", encoding="utf-8") as fp:
                    fp.write(new_record)
                new_size = os.path.getsize(transcript_path)
                self.assertLess(new_size, 4096)

                processed_lines: list[str] = []
                orig_process_line = watcher.process_line
                def capture_line(line: str, is_jsonl: bool = False, file_path: str | None = None) -> bool:
                    processed_lines.append(line)
                    return orig_process_line(line, is_jsonl=is_jsonl, file_path=file_path)
                watcher.process_line = capture_line

                switches = watcher.poll_once()
                self.assertEqual(switches, 0)
                self.assertEqual(watcher.file_cursors[transcript_path], new_size)
                self.assertEqual(len(processed_lines), 1)
                self.assertIn("hello after rotation", processed_lines[0])

                # Truncation to 0 bytes resets cursor and does not impede subsequent tailing
                watcher.file_cursors[transcript_path] = 4096
                with open(transcript_path, "w", encoding="utf-8") as fp:
                    fp.truncate(0)
                self.assertEqual(os.path.getsize(transcript_path), 0)

                switches_zero = watcher.poll_once()
                self.assertEqual(switches_zero, 0)
                self.assertEqual(watcher.file_cursors[transcript_path], 0)

                subsequent_record = json.dumps({"type": "user", "content": "record after zero truncation"}) + "\n"
                with open(transcript_path, "a", encoding="utf-8") as fp:
                    fp.write(subsequent_record)
                subsequent_size = os.path.getsize(transcript_path)

                switches_subsequent = watcher.poll_once()
                self.assertEqual(switches_subsequent, 0)
                self.assertEqual(watcher.file_cursors[transcript_path], subsequent_size)
                self.assertTrue(any("record after zero truncation" in l for l in processed_lines))

                # Truncation followed by quota error triggers failover from beginning without SeekError
                watcher.file_cursors[transcript_path] = 4096
                exhaustion_record = json.dumps({"status": "ERROR", "content": "RESOURCE_EXHAUSTED (code 429)"}) + "\n"
                with open(transcript_path, "w", encoding="utf-8") as fp:
                    fp.write(exhaustion_record)

                with patch.object(watcher, "handle_exhaustion", return_value=True) as mock_handle:
                    switches_exhaust = watcher.poll_once()
                    self.assertEqual(switches_exhaust, 1)
                    mock_handle.assert_called_once()

        # [PRE-PROBE-EDGE-002]: Clock skew or negative elapsed time resets last_switch_time safely
        with self.subTest("PRE-PROBE-EDGE-002: negative elapsed time resets last_switch_time and allows rotation"):
            cache_dir = Path(self.tmpdir) / "cache_skew"
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file = cache_dir / "last_known_quota.json"
            self.engine_mock.cache_dir = cache_dir
            self.engine_mock.cache_file = cache_file

            self.engine_mock.get_active_name.return_value = "primary-acc"
            self.engine_mock.list_accounts.return_value = {
                "active": "primary-acc",
                "accounts": [
                    {"name": "primary-acc", "valid": True, "is_active": True},
                    {"name": "standby-acc", "valid": True, "is_active": False},
                ],
            }
            cache_file.write_text(
                json.dumps({
                    "accounts": {
                        "primary-acc": {"gemini": {"fraction": 0.10}},
                        "standby-acc": {"gemini": {"fraction": 0.90}},
                    }
                }),
                encoding="utf-8",
            )
            self.engine_mock.switch_tokens.reset_mock()
            self.engine_mock.switch_tokens.return_value = {"success": True, "active": "standby-acc"}
            self.watcher.cooldown_manager.clear_cooldown()
            self.watcher.damping_seconds = 60.0

            # Clock skew: last_switch_time is in the future relative to current time
            self.watcher.last_switch_time = 5000.0
            exhaustion_line = json.dumps({"status": "ERROR", "content": "RESOURCE_EXHAUSTED (code 429)"})

            with patch("time.time", return_value=1000.0):
                res = self.watcher.handle_exhaustion(line=exhaustion_line)
                self.assertTrue(res)
                self.engine_mock.switch_tokens.assert_called_once_with("standby-acc", restart_server=True, force=True)
                self.assertIn("primary-acc", self.watcher.get_cooldowns())
                self.assertEqual(self.watcher.last_switch_time, 1000.0)

            # Healthy quota probe with clock skew safely resets last_switch_time to 0.0
            self.watcher.cooldown_manager.clear_cooldown()
            self.watcher.last_switch_time = 5000.0
            self.engine_mock.switch_tokens.reset_mock()
            cache_file.write_text(
                json.dumps({
                    "accounts": {
                        "primary-acc": {"gemini": {"fraction": 0.85}},
                        "standby-acc": {"gemini": {"fraction": 0.90}},
                    }
                }),
                encoding="utf-8",
            )
            with patch("time.time", return_value=1000.0):
                res_healthy = self.watcher.handle_exhaustion(line=exhaustion_line)
                self.assertFalse(res_healthy)
                self.assertEqual(self.watcher.last_switch_time, 0.0)

            # Verify process_line with clock skew also clears last_switch_time
            self.watcher.last_switch_time = 5000.0
            with patch("time.time", return_value=1000.0):
                with patch.object(self.watcher, "handle_exhaustion", return_value=False):
                    self.watcher.process_line(exhaustion_line)
                    self.assertEqual(self.watcher.last_switch_time, 0.0)

    @patch("scripts.watch.time.sleep", return_value=None)
    def test_damping_60s_suppresses_bursts(self, mock_sleep):
        """[PRE-PROBE-003], [PRE-PROBE-ERR-003], and [PRE-PROBE-004]:
        1. Default damping_seconds is 60.0, custom damping_seconds is preserved and honored.
        2. [PRE-PROBE-003]: Auto-rotation occurred 25s ago (elapsed=25.0s < 60.0s); subsequent
           exhaustion error is suppressed without placing any account into cooldown.
        3. [PRE-PROBE-ERR-003]: Auto-rotation occurred 10s ago (elapsed=10.0s < 60.0s); 10 consecutive
           burst exhaustion lines arriving within 2s are all suppressed with zero token switches or cooldown penalties.
        4. Exact boundary conditions:
           - elapsed = 59.9s (< 60.0s) -> suppressed.
           - elapsed = 60.0s (== 60.0s >= 60.0s) -> allowed, failover authorized when quota depleted.
           - [PRE-PROBE-004]: elapsed = 61.0s (>= 60.0s) -> re-arms failover switching.
        """
        # 1. Verify default damping_seconds and custom damping_seconds preservation
        default_watcher = LogWatcher(engine=self.engine_mock)
        self.assertEqual(default_watcher.damping_seconds, 60.0)

        none_watcher = LogWatcher(engine=self.engine_mock, damping_seconds=None)
        self.assertEqual(none_watcher.damping_seconds, 60.0)

        custom_watcher = LogWatcher(engine=self.engine_mock, damping_seconds=45.0)
        self.assertEqual(custom_watcher.damping_seconds, 45.0)

        legacy_watcher = LogWatcher(engine=self.engine_mock, damping_seconds=["/var/log/custom"])
        self.assertEqual(legacy_watcher.damping_seconds, 60.0)

        # Setup mock cache and engine for exhaustion testing
        cache_dir = Path(self.tmpdir) / "cache_burst"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / "last_known_quota.json"
        self.engine_mock.cache_dir = cache_dir
        self.engine_mock.cache_file = cache_file

        self.engine_mock.get_active_name.return_value = "primary-acc"
        self.engine_mock.list_accounts.return_value = {
            "active": "primary-acc",
            "accounts": [
                {"name": "primary-acc", "valid": True, "is_active": True},
                {"name": "standby-acc", "valid": True, "is_active": False},
            ],
        }
        # Depleted quota on primary-acc (10%), healthy on standby-acc (90%)
        cache_file.write_text(
            json.dumps({
                "accounts": {
                    "primary-acc": {"gemini": {"fraction": 0.10}},
                    "standby-acc": {"gemini": {"fraction": 0.90}},
                }
            }),
            encoding="utf-8",
        )
        self.engine_mock.switch_tokens.reset_mock()
        self.engine_mock.switch_tokens.return_value = {"success": True, "active": "standby-acc"}
        self.watcher.cooldown_manager.clear_cooldown()
        self.assertEqual(self.watcher.damping_seconds, 60.0)

        exhaustion_line = json.dumps({"status": "ERROR", "content": "RESOURCE_EXHAUSTED (code 429)"})

        # 2. [PRE-PROBE-003]: Auto-rotation occurred 25s ago (elapsed = 25.0s < 60.0s)
        self.watcher.last_switch_time = 1000.0
        with patch("time.time", return_value=1025.0):
            res_25s = self.watcher.handle_exhaustion(line=exhaustion_line)
            self.assertFalse(res_25s, "Exhaustion at elapsed=25.0s must be suppressed")
            self.engine_mock.switch_tokens.assert_not_called()
            self.assertNotIn("primary-acc", self.watcher.get_cooldowns())
            self.assertEqual(self.watcher.last_switch_time, 1000.0)

        # 3. [PRE-PROBE-ERR-003]: Auto-rotation occurred 10s ago (elapsed = 10.0s < 60.0s);
        #    10 consecutive exhaustion lines arrive within 2 seconds
        self.watcher.last_switch_time = 1000.0
        self.engine_mock.switch_tokens.reset_mock()
        self.watcher.cooldown_manager.clear_cooldown()

        for i in range(10):
            # Arrival times from 1010.0 to 1011.8 (elapsed 10.0s to 11.8s)
            arrival_time = 1010.0 + (i * 0.2)
            with patch("time.time", return_value=arrival_time):
                suppressed_proc = self.watcher.process_line(exhaustion_line)
                self.assertFalse(suppressed_proc, f"Burst event {i} must be suppressed by process_line")
                suppressed_handle = self.watcher.handle_exhaustion(line=exhaustion_line)
                self.assertFalse(suppressed_handle, f"Burst event {i} must be suppressed by handle_exhaustion")

        # Zero additional token switches or cooldown penalties across entire burst
        self.engine_mock.switch_tokens.assert_not_called()
        self.assertEqual(len(self.watcher.get_cooldowns()), 0)
        self.assertEqual(self.watcher.last_switch_time, 1000.0)

        # 4. Exact boundary conditions:
        # 4a. elapsed = 59.9s (< 60.0s) -> suppressed
        self.watcher.last_switch_time = 1000.0
        with patch("time.time", return_value=1059.9):
            res_59_9 = self.watcher.handle_exhaustion(line=exhaustion_line)
            self.assertFalse(res_59_9, "Exhaustion at elapsed=59.9s must be suppressed")
            self.engine_mock.switch_tokens.assert_not_called()
            self.assertNotIn("primary-acc", self.watcher.get_cooldowns())

        # 4b. elapsed = 60.0s (== 60.0s, boundary elapsed >= damping_seconds) -> allowed
        self.watcher.last_switch_time = 1000.0
        self.engine_mock.switch_tokens.reset_mock()
        with patch("time.time", return_value=1060.0):
            res_60_0 = self.watcher.handle_exhaustion(line=exhaustion_line)
            self.assertTrue(res_60_0, "Exhaustion at elapsed=60.0s must be allowed (elapsed >= damping_seconds)")
            self.engine_mock.switch_tokens.assert_called_once_with("standby-acc", restart_server=True, force=True)
            self.assertIn("primary-acc", self.watcher.get_cooldowns())
            self.assertEqual(self.watcher.last_switch_time, 1060.0)

        # 4c. [PRE-PROBE-004]: Damping window expiration at elapsed = 61.0s (>= 60.0s) re-arms failover
        self.watcher.cooldown_manager.clear_cooldown()
        self.watcher.last_switch_time = 1000.0
        self.engine_mock.switch_tokens.reset_mock()
        with patch("time.time", return_value=1061.0):
            res_61_0 = self.watcher.handle_exhaustion(line=exhaustion_line)
            self.assertTrue(res_61_0, "[PRE-PROBE-004] Exhaustion at elapsed=61.0s re-arms failover")
            self.engine_mock.switch_tokens.assert_called_once_with("standby-acc", restart_server=True, force=True)
            self.assertIn("primary-acc", self.watcher.get_cooldowns())
            self.assertEqual(self.watcher.last_switch_time, 1061.0)

    @patch("scripts.watch.time.sleep", return_value=None)
    def test_damping_expiration_and_boundary(self, mock_sleep):
        """[PRE-PROBE-004] & [PRE-PROBE-EDGE-004]: Damping expiration and temporal boundary re-arming.

        - [PRE-PROBE-004]: GIVEN an auto-rotation occurred 61 seconds ago (elapsed = 61.0s >= 60.0s),
          and the active account is genuinely depleted (quota <= 25%), WHEN a new quota error arrives,
          THEN LogWatcher.handle_exhaustion() verifies the damping window has elapsed, and proceeds
          to evaluate standby accounts and execute failover.
        - [PRE-PROBE-EDGE-004]: GIVEN an auto-rotation occurred exactly 60.0 seconds ago (elapsed = 60.0s),
          and active account has depleted quota (fraction <= 0.25), WHEN a new quota error arrives in
          transcript.jsonl, THEN LogWatcher.handle_exhaustion() evaluates that elapsed >= damping_seconds
          (damping window elapsed), and allows Pre-Switch Quota Probe and failover to proceed.
        """
        cache_dir = Path(self.tmpdir) / "cache_damping_boundary"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / "last_known_quota.json"
        self.engine_mock.cache_dir = cache_dir
        self.engine_mock.cache_file = cache_file

        self.engine_mock.get_active_name.return_value = "primary-acc"
        self.engine_mock.list_accounts.return_value = {
            "active": "primary-acc",
            "accounts": [
                {"name": "primary-acc", "valid": True, "is_active": True},
                {"name": "standby-acc", "valid": True, "is_active": False},
            ],
        }
        self.engine_mock.switch_tokens.return_value = {"success": True, "active": "standby-acc"}
        self.watcher.damping_seconds = 60.0

        exhaustion_line = json.dumps({"status": "ERROR", "content": "RESOURCE_EXHAUSTED (code 429)"})

        # --- Sub-scenario A: [PRE-PROBE-004] Damping window expiration at elapsed = 61.0s ---
        with self.subTest("[PRE-PROBE-004]: elapsed = 61.0s >= 60.0s with depleted quota executes failover"):
            cache_file.write_text(
                json.dumps({
                    "accounts": {
                        "primary-acc": {"gemini": {"fraction": 0.20}},
                        "standby-acc": {"gemini": {"fraction": 0.80}},
                    }
                }),
                encoding="utf-8",
            )
            self.watcher.cooldown_manager.clear_cooldown()
            self.watcher.last_switch_time = 1000.0
            self.engine_mock.switch_tokens.reset_mock()

            with patch("time.time", return_value=1061.0):
                res = self.watcher.handle_exhaustion(line=exhaustion_line)
                self.assertTrue(res, "handle_exhaustion must succeed when damping has expired (61.0s >= 60.0s)")
                self.engine_mock.switch_tokens.assert_called_once_with("standby-acc", restart_server=True, force=True)
                self.assertIn("primary-acc", self.watcher.get_cooldowns())
                self.assertEqual(self.watcher.last_switch_time, 1061.0)

        # --- Sub-scenario B: [PRE-PROBE-EDGE-004] Exact 60.0s temporal boundary in transcript.jsonl ---
        with self.subTest("[PRE-PROBE-EDGE-004]: elapsed = 60.0s exact boundary via transcript.jsonl allows probe and failover"):
            cache_file.write_text(
                json.dumps({
                    "accounts": {
                        "primary-acc": {"gemini": {"fraction": 0.25}},
                        "standby-acc": {"gemini": {"fraction": 0.90}},
                    }
                }),
                encoding="utf-8",
            )
            self.watcher.cooldown_manager.clear_cooldown()
            self.watcher.last_switch_time = 1000.0
            self.engine_mock.switch_tokens.reset_mock()

            with patch("time.time", return_value=1060.0):
                switched = self.watcher.process_line(exhaustion_line)
                self.assertTrue(switched, "process_line must allow failover at exact 60.0s boundary")
                self.engine_mock.switch_tokens.assert_called_once_with("standby-acc", restart_server=True, force=True)
                self.assertIn("primary-acc", self.watcher.get_cooldowns())
                self.assertEqual(self.watcher.last_switch_time, 1060.0)

        # --- Sub-scenario C: [PRE-PROBE-EDGE-004] Exact 60.0s temporal boundary direct handle_exhaustion ---
        with self.subTest("[PRE-PROBE-EDGE-004]: elapsed = 60.0s exact boundary via direct handle_exhaustion"):
            cache_file.write_text(
                json.dumps({
                    "accounts": {
                        "primary-acc": {"gemini": {"fraction": 0.15}},
                        "standby-acc": {"gemini": {"fraction": 0.85}},
                    }
                }),
                encoding="utf-8",
            )
            self.watcher.cooldown_manager.clear_cooldown()
            self.watcher.last_switch_time = 2000.0
            self.engine_mock.switch_tokens.reset_mock()

            with patch("time.time", return_value=2060.0):
                res = self.watcher.handle_exhaustion(line=exhaustion_line)
                self.assertTrue(res, "handle_exhaustion must allow failover at exact 60.0s boundary")
                self.engine_mock.switch_tokens.assert_called_once_with("standby-acc", restart_server=True, force=True)
                self.assertIn("primary-acc", self.watcher.get_cooldowns())
                self.assertEqual(self.watcher.last_switch_time, 2060.0)

        # --- Sub-scenario D: Exact 60.0s boundary with healthy quota (> 25%) allows probe but suppresses rotation ---
        with self.subTest("elapsed = 60.0s with healthy quota (> 25%) allows probe and suppresses rotation"):
            cache_file.write_text(
                json.dumps({
                    "accounts": {
                        "primary-acc": {"gemini": {"fraction": 0.75}},
                        "standby-acc": {"gemini": {"fraction": 0.90}},
                    }
                }),
                encoding="utf-8",
            )
            self.watcher.cooldown_manager.clear_cooldown()
            self.watcher.last_switch_time = 1000.0
            self.engine_mock.switch_tokens.reset_mock()

            with patch("time.time", return_value=1060.0):
                res = self.watcher.handle_exhaustion(line=exhaustion_line)
                self.assertFalse(res, "handle_exhaustion must suppress when active quota is healthy (> 25%) even after damping elapses")
                self.engine_mock.switch_tokens.assert_not_called()
                self.assertNotIn("primary-acc", self.watcher.get_cooldowns())
                self.assertEqual(self.watcher.last_switch_time, 1000.0)

        # --- Sub-scenario E: Just before boundary (elapsed = 59.9s < 60.0s) is suppressed by damping ---
        with self.subTest("elapsed = 59.9s (< 60.0s) is suppressed by damping window"):
            cache_file.write_text(
                json.dumps({
                    "accounts": {
                        "primary-acc": {"gemini": {"fraction": 0.10}},
                        "standby-acc": {"gemini": {"fraction": 0.90}},
                    }
                }),
                encoding="utf-8",
            )
            self.watcher.cooldown_manager.clear_cooldown()
            self.watcher.last_switch_time = 1000.0
            self.engine_mock.switch_tokens.reset_mock()

            with patch("time.time", return_value=1059.9):
                res = self.watcher.handle_exhaustion(line=exhaustion_line)
                self.assertFalse(res, "handle_exhaustion must be suppressed when elapsed < damping_seconds")
                self.engine_mock.switch_tokens.assert_not_called()
                self.assertNotIn("primary-acc", self.watcher.get_cooldowns())
                self.assertEqual(self.watcher.last_switch_time, 1000.0)

    @patch("scripts.watch.time.sleep", return_value=None)
    def test_pre_switch_network_failure_and_custom_damping(self, mock_sleep):
        """[PRE-PROBE-ERR-002] & [PRE-PROBE-EDGE-003]: Pre-Switch Network Timeout Resilience and Custom Damping.

        - [PRE-PROBE-ERR-002]: GIVEN active account "primary-acc" encounters a quota error pattern in
          transcript.jsonl, and cached quota is missing or stale, and live CloudCode telemetry request
          raises urllib.error.URLError or socket timeout, WHEN LogWatcher.handle_exhaustion() executes
          the Pre-Switch Quota Probe, THEN it catches the network exception gracefully without crashing
          the watcher daemon, and treats the quota state as unverified, authorizing rotation to the
          next standby account.
        - [PRE-PROBE-EDGE-003]: GIVEN LogWatcher initialized with custom damping_seconds = 90.0, WHEN
          an error arrives 75 seconds after a switch, THEN LogWatcher suppresses rotation in accordance
          with the configured 90.0-second window.
        """
        cache_dir = Path(self.tmpdir) / "cache_net_timeout"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / "last_known_quota.json"
        self.engine_mock.cache_dir = cache_dir
        self.engine_mock.cache_file = cache_file

        self.engine_mock.get_active_name.return_value = "primary-acc"
        self.engine_mock.list_accounts.return_value = {
            "active": "primary-acc",
            "accounts": [
                {"name": "primary-acc", "valid": True, "is_active": True},
                {"name": "standby-acc", "valid": True, "is_active": False},
            ],
        }
        self.engine_mock.switch_tokens.return_value = {"success": True, "active": "standby-acc"}
        exhaustion_line = json.dumps({"status": "ERROR", "content": "RESOURCE_EXHAUSTED (code 429)"})

        # --- Sub-scenario A: [PRE-PROBE-ERR-002] Missing cache + urllib.error.URLError during probe ---
        with self.subTest("[PRE-PROBE-ERR-002]: missing cache + URLError falls back safely to rotation"):
            cache_file.unlink(missing_ok=True)
            self.watcher.cooldown_manager.clear_cooldown()
            self.watcher.last_switch_time = 0.0
            self.engine_mock.switch_tokens.reset_mock()

            with patch("scripts.watch.fetch_account_quota", side_effect=urllib.error.URLError("Network unreachable")):
                res_url_err = self.watcher.handle_exhaustion(line=exhaustion_line)
                self.assertTrue(res_url_err, "Network URLError during probe must fall back safely to rotation")
                self.assertIn("primary-acc", self.watcher.get_cooldowns())
                self.engine_mock.switch_tokens.assert_called_once_with("standby-acc", restart_server=True, force=True)

        # --- Sub-scenario B: [PRE-PROBE-ERR-002] Stale cache + socket.timeout during probe ---
        with self.subTest("[PRE-PROBE-ERR-002]: stale cache + socket timeout falls back safely to rotation"):
            # Active account cache has expired cached_at (> 300s old) or explicit stale flag
            cache_file.write_text(
                json.dumps({
                    "accounts": {
                        "primary-acc": {"gemini": {"fraction": 0.80}, "cached_at": 1000.0, "stale": True},
                        "standby-acc": {"gemini": {"fraction": 0.90}},
                    }
                }),
                encoding="utf-8",
            )
            self.watcher.cooldown_manager.clear_cooldown()
            self.watcher.last_switch_time = 0.0
            self.engine_mock.switch_tokens.reset_mock()

            with patch("scripts.watch.fetch_account_quota", side_effect=socket.timeout("Socket connection timed out")):
                with patch("time.time", return_value=1600.0):
                    res_timeout = self.watcher.handle_exhaustion(line=exhaustion_line)
                    self.assertTrue(res_timeout, "Socket timeout during live probe must fall back safely to rotation")
                    self.assertIn("primary-acc", self.watcher.get_cooldowns())
                    self.engine_mock.switch_tokens.assert_called_once_with("standby-acc", restart_server=True, force=True)

        # --- Sub-scenario C: [PRE-PROBE-ERR-002] Missing cache + TimeoutError / OSError during probe ---
        with self.subTest("[PRE-PROBE-ERR-002]: missing cache + TimeoutError / OSError falls back safely"):
            cache_file.unlink(missing_ok=True)
            self.watcher.cooldown_manager.clear_cooldown()
            self.watcher.last_switch_time = 0.0
            self.engine_mock.switch_tokens.reset_mock()

            with patch("scripts.watch.fetch_account_quota", side_effect=TimeoutError("Request timed out")):
                res_to = self.watcher.handle_exhaustion(line=exhaustion_line)
                self.assertTrue(res_to, "TimeoutError during live probe must fall back safely to rotation")
                self.assertIn("primary-acc", self.watcher.get_cooldowns())
                self.engine_mock.switch_tokens.assert_called_once_with("standby-acc", restart_server=True, force=True)

        # --- Sub-scenario D: [PRE-PROBE-EDGE-003] Custom damping_seconds = 90.0 suppresses rotation at 75s ---
        with self.subTest("[PRE-PROBE-EDGE-003]: damping_seconds = 90.0 suppresses rotation at elapsed = 75.0s"):
            custom_watcher = LogWatcher(
                engine=self.engine_mock,
                cooldown_manager=self.watcher.cooldown_manager,
                damping_seconds=90.0,
            )
            self.assertEqual(custom_watcher.damping_seconds, 90.0)
            custom_watcher.cooldown_manager.clear_cooldown()
            custom_watcher.last_switch_time = 1000.0
            self.engine_mock.switch_tokens.reset_mock()

            # At elapsed = 75.0s (< 90.0s), handle_exhaustion suppresses rotation
            with patch("time.time", return_value=1075.0):
                res_damped = custom_watcher.handle_exhaustion(line=exhaustion_line)
                self.assertFalse(res_damped, "Custom damping_seconds=90.0 must suppress rotation at 75.0s")
                self.engine_mock.switch_tokens.assert_not_called()
                self.assertNotIn("primary-acc", custom_watcher.get_cooldowns())
                self.assertEqual(custom_watcher.last_switch_time, 1000.0)

            # Also verify process_line at elapsed = 75.0s is suppressed
            with patch("time.time", return_value=1075.0):
                proc_damped = custom_watcher.process_line(exhaustion_line)
                self.assertFalse(proc_damped, "Custom damping_seconds=90.0 must suppress process_line at 75.0s")
                self.engine_mock.switch_tokens.assert_not_called()
                self.assertNotIn("primary-acc", custom_watcher.get_cooldowns())

        # --- Sub-scenario E: [PRE-PROBE-EDGE-003] Custom damping_seconds = 90.0 expires at 90.0s and allows rotation ---
        with self.subTest("[PRE-PROBE-EDGE-003]: damping_seconds = 90.0 allows rotation when elapsed >= 90.0s"):
            cache_file.write_text(
                json.dumps({
                    "accounts": {
                        "primary-acc": {"gemini": {"fraction": 0.10}},
                        "standby-acc": {"gemini": {"fraction": 0.90}},
                    }
                }),
                encoding="utf-8",
            )
            custom_watcher = LogWatcher(
                engine=self.engine_mock,
                cooldown_manager=self.watcher.cooldown_manager,
                damping_seconds=90.0,
            )
            custom_watcher.cooldown_manager.clear_cooldown()
            custom_watcher.last_switch_time = 1000.0
            self.engine_mock.switch_tokens.reset_mock()

            # At elapsed = 90.0s (boundary elapsed >= damping_seconds), rotation is authorized
            with patch("time.time", return_value=1090.0):
                res_elapsed = custom_watcher.handle_exhaustion(line=exhaustion_line)
                self.assertTrue(res_elapsed, "Rotation must be authorized once custom damping window (90.0s) elapses")
                self.engine_mock.switch_tokens.assert_called_once_with("standby-acc", restart_server=True, force=True)
                self.assertIn("primary-acc", custom_watcher.get_cooldowns())
                self.assertEqual(custom_watcher.last_switch_time, 1090.0)



class TestPresenceDetectorWatchTargets(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.gemini_dir = Path(self.tmpdir) / ".gemini"
        self.proc_dir = Path(self.tmpdir) / "proc"
        self.proc_dir.mkdir(parents=True, exist_ok=True)
        self.detector = PresenceDetector(gemini_dir=self.gemini_dir, proc_dir=self.proc_dir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_get_watch_targets_omits_cli_logs(self):
        """[WATCH-BOUND-001]: GIVEN an active Antigravity session with active transcript
        and directory ~/.gemini/antigravity-cli/log/ contains multiple cli-*.log files,
        WHEN PresenceDetector.get_watch_targets() is invoked,
        THEN the returned target list contains exclusively the resolved transcript.jsonl path,
        and contains zero paths from ~/.gemini/antigravity-cli/log/.
        """
        session_id = "test-session-bound-001"
        self.detector.presence_dir.mkdir(parents=True, exist_ok=True)
        (self.detector.presence_dir / f"{session_id}.lock").touch()

        transcript_dir = self.detector.brain_dir / session_id / ".system_generated" / "logs"
        transcript_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = transcript_dir / "transcript.jsonl"
        transcript_path.write_text('{"event": "start"}\n', encoding="utf-8")

        self.detector.cli_log_dir.mkdir(parents=True, exist_ok=True)
        cli_log1 = self.detector.cli_log_dir / "cli-20261111-1.log"
        cli_log2 = self.detector.cli_log_dir / "cli-20261111-2.log"
        cli_log1.write_text("CLI log 1", encoding="utf-8")
        cli_log2.write_text("CLI log 2", encoding="utf-8")

        targets = self.detector.get_watch_targets()
        self.assertEqual(targets, [transcript_path])
        self.assertNotIn(cli_log1, targets)
        self.assertNotIn(cli_log2, targets)
        for target in targets:
            self.assertFalse(str(target).startswith(str(self.detector.cli_log_dir)))

        str_targets = self.detector.get_watch_targets(as_str=True)
        self.assertEqual(str_targets, [str(transcript_path)])

    def test_get_watch_targets_omits_cli_logs_when_no_active_transcript(self):
        """[WATCH-BOUND-EDGE-002]: GIVEN no Antigravity Desktop or CLI presence locks exist on the host,
        and PresenceDetector.get_active_transcript_path() returns None,
        WHEN PresenceDetector.get_watch_targets() is invoked,
        THEN it returns an empty list [] without raising FileNotFoundError or inspecting CLI logs.
        """
        self.detector.cli_log_dir.mkdir(parents=True, exist_ok=True)
        cli_log = self.detector.cli_log_dir / "cli-orphan.log"
        cli_log.write_text("Orphan CLI log", encoding="utf-8")

        self.assertIsNone(self.detector.get_active_transcript_path())

        targets = self.detector.get_watch_targets()
        self.assertEqual(targets, [])

        str_targets = self.detector.get_watch_targets(as_str=True)
        self.assertEqual(str_targets, [])


class TestSafeTurnBoundaryQuiescence(unittest.TestCase):
    """Tests for Safe Turn Boundary detection and server quiescence engine.

    Covers:
    - [WATCH-PROACTIVE-005]: Safe Turn Boundary verification via transcript record and process supervisor.
    - [WATCH-PROACTIVE-002]: Proactive rotation deferred while language server is busy or turn in-flight.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.gemini_dir = Path(self.tmpdir) / ".gemini"
        self.proc_dir = Path(self.tmpdir) / "proc"
        self.vault_dir = Path(self.tmpdir) / "vault"
        self.cache_dir = self.vault_dir / "cache"

        self.gemini_dir.mkdir(parents=True, exist_ok=True)
        self.proc_dir.mkdir(parents=True, exist_ok=True)
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.engine_mock = MagicMock()
        self.engine_mock.vault_dir = self.vault_dir
        self.engine_mock.cache_dir = self.cache_dir
        self.engine_mock.gemini_dir = self.gemini_dir
        self.engine_mock.get_quota_threshold.return_value = 0.20

        self.detector = PresenceDetector(gemini_dir=self.gemini_dir, proc_dir=self.proc_dir)
        self.supervisor = ProcessSupervisor(gemini_dir=self.gemini_dir, proc_dir=self.proc_dir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _setup_session(self, session_id: str = "session-quiescent-001") -> Path:
        """Helper creating presence lock and transcript directory structure."""
        presence_dir = self.gemini_dir / "antigravity" / "presence"
        presence_dir.mkdir(parents=True, exist_ok=True)
        lock_file = presence_dir / f"{session_id}.lock"
        lock_file.touch()

        logs_dir = self.gemini_dir / "antigravity" / "brain" / session_id / ".system_generated" / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = logs_dir / "transcript.jsonl"
        return transcript_path

    def test_safe_turn_boundary_quiescent_user_input_and_server_idle(self):
        """[WATCH-PROACTIVE-005]: GIVEN an active transcript tailing loop where the latest record
        has type == 'USER_INPUT' AND no open presence locks or active child processes exist in /proc
        for language_server,
        WHEN LogWatcher._is_turn_quiescent() or ProcessSupervisor.is_server_busy() is evaluated,
        THEN it confirms that the agent turn has completed and the system is waiting for user input
        AND certifies the state as a verified Safe Turn Boundary.
        """
        transcript_path = self._setup_session("session-005")
        records = [
            {"type": "MESSAGE", "status": "DONE", "content": "Initial assistant response."},
            {"type": "USER_INPUT", "content": "Next user instruction for the agent."},
        ]
        transcript_path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

        watcher = LogWatcher(
            engine=self.engine_mock,
            presence_detector=self.detector,
            supervisor=self.supervisor,
            log_dirs=[],
        )

        # Confirm ProcessSupervisor sees no open presence locks or active child tasks
        self.assertFalse(self.supervisor.is_server_busy(), "Server must be idle with no active locks or children")
        # Confirm LogWatcher certifies the state as a verified Safe Turn Boundary
        self.assertTrue(watcher._is_turn_quiescent(), "Safe Turn Boundary must be certified when latest record is USER_INPUT and server is idle")

    def test_safe_turn_boundary_quiescent_status_done_and_server_idle(self):
        """[WATCH-PROACTIVE-005]: Verifies status == 'DONE' also certifies Safe Turn Boundary."""
        transcript_path = self._setup_session("session-done")
        records = [
            {"type": "USER_INPUT", "content": "Perform task."},
            {"type": "MESSAGE", "status": "IN_PROGRESS", "content": "Running..."},
            {"type": "MESSAGE", "status": "DONE", "content": "Completed task."},
        ]
        transcript_path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

        watcher = LogWatcher(
            engine=self.engine_mock,
            presence_detector=self.detector,
            supervisor=self.supervisor,
            log_dirs=[],
        )

        self.assertFalse(self.supervisor.is_server_busy())
        self.assertTrue(watcher._is_turn_quiescent())

    def test_safe_turn_boundary_not_quiescent_when_turn_in_flight(self):
        """[WATCH-PROACTIVE-005]: When latest record is streaming (status == 'IN_PROGRESS'),
        _is_turn_quiescent() returns False.
        """
        transcript_path = self._setup_session("session-streaming")
        records = [
            {"type": "USER_INPUT", "content": "Generate code."},
            {"type": "MESSAGE", "status": "IN_PROGRESS", "content": "Writing tokens..."},
        ]
        transcript_path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

        watcher = LogWatcher(
            engine=self.engine_mock,
            presence_detector=self.detector,
            supervisor=self.supervisor,
            log_dirs=[],
        )

        self.assertFalse(self.supervisor.is_server_busy())
        self.assertFalse(watcher._is_turn_quiescent(), "In-flight streaming turn must not be quiescent")

    def test_safe_turn_boundary_not_quiescent_when_server_busy_with_presence_lock(self):
        """[WATCH-PROACTIVE-005]: When a process holds an open fd to presence lock,
        is_server_busy() is True and _is_turn_quiescent() returns False.
        """
        transcript_path = self._setup_session("session-locked")
        transcript_path.write_text(json.dumps({"type": "USER_INPUT", "content": "Hello"}) + "\n", encoding="utf-8")

        lock_path = self.gemini_dir / "antigravity" / "presence" / "session-locked.lock"
        pid_fd = self.proc_dir / "7777" / "fd"
        pid_fd.mkdir(parents=True, exist_ok=True)
        (pid_fd / "3").symlink_to(lock_path)

        watcher = LogWatcher(
            engine=self.engine_mock,
            presence_detector=self.detector,
            supervisor=self.supervisor,
            log_dirs=[],
        )

        self.assertTrue(self.supervisor.is_server_busy(), "Open fd on presence lock marks server busy")
        self.assertFalse(watcher._is_turn_quiescent(), "Turn must not be quiescent when presence lock is held")

    def test_safe_turn_boundary_not_quiescent_when_server_busy_with_child_process(self):
        """[WATCH-PROACTIVE-005]: When language_server has active child subtask in /proc,
        is_server_busy() is True and _is_turn_quiescent() returns False.
        """
        transcript_path = self._setup_session("session-child")
        transcript_path.write_text(json.dumps({"type": "USER_INPUT", "content": "Hello"}) + "\n", encoding="utf-8")

        # Set up language_server at PID 500
        ls_dir = self.proc_dir / "500"
        ls_dir.mkdir(parents=True, exist_ok=True)
        (ls_dir / "cmdline").write_bytes(b"/opt/antigravity/language_server\x00--standalone\x00--port\x008080\x00")

        # Set up active child process at PID 600 with PPID 500 (state 'S' = sleeping/running, not 'Z')
        child_dir = self.proc_dir / "600"
        child_dir.mkdir(parents=True, exist_ok=True)
        (child_dir / "stat").write_text("600 (active_tool) S 500 0 0 0 0")

        watcher = LogWatcher(
            engine=self.engine_mock,
            presence_detector=self.detector,
            supervisor=self.supervisor,
            log_dirs=[],
        )

        self.assertTrue(self.supervisor.is_server_busy(), "Active child task under language_server marks server busy")
        self.assertFalse(watcher._is_turn_quiescent(), "Turn must not be quiescent when language_server has active child process")

    def test_safe_turn_boundary_not_quiescent_empty_or_missing_transcript(self):
        """[WATCH-PROACTIVE-005]: Missing or empty transcript returns False for _is_turn_quiescent()."""
        watcher = LogWatcher(
            engine=self.engine_mock,
            presence_detector=self.detector,
            supervisor=self.supervisor,
            log_dirs=[],
        )
        self.assertFalse(watcher._is_turn_quiescent(), "No active transcript must return False")

        # Empty transcript file
        transcript_path = self._setup_session("session-empty")
        transcript_path.write_text("", encoding="utf-8")
        self.assertFalse(watcher._is_turn_quiescent(), "Empty transcript file must return False")

    def test_safe_turn_boundary_not_quiescent_corrupt_or_partial_line(self):
        """[WATCH-PROACTIVE-005]: Trailing unclosed JSON (streaming in flight) returns False."""
        transcript_path = self._setup_session("session-partial")
        # Line 1 completed, Line 2 is actively being streamed/written
        transcript_path.write_text(
            '{"type": "USER_INPUT", "content": "Prompt"}\n{"type": "MESSAGE", "status": "IN_PRO',
            encoding="utf-8",
        )
        watcher = LogWatcher(
            engine=self.engine_mock,
            presence_detector=self.detector,
            supervisor=self.supervisor,
            log_dirs=[],
        )
        self.assertFalse(watcher._is_turn_quiescent(), "Incomplete trailing record must not be quiescent")

    def test_safe_turn_boundary_quiescent_large_record_over_64kb(self):
        """[WATCH-PROACTIVE-005]: Expanding backward window accommodates large prompts/responses (>64KB)."""
        transcript_path = self._setup_session("session-large")
        large_content = "X" * 75000
        records = [
            {"type": "MESSAGE", "status": "DONE", "content": "Short intro"},
            {"type": "USER_INPUT", "content": large_content},
        ]
        transcript_path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

        watcher = LogWatcher(
            engine=self.engine_mock,
            presence_detector=self.detector,
            supervisor=self.supervisor,
            log_dirs=[],
        )
        self.assertFalse(self.supervisor.is_server_busy())
        self.assertTrue(watcher._is_turn_quiescent(), "Large record (>64KB) must be parsed without truncation")

    def test_safe_turn_boundary_not_quiescent_explicit_nonexistent_transcript_path(self):
        """[WATCH-PROACTIVE-005]: Explicit nonexistent path returns False without falling back to host brain_dir."""
        watcher = LogWatcher(
            engine=self.engine_mock,
            presence_detector=self.detector,
            supervisor=self.supervisor,
            log_dirs=[],
        )
        nonexistent = Path(self.tmpdir) / "nonexistent" / "transcript.jsonl"
        self.assertFalse(
            watcher._is_turn_quiescent(transcript_path=nonexistent),
            "Nonexistent explicit transcript path must immediately return False",
        )

    def test_safe_turn_boundary_not_quiescent_when_tool_calls_present(self):
        """[WATCH-PROACTIVE-005]: Step with status DONE but pending tool_calls is not quiescent."""
        transcript_path = self._setup_session("session-tool-calls")
        record = {
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "tool_calls": [{"name": "run_command", "args": {"CommandLine": "ls"}}],
        }
        transcript_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

        watcher = LogWatcher(
            engine=self.engine_mock,
            presence_detector=self.detector,
            supervisor=self.supervisor,
            log_dirs=[],
        )
        self.assertFalse(
            watcher._is_turn_quiescent(),
            "Turn awaiting tool call execution must not be quiescent",
        )

    def test_safe_turn_boundary_not_quiescent_when_status_in_progress_with_user_input(self):
        """[WATCH-PROACTIVE-005]: IN_PROGRESS status is never quiescent even if type is USER_INPUT."""
        transcript_path = self._setup_session("session-inprogress-user")
        record = {
            "type": "USER_INPUT",
            "status": "IN_PROGRESS",
            "content": "Streaming user input",
        }
        transcript_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

        watcher = LogWatcher(
            engine=self.engine_mock,
            presence_detector=self.detector,
            supervisor=self.supervisor,
            log_dirs=[],
        )
        self.assertFalse(watcher._is_turn_quiescent(), "IN_PROGRESS state must always return False")

    def test_safe_turn_boundary_proc_reading_graceful_on_permission_error(self):
        """[WATCH-PROACTIVE-005]: PermissionError during /proc traversal degrades gracefully to idle."""
        transcript_path = self._setup_session("session-perm-err")
        transcript_path.write_text(json.dumps({"type": "USER_INPUT", "content": "Hello"}) + "\n", encoding="utf-8")

        mock_proc_dir = MagicMock(spec=Path)
        mock_proc_dir.is_dir.return_value = True
        mock_proc_dir.iterdir.side_effect = PermissionError("Access denied to /proc")

        restricted_supervisor = ProcessSupervisor(
            gemini_dir=self.gemini_dir,
            proc_dir=mock_proc_dir,
        )
        watcher = LogWatcher(
            engine=self.engine_mock,
            presence_detector=self.detector,
            supervisor=restricted_supervisor,
            log_dirs=[],
        )
        # Should not raise PermissionError; should handle gracefully
        self.assertFalse(restricted_supervisor.is_server_busy())
        self.assertTrue(watcher._is_turn_quiescent())

    def test_proactive_rotation_deferred_while_server_busy_or_turn_not_quiescent(self):
        """[WATCH-PROACTIVE-002]: GIVEN active account 'primary-acc' has depleted quota at 14% (<= 20%)
        AND standby account 'standby-acc' has healthy quota at 90%
        AND the language server is currently executing an agentic turn or ProcessSupervisor.is_server_busy() returns True,
        WHEN LogWatcher.poll_once() evaluates proactive rotation eligibility,
        THEN it detects that the system is not at a Safe Turn Boundary
        AND defers credential rotation without restarting language_server or modifying active tokens
        AND returns False until the turn concludes and the server becomes quiescent.
        """
        transcript_path = self._setup_session("session-proactive-002")
        # Initially, the turn is in-flight (status == 'IN_PROGRESS')
        transcript_path.write_text(
            json.dumps({"type": "USER_INPUT", "content": "Do heavy computation."}) + "\n"
            + json.dumps({"type": "MESSAGE", "status": "IN_PROGRESS", "content": "Processing..."}) + "\n",
            encoding="utf-8",
        )

        quota_cache_file = self.cache_dir / "last_known_quota.json"
        quota_cache_file.write_text(
            json.dumps({
                "accounts": {
                    "primary-acc": {"gemini": {"fraction": 0.14}},
                    "standby-acc": {"gemini": {"fraction": 0.90}},
                }
            }),
            encoding="utf-8",
        )

        self.engine_mock.get_active_name.return_value = "primary-acc"
        self.engine_mock.list_accounts.return_value = {
            "accounts": [
                {"name": "primary-acc", "valid": True},
                {"name": "standby-acc", "valid": True},
            ]
        }
        self.engine_mock.switch_tokens.return_value = True

        watcher = LogWatcher(
            engine=self.engine_mock,
            presence_detector=self.detector,
            supervisor=self.supervisor,
            log_dirs=[],
        )

        # Case A: Turn is in-flight in transcript, server idle
        res_inflight = watcher.poll_once()
        self.assertFalse(res_inflight, "poll_once() must return 0/False while turn is in-flight")
        self.engine_mock.switch_tokens.assert_not_called()

        # Case B: Turn transcript reports DONE, but language_server is busy (e.g. presence lock held)
        transcript_path.write_text(
            json.dumps({"type": "MESSAGE", "status": "DONE", "content": "Finished step 1."}) + "\n",
            encoding="utf-8",
        )
        lock_path = self.gemini_dir / "antigravity" / "presence" / "session-proactive-002.lock"
        pid_fd = self.proc_dir / "8888" / "fd"
        pid_fd.mkdir(parents=True, exist_ok=True)
        (pid_fd / "4").symlink_to(lock_path)

        self.assertTrue(self.supervisor.is_server_busy())
        res_busy = watcher.poll_once()
        self.assertFalse(res_busy, "poll_once() must return 0/False while server is busy")
        self.engine_mock.switch_tokens.assert_not_called()

        # Clean up lock fd so server becomes quiescent
        (pid_fd / "4").unlink()
        self.assertFalse(self.supervisor.is_server_busy())
        lock_path.touch()

        # Case C: Both conditions met (turn completed with USER_INPUT or DONE AND server quiescent)
        # Safe Turn Boundary is reached -> rotation proceeds!
        res_quiescent = watcher.poll_once()
        self.assertTrue(res_quiescent, "poll_once() must trigger rotation once Safe Turn Boundary is reached")
        self.engine_mock.switch_tokens.assert_called_once_with("standby-acc", restart_server=True, force=True)


class TestSessionDiscoveryAndReloadResilience(unittest.TestCase):
    """Tests for session discovery guards and reload resilience during proactive failover.

    Covers:
    - [WATCH-PROACTIVE-ERR-002]: Missing transcript or unresolvable active session skips proactive rotation safely.
    - [WATCH-PROACTIVE-ERR-003]: Server reload failure on Safe Turn Boundary logged safely without crashing watcher daemon.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.gemini_dir = Path(self.tmpdir) / ".gemini"
        self.proc_dir = Path(self.tmpdir) / "proc"
        self.vault_dir = Path(self.tmpdir) / "vault"
        self.cache_dir = self.vault_dir / "cache"

        self.gemini_dir.mkdir(parents=True, exist_ok=True)
        self.proc_dir.mkdir(parents=True, exist_ok=True)
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.engine_mock = MagicMock()
        self.engine_mock.vault_dir = self.vault_dir
        self.engine_mock.cache_dir = self.cache_dir
        self.engine_mock.gemini_dir = self.gemini_dir
        self.engine_mock.get_quota_threshold.return_value = 0.20
        self.engine_mock.get_active_name.return_value = "primary-acc"
        self.engine_mock.list_accounts.return_value = {
            "accounts": [
                {"name": "primary-acc", "valid": True},
                {"name": "standby-acc", "valid": True},
            ]
        }

        # Setup quota cache: primary-acc at 14% (<= 20% threshold), standby-acc at 90%
        quota_cache_file = self.cache_dir / "last_known_quota.json"
        quota_cache_file.write_text(
            json.dumps({
                "accounts": {
                    "primary-acc": {"gemini": {"fraction": 0.14}},
                    "standby-acc": {"gemini": {"fraction": 0.90}},
                }
            }),
            encoding="utf-8",
        )

        self.detector = PresenceDetector(gemini_dir=self.gemini_dir, proc_dir=self.proc_dir)
        self.supervisor = ProcessSupervisor(gemini_dir=self.gemini_dir, proc_dir=self.proc_dir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_session_discovery_missing_transcript_skips_proactive_rotation(self):
        """[WATCH-PROACTIVE-ERR-002]: GIVEN no Antigravity session transcript is discovered by PresenceDetector
        WHEN LogWatcher.poll_once() evaluates proactive rotation
        THEN it determines that no active session boundary can be verified
        AND skips proactive rotation evaluation safely without raising exceptions.
        """
        # Case A: Old inactive session transcript exists in brain_dir from a previous session
        old_logs_dir = self.gemini_dir / "antigravity" / "brain" / "old-session-inactive" / ".system_generated" / "logs"
        old_logs_dir.mkdir(parents=True, exist_ok=True)
        old_transcript = old_logs_dir / "transcript.jsonl"
        old_transcript.write_text(
            json.dumps({"type": "MESSAGE", "status": "DONE", "content": "Old finished session."}) + "\n",
            encoding="utf-8",
        )
        os.utime(old_transcript, (time.time() - 3600.0, time.time() - 3600.0))

        # PresenceDetector discovers NO active session (no presence lock exists)
        self.assertIsNone(self.detector.get_active_session_id())
        self.assertIsNone(self.detector.get_active_transcript_path())

        watcher = LogWatcher(
            engine=self.engine_mock,
            presence_detector=self.detector,
            supervisor=self.supervisor,
            log_dirs=[],
        )

        # poll_once must safely evaluate proactive rotation, determine that no active
        # session boundary can be verified, and skip rotation without exceptions.
        res = watcher.poll_once()
        self.assertEqual(res, 0, "Must return 0 switches when active session cannot be verified")
        self.engine_mock.switch_tokens.assert_not_called()

        # Case B: Completely absent transcript file
        old_transcript.unlink(missing_ok=True)
        res_empty = watcher.poll_once()
        self.assertEqual(res_empty, 0, "Must return 0 switches when no transcript exists")
        self.engine_mock.switch_tokens.assert_not_called()

    def test_session_discovery_detector_exception_skips_safely(self):
        """[WATCH-PROACTIVE-ERR-002]: GIVEN PresenceDetector raises an error during discovery
        WHEN LogWatcher.poll_once() evaluates proactive rotation
        THEN it catches the exception and skips rotation safely without crashing.
        """
        mock_detector = MagicMock(spec=PresenceDetector)
        mock_detector.get_active_session_id.side_effect = OSError("Presence directory read failure")
        mock_detector.get_active_transcript_path.side_effect = OSError("Presence directory read failure")
        mock_detector.get_watch_targets.return_value = []

        watcher = LogWatcher(
            engine=self.engine_mock,
            presence_detector=mock_detector,
            supervisor=self.supervisor,
            log_dirs=[],
        )

        res = watcher.poll_once()
        self.assertEqual(res, 0, "Must gracefully handle discovery error and return 0")
        self.engine_mock.switch_tokens.assert_not_called()

    def test_session_discovery_recent_inactive_transcript_without_lock_skips_proactive_rotation(self):
        """[WATCH-PROACTIVE-ERR-002]: GIVEN an inactive transcript exists with recent mtime (not backdated)
        AND no Antigravity session presence lock is active
        WHEN LogWatcher.poll_once() evaluates proactive rotation
        THEN it determines that no active session boundary can be verified
        AND skips proactive rotation evaluation safely without triggering rotation.
        """
        recent_logs_dir = self.gemini_dir / "antigravity" / "brain" / "recent-inactive" / ".system_generated" / "logs"
        recent_logs_dir.mkdir(parents=True, exist_ok=True)
        recent_transcript = recent_logs_dir / "transcript.jsonl"
        recent_transcript.write_text(
            json.dumps({"type": "MESSAGE", "status": "DONE", "content": "Recently finished turn."}) + "\n",
            encoding="utf-8",
        )
        # Note: Transcript is brand new (mtime is now), but NO presence lock exists

        self.assertIsNone(self.detector.get_active_session_id())

        watcher = LogWatcher(
            engine=self.engine_mock,
            presence_detector=self.detector,
            supervisor=self.supervisor,
            log_dirs=[],
        )

        res = watcher.poll_once()
        self.assertEqual(res, 0, "Must not trigger rotation on inactive session even if transcript is recent")
        self.engine_mock.switch_tokens.assert_not_called()

    def test_session_discovery_active_session_with_missing_transcript_file_skips_safely(self):
        """[WATCH-PROACTIVE-ERR-002]: GIVEN an active session presence lock exists
        BUT transcript.jsonl has not yet been created on disk
        WHEN LogWatcher.poll_once() evaluates proactive rotation
        THEN it skips proactive rotation safely without raising exceptions.
        """
        session_id = "session-active-no-transcript"
        presence_dir = self.gemini_dir / "antigravity" / "presence"
        presence_dir.mkdir(parents=True, exist_ok=True)
        (presence_dir / f"{session_id}.lock").touch()

        self.assertEqual(self.detector.get_active_session_id(), session_id)

        watcher = LogWatcher(
            engine=self.engine_mock,
            presence_detector=self.detector,
            supervisor=self.supervisor,
            log_dirs=[],
        )

        res = watcher.poll_once()
        self.assertEqual(res, 0, "Must return 0 when transcript file is missing")
        self.engine_mock.switch_tokens.assert_not_called()

    def test_session_discovery_proactive_rotation_unexpected_engine_error_handled_cleanly(self):
        """[WATCH-PROACTIVE-ERR-003]: GIVEN an unexpected engine exception during proactive rotation evaluation
        WHEN LogWatcher.poll_once() evaluates proactive rotation
        THEN it logs the error to stderr and continues cleanly without crashing.
        """
        session_id = "session-active-err"
        presence_dir = self.gemini_dir / "antigravity" / "presence"
        presence_dir.mkdir(parents=True, exist_ok=True)
        (presence_dir / f"{session_id}.lock").touch()

        logs_dir = self.gemini_dir / "antigravity" / "brain" / session_id / ".system_generated" / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = logs_dir / "transcript.jsonl"
        transcript_path.write_text(
            json.dumps({"type": "MESSAGE", "status": "DONE", "content": "Done."}) + "\n",
            encoding="utf-8",
        )

        self.engine_mock.get_active_name.side_effect = OSError("Disk read error on active account file")

        watcher = LogWatcher(
            engine=self.engine_mock,
            presence_detector=self.detector,
            supervisor=self.supervisor,
            log_dirs=[],
        )

        from io import StringIO
        stderr_capture = StringIO()
        with patch("sys.stderr", stderr_capture):
            res = watcher.poll_once()

        self.assertEqual(res, 0)
        self.engine_mock.switch_tokens.assert_not_called()
        self.assertIn("Error evaluating proactive rotation", stderr_capture.getvalue())

    def test_session_discovery_reload_failure_due_to_supervisor_error_logged_and_preserves_state(self):
        """[WATCH-PROACTIVE-ERR-003]: GIVEN LogWatcher triggers proactive rotation to standby account "standby-acc"
        AND switch_tokens fails to terminate or restart language_server due to ProcessSupervisor error
        WHEN LogWatcher receives the failed switch result
        THEN it logs the failure message to stderr
        AND does not advance last_switch_time
        AND continues its polling loop cleanly without unhandled crash.
        """
        # Setup an active session at a valid Safe Turn Boundary
        session_id = "session-active-003"
        presence_dir = self.gemini_dir / "antigravity" / "presence"
        presence_dir.mkdir(parents=True, exist_ok=True)
        (presence_dir / f"{session_id}.lock").touch()

        logs_dir = self.gemini_dir / "antigravity" / "brain" / session_id / ".system_generated" / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = logs_dir / "transcript.jsonl"
        transcript_path.write_text(
            json.dumps({"type": "MESSAGE", "status": "DONE", "content": "Assistant finished turn."}) + "\n",
            encoding="utf-8",
        )

        # switch_tokens returns a failed switch result due to ProcessSupervisor error
        self.engine_mock.switch_tokens.return_value = {
            "success": False,
            "error": "ProcessSupervisor error: language_server failed to terminate cleanly",
        }

        watcher = LogWatcher(
            engine=self.engine_mock,
            presence_detector=self.detector,
            supervisor=self.supervisor,
            log_dirs=[],
        )
        initial_switch_time = watcher.last_switch_time

        from io import StringIO
        stderr_capture = StringIO()
        with patch("sys.stderr", stderr_capture):
            res = watcher.poll_once()

        self.assertEqual(res, 0, "poll_once() must return 0 when switch_tokens fails")
        self.engine_mock.switch_tokens.assert_called_once_with("standby-acc", restart_server=True, force=True)

        stderr_output = stderr_capture.getvalue()
        self.assertIn("ProcessSupervisor error: language_server failed to terminate cleanly", stderr_output)
        self.assertEqual(watcher.last_switch_time, initial_switch_time, "last_switch_time must not advance on reload failure")

        # Daemon polling loop can continue cleanly without crash
        stderr_capture_2 = StringIO()
        with patch("sys.stderr", stderr_capture_2):
            res_2 = watcher.poll_once()
        self.assertEqual(res_2, 0)

    def test_session_discovery_reload_failure_with_active_in_result_dict(self):
        """[WATCH-PROACTIVE-ERR-003]: GIVEN switch_tokens returns a dict with error even if active key exists
        WHEN LogWatcher processes the failed result
        THEN it treats the switch as failed, logs to stderr, and does not advance last_switch_time.
        """
        session_id = "session-active-003b"
        presence_dir = self.gemini_dir / "antigravity" / "presence"
        presence_dir.mkdir(parents=True, exist_ok=True)
        (presence_dir / f"{session_id}.lock").touch()

        logs_dir = self.gemini_dir / "antigravity" / "brain" / session_id / ".system_generated" / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = logs_dir / "transcript.jsonl"
        transcript_path.write_text(
            json.dumps({"type": "MESSAGE", "status": "DONE", "content": "Done."}) + "\n",
            encoding="utf-8",
        )

        self.engine_mock.switch_tokens.return_value = {
            "active": "standby-acc",
            "error": "ProcessSupervisor error: language_server failed to restart",
        }

        watcher = LogWatcher(
            engine=self.engine_mock,
            presence_detector=self.detector,
            supervisor=self.supervisor,
            log_dirs=[],
        )
        initial_switch_time = watcher.last_switch_time

        from io import StringIO
        stderr_capture = StringIO()
        with patch("sys.stderr", stderr_capture):
            res = watcher.poll_once()

        self.assertEqual(res, 0)
        stderr_output = stderr_capture.getvalue()
        self.assertIn("ProcessSupervisor error: language_server failed to restart", stderr_output)
        self.assertEqual(watcher.last_switch_time, initial_switch_time)

    def test_session_discovery_reload_exception_from_supervisor_handled_cleanly(self):
        """[WATCH-PROACTIVE-ERR-003]: GIVEN switch_tokens raises an unhandled ProcessSupervisor exception
        WHEN LogWatcher evaluates proactive rotation
        THEN it logs the exception to stderr, does not advance last_switch_time, and continues cleanly.
        """
        session_id = "session-active-003c"
        presence_dir = self.gemini_dir / "antigravity" / "presence"
        presence_dir.mkdir(parents=True, exist_ok=True)
        (presence_dir / f"{session_id}.lock").touch()

        logs_dir = self.gemini_dir / "antigravity" / "brain" / session_id / ".system_generated" / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = logs_dir / "transcript.jsonl"
        transcript_path.write_text(
            json.dumps({"type": "MESSAGE", "status": "DONE", "content": "Done."}) + "\n",
            encoding="utf-8",
        )

        self.engine_mock.switch_tokens.side_effect = RuntimeError("ProcessSupervisor error: SIGKILL escalation timed out")

        watcher = LogWatcher(
            engine=self.engine_mock,
            presence_detector=self.detector,
            supervisor=self.supervisor,
            log_dirs=[],
        )
        initial_switch_time = watcher.last_switch_time

        from io import StringIO
        stderr_capture = StringIO()
        with patch("sys.stderr", stderr_capture):
            res = watcher.poll_once()

        self.assertEqual(res, 0)
        stderr_output = stderr_capture.getvalue()
        self.assertIn("ProcessSupervisor error: SIGKILL escalation timed out", stderr_output)
        self.assertEqual(watcher.last_switch_time, initial_switch_time)


class TestProactiveThresholdFailoverTrigger(unittest.TestCase):
    """Tests for proactive threshold failover trigger at Safe Turn Boundary.

    Covers:
    - [WATCH-PROACTIVE-001]: Proactive rotation triggered when active account quota reaches or falls below threshold at Safe Turn Boundary.
    - [WATCH-PROACTIVE-EDGE-001]: Active quota at exact threshold boundary triggers proactive evaluation and rotation.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.gemini_dir = Path(self.tmpdir) / ".gemini"
        self.proc_dir = Path(self.tmpdir) / "proc"
        self.vault_dir = Path(self.tmpdir) / "vault"
        self.cache_dir = self.vault_dir / "cache"

        self.gemini_dir.mkdir(parents=True, exist_ok=True)
        self.proc_dir.mkdir(parents=True, exist_ok=True)
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.engine_mock = MagicMock()
        self.engine_mock.vault_dir = self.vault_dir
        self.engine_mock.cache_dir = self.cache_dir
        self.engine_mock.gemini_dir = self.gemini_dir
        self.engine_mock.get_quota_threshold.return_value = 0.20
        self.engine_mock.get_active_name.return_value = "primary-acc"
        self.engine_mock.list_accounts.return_value = {
            "active": "primary-acc",
            "accounts": [
                {"name": "primary-acc", "valid": True},
                {"name": "standby-acc", "valid": True},
            ],
        }
        self.engine_mock.switch_tokens.return_value = {
            "active": "standby-acc",
            "success": True,
            "server_restarted": True,
        }

        self.detector = PresenceDetector(gemini_dir=self.gemini_dir, proc_dir=self.proc_dir)
        self.supervisor = ProcessSupervisor(gemini_dir=self.gemini_dir, proc_dir=self.proc_dir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _setup_session(self, session_id: str = "session-proactive-trigger") -> tuple[Path, Path]:
        """Helper creating presence lock and active session transcript."""
        presence_dir = self.gemini_dir / "antigravity" / "presence"
        presence_dir.mkdir(parents=True, exist_ok=True)
        lock_file = presence_dir / f"{session_id}.lock"
        lock_file.touch()

        logs_dir = self.gemini_dir / "antigravity" / "brain" / session_id / ".system_generated" / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = logs_dir / "transcript.jsonl"
        return lock_file, transcript_path

    def test_proactive_rotation_trigger_at_safe_turn_boundary_001(self):
        """[WATCH-PROACTIVE-001]: GIVEN active account 'primary-acc' with remaining Gemini quota at 14%
        (fraction = 0.14 <= 0.20) AND standby account 'standby-acc' has healthy Gemini quota (85% >= 70%)
        AND the active transcript transcript.jsonl has recorded a completed turn event with status == 'DONE'
        AND ProcessSupervisor.is_server_busy() returns False indicating the Language Server is idle
        WHEN LogWatcher.poll_once() evaluates proactive rotation eligibility
        THEN it recognizes the Safe Turn Boundary and initiates credential rotation to 'standby-acc'
        AND reloads the Language Server via switch_tokens('standby-acc', restart_server=True, force=True)
        AND returns True after updating file cursors and resetting damping timer.
        """
        lock_file, transcript_path = self._setup_session("session-proactive-001")
        record = {
            "type": "MESSAGE",
            "status": "DONE",
            "content": "Turn execution completed successfully.",
        }
        transcript_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

        quota_cache_file = self.cache_dir / "last_known_quota.json"
        quota_cache_file.write_text(
            json.dumps({
                "accounts": {
                    "primary-acc": {"gemini": {"fraction": 0.14}},
                    "standby-acc": {"gemini": {"fraction": 0.85}},
                }
            }),
            encoding="utf-8",
        )

        watcher = LogWatcher(
            engine=self.engine_mock,
            presence_detector=self.detector,
            supervisor=self.supervisor,
            log_dirs=[],
        )

        self.assertFalse(self.supervisor.is_server_busy(), "Server must be idle")
        lock_file.touch()
        self.assertEqual(watcher.last_switch_time, 0.0, "Damping timer must start uninitialized")

        before_poll = time.time()
        res = watcher.poll_once()

        self.assertTrue(res, "poll_once() must return truthy/True when proactive rotation succeeds")
        self.engine_mock.switch_tokens.assert_called_once_with(
            "standby-acc", restart_server=True, force=True
        )

        # File cursors must be updated to EOF
        transcript_key = str(transcript_path)
        self.assertIn(transcript_key, watcher.file_cursors)
        self.assertEqual(watcher.file_cursors[transcript_key], os.path.getsize(transcript_path))

        # Damping timer must be reset to recent timestamp
        self.assertGreaterEqual(watcher.last_switch_time, before_poll)

        # Subsequent immediate poll within damping window must be suppressed
        res_damping = watcher.poll_once()
        self.assertFalse(res_damping, "Subsequent poll within damping window must be suppressed")
        self.assertEqual(self.engine_mock.switch_tokens.call_count, 1)

    def test_proactive_rotation_edge_001_exact_threshold_boundary(self):
        """[WATCH-PROACTIVE-EDGE-001]: GIVEN active account 'primary-acc' has exactly 20.0% Gemini quota remaining
        (fraction = 0.20) AND configured threshold is 20% (threshold = 0.20) AND standby account 'standby-acc' has 85% quota
        AND the system is at a verified Safe Turn Boundary
        WHEN LogWatcher.poll_once() evaluates proactive rotation
        THEN it determines that fraction <= threshold is satisfied AND initiates rotation to 'standby-acc'.
        """
        lock_file, transcript_path = self._setup_session("session-proactive-edge-001")
        record = {
            "type": "MESSAGE",
            "status": "DONE",
            "content": "Execution step finished.",
        }
        transcript_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

        quota_cache_file = self.cache_dir / "last_known_quota.json"
        quota_cache_file.write_text(
            json.dumps({
                "accounts": {
                    "primary-acc": {"gemini": {"fraction": 0.20}},
                    "standby-acc": {"gemini": {"fraction": 0.85}},
                }
            }),
            encoding="utf-8",
        )

        self.engine_mock.get_quota_threshold.return_value = 0.20

        watcher = LogWatcher(
            engine=self.engine_mock,
            presence_detector=self.detector,
            supervisor=self.supervisor,
            log_dirs=[],
        )

        self.assertFalse(self.supervisor.is_server_busy())
        lock_file.touch()

        res = watcher.poll_once()
        self.assertTrue(res, "Active account quota at exact 20.0% threshold must trigger proactive rotation")
        self.engine_mock.switch_tokens.assert_called_once_with(
            "standby-acc", restart_server=True, force=True
        )

    def test_proactive_rotation_edge_above_threshold_suppresses_rotation(self):
        """Active account quota strictly above threshold (e.g. 20.1% > 20.0%) suppresses proactive rotation."""
        lock_file, transcript_path = self._setup_session("session-proactive-above")
        transcript_path.write_text(
            json.dumps({"type": "MESSAGE", "status": "DONE", "content": "Done."}) + "\n",
            encoding="utf-8",
        )

        quota_cache_file = self.cache_dir / "last_known_quota.json"
        quota_cache_file.write_text(
            json.dumps({
                "accounts": {
                    "primary-acc": {"gemini": {"fraction": 0.201}},
                    "standby-acc": {"gemini": {"fraction": 0.85}},
                }
            }),
            encoding="utf-8",
        )

        self.engine_mock.get_quota_threshold.return_value = 0.20

        watcher = LogWatcher(
            engine=self.engine_mock,
            presence_detector=self.detector,
            supervisor=self.supervisor,
            log_dirs=[],
        )

        res = watcher.poll_once()
        self.assertFalse(res, "Quota above threshold must not trigger proactive rotation")
        self.engine_mock.switch_tokens.assert_not_called()

    def test_proactive_rotation_defaults_threshold_on_invalid_engine_threshold(self):
        """Proactive rotation defaults threshold to 20% when engine threshold raises an exception or is unparseable."""
        lock_file, transcript_path = self._setup_session("session-proactive-default-thresh")
        transcript_path.write_text(
            json.dumps({"type": "MESSAGE", "status": "DONE", "content": "Done."}) + "\n",
            encoding="utf-8",
        )

        quota_cache_file = self.cache_dir / "last_known_quota.json"
        quota_cache_file.write_text(
            json.dumps({
                "accounts": {
                    "primary-acc": {"gemini": {"fraction": 0.14}},
                    "standby-acc": {"gemini": {"fraction": 0.85}},
                }
            }),
            encoding="utf-8",
        )

        # get_quota_threshold raises an exception
        self.engine_mock.get_quota_threshold.side_effect = ValueError("Invalid threshold config")

        watcher = LogWatcher(
            engine=self.engine_mock,
            presence_detector=self.detector,
            supervisor=self.supervisor,
            log_dirs=[],
        )

        res = watcher.poll_once()
        self.assertTrue(res, "Invalid engine threshold method must default to 20% and trigger rotation")
        self.engine_mock.switch_tokens.assert_called_once_with(
            "standby-acc", restart_server=True, force=True
        )

    def test_proactive_rotation_cursor_advancement_when_evaluated_directly(self):
        """Direct evaluation of _evaluate_proactive_rotation discovers active transcript and updates cursors."""
        lock_file, transcript_path = self._setup_session("session-proactive-direct-eval")
        transcript_path.write_text(
            json.dumps({"type": "MESSAGE", "status": "DONE", "content": "Direct step."}) + "\n",
            encoding="utf-8",
        )

        quota_cache_file = self.cache_dir / "last_known_quota.json"
        quota_cache_file.write_text(
            json.dumps({
                "accounts": {
                    "primary-acc": {"gemini": {"fraction": 0.14}},
                    "standby-acc": {"gemini": {"fraction": 0.85}},
                }
            }),
            encoding="utf-8",
        )

        watcher = LogWatcher(
            engine=self.engine_mock,
            presence_detector=self.detector,
            supervisor=self.supervisor,
            log_dirs=[],
        )

        # Clear active_transcript_path to test dynamic discovery during direct evaluation
        watcher.active_transcript_path = None
        self.assertNotIn(str(transcript_path), watcher.file_cursors)

        res = watcher._evaluate_proactive_rotation()
        self.assertTrue(res)
        transcript_key = str(transcript_path)
        self.assertIn(transcript_key, watcher.file_cursors)
        self.assertEqual(watcher.file_cursors[transcript_key], os.path.getsize(transcript_path))
        self.assertGreater(watcher.last_switch_time, 0.0)

    def test_proactive_rotation_unexpected_switch_tokens_exception_handled_cleanly(self):
        """Unexpected switch_tokens exception logs to stderr and leaves damping timer unadvanced."""
        lock_file, transcript_path = self._setup_session("session-proactive-exc")
        transcript_path.write_text(
            json.dumps({"type": "MESSAGE", "status": "DONE", "content": "Done."}) + "\n",
            encoding="utf-8",
        )

        quota_cache_file = self.cache_dir / "last_known_quota.json"
        quota_cache_file.write_text(
            json.dumps({
                "accounts": {
                    "primary-acc": {"gemini": {"fraction": 0.14}},
                    "standby-acc": {"gemini": {"fraction": 0.85}},
                }
            }),
            encoding="utf-8",
        )

        self.engine_mock.switch_tokens.side_effect = RuntimeError("Fatal disk corruption during token switch")

        watcher = LogWatcher(
            engine=self.engine_mock,
            presence_detector=self.detector,
            supervisor=self.supervisor,
            log_dirs=[],
        )

        from io import StringIO
        stderr_capture = StringIO()
        with patch("sys.stderr", stderr_capture):
            res = watcher.poll_once()

        self.assertEqual(res, 0)
        self.assertEqual(watcher.last_switch_time, 0.0)
        self.assertIn("Fatal disk corruption during token switch", stderr_capture.getvalue())

    def test_proactive_rotation_handles_accounts_data_as_list_and_objects(self):
        """Proactive rotation handles accounts_data returned as a raw list of AccountInfo-like objects."""
        lock_file, transcript_path = self._setup_session("session-proactive-obj-list")
        transcript_path.write_text(
            json.dumps({"type": "MESSAGE", "status": "DONE", "content": "Done."}) + "\n",
            encoding="utf-8",
        )

        quota_cache_file = self.cache_dir / "last_known_quota.json"
        quota_cache_file.write_text(
            json.dumps({
                "accounts": {
                    "primary-acc": {"gemini": {"fraction": 0.14}},
                    "standby-acc": {"gemini": {"fraction": 0.90}},
                }
            }),
            encoding="utf-8",
        )

        acc_primary = AccountInfo(name="primary-acc", is_active=True)
        acc_standby = AccountInfo(name="standby-acc", is_active=False)
        self.engine_mock.list_accounts.return_value = [acc_primary, acc_standby]

        watcher = LogWatcher(
            engine=self.engine_mock,
            presence_detector=self.detector,
            supervisor=self.supervisor,
            log_dirs=[],
        )

        res = watcher.poll_once()
        self.assertTrue(res)
        self.engine_mock.switch_tokens.assert_called_once_with(
            "standby-acc", restart_server=True, force=True
        )


class TestStandbyQualificationAndTelemetryGuard(unittest.TestCase):
    """Tests for standby account qualification and telemetry guards during proactive rotation.

    Covers:
    - [WATCH-PROACTIVE-004]: Standby account qualification requires healthy quota threshold of 70% or higher.
    - [WATCH-PROACTIVE-ERR-001]: Telemetry fetch failure during proactive quota check retains active account without false rotation.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.gemini_dir = Path(self.tmpdir) / ".gemini"
        self.proc_dir = Path(self.tmpdir) / "proc"
        self.vault_dir = Path(self.tmpdir) / "vault"
        self.cache_dir = self.vault_dir / "cache"

        self.gemini_dir.mkdir(parents=True, exist_ok=True)
        self.proc_dir.mkdir(parents=True, exist_ok=True)
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.engine_mock = MagicMock()
        self.engine_mock.vault_dir = self.vault_dir
        self.engine_mock.cache_dir = self.cache_dir
        self.engine_mock.gemini_dir = self.gemini_dir
        self.engine_mock.get_quota_threshold.return_value = 0.20
        self.engine_mock.get_active_name.return_value = "primary-acc"
        self.engine_mock.list_accounts.return_value = {
            "active": "primary-acc",
            "accounts": [
                {"name": "primary-acc", "valid": True},
                {"name": "standby-1", "valid": True},
                {"name": "standby-2", "valid": True},
            ],
        }
        self.engine_mock.switch_tokens.return_value = {
            "active": "standby-1",
            "success": True,
            "server_restarted": True,
        }

        self.detector = PresenceDetector(gemini_dir=self.gemini_dir, proc_dir=self.proc_dir)
        self.supervisor = ProcessSupervisor(gemini_dir=self.gemini_dir, proc_dir=self.proc_dir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _setup_session(self, session_id: str = "session-standby-qual") -> tuple[Path, Path]:
        presence_dir = self.gemini_dir / "antigravity" / "presence"
        presence_dir.mkdir(parents=True, exist_ok=True)
        lock_file = presence_dir / f"{session_id}.lock"
        lock_file.touch()

        logs_dir = self.gemini_dir / "antigravity" / "brain" / session_id / ".system_generated" / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = logs_dir / "transcript.jsonl"
        return lock_file, transcript_path

    def test_standby_qualification_requires_healthy_quota_threshold_004(self):
        """[WATCH-PROACTIVE-004]: GIVEN active account 'primary-acc' has remaining quota of 14%
        AND standby account 'standby-1' has only 40% quota remaining (0.40 < 0.70)
        AND standby account 'standby-2' has 10% quota remaining (0.10 < 0.70)
        WHEN LogWatcher.poll_once() searches for eligible proactive rotation targets
        THEN it evaluates that no standby account meets the 70% healthy standby threshold
        AND suppresses proactive rotation, retaining 'primary-acc' until actual exhaustion occurs
        or healthy standby quota becomes available.
        """
        lock_file, transcript_path = self._setup_session("session-qual-004")
        record = {
            "type": "MESSAGE",
            "status": "DONE",
            "content": "Assistant finished response.",
        }
        transcript_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

        quota_cache_file = self.cache_dir / "last_known_quota.json"
        quota_cache_file.write_text(
            json.dumps({
                "accounts": {
                    "primary-acc": {"gemini": {"fraction": 0.14}},
                    "standby-1": {"gemini": {"fraction": 0.40}},
                    "standby-2": {"gemini": {"fraction": 0.10}},
                }
            }),
            encoding="utf-8",
        )

        watcher = LogWatcher(
            engine=self.engine_mock,
            presence_detector=self.detector,
            supervisor=self.supervisor,
            log_dirs=[],
        )

        res = watcher.poll_once()
        self.assertEqual(res, 0, "Proactive rotation must be suppressed when no standby meets 70% threshold")
        self.engine_mock.switch_tokens.assert_not_called()
        self.assertEqual(watcher.last_switch_time, 0.0)

        # Directly verify _probe_standby_candidates seam
        candidates = watcher._probe_standby_candidates(
            "primary-acc",
            {"primary-acc": {"fraction": 0.14}, "standby-1": {"fraction": 0.40}, "standby-2": {"fraction": 0.10}},
        )
        self.assertEqual(candidates, [], "Candidates must be empty when all standbys have < 70% quota")

    def test_standby_qualification_telemetry_fetch_failure_err_001(self):
        """[WATCH-PROACTIVE-ERR-001]: GIVEN LogWatcher.poll_once() attempts to evaluate active account quota
        AND last_known_quota.json is missing and live CloudCode telemetry raises urllib.error.URLError
        WHEN LogWatcher evaluates proactive rotation conditions
        THEN it catches the network exception gracefully without crashing the daemon
        AND suppresses proactive rotation, retaining the active account until valid telemetry is retrieved.
        """
        lock_file, transcript_path = self._setup_session("session-err-001")
        record = {
            "type": "MESSAGE",
            "status": "DONE",
            "content": "Turn execution finished.",
        }
        transcript_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

        quota_cache_file = self.cache_dir / "last_known_quota.json"
        if quota_cache_file.exists():
            quota_cache_file.unlink()

        watcher = LogWatcher(
            engine=self.engine_mock,
            presence_detector=self.detector,
            supervisor=self.supervisor,
            log_dirs=[],
        )

        from io import StringIO
        stderr_capture = StringIO()
        with patch("scripts.watch.fetch_account_quota", side_effect=urllib.error.URLError("Network unreachable")):
            with patch("sys.stderr", stderr_capture):
                res = watcher.poll_once()

        self.assertEqual(res, 0, "Daemon must catch URLError gracefully and return 0 (suppressing rotation)")
        self.engine_mock.switch_tokens.assert_not_called()
        self.assertEqual(watcher.last_switch_time, 0.0)

    def test_standby_qualification_qualifying_standby_selected_successfully(self):
        """Standby account with >= 70% quota qualifies and is selected over depleted accounts."""
        lock_file, transcript_path = self._setup_session("session-qual-success")
        record = {
            "type": "MESSAGE",
            "status": "DONE",
            "content": "Turn execution completed.",
        }
        transcript_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

        quota_cache_file = self.cache_dir / "last_known_quota.json"
        quota_cache_file.write_text(
            json.dumps({
                "accounts": {
                    "primary-acc": {"gemini": {"fraction": 0.14}},
                    "standby-1": {"gemini": {"fraction": 0.40}},
                    "standby-2": {"gemini": {"fraction": 0.85}},
                }
            }),
            encoding="utf-8",
        )

        watcher = LogWatcher(
            engine=self.engine_mock,
            presence_detector=self.detector,
            supervisor=self.supervisor,
            log_dirs=[],
        )

        # Directly verify _probe_standby_candidates seam returns highest qualified candidate first
        candidates = watcher._probe_standby_candidates(
            "primary-acc",
            {
                "primary-acc": {"gemini": {"fraction": 0.14}},
                "standby-1": {"gemini": {"fraction": 0.40}},
                "standby-2": {"gemini": {"fraction": 0.85}},
            },
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0][0], "standby-2")
        self.assertAlmostEqual(candidates[0][1], 0.85)

        res = watcher.poll_once()
        self.assertTrue(res)
        self.engine_mock.switch_tokens.assert_called_once_with(
            "standby-2", restart_server=True, force=True
        )

    def test_standby_qualification_live_telemetry_fallback_when_cache_missing(self):
        """Live CloudCode telemetry fallback resolves active and standby quotas when cache is missing."""
        lock_file, transcript_path = self._setup_session("session-live-fallback")
        record = {
            "type": "MESSAGE",
            "status": "DONE",
            "content": "Turn execution completed.",
        }
        transcript_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

        quota_cache_file = self.cache_dir / "last_known_quota.json"
        if quota_cache_file.exists():
            quota_cache_file.unlink()

        def fake_fetch_account_quota(engine, acc_name, client=None):
            if acc_name == "primary-acc":
                return acc_name, {"gemini": {"fraction": 0.14}}
            elif acc_name == "standby-1":
                return acc_name, {"gemini": {"fraction": 0.40}}
            elif acc_name == "standby-2":
                return acc_name, {"gemini": {"fraction": 0.85}}
            return acc_name, {"error": "unknown account"}

        watcher = LogWatcher(
            engine=self.engine_mock,
            presence_detector=self.detector,
            supervisor=self.supervisor,
            log_dirs=[],
        )

        with patch("scripts.watch.fetch_account_quota", side_effect=fake_fetch_account_quota):
            res = watcher.poll_once()

        self.assertTrue(res)
        self.engine_mock.switch_tokens.assert_called_once_with(
            "standby-2", restart_server=True, force=True
        )


class TestDrainWindowPreservationAndFloorOverride(unittest.TestCase):
    """Tests for drain window preservation and 5% hard floor override during proactive rotation.

    Covers:
    - [WATCH-PROACTIVE-003]: Drain window preservation retains active account when reset in <= 30m and quota > 5%.
    - [WATCH-PROACTIVE-EDGE-002]: Active quota at or below 5% inside drain window overrides retention and forces rotation.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.gemini_dir = Path(self.tmpdir) / ".gemini"
        self.proc_dir = Path(self.tmpdir) / "proc"
        self.vault_dir = Path(self.tmpdir) / "vault"
        self.cache_dir = self.vault_dir / "cache"

        self.gemini_dir.mkdir(parents=True, exist_ok=True)
        self.proc_dir.mkdir(parents=True, exist_ok=True)
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.engine_mock = MagicMock()
        self.engine_mock.vault_dir = self.vault_dir
        self.engine_mock.cache_dir = self.cache_dir
        self.engine_mock.gemini_dir = self.gemini_dir
        self.engine_mock.get_quota_threshold.return_value = 0.20
        self.engine_mock.get_active_name.return_value = "primary-acc"
        self.engine_mock.list_accounts.return_value = {
            "active": "primary-acc",
            "accounts": [
                {"name": "primary-acc", "valid": True},
                {"name": "standby-acc", "valid": True},
            ],
        }
        self.engine_mock.switch_tokens.return_value = {
            "active": "standby-acc",
            "success": True,
            "server_restarted": True,
        }

        self.detector = PresenceDetector(gemini_dir=self.gemini_dir, proc_dir=self.proc_dir)
        self.supervisor = ProcessSupervisor(gemini_dir=self.gemini_dir, proc_dir=self.proc_dir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _setup_session(self, session_id: str = "session-drain-window") -> tuple[Path, Path]:
        presence_dir = self.gemini_dir / "antigravity" / "presence"
        presence_dir.mkdir(parents=True, exist_ok=True)
        lock_file = presence_dir / f"{session_id}.lock"
        lock_file.touch()

        logs_dir = self.gemini_dir / "antigravity" / "brain" / session_id / ".system_generated" / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = logs_dir / "transcript.jsonl"
        return lock_file, transcript_path

    def test_drain_window_preservation_retains_active_account_watch_proactive_003(self):
        """[WATCH-PROACTIVE-003]: GIVEN active account 'primary-acc' has remaining quota of 12% (0.12 <= 0.20)
        AND the quota reset timestamp is in 18 minutes (reset_delta = 18.0 <= 30.0)
        AND the remaining balance exceeds the absolute floor of 5% (0.12 > 0.05)
        WHEN LogWatcher.poll_once() evaluates proactive rotation conditions
        THEN it applies the drain window preservation heuristic
        AND suppresses rotation, retaining 'primary-acc' as active to utilize remaining quota before reset.
        """
        lock_file, transcript_path = self._setup_session("session-drain-003")
        record = {
            "type": "MESSAGE",
            "status": "DONE",
            "content": "Step complete.",
        }
        transcript_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

        now = 1700000000.0
        quota_cache_file = self.cache_dir / "last_known_quota.json"
        quota_cache_file.write_text(
            json.dumps({
                "accounts": {
                    "primary-acc": {
                        "gemini": {
                            "fraction": 0.12,
                            "reset_time": now + (18.0 * 60.0),
                        }
                    },
                    "standby-acc": {"gemini": {"fraction": 0.95}},
                }
            }),
            encoding="utf-8",
        )

        watcher = LogWatcher(
            engine=self.engine_mock,
            presence_detector=self.detector,
            supervisor=self.supervisor,
            log_dirs=[],
        )

        with patch("time.time", return_value=now):
            res = watcher.poll_once()

        self.assertEqual(res, 0, "Drain window must suppress rotation when fraction > 0.05 and reset in <= 30m")
        self.engine_mock.switch_tokens.assert_not_called()

    def test_drain_window_hard_floor_override_forces_rotation_watch_proactive_edge_002(self):
        """[WATCH-PROACTIVE-EDGE-002]: GIVEN active account 'primary-acc' has 4% Gemini quota remaining (0.04 <= 0.05)
        AND quota reset is scheduled in 10 minutes (reset_delta = 10.0 <= 30.0)
        AND standby account 'standby-acc' has 95% quota
        AND the system is at a verified Safe Turn Boundary
        WHEN LogWatcher.poll_once() evaluates proactive rotation conditions
        THEN it evaluates that quota is at or below the 5% hard floor (0.04 <= 0.05)
        AND overrides the drain window retention, rotating immediately to 'standby-acc' to prevent impending 429 exhaustion.
        """
        lock_file, transcript_path = self._setup_session("session-drain-edge-002")
        record = {
            "type": "MESSAGE",
            "status": "DONE",
            "content": "Step complete.",
        }
        transcript_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

        now = 1700000000.0
        quota_cache_file = self.cache_dir / "last_known_quota.json"
        quota_cache_file.write_text(
            json.dumps({
                "accounts": {
                    "primary-acc": {
                        "gemini": {
                            "fraction": 0.04,
                            "reset_time": now + (10.0 * 60.0),
                        }
                    },
                    "standby-acc": {"gemini": {"fraction": 0.95}},
                }
            }),
            encoding="utf-8",
        )

        watcher = LogWatcher(
            engine=self.engine_mock,
            presence_detector=self.detector,
            supervisor=self.supervisor,
            log_dirs=[],
        )

        with patch("time.time", return_value=now):
            res = watcher.poll_once()

        self.assertTrue(res, "Hard floor <= 5% must override drain window and force rotation")
        self.engine_mock.switch_tokens.assert_called_once_with(
            "standby-acc", restart_server=True, force=True
        )

    def test_drain_window_exact_5_percent_hard_floor_override(self):
        """Active account at exact 5.0% quota floor overrides drain window and rotates immediately."""
        lock_file, transcript_path = self._setup_session("session-drain-exact-floor")
        record = {
            "type": "MESSAGE",
            "status": "DONE",
            "content": "Step complete.",
        }
        transcript_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

        now = 1700000000.0
        quota_cache_file = self.cache_dir / "last_known_quota.json"
        quota_cache_file.write_text(
            json.dumps({
                "accounts": {
                    "primary-acc": {
                        "gemini": {
                            "fraction": 0.05,
                            "reset_time": now + (10.0 * 60.0),
                        }
                    },
                    "standby-acc": {"gemini": {"fraction": 0.95}},
                }
            }),
            encoding="utf-8",
        )

        watcher = LogWatcher(
            engine=self.engine_mock,
            presence_detector=self.detector,
            supervisor=self.supervisor,
            log_dirs=[],
        )

        with patch("time.time", return_value=now):
            res = watcher.poll_once()

        self.assertTrue(res, "Exact 5% floor must override drain window and rotate")
        self.engine_mock.switch_tokens.assert_called_once_with(
            "standby-acc", restart_server=True, force=True
        )

    def test_drain_window_outside_30m_rotates_normally(self):
        """Active account with 12% quota but reset scheduled in 35 minutes (> 30m) rotates normally."""
        lock_file, transcript_path = self._setup_session("session-drain-outside-window")
        record = {
            "type": "MESSAGE",
            "status": "DONE",
            "content": "Step complete.",
        }
        transcript_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

        now = 1700000000.0
        quota_cache_file = self.cache_dir / "last_known_quota.json"
        quota_cache_file.write_text(
            json.dumps({
                "accounts": {
                    "primary-acc": {
                        "gemini": {
                            "fraction": 0.12,
                            "reset_time": now + (35.0 * 60.0),
                        }
                    },
                    "standby-acc": {"gemini": {"fraction": 0.95}},
                }
            }),
            encoding="utf-8",
        )

        watcher = LogWatcher(
            engine=self.engine_mock,
            presence_detector=self.detector,
            supervisor=self.supervisor,
            log_dirs=[],
        )

        with patch("time.time", return_value=now):
            res = watcher.poll_once()

        self.assertTrue(res, "Reset outside 30m window does not qualify for drain window; rotates normally")
        self.engine_mock.switch_tokens.assert_called_once_with(
            "standby-acc", restart_server=True, force=True
        )


class TestThresholdSyncAndCooldownResilience(unittest.TestCase):
    """Tests for Configurable Quota Threshold Synchronization and Cooldown Resilience.

    Scenarios:
    - [PRE-PROBE-SYNC-001]: GIVEN vault configuration has quota_threshold set to 0.20 (20%)
      AND active account has 22% remaining quota AND a reactive quota error pattern is captured in the transcript
      WHEN LogWatcher.handle_exhaustion() runs the Pre-Switch Quota Probe
      THEN it retrieves the configured threshold 0.20 via AgySwitchEngine.get_quota_threshold()
      AND evaluates that 0.22 > 0.20 constitutes healthy quota
      AND suppresses rotation, logging a transient RPC anomaly.
    - [COOLDOWN-ERR-001]: GIVEN cooldown.json is corrupted with malformed JSON syntax or truncated bytes
      WHEN LogWatcher.get_cooldowns() reads the cooldown file
      THEN it handles json.JSONDecodeError safely, treating the cooldown mapping as empty
      AND allows rotation to proceed among healthy accounts without crashing.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.vault_dir = Path(self.tmpdir) / "vault"
        self.cache_dir = self.vault_dir / "cache"
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.engine_mock = MagicMock()
        self.engine_mock.vault_dir = self.vault_dir
        self.engine_mock.cache_dir = self.cache_dir
        self.engine_mock.get_active_name.return_value = "primary-acc"
        self.engine_mock.list_accounts.return_value = {
            "active": "primary-acc",
            "accounts": [
                {"name": "primary-acc", "valid": True},
                {"name": "standby-acc", "valid": True},
            ],
        }
        self.engine_mock.switch_tokens.return_value = {
            "active": "standby-acc",
            "success": True,
            "server_restarted": True,
        }

        self.watcher = LogWatcher(
            engine=self.engine_mock,
            log_dirs=[],
            cooldown_minutes=60,
        )

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @patch("scripts.watch.time.sleep", return_value=None)
    def test_threshold_sync_pre_probe_sync_001_configurable_threshold_suppresses_rotation(self, mock_sleep):
        """[PRE-PROBE-SYNC-001]: LogWatcher.handle_exhaustion retrieves configured threshold from engine
        and suppresses rotation when active quota (22%) exceeds configured threshold (20%).
        """
        # Configure threshold to 20% on the engine
        self.engine_mock.get_quota_threshold.return_value = 0.20

        # Set up quota cache: primary-acc at 22% (0.22 > 0.20), standby-acc at 85%
        quota_cache_file = self.cache_dir / "last_known_quota.json"
        quota_cache_file.write_text(
            json.dumps({
                "accounts": {
                    "primary-acc": {"gemini": {"fraction": 0.22}},
                    "standby-acc": {"gemini": {"fraction": 0.85}},
                }
            }),
            encoding="utf-8",
        )

        error_line = 'Error: RESOURCE_EXHAUSTED (code 429)'
        res = self.watcher.handle_exhaustion(line=error_line, model_hint="gemini")

        # Must retrieve configured threshold via engine.get_quota_threshold()
        self.engine_mock.get_quota_threshold.assert_called()

        # 0.22 > 0.20 constitutes healthy quota: rotation must be suppressed (transient RPC anomaly)
        self.assertFalse(res, "Active account quota of 22% > configured 20% threshold must suppress rotation")
        self.engine_mock.switch_tokens.assert_not_called()
        self.assertNotIn("primary-acc", self.watcher.get_cooldowns())

        # Boundary check: depleted quota below threshold (18% <= 20%) authorizes rotation
        quota_cache_file.write_text(
            json.dumps({
                "accounts": {
                    "primary-acc": {"gemini": {"fraction": 0.18}},
                    "standby-acc": {"gemini": {"fraction": 0.85}},
                }
            }),
            encoding="utf-8",
        )
        self.watcher.last_switch_time = 0.0
        res_depleted = self.watcher.handle_exhaustion(line=error_line, model_hint="gemini")
        self.assertTrue(res_depleted, "Active account quota of 18% <= configured 20% threshold must authorize rotation")
        self.engine_mock.switch_tokens.assert_called_once_with("standby-acc", restart_server=True, force=True)

    def test_threshold_sync_cooldown_err_001_malformed_json_syntax_returns_empty_and_allows_rotation(self):
        """[COOLDOWN-ERR-001]: Corrupted cooldown.json with malformed JSON syntax handles JSONDecodeError
        safely, treating cooldown mapping as empty and allowing rotation to proceed without crashing.
        """
        cooldown_file = self.watcher.cooldown_file
        cooldown_file.parent.mkdir(parents=True, exist_ok=True)
        cooldown_file.write_text("{ malformed JSON: truncated ...", encoding="utf-8")

        # Direct read via get_cooldowns() handles json.JSONDecodeError safely
        cooldowns = self.watcher.get_cooldowns()
        self.assertEqual(cooldowns, {})

        # Set up depleted active account (10%) and healthy standby (85%)
        quota_cache_file = self.cache_dir / "last_known_quota.json"
        quota_cache_file.write_text(
            json.dumps({
                "accounts": {
                    "primary-acc": {"gemini": {"fraction": 0.10}},
                    "standby-acc": {"gemini": {"fraction": 0.85}},
                }
            }),
            encoding="utf-8",
        )

        error_line = 'Error: RESOURCE_EXHAUSTED (code 429)'
        with patch("scripts.watch.time.sleep", return_value=None):
            res = self.watcher.handle_exhaustion(line=error_line, model_hint="gemini")

        self.assertTrue(res, "Corrupted cooldown file must not crash daemon and must allow rotation to healthy standby")
        self.engine_mock.switch_tokens.assert_called_once_with("standby-acc", restart_server=True, force=True)

    def test_threshold_sync_cooldown_err_001_truncated_bytes_returns_empty_and_allows_rotation(self):
        """[COOLDOWN-ERR-001]: Corrupted cooldown.json with truncated/invalid bytes handles decode errors
        safely, treating cooldown mapping as empty and allowing rotation to proceed without crashing.
        """
        cooldown_file = self.watcher.cooldown_file
        cooldown_file.parent.mkdir(parents=True, exist_ok=True)
        cooldown_file.write_bytes(b"\x80\xff\xfe truncated raw bytes")

        # Direct read via get_cooldowns() handles decode error safely
        cooldowns = self.watcher.get_cooldowns()
        self.assertEqual(cooldowns, {})

        # Rotation must proceed among healthy accounts without crashing
        quota_cache_file = self.cache_dir / "last_known_quota.json"
        quota_cache_file.write_text(
            json.dumps({
                "accounts": {
                    "primary-acc": {"gemini": {"fraction": 0.10}},
                    "standby-acc": {"gemini": {"fraction": 0.85}},
                }
            }),
            encoding="utf-8",
        )

        error_line = 'Error: RESOURCE_EXHAUSTED (code 429)'
        with patch("scripts.watch.time.sleep", return_value=None):
            res = self.watcher.handle_exhaustion(line=error_line, model_hint="gemini")

        self.assertTrue(res, "Truncated bytes in cooldown.json must not crash daemon and must allow rotation to healthy standby")
        self.engine_mock.switch_tokens.assert_called_once_with("standby-acc", restart_server=True, force=True)


class TestUnifiedProactiveReactiveDamping(unittest.TestCase):
    """Tests for unified proactive and reactive 60-second damping state in LogWatcher.

    Scenarios:
    - [PRE-PROBE-SYNC-002]: Proactive rotation sets unified last_switch_time and suppresses
      subsequent reactive quota exhaustion errors within damping_seconds (60.0s).
    - [WATCH-PROACTIVE-EDGE-003]: Proactive rotation enforces 60-second damping interval against
      successive proactive rotations until elapsed >= 60.0s.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.gemini_dir = Path(self.tmpdir) / ".gemini"
        self.proc_dir = Path(self.tmpdir) / "proc"
        self.vault_dir = Path(self.tmpdir) / "vault"
        self.cache_dir = self.vault_dir / "cache"

        self.gemini_dir.mkdir(parents=True, exist_ok=True)
        self.proc_dir.mkdir(parents=True, exist_ok=True)
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.engine_mock = MagicMock()
        self.engine_mock.vault_dir = self.vault_dir
        self.engine_mock.cache_dir = self.cache_dir
        self.engine_mock.gemini_dir = self.gemini_dir
        self.engine_mock.get_quota_threshold.return_value = 0.20
        self.engine_mock.get_active_name.return_value = "primary-acc"
        self.engine_mock.list_accounts.return_value = {
            "active": "primary-acc",
            "accounts": [
                {"name": "primary-acc", "valid": True},
                {"name": "standby-acc", "valid": True},
                {"name": "standby-2", "valid": True},
            ],
        }
        self.engine_mock.switch_tokens.return_value = {
            "active": "standby-acc",
            "success": True,
            "server_restarted": True,
        }

        self.detector = PresenceDetector(gemini_dir=self.gemini_dir, proc_dir=self.proc_dir)
        self.supervisor = ProcessSupervisor(gemini_dir=self.gemini_dir, proc_dir=self.proc_dir)

    def _setup_session(self, session_id: str) -> tuple[Path, Path]:
        presence_dir = self.gemini_dir / "antigravity" / "presence"
        presence_dir.mkdir(parents=True, exist_ok=True)
        lock_file = presence_dir / f"{session_id}.lock"
        lock_file.touch()

        logs_dir = self.gemini_dir / "antigravity" / "brain" / session_id / ".system_generated" / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = logs_dir / "transcript.jsonl"
        return lock_file, transcript_path

    @patch("scripts.watch.time.sleep", return_value=None)
    def test_unified_damping_pre_probe_sync_002_proactive_suppresses_reactive_exhaustion(self, mock_sleep):
        """[PRE-PROBE-SYNC-002]: Proactive rotation sets unified last_switch_time and suppresses
        reactive exhaustion failover in handle_exhaustion() and poll_once() for elapsed < 60.0s.
        """
        from contextlib import redirect_stderr
        from io import StringIO

        _, transcript_path = self._setup_session("session-unified-damping-002")
        transcript_path.write_text(
            json.dumps({"type": "MESSAGE", "status": "DONE", "content": "Turn complete."}) + "\n",
            encoding="utf-8",
        )

        quota_cache_file = self.cache_dir / "last_known_quota.json"
        quota_cache_file.write_text(
            json.dumps({
                "accounts": {
                    "primary-acc": {"gemini": {"fraction": 0.14}},
                    "standby-acc": {"gemini": {"fraction": 0.92}},
                    "standby-2": {"gemini": {"fraction": 0.85}},
                }
            }),
            encoding="utf-8",
        )

        watcher = LogWatcher(
            engine=self.engine_mock,
            presence_detector=self.detector,
            supervisor=self.supervisor,
            log_dirs=[],
            cooldown_minutes=60,
        )
        self.assertEqual(watcher.damping_seconds, 60.0)

        t0 = 1700000000.0
        with patch("time.time", return_value=t0):
            proactive_switches = watcher.poll_once()

        self.assertEqual(proactive_switches, 1, "Initial proactive rotation at Safe Turn Boundary must succeed")
        self.assertEqual(watcher.last_switch_time, t0, "Proactive rotation must update unified last_switch_time")
        self.engine_mock.switch_tokens.assert_called_once_with("standby-acc", restart_server=True, force=True)

        # Simulate active account is now standby-acc, with depleted quota so failover would fire if undamped
        self.engine_mock.get_active_name.return_value = "standby-acc"
        quota_cache_file.write_text(
            json.dumps({
                "accounts": {
                    "primary-acc": {"gemini": {"fraction": 0.14}},
                    "standby-acc": {"gemini": {"fraction": 0.10}},
                    "standby-2": {"gemini": {"fraction": 0.90}},
                }
            }),
            encoding="utf-8",
        )

        # Reactive error from an earlier in-flight request arrives in transcript.jsonl at elapsed = 30.0s
        error_record = {"type": "ERROR", "status": "ERROR", "error": "RESOURCE_EXHAUSTED (code 429)"}
        with open(transcript_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(error_record) + "\n")

        t_30 = t0 + 30.0
        with patch("time.time", return_value=t_30):
            poll_switches = watcher.poll_once()
        self.assertEqual(poll_switches, 0, "poll_once() must suppress reactive error burst at elapsed=30.0s < 60.0s")
        self.assertEqual(self.engine_mock.switch_tokens.call_count, 1)

        # Direct handle_exhaustion() call at elapsed = 30.0s must also enforce unified 60s damping
        stderr_buf = StringIO()
        with patch("time.time", return_value=t_30), redirect_stderr(stderr_buf):
            res_damped = watcher.handle_exhaustion(line="Error: RESOURCE_EXHAUSTED (code 429)", model_hint="gemini")

        self.assertFalse(res_damped, "handle_exhaustion() must suppress rotation when elapsed=30.0s < 60.0s")
        self.assertIn("Damping window active (30.0s < 60.0s)", stderr_buf.getvalue())
        self.assertEqual(self.engine_mock.switch_tokens.call_count, 1)
        self.assertNotIn("standby-acc", watcher.get_cooldowns(), "Damped reactive error must not penalize newly active account")

        # Once elapsed >= 60.0s, reactive failover is re-armed
        self.engine_mock.switch_tokens.return_value = {
            "active": "standby-2",
            "success": True,
            "server_restarted": True,
        }
        t_60 = t0 + 60.0
        with patch("time.time", return_value=t_60):
            res_allowed = watcher.handle_exhaustion(line="Error: RESOURCE_EXHAUSTED (code 429)", model_hint="gemini")

        self.assertTrue(res_allowed, "handle_exhaustion() must allow rotation once elapsed=60.0s >= 60.0s")
        self.assertEqual(self.engine_mock.switch_tokens.call_count, 2)
        self.engine_mock.switch_tokens.assert_called_with("standby-2", restart_server=True, force=True)
        self.assertEqual(watcher.last_switch_time, t_60)

    def test_unified_damping_watch_proactive_edge_003_suppresses_successive_proactive_switches(self):
        """[WATCH-PROACTIVE-EDGE-003]: Proactive rotation enforces 60-second damping interval
        when newly active account reports quota below threshold until elapsed >= 60.0s.
        """
        lock_file, transcript_path = self._setup_session("session-unified-damping-edge-003")
        transcript_path.write_text(
            json.dumps({"type": "MESSAGE", "status": "DONE", "content": "Step complete."}) + "\n",
            encoding="utf-8",
        )

        quota_cache_file = self.cache_dir / "last_known_quota.json"
        quota_cache_file.write_text(
            json.dumps({
                "accounts": {
                    "primary-acc": {"gemini": {"fraction": 0.14}},
                    "standby-acc": {"gemini": {"fraction": 0.92}},
                    "standby-2": {"gemini": {"fraction": 0.85}},
                }
            }),
            encoding="utf-8",
        )

        watcher = LogWatcher(
            engine=self.engine_mock,
            presence_detector=self.detector,
            supervisor=self.supervisor,
            log_dirs=[],
        )
        self.assertEqual(watcher.damping_seconds, 60.0)

        t0 = 1700000000.0
        with patch("time.time", return_value=t0):
            first_switches = watcher.poll_once()

        self.assertEqual(first_switches, 1)
        self.assertEqual(watcher.last_switch_time, t0)
        self.engine_mock.switch_tokens.assert_called_once_with("standby-acc", restart_server=True, force=True)

        # Newly active account 'standby-acc' reports cached quota below threshold (15% <= 20%)
        self.engine_mock.get_active_name.return_value = "standby-acc"
        quota_cache_file.write_text(
            json.dumps({
                "accounts": {
                    "primary-acc": {"gemini": {"fraction": 0.14}},
                    "standby-acc": {"gemini": {"fraction": 0.15}},
                    "standby-2": {"gemini": {"fraction": 0.90}},
                }
            }),
            encoding="utf-8",
        )

        # At elapsed = 40.0s (< 60.0s), poll_once() must suppress successive proactive rotation
        lock_file.touch()
        t_40 = t0 + 40.0
        with patch("time.time", return_value=t_40):
            switches_40 = watcher.poll_once()
        self.assertEqual(switches_40, 0, "Successive proactive switch at elapsed=40.0s (< 60.0s) must be suppressed")
        self.assertEqual(self.engine_mock.switch_tokens.call_count, 1)
        self.assertEqual(watcher.last_switch_time, t0)

        # Just before 60s boundary (elapsed = 59.9s < 60.0s) -> still suppressed
        lock_file.touch()
        t_59_9 = t0 + 59.9
        with patch("time.time", return_value=t_59_9):
            switches_59_9 = watcher.poll_once()
        self.assertEqual(switches_59_9, 0, "Successive proactive switch at elapsed=59.9s (< 60.0s) must remain suppressed")
        self.assertEqual(self.engine_mock.switch_tokens.call_count, 1)

        # At exact 60s boundary (elapsed = 60.0s >= 60.0s) -> rotation permitted
        lock_file.touch()
        self.engine_mock.switch_tokens.return_value = {
            "active": "standby-2",
            "success": True,
            "server_restarted": True,
        }
        t_60 = t0 + 60.0
        with patch("time.time", return_value=t_60):
            switches_60 = watcher.poll_once()
        self.assertEqual(switches_60, 1, "Proactive rotation must proceed once elapsed=60.0s >= 60.0s")
        self.assertEqual(self.engine_mock.switch_tokens.call_count, 2)
        self.engine_mock.switch_tokens.assert_called_with("standby-2", restart_server=True, force=True)
        self.assertEqual(watcher.last_switch_time, t_60)

    @patch("scripts.watch.time.sleep", return_value=None)
    def test_unified_damping_reactive_failover_suppresses_proactive_rotation(self, mock_sleep):
        """Unified damping symmetry: reactive handle_exhaustion() updates last_switch_time and
        suppresses subsequent proactive rotation in poll_once() until elapsed >= 60.0s.
        """
        lock_file, transcript_path = self._setup_session("session-unified-damping-sym")
        transcript_path.write_text(
            json.dumps({"type": "MESSAGE", "status": "DONE", "content": "Turn complete."}) + "\n",
            encoding="utf-8",
        )

        quota_cache_file = self.cache_dir / "last_known_quota.json"
        quota_cache_file.write_text(
            json.dumps({
                "accounts": {
                    "primary-acc": {"gemini": {"fraction": 0.05}},
                    "standby-acc": {"gemini": {"fraction": 0.15}},
                    "standby-2": {"gemini": {"fraction": 0.90}},
                }
            }),
            encoding="utf-8",
        )

        watcher = LogWatcher(
            engine=self.engine_mock,
            presence_detector=self.detector,
            supervisor=self.supervisor,
            log_dirs=[],
        )

        t0 = 1700000000.0
        self.engine_mock.switch_tokens.return_value = {
            "active": "standby-2",
            "success": True,
            "server_restarted": True,
        }
        with patch("time.time", return_value=t0):
            reactive_ok = watcher.handle_exhaustion(line="Error: RESOURCE_EXHAUSTED (code 429)", model_hint="gemini")

        self.assertTrue(reactive_ok)
        self.assertEqual(watcher.last_switch_time, t0)

        # Even if active account reports below threshold at Safe Turn Boundary, proactive rotation is damped for 60s
        self.engine_mock.get_active_name.return_value = "standby-2"
        quota_cache_file.write_text(
            json.dumps({
                "accounts": {
                    "primary-acc": {"gemini": {"fraction": 0.95}},
                    "standby-acc": {"gemini": {"fraction": 0.15}},
                    "standby-2": {"gemini": {"fraction": 0.12}},
                }
            }),
            encoding="utf-8",
        )

        lock_file.touch()
        with patch("time.time", return_value=t0 + 40.0):
            switches_damped = watcher.poll_once()
        self.assertEqual(switches_damped, 0, "Proactive rotation must be suppressed within 60s of reactive failover")
        self.assertEqual(self.engine_mock.switch_tokens.call_count, 1)

        lock_file.touch()
        self.engine_mock.switch_tokens.return_value = {
            "active": "primary-acc",
            "success": True,
            "server_restarted": True,
        }
        with patch("time.time", return_value=t0 + 60.0):
            switches_after = watcher.poll_once()
        self.assertEqual(switches_after, 1, "Proactive rotation must proceed once 60s damping after reactive failover expires")
        self.assertEqual(self.engine_mock.switch_tokens.call_count, 2)
        self.assertEqual(watcher.last_switch_time, t0 + 60.0)

    def test_unified_damping_failed_switches_do_not_update_last_switch_time(self):
        """Failed or no-op switch attempts in either proactive or reactive paths must not
        update last_switch_time or spuriously arm the 60s damping window.
        """
        _, transcript_path = self._setup_session("session-unified-damping-fail")
        transcript_path.write_text(
            json.dumps({"type": "MESSAGE", "status": "DONE", "content": "Done."}) + "\n",
            encoding="utf-8",
        )

        quota_cache_file = self.cache_dir / "last_known_quota.json"
        quota_cache_file.write_text(
            json.dumps({
                "accounts": {
                    "primary-acc": {"gemini": {"fraction": 0.10}},
                    "standby-acc": {"gemini": {"fraction": 0.92}},
                    "standby-2": {"gemini": {"fraction": 0.85}},
                }
            }),
            encoding="utf-8",
        )

        failure_payloads: list[tuple[str, object]] = [
            ("error_with_active_key", {"active": "standby-acc", "error": "ProcessSupervisor error"}),
            ("mismatched_active_account", {"active": "primary-acc"}),
            ("explicit_success_false", {"success": False, "error": "Presence lock active"}),
            ("boolean_false", False),
        ]
        for label, payload in failure_payloads:
            with self.subTest(path="proactive", payload=label):
                watcher = LogWatcher(
                    engine=self.engine_mock,
                    presence_detector=self.detector,
                    supervisor=self.supervisor,
                    log_dirs=[],
                )
                self.assertEqual(watcher.last_switch_time, 0.0)
                self.engine_mock.switch_tokens.return_value = payload
                with patch("time.time", return_value=1700000100.0):
                    self.assertEqual(watcher.poll_once(), 0)
                self.assertEqual(watcher.last_switch_time, 0.0)

            with self.subTest(path="reactive", payload=label):
                watcher = LogWatcher(
                    engine=self.engine_mock,
                    presence_detector=self.detector,
                    supervisor=self.supervisor,
                    log_dirs=[],
                )
                watcher.cooldown_manager.clear_cooldown()
                self.engine_mock.switch_tokens.return_value = payload
                with patch("time.time", return_value=1700000200.0):
                    self.assertFalse(
                        watcher.handle_exhaustion(line="Error: RESOURCE_EXHAUSTED (code 429)", model_hint="gemini")
                    )
                self.assertEqual(watcher.last_switch_time, 0.0)

        # Exception during switch_tokens also preserves last_switch_time
        watcher = LogWatcher(
            engine=self.engine_mock,
            presence_detector=self.detector,
            supervisor=self.supervisor,
            log_dirs=[],
        )
        watcher.last_switch_time = 1700000000.0
        self.engine_mock.switch_tokens.side_effect = RuntimeError("Disk I/O error")
        with patch("time.time", return_value=1700000080.0):
            self.assertEqual(watcher.poll_once(), 0)
            self.assertFalse(
                watcher.handle_exhaustion(line="Error: RESOURCE_EXHAUSTED (code 429)", model_hint="gemini")
            )
        self.assertEqual(watcher.last_switch_time, 1700000000.0)

    @patch("scripts.watch.time.sleep", return_value=None)
    def test_unified_damping_boundary_and_clock_skew_proactive_and_reactive(self, mock_sleep):
        """Verifies exact sub-millisecond damping boundary (59.999s vs 60.0s), initial 0.0
        state, and backward clock skew recovery across proactive and reactive paths.
        """
        lock_file, transcript_path = self._setup_session("session-unified-damping-bounds")
        transcript_path.write_text(
            json.dumps({"type": "MESSAGE", "status": "DONE", "content": "Done."}) + "\n",
            encoding="utf-8",
        )
        quota_cache_file = self.cache_dir / "last_known_quota.json"
        quota_cache_file.write_text(
            json.dumps({
                "accounts": {
                    "primary-acc": {"gemini": {"fraction": 0.12}},
                    "standby-acc": {"gemini": {"fraction": 0.92}},
                    "standby-2": {"gemini": {"fraction": 0.88}},
                }
            }),
            encoding="utf-8",
        )

        watcher = LogWatcher(
            engine=self.engine_mock,
            presence_detector=self.detector,
            supervisor=self.supervisor,
            log_dirs=[],
        )
        self.assertEqual(watcher.last_switch_time, 0.0)

        # Initial last_switch_time=0.0 must never be treated as damped
        t0 = 1700000000.0
        with patch("time.time", return_value=t0):
            self.assertEqual(watcher.poll_once(), 1)
        self.assertEqual(watcher.last_switch_time, t0)

        # At elapsed = 59.999s (< 60.0s), both proactive poll_once() and reactive handle_exhaustion() remain suppressed
        self.engine_mock.get_active_name.return_value = "standby-acc"
        quota_cache_file.write_text(
            json.dumps({
                "accounts": {
                    "primary-acc": {"gemini": {"fraction": 0.12}},
                    "standby-acc": {"gemini": {"fraction": 0.10}},
                    "standby-2": {"gemini": {"fraction": 0.88}},
                }
            }),
            encoding="utf-8",
        )
        lock_file.touch()
        with patch("time.time", return_value=t0 + 59.999):
            self.assertEqual(watcher.poll_once(), 0)
            self.assertFalse(
                watcher.handle_exhaustion(line="Error: RESOURCE_EXHAUSTED (code 429)", model_hint="gemini")
            )
        self.assertEqual(self.engine_mock.switch_tokens.call_count, 1)

        # At exact 60.0s boundary, rotation succeeds
        self.engine_mock.switch_tokens.return_value = {
            "active": "standby-2",
            "success": True,
            "server_restarted": True,
        }
        lock_file.touch()
        with patch("time.time", return_value=t0 + 60.0):
            self.assertEqual(watcher.poll_once(), 1)
        self.assertEqual(self.engine_mock.switch_tokens.call_count, 2)
        self.assertEqual(watcher.last_switch_time, t0 + 60.0)

        # Backward clock skew (now < last_switch_time) resets last_switch_time and permits proactive rotation
        self.engine_mock.get_active_name.return_value = "standby-2"
        quota_cache_file.write_text(
            json.dumps({
                "accounts": {
                    "primary-acc": {"gemini": {"fraction": 0.95}},
                    "standby-acc": {"gemini": {"fraction": 0.10}},
                    "standby-2": {"gemini": {"fraction": 0.11}},
                }
            }),
            encoding="utf-8",
        )
        self.engine_mock.switch_tokens.return_value = {
            "active": "primary-acc",
            "success": True,
            "server_restarted": True,
        }
        lock_file.touch()
        skewed_now = t0 - 500.0
        with patch("time.time", return_value=skewed_now):
            self.assertEqual(watcher.poll_once(), 1)
        self.assertEqual(watcher.last_switch_time, skewed_now)


class TestDaemonLockMutualExclusion(unittest.TestCase):
    """
    [DAEMON-MUTEX-001] [DAEMON-MUTEX-002]
    Validates POSIX kernel file locking on watch.lock across DaemonManager and LogWatcher.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.vault_dir = Path(self.tmpdir)
        self.lock_file = self.vault_dir / "watch.lock"
        self.pid_file = self.vault_dir / "watch.pid"
        self.engine_mock = MagicMock()
        self.engine_mock.vault_dir = self.vault_dir
        self.open_fds: list[int] = []

    def tearDown(self) -> None:
        for fd in self.open_fds:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _hold_external_lock(self) -> int:
        self.vault_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(str(self.lock_file), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.open_fds.append(fd)
        return fd

    def test_DAEMON_MUTEX_001_daemon_manager_start_acquires_exclusive_lock(self) -> None:
        dm = DaemonManager(vault_dir=self.vault_dir)
        mock_proc = MagicMock(pid=23456)
        with patch("subprocess.Popen", return_value=mock_proc):
            pid = dm.start()
            self.assertEqual(pid, 23456)

        self.assertIsNotNone(dm._lock_fd)
        self.open_fds.append(dm._lock_fd)  # type: ignore[arg-type]
        self.assertTrue(self.lock_file.exists())
        self.assertEqual(self.lock_file.stat().st_mode & 0o777, 0o600)

        # Attempting secondary acquisition on another descriptor must raise BlockingIOError
        probe_fd = os.open(str(self.lock_file), os.O_RDWR)
        self.open_fds.append(probe_fd)
        with self.assertRaises(BlockingIOError):
            fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_DAEMON_MUTEX_001_log_watcher_watch_loop_acquires_exclusive_lock(self) -> None:
        watcher = LogWatcher(engine=self.engine_mock, log_dirs=[self.tmpdir])
        watcher.watch_loop(poll_interval=0.0, max_iterations=1)

        self.assertTrue(self.lock_file.exists())
        self.assertEqual(self.lock_file.stat().st_mode & 0o777, 0o600)

        # Confirm DaemonManager._acquire_lock retained or lock is held during execution
        dm = DaemonManager(vault_dir=self.vault_dir)
        fd = dm._acquire_lock()
        self.assertIsInstance(fd, int)
        self.open_fds.append(fd)
        self.assertEqual(dm._lock_fd, fd)

        # Verify exclusive locking invariant
        probe_fd = os.open(str(self.lock_file), os.O_RDWR)
        self.open_fds.append(probe_fd)
        with self.assertRaises(BlockingIOError):
            fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_DAEMON_MUTEX_002_secondary_daemon_manager_start_rejected_with_exit_code_1(self) -> None:
        self._hold_external_lock()
        self.pid_file.write_text("11111\n", encoding="utf-8")

        dm = DaemonManager(vault_dir=self.vault_dir)
        stderr_capture = io.StringIO()
        with patch("sys.stderr", stderr_capture), patch("subprocess.Popen") as mock_popen:
            with self.assertRaises(SystemExit) as ctx:
                dm.start()
            self.assertEqual(ctx.exception.code, 1)
            mock_popen.assert_not_called()

        self.assertIn("Another watcher daemon is already running", stderr_capture.getvalue())
        # Verify watch.pid was not corrupted or overwritten
        self.assertEqual(self.pid_file.read_text(encoding="utf-8").strip(), "11111")

    def test_DAEMON_MUTEX_002_secondary_watch_loop_rejected_with_exit_code_1(self) -> None:
        self._hold_external_lock()

        watcher = LogWatcher(engine=self.engine_mock, log_dirs=[self.tmpdir])
        stderr_capture = io.StringIO()
        with patch("sys.stderr", stderr_capture), \
             patch.object(watcher, "poll_once", side_effect=RuntimeError("watch_loop polled despite lock contention")):
            with self.assertRaises(SystemExit) as ctx:
                watcher.watch_loop(poll_interval=0.0, max_iterations=1)
            self.assertEqual(ctx.exception.code, 1)

        self.assertIn("Another watcher daemon is already running", stderr_capture.getvalue())

    def test_DAEMON_MUTEX_002_secondary_watch_run_cli_rejected_with_exit_code_1(self) -> None:
        self._hold_external_lock()

        stderr_capture = io.StringIO()
        with patch("sys.stderr", stderr_capture), \
             patch("agy_switcher.watcher._get_engine_class") as mock_engine_cls, \
             patch.object(LogWatcher, "poll_once", side_effect=RuntimeError("watch_loop polled despite lock contention")):
            mock_engine_cls.return_value = lambda: self.engine_mock
            with self.assertRaises(SystemExit) as ctx:
                watch_main(["run"])
            self.assertEqual(ctx.exception.code, 1)

        self.assertIn("Another watcher daemon is already running", stderr_capture.getvalue())

    def _test_all_daemon_mutex_scenarios(self) -> None:
        tests = [
            self.test_DAEMON_MUTEX_001_daemon_manager_start_acquires_exclusive_lock,
            self.test_DAEMON_MUTEX_001_log_watcher_watch_loop_acquires_exclusive_lock,
            self.test_DAEMON_MUTEX_002_secondary_daemon_manager_start_rejected_with_exit_code_1,
            self.test_DAEMON_MUTEX_002_secondary_watch_loop_rejected_with_exit_code_1,
            self.test_DAEMON_MUTEX_002_secondary_watch_run_cli_rejected_with_exit_code_1,
        ]
        for t in tests:
            self.setUp()
            try:
                t()
            finally:
                self.tearDown()


setattr(
    TestDaemonLockMutualExclusion,
    "test_DAEMON_MUTEX_001|DAEMON_MUTEX_002",
    TestDaemonLockMutualExclusion._test_all_daemon_mutex_scenarios,
)


class TestDaemonSystemdActiveDetection(unittest.TestCase):
    """
    [DAEMON-MUTEX-003] [DAEMON-EDGE-002]
    Validates user systemd service state detection and graceful fallback in DaemonManager.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.vault_dir = Path(self.tmpdir)
        self.lock_file = self.vault_dir / "watch.lock"
        self.pid_file = self.vault_dir / "watch.pid"
        self.engine_mock = MagicMock()
        self.engine_mock.vault_dir = self.vault_dir
        self.open_fds: list[int] = []

    def tearDown(self) -> None:
        for fd in self.open_fds:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_DAEMON_MUTEX_003_is_systemd_active_returns_true_when_active(self) -> None:
        dm = DaemonManager(vault_dir=self.vault_dir)
        mock_proc = MagicMock(returncode=0, stdout="active\n")
        with patch("shutil.which", return_value="/usr/bin/systemctl"), \
             patch("subprocess.run", return_value=mock_proc) as mock_run:
            self.assertTrue(dm.is_systemd_active())
            mock_run.assert_called_once_with(
                ["systemctl", "--user", "is-active", "agy-switch-watcher.service"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=5.0,
            )

    def test_DAEMON_MUTEX_003_is_systemd_active_returns_false_when_inactive(self) -> None:
        dm = DaemonManager(vault_dir=self.vault_dir)
        mock_proc = MagicMock(returncode=3, stdout="inactive\n")
        with patch("shutil.which", return_value="/usr/bin/systemctl"), \
             patch("subprocess.run", return_value=mock_proc):
            self.assertFalse(dm.is_systemd_active())

    def test_DAEMON_MUTEX_003_daemon_manager_start_rejected_when_systemd_active(self) -> None:
        dm = DaemonManager(vault_dir=self.vault_dir)
        stderr_capture = io.StringIO()
        with patch.object(DaemonManager, "is_systemd_active", return_value=True), \
             patch("sys.stderr", stderr_capture), \
             patch("subprocess.Popen") as mock_popen:
            with self.assertRaises(SystemExit) as ctx:
                dm.start()
            self.assertEqual(ctx.exception.code, 1)
            mock_popen.assert_not_called()

        stderr = stderr_capture.getvalue()
        self.assertIn("systemd", stderr.lower())
        self.assertIn("stop", stderr.lower())
        self.assertFalse(self.pid_file.exists())
        self.assertIsNone(dm._lock_fd)

    def test_DAEMON_MUTEX_003_watch_loop_rejected_when_systemd_active(self) -> None:
        watcher = LogWatcher(engine=self.engine_mock, log_dirs=[self.tmpdir])
        stderr_capture = io.StringIO()
        with patch.object(DaemonManager, "is_systemd_active", return_value=True), \
             patch("sys.stderr", stderr_capture), \
             patch.object(watcher, "poll_once", side_effect=RuntimeError("watch_loop polled despite systemd service")):
            with self.assertRaises(SystemExit) as ctx:
                watcher.watch_loop(poll_interval=0.0, max_iterations=1)
            self.assertEqual(ctx.exception.code, 1)

        stderr = stderr_capture.getvalue()
        self.assertIn("systemd", stderr.lower())
        self.assertIn("stop", stderr.lower())

    def test_DAEMON_MUTEX_003_watch_run_cli_rejected_when_systemd_active(self) -> None:
        stderr_capture = io.StringIO()
        with patch("sys.stderr", stderr_capture), \
             patch("agy_switcher.watcher._get_engine_class") as mock_engine_cls, \
             patch.object(DaemonManager, "is_systemd_active", return_value=True), \
             patch.object(LogWatcher, "poll_once", side_effect=RuntimeError("watch_loop polled despite systemd service")):
            mock_engine_cls.return_value = lambda: self.engine_mock
            with self.assertRaises(SystemExit) as ctx:
                watch_main(["run"])
            self.assertEqual(ctx.exception.code, 1)

        stderr = stderr_capture.getvalue()
        self.assertIn("systemd", stderr.lower())
        self.assertIn("stop", stderr.lower())

    def test_DAEMON_MUTEX_003_watch_start_cli_rejected_when_systemd_active(self) -> None:
        stderr_capture = io.StringIO()
        with patch("sys.stderr", stderr_capture), \
             patch("agy_switcher.watcher._get_engine_class") as mock_engine_cls, \
             patch.object(DaemonManager, "is_systemd_active", return_value=True), \
             patch("subprocess.Popen") as mock_popen:
            mock_engine_cls.return_value = lambda: self.engine_mock
            with self.assertRaises(SystemExit) as ctx:
                watch_main(["start"])
            self.assertEqual(ctx.exception.code, 1)
            mock_popen.assert_not_called()

        stderr = stderr_capture.getvalue()
        self.assertIn("systemd", stderr.lower())
        self.assertIn("stop", stderr.lower())

    def test_DAEMON_EDGE_002_missing_systemctl_binary_fallback_gracefully(self) -> None:
        dm = DaemonManager(vault_dir=self.vault_dir)
        mock_proc = MagicMock(pid=54321)
        with patch("shutil.which", return_value=None), \
             patch("subprocess.Popen", return_value=mock_proc):
            self.assertFalse(dm.is_systemd_active())
            pid = dm.start()
            self.assertEqual(pid, 54321)

        self.assertIsNotNone(dm._lock_fd)
        self.open_fds.append(dm._lock_fd)  # type: ignore[arg-type]
        self.assertTrue(self.lock_file.exists())

    def test_DAEMON_EDGE_002_systemctl_raises_filenotfound_fallback_gracefully(self) -> None:
        dm = DaemonManager(vault_dir=self.vault_dir)
        mock_proc = MagicMock(pid=65432)
        with patch("shutil.which", return_value="/usr/bin/systemctl"), \
             patch("subprocess.run", side_effect=FileNotFoundError("No such file or directory: 'systemctl'")), \
             patch("subprocess.Popen", return_value=mock_proc):
            self.assertFalse(dm.is_systemd_active())
            pid = dm.start()
            self.assertEqual(pid, 65432)

        self.assertIsNotNone(dm._lock_fd)
        self.open_fds.append(dm._lock_fd)  # type: ignore[arg-type]
        self.assertTrue(self.lock_file.exists())

    def test_DAEMON_EDGE_002_systemctl_timeout_fallback_gracefully(self) -> None:
        dm = DaemonManager(vault_dir=self.vault_dir)
        mock_proc = MagicMock(pid=76543)
        with patch("shutil.which", return_value="/usr/bin/systemctl"), \
             patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["systemctl"], timeout=5.0)), \
             patch("subprocess.Popen", return_value=mock_proc):
            self.assertFalse(dm.is_systemd_active())
            pid = dm.start()
            self.assertEqual(pid, 76543)

        self.assertIsNotNone(dm._lock_fd)
        self.open_fds.append(dm._lock_fd)  # type: ignore[arg-type]
        self.assertTrue(self.lock_file.exists())

    def test_DAEMON_EDGE_002_systemctl_raises_oserror_fallback_gracefully(self) -> None:
        dm = DaemonManager(vault_dir=self.vault_dir)
        mock_proc = MagicMock(pid=87654)
        with patch("shutil.which", return_value="/usr/bin/systemctl"), \
             patch("subprocess.run", side_effect=PermissionError("Permission denied")), \
             patch("subprocess.Popen", return_value=mock_proc):
            self.assertFalse(dm.is_systemd_active())
            pid = dm.start()
            self.assertEqual(pid, 87654)

        self.assertIsNotNone(dm._lock_fd)
        self.open_fds.append(dm._lock_fd)  # type: ignore[arg-type]
        self.assertTrue(self.lock_file.exists())

    def test_DAEMON_MUTEX_003_is_systemd_active_returns_false_when_failed(self) -> None:
        dm = DaemonManager(vault_dir=self.vault_dir)
        mock_proc = MagicMock(returncode=4, stdout="failed\n")
        with patch("shutil.which", return_value="/usr/bin/systemctl"), \
             patch("subprocess.run", return_value=mock_proc):
            self.assertFalse(dm.is_systemd_active())

    def test_DAEMON_MUTEX_003_is_systemd_active_custom_service_name(self) -> None:
        dm = DaemonManager(vault_dir=self.vault_dir)
        mock_proc = MagicMock(returncode=0, stdout="active\n")
        with patch("shutil.which", return_value="/usr/bin/systemctl"), \
             patch("subprocess.run", return_value=mock_proc) as mock_run:
            self.assertTrue(dm.is_systemd_active("custom-watcher.service"))
            mock_run.assert_called_once_with(
                ["systemctl", "--user", "is-active", "custom-watcher.service"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=5.0,
            )

    def _test_all_systemd_detection_scenarios(self) -> None:
        tests = [
            self.test_DAEMON_MUTEX_003_is_systemd_active_returns_true_when_active,
            self.test_DAEMON_MUTEX_003_is_systemd_active_returns_false_when_inactive,
            self.test_DAEMON_MUTEX_003_is_systemd_active_returns_false_when_failed,
            self.test_DAEMON_MUTEX_003_is_systemd_active_custom_service_name,
            self.test_DAEMON_MUTEX_003_daemon_manager_start_rejected_when_systemd_active,
            self.test_DAEMON_MUTEX_003_watch_loop_rejected_when_systemd_active,
            self.test_DAEMON_MUTEX_003_watch_run_cli_rejected_when_systemd_active,
            self.test_DAEMON_MUTEX_003_watch_start_cli_rejected_when_systemd_active,
            self.test_DAEMON_EDGE_002_missing_systemctl_binary_fallback_gracefully,
            self.test_DAEMON_EDGE_002_systemctl_raises_filenotfound_fallback_gracefully,
            self.test_DAEMON_EDGE_002_systemctl_timeout_fallback_gracefully,
            self.test_DAEMON_EDGE_002_systemctl_raises_oserror_fallback_gracefully,
        ]
        for t in tests:
            self.setUp()
            try:
                t()
            finally:
                self.tearDown()


setattr(
    TestDaemonSystemdActiveDetection,
    "test_DAEMON_MUTEX_003|DAEMON_EDGE_002",
    TestDaemonSystemdActiveDetection._test_all_systemd_detection_scenarios,
)


class TestDaemonLockHardeningAndSymlinkRejection(unittest.TestCase):
    """
    [DAEMON-ERR-001] [DAEMON-ERR-002]
    Validates watch.lock path hardening, permission denial handling,
    and symlink traversal rejection across DaemonManager and LogWatcher.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.vault_dir = Path(self.tmpdir) / "vault"
        self.vault_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock_file = self.vault_dir / "watch.lock"
        self.pid_file = self.vault_dir / "watch.pid"
        self.engine_mock = MagicMock()
        self.engine_mock.vault_dir = self.vault_dir
        self.open_fds: list[int] = []

    def tearDown(self) -> None:
        for fd in self.open_fds:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.chmod(self.vault_dir, 0o700)
        except OSError:
            pass
        if self.lock_file.exists() or self.lock_file.is_symlink():
            try:
                os.chmod(self.lock_file, 0o600)
            except OSError:
                pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_DAEMON_ERR_001_lock_file_permission_denied_logs_stderr_and_exits_1(self) -> None:
        """[DAEMON-ERR-001] Restrictive permissions on watch.lock logs actionable error and exits 1."""
        self.lock_file.touch(mode=0o400)
        os.chmod(self.lock_file, 0o400)

        dm = DaemonManager(vault_dir=self.vault_dir)
        stderr_capture = io.StringIO()
        with patch("sys.stderr", stderr_capture):
            with self.assertRaises(SystemExit) as ctx:
                dm.acquire_lock()
            self.assertEqual(ctx.exception.code, 1)

        err_output = stderr_capture.getvalue()
        self.assertIn("Permission denied", err_output)
        self.assertIn(str(self.lock_file), err_output)

    def test_DAEMON_ERR_001_vault_dir_permission_denied_logs_stderr_and_exits_1(self) -> None:
        """[DAEMON-ERR-001] Restrictive permissions on vault_dir logs actionable error and exits 1."""
        os.chmod(self.vault_dir, 0o500)

        dm = DaemonManager(vault_dir=self.vault_dir)
        stderr_capture = io.StringIO()
        with patch("sys.stderr", stderr_capture):
            with self.assertRaises(SystemExit) as ctx:
                dm.acquire_lock()
            self.assertEqual(ctx.exception.code, 1)

        err_output = stderr_capture.getvalue()
        self.assertIn("Permission denied", err_output)

    def test_DAEMON_ERR_001_watch_loop_permission_denied_logs_stderr_and_exits_1(self) -> None:
        """[DAEMON-ERR-001] LogWatcher.watch_loop exits cleanly with status 1 on PermissionError."""
        self.lock_file.touch(mode=0o400)
        os.chmod(self.lock_file, 0o400)

        watcher = LogWatcher(engine=self.engine_mock, log_dirs=[self.tmpdir])
        stderr_capture = io.StringIO()
        with patch("sys.stderr", stderr_capture):
            with self.assertRaises(SystemExit) as ctx:
                watcher.watch_loop(poll_interval=0.0, max_iterations=1)
            self.assertEqual(ctx.exception.code, 1)

        err_output = stderr_capture.getvalue()
        self.assertIn("Permission denied", err_output)

    def test_DAEMON_ERR_002_external_symlink_unlinked_safely_and_exits_1(self) -> None:
        """[DAEMON-ERR-002] watch.lock symlink pointing to external path is unlinked and rejected with exit 1."""
        external_dir = Path(self.tmpdir) / "external"
        external_dir.mkdir(parents=True, exist_ok=True)
        external_target = external_dir / "target.txt"
        external_target.write_text("sensitive data", encoding="utf-8")

        self.lock_file.symlink_to(external_target)
        self.assertTrue(self.lock_file.is_symlink())

        dm = DaemonManager(vault_dir=self.vault_dir)
        stderr_capture = io.StringIO()
        with patch("sys.stderr", stderr_capture):
            with self.assertRaises(SystemExit) as ctx:
                dm.acquire_lock()
            self.assertEqual(ctx.exception.code, 1)

        err_output = stderr_capture.getvalue()
        self.assertIn("symlink", err_output.lower())
        self.assertFalse(self.lock_file.is_symlink(), "Symlink must be removed")
        self.assertTrue(external_target.exists(), "External target must remain intact")
        self.assertEqual(external_target.read_text(encoding="utf-8"), "sensitive data")

    def test_DAEMON_ERR_002_dangling_symlink_unlinked_safely_and_exits_1(self) -> None:
        """[DAEMON-ERR-002] Dangling watch.lock symlink is unlinked safely and rejected with exit 1."""
        nonexistent_target = self.vault_dir / "nonexistent_target.txt"
        self.lock_file.symlink_to(nonexistent_target)
        self.assertTrue(self.lock_file.is_symlink())
        self.assertFalse(self.lock_file.exists())

        dm = DaemonManager(vault_dir=self.vault_dir)
        stderr_capture = io.StringIO()
        with patch("sys.stderr", stderr_capture):
            with self.assertRaises(SystemExit) as ctx:
                dm.acquire_lock()
            self.assertEqual(ctx.exception.code, 1)

        err_output = stderr_capture.getvalue()
        self.assertIn("symlink", err_output.lower())
        self.assertFalse(self.lock_file.is_symlink(), "Dangling symlink must be removed")

    def test_DAEMON_ERR_002_watch_loop_symlink_rejected_with_exit_code_1(self) -> None:
        """[DAEMON-ERR-002] LogWatcher.watch_loop terminates cleanly with exit code 1 when watch.lock is a symlink."""
        external_target = Path(self.tmpdir) / "other.txt"
        external_target.write_text("other data", encoding="utf-8")
        self.lock_file.symlink_to(external_target)

        watcher = LogWatcher(engine=self.engine_mock, log_dirs=[self.tmpdir])
        stderr_capture = io.StringIO()
        with patch("sys.stderr", stderr_capture):
            with self.assertRaises(SystemExit) as ctx:
                watcher.watch_loop(poll_interval=0.0, max_iterations=1)
            self.assertEqual(ctx.exception.code, 1)

        err_output = stderr_capture.getvalue()
        self.assertIn("symlink", err_output.lower())
        self.assertFalse(self.lock_file.is_symlink())

    def test_DAEMON_ERR_002_resolve_lock_path_valid_path_returned(self) -> None:
        """[DAEMON-ERR-002] _resolve_lock_path returns valid lock file path when clean."""
        dm = DaemonManager(vault_dir=self.vault_dir)
        resolved = dm._resolve_lock_path()
        self.assertEqual(resolved, self.lock_file)
        self.assertTrue(self.vault_dir.is_dir())

    def _test_all_lock_hardening_scenarios(self) -> None:
        tests = [
            self.test_DAEMON_ERR_001_lock_file_permission_denied_logs_stderr_and_exits_1,
            self.test_DAEMON_ERR_001_vault_dir_permission_denied_logs_stderr_and_exits_1,
            self.test_DAEMON_ERR_001_watch_loop_permission_denied_logs_stderr_and_exits_1,
            self.test_DAEMON_ERR_002_external_symlink_unlinked_safely_and_exits_1,
            self.test_DAEMON_ERR_002_dangling_symlink_unlinked_safely_and_exits_1,
            self.test_DAEMON_ERR_002_watch_loop_symlink_rejected_with_exit_code_1,
            self.test_DAEMON_ERR_002_resolve_lock_path_valid_path_returned,
        ]
        for t in tests:
            self.setUp()
            try:
                t()
            finally:
                self.tearDown()


setattr(
    TestDaemonLockHardeningAndSymlinkRejection,
    "test_DAEMON_ERR_001|DAEMON_ERR_002",
    TestDaemonLockHardeningAndSymlinkRejection._test_all_lock_hardening_scenarios,
)


class TestDaemonStaleLockAndConcurrentStartup(unittest.TestCase):
    """
    [DAEMON-EDGE-001] [DAEMON-EDGE-003]
    Validates stale lock recovery when previous process died unexpectedly without file corruption,
    and deterministic mutual exclusion when concurrent processes or threads invoke start() at the exact same millisecond.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.vault_dir = Path(self.tmpdir) / "vault"
        self.vault_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock_file = self.vault_dir / "watch.lock"
        self.pid_file = self.vault_dir / "watch.pid"
        self.engine_mock = MagicMock()
        self.engine_mock.vault_dir = self.vault_dir
        self.open_fds: list[int] = []
        self.active_dms: list[DaemonManager] = []

    def tearDown(self) -> None:
        for dm in self.active_dms:
            dm._release_lock()
        for fd in self.open_fds:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_DAEMON_EDGE_001_stale_lock_recovery_with_leftover_lock_file(self) -> None:
        """
        [DAEMON-EDGE-001] Stale lock recovery when previous process died unexpectedly.
        GIVEN a previous watcher daemon terminated abruptly leaving an existing watch.lock and dead PID
        AND no active process holds a kernel lock on watch.lock
        WHEN a new watcher daemon process initializes
        THEN fcntl.flock successfully acquires the exclusive non-blocking lock without deadlock or manual file deletion
        AND daemon startup proceeds normally.
        """
        self.lock_file.write_text("stale-lock-data\n", encoding="utf-8")
        os.chmod(self.lock_file, 0o600)
        self.pid_file.write_text("999999\n", encoding="utf-8")
        os.chmod(self.pid_file, 0o600)

        dm = DaemonManager(vault_dir=self.vault_dir)
        self.active_dms.append(dm)
        mock_proc = MagicMock(pid=34567)
        with patch("subprocess.Popen", return_value=mock_proc):
            pid = dm.start()
            self.assertEqual(pid, 34567)

        self.assertIsNotNone(dm._lock_fd)
        self.assertTrue(self.lock_file.exists())
        self.assertEqual(self.lock_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.pid_file.read_text(encoding="utf-8").strip(), "34567")

        # Exclusive lock is held on watch.lock: probe fd fails with BlockingIOError
        probe_fd = os.open(str(self.lock_file), os.O_RDWR)
        self.open_fds.append(probe_fd)
        with self.assertRaises(BlockingIOError):
            fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_DAEMON_EDGE_001_stale_lock_recovery_watch_loop(self) -> None:
        """
        [DAEMON-EDGE-001] Stale lock recovery in LogWatcher.watch_loop without deadlock.
        """
        self.lock_file.write_text("stale-watch-lock\n", encoding="utf-8")
        os.chmod(self.lock_file, 0o600)

        watcher = LogWatcher(engine=self.engine_mock, log_dirs=[self.tmpdir])
        watcher.watch_loop(poll_interval=0.0, max_iterations=1)

        self.assertTrue(self.lock_file.exists())
        self.assertEqual(self.lock_file.stat().st_mode & 0o777, 0o600)

        # After watch_loop completes, a new daemon can acquire the lock cleanly
        dm = DaemonManager(vault_dir=self.vault_dir)
        self.active_dms.append(dm)
        mock_proc = MagicMock(pid=56789)
        with patch("subprocess.Popen", return_value=mock_proc):
            pid = dm.start()
            self.assertEqual(pid, 56789)
        self.assertEqual(self.pid_file.read_text(encoding="utf-8").strip(), "56789")

    def test_DAEMON_EDGE_003_rapid_concurrent_startup_race_threads(self) -> None:
        """
        [DAEMON-EDGE-003] Rapid concurrent startup race condition resolves deterministically to one daemon.
        GIVEN two concurrent threads invoke DaemonManager.start() at the exact same millisecond
        WHEN both processes attempt to acquire an exclusive lock on watch.lock
        THEN exactly one process successfully acquires the lock and writes its PID
        AND the second process is rejected immediately upon lock contention and exits cleanly with exit code 1.
        """
        barrier = threading.Barrier(2)
        results: list[tuple[str, int, DaemonManager]] = []
        threads: list[threading.Thread] = []
        shared_stderr = io.StringIO()
        pid_counter = 20000
        lock = threading.Lock()

        def make_proc(*args: object, **kwargs: object) -> MagicMock:
            nonlocal pid_counter
            with lock:
                pid_counter += 1
                return MagicMock(pid=pid_counter)

        def worker(thread_idx: int) -> None:
            dm = DaemonManager(vault_dir=self.vault_dir)
            barrier.wait()
            try:
                res_pid = dm.start()
                with lock:
                    results.append(("success", res_pid, dm))
            except SystemExit as exc:
                with lock:
                    results.append(("exit", int(exc.code) if isinstance(exc.code, (int, str)) and str(exc.code).isdigit() else 1, dm))

        with patch("subprocess.Popen", side_effect=make_proc), patch("sys.stderr", shared_stderr):
            for idx in (1, 2):
                t = threading.Thread(target=worker, args=(idx,))
                threads.append(t)
                t.start()

            for t in threads:
                t.join(timeout=5.0)

        self.assertEqual(len(results), 2)
        statuses = [r[0] for r in results]
        self.assertEqual(statuses.count("success"), 1, f"Expected exactly 1 success, got: {results}")
        self.assertEqual(statuses.count("exit"), 1, f"Expected exactly 1 exit, got: {results}")

        winner = next(r for r in results if r[0] == "success")
        loser = next(r for r in results if r[0] == "exit")

        self.active_dms.append(winner[2])
        self.assertEqual(loser[1], 1, "Contending thread must exit with code 1")

        winning_pid = winner[1]
        self.assertEqual(self.pid_file.read_text(encoding="utf-8").strip(), str(winning_pid))
        self.assertIn("Another watcher daemon is already running", shared_stderr.getvalue())

    def test_DAEMON_EDGE_003_rapid_concurrent_startup_race_processes(self) -> None:
        """
        [DAEMON-EDGE-003] Rapid concurrent startup race between two fork processes.
        """
        pipe_r, pipe_w = os.pipe()
        start_r, start_w = os.pipe()
        done_r, done_w = os.pipe()

        def run_proc(assigned_pid: int) -> None:
            os.close(pipe_r)
            os.close(start_w)
            try:
                os.read(start_r, 1)
            except OSError:
                pass
            finally:
                try: os.close(start_r)
                except OSError: pass

            dm = DaemonManager(vault_dir=self.vault_dir)
            mock_proc = MagicMock(pid=assigned_pid)
            with patch("subprocess.Popen", return_value=mock_proc):
                try:
                    res = dm.start()
                    os.write(pipe_w, f"success:{res}\n".encode("utf-8"))
                    os.close(done_w)
                    try:
                        os.read(done_r, 1)
                    except OSError:
                        pass
                    finally:
                        try: os.close(done_r)
                        except OSError: pass
                    os._exit(0)
                except SystemExit as exc:
                    code = int(exc.code) if isinstance(exc.code, (int, str)) and str(exc.code).isdigit() else 1
                    os.write(pipe_w, f"exit:{code}\n".encode("utf-8"))
                    try:
                        os.write(done_w, b"x")
                    except OSError:
                        pass
                    finally:
                        try: os.close(done_w)
                        except OSError: pass
                        try: os.close(done_r)
                        except OSError: pass
                    os._exit(code)

        with patch.object(DaemonManager, "is_systemd_active", return_value=False):
            p1 = os.fork()
            if p1 == 0:
                run_proc(20001)

            p2 = os.fork()
            if p2 == 0:
                run_proc(20002)

            os.close(pipe_w)
            os.close(start_r)

            # Release both processes simultaneously
            os.close(start_w)

            _, s1 = os.waitpid(p1, 0)
            _, s2 = os.waitpid(p2, 0)

        try: os.close(done_r)
        except OSError: pass
        try: os.close(done_w)
        except OSError: pass

        with os.fdopen(pipe_r) as f:
            lines = [line.strip() for line in f.readlines()]

        self.assertEqual(len(lines), 2)
        success_lines = [l for l in lines if l.startswith("success:")]
        exit_lines = [l for l in lines if l.startswith("exit:")]
        self.assertEqual(len(success_lines), 1, f"Expected 1 success, got: {lines}")
        self.assertEqual(len(exit_lines), 1, f"Expected 1 exit, got: {lines}")
        self.assertEqual(exit_lines[0], "exit:1")

        exit_codes = sorted([os.waitstatus_to_exitcode(s1), os.waitstatus_to_exitcode(s2)])
        self.assertEqual(exit_codes, [0, 1])

        winner_pid = int(success_lines[0].split(":")[1])
        self.assertEqual(self.pid_file.read_text(encoding="utf-8").strip(), str(winner_pid))

    def test_DAEMON_EDGE_003_rapid_concurrent_startup_high_contention(self) -> None:
        """
        [DAEMON-EDGE-003] 5 concurrent threads attempting startup simultaneously.
        """
        concurrency = 5
        barrier = threading.Barrier(concurrency)
        results: list[tuple[str, int, DaemonManager]] = []
        threads: list[threading.Thread] = []
        shared_stderr = io.StringIO()
        pid_counter = 40000
        lock = threading.Lock()

        def make_proc(*args: object, **kwargs: object) -> MagicMock:
            nonlocal pid_counter
            with lock:
                pid_counter += 1
                return MagicMock(pid=pid_counter)

        def worker(thread_idx: int) -> None:
            dm = DaemonManager(vault_dir=self.vault_dir)
            barrier.wait()
            try:
                res_pid = dm.start()
                with lock:
                    results.append(("success", res_pid, dm))
            except SystemExit as exc:
                with lock:
                    results.append(("exit", int(exc.code) if isinstance(exc.code, (int, str)) and str(exc.code).isdigit() else 1, dm))

        with patch("subprocess.Popen", side_effect=make_proc), patch("sys.stderr", shared_stderr):
            for idx in range(concurrency):
                t = threading.Thread(target=worker, args=(idx,))
                threads.append(t)
                t.start()

            for t in threads:
                t.join(timeout=5.0)

        self.assertEqual(len(results), concurrency)
        statuses = [r[0] for r in results]
        self.assertEqual(statuses.count("success"), 1, f"Expected 1 winner, got: {results}")
        self.assertEqual(statuses.count("exit"), concurrency - 1, f"Expected {concurrency - 1} exits, got: {results}")

        winner = next(r for r in results if r[0] == "success")
        self.active_dms.append(winner[2])
        self.assertEqual(self.pid_file.read_text(encoding="utf-8").strip(), str(winner[1]))
        self.assertIn("Another watcher daemon is already running", shared_stderr.getvalue())

    def _test_all_stale_lock_and_concurrent_scenarios(self) -> None:
        tests = [
            self.test_DAEMON_EDGE_001_stale_lock_recovery_with_leftover_lock_file,
            self.test_DAEMON_EDGE_001_stale_lock_recovery_watch_loop,
            self.test_DAEMON_EDGE_003_rapid_concurrent_startup_race_threads,
            self.test_DAEMON_EDGE_003_rapid_concurrent_startup_race_processes,
            self.test_DAEMON_EDGE_003_rapid_concurrent_startup_high_contention,
        ]
        for t in tests:
            self.setUp()
            try:
                t()
            finally:
                self.tearDown()


setattr(
    TestDaemonStaleLockAndConcurrentStartup,
    "test_DAEMON_EDGE_001|DAEMON_EDGE_003",
    TestDaemonStaleLockAndConcurrentStartup._test_all_stale_lock_and_concurrent_scenarios,
)


class TestVaultLogIsolation(unittest.TestCase):
    """
    [VAULT-ISO-001] [VAULT-ISO-002]
    Validates hermetic audit log isolation:
    - Redirection of audit logs to AGY_VAULT_DIR when set in environment.
    - Prohibition of fallback to DEFAULT_VAULT_DIR (~/.antigravity-vault) under test runner
      execution or when engine is a Mock / has vault_dir=None.
    """

    def setUp(self) -> None:
        self.orig_env = os.environ.get("AGY_VAULT_DIR")
        self.tmpdir = tempfile.mkdtemp()
        self.isolated_vault = Path(self.tmpdir) / "iso_vault"
        self.isolated_vault.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.prod_vault = DEFAULT_VAULT_DIR
        self.prod_log = self.prod_vault / "watch.log"
        self.prod_log_exists = self.prod_log.exists()
        self.prod_log_size = self.prod_log.stat().st_size if self.prod_log_exists else 0
        self.prod_log_mtime = self.prod_log.stat().st_mtime if self.prod_log_exists else 0.0

    def tearDown(self) -> None:
        if self.orig_env is not None:
            os.environ["AGY_VAULT_DIR"] = self.orig_env
        else:
            os.environ.pop("AGY_VAULT_DIR", None)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _assert_prod_log_untouched(self, tag: str) -> None:
        """Verifies that production watch.log was not written to or modified."""
        if self.prod_log_exists:
            current_size = self.prod_log.stat().st_size
            current_mtime = self.prod_log.stat().st_mtime
            self.assertEqual(
                current_size,
                self.prod_log_size,
                f"Production watch.log size modified during {tag}: {current_size} != {self.prod_log_size}",
            )
            self.assertEqual(
                current_mtime,
                self.prod_log_mtime,
                f"Production watch.log mtime modified during {tag}: {current_mtime} != {self.prod_log_mtime}",
            )
            content = self.prod_log.read_text(encoding="utf-8", errors="ignore")
            self.assertNotIn(tag, content, f"Production watch.log contains test tag '{tag}'")

    def test_vault_iso_001_redirect_to_agy_vault_dir(self) -> None:
        """
        [VAULT-ISO-001] Audit log writes strictly to isolated directory when AGY_VAULT_DIR is set.
        GIVEN AGY_VAULT_DIR is set to an isolated temporary directory path in the environment
        WHEN LogWatcher._notify_desktop() or audit logging writes a rotation event log entry
        THEN the log entry is written strictly to AGY_VAULT_DIR/watch.log
        AND no write or modification is made to ~/.antigravity-vault/watch.log.
        """
        test_tag = f"iso001_acc_{time.time()}"
        os.environ["AGY_VAULT_DIR"] = str(self.isolated_vault)

        mock_engine = MagicMock()
        watcher = LogWatcher(engine=mock_engine, log_dirs=[self.tmpdir])
        watcher._notify_desktop(target_account=test_tag, old_account="prior-acc", reason="quota 429", reloaded=False)

        iso_log = self.isolated_vault / "watch.log"
        self.assertTrue(iso_log.exists(), f"Expected isolated log file at {iso_log}")
        iso_content = iso_log.read_text(encoding="utf-8")
        self.assertIn(test_tag, iso_content)
        self.assertIn("[watch] Switched from 'prior-acc' to", iso_content)
        self.assertIn("(reason: quota 429)", iso_content)

        # Also verify _append_audit_log if present
        audit_tag = f"audit_entry_{time.time()}"
        if hasattr(watcher, "_append_audit_log"):
            watcher._append_audit_log(target_account=audit_tag, old_account=test_tag, reason="direct audit", reloaded=False)
            iso_content_after = iso_log.read_text(encoding="utf-8")
            self.assertIn(audit_tag, iso_content_after)

        self._assert_prod_log_untouched(test_tag)
        self._assert_prod_log_untouched(audit_tag)

    def test_vault_iso_002_mock_engine_prohibits_production_log(self) -> None:
        """
        [VAULT-ISO-002] Test mock engine without vault_dir does not write to production ~/.antigravity-vault/watch.log.
        GIVEN a test execution where engine is a MagicMock or has vault_dir=None
        AND AGY_VAULT_DIR is not pointing to ~/.antigravity-vault
        WHEN LogWatcher._notify_desktop() or audit logging is invoked
        THEN logging safely redirects to the configured test sandbox or no-ops without writing to ~/.antigravity-vault/watch.log
        AND production vault files remain pristine and unpolluted.
        """
        os.environ.pop("AGY_VAULT_DIR", None)

        # Branch 1: MagicMock engine with default attributes (mock vault_dir)
        tag_mock = f"iso002_mock_{time.time()}"
        mock_engine = MagicMock()
        watcher_mock = LogWatcher(engine=mock_engine, log_dirs=[self.tmpdir])
        watcher_mock._notify_desktop(target_account=tag_mock, reason="mock test")
        if hasattr(watcher_mock, "_append_audit_log"):
            watcher_mock._append_audit_log(target_account=f"{tag_mock}_audit", reason="mock audit")
        self._assert_prod_log_untouched(tag_mock)
        self._assert_prod_log_untouched(f"{tag_mock}_audit")

        # Branch 2: Engine with explicit vault_dir=None
        tag_none = f"iso002_none_{time.time()}"
        none_engine = MagicMock(vault_dir=None)
        watcher_none = LogWatcher(engine=none_engine, log_dirs=[self.tmpdir])
        watcher_none._notify_desktop(target_account=tag_none, reason="none test")
        if hasattr(watcher_none, "_append_audit_log"):
            watcher_none._append_audit_log(target_account=f"{tag_none}_audit", reason="none audit")
        self._assert_prod_log_untouched(tag_none)
        self._assert_prod_log_untouched(f"{tag_none}_audit")

        # Branch 3: Mock engine with configured sandbox vault_dir redirects safely to sandbox
        sandbox_dir = Path(self.tmpdir) / "sandbox_vault"
        sandbox_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        tag_sandbox = f"iso002_sandbox_{time.time()}"
        sandbox_engine = MagicMock(vault_dir=sandbox_dir)
        watcher_sandbox = LogWatcher(engine=sandbox_engine, log_dirs=[self.tmpdir])
        watcher_sandbox._notify_desktop(target_account=tag_sandbox, reason="sandbox test")
        sandbox_log = sandbox_dir / "watch.log"
        self.assertTrue(sandbox_log.exists(), f"Expected sandbox log file at {sandbox_log}")
        self.assertIn(tag_sandbox, sandbox_log.read_text(encoding="utf-8"))
        self._assert_prod_log_untouched(tag_sandbox)

    def test_vault_iso_003_prod_agy_vault_dir_under_test_prohibits_production_log(self) -> None:
        """Production vault paths in AGY_VAULT_DIR or engine.vault_dir never write to production under tests."""
        tag_prod_env = f"iso003_prodenv_{time.time()}"
        os.environ["AGY_VAULT_DIR"] = str(self.prod_vault)
        mock_engine = MagicMock()
        watcher = LogWatcher(engine=mock_engine, log_dirs=[self.tmpdir])
        watcher._notify_desktop(target_account=tag_prod_env, reason="prod env test")
        self._assert_prod_log_untouched(tag_prod_env)

        tag_prod_eng = f"iso003_prodeng_{time.time()}"
        os.environ.pop("AGY_VAULT_DIR", None)
        prod_engine = MagicMock(vault_dir=self.prod_vault)
        watcher_prod = LogWatcher(engine=prod_engine, log_dirs=[self.tmpdir])
        watcher_prod._notify_desktop(target_account=tag_prod_eng, reason="prod eng test")
        self._assert_prod_log_untouched(tag_prod_eng)

    def test_VAULT_ERR_001_unwriteable_temp_vault_directory_handled_gracefully(self) -> None:
        """[VAULT-ERR-001] Read-only isolated vault directory logs error without crashing parent."""
        ro_vault = Path(self.tmpdir) / "ro_vault"
        ro_vault.mkdir(parents=True, exist_ok=True, mode=0o500)
        os.chmod(ro_vault, 0o500)
        os.environ["AGY_VAULT_DIR"] = str(ro_vault)

        mock_engine = MagicMock()
        watcher = LogWatcher(engine=mock_engine, log_dirs=[self.tmpdir])
        stderr_buf = io.StringIO()
        try:
            with patch("sys.stderr", stderr_buf):
                watcher._notify_desktop(target_account="ro_acc", reason="ro test")
                append_secure_log_entry(ro_vault / "watch.log", "direct ro write\n")
            err_output = stderr_buf.getvalue()
            self.assertTrue(
                "Failed to write audit log entry" in err_output
                or "Error appending audit log" in err_output
                or "Permission denied" in err_output,
                f"Expected error in stderr but got: {err_output!r}"
            )
        finally:
            os.chmod(ro_vault, 0o700)

        # Test simulated OSError during log append across diverse system error conditions
        for simulated_errno in (errno.ENOSPC, errno.EACCES, errno.EROFS, errno.EDQUOT, errno.EMFILE):
            with patch("agy_switcher.presence._open_secure_log_file", side_effect=OSError(simulated_errno, "simulated error")):
                stderr_sim = io.StringIO()
                with patch("sys.stderr", stderr_sim):
                    append_secure_log_entry(self.isolated_vault / "watch.log", "simulated write\n")
                self.assertIn("Failed to write audit log entry", stderr_sim.getvalue())

        # Test pre-existing read-only file (mode 0400) handling without crashing
        ro_file = Path(self.tmpdir) / "ro_file_test.log"
        ro_file.write_text("initial\n", encoding="utf-8")
        os.chmod(ro_file, 0o400)
        try:
            stderr_ro = io.StringIO()
            with patch("sys.stderr", stderr_ro):
                append_secure_log_entry(ro_file, "ro append test\n")
            self.assertIn("Failed to write audit log entry", stderr_ro.getvalue())
        finally:
            os.chmod(ro_file, 0o600)

        # Test resilience when stderr itself encounters an error (RuntimeError or BrokenPipeError)
        for stderr_err in (RuntimeError("Broken stderr stream"), BrokenPipeError(errno.EPIPE, "Broken pipe")):
            broken_stderr = MagicMock()
            broken_stderr.write.side_effect = stderr_err
            with patch("sys.stderr", broken_stderr):
                with patch("agy_switcher.presence._open_secure_log_file", side_effect=OSError(errno.EACCES, "denied")):
                    append_secure_log_entry(self.isolated_vault / "watch.log", "broken stderr test\n")

    test_vault_err_001_unwriteable_temp_vault_directory_handled_gracefully = test_VAULT_ERR_001_unwriteable_temp_vault_directory_handled_gracefully

    def test_VAULT_ERR_002_empty_or_whitespace_agy_vault_dir_handled_safely(self) -> None:
        """[VAULT-ERR-002] Empty or whitespace-only AGY_VAULT_DIR falls back safely without writing to prod."""
        for ws_val in ("", "   ", "\t  \n"):
            os.environ["AGY_VAULT_DIR"] = ws_val
            tag_ws = f"err002_ws_{time.time()}"
            mock_engine = MagicMock(spec=[])
            watcher = LogWatcher(engine=mock_engine, log_dirs=[self.tmpdir])
            watcher._notify_desktop(target_account=tag_ws, reason="whitespace env test")
            self._assert_prod_log_untouched(tag_ws)

            # Direct append_secure_log_entry should not touch prod
            append_secure_log_entry(self.prod_vault / "watch.log", f"{tag_ws}_direct\n")
            self._assert_prod_log_untouched(f"{tag_ws}_direct")

            # Direct _resolve_vault_log_path rejects empty/whitespace without fallback to DEFAULT_VAULT_DIR under test
            self.assertIsNone(_resolve_vault_log_path())
            self.assertIsNone(_resolve_vault_log_path(""))
            self.assertIsNone(_resolve_vault_log_path("   "))
            self.assertIsNone(_resolve_vault_log_path(Path("")))
            self.assertIsNone(_resolve_vault_log_path(self.prod_vault / "watch.log"))
            self.assertIsNone(watcher._resolve_vault_log_path())
            self.assertIsNone(watcher._resolve_vault_log_path(""))
            self.assertIsNone(watcher._resolve_vault_log_path("   "))
            self.assertIsNone(watcher._resolve_audit_log_dir())

            # Direct append_secure_log_entry with empty/whitespace suppresses without creating stray files
            append_secure_log_entry("", "direct empty string write\n")
            append_secure_log_entry("   ", "direct whitespace write\n")
            self.assertFalse(Path("   ").exists())

            # Low-level _open_secure_log_file rejects invalid empty/whitespace paths with ValueError
            with self.assertRaises(ValueError):
                _open_secure_log_file("")
            with self.assertRaises(ValueError):
                _open_secure_log_file("   ")

            # When engine provides an isolated sandbox vault_dir, it safely redirects to sandbox
            sandbox_dir = Path(self.tmpdir) / f"fallback_sandbox_{abs(hash(ws_val))}"
            sandbox_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            sandbox_engine = MagicMock(vault_dir=sandbox_dir)
            watcher_sandbox = LogWatcher(engine=sandbox_engine, log_dirs=[self.tmpdir])
            self.assertEqual(watcher_sandbox._resolve_audit_log_dir(), sandbox_dir)
            self.assertEqual(watcher_sandbox._resolve_vault_log_path(), sandbox_dir / "watch.log")
            self.assertEqual(_resolve_vault_log_path(engine=sandbox_engine), sandbox_dir / "watch.log")

            watcher_sandbox._notify_desktop(target_account=tag_ws, reason="whitespace with engine sandbox")
            sandbox_log = sandbox_dir / "watch.log"
            self.assertTrue(sandbox_log.exists())
            self.assertIn(tag_ws, sandbox_log.read_text(encoding="utf-8"))
            self._assert_prod_log_untouched(tag_ws)

    test_vault_err_002_empty_or_whitespace_agy_vault_dir_handled_safely = test_VAULT_ERR_002_empty_or_whitespace_agy_vault_dir_handled_safely

    def test_VAULT_ERR_003_filesystem_disk_full_enospc_during_log_append_caught_gracefully(self) -> None:
        """[VAULT-ERR-003] Filesystem disk full ENOSPC during log append caught gracefully without crashing."""
        with patch("agy_switcher.presence._open_secure_log_file", side_effect=OSError(errno.ENOSPC, "No space left on device")):
            stderr_buf = io.StringIO()
            with patch("sys.stderr", stderr_buf):
                append_secure_log_entry(self.isolated_vault / "watch.log", "disk full entry\n")
            err_output = stderr_buf.getvalue()
            self.assertIn("Failed to write audit log entry", err_output)
            self.assertTrue("ENOSPC" in err_output or "Disk full" in err_output, f"Expected ENOSPC alert but got: {err_output!r}")

        class FailingFlushFile(io.StringIO):
            def flush(self) -> None:
                raise OSError(errno.ENOSPC, "No space left on device")

        failing_file = FailingFlushFile()
        with patch("agy_switcher.presence._open_secure_log_file", return_value=999), \
             patch("builtins.open", return_value=failing_file), \
             patch("os.close"):
            stderr_flush = io.StringIO()
            with patch("sys.stderr", stderr_flush):
                append_secure_log_entry(self.isolated_vault / "watch.log", "flush fail entry\n")
            flush_err = stderr_flush.getvalue()
            self.assertIn("Failed to write audit log entry", flush_err)
            self.assertTrue("ENOSPC" in flush_err or "Disk full" in flush_err, f"Expected ENOSPC alert on flush but got: {flush_err!r}")

        class FailingWriteFile(io.StringIO):
            def write(self, s: str) -> int:
                raise OSError(errno.ENOSPC, "No space left on device")

        failing_write_file = FailingWriteFile()
        with patch("agy_switcher.presence._open_secure_log_file", return_value=999), \
             patch("builtins.open", return_value=failing_write_file), \
             patch("os.close"):
            stderr_write = io.StringIO()
            with patch("sys.stderr", stderr_write):
                append_secure_log_entry(self.isolated_vault / "watch.log", "write fail entry\n")
            write_err = stderr_write.getvalue()
            self.assertIn("Failed to write audit log entry", write_err)
            self.assertTrue("ENOSPC" in write_err or "Disk full" in write_err, f"Expected ENOSPC alert on write but got: {write_err!r}")

        with patch("pathlib.Path.mkdir", side_effect=OSError(errno.ENOSPC, "No space left on device")):
            stderr_mkdir = io.StringIO()
            with patch("sys.stderr", stderr_mkdir):
                append_secure_log_entry(self.isolated_vault / "new_dir" / "watch.log", "mkdir fail entry\n")
            mkdir_err = stderr_mkdir.getvalue()
            self.assertIn("Failed to write audit log entry", mkdir_err)
            self.assertTrue("ENOSPC" in mkdir_err or "Disk full" in mkdir_err, f"Expected ENOSPC alert on mkdir but got: {mkdir_err!r}")

        edquot_errno = getattr(errno, "EDQUOT", errno.ENOSPC)
        with patch("agy_switcher.presence._open_secure_log_file", side_effect=OSError(edquot_errno, "Disk quota exceeded")):
            stderr_quota = io.StringIO()
            with patch("sys.stderr", stderr_quota):
                append_secure_log_entry(self.isolated_vault / "watch.log", "edquot entry\n")
            quota_err = stderr_quota.getvalue()
            self.assertIn("Failed to write audit log entry", quota_err)

        with patch("sys.stderr", None), \
             patch("agy_switcher.presence._open_secure_log_file", side_effect=OSError(errno.ENOSPC, "No space left on device")):
            append_secure_log_entry(self.isolated_vault / "watch.log", "none stderr entry\n")

        os.environ["AGY_VAULT_DIR"] = str(self.isolated_vault)
        mock_engine = MagicMock()
        watcher = LogWatcher(engine=mock_engine, log_dirs=[self.tmpdir])
        with patch("agy_switcher.presence._open_secure_log_file", side_effect=OSError(errno.ENOSPC, "No space left on device")):
            stderr_rot = io.StringIO()
            with patch("sys.stderr", stderr_rot):
                watcher._notify_desktop(target_account="rotate_acc", reason="disk full rotation test")
            rot_err = stderr_rot.getvalue()
            self.assertTrue("Failed to write audit log entry" in rot_err or "Error appending audit log" in rot_err)
            self.assertTrue("ENOSPC" in rot_err or "Disk full" in rot_err, f"Expected ENOSPC alert on rotation but got: {rot_err!r}")

        broken_stderr = MagicMock()
        broken_stderr.write.side_effect = BrokenPipeError(errno.EPIPE, "Broken pipe")
        with patch("agy_switcher.presence._open_secure_log_file", side_effect=OSError(errno.ENOSPC, "No space left on device")):
            with patch("sys.stderr", broken_stderr):
                append_secure_log_entry(self.isolated_vault / "watch.log", "silent broken pipe\n")
                watcher._notify_desktop(target_account="rotate_acc2", reason="broken stderr rotation")

    test_vault_err_003_filesystem_disk_full_enospc_during_log_append_caught_gracefully = test_VAULT_ERR_003_filesystem_disk_full_enospc_during_log_append_caught_gracefully

    def test_VAULT_EDGE_001_concurrent_test_processes_write_to_independent_vaults(self) -> None:
        """[VAULT-EDGE-001] Concurrent test processes write to independent temporary vaults without interference."""
        num_workers = 3
        worker_dirs: list[Path] = []
        for i in range(num_workers):
            wdir = Path(self.tmpdir) / f"worker_vault_{i}"
            wdir.mkdir(parents=True, exist_ok=True, mode=0o700)
            worker_dirs.append(wdir)

        unique_run_id = f"edge001_{time.time()}"
        worker_script = """
import os, sys, time
from pathlib import Path
from unittest.mock import MagicMock

repo_root = os.path.abspath(sys.argv[1])
sys.path.insert(0, repo_root)
sys.path.insert(0, os.path.join(repo_root, "scripts"))

from scripts.watch import LogWatcher, DaemonManager, append_secure_log_entry

worker_id = sys.argv[2]
run_id = sys.argv[3]
vault_dir = Path(os.environ["AGY_VAULT_DIR"])

dm = DaemonManager()
lock_fd = dm.acquire_lock()
if lock_fd is None:
    sys.exit(2)

try:
    mock_engine = MagicMock()
    mock_engine.vault_dir = vault_dir
    watcher = LogWatcher(engine=mock_engine, log_dirs=[str(vault_dir)])

    for j in range(3):
        watcher._notify_desktop(target_account=f"{run_id}_w{worker_id}_{j}", reason=f"rotation_{j}")
        append_secure_log_entry(vault_dir / "watch.log", f"direct_{run_id}_w{worker_id}_{j}\\n")
        time.sleep(0.01)
finally:
    dm.release_lock()

sys.exit(0)
"""
        repo_root = str(Path(__file__).resolve().parent.parent)
        procs: list[subprocess.Popen[bytes]] = []
        for i, wdir in enumerate(worker_dirs):
            env = dict(os.environ)
            env["AGY_VAULT_DIR"] = str(wdir)
            env["PYTHONPATH"] = f"{repo_root}/scripts:{repo_root}"
            p = subprocess.Popen(
                [sys.executable, "-c", worker_script, repo_root, str(i), unique_run_id],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            procs.append(p)

        try:
            for i, p in enumerate(procs):
                stdout_b, stderr_b = p.communicate(timeout=15.0)
                self.assertEqual(
                    p.returncode,
                    0,
                    f"Worker {i} failed with returncode {p.returncode}: stdout={stdout_b.decode()!r}, stderr={stderr_b.decode()!r}"
                )
        finally:
            for p in procs:
                if p.poll() is None:
                    try:
                        p.kill()
                        p.wait()
                    except Exception:
                        pass

        for i, wdir in enumerate(worker_dirs):
            log_file = wdir / "watch.log"
            self.assertTrue(log_file.exists(), f"Log file for worker {i} not found at {log_file}")
            content = log_file.read_text(encoding="utf-8")
            for j in range(3):
                self.assertIn(f"{unique_run_id}_w{i}_{j}", content)
                self.assertIn(f"direct_{unique_run_id}_w{i}_{j}", content)
            for other in range(num_workers):
                if other != i:
                    self.assertNotIn(f"{unique_run_id}_w{other}_", content)
                    self.assertNotIn(f"direct_{unique_run_id}_w{other}_", content)

        self._assert_prod_log_untouched(unique_run_id)

    test_vault_edge_001_concurrent_test_processes_write_to_independent_vaults = test_VAULT_EDGE_001_concurrent_test_processes_write_to_independent_vaults

    def test_VAULT_EDGE_002_dynamic_mutation_of_agy_vault_dir_rebinds_destination(self) -> None:
        """[VAULT-EDGE-002] Dynamic mutation of AGY_VAULT_DIR rebinds logging destination and cleans up handles hermetically."""
        vault_a = Path(self.tmpdir) / "vault_a"
        vault_b = Path(self.tmpdir) / "vault_b"
        vault_c = Path(self.tmpdir) / "vault_c"
        vault_a.mkdir(parents=True, exist_ok=True, mode=0o700)
        vault_b.mkdir(parents=True, exist_ok=True, mode=0o700)
        vault_c.mkdir(parents=True, exist_ok=True, mode=0o700)

        # Step 1: Initial AGY_VAULT_DIR binding
        os.environ["AGY_VAULT_DIR"] = str(vault_a)
        watcher = LogWatcher(engine=MagicMock(), log_dirs=[self.tmpdir])
        lock_fd1 = watcher.daemon_manager.acquire_lock()
        self.assertIsNotNone(lock_fd1)
        self.assertEqual(watcher.daemon_manager.vault_dir, vault_a)
        self.assertEqual(watcher.daemon_manager.log_file, vault_a / "watch.log")
        self.assertEqual(watcher.cooldown_file, vault_a / "cache" / "cooldown.json")

        watcher._notify_desktop(target_account="dest_a", reason="step 1")
        watcher.set_cooldown("acc_a", 9999999999)
        append_secure_log_entry(vault_a / "watch.log", "direct a\n")

        self.assertTrue((vault_a / "watch.log").exists())
        content_a = (vault_a / "watch.log").read_text(encoding="utf-8")
        self.assertIn("dest_a", content_a)
        self.assertIn("direct a", content_a)
        self.assertTrue((vault_a / "cache" / "cooldown.json").exists())
        self.assertIn("acc_a", (vault_a / "cache" / "cooldown.json").read_text(encoding="utf-8"))
        self.assertFalse((vault_b / "watch.log").exists())

        # Step 2: Dynamic mutation to vault_b rebinds destinations and cleans up old handles
        os.environ["AGY_VAULT_DIR"] = str(vault_b)
        self.assertEqual(watcher.daemon_manager.vault_dir, vault_b)
        self.assertEqual(watcher.daemon_manager.log_file, vault_b / "watch.log")
        self.assertEqual(watcher.cooldown_file, vault_b / "cache" / "cooldown.json")

        watcher._notify_desktop(target_account="dest_b", reason="step 2")
        watcher.set_cooldown("acc_b", 9999999999)
        append_secure_log_entry("watch.log", "direct b\n")
        # Passing old path rebinds hermetically to current AGY_VAULT_DIR under test runner
        append_secure_log_entry(vault_a / "watch.log", "direct old path b\n")

        lock_fd2 = watcher.daemon_manager.acquire_lock()
        self.assertIsNotNone(lock_fd2)
        self.assertEqual(watcher.daemon_manager._locked_path, vault_b / "watch.lock")

        # Verify vault_a lock was released and closed (re-lockable), while vault_b is actively locked
        test_fd_a = os.open(str(vault_a / "watch.lock"), os.O_RDWR)
        fcntl.flock(test_fd_a, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.close(test_fd_a)

        test_fd_b = os.open(str(vault_b / "watch.lock"), os.O_RDWR)
        with self.assertRaises((BlockingIOError, OSError)):
            fcntl.flock(test_fd_b, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.close(test_fd_b)

        self.assertTrue((vault_b / "watch.log").exists())
        content_b = (vault_b / "watch.log").read_text(encoding="utf-8")
        self.assertIn("dest_b", content_b)
        self.assertIn("direct b", content_b)
        self.assertIn("direct old path b", content_b)
        self.assertTrue((vault_b / "cache" / "cooldown.json").exists())
        self.assertIn("acc_b", (vault_b / "cache" / "cooldown.json").read_text(encoding="utf-8"))

        # Assert no writes leaked to vault_a
        content_a_after = (vault_a / "watch.log").read_text(encoding="utf-8")
        self.assertNotIn("dest_b", content_a_after)
        self.assertNotIn("direct b", content_a_after)
        self.assertNotIn("direct old path b", content_a_after)
        self.assertNotIn("acc_b", (vault_a / "cache" / "cooldown.json").read_text(encoding="utf-8"))

        # Explicit handle cleanup on watcher
        watcher.close()
        self.assertIsNone(watcher.daemon_manager._lock_fd)

        # Step 3: Context manager handle lifecycle cleanup on vault_c
        os.environ["AGY_VAULT_DIR"] = str(vault_c)
        with LogWatcher(engine=MagicMock(), log_dirs=[self.tmpdir]) as watcher3:
            watcher3._notify_desktop(target_account="dest_c", reason="step 3")
            fd3 = watcher3.daemon_manager.acquire_lock()
            self.assertIsNotNone(fd3)
            self.assertEqual(watcher3.daemon_manager.vault_dir, vault_c)
            self.assertIsNotNone(watcher3.daemon_manager._lock_fd)

        # After exiting with block, file descriptor must be released and cleaned up
        self.assertIsNone(watcher3.daemon_manager._lock_fd)
        self.assertTrue((vault_c / "watch.log").exists())
        self.assertIn("dest_c", (vault_c / "watch.log").read_text(encoding="utf-8"))
        self.assertNotIn("dest_c", (vault_a / "watch.log").read_text(encoding="utf-8"))
        self.assertNotIn("dest_c", (vault_b / "watch.log").read_text(encoding="utf-8"))

    test_vault_edge_002_dynamic_mutation_of_agy_vault_dir_rebinds_destination = test_VAULT_EDGE_002_dynamic_mutation_of_agy_vault_dir_rebinds_destination

    test_VAULT_ISO_001_redirect_to_agy_vault_dir = test_vault_iso_001_redirect_to_agy_vault_dir
    test_VAULT_ISO_002_mock_engine_prohibits_production_log = test_vault_iso_002_mock_engine_prohibits_production_log

    def _test_all_vault_isolation_scenarios(self) -> None:
        scenarios = [
            self.test_vault_iso_001_redirect_to_agy_vault_dir,
            self.test_vault_iso_002_mock_engine_prohibits_production_log,
            self.test_vault_iso_003_prod_agy_vault_dir_under_test_prohibits_production_log,
            self.test_VAULT_ERR_001_unwriteable_temp_vault_directory_handled_gracefully,
            self.test_VAULT_ERR_002_empty_or_whitespace_agy_vault_dir_handled_safely,
            self.test_vault_edge_002_dynamic_mutation_of_agy_vault_dir_rebinds_destination,
        ]
        for scenario in scenarios:
            self.setUp()
            try:
                scenario()
            finally:
                self.tearDown()

    def _test_vault_error_scenarios(self) -> None:
        scenarios = [
            self.test_VAULT_ERR_001_unwriteable_temp_vault_directory_handled_gracefully,
            self.test_VAULT_ERR_002_empty_or_whitespace_agy_vault_dir_handled_safely,
        ]
        for scenario in scenarios:
            self.setUp()
            try:
                scenario()
            finally:
                self.tearDown()


    def _test_vault_ticket07_scenarios(self) -> None:
        scenarios = [
            self.test_VAULT_ERR_003_filesystem_disk_full_enospc_during_log_append_caught_gracefully,
            self.test_VAULT_EDGE_001_concurrent_test_processes_write_to_independent_vaults,
        ]
        for scenario in scenarios:
            self.setUp()
            try:
                scenario()
            finally:
                self.tearDown()


    def _test_vault_ticket08_scenarios(self) -> None:
        scenarios = [
            self.test_VAULT_EDGE_002_dynamic_mutation_of_agy_vault_dir_rebinds_destination,
        ]
        for scenario in scenarios:
            self.setUp()
            try:
                scenario()
            finally:
                self.tearDown()


setattr(
    TestVaultLogIsolation,
    "test_VAULT_ISO_001|VAULT_ISO_002",
    TestVaultLogIsolation._test_all_vault_isolation_scenarios,
)
setattr(
    TestVaultLogIsolation,
    "test_VAULT_ERR_001|VAULT_ERR_002",
    TestVaultLogIsolation._test_vault_error_scenarios,
)
setattr(
    TestVaultLogIsolation,
    "test_VAULT_ERR_003|VAULT_EDGE_001",
    TestVaultLogIsolation._test_vault_ticket07_scenarios,
)
setattr(
    TestVaultLogIsolation,
    "test_VAULT_EDGE_002_ticket08_gate",
    TestVaultLogIsolation._test_vault_ticket08_scenarios,
)


if __name__ == '__main__':
    unittest.main()

