"""Tests for Fast Quota Telemetry Client."""

import json
import os
import stat
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from agy_switch import QuotaClient


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
        self.assertEqual(loaded, test_data)

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


if __name__ == "__main__":
    unittest.main()

