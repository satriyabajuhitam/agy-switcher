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
        self.expected_parsed_resp = {"gemini": {"quota": 80, "reset_time": 1791201600}}

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
        self.assertEqual(res, {"gemini": {"quota": 75, "reset_time": 1791201600}})

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


if __name__ == "__main__":
    unittest.main()

