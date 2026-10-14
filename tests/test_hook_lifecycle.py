#!/usr/bin/env python3
"""Tests for Ticket 01: Basic Hook Lifecycle & Configuration (agy-switch)."""

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
                "id_token": f"header.eyJlbWFpbCI6ICJ{acc_name}@example.com\"}}",
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
                "id_token": f"header.eyJlbWFpbCI6ICJ{acc_name}@example.com\"}}",
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
                "id_token": f"header.eyJlbWFpbCI6ICJ{acc_name}@example.com\"}}",
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
                "id_token": f"header.eyJlbWFpbCI6ICJ{acc_name}@example.com\"}}",
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
        """Intermediate quota (15% <= quota < 25%) triggers Tier 2 single probe even if cache is fresh."""
        from agy_switch import handle_pre_invocation_hook
        import io

        self.engine.set_auto_switch(True)
        for acc_name in ["acc1", "acc2"]:
            acc_dir = self.vault_dir / "accounts" / acc_name
            acc_dir.mkdir(parents=True, exist_ok=True)
            (acc_dir / "token.json").write_text(json.dumps({
                "token": {"access_token": f"tok_{acc_name}", "refresh_token": f"ref_{acc_name}", "token_type": "Bearer", "expiry": "2026-10-05T12:00:00Z"},
                "auth_method": "oauth",
                "id_token": f"header.eyJlbWFpbCI6ICJ{acc_name}@example.com\"}}",
            }))
        self.engine.set_active_name("acc1")

        # Cache created at 1000.0 with 20% quota
        cache_dir = self.vault_dir / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "last_known_quota.json").write_text(json.dumps({
            "timestamp": 1000.0,
            "accounts": {
                "acc1": {
                    "cached_at": 1000.0,
                    "gemini": {"quota": 20, "reset_time": 1700000000},
                },
                "acc2": {
                    "cached_at": 1000.0,
                    "gemini": {"quota": 90, "reset_time": 1700000000},
                }
            }
        }), encoding="utf-8")

        # Virtual clock at 1050.0 (delta = 50s <= 300s, but quota 20% is intermediate)
        fresh_quota = {"gemini": {"quota": 22, "reset_time": 1700000000}}
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
                "id_token": f"header.eyJlbWFpbCI6ICJ{acc_name}@example.com\"}}",
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
                "id_token": f"header.eyJlbWFpbCI6ICJ{acc_name}@example.com\"}}",
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
                "id_token": f"header.eyJlbWFpbCI6ICJ{acc_name}@example.com\"}}",
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
                "id_token": f"header.eyJlbWFpbCI6ICJ{acc_name}@example.com\"}}",
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
                "id_token": f"header.eyJlbWFpbCI6ICJ{acc_name}@example.com\"}}",
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
                "id_token": f"header.eyJlbWFpbCI6ICJ{acc_name}@example.com\"}}",
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
                "id_token": f"header.eyJlbWFpbCI6ICJ{acc_name}@example.com\"}}",
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
                "id_token": f"header.eyJlbWFpbCI6ICJ{acc_name}@example.com\"}}",
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
                "id_token": f"header.eyJlbWFpbCI6ICJ{acc_name}@example.com\"}}",
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
                "id_token": f"header.eyJlbWFpbCI6ICJ{acc_name}@example.com\"}}",
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
                "id_token": f"header.eyJlbWFpbCI6ICJ{acc_name}@example.com\"}}",
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
                "id_token": f"header.eyJlbWFpbCI6ICJ{acc_name}@example.com\"}}",
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
                "id_token": f"header.eyJlbWFpbCI6ICJ{acc_name}@example.com\"}}",
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
                "id_token": f"header.eyJlbWFpbCI6ICJ{acc_name}@example.com\"}}",
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
                "id_token": f"header.eyJlbWFpbCI6ICJ{acc_name}@example.com\"}}",
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
                "id_token": f"header.eyJlbWFpbCI6ICJ{acc_name}@example.com\"}}",
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
                "id_token": f"header.eyJlbWFpbCI6ICJ{acc_name}@example.com",
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
                "id_token": f"header.eyJlbWFpbCI6ICJ{acc_name}@example.com",
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


if __name__ == "__main__":
    unittest.main()

