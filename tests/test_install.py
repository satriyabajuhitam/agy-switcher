import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"


def get_current_branch():
    res = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    branch = res.stdout.strip()
    return "main" if branch == "HEAD" else branch


class TestPrerequisitesAndCollisionProtection(unittest.TestCase):
    __qualname__ = "Test_BOOTSTRAP_ERR_001|BOOTSTRAP_ERR_003"
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

    def run_installer(self, *args, extra_env=None, cwd=None):
        env = self.env.copy()
        if extra_env:
            env.update(extra_env)
        cmd = ["bash", str(INSTALL_SH)] + list(args)
        return subprocess.run(
            cmd,
            cwd=cwd or self.temp_workspace.name,
            env=env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )

    def run_piped_bootstrap(self, *args, extra_env=None):
        env = self.env.copy()
        default_env = {
            "AGY_SWITCH_REPO_URL": f"file://{REPO_ROOT}",
            "AGY_SWITCH_BRANCH": get_current_branch(),
        }
        for k, v in default_env.items():
            if k not in env:
                env[k] = v
        if extra_env:
            env.update(extra_env)

        cmd = ["bash", "-s", "--"] + list(args)
        return subprocess.run(
            cmd,
            cwd=self.temp_workspace.name,
            env=env,
            capture_output=True,
            text=True,
            input=INSTALL_SH.read_text(encoding="utf-8"),
        )

    def _create_isolated_bin_dir(self, exclude_commands=(), custom_scripts=None):
        fake_bin = Path(self.temp_workspace.name) / "fake_bin"
        fake_bin.mkdir(parents=True, exist_ok=True)

        # Standard unix utilities that install.sh might invoke
        standard_cmds = [
            "bash", "sh", "echo", "printf", "cat", "mkdir", "rm", "cp",
            "chmod", "ln", "dirname", "basename", "readlink", "realpath",
            "ls", "grep", "sed", "awk", "git", "python3", "which", "command"
        ]

        for cmd in standard_cmds:
            if cmd in exclude_commands:
                continue
            cmd_path = shutil.which(cmd)
            if cmd_path:
                symlink_target = fake_bin / cmd
                if not symlink_target.exists():
                    try:
                        symlink_target.symlink_to(cmd_path)
                    except OSError:
                        pass

        if custom_scripts:
            for name, script_content in custom_scripts.items():
                target = fake_bin / name
                if target.is_symlink() or target.exists():
                    target.unlink()
                target.write_text(script_content, encoding="utf-8")
                target.chmod(0o755)

        return str(fake_bin)

    # -------------------------------------------------------------------------
    # Scenario: [BOOTSTRAP-ERR-001] Missing git or python3 fails early
    # -------------------------------------------------------------------------

    def test_BOOTSTRAP_ERR_001_missing_git_fails_early(self):
        """When git is missing, install.sh halts early with error mentioning git."""
        fake_bin = self._create_isolated_bin_dir(exclude_commands=["git"])
        res = self.run_installer("--local", extra_env={"PATH": fake_bin})

        self.assertNotEqual(res.returncode, 0, "Installer must fail when git is missing")
        self.assertIn("git", res.stderr.lower(), "Stderr must mention missing 'git' prerequisite")
        self.assertFalse(self.local_plugin.exists(), "Filesystem state must not be modified")

    def test_BOOTSTRAP_ERR_001_missing_git_in_bootstrap_mode(self):
        """When git is missing in bootstrap mode, installer halts before cloning."""
        fake_bin = self._create_isolated_bin_dir(exclude_commands=["git"])
        res = self.run_piped_bootstrap("--global", extra_env={"PATH": fake_bin})

        self.assertNotEqual(res.returncode, 0, "Bootstrap must fail when git is missing")
        self.assertIn("git", res.stderr.lower(), "Stderr must mention missing 'git'")
        self.assertFalse(self.global_plugin.exists(), "Filesystem state must not be modified")

    def test_BOOTSTRAP_ERR_001_missing_python3_fails_early(self):
        """When python3 is missing, install.sh halts early with error mentioning python3."""
        fake_bin = self._create_isolated_bin_dir(exclude_commands=["python3"])
        res = self.run_installer("--local", extra_env={"PATH": fake_bin})

        self.assertNotEqual(res.returncode, 0, "Installer must fail when python3 is missing")
        self.assertIn("python3", res.stderr.lower(), "Stderr must mention missing 'python3' prerequisite")
        self.assertFalse(self.local_plugin.exists(), "Filesystem state must not be modified")

    def test_BOOTSTRAP_ERR_001_missing_python3_in_bootstrap_mode(self):
        """When python3 is missing in bootstrap mode, installer halts before setup."""
        fake_bin = self._create_isolated_bin_dir(exclude_commands=["python3"])
        res = self.run_piped_bootstrap("--global", extra_env={"PATH": fake_bin})

        self.assertNotEqual(res.returncode, 0, "Bootstrap must fail when python3 is missing")
        self.assertIn("python3", res.stderr.lower(), "Stderr must mention missing 'python3'")
        self.assertFalse(self.global_plugin.exists(), "Filesystem state must not be modified")

    def test_BOOTSTRAP_ERR_001_python3_version_below_3_10_fails_early(self):
        """When python3 version is < 3.10, install.sh halts early with version error."""
        fake_python = (
            "#!/bin/sh\n"
            "if [ \"$1\" = \"--version\" ]; then\n"
            "    echo 'Python 3.9.7'\n"
            "    exit 0\n"
            "fi\n"
            "exit 1\n"
        )
        fake_bin = self._create_isolated_bin_dir(
            custom_scripts={"python3": fake_python}
        )
        res = self.run_installer("--local", extra_env={"PATH": fake_bin})

        self.assertNotEqual(res.returncode, 0, "Installer must fail when python3 < 3.10")
        combined_err = (res.stderr + res.stdout).lower()
        self.assertTrue(
            "3.10" in combined_err or "python" in combined_err,
            f"Stderr must mention python version requirement: {res.stderr}"
        )
        self.assertFalse(self.local_plugin.exists(), "Filesystem state must not be modified")

    # -------------------------------------------------------------------------
    # Scenario: [BOOTSTRAP-ERR-003] Existing foreign file at canonical launcher path
    # -------------------------------------------------------------------------

    def test_BOOTSTRAP_ERR_003_foreign_regular_file_blocks_installer(self):
        """A non-symlink regular file at ~/.local/bin/agy-switch halts safely without overwrite."""
        self.bin_link.parent.mkdir(parents=True, exist_ok=True)
        foreign_content = "FOREIGN_BINARY_CONTENT_DO_NOT_OVERWRITE"
        self.bin_link.write_text(foreign_content, encoding="utf-8")

        res = self.run_installer("--global")

        self.assertNotEqual(res.returncode, 0, "Installer must refuse to overwrite foreign regular file")
        self.assertIn("foreign", res.stderr.lower(), f"Expected foreign binary warning in stderr: {res.stderr}")
        self.assertTrue(self.bin_link.is_file() and not self.bin_link.is_symlink(), "Foreign file must not be converted to symlink")
        self.assertEqual(self.bin_link.read_text(encoding="utf-8"), foreign_content, "Foreign file content must not be modified")

    def test_BOOTSTRAP_ERR_003_foreign_executable_blocks_installer(self):
        """An executable regular file (binary) at ~/.local/bin/agy-switch halts safely."""
        self.bin_link.parent.mkdir(parents=True, exist_ok=True)
        foreign_content = "#!/bin/sh\necho 'i am a foreign utility'\n"
        self.bin_link.write_text(foreign_content, encoding="utf-8")
        self.bin_link.chmod(0o755)

        res = self.run_installer("--local")

        self.assertNotEqual(res.returncode, 0, "Installer must refuse to overwrite foreign executable")
        self.assertIn("foreign", res.stderr.lower(), f"Expected foreign binary error in stderr: {res.stderr}")
        self.assertTrue(self.bin_link.is_file() and not self.bin_link.is_symlink(), "Foreign executable must not be unlinked")
        self.assertEqual(self.bin_link.read_text(encoding="utf-8"), foreign_content, "Foreign file content must be preserved")

    def test_BOOTSTRAP_ERR_003_symlink_refreshed_idempotently(self):
        """Pre-existing symlinks at ~/.local/bin/agy-switch are refreshed idempotently without error."""
        self.bin_link.parent.mkdir(parents=True, exist_ok=True)
        dummy_target = Path(self.temp_workspace.name) / "old_target"
        dummy_target.write_text("#!/bin/sh\necho old\n", encoding="utf-8")
        dummy_target.chmod(0o755)
        self.bin_link.symlink_to(dummy_target)

        res = self.run_installer("--global")

        self.assertEqual(res.returncode, 0, f"Installer should refresh existing symlink: {res.stderr}")
        self.assertTrue(self.bin_link.is_symlink(), "Existing link should be updated as a symlink")
        self.assertNotEqual(os.readlink(self.bin_link), str(dummy_target), "Symlink target must be updated")

    def test_BOOTSTRAP_ERR_003_idempotent_existing_agy_launcher(self):
        """An existing agy-switch launcher symlink is safely refreshed without error."""
        self.bin_link.parent.mkdir(parents=True, exist_ok=True)
        agy_target = REPO_ROOT / "scripts" / "agy_switch.py"
        self.bin_link.symlink_to(agy_target)

        res = self.run_installer("--global")

        self.assertEqual(res.returncode, 0, f"Installer should succeed on idempotent re-install: {res.stderr}")
        self.assertTrue(self.bin_link.is_symlink(), "Launcher must be a symlink")

    def test_BOOTSTRAP_ERR_003_uninstall_ignores_foreign_binary(self):
        """Uninstalling does not fail on or delete a foreign file at ~/.local/bin/agy-switch."""
        self.bin_link.parent.mkdir(parents=True, exist_ok=True)
        foreign_content = "FOREIGN_BINARY"
        self.bin_link.write_text(foreign_content, encoding="utf-8")

        res = self.run_installer("--uninstall")

        self.assertEqual(res.returncode, 0, f"Uninstall should succeed without error: {res.stderr}")
        self.assertTrue(self.bin_link.is_file(), "Foreign file should still exist")
        self.assertEqual(self.bin_link.read_text(encoding="utf-8"), foreign_content, "Foreign file content must not be modified")


if __name__ == "__main__":
    unittest.main()
