"""Tests for Fast Quota Telemetry Client."""

import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from quota_client import QuotaClient


class TestQuotaClient(unittest.TestCase):
    def setUp(self):
        self.client = QuotaClient(timeout=0.1)
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

    @patch("urllib.request.urlopen")
    def test_fetch_quota_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(self.mock_quota_resp).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        res = self.client.fetch_quota(self.access_token)
        self.assertEqual(res, self.mock_quota_resp)

        req_arg = mock_urlopen.call_args[0][0]
        self.assertEqual(req_arg.full_url, self.client.quota_endpoint)
        self.assertEqual(req_arg.headers.get("Authorization"), f"Bearer {self.access_token}")

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
        self.assertEqual(res, self.mock_quota_resp)
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


if __name__ == "__main__":
    unittest.main()
