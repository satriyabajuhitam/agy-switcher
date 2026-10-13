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
    return res.stdout.strip()


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
            stdin=subprocess.DEVNULL,
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

    def test_piped_bootstrap_installs_globally_by_default(self):
        env = self.env.copy()
        env["AGY_SWITCH_REPO_URL"] = f"file://{REPO_ROOT}"
        env["AGY_SWITCH_BRANCH"] = get_current_branch()

        cmd = f"cat '{INSTALL_SH}' | bash"
        res = subprocess.run(
            cmd,
            shell=True,
            cwd=self.temp_workspace.name,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Stdout: {res.stdout}\nStderr: {res.stderr}")
        self.assertIn("agy-switch Remote Bootstrap Installer", res.stdout)

        default_target_repo = Path(self.temp_home.name) / "agy-switcher"
        self.assertTrue(default_target_repo.is_dir(), "Repository should be cloned to ~/agy-switcher")
        self.assertTrue((default_target_repo / ".git").is_dir(), ".git must exist")
        self.assertTrue((default_target_repo / "scripts" / "agy_switch.py").is_file())
        self.assertTrue((default_target_repo / "plugin.json").is_file())

        # Verify global installation was performed by default
        self.assertTrue(self.global_plugin.exists(), "Global plugin should be installed")
        self.assertTrue(self.bin_link.exists(), "Launcher binary link should be created")
        self.assertEqual(self.global_plugin.resolve(), default_target_repo.resolve())
        self.assertEqual(
            self.bin_link.resolve(),
            (default_target_repo / "scripts" / "agy_switch.py").resolve(),
        )

    def test_piped_bootstrap_respects_agy_switch_home_override(self):
        custom_home = Path(self.temp_workspace.name) / "custom-switcher"
        env = self.env.copy()
        env["AGY_SWITCH_REPO_URL"] = f"file://{REPO_ROOT}"
        env["AGY_SWITCH_BRANCH"] = get_current_branch()
        env["AGY_SWITCH_HOME"] = str(custom_home)

        res = subprocess.run(
            f"cat '{INSTALL_SH}' | bash",
            shell=True,
            cwd=self.temp_workspace.name,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Stdout: {res.stdout}\nStderr: {res.stderr}")
        self.assertTrue(custom_home.is_dir(), "Repository should be cloned to custom AGY_SWITCH_HOME")
        self.assertTrue((custom_home / "scripts" / "agy_switch.py").is_file())
        self.assertFalse((Path(self.temp_home.name) / "agy-switcher").exists(), "Default ~/agy-switcher should not exist")
        self.assertTrue(self.global_plugin.exists(), "Global plugin should be installed")
        self.assertTrue(self.bin_link.exists(), "Launcher binary link should be created")
        self.assertEqual(self.global_plugin.resolve(), custom_home.resolve())
        self.assertEqual(
            self.bin_link.resolve(),
            (custom_home / "scripts" / "agy_switch.py").resolve(),
        )

    def test_local_flags_do_not_trigger_bootstrap_clone(self):
        # Running local installation directly from repo root
        res_local = self.run_installer("--local")
        self.assertEqual(res_local.returncode, 0, f"Stderr: {res_local.stderr}")
        self.assertNotIn("agy-switch Remote Bootstrap Installer", res_local.stdout)
        self.assertFalse((Path(self.temp_home.name) / "agy-switcher").exists())
        self.assertTrue(self.local_plugin.is_symlink())

        # Running global installation directly from repo root
        res_global = self.run_installer("--global")
        self.assertEqual(res_global.returncode, 0, f"Stderr: {res_global.stderr}")
        self.assertNotIn("agy-switch Remote Bootstrap Installer", res_global.stdout)
        self.assertFalse((Path(self.temp_home.name) / "agy-switcher").exists())
        self.assertTrue(self.global_plugin.is_symlink())

    def test_bootstrap_missing_git_halts_with_code_1_and_descriptive_error(self):
        env = self.env.copy()
        # Provide a restricted PATH containing essential shell utilities but without git
        temp_bin = Path(self.temp_workspace.name) / "bin_no_git"
        temp_bin.mkdir(parents=True, exist_ok=True)
        for tool in ["bash", "sh", "rm", "cat", "ls", "dirname", "mkdir"]:
            tool_path = shutil.which(tool)
            if tool_path and Path(tool_path).exists():
                (temp_bin / tool).symlink_to(tool_path)

        env["PATH"] = str(temp_bin)
        env["AGY_SWITCH_REPO_URL"] = f"file://{REPO_ROOT}"
        env["AGY_SWITCH_BRANCH"] = get_current_branch()

        res = subprocess.run(
            f"cat '{INSTALL_SH}' | bash",
            shell=True,
            cwd=self.temp_workspace.name,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 1)
        output = res.stdout + res.stderr
        self.assertIn("Error: git is required for remote installation.", output)
        self.assertFalse((Path(self.temp_home.name) / "agy-switcher").exists())

    def test_piped_bootstrap_forwards_local_flag(self):
        env = self.env.copy()
        env["AGY_SWITCH_REPO_URL"] = f"file://{REPO_ROOT}"
        env["AGY_SWITCH_BRANCH"] = get_current_branch()

        cmd = f"cat '{INSTALL_SH}' | bash -s -- --local"
        res = subprocess.run(
            cmd,
            shell=True,
            cwd=self.temp_workspace.name,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Stdout: {res.stdout}\nStderr: {res.stderr}")
        self.assertTrue(self.local_plugin.is_symlink())
        self.assertFalse(self.global_plugin.exists())
        default_target_repo = Path(self.temp_home.name) / "agy-switcher"
        self.assertEqual(self.local_plugin.resolve(), default_target_repo.resolve())

    def test_standalone_script_without_adjacent_files_activates_bootstrap(self):
        standalone_script = Path(self.temp_workspace.name) / "standalone_install.sh"
        standalone_script.write_text(INSTALL_SH.read_text())

        env = self.env.copy()
        env["AGY_SWITCH_REPO_URL"] = f"file://{REPO_ROOT}"
        env["AGY_SWITCH_BRANCH"] = get_current_branch()

        res = subprocess.run(
            ["bash", str(standalone_script)],
            cwd=self.temp_workspace.name,
            env=env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )
        self.assertEqual(res.returncode, 0, f"Stdout: {res.stdout}\nStderr: {res.stderr}")
        self.assertIn("agy-switch Remote Bootstrap Installer", res.stdout)
        self.assertTrue((Path(self.temp_home.name) / "agy-switcher").is_dir())
        self.assertTrue(self.global_plugin.exists())

    def test_bootstrap_clone_failure_cleans_up_target_dir(self):
        env = self.env.copy()
        env["AGY_SWITCH_REPO_URL"] = "file:///nonexistent/repo.git"
        env["AGY_SWITCH_BRANCH"] = "dev"

        target_dir = Path(self.temp_home.name) / "agy-switcher"
        res = subprocess.run(
            f"cat '{INSTALL_SH}' | bash",
            shell=True,
            cwd=self.temp_workspace.name,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertFalse(target_dir.exists(), "Failed clone must clean up target directory")

    def test_piped_bootstrap_defaults_to_dev_branch_when_branch_env_unset(self):
        env = self.env.copy()
        env["AGY_SWITCH_REPO_URL"] = f"file://{REPO_ROOT}"
        env.pop("AGY_SWITCH_BRANCH", None)

        cmd = f"cat '{INSTALL_SH}' | bash"
        res = subprocess.run(
            cmd,
            shell=True,
            cwd=self.temp_workspace.name,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Stdout: {res.stdout}\nStderr: {res.stderr}")
        default_target_repo = Path(self.temp_home.name) / "agy-switcher"
        branch_res = subprocess.run(
            ["git", "-C", str(default_target_repo), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(branch_res.stdout.strip(), "dev")

    def test_piped_bootstrap_into_preexisting_empty_directory(self):
        default_target_repo = Path(self.temp_home.name) / "agy-switcher"
        default_target_repo.mkdir(parents=True, exist_ok=True)

        env = self.env.copy()
        env["AGY_SWITCH_REPO_URL"] = f"file://{REPO_ROOT}"
        env["AGY_SWITCH_BRANCH"] = get_current_branch()

        cmd = f"cat '{INSTALL_SH}' | bash"
        res = subprocess.run(
            cmd,
            shell=True,
            cwd=self.temp_workspace.name,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Stdout: {res.stdout}\nStderr: {res.stderr}")
        self.assertTrue((default_target_repo / ".git").is_dir())
        self.assertTrue(self.global_plugin.exists())

    def test_piped_bootstrap_with_custom_home_containing_spaces(self):
        custom_home = Path(self.temp_workspace.name) / "custom path with spaces"
        env = self.env.copy()
        env["AGY_SWITCH_REPO_URL"] = f"file://{REPO_ROOT}"
        env["AGY_SWITCH_BRANCH"] = get_current_branch()
        env["AGY_SWITCH_HOME"] = str(custom_home)

        res = subprocess.run(
            f"cat '{INSTALL_SH}' | bash",
            shell=True,
            cwd=self.temp_workspace.name,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Stdout: {res.stdout}\nStderr: {res.stderr}")
        self.assertTrue(custom_home.is_dir())
        self.assertEqual(self.global_plugin.resolve(), custom_home.resolve())

    def test_piped_bootstrap_forwards_short_flags_and_copy_mode(self):
        env = self.env.copy()
        env["AGY_SWITCH_REPO_URL"] = f"file://{REPO_ROOT}"
        env["AGY_SWITCH_BRANCH"] = get_current_branch()

        # Test short -l flag
        cmd_l = f"cat '{INSTALL_SH}' | bash -s -- -l"
        res_l = subprocess.run(
            cmd_l,
            shell=True,
            cwd=self.temp_workspace.name,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(res_l.returncode, 0, f"Stdout: {res_l.stdout}\nStderr: {res_l.stderr}")
        self.assertTrue(self.local_plugin.is_symlink())

        # Test --copy mode without explicit target flag defaults to --global --copy
        cmd_copy = f"cat '{INSTALL_SH}' | bash -s -- --copy"
        res_copy = subprocess.run(
            cmd_copy,
            shell=True,
            cwd=self.temp_workspace.name,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(res_copy.returncode, 0, f"Stdout: {res_copy.stdout}\nStderr: {res_copy.stderr}")
        self.assertTrue(self.global_plugin.is_dir())
        self.assertFalse(self.global_plugin.is_symlink())

    def test_piped_bootstrap_preserves_installer_exit_code_on_invalid_arguments(self):
        env = self.env.copy()
        env["AGY_SWITCH_REPO_URL"] = f"file://{REPO_ROOT}"
        env["AGY_SWITCH_BRANCH"] = get_current_branch()

        cmd = f"cat '{INSTALL_SH}' | bash -s -- --invalid-flag"
        res = subprocess.run(
            cmd,
            shell=True,
            cwd=self.temp_workspace.name,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 1)
        self.assertIn("Unknown parameter passed: --invalid-flag", res.stdout + res.stderr)

    def test_piped_bootstrap_safe_under_bash_nounset(self):
        env = self.env.copy()
        env["AGY_SWITCH_REPO_URL"] = f"file://{REPO_ROOT}"
        env["AGY_SWITCH_BRANCH"] = get_current_branch()

        cmd = f"cat '{INSTALL_SH}' | bash -u"
        res = subprocess.run(
            cmd,
            shell=True,
            cwd=self.temp_workspace.name,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Stdout: {res.stdout}\nStderr: {res.stderr}")
        self.assertNotIn("unbound variable", res.stderr)
        self.assertTrue(self.global_plugin.exists())

    def test_piped_bootstrap_forwards_uninstall_flag(self):
        # First install
        env = self.env.copy()
        env["AGY_SWITCH_REPO_URL"] = f"file://{REPO_ROOT}"
        env["AGY_SWITCH_BRANCH"] = get_current_branch()

        res_install = subprocess.run(
            f"cat '{INSTALL_SH}' | bash",
            shell=True,
            cwd=self.temp_workspace.name,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(res_install.returncode, 0)
        self.assertTrue(self.global_plugin.exists())
        self.assertTrue(self.bin_link.exists())

        # Forward --uninstall via piped bootstrap
        res_uninstall = subprocess.run(
            f"cat '{INSTALL_SH}' | bash -s -- --uninstall",
            shell=True,
            cwd=self.temp_workspace.name,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(res_uninstall.returncode, 0, f"Stdout: {res_uninstall.stdout}\nStderr: {res_uninstall.stderr}")
        self.assertFalse(self.global_plugin.exists())
        self.assertFalse(self.bin_link.exists())

    def test_piped_bootstrap_when_target_is_regular_file_does_not_delete_it(self):
        target_file = Path(self.temp_home.name) / "agy-switcher"
        target_file.write_text("pre-existing important file")

        env = self.env.copy()
        env["AGY_SWITCH_REPO_URL"] = f"file://{REPO_ROOT}"
        env["AGY_SWITCH_BRANCH"] = get_current_branch()

        res = subprocess.run(
            f"cat '{INSTALL_SH}' | bash",
            shell=True,
            cwd=self.temp_workspace.name,
            env=env,
            capture_output=True,
            text=True,
        )
        # Should fail or not overwrite, but crucially the file MUST NOT be deleted
        self.assertTrue(target_file.is_file(), "Regular file at target path must not be deleted")
        self.assertEqual(target_file.read_text(), "pre-existing important file")


if __name__ == "__main__":
    unittest.main()
