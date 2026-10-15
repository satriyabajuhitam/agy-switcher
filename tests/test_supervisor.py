"""Unit tests for ProcessSupervisor, Layer 0 foundation models/exceptions, and Layer 1 formatters."""

import os
import signal
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import sys

# Add scripts directory to sys.path so agy_switcher can be imported
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# Imports from new modular sub-package
from agy_switcher.exceptions import (
    AgySwitchError,
    InvalidTokenError,
    AccountNotFoundError,
    ActiveTurnRunningError,
    DaemonError,
    DaemonAlreadyRunningError,
)
from agy_switcher.models import (
    DiagnosticItem,
    DiagnosticReport,
    QuotaStatus,
    EvaluationCandidate,
)
from agy_switcher.supervisor import ProcessSupervisor
from agy_switcher.formatters import (
    format_countdown,
    format_accounts_listicle,
    format_accounts_table,
    format_status_report,
    format_diagnostic_report,
    _format_bucket_countdown,
    _format_gemini_quota_summary,
)


class TestLayer0Exceptions(unittest.TestCase):
    def test_inheritance_hierarchy(self):
        self.assertTrue(issubclass(AgySwitchError, Exception))
        self.assertTrue(issubclass(InvalidTokenError, AgySwitchError))
        self.assertTrue(issubclass(AccountNotFoundError, AgySwitchError))
        self.assertTrue(issubclass(ActiveTurnRunningError, AgySwitchError))
        self.assertTrue(issubclass(DaemonError, AgySwitchError))
        self.assertTrue(issubclass(DaemonError, RuntimeError))
        self.assertTrue(issubclass(DaemonAlreadyRunningError, DaemonError))

    def test_daemon_already_running_error_pid(self):
        err = DaemonAlreadyRunningError(pid=12345)
        self.assertEqual(err.pid, 12345)
        self.assertIn("12345", str(err))

        custom_err = DaemonAlreadyRunningError(pid=999, message="Custom daemon error")
        self.assertEqual(custom_err.pid, 999)
        self.assertEqual(str(custom_err), "Custom daemon error")


class TestLayer0Models(unittest.TestCase):
    def test_diagnostic_item_fields_and_to_dict(self):
        # Initialized with layer and title
        item = DiagnosticItem(
            layer=1,
            title="Vault Permissions",
            status="PASS",
            message="Secure",
            can_fix=False,
        )
        self.assertEqual(item.layer, 1)
        self.assertEqual(item.title, "Vault Permissions")
        self.assertEqual(item.name, "Vault Permissions")
        self.assertEqual(item.category, "permissions")
        self.assertEqual(item.status, "PASS")
        self.assertEqual(item.message, "Secure")
        self.assertFalse(item.can_fix)
        self.assertFalse(item.fixed)
        self.assertIsNone(item.fix_action)

        d = item.to_dict()
        self.assertEqual(d["layer"], 1)
        self.assertEqual(d["title"], "Vault Permissions")
        self.assertEqual(d["name"], "Vault Permissions")
        self.assertEqual(d["category"], "permissions")
        self.assertEqual(d["status"], "PASS")

        # Initialized with category and name (backward compatibility)
        item2 = DiagnosticItem(
            category="token_integrity",
            name="JWT Token Check",
            status="WARN",
            message="Token expiring soon",
            can_fix=True,
            fixed=True,
            fix_action="Refreshed token",
        )
        self.assertEqual(item2.layer, 2)
        self.assertEqual(item2.title, "JWT Token Check")
        self.assertTrue(item2.can_fix)
        self.assertTrue(item2.fixed)
        self.assertEqual(item2.fix_action, "Refreshed token")

    def test_diagnostic_report_auto_exit_code_and_status(self):
        # All PASS
        p_item = DiagnosticItem(layer=1, title="Perms", status="PASS", message="OK")
        rep_pass = DiagnosticReport(items=[p_item])
        self.assertEqual(rep_pass.overall_status, "PASS")
        self.assertEqual(rep_pass.exit_code, 0)

        # Has WARN
        w_item = DiagnosticItem(layer=2, title="JWT", status="WARN", message="Warn")
        rep_warn = DiagnosticReport(items=[p_item, w_item])
        self.assertEqual(rep_warn.overall_status, "WARN")
        self.assertEqual(rep_warn.exit_code, 1)

        # Has FAIL
        f_item = DiagnosticItem(layer=3, title="Sync", status="FAIL", message="Fail")
        rep_fail = DiagnosticReport(items=[p_item, w_item, f_item])
        self.assertEqual(rep_fail.overall_status, "FAIL")
        self.assertEqual(rep_fail.exit_code, 2)

        # Explicit override
        rep_explicit = DiagnosticReport(items=[f_item], overall_status="WARN", exit_code=1)
        self.assertEqual(rep_explicit.overall_status, "WARN")
        self.assertEqual(rep_explicit.exit_code, 1)

        d = rep_fail.to_dict()
        self.assertEqual(d["overall_status"], "FAIL")
        self.assertEqual(d["exit_code"], 2)
        self.assertEqual(d["summary"]["total"], 3)
        self.assertEqual(d["summary"]["passed"], 1)
        self.assertEqual(d["summary"]["warned"], 1)
        self.assertEqual(d["summary"]["failed"], 1)

    def test_quota_status_methods(self):
        reset_time = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)
        curr_time = datetime(2026, 10, 15, 11, 30, 0, tzinfo=timezone.utc)

        status_available = QuotaStatus(fraction=0.85, reset_time=reset_time)
        self.assertFalse(status_available.is_exhausted())
        self.assertTrue(status_available.has_available_quota())
        self.assertEqual(status_available.remaining_percentage(), 85)
        self.assertAlmostEqual(status_available.get_reset_delta_minutes(curr_time), 30.0)

        status_exhausted = QuotaStatus(fraction=0.0, reset_time=reset_time)
        self.assertTrue(status_exhausted.is_exhausted())
        self.assertFalse(status_exhausted.has_available_quota())
        self.assertEqual(status_exhausted.remaining_percentage(), 0)

    def test_evaluation_candidate(self):
        reset_time = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)
        qs = QuotaStatus(fraction=0.5, reset_time=reset_time)
        candidate = EvaluationCandidate(account_id="work-acc", status=qs, reset_delta=30.0)
        self.assertEqual(candidate.account_id, "work-acc")
        self.assertEqual(candidate.status, qs)
        self.assertEqual(candidate.reset_delta, 30.0)


class TestProcessSupervisor(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.proc_dir = Path(self.temp_dir.name) / "proc"
        self.gemini_dir = Path(self.temp_dir.name) / "gemini"
        self.proc_dir.mkdir(parents=True)
        self.gemini_dir.mkdir(parents=True)
        self.supervisor = ProcessSupervisor(gemini_dir=self.gemini_dir, proc_dir=self.proc_dir)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_locate_language_server_pid_from_proc(self):
        # 1. No matching process
        self.assertIsNone(self.supervisor.locate_language_server_pid())

        # 2. Unrelated process
        pid101 = self.proc_dir / "101"
        pid101.mkdir()
        (pid101 / "cmdline").write_bytes(b"/usr/bin/python3\x00some_script.py\x00")
        self.assertIsNone(self.supervisor.locate_language_server_pid())

        # 3. Matching language_server with --standalone
        pid202 = self.proc_dir / "202"
        pid202.mkdir()
        (pid202 / "cmdline").write_bytes(b"/opt/antigravity/language_server\x00--standalone\x00--port\x008080\x00")
        self.assertEqual(self.supervisor.locate_language_server_pid(), 202)

    def test_is_server_busy_with_presence_locks(self):
        presence_dir = self.gemini_dir / "antigravity" / "presence"
        presence_dir.mkdir(parents=True)

        # No locks initially
        self.assertFalse(self.supervisor.is_server_busy())

        # With lock file
        lock_file = presence_dir / "session.lock"
        lock_file.touch()
        self.assertTrue(self.supervisor.is_server_busy())

    def test_is_server_busy_with_child_process(self):
        # PID 202 is language server, PID 303 is child process
        child_dir = self.proc_dir / "303"
        child_dir.mkdir()
        # stat format: pid (name) state ppid ...
        (child_dir / "stat").write_text("303 (subtask) S 202 0 0 0 0")
        self.assertTrue(self.supervisor.is_server_busy(pid=202))

        # Zombie state ('Z') should not count as busy
        (child_dir / "stat").write_text("303 (subtask) Z 202 0 0 0 0")
        self.assertFalse(self.supervisor.is_server_busy(pid=202))

    def test_verify_turn_safety(self):
        # When server is idle
        self.supervisor.verify_turn_safety(force=False)

        # When server is busy
        presence_dir = self.gemini_dir / "antigravity" / "presence"
        presence_dir.mkdir(parents=True)
        (presence_dir / "turn.lock").touch()

        # Mock locate_language_server_pid to return active PID
        with patch.object(self.supervisor, "locate_language_server_pid", return_value=123):
            # Without force: raises ActiveTurnRunningError
            with self.assertRaises(ActiveTurnRunningError):
                self.supervisor.verify_turn_safety(force=False)

            # With force: succeeds without raising
            self.supervisor.verify_turn_safety(force=True)

    def test_restart_language_server(self):
        # Server not running
        with patch.object(self.supervisor, "locate_language_server_pid", return_value=None):
            self.assertFalse(self.supervisor.restart_language_server())

        # Server running and idle
        with patch.object(self.supervisor, "locate_language_server_pid", return_value=456):
            with patch.object(self.supervisor, "is_server_busy", return_value=False):
                with patch.object(self.supervisor, "_send_signal") as mock_send:
                    with patch.object(self.supervisor, "_poll_process_exit", return_value=True) as mock_poll:
                        res = self.supervisor.restart_language_server(force=False)
                        self.assertTrue(res)
                        mock_send.assert_called_once_with(456, signal.SIGTERM)
                        mock_poll.assert_called_once_with(456, timeout=3.0)

        # Server running and busy without force
        with patch.object(self.supervisor, "locate_language_server_pid", return_value=456):
            with patch.object(self.supervisor, "is_server_busy", return_value=True):
                with self.assertRaises(ActiveTurnRunningError):
                    self.supervisor.restart_language_server(force=False)

    def test_restart_language_server_force_clean_sigterm_no_sigkill(self):
        with patch.object(self.supervisor, "locate_language_server_pid", return_value=456):
            with patch.object(self.supervisor, "is_server_busy", return_value=False):
                with patch.object(self.supervisor, "_send_signal") as mock_send:
                    with patch.object(self.supervisor, "_poll_process_exit", return_value=True) as mock_poll:
                        res = self.supervisor.restart_language_server(force=True)
                        self.assertTrue(res)
                        mock_send.assert_called_once_with(456, signal.SIGTERM)
                        mock_poll.assert_called_once_with(456, timeout=3.0)

    def test_restart_language_server_sigkill_escalation_on_timeout(self):
        with patch.object(self.supervisor, "locate_language_server_pid", return_value=456):
            with patch.object(self.supervisor, "is_server_busy", return_value=False):
                with patch.object(self.supervisor, "_send_signal") as mock_send:
                    with patch.object(self.supervisor, "_poll_process_exit", side_effect=[False, True]) as mock_poll:
                        res = self.supervisor.restart_language_server(force=True)
                        self.assertTrue(res)
                        mock_send.assert_has_calls([call(456, signal.SIGTERM), call(456, signal.SIGKILL)], any_order=False)
                        mock_poll.assert_has_calls([call(456, timeout=3.0), call(456, timeout=1.0)], any_order=False)

    def test_restart_language_server_sigkill_escalation_fails_after_sigkill(self):
        with patch.object(self.supervisor, "locate_language_server_pid", return_value=456):
            with patch.object(self.supervisor, "is_server_busy", return_value=False):
                with patch.object(self.supervisor, "_send_signal") as mock_send:
                    with patch.object(self.supervisor, "_poll_process_exit", side_effect=[False, False]) as mock_poll:
                        res = self.supervisor.restart_language_server(force=True)
                        self.assertFalse(res)
                        mock_send.assert_has_calls([call(456, signal.SIGTERM), call(456, signal.SIGKILL)], any_order=False)
                        mock_poll.assert_has_calls([call(456, timeout=3.0), call(456, timeout=1.0)], any_order=False)

    def test_restart_language_server_no_escalation_when_not_force(self):
        with patch.object(self.supervisor, "locate_language_server_pid", return_value=456):
            with patch.object(self.supervisor, "is_server_busy", return_value=False):
                with patch.object(self.supervisor, "_send_signal") as mock_send:
                    with patch.object(self.supervisor, "_poll_process_exit", return_value=False) as mock_poll:
                        res = self.supervisor.restart_language_server(force=False)
                        self.assertFalse(res)
                        mock_send.assert_called_once_with(456, signal.SIGTERM)
                        mock_poll.assert_called_once_with(456, timeout=3.0)

    def test_restart_language_server_sigkill_handles_oserror(self):
        with patch.object(self.supervisor, "locate_language_server_pid", return_value=456):
            with patch.object(self.supervisor, "is_server_busy", return_value=False):
                with patch.object(self.supervisor, "_send_signal", side_effect=[None, OSError("Process died")]) as mock_send:
                    with patch.object(self.supervisor, "_poll_process_exit", side_effect=[False, True]) as mock_poll:
                        res = self.supervisor.restart_language_server(force=True)
                        self.assertTrue(res)
                        mock_send.assert_has_calls([call(456, signal.SIGTERM), call(456, signal.SIGKILL)], any_order=False)
                        mock_poll.assert_has_calls([call(456, timeout=3.0), call(456, timeout=1.0)], any_order=False)

    def test_restart_language_server_force_when_busy(self):
        with patch.object(self.supervisor, "locate_language_server_pid", return_value=456):
            with patch.object(self.supervisor, "is_server_busy", return_value=True):
                with patch.object(self.supervisor, "_send_signal") as mock_send:
                    with patch.object(self.supervisor, "_poll_process_exit", return_value=True) as mock_poll:
                        res = self.supervisor.restart_language_server(force=True)
                        self.assertTrue(res)
                        mock_send.assert_called_once_with(456, signal.SIGTERM)
                        mock_poll.assert_called_once_with(456, timeout=3.0)

    def test_restart_language_server_force_pid_none(self):
        with patch.object(self.supervisor, "locate_language_server_pid", return_value=None):
            self.assertFalse(self.supervisor.restart_language_server(force=True))

    def test_restart_language_server_sigterm_process_lookup_error(self):
        with patch.object(self.supervisor, "locate_language_server_pid", return_value=456):
            with patch.object(self.supervisor, "is_server_busy", return_value=False):
                with patch.object(self.supervisor, "_send_signal", side_effect=ProcessLookupError("Process disappeared")):
                    with patch.object(self.supervisor, "_poll_process_exit") as mock_poll:
                        res = self.supervisor.restart_language_server(force=True)
                        self.assertTrue(res)
                        mock_poll.assert_not_called()

    def test_poll_process_exit_permission_error_returns_false(self):
        with patch("os.kill", side_effect=PermissionError(1, "Operation not permitted")):
            self.assertFalse(self.supervisor._poll_process_exit(456, timeout=0.1))




class TestLayer1Formatters(unittest.TestCase):
    def test_format_countdown(self):
        curr = 1700000000.0
        self.assertEqual(format_countdown(None, curr), "-")
        self.assertEqual(format_countdown(curr - 10, curr), "expired")
        self.assertEqual(format_countdown(curr + 300, curr), "5m")
        self.assertEqual(format_countdown(curr + 3600 + 120, curr), "1h 2m")
        self.assertEqual(format_countdown(curr + 3600, curr), "1h")
        self.assertEqual(format_countdown(curr + 86400 + 7200, curr), "1d 2h")
        self.assertEqual(format_countdown(curr + 86400, curr), "1d")

    def test_format_gemini_quota_summary(self):
        # Empty / error
        self.assertEqual(_format_gemini_quota_summary({}), "Error / Unreachable")

        # Cached
        acc_cached = {"quota": {"gemini": {"quota": 80, "cached": True}}}
        self.assertEqual(_format_gemini_quota_summary(acc_cached), "80% (Cached)")

        # With buckets
        curr = 1700000000.0
        acc_buckets = {
            "quota": {
                "gemini": {
                    "quota": 90,
                    "buckets": {
                        "5h": {"quota": 90, "reset_time": curr + 3600},
                        "weekly": {"quota": 70, "reset_time": curr + 86400},
                    },
                }
            }
        }
        summary = _format_gemini_quota_summary(acc_buckets, current_timestamp=curr)
        self.assertIn("90%", summary)
        self.assertIn("5h: 90% (in 1h)", summary)
        self.assertIn("wk: 70% (in 1d)", summary)

    def test_format_accounts_listicle_and_table(self):
        data = {
            "active": "acc1",
            "accounts": [
                {"name": "acc1", "email": "acc1@example.com", "is_active": True, "valid": True},
                {"name": "acc2", "email": "acc2@example.com", "is_active": False, "valid": True},
            ],
        }
        listicle = format_accounts_listicle(data, show_quota=False)
        self.assertIn("• [*] acc1 (acc1@example.com) [Active]", listicle)
        self.assertIn("• [ ] acc2 (acc2@example.com) [Standby]", listicle)
        self.assertIn("Active Account: acc1", listicle)

        table = format_accounts_table(data, show_quota=False)
        self.assertIn("* acc1", table)
        self.assertIn("  acc2", table)
        self.assertIn("Active Account: acc1", table)

    def test_format_status_report(self):
        status = {
            "active": "primary",
            "email": "user@gmail.com",
            "server_running": True,
            "server_pid": 1234,
            "server_busy": False,
        }
        rep = format_status_report(status)
        self.assertIn("Active Account    : primary (user@gmail.com)", rep)
        self.assertIn("Language Server   : Running (PID: 1234)", rep)
        self.assertIn("Server State      : Idle (Safe to restart)", rep)

    def test_format_diagnostic_report(self):
        item1 = DiagnosticItem(layer=1, title="Perms", status="PASS", message="Secure")
        item2 = DiagnosticItem(layer=2, title="Token", status="FAIL", message="Invalid token for 'acc1'", can_fix=False)
        report = DiagnosticReport(items=[item1, item2])
        out = format_diagnostic_report(report)
        self.assertIn("1. [PASS] Perms: Secure", out)
        self.assertIn("2. [FAIL] Token: Invalid token for 'acc1'", out)
        self.assertIn("Action required: Run 'agy-switch login acc1'", out)
        self.assertIn("Failures     : 1", out)


class TestLayerDAGCompliance(unittest.TestCase):
    """Static and dynamic verification of Strict 5-Layer DAG compliance."""

    def test_ast_import_isolation(self):
        import ast

        switcher_dir = REPO_ROOT / "scripts" / "agy_switcher"

        # Layer 0 files: exceptions.py, models.py
        for l0_file in ["exceptions.py", "models.py"]:
            tree = ast.parse((switcher_dir / l0_file).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertFalse(alias.name.startswith("agy_switcher"), f"Layer 0 {l0_file} must not import from agy_switcher: {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        self.assertFalse(node.module.startswith("agy_switcher"), f"Layer 0 {l0_file} must not import from agy_switcher: {node.module}")

        # Layer 1 files: supervisor.py, formatters.py
        for l1_file in ["supervisor.py", "formatters.py"]:
            tree = ast.parse((switcher_dir / l1_file).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        for forbidden in ["vault", "telemetry", "rotation", "watcher", "doctor", "hook", "cli"]:
                            self.assertNotIn(forbidden, alias.name, f"Layer 1 {l1_file} imported forbidden Layer >= 2 module: {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        for forbidden in ["vault", "telemetry", "rotation", "watcher", "doctor", "hook", "cli"]:
                            self.assertNotIn(forbidden, node.module, f"Layer 1 {l1_file} imported forbidden Layer >= 2 module: {node.module}")

    def test_subprocess_standalone_import_isolation(self):
        import subprocess

        commands = [
            "import sys; sys.path.insert(0, 'scripts'); import agy_switcher.exceptions; assert 'agy_switcher.models' not in sys.modules; assert 'agy_switch' not in sys.modules",
            "import sys; sys.path.insert(0, 'scripts'); import agy_switcher.models; assert 'agy_switcher.supervisor' not in sys.modules; assert 'agy_switch' not in sys.modules",
            "import sys; sys.path.insert(0, 'scripts'); import agy_switcher.supervisor; assert 'agy_switcher.vault' not in sys.modules; assert 'agy_switch' not in sys.modules",
            "import sys; sys.path.insert(0, 'scripts'); import agy_switcher.formatters; assert 'agy_switcher.vault' not in sys.modules; assert 'agy_switch' not in sys.modules",
        ]

        for cmd in commands:
            res = subprocess.run(
                ["python3", "-c", cmd],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 0, f"Subprocess command failed: {cmd}\nstderr: {res.stderr}")


class TestEdgeCasesAndBoundaryConditions(unittest.TestCase):
    """Adversarial probe for boundary conditions and defensive handling."""

    def test_diagnostic_item_unmapped_category_and_layer(self):
        item = DiagnosticItem(category="unknown_cat", name="Custom Check", status="WARN", message="test")
        self.assertEqual(item.layer, 0)
        self.assertEqual(item.category, "unknown_cat")
        d = item.to_dict()
        self.assertEqual(d["layer"], 0)
        self.assertEqual(d["category"], "unknown_cat")

        item2 = DiagnosticItem(layer=99, title="Custom Title")
        self.assertEqual(item2.layer, 99)
        self.assertEqual(item2.category, "")

    def test_diagnostic_report_empty(self):
        rep = DiagnosticReport(items=[])
        self.assertEqual(rep.overall_status, "PASS")
        self.assertEqual(rep.exit_code, 0)
        d = rep.to_dict()
        self.assertEqual(d["summary"]["total"], 0)

    def test_format_countdown_boundaries(self):
        # Non-finite values
        self.assertEqual(format_countdown(float("nan")), "-")
        self.assertEqual(format_countdown(float("inf")), "-")
        self.assertEqual(format_countdown(True), "-")
        self.assertEqual(format_countdown("non-numeric"), "-")
        self.assertEqual(format_countdown(-500), "-")

        curr = 1000000.0
        # Exactly expired
        self.assertEqual(format_countdown(curr, current_timestamp=curr), "expired")
        self.assertEqual(format_countdown(curr - 1, current_timestamp=curr), "expired")

        # 59 seconds -> 1m
        self.assertEqual(format_countdown(curr + 59, current_timestamp=curr), "1m")
        # Exactly 1 hour
        self.assertEqual(format_countdown(curr + 3600, current_timestamp=curr), "1h")
        # Exactly 1 day
        self.assertEqual(format_countdown(curr + 86400, current_timestamp=curr), "1d")

    def test_supervisor_corrupted_stat_and_cmdline(self):
        with tempfile.TemporaryDirectory() as tmp:
            p_dir = Path(tmp) / "proc"
            p_dir.mkdir()
            g_dir = Path(tmp) / "gemini"
            g_dir.mkdir()

            sup = ProcessSupervisor(gemini_dir=g_dir, proc_dir=p_dir)

            # Corrupted / unreadable cmdline
            pid1 = p_dir / "101"
            pid1.mkdir()
            (pid1 / "cmdline").write_bytes(b"\xff\xfe\x00")
            self.assertIsNone(sup.locate_language_server_pid())

            # Non-numeric pid dir
            (p_dir / "abc").mkdir()
            self.assertIsNone(sup.locate_language_server_pid())

            # Corrupted stat file
            (pid1 / "stat").write_text("invalid format")
            self.assertFalse(sup.is_server_busy(pid=101))


if __name__ == "__main__":
    unittest.main()
