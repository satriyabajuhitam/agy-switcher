#!/usr/bin/env python3
"""Tests for Ticket 01: Basic Hook Lifecycle & Configuration (agy-switch)."""

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from agy_switch import (
    AgySwitchEngine,
    main,
)


class TestHookLifecycleAndConfig(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)
        self.vault_dir = self.base_path / ".antigravity-vault"
        self.gemini_dir = self.base_path / ".gemini"
        self.legacy_root = self.base_path / ".agy-cli-manager"
        self.engine = AgySwitchEngine(
            vault_dir=self.vault_dir,
            gemini_dir=self.gemini_dir,
            legacy_root=self.legacy_root,
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
            "modelName": "gemini-3.7-flash-high",
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

    def test_hook_pre_invocation_empty_quotas_warns_exhausted(self):
        """When auto-switch is on and all network quotas fail (empty quotas), output exhausted warning ephemeralMessage."""
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

        input_payload = json.dumps({"modelName": "gemini-1.5-pro"})

        # Mock QuotaClient.fetch_quota to return empty dict (simulating network failure)
        with patch("quota_client.QuotaClient.fetch_quota", return_value={}):
            with patch("sys.stdin", io.StringIO(input_payload)):
                out_capture = io.StringIO()
                with patch("sys.stdout", out_capture):
                    ret = handle_pre_invocation_hook(self.engine)

        self.assertEqual(ret, 0)
        output = json.loads(out_capture.getvalue())
        self.assertIn("injectSteps", output, "Hook should output injectSteps warning when quotas are empty")
        self.assertEqual(len(output["injectSteps"]), 1)
        step = output["injectSteps"][0]
        self.assertEqual(step.get("type"), "ephemeralMessage")
        self.assertIn("exhausted their gemini-1.5-pro quota", step.get("message", ""))

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

        input_payload = json.dumps({"modelName": "gemini-1.5-pro"})
        zero_quota = {
            "groups": [
                {
                    "group": "Gemini Models",
                    "buckets": [{"bucketId": "five_hour", "remainingFraction": 0.0, "resetTime": "2026-10-05T15:00:00Z"}]
                }
            ]
        }

        with patch("quota_client.QuotaClient.fetch_quota", return_value=zero_quota):
            with patch("sys.stdin", io.StringIO(input_payload)):
                out_capture = io.StringIO()
                with patch("sys.stdout", out_capture):
                    ret = handle_pre_invocation_hook(self.engine)

        self.assertEqual(ret, 0)
        output = json.loads(out_capture.getvalue())
        self.assertIn("injectSteps", output)
        step = output["injectSteps"][0]
        self.assertEqual(step.get("type"), "ephemeralMessage")
        self.assertIn("exhausted their gemini-1.5-pro quota", step.get("message", ""))

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

        input_payload = json.dumps({"modelName": "gemini-1.5-pro"})

        with patch.object(self.engine, "get_account_tokens", wraps=self.engine.get_account_tokens) as spy_tokens:
            with patch("quota_client.QuotaClient.fetch_quota", return_value={}):
                with patch("sys.stdin", io.StringIO(input_payload)):
                    out_capture = io.StringIO()
                    with patch("sys.stdout", out_capture):
                        handle_pre_invocation_hook(self.engine)

        self.assertTrue(spy_tokens.called, "Hook must invoke engine.get_account_tokens()")


if __name__ == "__main__":
    unittest.main()
