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

from scripts.watch import LogWatcher, PresenceDetector

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


if __name__ == '__main__':
    unittest.main()

