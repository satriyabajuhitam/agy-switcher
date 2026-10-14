import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.watch import PresenceDetector


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


if __name__ == "__main__":
    unittest.main()
