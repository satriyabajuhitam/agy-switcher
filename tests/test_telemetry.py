"""Unit tests for Layer 1 Telemetry Engine: QuotaClient, thread-safe cache, and defensive parsers."""

from __future__ import annotations

import concurrent.futures
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

# Ensure scripts is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from agy_switcher.exceptions import AgySwitchError
from agy_switcher.telemetry import (
    AUTH_ERROR_CODE_PATTERN,
    AUTH_PHRASES,
    DEFAULT_CLIENT_ID,
    DEFAULT_CLIENT_SECRET,
    HTTP_SERVER_ERROR_PATTERN,
    QuotaClient,
    _has_auth_indicator,
    _is_auth_error,
    _is_network_error,
    _is_stale_exhausted_cache,
    _parse_quota_defensive,
    _parse_timestamp_defensive,
    _sanitize_expires_in,
    fetch_account_quota,
    fetch_all_accounts_quota,
    normalize_model_name,
    run_quota_pipeline,
    sort_accounts_by_quota,
)


class TestTelemetryLayerIsolation(unittest.TestCase):
    """Verifies that agy_switcher.telemetry can be imported in isolation without higher layers."""

    def test_isolated_import_does_not_load_vault_or_cli(self):
        scripts_dir = str(REPO_ROOT / "scripts")
        code = (
            "import sys; "
            f"sys.path.insert(0, {scripts_dir!r}); "
            "import agy_switcher.telemetry; "
            "assert 'agy_switcher.vault' not in sys.modules, 'vault leaked into sys.modules'; "
            "assert 'agy_switcher.cli' not in sys.modules, 'cli leaked into sys.modules'; "
            "assert 'agy_switcher.doctor' not in sys.modules, 'doctor leaked into sys.modules'; "
            "assert 'agy_switcher.watcher' not in sys.modules, 'watcher leaked into sys.modules'; "
            "assert 'agy_switcher.hook' not in sys.modules, 'hook leaked into sys.modules'"
        )
        res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Isolation assertion failed: {res.stderr}")


class TestDefensiveParsers(unittest.TestCase):
    def test_sanitize_expires_in(self):
        self.assertEqual(_sanitize_expires_in(3600), 3600)
        self.assertEqual(_sanitize_expires_in("1800"), 1800)
        self.assertEqual(_sanitize_expires_in("invalid", default=1234), 1234)
        self.assertEqual(_sanitize_expires_in(-50, default=3600), 3600)
        self.assertEqual(_sanitize_expires_in(0, default=3600), 3600)
        self.assertEqual(_sanitize_expires_in(True, default=3600), 3600)
        self.assertEqual(_sanitize_expires_in(False, default=3600), 3600)
        self.assertEqual(_sanitize_expires_in(float("inf"), default=3600), 3600)
        self.assertEqual(_sanitize_expires_in(float("nan"), default=3600), 3600)
        self.assertEqual(_sanitize_expires_in(40000000, default=3600), 3600)

    def test_parse_timestamp_defensive(self):
        self.assertEqual(_parse_timestamp_defensive(None), float("inf"))
        self.assertEqual(_parse_timestamp_defensive(True), float("inf"))
        self.assertEqual(_parse_timestamp_defensive(False), float("inf"))
        self.assertEqual(_parse_timestamp_defensive(-100), float("inf"))
        self.assertEqual(_parse_timestamp_defensive(0), float("inf"))
        self.assertEqual(_parse_timestamp_defensive(1700000000), 1700000000.0)
        self.assertEqual(_parse_timestamp_defensive("1700000000"), 1700000000.0)
        self.assertEqual(_parse_timestamp_defensive("2026-10-05T12:00:00Z"), 1791201600.0)
        self.assertEqual(_parse_timestamp_defensive("2026-10-05T12:00:00+00:00"), 1791201600.0)
        self.assertEqual(_parse_timestamp_defensive("not-a-date"), float("inf"))

    def test_parse_quota_defensive(self):
        self.assertEqual(_parse_quota_defensive(None), -1)
        self.assertEqual(_parse_quota_defensive(True), -1)
        self.assertEqual(_parse_quota_defensive(False), -1)
        self.assertEqual(_parse_quota_defensive(-5), -1)
        self.assertEqual(_parse_quota_defensive(0), 0)
        self.assertEqual(_parse_quota_defensive(85), 85)
        self.assertEqual(_parse_quota_defensive("90"), 90.0)
        self.assertEqual(_parse_quota_defensive("75%"), 75.0)
        self.assertEqual(_parse_quota_defensive("invalid"), -1)

    def test_normalize_model_name(self):
        self.assertEqual(normalize_model_name("gemini"), "gemini")
        self.assertEqual(normalize_model_name("Gemini-3.8-Pro"), "gemini")
        with self.assertRaises(AgySwitchError):
            normalize_model_name("claude")
        with self.assertRaises(AgySwitchError):
            normalize_model_name("claude-3-5-sonnet")
        with self.assertRaises(AgySwitchError):
            normalize_model_name("gpt-4")
        with self.assertRaises(AgySwitchError):
            normalize_model_name("")
        with self.assertRaises(AgySwitchError):
            normalize_model_name(None)  # type: ignore

    def test_normalize_model_name_gemini_variants(self) -> None:
        """[QUOTA-GEMINI-001] normalize_model_name normalizes Gemini model variants to 'gemini'."""
        variants = ["gemini", "gemini-3.8-flash", "gemini-3.8-pro", "GEMINI-2.5-FLASH"]
        for var in variants:
            with self.subTest(variant=var):
                self.assertEqual(normalize_model_name(var), "gemini")

    def test_normalize_model_name_rejects_non_gemini(self) -> None:
        """[QUOTA-GEMINI-ERR-001] normalize_model_name raises AgySwitchError on non-Gemini models."""
        unsupported_models = [
            "claude",
            "CLAUDE",
            "claude-3-5-sonnet",
            "  claude-3-5-sonnet  ",
            "gpt-4o",
            "GPT-4O",
            "anthropic",
            "ANTHROPIC",
            "  anthropic  ",
            "deepseek",
            "llama-3",
            "gemini-claude",
            "   ",
        ]
        for unsupported in unsupported_models:
            with self.assertRaises(AgySwitchError) as ctx:
                normalize_model_name(unsupported)
            self.assertIn("strictly 'gemini'", str(ctx.exception).lower())

    def test_has_auth_indicator(self):
        self.assertTrue(_has_auth_indicator("HTTP Error 401: Unauthorized"))
        self.assertTrue(_has_auth_indicator("Token refresh rejected"))
        self.assertTrue(_has_auth_indicator("access_denied"))
        self.assertFalse(_has_auth_indicator("Connection timed out"))
        self.assertFalse(_has_auth_indicator(None))

    def test_is_auth_error(self):
        self.assertTrue(_is_auth_error({"error_type": "auth"}))
        self.assertTrue(_is_auth_error({"code": 401}))
        self.assertTrue(_is_auth_error({"status_code": "403"}))
        self.assertFalse(_is_auth_error({"code": 500}))
        self.assertTrue(_is_auth_error(urllib.error.HTTPError("http://url", 401, "Unauthorized", {}, None)))
        self.assertFalse(_is_auth_error(urllib.error.HTTPError("http://url", 503, "Unavailable", {}, None)))
        self.assertFalse(_is_auth_error(TimeoutError("Timeout")))
        self.assertTrue(_is_auth_error("Token refresh rejected"))

    def test_is_network_error(self):
        self.assertTrue(_is_network_error(urllib.error.HTTPError("http://url", 503, "Unavailable", {}, None)))
        self.assertFalse(_is_network_error(urllib.error.HTTPError("http://url", 401, "Unauthorized", {}, None)))
        self.assertTrue(_is_network_error(TimeoutError("timed out")))
        self.assertTrue(_is_network_error(ConnectionError("connection reset")))
        self.assertTrue(_is_network_error({"error_type": "network"}))
        self.assertFalse(_is_network_error({"error_type": "auth"}))
        self.assertTrue(_is_network_error("socket timeout"))

    def test_is_stale_exhausted_cache(self):
        now = time.time()
        # Invalid cached entry
        self.assertTrue(_is_stale_exhausted_cache(None, None, "gemini", now))
        self.assertTrue(_is_stale_exhausted_cache("not_dict", None, "gemini", now))

        # Mock rot_engine
        mock_engine = MagicMock()
        # Healthy quota (fraction > 0) is never stale exhausted
        mock_status = MagicMock()
        mock_status.fraction = 0.5
        mock_engine.get_quota_status.return_value = mock_status
        self.assertFalse(_is_stale_exhausted_cache({"cached_at": now}, mock_engine, "gemini", now))

        # 0% quota, recent cached_at, future reset_time -> not stale
        mock_status.fraction = 0.0
        mock_status.reset_time = datetime.fromtimestamp(now + 1000, tz=timezone.utc)
        entry = {"cached_at": now}
        self.assertFalse(_is_stale_exhausted_cache(entry, mock_engine, "gemini", now))

        # 0% quota, old cached_at (beyond 300s TTL) -> stale
        old_entry = {"cached_at": now - 301}
        self.assertTrue(_is_stale_exhausted_cache(old_entry, mock_engine, "gemini", now))

        # 0% quota, past reset_time -> stale
        mock_status.reset_time = datetime.fromtimestamp(now - 10, tz=timezone.utc)
        self.assertTrue(_is_stale_exhausted_cache(entry, mock_engine, "gemini", now))


class TestQuotaClient(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self.test_dir.name) / "cache"
        self.client = QuotaClient(timeout=0.5, cache_dir=self.cache_dir)

    def tearDown(self):
        self.test_dir.cleanup()

    def test_init_defaults(self):
        self.assertEqual(self.client.client_id, DEFAULT_CLIENT_ID)
        self.assertEqual(self.client.client_secret, DEFAULT_CLIENT_SECRET)
        self.assertEqual(self.client.timeout, 0.5)
        self.assertEqual(self.client.cache_file, self.cache_dir / "last_known_quota.json")

    def test_thread_local_errors(self):
        self.assertIsNone(self.client.last_error)
        self.assertIsNone(self.client.last_error_type)
        self.client.last_error = "Test Error"
        self.client.last_error_type = "auth"
        self.assertEqual(self.client.last_error, "Test Error")
        self.assertEqual(self.client.last_error_type, "auth")

    def test_parse_quota_response_hybrid(self):
        raw = {
            "groups": [
                {
                    "displayName": "Gemini Models",
                    "buckets": [
                        {"bucketId": "5h", "remainingFraction": 0.8, "resetTime": "2026-10-05T12:00:00Z"},
                        {"bucketId": "weekly", "remainingFraction": 0.95, "resetTime": "2026-10-10T12:00:00Z"},
                    ]
                },
                {
                    "group": "Claude Models",
                    "buckets": [
                        {"bucketId": "5h", "remainingFraction": 0.5, "resetTime": "2026-10-05T15:00:00Z"}
                    ]
                }
            ]
        }
        res = self.client.parse_quota_response(raw)
        self.assertIn("gemini", res)
        self.assertEqual(res["gemini"]["quota"], 80)
        self.assertEqual(res["gemini"]["buckets"]["5h"]["quota"], 80)
        self.assertEqual(res["gemini"]["buckets"]["weekly"]["quota"], 95)
        self.assertNotIn("claude", res)

    def test_parse_quota_response_gemini_exclusive(self) -> None:
        """[QUOTA-GEMINI-002] parse_quota_response extracts exclusively Gemini models and ignores Claude/third-party."""
        raw = {
            "groups": [
                {
                    "displayName": "Gemini Models",
                    "buckets": [
                        {"bucketId": "5h", "remainingFraction": 0.8, "resetTime": "2026-10-05T12:00:00Z"},
                        {"bucketId": "weekly", "remainingFraction": 0.95, "resetTime": "2026-10-10T12:00:00Z"},
                    ]
                },
                {
                    "group": "Claude Models",
                    "buckets": [
                        {"bucketId": "5h", "remainingFraction": 0.5, "resetTime": "2026-10-05T15:00:00Z"}
                    ]
                },
                {
                    "displayName": "Claude and GPT models",
                    "buckets": [
                        {"bucketId": "weekly", "remainingFraction": 0.3, "resetTime": "2026-10-05T15:00:00Z"}
                    ]
                },
                {
                    "displayName": "Anthropic Claude 3.5 Sonnet",
                    "buckets": [
                        {"bucketId": "5h", "remainingFraction": 0.4, "resetTime": "2026-10-05T15:00:00Z"}
                    ]
                }
            ]
        }
        res = self.client.parse_quota_response(raw)
        self.assertIn("gemini", res)
        self.assertEqual(res["gemini"]["quota"], 80)
        self.assertEqual(res["gemini"]["buckets"]["5h"]["quota"], 80)
        self.assertEqual(res["gemini"]["buckets"]["weekly"]["quota"], 95)
        # Verify third-party / Claude models are completely ignored
        self.assertNotIn("claude", res)
        self.assertNotIn("gpt", res)
        self.assertNotIn("anthropic", res)
        self.assertEqual(list(res.keys()), ["gemini"])

    def test_parse_quota_response_missing_or_empty_gemini_group(self) -> None:
        """[QUOTA-GEMINI-ERR-002] parse_quota_response handles missing or empty Gemini group gracefully."""
        cases: list[object] = [
            {},
            {"groups": []},
            {"groups": [{"displayName": "Claude Models", "buckets": [{"bucketId": "5h", "remainingFraction": 0.5}]}]},
            {"groups": [{"displayName": "Gemini Models", "buckets": []}]},
            {"groups": [{"displayName": "Gemini Models"}]},
            {"groups": [{"displayName": "Gemini Models", "buckets": None}]},
            {"groups": [{"displayName": "Gemini Models", "buckets": "not-a-list"}]},
            {"groups": [{"displayName": "Gemini Models", "buckets": [None, 123, "bad", {}]}]},
            {"groups": [{"displayName": "Gemini Models", "buckets": [{"bucketId": {"nested": 1}, "remainingFraction": 0.5}]}]},
            {"groups": [{"group": None, "displayName": None}]},
            {"groups": [None, 123, "corrupted"]},
            None,
            [],
            "not-a-dict",
        ]
        for case in cases:
            with self.subTest(payload=case):
                res = self.client.parse_quota_response(case)
                self.assertEqual(res, {})
                self.assertNotIn("gemini", res)

    def test_parse_quota_response_malformed_bucket_values_do_not_crash(self) -> None:
        """[QUOTA-GEMINI-ERR-002] parse_quota_response sanitizes NaN/Inf/bool bucket metrics and non-string displayName."""
        # Non-finite or bool remainingFraction / resetTime must never crash or inflate quota to 100%
        nan_inf_bool_payload = {
            "groups": [
                {
                    "displayName": 123,
                    "group": "Gemini Models",
                    "buckets": [
                        {"bucketId": "5h", "remainingFraction": float("nan"), "resetTime": float("nan")},
                        {"bucketId": "weekly", "remainingFraction": float("inf"), "resetTime": float("inf")},
                    ],
                }
            ]
        }
        res_nan = self.client.parse_quota_response(nan_inf_bool_payload)
        self.assertEqual(res_nan["gemini"]["quota"], 0)
        self.assertEqual(res_nan["gemini"]["reset_time"], 0)
        self.assertEqual(res_nan["gemini"]["buckets"]["5h"]["quota"], 0)
        self.assertEqual(res_nan["gemini"]["buckets"]["weekly"]["quota"], 0)

        bool_payload = {
            "groups": [
                {
                    "displayName": "",
                    "group": "Gemini Models",
                    "buckets": [
                        {"bucketId": "5h", "remainingFraction": True, "resetTime": True},
                    ],
                }
            ]
        }
        res_bool = self.client.parse_quota_response(bool_payload)
        self.assertEqual(res_bool["gemini"]["quota"], 0)
        self.assertEqual(res_bool["gemini"]["reset_time"], 0)

        # Empty Gemini group followed by a valid Gemini group recovers cleanly
        recovery_payload = {
            "groups": [
                {"displayName": "Gemini Models", "buckets": []},
                {"displayName": "Gemini Models", "buckets": [{"bucketId": "5h", "remainingFraction": 0.6, "resetTime": 1700000000}]},
            ]
        }
        res_rec = self.client.parse_quota_response(recovery_payload)
        self.assertEqual(res_rec["gemini"]["quota"], 60)
        self.assertEqual(res_rec["gemini"]["reset_time"], 1700000000)

    @patch("urllib.request.urlopen")
    def test_fetch_quota_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "groups": [{
                "displayName": "Gemini Models",
                "buckets": [{"bucketId": "5h", "remainingFraction": 0.7, "resetTime": "2026-10-05T12:00:00Z"}]
            }]
        }).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        res = self.client.fetch_quota("ya29.test_token")
        self.assertEqual(res["gemini"]["quota"], 70)

    @patch("urllib.request.urlopen")
    def test_fetch_quota_refresh_flow(self, mock_urlopen):
        # First call fails with 401, second call succeeds with refresh, third call succeeds with quota
        err_401 = urllib.error.HTTPError("http://quota", 401, "Unauthorized", {}, None)
        refresh_resp = MagicMock()
        refresh_resp.read.return_value = json.dumps({"access_token": "new_tok", "expires_in": 3600}).encode("utf-8")
        refresh_resp.__enter__.return_value = refresh_resp

        quota_resp = MagicMock()
        quota_resp.read.return_value = json.dumps({
            "groups": [{"displayName": "Gemini", "buckets": [{"bucketId": "5h", "remainingFraction": 0.9}]}]
        }).encode("utf-8")
        quota_resp.__enter__.return_value = quota_resp

        mock_urlopen.side_effect = [err_401, refresh_resp, quota_resp]

        on_refresh_mock = MagicMock()
        res = self.client.fetch_quota("old_tok", refresh_token="ref_tok", on_refresh=on_refresh_mock)
        self.assertEqual(res["gemini"]["quota"], 90)
        on_refresh_mock.assert_called_once()


class TestThreadSafeCache(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self.test_dir.name) / "cache"
        self.client = QuotaClient(cache_dir=self.cache_dir)

    def tearDown(self):
        self.test_dir.cleanup()

    def test_cache_permissions(self):
        self.client.save_cache({"accounts": {"acc1": {"gemini": {"quota": 80}}}})
        self.assertTrue(self.client.cache_file.is_file())
        file_mode = stat.S_IMODE(self.client.cache_file.stat().st_mode)
        dir_mode = stat.S_IMODE(self.cache_dir.stat().st_mode)
        self.assertEqual(file_mode, 0o600)
        self.assertEqual(dir_mode, 0o700)

    def test_concurrent_save_cache_no_lost_updates(self):
        """Spawns 10 threads concurrently updating distinct accounts with merge=True."""
        num_threads = 10
        errors = []

        def worker(acc_id: int):
            try:
                acc_name = f"acc_{acc_id}"
                client = QuotaClient(cache_dir=self.cache_dir)
                for i in range(5):
                    client.save_cache({
                        "accounts": {
                            acc_name: {
                                "gemini": {
                                    "quota": 10 * acc_id + i,
                                    "reset_time": 1700000000 + i,
                                }
                            }
                        }
                    }, merge=True)
                    time.sleep(0.005)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        cache = self.client.load_cache()
        accounts = cache.get("accounts", {})
        # All 10 accounts must be present in the merged cache without data loss
        self.assertEqual(len(accounts), num_threads)
        for i in range(num_threads):
            self.assertIn(f"acc_{i}", accounts)
            self.assertIn("gemini", accounts[f"acc_{i}"])

    def test_cache_sanitization_strips_claude(self) -> None:
        """[QUOTA-GEMINI-EDGE-001] load_cache and save_cache strip legacy 'claude' keys and persist sanitized cache."""
        self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        legacy_disk_payload = {
            "timestamp": 1700000000.0,
            "claude": {"legacy_top_level": True},
            "accounts": {
                "acc_mixed": {
                    "cached_at": 1700000000.0,
                    "gemini": {
                        "quota": 85,
                        "reset_time": 1700005000,
                        "cached": False,
                        "buckets": {"5h": {"quota": 85, "reset_time": 1700005000}},
                    },
                    "claude": {
                        "quota": 60,
                        "reset_time": 1700003000,
                        "cached": False,
                        "buckets": {"5h": {"quota": 60, "reset_time": 1700003000}},
                    },
                },
                "acc_claude_only": {
                    "cached_at": 1700000000.0,
                    "claude": {
                        "quota": 50,
                        "reset_time": 1700002000,
                        "cached": False,
                        "buckets": {},
                    },
                },
                "acc_gemini_only": {
                    "cached_at": 1700000000.0,
                    "gemini": {
                        "quota": 90,
                        "reset_time": 1700006000,
                        "cached": False,
                        "buckets": {"weekly": {"quota": 90, "reset_time": 1700006000}},
                    },
                },
            },
        }
        self.client.cache_file.write_text(json.dumps(legacy_disk_payload), encoding="utf-8")

        # 1. QuotaClient.load_cache() must strip all legacy 'claude' keys and persist sanitized file to disk
        loaded = self.client.load_cache()
        self.assertNotIn("claude", loaded)
        self.assertIn("acc_mixed", loaded.get("accounts", {}))
        self.assertNotIn("claude", loaded["accounts"]["acc_mixed"])
        self.assertIn("gemini", loaded["accounts"]["acc_mixed"])
        self.assertEqual(loaded["accounts"]["acc_mixed"]["gemini"]["quota"], 85)
        self.assertIn("acc_gemini_only", loaded["accounts"])
        self.assertIn("gemini", loaded["accounts"]["acc_gemini_only"])
        self.assertNotIn("claude", loaded["accounts"].get("acc_claude_only", {}))

        # Check persisted file on disk after load_cache()
        on_disk_raw = self.client.cache_file.read_text(encoding="utf-8")
        self.assertNotIn("claude", on_disk_raw)
        on_disk = json.loads(on_disk_raw)
        self.assertNotIn("claude", on_disk)
        self.assertNotIn("claude", on_disk.get("accounts", {}).get("acc_mixed", {}))
        self.assertIn("gemini", on_disk.get("accounts", {}).get("acc_mixed", {}))
        self.assertEqual(stat.S_IMODE(self.client.cache_file.stat().st_mode), 0o600)

        # 2. QuotaClient.save_cache() must strip legacy 'claude' keys on write
        incoming_with_claude = {
            "timestamp": 1700001000.0,
            "claude": {"stray": 123},
            "accounts": {
                "acc_new": {
                    "gemini": {"quota": 70, "reset_time": 1700007000},
                    "claude": {"quota": 40, "reset_time": 1700004000},
                },
                "acc_stray_claude": {
                    "claude": {"quota": 30, "reset_time": 1700001000},
                },
            },
        }
        self.client.save_cache(incoming_with_claude, merge=True)
        on_disk_raw2 = self.client.cache_file.read_text(encoding="utf-8")
        self.assertNotIn("claude", on_disk_raw2)
        loaded2 = self.client.load_cache()
        self.assertNotIn("claude", loaded2)
        self.assertIn("acc_new", loaded2.get("accounts", {}))
        self.assertIn("gemini", loaded2["accounts"]["acc_new"])
        self.assertNotIn("claude", loaded2["accounts"]["acc_new"])
        self.assertEqual(loaded2["accounts"]["acc_new"]["gemini"]["quota"], 70)

    def test_cache_sanitization_strips_claude_deep_nested_case_variants_and_corrupt_entries(self) -> None:
        """[QUOTA-GEMINI-EDGE-001] Sanitizes case-variant/nested claude keys, invalid accounts, and syncs schema."""
        self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

        # 1. Missing/invalid timestamp + deeply nested case-variant Claude + non-dict entries + alias named claude
        dirty_disk_payload = {
            "timestamp": "corrupt-ts",
            "Claude": {"top": True},
            "metadata": {"nested": [{"CLAUDE": 1, "keep": "ok"}]},
            "accounts": {
                "acc_valid": {
                    "Claude": {"quota": 30},
                    "gemini": {
                        "quota": 85,
                        "reset_time": 1700005000,
                        "claude": {"nested_in_gemini": True},
                        "buckets": {
                            "5h": {"quota": 85, "reset_time": 1700005000, "Claude": 10},
                            "claude_bucket": {"quota": 20},
                        },
                    },
                    "extra_errored": {"error": "stale"},
                },
                "claude@example.com": {
                    "gemini": {"quota": 92, "reset_time": 1700006000},
                    "claude": {"quota": 15},
                },
                "acc_only_nested_claude": {
                    "gemini": {"Claude": {"quota": 40}},
                },
                "acc_non_dict": "corrupt-string",
                "acc_none": None,
                "acc_err_legacy": {"error": "Fetch failed", "quota": 50},
                "acc_legacy_groups": {
                    "groups": [
                        {"displayName": "Claude Models", "buckets": [{"bucketId": "5h", "remainingFraction": 0.4}]},
                        {"displayName": "Gemini Models", "buckets": [{"bucketId": "5h", "remainingFraction": 0.7}]},
                    ]
                },
                "acc_claude_groups_only": {
                    "groups": [
                        {"displayName": "Claude and GPT models", "buckets": [{"bucketId": "5h", "remainingFraction": 0.4}]},
                    ]
                },
                "   ": {"gemini": {"quota": 100}},
            },
        }
        self.client.cache_file.write_text(json.dumps(dirty_disk_payload), encoding="utf-8")

        loaded = self.client.load_cache()
        persisted = json.loads(self.client.cache_file.read_text(encoding="utf-8"))

        # Returned dict must match the exact persisted schema (including generated timestamp and cached_at)
        self.assertEqual(loaded, persisted)
        self.assertIsInstance(loaded["timestamp"], float)
        self.assertGreater(loaded["timestamp"], 0.0)
        self.assertNotIn("Claude", loaded)
        self.assertEqual(loaded["metadata"], {"nested": [{"keep": "ok"}]})

        accounts = loaded["accounts"]
        self.assertIn("acc_valid", accounts)
        self.assertEqual(accounts["acc_valid"]["cached_at"], loaded["timestamp"])
        self.assertNotIn("Claude", accounts["acc_valid"])
        self.assertNotIn("extra_errored", accounts["acc_valid"])
        self.assertNotIn("claude", accounts["acc_valid"]["gemini"])
        self.assertEqual(
            accounts["acc_valid"]["gemini"]["buckets"],
            {"5h": {"quota": 85, "reset_time": 1700005000}},
        )

        # Account alias containing 'claude' must be preserved when it has valid Gemini quota
        self.assertIn("claude@example.com", accounts)
        self.assertEqual(accounts["claude@example.com"]["gemini"]["quota"], 92)
        self.assertNotIn("claude", accounts["claude@example.com"])

        # Legacy groups stripped of Claude while keeping Gemini group; Claude-only groups discarded
        self.assertIn("acc_legacy_groups", accounts)
        self.assertEqual(len(accounts["acc_legacy_groups"]["groups"]), 1)
        self.assertEqual(accounts["acc_legacy_groups"]["groups"][0]["displayName"], "Gemini Models")
        self.assertNotIn("acc_claude_groups_only", accounts)

        # Invalid/corrupt/claude-only entries must be purged from both memory and disk
        for removed in ("acc_only_nested_claude", "acc_non_dict", "acc_none", "acc_err_legacy", "   "):
            self.assertNotIn(removed, accounts)

        # 2. Non-dict 'accounts' field on disk is sanitized and persisted cleanly
        self.client.cache_file.write_text(
            json.dumps({"timestamp": 1700000000.0, "accounts": "corrupt"}),
            encoding="utf-8",
        )
        loaded_bad_accounts = self.client.load_cache()
        persisted_bad_accounts = json.loads(self.client.cache_file.read_text(encoding="utf-8"))
        self.assertEqual(loaded_bad_accounts, persisted_bad_accounts)
        self.assertEqual(loaded_bad_accounts["accounts"], {})

        # 3. save_cache(merge=True/False) strips nested/case-variant Claude in metadata & accounts
        self.client.save_cache(
            {
                "timestamp": 1700002000.0,
                "Claude": {"stray": True},
                "meta": {"CLAUDE": "drop"},
                "accounts": {
                    "acc_save_nested": {
                        "gemini": {
                            "quota": 75,
                            "buckets": {"5h": {"quota": 75}, "Claude": {"quota": 10}},
                        }
                    }
                },
            },
            merge=True,
        )
        saved_disk = json.loads(self.client.cache_file.read_text(encoding="utf-8"))
        self.assertNotIn("Claude", saved_disk)
        self.assertEqual(saved_disk["meta"], {})
        self.assertEqual(saved_disk["accounts"]["acc_save_nested"]["gemini"]["buckets"], {"5h": {"quota": 75}})


class TestStandaloneTelemetryHelpers(unittest.TestCase):
    def test_sort_accounts_by_quota_gemini(self):
        accounts = [
            {"name": "b", "quota": {"gemini": {"quota": 50, "reset_time": 1000}}},
            {"name": "a", "quota": {"gemini": {"quota": 90, "reset_time": 2000}}},
            {"name": "c", "quota": {"gemini": {"quota": 50, "reset_time": 500}}},
        ]
        sorted_accs = sort_accounts_by_quota(accounts)
        self.assertEqual([a["name"] for a in sorted_accs], ["a", "c", "b"])

    def test_sort_accounts_by_quota_custom_model(self):
        accounts = [
            {"name": "acc1", "quota": {"gemini": {"quota": 30, "reset_time": 1000}}},
            {"name": "acc2", "quota": {"gemini": {"quota": 80, "reset_time": 1000}}},
        ]
        sorted_accs = sort_accounts_by_quota(accounts, target_model="gemini-3.8-flash")
        self.assertEqual([a["name"] for a in sorted_accs], ["acc2", "acc1"])

    def test_fetch_account_quota_and_all_accounts(self):
        mock_engine = MagicMock()
        mock_engine.get_account_tokens.return_value = {
            "access_token": "fake_token",
            "refresh_token": "fake_refresh",
        }
        mock_client = MagicMock()
        mock_client.fetch_quota.return_value = {
            "gemini": {"quota": 95, "reset_time": 1700000000, "cached": False, "buckets": {}}
        }

        name, q = fetch_account_quota(mock_engine, "acc1", client=mock_client)
        self.assertEqual(name, "acc1")
        self.assertEqual(q["gemini"]["quota"], 95)

        all_q = fetch_all_accounts_quota(mock_engine, [{"name": "acc1"}, {"name": "acc2"}], client=mock_client)
        self.assertIn("acc1", all_q)
        self.assertIn("acc2", all_q)

    def test_run_quota_pipeline(self):
        mock_engine = MagicMock()
        mock_engine.get_account_tokens.return_value = {"access_token": "fake_token"}
        mock_client = MagicMock()
        mock_client.load_cache.return_value = {}
        mock_client.fetch_quota.return_value = {
            "gemini": {"quota": 85, "reset_time": 1700000000, "cached": False, "buckets": {}}
        }

        data = {"accounts": [{"name": "acc1"}, {"name": "acc2"}]}
        res = run_quota_pipeline(mock_engine, data, client=mock_client)
        self.assertEqual(len(res["accounts"]), 2)
        mock_client.save_cache.assert_called_once()

    def test_run_quota_pipeline_preserves_claude_quota(self):
        """run_quota_pipeline retains Claude quota models and does not mark pure Claude accounts as error."""
        mock_engine = MagicMock()
        mock_engine.get_account_tokens.side_effect = lambda name: {
            "acc_claude": {"access_token": "token_claude"},
            "acc_gemini": {"access_token": "token_gemini"},
            "acc_hybrid": {"access_token": "token_hybrid"},
        }.get(name)
        mock_client = MagicMock()
        mock_client.load_cache.return_value = {}

        def mock_fetch(tok, refresh_token=None, on_refresh=None):
            if tok == "token_claude":
                return {
                    "claude": {"quota": 80, "reset_time": 1700001000, "cached": False, "buckets": {}}
                }
            elif tok == "token_gemini":
                return {
                    "gemini": {"quota": 90, "reset_time": 1700002000, "cached": False, "buckets": {}}
                }
            elif tok == "token_hybrid":
                return {
                    "gemini": {"quota": 30, "reset_time": 1700003000, "cached": False, "buckets": {}},
                    "claude": {"quota": 95, "reset_time": 1700003000, "cached": False, "buckets": {}},
                }
            return {}

        mock_client.fetch_quota.side_effect = mock_fetch

        data = {"accounts": [{"name": "acc_claude"}, {"name": "acc_gemini"}, {"name": "acc_hybrid"}]}
        res = run_quota_pipeline(mock_engine, data, client=mock_client)

        # Verify save_cache was called with all valid quotas including Claude
        self.assertTrue(mock_client.save_cache.called)
        saved_cache = mock_client.save_cache.call_args[0][0]
        self.assertIn("acc_claude", saved_cache["accounts"])
        self.assertIn("acc_gemini", saved_cache["accounts"])
        self.assertIn("acc_hybrid", saved_cache["accounts"])

        acc_map = {a["name"]: a for a in res["accounts"]}
        # acc_claude must retain claude quota and NOT be marked Unreachable
        self.assertIn("claude", acc_map["acc_claude"]["quota"])
        self.assertEqual(acc_map["acc_claude"]["quota"]["claude"]["quota"], 80)
        self.assertNotIn("error", acc_map["acc_claude"]["quota"])

        # acc_hybrid must retain both gemini and claude
        self.assertIn("gemini", acc_map["acc_hybrid"]["quota"])
        self.assertIn("claude", acc_map["acc_hybrid"]["quota"])

        # Sorting by gemini model must place acc_gemini (90%) before others
        sorted_by_gemini = sort_accounts_by_quota(res["accounts"], target_model="gemini")
        gemini_order = [a["name"] for a in sorted_by_gemini]
        self.assertEqual(gemini_order[0], "acc_gemini")

    def test_run_quota_pipeline_claude_cache_fallback(self):
        """run_quota_pipeline falls back to cached Claude quota when live fetch fails."""
        mock_engine = MagicMock()
        mock_engine.get_account_tokens.return_value = {"access_token": "fake_token"}
        mock_client = MagicMock()
        mock_client.load_cache.return_value = {
            "timestamp": 1700000000,
            "accounts": {
                "acc_claude": {
                    "claude": {"quota": 75, "reset_time": 1700001000, "cached": False, "buckets": {}}
                }
            },
        }
        mock_client.fetch_quota.return_value = None

        data = {"accounts": [{"name": "acc_claude"}]}
        res = run_quota_pipeline(mock_engine, data, client=mock_client)
        acc_map = {a["name"]: a for a in res["accounts"]}

        self.assertIn("claude", acc_map["acc_claude"]["quota"])
        self.assertEqual(acc_map["acc_claude"]["quota"]["claude"]["quota"], 75)
        self.assertTrue(acc_map["acc_claude"]["quota"]["claude"]["cached"])
        self.assertNotIn("error", acc_map["acc_claude"]["quota"])

    def test_run_quota_pipeline_partial_live_merges_with_cache(self):
        """run_quota_pipeline preserves cached Claude when live fetch only returns Gemini."""
        mock_engine = MagicMock()
        mock_engine.get_account_tokens.return_value = {"access_token": "token_partial"}
        mock_client = MagicMock()
        mock_client.load_cache.return_value = {
            "timestamp": 1700000000,
            "accounts": {
                "acc_partial": {
                    "claude": {"quota": 65, "reset_time": 1700002000, "cached": False, "buckets": {}}
                }
            },
        }
        mock_client.fetch_quota.return_value = {
            "gemini": {"quota": 90, "reset_time": 1700003000, "cached": False, "buckets": {}}
        }

        data = {"accounts": [{"name": "acc_partial"}]}
        res = run_quota_pipeline(mock_engine, data, client=mock_client)
        acc = res["accounts"][0]

        self.assertIn("gemini", acc["quota"])
        self.assertIn("claude", acc["quota"])
        self.assertEqual(acc["quota"]["gemini"]["quota"], 90)
        self.assertFalse(acc["quota"]["gemini"]["cached"])
        self.assertEqual(acc["quota"]["claude"]["quota"], 65)
        self.assertTrue(acc["quota"]["claude"]["cached"])

    def test_run_quota_pipeline_unreachable_symmetric_model_schema(self):
        """run_quota_pipeline provides both gemini and claude keys when account is unreachable and uncached."""
        mock_engine = MagicMock()
        mock_engine.get_account_tokens.return_value = {"access_token": "token_err"}
        mock_client = MagicMock()
        mock_client.load_cache.return_value = {}
        mock_client.fetch_quota.return_value = None

        data = {"accounts": [{"name": "acc_down"}]}
        res = run_quota_pipeline(mock_engine, data, client=mock_client)
        acc = res["accounts"][0]

        self.assertEqual(acc["quota"]["error"], "Unreachable")
        self.assertIn("gemini", acc["quota"])
        self.assertIn("claude", acc["quota"])
        self.assertIsNone(acc["quota"]["gemini"])
        self.assertIsNone(acc["quota"]["claude"])


if __name__ == "__main__":
    unittest.main()

