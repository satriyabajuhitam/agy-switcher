"""Tests for Fast Quota Telemetry Client."""

import json
import os
import stat
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from agy_switch import (
    QuotaClient,
    _is_auth_error,
    _is_network_error,
    fetch_account_quota,
    fetch_all_accounts_quota,
)


class TestQuotaClient(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self.test_dir.name) / "cache"
        self.client = QuotaClient(timeout=0.1, cache_dir=self.cache_dir)
        self.access_token = "ya29.fake_access"
        self.refresh_token = "1//fake_refresh"
        self.mock_quota_resp = {
            "groups": [
                {
                    "group": "Gemini Models",
                    "buckets": [
                        {"bucketId": "five_hour", "remainingFraction": 0.8, "resetTime": "2026-10-05T12:00:00Z"},
                        {"bucketId": "weekly", "remainingFraction": 0.9, "resetTime": "2026-10-10T12:00:00Z"},
                    ]
                }
            ]
        }
        self.expected_parsed_resp = {
            "gemini": {
                "quota": 80,
                "reset_time": 1791201600,
                "cached": False,
                "buckets": {
                    "5h": {"quota": 80, "reset_time": 1791201600},
                    "weekly": {"quota": 90, "reset_time": 1791633600},
                },
            }
        }

    def tearDown(self):
        self.test_dir.cleanup()

    @patch("urllib.request.urlopen")
    def test_fetch_quota_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(self.mock_quota_resp).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        res = self.client.fetch_quota(self.access_token)
        self.assertEqual(res, self.expected_parsed_resp)

        req_arg = mock_urlopen.call_args[0][0]
        self.assertEqual(req_arg.full_url, self.client.quota_endpoint)
        self.assertEqual(req_arg.headers.get("Authorization"), f"Bearer {self.access_token}")

    @patch("urllib.request.urlopen")
    def test_fetch_quota_success_display_name_schema(self, mock_urlopen):
        # Real CloudCode API returns displayName instead of group
        real_cloudcode_resp = {
            "groups": [
                {
                    "displayName": "Gemini Models",
                    "buckets": [
                        {"bucketId": "five_hour", "remainingFraction": 0.75, "resetTime": "2026-10-05T12:00:00Z"},
                    ]
                }
            ]
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(real_cloudcode_resp).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        res = self.client.fetch_quota(self.access_token)
        self.assertEqual(
            res,
            {
                "gemini": {
                    "quota": 75,
                    "reset_time": 1791201600,
                    "cached": False,
                    "buckets": {
                        "5h": {"quota": 75, "reset_time": 1791201600},
                    },
                }
            },
        )

    @patch("urllib.request.urlopen")
    def test_fetch_quota_network_error_fails_gracefully(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("Network unreachable")
        res = self.client.fetch_quota(self.access_token)
        self.assertEqual(res, {})

    @patch("urllib.request.urlopen")
    def test_fetch_quota_timeout_fails_gracefully(self, mock_urlopen):
        mock_urlopen.side_effect = TimeoutError("Timed out")
        res = self.client.fetch_quota(self.access_token)
        self.assertEqual(res, {})

    @patch("urllib.request.urlopen")
    def test_fetch_quota_refresh_flow(self, mock_urlopen):
        # Setup mock to return 401 on first call (fetch quota), success on token refresh, success on second fetch quota
        mock_401 = urllib.error.HTTPError(self.client.quota_endpoint, 401, "Unauthorized", {}, None)
        
        mock_refresh_resp = MagicMock()
        mock_refresh_resp.read.return_value = json.dumps({"access_token": "ya29.new_token"}).encode("utf-8")
        mock_refresh_resp.__enter__.return_value = mock_refresh_resp

        mock_quota_success = MagicMock()
        mock_quota_success.read.return_value = json.dumps(self.mock_quota_resp).encode("utf-8")
        mock_quota_success.__enter__.return_value = mock_quota_success

        mock_urlopen.side_effect = [mock_401, mock_refresh_resp, mock_quota_success]

        res = self.client.fetch_quota(self.access_token, refresh_token=self.refresh_token)
        self.assertEqual(res, self.expected_parsed_resp)
        self.assertEqual(mock_urlopen.call_count, 3)

    @patch("urllib.request.urlopen")
    def test_fetch_quota_refresh_fails_gracefully(self, mock_urlopen):
        # Fetch quota -> 401, refresh -> 400 (revoked), should fail gracefully
        mock_401 = urllib.error.HTTPError(self.client.quota_endpoint, 401, "Unauthorized", {}, None)
        mock_400_refresh = urllib.error.HTTPError(self.client.token_endpoint, 400, "Bad Request", {}, None)
        
        mock_urlopen.side_effect = [mock_401, mock_400_refresh]

        res = self.client.fetch_quota(self.access_token, refresh_token=self.refresh_token)
        self.assertEqual(res, {})
        self.assertEqual(mock_urlopen.call_count, 2)

    def test_cache_permissions_and_io(self):
        # Test save
        test_data = {"test_key": "test_val"}
        self.client.save_cache(test_data)
        
        # Verify directory permissions
        self.assertTrue(self.cache_dir.is_dir())
        dir_stat = os.stat(self.cache_dir)
        self.assertEqual(stat.S_IMODE(dir_stat.st_mode), 0o700)

        # Verify file permissions
        cache_file = self.client.cache_file
        self.assertTrue(cache_file.is_file())
        file_stat = os.stat(cache_file)
        self.assertEqual(stat.S_IMODE(file_stat.st_mode), 0o600)

        # Test load
        loaded = self.client.load_cache()
        self.assertEqual(loaded.get("test_key"), "test_val")
        self.assertIn("timestamp", loaded)
        self.assertIsInstance(loaded["timestamp"], float)

    def test_init_with_engine(self):
        engine_mock = MagicMock()
        engine_mock.cache_dir = self.cache_dir
        engine_mock.cache_file = self.cache_dir / "last_known_quota.json"
        
        client = QuotaClient(engine_mock)
        self.assertEqual(client.cache_dir, self.cache_dir)
        self.assertEqual(client.cache_file, engine_mock.cache_file)
        self.assertEqual(client.client_id, "1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com")

    def test_parse_quota_response_invalid_inputs(self):
        for invalid_input in [None, {}, "invalid", 123, [], {"groups": []}, {"groups": None}, {"groups": "string"}]:
            res = self.client.parse_quota_response(invalid_input)
            self.assertEqual(res, {})

    def test_parse_quota_response_bucket_normalization(self):
        variations = [
            ("five_hour", "5h"),
            ("five_hours", "5h"),
            ("5h", "5h"),
            ("rolling_5h", "5h"),
            ("weekly", "weekly"),
            ("week", "weekly"),
            ("7d", "weekly"),
            ("custom-limit!", "customlimit"),
        ]
        for raw_id, expected_id in variations:
            payload = {
                "groups": [
                    {
                        "displayName": "Gemini Models",
                        "buckets": [
                            {"bucketId": raw_id, "remainingFraction": 0.8, "resetTime": "2026-10-05T12:00:00Z"}
                        ]
                    }
                ]
            }
            res = self.client.parse_quota_response(payload)
            self.assertIn("gemini", res)
            self.assertIn("buckets", res["gemini"])
            self.assertIn(expected_id, res["gemini"]["buckets"])
            self.assertEqual(res["gemini"]["buckets"][expected_id]["quota"], 80)
            self.assertEqual(res["gemini"]["buckets"][expected_id]["reset_time"], 1791201600)

    def test_parse_quota_response_rounding_and_clamping(self):
        test_cases = [
            (0.796, 80),
            (0.804, 80),
            (0.0, 0),
            (1.0, 100),
            (None, 0),
            (1.5, 100),
            (-0.2, 0),
            ("not_a_number", 0),
        ]
        for frac, expected_quota in test_cases:
            payload = {
                "groups": [
                    {
                        "displayName": "Gemini Models",
                        "buckets": [
                            {"bucketId": "5h", "remainingFraction": frac, "resetTime": "2026-10-05T12:00:00Z"}
                        ]
                    }
                ]
            }
            res = self.client.parse_quota_response(payload)
            self.assertEqual(res["gemini"]["quota"], expected_quota)
            self.assertEqual(res["gemini"]["buckets"]["5h"]["quota"], expected_quota)

    def test_parse_quota_response_deterministic_tie_breaking(self):
        # When 5h and weekly share identical percentages (100%), earliest reset timestamp wins
        payload = {
            "groups": [
                {
                    "displayName": "Gemini Models",
                    "buckets": [
                        {"bucketId": "weekly", "remainingFraction": 1.0, "resetTime": "2026-10-10T12:00:00Z"},
                        {"bucketId": "5h", "remainingFraction": 1.0, "resetTime": "2026-10-05T12:00:00Z"},
                    ]
                }
            ]
        }
        res = self.client.parse_quota_response(payload)
        self.assertEqual(res["gemini"]["quota"], 100)
        # 5h window (1791201600) is earlier than weekly (1791633600), so 5h must be selected as worst-case reset_time
        self.assertEqual(res["gemini"]["reset_time"], 1791201600)
        self.assertFalse(res["gemini"]["cached"])
        self.assertEqual(res["gemini"]["buckets"]["5h"]["quota"], 100)
        self.assertEqual(res["gemini"]["buckets"]["weekly"]["quota"], 100)

    def test_parse_quota_response_worst_case_selection(self):
        # Case 1: 5h is lower (10% vs 90%)
        payload1 = {
            "groups": [
                {
                    "displayName": "Gemini Models",
                    "buckets": [
                        {"bucketId": "5h", "remainingFraction": 0.1, "resetTime": "2026-10-05T12:00:00Z"},
                        {"bucketId": "weekly", "remainingFraction": 0.9, "resetTime": "2026-10-10T12:00:00Z"},
                    ]
                }
            ]
        }
        res1 = self.client.parse_quota_response(payload1)
        self.assertEqual(res1["gemini"]["quota"], 10)
        self.assertEqual(res1["gemini"]["reset_time"], 1791201600)

        # Case 2: Weekly is lower (5% vs 100%)
        payload2 = {
            "groups": [
                {
                    "displayName": "Gemini Models",
                    "buckets": [
                        {"bucketId": "5h", "remainingFraction": 1.0, "resetTime": "2026-10-05T12:00:00Z"},
                        {"bucketId": "weekly", "remainingFraction": 0.05, "resetTime": "2026-10-10T12:00:00Z"},
                    ]
                }
            ]
        }
        res2 = self.client.parse_quota_response(payload2)
        self.assertEqual(res2["gemini"]["quota"], 5)
        self.assertEqual(res2["gemini"]["reset_time"], 1791633600)

    def test_parse_quota_response_claude_preservation(self):
        payload = {
            "groups": [
                {
                    "displayName": "Claude and GPT models",
                    "buckets": [
                        {"bucketId": "5h", "remainingFraction": 0.6, "resetTime": "2026-10-05T12:00:00Z"},
                        {"bucketId": "weekly", "remainingFraction": 0.8, "resetTime": "2026-10-10T12:00:00Z"},
                    ]
                }
            ]
        }
        res = self.client.parse_quota_response(payload)
        self.assertIn("claude", res)
        self.assertEqual(res["claude"]["quota"], 60)
        self.assertEqual(res["claude"]["reset_time"], 1791201600)
        self.assertFalse(res["claude"]["cached"])
        self.assertIn("5h", res["claude"]["buckets"])
        self.assertIn("weekly", res["claude"]["buckets"])

    def test_parse_quota_response_malformed_reset_time(self):
        payload = {
            "groups": [
                {
                    "displayName": "Gemini Models",
                    "buckets": [
                        {"bucketId": "5h", "remainingFraction": 0.5, "resetTime": "invalid-timestamp-string"},
                        {"bucketId": "weekly", "remainingFraction": 0.7}
                    ]
                }
            ]
        }
        res = self.client.parse_quota_response(payload)
        self.assertEqual(res["gemini"]["quota"], 50)
        self.assertEqual(res["gemini"]["reset_time"], 0)
        self.assertEqual(res["gemini"]["buckets"]["5h"]["reset_time"], 0)
        self.assertEqual(res["gemini"]["buckets"]["weekly"]["reset_time"], 0)

    def test_parse_quota_response_float_tie_breaking(self):
        # When rounded integer percentages tie (e.g. 5%), raw unrounded float remainingFraction breaks the tie
        payload = {
            "groups": [
                {
                    "displayName": "Gemini Models",
                    "buckets": [
                        # Bucket B: 0.054 -> rounds to 5%, earlier reset time
                        {"bucketId": "weekly", "remainingFraction": 0.054, "resetTime": "2026-10-01T12:00:00Z"},
                        # Bucket A: 0.046 -> rounds to 5%, later reset time, but lower raw fraction (worst case)
                        {"bucketId": "5h", "remainingFraction": 0.046, "resetTime": "2026-10-05T12:00:00Z"},
                    ]
                }
            ]
        }
        res = self.client.parse_quota_response(payload)
        self.assertEqual(res["gemini"]["quota"], 5)
        # Raw float 0.046 < 0.054, so 5h (1791201600) must win as worst case despite later resetTime
        self.assertEqual(res["gemini"]["reset_time"], 1791201600)

    def test_save_cache_excludes_error_entries(self):
        # Pre-seed cache with a valid account
        self.client.save_cache({
            "timestamp": 1000.0,
            "accounts": {
                "acc_existing": {"gemini": {"quota": 90, "reset_time": 5000}}
            }
        })

        # Save with merge=True where incoming has a valid account and an error account
        incoming = {
            "timestamp": 2000.0,
            "accounts": {
                "acc_valid": {"gemini": {"quota": 80, "reset_time": 6000}},
                "acc_error": {"error": "Fetch failed"},
            }
        }
        self.client.save_cache(incoming, merge=True)

        loaded = self.client.load_cache()
        self.assertIn("acc_existing", loaded["accounts"])
        self.assertIn("acc_valid", loaded["accounts"])
        self.assertNotIn("acc_error", loaded["accounts"])

    def test_save_cache_auto_generates_root_timestamp_when_absent(self):
        """save_cache guarantees top-level 'timestamp' defaulting to time.time() when absent."""
        fixed_now = 1791200500.0
        with patch("time.time", return_value=fixed_now):
            self.client.save_cache({
                "accounts": {
                    "acc1": {"gemini": {"quota": 80, "reset_time": 5000}}
                }
            })

        loaded = self.client.load_cache()
        self.assertIn("timestamp", loaded)
        self.assertEqual(loaded["timestamp"], fixed_now)
        self.assertIn("acc1", loaded["accounts"])

    def test_save_cache_preserves_provided_root_timestamp(self):
        """save_cache preserves top-level 'timestamp' if explicitly provided."""
        self.client.save_cache({
            "timestamp": 12345.67,
            "accounts": {
                "acc1": {"gemini": {"quota": 80, "reset_time": 5000}}
            }
        })
        loaded = self.client.load_cache()
        self.assertEqual(loaded.get("timestamp"), 12345.67)

    def test_save_cache_preserves_account_level_cached_at(self):
        """save_cache preserves account-level cached_at as epoch float or int."""
        payload = {
            "timestamp": 1791200000.0,
            "accounts": {
                "acc_float": {
                    "cached_at": 1791200100.5,
                    "gemini": {"quota": 85, "reset_time": 5000}
                },
                "acc_int": {
                    "cached_at": 1791200200,
                    "gemini": {"quota": 90, "reset_time": 6000}
                }
            }
        }
        self.client.save_cache(payload)
        loaded = self.client.load_cache()
        self.assertIn("accounts", loaded)
        self.assertEqual(loaded["accounts"]["acc_float"]["cached_at"], 1791200100.5)
        self.assertIsInstance(loaded["accounts"]["acc_float"]["cached_at"], float)
        self.assertEqual(loaded["accounts"]["acc_int"]["cached_at"], 1791200200)
        self.assertIsInstance(loaded["accounts"]["acc_int"]["cached_at"], int)

    def test_save_cache_partial_merge_preserves_standby_and_cached_at(self):
        """save_cache(merge=True) preserves standby accounts and their cached_at when updating single account."""
        # Initial cache with standby and active accounts
        self.client.save_cache({
            "timestamp": 1000.0,
            "accounts": {
                "acc_active": {"cached_at": 1000.0, "gemini": {"quota": 50, "reset_time": 5000}},
                "acc_standby": {"cached_at": 900.0, "gemini": {"quota": 90, "reset_time": 6000}},
            }
        })

        # Single account update for active account with fresh cached_at and no top-level timestamp
        fixed_now = 1500.0
        with patch("time.time", return_value=fixed_now):
            self.client.save_cache({
                "accounts": {
                    "acc_active": {"cached_at": 1500.0, "gemini": {"quota": 85, "reset_time": 5000}}
                }
            }, merge=True)

        loaded = self.client.load_cache()
        self.assertEqual(loaded["timestamp"], fixed_now)
        # Standby account preserved with its original cached_at intact
        self.assertIn("acc_standby", loaded["accounts"])
        self.assertEqual(loaded["accounts"]["acc_standby"]["cached_at"], 900.0)
        self.assertEqual(loaded["accounts"]["acc_standby"]["gemini"]["quota"], 90)
        # Active account updated with fresh cached_at
        self.assertIn("acc_active", loaded["accounts"])
        self.assertEqual(loaded["accounts"]["acc_active"]["cached_at"], 1500.0)
        self.assertEqual(loaded["accounts"]["acc_active"]["gemini"]["quota"], 85)

    def test_save_cache_filters_error_laden_accounts_without_corrupting_valid_cache(self):
        """save_cache excludes error-laden or malformed accounts without corrupting existing valid entries."""
        self.client.save_cache({
            "timestamp": 1000.0,
            "accounts": {
                "acc_valid": {"cached_at": 1000.0, "gemini": {"quota": 75, "reset_time": 5000}},
            }
        })

        incoming = {
            "accounts": {
                "acc_valid": {"error": "Fetch failed"},  # Should not overwrite existing valid acc_valid
                "acc_gem_err": {"gemini": {"error": "Quota error"}},
                "acc_claude_err": {"claude": {"error": "Quota error"}},
                "acc_empty": {},
                "acc_none": None,
                "acc_str": "malformed_string",
                "acc_fresh": {"cached_at": 1200.0, "gemini": {"quota": 95, "reset_time": 6000}},
            }
        }
        self.client.save_cache(incoming, merge=True)

        loaded = self.client.load_cache()
        # Existing valid account kept intact
        self.assertIn("acc_valid", loaded["accounts"])
        self.assertEqual(loaded["accounts"]["acc_valid"]["gemini"]["quota"], 75)
        self.assertEqual(loaded["accounts"]["acc_valid"]["cached_at"], 1000.0)
        # Fresh valid account added
        self.assertIn("acc_fresh", loaded["accounts"])
        self.assertEqual(loaded["accounts"]["acc_fresh"]["gemini"]["quota"], 95)
        # Error-laden accounts filtered out
        for invalid_key in ["acc_gem_err", "acc_claude_err", "acc_empty", "acc_none", "acc_str"]:
            self.assertNotIn(invalid_key, loaded["accounts"])

    def test_save_cache_directory_and_file_permissions_restoration(self):
        """save_cache creates and restores 0700 dir permissions and 0600 file permissions."""
        self.client.save_cache({"accounts": {"acc1": {"gemini": {"quota": 80}}}})
        self.assertEqual(stat.S_IMODE(os.stat(self.cache_dir).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.client.cache_file).st_mode), 0o600)

        # Deliberately loosen permissions
        os.chmod(self.cache_dir, 0o755)
        os.chmod(self.client.cache_file, 0o644)
        self.assertEqual(stat.S_IMODE(os.stat(self.cache_dir).st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(os.stat(self.client.cache_file).st_mode), 0o644)

        # Save again and verify permissions restored
        self.client.save_cache({"accounts": {"acc1": {"gemini": {"quota": 85}}}})
        self.assertEqual(stat.S_IMODE(os.stat(self.cache_dir).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.client.cache_file).st_mode), 0o600)

    def test_save_cache_handles_non_dict_data_gracefully(self):
        """save_cache gracefully handles non-dict data without crashing."""
        for invalid_data in [None, "string", [1, 2, 3], 123]:
            self.client.save_cache(invalid_data)  # type: ignore
            loaded = self.client.load_cache()
            self.assertIn("timestamp", loaded)
            self.assertIsInstance(loaded["timestamp"], float)
            self.assertEqual(loaded.get("accounts"), {})

    def test_save_cache_malformed_accounts_field_does_not_corrupt_merge(self):
        """save_cache(merge=True) preserves existing accounts when incoming accounts field is non-dict."""
        self.client.save_cache({
            "timestamp": 1000.0,
            "accounts": {
                "acc_standby": {"cached_at": 950.0, "gemini": {"quota": 80, "reset_time": 5000}}
            }
        })

        for malformed_accounts in [None, "not_a_dict", [1, 2, 3], 123]:
            self.client.save_cache({"accounts": malformed_accounts}, merge=True)  # type: ignore
            loaded = self.client.load_cache()
            self.assertIn("acc_standby", loaded["accounts"])
            self.assertEqual(loaded["accounts"]["acc_standby"]["cached_at"], 950.0)

    def test_save_cache_filters_whitespace_and_empty_account_keys(self):
        """save_cache filters out empty strings or whitespace-only account keys."""
        incoming = {
            "accounts": {
                "": {"gemini": {"quota": 50}},
                "   ": {"gemini": {"quota": 60}},
                "acc_real": {"gemini": {"quota": 70}},
            }
        }
        self.client.save_cache(incoming, merge=False)
        loaded = self.client.load_cache()
        self.assertIn("acc_real", loaded["accounts"])
        self.assertNotIn("", loaded["accounts"])
        self.assertNotIn("   ", loaded["accounts"])

    def test_save_cache_invalid_timestamp_types_fallback_to_time(self):
        """save_cache falls back to time.time() if incoming timestamp is unparseable or non-finite."""
        fixed_now = 1791200999.0
        with patch("time.time", return_value=fixed_now):
            for bad_ts in ["not_a_number", None, float("inf"), float("-inf"), float("nan"), True, False, -100, 0]:
                self.client.save_cache({"timestamp": bad_ts, "accounts": {}})
                loaded = self.client.load_cache()
                self.assertEqual(loaded["timestamp"], fixed_now, f"Failed for bad timestamp: {bad_ts}")

    def test_save_cache_auto_injects_cached_at_when_omitted(self):
        """save_cache automatically injects cached_at equal to root timestamp when omitted by caller."""
        fixed_now = 1791200555.0
        with patch("time.time", return_value=fixed_now):
            self.client.save_cache({
                "accounts": {
                    "acc_no_cached_at": {"gemini": {"quota": 80, "reset_time": 5000}}
                }
            })
        loaded = self.client.load_cache()
        self.assertIn("acc_no_cached_at", loaded["accounts"])
        self.assertEqual(loaded["accounts"]["acc_no_cached_at"]["cached_at"], fixed_now)

    def test_save_cache_rejects_non_dict_model_and_arbitrary_junk_payloads(self):
        """save_cache rejects accounts with non-dict model data (e.g. string errors) or missing quota info."""
        incoming = {
            "accounts": {
                "acc_str_gemini": {"gemini": "500 Internal Server Error"},
                "acc_none_gemini": {"gemini": None},
                "acc_arbitrary_junk": {"dummy_key": "some_value"},
                "acc_valid_legacy": {"quota": 60, "reset_time": 5000},
                "acc_valid_hybrid": {"gemini": {"quota": 90, "reset_time": 6000}},
            }
        }
        self.client.save_cache(incoming, merge=False)
        loaded = self.client.load_cache()
        self.assertNotIn("acc_str_gemini", loaded["accounts"])
        self.assertNotIn("acc_none_gemini", loaded["accounts"])
        self.assertNotIn("acc_arbitrary_junk", loaded["accounts"])
        self.assertIn("acc_valid_legacy", loaded["accounts"])
        self.assertIn("acc_valid_hybrid", loaded["accounts"])

    def test_save_cache_logs_stderr_on_write_failure(self):
        """save_cache outputs diagnostic error to stderr when atomic write fails."""
        import io
        stderr_capture = io.StringIO()
        with patch("sys.stderr", stderr_capture):
            with patch("os.replace", side_effect=OSError("Disk full")):
                self.client.save_cache({"accounts": {"acc1": {"gemini": {"quota": 80}}}})
        err_output = stderr_capture.getvalue()
        self.assertIn("[QuotaClient] Failed to persist quota cache", err_output)
        self.assertIn("Disk full", err_output)


    @patch("urllib.request.urlopen")
    def test_refresh_access_token_success(self, mock_urlopen):
        """refresh_access_token returns dict with access_token and expires_in."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "access_token": "ya29.refreshed_token",
            "expires_in": 7200,
        }).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        res = self.client.refresh_access_token(self.refresh_token)
        self.assertEqual(res, {
            "access_token": "ya29.refreshed_token",
            "expires_in": 7200,
        })

    @patch("urllib.request.urlopen")
    def test_refresh_access_token_default_expires_in(self, mock_urlopen):
        """refresh_access_token defaults expires_in to 3600 when omitted in response."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "access_token": "ya29.refreshed_token",
        }).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        res = self.client.refresh_access_token(self.refresh_token)
        self.assertEqual(res, {
            "access_token": "ya29.refreshed_token",
            "expires_in": 3600,
        })

    @patch("urllib.request.urlopen")
    def test_refresh_access_token_string_and_invalid_expires_in(self, mock_urlopen):
        """refresh_access_token parses string expires_in and defaults invalid/negative values to 3600."""
        # String integer
        mock_resp1 = MagicMock()
        mock_resp1.read.return_value = json.dumps({
            "access_token": "ya29.tok1",
            "expires_in": "1800",
        }).encode("utf-8")
        mock_resp1.__enter__.return_value = mock_resp1
        mock_urlopen.return_value = mock_resp1

        res1 = self.client.refresh_access_token(self.refresh_token)
        self.assertEqual(res1, {"access_token": "ya29.tok1", "expires_in": 1800})

        # Negative number -> defaults to 3600
        mock_resp2 = MagicMock()
        mock_resp2.read.return_value = json.dumps({
            "access_token": "ya29.tok2",
            "expires_in": -100,
        }).encode("utf-8")
        mock_resp2.__enter__.return_value = mock_resp2
        mock_urlopen.return_value = mock_resp2

        res2 = self.client.refresh_access_token(self.refresh_token)
        self.assertEqual(res2, {"access_token": "ya29.tok2", "expires_in": 3600})

        # Non-numeric string -> defaults to 3600
        mock_resp3 = MagicMock()
        mock_resp3.read.return_value = json.dumps({
            "access_token": "ya29.tok3",
            "expires_in": "not-a-number",
        }).encode("utf-8")
        mock_resp3.__enter__.return_value = mock_resp3
        mock_urlopen.return_value = mock_resp3

        res3 = self.client.refresh_access_token(self.refresh_token)
        self.assertEqual(res3, {"access_token": "ya29.tok3", "expires_in": 3600})

    @patch("urllib.request.urlopen")
    def test_refresh_access_token_error_or_missing_token(self, mock_urlopen):
        """refresh_access_token returns None on error response or missing/empty token."""
        for error_payload in [
            {"error": "invalid_grant", "error_description": "Token revoked"},
            {"access_token": ""},
            {"access_token": "   "},
            {"access_token": None},
            {"access_token": 12345},
            {},
        ]:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(error_payload).encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp
            mock_urlopen.return_value = mock_resp

            res = self.client.refresh_access_token(self.refresh_token)
            self.assertIsNone(res, f"Expected None for payload {error_payload}")

    @patch("urllib.request.urlopen")
    def test_refresh_access_token_revoked_http_error(self, mock_urlopen):
        """refresh_access_token returns None on HTTP 400 Bad Request."""
        mock_urlopen.side_effect = urllib.error.HTTPError(
            self.client.token_endpoint, 400, "Bad Request", {}, None
        )
        res = self.client.refresh_access_token(self.refresh_token)
        self.assertIsNone(res)

    @patch("urllib.request.urlopen")
    def test_fetch_quota_refresh_unpacks_dict_and_passes_string_token(self, mock_urlopen):
        """fetch_quota unpacks the string token from refresh dict before recursive call and forwards dict to callback."""
        mock_401 = urllib.error.HTTPError(self.client.quota_endpoint, 401, "Unauthorized", {}, None)

        mock_refresh_resp = MagicMock()
        mock_refresh_resp.read.return_value = json.dumps({
            "access_token": "ya29.unpacked_token",
            "expires_in": 3600,
        }).encode("utf-8")
        mock_refresh_resp.__enter__.return_value = mock_refresh_resp

        mock_quota_success = MagicMock()
        mock_quota_success.read.return_value = json.dumps(self.mock_quota_resp).encode("utf-8")
        mock_quota_success.__enter__.return_value = mock_quota_success

        mock_urlopen.side_effect = [mock_401, mock_refresh_resp, mock_quota_success]

        callback_received = []

        def on_refresh_callback(tok_data):
            callback_received.append(tok_data)

        res = self.client.fetch_quota(
            self.access_token,
            refresh_token=self.refresh_token,
            on_refresh=on_refresh_callback,
        )
        self.assertEqual(res, self.expected_parsed_resp)
        self.assertEqual(mock_urlopen.call_count, 3)

        # Verify callback received full dictionary
        self.assertEqual(len(callback_received), 1)
        self.assertEqual(callback_received[0], {
            "access_token": "ya29.unpacked_token",
            "expires_in": 3600,
        })

        # Verify second quota request received string token, not dict
        second_quota_call_req = mock_urlopen.call_args_list[2][0][0]
        self.assertEqual(
            second_quota_call_req.headers.get("Authorization"),
            "Bearer ya29.unpacked_token",
        )

    def test_refresh_access_token_empty_or_none_refresh_token(self):
        """refresh_access_token returns None immediately for empty or non-string refresh token without network call."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            self.assertIsNone(self.client.refresh_access_token(""))
            self.assertIsNone(self.client.refresh_access_token("   "))
            self.assertIsNone(self.client.refresh_access_token(None))  # type: ignore
            self.assertIsNone(self.client.refresh_access_token(12345))  # type: ignore
            mock_urlopen.assert_not_called()

    @patch("urllib.request.urlopen")
    def test_refresh_access_token_non_finite_and_bool_expires_in(self, mock_urlopen):
        """refresh_access_token handles inf, -inf, nan, and bool without crashing, defaulting to 3600."""
        for tricky_exp in ["inf", "-inf", float("inf"), float("-inf"), float("nan"), True, False]:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps({
                "access_token": "ya29.tricky",
                "expires_in": tricky_exp,
            }).encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp
            mock_urlopen.return_value = mock_resp

            res = self.client.refresh_access_token(self.refresh_token)
            self.assertIsNotNone(res)
            self.assertEqual(res["expires_in"], 3600, f"Failed for expires_in={tricky_exp}")

    @patch("urllib.request.urlopen")
    def test_fetch_quota_dict_access_token_at_entry(self, mock_urlopen):
        """fetch_quota unpacks access_token if passed as a dictionary at function entry."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(self.mock_quota_resp).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        res = self.client.fetch_quota({"access_token": "ya29.entry_dict_token"})
        self.assertEqual(res, self.expected_parsed_resp)

        req_arg = mock_urlopen.call_args[0][0]
        self.assertEqual(req_arg.headers.get("Authorization"), "Bearer ya29.entry_dict_token")

    @patch("urllib.request.urlopen")
    def test_fetch_quota_unicode_decode_error_fails_gracefully(self, mock_urlopen):
        """fetch_quota returns empty dict on UnicodeDecodeError without crashing."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"\xff\xfe\x00\x00"  # Invalid utf-8
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        res = self.client.fetch_quota(self.access_token)
        self.assertEqual(res, {})

    def test_save_cache_with_evict_keys(self):
        """save_cache permanently evicts specified accounts from disk and memory."""
        # Initial save with 2 accounts
        initial_data = {
            "timestamp": 1000.0,
            "accounts": {
                "acc1": {"cached_at": 1000.0, "gemini": {"quota": 80, "reset_time": 1700000000}},
                "acc2": {"cached_at": 1000.0, "gemini": {"quota": 50, "reset_time": 1700000000}},
            }
        }
        self.client.save_cache(initial_data, merge=False)
        loaded = self.client.load_cache()
        self.assertIn("acc1", loaded.get("accounts", {}))
        self.assertIn("acc2", loaded.get("accounts", {}))

        # Evict acc2 with merge=True
        self.client.save_cache(
            {"accounts": {"acc1": {"cached_at": 1100.0, "gemini": {"quota": 85, "reset_time": 1700000000}}}},
            merge=True,
            evict_keys=["acc2"],
        )
        loaded_after_evict = self.client.load_cache()
        self.assertIn("acc1", loaded_after_evict.get("accounts", {}))
        self.assertNotIn("acc2", loaded_after_evict.get("accounts", {}))

        # Evict acc1 with merge=False
        self.client.save_cache(
            {"accounts": {"acc1": {"cached_at": 1200.0, "gemini": {"quota": 90, "reset_time": 1700000000}}}},
            merge=False,
            evict_keys=["acc1"],
        )
        loaded_empty = self.client.load_cache()
        self.assertNotIn("acc1", loaded_empty.get("accounts", {}))

    def test_error_classification_authoritative_nameserver_is_network_not_auth(self):
        """Authoritative nameserver unreachable is classified as network, not auth."""
        err_msg = "Authoritative nameserver unreachable"
        self.assertFalse(_is_auth_error(err_msg))
        self.assertTrue(_is_network_error(err_msg))

        # Dict with error_type
        self.assertFalse(_is_auth_error({"error": err_msg, "error_type": "network"}))
        self.assertTrue(_is_network_error({"error": err_msg, "error_type": "network"}))

        # Auth error classification
        auth_msg = "Token refresh rejected"
        self.assertTrue(_is_auth_error(auth_msg))
        self.assertFalse(_is_network_error(auth_msg))

    def test_quota_client_thread_local_error_isolation(self):
        """QuotaClient last_error and last_error_type maintain isolated thread-local state."""
        import threading

        client = QuotaClient(timeout=1.0, cache_dir=self.cache_dir)
        results = {}

        def worker_auth():
            client.last_error = "HTTP Error 401: Unauthorized"
            client.last_error_type = "auth"
            time.sleep(0.01)
            results["auth"] = (client.last_error, client.last_error_type)

        def worker_net():
            client.last_error = "Timed out"
            client.last_error_type = "network"
            time.sleep(0.01)
            results["net"] = (client.last_error, client.last_error_type)

        t1 = threading.Thread(target=worker_auth)
        t2 = threading.Thread(target=worker_net)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(results["auth"], ("HTTP Error 401: Unauthorized", "auth"))
        self.assertEqual(results["net"], ("Timed out", "network"))

    def test_fetch_all_accounts_quota_concurrent_error_type_isolation(self):
        """fetch_all_accounts_quota isolates error types across concurrent worker threads."""
        mock_engine = MagicMock()
        mock_engine.get_account_tokens.side_effect = lambda name: {
            "access_token": f"tok_{name}",
            "refresh_token": f"ref_{name}",
        }

        client = QuotaClient(timeout=1.0, cache_dir=self.cache_dir)

        def fake_fetch_quota(tok, refresh_token=None, on_refresh=None):
            if "auth_fail" in tok:
                client.last_error = "HTTP Error 401: Unauthorized"
                client.last_error_type = "auth"
                return {}
            elif "net_fail" in tok:
                client.last_error = "Connection reset by peer"
                client.last_error_type = "network"
                return {}
            return {"gemini": {"quota": 80, "reset_time": 1700000000}}

        with patch.object(client, "fetch_quota", side_effect=fake_fetch_quota):
            accounts = [
                {"name": "acc_auth_fail"},
                {"name": "acc_net_fail"},
                {"name": "acc_success"},
            ]
            results = fetch_all_accounts_quota(mock_engine, accounts, client=client, max_workers=3)

        self.assertEqual(results["acc_auth_fail"].get("error_type"), "auth")
        self.assertEqual(results["acc_net_fail"].get("error_type"), "network")
        self.assertIn("gemini", results["acc_success"])


if __name__ == "__main__":
    unittest.main()

