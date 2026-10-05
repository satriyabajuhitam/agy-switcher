"""Fast Quota Telemetry Client for Antigravity."""

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_CLIENT_ID = "32555940559.apps.googleusercontent.com"
DEFAULT_CLIENT_SECRET = "ZmssLNjJy2998hD4CTg2ejr2"

class QuotaClient:
    def __init__(
        self,
        client_id: str = DEFAULT_CLIENT_ID,
        client_secret: str = DEFAULT_CLIENT_SECRET,
        timeout: float = 2.0,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout = timeout
        self.token_endpoint = "https://oauth2.googleapis.com/token"
        self.quota_endpoint = "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary"

    def refresh_access_token(self, refresh_token: str) -> str | None:
        """Refreshes the OAuth access token."""
        data = urllib.parse.urlencode({
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }).encode("utf-8")

        req = urllib.request.Request(self.token_endpoint, data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
                token_data = json.loads(body)
                return token_data.get("access_token")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            print(f"[QuotaClient] Failed to refresh token: {e}", file=sys.stderr)
            return None

    def fetch_quota(self, access_token: str, refresh_token: str | None = None) -> dict[str, Any]:
        """Fetches quota from the internal CloudCode API."""
        req = urllib.request.Request(
            self.quota_endpoint,
            data=b'{"project": ""}',
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "User-Agent": "Antigravity/2.5.5",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body)
        except urllib.error.HTTPError as e:
            if e.code == 401 and refresh_token:
                # Try refreshing the token
                new_access_token = self.refresh_access_token(refresh_token)
                if new_access_token:
                    # Retry fetch with new token (prevent infinite loop)
                    return self.fetch_quota(new_access_token, refresh_token=None)
            print(f"[QuotaClient] HTTP Error {e.code}: {e.reason}", file=sys.stderr)
            return {}
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            print(f"[QuotaClient] Network or Parse Error: {e}", file=sys.stderr)
            return {}
