import json
import socket
import unittest
from unittest.mock import MagicMock, patch, mock_open
from pathlib import Path
import os
import shutil
import sys
import tempfile
import time
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from scripts.watch import LogWatcher, PresenceDetector
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


if __name__ == '__main__':
    unittest.main()

