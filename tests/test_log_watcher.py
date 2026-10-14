import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.watch import PresenceDetector, CooldownManager, LogWatcher


class TestPresenceDetector(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp_dir.name)

        self.gemini_dir = self.base_dir / ".gemini"
        self.presence_dir = self.gemini_dir / "antigravity" / "presence"
        self.brain_dir = self.gemini_dir / "antigravity" / "brain"
        self.cli_log_dir = self.gemini_dir / "antigravity-cli" / "log"
        self.proc_dir = self.base_dir / "proc"

        self.presence_dir.mkdir(parents=True, exist_ok=True)
        self.brain_dir.mkdir(parents=True, exist_ok=True)
        self.cli_log_dir.mkdir(parents=True, exist_ok=True)
        self.proc_dir.mkdir(parents=True, exist_ok=True)

        self.detector = PresenceDetector(
            gemini_dir=self.gemini_dir,
            proc_dir=self.proc_dir,
        )

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _create_mock_proc_process(self, pid: int, cmdline: bytes, fd_targets: list[Path]) -> Path:
        pid_dir = self.proc_dir / str(pid)
        pid_dir.mkdir(parents=True, exist_ok=True)

        cmdline_file = pid_dir / "cmdline"
        cmdline_file.write_bytes(cmdline)

        fd_dir = pid_dir / "fd"
        fd_dir.mkdir(parents=True, exist_ok=True)

        for i, target in enumerate(fd_targets):
            fd_link = fd_dir / str(i + 10)
            try:
                os.symlink(str(target), str(fd_link))
            except OSError:
                pass

        return pid_dir

    def test_proc_fd_discovery_success(self):
        """Tier 1: Discovers active session ID from language_server proc fd pointing to presence/<uuid>.lock."""
        active_uuid = "11111111-2222-3333-4444-555555555555"
        lock_file = self.presence_dir / f"{active_uuid}.lock"
        lock_file.touch()

        # Mock unrelated process
        self._create_mock_proc_process(
            pid=1001,
            cmdline=b"/usr/bin/python3\x00some_script.py\x00",
            fd_targets=[self.base_dir / "unrelated.txt"],
        )

        # Mock language_server process holding lock
        self._create_mock_proc_process(
            pid=2002,
            cmdline=b"/path/to/language_server\x00--standalone\x00",
            fd_targets=[lock_file, self.base_dir / "other.log"],
        )

        session_id = self.detector.get_active_session_id()
        self.assertEqual(session_id, active_uuid)

    def test_proc_fd_multiple_candidates_picks_newest(self):
        """Tier 1: When multiple language_server processes/fds exist, selects the lock with newest mtime."""
        uuid_old = "00000000-0000-0000-0000-000000000001"
        uuid_new = "00000000-0000-0000-0000-000000000002"

        lock_old = self.presence_dir / f"{uuid_old}.lock"
        lock_new = self.presence_dir / f"{uuid_new}.lock"

        lock_old.touch()
        os.utime(lock_old, (1000.0, 1000.0))

        lock_new.touch()
        os.utime(lock_new, (2000.0, 2000.0))

        self._create_mock_proc_process(
            pid=2001,
            cmdline=b"language_server --client=desktop\x00",
            fd_targets=[lock_old],
        )
        self._create_mock_proc_process(
            pid=2002,
            cmdline=b"language_server --client=desktop\x00",
            fd_targets=[lock_new],
        )

        session_id = self.detector.get_active_session_id()
        self.assertEqual(session_id, uuid_new)

    def test_proc_fd_with_deleted_marker(self):
        """Tier 1: Strips Linux ' (deleted)' suffix from symlink targets."""
        active_uuid = "deleted-1111-2222-3333-444455556666"
        lock_file = self.presence_dir / f"{active_uuid}.lock"
        lock_file.touch()

        # Create symlink pointing to target path + ' (deleted)' simulated via mock readlink
        pid_dir = self.proc_dir / "2005"
        pid_dir.mkdir(parents=True, exist_ok=True)
        (pid_dir / "cmdline").write_bytes(b"language_server\x00")
        fd_dir = pid_dir / "fd"
        fd_dir.mkdir(parents=True, exist_ok=True)
        dummy_link = fd_dir / "15"
        dummy_link.touch()

        with patch("os.readlink", return_value=f"{lock_file} (deleted)"):
            session_id = self.detector.get_active_session_id()
            self.assertEqual(session_id, active_uuid)

    def test_proc_inaccessible_fallback_to_presence_locks(self):
        """Tier 2 fallback: When /proc is inaccessible (raises OSError), falls back to newest .lock mtime."""
        uuid_old = "aaaaaaa1-0000-0000-0000-000000000000"
        uuid_new = "aaaaaaa2-0000-0000-0000-000000000000"

        lock_old = self.presence_dir / f"{uuid_old}.lock"
        lock_new = self.presence_dir / f"{uuid_new}.lock"

        lock_old.touch()
        os.utime(lock_old, (100.0, 100.0))

        lock_new.touch()
        os.utime(lock_new, (500.0, 500.0))

        # Test 1: Real OS PermissionError via chmod 0 on restricted proc directory
        restricted_proc = self.base_dir / "restricted_proc"
        restricted_proc.mkdir(mode=0o000, exist_ok=True)
        try:
            os.chmod(restricted_proc, 0o000)
            detector = PresenceDetector(gemini_dir=self.gemini_dir, proc_dir=restricted_proc)
            session_id = detector.get_active_session_id()
            self.assertEqual(session_id, uuid_new)
        finally:
            os.chmod(restricted_proc, 0o700)

        # Test 2: Simulated unexpected OSError from _discover_from_proc
        with patch.object(self.detector, "_discover_from_proc", side_effect=OSError("Proc access restricted")):
            session_id = self.detector.get_active_session_id()
            self.assertEqual(session_id, uuid_new)

    def test_proc_no_language_server_fallback(self):
        """Tier 2 fallback: When /proc has processes but none are language_server, falls back to presence/."""
        uuid_standalone = "bbbbbbbb-1111-2222-3333-444444444444"
        lock_file = self.presence_dir / f"{uuid_standalone}.lock"
        lock_file.touch()

        self._create_mock_proc_process(
            pid=3001,
            cmdline=b"/bin/bash\x00",
            fd_targets=[],
        )

        session_id = self.detector.get_active_session_id()
        self.assertEqual(session_id, uuid_standalone)

    def test_proc_and_presence_both_empty_returns_none(self):
        """Returns None without raising exceptions if neither /proc nor presence/ have valid locks."""
        self.assertIsNone(self.detector.get_active_session_id())

    def test_presence_dir_missing_returns_none(self):
        """Returns None without raising exceptions if presence directory does not exist."""
        non_existent_gemini = self.base_dir / "non_existent_gemini"
        detector = PresenceDetector(gemini_dir=non_existent_gemini, proc_dir=self.proc_dir)
        self.assertIsNone(detector.get_active_session_id())

    def test_active_transcript_path_when_session_dir_exists(self):
        """Returns exact Path to brain/<active-id>/.system_generated/logs/transcript.jsonl when session dir exists."""
        uuid = "cccccccc-1111-2222-3333-444444444444"
        (self.presence_dir / f"{uuid}.lock").touch()

        session_brain = self.brain_dir / uuid
        session_brain.mkdir(parents=True, exist_ok=True)

        expected_transcript = session_brain / ".system_generated" / "logs" / "transcript.jsonl"
        transcript_path = self.detector.get_active_transcript_path()

        self.assertIsInstance(transcript_path, Path)
        self.assertEqual(transcript_path, expected_transcript)

    def test_active_transcript_path_when_session_dir_missing(self):
        """Returns None if active session ID exists in presence, but session dir under brain/ does not exist."""
        uuid = "dddddddd-1111-2222-3333-444444444444"
        (self.presence_dir / f"{uuid}.lock").touch()

        # brain/<uuid> not created
        self.assertIsNone(self.detector.get_active_transcript_path())

    def test_active_transcript_path_when_no_active_session(self):
        """Returns None if no active session ID can be discovered."""
        self.assertIsNone(self.detector.get_active_transcript_path())

    def test_watch_targets_aggregates_transcript_and_cli_logs(self):
        """Returns active transcript path (if existing) and existing antigravity-cli/log/*.log files."""
        uuid = "eeeeeeee-1111-2222-3333-444444444444"
        (self.presence_dir / f"{uuid}.lock").touch()

        session_logs_dir = self.brain_dir / uuid / ".system_generated" / "logs"
        session_logs_dir.mkdir(parents=True, exist_ok=True)
        transcript_file = session_logs_dir / "transcript.jsonl"
        transcript_file.write_text('{"type": "START"}\n', encoding="utf-8")

        cli_log1 = self.cli_log_dir / "cli_20261014.log"
        cli_log2 = self.cli_log_dir / "cli_20261013.log"
        cli_txt = self.cli_log_dir / "ignored.txt"
        cli_log1.write_text("log 1", encoding="utf-8")
        cli_log2.write_text("log 2", encoding="utf-8")
        cli_txt.write_text("text", encoding="utf-8")

        # Fake old invalid directory that must NOT be watched
        old_invalid_dir = self.gemini_dir / "antigravity" / "log"
        old_invalid_dir.mkdir(parents=True, exist_ok=True)
        (old_invalid_dir / "fake.log").write_text("should be omitted", encoding="utf-8")

        targets = self.detector.get_watch_targets()

        # Target list must include transcript and cli logs as Paths
        self.assertIn(transcript_file, targets)
        self.assertIn(cli_log1, targets)
        self.assertIn(cli_log2, targets)
        self.assertNotIn(cli_txt, targets)
        self.assertNotIn(old_invalid_dir / "fake.log", targets)

        # Also verify string output support if requested
        str_targets = self.detector.get_watch_targets(as_str=True)
        self.assertIn(str(transcript_file), str_targets)
        self.assertIn(str(cli_log1), str_targets)

    def test_watch_targets_when_transcript_not_created_yet(self):
        """If session dir exists but transcript.jsonl has not yet been created, omit transcript from targets."""
        uuid = "ffffffff-1111-2222-3333-444444444444"
        (self.presence_dir / f"{uuid}.lock").touch()

        session_brain = self.brain_dir / uuid
        session_brain.mkdir(parents=True, exist_ok=True)
        # transcript.jsonl is not touched

        cli_log = self.cli_log_dir / "active.log"
        cli_log.touch()

        targets = self.detector.get_watch_targets()
        self.assertEqual(targets, [cli_log])

    def test_default_init_paths(self):
        """Verify default constructor uses ~/.gemini and /proc."""
        default_detector = PresenceDetector()
        self.assertEqual(default_detector.gemini_dir, Path.home() / ".gemini")
        self.assertEqual(default_detector.proc_dir, Path("/proc"))
        self.assertEqual(default_detector.presence_dir, Path.home() / ".gemini" / "antigravity" / "presence")
        self.assertEqual(default_detector.brain_dir, Path.home() / ".gemini" / "antigravity" / "brain")

    def test_non_digit_proc_entries_ignored(self):
        """Entries in proc_dir that are not numeric PIDs (e.g. cpuinfo, sys) are skipped."""
        (self.proc_dir / "sys").mkdir(parents=True, exist_ok=True)
        (self.proc_dir / "cpuinfo").write_text("cpu info", encoding="utf-8")

        uuid = "proc-ignore-1111-2222-333344445555"
        lock_file = self.presence_dir / f"{uuid}.lock"
        lock_file.touch()

        self._create_mock_proc_process(
            pid=9999,
            cmdline=b"language_server\x00",
            fd_targets=[lock_file],
        )

        session_id = self.detector.get_active_session_id()
        self.assertEqual(session_id, uuid)


class TestCooldownManager(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp_dir.name)
        self.cache_dir = self.base_dir / "cache"
        self.manager = CooldownManager(
            cache_dir=self.cache_dir,
            default_cooldown_seconds=3600,
        )

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_permission_hardening_0600(self):
        """Created cooldown.json file strictly enforces mode 0600 (st_mode & 0o777 == 0o600)."""
        self.manager.set_cooldown("account_test", reset_time=None)
        self.assertTrue(self.manager.cooldown_file.is_file())
        st_mode = self.manager.cooldown_file.stat().st_mode & 0o777
        self.assertEqual(st_mode, 0o600)

    def test_cache_directory_hardening_0700(self):
        """Parent cache directory is hardened to mode 0700."""
        self.manager.set_cooldown("account_test", reset_time=None)
        st_mode = self.cache_dir.stat().st_mode & 0o777
        self.assertEqual(st_mode, 0o700)

    def test_atomic_write_uses_temp_file_and_replace(self):
        """Writes are performed via temporary files and os.replace to guarantee atomicity."""
        with patch("os.replace", wraps=os.replace) as mock_replace:
            self.manager.set_cooldown("acc_atomic", reset_time=None)
            self.assertEqual(mock_replace.call_count, 1)
            src, dst = mock_replace.call_args[0]
            src_path = Path(src)
            dst_path = Path(dst)
            self.assertEqual(dst_path.resolve(), self.manager.cooldown_file.resolve())
            self.assertEqual(src_path.parent.resolve(), self.cache_dir.resolve())
            self.assertTrue(src_path.name.startswith(".cooldown.json.tmp."))

    def test_atomic_write_cleans_up_temp_file_on_write_error(self):
        """If an error occurs during write/fsync, temporary file is cleaned up and not orphaned."""
        with patch("os.fsync", side_effect=OSError("Simulated fsync error")):
            with self.assertRaises(OSError):
                self.manager.set_cooldown("acc_fail", reset_time=None)

        tmp_files = list(self.cache_dir.glob(".cooldown.json.tmp.*"))
        self.assertEqual(tmp_files, [])

    def test_atomic_write_cleans_up_temp_file_on_replace_error(self):
        """If os.replace fails, temporary file is deleted without leaking."""
        with patch("os.replace", side_effect=OSError("Simulated replace error")):
            with self.assertRaises(OSError):
                self.manager.set_cooldown("acc_fail", reset_time=None)

        tmp_files = list(self.cache_dir.glob(".cooldown.json.tmp.*"))
        self.assertEqual(tmp_files, [])

    def test_set_cooldown_with_future_reset_time(self):
        """set_cooldown(account_name, reset_time=...) sets cooldown_until to reset_time when reset_time > now."""
        now = 1700000000.0
        future_reset = now + 1500.0
        with patch("time.time", return_value=now):
            record = self.manager.set_cooldown("acc_dynamic", reset_time=future_reset, reason="RATE_LIMITED")
            self.assertEqual(record["cooldown_until"], future_reset)
            self.assertEqual(record["exhausted_at"], now)
            self.assertEqual(record["reason"], "RATE_LIMITED")
            self.assertTrue(self.manager.is_in_cooldown("acc_dynamic"))

    def test_set_cooldown_with_none_reset_time(self):
        """set_cooldown(account_name, reset_time=None) sets cooldown_until to now + default_cooldown_seconds."""
        now = 1700000000.0
        with patch("time.time", return_value=now):
            record = self.manager.set_cooldown("acc_default", reset_time=None)
            self.assertEqual(record["cooldown_until"], now + 3600.0)
            self.assertEqual(record["exhausted_at"], now)
            self.assertEqual(record["reason"], "RESOURCE_EXHAUSTED")
            self.assertTrue(self.manager.is_in_cooldown("acc_default"))

    def test_set_cooldown_with_past_reset_time(self):
        """set_cooldown with past reset_time defaults to now + default_cooldown_seconds."""
        now = 1700000000.0
        past_reset = now - 500.0
        with patch("time.time", return_value=now):
            record = self.manager.set_cooldown("acc_past", reset_time=past_reset)
            self.assertEqual(record["cooldown_until"], now + 3600.0)
            self.assertTrue(self.manager.is_in_cooldown("acc_past"))

    def test_set_cooldown_with_equal_reset_time(self):
        """set_cooldown with reset_time == now defaults to now + default_cooldown_seconds."""
        now = 1700000000.0
        with patch("time.time", return_value=now):
            record = self.manager.set_cooldown("acc_equal", reset_time=now)
            self.assertEqual(record["cooldown_until"], now + 3600.0)

    def test_set_cooldown_invalid_account_raises_value_error(self):
        """Calling set_cooldown with empty or null account raises ValueError."""
        with self.assertRaises(ValueError):
            self.manager.set_cooldown("")
        with self.assertRaises(ValueError):
            self.manager.set_cooldown(None)

    def test_is_in_cooldown_active_and_expired(self):
        """is_in_cooldown returns True for active cooldowns and False once cooldown_until <= now."""
        now = 1700000000.0
        with patch("time.time", return_value=now):
            self.manager.set_cooldown("acc_test", reset_time=now + 500.0)

        # Active at now + 499
        with patch("time.time", return_value=now + 499.0):
            self.assertTrue(self.manager.is_in_cooldown("acc_test"))

        # Boundary: at exactly cooldown_until (now + 500.0), cooldown_until <= now is True -> cooldown False
        with patch("time.time", return_value=now + 500.0):
            self.assertFalse(self.manager.is_in_cooldown("acc_test"))

        # Expired at now + 501
        with patch("time.time", return_value=now + 501.0):
            self.assertFalse(self.manager.is_in_cooldown("acc_test"))

    def test_is_in_cooldown_non_existent(self):
        """is_in_cooldown returns False for unknown account names or empty string."""
        self.assertFalse(self.manager.is_in_cooldown("unknown_acc"))
        self.assertFalse(self.manager.is_in_cooldown(""))

    def test_get_cooldowns_legacy_flat_float_compatibility(self):
        """get_cooldowns() parses legacy flat float records without error and preserves them until expired."""
        now = 1700000000.0
        legacy_exhausted = now - 1000.0  # Expiring at now - 1000 + 3600 = now + 2600 (still active)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        import json
        self.manager.cooldown_file.write_text(json.dumps({"legacy_acc": legacy_exhausted}), encoding="utf-8")

        with patch("time.time", return_value=now):
            cooldowns = self.manager.get_cooldowns()
            self.assertIn("legacy_acc", cooldowns)
            self.assertEqual(cooldowns["legacy_acc"]["exhausted_at"], legacy_exhausted)
            self.assertEqual(cooldowns["legacy_acc"]["cooldown_until"], legacy_exhausted + 3600.0)
            self.assertEqual(cooldowns["legacy_acc"]["reason"], "RESOURCE_EXHAUSTED")
            self.assertTrue(self.manager.is_in_cooldown("legacy_acc"))

    def test_auto_pruning_expired_records_on_query(self):
        """Expired records are pruned automatically from cooldown.json during queries."""
        now = 1700000000.0
        data = {
            "active_acc": {
                "cooldown_until": now + 600.0,
                "exhausted_at": now - 100.0,
                "reason": "RESOURCE_EXHAUSTED",
            },
            "expired_acc": {
                "cooldown_until": now - 50.0,
                "exhausted_at": now - 3650.0,
                "reason": "RESOURCE_EXHAUSTED",
            },
        }
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        import json
        self.manager.cooldown_file.write_text(json.dumps(data), encoding="utf-8")

        with patch("time.time", return_value=now):
            cooldowns = self.manager.get_cooldowns()
            self.assertIn("active_acc", cooldowns)
            self.assertNotIn("expired_acc", cooldowns)

            # Check that file on disk was pruned
            on_disk = json.loads(self.manager.cooldown_file.read_text(encoding="utf-8"))
            self.assertIn("active_acc", on_disk)
            self.assertNotIn("expired_acc", on_disk)

    def test_auto_pruning_expired_records_on_update(self):
        """Expired records are pruned automatically from cooldown.json during updates."""
        now = 1700000000.0
        data = {
            "expired_acc": {
                "cooldown_until": now - 100.0,
                "exhausted_at": now - 3700.0,
                "reason": "RESOURCE_EXHAUSTED",
            }
        }
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        import json
        self.manager.cooldown_file.write_text(json.dumps(data), encoding="utf-8")

        with patch("time.time", return_value=now):
            self.manager.set_cooldown("new_acc", reset_time=now + 500.0)

            on_disk = json.loads(self.manager.cooldown_file.read_text(encoding="utf-8"))
            self.assertIn("new_acc", on_disk)
            self.assertNotIn("expired_acc", on_disk)

    def test_corrupted_json_handled_gracefully(self):
        """Corrupted JSON in cooldown.json returns {} and does not raise unhandled exceptions."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.manager.cooldown_file.write_text("{corrupted JSON content", encoding="utf-8")

        self.assertEqual(self.manager.get_cooldowns(), {})
        self.assertFalse(self.manager.is_in_cooldown("any_acc"))

    def test_non_dict_json_handled_gracefully(self):
        """Non-dict JSON (array, string, number) returns {} and resets safely."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        import json
        self.manager.cooldown_file.write_text(json.dumps(["item1", "item2"]), encoding="utf-8")

        self.assertEqual(self.manager.get_cooldowns(), {})

        self.manager.cooldown_file.write_text(json.dumps("plain string"), encoding="utf-8")
        self.assertEqual(self.manager.get_cooldowns(), {})

    def test_clear_cooldown_single_and_all(self):
        """clear_cooldown can clear an individual account or all accounts."""
        now = 1700000000.0
        with patch("time.time", return_value=now):
            self.manager.set_cooldown("acc1", reset_time=now + 500.0)
            self.manager.set_cooldown("acc2", reset_time=now + 500.0)

            # Remove single
            removed = self.manager.clear_cooldown("acc1")
            self.assertTrue(removed)
            self.assertFalse(self.manager.is_in_cooldown("acc1"))
            self.assertTrue(self.manager.is_in_cooldown("acc2"))

            # Clear all
            cleared = self.manager.clear_cooldown()
            self.assertTrue(cleared)
            self.assertFalse(self.manager.is_in_cooldown("acc2"))
            self.assertEqual(self.manager.get_cooldowns(), {})

    def test_get_reset_time_from_cache(self):
        """Helper get_reset_time_from_cache extracts future reset_time from last_known_quota.json."""
        now = 1700000000.0
        future_claude = now + 1200.0
        future_gemini = now + 600.0
        quota_data = {
            "accounts": {
                "acc_cached": {
                    "gemini": {"quota": 0, "reset_time": future_gemini},
                    "claude": {"quota": 0, "reset_time": future_claude},
                }
            }
        }
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        import json
        (self.cache_dir / "last_known_quota.json").write_text(json.dumps(quota_data), encoding="utf-8")

        with patch("time.time", return_value=now):
            self.assertEqual(
                self.manager.get_reset_time_from_cache("acc_cached", model_hint="claude"),
                future_claude,
            )
            self.assertEqual(
                self.manager.get_reset_time_from_cache("acc_cached", model_hint="gemini"),
                future_gemini,
            )
            # Default lookup picks earliest future reset time
    def test_get_reset_time_from_cache_with_expired_hint_returns_none(self):
        """When a model_hint is supplied and that model is expired, returns None (no cross-model leakage)."""
        now = 1700000000.0
        quota_data = {
            "accounts": {
                "acc_leak_test": {
                    "gemini": {"quota": 0, "reset_time": now - 100.0},  # expired
                    "claude": {"quota": 100, "reset_time": now + 3600.0},  # in future
                }
            }
        }
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        (self.cache_dir / "last_known_quota.json").write_text(json.dumps(quota_data), encoding="utf-8")

        with patch("time.time", return_value=now):
            # Model hint specified for gemini (expired) must return None, NOT claude's reset time
            self.assertIsNone(
                self.manager.get_reset_time_from_cache("acc_leak_test", model_hint="gemini")
            )

    def test_transient_oserror_does_not_wipe_cooldown_file(self):
        """A transient OSError while reading cooldown.json must return {} in memory without wiping disk file."""
        now = 1700000000.0
        data = {
            "important_acc": {
                "cooldown_until": now + 1000.0,
                "exhausted_at": now - 100.0,
                "reason": "RESOURCE_EXHAUSTED",
            }
        }
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.manager.cooldown_file.write_text(json.dumps(data), encoding="utf-8")

        with patch.object(Path, "read_text", side_effect=OSError("Disk busy")):
            res = self.manager.get_cooldowns()
            self.assertEqual(res, {})

        # Disk file must remain completely intact and un-wiped
        disk_content = json.loads(self.manager.cooldown_file.read_text(encoding="utf-8"))
        self.assertIn("important_acc", disk_content)

    def test_schema_repair_missing_fields_persisted_to_disk(self):
        """Records missing fields are normalized and written back to disk."""
        now = 1700000000.0
        partial_data = {
            "acc_partial": {
                "cooldown_until": now + 500.0
                # missing exhausted_at and reason
            }
        }
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.manager.cooldown_file.write_text(json.dumps(partial_data), encoding="utf-8")

        with patch("time.time", return_value=now):
            cooldowns = self.manager.get_cooldowns()
            self.assertIn("acc_partial", cooldowns)
            self.assertEqual(cooldowns["acc_partial"]["reason"], "RESOURCE_EXHAUSTED")
            self.assertEqual(cooldowns["acc_partial"]["exhausted_at"], now)

            # Check that file on disk was repaired
            on_disk = json.loads(self.manager.cooldown_file.read_text(encoding="utf-8"))
            self.assertEqual(on_disk["acc_partial"]["reason"], "RESOURCE_EXHAUSTED")

    def test_non_finite_float_and_whitespace_account_names(self):
        """Non-finite floats (inf/nan) and invalid account names are rejected or pruned."""
        with self.assertRaises(ValueError):
            self.manager.set_cooldown("   ")

        now = 1700000000.0
        with patch("time.time", return_value=now):
            # reset_time as inf falls back to now + default_cooldown_seconds
            rec = self.manager.set_cooldown("acc_inf", reset_time=float("inf"))
            self.assertEqual(rec["cooldown_until"], now + 3600.0)

    def test_iso_timestamp_in_reset_time(self):
        """ISO-8601 strings in reset_time are parsed correctly."""
        now = 1700000000.0
        iso_str = "2026-10-14T10:00:00Z"
        from datetime import datetime, timezone
        expected_ts = datetime.fromisoformat("2026-10-14T10:00:00+00:00").timestamp()

        with patch("time.time", return_value=now):
            if expected_ts > now:
                rec = self.manager.set_cooldown("acc_iso", reset_time=iso_str)
                self.assertEqual(rec["cooldown_until"], expected_ts)


class TestLogWatcherCooldownIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self.tmp_dir.name) / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cooldown_mgr = CooldownManager(self.cache_dir)

        self.engine_mock = MagicMock()
        self.engine_mock.cache_dir = self.cache_dir
        self.watcher = LogWatcher(
            engine=self.engine_mock,
            cooldown_manager=self.cooldown_mgr,
            log_dirs=[str(self.tmp_dir.name)],
        )

    def tearDown(self):
        self.tmp_dir.cleanup()

    @patch("scripts.watch.time.sleep", return_value=None)
    @patch("scripts.watch.RotationEngine")
    def test_trigger_switch_sets_cooldown_with_model_hint_and_reason(self, mock_rot_cls, mock_sleep):
        """trigger_switch passes model hint to reset cache query and parses reason."""
        now = 1700000000.0
        future_claude = now + 1800.0
        quota_data = {
            "accounts": {
                "acc1": {
                    "claude": {"quota": 0, "reset_time": future_claude},
                    "gemini": {"quota": 80, "reset_time": now + 600.0},
                }
            }
        }
        (self.cache_dir / "last_known_quota.json").write_text(json.dumps(quota_data), encoding="utf-8")

        self.engine_mock.get_active_name.return_value = "acc1"
        self.engine_mock.list_accounts.return_value = {
            "active": "acc1",
            "accounts": [
                {"name": "acc1", "valid": True},
                {"name": "acc2", "valid": True},
            ],
        }

        mock_rot_instance = MagicMock()
        mock_rot_instance.evaluate.return_value = "acc2"
        mock_rot_cls.return_value = mock_rot_instance

        with patch("time.time", return_value=now):
            res = self.watcher.trigger_switch(
                line="Error: Individual quota reached for claude-3-5-sonnet"
            )
            self.assertTrue(res)

            # Check that acc1 was placed in cooldown until future_claude with parsed reason
            cooldowns = self.cooldown_mgr.get_cooldowns()
            self.assertIn("acc1", cooldowns)
            self.assertEqual(cooldowns["acc1"]["cooldown_until"], future_claude)
            self.assertEqual(cooldowns["acc1"]["reason"], "Individual quota reached")

    @patch("scripts.watch.time.sleep", return_value=None)
    @patch("scripts.watch.RotationEngine")
    def test_trigger_switch_filters_candidates_in_cooldown(self, mock_rot_cls, mock_sleep):
        """Candidates currently in cooldown are excluded before evaluate is called."""
        now = 1700000000.0
        self.engine_mock.get_active_name.return_value = "acc1"
        self.engine_mock.list_accounts.return_value = {
            "active": "acc1",
            "accounts": [
                {"name": "acc1", "valid": True},
                {"name": "acc2", "valid": True},  # in cooldown
                {"name": "acc3", "valid": True},  # available
            ],
        }

        # Place acc2 in active cooldown
        with patch("time.time", return_value=now):
            self.cooldown_mgr.set_cooldown("acc2", reset_time=now + 500.0)

        mock_rot_instance = MagicMock()
        mock_rot_instance.evaluate.return_value = "acc3"
        mock_rot_cls.return_value = mock_rot_instance

        with patch("time.time", return_value=now):
            res = self.watcher.trigger_switch(line="RESOURCE_EXHAUSTED (code 429)")
            self.assertTrue(res)

            # Verify acc2 was NOT passed in valid_accounts to rot_engine.evaluate
            eval_args, _ = mock_rot_instance.evaluate.call_args
            candidate_names = [a["name"] for a in eval_args[0]]
            self.assertIn("acc3", candidate_names)
            self.assertNotIn("acc2", candidate_names)
            self.assertNotIn("acc1", candidate_names)

    @patch("scripts.watch.time.sleep", return_value=None)
    def test_trigger_switch_returns_false_when_all_standby_in_cooldown(self, mock_sleep):
        """trigger_switch returns False when all other standby accounts are in cooldown."""
        now = 1700000000.0
        self.engine_mock.get_active_name.return_value = "acc1"
        self.engine_mock.list_accounts.return_value = {
            "active": "acc1",
            "accounts": [
                {"name": "acc1", "valid": True},
                {"name": "acc2", "valid": True},
            ],
        }

        # Place acc2 in cooldown
        with patch("time.time", return_value=now):
            self.cooldown_mgr.set_cooldown("acc2", reset_time=now + 500.0)

        with patch("time.time", return_value=now):
            res = self.watcher.trigger_switch(line="RESOURCE_EXHAUSTED (code 429)")
            self.assertFalse(res)


if __name__ == "__main__":
    unittest.main()
