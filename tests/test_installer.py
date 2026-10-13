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

    def run_piped_bootstrap(
        self,
        args=None,
        extra_env=None,
        script_input=None,
        cwd=None,
        bash_options=None,
    ):
        env = self.env.copy()
        default_env = {
            "AGY_SWITCH_REPO_URL": f"file://{REPO_ROOT}",
            "AGY_SWITCH_BRANCH": get_current_branch(),
        }
        for k, v in default_env.items():
            if k not in env:
                env[k] = v

        if extra_env:
            for k, v in extra_env.items():
                if v is None:
                    env.pop(k, None)
                else:
                    env[k] = str(v)

        cmd = ["bash"]
        if bash_options:
            if isinstance(bash_options, (list, tuple)):
                cmd.extend(bash_options)
            else:
                cmd.append(bash_options)

        if args is not None:
            cmd.extend(["-s", "--"])
            if isinstance(args, (list, tuple)):
                cmd.extend(args)
            else:
                cmd.append(args)

        input_data = (
            script_input
            if script_input is not None
            else INSTALL_SH.read_text()
        )
        if isinstance(input_data, bytes):
            input_data = input_data.decode("utf-8")

        return subprocess.run(
            cmd,
            cwd=cwd or self.temp_workspace.name,
            env=env,
            capture_output=True,
            text=True,
            input=input_data,
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
        res = self.run_piped_bootstrap()
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
        res = self.run_piped_bootstrap(extra_env={"AGY_SWITCH_HOME": str(custom_home)})
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
        temp_bin = Path(self.temp_workspace.name) / "bin_no_git"
        temp_bin.mkdir(parents=True, exist_ok=True)
        for tool in ["bash", "sh", "rm", "cat", "ls", "dirname", "mkdir"]:
            tool_path = shutil.which(tool)
            if tool_path and Path(tool_path).exists():
                (temp_bin / tool).symlink_to(tool_path)

        res = self.run_piped_bootstrap(extra_env={"PATH": str(temp_bin)})
        self.assertEqual(res.returncode, 1)
        output = res.stdout + res.stderr
        self.assertIn("Error: git is required for remote installation.", output)
        self.assertFalse((Path(self.temp_home.name) / "agy-switcher").exists())

    def test_piped_bootstrap_forwards_local_flag(self):
        res = self.run_piped_bootstrap(args=["--local"])
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
        target_dir = Path(self.temp_home.name) / "agy-switcher"
        res = self.run_piped_bootstrap(
            extra_env={
                "AGY_SWITCH_REPO_URL": "file:///nonexistent/repo.git",
                "AGY_SWITCH_BRANCH": "dev",
            }
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertFalse(target_dir.exists(), "Failed clone must clean up target directory")

    def test_piped_bootstrap_defaults_to_dev_branch_when_branch_env_unset(self):
        res = self.run_piped_bootstrap(extra_env={"AGY_SWITCH_BRANCH": None})
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

        res = self.run_piped_bootstrap()
        self.assertEqual(res.returncode, 0, f"Stdout: {res.stdout}\nStderr: {res.stderr}")
        self.assertTrue((default_target_repo / ".git").is_dir())
        self.assertTrue(self.global_plugin.exists())

    def test_piped_bootstrap_with_custom_home_containing_spaces(self):
        custom_home = Path(self.temp_workspace.name) / "custom path with spaces"
        res = self.run_piped_bootstrap(extra_env={"AGY_SWITCH_HOME": str(custom_home)})
        self.assertEqual(res.returncode, 0, f"Stdout: {res.stdout}\nStderr: {res.stderr}")
        self.assertTrue(custom_home.is_dir())
        self.assertEqual(self.global_plugin.resolve(), custom_home.resolve())

    def test_piped_bootstrap_forwards_short_flags_and_copy_mode(self):
        # Test short -l flag
        res_l = self.run_piped_bootstrap(args=["-l"])
        self.assertEqual(res_l.returncode, 0, f"Stdout: {res_l.stdout}\nStderr: {res_l.stderr}")
        self.assertTrue(self.local_plugin.is_symlink())

        # Clean up local plugin before testing copy mode
        self.local_plugin.unlink(missing_ok=True)

        # Test --copy mode without explicit target flag defaults to --global --copy
        res_copy = self.run_piped_bootstrap(args=["--copy"])
        self.assertEqual(res_copy.returncode, 0, f"Stdout: {res_copy.stdout}\nStderr: {res_copy.stderr}")
        self.assertTrue(self.global_plugin.is_dir())
        self.assertFalse(self.global_plugin.is_symlink())

    def test_piped_bootstrap_preserves_installer_exit_code_on_invalid_arguments(self):
        res = self.run_piped_bootstrap(args=["--invalid-flag"])
        self.assertEqual(res.returncode, 1)
        self.assertIn("Unknown parameter passed: --invalid-flag", res.stdout + res.stderr)

    def test_piped_bootstrap_safe_under_bash_nounset(self):
        res = self.run_piped_bootstrap(bash_options=["-u"])
        self.assertEqual(res.returncode, 0, f"Stdout: {res.stdout}\nStderr: {res.stderr}")
        self.assertNotIn("unbound variable", res.stderr)
        self.assertTrue(self.global_plugin.exists())

    def test_piped_bootstrap_forwards_uninstall_flag(self):
        # First install
        res_install = self.run_piped_bootstrap()
        self.assertEqual(res_install.returncode, 0)
        self.assertTrue(self.global_plugin.exists())
        self.assertTrue(self.bin_link.exists())

        # Forward --uninstall via piped bootstrap
        res_uninstall = self.run_piped_bootstrap(args=["--uninstall"])
        self.assertEqual(res_uninstall.returncode, 0, f"Stdout: {res_uninstall.stdout}\nStderr: {res_uninstall.stderr}")
        self.assertFalse(self.global_plugin.exists())
        self.assertFalse(self.bin_link.exists())

    def test_piped_bootstrap_when_target_is_regular_file_does_not_delete_it(self):
        target_file = Path(self.temp_home.name) / "agy-switcher"
        target_file.write_text("pre-existing important file")

        res = self.run_piped_bootstrap()
        self.assertTrue(target_file.is_file(), "Regular file at target path must not be deleted")
        self.assertEqual(target_file.read_text(), "pre-existing important file")

    def test_piped_bootstrap_rerun_clean_repo_idempotent_and_syncs(self):
        res1 = self.run_piped_bootstrap()
        self.assertEqual(res1.returncode, 0, f"Stdout: {res1.stdout}\nStderr: {res1.stderr}")
        default_target_repo = Path(self.temp_home.name) / "agy-switcher"
        self.assertTrue(default_target_repo.is_dir())

        res2 = self.run_piped_bootstrap()
        self.assertEqual(res2.returncode, 0, f"Stdout: {res2.stdout}\nStderr: {res2.stderr}")
        self.assertTrue(self.global_plugin.exists())
        self.assertTrue(self.bin_link.exists())

    def test_piped_bootstrap_syncs_updates_from_origin(self):
        upstream = Path(self.temp_workspace.name) / "upstream_repo"
        upstream.mkdir()
        branch = get_current_branch()
        subprocess.run(
            ["git", "clone", "--single-branch", "-b", branch, f"file://{REPO_ROOT}", str(upstream)],
            check=True,
            capture_output=True,
        )

        res1 = self.run_piped_bootstrap(
            extra_env={
                "AGY_SWITCH_REPO_URL": f"file://{upstream}",
                "AGY_SWITCH_BRANCH": branch,
            }
        )
        self.assertEqual(res1.returncode, 0)
        target_repo = Path(self.temp_home.name) / "agy-switcher"

        marker_file = upstream / "version_marker.txt"
        marker_file.write_text("v2.0.0")
        subprocess.run(["git", "-C", str(upstream), "add", "version_marker.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(upstream), "-c", "user.name=test", "-c", "user.email=test@test.com", "commit", "-m", "advance upstream"],
            check=True,
        )

        res2 = self.run_piped_bootstrap(
            extra_env={
                "AGY_SWITCH_REPO_URL": f"file://{upstream}",
                "AGY_SWITCH_BRANCH": branch,
            }
        )
        self.assertEqual(res2.returncode, 0, f"Stdout: {res2.stdout}\nStderr: {res2.stderr}")
        self.assertTrue((target_repo / "version_marker.txt").exists(), "Target repo must be fast-forwarded with upstream changes")
        self.assertEqual((target_repo / "version_marker.txt").read_text(), "v2.0.0")

    def test_piped_bootstrap_dirty_repo_rejected_and_preserves_changes(self):
        res1 = self.run_piped_bootstrap()
        self.assertEqual(res1.returncode, 0)
        target_repo = Path(self.temp_home.name) / "agy-switcher"

        readme = target_repo / "README.md"
        original_readme = readme.read_text()
        uncommitted_content = original_readme + "\nUncommitted local changes"
        readme.write_text(uncommitted_content)

        res2 = self.run_piped_bootstrap()
        self.assertEqual(res2.returncode, 1, f"Expected returncode 1, got {res2.returncode}")
        output = res2.stdout + res2.stderr
        self.assertIn(f"Error: Working directory at {target_repo} has uncommitted changes.", output)
        self.assertEqual(readme.read_text(), uncommitted_content, "Uncommitted changes must be preserved untouched")

    def test_piped_bootstrap_foreign_directory_collision_fails_and_preserves_foreign_files(self):
        target_dir = Path(self.temp_home.name) / "agy-switcher"
        target_dir.mkdir(parents=True, exist_ok=True)
        foreign_file = target_dir / "my_important_document.txt"
        foreign_file.write_text("important foreign content")

        res = self.run_piped_bootstrap()
        self.assertEqual(res.returncode, 1, f"Expected returncode 1, got {res.returncode}")
        output = res.stdout + res.stderr
        self.assertIn(
            f"Error: Destination directory '{target_dir}' already exists and is not an agy-switch repository.",
            output,
        )
        self.assertTrue(foreign_file.is_file())
        self.assertEqual(foreign_file.read_text(), "important foreign content")

    def test_piped_bootstrap_existing_git_repo_missing_identity_files_fails(self):
        target_dir = Path(self.temp_home.name) / "agy-switcher"
        target_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", str(target_dir)], check=True, capture_output=True)
        unrelated_file = target_dir / "unrelated.txt"
        unrelated_file.write_text("hello")
        subprocess.run(["git", "-C", str(target_dir), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(target_dir), "-c", "user.name=test", "-c", "user.email=test@test.com", "commit", "-m", "init"],
            check=True,
        )

        res = self.run_piped_bootstrap()
        self.assertEqual(res.returncode, 1, f"Expected returncode 1, got {res.returncode}")
        output = res.stdout + res.stderr
        self.assertIn(
            f"Error: Existing git repository at '{target_dir}' is not an agy-switch repository.",
            output,
        )
        self.assertTrue(unrelated_file.is_file())
        self.assertEqual(unrelated_file.read_text(), "hello")

    def test_piped_bootstrap_diverged_repo_fails_with_code_1(self):
        upstream = Path(self.temp_workspace.name) / "upstream_repo"
        upstream.mkdir()
        branch = get_current_branch()
        subprocess.run(
            ["git", "clone", "--single-branch", "-b", branch, f"file://{REPO_ROOT}", str(upstream)],
            check=True,
            capture_output=True,
        )

        extra_env = {
            "AGY_SWITCH_REPO_URL": f"file://{upstream}",
            "AGY_SWITCH_BRANCH": branch,
        }
        res1 = self.run_piped_bootstrap(extra_env=extra_env)
        self.assertEqual(res1.returncode, 0)
        target_repo = Path(self.temp_home.name) / "agy-switcher"

        (upstream / "upstream_change.txt").write_text("from upstream")
        subprocess.run(["git", "-C", str(upstream), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(upstream), "-c", "user.name=test", "-c", "user.email=test@test.com", "commit", "-m", "upstream commit"],
            check=True,
        )

        (target_repo / "local_change.txt").write_text("from local")
        subprocess.run(["git", "-C", str(target_repo), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(target_repo), "-c", "user.name=test", "-c", "user.email=test@test.com", "commit", "-m", "local commit"],
            check=True,
        )

        res2 = self.run_piped_bootstrap(extra_env=extra_env)
        self.assertEqual(res2.returncode, 1, f"Expected returncode 1, got {res2.returncode}")
        output = res2.stdout + res2.stderr
        self.assertIn(f"Error: Cannot fast-forward branch '{branch}'. Local branch has diverged.", output)

    def test_piped_bootstrap_exports_git_terminal_prompt_zero(self):
        bin_dir = Path(self.temp_workspace.name) / "bin_probe_prompt"
        bin_dir.mkdir(parents=True, exist_ok=True)
        probe_git = bin_dir / "git"
        real_git = shutil.which("git")
        probe_file = Path(self.temp_workspace.name) / "prompt_probe.txt"
        probe_git.write_text(
            f'#!/bin/bash\nif [ "$1" = "clone" ]; then\n    echo "PROMPT_VALUE=$GIT_TERMINAL_PROMPT" > "{probe_file}"\nfi\nexec "{real_git}" "$@"\n'
        )
        probe_git.chmod(0o755)

        res = self.run_piped_bootstrap(extra_env={"PATH": f"{bin_dir}:{self.env['PATH']}"})
        self.assertEqual(res.returncode, 0, f"Stderr: {res.stderr}")
        self.assertTrue(probe_file.exists(), "Probe file must be written during clone")
        self.assertEqual(probe_file.read_text().strip(), "PROMPT_VALUE=0")

    def _test_signal_cleanup(self, sig_name, sig_flag):
        bin_dir = Path(self.temp_workspace.name) / f"bin_probe_{sig_name.lower()}"
        bin_dir.mkdir(parents=True, exist_ok=True)
        probe_git = bin_dir / "git"
        real_git = shutil.which("git")
        target_dir = Path(self.temp_home.name) / f"agy-switcher-{sig_name.lower()}"

        probe_script = f'''#!/bin/bash
if [ "$1" = "clone" ]; then
    target="${{@: -1}}"
    mkdir -p "$target"
    touch "$target/incomplete_marker.txt"
    kill -{sig_flag} "$PPID"
    sleep 1
    exit 1
fi
exec "{real_git}" "$@"
'''
        probe_git.write_text(probe_script)
        probe_git.chmod(0o755)

        res = self.run_piped_bootstrap(
            extra_env={
                "PATH": f"{bin_dir}:{self.env['PATH']}",
                "AGY_SWITCH_HOME": str(target_dir),
            }
        )
        self.assertEqual(res.returncode, 1)
        self.assertIn(
            "Error: Installation interrupted or failed. Cleaning up incomplete clone...",
            res.stderr,
        )
        self.assertFalse(
            target_dir.exists(),
            f"Target directory must be cleaned up on {sig_name} interruption",
        )

    def test_piped_bootstrap_interrupted_by_sigterm_cleans_up_incomplete_target(self):
        self._test_signal_cleanup("SIGTERM", "TERM")

    def test_piped_bootstrap_interrupted_by_sigint_cleans_up_incomplete_target(self):
        self._test_signal_cleanup("SIGINT", "INT")

    def test_piped_bootstrap_switch_branch_on_existing_clean_repo(self):
        upstream = Path(self.temp_workspace.name) / "upstream_repo"
        upstream.mkdir()
        current_branch = get_current_branch()
        subprocess.run(
            ["git", "clone", "--single-branch", "-b", current_branch, f"file://{REPO_ROOT}", str(upstream)],
            check=True,
            capture_output=True,
        )
        subprocess.run(["git", "-C", str(upstream), "branch", "feature-alt"], check=True)
        (upstream / "alt_marker.txt").write_text("alt branch content")
        subprocess.run(["git", "-C", str(upstream), "checkout", "feature-alt"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(upstream), "add", "alt_marker.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(upstream), "-c", "user.name=test", "-c", "user.email=test@test.com", "commit", "-m", "alt commit"],
            check=True,
        )
        subprocess.run(["git", "-C", str(upstream), "checkout", current_branch], check=True, capture_output=True)

        extra_env = {
            "AGY_SWITCH_REPO_URL": f"file://{upstream}",
            "AGY_SWITCH_BRANCH": current_branch,
        }
        res1 = self.run_piped_bootstrap(extra_env=extra_env)
        self.assertEqual(res1.returncode, 0, f"Stderr: {res1.stderr}")
        target_repo = Path(self.temp_home.name) / "agy-switcher"

        extra_env["AGY_SWITCH_BRANCH"] = "feature-alt"
        res2 = self.run_piped_bootstrap(extra_env=extra_env)
        self.assertEqual(res2.returncode, 0, f"Stderr: {res2.stderr}")
        branch_res = subprocess.run(
            ["git", "-C", str(target_repo), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(branch_res.stdout.strip(), "feature-alt")
        self.assertTrue((target_repo / "alt_marker.txt").exists())

        extra_env["AGY_SWITCH_BRANCH"] = current_branch
        res3 = self.run_piped_bootstrap(extra_env=extra_env)
        self.assertEqual(res3.returncode, 0, f"Stderr: {res3.stderr}")
        branch_res2 = subprocess.run(
            ["git", "-C", str(target_repo), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(branch_res2.stdout.strip(), current_branch)

    def test_piped_bootstrap_existing_git_repo_missing_plugin_json_fails(self):
        target_dir = Path(self.temp_home.name) / "agy-switcher"
        target_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", str(target_dir)], check=True, capture_output=True)
        scripts_dir = target_dir / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        (scripts_dir / "agy_switch.py").write_text("#!/usr/bin/env python3\n")
        subprocess.run(["git", "-C", str(target_dir), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(target_dir), "-c", "user.name=test", "-c", "user.email=test@test.com", "commit", "-m", "init"],
            check=True,
        )

        res = self.run_piped_bootstrap()
        self.assertEqual(res.returncode, 1)
        self.assertIn(
            f"Error: Existing git repository at '{target_dir}' is not an agy-switch repository.",
            res.stdout + res.stderr,
        )

    def test_piped_bootstrap_existing_git_repo_missing_agy_switch_script_fails(self):
        target_dir = Path(self.temp_home.name) / "agy-switcher"
        target_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", str(target_dir)], check=True, capture_output=True)
        (target_dir / "plugin.json").write_text('{"name": "test"}')
        subprocess.run(["git", "-C", str(target_dir), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(target_dir), "-c", "user.name=test", "-c", "user.email=test@test.com", "commit", "-m", "init"],
            check=True,
        )

        res = self.run_piped_bootstrap()
        self.assertEqual(res.returncode, 1)
        self.assertIn(
            f"Error: Existing git repository at '{target_dir}' is not an agy-switch repository.",
            res.stdout + res.stderr,
        )

    def test_piped_bootstrap_dirty_repo_with_untracked_files_fails(self):
        res1 = self.run_piped_bootstrap()
        self.assertEqual(res1.returncode, 0)
        target_repo = Path(self.temp_home.name) / "agy-switcher"

        untracked_file = target_repo / "untracked_work.txt"
        untracked_file.write_text("work in progress")

        res2 = self.run_piped_bootstrap()
        self.assertEqual(res2.returncode, 1)
        output = res2.stdout + res2.stderr
        self.assertIn(f"Error: Working directory at {target_repo} has uncommitted changes.", output)
        self.assertTrue(untracked_file.is_file())
        self.assertEqual(untracked_file.read_text(), "work in progress")

    def test_piped_bootstrap_valid_git_worktree_supported(self):
        upstream_fixture = Path(self.temp_workspace.name) / "upstream_fixture"
        current_branch = get_current_branch()
        subprocess.run(
            ["git", "clone", "--single-branch", "-b", current_branch, f"file://{REPO_ROOT}", str(upstream_fixture)],
            check=True,
            capture_output=True,
        )
        worktree_branch = "worktree-test-branch"
        subprocess.run(
            ["git", "-C", str(upstream_fixture), "branch", worktree_branch],
            check=True,
            capture_output=True,
        )

        fixture_repo = Path(self.temp_workspace.name) / "fixture_repo"
        subprocess.run(
            ["git", "clone", f"file://{upstream_fixture}", str(fixture_repo)],
            check=True,
            capture_output=True,
        )

        target_dir = Path(self.temp_home.name) / "agy-switcher"
        subprocess.run(
            ["git", "-C", str(fixture_repo), "worktree", "add", "-b", worktree_branch, str(target_dir), f"origin/{worktree_branch}"],
            check=True,
            capture_output=True,
        )
        self.assertTrue((target_dir / ".git").is_file(), ".git must be a worktree file, not a directory")

        res = self.run_piped_bootstrap(
            extra_env={
                "AGY_SWITCH_REPO_URL": f"file://{upstream_fixture}",
                "AGY_SWITCH_BRANCH": worktree_branch,
            }
        )
        self.assertEqual(res.returncode, 0, f"Stderr: {res.stderr}")
        self.assertTrue(self.global_plugin.exists())
        self.assertTrue(self.bin_link.exists())

        # Ensure REPO_ROOT has not been touched
        res_branches = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "branch", "--list", worktree_branch],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(res_branches.stdout.strip(), "", "REPO_ROOT must not contain worktree branch")

        wt_list = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertNotIn(worktree_branch, wt_list.stdout)
        self.assertNotIn(str(target_dir), wt_list.stdout)

    def test_installer_help_flag_displays_usage_and_exits_zero(self):
        for flag in ["-h", "--help"]:
            with self.subTest(flag=flag):
                res = self.run_installer(flag)
                self.assertEqual(res.returncode, 0, f"Flag {flag} failed with code {res.returncode}")
                output = res.stdout
                self.assertIn("Usage:", output)
                self.assertIn("Options:", output)
                self.assertIn("--help", output)
                self.assertIn("AGY_SWITCH_HOME", output)
                self.assertIn("AGY_SWITCH_BRANCH", output)
                self.assertIn("AGY_SWITCH_REPO_URL", output)
                self.assertEqual(res.stderr.strip(), "")

    def test_piped_bootstrap_help_flag_exits_early_without_cloning(self):
        for flag in ["-h", "--help"]:
            with self.subTest(flag=flag):
                res = self.run_piped_bootstrap(args=[flag])
                self.assertEqual(res.returncode, 0, f"Piped {flag} failed with code {res.returncode}")
                output = res.stdout
                self.assertIn("Usage:", output)
                self.assertIn("Options:", output)
                self.assertIn("AGY_SWITCH_HOME", output)
                self.assertIn("AGY_SWITCH_BRANCH", output)
                self.assertIn("AGY_SWITCH_REPO_URL", output)
                self.assertEqual(res.stderr.strip(), "")
                self.assertFalse(
                    (Path(self.temp_home.name) / "agy-switcher").exists(),
                    "Help flag must short-circuit without cloning repository",
                )
                self.assertFalse(self.global_plugin.exists(), "Help flag must not install global plugin")
                self.assertFalse(self.bin_link.exists(), "Help flag must not create launcher link")

    def test_piped_bootstrap_with_trailing_slash_in_home_trims_and_installs_cleanly(self):
        for slash_suffix in ["/", "///"]:
            with self.subTest(slash_suffix=slash_suffix):
                custom_home = f"{self.temp_workspace.name}/custom-trimmed{slash_suffix}"
                res = self.run_piped_bootstrap(extra_env={"AGY_SWITCH_HOME": custom_home})
                self.assertEqual(res.returncode, 0, f"Stdout: {res.stdout}\nStderr: {res.stderr}")
                expected_dir = Path(self.temp_workspace.name) / "custom-trimmed"
                self.assertTrue(expected_dir.is_dir())
                self.assertTrue((expected_dir / "scripts" / "agy_switch.py").is_file())
                self.assertEqual(self.global_plugin.resolve(), expected_dir.resolve())

    def test_safe_cleanup_target_refuses_deletion_of_home_on_clone_failure(self):
        for slash_suffix in ["/", "///"]:
            with self.subTest(slash_suffix=slash_suffix):
                sentinel_file = Path(self.temp_home.name) / "sentinel_user_data.txt"
                sentinel_file.write_text("critical user file")

                res = self.run_piped_bootstrap(
                    extra_env={
                        "AGY_SWITCH_REPO_URL": "file:///nonexistent/repo.git",
                        "AGY_SWITCH_BRANCH": "dev",
                        "AGY_SWITCH_HOME": f"{self.temp_home.name}{slash_suffix}",
                    }
                )
                self.assertNotEqual(res.returncode, 0)
                # HOME and sentinel file must remain intact!
                self.assertTrue(Path(self.temp_home.name).is_dir(), "HOME directory must never be deleted")
                self.assertTrue(sentinel_file.is_file(), "User files in HOME must never be deleted")
                self.assertEqual(sentinel_file.read_text(), "critical user file")

    def test_installer_all_flag_without_uninstall_installs_both_local_and_global(self):
        for flag in ["-a", "--all"]:
            with self.subTest(flag=flag):
                # Clean up before each subtest
                if self.local_plugin.is_symlink() or self.local_plugin.is_dir():
                    self.local_plugin.unlink(missing_ok=True)
                if self.global_plugin.is_symlink() or self.global_plugin.is_dir():
                    self.global_plugin.unlink(missing_ok=True)
                if self.bin_link.is_symlink() or self.bin_link.is_file():
                    self.bin_link.unlink(missing_ok=True)

                res = self.run_installer(flag)
                self.assertEqual(res.returncode, 0, f"Stderr: {res.stderr}")
                self.assertTrue(self.local_plugin.is_symlink(), f"Flag {flag} should install local plugin")
                self.assertTrue(self.global_plugin.is_symlink(), f"Flag {flag} should install global plugin")
                self.assertTrue(self.bin_link.is_symlink(), f"Flag {flag} should create launcher binary link")

    def test_piped_bootstrap_forwards_all_flag_and_installs_both(self):
        upstream = Path(self.temp_workspace.name) / "upstream_repo_all"
        upstream.mkdir()
        subprocess.run(["git", "init", str(upstream)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(upstream), "config", "user.name", "test"], check=True)
        subprocess.run(["git", "-C", str(upstream), "config", "user.email", "test@test.com"], check=True)
        subprocess.run(["git", "-C", str(upstream), "checkout", "-b", "dev"], check=True, capture_output=True)
        (upstream / "scripts").mkdir()
        (upstream / "scripts" / "agy_switch.py").write_text("#!/usr/bin/env python3\n")
        (upstream / "scripts" / "agy_switch.py").chmod(0o755)
        (upstream / "plugin.json").write_text('{"name": "agy-switch"}')
        (upstream / "install.sh").write_text(INSTALL_SH.read_text())
        (upstream / "install.sh").chmod(0o755)
        subprocess.run(["git", "-C", str(upstream), "add", "."], check=True)
        subprocess.run(["git", "-C", str(upstream), "commit", "-m", "init"], check=True, capture_output=True)

        res = self.run_piped_bootstrap(
            args=["--all"],
            extra_env={
                "AGY_SWITCH_REPO_URL": f"file://{upstream.resolve()}",
                "AGY_SWITCH_BRANCH": "dev",
            },
        )
        self.assertEqual(res.returncode, 0, f"Stdout: {res.stdout}\nStderr: {res.stderr}")
        self.assertTrue(self.local_plugin.is_symlink(), "--all via bootstrap should install local plugin")
        self.assertTrue(self.global_plugin.is_symlink(), "--all via bootstrap should install global plugin")
        self.assertTrue(self.bin_link.is_symlink(), "--all via bootstrap should create launcher link")

    def test_piped_bootstrap_preserves_single_branch_fetch_refspec(self):
        res = self.run_piped_bootstrap()
        self.assertEqual(res.returncode, 0)
        target_repo = Path(self.temp_home.name) / "agy-switcher"

        fetch_cfg = subprocess.run(
            ["git", "-C", str(target_repo), "config", "remote.origin.fetch"],
            capture_output=True,
            text=True,
            check=True,
        )
        # Should NOT be wild-card +refs/heads/*:refs/remotes/origin/*
        self.assertNotIn("*", fetch_cfg.stdout.strip(), "Must not expand fetch refspec to all branches")

    def test_piped_bootstrap_does_not_mutate_remote_origin_url_on_existing_repo(self):
        # First install
        res1 = self.run_piped_bootstrap()
        self.assertEqual(res1.returncode, 0)
        target_repo = Path(self.temp_home.name) / "agy-switcher"

        # Check existing remote origin url
        orig_url = subprocess.run(
            ["git", "-C", str(target_repo), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        # Second bootstrap run with a different AGY_SWITCH_REPO_URL specified
        res2 = self.run_piped_bootstrap(
            extra_env={"AGY_SWITCH_REPO_URL": "https://example.com/unsolicited/url.git"}
        )
        # It should succeed without rewriting existing remote.origin.url
        self.assertEqual(res2.returncode, 0)
        current_url = subprocess.run(
            ["git", "-C", str(target_repo), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self.assertEqual(current_url, orig_url, "Existing remote.origin.url must not be mutated")

    def test_piped_bootstrap_sync_fetches_strictly_target_branch(self):
        # Create an upstream repo with dev and feature-secret branch
        upstream = Path(self.temp_workspace.name) / "upstream_repo"
        upstream.mkdir()
        subprocess.run(["git", "init", str(upstream)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(upstream), "config", "user.name", "test"], check=True)
        subprocess.run(["git", "-C", str(upstream), "config", "user.email", "test@test.com"], check=True)
        subprocess.run(["git", "-C", str(upstream), "checkout", "-b", "dev"], check=True, capture_output=True)
        
        # Populate with dummy booster files
        (upstream / "scripts").mkdir()
        (upstream / "scripts" / "agy_switch.py").write_text("#!/usr/bin/env python3\n")
        (upstream / "scripts" / "agy_switch.py").chmod(0o755)
        (upstream / "plugin.json").write_text('{"name": "agy-switch"}')
        (upstream / "install.sh").write_text(INSTALL_SH.read_text())
        (upstream / "install.sh").chmod(0o755)
        subprocess.run(["git", "-C", str(upstream), "add", "."], check=True)
        subprocess.run(["git", "-C", str(upstream), "commit", "-m", "init"], check=True, capture_output=True)

        # Create another branch in upstream
        subprocess.run(["git", "-C", str(upstream), "branch", "feature-secret"], check=True)

        res1 = self.run_piped_bootstrap(
            extra_env={
                "AGY_SWITCH_REPO_URL": f"file://{upstream.resolve()}",
                "AGY_SWITCH_BRANCH": "dev",
            }
        )
        self.assertEqual(res1.returncode, 0, f"Stderr: {res1.stderr}")
        target_repo = Path(self.temp_home.name) / "agy-switcher"

        # Check remote branches present in local clone
        remotes_out = subprocess.run(
            ["git", "-C", str(target_repo), "branch", "-r"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.assertNotIn("feature-secret", remotes_out, "Strict branch fetch must not pull other remote branches")

    def test_piped_bootstrap_preserves_vault_tokens_and_strict_permissions(self):
        vault_dir = Path(self.temp_home.name) / ".antigravity-vault"
        acc1_dir = vault_dir / "accounts" / "primary_acc"
        acc2_dir = vault_dir / "accounts" / "standby_acc"
        acc1_dir.mkdir(parents=True, exist_ok=True)
        acc2_dir.mkdir(parents=True, exist_ok=True)
        vault_dir.chmod(0o700)
        acc1_dir.chmod(0o700)
        acc2_dir.chmod(0o700)

        token1_file = acc1_dir / "token.json"
        token2_file = acc2_dir / "token.json"
        content1 = '{"access_token": "primary_token_secret", "refresh_token": "primary_refresh"}'
        content2 = '{"access_token": "standby_token_secret", "refresh_token": "standby_refresh"}'
        token1_file.write_text(content1)
        token2_file.write_text(content2)
        token1_file.chmod(0o600)
        token2_file.chmod(0o600)

        self.assertEqual(vault_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(acc1_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(acc2_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(token1_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(token2_file.stat().st_mode & 0o777, 0o600)

        # First run (initial clone)
        res1 = self.run_piped_bootstrap()
        self.assertEqual(res1.returncode, 0, f"Stdout: {res1.stdout}\nStderr: {res1.stderr}")
        self.assertTrue(self.global_plugin.exists())

        # Second run (idempotent pull)
        res2 = self.run_piped_bootstrap()
        self.assertEqual(res2.returncode, 0, f"Stdout: {res2.stdout}\nStderr: {res2.stderr}")

        # Verify vault directory mode remains strictly 0700
        self.assertEqual(
            vault_dir.stat().st_mode & 0o777,
            0o700,
            "Vault directory ~/.antigravity-vault must retain 0700 permissions",
        )
        self.assertEqual(
            acc1_dir.stat().st_mode & 0o777,
            0o700,
            "Account directory 1 must retain 0700 permissions",
        )
        self.assertEqual(
            acc2_dir.stat().st_mode & 0o777,
            0o700,
            "Account directory 2 must retain 0700 permissions",
        )
        # Verify token files exist, content unmutated, and mode strictly 0600
        for token_file, expected_content in [(token1_file, content1), (token2_file, content2)]:
            self.assertTrue(token_file.is_file(), "Vault token file must exist untouched")
            self.assertEqual(
                token_file.read_text(),
                expected_content,
                "Vault token contents must not be mutated or overwritten",
            )
            self.assertEqual(
                token_file.stat().st_mode & 0o777,
                0o600,
                "Vault token file must retain 0600 permissions",
            )


if __name__ == "__main__":
    unittest.main()
