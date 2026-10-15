"""Unit tests for Layer 2: RotationEngine (rotation.py) and DoctorEngine (doctor.py).

Verifies:
1. Layer isolation and strict 5-layer DAG compliance via AST inspection:
   - rotation.py imports ONLY Layer 0 (exceptions, models) and Layer 1 (telemetry).
   - doctor.py imports ONLY Layer 0 (exceptions, models) and Layer 1 (supervisor).
   - Neither module imports vault.py (zero circular import risk).
2. RotationEngine heuristics: cache_optimized, drain_window, auto_highest, imminent reset window.
3. RotationEngine model steering: strict model isolation, cooldown exemptions for non-target models.
4. DoctorEngine 5-layer health inspection and auto-healing:
   - Permissions audit (0700/0600)
   - Token & JWT identity integrity (pure standard library base64 decode)
   - Dual-destination sync (Desktop & CLI)
   - Lifecycle hook status (hooks.json PreInvocation command)
   - Process supervisor and daemon health with cmdline pattern matching:
     watch.py, watcher.py, agy_switch.py, agy-switch
5. Standalone execution of DoctorEngine and RotationEngine without AgySwitchEngine.
"""

from __future__ import annotations

import ast
import base64
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from agy_switcher.exceptions import (
    AgySwitchError,
    InvalidTokenError,
)
from agy_switcher.models import (
    DiagnosticItem,
    DiagnosticReport,
    EvaluationCandidate,
    QuotaStatus,
)
from agy_switcher.supervisor import ProcessSupervisor
from agy_switcher.rotation import RotationEngine
from agy_switcher.doctor import (
    DoctorEngine,
    run_doctor,
    auto_heal,
    extract_email_from_jwt,
    validate_token_dict,
    validate_token_file,
)


def create_sample_jwt_token(
    email: str = "test@example.com",
    expiry_offset_hours: int = 24,
    access_token: str | None = None,
    refresh_token: str | None = None,
) -> dict:
    payload = json.dumps({"email": email, "sub": "12345", "aud": "test_aud"}).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(payload).decode("utf-8").rstrip("=")
    exp_dt = datetime.now(timezone.utc) + timedelta(hours=expiry_offset_hours)
    exp_str = exp_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "token": {
            "access_token": access_token or f"ya29.sample_access_token_for_{email}",
            "token_type": "Bearer",
            "refresh_token": refresh_token or f"1//sample_refresh_token_for_{email}",
            "expiry": exp_str,
        },
        "auth_method": "oauth",
        "id_token": f"header.{payload_b64}.signature",
    }


class TestLayer2DAGCompliance(unittest.TestCase):
    """Verifies that Layer 2 modules adhere strictly to the 5-layer DAG constraints."""

    def test_rotation_imports_only_layer0_and_layer1(self):
        rotation_file = REPO_ROOT / "scripts" / "agy_switcher" / "rotation.py"
        self.assertTrue(rotation_file.is_file())

        tree = ast.parse(rotation_file.read_text(encoding="utf-8"))
        allowed_modules = {
            "agy_switcher.exceptions",
            "agy_switcher.models",
            "agy_switcher.telemetry",
        }

        forbidden_layers = {
            "agy_switcher.vault",
            "agy_switcher.doctor",
            "agy_switcher.watcher",
            "agy_switcher.hook",
            "agy_switcher.cli",
            "agy_switch",
            "watch",
        }

        def is_forbidden(mod_name: str) -> bool:
            return mod_name in forbidden_layers or any(mod_name.startswith(f + ".") for f in forbidden_layers)

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.name
                    self.assertFalse(
                        is_forbidden(name),
                        f"rotation.py illegally imports forbidden module '{name}'",
                    )
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                self.assertFalse(
                    is_forbidden(mod),
                    f"rotation.py illegally imports from forbidden module '{mod}'",
                )
                if mod.startswith("agy_switcher"):
                    self.assertIn(
                        mod,
                        allowed_modules,
                        f"rotation.py imports '{mod}' which is not in allowed Layer 0/1 modules",
                    )

    def test_doctor_imports_only_layer0_and_layer1_and_no_vault(self):
        doctor_file = REPO_ROOT / "scripts" / "agy_switcher" / "doctor.py"
        self.assertTrue(doctor_file.is_file())

        tree = ast.parse(doctor_file.read_text(encoding="utf-8"))
        allowed_modules = {
            "agy_switcher.exceptions",
            "agy_switcher.models",
            "agy_switcher.supervisor",
        }

        forbidden_layers = {
            "agy_switcher.vault",
            "agy_switcher.rotation",
            "agy_switcher.watcher",
            "agy_switcher.hook",
            "agy_switcher.cli",
            "agy_switch",
            "watch",
        }

        def is_forbidden(mod_name: str) -> bool:
            return mod_name in forbidden_layers or any(mod_name.startswith(f + ".") for f in forbidden_layers)

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.name
                    self.assertFalse(
                        is_forbidden(name),
                        f"doctor.py illegally imports forbidden module '{name}'",
                    )
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                self.assertFalse(
                    is_forbidden(mod),
                    f"doctor.py illegally imports from forbidden module '{mod}'",
                )
                if mod.startswith("agy_switcher"):
                    self.assertIn(
                        mod,
                        allowed_modules,
                        f"doctor.py imports '{mod}' which is not in allowed Layer 0/1 modules",
                    )

    def test_isolated_rotation_import_does_not_load_vault_or_doctor(self):
        import subprocess
        scripts_dir = str(REPO_ROOT / "scripts")
        cmd = [
            sys.executable,
            "-c",
            f"import sys; sys.path.insert(0, {scripts_dir!r}); "
            "import agy_switcher.rotation; "
            "assert 'agy_switcher.vault' not in sys.modules, 'vault leaked into sys.modules'; "
            "assert 'agy_switcher.doctor' not in sys.modules, 'doctor leaked into sys.modules'; "
            "assert 'agy_switcher.cli' not in sys.modules, 'cli leaked into sys.modules'; "
            "print('ISOLATED_ROTATION_OK')",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Subprocess failed: {res.stderr}")
        self.assertIn("ISOLATED_ROTATION_OK", res.stdout)

    def test_isolated_doctor_import_does_not_load_vault(self):
        import subprocess
        scripts_dir = str(REPO_ROOT / "scripts")
        cmd = [
            sys.executable,
            "-c",
            f"import sys; sys.path.insert(0, {scripts_dir!r}); "
            "import agy_switcher.doctor; "
            "assert 'agy_switcher.vault' not in sys.modules, 'vault leaked into sys.modules'; "
            "assert 'agy_switcher.rotation' not in sys.modules, 'rotation leaked into sys.modules'; "
            "assert 'agy_switcher.cli' not in sys.modules, 'cli leaked into sys.modules'; "
            "print('ISOLATED_DOCTOR_OK')",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Subprocess failed: {res.stderr}")
        self.assertIn("ISOLATED_DOCTOR_OK", res.stdout)


class TestRotationEngineLayer2(unittest.TestCase):
    """Detailed unit tests for RotationEngine in agy_switcher.rotation."""

    def setUp(self):
        self.base_time = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)

    def _make_quota(
        self,
        gemini_frac: float = 1.0,
        gemini_reset: int = 120,
        claude_frac: float = 1.0,
        claude_reset: int = 120,
    ) -> dict:
        t_gem = int((self.base_time + timedelta(minutes=gemini_reset)).timestamp())
        t_claude = int((self.base_time + timedelta(minutes=claude_reset)).timestamp())
        return {
            "gemini": {"quota": int(round(gemini_frac * 100)), "reset_time": t_gem},
            "claude": {"quota": int(round(claude_frac * 100)), "reset_time": t_claude},
        }

    def test_flexible_init_with_path(self):
        vault_path = Path("/tmp/mock_vault")
        rot = RotationEngine(vault_path)
        self.assertEqual(rot.vault_dir, vault_path)
        self.assertEqual(rot.model_name, "gemini")
        self.assertEqual(rot.target_model_key, "gemini")

    def test_model_steering_claude(self):
        rot = RotationEngine(model_name="claude-3-5-sonnet")
        self.assertEqual(rot.target_group_prefix, "Claude")
        self.assertEqual(rot.target_model_key, "claude")

    def test_cache_optimized_retains_when_above_15_percent(self):
        rot = RotationEngine(model_name="gemini", active_account_id="acc1")
        accounts = {
            "acc1": self._make_quota(gemini_frac=0.16),
            "acc2": self._make_quota(gemini_frac=0.99),
        }
        res = rot.evaluate(accounts, current_active="acc1", current_time=self.base_time)
        self.assertIsNone(res, "Active account with > 15% quota must be retained")

    def test_drain_window_retains_when_reset_imminent(self):
        rot = RotationEngine(model_name="gemini", active_account_id="acc1")
        # 6% quota and reset in 25m (<= 30m)
        accounts = {
            "acc1": self._make_quota(gemini_frac=0.06, gemini_reset=25),
            "acc2": self._make_quota(gemini_frac=0.90, gemini_reset=120),
        }
        res = rot.evaluate(accounts, current_active="acc1", current_time=self.base_time)
        self.assertIsNone(res, "Active account with > 5% and reset in <= 30m must be retained")

    def test_imminent_reset_priority_over_higher_distant_quota(self):
        rot = RotationEngine(model_name="gemini", active_account_id="acc1")
        accounts = {
            "acc1": self._make_quota(gemini_frac=0.04, gemini_reset=60),
            "acc2": self._make_quota(gemini_frac=0.80, gemini_reset=120),
            "acc3": self._make_quota(gemini_frac=0.25, gemini_reset=35),  # <= 45m reset delta, >= 15% quota
        }
        res = rot.evaluate(accounts, current_active="acc1", current_time=self.base_time)
        self.assertEqual(res, "acc3", "Imminent healthy account (acc3) must be prioritized")

    def test_strict_model_isolation_claude_exhausted(self):
        """When switching for Claude, accounts with 0% Claude quota are rejected even if Gemini is 100%."""
        rot = RotationEngine(model_name="claude-3-5-sonnet", active_account_id="acc1")
        accounts = {
            "acc1": self._make_quota(gemini_frac=1.0, claude_frac=0.0),
            "acc2": self._make_quota(gemini_frac=1.0, claude_frac=0.0),
        }
        res = rot.evaluate(accounts, current_active="acc1", current_time=self.base_time)
        self.assertIsNone(res, "Strict no-cross-model-fallback: no eligible standby for Claude")

    def test_cooldown_cross_model_exemption(self):
        """Account in cooldown for Claude exhaustion is exempt when evaluating Gemini target."""
        rot = RotationEngine(model_name="gemini-3.8-pro", active_account_id="acc1")
        accounts = {
            "acc1": self._make_quota(gemini_frac=0.02, claude_frac=0.0),
            "acc2": self._make_quota(gemini_frac=0.85, claude_frac=0.0),
        }
        cooldowns = {
            "acc2": {"reason": "RESOURCE_EXHAUSTED for claude-3-5-sonnet"},
        }
        res = rot.evaluate(accounts, current_active="acc1", current_time=self.base_time, cooldowns=cooldowns)
        self.assertEqual(res, "acc2", "acc2 should be exempt from cooldown for Gemini")


class TestDoctorEngineLayer2(unittest.TestCase):
    """Detailed unit tests for DoctorEngine in agy_switcher.doctor."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)
        self.vault_dir = self.base_path / ".antigravity-vault"
        self.gemini_dir = self.base_path / ".gemini"
        self.proc_dir = self.base_path / "proc"
        self.proc_dir.mkdir(parents=True, exist_ok=True)

        self.doctor = DoctorEngine(
            vault_dir=self.vault_dir,
            gemini_dir=self.gemini_dir,
            proc_dir=self.proc_dir,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def _setup_pristine_environment(self) -> str:
        acc_name = "acc_primary"
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.vault_dir, 0o700)
        (self.vault_dir / "accounts").mkdir(parents=True, exist_ok=True)
        os.chmod(self.vault_dir / "accounts", 0o700)

        acc_dir = self.vault_dir / "accounts" / acc_name
        acc_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(acc_dir, 0o700)

        token_data = create_sample_jwt_token(email="primary@example.com")
        token_path = acc_dir / "token.json"
        token_path.write_text(json.dumps(token_data, indent=2), encoding="utf-8")
        os.chmod(token_path, 0o600)

        # Set active account
        active_file = self.vault_dir / "active"
        active_file.write_text(f"{acc_name}\n", encoding="utf-8")
        os.chmod(active_file, 0o600)

        # Cache file
        cache_dir = self.vault_dir / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(cache_dir, 0o700)
        cache_file = cache_dir / "last_known_quota.json"
        cache_file.write_text("{}", encoding="utf-8")
        os.chmod(cache_file, 0o600)

        # Dual destination sync
        desktop_token = self.gemini_dir / "jetski-standalone-oauth-token"
        desktop_token.parent.mkdir(parents=True, exist_ok=True)
        desktop_token.write_text(json.dumps(token_data), encoding="utf-8")
        os.chmod(desktop_token, 0o600)

        cli_token = self.gemini_dir / "antigravity-cli" / "antigravity-oauth-token"
        cli_token.parent.mkdir(parents=True, exist_ok=True)
        cli_token.write_text(json.dumps(token_data), encoding="utf-8")
        os.chmod(cli_token, 0o600)

        # Hooks
        hooks_file = self.gemini_dir / "config" / "hooks.json"
        hooks_file.parent.mkdir(parents=True, exist_ok=True)
        launcher_path = REPO_ROOT / "scripts" / "agy_switch.py"
        hook_payload = {
            "agy-switch-auto-rotate": {
                "enabled": True,
                "PreInvocation": [
                    {
                        "type": "command",
                        "command": f"python3 {launcher_path} hook-pre-invocation",
                    }
                ],
            }
        }
        hooks_file.write_text(json.dumps(hook_payload), encoding="utf-8")

        # Mock processes in proc_dir
        ls_pid_dir = self.proc_dir / "101"
        ls_pid_dir.mkdir(parents=True, exist_ok=True)
        (ls_pid_dir / "cmdline").write_bytes(b"/usr/bin/language_server\x00--standalone\x00")
        (ls_pid_dir / "stat").write_text("101 (language_server) S 1 101 101 0 0 0 0 0 0 0 0 0")

        watch_pid_dir = self.proc_dir / "202"
        watch_pid_dir.mkdir(parents=True, exist_ok=True)
        (watch_pid_dir / "cmdline").write_bytes(b"python3\x00scripts/watch.py\x00run\x00")
        (watch_pid_dir / "stat").write_text("202 (python3) S 1 202 202 0 0 0 0 0 0 0 0 0")

        watch_pid_file = self.vault_dir / "watch.pid"
        watch_pid_file.write_text("202\n", encoding="utf-8")
        os.chmod(watch_pid_file, 0o600)

        return acc_name

    def test_doctor_pristine_environment_all_pass(self):
        self._setup_pristine_environment()
        report = self.doctor.run_diagnostics()
        self.assertEqual(report.overall_status, "PASS")
        self.assertEqual(report.exit_code, 0)
        self.assertEqual(len(report.items), 5)
        for item in report.items:
            self.assertEqual(item.status, "PASS", f"Item {item.name} failed: {item.message}")

    def test_cmdline_pattern_matching_watcher_py(self):
        """Verifies that watcher.py is recognized as an active watcher daemon."""
        self._setup_pristine_environment()
        watch_pid_dir = self.proc_dir / "202"
        # Overwrite cmdline to use watcher.py
        (watch_pid_dir / "cmdline").write_bytes(b"python3\x00scripts/agy_switcher/watcher.py\x00run\x00")

        item = self.doctor._check_process_health()
        self.assertEqual(item.status, "PASS")
        self.assertIn("watcher active (PID 202)", item.message)

    def test_cmdline_pattern_matching_agy_switch(self):
        """Verifies that agy-switch watch is recognized as an active watcher daemon."""
        self._setup_pristine_environment()
        watch_pid_dir = self.proc_dir / "202"
        # Overwrite cmdline to use agy-switch watch
        (watch_pid_dir / "cmdline").write_bytes(b"/usr/local/bin/agy-switch\x00watch\x00start\x00")

        item = self.doctor._check_process_health()
        self.assertEqual(item.status, "PASS")
        self.assertIn("watcher active (PID 202)", item.message)

    def test_cmdline_pattern_rejects_unrelated_npm_watch(self):
        """Verifies that npm run watch is rejected as a recycled/stale PID."""
        self._setup_pristine_environment()
        watch_pid_dir = self.proc_dir / "202"
        (watch_pid_dir / "cmdline").write_bytes(b"node\x00/usr/bin/npm\x00run\x00watch\x00")

        item = self.doctor._check_process_health()
        self.assertEqual(item.status, "WARN")
        self.assertTrue(item.can_fix)
        self.assertIn("recycled PID", item.message)

    def test_fix_diagnostics_repairs_permissions_and_stale_pid(self):
        self._setup_pristine_environment()

        # Insecure permission on accounts dir
        os.chmod(self.vault_dir / "accounts", 0o777)
        # Stale PID file (process not running)
        shutil.rmtree(self.proc_dir / "202")

        report = self.doctor.run_diagnostics()
        self.assertEqual(report.overall_status, "FAIL")

        fixes = self.doctor.fix_diagnostics(report)
        self.assertGreater(len(fixes), 0)

        # After repair, permissions should be 0700 and stale PID removed
        mode = stat.S_IMODE((self.vault_dir / "accounts").stat().st_mode)
        self.assertEqual(mode, 0o700)
        self.assertFalse((self.vault_dir / "watch.pid").exists())

    def test_convenience_functions_run_doctor_and_auto_heal(self):
        self._setup_pristine_environment()
        report = run_doctor(
            vault_dir=self.vault_dir,
            gemini_dir=self.gemini_dir,
            proc_dir=self.proc_dir,
        )
        self.assertEqual(report.overall_status, "PASS")

        # Induce a fixable anomaly
        (self.vault_dir / "watch.pid").write_text("99999\n", encoding="utf-8")
        report2 = run_doctor(vault_dir=self.vault_dir, gemini_dir=self.gemini_dir, proc_dir=self.proc_dir)
        self.assertEqual(report2.overall_status, "WARN")

        fixes = auto_heal(report2, vault_dir=self.vault_dir, gemini_dir=self.gemini_dir, proc_dir=self.proc_dir)
        self.assertIn("Unlinked stale watch.pid file", fixes)


if __name__ == "__main__":
    unittest.main()
