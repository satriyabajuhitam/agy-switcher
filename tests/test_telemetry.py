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
        self.assertEqual(normalize_model_name("claude"), "claude")
        self.assertEqual(normalize_model_name("claude-3-5-sonnet"), "claude")
        with self.assertRaises(AgySwitchError):
            normalize_model_name("gpt-4")
        with self.assertRaises(AgySwitchError):
            normalize_model_name("")
        with self.assertRaises(AgySwitchError):
            normalize_model_name(None)  # type: ignore

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
        self.assertIn("claude", res)
        self.assertEqual(res["claude"]["quota"], 50)

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
            {"name": "acc1", "quota": {"claude": {"quota": 30, "reset_time": 1000}}},
            {"name": "acc2", "quota": {"claude": {"quota": 80, "reset_time": 1000}}},
        ]
        sorted_accs = sort_accounts_by_quota(accounts, target_model="claude")
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


if __name__ == "__main__":
    unittest.main()
