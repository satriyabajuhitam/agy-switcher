import unittest
from unittest.mock import MagicMock, patch, mock_open
from pathlib import Path
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.watch import LogWatcher

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
            
            self.engine_mock.switch_tokens.assert_called_once_with('acc2', restart_server=True, force=True)

    @patch("scripts.watch.time.sleep", return_value=None)
    def test_trigger_switch_aborts_when_all_exhausted(self, sleep_mock):
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
            self.engine_mock.switch_tokens.assert_called_once_with('acc3', restart_server=True, force=True)


    def test_process_line_returns_boolean(self):
        # Non-exhausted returns False
        self.assertFalse(self.watcher.process_line("Normal log line"))

        # Exhausted with switch returns True
        with patch.object(self.watcher, "trigger_switch", return_value=True):
            self.assertTrue(self.watcher.process_line("RESOURCE_EXHAUSTED (code 429)"))

        # Exhausted with failed switch returns False
        with patch.object(self.watcher, "trigger_switch", return_value=False):
            self.assertFalse(self.watcher.process_line("RESOURCE_EXHAUSTED (code 429)"))

    def test_watch_batch_burst_lines_breaks_on_switch(self):
        """Verify that when a batch of lines contains multiple 429 errors, only the first triggers a switch."""
        switch_count = 0
        def fake_trigger_switch(line="", **kwargs):
            nonlocal switch_count
            switch_count += 1
            return True

        self.watcher.trigger_switch = fake_trigger_switch

        batch_lines = [
            "RESOURCE_EXHAUSTED (code 429) line 1\n",
            "RESOURCE_EXHAUSTED (code 429) line 2\n",
            "RESOURCE_EXHAUSTED (code 429) line 3\n",
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
            line1 = "line 1: normal startup\n"
            line2 = "line 2: RESOURCE_EXHAUSTED (code 429)\n"
            line3 = "line 3: trailing content\n"
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

            with patch("time.time", return_value=100.0):
                switched = self.watcher.process_line("RESOURCE_EXHAUSTED (code 429)")
                self.assertTrue(switched)
                self.assertEqual(self.watcher.last_switch_time, 100.0)
                self.assertEqual(self.watcher.damping_seconds, 15.0)

            # Inside damping window (100.0 + 5.0 < 100.0 + 15.0) -> suppressed
            with patch("time.time", return_value=105.0):
                switched_suppressed = self.watcher.process_line("RESOURCE_EXHAUSTED (code 429)")
                self.assertFalse(switched_suppressed)
                # Switch count should still be 1
                self.assertEqual(self.engine_mock.switch_tokens.call_count, 1)
                self.assertEqual(self.watcher.damping_seconds, 15.0)

            # Outside damping window (100.0 + 16.0 > 100.0 + 15.0) -> allowed
            with patch("time.time", return_value=116.0):
                switched_allowed = self.watcher.process_line("RESOURCE_EXHAUSTED (code 429)")
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


if __name__ == '__main__':
    unittest.main()

