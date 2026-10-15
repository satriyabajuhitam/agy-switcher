"""Unit and architectural tests for Static AST DAG Validation Suite & End-to-End Package Verification.

Validates:
1. Strict 5-Layer DAG compliance via static AST analysis:
   - Layer 0: exceptions.py, models.py
   - Layer 1: supervisor.py, formatters.py, telemetry.py
   - Layer 2: vault.py, rotation.py, doctor.py, watcher.py
   - Layer 3: hook.py
   - Layer 4: cli.py, __init__.py, agy_switch.py, watch.py
   - No module in Layer N imports from Layer >= N+1 at module level.
   - doctor.py, rotation.py, and watcher.py do not import vault.py at module level.
   - No sub-module in agy_switcher imports agy_switch or watch at module level.
2. Pure isolated subprocess namespace imports and PEP 562 lazy-loading.
3. Re-export facade launchers (scripts/agy_switch.py & scripts/watch.py):
   - Symlink execution via canonical launcher path.
   - 100% symbol parity between agy_switch and agy_switcher.
   - Realpath resolution and exclusion of orphaned helpers.
4. CLI subcommand parsing, return codes, and Dynamic Module Proxy Bridge.
"""

from __future__ import annotations

import ast
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
PACKAGE_DIR = SCRIPTS_DIR / "agy_switcher"
sys.path.insert(0, str(SCRIPTS_DIR))

import agy_switch
import agy_switcher
from agy_switcher.cli import build_parser, main
from agy_switcher.vault import AgySwitchEngine


# 5-Layer Architectural Hierarchy
PACKAGE_LAYER_MAP: dict[str, int] = {
    # Layer 0: Foundation
    "exceptions.py": 0,
    "models.py": 0,
    # Layer 1: Core Services & IO
    "supervisor.py": 1,
    "formatters.py": 1,
    "telemetry.py": 1,
    # Layer 2: Domain Engines
    "vault.py": 2,
    "rotation.py": 2,
    "doctor.py": 2,
    "watcher.py": 2,
    # Layer 3: Workflow & Orchestration
    "hook.py": 3,
    # Layer 4: Presentation & Dispatch
    "cli.py": 4,
    "__init__.py": 4,
}

LAUNCHER_LAYER_MAP: dict[str, int] = {
    "agy_switch.py": 4,
    "watch.py": 4,
}

MODULE_NAME_TO_LAYER: dict[str, int] = {
    "exceptions": 0,
    "models": 0,
    "supervisor": 1,
    "formatters": 1,
    "telemetry": 1,
    "vault": 2,
    "rotation": 2,
    "doctor": 2,
    "watcher": 2,
    "hook": 3,
    "cli": 4,
}


class TestModularArchitectureDAG(unittest.TestCase):
    """Static AST and structural analysis of the 5-layer dependency graph."""

    def test_strict_5_layer_dag_no_upward_imports(self):
        """Scans AST of every module in agy_switcher to ensure no Layer N imports Layer >= N+1."""
        for filename, current_layer in PACKAGE_LAYER_MAP.items():
            file_path = PACKAGE_DIR / filename
            self.assertTrue(file_path.is_file(), f"Missing expected module: {filename}")

            tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))

            for node in tree.body:
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        mod_name = alias.name
                        self.assertFalse(
                            mod_name in ("agy_switch", "watch") or mod_name.startswith(("agy_switch.", "watch.")),
                            f"{filename} imports root launcher '{mod_name}' at module level!",
                        )
                        if mod_name.startswith("agy_switcher."):
                            sub_name = mod_name.split(".")[1]
                            imported_layer = MODULE_NAME_TO_LAYER.get(sub_name)
                            if imported_layer is not None:
                                self.assertLessEqual(
                                    imported_layer,
                                    current_layer,
                                    f"DAG violation in {filename} (Layer {current_layer}): imports {sub_name} (Layer {imported_layer})",
                                )
                elif isinstance(node, ast.ImportFrom):
                    mod_name = node.module or ""
                    self.assertFalse(
                        mod_name in ("agy_switch", "watch") or mod_name.startswith(("agy_switch.", "watch.")),
                        f"{filename} imports from root launcher '{mod_name}' at module level!",
                    )
                    if mod_name.startswith("agy_switcher."):
                        sub_name = mod_name.split(".")[1]
                        imported_layer = MODULE_NAME_TO_LAYER.get(sub_name)
                        if imported_layer is not None:
                            self.assertLessEqual(
                                imported_layer,
                                current_layer,
                                f"DAG violation in {filename} (Layer {current_layer}): imports from {sub_name} (Layer {imported_layer})",
                            )
                    elif mod_name == "agy_switcher":
                        for alias in node.names:
                            imported_layer = MODULE_NAME_TO_LAYER.get(alias.name)
                            if imported_layer is not None:
                                self.assertLessEqual(
                                    imported_layer,
                                    current_layer,
                                    f"DAG violation in {filename} (Layer {current_layer}): imports {alias.name} (Layer {imported_layer})",
                                )

    def test_layer_4_launchers_static_dag(self):
        """Scans root launcher scripts (agy_switch.py, watch.py) to verify Layer 4 status."""
        for launcher_name, expected_layer in LAUNCHER_LAYER_MAP.items():
            launcher_path = SCRIPTS_DIR / launcher_name
            self.assertTrue(launcher_path.is_file(), f"Missing launcher: {launcher_name}")

            tree = ast.parse(launcher_path.read_text(encoding="utf-8"), filename=str(launcher_path))
            for node in tree.body:
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith("agy_switcher."):
                            sub_name = alias.name.split(".")[1]
                            imported_layer = MODULE_NAME_TO_LAYER.get(sub_name)
                            if imported_layer is not None:
                                self.assertLessEqual(
                                    imported_layer,
                                    expected_layer,
                                    f"Launcher {launcher_name} imports layer {imported_layer} > {expected_layer}",
                                )
                elif isinstance(node, ast.ImportFrom):
                    mod_name = node.module or ""
                    if mod_name.startswith("agy_switcher."):
                        sub_name = mod_name.split(".")[1]
                        imported_layer = MODULE_NAME_TO_LAYER.get(sub_name)
                        if imported_layer is not None:
                            self.assertLessEqual(
                                imported_layer,
                                expected_layer,
                                f"Launcher {launcher_name} imports from layer {imported_layer} > {expected_layer}",
                            )

    def test_layer_2_horizontal_independence(self):
        """DoctorEngine, RotationEngine, and Watcher must NOT import vault.py at module level."""
        for fn in ["doctor.py", "rotation.py", "watcher.py"]:
            file_path = PACKAGE_DIR / fn
            tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
            for node in tree.body:
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotIn("vault", alias.name, f"{fn} imports vault at module level!")
                elif isinstance(node, ast.ImportFrom):
                    self.assertNotIn("vault", node.module or "", f"{fn} imports from vault at module level!")

    def test_zero_external_pip_dependencies(self):
        """All modules in agy_switcher and scripts/ must strictly import standard library only."""
        allowed_stdlib = {
            "__future__",
            "argparse",
            "ast",
            "base64",
            "concurrent",
            "contextlib",
            "dataclasses",
            "datetime",
            "functools",
            "glob",
            "importlib",
            "io",
            "itertools",
            "json",
            "math",
            "os",
            "pathlib",
            "re",
            "shlex",
            "shutil",
            "signal",
            "stat",
            "subprocess",
            "sys",
            "tempfile",
            "threading",
            "time",
            "typing",
            "unittest",
            "urllib",
            "agy_switcher",
            "agy_switch",
            "scripts",
        }
        all_py_files = list(PACKAGE_DIR.glob("*.py")) + [
            SCRIPTS_DIR / "agy_switch.py",
            SCRIPTS_DIR / "watch.py",
        ]
        for file_path in all_py_files:
            tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        top = alias.name.split(".")[0]
                        self.assertIn(
                            top,
                            allowed_stdlib,
                            f"{file_path.name} imports external non-stdlib dependency: {alias.name}",
                        )
                elif isinstance(node, ast.ImportFrom):
                    if node.level == 0 and node.module:
                        top = node.module.split(".")[0]
                        self.assertIn(
                            top,
                            allowed_stdlib,
                            f"{file_path.name} imports external non-stdlib dependency from: {node.module}",
                        )


class TestSubprocessNamespaceIsolation(unittest.TestCase):
    """Verifies pure isolated runtime imports via subprocess."""

    def test_telemetry_isolated_import(self):
        """Importing telemetry does not load vault or agy_switch into sys.modules."""
        cmd = [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, 'scripts'); "
            "import agy_switcher.telemetry; "
            "assert 'agy_switcher.vault' not in sys.modules, 'vault leaked into sys.modules'; "
            "assert 'agy_switcher.cli' not in sys.modules, 'cli leaked into sys.modules'; "
            "assert 'agy_switch' not in sys.modules, 'agy_switch leaked into sys.modules'; "
            "print('OK')",
        ]
        res = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=10)
        self.assertEqual(res.returncode, 0, f"Stderr: {res.stderr}")
        self.assertIn("OK", res.stdout)

    def test_supervisor_isolated_import(self):
        """Importing supervisor does not load cli, vault, or agy_switch into sys.modules."""
        cmd = [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, 'scripts'); "
            "import agy_switcher.supervisor; "
            "assert 'agy_switcher.cli' not in sys.modules, 'cli leaked into sys.modules'; "
            "assert 'agy_switcher.vault' not in sys.modules, 'vault leaked into sys.modules'; "
            "assert 'agy_switch' not in sys.modules, 'agy_switch leaked into sys.modules'; "
            "print('OK')",
        ]
        res = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=10)
        self.assertEqual(res.returncode, 0, f"Stderr: {res.stderr}")
        self.assertIn("OK", res.stdout)

    def test_models_and_exceptions_isolated_import(self):
        """Layer 0 imports do not pull in any Layer 1-4 modules."""
        cmd = [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, 'scripts'); "
            "import agy_switcher.models; "
            "import agy_switcher.exceptions; "
            "assert 'agy_switcher.supervisor' not in sys.modules; "
            "assert 'agy_switcher.telemetry' not in sys.modules; "
            "assert 'agy_switcher.vault' not in sys.modules; "
            "assert 'agy_switcher.cli' not in sys.modules; "
            "assert 'agy_switch' not in sys.modules; "
            "print('OK')",
        ]
        res = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=10)
        self.assertEqual(res.returncode, 0, f"Stderr: {res.stderr}")
        self.assertIn("OK", res.stdout)

    def test_formatters_isolated_import(self):
        """Importing formatters does not load vault, doctor, or cli into sys.modules."""
        cmd = [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, 'scripts'); "
            "import agy_switcher.formatters; "
            "assert 'agy_switcher.vault' not in sys.modules; "
            "assert 'agy_switcher.doctor' not in sys.modules; "
            "assert 'agy_switcher.cli' not in sys.modules; "
            "print('OK')",
        ]
        res = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=10)
        self.assertEqual(res.returncode, 0, f"Stderr: {res.stderr}")
        self.assertIn("OK", res.stdout)

    def test_cli_isolated_import(self):
        """Importing agy_switcher.cli does not load the root launcher agy_switch."""
        cmd = [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, 'scripts'); "
            "import agy_switcher.cli; "
            "assert 'agy_switch' not in sys.modules; "
            "print('OK')",
        ]
        res = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=10)
        self.assertEqual(res.returncode, 0, f"Stderr: {res.stderr}")
        self.assertIn("OK", res.stdout)

    def test_package_pep562_lazy_loading(self):
        """import agy_switcher does not eagerly import heavy submodules until accessed."""
        cmd = [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, 'scripts'); "
            "import agy_switcher; "
            "assert 'agy_switcher.cli' not in sys.modules; "
            "assert 'agy_switcher.doctor' not in sys.modules; "
            "assert 'agy_switcher.watcher' not in sys.modules; "
            "assert 'agy_switcher.vault' not in sys.modules; "
            "# Accessing attribute triggers lazy load\n"
            "_ = agy_switcher.AgySwitchEngine; "
            "assert 'agy_switcher.vault' in sys.modules; "
            "print('OK')",
        ]
        res = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=10)
        self.assertEqual(res.returncode, 0, f"Stderr: {res.stderr}")
        self.assertIn("OK", res.stdout)


class TestReExportLaunchers(unittest.TestCase):
    """Validates contract rules for scripts/agy_switch.py and scripts/watch.py."""

    def test_agy_switch_launcher_uses_realpath_not_abspath(self):
        launcher_path = SCRIPTS_DIR / "agy_switch.py"
        source = launcher_path.read_text(encoding="utf-8")
        self.assertIn("os.path.realpath(__file__)", source)
        self.assertNotIn("os.path.abspath(__file__)", source)

    def test_agy_switch_launcher_purged_orphaned_helper(self):
        launcher_path = SCRIPTS_DIR / "agy_switch.py"
        source = launcher_path.read_text(encoding="utf-8")
        self.assertNotIn("def _is_valid_entry", source)

    def test_agy_switch_launcher_completeness(self):
        self.assertTrue(hasattr(agy_switch, "__all__"))
        for symbol in agy_switch.__all__:
            self.assertTrue(
                hasattr(agy_switch, symbol),
                f"Symbol '{symbol}' in __all__ not found on agy_switch",
            )

    def test_reexport_surface_parity_with_agy_switcher(self):
        """Every public symbol in agy_switcher.__all__ must match agy_switch identically."""
        self.assertTrue(hasattr(agy_switcher, "__all__"))
        for symbol in agy_switcher.__all__:
            self.assertTrue(
                hasattr(agy_switcher, symbol),
                f"Symbol '{symbol}' defined in __all__ missing from agy_switcher module",
            )
            self.assertTrue(
                hasattr(agy_switch, symbol),
                f"Symbol '{symbol}' defined in agy_switcher missing from agy_switch launcher",
            )
            pkg_obj = getattr(agy_switcher, symbol)
            launcher_obj = getattr(agy_switch, symbol)
            self.assertIs(
                pkg_obj,
                launcher_obj,
                f"Symbol identity mismatch for '{symbol}': {pkg_obj} vs {launcher_obj}",
            )

    def test_watch_launcher_uses_realpath_and_exports(self):
        watch_path = SCRIPTS_DIR / "watch.py"
        source = watch_path.read_text(encoding="utf-8")
        self.assertIn("os.path.realpath(__file__)", source)
        try:
            from scripts import watch
        except ImportError:
            import watch
        self.assertTrue(hasattr(watch, "LogWatcher"))
        self.assertTrue(hasattr(watch, "DaemonManager"))
        self.assertTrue(hasattr(watch, "main"))

    def test_canonical_symlink_execution(self):
        """Verifies executable execution via canonical symlink (ln -s scripts/agy_switch.py)."""
        target_script = (SCRIPTS_DIR / "agy_switch.py").resolve()
        with tempfile.TemporaryDirectory() as tmpdir:
            symlink_path = Path(tmpdir) / "test-launcher"
            os.symlink(target_script, symlink_path)

            # Test general --help
            res = subprocess.run(
                [str(symlink_path), "--help"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(res.returncode, 0, f"Stderr: {res.stderr}")
            self.assertIn("usage: agy-switch", res.stdout)
            self.assertIn("{list,quota,status,doctor,switch", res.stdout)

            # Test subcommand --help
            res_sub = subprocess.run(
                [str(symlink_path), "status", "--help"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(res_sub.returncode, 0, f"Stderr: {res_sub.stderr}")
            self.assertIn("usage: agy-switch status", res_sub.stdout)

    def test_watch_symlink_execution(self):
        """Verifies executable execution of watch.py via symlink."""
        target_script = (SCRIPTS_DIR / "watch.py").resolve()
        with tempfile.TemporaryDirectory() as tmpdir:
            symlink_path = Path(tmpdir) / "test-watch-launcher"
            os.symlink(target_script, symlink_path)

            res = subprocess.run(
                [str(symlink_path), "--help"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(res.returncode, 0, f"Stderr: {res.stderr}")
            self.assertIn("usage: watch.py", res.stdout)
            self.assertIn("--generate-systemd", res.stdout)


class TestCLIDispatcher(unittest.TestCase):
    """Validates CLI argument parsing and proxy bridge interception."""

    def setUp(self):
        self.engine = MagicMock(spec=AgySwitchEngine)
        self.engine.list_accounts.return_value = {
            "active": "acc1",
            "accounts": [
                {"name": "acc1", "is_active": True, "valid": True, "email": "a@b.com"},
            ],
        }
        self.engine.get_active_name.return_value = "acc1"
        self.engine.get_status.return_value = {
            "active": "acc1",
            "email": "a@b.com",
            "server_running": False,
            "server_pid": None,
            "server_busy": False,
        }

    def test_parser_contains_all_subcommands(self):
        parser = build_parser()
        subparsers_dict = {}
        for action in parser._actions:
            if action.dest == "command" and hasattr(action, "choices") and action.choices:
                subparsers_dict = action.choices
                break

        expected_commands = [
            "list",
            "quota",
            "status",
            "doctor",
            "switch",
            "add",
            "remove",
            "rename",
            "migrate",
            "login",
            "config",
            "hook",
            "hook-pre-invocation",
            "watch",
            "internal-probe",
        ]
        for cmd in expected_commands:
            self.assertIn(cmd, subparsers_dict, f"Subcommand '{cmd}' missing from CLI parser")

    @patch("agy_switch.QuotaClient")
    def test_cli_proxy_bridge_quota_client(self, mock_qc_cls):
        mock_qc = mock_qc_cls.return_value
        mock_qc.fetch_quota.return_value = {"gemini": {"quota": 90, "reset_time": 1700000000}}
        mock_qc.load_cache.return_value = {}

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            code = main(["list", "--quota"], engine=self.engine)
        self.assertEqual(code, 0)
        self.assertEqual(mock_qc.fetch_quota.call_count, 1)

    @patch("agy_switch.RotationEngine")
    @patch("agy_switch.QuotaClient")
    def test_cli_proxy_bridge_rotation_engine(self, mock_qc_cls, mock_re_cls):
        mock_qc = mock_qc_cls.return_value
        mock_qc.fetch_quota.return_value = {"gemini": {"quota": 10}}
        mock_re = mock_re_cls.return_value
        mock_re.evaluate.return_value = "acc2"

        with patch("sys.stdout"):
            code = main(["switch", "--auto"], engine=self.engine)
        self.assertEqual(code, 0)
        self.engine.switch_tokens.assert_called_once_with("acc2", restart_server=True, force=False)


if __name__ == "__main__":
    unittest.main()
