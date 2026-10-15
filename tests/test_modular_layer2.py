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

    def test_watcher_imports_only_layer0_and_layer1(self):
        watcher_file = REPO_ROOT / "scripts" / "agy_switcher" / "watcher.py"
        self.assertTrue(watcher_file.is_file(), "scripts/agy_switcher/watcher.py must exist")

        tree = ast.parse(watcher_file.read_text(encoding="utf-8"))
        allowed_modules = {
            "agy_switcher.exceptions",
            "agy_switcher.models",
            "agy_switcher.supervisor",
            "agy_switcher.formatters",
            "agy_switcher.telemetry",
        }

        forbidden_layers = {
            "agy_switcher.vault",
            "agy_switcher.rotation",
            "agy_switcher.doctor",
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
                        f"watcher.py illegally imports forbidden module '{name}'",
                    )
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                self.assertFalse(
                    is_forbidden(mod),
                    f"watcher.py illegally imports from forbidden module '{mod}'",
                )
                if mod.startswith("agy_switcher"):
                    self.assertIn(
                        mod,
                        allowed_modules,
                        f"watcher.py imports '{mod}' which is not in allowed Layer 0/1 modules",
                    )

    def test_isolated_watcher_import_does_not_load_vault_or_rotation_or_doctor(self):
        import subprocess
        scripts_dir = str(REPO_ROOT / "scripts")
        cmd = [
            sys.executable,
            "-c",
            f"import sys; sys.path.insert(0, {scripts_dir!r}); "
            "import agy_switcher.watcher; "
            "assert 'agy_switcher.vault' not in sys.modules, 'vault leaked into sys.modules'; "
            "assert 'agy_switcher.rotation' not in sys.modules, 'rotation leaked into sys.modules'; "
            "assert 'agy_switcher.doctor' not in sys.modules, 'doctor leaked into sys.modules'; "
            "assert 'agy_switcher.cli' not in sys.modules, 'cli leaked into sys.modules'; "
            "print('ISOLATED_WATCHER_OK')",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Subprocess failed: {res.stderr}")
        self.assertIn("ISOLATED_WATCHER_OK", res.stdout)

    def test_daemon_manager_default_watch_script_points_to_canonical_watch_py(self):
        from agy_switcher.watcher import DaemonManager
        dm = DaemonManager()
        self.assertEqual(dm.watch_script.name, "watch.py")
        self.assertEqual(dm.watch_script.parent.name, "scripts")
        self.assertTrue(dm.watch_script.is_file(), f"Canonical watch script {dm.watch_script} must exist")

    def test_watch_py_shim_reexports_all_symbols(self):
        import scripts.watch as watch_mod
        from agy_switcher.watcher import (
            LogWatcher as SubLogWatcher,
            PresenceDetector as SubPresenceDetector,
            CooldownManager as SubCooldownManager,
            DaemonManager as SubDaemonManager,
            _open_secure_log_file as sub_open_secure,
        )
        self.assertIs(watch_mod.LogWatcher, SubLogWatcher)
        self.assertIs(watch_mod.PresenceDetector, SubPresenceDetector)
        self.assertIs(watch_mod.CooldownManager, SubCooldownManager)
        self.assertIs(watch_mod.DaemonManager, SubDaemonManager)
        self.assertIs(watch_mod._open_secure_log_file, sub_open_secure)
        self.assertTrue(hasattr(watch_mod, "RotationEngine"))
        self.assertTrue(hasattr(watch_mod, "QuotaClient"))
        self.assertTrue(hasattr(watch_mod, "AgySwitchEngine"))
        self.assertTrue(hasattr(watch_mod, "DaemonError"))
        self.assertTrue(hasattr(watch_mod, "DaemonAlreadyRunningError"))
        self.assertTrue(hasattr(watch_mod, "time"))
        self.assertTrue(hasattr(watch_mod, "shutil"))
        self.assertTrue(hasattr(watch_mod, "subprocess"))

    def test_agy_switcher_exports_watcher_symbols(self):
        import agy_switcher
        for sym in (
            "PresenceDetector",
            "CooldownManager",
            "LogWatcher",
            "DaemonManager",
            "_open_secure_log_file",
        ):
            self.assertTrue(
                hasattr(agy_switcher, sym),
                f"agy_switcher package must export '{sym}' via PEP 562 lazy loading",
            )

    def test_watcher_secure_permissions_and_modes(self):
        from agy_switcher.watcher import _open_secure_log_file, CooldownManager, DaemonManager
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)

            # 1. _open_secure_log_file mode 0600
            test_log = tmp_path / "test.log"
            fd = _open_secure_log_file(test_log)
            os.close(fd)
            self.assertTrue(test_log.is_file())
            self.assertEqual(stat.S_IMODE(test_log.stat().st_mode), 0o600)

            # 2. CooldownManager mode 0600
            cm = CooldownManager(cache_dir=tmp_path / "cache")
            cm.set_cooldown("test_acc")
            cooldown_file = tmp_path / "cache" / "cooldown.json"
            self.assertTrue(cooldown_file.is_file())
            self.assertEqual(stat.S_IMODE(cooldown_file.stat().st_mode), 0o600)

            # 3. DaemonManager mode 0600 on watch.pid
            dm = DaemonManager(vault_dir=tmp_path / "vault")
            dm._write_pid_file(12345)
            pid_file = tmp_path / "vault" / "watch.pid"
            self.assertTrue(pid_file.is_file())
            self.assertEqual(stat.S_IMODE(pid_file.stat().st_mode), 0o600)

            # 4. _open_secure_log_file creates non-existent parent directory with 0700
            nested_log = tmp_path / "nested" / "sub" / "daemon.log"
            fd_nested = _open_secure_log_file(nested_log)
            os.close(fd_nested)
            self.assertTrue(nested_log.is_file())
            self.assertEqual(stat.S_IMODE(nested_log.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(nested_log.parent.stat().st_mode), 0o700)

    def test_watcher_direct_mocking_on_watcher_module(self):
        """Verify that agy_switcher.watcher allows direct mocking of RotationEngine and QuotaClient."""
        from unittest.mock import MagicMock, patch
        import agy_switcher.watcher as watcher_mod

        # 1. Direct patch on agy_switcher.watcher.RotationEngine
        with patch("agy_switcher.watcher.RotationEngine") as mock_rot_cls:
            resolved_cls = watcher_mod._get_rotation_engine_class()
            self.assertIs(resolved_cls, mock_rot_cls)

        # 2. Direct patch on agy_switcher.watcher.QuotaClient
        with patch("agy_switcher.watcher.QuotaClient") as mock_qc_cls:
            resolved_qc = watcher_mod._get_quota_client_class()
            self.assertIs(resolved_qc, mock_qc_cls)

        # 3. Direct patch on agy_switcher.watcher.AgySwitchEngine
        with patch("agy_switcher.watcher.AgySwitchEngine") as mock_eng_cls:
            resolved_eng = watcher_mod._get_engine_class()
            self.assertIs(resolved_eng, mock_eng_cls)

    def test_watcher_cli_invocation_via_python_module(self):
        """Verify executing python3 -m agy_switcher.watcher --help runs main() and exits cleanly."""
        import subprocess
        scripts_dir = str(REPO_ROOT / "scripts")
        cmd = [sys.executable, "-m", "agy_switcher.watcher", "--help"]
        res = subprocess.run(cmd, capture_output=True, text=True, cwd=scripts_dir)
        self.assertEqual(res.returncode, 0, f"Module execution failed: {res.stderr}")
        self.assertIn("Antigravity Account Switcher Log Watcher", res.stdout)
        self.assertIn("watch.py", res.stdout)



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


    def test_spec_constructor_and_evaluate_candidates_list_str(self):
        """RotationEngine supports spec constructor with vault_dir and evaluate with list[str] candidates."""
        tmp_vault = self.base_time  # just a dummy, let's create a real temp dir
        with tempfile.TemporaryDirectory() as td:
            v_dir = Path(td) / ".antigravity-vault"
            cache_dir = v_dir / "cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file = cache_dir / "last_known_quota.json"

            t_future = int((self.base_time + timedelta(hours=2)).timestamp())
            cache_payload = {
                "acc1": {"gemini": {"quota": 10, "reset_time": t_future}},
                "acc2": {"gemini": {"quota": 90, "reset_time": t_future}},
            }
            cache_file.write_text(json.dumps(cache_payload), encoding="utf-8")

            # Spec constructor using kwargs
            rot = RotationEngine(vault_dir=v_dir)
            self.assertEqual(rot.vault_dir, v_dir)
            self.assertEqual(rot.target_model_key, "gemini")

            # evaluate with candidate list of strings and active kwarg alias
            res = rot.evaluate(["acc1", "acc2"], active="acc1", current_time=self.base_time)
            self.assertEqual(res, "acc2", "Should load quotas for string candidates from vault cache and select acc2")

    def test_spec_constructor_positional_string_path_and_client_cache(self):
        """RotationEngine positional string path is recognized as vault_dir, and client.load_cache is used."""
        with tempfile.TemporaryDirectory() as td:
            v_path_str = str(Path(td) / "vault")
            mock_client = MagicMock()
            t_future = int((self.base_time + timedelta(hours=2)).timestamp())
            mock_client.load_cache.return_value = {
                "acc_a": {"gemini": {"quota": 5, "reset_time": t_future}},
                "acc_b": {"gemini": {"quota": 80, "reset_time": t_future}},
            }
            mock_cm = MagicMock()
            mock_cm.get_active_cooldowns.return_value = {}

            rot = RotationEngine(v_path_str, mock_client, mock_cm)
            self.assertEqual(rot.vault_dir, Path(v_path_str))
            self.assertEqual(rot.client, mock_client)
            self.assertEqual(rot.cooldown_manager, mock_cm)

            res = rot.evaluate(["acc_a", "acc_b"], current_active="acc_a", current_time=self.base_time)
            self.assertEqual(res, "acc_b")
            mock_client.load_cache.assert_called_once()


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
            self.assertIn(item.layer, (1, 2, 3, 4, 5))
            self.assertTrue(bool(item.title))

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

    def test_doctor_spec_constructor_and_custom_token_paths(self):
        """DoctorEngine supports the 8-argument spec constructor and custom desktop/cli token files."""
        self._setup_pristine_environment()
        custom_desktop = self.base_path / "custom_desktop_token"
        custom_cli = self.base_path / "custom_cli_token"
        token_data = create_sample_jwt_token(email="custom@example.com")
        custom_desktop.write_text(json.dumps(token_data), encoding="utf-8")
        custom_cli.write_text(json.dumps(token_data), encoding="utf-8")
        os.chmod(custom_desktop, 0o600)
        os.chmod(custom_cli, 0o600)

        sup = ProcessSupervisor(gemini_dir=self.gemini_dir, proc_dir=self.proc_dir)
        cb = {"sync_tokens": MagicMock(), "get_active_account": lambda: "acc_primary"}

        doc = DoctorEngine(
            vault_dir=self.vault_dir,
            accounts_dir=self.vault_dir / "accounts",
            cache_dir=self.vault_dir / "cache",
            config_file=self.vault_dir / "config.json",
            desktop_token_file=custom_desktop,
            cli_token_file=custom_cli,
            supervisor=sup,
            engine_callbacks=cb,
        )

        self.assertEqual(doc.desktop_token_file, custom_desktop)
        self.assertEqual(doc.cli_token_file, custom_cli)
        self.assertEqual(doc.get_active_name(), "acc_primary")

        # Test positional spec constructor
        doc_pos = DoctorEngine(
            self.vault_dir,
            self.vault_dir / "accounts",
            self.vault_dir / "cache",
            self.vault_dir / "config.json",
            custom_desktop,
            custom_cli,
            sup,
            cb,
        )
        self.assertEqual(doc_pos.desktop_token_file, custom_desktop)
        self.assertEqual(doc_pos.cli_token_file, custom_cli)

    def test_broken_symlink_watch_pid_does_not_break_permissions_repair(self):
        """A broken symlink watch.pid is handled safely without crashing or failing permissions repair."""
        self._setup_pristine_environment()
        # Insecure permissions on accounts
        os.chmod(self.vault_dir / "accounts", 0o777)
        # Create broken symlink for watch.pid
        watch_pid_file = self.vault_dir / "watch.pid"
        if watch_pid_file.exists():
            watch_pid_file.unlink()
        watch_pid_file.symlink_to(self.base_path / "ghost_target.pid")

        report = self.doctor.run_diagnostics()
        self.assertEqual(report.overall_status, "FAIL")

        fixes = self.doctor.fix_diagnostics(report)
        self.assertIn("Restored POSIX permissions (0700 for directories, 0600 for files)", fixes)
        self.assertIn("Unlinked stale watch.pid file", fixes)
        self.assertFalse(watch_pid_file.exists())
        self.assertEqual(stat.S_IMODE((self.vault_dir / "accounts").stat().st_mode), 0o700)

    def test_lifecycle_hook_path_with_tilde_expansion(self):
        """PreInvocation hook commands using '~/' are expanded correctly."""
        self._setup_pristine_environment()
        fake_home = self.base_path / "home"
        fake_home.mkdir(parents=True, exist_ok=True)
        script_file = fake_home / "test_hook.py"
        script_file.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        os.chmod(script_file, 0o755)

        hooks_file = self.gemini_dir / "config" / "hooks.json"
        hook_payload = {
            "agy-switch-auto-rotate": {
                "enabled": True,
                "PreInvocation": [
                    {
                        "type": "command",
                        "command": f"python3 ~/test_hook.py hook-pre-invocation",
                    }
                ],
            }
        }
        hooks_file.write_text(json.dumps(hook_payload), encoding="utf-8")
        with patch.dict(os.environ, {"HOME": str(fake_home)}):
            item = self.doctor._check_lifecycle_hooks()
            self.assertEqual(item.status, "PASS")

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

