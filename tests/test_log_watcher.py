import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.watch import (
    PresenceDetector,
    CooldownManager,
    LogWatcher,
    DaemonManager,
    DaemonError,
    DaemonAlreadyRunningError,
)


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

    def test_get_active_transcript_path_with_session_id_bypasses_proc_scan(self):
        """When session_id is provided, get_active_transcript_path reuses it and bypasses /proc scans."""
        uuid = "reuse-1111-2222-3333-444455556666"
        session_brain = self.brain_dir / uuid
        session_brain.mkdir(parents=True, exist_ok=True)
        expected_transcript = session_brain / ".system_generated" / "logs" / "transcript.jsonl"

        with patch.object(self.detector, "get_active_session_id") as mock_get_session:
            with patch.object(self.detector, "_discover_from_proc") as mock_proc:
                res = self.detector.get_active_transcript_path(session_id=uuid)
                self.assertEqual(res, expected_transcript)
                mock_get_session.assert_not_called()
                mock_proc.assert_not_called()

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
        """Returns active transcript path (if existing) and strictly omits antigravity-cli/log/*.log files."""
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

        # Target list must include transcript and omit raw cli logs as Paths
        self.assertEqual(targets, [transcript_file])
        self.assertNotIn(cli_log1, targets)
        self.assertNotIn(cli_log2, targets)
        self.assertNotIn(cli_txt, targets)
        self.assertNotIn(old_invalid_dir / "fake.log", targets)

        # Also verify string output support if requested
        str_targets = self.detector.get_watch_targets(as_str=True)
        self.assertEqual(str_targets, [str(transcript_file)])

    def test_watch_targets_when_transcript_not_created_yet(self):
        """If session dir exists but transcript.jsonl has not yet been created, omit transcript and return empty."""
        uuid = "ffffffff-1111-2222-3333-444444444444"
        (self.presence_dir / f"{uuid}.lock").touch()

        session_brain = self.brain_dir / uuid
        session_brain.mkdir(parents=True, exist_ok=True)
        # transcript.jsonl is not touched

        cli_log = self.cli_log_dir / "active.log"
        cli_log.touch()

        targets = self.detector.get_watch_targets()
        self.assertEqual(targets, [])

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

    def test_dual_directory_init_attributes(self):
        self.assertEqual(
            self.detector.cli_presence_dir,
            self.gemini_dir / "antigravity-cli" / "presence",
        )
        self.assertEqual(
            self.detector.cli_brain_dir,
            self.gemini_dir / "antigravity-cli" / "brain",
        )
        self.assertEqual(
            self.detector.cli_log_dir,
            self.gemini_dir / "antigravity-cli" / "log",
        )

    def test_dual_directory_tier1_cli_proc_fd(self):
        """[REQ-2-SCN-001]: GIVEN an Antigravity CLI process holds an open fd to cli presence lock,
        WHEN get_active_session_id() is called, THEN it returns the cli session ID via Tier 1.
        """
        cli_presence_dir = self.gemini_dir / "antigravity-cli" / "presence"
        cli_presence_dir.mkdir(parents=True, exist_ok=True)
        cli_session = "cli-session-123"
        lock_file = cli_presence_dir / f"{cli_session}.lock"
        lock_file.touch()

        self._create_mock_proc_process(
            pid=4001,
            cmdline=b"antigravity-cli\x00",
            fd_targets=[lock_file],
        )

        session_id = self.detector.get_active_session_id()
        self.assertEqual(session_id, cli_session)

    def test_dual_directory_tier1_multiple_processes_newest_mtime(self):
        """[REQ-2-SCN-001/002]: GIVEN Desktop process with old lock and CLI process with new lock,
        WHEN get_active_session_id() is called, THEN Tier 1 selects the newer CLI session ID.
        """
        cli_presence_dir = self.gemini_dir / "antigravity-cli" / "presence"
        cli_presence_dir.mkdir(parents=True, exist_ok=True)

        desk_lock = self.presence_dir / "desk-session.lock"
        cli_lock = cli_presence_dir / "cli-session.lock"

        desk_lock.touch()
        os.utime(desk_lock, (1000.0, 1000.0))

        cli_lock.touch()
        os.utime(cli_lock, (2000.0, 2000.0))

        self._create_mock_proc_process(
            pid=4001,
            cmdline=b"language_server\x00",
            fd_targets=[desk_lock],
        )
        self._create_mock_proc_process(
            pid=4002,
            cmdline=b"antigravity-cli\x00",
            fd_targets=[cli_lock],
        )

        session_id = self.detector.get_active_session_id()
        self.assertEqual(session_id, "cli-session")

    def test_dual_directory_tier1_multiple_processes_deterministic_tie_breaking(self):
        """[REQ-2-EDGE-002]: GIVEN concurrent processes holding locks with identical mtime,
        WHEN get_active_session_id() is called, THEN tie is resolved via lexical stem ordering.
        """
        cli_presence_dir = self.gemini_dir / "antigravity-cli" / "presence"
        cli_presence_dir.mkdir(parents=True, exist_ok=True)

        desk_lock = self.presence_dir / "session-desk.lock"
        cli_lock = cli_presence_dir / "session-cli.lock"

        desk_lock.touch()
        os.utime(desk_lock, (1000.0, 1000.0))

        cli_lock.touch()
        os.utime(cli_lock, (1000.0, 1000.0))

        self._create_mock_proc_process(
            pid=4001,
            cmdline=b"language_server\x00",
            fd_targets=[desk_lock],
        )
        self._create_mock_proc_process(
            pid=4002,
            cmdline=b"antigravity-cli\x00",
            fd_targets=[cli_lock],
        )

        session_id = self.detector.get_active_session_id()
        self.assertEqual(session_id, "session-cli")

    def test_dual_directory_tier1_directory_and_non_absolute_symlink_ignored(self):
        """[REQ-2-EDGE-003]: GIVEN process holds fds to directory.lock and non-absolute targets,
        WHEN get_active_session_id() is called, THEN invalid targets are skipped in Tier 1.
        """
        cli_presence_dir = self.gemini_dir / "antigravity-cli" / "presence"
        cli_presence_dir.mkdir(parents=True, exist_ok=True)

        dir_lock = cli_presence_dir / "dir-session.lock"
        dir_lock.mkdir()

        valid_lock = cli_presence_dir / "valid-session.lock"
        valid_lock.touch()

        pid_dir = self.proc_dir / "4005"
        pid_dir.mkdir(parents=True, exist_ok=True)
        (pid_dir / "cmdline").write_bytes(b"antigravity\x00")
        fd_dir = pid_dir / "fd"
        fd_dir.mkdir(parents=True, exist_ok=True)

        os.symlink(str(dir_lock), str(fd_dir / "1"))
        os.symlink("relative.lock", str(fd_dir / "2"))
        os.symlink(str(valid_lock), str(fd_dir / "3"))

        session_id = self.detector.get_active_session_id()
        self.assertEqual(session_id, "valid-session")

    def test_dual_directory_tier1_inaccessible_proc_falls_back_to_tier2(self):
        """[REQ-2-ERR-001]: GIVEN proc_dir raises PermissionError on iterdir,
        WHEN get_active_session_id() is called, THEN it safely falls back to Tier 2 newest mtime.
        """
        cli_presence_dir = self.gemini_dir / "antigravity-cli" / "presence"
        cli_presence_dir.mkdir(parents=True, exist_ok=True)

        desk_lock = self.presence_dir / "desk-session.lock"
        cli_lock = cli_presence_dir / "cli-session.lock"

        desk_lock.touch()
        os.utime(desk_lock, (1000.0, 1000.0))

        cli_lock.touch()
        os.utime(cli_lock, (2000.0, 2000.0))

        original_iterdir = Path.iterdir

        def selective_iterdir(path_obj):
            if path_obj == self.proc_dir:
                raise PermissionError("Access denied to proc")
            return original_iterdir(path_obj)

        with patch.object(Path, "iterdir", selective_iterdir):
            session_id = self.detector.get_active_session_id()
            self.assertEqual(session_id, "cli-session")

    def test_dual_directory_tier2_newest_mtime(self):
        """[REQ-2-SCN-002]: GIVEN Desktop lock with mtime 1000 and CLI lock with mtime 2000,
        WHEN get_active_session_id() evaluates presence locks, THEN it selects cli-session.
        """
        cli_presence_dir = self.gemini_dir / "antigravity-cli" / "presence"
        cli_presence_dir.mkdir(parents=True, exist_ok=True)

        desk_lock = self.presence_dir / "desk-session.lock"
        cli_lock = cli_presence_dir / "cli-session.lock"

        desk_lock.touch()
        os.utime(desk_lock, (1000.0, 1000.0))

        cli_lock.touch()
        os.utime(cli_lock, (2000.0, 2000.0))

        session_id = self.detector.get_active_session_id()
        self.assertEqual(session_id, "cli-session")

    def test_dual_directory_deterministic_tie_breaking(self):
        """[REQ-2-EDGE-002]: GIVEN Desktop lock session-desk.lock and CLI lock session-cli.lock
        with identical mtime 1000.0, WHEN get_active_session_id() is called,
        THEN tie is resolved deterministically via lexical ordering on session stem.
        """
        cli_presence_dir = self.gemini_dir / "antigravity-cli" / "presence"
        cli_presence_dir.mkdir(parents=True, exist_ok=True)

        desk_lock = self.presence_dir / "session-desk.lock"
        cli_lock = cli_presence_dir / "session-cli.lock"

        desk_lock.touch()
        os.utime(desk_lock, (1000.0, 1000.0))

        cli_lock.touch()
        os.utime(cli_lock, (1000.0, 1000.0))

        session_id = self.detector.get_active_session_id()
        self.assertEqual(session_id, "session-cli")

    def test_dual_directory_permission_error_fallback(self):
        """[REQ-2-ERR-001]: GIVEN cli_presence_dir raises PermissionError on listing,
        WHEN get_active_session_id() is called, THEN it safely falls back to Desktop lock.
        """
        desk_lock = self.presence_dir / "fallback-desk.lock"
        desk_lock.touch()

        cli_presence_dir = self.gemini_dir / "antigravity-cli" / "presence"
        cli_presence_dir.mkdir(parents=True, exist_ok=True)

        original_iterdir = Path.iterdir

        def selective_iterdir(path_obj):
            if "antigravity-cli" in str(path_obj):
                raise PermissionError("Access denied to CLI presence")
            return original_iterdir(path_obj)

        with patch.object(Path, "iterdir", selective_iterdir):
            session_id = self.detector.get_active_session_id()
            self.assertEqual(session_id, "fallback-desk")

    def test_dual_directory_malformed_lock_names(self):
        """[REQ-2-EDGE-003]: GIVEN a presence directory contains .lock (empty stem) or non-lock,
        WHEN get_active_session_id() is called, THEN empty stems are filtered out.
        """
        cli_presence_dir = self.gemini_dir / "antigravity-cli" / "presence"
        cli_presence_dir.mkdir(parents=True, exist_ok=True)

        (cli_presence_dir / ".lock").touch()
        (cli_presence_dir / "valid-cli.lock").touch()
        (self.presence_dir / "notes.txt").touch()

        session_id = self.detector.get_active_session_id()
        self.assertEqual(session_id, "valid-cli")

    def test_dual_directory_relative_traversal_symlink_filtered(self):
        """[REQ-2-EDGE-003]: GIVEN a presence directory contains a relative traversal symlink,
        WHEN get_active_session_id() is called, THEN the traversal symlink is rejected.
        """
        cli_presence_dir = self.gemini_dir / "antigravity-cli" / "presence"
        cli_presence_dir.mkdir(parents=True, exist_ok=True)

        external_file = self.gemini_dir / "external.txt"
        external_file.touch()
        os.utime(external_file, (3000.0, 3000.0))

        traversal_symlink = cli_presence_dir / "traversal-attack.lock"
        traversal_symlink.symlink_to("../../external.txt")

        valid_lock = cli_presence_dir / "valid-session.lock"
        valid_lock.touch()
        os.utime(valid_lock, (1000.0, 1000.0))

        session_id = self.detector.get_active_session_id()
        self.assertEqual(session_id, "valid-session")

    def test_dual_directory_dot_prefixed_stems_filtered(self):
        """[REQ-2-EDGE-003]: GIVEN presence directory contains dot-prefixed stems,
        WHEN get_active_session_id() is called, THEN hidden and dot-prefixed stems are ignored.
        """
        cli_presence_dir = self.gemini_dir / "antigravity-cli" / "presence"
        cli_presence_dir.mkdir(parents=True, exist_ok=True)

        (cli_presence_dir / ".hidden.lock").touch()
        (cli_presence_dir / "..traversal.lock").touch()
        valid_lock = cli_presence_dir / "valid-regular.lock"
        valid_lock.touch()

        session_id = self.detector.get_active_session_id()
        self.assertEqual(session_id, "valid-regular")

    def test_transcript_targets_cli_exclusive(self):
        """[REQ-2-SCN-004]: GIVEN an active session ID 'cli-uuid-456' whose transcript exists exclusively
        at ~/.gemini/antigravity-cli/brain/cli-uuid-456/.system_generated/logs/transcript.jsonl,
        WHEN PresenceDetector.get_active_transcript_path('cli-uuid-456') is called,
        THEN it returns the Path pointing to the CLI transcript file.
        """
        cli_uuid = "cli-uuid-456"
        cli_transcript_dir = self.detector.cli_brain_dir / cli_uuid / ".system_generated" / "logs"
        cli_transcript_dir.mkdir(parents=True, exist_ok=True)
        cli_transcript = cli_transcript_dir / "transcript.jsonl"
        cli_transcript.write_text('{"event": "cli_start"}\n', encoding="utf-8")

        result = self.detector.get_active_transcript_path(cli_uuid)
        self.assertEqual(result, cli_transcript)

        cli_presence = self.detector.cli_presence_dir / f"{cli_uuid}.lock"
        cli_presence.parent.mkdir(parents=True, exist_ok=True)
        cli_presence.touch()

        auto_result = self.detector.get_active_transcript_path()
        self.assertEqual(auto_result, cli_transcript)

    def test_transcript_targets_concurrent_mtime(self):
        """[REQ-2-SCN-004]: GIVEN session 'hybrid-sess' exists in both Desktop brain/ (mtime 1500.0)
        and CLI brain/ (mtime 2500.0), WHEN PresenceDetector.get_active_transcript_path('hybrid-sess') is called,
        THEN the returned path points to the CLI transcript file with mtime 2500.0.
        """
        session_id = "hybrid-sess"
        desk_dir = self.detector.brain_dir / session_id / ".system_generated" / "logs"
        desk_dir.mkdir(parents=True, exist_ok=True)
        desk_transcript = desk_dir / "transcript.jsonl"
        desk_transcript.write_text('{"event": "desktop"}\n', encoding="utf-8")
        os.utime(desk_transcript, (1500.0, 1500.0))

        cli_dir = self.detector.cli_brain_dir / session_id / ".system_generated" / "logs"
        cli_dir.mkdir(parents=True, exist_ok=True)
        cli_transcript = cli_dir / "transcript.jsonl"
        cli_transcript.write_text('{"event": "cli"}\n', encoding="utf-8")
        os.utime(cli_transcript, (2500.0, 2500.0))

        result = self.detector.get_active_transcript_path(session_id)
        self.assertEqual(result, cli_transcript)

        os.utime(desk_transcript, (3500.0, 3500.0))
        result2 = self.detector.get_active_transcript_path(session_id)
        self.assertEqual(result2, desk_transcript)

    def test_transcript_targets_deduplication(self):
        """[REQ-2-SCN-006]: GIVEN an active CLI transcript file exists and multiple .log files exist
        in ~/.gemini/antigravity-cli/log/, WHEN PresenceDetector.get_watch_targets() is invoked,
        THEN the returned list contains exclusively the CLI transcript path and omits raw .log files.
        """
        session_id = "cli-dedup-sess"
        cli_dir = self.detector.cli_brain_dir / session_id / ".system_generated" / "logs"
        cli_dir.mkdir(parents=True, exist_ok=True)
        cli_transcript = cli_dir / "transcript.jsonl"
        cli_transcript.write_text('{"event": "cli_entry"}\n', encoding="utf-8")

        cli_lock = self.detector.cli_presence_dir / f"{session_id}.lock"
        cli_lock.parent.mkdir(parents=True, exist_ok=True)
        cli_lock.touch()

        log1 = self.detector.cli_log_dir / "agent.log"
        log2 = self.detector.cli_log_dir / "runner.log"
        log1.write_text("agent log", encoding="utf-8")
        log2.write_text("runner log", encoding="utf-8")

        targets_path = self.detector.get_watch_targets(as_str=False)
        self.assertEqual(targets_path, [cli_transcript])
        self.assertNotIn(log1, targets_path)
        self.assertNotIn(log2, targets_path)

        targets_str = self.detector.get_watch_targets(as_str=True)
        self.assertEqual(targets_str, [str(cli_transcript)])

    def test_transcript_targets_unreadable_omitted_safely(self):
        """[REQ-2-ERR-002]: GIVEN an active session ID is discovered,
        AND the transcript file raises PermissionError or is unlinked when queried with is_file(),
        WHEN PresenceDetector.get_watch_targets() is called,
        THEN the unreadable transcript is omitted without raising an unhandled exception.
        """
        session_id = "unreadable-sess"
        cli_dir = self.detector.cli_brain_dir / session_id / ".system_generated" / "logs"
        cli_dir.mkdir(parents=True, exist_ok=True)
        cli_transcript = cli_dir / "transcript.jsonl"
        cli_transcript.touch()

        cli_lock = self.detector.cli_presence_dir / f"{session_id}.lock"
        cli_lock.parent.mkdir(parents=True, exist_ok=True)
        cli_lock.touch()

        original_is_file = Path.is_file

        def selective_is_file(path_obj):
            if "transcript.jsonl" in str(path_obj):
                raise PermissionError("Access denied to transcript")
            return original_is_file(path_obj)

        with patch.object(Path, "is_file", selective_is_file):
            targets = self.detector.get_watch_targets()
            self.assertNotIn(cli_transcript, targets)


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
            self.assertEqual(
                self.manager.get_reset_time_from_cache("acc_cached"),
                future_gemini,
            )

    def test_get_reset_time_from_flat_cache_schema(self):
        """Flat cache schema {"quota": 0, "reset_time": future_time} extracts future reset_time correctly."""
        now = 1700000000.0
        future_reset = now + 1800.0
        quota_data = {
            "accounts": {
                "acc_flat": {
                    "quota": 0,
                    "reset_time": future_reset,
                },
                "acc_flat_expired": {
                    "quota": 0,
                    "reset_time": now - 100.0,
                },
            }
        }
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        (self.cache_dir / "last_known_quota.json").write_text(json.dumps(quota_data), encoding="utf-8")

        with patch("time.time", return_value=now):
            self.assertEqual(
                self.manager.get_reset_time_from_cache("acc_flat"),
                future_reset,
            )
            self.assertIsNone(
                self.manager.get_reset_time_from_cache("acc_flat_expired")
            )
            self.assertIsNone(
                self.manager.get_reset_time_from_cache("non_existent_acc")
            )

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

    @patch("scripts.watch.time.sleep", return_value=None)
    @patch("scripts.watch.RotationEngine")
    def test_trigger_switch_aborts_when_rotation_engine_evaluates_none(self, mock_rot_cls, mock_sleep):
        """When rotation engine evaluate returns None, trigger_switch aborts without switching."""
        now = 1700000000.0
        self.engine_mock.get_active_name.return_value = "acc1"
        self.engine_mock.list_accounts.return_value = {
            "active": "acc1",
            "accounts": [
                {"name": "acc1", "valid": True},
                {"name": "acc2", "valid": True},
            ],
        }

        mock_rot_instance = MagicMock()
        mock_rot_instance.evaluate.return_value = None
        mock_rot_cls.return_value = mock_rot_instance

        stderr_buf = io.StringIO()
        with patch("sys.stderr", stderr_buf):
            with patch("time.time", return_value=now):
                res = self.watcher.trigger_switch(line="RESOURCE_EXHAUSTED (code 429)")

        self.assertFalse(res)
        self.engine_mock.switch_tokens.assert_not_called()
        self.assertIn(
            "[watch] All other valid accounts are exhausted or in cooldown. Cannot switch.",
            stderr_buf.getvalue(),
        )

    @patch("scripts.watch.time.sleep", return_value=None)
    @patch("scripts.watch.RotationEngine")
    def test_trigger_switch_aborts_when_rotation_engine_returns_active_account(self, mock_rot_cls, mock_sleep):
        """When rotation engine evaluate returns active_name, trigger_switch aborts without switching."""
        now = 1700000000.0
        self.engine_mock.get_active_name.return_value = "acc1"
        self.engine_mock.list_accounts.return_value = {
            "active": "acc1",
            "accounts": [
                {"name": "acc1", "valid": True},
                {"name": "acc2", "valid": True},
            ],
        }

        mock_rot_instance = MagicMock()
        mock_rot_instance.evaluate.return_value = "acc1"
        mock_rot_cls.return_value = mock_rot_instance

        stderr_buf = io.StringIO()
        with patch("sys.stderr", stderr_buf):
            with patch("time.time", return_value=now):
                res = self.watcher.trigger_switch(line="RESOURCE_EXHAUSTED (code 429)")

        self.assertFalse(res)
        self.engine_mock.switch_tokens.assert_not_called()
        self.assertIn(
            "[watch] All other valid accounts are exhausted or in cooldown. Cannot switch.",
            stderr_buf.getvalue(),
        )

    @patch("scripts.watch.time.sleep", return_value=None)
    @patch("scripts.watch.RotationEngine")
    def test_trigger_switch_aborts_when_rotation_engine_returns_unknown_account(self, mock_rot_cls, mock_sleep):
        """When rotation engine evaluate returns an unknown account not in valid_accounts, abort without switching."""
        now = 1700000000.0
        self.engine_mock.get_active_name.return_value = "acc1"
        self.engine_mock.list_accounts.return_value = {
            "active": "acc1",
            "accounts": [
                {"name": "acc1", "valid": True},
                {"name": "acc2", "valid": True},
            ],
        }

        mock_rot_instance = MagicMock()
        mock_rot_instance.evaluate.return_value = "unknown_ghost_account"
        mock_rot_cls.return_value = mock_rot_instance

        stderr_buf = io.StringIO()
        with patch("sys.stderr", stderr_buf):
            with patch("time.time", return_value=now):
                res = self.watcher.trigger_switch(line="RESOURCE_EXHAUSTED (code 429)")

        self.assertFalse(res)
        self.engine_mock.switch_tokens.assert_not_called()
        self.assertIn(
            "[watch] All other valid accounts are exhausted or in cooldown. Cannot switch.",
            stderr_buf.getvalue(),
        )

    @patch("scripts.watch.time.sleep", return_value=None)
    def test_trigger_switch_handles_none_accounts_list(self, mock_sleep):
        """When list_accounts returns None accounts, aborts gracefully without throwing TypeError."""
        now = 1700000000.0
        self.engine_mock.get_active_name.return_value = "acc1"
        self.engine_mock.list_accounts.return_value = {
            "active": "acc1",
            "accounts": None,
        }

        stderr_buf = io.StringIO()
        with patch("sys.stderr", stderr_buf):
            with patch("time.time", return_value=now):
                res = self.watcher.trigger_switch(line="RESOURCE_EXHAUSTED (code 429)")

        self.assertFalse(res)
        self.engine_mock.switch_tokens.assert_not_called()
        self.assertIn(
            "[watch] All other valid accounts are in cooldown. Cannot switch.",
            stderr_buf.getvalue(),
        )

    def test_cooldown_clamp_001_strictly_clamps_to_5h_rolling_bucket(self):
        """[COOLDOWN-CLAMP-001]: On HTTP 429, clamps cooldown strictly to 5h rolling bucket reset timestamp."""
        now = 1700000000.0
        rolling_5h_reset = now + 7200.0  # 2 hours in future
        weekly_reset = now + 432000.0    # 5 days in future
        quota_data = {
            "accounts": {
                "primary-acc": {
                    "gemini": {
                        "quota": 0,
                        "reset_time": weekly_reset,
                        "buckets": {
                            "5h": {"quota": 0, "reset_time": rolling_5h_reset},
                            "weekly": {"quota": 0, "reset_time": weekly_reset},
                        },
                    }
                }
            }
        }
        (self.cache_dir / "last_known_quota.json").write_text(json.dumps(quota_data), encoding="utf-8")

        with patch("time.time", return_value=now):
            # Check get_reset_time_from_cache prioritizes 5h rolling bucket
            cache_reset = self.cooldown_mgr.get_reset_time_from_cache("primary-acc", model_hint="gemini")
            self.assertEqual(cache_reset, rolling_5h_reset)

            # Check set_cooldown clamps to 5h rolling bucket
            record = self.watcher.set_cooldown("primary-acc")
            self.assertEqual(record["cooldown_until"], rolling_5h_reset)

            # Verify persisted record in cooldown.json
            on_disk = json.loads(self.cooldown_mgr.cooldown_file.read_text(encoding="utf-8"))
            self.assertIn("primary-acc", on_disk)
            self.assertEqual(on_disk["primary-acc"]["cooldown_until"], rolling_5h_reset)

    def test_cooldown_clamp_003_rejects_weekly_reset_and_caps_at_5h_or_fallback(self):
        """[COOLDOWN-CLAMP-003]: Rejects multi-day weekly reset and caps duration at 5 hours or 30-min fallback."""
        now = 1700000000.0
        weekly_reset = now + 518400.0  # 6 days in future
        quota_data = {
            "accounts": {
                "primary-acc": {
                    "gemini": {
                        "quota": 0,
                        "reset_time": weekly_reset,
                        "buckets": {
                            "weekly": {"quota": 0, "reset_time": weekly_reset},
                        },
                    }
                }
            }
        }
        (self.cache_dir / "last_known_quota.json").write_text(json.dumps(quota_data), encoding="utf-8")

        with patch("time.time", return_value=now):
            # Weekly reset in cache must be rejected as candidate
            cache_reset = self.cooldown_mgr.get_reset_time_from_cache("primary-acc", model_hint="gemini")
            self.assertIsNone(cache_reset)

            # When set_cooldown is invoked, it rejects the weekly reset and caps at <= 18000 (30m fallback)
            record = self.watcher.set_cooldown("primary-acc")
            self.assertNotEqual(record["cooldown_until"], weekly_reset)
            self.assertLessEqual(record["cooldown_until"] - now, 18000.0)
            self.assertEqual(record["cooldown_until"], now + 1800.0)

            # Verify persisted record in cooldown.json
            on_disk = json.loads(self.cooldown_mgr.cooldown_file.read_text(encoding="utf-8"))
            self.assertIn("primary-acc", on_disk)
            self.assertNotEqual(on_disk["primary-acc"]["cooldown_until"], weekly_reset)
            self.assertLessEqual(on_disk["primary-acc"]["cooldown_until"] - now, 18000.0)

            # Even if weekly reset is passed directly as argument, it must be rejected and capped
            record_direct = self.watcher.set_cooldown("primary-acc", reset_time=weekly_reset)
            self.assertNotEqual(record_direct["cooldown_until"], weekly_reset)
            self.assertLessEqual(record_direct["cooldown_until"] - now, 18000.0)

    def test_cooldown_clamp_clock_skew_5h_bucket_clamped_to_5h_ceiling(self):
        """Probes edge case: 5h rolling bucket with duration exceeding 18000s (e.g. 18050s clock skew) is clamped to 5h ceiling."""
        now = 1700000000.0
        rolling_5h_skew = now + 18050.0  # 5 hours and 50 seconds in future
        quota_data = {
            "accounts": {
                "primary-acc": {
                    "gemini": {
                        "quota": 0,
                        "buckets": {
                            "5h": {"quota": 0, "reset_time": rolling_5h_skew},
                        },
                    }
                }
            }
        }
        (self.cache_dir / "last_known_quota.json").write_text(json.dumps(quota_data), encoding="utf-8")

        with patch("time.time", return_value=now):
            # Cache extraction clamps to 18000s ceiling
            cache_reset = self.cooldown_mgr.get_reset_time_from_cache("primary-acc", model_hint="gemini")
            self.assertEqual(cache_reset, now + 18000.0)

            # set_cooldown clamps to exactly now + 18000.0 (5h) instead of dropping to 30m
            record = self.watcher.set_cooldown("primary-acc")
            self.assertEqual(record["cooldown_until"], now + 18000.0)

    def test_cooldown_clamp_non_weekly_bucket_exceeding_5h_clamped_to_5h_ceiling(self):
        """Probes edge case: non-weekly bucket or direct reset_time of 6h (21600s) is clamped to 5h ceiling."""
        now = 1700000000.0
        six_hours_future = now + 21600.0  # 6 hours in future (non-weekly)

        with patch("time.time", return_value=now):
            # Direct reset_time of 6 hours must be clamped to 5h ceiling
            record_direct = self.watcher.set_cooldown("primary-acc", reset_time=six_hours_future)
            self.assertEqual(record_direct["cooldown_until"], now + 18000.0)

            # In cache, a non-weekly bucket with duration > 18000s is clamped to 18000s
            quota_data = {
                "accounts": {
                    "primary-acc": {
                        "gemini": {
                            "quota": 0,
                            "buckets": [
                                {"bucketId": "custom_window", "resetTime": six_hours_future}
                            ],
                        }
                    }
                }
            }
            (self.cache_dir / "last_known_quota.json").write_text(json.dumps(quota_data), encoding="utf-8")
            cache_reset = self.cooldown_mgr.get_reset_time_from_cache("primary-acc", model_hint="gemini")
            self.assertEqual(cache_reset, now + 18000.0)

            record = self.watcher.set_cooldown("primary-acc")
            self.assertEqual(record["cooldown_until"], now + 18000.0)

    def test_cooldown_clamp_empty_none_or_malformed_buckets_graceful_handling(self):
        """Probes edge case: buckets is None, empty list, or contains non-dict items."""
        now = 1700000000.0

        with patch("time.time", return_value=now):
            # Case 1: buckets is None, flat schema with resetTime (camelCase)
            q1 = {"accounts": {"primary-acc": {"gemini": {"quota": 0, "buckets": None, "resetTime": now + 7200.0}}}}
            (self.cache_dir / "last_known_quota.json").write_text(json.dumps(q1), encoding="utf-8")
            self.assertEqual(self.cooldown_mgr.get_reset_time_from_cache("primary-acc", "gemini"), now + 7200.0)

            # Case 2: buckets is empty list, no reset_time -> falls back to 30m
            q2 = {"accounts": {"primary-acc": {"gemini": {"quota": 0, "buckets": []}}}}
            (self.cache_dir / "last_known_quota.json").write_text(json.dumps(q2), encoding="utf-8")
            self.assertIsNone(self.cooldown_mgr.get_reset_time_from_cache("primary-acc", "gemini"))
            rec2 = self.watcher.set_cooldown("primary-acc")
            self.assertEqual(rec2["cooldown_until"], now + 1800.0)

            # Case 3: buckets has invalid/corrupted elements [None, 123, "string"]
            q3 = {"accounts": {"primary-acc": {"gemini": {"quota": 0, "buckets": [None, 123, "string"]}}}}
            (self.cache_dir / "last_known_quota.json").write_text(json.dumps(q3), encoding="utf-8")
            self.assertIsNone(self.cooldown_mgr.get_reset_time_from_cache("primary-acc", "gemini"))

    def test_cooldown_clamp_malformed_past_or_negative_delta_reset_time(self):
        """Probes edge case: malformed, past, or negative-delta reset_time safely defaults to 30m fallback."""
        now = 1700000000.0

        with patch("time.time", return_value=now):
            # Past timestamp
            rec_past = self.watcher.set_cooldown("primary-acc", reset_time=now - 100.0)
            self.assertEqual(rec_past["cooldown_until"], now + 1800.0)

            # Exactly now
            rec_now = self.watcher.set_cooldown("primary-acc", reset_time=now)
            self.assertEqual(rec_now["cooldown_until"], now + 1800.0)

            # Malformed string
            rec_malformed = self.watcher.set_cooldown("primary-acc", reset_time="not-a-timestamp")
            self.assertEqual(rec_malformed["cooldown_until"], now + 1800.0)

            # Negative float
            rec_neg = self.watcher.set_cooldown("primary-acc", reset_time=-500.0)
            self.assertEqual(rec_neg["cooldown_until"], now + 1800.0)

    def test_is_account_cooldown_penalized_clean_robustness(self):
        """Probes _is_account_cooldown_penalized handles None/non-dict and expired cooldown records cleanly."""
        now = 1700000000.0
        with patch("time.time", return_value=now):
            # Non-dict cooldowns
            self.assertFalse(self.watcher._is_account_cooldown_penalized("acc1", None))  # type: ignore[arg-type]
            self.assertFalse(self.watcher._is_account_cooldown_penalized("", {"acc1": {}}))

            # Active cooldown
            self.assertTrue(self.watcher._is_account_cooldown_penalized("acc1", {"acc1": {"cooldown_until": now + 600.0}}))

            # Expired cooldown record
            self.assertFalse(self.watcher._is_account_cooldown_penalized("acc1", {"acc1": {"cooldown_until": now - 10.0}}))


class _StubSwitchEngine:
    """External engine stub for testing LogWatcher at its public seam without internal mocking."""

    def __init__(self, cache_dir: Path, vault_dir: Path, gemini_dir: Path) -> None:
        self.cache_dir = cache_dir
        self.vault_dir = vault_dir
        self.gemini_dir = gemini_dir
        self.active_name_calls = 0
        self.list_accounts_calls = 0
        self.switch_calls: list[tuple[str, bool, bool]] = []

    def get_active_name(self) -> str:
        self.active_name_calls += 1
        return "acc1"

    def list_accounts(self) -> dict[str, object]:
        self.list_accounts_calls += 1
        return {
            "active": "acc1",
            "accounts": [
                {"name": "acc1", "valid": True},
                {"name": "acc2", "valid": True},
            ],
        }

    def switch_tokens(
        self,
        target_account: str,
        *,
        restart_server: bool = False,
        force: bool = False,
    ) -> dict[str, object]:
        self.switch_calls.append((target_account, restart_server, force))
        return {"success": True, "active": target_account, "server_restarted": False}


class TestLogWatcherProcessing(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp_dir.name)
        self.cache_dir = self.base_dir / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.vault_dir = self.base_dir / "vault"
        self.vault_dir.mkdir(parents=True, exist_ok=True)

        self.engine_mock = MagicMock()
        self.engine_mock.cache_dir = self.cache_dir
        self.engine_mock.vault_dir = self.vault_dir
        self.engine_mock.get_active_name.return_value = "acc1"
        self.engine_mock.list_accounts.return_value = {
            "active": "acc1",
            "accounts": [
                {"name": "acc1", "valid": True},
                {"name": "acc2", "valid": True},
            ],
        }

        self.cooldown_mgr = CooldownManager(self.cache_dir)
        self.watcher = LogWatcher(
            engine=self.engine_mock,
            cooldown_manager=self.cooldown_mgr,
            damping_seconds=15.0,
            log_dirs=[str(self.base_dir)],
        )

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_default_log_dirs_contains_only_cli_log_dir(self):
        """LogWatcher.__init__ default self.log_dirs contains only cli_log_dir and does not include antigravity/log."""
        default_watcher = LogWatcher(engine=self.engine_mock)
        expected_cli_log_dir = str(default_watcher.presence_detector.cli_log_dir)
        self.assertEqual(default_watcher.log_dirs, [expected_cli_log_dir])
        self.assertNotIn("antigravity/log", "".join(default_watcher.log_dirs))

    def test_scan_active_targets_passes_session_id_to_get_active_transcript_path(self):
        """scan_active_targets() passes discovered session_id to get_active_transcript_path avoiding duplicate /proc scan."""
        mock_detector = MagicMock()
        mock_detector.get_active_session_id.return_value = "session-dedup"
        mock_detector.get_active_transcript_path.return_value = Path("/dummy/transcript.jsonl")
        mock_detector.get_watch_targets.return_value = ["/dummy/transcript.jsonl"]
        self.watcher.presence_detector = mock_detector

        self.watcher.scan_active_targets()
        mock_detector.get_active_transcript_path.assert_called_with(session_id="session-dedup")

    @patch.object(LogWatcher, "trigger_switch", return_value=True)
    def test_jsonl_error_status_resource_exhausted_triggers_switch(self, mock_switch):
        """Line with status == 'ERROR' and RESOURCE_EXHAUSTED in content triggers rotation."""
        line = json.dumps({
            "status": "ERROR",
            "type": "MODEL",
            "content": "Error: RESOURCE_EXHAUSTED (code 429): Quota limit reached",
        })
        res = self.watcher.process_line(line, is_jsonl=True)
        self.assertTrue(res)
        mock_switch.assert_called_once()

    @patch.object(LogWatcher, "trigger_switch", return_value=True)
    def test_jsonl_error_status_code_429_triggers_switch(self, mock_switch):
        """Line with status == 'ERROR' and code 429 in content triggers rotation."""
        line = json.dumps({
            "status": "ERROR",
            "content": "HTTP error: code 429 Too Many Requests",
        })
        res = self.watcher.process_line(line, is_jsonl=True)
        self.assertTrue(res)
        mock_switch.assert_called_once()

    @patch.object(LogWatcher, "trigger_switch", return_value=True)
    def test_jsonl_error_field_payload_triggers_switch(self, mock_switch):
        """Line with error payload field containing exhaustion error triggers rotation."""
        line = json.dumps({
            "status": "FAILED",
            "error": {"code": 429, "message": "RESOURCE_EXHAUSTED"},
        })
        res = self.watcher.process_line(line, is_jsonl=True)
        self.assertTrue(res)
        mock_switch.assert_called_once()

    @patch.object(LogWatcher, "trigger_switch", return_value=True)
    def test_jsonl_error_message_type_triggers_switch(self, mock_switch):
        """Line with type == 'ERROR_MESSAGE' triggers rotation on exhaustion keywords."""
        line = json.dumps({
            "type": "ERROR_MESSAGE",
            "content": "Individual quota reached for active model",
        })
        res = self.watcher.process_line(line, is_jsonl=True)
        self.assertTrue(res)
        mock_switch.assert_called_once()

    @patch.object(LogWatcher, "trigger_switch", return_value=True)
    def test_jsonl_weekly_quota_triggers_switch(self, mock_switch):
        """Line with status == 'ERROR' and 'weekly quota reached' triggers rotation."""
        line = json.dumps({
            "status": "ERROR",
            "content": "weekly quota reached, resets on Monday",
        })
        res = self.watcher.process_line(line, is_jsonl=True)
        self.assertTrue(res)
        mock_switch.assert_called_once()

    @patch.object(LogWatcher, "trigger_switch", return_value=True)
    def test_jsonl_immunity_done_status_with_exhaustion_text(self, mock_switch):
        """Line with status == 'DONE' DOES NOT trigger rotation even if content contains RESOURCE_EXHAUSTED."""
        line = json.dumps({
            "status": "DONE",
            "type": "PLANNER_RESPONSE",
            "content": "The user asked how to handle RESOURCE_EXHAUSTED and code 429 errors.",
        })
        res = self.watcher.process_line(line, is_jsonl=True)
        self.assertFalse(res)
        mock_switch.assert_not_called()

    @patch.object(LogWatcher, "trigger_switch", return_value=True)
    def test_jsonl_immunity_user_input_type_with_exhaustion_text(self, mock_switch):
        """Line with type == 'USER_INPUT' DOES NOT trigger rotation even if content mentions quota exhaustion."""
        line = json.dumps({
            "type": "USER_INPUT",
            "status": "ERROR",  # Even if status is somehow ERROR, USER_INPUT is immune
            "content": "Why did my previous request say RESOURCE_EXHAUSTED?",
        })
        res = self.watcher.process_line(line, is_jsonl=True)
        self.assertFalse(res)
        mock_switch.assert_not_called()

    def _build_isolated_watcher(
        self,
    ) -> tuple[LogWatcher, _StubSwitchEngine, CooldownManager, Path]:
        """Builds a LogWatcher wired only to isolated temp files and an external engine stub."""
        gemini_dir = self.base_dir / ".gemini_immunity"
        gemini_dir.mkdir(parents=True, exist_ok=True)
        (self.cache_dir / "last_known_quota.json").write_text(
            json.dumps({
                "timestamp": 1700000000.0,
                "accounts": {
                    "acc1": {"gemini": {"quota": 0, "reset_time": "2026-11-11T12:00:00Z"}},
                    "acc2": {"gemini": {"quota": 90, "reset_time": "2026-11-11T12:00:00Z"}},
                },
            }),
            encoding="utf-8",
        )
        engine_stub = _StubSwitchEngine(
            cache_dir=self.cache_dir,
            vault_dir=self.vault_dir,
            gemini_dir=gemini_dir,
        )
        detector = PresenceDetector(
            gemini_dir=gemini_dir,
            proc_dir=self.base_dir / "nonexistent_proc",
        )
        cooldown_mgr = CooldownManager(self.cache_dir)
        watcher = LogWatcher(
            engine=engine_stub,
            cooldown_manager=cooldown_mgr,
            damping_seconds=15.0,
            presence_detector=detector,
        )
        return watcher, engine_stub, cooldown_mgr, gemini_dir

    def test_process_line_immunity_done_and_user_input_done_status(self) -> None:
        """[WATCH-BOUND-004] Records with status DONE are granted immediate immunity without calling handle_exhaustion."""
        done_variants: list[dict[str, str]] = [
            {"status": "DONE", "content": "Found 3 matches for RESOURCE_EXHAUSTED in codebase"},
            {
                "status": "DONE",
                "type": "ERROR_MESSAGE",
                "error": "RESOURCE_EXHAUSTED (code 429)",
                "message": "429 Too Many Requests",
                "content": "Found 3 matches for RESOURCE_EXHAUSTED in codebase",
            },
            {
                "status": "done",
                "error": "RESOURCE_EXHAUSTED (code 429)",
                "content": "Found 3 matches for RESOURCE_EXHAUSTED in codebase",
            },
            {
                "status": " DONE ",
                "error": "RESOURCE_EXHAUSTED (code 429)",
                "content": "Found 3 matches for RESOURCE_EXHAUSTED in codebase",
            },
        ]

        with patch("shutil.which", return_value=None):
            for payload in done_variants:
                line = json.dumps(payload)
                for is_jsonl in (True, False):
                    with self.subTest(payload=payload, is_jsonl=is_jsonl):
                        watcher, engine_stub, cooldown_mgr, _ = self._build_isolated_watcher()
                        self.assertFalse(watcher.process_line(line, is_jsonl=is_jsonl))
                        self.assertEqual(engine_stub.switch_calls, [])
                        self.assertEqual(engine_stub.active_name_calls, 0)
                        self.assertEqual(engine_stub.list_accounts_calls, 0)
                        self.assertEqual(cooldown_mgr.get_cooldowns(), {})
                        self.assertFalse((self.vault_dir / "watch.log").exists())

    def test_process_line_immunity_done_and_user_input_user_input_type(self) -> None:
        """[WATCH-BOUND-005] Records with type USER_INPUT are suppressed immediately without calling handle_exhaustion."""
        user_input_variants: list[dict[str, str]] = [
            {"type": "USER_INPUT", "content": "How do I handle RESOURCE_EXHAUSTED and code 429?"},
            {
                "type": "USER_INPUT",
                "status": "ERROR",
                "error": "RESOURCE_EXHAUSTED (code 429)",
                "message": "Individual quota reached",
                "content": "How do I handle RESOURCE_EXHAUSTED and code 429?",
            },
            {
                "type": "user_input",
                "status": "ERROR",
                "error": "RESOURCE_EXHAUSTED (code 429)",
                "content": "How do I handle RESOURCE_EXHAUSTED and code 429?",
            },
            {
                "type": " USER_INPUT ",
                "status": "ERROR",
                "error": "RESOURCE_EXHAUSTED (code 429)",
                "content": "How do I handle RESOURCE_EXHAUSTED and code 429?",
            },
        ]

        with patch("shutil.which", return_value=None):
            for payload in user_input_variants:
                line = json.dumps(payload)
                for is_jsonl in (True, False):
                    with self.subTest(payload=payload, is_jsonl=is_jsonl):
                        watcher, engine_stub, cooldown_mgr, _ = self._build_isolated_watcher()
                        self.assertFalse(watcher.process_line(line, is_jsonl=is_jsonl))
                        self.assertEqual(engine_stub.switch_calls, [])
                        self.assertEqual(engine_stub.active_name_calls, 0)
                        self.assertEqual(engine_stub.list_accounts_calls, 0)
                        self.assertEqual(cooldown_mgr.get_cooldowns(), {})
                        self.assertFalse((self.vault_dir / "watch.log").exists())

    def test_process_line_immunity_done_and_user_input_transcript_tailing(self) -> None:
        """[WATCH-BOUND-004][WATCH-BOUND-005] poll_once ignores DONE and USER_INPUT lines before rotating on real ERROR."""
        watcher, engine_stub, cooldown_mgr, gemini_dir = self._build_isolated_watcher()

        session_id = "immunity-session-01"
        lock_file = gemini_dir / "antigravity" / "presence" / f"{session_id}.lock"
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        lock_file.touch()

        transcript_file = (
            gemini_dir / "antigravity" / "brain" / session_id / ".system_generated" / "logs" / "transcript.jsonl"
        )
        transcript_file.parent.mkdir(parents=True, exist_ok=True)
        transcript_file.write_text('{"type": "START"}\n', encoding="utf-8")

        watcher.poll_once()
        initial_cursor = watcher.file_cursors[str(transcript_file)]

        immune_batch = (
            json.dumps({"status": "DONE", "content": "Found 3 matches for RESOURCE_EXHAUSTED in codebase"}) + "\n"
            + json.dumps({"type": "USER_INPUT", "content": "How do I handle RESOURCE_EXHAUSTED and code 429?"}) + "\n"
        )
        with open(transcript_file, "a", encoding="utf-8") as f:
            f.write(immune_batch)

        with patch("shutil.which", return_value=None):
            switches = watcher.poll_once()
            self.assertEqual(switches, 0)
            self.assertEqual(engine_stub.switch_calls, [])
            self.assertEqual(engine_stub.active_name_calls, 0)
            self.assertEqual(cooldown_mgr.get_cooldowns(), {})
            self.assertEqual(
                watcher.file_cursors[str(transcript_file)],
                initial_cursor + len(immune_batch.encode("utf-8")),
            )

            error_line = json.dumps({"status": "ERROR", "error": "Quota exceeded: RESOURCE_EXHAUSTED (code 429)"}) + "\n"
            with open(transcript_file, "a", encoding="utf-8") as f:
                f.write(error_line)

            switches_after_error = watcher.poll_once()
            self.assertEqual(switches_after_error, 1)
            self.assertEqual(engine_stub.switch_calls, [("acc2", True, True)])

    def test_process_line_triggers_on_error_status_resource_exhausted(self) -> None:
        """[WATCH-BOUND-002] status == 'ERROR' with RESOURCE_EXHAUSTED dispatches exhaustion handling."""
        line = json.dumps({
            "status": "ERROR",
            "error": "Quota exceeded for project: RESOURCE_EXHAUSTED",
        })
        with patch.dict(os.environ, {}, clear=True), patch("scripts.watch.shutil.which", return_value=None):
            for is_jsonl in (True, False):
                with self.subTest(is_jsonl=is_jsonl):
                    (self.cache_dir / "cooldown.json").unlink(missing_ok=True)
                    watcher, engine_stub, cooldown_mgr, _ = self._build_isolated_watcher()
                    self.assertTrue(watcher.process_line(line, is_jsonl=is_jsonl))
                    self.assertEqual(engine_stub.switch_calls, [("acc2", True, True)])
                    cooldowns = cooldown_mgr.get_cooldowns()
                    self.assertIn("acc1", cooldowns)
                    self.assertEqual(cooldowns["acc1"]["reason"], "RESOURCE_EXHAUSTED")
                    self.assertGreater(watcher.last_switch_time, 0.0)

    def test_process_line_triggers_on_error_status_error_message_http_429(self) -> None:
        """[WATCH-BOUND-003] type == 'ERROR_MESSAGE' with HTTP 429 dispatches exhaustion handling."""
        line = json.dumps({
            "type": "ERROR_MESSAGE",
            "message": "googleapi: Error 429: Rate limit exceeded",
        })
        with patch.dict(os.environ, {}, clear=True), patch("scripts.watch.shutil.which", return_value=None):
            for is_jsonl in (True, False):
                with self.subTest(is_jsonl=is_jsonl):
                    (self.cache_dir / "cooldown.json").unlink(missing_ok=True)
                    watcher, engine_stub, cooldown_mgr, _ = self._build_isolated_watcher()
                    self.assertTrue(watcher.process_line(line, is_jsonl=is_jsonl))
                    self.assertEqual(engine_stub.switch_calls, [("acc2", True, True)])
                    cooldowns = cooldown_mgr.get_cooldowns()
                    self.assertIn("acc1", cooldowns)
                    self.assertGreater(watcher.last_switch_time, 0.0)

    def test_process_line_triggers_on_error_status_http_429_variants_and_non_quota_guards(self) -> None:
        """HTTP 429 structured error variants trigger dispatch while unrelated 429 numbers do not."""
        triggering_payloads: list[dict[str, object]] = [
            {"type": "ERROR_MESSAGE", "message": "HTTP 429: Too Many Requests"},
            {"type": "ERROR_MESSAGE", "message": "HTTP/1.1 429 Too Many Requests"},
            {"status": "ERROR", "error": {"code": 429, "message": "Rate limit exceeded"}},
            {"status": "ERROR", "message": "status: 429 rate limited"},
            {"type": "ERROR_MESSAGE", "message": "429: Rate limit exceeded"},
            {"type": "ERROR_MESSAGE", "message": "googleapi: Error (429): Rate limit exceeded"},
            {"status": "ERROR", "error": "status (429) rate limited"},
            {"type": "ERROR_MESSAGE", "message": "(429) Too Many Requests"},
            {"type": "ERROR_MESSAGE", "message": "429: Quota exceeded"},
            {"type": "ERROR_MESSAGE", "message": "Rate limit exceeded (429)"},
            {"type": "ERROR_MESSAGE", "message": "errorCode: 429"},
            {"status": "ERROR", "error": "http_status=429 rate limited"},
            {"status": "ERROR", "error": "rpc error: code = ResourceExhausted desc = Quota limit reached"},
        ]
        non_triggering_payloads: list[dict[str, object]] = [
            {"status": "ERROR", "error": "SyntaxError at line 429: unexpected EOF"},
            {"status": "ERROR", "error": "Error at line 429: invalid token"},
            {"type": "ERROR_MESSAGE", "message": "Connection failed on port 4290"},
            {"type": "ERROR_MESSAGE", "message": "Connection failed on port 429"},
            {"status": "ERROR", "error": "Processed 429 items before timeout"},
            {"type": "ERROR_MESSAGE", "message": "Timeout after 429ms waiting for socket"},
            {"status": "ERROR", "error": "429: invalid JSON syntax"},
            {"type": "ERROR_MESSAGE", "message": "Error code: 4291"},
            {"status": "INFO", "code": 429, "message": "Rate limit exceeded"},
        ]

        with patch.dict(os.environ, {}, clear=True), patch("shutil.which", return_value=None):
            for payload in triggering_payloads:
                with self.subTest(triggering=payload):
                    (self.cache_dir / "cooldown.json").unlink(missing_ok=True)
                    watcher, engine_stub, cooldown_mgr, _ = self._build_isolated_watcher()
                    self.assertTrue(watcher.process_line(json.dumps(payload), is_jsonl=True))
                    self.assertEqual(engine_stub.switch_calls, [("acc2", True, True)])
                    self.assertIn("acc1", cooldown_mgr.get_cooldowns())

            for payload in non_triggering_payloads:
                with self.subTest(non_triggering=payload):
                    (self.cache_dir / "cooldown.json").unlink(missing_ok=True)
                    watcher, engine_stub, cooldown_mgr, _ = self._build_isolated_watcher()
                    self.assertFalse(watcher.process_line(json.dumps(payload), is_jsonl=True))
                    self.assertEqual(engine_stub.switch_calls, [])
                    self.assertEqual(cooldown_mgr.get_cooldowns(), {})

    def test_process_line_triggers_on_error_status_nested_payloads_and_envelope_normalization(self) -> None:
        """Nested dict/list payloads, numeric status codes, and case/whitespace envelopes trigger safely."""
        triggering_payloads: list[dict[str, object]] = [
            {"type": "ERROR_MESSAGE", "message": {"detail": "Upstream failed:\nError 429"}},
            {"type": "ERROR_MESSAGE", "message": {"detail": "Upstream failed:\nError 429: Rate limit exceeded"}},
            {"type": "ERROR_MESSAGE", "message": {"detail": "Upstream failed:\n429 Too Many Requests"}},
            {"type": "ERROR_MESSAGE", "message": [{"type": "text", "text": 'HTTP "429" Too Many Requests'}]},
            {"status": "ERROR", "error": {"details": [{"metadata": {"http_status": "429"}}]}},
            {"status": "ERROR", "content": {"parts": [{"text": "Upstream:\nError (429): Quota exceeded"}]}},
            {"status": "ERROR", "error": 429},
            {"status": "ERROR", "code": 429, "message": "Rate limit exceeded"},
            {"type": "ERROR_MESSAGE", "http_status": 429, "message": "Rate limit exceeded"},
            {"status": "error", "message": "googleapi: Error 429: Rate limit exceeded"},
            {"status": " ERROR ", "content": "RESOURCE_EXHAUSTED"},
            {"type": "error_message", "message": "googleapi: Error 429: Rate limit exceeded"},
            {"type": " ERROR_MESSAGE ", "message": "googleapi: Error 429: Rate limit exceeded"},
        ]
        non_triggering_payloads: list[dict[str, object]] = [
            {"status": " done ", "error": "RESOURCE_EXHAUSTED", "code": 429},
            {"type": " user_input ", "status": "ERROR", "code": 429, "message": "Error 429"},
            {"status": "ERROR", "line": 429, "error": "SyntaxError"},
            {"status": "ERROR", "error": {"line": 429, "detail": "Processed 429 items"}},
            {"status": "ERROR", "error": True},
        ]

        with patch.dict(os.environ, {}, clear=True), patch("shutil.which", return_value=None):
            for payload in triggering_payloads:
                with self.subTest(triggering=payload):
                    (self.cache_dir / "cooldown.json").unlink(missing_ok=True)
                    watcher, engine_stub, cooldown_mgr, _ = self._build_isolated_watcher()
                    self.assertTrue(watcher.process_line(json.dumps(payload), is_jsonl=True))
                    self.assertEqual(engine_stub.switch_calls, [("acc2", True, True)])
                    self.assertIn("acc1", cooldown_mgr.get_cooldowns())

            for payload in non_triggering_payloads:
                with self.subTest(non_triggering=payload):
                    (self.cache_dir / "cooldown.json").unlink(missing_ok=True)
                    watcher, engine_stub, cooldown_mgr, _ = self._build_isolated_watcher()
                    self.assertFalse(watcher.process_line(json.dumps(payload), is_jsonl=True))
                    self.assertEqual(engine_stub.switch_calls, [])
                    self.assertEqual(cooldown_mgr.get_cooldowns(), {})

    def test_process_line_triggers_on_error_status_transcript_tailing(self) -> None:
        """[WATCH-BOUND-002][WATCH-BOUND-003] poll_once dispatches exhaustion for structured transcript lines."""
        cases = [
            (
                "watch-bound-002",
                {"status": "ERROR", "error": "Quota exceeded for project: RESOURCE_EXHAUSTED"},
            ),
            (
                "watch-bound-003",
                {"type": "ERROR_MESSAGE", "message": "googleapi: Error 429: Rate limit exceeded"},
            ),
        ]

        with patch.dict(os.environ, {}, clear=True), patch("shutil.which", return_value=None):
            for session_id, payload in cases:
                with self.subTest(session_id=session_id):
                    (self.cache_dir / "cooldown.json").unlink(missing_ok=True)
                    watcher, engine_stub, cooldown_mgr, gemini_dir = self._build_isolated_watcher()

                    lock_file = gemini_dir / "antigravity" / "presence" / f"{session_id}.lock"
                    lock_file.parent.mkdir(parents=True, exist_ok=True)
                    lock_file.touch()
                    try:
                        transcript_file = (
                            gemini_dir
                            / "antigravity"
                            / "brain"
                            / session_id
                            / ".system_generated"
                            / "logs"
                            / "transcript.jsonl"
                        )
                        transcript_file.parent.mkdir(parents=True, exist_ok=True)
                        transcript_file.write_text('{"type": "START"}\n', encoding="utf-8")

                        self.assertEqual(watcher.poll_once(), 0)
                        with open(transcript_file, "a", encoding="utf-8") as f:
                            f.write(json.dumps(payload) + "\n")

                        self.assertEqual(watcher.poll_once(), 1)
                        self.assertEqual(engine_stub.switch_calls, [("acc2", True, True)])
                        self.assertIn("acc1", cooldown_mgr.get_cooldowns())
                    finally:
                        lock_file.unlink(missing_ok=True)

    @patch.object(LogWatcher, "trigger_switch", return_value=True)
    def test_jsonl_immunity_model_thinking_and_code_view(self, mock_switch):
        """Line representing model thinking or code file inspection DOES NOT trigger rotation."""
        # Non-error planner response (status IN_PROGRESS or missing)
        code_line = json.dumps({
            "type": "PLANNER_RESPONSE",
            "content": "def handle_error():\n    if err == 'RESOURCE_EXHAUSTED':\n        return True",
        })
        self.assertFalse(self.watcher.process_line(code_line, is_jsonl=True))
        mock_switch.assert_not_called()

        # Model thinking field mentioning exhaustion without error status
        thinking_line = json.dumps({
            "type": "MODEL",
            "thinking": "Thinking about RESOURCE_EXHAUSTED (code 429)...",
            "content": "Here is the solution to your query.",
        })
        self.assertFalse(self.watcher.process_line(thinking_line, is_jsonl=True))
        mock_switch.assert_not_called()

    def test_jsonl_malformed_and_non_json_lines_ignored(self):
        """Malformed JSON or non-JSON lines in JSONL mode return False safely."""
        self.assertFalse(self.watcher.process_line("{malformed json line", is_jsonl=True))
        self.assertFalse(self.watcher.process_line("not json at all", is_jsonl=True))
        self.assertFalse(self.watcher.process_line("", is_jsonl=True))

    @patch.object(LogWatcher, "trigger_switch", return_value=True)
    def test_process_line_ignores_non_json_plain_text(self, mock_switch):
        """[WATCH-BOUND-ERR-001] Non-JSON line in transcript or plain text file is unconditionally ignored."""
        plain_variants = [
            "RESOURCE_EXHAUSTED: unit test simulated failure",
            "  RESOURCE_EXHAUSTED: unit test simulated failure\n",
            "\tRESOURCE_EXHAUSTED: unit test simulated failure\r\n",
            "\ufeffRESOURCE_EXHAUSTED: unit test simulated failure\n",
            "\ufeff{\"status\": \"ERROR\", \"error\": \"RESOURCE_EXHAUSTED\"}\n",
            "2026-10-14 10:00:00 [ERROR] RESOURCE_EXHAUSTED (code 429) received",
            "   \t\r\n",
        ]
        for line in plain_variants:
            for is_jsonl in (True, False):
                with self.subTest(line=line, is_jsonl=is_jsonl):
                    self.assertFalse(self.watcher.process_line(line, is_jsonl=is_jsonl))
        mock_switch.assert_not_called()

    @patch.object(LogWatcher, "trigger_switch", return_value=True)
    def test_process_line_ignores_non_json_non_dict_and_missing_envelope(self, mock_switch):
        """[WATCH-BOUND-ERR-001] Non-dict JSON and JSON dicts lacking required error envelopes are ignored."""
        non_dict_or_envelope_missing = [
            json.dumps("RESOURCE_EXHAUSTED"),
            json.dumps(["RESOURCE_EXHAUSTED"]),
            json.dumps([{"status": "ERROR", "error": "RESOURCE_EXHAUSTED"}]),
            json.dumps(123),
            json.dumps(None),
            json.dumps(True),
            json.dumps({}),
            json.dumps({"text": "RESOURCE_EXHAUSTED"}),
            json.dumps({"content": "RESOURCE_EXHAUSTED"}),
            json.dumps({"message": "RESOURCE_EXHAUSTED"}),
            json.dumps({"status": "INFO", "content": "RESOURCE_EXHAUSTED"}),
            json.dumps({"status": "ERROR", "error": ""}),
            json.dumps({"status": "ERROR", "error": None}),
            json.dumps({"status": "ERROR", "thinking": "RESOURCE_EXHAUSTED"}),
        ]
        for line in non_dict_or_envelope_missing:
            for is_jsonl in (True, False):
                with self.subTest(line=line, is_jsonl=is_jsonl):
                    self.assertFalse(self.watcher.process_line(line, is_jsonl=is_jsonl))
        mock_switch.assert_not_called()

    @patch.object(LogWatcher, "trigger_switch", return_value=True)
    def test_process_line_ignores_non_json_malformed_truncated(self, mock_switch):
        """[WATCH-BOUND-ERR-002] Malformed or truncated JSON line does not crash watcher daemon."""
        partial_line = '{"status": "ERROR", "error": "RESOURCE_EX'
        self.assertFalse(self.watcher.process_line(partial_line, is_jsonl=True))
        self.assertFalse(self.watcher.process_line(partial_line, is_jsonl=False))
        mock_switch.assert_not_called()

        # Additional malformed variations
        for bad_line in (
            '{"status": "ERROR", ',
            "{invalid json",
            '{"status": "ERROR", "error": "RESOURCE_EX\ufffd',
            '{"status": "ERROR", "error": "RESOURCE_EXHAUSTED"} trailing',
            '{"status": "ERROR"}{"error": "RESOURCE_EXHAUSTED"}',
        ):
            with self.subTest(bad_line=bad_line):
                self.assertFalse(self.watcher.process_line(bad_line, is_jsonl=True))
                self.assertFalse(self.watcher.process_line(bad_line, is_jsonl=False))
        mock_switch.assert_not_called()

    def test_process_line_ignores_non_json_surrogate_stderr_safety(self):
        """[WATCH-BOUND-ERR-002] Escaped surrogate in JSON error payload does not raise UnicodeEncodeError on strict stderr."""
        strict_err = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="strict")
        line = '{"status": "ERROR", "error": "RESOURCE_EXHAUSTED \\ud800"}'
        with patch("sys.stderr", strict_err), patch.object(self.watcher, "trigger_switch", return_value=True) as mock_switch:
            self.assertTrue(self.watcher.process_line(line, is_jsonl=True))
            mock_switch.assert_called_once()

    def test_process_line_ignores_non_json_poll_once_survives_corrupt_and_non_utf8(self):
        """[WATCH-BOUND-ERR-002] Tailing loop survives corrupt/non-UTF8/plain-text lines and processes subsequent valid JSON errors."""
        test_file = self.base_dir / "corrupt_recovery.transcript.jsonl"
        test_file.write_bytes(b"")

        mock_detector = MagicMock()
        mock_detector.get_active_session_id.return_value = "corrupt-session"
        mock_detector.get_active_transcript_path.return_value = test_file
        mock_detector.get_watch_targets.return_value = [str(test_file)]
        self.watcher.presence_detector = mock_detector

        self.watcher.poll_once()
        self.assertEqual(self.watcher.file_cursors[str(test_file)], 0)

        # Tick 1: Write corrupt/truncated JSON, non-UTF8 JSON bytes, plain text, and envelope-missing JSON
        corrupt_batch = (
            b'{"status": "ERROR", "error": "RESOURCE_EX\n'
            b'{"status": "ERROR", \xff\xfe"error": "RESOURCE_EXHAUSTED"}\n'
            b"RESOURCE_EXHAUSTED: unit test simulated failure\n"
            b'{"content": "RESOURCE_EXHAUSTED"}\n'
        )
        test_file.write_bytes(corrupt_batch)

        def _mock_switch(*args, **kwargs):
            for f in list(self.watcher.file_cursors.keys()):
                try:
                    if os.path.isfile(f):
                        self.watcher.file_cursors[f] = os.path.getsize(f)
                except OSError:
                    pass
            return True

        with patch.object(self.watcher, "trigger_switch", side_effect=_mock_switch) as mock_switch:
            self.assertEqual(self.watcher.poll_once(), 0)
            mock_switch.assert_not_called()
            self.assertEqual(self.watcher.file_cursors[str(test_file)], len(corrupt_batch))

            # Tick 2: Mid-flush partial JSON without newline holds cursor safely
            partial_mid_flush = b'{"status": "ERROR", "error": "RESOURCE_EX'
            with open(test_file, "ab") as f:
                f.write(partial_mid_flush)
            self.assertEqual(self.watcher.poll_once(), 0)
            self.assertEqual(self.watcher.file_cursors[str(test_file)], len(corrupt_batch))

            # Tick 3: Flush newline terminating the corrupt line + valid structured JSON error line
            valid_line = b'\n{"status": "ERROR", "error": "RESOURCE_EXHAUSTED (code 429)"}\n'
            with open(test_file, "ab") as f:
                f.write(valid_line)

            self.assertEqual(self.watcher.poll_once(), 1)
            mock_switch.assert_called_once()
            self.assertEqual(
                self.watcher.file_cursors[str(test_file)],
                len(corrupt_batch) + len(partial_mid_flush) + len(valid_line),
            )

        # Verify watch_loop does not crash or stop across corrupt + valid iterations
        self.watcher.last_switch_time = 0.0
        with open(test_file, "ab") as f:
            f.write(b'{"status": "ERROR", "error": "RESOURCE_EX\n')
        with patch.object(self.watcher, "trigger_switch", side_effect=_mock_switch) as mock_switch:
            self.watcher.watch_loop(poll_interval=0.0, max_iterations=1)
            mock_switch.assert_not_called()
            with open(test_file, "ab") as f:
                f.write(b'{"status": "ERROR", "error": "RESOURCE_EXHAUSTED (code 429)"}\n')
            self.watcher.watch_loop(poll_interval=0.0, max_iterations=1)
            mock_switch.assert_called_once()

    def test_process_line_ignores_non_json_poll_once_oversized_unterminated_line(self):
        """[WATCH-BOUND-ERR-002] Oversized corrupt line exceeding max_chunk_bytes advances cursor instead of wedging tailer."""
        test_file = self.base_dir / "oversized_corrupt.transcript.jsonl"
        test_file.write_bytes(b"")

        bounded_watcher = LogWatcher(
            engine=self.engine_mock,
            cooldown_manager=self.cooldown_mgr,
            damping_seconds=15.0,
            log_dirs=[str(self.base_dir)],
            max_chunk_bytes=64,
        )
        mock_detector = MagicMock()
        mock_detector.get_active_session_id.return_value = "oversized-session"
        mock_detector.get_active_transcript_path.return_value = test_file
        mock_detector.get_watch_targets.return_value = [str(test_file)]
        bounded_watcher.presence_detector = mock_detector

        bounded_watcher.poll_once()

        oversized_corrupt = b'{"status": "ERROR", "error": "' + (b"X" * 100) + b"\n"
        valid_error = b'{"status": "ERROR", "error": "RESOURCE_EXHAUSTED (code 429)"}\n'
        test_file.write_bytes(oversized_corrupt + valid_error)

        def _mock_switch(*args, **kwargs):
            for f in list(bounded_watcher.file_cursors.keys()):
                try:
                    if os.path.isfile(f):
                        bounded_watcher.file_cursors[f] = os.path.getsize(f)
                except OSError:
                    pass
            return True

        with patch.object(bounded_watcher, "trigger_switch", side_effect=_mock_switch) as mock_switch:
            switches = 0
            for _ in range(5):
                switches += bounded_watcher.poll_once()
                if switches:
                    break
            self.assertEqual(switches, 1)
            mock_switch.assert_called_once()
            self.assertEqual(
                bounded_watcher.file_cursors[str(test_file)],
                len(oversized_corrupt) + len(valid_error),
            )

    @patch.object(LogWatcher, "trigger_switch", return_value=True)
    def test_raw_cli_log_ignores_normal_lines(self, mock_switch):
        """Raw CLI text line without exhaustion pattern does not trigger rotation."""
        line = "2026-10-14 10:00:00 [INFO] Successfully completed request"
        res = self.watcher.process_line(line, is_jsonl=False)
        self.assertFalse(res)
        mock_switch.assert_not_called()

    @patch("scripts.watch.time.sleep", return_value=None)
    def test_damping_window_suppresses_subsequent_rotations(self, mock_sleep):
        """Exhaustion events within 15 seconds of a prior switch are suppressed."""
        now = 1000.0
        line = json.dumps({"status": "ERROR", "content": "RESOURCE_EXHAUSTED (code 429)"})

        # Mock RotationEngine to return acc2
        with patch("scripts.watch.RotationEngine") as mock_rot_cls:
            mock_rot = MagicMock()
            mock_rot.evaluate.return_value = "acc2"
            mock_rot_cls.return_value = mock_rot

            # First event at t = 1000.0 -> Successful switch
            with patch("time.time", return_value=now):
                res1 = self.watcher.process_line(line, is_jsonl=True)
                self.assertTrue(res1)
                self.assertEqual(self.watcher.last_switch_time, 1000.0)
                self.assertEqual(self.engine_mock.switch_tokens.call_count, 1)

            # Second event at t = 1005.0 (5s elapsed < 15s damping window) -> Suppressed
            with patch("time.time", return_value=now + 5.0):
                res2 = self.watcher.process_line(line, is_jsonl=True)
                self.assertFalse(res2)
                # switch_tokens NOT called again
                self.assertEqual(self.engine_mock.switch_tokens.call_count, 1)

            # Third event at t = 1014.9 (14.9s elapsed < 15s damping window) -> Suppressed
            with patch("time.time", return_value=now + 14.9):
                res3 = self.watcher.process_line(line, is_jsonl=True)
                self.assertFalse(res3)
                self.assertEqual(self.engine_mock.switch_tokens.call_count, 1)

            # Fourth event at t = 1015.1 (15.1s elapsed >= 15s damping window) -> Allowed
            # Reset acc1 as active or valid for next test
            self.cooldown_mgr.clear_cooldown("acc1")
            with patch("time.time", return_value=now + 15.1):
                res4 = self.watcher.process_line(line, is_jsonl=True)
                self.assertTrue(res4)
                self.assertEqual(self.engine_mock.switch_tokens.call_count, 2)
                self.assertEqual(self.watcher.last_switch_time, 1015.1)

    @patch("scripts.watch.time.sleep", return_value=None)
    def test_damping_window_resets_on_backward_clock_jump(self, mock_sleep):
        """When system clock jumps backward (e.g. NTP sync), damping resets safely instead of hanging."""
        line = json.dumps({"status": "ERROR", "content": "RESOURCE_EXHAUSTED (code 429)"})
        self.watcher.last_switch_time = 2000.0

        with patch("scripts.watch.RotationEngine") as mock_rot_cls:
            mock_rot = MagicMock()
            mock_rot.evaluate.return_value = "acc2"
            mock_rot_cls.return_value = mock_rot

            # Clock moved back to 1000.0 (elapsed is -1000 < 0)
            with patch("time.time", return_value=1000.0):
                res = self.watcher.process_line(line, is_jsonl=True)
                self.assertTrue(res)
                self.assertEqual(self.watcher.last_switch_time, 1000.0)

    @patch("scripts.watch.time.sleep", return_value=None)
    @patch("scripts.watch.RotationEngine")
    def test_trigger_switch_calls_engine_switch_tokens_turn_safe(self, mock_rot_cls, mock_sleep):
        """trigger_switch calls engine.switch_tokens with restart_server=True and force=True under ADR 0010."""
        mock_rot = MagicMock()
        mock_rot.evaluate.return_value = "acc2"
        mock_rot_cls.return_value = mock_rot

        res = self.watcher.trigger_switch(line="RESOURCE_EXHAUSTED (code 429)")
        self.assertTrue(res)
        self.engine_mock.switch_tokens.assert_called_once_with("acc2", restart_server=True, force=True)

    @patch("scripts.watch.time.sleep", return_value=None)
    @patch("scripts.watch.RotationEngine")
    @patch("scripts.watch.shutil.which", return_value="/usr/bin/notify-send")
    @patch("scripts.watch.subprocess.Popen")
    def test_trigger_switch_spawns_nonblocking_desktop_notification_when_available(
        self, mock_popen, mock_which, mock_rot_cls, mock_sleep
    ):
        """If notify-send is installed, trigger_switch spawns it asynchronously with DEVNULL."""
        mock_rot = MagicMock()
        mock_rot.evaluate.return_value = "acc2"
        mock_rot_cls.return_value = mock_rot

        self.watcher.trigger_switch(line="RESOURCE_EXHAUSTED (code 429)")

        mock_which.assert_called_with("notify-send")
        mock_popen.assert_called_once()
        popen_args, popen_kwargs = mock_popen.call_args
        cmd_args = popen_args[0]
        self.assertEqual(cmd_args[0], "/usr/bin/notify-send")
        self.assertIn("agy-switch", cmd_args[1])
        self.assertIn("acc2", cmd_args[2])
        self.assertEqual(popen_kwargs.get("stdout"), subprocess.DEVNULL)
        self.assertEqual(popen_kwargs.get("stderr"), subprocess.DEVNULL)

    @patch("scripts.watch.time.sleep", return_value=None)
    @patch("scripts.watch.RotationEngine")
    @patch("scripts.watch.shutil.which", return_value=None)
    @patch("scripts.watch.subprocess.Popen")
    def test_trigger_switch_skips_desktop_notification_when_missing(
        self, mock_popen, mock_which, mock_rot_cls, mock_sleep
    ):
        """If notify-send is NOT installed, no subprocess is spawned."""
        mock_rot = MagicMock()
        mock_rot.evaluate.return_value = "acc2"
        mock_rot_cls.return_value = mock_rot

        self.watcher.trigger_switch(line="RESOURCE_EXHAUSTED (code 429)")
        mock_which.assert_called_with("notify-send")
        mock_popen.assert_not_called()

    def test_dynamic_active_session_switching_and_cursor_cleanup(self):
        """When active session ID changes, watcher switches transcript target and prunes old cursor."""
        mock_detector = MagicMock()
        self.watcher.presence_detector = mock_detector

        # Sesi 1
        transcript_s1 = self.base_dir / "s1" / "transcript.jsonl"
        mock_detector.get_active_session_id.return_value = "session-1"
        mock_detector.get_active_transcript_path.return_value = transcript_s1
        mock_detector.get_watch_targets.return_value = [str(transcript_s1)]

        targets1 = self.watcher.scan_active_targets()
        self.assertIn(str(transcript_s1), targets1)
        self.assertEqual(self.watcher.active_session_id, "session-1")

        # Simulate cursor set for session 1
        self.watcher.file_cursors[str(transcript_s1)] = 1234

        # Sesi berubah ke Sesi 2
        transcript_s2 = self.base_dir / "s2" / "transcript.jsonl"
        mock_detector.get_active_session_id.return_value = "session-2"
        mock_detector.get_active_transcript_path.return_value = transcript_s2
        mock_detector.get_watch_targets.return_value = [str(transcript_s2)]

        targets2 = self.watcher.scan_active_targets()
        self.assertIn(str(transcript_s2), targets2)
        self.assertEqual(self.watcher.active_session_id, "session-2")
        # Old cursor must be pruned!
        self.assertNotIn(str(transcript_s1), self.watcher.file_cursors)

    def test_scan_active_targets_dual_directory_cli_transcript(self):
        """[REQ-2-SCN-006]: GIVEN LogWatcher is initialized with a PresenceDetector supporting dual directories,
        AND an active CLI session has an active transcript,
        WHEN LogWatcher.scan_active_targets() is invoked,
        THEN the returned targets list includes the active CLI transcript path.
        """
        gemini_dir = self.base_dir / ".gemini_watcher_dual"
        cli_brain = gemini_dir / "antigravity-cli" / "brain" / "cli-sess-scan" / ".system_generated" / "logs"
        cli_brain.mkdir(parents=True, exist_ok=True)
        cli_transcript = cli_brain / "transcript.jsonl"
        cli_transcript.write_text('{"event": "start"}\n', encoding="utf-8")

        cli_presence = gemini_dir / "antigravity-cli" / "presence" / "cli-sess-scan.lock"
        cli_presence.parent.mkdir(parents=True, exist_ok=True)
        cli_presence.touch()

        detector = PresenceDetector(gemini_dir=gemini_dir, proc_dir=self.base_dir / "nonexistent_proc")
        watcher = LogWatcher(engine=MagicMock(), presence_detector=detector)

        targets = watcher.scan_active_targets()
        self.assertIn(str(cli_transcript), targets)
        self.assertEqual(watcher.active_session_id, "cli-sess-scan")
        self.assertEqual(watcher.active_transcript_path, cli_transcript)

    def test_scan_active_targets_deduplicates_paths(self):
        """[REQ-2-SCN-006]: Verify scan_active_targets deduplicates targets even if log_dirs contains overlaps."""
        mock_detector = MagicMock()
        mock_detector.get_active_session_id.return_value = "dedup-sess"
        mock_detector.get_active_transcript_path.return_value = Path("/dummy/transcript.jsonl")
        mock_detector.get_watch_targets.return_value = ["/dummy/transcript.jsonl", "/dummy/agent.log"]

        watcher = LogWatcher(engine=MagicMock(), presence_detector=mock_detector)
        # Simulate custom log_dir having duplicate
        custom_dir = self.base_dir / "custom_logs"
        custom_dir.mkdir(parents=True, exist_ok=True)
        dup_file = custom_dir / "agent.log"
        dup_file.touch()
        watcher.log_dirs = [str(custom_dir)]

        # Mock presence_detector.get_watch_targets returning the duplicate path as well
        mock_detector.get_watch_targets.return_value = [str(dup_file), str(dup_file)]

        targets = watcher.scan_active_targets()
        self.assertEqual(targets.count(str(dup_file)), 1)

    def test_req_2_scn_007_dynamic_session_switch_desktop_to_cli_prunes_stale_cursors(self):
        """[REQ-2-SCN-007]: GIVEN a running LogWatcher daemon tailing an active Desktop transcript when a user initiates a newer CLI session,
        WHEN LogWatcher.scan_active_targets() runs on the next tick,
        THEN it detects the newer CLI transcript via mtime, adjusts watch targets without duplicate file descriptors,
        and resets stale transcript cursors cleanly.
        """
        gemini_dir = self.base_dir / ".gemini_e2e_watcher"
        desktop_brain = gemini_dir / "antigravity" / "brain" / "desk-sess" / ".system_generated" / "logs"
        desktop_brain.mkdir(parents=True, exist_ok=True)
        desktop_transcript = desktop_brain / "transcript.jsonl"
        desktop_transcript.write_text('{"event": "start_desktop"}\n', encoding="utf-8")

        desktop_presence = gemini_dir / "antigravity" / "presence" / "desk-sess.lock"
        desktop_presence.parent.mkdir(parents=True, exist_ok=True)
        desktop_presence.touch()
        os.utime(desktop_presence, (1000.0, 1000.0))

        cli_log_dir = gemini_dir / "antigravity-cli" / "log"
        cli_log_dir.mkdir(parents=True, exist_ok=True)
        cli_agent_log = cli_log_dir / "agent.log"
        cli_agent_log.write_text("agent ready\n", encoding="utf-8")

        detector = PresenceDetector(gemini_dir=gemini_dir, proc_dir=self.base_dir / "nonexistent_proc")
        watcher = LogWatcher(engine=MagicMock(), presence_detector=detector)

        targets_tick1 = watcher.scan_active_targets()
        self.assertIn(str(desktop_transcript), targets_tick1)
        self.assertEqual(watcher.active_session_id, "desk-sess")
        self.assertEqual(watcher.active_transcript_path, desktop_transcript)

        watcher.file_cursors[str(desktop_transcript)] = 42
        watcher.file_cursors[str(cli_agent_log)] = 15

        cli_brain = gemini_dir / "antigravity-cli" / "brain" / "cli-sess" / ".system_generated" / "logs"
        cli_brain.mkdir(parents=True, exist_ok=True)
        cli_transcript = cli_brain / "transcript.jsonl"
        cli_transcript.write_text('{"event": "start_cli"}\n', encoding="utf-8")

        cli_presence = gemini_dir / "antigravity-cli" / "presence" / "cli-sess.lock"
        cli_presence.parent.mkdir(parents=True, exist_ok=True)
        cli_presence.touch()
        os.utime(cli_presence, (2000.0, 2000.0))

        targets_tick2 = watcher.scan_active_targets()

        self.assertEqual(watcher.active_session_id, "cli-sess")
        self.assertEqual(watcher.active_transcript_path, cli_transcript)
        self.assertIn(str(cli_transcript), targets_tick2)
        self.assertNotIn(str(desktop_transcript), targets_tick2)
        self.assertEqual(len(targets_tick2), len(set(targets_tick2)))
        self.assertNotIn(str(desktop_transcript), watcher.file_cursors)
        self.assertEqual(watcher.file_cursors.get(str(cli_agent_log)), 15)

    def test_poll_once_reads_lines_and_handles_truncation(self):
        """poll_once correctly tails file, advances cursor, and resets cursor on truncation."""
        test_file = self.base_dir / "test_session.transcript.jsonl"
        test_file.write_text('{"type": "START"}\n', encoding="utf-8")

        mock_detector = MagicMock()
        mock_detector.get_active_session_id.return_value = "test-session"
        mock_detector.get_active_transcript_path.return_value = test_file
        mock_detector.get_watch_targets.return_value = [str(test_file)]
        self.watcher.presence_detector = mock_detector

        # Initial poll sets cursor to end of file
        self.watcher.poll_once()
        initial_cursor = self.watcher.file_cursors[str(test_file)]
        self.assertEqual(initial_cursor, test_file.stat().st_size)

        # Append an error line
        error_line = json.dumps({"status": "ERROR", "content": "RESOURCE_EXHAUSTED (code 429)"}) + "\n"
        with open(test_file, "a", encoding="utf-8") as f:
            f.write(error_line)

        # Mock trigger_switch with ADR 0010 multi-target EOF seeking side effect
        def _mock_switch(*args, **kwargs):
            for f in list(self.watcher.file_cursors.keys()):
                try:
                    if os.path.isfile(f):
                        self.watcher.file_cursors[f] = os.path.getsize(f)
                except OSError:
                    pass
            return True

        with patch.object(self.watcher, "trigger_switch", side_effect=_mock_switch) as mock_switch:
            switches = self.watcher.poll_once()
            self.assertEqual(switches, 1)
            mock_switch.assert_called_once()
            self.assertEqual(self.watcher.file_cursors[str(test_file)], test_file.stat().st_size)

        # Truncate file (simulate log rotation / rewrite)
        test_file.write_text('{"type": "RESET"}\n', encoding="utf-8")
        self.assertTrue(test_file.stat().st_size < self.watcher.file_cursors[str(test_file)])

        self.watcher.poll_once()
        # Cursor should adapt to the new truncated file size
        self.assertEqual(self.watcher.file_cursors[str(test_file)], test_file.stat().st_size)

    def test_notify_desktop_foreground_watch_log_mode_0600(self):
        """watch.log created during foreground _notify_desktop strictly enforces mode 0600."""
        watch_log = self.vault_dir / "watch.log"
        if watch_log.exists():
            watch_log.unlink()

        with patch("scripts.watch.shutil.which", return_value=None):
            self.watcher._notify_desktop(target_account="acc2", reason="test exhaustion")

        self.assertTrue(watch_log.is_file())
        st_mode = watch_log.stat().st_mode & 0o777
        self.assertEqual(st_mode, 0o600)
        content = watch_log.read_text(encoding="utf-8")
        self.assertIn("Switched to 'acc2' (test exhaustion)", content)

    def test_poll_once_partial_chunk_buffering_and_completion(self):
        """poll_once holds un-terminated chunk without advancing cursor or busy-reading; writing remainder parses cleanly."""
        test_file = self.base_dir / "partial_test.transcript.jsonl"
        test_file.write_bytes(b"")

        mock_detector = MagicMock()
        mock_detector.get_active_session_id.return_value = "partial-session"
        mock_detector.get_active_transcript_path.return_value = test_file
        mock_detector.get_watch_targets.return_value = [str(test_file)]
        self.watcher.presence_detector = mock_detector

        # Initial poll sets cursor to 0
        self.watcher.poll_once()
        self.assertEqual(self.watcher.file_cursors[str(test_file)], 0)

        # Write partial chunk without newline
        partial_bytes = b'{"status": "ERROR", "content": "RESOURCE_EXHAUSTED'
        test_file.write_bytes(partial_bytes)

        def _mock_switch(*args, **kwargs):
            for f in list(self.watcher.file_cursors.keys()):
                try:
                    if os.path.isfile(f):
                        self.watcher.file_cursors[f] = os.path.getsize(f)
                except OSError:
                    pass
            return True

        with patch.object(self.watcher, "trigger_switch", side_effect=_mock_switch) as mock_switch:
            # First tick with incomplete line: must NOT advance cursor and must NOT trigger switch
            switches = self.watcher.poll_once()
            self.assertEqual(switches, 0)
            mock_switch.assert_not_called()
            self.assertEqual(self.watcher.file_cursors[str(test_file)], 0)

            # Stagnant tick with no new writes: must avoid busy-reading and hold cursor at 0
            switches_stagnant = self.watcher.poll_once()
            self.assertEqual(switches_stagnant, 0)
            mock_switch.assert_not_called()
            self.assertEqual(self.watcher.file_cursors[str(test_file)], 0)

            # Complete line arrival in next tick
            with open(test_file, "ab") as f:
                f.write(b' (code 429)"}\n')

            switches_complete = self.watcher.poll_once()
            self.assertEqual(switches_complete, 1)
            mock_switch.assert_called_once()
            expected_cursor = len(b'{"status": "ERROR", "content": "RESOURCE_EXHAUSTED (code 429)"}\n')
            self.assertEqual(self.watcher.file_cursors[str(test_file)], expected_cursor)

    def test_poll_once_multibyte_utf8_split_chunk(self):
        """Splitting multibyte UTF-8 characters across chunk boundaries does not corrupt stream cursor or error detection."""
        test_file = self.base_dir / "multibyte_test.transcript.jsonl"
        test_file.write_bytes(b"")

        mock_detector = MagicMock()
        mock_detector.get_active_session_id.return_value = "multibyte-session"
        mock_detector.get_active_transcript_path.return_value = test_file
        mock_detector.get_watch_targets.return_value = [str(test_file)]
        self.watcher.presence_detector = mock_detector

        self.watcher.poll_once()
        self.assertEqual(self.watcher.file_cursors[str(test_file)], 0)

        # Line 1: Normal JSON line containing 4-byte UTF-8 emoji 🚀 (b'\xf0\x9f\x9a\x80') with newline
        line1_bytes = (
            json.dumps({"type": "info", "content": "info 🚀 normal operation"}, ensure_ascii=False).encode("utf-8")
            + b"\n"
        )
        # Line 2: Structured error containing 3-byte Japanese char 日本 ('\xe6\x97\xa5\xe6\x9c\xac')
        full_line2_bytes = (
            json.dumps(
                {"status": "ERROR", "content": "error prefix 日本 RESOURCE_EXHAUSTED (code 429)"},
                ensure_ascii=False,
            ).encode("utf-8")
            + b"\n"
        )
        nihon_idx = full_line2_bytes.index("日本".encode("utf-8"))
        split_point = nihon_idx + 1  # cut mid-character
        line2_partial = full_line2_bytes[:split_point]
        line2_remainder = full_line2_bytes[split_point:]

        test_file.write_bytes(line1_bytes + line2_partial)

        def _mock_switch(*args, **kwargs):
            for f in list(self.watcher.file_cursors.keys()):
                try:
                    if os.path.isfile(f):
                        self.watcher.file_cursors[f] = os.path.getsize(f)
                except OSError:
                    pass
            return True

        with patch.object(self.watcher, "trigger_switch", side_effect=_mock_switch) as mock_switch:
            # Tick 1: Line 1 should be processed cleanly; partial multibyte Line 2 must be held at line 1 boundary
            switches = self.watcher.poll_once()
            self.assertEqual(switches, 0)
            mock_switch.assert_not_called()
            self.assertEqual(self.watcher.file_cursors[str(test_file)], len(line1_bytes))

            # Tick 2: Append remainder of multibyte character + error keyword + newline
            with open(test_file, "ab") as f:
                f.write(line2_remainder)

            switches2 = self.watcher.poll_once()
            self.assertEqual(switches2, 1)
            mock_switch.assert_called_once()
            self.assertEqual(
                self.watcher.file_cursors[str(test_file)],
                len(line1_bytes) + len(full_line2_bytes),
            )

    def test_poll_once_mid_batch_switch_advances_cursor_to_eof(self):
        """When failover triggers under ADR 0010, cursor advances to EOF to prevent restart storms."""
        test_file = self.base_dir / "batch_test.transcript.jsonl"
        test_file.write_bytes(b"")

        mock_detector = MagicMock()
        mock_detector.get_active_session_id.return_value = "batch-session"
        mock_detector.get_active_transcript_path.return_value = test_file
        mock_detector.get_watch_targets.return_value = [str(test_file)]
        self.watcher.presence_detector = mock_detector

        self.watcher.poll_once()

        line1 = json.dumps({"type": "info", "content": "startup normal"}).encode("utf-8") + b"\n"
        line2 = json.dumps({"status": "ERROR", "content": "RESOURCE_EXHAUSTED (code 429)"}).encode("utf-8") + b"\n"
        line3 = json.dumps({"type": "info", "content": "subsequent message that must be preserved"}).encode("utf-8") + b"\n"
        test_file.write_bytes(line1 + line2 + line3)

        def _mock_switch(*args, **kwargs):
            for f in list(self.watcher.file_cursors.keys()):
                try:
                    if os.path.isfile(f):
                        self.watcher.file_cursors[f] = os.path.getsize(f)
                except OSError:
                    pass
            return True

        with patch.object(self.watcher, "trigger_switch", side_effect=_mock_switch) as mock_switch:
            switches = self.watcher.poll_once()
            self.assertEqual(switches, 1)
            mock_switch.assert_called_once()
            # Under ADR 0010, cursor MUST advance to EOF, NOT end of line 2
            self.assertEqual(self.watcher.file_cursors[str(test_file)], len(line1) + len(line2) + len(line3))

        # In next tick, reading from cursor processes 0 lines because cursor is at EOF
        with patch.object(self.watcher, "process_line") as mock_proc:
            switches2 = self.watcher.poll_once()
            self.assertEqual(switches2, 0)
            mock_proc.assert_not_called()

    def test_nested_error_payload_and_io_robustness_nested_content_traversal(self) -> None:
        """[WATCH-BOUND-EDGE-001] GIVEN JSON line with nested content payload,
        LogWatcher.process_line() extracts tokens, matches ResourceExhausted, and triggers exhaustion.
        """
        nested_payloads = [
            # Exact AC specification line:
            {"status": "ERROR", "content": [{"type": "text", "text": "rpc error: code = ResourceExhausted desc = Quota limit reached"}]},
            # Deeply nested content structures:
            {"status": "ERROR", "content": [{"parts": [{"text": "rpc error: code = ResourceExhausted desc = Quota limit reached"}]}]},
            {"status": "ERROR", "content": {"details": [{"message": "rpc error: code = ResourceExhausted desc = Quota limit reached"}]}},
            {"status": "ERROR", "content": [{"type": "text", "text": "resource exhausted: quota exceeded"}]},
            {"status": "ERROR", "content": [{"code": [429], "desc": "rate limit exceeded"}]},
        ]

        for payload in nested_payloads:
            line = json.dumps(payload)
            with self.subTest(payload=payload):
                with patch.object(self.watcher, "handle_exhaustion", return_value=True) as mock_handle:
                    res = self.watcher.process_line(line, is_jsonl=True)
                    self.assertTrue(res)
                    mock_handle.assert_called_once()
                    call_kwargs = mock_handle.call_args.kwargs
                    line_arg = call_kwargs.get("line") or (mock_handle.call_args.args[0] if mock_handle.call_args.args else "")
                    self.assertTrue(
                        any(token in line_arg.lower() for token in ("resourceexhausted", "resource exhausted", "429")),
                        f"Expected exhaustion token in dispatched line, got: {line_arg}",
                    )

        # End-to-end verification with isolated stub engine
        watcher, engine_stub, cooldown_mgr, _ = self._build_isolated_watcher()
        line = json.dumps({
            "status": "ERROR",
            "content": [{"type": "text", "text": "rpc error: code = ResourceExhausted desc = Quota limit reached"}],
        })
        with patch("scripts.watch.shutil.which", return_value=None):
            self.assertTrue(watcher.process_line(line, is_jsonl=True))
            self.assertEqual(engine_stub.switch_calls, [("acc2", True, True)])
            self.assertIn("acc1", cooldown_mgr.get_cooldowns())

    def test_nested_error_payload_and_io_robustness_unreadable_transcript_io(self) -> None:
        """[WATCH-BOUND-ERR-003] GIVEN active transcript path unreadable due to filesystem permissions,
        LogWatcher.poll_once() catches OSError safely and skips target without raising or terminating watcher loop.
        """
        transcript_file = self.base_dir / "unreadable_robustness.transcript.jsonl"
        transcript_file.write_text('{"type": "START"}\n', encoding="utf-8")

        mock_detector = MagicMock()
        mock_detector.get_active_session_id.return_value = "unreadable-robustness-session"
        mock_detector.get_active_transcript_path.return_value = transcript_file
        mock_detector.get_watch_targets.return_value = [str(transcript_file)]
        self.watcher.presence_detector = mock_detector

        # Initial poll primes cursor
        self.watcher.poll_once()
        initial_cursor = self.watcher.file_cursors[str(transcript_file)]
        self.assertGreater(initial_cursor, 0)

        # Append new exhaustion error record
        error_line = json.dumps({"status": "ERROR", "content": [{"type": "text", "text": "rpc error: code = ResourceExhausted"}]}) + "\n"
        with open(transcript_file, "a", encoding="utf-8") as f:
            f.write(error_line)

        # Scenario A: Real filesystem permission denied (chmod 000)
        transcript_file.chmod(0o000)
        try:
            switches = self.watcher.poll_once()
            # Must catch OSError and skip target safely without raising
            self.assertEqual(switches, 0)
        finally:
            transcript_file.chmod(0o644)

        # Scenario B: Simulated open PermissionError / EACCES via mock
        orig_open = open

        def mock_open_permission_denied(path, mode="r", *args, **kwargs):
            if str(path) == str(transcript_file) and "b" in mode:
                raise PermissionError(13, "Permission denied")
            return orig_open(path, mode, *args, **kwargs)

        with patch("builtins.open", side_effect=mock_open_permission_denied):
            # poll_once must handle PermissionError safely
            switches = self.watcher.poll_once()
            self.assertEqual(switches, 0)

            # watch_loop must continue running without terminating on PermissionError
            self.watcher.watch_loop(poll_interval=0.0, max_iterations=2)

        # Scenario C: Restored readable permissions resumes tailing cleanly
        with patch.object(self.watcher, "trigger_switch", return_value=True) as mock_switch:
            switches_resumed = self.watcher.poll_once()
            self.assertEqual(switches_resumed, 1)
            mock_switch.assert_called_once()

    def test_nested_error_payload_and_io_robustness_adversarial_edge_cases(self) -> None:
        """Adversarial stress-testing: non-scalars, cycles, protobuf oneof keys, and scan OSError."""
        # 1. Non-string types, None, booleans, floats, empty strings
        adversarial_payload = {
            "status": "ERROR",
            "content": [
                None,
                True,
                False,
                42.5,
                "",
                {"details": None, "active": True, "count": 0, "blank": "  "},
                {"parts": [{"type": "text", "text": "rpc error: code = ResourceExhausted"}]},
            ],
        }
        with patch.object(self.watcher, "handle_exhaustion", return_value=True) as mock_handle:
            self.assertTrue(self.watcher.process_line(json.dumps(adversarial_payload), is_jsonl=True))
            mock_handle.assert_called_once()

        # 2. Cycle resilience in _extract_text_fragments
        from agy_switcher.watcher import _extract_text_fragments
        cyclic_dict: dict[str, object] = {"label": "node"}
        cyclic_dict["self"] = cyclic_dict
        cyclic_list: list[object] = ["text fragment"]
        cyclic_list.append(cyclic_list)
        cyclic_dict["sublist"] = cyclic_list

        extracted = _extract_text_fragments(cyclic_dict)
        self.assertIn("node", extracted)
        self.assertIn("text fragment", extracted)

        # 3. Protobuf / gRPC oneof error keys where values are empty or None
        oneof_payloads = [
            {"status": "ERROR", "content": [{"ResourceExhausted": {}}]},
            {"status": "ERROR", "content": [{"resource_exhausted": None}]},
        ]
        for p in oneof_payloads:
            with self.subTest(oneof=p):
                with patch.object(self.watcher, "handle_exhaustion", return_value=True) as mock_handle:
                    self.assertTrue(self.watcher.process_line(json.dumps(p), is_jsonl=True))
                    mock_handle.assert_called_once()

        # 4. Top-level type == "ERROR" and top-level details / detail / reason
        extended_envelopes = [
            {"type": "ERROR", "content": [{"type": "text", "text": "rpc error: code = ResourceExhausted"}]},
            {"status": "ERROR", "details": [{"message": "rpc error: code = ResourceExhausted"}]},
            {"status": "ERROR", "detail": "429: Too Many Requests"},
            {"status": "ERROR", "reason": "RESOURCE_EXHAUSTED"},
        ]
        for p in extended_envelopes:
            with self.subTest(envelope=p):
                with patch.object(self.watcher, "handle_exhaustion", return_value=True) as mock_handle:
                    self.assertTrue(self.watcher.process_line(json.dumps(p), is_jsonl=True))
                    mock_handle.assert_called_once()

        # 5. scan_active_targets OSError robustness
        with patch.object(self.watcher, "scan_active_targets", side_effect=PermissionError(13, "Permission denied")):
            self.assertEqual(self.watcher.poll_once(), 0)



class TestDaemonLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp_dir.name)
        self.vault_dir = self.base_dir / "vault"
        self.gemini_dir = self.base_dir / ".gemini"
        self.proc_dir = self.base_dir / "proc"

        self.vault_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.gemini_dir.mkdir(parents=True, exist_ok=True)
        self.proc_dir.mkdir(parents=True, exist_ok=True)

        self.mock_script = self.base_dir / "mock_watch.py"
        self.mock_script.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(60)\n", encoding="utf-8")

        self.dm = DaemonManager(
            vault_dir=self.vault_dir,
            watch_script=self.mock_script,
            proc_dir=self.proc_dir,
            gemini_dir=self.gemini_dir,
        )

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _create_mock_proc(self, pid: int, cmdline: bytes) -> Path:
        p_dir = self.proc_dir / str(pid)
        p_dir.mkdir(parents=True, exist_ok=True)
        (p_dir / "cmdline").write_bytes(cmdline)
        return p_dir

    def test_daemon_start_creates_pid_file_with_0600_permissions(self):
        """start() spawns background process with start_new_session=True and writes watch.pid with mode 0600."""
        mock_proc = MagicMock(pid=12345)
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            pid = self.dm.start(cooldown=45)
            self.assertEqual(pid, 12345)

            self.assertTrue(self.dm.pid_file.is_file())
            st_mode = self.dm.pid_file.stat().st_mode & 0o777
            self.assertEqual(st_mode, 0o600)
            self.assertEqual(self.dm.pid_file.read_text(encoding="utf-8").strip(), "12345")

            self.assertTrue(self.dm.log_file.exists())
            log_mode = self.dm.log_file.stat().st_mode & 0o777
            self.assertEqual(log_mode, 0o600)

            mock_popen.assert_called_once()
            popen_args, popen_kwargs = mock_popen.call_args
            self.assertTrue(popen_kwargs.get("start_new_session"))
            self.assertIn("--cooldown", popen_args[0])
            self.assertIn("45", popen_args[0])
            self.assertIn("run", popen_args[0])

    def test_daemon_start_rejects_if_already_running(self):
        """start() rejects execution and reports active PID if daemon is already running."""
        self._create_mock_proc(12345, b"python3\x00mock_watch.py\x00run\x00")
        self.dm.pid_file.write_text("12345\n", encoding="utf-8")

        with patch("os.kill", return_value=None):
            with self.assertRaises(DaemonAlreadyRunningError) as ctx:
                self.dm.start()
            self.assertEqual(ctx.exception.pid, 12345)
            self.assertIn("12345", str(ctx.exception))
            self.assertIsInstance(ctx.exception, RuntimeError)

    def test_daemon_start_prunes_stale_pid_and_starts(self):
        """start() automatically prunes dead/stale PID file and successfully launches."""
        self.dm.pid_file.write_text("99999\n", encoding="utf-8")
        mock_proc = MagicMock(pid=54321)

        with patch("os.kill", side_effect=ProcessLookupError):
            with patch("subprocess.Popen", return_value=mock_proc):
                pid = self.dm.start()
                self.assertEqual(pid, 54321)
                self.assertEqual(self.dm.pid_file.read_text(encoding="utf-8").strip(), "54321")

    def test_daemon_stop_graceful_sigterm(self):
        """stop() gracefully terminates the running daemon with SIGTERM and unlinks watch.pid."""
        self.dm.pid_file.write_text("12345\n", encoding="utf-8")
        self._create_mock_proc(12345, b"python3\x00mock_watch.py\x00run\x00")

        alive = [True]
        signals_received = []

        def mock_kill(pid, sig):
            if sig == 0:
                if not alive[0]:
                    raise ProcessLookupError
                return None
            signals_received.append(sig)
            if sig == signal.SIGTERM:
                alive[0] = False
                return None
            raise ValueError(f"Unexpected signal {sig}")

        with patch("os.kill", side_effect=mock_kill):
            stopped = self.dm.stop(timeout=1.0)
            self.assertTrue(stopped)
            self.assertEqual(signals_received, [signal.SIGTERM])
            self.assertFalse(self.dm.pid_file.exists())

    def test_daemon_stop_escalates_to_sigkill_on_timeout(self):
        """stop() escalates to SIGKILL if process does not exit within timeout, and cleans up PID file."""
        self.dm.pid_file.write_text("12345\n", encoding="utf-8")
        self._create_mock_proc(12345, b"python3\x00mock_watch.py\x00run\x00")

        alive = [True]
        signals_received = []

        def mock_kill(pid, sig):
            if sig == 0:
                if not alive[0]:
                    raise ProcessLookupError
                return None
            signals_received.append(sig)
            if sig == signal.SIGKILL:
                alive[0] = False
                return None
            return None

        with patch("os.kill", side_effect=mock_kill):
            stopped = self.dm.stop(timeout=0.05)
            self.assertTrue(stopped)
            self.assertEqual(signals_received, [signal.SIGTERM, signal.SIGKILL])
            self.assertFalse(self.dm.pid_file.exists())

    def test_daemon_stop_survives_sigkill_retains_pid_and_returns_false(self):
        """If daemon process survives SIGKILL, stop() does NOT delete watch.pid and returns False."""
        self.dm.pid_file.write_text("12345\n", encoding="utf-8")
        self._create_mock_proc(12345, b"python3\x00mock_watch.py\x00run\x00")

        def mock_kill(pid, sig):
            # Process survives both SIGTERM and SIGKILL
            return None

        with patch("os.kill", side_effect=mock_kill):
            stopped = self.dm.stop(timeout=0.05)
            self.assertFalse(stopped)
            self.assertTrue(self.dm.pid_file.exists())
            self.assertEqual(self.dm.pid_file.read_text(encoding="utf-8").strip(), "12345")

    def test_daemon_stop_default_timeout_is_5_seconds(self):
        """stop() uses 5 seconds default timeout before escalating to SIGKILL."""
        self.dm.pid_file.write_text("12345\n", encoding="utf-8")
        self._create_mock_proc(12345, b"python3\x00mock_watch.py\x00run\x00")

        signals_received = []
        simulated_time = [1000.0]

        def mock_time():
            return simulated_time[0]

        def mock_sleep(duration):
            simulated_time[0] += duration

        def mock_kill(pid, sig):
            if sig == 0:
                if signal.SIGKILL in signals_received:
                    raise ProcessLookupError
                return None
            signals_received.append(sig)
            return None

        with patch("time.time", side_effect=mock_time), patch("time.sleep", side_effect=mock_sleep), patch("os.kill", side_effect=mock_kill):
            stopped = self.dm.stop()
            self.assertTrue(stopped)
            self.assertEqual(signals_received, [signal.SIGTERM, signal.SIGKILL])
            self.assertGreaterEqual(simulated_time[0], 1005.0)
            self.assertFalse(self.dm.pid_file.exists())

    def test_daemon_stop_when_not_running_returns_false(self):
        """stop() returns False safely when no PID file exists."""
        self.assertFalse(self.dm.stop())

    def test_daemon_stop_prunes_stale_pid_file(self):
        """stop() removes stale PID file and returns False when process is already dead."""
        self.dm.pid_file.write_text("99999\n", encoding="utf-8")
        with patch("os.kill", side_effect=ProcessLookupError):
            self.assertFalse(self.dm.stop())
            self.assertFalse(self.dm.pid_file.exists())

    def test_daemon_status_running(self):
        """status() reports running with PID, log file, and targets when daemon is active."""
        self.dm.pid_file.write_text("12345\n", encoding="utf-8")
        self._create_mock_proc(12345, b"python3\x00mock_watch.py\x00run\x00")

        with patch("os.kill", return_value=None):
            st = self.dm.status()
            self.assertTrue(st["running"])
            self.assertEqual(st["pid"], 12345)
            self.assertEqual(st["log_file"], str(self.dm.log_file))
            self.assertIsInstance(st["targets"], list)

    def test_daemon_status_stopped(self):
        """status() reports stopped when no daemon is active."""
        st = self.dm.status()
        self.assertFalse(st["running"])
        self.assertIsNone(st["pid"])
        self.assertEqual(st["targets"], [])

    def test_daemon_status_prunes_stale_pid(self):
        """status() automatically purges dead/stale PID file and reports stopped."""
        self.dm.pid_file.write_text("99999\n", encoding="utf-8")
        with patch("os.kill", side_effect=ProcessLookupError):
            st = self.dm.status()
            self.assertFalse(st["running"])
            self.assertIsNone(st["pid"])
            self.assertFalse(self.dm.pid_file.exists())

    def test_daemon_pid_validation_cmdline_mismatch_prunes(self):
        """If PID exists in OS but cmdline doesn't match watch process, it is treated as stale and pruned."""
        self.dm.pid_file.write_text("12345\n", encoding="utf-8")
        self._create_mock_proc(12345, b"/usr/sbin/nginx\x00-g\x00daemon off;\x00")

        with patch("os.kill", return_value=None):
            st = self.dm.status()
            self.assertFalse(st["running"])
            self.assertFalse(self.dm.pid_file.exists())

    def test_daemon_corrupt_pid_file_pruned(self):
        """Non-integer content in watch.pid is cleaned up automatically."""
        self.dm.pid_file.write_text("corrupted_pid\n", encoding="utf-8")
        st = self.dm.status()
        self.assertFalse(st["running"])
        self.assertFalse(self.dm.pid_file.exists())

    def test_daemon_empty_pid_file_pruned(self):
        """Empty watch.pid is pruned automatically."""
        self.dm.pid_file.write_text("  \n", encoding="utf-8")
        st = self.dm.status()
        self.assertFalse(st["running"])
        self.assertFalse(self.dm.pid_file.exists())

    def test_generate_systemd_content(self):
        """generate_systemd() outputs a valid unit referencing %h/.local/bin/agy-switch watch run and vault paths."""
        unit = self.dm.generate_systemd()
        self.assertIn("[Unit]", unit)
        self.assertIn("Description=Antigravity Account Switcher Log Watcher Daemon", unit)
        self.assertIn("ExecStart=%h/.local/bin/agy-switch watch run", unit)
        self.assertIn("StandardOutput=append:%h/.antigravity-vault/watch.log", unit)
        self.assertIn("StandardError=append:%h/.antigravity-vault/watch.log", unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("RestartSec=5s", unit)
        self.assertIn("WantedBy=default.target", unit)

    def test_generate_systemd_writes_to_file(self):
        """generate_systemd(target_path=...) writes the unit file and creates parent directories."""
        target = self.base_dir / "user_systemd" / "agy-switch-watcher.service"
        unit = self.dm.generate_systemd(target_path=target)
        self.assertTrue(target.is_file())
        self.assertEqual(target.read_text(encoding="utf-8"), unit)

    def test_cli_watch_generate_systemd_prints_unit(self):
        """agy-switch watch --generate-systemd prints the valid systemd unit definition and returns 0."""
        from io import StringIO
        from scripts import agy_switch

        mock_engine = MagicMock()
        mock_engine.vault_dir = self.vault_dir

        out = StringIO()
        with patch("sys.stdout", out):
            code = agy_switch.main(["watch", "--generate-systemd"], engine=mock_engine)

        self.assertEqual(code, 0)
        output = out.getvalue()
        self.assertIn("ExecStart=%h/.local/bin/agy-switch watch run", output)
        self.assertIn("StandardOutput=append:%h/.antigravity-vault/watch.log", output)

    def test_cli_watch_generate_systemd_clean_stdout_when_auto_migration_runs(self):
        """Running agy-switch watch --generate-systemd produces 100% valid unit definition on stdout without diagnostic strings even when auto-migration runs."""
        from io import StringIO
        from scripts import agy_switch

        mock_engine = MagicMock()
        mock_engine.vault_dir = self.vault_dir
        mock_engine.auto_migrate_if_needed.return_value = ["migrated_acc1", "migrated_acc2"]

        out = StringIO()
        err = StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err):
            code = agy_switch.main(["watch", "--generate-systemd"], engine=mock_engine)

        self.assertEqual(code, 0)
        stdout_val = out.getvalue()
        stderr_val = err.getvalue()

        # stdout must strictly contain the unit definition and begin with [Unit]
        self.assertTrue(stdout_val.strip().startswith("[Unit]"))
        self.assertIn("ExecStart=%h/.local/bin/agy-switch watch run", stdout_val)
        self.assertNotIn("Auto-migrated", stdout_val)

        # Diagnostic auto-migration notification must be in stderr
        self.assertIn("Auto-migrated", stderr_val)
        self.assertIn("migrated_acc1", stderr_val)

    def test_cli_watch_start_stop_status_dispatch(self):
        """CLI agy-switch watch start/stop/status dispatches to DaemonManager."""
        from io import StringIO
        from scripts import agy_switch, watch

        mock_engine = MagicMock()
        mock_engine.vault_dir = self.vault_dir
        mock_engine.proc_dir = self.proc_dir
        mock_engine.gemini_dir = self.gemini_dir

        mock_proc = MagicMock(pid=8888)
        with patch("subprocess.Popen", return_value=mock_proc):
            out = StringIO()
            with patch("sys.stdout", out):
                code = agy_switch.main(["watch", "start", "--cooldown", "90"], engine=mock_engine)
            self.assertEqual(code, 0)
            self.assertIn("8888", out.getvalue())

        # Check status
        self._create_mock_proc(8888, b"python3\x00scripts/watch.py\x00run\x00")
        with patch("os.kill", return_value=None):
            out = StringIO()
            with patch("sys.stdout", out):
                code = agy_switch.main(["watch", "status"], engine=mock_engine)
            self.assertEqual(code, 0)
            self.assertIn("8888", out.getvalue())
            self.assertIn("Daemon is running", out.getvalue())

        # Check stop
        with patch("os.kill", side_effect=lambda pid, sig: None if sig in (0, signal.SIGTERM) else None):
            with patch.object(watch.DaemonManager, "is_process_alive", side_effect=[True, False, False]):
                out = StringIO()
                with patch("sys.stdout", out):
                    code = agy_switch.main(["watch", "stop"], engine=mock_engine)
                self.assertEqual(code, 0)
                self.assertIn("stopped successfully", out.getvalue())

    def test_real_subprocess_start_and_stop_lifecycle(self):
        """End-to-end integration test spawning a real python subprocess and stopping it with SIGTERM."""
        real_script = self.base_dir / "real_watch.py"
        real_script.write_text(
            "#!/usr/bin/env python3\n"
            "import time, sys, signal\n"
            "signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))\n"
            "while True:\n"
            "    time.sleep(0.1)\n",
            encoding="utf-8"
        )
        dm = DaemonManager(
            vault_dir=self.vault_dir,
            watch_script=real_script,
            gemini_dir=self.gemini_dir,
        )

        # 1. Start real daemon
        pid = dm.start()
        self.assertIsInstance(pid, int)
        self.assertTrue(dm.pid_file.is_file())

        try:
            # 2. Check status
            st = dm.status()
            self.assertTrue(st["running"])
            self.assertEqual(st["pid"], pid)

            # 3. Reject duplicate start
            with self.assertRaises(DaemonAlreadyRunningError):
                dm.start()
        finally:
            # 4. Stop real daemon
            stopped = dm.stop(timeout=3.0)
            self.assertTrue(stopped)
            self.assertFalse(dm.pid_file.exists())
            self.assertFalse(dm.status()["running"])

    def test_daemon_cmdline_validation_rejects_innocent_processes(self):
        """Processes whose cmdline merely references watch.py as an argument (vim, git, train.py) are rejected."""
        # 1. Text editor editing watch.py
        self._create_mock_proc(20001, b"/usr/bin/vim\x00scripts/watch.py\x00")
        self.dm.pid_file.write_text("20001\n", encoding="utf-8")
        with patch("os.kill", return_value=None):
            self.assertFalse(self.dm.is_process_alive(20001))
            st = self.dm.status()
            self.assertFalse(st["running"])
            self.assertFalse(self.dm.pid_file.exists())

        # 2. Git diff on watch.py
        self._create_mock_proc(20002, b"git\x00diff\x00scripts/watch.py\x00")
        self.dm.pid_file.write_text("20002\n", encoding="utf-8")
        with patch("os.kill", return_value=None):
            self.assertFalse(self.dm.is_process_alive(20002))
            self.assertIsNone(self.dm.get_running_pid())
            self.assertFalse(self.dm.pid_file.exists())

        # 3. Python script running an unrelated script with watch.py as an argument flag
        self._create_mock_proc(20003, b"python3\x00train.py\x00--data\x00scripts/watch.py\x00")
        self.dm.pid_file.write_text("20003\n", encoding="utf-8")
        with patch("os.kill", return_value=None):
            self.assertFalse(self.dm.is_process_alive(20003))
            self.assertIsNone(self.dm.get_running_pid())
            self.assertFalse(self.dm.pid_file.exists())

    def test_daemon_is_process_alive_permission_error_returns_false(self):
        """PermissionError on os.kill(pid, 0) means the process belongs to another user and is not our daemon."""
        self.dm.pid_file.write_text("10001\n", encoding="utf-8")
        with patch("os.kill", side_effect=PermissionError("Operation not permitted")):
            self.assertFalse(self.dm.is_process_alive(10001))
            self.assertIsNone(self.dm.get_running_pid())
            self.assertFalse(self.dm.pid_file.exists())

    def test_daemon_is_process_alive_pid_one_and_self_pid_rejected(self):
        """PID 1 (init) and self process PID are never recognized as valid background watch daemons."""
        self.assertFalse(self.dm.is_process_alive(1))
        self.assertFalse(self.dm.is_process_alive(os.getpid()))

        self.dm.pid_file.write_text("1\n", encoding="utf-8")
        with patch("os.kill", return_value=None):
            self.assertIsNone(self.dm.get_running_pid())
            self.assertFalse(self.dm.pid_file.exists())

    def test_daemon_broken_symlink_pid_file_cleaned(self):
        """Broken symlink for watch.pid is cleaned up safely without throwing exceptions."""
        broken_target = self.base_dir / "nonexistent_target_pid"
        self.dm.pid_file.symlink_to(broken_target)
        self.assertTrue(self.dm.pid_file.is_symlink())

        self.assertIsNone(self.dm.get_running_pid())
        self.assertFalse(self.dm.pid_file.exists())
        self.assertFalse(self.dm.pid_file.is_symlink())

    def test_daemon_start_orphaned_process_cleaned_on_pid_write_failure(self):
        """If writing watch.pid fails after Popen, the spawned background process is terminated to prevent leaks."""
        mock_proc = MagicMock(pid=33333)
        with patch("subprocess.Popen", return_value=mock_proc):
            with patch.object(self.dm, "_write_pid_file", side_effect=OSError("Disk full")):
                with self.assertRaises(OSError):
                    self.dm.start()
                mock_proc.terminate.assert_called_once()

    def test_daemon_negative_and_zero_pid_pruned(self):
        """Negative and zero PIDs in watch.pid are recognized as invalid and pruned."""
        for invalid_pid in ("-50\n", "0\n"):
            self.dm.pid_file.write_text(invalid_pid, encoding="utf-8")
            st = self.dm.status()
            self.assertFalse(st["running"])
            self.assertIsNone(st["pid"])
            self.assertFalse(self.dm.pid_file.exists())

    def test_stop_permission_error_handles_gracefully(self):
        """stop() retains watch.pid, emits error to stderr, and returns False when PermissionError is raised on os.kill."""
        from io import StringIO
        self.dm.pid_file.write_text("12345\n", encoding="utf-8")
        err_out = StringIO()
        with patch.object(self.dm, "get_running_pid", return_value=12345):
            with patch("os.kill", side_effect=PermissionError("Access denied")), patch("sys.stderr", err_out):
                res = self.dm.stop()
                self.assertFalse(res)
                self.assertTrue(self.dm.pid_file.exists())
                self.assertIn("Permission denied", err_out.getvalue())

    def test_daemon_start_log_file_symlink_cleaned(self):
        """If watch.log is a pre-existing symlink, start() unlinks it to prevent writing through symlinks."""
        symlink_target = self.base_dir / "some_target.log"
        symlink_target.write_text("initial target content\n", encoding="utf-8")
        self.dm.log_file.symlink_to(symlink_target)
        self.assertTrue(self.dm.log_file.is_symlink())

        mock_proc = MagicMock(pid=44444)
        with patch("subprocess.Popen", return_value=mock_proc):
            pid = self.dm.start()
            self.assertEqual(pid, 44444)
            self.assertFalse(self.dm.log_file.is_symlink())
            self.assertTrue(self.dm.log_file.is_file())
            self.assertEqual(self.dm.log_file.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()

