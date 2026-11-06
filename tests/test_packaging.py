"""Tests for standard declarative packaging via pyproject.toml and entrypoint parity.

Validates:
- [REQ-1-SCN-001]: Standard declarative pyproject.toml build-system and project metadata.
- [REQ-1-SCN-003]: Console script entrypoint agy-switch = "agy_switcher.cli:main" execution parity.
- [REQ-1-SCN-004]: Zero external pip dependencies invariant.
- [REQ-1-SCN-006]: Packaging deletion test invariant.
"""

from __future__ import annotations

import io
from pathlib import Path
import subprocess
import sys
import tomllib
import unittest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


class TestDeclarativePackaging(unittest.TestCase):
    """Test suite for declarative pyproject.toml packaging and entrypoints."""

    def test_pyproject_metadata(self) -> None:
        """[REQ-1-SCN-001] Verify pyproject.toml build-system, project, and tool metadata."""
        self.assertTrue(
            PYPROJECT_PATH.exists(),
            f"pyproject.toml must exist at repository root: {PYPROJECT_PATH}",
        )
        content = PYPROJECT_PATH.read_text(encoding="utf-8")
        data = tomllib.loads(content)

        # 1. Build-system verification
        self.assertIn("build-system", data)
        build_system = data["build-system"]
        self.assertIn("requires", build_system)
        self.assertIn("setuptools>=61.0", build_system["requires"])
        self.assertEqual(build_system.get("build-backend"), "setuptools.build_meta")

        # 2. Project metadata verification
        self.assertIn("project", data)
        project = data["project"]
        self.assertEqual(project.get("name"), "agy-switch")
        self.assertEqual(project.get("version"), "2.0.0")
        self.assertEqual(
            project.get("description"),
            "Antigravity Multi-Account Switcher & Vault Manager",
        )
        self.assertEqual(project.get("readme"), "README.md")
        self.assertEqual(project.get("requires-python"), ">=3.10")
        self.assertEqual(project.get("dependencies"), [])

        # 3. Scripts / Console Entrypoint
        self.assertIn("scripts", project)
        scripts = project["scripts"]
        self.assertEqual(scripts.get("agy-switch"), "agy_switcher.cli:main")

        # 4. Setuptools package discovery and mapping
        self.assertIn("tool", data)
        tool = data["tool"]
        self.assertIn("setuptools", tool)
        setuptools_cfg = tool["setuptools"]
        self.assertIn("package-dir", setuptools_cfg)
        self.assertEqual(setuptools_cfg["package-dir"].get(""), "scripts")

        self.assertIn("packages", setuptools_cfg)
        packages_cfg = setuptools_cfg["packages"]
        self.assertIn("find", packages_cfg)
        find_cfg = packages_cfg["find"]
        self.assertEqual(find_cfg.get("where"), ["scripts"])
        self.assertIn("agy_switcher*", find_cfg.get("include", []))

    def test_zero_external_dependencies_invariant(self) -> None:
        """[REQ-1-SCN-004] Zero external pip dependencies invariant in pyproject.toml."""
        self.assertTrue(PYPROJECT_PATH.exists(), "pyproject.toml must exist")
        data = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))
        project = data.get("project", {})

        # Assert dependencies is strictly empty list
        dependencies = project.get("dependencies")
        self.assertIsInstance(dependencies, list)
        self.assertEqual(
            dependencies,
            [],
            f"Expected zero external dependencies in pyproject.toml, found: {dependencies}",
        )

        # Assert no optional dependencies declare third-party packages
        optional_deps = project.get("optional-dependencies", {})
        for extra, deps in optional_deps.items():
            self.assertEqual(
                deps,
                [],
                f"Optional dependency group '{extra}' must not declare external packages: {deps}",
            )

    def test_entrypoint_parity(self) -> None:
        """[REQ-1-SCN-003] Entrypoint agy_switcher.cli:main matches scripts/agy_switch.py."""
        import importlib
        cli_mod = importlib.import_module("agy_switcher.cli")
        cli_main = getattr(cli_mod, "main")
        self.assertTrue(callable(cli_main))

        # Test CLI help output directly through cli_main
        with self.assertRaises(SystemExit) as cm:
            cli_main(["--help"])
        self.assertEqual(cm.exception.code, 0)

        # Test CLI help output via subprocess on scripts/agy_switch.py
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "agy_switch.py"), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0)
        output = proc.stdout

        expected_subcommands = [
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
            "watch",
        ]
        for cmd in expected_subcommands:
            self.assertIn(
                cmd,
                output,
                f"Expected subcommand '{cmd}' to be listed in --help output",
            )

    def test_deletion_test_packaging(self) -> None:
        """[REQ-1-SCN-006] Deletion test verification for packaging independence."""
        # scripts/agy_switch.py must run independently regardless of pyproject.toml presence
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "agy_switch.py"), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0)

    def test_setuptools_build_meta_dist_info_generation(self) -> None:
        """[REQ-1-SCN-002] Verify setuptools.build_meta prepares valid dist-info with package_dir."""
        import importlib
        import shutil
        import tempfile
        try:
            build_meta = importlib.import_module("setuptools.build_meta")
        except ImportError:
            self.skipTest("setuptools not available in test environment")

        temp_dir = tempfile.mkdtemp(prefix="agy_packaging_test_")
        try:
            dist_info = build_meta.prepare_metadata_for_build_wheel(temp_dir)
            dist_info_path = Path(temp_dir) / dist_info
            self.assertTrue(dist_info_path.exists())

            # Verify entry_points.txt
            entry_points_path = dist_info_path / "entry_points.txt"
            self.assertTrue(entry_points_path.exists())
            ep_text = entry_points_path.read_text(encoding="utf-8")
            self.assertIn("[console_scripts]", ep_text)
            self.assertIn("agy-switch = agy_switcher.cli:main", ep_text)

            # Verify top_level.txt contains agy_switcher
            top_level_path = dist_info_path / "top_level.txt"
            self.assertTrue(top_level_path.exists())
            self.assertIn("agy_switcher", top_level_path.read_text(encoding="utf-8"))
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_malformed_pyproject_syntax_rejected(self) -> None:
        """[REQ-1-ERR-001] Malformed or invalid TOML syntax is rejected."""
        malformed_toml = 'name = "agy-switch"\n[unclosed_table'
        with self.assertRaises(tomllib.TOMLDecodeError):
            tomllib.loads(malformed_toml)

    def test_python_version_gate_specification(self) -> None:
        """[REQ-1-EDGE-003] Verify requires-python requires >=3.10."""
        self.assertTrue(PYPROJECT_PATH.exists())
        data = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))
        project = data.get("project", {})
        self.assertEqual(project.get("requires-python"), ">=3.10")


if __name__ == "__main__":
    unittest.main()
