import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"


class TestInstallerScript(unittest.TestCase):

    def setUp(self):
        self.temp_home = tempfile.TemporaryDirectory()
        self.temp_workspace = tempfile.TemporaryDirectory()
        self.env = os.environ.copy()
        self.env["HOME"] = self.temp_home.name
        self.bin_link = Path(self.temp_home.name) / ".local" / "bin" / "agy-switch"
        self.global_plugin = (
            Path(self.temp_home.name) / ".gemini" / "config" / "plugins" / "agy-switch"
        )
        self.local_plugin = (
            Path(self.temp_workspace.name) / ".agents" / "plugins" / "agy-switch"
        )

    def tearDown(self):
        self.temp_home.cleanup()
        self.temp_workspace.cleanup()

    def run_installer(self, *args, cwd=None):
        cmd = ["bash", str(INSTALL_SH)] + list(args)
        return subprocess.run(
            cmd,
            cwd=cwd or self.temp_workspace.name,
            env=self.env,
            capture_output=True,
            text=True,
        )

    def test_script_syntax_and_set_e(self):
        content = INSTALL_SH.read_text()
        self.assertIn("set -e", content, "install.sh must declare 'set -e'")

    def test_symlink_commands_use_force_flag(self):
        content = INSTALL_SH.read_text()
        # Look for ln -s without -f
        lines = content.splitlines()
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "ln -s " in stripped and "ln -sf" not in stripped:
                self.fail(f"Line {i} uses unforced symlink 'ln -s': {line}")

    def test_install_local_symlink_and_idempotency(self):
        res = self.run_installer("--local")
        self.assertEqual(res.returncode, 0, f"Stderr: {res.stderr}")
        self.assertTrue(self.local_plugin.is_symlink())
        self.assertTrue(self.bin_link.is_symlink())

        # Test idempotency - running again should succeed without error
        res2 = self.run_installer("--local")
        self.assertEqual(res2.returncode, 0, f"Stderr: {res2.stderr}")
        self.assertTrue(self.local_plugin.is_symlink())
        self.assertTrue(self.bin_link.is_symlink())

    def test_install_global_symlink(self):
        res = self.run_installer("--global")
        self.assertEqual(res.returncode, 0, f"Stderr: {res.stderr}")
        self.assertTrue(self.global_plugin.is_symlink())
        self.assertTrue(self.bin_link.is_symlink())

    def test_install_copy_mode(self):
        res = self.run_installer("--local", "--copy")
        self.assertEqual(res.returncode, 0, f"Stderr: {res.stderr}")
        self.assertTrue(self.local_plugin.is_dir())
        self.assertFalse(self.local_plugin.is_symlink())
        self.assertTrue(self.bin_link.is_symlink())

    def test_uninstall_local(self):
        self.run_installer("--local")
        self.assertTrue(self.local_plugin.exists())
        self.assertTrue(self.bin_link.exists())

        res = self.run_installer("--uninstall", "--local")
        self.assertEqual(res.returncode, 0, f"Stderr: {res.stderr}")
        self.assertFalse(self.local_plugin.exists())
        self.assertFalse(self.bin_link.exists())

    def test_uninstall_global(self):
        self.run_installer("--global")
        self.assertTrue(self.global_plugin.exists())
        self.assertTrue(self.bin_link.exists())

        res = self.run_installer("--uninstall", "--global")
        self.assertEqual(res.returncode, 0, f"Stderr: {res.stderr}")
        self.assertFalse(self.global_plugin.exists())
        self.assertFalse(self.bin_link.exists())

    def test_uninstall_without_flags_removes_both_and_launcher(self):
        # Install both
        self.run_installer("--local")
        self.run_installer("--global")
        self.assertTrue(self.local_plugin.exists())
        self.assertTrue(self.global_plugin.exists())
        self.assertTrue(self.bin_link.exists())

        # Run uninstall with no target flag
        res = self.run_installer("--uninstall")
        self.assertEqual(res.returncode, 0, f"Stderr: {res.stderr}")
        self.assertFalse(self.local_plugin.exists())
        self.assertFalse(self.global_plugin.exists())
        self.assertFalse(self.bin_link.exists())

    def test_uninstall_cleans_orphaned_broken_symlink(self):
        # Simulate an orphaned broken launcher symlink
        bin_dir = Path(self.temp_home.name) / ".local" / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        orphan_target = (
            Path(self.temp_workspace.name)
            / ".agents"
            / "plugins"
            / "agy-switch"
            / "scripts"
            / "agy_switch.py"
        )
        self.bin_link.symlink_to(orphan_target)
        self.assertTrue(self.bin_link.is_symlink())
        self.assertFalse(self.bin_link.exists())  # Broken link

        res = self.run_installer("--uninstall")
        self.assertEqual(res.returncode, 0, f"Stderr: {res.stderr}")
        self.assertFalse(self.bin_link.is_symlink())
        self.assertFalse(self.bin_link.exists())


if __name__ == "__main__":
    unittest.main()
