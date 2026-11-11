#!/usr/bin/env python3
"""End-to-End Headless Verification across Supervisor, Watcher, and CLI (Ticket 08)."""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from agy_switch import (
    ActiveTurnRunningError,
    AgySwitchEngine,
    LogWatcher,
    PresenceDetector,
    ProcessSupervisor,
    format_accounts_listicle,
    format_accounts_table,
    main,
)


class TestHeadlessLifecycleE2E(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name)

        self.vault_dir = self.base_dir / ".antigravity-vault"
        self.gemini_dir = self.base_dir / ".gemini"
        self.proc_dir = self.base_dir / "proc"

        self.vault_dir.mkdir(parents=True, exist_ok=True)
        self.gemini_dir.mkdir(parents=True, exist_ok=True)
        self.proc_dir.mkdir(parents=True, exist_ok=True)

        self.engine = AgySwitchEngine(vault_dir=str(self.vault_dir), gemini_dir=str(self.gemini_dir))
        self.supervisor = ProcessSupervisor(
            gemini_dir=self.gemini_dir,
            proc_dir=self.proc_dir,
        )
        self.detector = PresenceDetector(
            gemini_dir=self.gemini_dir,
            proc_dir=self.proc_dir,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_req_1_scn_003_coexistence_and_lifecycle(self) -> None:
        """[REQ-1-SCN-003]: Stale locks pruned across directories, active lock retained, and server reported busy."""
        desktop_presence = self.gemini_dir / "antigravity" / "presence"
        cli_presence = self.gemini_dir / "antigravity-cli" / "presence"
        desktop_presence.mkdir(parents=True, exist_ok=True)
        cli_presence.mkdir(parents=True, exist_ok=True)

        stale_desktop_lock = desktop_presence / "stale.lock"
        stale_desktop_lock.touch()
        notes_file = desktop_presence / "notes.txt"
        notes_file.touch()

        active_lock = cli_presence / "active.lock"
        active_lock.touch()
        stale_cli_lock = cli_presence / "stale_cli.lock"
        stale_cli_lock.touch()
        ds_store = cli_presence / ".DS_Store"
        ds_store.touch()

        pid_fd_dir = self.proc_dir / "5100" / "fd"
        pid_fd_dir.mkdir(parents=True, exist_ok=True)
        (pid_fd_dir / "10").symlink_to(active_lock)

        self.assertTrue(self.supervisor.is_server_busy())
        self.assertFalse(stale_desktop_lock.exists(), "stale.lock must be pruned from disk")
        self.assertFalse(stale_cli_lock.exists(), "stale_cli.lock must be pruned from disk")
        self.assertTrue(active_lock.exists(), "active.lock must be retained on disk")
        self.assertTrue(notes_file.exists(), "non-lock files must never be pruned")
        self.assertTrue(ds_store.exists(), "hidden system files must never be pruned")

        self.assertTrue(self.supervisor.is_server_busy())
        self.assertTrue(active_lock.exists())

        with self.assertRaises(ActiveTurnRunningError):
            self.supervisor.verify_turn_safety(force=False, pid=5100)
        self.supervisor.verify_turn_safety(force=True, pid=5100)

        (pid_fd_dir / "10").unlink()
        self.assertFalse(self.supervisor.is_server_busy())
        self.assertFalse(active_lock.exists(), "orphaned active.lock must be unlinked after process termination")

    def test_req_2_scn_007_dynamic_session_switch_and_cursor_cleanup(self) -> None:
        """[REQ-2-SCN-007]: Dynamic session switch between Desktop and CLI clears old transcript cursors cleanly while retaining log cursors."""
        desktop_brain = self.gemini_dir / "antigravity" / "brain" / "desk-turn" / ".system_generated" / "logs"
        desktop_brain.mkdir(parents=True, exist_ok=True)
        desktop_transcript = desktop_brain / "transcript.jsonl"
        desktop_transcript.write_text('{"event": "start_desktop"}\n', encoding="utf-8")

        desktop_presence = self.gemini_dir / "antigravity" / "presence" / "desk-turn.lock"
        desktop_presence.parent.mkdir(parents=True, exist_ok=True)
        desktop_presence.touch()
        os.utime(desktop_presence, (1000.0, 1000.0))

        cli_log_dir = self.gemini_dir / "antigravity-cli" / "log"
        cli_log_dir.mkdir(parents=True, exist_ok=True)
        runner_log = cli_log_dir / "runner.log"
        runner_log.write_text("runner started\n", encoding="utf-8")

        mock_engine = MagicMock()
        watcher = LogWatcher(engine=mock_engine, presence_detector=self.detector)

        targets_initial = watcher.scan_active_targets()
        self.assertIn(str(desktop_transcript), targets_initial)
        self.assertEqual(watcher.active_session_id, "desk-turn")
        self.assertEqual(watcher.active_transcript_path, desktop_transcript)

        watcher.file_cursors[str(desktop_transcript)] = 100
        watcher.file_cursors[str(runner_log)] = 50

        cli_brain = self.gemini_dir / "antigravity-cli" / "brain" / "cli-turn" / ".system_generated" / "logs"
        cli_brain.mkdir(parents=True, exist_ok=True)
        cli_transcript = cli_brain / "transcript.jsonl"
        cli_transcript.write_text('{"event": "start_cli"}\n', encoding="utf-8")

        cli_presence = self.gemini_dir / "antigravity-cli" / "presence" / "cli-turn.lock"
        cli_presence.parent.mkdir(parents=True, exist_ok=True)
        cli_presence.touch()
        os.utime(cli_presence, (2000.0, 2000.0))

        targets_updated = watcher.scan_active_targets()
        self.assertEqual(watcher.active_session_id, "cli-turn")
        self.assertEqual(watcher.active_transcript_path, cli_transcript)
        self.assertIn(str(cli_transcript), targets_updated)
        self.assertNotIn(str(desktop_transcript), targets_updated)
        self.assertEqual(len(targets_updated), len(set(targets_updated)))
        self.assertNotIn(str(desktop_transcript), watcher.file_cursors)
        self.assertEqual(watcher.file_cursors.get(str(runner_log)), 50)

        watcher.file_cursors[str(cli_transcript)] = 200

        desktop_brain_2 = self.gemini_dir / "antigravity" / "brain" / "desk-turn-2" / ".system_generated" / "logs"
        desktop_brain_2.mkdir(parents=True, exist_ok=True)
        desktop_transcript_2 = desktop_brain_2 / "transcript.jsonl"
        desktop_transcript_2.write_text('{"event": "start_desktop_2"}\n', encoding="utf-8")

        desktop_presence_2 = self.gemini_dir / "antigravity" / "presence" / "desk-turn-2.lock"
        desktop_presence_2.touch()
        os.utime(desktop_presence_2, (3000.0, 3000.0))

        targets_bidirectional = watcher.scan_active_targets()
        self.assertEqual(watcher.active_session_id, "desk-turn-2")
        self.assertEqual(watcher.active_transcript_path, desktop_transcript_2)
        self.assertIn(str(desktop_transcript_2), targets_bidirectional)
        self.assertNotIn(str(cli_transcript), targets_bidirectional)
        self.assertNotIn(str(cli_transcript), watcher.file_cursors)
        self.assertEqual(watcher.file_cursors.get(str(runner_log)), 50)

        desktop_presence.unlink(missing_ok=True)
        desktop_presence_2.unlink(missing_ok=True)
        cli_presence.unlink(missing_ok=True)

        targets_idle = watcher.scan_active_targets()
        self.assertIsNone(watcher.active_session_id)
        self.assertIsNone(watcher.active_transcript_path)
        self.assertNotIn(str(desktop_transcript_2), targets_idle)
        self.assertIn(str(runner_log), targets_idle)
        self.assertNotIn(str(desktop_transcript_2), watcher.file_cursors)
        self.assertEqual(watcher.file_cursors.get(str(runner_log)), 50)

    def test_e2e_cli_model_quota_formatting(self) -> None:
        """Integration verification of table and listicle formatters with target_model parameter."""
        data = {
            "active": "acc-prod",
            "accounts": [
                {
                    "name": "acc-prod",
                    "email": "prod@example.com",
                    "is_active": True,
                    "valid": True,
                    "has_quota": True,
                    "quota": {
                        "gemini": {"quota": 90, "cached": False, "reset_time": 1800000000},
                        "claude": {"quota": 30, "cached": False, "reset_time": 1800000000},
                    },
                }
            ]
        }

        table_claude = format_accounts_table(data, show_quota=True, target_model="claude")
        self.assertIn("CLAUDE QUOTA", table_claude)
        self.assertIn("30%", table_claude)

        table_gemini = format_accounts_table(data, show_quota=True, target_model="gemini")
        self.assertIn("GEMINI QUOTA", table_gemini)
        self.assertIn("90%", table_gemini)

        listicle_claude = format_accounts_listicle(data, show_quota=True, target_model="claude")
        self.assertIn("Claude: 30%", listicle_claude)

        listicle_gemini = format_accounts_listicle(data, show_quota=True, target_model="gemini")
        self.assertIn("Gemini: 90%", listicle_gemini)

    def test_e2e_cli_main_quota_command_dispatch(self) -> None:
        """Integration verification of CLI router and target_model parameter forwarding through main()."""
        acc_dir = self.vault_dir / "accounts" / "acc-prod"
        acc_dir.mkdir(parents=True, exist_ok=True)
        (acc_dir / "token.json").write_text(json.dumps({
            "access_token": "mock-token",
            "refresh_token": "mock-refresh",
            "email": "prod@example.com",
        }), encoding="utf-8")
        (self.vault_dir / "active").write_text("acc-prod\n", encoding="utf-8")

        cache_dir = self.vault_dir / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "last_known_quota.json").write_text(json.dumps({
            "accounts": {
                "acc-prod": {
                    "gemini": {"quota": 85, "reset_time": 1800000000},
                    "claude": {"quota": 25, "reset_time": 1800000000},
                }
            }
        }), encoding="utf-8")

        buf_claude = io.StringIO()
        with patch("sys.stderr", buf_claude):
            exit_code = main(["quota", "--model", "claude"], engine=self.engine)
        self.assertEqual(exit_code, 1)
        self.assertIn("Unsupported model 'claude'", buf_claude.getvalue())

        buf_gemini = io.StringIO()
        with patch("sys.stdout", buf_gemini):
            exit_code = main(["quota", "--model", "gemini"], engine=self.engine)
        self.assertEqual(exit_code, 0)
        self.assertIn("GEMINI QUOTA", buf_gemini.getvalue())
        self.assertIn("85%", buf_gemini.getvalue())

        buf_err = io.StringIO()
        with patch("sys.stderr", buf_err):
            exit_code = main(["quota", "--model", "unsupported-model"], engine=self.engine)
        self.assertEqual(exit_code, 1)
        self.assertIn("Unsupported model 'unsupported-model'", buf_err.getvalue())


if __name__ == "__main__":
    unittest.main()
