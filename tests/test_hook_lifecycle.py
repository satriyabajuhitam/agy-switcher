#!/usr/bin/env python3
"""Tests for Ticket 01: Basic Hook Lifecycle & Configuration (agy-switch)."""

import base64
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from agy_switch import (
    AgySwitchEngine,
    main,
)


def _make_jwt(email: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"email": email}).encode("utf-8")).decode("utf-8").rstrip("=")
    return f"header.{payload}.signature"


def load_hook_declarations(
    hooks_path: Path | str | None,
    warning_sink: list[str] | None = None,
) -> dict[str, dict[str, object]]:
    """Loads and validates hook declarations from a hooks.json file.

    Resiliently handles missing, unreadable, corrupt, non-object JSON declarations,
    logging diagnostic warnings and defaulting unparsable hooks to disabled.
    """
    def _warn(msg: str) -> None:
        if warning_sink is not None:
            warning_sink.append(msg)
        sys.stderr.write(f"[agy-switch hook] {msg}\n")
        sys.stderr.flush()

    if hooks_path is None:
        _warn("Warning: Lifecycle hooks path is None; defaulting build-quality-gate to disabled.")
        return {}

    target = Path(hooks_path)
    if not target.exists():
        _warn(f"Warning: Lifecycle hooks file not found at '{target}'; defaulting build-quality-gate to disabled.")
        return {}

    if target.is_dir():
        _warn(f"Warning: Lifecycle hooks path '{target}' is a directory, not a file; defaulting build-quality-gate to disabled.")
        return {}

    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        _warn(f"Warning: Lifecycle hooks file '{target}' is not valid UTF-8 text: {exc}; defaulting build-quality-gate to disabled.")
        return {}
    except OSError as exc:
        _warn(f"Warning: Could not read lifecycle hooks file '{target}': {exc}; defaulting build-quality-gate to disabled.")
        return {}

    trimmed = content.strip()
    if not trimmed:
        _warn(f"Warning: Lifecycle hooks file '{target}' is empty (0 bytes); defaulting build-quality-gate to disabled.")
        return {}

    try:
        raw_data: object = json.loads(trimmed)
    except Exception as exc:
        _warn(f"Warning: Malformed JSON syntax in lifecycle hooks file '{target}': {exc}; defaulting build-quality-gate to disabled.")
        return {}

    if not isinstance(raw_data, dict):
        _warn(
            f"Warning: Malformed hooks file '{target}': root JSON element must be an object, "
            f"got {type(raw_data).__name__}; defaulting build-quality-gate to disabled."
        )
        return {}

    validated_hooks: dict[str, dict[str, object]] = {}
    for hook_name, hook_def in raw_data.items():
        if not isinstance(hook_def, dict):
            _warn(
                f"Warning: Malformed hook declaration '{hook_name}' in '{target}': entry must be an object, "
                f"got {type(hook_def).__name__}; defaulting hook to disabled."
            )
            continue

        entry: dict[str, object] = {str(k): v for k, v in hook_def.items()}
        # For build-quality-gate, ensure enabled is explicitly a boolean
        if hook_name == "build-quality-gate":
            if "enabled" not in entry or not isinstance(entry["enabled"], bool):
                _warn(
                    f"Warning: 'build-quality-gate' in '{target}' lacks boolean 'enabled' attribute; defaulting to disabled."
                )
                entry["enabled"] = False
        validated_hooks[str(hook_name)] = entry

    return validated_hooks


def resolve_effective_hooks(
    plugin_hooks: dict[str, dict[str, object]],
    workspace_hooks: dict[str, dict[str, object]],
) -> dict[str, dict[str, object]]:
    """Resolves effective hooks by layering workspace overrides over plugin-level hooks."""
    effective: dict[str, dict[str, object]] = {}
    for k, v in plugin_hooks.items():
        if isinstance(v, dict):
            effective[k] = dict(v)
    for k, v in workspace_hooks.items():
        if isinstance(v, dict):
            if k in effective:
                merged_entry = dict(effective[k])
                merged_entry.update(v)
                effective[k] = merged_entry
            else:
                effective[k] = dict(v)
    return effective


def get_scheduled_hooks(
    config: dict[str, dict[str, object]],
    event_name: str,
) -> list[dict[str, object]]:
    """Filters hooks scheduled to execute for a specific lifecycle event (e.g., Stop or PreInvocation)."""
    scheduled: list[dict[str, object]] = []
    for hook_id, hook_def in config.items():
        if not isinstance(hook_def, dict):
            continue
        if not hook_def.get("enabled", True):
            continue
        # For build-quality-gate, enabled must be boolean True
        if hook_id == "build-quality-gate" and hook_def.get("enabled") is not True:
            continue
        if event_name in hook_def:
            scheduled.append({"id": hook_id, "definition": hook_def})
    return scheduled


class TestHookLifecycleAndConfig(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)
        self.vault_dir = self.base_path / ".antigravity-vault"
        self.gemini_dir = self.base_path / ".gemini"
        self.legacy_root = self.base_path / ".agy-cli-manager"
        self.proc_dir = self.base_path / "proc"
        self.proc_dir.mkdir(parents=True, exist_ok=True)
        self.engine = AgySwitchEngine(
            vault_dir=self.vault_dir,
            gemini_dir=self.gemini_dir,
            legacy_root=self.legacy_root,
            proc_dir=self.proc_dir,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_config_auto_switch_on_off(self):
        """Engine should get and set auto-switch configuration."""
        self.assertFalse(self.engine.get_auto_switch())

        # Set auto-switch on
        self.engine.set_auto_switch(True)
        self.assertTrue(self.engine.get_auto_switch())
        config_path = self.vault_dir / "config.json"
        self.assertTrue(config_path.is_file())
        self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), 0o600)
        data = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertTrue(data.get("auto_switch"))

        # Set auto-switch off
        self.engine.set_auto_switch(False)
        self.assertFalse(self.engine.get_auto_switch())
        data = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertFalse(data.get("auto_switch"))

    def test_cli_config_auto_switch(self):
        """CLI config auto-switch on/off commands."""
        ret = main(["config", "auto-switch", "on"], engine=self.engine)
        self.assertEqual(ret, 0)
        self.assertTrue(self.engine.get_auto_switch())

        ret = main(["config", "auto-switch", "off"], engine=self.engine)
        self.assertEqual(ret, 0)
        self.assertFalse(self.engine.get_auto_switch())

        ret = main(["config", "auto-switch"], engine=self.engine)
        self.assertEqual(ret, 0)

    def test_hook_install_creates_hooks_json_and_merges(self):
        """hook install should create ~/.gemini/config/hooks.json and merge without clobbering."""
        hooks_file = self.gemini_dir / "config" / "hooks.json"
        hooks_file.parent.mkdir(parents=True, exist_ok=True)
        existing_hooks = {
            "existing-lint": {
                "PostToolUse": [{"type": "command", "command": "./lint.sh"}]
            }
        }
        hooks_file.write_text(json.dumps(existing_hooks), encoding="utf-8")

        res_path = self.engine.install_hook(hooks_file=hooks_file)
        self.assertEqual(res_path, hooks_file)
        self.assertTrue(hooks_file.is_file())

        data = json.loads(hooks_file.read_text(encoding="utf-8"))
        # Preserves existing hooks
        self.assertIn("existing-lint", data)
        self.assertEqual(data["existing-lint"], existing_hooks["existing-lint"])

        # Injects agy-switch-auto-rotate
        self.assertIn("agy-switch-auto-rotate", data)
        hook_obj = data["agy-switch-auto-rotate"]
        self.assertTrue(hook_obj.get("enabled"))
        self.assertIn("PreInvocation", hook_obj)
        pre_inv = hook_obj["PreInvocation"]
        self.assertIsInstance(pre_inv, list)
        self.assertEqual(len(pre_inv), 1)
        self.assertEqual(pre_inv[0]["type"], "command")
        self.assertIn("hook-pre-invocation", pre_inv[0]["command"])
        # Verify script path in command is absolute
        script_path = str(REPO_ROOT / "scripts" / "agy_switch.py")
        self.assertIn(script_path, pre_inv[0]["command"])

    def test_install_hook_prunes_stale_gate(self):
        """[HOOK-CLEAN-001] install_hook deletes build-quality-gate and sets 0o600 permissions."""
        hooks_file = self.gemini_dir / "config" / "hooks.json"
        hooks_file.parent.mkdir(parents=True, exist_ok=True)
        initial_hooks = {
            "agy-switch-auto-rotate": {
                "enabled": True,
                "PreInvocation": [{"type": "command", "command": "old-cmd"}],
            },
            "build-quality-gate": {
                "enabled": True,
                "Stop": [{"type": "command", "command": "./scripts/verify-build.sh"}],
            },
            "custom-hook": {
                "enabled": True,
                "PostToolUse": [{"type": "command", "command": "./lint.sh"}],
            },
        }
        hooks_file.write_text(json.dumps(initial_hooks), encoding="utf-8")

        res_path = self.engine.install_hook(hooks_file=hooks_file)
        self.assertEqual(res_path, hooks_file)
        self.assertTrue(hooks_file.is_file())

        # Check permissions: 0o600
        file_mode = stat.S_IMODE(os.stat(hooks_file).st_mode)
        self.assertEqual(file_mode, 0o600)

        data = json.loads(hooks_file.read_text(encoding="utf-8"))
        # Explicitly deletes build-quality-gate
        self.assertNotIn("build-quality-gate", data)
        # Preserves custom hooks
        self.assertIn("custom-hook", data)
        # Updates agy-switch-auto-rotate with PreInvocation
        self.assertIn("agy-switch-auto-rotate", data)
        hook_obj = data["agy-switch-auto-rotate"]
        self.assertTrue(hook_obj.get("enabled"))
        self.assertIn("PreInvocation", hook_obj)
        self.assertEqual(hook_obj["PreInvocation"][0]["timeout"], 5)

    def test_install_hook_prunes_stale_gate_permission_denied_file(self):
        """[HOOK-CLEAN-ERR-001] Read-only hooks.json reports error to stderr, leaves file unmodified without dangling tmp files."""
        hooks_file = self.gemini_dir / "config" / "hooks.json"
        hooks_file.parent.mkdir(parents=True, exist_ok=True)
        initial_content = json.dumps({
            "build-quality-gate": {
                "enabled": True,
                "Stop": [{"type": "command", "command": "./verify.sh"}],
            }
        })
        hooks_file.write_text(initial_content, encoding="utf-8")
        os.chmod(hooks_file, 0o400)

        import io
        stderr_capture = io.StringIO()
        try:
            with patch("sys.stderr", stderr_capture):
                res_path = self.engine.install_hook(hooks_file=hooks_file)
        finally:
            os.chmod(hooks_file, 0o600)

        err_output = stderr_capture.getvalue()
        self.assertIn("Permission denied", err_output)
        self.assertEqual(hooks_file.read_text(encoding="utf-8"), initial_content)
        tmp_files = list(hooks_file.parent.glob(f".{hooks_file.name}.tmp.*"))
        self.assertEqual(tmp_files, [])

    def test_install_hook_prunes_stale_gate_permission_denied_directory(self):
        """[HOOK-CLEAN-ERR-001] Read-only directory reports error to stderr, leaves file unmodified without dangling tmp files."""
        hooks_dir = self.gemini_dir / "config"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        hooks_file = hooks_dir / "hooks.json"
        initial_content = json.dumps({
            "build-quality-gate": {
                "enabled": True,
                "Stop": [{"type": "command", "command": "./verify.sh"}],
            }
        })
        hooks_file.write_text(initial_content, encoding="utf-8")
        os.chmod(hooks_dir, 0o500)

        import io
        stderr_capture = io.StringIO()
        try:
            with patch("sys.stderr", stderr_capture):
                res_path = self.engine.install_hook(hooks_file=hooks_file)
        finally:
            os.chmod(hooks_dir, 0o700)

        err_output = stderr_capture.getvalue()
        self.assertIn("Permission denied", err_output)
        self.assertEqual(hooks_file.read_text(encoding="utf-8"), initial_content)
        tmp_files = list(hooks_dir.glob(f".{hooks_file.name}.tmp.*"))
        self.assertEqual(tmp_files, [])

    def test_install_sh_prunes_stale_gate(self):
        """[HOOK-CLEAN-EDGE-001] install.sh fallback hook registration removes build-quality-gate while leaving user hooks intact."""
        hooks_dir = self.gemini_dir / "config"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        hooks_file = hooks_dir / "hooks.json"
        initial_hooks = {
            "agy-switch-auto-rotate": {
                "enabled": True,
                "PreInvocation": [{"type": "command", "command": "old-cmd"}],
            },
            "build-quality-gate": {
                "enabled": True,
                "Stop": [{"type": "command", "command": "./scripts/verify-build.sh"}],
            },
            "custom-hook": {
                "enabled": True,
                "PostToolUse": [{"type": "command", "command": "./lint.sh"}],
            },
        }
        hooks_file.write_text(json.dumps(initial_hooks, indent=2), encoding="utf-8")

        env = os.environ.copy()
        env["HOME"] = str(self.base_path)

        res = subprocess.run(
            ["bash", str(REPO_ROOT / "install.sh"), "__register_pre_invocation_hook", str(self.base_path / "nonexistent")],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"Command failed: {res.stderr}\n{res.stdout}")

        file_mode = stat.S_IMODE(os.stat(hooks_file).st_mode)
        self.assertEqual(file_mode, 0o600)

        data = json.loads(hooks_file.read_text(encoding="utf-8"))
        self.assertNotIn("build-quality-gate", data)
        self.assertIn("custom-hook", data)
        self.assertIn("agy-switch-auto-rotate", data)
        self.assertTrue(data["agy-switch-auto-rotate"]["enabled"])
        self.assertEqual(
            data["agy-switch-auto-rotate"]["PreInvocation"][0]["command"],
            "~/.local/bin/agy-switch hook-pre-invocation",
        )

    def test_install_sh_prunes_stale_gate_idempotent_multi_run(self):
        """[HOOK-CLEAN-EDGE-002] Multiple repeated invocations of install.sh fallback remain idempotent without duplicate backups."""
        hooks_dir = self.gemini_dir / "config"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        hooks_file = hooks_dir / "hooks.json"
        initial_hooks = {
            "build-quality-gate": {
                "enabled": True,
                "Stop": [{"type": "command", "command": "./scripts/verify-build.sh"}],
            },
            "custom-hook": {
                "enabled": True,
                "PostToolUse": [{"type": "command", "command": "./lint.sh"}],
            },
        }
        hooks_file.write_text(json.dumps(initial_hooks, indent=2), encoding="utf-8")

        env = os.environ.copy()
        env["HOME"] = str(self.base_path)

        for run_idx in range(3):
            res = subprocess.run(
                ["bash", str(REPO_ROOT / "install.sh"), "__register_pre_invocation_hook", str(self.base_path / "nonexistent")],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 0, f"Run {run_idx + 1} failed: {res.stderr}\n{res.stdout}")

        bak_files = list(hooks_dir.glob("hooks.json.bak.*"))
        self.assertEqual(bak_files, [], f"Expected zero backup files, found: {bak_files}")

        file_mode = stat.S_IMODE(os.stat(hooks_file).st_mode)
        self.assertEqual(file_mode, 0o600)

        data = json.loads(hooks_file.read_text(encoding="utf-8"))
        self.assertNotIn("build-quality-gate", data)
        self.assertIn("custom-hook", data)
        self.assertIn("agy-switch-auto-rotate", data)

    def test_install_sh_prunes_stale_gate_permission_denied_file(self):
        """[HOOK-CLEAN-ERR-001] Read-only hooks.json reports error to stderr, leaves file unmodified without dangling tmp files."""
        hooks_dir = self.gemini_dir / "config"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        hooks_file = hooks_dir / "hooks.json"
        initial_content = json.dumps({
            "build-quality-gate": {
                "enabled": True,
                "Stop": [{"type": "command", "command": "./verify.sh"}],
            }
        })
        hooks_file.write_text(initial_content, encoding="utf-8")
        os.chmod(hooks_file, 0o400)

        env = os.environ.copy()
        env["HOME"] = str(self.base_path)

        try:
            res = subprocess.run(
                ["bash", str(REPO_ROOT / "install.sh"), "__register_pre_invocation_hook", str(self.base_path / "nonexistent")],
                env=env,
                capture_output=True,
                text=True,
            )
        finally:
            os.chmod(hooks_file, 0o600)

        self.assertIn("Permission denied", res.stderr)
        self.assertEqual(hooks_file.read_text(encoding="utf-8"), initial_content)
        tmp_files = list(hooks_dir.glob(f".{hooks_file.name}.tmp.*"))
        self.assertEqual(tmp_files, [])

    def test_install_sh_prunes_stale_gate_permission_denied_directory(self):
        """[HOOK-CLEAN-ERR-001] Read-only directory reports error to stderr, leaves file unmodified without dangling tmp files."""
        hooks_dir = self.gemini_dir / "config"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        hooks_file = hooks_dir / "hooks.json"
        initial_content = json.dumps({
            "build-quality-gate": {
                "enabled": True,
                "Stop": [{"type": "command", "command": "./verify.sh"}],
            }
        })
        hooks_file.write_text(initial_content, encoding="utf-8")
        os.chmod(hooks_dir, 0o500)

        env = os.environ.copy()
        env["HOME"] = str(self.base_path)

        try:
            res = subprocess.run(
                ["bash", str(REPO_ROOT / "install.sh"), "__register_pre_invocation_hook", str(self.base_path / "nonexistent")],
                env=env,
                capture_output=True,
                text=True,
            )
        finally:
            os.chmod(hooks_dir, 0o700)

        self.assertIn("Permission denied", res.stderr)
        self.assertEqual(hooks_file.read_text(encoding="utf-8"), initial_content)
        tmp_files = list(hooks_dir.glob(f".{hooks_file.name}.tmp.*"))
        self.assertEqual(tmp_files, [])

    def test_install_sh_prunes_stale_gate_global_install(self):
        """[HOOK-CLEAN-EDGE-001] install.sh --global prunes build-quality-gate and preserves custom hooks."""
        hooks_dir = self.gemini_dir / "config"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        hooks_file = hooks_dir / "hooks.json"
        initial_hooks = {
            "build-quality-gate": {
                "enabled": True,
                "Stop": [{"type": "command", "command": "./scripts/verify-build.sh"}],
            },
            "custom-hook": {
                "enabled": True,
                "PostToolUse": [{"type": "command", "command": "./lint.sh"}],
            },
        }
        hooks_file.write_text(json.dumps(initial_hooks, indent=2), encoding="utf-8")

        env = os.environ.copy()
        env["HOME"] = str(self.base_path)

        res = subprocess.run(
            ["bash", str(REPO_ROOT / "install.sh"), "--global"],
            env=env,
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        self.assertEqual(res.returncode, 0, f"install.sh --global failed: {res.stderr}\n{res.stdout}")

        data = json.loads(hooks_file.read_text(encoding="utf-8"))
        self.assertNotIn("build-quality-gate", data)
        self.assertIn("custom-hook", data)
        self.assertIn("agy-switch-auto-rotate", data)

    def test_cli_hook_install(self):
        """CLI command 'hook install' should register hook."""
        ret = main(["hook", "install"], engine=self.engine)
        self.assertEqual(ret, 0)
        hooks_file = self.gemini_dir / "config" / "hooks.json"
        self.assertTrue(hooks_file.is_file())
        data = json.loads(hooks_file.read_text(encoding="utf-8"))
        self.assertIn("agy-switch-auto-rotate", data)

    def test_hook_pre_invocation_clean_stdout(self):
        """hook-pre-invocation should output strictly valid JSON on stdout."""
        script_path = REPO_ROOT / "scripts" / "agy_switch.py"
        sample_input = json.dumps({
            "conversationId": "test-conv-123",
            "modelName": "gemini-3.8-pro",
            "invocationNum": 1,
            "workspacePaths": ["/srv/agy-workspaces/booster/agy-switcher"]
        })

        proc = subprocess.run(
            [sys.executable, str(script_path), "hook-pre-invocation"],
            input=sample_input,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0)
        # Parse stdout as JSON
        try:
            out_data = json.loads(proc.stdout)
            self.assertIsInstance(out_data, dict)
        except json.JSONDecodeError as e:
            self.fail(f"stdout is not valid JSON: {proc.stdout!r}, error: {e}")

    def test_hook_pre_invocation_handles_empty_or_invalid_stdin(self):
        """hook-pre-invocation must not crash on empty or non-JSON stdin and return valid JSON."""
        script_path = REPO_ROOT / "scripts" / "agy_switch.py"

        for bad_input in ["", "not-a-json", "{broken"]:
            proc = subprocess.run(
                [sys.executable, str(script_path), "hook-pre-invocation"],
                input=bad_input,
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0)
            try:
                out_data = json.loads(proc.stdout)
                self.assertIsInstance(out_data, dict)
            except json.JSONDecodeError as e:
                self.fail(f"stdout is not valid JSON for input {bad_input!r}: {proc.stdout!r}, error: {e}")

    def test_hook_pre_invocation_empty_quotas_proceeds_silently(self):
        """When auto-switch is on and all network quotas fail (empty quotas/network outage), hook must proceed silently with {} without emitting false alarm."""
        from agy_switch import handle_pre_invocation_hook
        import io

        self.engine.set_auto_switch(True)
        # Register an account
        acc_dir = self.vault_dir / "accounts" / "acc1"
        acc_dir.mkdir(parents=True, exist_ok=True)
        (acc_dir / "token.json").write_text(json.dumps({
            "token": {"access_token": "token1", "refresh_token": "r1", "token_type": "Bearer", "expiry": "2026-10-05T12:00:00Z"},
            "auth_method": "oauth",
            "id_token": "header.eyJlbWFpbCI6ICJhY2MxQGV4YW1wbGUuY29tIn0.signature",
        }))
        self.engine.set_active_name("acc1")

        input_payload = json.dumps({"modelName": "gemini-3.8-pro"})

        # Mock QuotaClient.fetch_quota to return empty dict (simulating network failure)
        with patch("agy_switch.QuotaClient.fetch_quota", return_value={}):
            with patch("sys.stdin", io.StringIO(input_payload)):
                out_capture = io.StringIO()
                with patch("sys.stdout", out_capture):
                    ret = handle_pre_invocation_hook(self.engine)

        self.assertEqual(ret, 0)
        output = json.loads(out_capture.getvalue())
        self.assertEqual(output, {}, "Hook must proceed silently with {} when network quotas fail without valid cache")
        self.assertEqual(self.engine.get_active_name(), "acc1")

    def test_hook_pre_invocation_all_quotas_zero_warns_exhausted(self):
        """When auto-switch is on and all accounts have 0 quota, output exhausted warning ephemeralMessage."""
        from agy_switch import handle_pre_invocation_hook
        import io

        self.engine.set_auto_switch(True)
        acc_dir = self.vault_dir / "accounts" / "acc1"
        acc_dir.mkdir(parents=True, exist_ok=True)
        (acc_dir / "token.json").write_text(json.dumps({
            "token": {"access_token": "token1", "refresh_token": "r1", "token_type": "Bearer", "expiry": "2026-10-05T12:00:00Z"},
            "auth_method": "oauth",
            "id_token": "header.eyJlbWFpbCI6ICJhY2MxQGV4YW1wbGUuY29tIn0.signature",
        }))
        self.engine.set_active_name("acc1")

        input_payload = json.dumps({"modelName": "gemini-3.8-pro"})
        zero_quota = {
            "groups": [
                {
                    "group": "Gemini Models",
                    "buckets": [{"bucketId": "five_hour", "remainingFraction": 0.0, "resetTime": "2026-10-05T15:00:00Z"}]
                }
            ]
        }

        with patch("agy_switch.QuotaClient.fetch_quota", return_value=zero_quota):
            with patch("sys.stdin", io.StringIO(input_payload)):
                out_capture = io.StringIO()
                with patch("sys.stdout", out_capture):
                    ret = handle_pre_invocation_hook(self.engine)

        self.assertEqual(ret, 0)
        output = json.loads(out_capture.getvalue())
        self.assertIn("injectSteps", output)
        step = output["injectSteps"][0]
        self.assertIn("ephemeralMessage", step)
        self.assertNotIn("type", step)
        self.assertNotIn("message", step)
        self.assertIn("exhausted their gemini-3.8-pro quota", step["ephemeralMessage"])

    def test_hook_pre_invocation_uses_get_account_tokens(self):
        """Hook must call engine.get_account_tokens instead of directly parsing token dicts."""
        from agy_switch import handle_pre_invocation_hook
        import io

        self.engine.set_auto_switch(True)
        acc_dir = self.vault_dir / "accounts" / "acc1"
        acc_dir.mkdir(parents=True, exist_ok=True)
        (acc_dir / "token.json").write_text(json.dumps({
            "token": {"access_token": "token1", "refresh_token": "r1", "token_type": "Bearer", "expiry": "2026-10-05T12:00:00Z"},
            "auth_method": "oauth",
            "id_token": "header.eyJlbWFpbCI6ICJhY2MxQGV4YW1wbGUuY29tIn0.signature",
        }))
        self.engine.set_active_name("acc1")

        input_payload = json.dumps({"modelName": "auto"})

        with patch.object(self.engine, "get_account_tokens", wraps=self.engine.get_account_tokens) as spy_tokens:
            with patch("agy_switch.QuotaClient.fetch_quota", return_value={}):
                with patch("sys.stdin", io.StringIO(input_payload)):
                    out_capture = io.StringIO()
                    with patch("sys.stdout", out_capture):
                        handle_pre_invocation_hook(self.engine)

        self.assertTrue(spy_tokens.called, "Hook must invoke engine.get_account_tokens()")

    def test_hook_pre_invocation_uses_semantic_quota_methods(self):
        """Hook must use semantic methods on RotationEngine/QuotaStatus rather than raw fraction access."""
        from agy_switch import handle_pre_invocation_hook
        from agy_switch import RotationEngine
        import io

        self.engine.set_auto_switch(True)
        acc_dir = self.vault_dir / "accounts" / "acc1"
        acc_dir.mkdir(parents=True, exist_ok=True)
        (acc_dir / "token.json").write_text(json.dumps({
            "token": {"access_token": "token1", "refresh_token": "r1", "token_type": "Bearer", "expiry": "2026-10-05T12:00:00Z"},
            "auth_method": "oauth",
            "id_token": "header.eyJlbWFpbCI6ICJhY2MxQGV4YW1wbGUuY29tIn0.signature",
        }))
        self.engine.set_active_name("acc1")

        input_payload = json.dumps({"modelName": "gemini-3.8-pro"})
        dummy_quota = {
            "groups": [
                {
                    "group": "Gemini Models",
                    "buckets": [{"bucketId": "five_hour", "remainingFraction": 0.8, "resetTime": "2026-10-05T15:00:00Z"}]
                }
            ]
        }

        with patch("agy_switch.QuotaClient.fetch_quota", return_value=dummy_quota):
            with patch.object(RotationEngine, "has_available_quota", return_value=True) as spy_has_quota:
                with patch("sys.stdin", io.StringIO(input_payload)):
                    out_capture = io.StringIO()
                    with patch("sys.stdout", out_capture):
                        handle_pre_invocation_hook(self.engine)

        self.assertTrue(spy_has_quota.called, "Hook must invoke rot_engine.has_available_quota()")

    def test_hook_pre_invocation_auto_rotate_ephemeral_schema(self):
        """When auto-rotation succeeds, hook output must use {"ephemeralMessage": "..."} without type/message keys."""
        from agy_switch import handle_pre_invocation_hook
        import io

        self.engine.set_auto_switch(True)
        # Register acc1 (active, low quota) and acc2 (standby, high quota)
        for acc_name in ["acc1", "acc2"]:
            acc_dir = self.vault_dir / "accounts" / acc_name
            acc_dir.mkdir(parents=True, exist_ok=True)
            (acc_dir / "token.json").write_text(json.dumps({
                "token": {"access_token": f"tok_{acc_name}", "refresh_token": f"ref_{acc_name}", "token_type": "Bearer", "expiry": "2026-10-05T12:00:00Z"},
                "auth_method": "oauth",
                "id_token": _make_jwt(f"{acc_name}@example.com"),
            }))
        self.engine.set_active_name("acc1")

        input_payload = json.dumps({"modelName": "gemini-3.8-pro"})

        mock_quotas = {
            "tok_acc1": {"gemini": {"quota": 5, "reset_time": 1700000000}},
            "tok_acc2": {"gemini": {"quota": 90, "reset_time": 1700000000}},
        }

        with patch("agy_switch.QuotaClient.fetch_quota", side_effect=lambda tok, refresh_token=None: mock_quotas.get(tok, {})):
            with patch("sys.stdin", io.StringIO(input_payload)):
                out_capture = io.StringIO()
                with patch("sys.stdout", out_capture):
                    ret = handle_pre_invocation_hook(self.engine)

        self.assertEqual(ret, 0)
        self.assertEqual(self.engine.get_active_name(), "acc2")
        output = json.loads(out_capture.getvalue())
        self.assertIn("injectSteps", output)
        self.assertEqual(len(output["injectSteps"]), 1)
        step = output["injectSteps"][0]
        self.assertIn("ephemeralMessage", step)
        self.assertNotIn("type", step)
        self.assertNotIn("message", step)
        self.assertIn("Auto-rotated to **acc2**", step["ephemeralMessage"])
        self.assertIn("90% remaining", step["ephemeralMessage"])

    def test_hook_pre_invocation_all_exhausted_only_when_all_accounts_zero(self):
        """all_exhausted should not trigger just because active account is at 0% when standby has quota."""
        from agy_switch import handle_pre_invocation_hook
        import io

        self.engine.set_auto_switch(True)
        # acc1 has 0% quota, acc2 has 85% quota
        for acc_name in ["acc1", "acc2"]:
            acc_dir = self.vault_dir / "accounts" / acc_name
            acc_dir.mkdir(parents=True, exist_ok=True)
            (acc_dir / "token.json").write_text(json.dumps({
                "token": {"access_token": f"tok_{acc_name}", "refresh_token": f"ref_{acc_name}", "token_type": "Bearer", "expiry": "2026-10-05T12:00:00Z"},
                "auth_method": "oauth",
                "id_token": _make_jwt(f"{acc_name}@example.com"),
            }))
        self.engine.set_active_name("acc1")

        input_payload = json.dumps({"modelName": "gemini-3.8-pro"})

        mock_quotas = {
            "tok_acc1": {"gemini": {"quota": 0, "reset_time": 1700000000}},
            "tok_acc2": {"gemini": {"quota": 85, "reset_time": 1700000000}},
        }

        with patch("agy_switch.QuotaClient.fetch_quota", side_effect=lambda tok, refresh_token=None: mock_quotas.get(tok, {})):
            with patch("sys.stdin", io.StringIO(input_payload)):
                out_capture = io.StringIO()
                with patch("sys.stdout", out_capture):
                    ret = handle_pre_invocation_hook(self.engine)

        self.assertEqual(ret, 0)
        self.assertEqual(self.engine.get_active_name(), "acc2")
        output = json.loads(out_capture.getvalue())
        self.assertIn("injectSteps", output)
        step = output["injectSteps"][0]
        self.assertIn("ephemeralMessage", step)
        self.assertNotIn("type", step)
        self.assertNotIn("message", step)
        self.assertIn("Auto-rotated to **acc2**", step["ephemeralMessage"])
        self.assertNotIn("All Antigravity accounts have exhausted", step["ephemeralMessage"])

    def test_hook_pre_invocation_model_name_auto_with_zero_claude(self):
        """modelName: 'auto' gracefully defaults to evaluating Gemini and does not crash when Claude is 0%."""
        from agy_switch import handle_pre_invocation_hook
        import io

        self.engine.set_auto_switch(True)
        acc_dir = self.vault_dir / "accounts" / "acc1"
        acc_dir.mkdir(parents=True, exist_ok=True)
        (acc_dir / "token.json").write_text(json.dumps({
            "token": {"access_token": "tok1", "refresh_token": "ref1", "token_type": "Bearer", "expiry": "2026-10-05T12:00:00Z"},
            "auth_method": "oauth",
            "id_token": "header.eyJlbWFpbCI6ICJhY2MxQGV4YW1wbGUuY29tIn0.sig",
        }))
        self.engine.set_active_name("acc1")

        input_payload = json.dumps({"modelName": "auto"})

        mock_quota = {
            "gemini": {"quota": 80, "reset_time": 1700000000},
            "claude": {"quota": 0, "reset_time": 1700000000},
        }

        with patch("agy_switch.QuotaClient.fetch_quota", return_value=mock_quota):
            with patch("sys.stdin", io.StringIO(input_payload)):
                out_capture = io.StringIO()
                with patch("sys.stdout", out_capture):
                    ret = handle_pre_invocation_hook(self.engine)

        self.assertEqual(ret, 0)
        output = json.loads(out_capture.getvalue())
        # acc1 has 80% Gemini quota (>15%), so it should retain acc1 and return empty {}
        self.assertEqual(output, {})
        self.assertEqual(self.engine.get_active_name(), "acc1")

    def test_hook_pre_invocation_model_name_auto_rotates_when_gemini_low(self):
        """modelName: 'auto' auto-rotates when Gemini is low, even when Claude quota is 0% across accounts."""
        from agy_switch import handle_pre_invocation_hook
        import io

        self.engine.set_auto_switch(True)
        for acc_name in ["acc1", "acc2"]:
            acc_dir = self.vault_dir / "accounts" / acc_name
            acc_dir.mkdir(parents=True, exist_ok=True)
            (acc_dir / "token.json").write_text(json.dumps({
                "token": {"access_token": f"tok_{acc_name}", "refresh_token": f"ref_{acc_name}", "token_type": "Bearer", "expiry": "2026-10-05T12:00:00Z"},
                "auth_method": "oauth",
                "id_token": _make_jwt(f"{acc_name}@example.com"),
            }))
        self.engine.set_active_name("acc1")

        input_payload = json.dumps({"modelName": "auto"})

        mock_quotas = {
            "tok_acc1": {"gemini": {"quota": 5, "reset_time": 1700000000}, "claude": {"quota": 0, "reset_time": 1700000000}},
            "tok_acc2": {"gemini": {"quota": 90, "reset_time": 1700000000}, "claude": {"quota": 0, "reset_time": 1700000000}},
        }

        with patch("agy_switch.QuotaClient.fetch_quota", side_effect=lambda tok, refresh_token=None: mock_quotas.get(tok, {})):
            with patch("sys.stdin", io.StringIO(input_payload)):
                out_capture = io.StringIO()
                with patch("sys.stdout", out_capture):
                    ret = handle_pre_invocation_hook(self.engine)

        self.assertEqual(ret, 0)
        self.assertEqual(self.engine.get_active_name(), "acc2")
        output = json.loads(out_capture.getvalue())
        self.assertIn("injectSteps", output)
        step = output["injectSteps"][0]
        self.assertIn("ephemeralMessage", step)
        self.assertNotIn("type", step)
        self.assertNotIn("message", step)
        self.assertIn("Auto-rotated to **acc2**", step["ephemeralMessage"])

    def test_tier_1_healthy_cache_bypasses_with_zero_network_and_disk_io(self):
        """Tier 1 bypasses execution with 0 network calls and 0 disk writes when active account has cached quota >= 25% and cached_at within 300s."""
        from agy_switch import handle_pre_invocation_hook, QuotaClient
        import io

        self.engine.set_auto_switch(True)
        acc_dir = self.vault_dir / "accounts" / "acc1"
        acc_dir.mkdir(parents=True, exist_ok=True)
        (acc_dir / "token.json").write_text(json.dumps({
            "token": {"access_token": "token1", "refresh_token": "r1", "token_type": "Bearer", "expiry": "2026-10-05T12:00:00Z"},
            "auth_method": "oauth",
            "id_token": "header.eyJlbWFpbCI6ICJhY2MxQGV4YW1wbGUuY29tIn0.sig",
        }))
        self.engine.set_active_name("acc1")

        # Seed cache with fresh quota (cached_at = 1000.0, quota = 80%)
        cache_dir = self.vault_dir / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / "last_known_quota.json"
        cache_file.write_text(json.dumps({
            "timestamp": 1000.0,
            "accounts": {
                "acc1": {
                    "cached_at": 1000.0,
                    "gemini": {"quota": 80, "reset_time": 1700000000},
                }
            }
        }), encoding="utf-8")

        input_payload = json.dumps({"modelName": "gemini-3.8-pro"})

        # Virtual clock at 1100.0 (delta = 100s <= 300s)
        with patch("time.time", return_value=1100.0):
            with patch("agy_switch.fetch_account_quota") as mock_fetch:
                with patch.object(QuotaClient, "save_cache") as mock_save:
                    with patch("sys.stdin", io.StringIO(input_payload)):
                        out_capture = io.StringIO()
                        with patch("sys.stdout", out_capture):
                            ret = handle_pre_invocation_hook(self.engine)

        self.assertEqual(ret, 0)
        self.assertEqual(json.loads(out_capture.getvalue()), {})
        # Strictly zero network calls and zero disk writes
        mock_fetch.assert_not_called()
        mock_save.assert_not_called()

    def test_tier_1_model_name_mapping(self):
        """Stdin payload modelName correctly maps to 'gemini' or 'claude' when looking up quota cache."""
        from agy_switch import handle_pre_invocation_hook
        import io

        self.engine.set_auto_switch(True)
        acc_dir = self.vault_dir / "accounts" / "acc1"
        acc_dir.mkdir(parents=True, exist_ok=True)
        (acc_dir / "token.json").write_text(json.dumps({
            "token": {"access_token": "token1", "refresh_token": "r1", "token_type": "Bearer", "expiry": "2026-10-05T12:00:00Z"},
            "auth_method": "oauth",
            "id_token": "header.eyJlbWFpbCI6ICJhY2MxQGV4YW1wbGUuY29tIn0.sig",
        }))
        self.engine.set_active_name("acc1")

        # Cache where Gemini is healthy (80%) but Claude is low (10%)
        cache_dir = self.vault_dir / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "last_known_quota.json").write_text(json.dumps({
            "timestamp": 1000.0,
            "accounts": {
                "acc1": {
                    "cached_at": 1000.0,
                    "gemini": {"quota": 80, "reset_time": 1700000000},
                    "claude": {"quota": 10, "reset_time": 1700000000},
                }
            }
        }), encoding="utf-8")

        # 1. gemini-3.8-pro -> maps to gemini (80% >= 25%) -> Tier 1 bypasses (0 network calls)
        with patch("time.time", return_value=1100.0):
            with patch("agy_switch.fetch_account_quota") as mock_fetch:
                with patch("sys.stdin", io.StringIO(json.dumps({"modelName": "gemini-3.8-pro"}))):
                    out_capture = io.StringIO()
                    with patch("sys.stdout", out_capture):
                        ret = handle_pre_invocation_hook(self.engine)
                self.assertEqual(ret, 0)
                self.assertEqual(json.loads(out_capture.getvalue()), {})
                mock_fetch.assert_not_called()

        # 2. auto -> maps to gemini (80% >= 25%) -> Tier 1 bypasses (0 network calls)
        with patch("time.time", return_value=1100.0):
            with patch("agy_switch.fetch_account_quota") as mock_fetch:
                with patch("sys.stdin", io.StringIO(json.dumps({"modelName": "auto"}))):
                    out_capture = io.StringIO()
                    with patch("sys.stdout", out_capture):
                        ret = handle_pre_invocation_hook(self.engine)
                self.assertEqual(ret, 0)
                self.assertEqual(json.loads(out_capture.getvalue()), {})
                mock_fetch.assert_not_called()

        # 3. claude-3-5-sonnet -> maps to claude (10% < 25%) -> Tier 1 does NOT bypass (probes active account)
        with patch("time.time", return_value=1100.0):
            with patch("agy_switch.fetch_account_quota", return_value=("acc1", {"claude": {"quota": 85}})) as mock_fetch:
                with patch("sys.stdin", io.StringIO(json.dumps({"modelName": "claude-3-5-sonnet"}))):
                    out_capture = io.StringIO()
                    with patch("sys.stdout", out_capture):
                        ret = handle_pre_invocation_hook(self.engine)
                self.assertEqual(ret, 0)
                mock_fetch.assert_called_once()

    def test_tier_2_single_account_probe_when_cache_expired(self):
        """Cache expiration (> 300s) triggers Tier 2 single probe and updates cache with fresh cached_at; standby accounts not probed."""
        from agy_switch import handle_pre_invocation_hook
        import io

        self.engine.set_auto_switch(True)
        for acc_name in ["acc1", "acc2"]:
            acc_dir = self.vault_dir / "accounts" / acc_name
            acc_dir.mkdir(parents=True, exist_ok=True)
            (acc_dir / "token.json").write_text(json.dumps({
                "token": {"access_token": f"tok_{acc_name}", "refresh_token": f"ref_{acc_name}", "token_type": "Bearer", "expiry": "2026-10-05T12:00:00Z"},
                "auth_method": "oauth",
                "id_token": _make_jwt(f"{acc_name}@example.com"),
            }))
        self.engine.set_active_name("acc1")

        # Cache created at 1000.0
        cache_dir = self.vault_dir / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / "last_known_quota.json"
        cache_file.write_text(json.dumps({
            "timestamp": 1000.0,
            "accounts": {
                "acc1": {
                    "cached_at": 1000.0,
                    "gemini": {"quota": 80, "reset_time": 1700000000},
                },
                "acc2": {
                    "cached_at": 1000.0,
                    "gemini": {"quota": 90, "reset_time": 1700000000},
                }
            }
        }), encoding="utf-8")

        # Virtual time at 1350.0 (delta = 350s > 300s TTL)
        fresh_quota = {"gemini": {"quota": 85, "reset_time": 1700000000}}
        with patch("time.time", return_value=1350.0):
            with patch("agy_switch.fetch_account_quota", return_value=("acc1", fresh_quota)) as mock_fetch:
                with patch("sys.stdin", io.StringIO(json.dumps({"modelName": "gemini-3.8-pro"}))):
                    out_capture = io.StringIO()
                    with patch("sys.stdout", out_capture):
                        ret = handle_pre_invocation_hook(self.engine)

        self.assertEqual(ret, 0)
        self.assertEqual(json.loads(out_capture.getvalue()), {})
        # Tier 2 probes ONLY active account 'acc1'; zero calls to standby 'acc2'
        self.assertEqual(mock_fetch.call_count, 1)
        self.assertEqual(mock_fetch.call_args[0][1], "acc1")

        # Verify cache persisted with fresh cached_at = 1350.0 and merged standby preserved
        saved_cache = json.loads(cache_file.read_text(encoding="utf-8"))
        self.assertEqual(saved_cache["accounts"]["acc1"]["cached_at"], 1350.0)
        self.assertEqual(saved_cache["accounts"]["acc1"]["gemini"]["quota"], 85)
        self.assertIn("acc2", saved_cache["accounts"])

    def test_tier_2_single_account_probe_when_quota_intermediate(self):
        """Intermediate quota (threshold <= quota < tier1_threshold) triggers Tier 2 single probe even if cache is fresh."""
        from agy_switch import handle_pre_invocation_hook
        import io

        self.engine.set_auto_switch(True)
        for acc_name in ["acc1", "acc2"]:
            acc_dir = self.vault_dir / "accounts" / acc_name
            acc_dir.mkdir(parents=True, exist_ok=True)
            (acc_dir / "token.json").write_text(json.dumps({
                "token": {"access_token": f"tok_{acc_name}", "refresh_token": f"ref_{acc_name}", "token_type": "Bearer", "expiry": "2026-10-05T12:00:00Z"},
                "auth_method": "oauth",
                "id_token": _make_jwt(f"{acc_name}@example.com"),
            }))
        self.engine.set_active_name("acc1")

        # Cache created at 1000.0 with 30% quota (intermediate: < 35% tier1 threshold)
        cache_dir = self.vault_dir / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "last_known_quota.json").write_text(json.dumps({
            "timestamp": 1000.0,
            "accounts": {
                "acc1": {
                    "cached_at": 1000.0,
                    "gemini": {"quota": 30, "reset_time": 1700000000},
                },
                "acc2": {
                    "cached_at": 1000.0,
                    "gemini": {"quota": 90, "reset_time": 1700000000},
                }
            }
        }), encoding="utf-8")

        # Virtual clock at 1050.0 (delta = 50s <= 300s, but quota 30% is intermediate)
        fresh_quota = {"gemini": {"quota": 28, "reset_time": 1700000000}}
        with patch("time.time", return_value=1050.0):
            with patch("agy_switch.fetch_account_quota", return_value=("acc1", fresh_quota)) as mock_fetch:
                with patch("sys.stdin", io.StringIO(json.dumps({"modelName": "gemini-3.8-pro"}))):
                    out_capture = io.StringIO()
                    with patch("sys.stdout", out_capture):
                        ret = handle_pre_invocation_hook(self.engine)

        self.assertEqual(ret, 0)
        self.assertEqual(json.loads(out_capture.getvalue()), {})
        # Only active account probed
        mock_fetch.assert_called_once()
        self.assertEqual(mock_fetch.call_args[0][1], "acc1")
        self.assertEqual(self.engine.get_active_name(), "acc1")

    def test_tier_2_probe_auth_corruption_skips_cache_and_fails_over(self):
        """Auth corruption on active account skips cache fallback and triggers failover to healthy standby."""
        from agy_switch import handle_pre_invocation_hook
        import io

        self.engine.set_auto_switch(True)
        for acc_name in ["acc1", "acc2"]:
            acc_dir = self.vault_dir / "accounts" / acc_name
            acc_dir.mkdir(parents=True, exist_ok=True)
            (acc_dir / "token.json").write_text(json.dumps({
                "token": {"access_token": f"tok_{acc_name}", "refresh_token": f"ref_{acc_name}", "token_type": "Bearer", "expiry": "2026-10-05T12:00:00Z"},
                "auth_method": "oauth",
                "id_token": _make_jwt(f"{acc_name}@example.com"),
            }))
        self.engine.set_active_name("acc1")

        # acc1 has high cached quota (90%), but expired cache (delta > 300s)
        cache_dir = self.vault_dir / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "last_known_quota.json").write_text(json.dumps({
            "timestamp": 1000.0,
            "accounts": {
                "acc1": {
                    "cached_at": 1000.0,
                    "gemini": {"quota": 90, "reset_time": 1700000000},
                },
            }
        }), encoding="utf-8")

        mock_quotas = {
            "tok_acc1": {},  # token revoked / invalid
            "tok_acc2": {"gemini": {"quota": 80, "reset_time": 1700000000}},
        }

        # Mock fetch_quota returning HTTPError 401 Unauthorized for acc1 (revoked token)
        def fake_fetch_quota(tok, refresh_token=None, on_refresh=None):
            if tok == "tok_acc1":
                raise urllib.error.HTTPError("https://cloudcode-pa.googleapis.com", 401, "Unauthorized", {}, io.BytesIO(b""))
            return mock_quotas.get(tok, {})

        with patch("time.time", return_value=1350.0):
            with patch("agy_switch.QuotaClient.fetch_quota", side_effect=fake_fetch_quota):
                with patch("sys.stdin", io.StringIO(json.dumps({"modelName": "gemini-3.8-pro"}))):
                    out_capture = io.StringIO()
                    with patch("sys.stdout", out_capture):
                        ret = handle_pre_invocation_hook(self.engine)

        self.assertEqual(ret, 0)
        # Auth failure on acc1 must NOT fall back to 90% cache; must rotate to acc2
        self.assertEqual(self.engine.get_active_name(), "acc2")
        output = json.loads(out_capture.getvalue())
        self.assertIn("injectSteps", output)
        self.assertIn("Auto-rotated to **acc2**", output["injectSteps"][0]["ephemeralMessage"])

    def test_tier_2_probe_network_timeout_with_cached_quota_retains_active_silently(self):
        """Network timeout with cached quota >= 15% retains active account silently without emitting false alarms."""
        from agy_switch import handle_pre_invocation_hook
        import io

        self.engine.set_auto_switch(True)
        acc_dir = self.vault_dir / "accounts" / "acc1"
        acc_dir.mkdir(parents=True, exist_ok=True)
        (acc_dir / "token.json").write_text(json.dumps({
            "token": {"access_token": "tok1", "refresh_token": "ref1", "token_type": "Bearer", "expiry": "2026-10-05T12:00:00Z"},
            "auth_method": "oauth",
            "id_token": "header.eyJlbWFpbCI6ICJhY2MxQGV4YW1wbGUuY29tIn0.sig",
        }))
        self.engine.set_active_name("acc1")

        # Cache with 50% quota
        cache_dir = self.vault_dir / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "last_known_quota.json").write_text(json.dumps({
            "timestamp": 1000.0,
            "accounts": {
                "acc1": {
                    "cached_at": 1000.0,
                    "gemini": {"quota": 50, "reset_time": 1700000000},
                }
            }
        }), encoding="utf-8")

        # Virtual clock at 1400.0 (cache expired). Probe fails with TimeoutError.
        with patch("time.time", return_value=1400.0):
            with patch("agy_switch.QuotaClient.fetch_quota", side_effect=TimeoutError("Timed out")):
                with patch("sys.stdin", io.StringIO(json.dumps({"modelName": "gemini-3.8-pro"}))):
                    out_capture = io.StringIO()
                    with patch("sys.stdout", out_capture):
                        ret = handle_pre_invocation_hook(self.engine)

        self.assertEqual(ret, 0)
        # Retained active account silently: output is {} and no false alarm warning emitted
        output = json.loads(out_capture.getvalue())
        self.assertEqual(output, {})
        self.assertEqual(self.engine.get_active_name(), "acc1")

    def test_tier_2_probe_quota_drops_below_15_triggers_failover(self):
        """When Tier 2 single probe reveals active quota < 15%, hook proceeds to Tier 3 failover."""
        from agy_switch import handle_pre_invocation_hook
        import io

        self.engine.set_auto_switch(True)
        for acc_name in ["acc1", "acc2"]:
            acc_dir = self.vault_dir / "accounts" / acc_name
            acc_dir.mkdir(parents=True, exist_ok=True)
            (acc_dir / "token.json").write_text(json.dumps({
                "token": {"access_token": f"tok_{acc_name}", "refresh_token": f"ref_{acc_name}", "token_type": "Bearer", "expiry": "2026-10-05T12:00:00Z"},
                "auth_method": "oauth",
                "id_token": _make_jwt(f"{acc_name}@example.com"),
            }))
        self.engine.set_active_name("acc1")

        # Cache expired (delta > 300s)
        cache_dir = self.vault_dir / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "last_known_quota.json").write_text(json.dumps({
            "timestamp": 1000.0,
            "accounts": {
                "acc1": {
                    "cached_at": 1000.0,
                    "gemini": {"quota": 80, "reset_time": 1700000000},
                },
                "acc2": {
                    "cached_at": 1000.0,
                    "gemini": {"quota": 90, "reset_time": 1700000000},
                }
            }
        }), encoding="utf-8")

        mock_quotas = {
            "tok_acc1": {"gemini": {"quota": 10, "reset_time": 1700000000}},
            "tok_acc2": {"gemini": {"quota": 90, "reset_time": 1700000000}},
        }

        with patch("time.time", return_value=1350.0):
            with patch("agy_switch.QuotaClient.fetch_quota", side_effect=lambda tok, refresh_token=None: mock_quotas.get(tok, {})):
                with patch("sys.stdin", io.StringIO(json.dumps({"modelName": "gemini-3.8-pro"}))):
                    out_capture = io.StringIO()
                    with patch("sys.stdout", out_capture):
                        ret = handle_pre_invocation_hook(self.engine)

        self.assertEqual(ret, 0)
        self.assertEqual(self.engine.get_active_name(), "acc2")
        output = json.loads(out_capture.getvalue())
        self.assertIn("injectSteps", output)
        self.assertIn("Auto-rotated to **acc2**", output["injectSteps"][0]["ephemeralMessage"])

    def test_total_network_failure_no_cache_proceeds_silently(self):
        """When network probe fails and no cache exists in vault, hook proceeds silently with {} without false alarm."""
        from agy_switch import handle_pre_invocation_hook
        import io

        self.engine.set_auto_switch(True)
        acc_dir = self.vault_dir / "accounts" / "acc1"
        acc_dir.mkdir(parents=True, exist_ok=True)
        (acc_dir / "token.json").write_text(json.dumps({
            "token": {"access_token": "tok1", "refresh_token": "ref1", "token_type": "Bearer", "expiry": "2026-10-05T12:00:00Z"},
            "auth_method": "oauth",
            "id_token": "header.eyJlbWFpbCI6ICJhY2MxQGV4YW1wbGUuY29tIn0.sig",
        }))
        self.engine.set_active_name("acc1")

        # No cache file exists
        with patch("time.time", return_value=1000.0):
            with patch("agy_switch.QuotaClient.fetch_quota", side_effect=TimeoutError("Network down")):
                with patch("sys.stdin", io.StringIO(json.dumps({"modelName": "gemini-3.8-pro"}))):
                    out_capture = io.StringIO()
                    with patch("sys.stdout", out_capture):
                        ret = handle_pre_invocation_hook(self.engine)

        self.assertEqual(ret, 0)
        output = json.loads(out_capture.getvalue())
        self.assertEqual(output, {}, "Must output empty {} without injecting false alarm")
        self.assertEqual(self.engine.get_active_name(), "acc1")

    def test_partial_standby_unreachable_does_not_warn_exhausted(self):
        """Acc 1 is 0%, Acc 2 probe times out (no cache) -> Hook does NOT emit all-exhausted warning; outputs {} and remains on acc1."""
        from agy_switch import handle_pre_invocation_hook
        import io

        self.engine.set_auto_switch(True)
        for acc_name in ["acc1", "acc2"]:
            acc_dir = self.vault_dir / "accounts" / acc_name
            acc_dir.mkdir(parents=True, exist_ok=True)
            (acc_dir / "token.json").write_text(json.dumps({
                "token": {"access_token": f"tok_{acc_name}", "refresh_token": f"ref_{acc_name}", "token_type": "Bearer", "expiry": "2026-10-05T12:00:00Z"},
                "auth_method": "oauth",
                "id_token": _make_jwt(f"{acc_name}@example.com"),
            }))
        self.engine.set_active_name("acc1")

        def fake_fetch_quota(tok, refresh_token=None):
            if tok == "tok_acc1":
                return {"gemini": {"quota": 0, "reset_time": 1700000000}}
            raise TimeoutError("Standby unreachable")

        with patch("time.time", return_value=1000.0):
            with patch("agy_switch.QuotaClient.fetch_quota", side_effect=fake_fetch_quota):
                with patch("sys.stdin", io.StringIO(json.dumps({"modelName": "gemini-3.8-pro"}))):
                    out_capture = io.StringIO()
                    with patch("sys.stdout", out_capture):
                        ret = handle_pre_invocation_hook(self.engine)

        self.assertEqual(ret, 0)
        output = json.loads(out_capture.getvalue())
        # Standby status is indeterminate: hook must NOT emit all-exhausted warning
        self.assertEqual(output, {})
        self.assertEqual(self.engine.get_active_name(), "acc1")

    def test_tier_3_standby_probes_persist_to_cache(self):
        """Tier 3 concurrent probe across standby accounts persists fresh telemetry with cached_at to cache."""
        from agy_switch import handle_pre_invocation_hook
        import io

        self.engine.set_auto_switch(True)
        for acc_name in ["acc1", "acc2"]:
            acc_dir = self.vault_dir / "accounts" / acc_name
            acc_dir.mkdir(parents=True, exist_ok=True)
            (acc_dir / "token.json").write_text(json.dumps({
                "token": {"access_token": f"tok_{acc_name}", "refresh_token": f"ref_{acc_name}", "token_type": "Bearer", "expiry": "2026-10-05T12:00:00Z"},
                "auth_method": "oauth",
                "id_token": _make_jwt(f"{acc_name}@example.com"),
            }))
        self.engine.set_active_name("acc1")

        cache_dir = self.vault_dir / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / "last_known_quota.json"

        mock_quotas = {
            "tok_acc1": {"gemini": {"quota": 5, "reset_time": 1700000000}},
            "tok_acc2": {"gemini": {"quota": 95, "reset_time": 1700000000}},
        }

        with patch("time.time", return_value=2000.0):
            with patch("agy_switch.QuotaClient.fetch_quota", side_effect=lambda tok, refresh_token=None: mock_quotas.get(tok, {})):
                with patch("sys.stdin", io.StringIO(json.dumps({"modelName": "gemini-3.8-pro"}))):
                    out_capture = io.StringIO()
                    with patch("sys.stdout", out_capture):
                        ret = handle_pre_invocation_hook(self.engine)

        self.assertEqual(ret, 0)
        self.assertEqual(self.engine.get_active_name(), "acc2")

        # Verify cache persisted both accounts with cached_at = 2000.0
        self.assertTrue(cache_file.is_file())
        saved_cache = json.loads(cache_file.read_text(encoding="utf-8"))
        self.assertEqual(saved_cache["accounts"]["acc2"]["cached_at"], 2000.0)
        self.assertEqual(saved_cache["accounts"]["acc2"]["gemini"]["quota"], 95)
        self.assertEqual(saved_cache["accounts"]["acc1"]["cached_at"], 2000.0)
        self.assertEqual(saved_cache["accounts"]["acc1"]["gemini"]["quota"], 5)

    def test_tier_1_model_name_auto_and_missing_defaults_to_gemini(self):
        """When modelName is None, missing, or non-string, hook defaults safely to auto/gemini without crashing."""
        from agy_switch import handle_pre_invocation_hook
        import io

        self.engine.set_auto_switch(True)
        acc_dir = self.vault_dir / "accounts" / "acc1"
        acc_dir.mkdir(parents=True, exist_ok=True)
        (acc_dir / "token.json").write_text(json.dumps({
            "token": {"access_token": "tok1", "refresh_token": "ref1", "token_type": "Bearer", "expiry": "2026-10-05T12:00:00Z"},
            "auth_method": "oauth",
            "id_token": "header.eyJlbWFpbCI6ICJhY2MxQGV4YW1wbGUuY29tIn0.sig",
        }))
        self.engine.set_active_name("acc1")

        cache_dir = self.vault_dir / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "last_known_quota.json").write_text(json.dumps({
            "timestamp": 1000.0,
            "accounts": {
                "acc1": {
                    "cached_at": 1000.0,
                    "gemini": {"quota": 80, "reset_time": 1700000000},
                }
            }
        }), encoding="utf-8")

        for payload_dict in [{}, {"modelName": None}, {"modelName": 123}, {"modelName": ""}, {"modelName": "auto"}]:
            with patch("time.time", return_value=1100.0):
                with patch("agy_switch.fetch_account_quota") as mock_fetch:
                    with patch("sys.stdin", io.StringIO(json.dumps(payload_dict))):
                        out_capture = io.StringIO()
                        with patch("sys.stdout", out_capture):
                            ret = handle_pre_invocation_hook(self.engine)
                    self.assertEqual(ret, 0)
                    self.assertEqual(json.loads(out_capture.getvalue()), {})
                    # Tier 1 healthy bypass on gemini quota (80% >= 25%)
                    mock_fetch.assert_not_called()

    def test_tier_3_revoked_refresh_token_failover_to_standby(self):
        """Active account refresh token rejection (e.g. HTTP 400 Bad Request on OAuth token refresh) triggers Tier 3 failover to standby."""
        from agy_switch import handle_pre_invocation_hook
        import io

        self.engine.set_auto_switch(True)
        for acc_name in ["acc1", "acc2"]:
            acc_dir = self.vault_dir / "accounts" / acc_name
            acc_dir.mkdir(parents=True, exist_ok=True)
            (acc_dir / "token.json").write_text(json.dumps({
                "token": {"access_token": f"tok_{acc_name}", "refresh_token": f"ref_{acc_name}", "token_type": "Bearer", "expiry": "2026-10-05T12:00:00Z"},
                "auth_method": "oauth",
                "id_token": _make_jwt(f"{acc_name}@example.com"),
            }))
        self.engine.set_active_name("acc1")

        # Stale cache for acc1 indicating 90% quota
        cache_dir = self.vault_dir / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "last_known_quota.json").write_text(json.dumps({
            "timestamp": 1000.0,
            "accounts": {
                "acc1": {
                    "cached_at": 1000.0,
                    "gemini": {"quota": 90, "reset_time": 1700000000},
                },
            }
        }), encoding="utf-8")

        mock_quotas = {
            "tok_acc2": {"gemini": {"quota": 85, "reset_time": 1700000000}},
        }

        # Mock QuotaClient.fetch_quota:
        # For acc1, simulates 401 on CloudCode and refresh failure (returns {} and sets auth error)
        # For acc2, returns valid 85% quota
        def fake_fetch_quota(client_self, tok, refresh_token=None, on_refresh=None):
            if tok == "tok_acc1":
                client_self.last_error = "Token refresh rejected"
                client_self.last_error_type = "auth"
                return {}
            return mock_quotas.get(tok, {})

        with patch("time.time", return_value=1500.0):
            with patch("agy_switch.QuotaClient.fetch_quota", new=fake_fetch_quota):
                with patch.object(self.engine, "switch_tokens", wraps=self.engine.switch_tokens) as spy_switch:
                    with patch("sys.stdin", io.StringIO(json.dumps({"modelName": "gemini-3.8-pro"}))):
                        out_capture = io.StringIO()
                        err_capture = io.StringIO()
                        with patch("sys.stdout", out_capture), patch("sys.stderr", err_capture):
                            ret = handle_pre_invocation_hook(self.engine)

        self.assertEqual(ret, 0)
        # Succeeded in failover to acc2
        self.assertEqual(self.engine.get_active_name(), "acc2")
        spy_switch.assert_called_once_with("acc2", restart_server=False, force=True)

        output = json.loads(out_capture.getvalue())
        self.assertIn("injectSteps", output)
        step = output["injectSteps"][0]
        self.assertIn("ephemeralMessage", step)
        self.assertIn("Auto-rotated to **acc2**", step["ephemeralMessage"])
        self.assertIn("85% remaining", step["ephemeralMessage"])

        # Credential protection assertion: never leak token strings
        stderr_text = err_capture.getvalue()
        stdout_text = out_capture.getvalue()
        for secret in ["tok_acc1", "ref_acc1", "tok_acc2", "ref_acc2"]:
            self.assertNotIn(secret, stderr_text)
            self.assertNotIn(secret, stdout_text)

    def test_tier_3_all_registered_accounts_zero_warns_exhausted(self):
        """When all registered accounts in vault are verified at 0% quota, hook strictly emits exhaustion warning."""
        from agy_switch import handle_pre_invocation_hook
        import io

        self.engine.set_auto_switch(True)
        for acc_name in ["acc1", "acc2", "acc3"]:
            acc_dir = self.vault_dir / "accounts" / acc_name
            acc_dir.mkdir(parents=True, exist_ok=True)
            (acc_dir / "token.json").write_text(json.dumps({
                "token": {"access_token": f"tok_{acc_name}", "refresh_token": f"ref_{acc_name}", "token_type": "Bearer", "expiry": "2026-10-05T12:00:00Z"},
                "auth_method": "oauth",
                "id_token": _make_jwt(f"{acc_name}@example.com"),
            }))
        self.engine.set_active_name("acc1")

        zero_quota = {"gemini": {"quota": 0, "reset_time": 1700000000}}

        with patch("time.time", return_value=1000.0):
            with patch("agy_switch.QuotaClient.fetch_quota", return_value=zero_quota):
                with patch("sys.stdin", io.StringIO(json.dumps({"modelName": "gemini-3.8-pro"}))):
                    out_capture = io.StringIO()
                    with patch("sys.stdout", out_capture):
                        ret = handle_pre_invocation_hook(self.engine)

        self.assertEqual(ret, 0)
        output = json.loads(out_capture.getvalue())
        self.assertIn("injectSteps", output)
        step = output["injectSteps"][0]
        self.assertIn("ephemeralMessage", step)
        self.assertIn("⚠️ All Antigravity accounts have exhausted their gemini-3.8-pro quota.", step["ephemeralMessage"])
        # Active account remains acc1
        self.assertEqual(self.engine.get_active_name(), "acc1")

    def test_tier_3_partial_standby_unreachable_multi_accounts_proceeds_silently(self):
        """When 2 accounts are 0% and 1 standby account is unreachable without cache, all_exhausted is False and hook proceeds silently with {}."""
        from agy_switch import handle_pre_invocation_hook
        import io

        self.engine.set_auto_switch(True)
        for acc_name in ["acc1", "acc2", "acc3"]:
            acc_dir = self.vault_dir / "accounts" / acc_name
            acc_dir.mkdir(parents=True, exist_ok=True)
            (acc_dir / "token.json").write_text(json.dumps({
                "token": {"access_token": f"tok_{acc_name}", "refresh_token": f"ref_{acc_name}", "token_type": "Bearer", "expiry": "2026-10-05T12:00:00Z"},
                "auth_method": "oauth",
                "id_token": _make_jwt(f"{acc_name}@example.com"),
            }))
        self.engine.set_active_name("acc1")

        def fake_fetch_quota(tok, refresh_token=None):
            if tok in ("tok_acc1", "tok_acc2"):
                return {"gemini": {"quota": 0, "reset_time": 1700000000}}
            raise TimeoutError("acc3 network timeout")

        with patch("time.time", return_value=1000.0):
            with patch("agy_switch.QuotaClient.fetch_quota", side_effect=fake_fetch_quota):
                with patch("sys.stdin", io.StringIO(json.dumps({"modelName": "gemini-3.8-pro"}))):
                    out_capture = io.StringIO()
                    with patch("sys.stdout", out_capture):
                        ret = handle_pre_invocation_hook(self.engine)

        self.assertEqual(ret, 0)
        output = json.loads(out_capture.getvalue())
        # Must output empty {} without false alarm
        self.assertEqual(output, {})
        self.assertEqual(self.engine.get_active_name(), "acc1")

    def test_tier_3_standby_probe_failure_falls_back_to_standby_cache(self):
        """When active account quota drops < 15% and standby probe fails but standby has valid cached quota, hook rotates to standby using cache."""
        from agy_switch import handle_pre_invocation_hook
        import io

        self.engine.set_auto_switch(True)
        for acc_name in ["acc1", "acc2"]:
            acc_dir = self.vault_dir / "accounts" / acc_name
            acc_dir.mkdir(parents=True, exist_ok=True)
            (acc_dir / "token.json").write_text(json.dumps({
                "token": {"access_token": f"tok_{acc_name}", "refresh_token": f"ref_{acc_name}", "token_type": "Bearer", "expiry": "2026-10-05T12:00:00Z"},
                "auth_method": "oauth",
                "id_token": _make_jwt(f"{acc_name}@example.com"),
            }))
        self.engine.set_active_name("acc1")

        # Cache with 75% quota for acc2
        cache_dir = self.vault_dir / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "last_known_quota.json").write_text(json.dumps({
            "timestamp": 1000.0,
            "accounts": {
                "acc1": {
                    "cached_at": 1000.0,
                    "gemini": {"quota": 10, "reset_time": 1700000000},
                },
                "acc2": {
                    "cached_at": 1000.0,
                    "gemini": {"quota": 75, "reset_time": 1700000000},
                },
            }
        }), encoding="utf-8")

        # Probe for acc1 returns 10%, probe for acc2 fails with TimeoutError
        def fake_fetch_quota(tok, refresh_token=None):
            if tok == "tok_acc1":
                return {"gemini": {"quota": 10, "reset_time": 1700000000}}
            raise TimeoutError("Standby acc2 timeout")

        with patch("time.time", return_value=1200.0):
            with patch("agy_switch.QuotaClient.fetch_quota", side_effect=fake_fetch_quota):
                with patch("sys.stdin", io.StringIO(json.dumps({"modelName": "gemini-3.8-pro"}))):
                    out_capture = io.StringIO()
                    with patch("sys.stdout", out_capture):
                        ret = handle_pre_invocation_hook(self.engine)

        self.assertEqual(ret, 0)
        # Rotated to acc2 using cached telemetry
        self.assertEqual(self.engine.get_active_name(), "acc2")
        output = json.loads(out_capture.getvalue())
        self.assertIn("injectSteps", output)
        self.assertIn("Auto-rotated to **acc2**", output["injectSteps"][0]["ephemeralMessage"])
        self.assertIn("75% remaining", output["injectSteps"][0]["ephemeralMessage"])

    def test_tier_3_standby_auth_corruption_purged_from_combined_quotas(self):
        """When standby account in cache suffers auth corruption during Tier 3 probe, it is evicted from combined_quotas so engine does not rotate to it."""
        from agy_switch import handle_pre_invocation_hook
        import io

        self.engine.set_auto_switch(True)
        for acc_name in ["acc1", "acc2"]:
            acc_dir = self.vault_dir / "accounts" / acc_name
            acc_dir.mkdir(parents=True, exist_ok=True)
            (acc_dir / "token.json").write_text(json.dumps({
                "token": {"access_token": f"tok_{acc_name}", "refresh_token": f"ref_{acc_name}", "token_type": "Bearer", "expiry": "2026-10-05T12:00:00Z"},
                "auth_method": "oauth",
                "id_token": _make_jwt(f"{acc_name}@example.com"),
            }))
        self.engine.set_active_name("acc1")

        # Stale cache has acc2 with 90% quota
        cache_dir = self.vault_dir / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "last_known_quota.json").write_text(json.dumps({
            "timestamp": 1000.0,
            "accounts": {
                "acc1": {
                    "cached_at": 1000.0,
                    "gemini": {"quota": 10, "reset_time": 1700000000},
                },
                "acc2": {
                    "cached_at": 1000.0,
                    "gemini": {"quota": 90, "reset_time": 1700000000},
                },
            }
        }), encoding="utf-8")

        # acc1 has 10% quota. acc2 fails with 401 Unauthorized (revoked)
        def fake_fetch_quota(tok, refresh_token=None):
            if tok == "tok_acc1":
                return {"gemini": {"quota": 10, "reset_time": 1700000000}}
            raise urllib.error.HTTPError("https://cloudcode-pa.googleapis.com", 401, "Unauthorized", {}, io.BytesIO(b""))

        with patch("time.time", return_value=1200.0):
            with patch("agy_switch.QuotaClient.fetch_quota", side_effect=fake_fetch_quota):
                with patch("sys.stdin", io.StringIO(json.dumps({"modelName": "gemini-3.8-pro"}))):
                    out_capture = io.StringIO()
                    with patch("sys.stdout", out_capture):
                        ret = handle_pre_invocation_hook(self.engine)

        self.assertEqual(ret, 0)
        # acc2 must NOT be rotated to because its token is corrupted, even though cache had 90%
        self.assertEqual(self.engine.get_active_name(), "acc1")
        output = json.loads(out_capture.getvalue())
        self.assertEqual(output, {})

    def test_tier_3_excludes_active_account_from_standby_probe(self):
        """Tier 3 concurrent probe only queries standby accounts, never active_name."""
        from agy_switch import handle_pre_invocation_hook
        import io

        self.engine.set_auto_switch(True)
        for acc_name in ["acc1", "acc2", "acc3"]:
            acc_dir = self.vault_dir / "accounts" / acc_name
            acc_dir.mkdir(parents=True, exist_ok=True)
            (acc_dir / "token.json").write_text(json.dumps({
                "token": {
                    "access_token": f"tok_{acc_name}",
                    "refresh_token": f"ref_{acc_name}",
                    "token_type": "Bearer",
                    "expiry": "2026-10-05T12:00:00Z",
                },
                "auth_method": "oauth",
                "id_token": _make_jwt(f"{acc_name}@example.com"),
            }))
        self.engine.set_active_name("acc1")

        # Mock fetch_account_quota (Tier 2 probe for active account) to return 10% (< 15%)
        with patch("agy_switch.fetch_account_quota", return_value=("acc1", {"gemini": {"quota": 10, "reset_time": 1700000000}})):
            with patch("agy_switch.fetch_all_accounts_quota", return_value={
                "acc2": {"gemini": {"quota": 80, "reset_time": 1700000000}},
                "acc3": {"gemini": {"quota": 90, "reset_time": 1700000000}},
            }) as mock_fetch_all:
                with patch("sys.stdin", io.StringIO(json.dumps({"modelName": "gemini-3.8-pro"}))):
                    with patch("sys.stdout", io.StringIO()):
                        ret = handle_pre_invocation_hook(self.engine)

        self.assertEqual(ret, 0)
        mock_fetch_all.assert_called_once()
        probed_accounts = mock_fetch_all.call_args[0][1]
        probed_names = [a.get("name") for a in probed_accounts]
        self.assertNotIn("acc1", probed_names)
        self.assertIn("acc2", probed_names)
        self.assertIn("acc3", probed_names)

    def test_tier_2_does_not_write_cache_on_critical_quota_fallthrough(self):
        """Tier 2 does not write to disk cache when quota < 15%, deferring single write to Tier 3."""
        from agy_switch import handle_pre_invocation_hook
        import io

        self.engine.set_auto_switch(True)
        for acc_name in ["acc1", "acc2"]:
            acc_dir = self.vault_dir / "accounts" / acc_name
            acc_dir.mkdir(parents=True, exist_ok=True)
            (acc_dir / "token.json").write_text(json.dumps({
                "token": {
                    "access_token": f"tok_{acc_name}",
                    "refresh_token": f"ref_{acc_name}",
                    "token_type": "Bearer",
                    "expiry": "2026-10-05T12:00:00Z",
                },
                "auth_method": "oauth",
                "id_token": _make_jwt(f"{acc_name}@example.com"),
            }))
        self.engine.set_active_name("acc1")

        with patch("agy_switch.fetch_account_quota", return_value=("acc1", {"gemini": {"quota": 10, "reset_time": 1700000000}})):
            with patch("agy_switch.fetch_all_accounts_quota", return_value={
                "acc2": {"gemini": {"quota": 80, "reset_time": 1700000000}},
            }):
                with patch("agy_switch.QuotaClient.save_cache") as mock_save:
                    with patch("sys.stdin", io.StringIO(json.dumps({"modelName": "gemini-3.8-pro"}))):
                        with patch("sys.stdout", io.StringIO()):
                            ret = handle_pre_invocation_hook(self.engine)

        self.assertEqual(ret, 0)
        # Exactly 1 save_cache call in Tier 3, NOT 2 (Tier 2 did not write before fallthrough)
        self.assertEqual(mock_save.call_count, 1)

    def test_fallback_model_name_defaults_to_auto_and_sanitizes_display_to_gemini(self):
        """Fallback modelName defaults to 'auto' and sanitizes warning display to 'Gemini'."""
        from agy_switch import handle_pre_invocation_hook
        import io

        self.engine.set_auto_switch(True)
        for acc_name in ["acc1"]:
            acc_dir = self.vault_dir / "accounts" / acc_name
            acc_dir.mkdir(parents=True, exist_ok=True)
            (acc_dir / "token.json").write_text(json.dumps({
                "token": {
                    "access_token": f"tok_{acc_name}",
                    "refresh_token": f"ref_{acc_name}",
                    "token_type": "Bearer",
                    "expiry": "2026-10-05T12:00:00Z",
                },
                "auth_method": "oauth",
                "id_token": _make_jwt(f"{acc_name}@example.com"),
            }))
        self.engine.set_active_name("acc1")

        # acc1 has 0% quota
        with patch("agy_switch.fetch_account_quota", return_value=("acc1", {"gemini": {"quota": 0, "reset_time": 1700000000}})):
            with patch("agy_switch.fetch_all_accounts_quota", return_value={}):
                # Test with empty payload (no modelName)
                with patch("sys.stdin", io.StringIO("{}")):
                    out_capture = io.StringIO()
                    with patch("sys.stdout", out_capture):
                        ret = handle_pre_invocation_hook(self.engine)

        self.assertEqual(ret, 0)
        output = json.loads(out_capture.getvalue())
        self.assertIn("injectSteps", output)
        self.assertEqual(
            output["injectSteps"][0]["ephemeralMessage"],
            "⚠️ All Antigravity accounts have exhausted their Gemini quota.",
        )

    def test_combined_quotas_strictly_excludes_unregistered_vault_accounts(self):
        """combined_quotas only retains accounts currently registered in the vault."""
        from agy_switch import handle_pre_invocation_hook
        import io

        self.engine.set_auto_switch(True)
        # Vault only has acc1 and acc2
        for acc_name in ["acc1", "acc2"]:
            acc_dir = self.vault_dir / "accounts" / acc_name
            acc_dir.mkdir(parents=True, exist_ok=True)
            (acc_dir / "token.json").write_text(json.dumps({
                "token": {
                    "access_token": f"tok_{acc_name}",
                    "refresh_token": f"ref_{acc_name}",
                    "token_type": "Bearer",
                    "expiry": "2026-10-05T12:00:00Z",
                },
                "auth_method": "oauth",
                "id_token": _make_jwt(f"{acc_name}@example.com"),
            }))
        self.engine.set_active_name("acc1")

        # Cache file has a stale deleted account 'acc_deleted' with 100% quota
        cache_dir = self.vault_dir / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "last_known_quota.json").write_text(json.dumps({
            "timestamp": 1000.0,
            "accounts": {
                "acc1": {"cached_at": 1000.0, "gemini": {"quota": 10, "reset_time": 1700000000}},
                "acc2": {"cached_at": 1000.0, "gemini": {"quota": 80, "reset_time": 1700000000}},
                "acc_deleted": {"cached_at": 1000.0, "gemini": {"quota": 100, "reset_time": 1700000000}},
            }
        }), encoding="utf-8")

        with patch("time.time", return_value=1200.0):
            with patch("agy_switch.fetch_account_quota", return_value=("acc1", {"gemini": {"quota": 10, "reset_time": 1700000000}})):
                with patch("agy_switch.fetch_all_accounts_quota", return_value={
                    "acc2": {"gemini": {"quota": 80, "reset_time": 1700000000}},
                }):
                    with patch("sys.stdin", io.StringIO(json.dumps({"modelName": "gemini-3.8-pro"}))):
                        with patch("sys.stdout", io.StringIO()):
                            ret = handle_pre_invocation_hook(self.engine)

        self.assertEqual(ret, 0)
        # Rotated to acc2 (80%), NOT acc_deleted (100%) because acc_deleted was purged from combined_quotas
        self.assertEqual(self.engine.get_active_name(), "acc2")

    def test_hook_active_auth_error_standby_timeout_rotates_to_standby_without_eviction(self):
        """Active auth corruption + standby transient network timeout retains standby cache and rotates."""
        from agy_switch import handle_pre_invocation_hook
        import io

        self.engine.set_auto_switch(True)
        for acc_name in ["acc1", "acc2"]:
            acc_dir = self.vault_dir / "accounts" / acc_name
            acc_dir.mkdir(parents=True, exist_ok=True)
            (acc_dir / "token.json").write_text(json.dumps({
                "token": {
                    "access_token": f"tok_{acc_name}",
                    "refresh_token": f"ref_{acc_name}",
                    "token_type": "Bearer",
                    "expiry": "2026-10-05T12:00:00Z",
                },
                "auth_method": "oauth",
                "id_token": _make_jwt(f"{acc_name}@example.com"),
            }))
        self.engine.set_active_name("acc1")

        # Cache contains acc1 (10%) and acc2 (80%)
        cache_dir = self.vault_dir / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "last_known_quota.json").write_text(json.dumps({
            "timestamp": 1000.0,
            "accounts": {
                "acc1": {"cached_at": 1000.0, "gemini": {"quota": 10, "reset_time": 1700000000}},
                "acc2": {"cached_at": 1000.0, "gemini": {"quota": 80, "reset_time": 1700000000}},
            }
        }), encoding="utf-8")

        # acc1 fails with auth error setting client.last_error_type="auth"; acc2 fails with transient network timeout
        def fake_fetch_quota(self_client, token, refresh_token=None, on_refresh=None):
            self_client.last_error = "HTTP Error 401: Unauthorized"
            self_client.last_error_type = "auth"
            return {}

        with patch("time.time", return_value=1200.0):
            with patch("agy_switch.QuotaClient.fetch_quota", new=fake_fetch_quota):
                with patch("agy_switch.fetch_all_accounts_quota", return_value={
                    "acc2": {"error": "Timed out", "error_type": "network"},
                }):
                    with patch("sys.stdin", io.StringIO(json.dumps({"modelName": "gemini-3.8-pro"}))):
                        out_capture = io.StringIO()
                        with patch("sys.stdout", out_capture):
                            ret = handle_pre_invocation_hook(self.engine)

        self.assertEqual(ret, 0)
        # acc2 must NOT be evicted despite client.last_error_type="auth" from Tier 2
        # Switcher should successfully rotate to acc2
        self.assertEqual(self.engine.get_active_name(), "acc2")
        out_json = json.loads(out_capture.getvalue())
        self.assertIn("injectSteps", out_json)
        self.assertIn("acc2", out_json["injectSteps"][0]["ephemeralMessage"])

    def test_hook_tier3_permanently_purges_deleted_vault_accounts_from_disk_cache(self):
        """Deleted vault accounts are permanently removed from last_known_quota.json on disk."""
        from agy_switch import handle_pre_invocation_hook
        import io

        self.engine.set_auto_switch(True)
        # Vault only has acc1 and acc2
        for acc_name in ["acc1", "acc2"]:
            acc_dir = self.vault_dir / "accounts" / acc_name
            acc_dir.mkdir(parents=True, exist_ok=True)
            (acc_dir / "token.json").write_text(json.dumps({
                "token": {
                    "access_token": f"tok_{acc_name}",
                    "refresh_token": f"ref_{acc_name}",
                    "token_type": "Bearer",
                    "expiry": "2026-10-05T12:00:00Z",
                },
                "auth_method": "oauth",
                "id_token": _make_jwt(f"{acc_name}@example.com"),
            }))
        self.engine.set_active_name("acc1")

        # Cache file on disk contains acc1, acc2, and deleted account acc_deleted
        cache_dir = self.vault_dir / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / "last_known_quota.json"
        cache_file.write_text(json.dumps({
            "timestamp": 1000.0,
            "accounts": {
                "acc1": {"cached_at": 1000.0, "gemini": {"quota": 10, "reset_time": 1700000000}},
                "acc2": {"cached_at": 1000.0, "gemini": {"quota": 80, "reset_time": 1700000000}},
                "acc_deleted": {"cached_at": 1000.0, "gemini": {"quota": 100, "reset_time": 1700000000}},
            }
        }), encoding="utf-8")

        with patch("time.time", return_value=1200.0):
            with patch("agy_switch.fetch_account_quota", return_value=("acc1", {"gemini": {"quota": 10, "reset_time": 1700000000}})):
                with patch("agy_switch.fetch_all_accounts_quota", return_value={
                    "acc2": {"gemini": {"quota": 80, "reset_time": 1700000000}},
                }):
                    with patch("sys.stdin", io.StringIO(json.dumps({"modelName": "gemini-3.8-pro"}))):
                        with patch("sys.stdout", io.StringIO()):
                            ret = handle_pre_invocation_hook(self.engine)

        self.assertEqual(ret, 0)
        # Directly inspect disk cache file
        disk_data = json.loads(cache_file.read_text(encoding="utf-8"))
        self.assertIn("acc1", disk_data.get("accounts", {}))
        self.assertIn("acc2", disk_data.get("accounts", {}))
        self.assertNotIn("acc_deleted", disk_data.get("accounts", {}))

    def test_hook_tier2_http_5xx_retains_active_via_cache_without_standby_probe(self):
        """Active account probe returning HTTP 5xx falls back to cache without triggering Tier 3 probe."""
        from agy_switch import handle_pre_invocation_hook
        import io

        self.engine.set_auto_switch(True)
        for acc_name in ["acc1", "acc2"]:
            acc_dir = self.vault_dir / "accounts" / acc_name
            acc_dir.mkdir(parents=True, exist_ok=True)
            (acc_dir / "token.json").write_text(json.dumps({
                "token": {
                    "access_token": f"tok_{acc_name}",
                    "refresh_token": f"ref_{acc_name}",
                    "token_type": "Bearer",
                    "expiry": "2026-10-05T12:00:00Z",
                },
                "auth_method": "oauth",
                "id_token": _make_jwt(f"{acc_name}@example.com"),
            }))
        self.engine.set_active_name("acc1")

        # Cache contains acc1 with 50% quota
        cache_dir = self.vault_dir / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "last_known_quota.json").write_text(json.dumps({
            "timestamp": 1000.0,
            "accounts": {
                "acc1": {"cached_at": 1000.0, "gemini": {"quota": 50, "reset_time": 1700000000}},
            }
        }), encoding="utf-8")

        # Tier 2 returns HTTP 503 (stale cache >300s bypasses Tier 1)
        with patch("time.time", return_value=1500.0):
            with patch("agy_switch.fetch_account_quota", return_value=("acc1", {"error": "HTTP Error 503: Service Unavailable", "error_type": "http"})):
                with patch("agy_switch.fetch_all_accounts_quota") as mock_standby_fetch:
                    with patch("sys.stdin", io.StringIO(json.dumps({"modelName": "gemini-3.8-pro"}))):
                        out_capture = io.StringIO()
                        with patch("sys.stdout", out_capture):
                            ret = handle_pre_invocation_hook(self.engine)

        self.assertEqual(ret, 0)
        # Standby probe must NOT be called
        mock_standby_fetch.assert_not_called()
        self.assertEqual(self.engine.get_active_name(), "acc1")
        self.assertEqual(out_capture.getvalue().strip(), "{}")


    def test_hook_tier2_401ms_probe_error_retains_active_via_cache_without_tier3_auth_failover(self):
        """Active account probe error containing '401ms' falls back to valid cache rather than triggering Tier 3 auth failover."""
        from agy_switch import handle_pre_invocation_hook
        import io

        self.engine.set_auto_switch(True)
        for acc_name in ["acc1", "acc2"]:
            acc_dir = self.vault_dir / "accounts" / acc_name
            acc_dir.mkdir(parents=True, exist_ok=True)
            (acc_dir / "token.json").write_text(json.dumps({
                "token": {
                    "access_token": f"tok_{acc_name}",
                    "refresh_token": f"ref_{acc_name}",
                    "token_type": "Bearer",
                    "expiry": "2026-10-05T12:00:00Z",
                },
                "auth_method": "oauth",
                "id_token": _make_jwt(f"{acc_name}@example.com"),
            }))
        self.engine.set_active_name("acc1")

        # Cache contains acc1 with 50% quota
        cache_dir = self.vault_dir / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "last_known_quota.json").write_text(json.dumps({
            "timestamp": 1000.0,
            "accounts": {
                "acc1": {"cached_at": 1000.0, "gemini": {"quota": 50, "reset_time": 1700000000}},
                "acc2": {"cached_at": 1000.0, "gemini": {"quota": 90, "reset_time": 1700000000}},
            }
        }), encoding="utf-8")

        # Tier 2 returns 'Timed out after 401ms'
        with patch("time.time", return_value=1500.0):
            with patch("agy_switch.fetch_account_quota", return_value=("acc1", {"error": "Timed out after 401ms", "error_type": "network"})):
                with patch("agy_switch.fetch_all_accounts_quota") as mock_standby_fetch:
                    with patch("sys.stdin", io.StringIO(json.dumps({"modelName": "gemini-3.8-pro"}))):
                        out_capture = io.StringIO()
                        with patch("sys.stdout", out_capture):
                            ret = handle_pre_invocation_hook(self.engine)

        self.assertEqual(ret, 0)
        # 401ms must NOT trigger Tier 3 failover
        mock_standby_fetch.assert_not_called()
        self.assertEqual(self.engine.get_active_name(), "acc1")
        self.assertEqual(out_capture.getvalue().strip(), "{}")

    def test_hook_tier3_active_network_error_not_evicted_from_combined_quotas(self):
        """Active account experiencing network error is not evicted from combined_quotas during Tier 3 rotation."""
        from agy_switch import handle_pre_invocation_hook
        import io

        self.engine.set_auto_switch(True)
        for acc_name in ["acc1", "acc2"]:
            acc_dir = self.vault_dir / "accounts" / acc_name
            acc_dir.mkdir(parents=True, exist_ok=True)
            (acc_dir / "token.json").write_text(json.dumps({
                "token": {
                    "access_token": f"tok_{acc_name}",
                    "refresh_token": f"ref_{acc_name}",
                    "token_type": "Bearer",
                    "expiry": "2026-10-05T12:00:00Z",
                },
                "auth_method": "oauth",
                "id_token": _make_jwt(f"{acc_name}@example.com"),
            }))
        self.engine.set_active_name("acc1")

        # Cache contains acc1 (10% - below 15% threshold so Tier 2 falls through) and acc2 (80%)
        cache_dir = self.vault_dir / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / "last_known_quota.json"
        cache_file.write_text(json.dumps({
            "timestamp": 1000.0,
            "accounts": {
                "acc1": {"cached_at": 1000.0, "gemini": {"quota": 10, "reset_time": 1700000000}},
                "acc2": {"cached_at": 1000.0, "gemini": {"quota": 80, "reset_time": 1700000000}},
            }
        }), encoding="utf-8")

        # Tier 2 returns network error with 401ms
        with patch("time.time", return_value=1500.0):
            with patch("agy_switch.fetch_account_quota", return_value=("acc1", {"error": "Timed out after 401ms", "error_type": "network"})):
                with patch("agy_switch.fetch_all_accounts_quota", return_value={
                    "acc2": {"gemini": {"quota": 80, "reset_time": 1700000000}},
                }):
                    with patch("sys.stdin", io.StringIO(json.dumps({"modelName": "gemini-3.8-pro"}))):
                        out_capture = io.StringIO()
                        with patch("sys.stdout", out_capture):
                            ret = handle_pre_invocation_hook(self.engine)

        self.assertEqual(ret, 0)
        # acc1 has network error, NOT auth corruption. It should NOT be evicted from disk cache
        disk_data = json.loads(cache_file.read_text(encoding="utf-8"))
        self.assertIn("acc1", disk_data.get("accounts", {}))
        self.assertIn("acc2", disk_data.get("accounts", {}))

    def test_hook_active_zero_standby_auth_corruption_warns_exhausted(self):
        """Account 1 is 0% quota, Account 2 has revoked token (auth corruption) -> emits exhaustion warning."""
        from agy_switch import handle_pre_invocation_hook
        import io

        self.engine.set_auto_switch(True)
        for acc_name in ["acc1", "acc2"]:
            acc_dir = self.vault_dir / "accounts" / acc_name
            acc_dir.mkdir(parents=True, exist_ok=True)
            (acc_dir / "token.json").write_text(json.dumps({
                "token": {
                    "access_token": f"tok_{acc_name}",
                    "refresh_token": f"ref_{acc_name}",
                    "token_type": "Bearer",
                    "expiry": "2026-10-05T12:00:00Z",
                },
                "auth_method": "oauth",
                "id_token": _make_jwt(f"{acc_name}@example.com"),
            }))
        self.engine.set_active_name("acc1")

        # acc1 returns verified 0% quota
        zero_quota = {"gemini": {"quota": 0, "reset_time": 1700000000}}

        def fake_fetch_account_quota(eng, name, client=None):
            if name == "acc1":
                return "acc1", zero_quota
            return "acc2", {"error": "HTTP Error 401: Unauthorized", "error_type": "auth"}

        def fake_fetch_all(eng, accounts, client=None, max_workers=5):
            return {"acc2": {"error": "HTTP Error 401: Unauthorized", "error_type": "auth"}}

        with patch("agy_switch.fetch_account_quota", side_effect=fake_fetch_account_quota):
            with patch("agy_switch.fetch_all_accounts_quota", side_effect=fake_fetch_all):
                with patch("sys.stdin", io.StringIO(json.dumps({"modelName": "gemini-3.8-pro"}))):
                    out_capture = io.StringIO()
                    with patch("sys.stdout", out_capture):
                        ret = handle_pre_invocation_hook(self.engine)

        self.assertEqual(ret, 0)
        output = json.loads(out_capture.getvalue())
        self.assertIn("injectSteps", output, "Must emit injectSteps when all functional accounts are 0% and others dead from auth error")
        step = output["injectSteps"][0]
        self.assertIn("ephemeralMessage", step)
        self.assertIn("exhausted their gemini-3.8-pro quota", step["ephemeralMessage"])

    def test_hook_active_zero_standby_timeout_expired_cache_proceeds_silently(self):
        """Account 1 is 0% quota, Account 2 probe times out with expired 0% cache -> outputs {} (no false alarm)."""
        from agy_switch import handle_pre_invocation_hook
        import io

        self.engine.set_auto_switch(True)
        for acc_name in ["acc1", "acc2"]:
            acc_dir = self.vault_dir / "accounts" / acc_name
            acc_dir.mkdir(parents=True, exist_ok=True)
            (acc_dir / "token.json").write_text(json.dumps({
                "token": {
                    "access_token": f"tok_{acc_name}",
                    "refresh_token": f"ref_{acc_name}",
                    "token_type": "Bearer",
                    "expiry": "2026-10-05T12:00:00Z",
                },
                "auth_method": "oauth",
                "id_token": _make_jwt(f"{acc_name}@example.com"),
            }))
        self.engine.set_active_name("acc1")

        # Cache contains acc2 with 0% quota from hours ago (>300s TTL)
        cache_dir = self.vault_dir / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "last_known_quota.json").write_text(json.dumps({
            "timestamp": 1000.0,
            "accounts": {
                "acc2": {"cached_at": 1000.0, "gemini": {"quota": 0, "reset_time": 1100.0}},
            }
        }), encoding="utf-8")

        zero_quota = {"gemini": {"quota": 0, "reset_time": 1700000000}}

        with patch("time.time", return_value=1500.0):
            with patch("agy_switch.fetch_account_quota", return_value=("acc1", zero_quota)):
                with patch("agy_switch.fetch_all_accounts_quota", return_value={
                    "acc2": {"error": "Timed out", "error_type": "network"},
                }):
                    with patch("sys.stdin", io.StringIO(json.dumps({"modelName": "gemini-3.8-pro"}))):
                        out_capture = io.StringIO()
                        with patch("sys.stdout", out_capture):
                            ret = handle_pre_invocation_hook(self.engine)

        self.assertEqual(ret, 0)
        output = json.loads(out_capture.getvalue())
        self.assertEqual(output, {}, "Standby probe timeout with expired 0% cache must output {} without false warning")

    def test_hook_active_zero_standby_timeout_reset_elapsed_cache_proceeds_silently(self):
        """Account 1 is 0% quota, Account 2 probe times out with fresh cached_at but elapsed reset_time -> outputs {}."""
        from agy_switch import handle_pre_invocation_hook
        import io

        self.engine.set_auto_switch(True)
        for acc_name in ["acc1", "acc2"]:
            acc_dir = self.vault_dir / "accounts" / acc_name
            acc_dir.mkdir(parents=True, exist_ok=True)
            (acc_dir / "token.json").write_text(json.dumps({
                "token": {
                    "access_token": f"tok_{acc_name}",
                    "refresh_token": f"ref_{acc_name}",
                    "token_type": "Bearer",
                    "expiry": "2026-10-05T12:00:00Z",
                },
                "auth_method": "oauth",
                "id_token": _make_jwt(f"{acc_name}@example.com"),
            }))
        self.engine.set_active_name("acc1")

        # Cache has acc2 cached_at=1450.0 (fresh <= 300s relative to now=1500.0), but reset_time=1400.0 (elapsed!)
        cache_dir = self.vault_dir / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "last_known_quota.json").write_text(json.dumps({
            "timestamp": 1450.0,
            "accounts": {
                "acc2": {"cached_at": 1450.0, "gemini": {"quota": 0, "reset_time": 1400.0}},
            }
        }), encoding="utf-8")

        zero_quota = {"gemini": {"quota": 0, "reset_time": 1700000000}}

        with patch("time.time", return_value=1500.0):
            with patch("agy_switch.fetch_account_quota", return_value=("acc1", zero_quota)):
                with patch("agy_switch.fetch_all_accounts_quota", return_value={
                    "acc2": {"error": "Network unreachable", "error_type": "network"},
                }):
                    with patch("sys.stdin", io.StringIO(json.dumps({"modelName": "gemini-3.8-pro"}))):
                        out_capture = io.StringIO()
                        with patch("sys.stdout", out_capture):
                            ret = handle_pre_invocation_hook(self.engine)

        self.assertEqual(ret, 0)
        output = json.loads(out_capture.getvalue())
        self.assertEqual(output, {}, "Standby probe timeout with elapsed reset_time must output {} without false alarm")

    def test_hook_active_auth_corruption_standby_zero_warns_exhausted(self):
        """Active account suffers auth corruption, standby account has verified 0% quota -> emits exhaustion warning."""
        from agy_switch import handle_pre_invocation_hook
        import io

        self.engine.set_auto_switch(True)
        for acc_name in ["acc1", "acc2"]:
            acc_dir = self.vault_dir / "accounts" / acc_name
            acc_dir.mkdir(parents=True, exist_ok=True)
            (acc_dir / "token.json").write_text(json.dumps({
                "token": {
                    "access_token": f"tok_{acc_name}",
                    "refresh_token": f"ref_{acc_name}",
                    "token_type": "Bearer",
                    "expiry": "2026-10-05T12:00:00Z",
                },
                "auth_method": "oauth",
                "id_token": _make_jwt(f"{acc_name}@example.com"),
            }))
        self.engine.set_active_name("acc1")

        zero_quota = {"gemini": {"quota": 0, "reset_time": 1700000000}}

        with patch("time.time", return_value=1500.0):
            with patch("agy_switch.fetch_account_quota", return_value=("acc1", {"error": "HTTP Error 401: Unauthorized", "error_type": "auth"})):
                with patch("agy_switch.fetch_all_accounts_quota", return_value={
                    "acc2": zero_quota,
                }):
                    with patch("sys.stdin", io.StringIO(json.dumps({"modelName": "gemini-3.8-pro"}))):
                        out_capture = io.StringIO()
                        with patch("sys.stdout", out_capture):
                            ret = handle_pre_invocation_hook(self.engine)

        self.assertEqual(ret, 0)
        output = json.loads(out_capture.getvalue())
        self.assertIn("injectSteps", output)
        step = output["injectSteps"][0]
        self.assertIn("ephemeralMessage", step)
        self.assertIn("exhausted their gemini-3.8-pro quota", step["ephemeralMessage"])

    def test_hook_active_auth_error_standby_timeout_no_err_type_does_not_leak_auth(self):
        """Active account auth error must not contaminate standby network timeout lacking error_type key."""
        from agy_switch import handle_pre_invocation_hook
        import io

        self.engine.set_auto_switch(True)
        for acc_name in ["acc1", "acc2"]:
            acc_dir = self.vault_dir / "accounts" / acc_name
            acc_dir.mkdir(parents=True, exist_ok=True)
            (acc_dir / "token.json").write_text(json.dumps({
                "token": {
                    "access_token": f"tok_{acc_name}",
                    "refresh_token": f"ref_{acc_name}",
                    "token_type": "Bearer",
                    "expiry": "2026-10-05T12:00:00Z",
                },
                "auth_method": "oauth",
                "id_token": _make_jwt(f"{acc_name}@example.com"),
            }))
        self.engine.set_active_name("acc1")

        # acc1 active probe fails with 401 Unauthorized, setting client.last_error_type="auth"
        # acc2 standby probe fails with bare error dict without error_type key
        def fake_fetch_account_quota(eng, name, client=None):
            if name == "acc1":
                if client is not None:
                    client.last_error = "HTTP Error 401: Unauthorized"
                    client.last_error_type = "auth"
                return "acc1", {"error": "HTTP Error 401: Unauthorized", "error_type": "auth"}
            return "acc2", {"error": "Connection timed out"}

        with patch("agy_switch.fetch_account_quota", side_effect=fake_fetch_account_quota):
            with patch("agy_switch.fetch_all_accounts_quota", return_value={
                "acc2": {"error": "Connection timed out"},
            }):
                with patch("sys.stdin", io.StringIO(json.dumps({"modelName": "gemini-3.8-pro"}))):
                    out_capture = io.StringIO()
                    with patch("sys.stdout", out_capture):
                        ret = handle_pre_invocation_hook(self.engine)

        self.assertEqual(ret, 0)
        output = json.loads(out_capture.getvalue())
        # acc2 must NOT be treated as terminal auth corruption; output must be empty {}
        self.assertEqual(output, {}, "Standby network timeout without error_type must not be poisoned by active auth error")

    def test_hook_active_zero_standby_timeout_missing_cached_at_proceeds_silently(self):
        """Cache entry with 0% quota but missing/zero cached_at is treated as expired even when now <= 300s."""
        from agy_switch import handle_pre_invocation_hook
        import io

        self.engine.set_auto_switch(True)
        for acc_name in ["acc1", "acc2"]:
            acc_dir = self.vault_dir / "accounts" / acc_name
            acc_dir.mkdir(parents=True, exist_ok=True)
            (acc_dir / "token.json").write_text(json.dumps({
                "token": {
                    "access_token": f"tok_{acc_name}",
                    "refresh_token": f"ref_{acc_name}",
                    "token_type": "Bearer",
                    "expiry": "2026-10-05T12:00:00Z",
                },
                "auth_method": "oauth",
                "id_token": _make_jwt(f"{acc_name}@example.com"),
            }))
        self.engine.set_active_name("acc1")

        # Cache contains acc2 with 0% quota and missing cached_at (or 0)
        cache_dir = self.vault_dir / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "last_known_quota.json").write_text(json.dumps({
            "timestamp": 100.0,
            "accounts": {
                "acc2": {"gemini": {"quota": 0, "reset_time": 2000.0}},
            }
        }), encoding="utf-8")

        zero_quota = {"gemini": {"quota": 0, "reset_time": 1700000000}}

        with patch("time.time", return_value=100.0):
            with patch("agy_switch.fetch_account_quota", return_value=("acc1", zero_quota)):
                with patch("agy_switch.fetch_all_accounts_quota", return_value={
                    "acc2": {"error": "Timed out", "error_type": "network"},
                }):
                    with patch("sys.stdin", io.StringIO(json.dumps({"modelName": "gemini-3.8-pro"}))):
                        out_capture = io.StringIO()
                        with patch("sys.stdout", out_capture):
                            ret = handle_pre_invocation_hook(self.engine)

        self.assertEqual(ret, 0)
        output = json.loads(out_capture.getvalue())
        self.assertEqual(output, {}, "Missing cached_at must be treated as expired and proceed silently without false alarm")

    def test_hook_model_mismatch_missing_model_quota_does_not_warn_exhausted(self):
        """Accounts with healthy Gemini quota but missing Claude quota must not trigger false Claude exhaustion alarm."""
        from agy_switch import handle_pre_invocation_hook
        import io

        self.engine.set_auto_switch(True)
        for acc_name in ["acc1", "acc2"]:
            acc_dir = self.vault_dir / "accounts" / acc_name
            acc_dir.mkdir(parents=True, exist_ok=True)
            (acc_dir / "token.json").write_text(json.dumps({
                "token": {
                    "access_token": f"tok_{acc_name}",
                    "refresh_token": f"ref_{acc_name}",
                    "token_type": "Bearer",
                    "expiry": "2026-10-05T12:00:00Z",
                },
                "auth_method": "oauth",
                "id_token": _make_jwt(f"{acc_name}@example.com"),
            }))
        self.engine.set_active_name("acc1")

        # Both accounts have valid Gemini quota, but NO Claude quota
        gemini_quota_1 = {"gemini": {"quota": 80, "reset_time": 1700000000}}
        gemini_quota_2 = {"gemini": {"quota": 90, "reset_time": 1700000000}}

        with patch("time.time", return_value=1500.0):
            with patch("agy_switch.fetch_account_quota", return_value=("acc1", gemini_quota_1)):
                with patch("agy_switch.fetch_all_accounts_quota", return_value={
                    "acc2": gemini_quota_2,
                }):
                    with patch("sys.stdin", io.StringIO(json.dumps({"modelName": "claude-3-5-sonnet"}))):
                        out_capture = io.StringIO()
                        with patch("sys.stdout", out_capture):
                            ret = handle_pre_invocation_hook(self.engine)

        self.assertEqual(ret, 0)
        output = json.loads(out_capture.getvalue())
        self.assertEqual(output, {}, "Missing model quota must not trigger false all-exhausted warning")

    def test_hook_active_timeout_stale_cache_standby_zero_proceeds_silently(self):
        """Active account probe timeout with stale 0% cache is dropped, preventing false exhaustion warning."""
        from agy_switch import handle_pre_invocation_hook
        import io

        self.engine.set_auto_switch(True)
        for acc_name in ["acc1", "acc2"]:
            acc_dir = self.vault_dir / "accounts" / acc_name
            acc_dir.mkdir(parents=True, exist_ok=True)
            (acc_dir / "token.json").write_text(json.dumps({
                "token": {
                    "access_token": f"tok_{acc_name}",
                    "refresh_token": f"ref_{acc_name}",
                    "token_type": "Bearer",
                    "expiry": "2026-10-05T12:00:00Z",
                },
                "auth_method": "oauth",
                "id_token": _make_jwt(f"{acc_name}@example.com"),
            }))
        self.engine.set_active_name("acc1")

        # Cache contains acc1 with 0% quota and old cached_at (1000s ago)
        cache_dir = self.vault_dir / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "last_known_quota.json").write_text(json.dumps({
            "timestamp": 1000.0,
            "accounts": {
                "acc1": {"gemini": {"quota": 0, "reset_time": 2000.0}, "cached_at": 1000.0},
            }
        }), encoding="utf-8")

        standby_zero = {"gemini": {"quota": 0, "reset_time": 1700000000}}

        with patch("time.time", return_value=1500.0):
            with patch("agy_switch.fetch_account_quota", return_value=("acc1", {"error": "Connection timed out", "error_type": "network"})):
                with patch("agy_switch.fetch_all_accounts_quota", return_value={
                    "acc2": standby_zero,
                }):
                    with patch("sys.stdin", io.StringIO(json.dumps({"modelName": "gemini-3.8-pro"}))):
                        out_capture = io.StringIO()
                        with patch("sys.stdout", out_capture):
                            ret = handle_pre_invocation_hook(self.engine)

        self.assertEqual(ret, 0)
        output = json.loads(out_capture.getvalue())
        self.assertEqual(output, {}, "Active timeout with stale 0% cache must be indeterminate and proceed silently")

    def test_hook_all_accounts_auth_corruption_proceeds_silently(self):
        """When all accounts suffer auth corruption, no verified accounts exist, so hook proceeds silently."""
        from agy_switch import handle_pre_invocation_hook
        import io

        self.engine.set_auto_switch(True)
        for acc_name in ["acc1", "acc2"]:
            acc_dir = self.vault_dir / "accounts" / acc_name
            acc_dir.mkdir(parents=True, exist_ok=True)
            (acc_dir / "token.json").write_text(json.dumps({
                "token": {
                    "access_token": f"tok_{acc_name}",
                    "refresh_token": f"ref_{acc_name}",
                    "token_type": "Bearer",
                    "expiry": "2026-10-05T12:00:00Z",
                },
                "auth_method": "oauth",
                "id_token": _make_jwt(f"{acc_name}@example.com"),
            }))
        self.engine.set_active_name("acc1")

        with patch("time.time", return_value=1500.0):
            with patch("agy_switch.fetch_account_quota", return_value=("acc1", {"error": "HTTP Error 401: Unauthorized", "error_type": "auth"})):
                with patch("agy_switch.fetch_all_accounts_quota", return_value={
                    "acc2": {"error": "HTTP Error 401: Unauthorized", "error_type": "auth"},
                }):
                    with patch("sys.stdin", io.StringIO(json.dumps({"modelName": "gemini-3.8-pro"}))):
                        out_capture = io.StringIO()
                        with patch("sys.stdout", out_capture):
                            ret = handle_pre_invocation_hook(self.engine)

        self.assertEqual(ret, 0)
        output = json.loads(out_capture.getvalue())
        self.assertEqual(output, {}, "Complete auth corruption across all accounts must not emit false quota exhaustion warning")

    def test_hook_active_dict_network_error_triggers_stale_cache_evaluation(self):
        """Active account probe returning a dict network error with stale 0% cache drops active account from combined quotas."""
        from agy_switch import handle_pre_invocation_hook
        import io

        self.engine.set_auto_switch(True)
        for acc_name in ["acc1", "acc2"]:
            acc_dir = self.vault_dir / "accounts" / acc_name
            acc_dir.mkdir(parents=True, exist_ok=True)
            (acc_dir / "token.json").write_text(json.dumps({
                "token": {
                    "access_token": f"tok_{acc_name}",
                    "refresh_token": f"ref_{acc_name}",
                    "token_type": "Bearer",
                    "expiry": "2026-10-05T12:00:00Z",
                },
                "auth_method": "oauth",
                "id_token": _make_jwt(f"{acc_name}@example.com"),
            }))
        self.engine.set_active_name("acc1")

        # Cache contains acc1 with 0% quota and old cached_at (1000s ago)
        cache_dir = self.vault_dir / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "last_known_quota.json").write_text(json.dumps({
            "timestamp": 1000.0,
            "accounts": {
                "acc1": {"gemini": {"quota": 0, "reset_time": 2000.0}, "cached_at": 1000.0},
            }
        }), encoding="utf-8")

        standby_zero = {"gemini": {"quota": 0, "reset_time": 1700000000}}

        with patch("time.time", return_value=1500.0):
            with patch("agy_switch.fetch_account_quota", return_value=("acc1", {"error": "Connection reset by peer", "error_type": "network"})):
                with patch("agy_switch.fetch_all_accounts_quota", return_value={
                    "acc2": standby_zero,
                }):
                    with patch("sys.stdin", io.StringIO(json.dumps({"modelName": "gemini-3.8-pro"}))):
                        out_capture = io.StringIO()
                        with patch("sys.stdout", out_capture):
                            ret = handle_pre_invocation_hook(self.engine)

        self.assertEqual(ret, 0)
        output = json.loads(out_capture.getvalue())
        self.assertEqual(output, {}, "Active network error with stale 0% cache must evict stale cache and proceed silently")

    def test_hook_active_guard_symmetry_ast_contract(self):
        """Line 2226 in handle_pre_invocation_hook must inspect both probe_error and active_quota.get('error') for symmetry with line 2219."""
        import inspect
        from agy_switch import handle_pre_invocation_hook

        source = inspect.getsource(handle_pre_invocation_hook)
        # Check specific guard symmetry in Tier 3
        expected_guard = 'elif active_name and (probe_error is not None or (isinstance(active_quota, dict) and active_quota.get("error"))):'
        self.assertIn(expected_guard, source)

    def test_hooks_json_scoping(self) -> None:
        """[HOOK-SCOPE-001] [HOOK-SCOPE-EDGE-001] Scoped lifecycle hooks in root hooks.json and workspace overrides."""
        root_hooks_path = REPO_ROOT / "hooks.json"
        self.assertTrue(root_hooks_path.is_file(), f"Repository root hooks.json must exist at {root_hooks_path}")

        raw_content = root_hooks_path.read_text(encoding="utf-8")
        try:
            hooks_data_raw = json.loads(raw_content)
        except Exception as exc:
            self.fail(f"Root hooks.json must be valid JSON: {exc}")

        self.assertIsInstance(hooks_data_raw, dict)
        assert isinstance(hooks_data_raw, dict)

        hooks_data: dict[str, dict[str, object]] = {}
        for k, v in hooks_data_raw.items():
            self.assertIsInstance(v, dict, f"Hook entry {k} must be an object")
            assert isinstance(v, dict)
            hooks_data[str(k)] = {str(inner_k): inner_v for inner_k, inner_v in v.items()}

        # [HOOK-SCOPE-001] build-quality-gate must be disabled by default; agy-switch-auto-rotate enabled
        self.assertIn("build-quality-gate", hooks_data, "build-quality-gate must be declared in root hooks.json")
        self.assertIn("agy-switch-auto-rotate", hooks_data, "agy-switch-auto-rotate must be declared in root hooks.json")

        gate_hook = hooks_data["build-quality-gate"]
        auto_rotate_hook = hooks_data["agy-switch-auto-rotate"]

        self.assertIs(
            gate_hook.get("enabled"),
            False,
            "build-quality-gate must have 'enabled': false by default to prevent triggering in foreign workspaces",
        )
        self.assertIs(
            auto_rotate_hook.get("enabled"),
            True,
            "agy-switch-auto-rotate must have 'enabled': true by default",
        )

        # Verify command structures and target hooks
        self.assertIn("Stop", gate_hook, "build-quality-gate must define a Stop lifecycle hook")
        gate_stop = gate_hook.get("Stop")
        self.assertIsInstance(gate_stop, list)
        assert isinstance(gate_stop, list)
        self.assertTrue(len(gate_stop) > 0, "build-quality-gate Stop hook list must not be empty")
        gate_stop_first = gate_stop[0]
        self.assertIsInstance(gate_stop_first, dict)
        assert isinstance(gate_stop_first, dict)
        self.assertIn("verify-build.sh", str(gate_stop_first.get("command", "")))

        self.assertIn("PreInvocation", auto_rotate_hook, "agy-switch-auto-rotate must define a PreInvocation hook")
        auto_rotate_pre = auto_rotate_hook.get("PreInvocation")
        self.assertIsInstance(auto_rotate_pre, list)
        assert isinstance(auto_rotate_pre, list)
        self.assertTrue(len(auto_rotate_pre) > 0, "agy-switch-auto-rotate PreInvocation list must not be empty")
        auto_rotate_first = auto_rotate_pre[0]
        self.assertIsInstance(auto_rotate_first, dict)
        assert isinstance(auto_rotate_first, dict)
        self.assertIn("hook-pre-invocation", str(auto_rotate_first.get("command", "")))

        # Scheduling evaluation: build-quality-gate must not be scheduled automatically on agent turn Stop events
        def get_scheduled_hooks(config: dict[str, dict[str, object]], event_name: str) -> list[dict[str, object]]:
            scheduled: list[dict[str, object]] = []
            for hook_id, hook_def in config.items():
                if not isinstance(hook_def, dict):
                    continue
                if not hook_def.get("enabled", True):
                    continue
                if event_name in hook_def:
                    scheduled.append({"id": hook_id, "definition": hook_def})
            return scheduled

        plugin_scheduled_stop = get_scheduled_hooks(hooks_data, "Stop")
        self.assertEqual(
            plugin_scheduled_stop,
            [],
            "build-quality-gate must not be scheduled or invoked automatically on agent turn Stop events when disabled",
        )

        plugin_scheduled_pre = get_scheduled_hooks(hooks_data, "PreInvocation")
        self.assertEqual(len(plugin_scheduled_pre), 1)
        self.assertEqual(plugin_scheduled_pre[0]["id"], "agy-switch-auto-rotate")

        # [HOOK-SCOPE-EDGE-001] Workspace-level local opt-in override preservation
        def resolve_effective_hooks(
            plugin_hooks: dict[str, dict[str, object]],
            workspace_hooks: dict[str, dict[str, object]],
        ) -> dict[str, dict[str, object]]:
            effective: dict[str, dict[str, object]] = {}
            for k, v in plugin_hooks.items():
                effective[k] = dict(v)
            for k, v in workspace_hooks.items():
                if k in effective:
                    merged_entry = dict(effective[k])
                    merged_entry.update(v)
                    effective[k] = merged_entry
                else:
                    effective[k] = dict(v)
            return effective

        # Scenario A: Workspace overrides with .agents/hooks.json setting enabled: True
        agents_workspace_config: dict[str, dict[str, object]] = {
            "build-quality-gate": {
                "enabled": True,
            }
        }
        resolved_agents = resolve_effective_hooks(hooks_data, agents_workspace_config)
        resolved_gate_agents = resolved_agents["build-quality-gate"]
        self.assertIsInstance(resolved_gate_agents, dict)
        assert isinstance(resolved_gate_agents, dict)
        self.assertIs(resolved_gate_agents.get("enabled"), True)
        scheduled_stop_agents = get_scheduled_hooks(resolved_agents, "Stop")
        self.assertEqual(len(scheduled_stop_agents), 1)
        self.assertEqual(scheduled_stop_agents[0]["id"], "build-quality-gate")
        gate_stop_def = scheduled_stop_agents[0]["definition"]
        self.assertIsInstance(gate_stop_def, dict)
        assert isinstance(gate_stop_def, dict)
        gate_stop_handlers = gate_stop_def.get("Stop")
        self.assertIsInstance(gate_stop_handlers, list)
        assert isinstance(gate_stop_handlers, list)
        self.assertIn("verify-build.sh", str(gate_stop_handlers[0].get("command", "")))

        # Scenario B: Workspace overrides with .gemini/hooks.json setting enabled: True and custom Stop command
        gemini_workspace_config: dict[str, dict[str, object]] = {
            "build-quality-gate": {
                "enabled": True,
                "Stop": [
                    {
                        "type": "command",
                        "command": "bash /srv/agy-workspaces/booster/agy-switcher/scripts/verify-build.sh",
                        "timeout": 45,
                    }
                ],
            }
        }
        resolved_gemini = resolve_effective_hooks(hooks_data, gemini_workspace_config)
        resolved_gate_gemini = resolved_gemini["build-quality-gate"]
        self.assertIsInstance(resolved_gate_gemini, dict)
        assert isinstance(resolved_gate_gemini, dict)
        self.assertIs(resolved_gate_gemini.get("enabled"), True)
        scheduled_stop_gemini = get_scheduled_hooks(resolved_gemini, "Stop")
        self.assertEqual(len(scheduled_stop_gemini), 1)
        self.assertEqual(scheduled_stop_gemini[0]["id"], "build-quality-gate")

        # Scenario C: Foreign workspace without override keeps build-quality-gate disabled
        foreign_workspace_config: dict[str, dict[str, object]] = {}
        resolved_foreign = resolve_effective_hooks(hooks_data, foreign_workspace_config)
        resolved_gate_foreign = resolved_foreign["build-quality-gate"]
        self.assertIsInstance(resolved_gate_foreign, dict)
        assert isinstance(resolved_gate_foreign, dict)
        self.assertIs(resolved_gate_foreign.get("enabled"), False)
        self.assertEqual(get_scheduled_hooks(resolved_foreign, "Stop"), [])

        # Scenario D: Workspace explicitly disables agy-switch-auto-rotate
        disable_auto_rotate_config: dict[str, dict[str, object]] = {
            "agy-switch-auto-rotate": {
                "enabled": False,
            }
        }
        resolved_disabled_auto = resolve_effective_hooks(hooks_data, disable_auto_rotate_config)
        self.assertIs(resolved_disabled_auto["agy-switch-auto-rotate"].get("enabled"), False)
        self.assertEqual(get_scheduled_hooks(resolved_disabled_auto, "PreInvocation"), [])

        # Scenario E: Workspace explicitly maintains build-quality-gate disabled
        explicit_disabled_gate_config: dict[str, dict[str, object]] = {
            "build-quality-gate": {
                "enabled": False,
            }
        }
        resolved_explicit_disabled = resolve_effective_hooks(hooks_data, explicit_disabled_gate_config)
        self.assertIs(resolved_explicit_disabled["build-quality-gate"].get("enabled"), False)
        self.assertEqual(get_scheduled_hooks(resolved_explicit_disabled, "Stop"), [])

        # Scenario F: Workspace introduces a new unrelated hook without clobbering existing scoping
        unrelated_workspace_config: dict[str, dict[str, object]] = {
            "custom-linter": {
                "enabled": True,
                "PostToolUse": [{"type": "command", "command": "./lint.sh"}],
            }
        }
        resolved_unrelated = resolve_effective_hooks(hooks_data, unrelated_workspace_config)
        self.assertIn("custom-linter", resolved_unrelated)
        self.assertIs(resolved_unrelated["build-quality-gate"].get("enabled"), False)
        self.assertIs(resolved_unrelated["agy-switch-auto-rotate"].get("enabled"), True)
        self.assertEqual(len(get_scheduled_hooks(resolved_unrelated, "Stop")), 0)
        self.assertEqual(len(get_scheduled_hooks(resolved_unrelated, "PreInvocation")), 1)
        self.assertEqual(len(get_scheduled_hooks(resolved_unrelated, "PostToolUse")), 1)

    def test_corrupt_hooks_json(self) -> None:
        """[HOOK-SCOPE-ERR-001] Missing, unreadable, or syntactically invalid hooks.json fallback resilience."""
        # 1. Non-existent path returns empty dict, logs diagnostic warning, Stop hooks empty
        with self.subTest(scenario="missing_hooks_file"):
            missing_path = self.base_path / "nonexistent_hooks.json"
            warnings: list[str] = []
            hooks = load_hook_declarations(missing_path, warning_sink=warnings)
            self.assertEqual(hooks, {})
            self.assertTrue(len(warnings) > 0)
            self.assertIn("not found", warnings[0].lower())
            self.assertIn("defaulting build-quality-gate to disabled", warnings[0].lower())
            scheduled_stop = get_scheduled_hooks(hooks, "Stop")
            self.assertEqual(scheduled_stop, [])

        # 2. None path returns empty dict, logs diagnostic warning, Stop hooks empty
        with self.subTest(scenario="none_hooks_path"):
            warnings = []
            hooks = load_hook_declarations(None, warning_sink=warnings)
            self.assertEqual(hooks, {})
            self.assertTrue(len(warnings) > 0)
            self.assertIn("path is none", warnings[0].lower())
            self.assertIn("defaulting build-quality-gate to disabled", warnings[0].lower())
            self.assertEqual(get_scheduled_hooks(hooks, "Stop"), [])

        # 3. 0-byte empty file returns empty dict, logs diagnostic warning, Stop hooks empty
        with self.subTest(scenario="empty_0_byte_hooks_file"):
            empty_file = self.base_path / "empty_hooks.json"
            empty_file.write_bytes(b"")
            warnings = []
            hooks = load_hook_declarations(empty_file, warning_sink=warnings)
            self.assertEqual(hooks, {})
            self.assertTrue(len(warnings) > 0)
            self.assertIn("empty (0 bytes)", warnings[0].lower())
            self.assertIn("defaulting build-quality-gate to disabled", warnings[0].lower())
            self.assertEqual(get_scheduled_hooks(hooks, "Stop"), [])

        # 4. Whitespace-only file returns empty dict, logs diagnostic warning, Stop hooks empty
        with self.subTest(scenario="whitespace_only_hooks_file"):
            ws_file = self.base_path / "whitespace_hooks.json"
            ws_file.write_text("   \n\t  \n  ", encoding="utf-8")
            warnings = []
            hooks = load_hook_declarations(ws_file, warning_sink=warnings)
            self.assertEqual(hooks, {})
            self.assertTrue(len(warnings) > 0)
            self.assertIn("empty (0 bytes)", warnings[0].lower())
            self.assertIn("defaulting build-quality-gate to disabled", warnings[0].lower())
            self.assertEqual(get_scheduled_hooks(hooks, "Stop"), [])

        # 5. Syntactically invalid JSON: truncated, trailing commas, garbage text
        corrupt_syntax_cases: list[tuple[str, str]] = [
            ("truncated_json", '{"build-quality-gate": {"enabled": true, "Stop": ['),
            ("trailing_comma", '{"build-quality-gate": {"enabled": true,}, }'),
            ("unquoted_keys", '{build-quality-gate: true}'),
            ("random_binary_garbage", '<<< ANTIGRAVITY_CORRUPT_BUFFER >>>\x00\x01\x02'),
        ]
        for name, bad_content in corrupt_syntax_cases:
            with self.subTest(scenario=f"malformed_syntax_{name}"):
                bad_file = self.base_path / f"corrupt_{name}.json"
                bad_file.write_text(bad_content, encoding="utf-8", errors="replace")
                warnings = []
                hooks = load_hook_declarations(bad_file, warning_sink=warnings)
                self.assertEqual(hooks, {})
                self.assertTrue(len(warnings) > 0)
                self.assertIn("malformed json syntax", warnings[0].lower())
                self.assertIn("defaulting build-quality-gate to disabled", warnings[0].lower())
                self.assertEqual(get_scheduled_hooks(hooks, "Stop"), [])

        # 6. Non-object root JSON elements (lists, strings, numbers, booleans, null)
        non_object_cases: list[tuple[str, str]] = [
            ("list_root", '["build-quality-gate", "agy-switch-auto-rotate"]'),
            ("string_root", '"build-quality-gate"'),
            ("int_root", '42'),
            ("float_root", '3.14159'),
            ("bool_root", 'true'),
            ("null_root", 'null'),
        ]
        for name, bad_content in non_object_cases:
            with self.subTest(scenario=f"non_object_root_{name}"):
                non_obj_file = self.base_path / f"non_obj_{name}.json"
                non_obj_file.write_text(bad_content, encoding="utf-8")
                warnings = []
                hooks = load_hook_declarations(non_obj_file, warning_sink=warnings)
                self.assertEqual(hooks, {})
                self.assertTrue(len(warnings) > 0)
                self.assertIn("root json element must be an object", warnings[0].lower())
                self.assertIn("defaulting build-quality-gate to disabled", warnings[0].lower())
                self.assertEqual(get_scheduled_hooks(hooks, "Stop"), [])

        # 7. Corrupted individual hook entries (non-object values in valid root dictionary)
        with self.subTest(scenario="corrupted_entry_types"):
            entry_corrupt_file = self.base_path / "corrupt_entries.json"
            entry_corrupt_file.write_text(
                json.dumps({
                    "build-quality-gate": "corrupted_string_value",
                    "agy-switch-auto-rotate": [1, 2, 3],
                    "valid-custom-hook": {"enabled": False, "PostToolUse": []},
                }),
                encoding="utf-8",
            )
            warnings = []
            hooks = load_hook_declarations(entry_corrupt_file, warning_sink=warnings)
            self.assertIn("valid-custom-hook", hooks)
            self.assertNotIn("build-quality-gate", hooks)
            self.assertNotIn("agy-switch-auto-rotate", hooks)
            self.assertTrue(len(warnings) >= 2)
            self.assertTrue(any("build-quality-gate" in w and "must be an object" in w for w in warnings))
            self.assertTrue(any("agy-switch-auto-rotate" in w and "must be an object" in w for w in warnings))
            self.assertEqual(get_scheduled_hooks(hooks, "Stop"), [])

        # 8. Non-boolean enabled attribute in build-quality-gate coerces to disabled
        with self.subTest(scenario="non_boolean_enabled_coercion"):
            non_bool_file = self.base_path / "non_bool_enabled.json"
            non_bool_file.write_text(
                json.dumps({
                    "build-quality-gate": {
                        "enabled": "true",  # string, not bool
                        "Stop": [{"type": "command", "command": "bash /scripts/verify-build.sh"}],
                    }
                }),
                encoding="utf-8",
            )
            warnings = []
            hooks = load_hook_declarations(non_bool_file, warning_sink=warnings)
            self.assertIn("build-quality-gate", hooks)
            self.assertIs(hooks["build-quality-gate"].get("enabled"), False)
            self.assertTrue(len(warnings) > 0)
            self.assertIn("lacks boolean 'enabled' attribute", warnings[0])
            self.assertEqual(get_scheduled_hooks(hooks, "Stop"), [])

        # 9. Path is a directory instead of a file
        with self.subTest(scenario="directory_path_resilience"):
            dir_hooks = self.base_path / "hooks_dir_target"
            dir_hooks.mkdir(parents=True, exist_ok=True)
            warnings = []
            hooks = load_hook_declarations(dir_hooks, warning_sink=warnings)
            self.assertEqual(hooks, {})
            self.assertTrue(len(warnings) > 0)
            self.assertIn("is a directory, not a file", warnings[0].lower())
            self.assertEqual(get_scheduled_hooks(hooks, "Stop"), [])

        # 10. Unreadable file permissions / OS read failure
        with self.subTest(scenario="unreadable_file_permissions"):
            unreadable_file = self.base_path / "unreadable_hooks.json"
            unreadable_file.write_text('{"build-quality-gate": {"enabled": true}}', encoding="utf-8")
            try:
                unreadable_file.chmod(0o000)
                warnings = []
                if not os.access(unreadable_file, os.R_OK):
                    hooks = load_hook_declarations(unreadable_file, warning_sink=warnings)
                    self.assertEqual(hooks, {})
                    self.assertTrue(len(warnings) > 0)
                    self.assertIn("could not read lifecycle hooks file", warnings[0].lower())
                    self.assertEqual(get_scheduled_hooks(hooks, "Stop"), [])
                else:
                    # In environments running as root where chmod 0o000 is still readable,
                    # test unreadable directory traversal
                    sub_unreadable = self.base_path / "denied_dir" / "hooks.json"
                    sub_unreadable.parent.mkdir(parents=True, exist_ok=True)
                    sub_unreadable.write_text("{}", encoding="utf-8")
                    sub_unreadable.parent.chmod(0o000)
                    try:
                        if not os.access(sub_unreadable, os.R_OK):
                            hooks = load_hook_declarations(sub_unreadable, warning_sink=warnings)
                            self.assertEqual(hooks, {})
                            self.assertTrue(len(warnings) > 0)
                    finally:
                        sub_unreadable.parent.chmod(0o755)
            finally:
                unreadable_file.chmod(0o644)

        # 11. Malformed non-UTF-8 byte sequences
        with self.subTest(scenario="invalid_utf8_encoding"):
            bad_utf8_file = self.base_path / "bad_utf8.json"
            bad_utf8_file.write_bytes(b"\x80\x81\xff\xfe\xaa\xbb\xcc")
            warnings = []
            hooks = load_hook_declarations(bad_utf8_file, warning_sink=warnings)
            self.assertEqual(hooks, {})
            self.assertTrue(len(warnings) > 0)
            self.assertIn("not valid utf-8 text", warnings[0].lower())
            self.assertEqual(get_scheduled_hooks(hooks, "Stop"), [])

        # 12. Workspace override fallback resilience
        with self.subTest(scenario="workspace_override_fallback_layering"):
            # Sub-scenario 12a: Corrupted plugin hooks + valid workspace override
            corrupt_plugin_file = self.base_path / "corrupt_plugin_hooks.json"
            corrupt_plugin_file.write_text('{"build-quality-gate": INVALID_SYNTAX', encoding="utf-8")
            valid_ws_file = self.base_path / "valid_ws_hooks.json"
            valid_ws_file.write_text(
                json.dumps({
                    "build-quality-gate": {
                        "enabled": True,
                        "Stop": [{"type": "command", "command": "bash /srv/agy-workspaces/booster/agy-switcher/scripts/verify-build.sh"}],
                    }
                }),
                encoding="utf-8",
            )
            warn_plugin: list[str] = []
            warn_ws: list[str] = []
            p_hooks = load_hook_declarations(corrupt_plugin_file, warning_sink=warn_plugin)
            w_hooks = load_hook_declarations(valid_ws_file, warning_sink=warn_ws)
            self.assertEqual(p_hooks, {})
            self.assertTrue(len(warn_plugin) > 0)
            self.assertEqual(len(warn_ws), 0)

            effective_a = resolve_effective_hooks(p_hooks, w_hooks)
            self.assertIn("build-quality-gate", effective_a)
            self.assertIs(effective_a["build-quality-gate"].get("enabled"), True)
            scheduled_stop_a = get_scheduled_hooks(effective_a, "Stop")
            self.assertEqual(len(scheduled_stop_a), 1)
            self.assertEqual(scheduled_stop_a[0]["id"], "build-quality-gate")

            # Sub-scenario 12b: Corrupted plugin hooks + corrupted workspace override
            corrupt_ws_file = self.base_path / "corrupt_ws_hooks.json"
            corrupt_ws_file.write_text('["not", "a", "dict"]', encoding="utf-8")
            warn_ws_corrupt: list[str] = []
            w_corrupt_hooks = load_hook_declarations(corrupt_ws_file, warning_sink=warn_ws_corrupt)
            self.assertEqual(w_corrupt_hooks, {})
            self.assertTrue(len(warn_ws_corrupt) > 0)

            effective_b = resolve_effective_hooks(p_hooks, w_corrupt_hooks)
            self.assertEqual(effective_b, {})
            scheduled_stop_b = get_scheduled_hooks(effective_b, "Stop")
            self.assertEqual(scheduled_stop_b, [])

            # Sub-scenario 12c: Valid plugin root hooks + corrupted workspace override
            root_hooks_path = REPO_ROOT / "hooks.json"
            warn_root: list[str] = []
            root_hooks = load_hook_declarations(root_hooks_path, warning_sink=warn_root)
            self.assertEqual(len(warn_root), 0)
            self.assertIs(root_hooks["build-quality-gate"].get("enabled"), False)
            self.assertIs(root_hooks["agy-switch-auto-rotate"].get("enabled"), True)

            effective_c = resolve_effective_hooks(root_hooks, w_corrupt_hooks)
            self.assertIs(effective_c["build-quality-gate"].get("enabled"), False)
            self.assertIs(effective_c["agy-switch-auto-rotate"].get("enabled"), True)
            self.assertEqual(get_scheduled_hooks(effective_c, "Stop"), [])
            scheduled_pre_c = get_scheduled_hooks(effective_c, "PreInvocation")
            self.assertEqual(len(scheduled_pre_c), 1)
            self.assertEqual(scheduled_pre_c[0]["id"], "agy-switch-auto-rotate")




if __name__ == "__main__":
    unittest.main()


