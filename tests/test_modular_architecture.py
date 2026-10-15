"""Unit and architectural tests for Layer 4: CLI Dispatcher, Package Init & Re-Export Launchers.

Validates:
1. Strict 5-Layer DAG compliance via static AST analysis:
   - Layer 0: exceptions.py, models.py
   - Layer 1: supervisor.py, formatters.py, telemetry.py
   - Layer 2: vault.py, rotation.py, doctor.py, watcher.py
   - Layer 3: hook.py
   - Layer 4: cli.py, __init__.py
   - No module in Layer N imports from Layer >= N+1 at module level.
   - doctor.py and rotation.py do not import vault.py at module level.
   - No sub-module in agy_switcher imports agy_switch at module level.
2. Isolated subprocess namespace imports and PEP 562 lazy-loading.
3. Re-export facade launcher (scripts/agy_switch.py) symlink safety and completeness.
4. Re-export shim launcher (scripts/watch.py) symlink safety and delegation.
5. CLI subcommand parsing, return codes, and Dynamic Module Proxy Bridge.
"""

from __future__ import annotations

import ast
import inspect
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


LAYER_MAP = {
    # Layer 0
    "exceptions.py": 0,
    "models.py": 0,
    # Layer 1
    "supervisor.py": 1,
    "formatters.py": 1,
    "telemetry.py": 1,
    # Layer 2
    "vault.py": 2,
    "rotation.py": 2,
    "doctor.py": 2,
    "watcher.py": 2,
    # Layer 3
    "hook.py": 3,
    # Layer 4
    "cli.py": 4,
    "__init__.py": 4,
}

MODULE_NAME_TO_LAYER = {
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
        for filename, current_layer in LAYER_MAP.items():
            file_path = PACKAGE_DIR / filename
            self.assertTrue(file_path.is_file(), f"Missing expected module: {filename}")

            tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))

            for node in tree.body:
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        mod_name = alias.name
                        self.assertFalse(
                            mod_name == "agy_switch" or mod_name.startswith("agy_switch."),
                            f"{filename} imports agy_switch at module level!",
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
                        mod_name == "agy_switch" or mod_name.startswith("agy_switch."),
                        f"{filename} imports from agy_switch at module level!",
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

    def test_layer_2_horizontal_independence(self):
        """DoctorEngine and RotationEngine must NOT import vault.py at module level."""
        for fn in ["doctor.py", "rotation.py"]:
            file_path = PACKAGE_DIR / fn
            tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
            for node in tree.body:
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotIn("vault", alias.name)
                elif isinstance(node, ast.ImportFrom):
                    self.assertNotIn("vault", node.module or "")

    def test_zero_external_pip_dependencies(self):
        """All modules in agy_switcher must strictly import standard library only."""
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
            "scripts",
        }
        for file_path in PACKAGE_DIR.glob("*.py"):
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
        cmd = [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, 'scripts'); "
            "import agy_switcher.telemetry; "
            "assert 'agy_switcher.vault' not in sys.modules; "
            "assert 'agy_switch' not in sys.modules; "
            "print('OK')",
        ]
        res = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Stderr: {res.stderr}")
        self.assertIn("OK", res.stdout)

    def test_cli_isolated_import(self):
        cmd = [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, 'scripts'); "
            "import agy_switcher.cli; "
            "assert 'agy_switch' not in sys.modules; "
            "print('OK')",
        ]
        res = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Stderr: {res.stderr}")
        self.assertIn("OK", res.stdout)

    def test_package_pep562_lazy_loading(self):
        """import agy_switcher does not eagerly import heavy submodules."""
        cmd = [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, 'scripts'); "
            "import agy_switcher; "
            "assert 'agy_switcher.cli' not in sys.modules; "
            "assert 'agy_switcher.doctor' not in sys.modules; "
            "assert 'agy_switcher.watcher' not in sys.modules; "
            "# Accessing attribute triggers lazy load\n"
            "_ = agy_switcher.AgySwitchEngine; "
            "assert 'agy_switcher.vault' in sys.modules; "
            "print('OK')",
        ]
        res = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True)
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
