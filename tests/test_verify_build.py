"""Unit tests for Workspace Guard and execution scoping in scripts/verify-build.sh.

Governed by ADR 0020 and .scratch/lifecycle-hooks/spec.md#L59-L75.
Verifies fast-path exit 0 (<5ms) on foreign workspace and full verification inside repo.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

REPO_ROOT = Path(__file__).resolve().parent.parent
VERIFY_BUILD_SCRIPT = REPO_ROOT / "scripts" / "verify-build.sh"


class TestVerifyBuildWorkspaceGuard(unittest.TestCase):
    """Test suite for scripts/verify-build.sh Workspace Guard."""

    def setUp(self) -> None:
        """Verify the target script exists and is executable."""
        self.assertTrue(
            VERIFY_BUILD_SCRIPT.is_file(),
            f"Script {VERIFY_BUILD_SCRIPT} must exist",
        )

    def _run_script(
        self,
        stdin_data: str | None = None,
        cwd: Path | str | None = None,
        env_extra: dict[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], float]:
        """Execute scripts/verify-build.sh measuring runtime duration."""
        env = os.environ.copy()
        if env_extra:
            env.update(env_extra)

        start = time.perf_counter()
        proc = subprocess.run(
            ["bash", str(VERIFY_BUILD_SCRIPT)],
            input=stdin_data,
            cwd=str(cwd) if cwd else str(REPO_ROOT),
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        elapsed = time.perf_counter() - start
        return proc, elapsed

    def test_workspace_guard_basic(self) -> None:
        """[WS-GUARD-001] Fast exit 0 on foreign workspace, proceed inside agy-switcher."""
        # 1. Foreign workspace passed via stdin payload
        foreign_payload = json.dumps({
            "cwd": "/home/user/projects/other-app",
            "workspacePaths": ["/home/user/projects/other-app"],
        })
        proc, elapsed = self._run_script(stdin_data=foreign_payload, cwd="/tmp")
        self.assertEqual(
            proc.returncode,
            0,
            f"Foreign workspace must exit 0, got {proc.returncode}. stderr: {proc.stderr}",
        )
        # Verify that heavy verification stages were NOT executed
        self.assertNotIn("Verifying 5-layer AST modular architecture", proc.stdout)
        self.assertNotIn("mypy", proc.stdout)
        self.assertNotIn("Running full unit test suite", proc.stdout)
        # Verify fast-path performance (< 0.25s including python subprocess spawn overhead)
        self.assertLess(
            elapsed,
            0.25,
            f"Fast-path foreign exit took too long: {elapsed:.3f}s",
        )

        # 2. Foreign workspace payload even when invoked from inside repo cwd
        proc_in_repo, elapsed_in_repo = self._run_script(
            stdin_data=foreign_payload,
            cwd=REPO_ROOT,
        )
        self.assertEqual(proc_in_repo.returncode, 0)
        self.assertNotIn("Verifying 5-layer AST modular architecture", proc_in_repo.stdout)
        self.assertLess(elapsed_in_repo, 0.25)

        # 3. Invocation from external $PWD with empty stdin
        proc_ext_empty, elapsed_ext_empty = self._run_script(stdin_data="", cwd="/tmp")
        self.assertEqual(proc_ext_empty.returncode, 0)
        self.assertNotIn("Verifying 5-layer AST modular architecture", proc_ext_empty.stdout)
        self.assertLess(elapsed_ext_empty, 0.25)

        # 4. [WS-GUARD-002] Inside agy-switcher: guard permits execution
        proc_pass, _ = self._run_script(
            stdin_data=None,
            cwd=REPO_ROOT,
            env_extra={"AGY_VERIFY_DRY_RUN": "1"},
        )
        self.assertEqual(proc_pass.returncode, 0)
        self.assertIn("[GUARD_PASSED]", proc_pass.stdout)

        # 5. [WS-GUARD-EDGE-002] Multi-root workspace containing agy-switcher in workspacePaths
        multi_root_payload = json.dumps({
            "cwd": "/home/user/projects/other-app",
            "workspacePaths": [
                "/home/user/projects/other-app",
                str(REPO_ROOT),
            ],
        })
        proc_multi, _ = self._run_script(
            stdin_data=multi_root_payload,
            cwd="/tmp",
            env_extra={"AGY_VERIFY_DRY_RUN": "1"},
        )
        self.assertEqual(proc_multi.returncode, 0)
        self.assertIn("[GUARD_PASSED]", proc_multi.stdout)

    def test_workspace_guard_error_resilience(self) -> None:
        """[WS-GUARD-ERR-001] & [WS-GUARD-ERR-002] Graceful non-blocking exit 0 on malformed input."""
        # Malformed JSON from external directory
        proc_malformed, elapsed = self._run_script(
            stdin_data='{"cwd": "/tmp", "workspacePaths": incomplete',
            cwd="/tmp",
        )
        self.assertEqual(proc_malformed.returncode, 0)
        self.assertNotIn("Verifying 5-layer AST modular architecture", proc_malformed.stdout)
        self.assertLess(elapsed, 0.25)

        # Non-object JSON array
        proc_array, elapsed_arr = self._run_script(
            stdin_data='["item1", "item2"]',
            cwd="/tmp",
        )
        self.assertEqual(proc_array.returncode, 0)
        self.assertNotIn("Verifying 5-layer AST modular architecture", proc_array.stdout)
        self.assertLess(elapsed_arr, 0.25)

        # Non-string cwd value
        proc_num_cwd, elapsed_num = self._run_script(
            stdin_data='{"cwd": 12345}',
            cwd="/tmp",
        )
        self.assertEqual(proc_num_cwd.returncode, 0)
        self.assertNotIn("Verifying 5-layer AST modular architecture", proc_num_cwd.stdout)
        self.assertLess(elapsed_num, 0.25)

    def test_workspace_guard_subpath_and_marker(self) -> None:
        """[WS-GUARD-EDGE-001] Sub-directory execution and pyproject.toml marker resolution."""
        # Subpath inside repository (e.g. tests/)
        proc_subpath, _ = self._run_script(
            stdin_data=None,
            cwd=REPO_ROOT / "tests",
            env_extra={"AGY_VERIFY_DRY_RUN": "1"},
        )
        self.assertEqual(proc_subpath.returncode, 0)
        self.assertIn("[GUARD_PASSED]", proc_subpath.stdout)

        # External temporary clone/worktree with pyproject.toml marker
        with tempfile.TemporaryDirectory() as tmpdir:
            worktree_dir = Path(tmpdir) / "sub" / "worktree"
            worktree_dir.mkdir(parents=True)
            marker_file = worktree_dir / "pyproject.toml"
            marker_file.write_text('[project]\nname = "agy-switch"\n', encoding="utf-8")

            nested_dir = worktree_dir / "deep" / "nested"
            nested_dir.mkdir(parents=True)

            proc_marker, _ = self._run_script(
                stdin_data=None,
                cwd=nested_dir,
                env_extra={"AGY_VERIFY_DRY_RUN": "1"},
            )
            self.assertEqual(proc_marker.returncode, 0)
            self.assertIn("[GUARD_PASSED]", proc_marker.stdout)

    def test_workspace_guard_tier1_execution(self) -> None:
        """[WS-GUARD-002] Inside repo, verification executes and runs gate stages."""
        proc, _ = self._run_script(
            stdin_data=None,
            cwd=REPO_ROOT,
            env_extra={"AGY_VERIFY_MAX_TIER": "1"},
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("Verifying 5-layer AST modular architecture", proc.stdout)
        self.assertIn("Tier 1 gate passed cleanly", proc.stdout)


if __name__ == "__main__":
    unittest.main()
