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
        stdin_data: str | bytes | None = None,
        cwd: Path | str | None = None,
        env_extra: dict[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], float]:
        """Execute scripts/verify-build.sh measuring runtime duration."""
        env = os.environ.copy()
        if env_extra:
            env.update(env_extra)

        stdin_bytes: bytes | None
        if isinstance(stdin_data, str):
            stdin_bytes = stdin_data.encode("utf-8")
        else:
            stdin_bytes = stdin_data

        start = time.perf_counter()
        proc = subprocess.run(
            ["bash", str(VERIFY_BUILD_SCRIPT)],
            input=stdin_bytes,
            cwd=str(cwd) if cwd else str(REPO_ROOT),
            capture_output=True,
            env=env,
            check=False,
        )
        elapsed = time.perf_counter() - start
        completed = subprocess.CompletedProcess(
            args=proc.args,
            returncode=proc.returncode,
            stdout=proc.stdout.decode("utf-8", errors="replace"),
            stderr=proc.stderr.decode("utf-8", errors="replace"),
        )
        return completed, elapsed

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

        # 6. Multi-root workspace containing empty string or whitespace before agy-switcher
        multi_root_with_empty_payload = json.dumps({
            "cwd": "/home/user/projects/other-app",
            "workspacePaths": [
                "",
                "   ",
                str(REPO_ROOT),
            ],
        })
        proc_multi_empty, _ = self._run_script(
            stdin_data=multi_root_with_empty_payload,
            cwd="/tmp",
            env_extra={"AGY_VERIFY_DRY_RUN": "1"},
        )
        self.assertEqual(proc_multi_empty.returncode, 0)
        self.assertIn("[GUARD_PASSED]", proc_multi_empty.stdout)

    def test_workspace_guard_malformed(self) -> None:
        """[WS-GUARD-ERR-001] & [WS-GUARD-ERR-002] Workspace guard resilience on malformed stdin."""
        # Seam Check: verify scripts/verify-build.sh declares workspace_guard_resilience seam
        script_content = VERIFY_BUILD_SCRIPT.read_text(encoding="utf-8")
        self.assertIn(
            "workspace_guard_resilience",
            script_content,
            "scripts/verify-build.sh must declare workspace_guard_resilience seam",
        )
        self.assertIn(
            "-n 65536",
            script_content,
            "scripts/verify-build.sh must bound stdin read with -n 65536 to prevent unbounded memory/time consumption",
        )

        # [WS-GUARD-ERR-001]: Non-blocking exit 0 when receiving empty, malformed, or non-JSON stdin outside repo
        err_001_payloads: list[str | bytes | None] = [
            # Malformed JSON (syntax errors, truncated syntax)
            '{"cwd": "/tmp", "workspacePaths": incomplete',
            '{"cwd": ',
            '{"',
            '{"cwd": "',
            '{"cwd": "/tmp"',
            '{"cwd": "/tmp", "workspacePaths":',
            '{"workspacePaths": [',
            '{"workspacePaths": ["/tmp',
            '{"workspacePaths": ["/tmp", ',
            '{not valid json}',
            '{"unclosed_brace": true',
            # Truncated bytes / binary streams
            b"\x00\x01\x02\xff\xfe\xfd\x80\x81",
            b"\x00",
            b"\xff\xfe",
            b"prefix\x00suffix",
            # Malformed unicode / surrogate sequences
            b'{"cwd": "/tmp\xed\xa0\x80"}',
            b'{"cwd": "\xff\xfe\xfd"}',
            b'{"cwd":\x00"/tmp"}',
            b'{"\xff\xfe": 1}',
            b"\x80\x81\x82\x83\x84\x85\x86\x87",
            # Empty stdin and EOF on stdin
            "",
            b"",
            None,
            "   \n\t\r\n   ",
            # Non-JSON streams (logs, html, yaml)
            "Plain text log stream line 1\nLine 2 with arbitrary characters: @#$%^&*()\n",
            "<!DOCTYPE html><html><body>Error 500</body></html>",
            "::: non json payload :::",
            # Large non-JSON payload bounded read (70KB and 120KB)
            "x" * 70000,
            "y" * 120000,
        ]

        for payload in err_001_payloads:
            with self.subTest(scenario="WS-GUARD-ERR-001", payload=str(payload)[:30]):
                proc, elapsed = self._run_script(stdin_data=payload, cwd="/tmp")
                self.assertEqual(
                    proc.returncode,
                    0,
                    f"Expected exit 0 for payload {payload!r}, got {proc.returncode}. Stderr: {proc.stderr}",
                )
                self.assertNotIn("Verifying 5-layer AST modular architecture", proc.stdout)
                self.assertNotIn("mypy", proc.stdout)
                self.assertNotIn("Running full unit test suite", proc.stdout)
                self.assertLess(
                    elapsed,
                    0.25,
                    f"Execution exceeded fast-path threshold for payload {payload!r}: {elapsed:.3f}s",
                )

        # [WS-GUARD-ERR-002]: Non-object JSON payloads (arrays, primitives, non-string cwd) caught cleanly
        err_002_payloads: list[str] = [
            # Non-object JSON arrays
            '["item1", "item2"]',
            '[1, 2, 3]',
            '[{"cwd": "/tmp"}]',
            '[]',
            '[[]]',
            '[[1, 2], [3, 4]]',
            '[{}]',
            '[null, false, true, "string", 123]',
            '[[[[[[[[1]]]]]]]]',
            # Primitive JSON values
            '"just a plain string"',
            '12345',
            '42.5',
            'true',
            'false',
            'null',
            '0',
            '-1',
            '1e10',
            # Non-string cwd or unexpected JSON schemas
            '{"cwd": 12345}',
            '{"cwd": null}',
            '{"cwd": true}',
            '{"cwd": false}',
            '{"cwd": ["/tmp"]}',
            '{"cwd": {"path": "/tmp"}}',
            '{"workspacePaths": "not_an_array"}',
            '{"workspacePaths": [1, 2, 3]}',
            '{"workspacePaths": []}',
            '{"workspacePaths": [""]}',
            '{"workspacePaths": [null]}',
            '{"workspacePaths": false}',
            '{"cwd": null, "workspacePaths": null}',
            '{"cwd": false, "workspacePaths": false}',
            '{"cwd": true, "workspacePaths": true}',
            '{"cwd": 0, "workspacePaths": 0}',
            '{"cwd": {}, "workspacePaths": {}}',
            '{"cwd": [], "workspacePaths": []}',
            '{}',
            '{"": ""}',
            '{"cwd": ""}',
            '{"cwd": "   "}',
            '{"unknown_field": 42}',
            '{"a": {"b": {"c": {"cwd": "/tmp"}}}}',
            '{\n  \t  "cwd"   \t\n:  \t\n  "/tmp" \n\t }',
            '{\n  "workspacePaths":\n  [\n    "/tmp",\n    ""\n  ]\n}',
        ]

        for payload_str in err_002_payloads:
            with self.subTest(scenario="WS-GUARD-ERR-002", payload=payload_str):
                proc, elapsed = self._run_script(stdin_data=payload_str, cwd="/tmp")
                self.assertEqual(
                    proc.returncode,
                    0,
                    f"Expected exit 0 for non-object JSON {payload_str!r}, got {proc.returncode}. Stderr: {proc.stderr}",
                )
                self.assertNotIn("Verifying 5-layer AST modular architecture", proc.stdout)
                self.assertNotIn("mypy", proc.stdout)
                self.assertNotIn("Running full unit test suite", proc.stdout)
                self.assertLess(
                    elapsed,
                    0.25,
                    f"Fast-path execution took too long: {elapsed:.3f}s",
                )

    def test_workspace_guard_error_resilience(self) -> None:
        """Backward-compatible alias for test_workspace_guard_malformed."""
        self.test_workspace_guard_malformed()

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

    def test_workspace_guard_paths(self) -> None:
        """[WS-GUARD-EDGE-001] & [WS-GUARD-EDGE-002] Subpath, symlink & multi-root resolution."""
        # Seam Check: verify scripts/verify-build.sh declares workspace_guard_paths seam
        script_content = VERIFY_BUILD_SCRIPT.read_text(encoding="utf-8")
        self.assertIn(
            "workspace_guard_paths",
            script_content,
            "scripts/verify-build.sh must declare workspace_guard_paths seam",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)

            # --- [WS-GUARD-EDGE-001]: Symlinked verify-build.sh execution ---
            # Symlinked script invoked from a repository sub-directory (parent contains pyproject.toml)
            # must resolve REPO_ROOT realpath and permit Tier 1 gate to execute and pass.
            symlink_script = tmppath / "verify-symlink.sh"
            symlink_script.symlink_to(VERIFY_BUILD_SCRIPT)

            proc_symlink_script = subprocess.run(
                ["bash", str(symlink_script)],
                cwd=str(REPO_ROOT / "tests"),
                capture_output=True,
                text=True,
                env={**os.environ, "AGY_VERIFY_MAX_TIER": "1"},
                check=False,
            )
            self.assertEqual(
                proc_symlink_script.returncode,
                0,
                f"Symlinked script must resolve REPO_ROOT and exit 0. Stderr: {proc_symlink_script.stderr}",
            )
            self.assertIn("Tier 1 gate passed cleanly", proc_symlink_script.stdout)

            # --- [WS-GUARD-EDGE-001]: Subpath & Symlinked workspace directories ---
            # Subpath invocation from inside repository (tests/ and scripts/)
            for subpath in [REPO_ROOT / "tests", REPO_ROOT / "scripts"]:
                proc_sub, _ = self._run_script(
                    stdin_data=None,
                    cwd=subpath,
                    env_extra={"AGY_VERIFY_DRY_RUN": "1"},
                )
                self.assertEqual(proc_sub.returncode, 0)
                self.assertIn("[GUARD_PASSED]", proc_sub.stdout)

            # Symlink pointing to the repository root
            symlink_repo = tmppath / "symlink-repo-root"
            symlink_repo.symlink_to(REPO_ROOT)
            proc_sym_repo, _ = self._run_script(
                stdin_data=None,
                cwd=symlink_repo,
                env_extra={"AGY_VERIFY_DRY_RUN": "1"},
            )
            self.assertEqual(proc_sym_repo.returncode, 0)
            self.assertIn("[GUARD_PASSED]", proc_sym_repo.stdout)

            # Symlink pointing to a subpath inside the repository (e.g. tests/)
            symlink_subpath = tmppath / "symlink-to-tests"
            symlink_subpath.symlink_to(REPO_ROOT / "tests")
            proc_sym_sub, _ = self._run_script(
                stdin_data=None,
                cwd=symlink_subpath,
                env_extra={"AGY_VERIFY_DRY_RUN": "1"},
            )
            self.assertEqual(proc_sym_sub.returncode, 0)
            self.assertIn("[GUARD_PASSED]", proc_sym_sub.stdout)

            # Symlinked subpath inside an external clone with pyproject.toml marker
            ext_worktree = tmppath / "ext-worktree"
            ext_worktree.mkdir()
            (ext_worktree / "pyproject.toml").write_text(
                '[project]\nname = "agy-switch"\n',
                encoding="utf-8",
            )
            (ext_worktree / "subdir").mkdir()
            symlink_ext_sub = tmppath / "symlink-to-ext-sub"
            symlink_ext_sub.symlink_to(ext_worktree / "subdir")

            proc_sym_ext, _ = self._run_script(
                stdin_data=None,
                cwd=symlink_ext_sub,
                env_extra={"AGY_VERIFY_DRY_RUN": "1"},
            )
            self.assertEqual(proc_sym_ext.returncode, 0)
            self.assertIn("[GUARD_PASSED]", proc_sym_ext.stdout)

            # --- [WS-GUARD-EDGE-002]: Multi-root workspace sessions ---
            # Foreign cwd, but workspacePaths includes agy-switcher repo root
            multi_root_payload = json.dumps({
                "cwd": "/home/user/foreign-app",
                "workspacePaths": [
                    "/home/user/foreign-app",
                    str(REPO_ROOT),
                ],
            })
            proc_mr_root, _ = self._run_script(
                stdin_data=multi_root_payload,
                cwd="/tmp",
                env_extra={"AGY_VERIFY_MAX_TIER": "1"},
            )
            self.assertEqual(proc_mr_root.returncode, 0)
            self.assertIn("Tier 1 gate passed cleanly", proc_mr_root.stdout)

            # Foreign cwd, workspacePaths includes a symlink to agy-switcher repo
            multi_root_sym_repo = json.dumps({
                "cwd": "/home/user/foreign-app",
                "workspacePaths": [
                    "/home/user/foreign-app",
                    str(symlink_repo),
                ],
            })
            proc_mr_sym_repo, _ = self._run_script(
                stdin_data=multi_root_sym_repo,
                cwd="/tmp",
                env_extra={"AGY_VERIFY_DRY_RUN": "1"},
            )
            self.assertEqual(proc_mr_sym_repo.returncode, 0)
            self.assertIn("[GUARD_PASSED]", proc_mr_sym_repo.stdout)

            # Foreign cwd, workspacePaths includes a subpath inside agy-switcher (e.g. scripts/)
            multi_root_subpath = json.dumps({
                "cwd": "/home/user/foreign-app",
                "workspacePaths": [
                    "/home/user/foreign-app",
                    str(REPO_ROOT / "scripts"),
                ],
            })
            proc_mr_sub, _ = self._run_script(
                stdin_data=multi_root_subpath,
                cwd="/tmp",
                env_extra={"AGY_VERIFY_DRY_RUN": "1"},
            )
            self.assertEqual(proc_mr_sub.returncode, 0)
            self.assertIn("[GUARD_PASSED]", proc_mr_sub.stdout)

            # Foreign cwd, workspacePaths includes a symlink to a subpath inside agy-switcher
            multi_root_sym_sub = json.dumps({
                "cwd": "/home/user/foreign-app",
                "workspacePaths": [
                    "/home/user/foreign-app",
                    str(symlink_subpath),
                ],
            })
            proc_mr_sym_sub, _ = self._run_script(
                stdin_data=multi_root_sym_sub,
                cwd="/tmp",
                env_extra={"AGY_VERIFY_DRY_RUN": "1"},
            )
            self.assertEqual(proc_mr_sym_sub.returncode, 0)
            self.assertIn("[GUARD_PASSED]", proc_mr_sym_sub.stdout)

            # Foreign cwd, workspacePaths includes external worktree with marker
            multi_root_ext = json.dumps({
                "cwd": "/home/user/foreign-app",
                "workspacePaths": [
                    "/home/user/foreign-app",
                    str(symlink_ext_sub),
                ],
            })
            proc_mr_ext, _ = self._run_script(
                stdin_data=multi_root_ext,
                cwd="/tmp",
                env_extra={"AGY_VERIFY_DRY_RUN": "1"},
            )
            self.assertEqual(proc_mr_ext.returncode, 0)
            self.assertIn("[GUARD_PASSED]", proc_mr_ext.stdout)

            # Negative case: Foreign cwd and workspacePaths containing only foreign paths
            multi_root_foreign_only = json.dumps({
                "cwd": "/home/user/foreign-app",
                "workspacePaths": [
                    "/home/user/foreign-app",
                    "/home/user/another-project",
                ],
            })
            proc_mr_foreign, _ = self._run_script(
                stdin_data=multi_root_foreign_only,
                cwd="/tmp",
                env_extra={"AGY_VERIFY_MAX_TIER": "1"},
            )
            self.assertEqual(proc_mr_foreign.returncode, 0)
            self.assertNotIn("Tier 1 gate passed cleanly", proc_mr_foreign.stdout)
            self.assertNotIn("[GUARD_PASSED]", proc_mr_foreign.stdout)

    def test_workspace_guard_rejects_foreign_directory_with_alias_name(self) -> None:
        """Prevent bare basename false positives when foreign directory is named agy-switch or agy-switcher."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            for alias in ("agy-switch", "agy-switcher"):
                foreign_alias_dir = tmppath / alias
                foreign_alias_dir.mkdir(parents=True)
                # 1. Unrelated pyproject.toml
                foreign_pyproject = foreign_alias_dir / "pyproject.toml"
                foreign_pyproject.write_text('[project]\nname = "unrelated-app"\n', encoding="utf-8")

                # Foreign workspace payload with alias basename
                payload = json.dumps({
                    "cwd": str(foreign_alias_dir),
                    "workspacePaths": [str(foreign_alias_dir)],
                })
                proc, elapsed = self._run_script(
                    stdin_data=payload,
                    cwd=foreign_alias_dir,
                    env_extra={"AGY_VERIFY_DRY_RUN": "1"},
                )
                self.assertEqual(proc.returncode, 0)
                self.assertNotIn("[GUARD_PASSED]", proc.stdout)
                self.assertNotIn("Tier 1 gate passed cleanly", proc.stdout)
                self.assertLess(elapsed, 0.25)

                # 2. Foreign alias dir without pyproject.toml at all
                foreign_empty_alias = tmppath / f"empty-{alias}" / alias
                foreign_empty_alias.mkdir(parents=True)
                proc_empty, elapsed_empty = self._run_script(
                    stdin_data=None,
                    cwd=foreign_empty_alias,
                    env_extra={"AGY_VERIFY_DRY_RUN": "1"},
                )
                self.assertEqual(proc_empty.returncode, 0)
                self.assertNotIn("[GUARD_PASSED]", proc_empty.stdout)
                self.assertNotIn("Tier 1 gate passed cleanly", proc_empty.stdout)
                self.assertLess(elapsed_empty, 0.25)


if __name__ == "__main__":
    unittest.main()
