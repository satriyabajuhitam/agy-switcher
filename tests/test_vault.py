"""Unit tests for AgySwitchEngine vault operations and Token Identity."""

from __future__ import annotations

import base64
import email.message
import io
import itertools
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import cast
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from agy_switcher.exceptions import (
    AccountAuthError,
    AccountNotFoundError,
    ActiveTurnRunningError,
    InvalidTokenError,
)
from agy_switcher.vault import AgySwitchEngine, AgySwitchError


def make_valid_token_payload(email: str = "remote@example.com") -> dict[str, object]:
    """Generates a valid Google OAuth token structure with standard JWT."""
    payload_json = json.dumps({"email": email, "sub": "12345", "aud": "test_client"}).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(payload_json).decode("utf-8").rstrip("=")
    return {
        "token": {
            "access_token": f"ya29.sample_token_for_{email}",
            "token_type": "Bearer",
            "refresh_token": f"1//sample_refresh_for_{email}",
            "expiry": "2026-12-31T23:59:59Z",
        },
        "auth_method": "oauth",
        "id_token": f"header.{payload_b64}.signature",
    }


def make_token_payload_without_identity(
    access_token: str = "ya29.no_identity_access_token",
) -> dict[str, object]:
    """Generates a valid agy 1.5.10 OAuth token lacking both id_token and id_token_email."""
    return {
        "token": {
            "access_token": access_token,
            "token_type": "Bearer",
            "refresh_token": "1//no_identity_refresh_token",
            "expiry": "2026-12-31T23:59:59Z",
        },
        "auth_method": "oauth",
    }


class _MockHTTPResponse:
    """Minimal context-manager HTTP response stub for urllib.request.urlopen."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _MockHTTPResponse:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


class _StubLoginProcess:
    """Minimal subprocess stub for mocked login_account runs."""

    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode

    def poll(self) -> int:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def terminate(self) -> None:
        return None

    def kill(self) -> None:
        return None


class TestAgySwitchEngine(unittest.TestCase):
    """Test suite for AgySwitchEngine covering vault operations and Token Identity."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)
        self.vault_dir = self.base_path / ".antigravity-vault"
        self.gemini_dir = self.base_path / ".gemini"
        self.legacy_root = self.base_path / ".agy-cli-manager"

        self.vault_dir.mkdir(parents=True, mode=0o700)
        self.gemini_dir.mkdir(parents=True, mode=0o700)
        self.legacy_root.mkdir(parents=True, mode=0o700)

        self.engine = AgySwitchEngine(
            vault_dir=self.vault_dir,
            gemini_dir=self.gemini_dir,
            legacy_root=self.legacy_root,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_oob_oauth_retired_and_vault_structure(self) -> None:
        """[OOB-RETIRE-003] Verifies OOB OAuth builders are retired while DEFAULT_CLIENT_ID remains."""
        import agy_switcher.telemetry as telemetry_mod
        from agy_switcher.telemetry import DEFAULT_CLIENT_ID

        self.assertFalse(hasattr(self.engine, "get_oauth_url"))
        self.assertFalse(hasattr(telemetry_mod, "build_oauth_url"))
        self.assertTrue(bool(DEFAULT_CLIENT_ID))
        self.assertFalse(hasattr(self.engine, "vault"))

    def test_retired_headless_symbols_absent_from_vault(self) -> None:
        """[TTY-LOGIN-002] Retired headless login methods, stream-scraping helpers, and regex/constants are absent from vault."""
        import agy_switcher.vault as vault_mod

        retired_symbols = (
            "login_headless",
            "_run_headless_agy_session",
            "_finalize_missing_pkce_url",
            "_wait_for_pkce_url",
            "_prompt_and_forward_auth_code",
            "_wait_for_subprocess_completion",
            "_extract_pkce_oauth_url",
            "_StreamEventQueue",
            "_stream_reader",
            "_resolve_isolated_pgid",
            "_signal_process_or_group",
            "_terminate_process",
            "_close_process_streams",
            "_consume_stream_event",
            "_drain_stream_queue",
            "_extract_pkce_url_from_streams",
            "_poll_and_drain_streams",
            "_get_returncode",
            "_ANSI_ESCAPE_RE",
            "_GOOGLE_OAUTH_URL_RE",
            "_PKCE_REDIRECT_URI_MARKER",
        )
        for symbol in retired_symbols:
            with self.subTest(symbol=symbol):
                self.assertFalse(hasattr(AgySwitchEngine, symbol), f"AgySwitchEngine still defines {symbol}")
                self.assertFalse(hasattr(self.engine, symbol), f"engine instance still exposes {symbol}")
                self.assertFalse(hasattr(vault_mod, symbol), f"agy_switcher.vault still defines {symbol}")

    def _run_registration_path(
        self,
        method: str,
        alias: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        """Executes add_account or login_account with subprocess mocked."""
        if method == "add_account":
            src_token = self.base_path / f"{alias}_src_token.json"
            src_token.write_text(json.dumps(payload), encoding="utf-8")
            return self.engine.add_account(alias, src_token)

        def mock_popen(args: list[str], **kwargs: object) -> _StubLoginProcess:
            env = kwargs.get("env")
            assert isinstance(env, dict)
            temp_home = Path(env["HOME"])
            token_target = temp_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
            token_target.parent.mkdir(parents=True, exist_ok=True)
            token_target.write_text(json.dumps(payload), encoding="utf-8")
            return _StubLoginProcess(returncode=0)

        with patch.object(self.engine, "resolve_agy_binary", return_value="/bin/mock_agy"), \
             patch("subprocess.Popen", side_effect=mock_popen):
            return self.engine.login_account(alias, force=True)

    def test_add_account_and_login_userinfo_persists_id_token_email(self) -> None:
        """[IDENTITY-RES-002] Missing id_token/id_token_email resolves email via Google UserInfo and writes 0600 id_token_email."""
        self.assertFalse(hasattr(self.engine, "login_headless"))
        self.assertFalse(hasattr(AgySwitchEngine, "login_headless"))
        for method in ("add_account", "login_account"):
            with self.subTest(method=method):
                alias = f"ui_{method}"
                access_token = f"ya29.userinfo_tok_{method}"
                payload = make_token_payload_without_identity(access_token=access_token)
                captured_requests: list[tuple[urllib.request.Request, object]] = []

                def mock_urlopen(req: urllib.request.Request, timeout: object = None) -> _MockHTTPResponse:
                    captured_requests.append((req, timeout))
                    return _MockHTTPResponse(b'{"email": "userinfo@example.com", "verified_email": true}')

                with patch("urllib.request.urlopen", side_effect=mock_urlopen):
                    result = self._run_registration_path(method, alias, payload)

                self.assertEqual(len(captured_requests), 1, f"Expected 1 UserInfo call for {method}")
                req, timeout = captured_requests[0]
                self.assertEqual(req.get_method(), "GET")
                self.assertIn("googleapis.com", req.full_url)
                self.assertIn("userinfo", req.full_url)
                self.assertEqual(req.get_header("Authorization"), f"Bearer {access_token}")
                self.assertIsInstance(timeout, (int, float))
                assert isinstance(timeout, (int, float))
                self.assertGreater(timeout, 0.0)

                token_path = self.vault_dir / "accounts" / alias / "token.json"
                self.assertTrue(token_path.is_file())
                self.assertEqual(stat.S_IMODE(token_path.stat().st_mode), 0o600)
                saved = json.loads(token_path.read_text(encoding="utf-8"))
                self.assertEqual(saved.get("id_token_email"), "userinfo@example.com")

                if method == "add_account":
                    self.assertEqual(result, {"name": alias, "email": "userinfo@example.com"})
                else:
                    self.assertEqual(
                        result,
                        {"name": alias, "email": "userinfo@example.com", "status": "standby"},
                    )

    def test_add_account_and_login_skip_userinfo_when_id_token_email_present(self) -> None:
        """Registration resolves existing id_token_email without calling Google UserInfo."""
        for method in ("add_account", "login_account"):
            with self.subTest(method=method):
                alias = f"existing_email_{method}"
                payload = make_token_payload_without_identity()
                payload["id_token_email"] = "  preset@example.com  "

                with patch("urllib.request.urlopen") as mock_urlopen:
                    result = self._run_registration_path(method, alias, payload)

                mock_urlopen.assert_not_called()
                self.assertEqual(result.get("email"), "preset@example.com")

    def test_add_account_and_login_userinfo_errors_degrade_gracefully(self) -> None:
        """[IDENTITY-ERR-001] URLError, TimeoutError, and HTTP 503 degrade gracefully to email=None without aborting registration."""
        hdrs = email.message.Message()
        errors: list[tuple[str, Exception]] = [
            ("url_error", urllib.error.URLError("Network unreachable")),
            ("timeout_error", TimeoutError("Connection timed out")),
            (
                "http_503",
                urllib.error.HTTPError(
                    "https://www.googleapis.com/oauth2/v2/userinfo",
                    503,
                    "Service Unavailable",
                    hdrs,
                    io.BytesIO(b"Service Unavailable"),
                ),
            ),
        ]

        for err_label, err in errors:
            for method in ("add_account", "login_account"):
                with self.subTest(err=err_label, method=method):
                    alias = f"err_{err_label}_{method}"
                    payload = make_token_payload_without_identity()

                    with patch("urllib.request.urlopen", side_effect=err) as mock_urlopen:
                        result = self._run_registration_path(method, alias, payload)

                    self.assertEqual(mock_urlopen.call_count, 1)
                    token_path = self.vault_dir / "accounts" / alias / "token.json"
                    self.assertTrue(token_path.is_file())
                    self.assertEqual(stat.S_IMODE(token_path.stat().st_mode), 0o600)
                    saved = json.loads(token_path.read_text(encoding="utf-8"))
                    self.assertNotIn("id_token_email", saved)
                    expected_token = payload["token"]
                    assert isinstance(expected_token, dict)
                    self.assertEqual(saved["token"]["access_token"], expected_token["access_token"])

                    if method == "add_account":
                        self.assertEqual(result, {"name": alias, "email": None})
                    else:
                        self.assertEqual(result, {"name": alias, "email": None, "status": "standby"})

    def test_add_account_and_login_skip_userinfo_when_id_token_has_email(self) -> None:
        """Registration resolves existing id_token JWT email without calling Google UserInfo."""
        for method in ("add_account", "login_account"):
            with self.subTest(method=method):
                alias = f"jwt_email_{method}"
                payload = make_valid_token_payload("jwt_user@example.com")

                with patch("urllib.request.urlopen") as mock_urlopen:
                    result = self._run_registration_path(method, alias, payload)

                mock_urlopen.assert_not_called()
                token_path = self.vault_dir / "accounts" / alias / "token.json"
                self.assertEqual(stat.S_IMODE(token_path.stat().st_mode), 0o600)
                saved = json.loads(token_path.read_text(encoding="utf-8"))
                self.assertNotIn("id_token_email", saved)
                if method == "add_account":
                    self.assertEqual(result, {"name": alias, "email": "jwt_user@example.com"})
                else:
                    self.assertEqual(
                        result,
                        {"name": alias, "email": "jwt_user@example.com", "status": "standby"},
                    )

    def test_add_account_and_login_userinfo_fallback_when_jwt_lacks_email_claim(self) -> None:
        """Valid JWT without email claim falls back to Google UserInfo lookup during registration."""
        no_email_jwt_payload = base64.urlsafe_b64encode(b'{"sub":"no_email_user"}').decode("utf-8").rstrip("=")
        for method in ("add_account", "login_account"):
            with self.subTest(method=method):
                alias = f"jwt_no_email_{method}"
                payload = make_token_payload_without_identity(access_token=f"ya29.jwt_no_email_{method}")
                payload["id_token"] = f"header.{no_email_jwt_payload}.signature"

                with patch(
                    "urllib.request.urlopen",
                    return_value=_MockHTTPResponse(b'{"email": "from_userinfo@example.com"}'),
                ) as mock_urlopen:
                    result = self._run_registration_path(method, alias, payload)

                mock_urlopen.assert_called_once()
                token_path = self.vault_dir / "accounts" / alias / "token.json"
                self.assertEqual(stat.S_IMODE(token_path.stat().st_mode), 0o600)
                saved = json.loads(token_path.read_text(encoding="utf-8"))
                self.assertEqual(saved.get("id_token_email"), "from_userinfo@example.com")
                if method == "add_account":
                    self.assertEqual(result, {"name": alias, "email": "from_userinfo@example.com"})
                else:
                    self.assertEqual(
                        result,
                        {"name": alias, "email": "from_userinfo@example.com", "status": "standby"},
                    )

    def test_add_account_and_login_userinfo_incomplete_read_and_control_char_token_degrade(self) -> None:
        """http.client.IncompleteRead and control-char access_token degrade cleanly without aborting or leaking tokens."""
        import http.client

        for method in ("add_account", "login_account"):
            with self.subTest(case="incomplete_read", method=method):
                alias = f"incomplete_{method}"
                secret_tok = f"ya29.secret_tok_{method}"
                payload = make_token_payload_without_identity(access_token=secret_tok)

                class _IncompleteResp:
                    def read(self) -> bytes:
                        raise http.client.IncompleteRead(b'{"email": "half')

                    def __enter__(self) -> _IncompleteResp:
                        return self

                    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
                        return None

                with patch("urllib.request.urlopen", return_value=_IncompleteResp()) as mock_urlopen:
                    result = self._run_registration_path(method, alias, payload)

                mock_urlopen.assert_called_once()
                token_path = self.vault_dir / "accounts" / alias / "token.json"
                self.assertEqual(stat.S_IMODE(token_path.stat().st_mode), 0o600)
                saved = json.loads(token_path.read_text(encoding="utf-8"))
                self.assertNotIn("id_token_email", saved)
                self.assertNotIn(secret_tok, repr(result))
                if method == "add_account":
                    self.assertEqual(result, {"name": alias, "email": None})
                else:
                    self.assertEqual(result, {"name": alias, "email": None, "status": "standby"})

        ctrl_payload = make_token_payload_without_identity(access_token="ya29.bad\r\nX-Injected: 1")
        with patch("urllib.request.urlopen") as mock_urlopen:
            result = self._run_registration_path("add_account", "ctrl_tok_acc", ctrl_payload)
        mock_urlopen.assert_not_called()
        self.assertEqual(result, {"name": "ctrl_tok_acc", "email": None})

    def test_add_account_and_login_userinfo_http_401_500_non_json_and_whitespace_email(self) -> None:
        """[IDENTITY-ERR-002] UserInfo 401/500, non-JSON HTML, malformed payloads, and invalid id_token_email degrade cleanly."""
        class _StatusHTTPResponse(_MockHTTPResponse):
            def __init__(self, body: bytes, status: int) -> None:
                super().__init__(body)
                self.status = status

        class _GetCodeHTTPResponse(_MockHTTPResponse):
            def __init__(self, body: bytes, status: int) -> None:
                super().__init__(body)
                self._status = status

            def getcode(self) -> int:
                return self._status

        hdrs = email.message.Message()
        cases: list[tuple[str, object]] = [
            (
                "http_401",
                urllib.error.HTTPError(
                    "https://www.googleapis.com/oauth2/v2/userinfo",
                    401,
                    "Unauthorized",
                    hdrs,
                    io.BytesIO(b"Unauthorized"),
                ),
            ),
            (
                "http_500",
                urllib.error.HTTPError(
                    "https://www.googleapis.com/oauth2/v2/userinfo",
                    500,
                    "Internal Server Error",
                    hdrs,
                    io.BytesIO(b"Internal Server Error"),
                ),
            ),
            ("status_401", _StatusHTTPResponse(b'{"email": "should_not_use@example.com"}', 401)),
            ("status_500", _StatusHTTPResponse(b'{"email": "should_not_use@example.com"}', 500)),
            ("getcode_401", _GetCodeHTTPResponse(b'{"email": "should_not_use@example.com"}', 401)),
            ("getcode_500", _GetCodeHTTPResponse(b'{"email": "should_not_use@example.com"}', 500)),
            ("non_json_html", _MockHTTPResponse(b"<html>error</html>")),
            ("whitespace_email", _MockHTTPResponse(b'{"email": "   "}')),
            ("non_str_email_int", _MockHTTPResponse(b'{"email": 123}')),
            ("non_str_email_null", _MockHTTPResponse(b'{"email": null}')),
            ("json_list", _MockHTTPResponse(b'["a@example.com"]')),
            ("invalid_utf8_in_json", _MockHTTPResponse(b'{"email": "bad\xff@example.com"}')),
            ("invalid_utf8_bytes", _MockHTTPResponse(b"\xff\xfe")),
        ]

        for case_label, outcome in cases:
            for method in ("add_account", "login_account"):
                with self.subTest(case=case_label, method=method):
                    alias = f"err2_{case_label}_{method}"
                    payload = make_token_payload_without_identity(
                        access_token=f"ya29.{case_label}_{method}",
                    )
                    payload["id_token_email"] = "   "

                    if isinstance(outcome, Exception):
                        cm = patch("urllib.request.urlopen", side_effect=outcome)
                    else:
                        cm = patch("urllib.request.urlopen", return_value=outcome)

                    with cm as mock_urlopen:
                        result = self._run_registration_path(method, alias, payload)

                    self.assertEqual(mock_urlopen.call_count, 1)
                    token_path = self.vault_dir / "accounts" / alias / "token.json"
                    self.assertTrue(token_path.is_file())
                    self.assertEqual(stat.S_IMODE(token_path.stat().st_mode), 0o600)
                    saved = json.loads(token_path.read_text(encoding="utf-8"))
                    self.assertNotIn("id_token_email", saved)

                    if method == "add_account":
                        self.assertEqual(result, {"name": alias, "email": None})
                    else:
                        self.assertEqual(result, {"name": alias, "email": None, "status": "standby"})

        # Pre-populated invalid id_token_email values are stripped across all registration methods
        invalid_presets: list[tuple[str, object]] = [
            ("ws", "   "),
            ("int", 123),
            ("none", None),
            ("ctrl", "bad\n@example.com"),
        ]
        for label, bad_preset in invalid_presets:
            for method in ("add_account", "login_account"):
                with self.subTest(preset=label, method=method):
                    alias = f"bad_preset_{label}_{method}"
                    payload = make_token_payload_without_identity(access_token=f"ya29.{alias}")
                    payload["id_token_email"] = bad_preset
                    with patch("urllib.request.urlopen", return_value=_MockHTTPResponse(b"<html>error</html>")):
                        result = self._run_registration_path(method, alias, payload)
                    token_path = self.vault_dir / "accounts" / alias / "token.json"
                    self.assertEqual(stat.S_IMODE(token_path.stat().st_mode), 0o600)
                    saved = json.loads(token_path.read_text(encoding="utf-8"))
                    self.assertNotIn("id_token_email", saved)
                    if method == "add_account":
                        self.assertEqual(result, {"name": alias, "email": None})
                    else:
                        self.assertEqual(result, {"name": alias, "email": None, "status": "standby"})

    def test_fetch_google_userinfo_email_rejects_invalid_url_scheme_and_netloc(self) -> None:
        """_fetch_google_userinfo_email rejects non-HTTP(S) schemes or empty netloc before urlopen."""
        from agy_switcher import vault as vault_mod

        for bad_url in ("file:///etc/passwd", "ftp://example.com/userinfo", "https://", "/relative/path"):
            with self.subTest(bad_url=bad_url):
                with patch.object(vault_mod, "GOOGLE_USERINFO_ENDPOINT", bad_url), \
                     patch("urllib.request.urlopen") as mock_urlopen:
                    with self.assertRaises(ValueError):
                        vault_mod._fetch_google_userinfo_email("ya29.valid_token")
                mock_urlopen.assert_not_called()

    def test_harvest_live_tokens_and_switch_tokens_preserve_id_token_email(self) -> None:
        """[IDENTITY-EDGE-002] harvest_live_tokens_if_needed and switch_tokens derive alias and preserve id_token_email atomically."""
        for source_kind in ("desktop", "cli"):
            with self.subTest(source=source_kind):
                with tempfile.TemporaryDirectory() as tmp_dir:
                    base = Path(tmp_dir)
                    vault_dir = base / ".antigravity-vault"
                    gemini_dir = base / ".gemini"
                    legacy_root = base / ".agy-cli-manager"
                    proc_dir = base / "proc"
                    proc_dir.mkdir(parents=True, mode=0o700)
                    engine = AgySwitchEngine(
                        vault_dir=vault_dir,
                        gemini_dir=gemini_dir,
                        legacy_root=legacy_root,
                        proc_dir=proc_dir,
                    )

                    live_payload = make_token_payload_without_identity(
                        access_token=f"ya29.harvest_{source_kind}",
                    )
                    live_payload["id_token_email"] = "harvest@example.com"

                    live_file = engine.desktop_token_file if source_kind == "desktop" else engine.cli_token_file
                    live_file.parent.mkdir(parents=True, exist_ok=True)
                    live_file.write_text(json.dumps(live_payload), encoding="utf-8")

                    harvested_alias = engine.harvest_live_tokens_if_needed()
                    self.assertEqual(harvested_alias, "harvest")
                    self.assertEqual(engine.get_active_name(), "harvest")

                    vault_token = vault_dir / "accounts" / "harvest" / "token.json"
                    self.assertTrue(vault_token.is_file())
                    self.assertEqual(stat.S_IMODE(vault_token.stat().st_mode), 0o600)
                    self.assertEqual(
                        json.loads(vault_token.read_text(encoding="utf-8")).get("id_token_email"),
                        "harvest@example.com",
                    )

                    swap_res = engine.switch_tokens("harvest", restart_server=False)
                    self.assertEqual(swap_res["active"], "harvest")
                    self.assertEqual(swap_res["email"], "harvest@example.com")
                    self.assertFalse(swap_res["server_restarted"])

                    for target_file in (vault_token, engine.desktop_token_file, engine.cli_token_file):
                        self.assertTrue(target_file.is_file())
                        self.assertEqual(stat.S_IMODE(target_file.stat().st_mode), 0o600)
                        loaded = json.loads(target_file.read_text(encoding="utf-8"))
                        self.assertEqual(loaded.get("id_token_email"), "harvest@example.com")

                    # Subsequent harvest on non-empty vault (when "harvest" already exists) is a no-op
                    self.assertIsNone(engine.harvest_live_tokens_if_needed())

        # Valid uppercase/dot local-part derives alias; invalid local-part chars fall back safely to "primary"
        alias_cases = [
            ("Harvest.User@example.com", "Harvest.User"),
            ("Harvest.User+1@example.com", "primary"),
            ("   ", "primary"),
            ("../evil@example.com", "primary"),
            (".@example.com", "primary"),
        ]
        for candidate_email, expected_alias in alias_cases:
            with self.subTest(candidate_email=candidate_email):
                with tempfile.TemporaryDirectory() as tmp_dir:
                    base = Path(tmp_dir)
                    engine = AgySwitchEngine(
                        vault_dir=base / ".antigravity-vault",
                        gemini_dir=base / ".gemini",
                        legacy_root=base / ".agy-cli-manager",
                        proc_dir=base / "proc",
                    )
                    live_payload = make_token_payload_without_identity()
                    live_payload["id_token_email"] = candidate_email
                    engine.desktop_token_file.parent.mkdir(parents=True, exist_ok=True)
                    engine.desktop_token_file.write_text(json.dumps(live_payload), encoding="utf-8")
                    self.assertEqual(engine.harvest_live_tokens_if_needed(), expected_alias)

        # Corrupt desktop token falls back to valid CLI token and preserves id_token_email
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            engine = AgySwitchEngine(
                vault_dir=base / ".antigravity-vault",
                gemini_dir=base / ".gemini",
                legacy_root=base / ".agy-cli-manager",
                proc_dir=base / "proc",
            )
            engine.desktop_token_file.parent.mkdir(parents=True, exist_ok=True)
            engine.desktop_token_file.write_text("{corrupt-json", encoding="utf-8")
            cli_payload = make_token_payload_without_identity(access_token="ya29.cli_fallback")
            cli_payload["id_token_email"] = "harvest@example.com"
            engine.cli_token_file.parent.mkdir(parents=True, exist_ok=True)
            engine.cli_token_file.write_text(json.dumps(cli_payload), encoding="utf-8")
            self.assertEqual(engine.harvest_live_tokens_if_needed(), "harvest")
            saved_cli = json.loads(
                (engine.accounts_dir / "harvest" / "token.json").read_text(encoding="utf-8")
            )
            self.assertEqual(saved_cli.get("id_token_email"), "harvest@example.com")

    def test_read_surfaces_resolve_id_token_email(self) -> None:
        """[IDENTITY-EDGE-001] list_accounts, switch_tokens, and get_status resolve id_token_email cleanly."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            engine = AgySwitchEngine(
                vault_dir=base / ".antigravity-vault",
                gemini_dir=base / ".gemini",
                legacy_root=base / ".agy-cli-manager",
                proc_dir=base / "proc",
            )

            work_dir = engine.accounts_dir / "work"
            work_dir.mkdir(parents=True, exist_ok=True)
            work_payload = make_token_payload_without_identity(access_token="ya29.work")
            work_payload["id_token_email"] = "work@example.com"
            (work_dir / "token.json").write_text(json.dumps(work_payload), encoding="utf-8")

            standby_dir = engine.accounts_dir / "standby"
            standby_dir.mkdir(parents=True, exist_ok=True)
            standby_payload = make_token_payload_without_identity(access_token="ya29.standby")
            standby_payload["id_token"] = ""
            standby_payload["id_token_email"] = "  standby@example.com  "
            (standby_dir / "token.json").write_text(json.dumps(standby_payload), encoding="utf-8")

            jwt_dir = engine.accounts_dir / "jwt_first"
            jwt_dir.mkdir(parents=True, exist_ok=True)
            jwt_payload = make_valid_token_payload("jwt@example.com")
            jwt_payload["id_token_email"] = "legacy@example.com"
            (jwt_dir / "token.json").write_text(json.dumps(jwt_payload), encoding="utf-8")

            engine.set_active_name("work")

            listed = engine.list_accounts()
            self.assertEqual(listed["active"], "work")
            by_name = {entry["name"]: entry for entry in listed["accounts"]}
            self.assertEqual(by_name["work"]["email"], "work@example.com")
            self.assertTrue(by_name["work"]["valid"])
            self.assertTrue(by_name["work"]["is_active"])
            self.assertEqual(by_name["standby"]["email"], "standby@example.com")
            self.assertTrue(by_name["standby"]["valid"])
            self.assertFalse(by_name["standby"]["is_active"])
            self.assertEqual(by_name["jwt_first"]["email"], "jwt@example.com")

            status = engine.get_status()
            self.assertEqual(status["active"], "work")
            self.assertEqual(status["email"], "work@example.com")

            swap_standby = engine.switch_tokens("standby", restart_server=False)
            self.assertEqual(swap_standby["active"], "standby")
            self.assertEqual(swap_standby["email"], "standby@example.com")
            self.assertEqual(engine.get_status()["email"], "standby@example.com")

            swap_work = engine.switch_tokens("work", restart_server=False)
            self.assertEqual(swap_work["active"], "work")
            self.assertEqual(swap_work["email"], "work@example.com")
            for target_file in (engine.desktop_token_file, engine.cli_token_file):
                loaded = json.loads(target_file.read_text(encoding="utf-8"))
                self.assertEqual(loaded.get("id_token_email"), "work@example.com")

            # switch_account facade alias also resolves and preserves id_token_email
            swap_acc_res = engine.switch_account("standby", no_restart=True)
            self.assertEqual(swap_acc_res["active"], "standby")
            self.assertEqual(swap_acc_res["email"], "standby@example.com")
            for target_file in (engine.desktop_token_file, engine.cli_token_file):
                loaded = json.loads(target_file.read_text(encoding="utf-8"))
                self.assertEqual(loaded.get("id_token_email"), "  standby@example.com  ")

            # Whitespace-only, non-string, JWT-without-email-claim, and corrupt edge cases
            ws_dir = engine.accounts_dir / "ws_only"
            ws_dir.mkdir(parents=True, exist_ok=True)
            ws_payload = make_token_payload_without_identity(access_token="ya29.ws")
            ws_payload["id_token_email"] = "   "
            (ws_dir / "token.json").write_text(json.dumps(ws_payload), encoding="utf-8")

            non_str_dir = engine.accounts_dir / "non_str"
            non_str_dir.mkdir(parents=True, exist_ok=True)
            non_str_payload = make_token_payload_without_identity(access_token="ya29.nonstr")
            non_str_payload["id_token_email"] = 12345
            (non_str_dir / "token.json").write_text(json.dumps(non_str_payload), encoding="utf-8")

            no_claim_b64 = base64.urlsafe_b64encode(json.dumps({"sub": "u1"}).encode("utf-8")).decode("utf-8").rstrip("=")
            jwt_noclaim_dir = engine.accounts_dir / "jwt_noclaim"
            jwt_noclaim_dir.mkdir(parents=True, exist_ok=True)
            jwt_noclaim_payload = make_token_payload_without_identity(access_token="ya29.noclaim")
            jwt_noclaim_payload["id_token"] = f"header.{no_claim_b64}.sig"
            jwt_noclaim_payload["id_token_email"] = "fallback@example.com"
            (jwt_noclaim_dir / "token.json").write_text(json.dumps(jwt_noclaim_payload), encoding="utf-8")

            corrupt_dir = engine.accounts_dir / "corrupt_acc"
            corrupt_dir.mkdir(parents=True, exist_ok=True)
            (corrupt_dir / "token.json").write_text("{corrupt", encoding="utf-8")

            listed_edge = {entry["name"]: entry for entry in engine.list_accounts()["accounts"]}
            self.assertTrue(listed_edge["ws_only"]["valid"])
            self.assertIsNone(listed_edge["ws_only"]["email"])
            self.assertTrue(listed_edge["non_str"]["valid"])
            self.assertIsNone(listed_edge["non_str"]["email"])
            self.assertTrue(listed_edge["jwt_noclaim"]["valid"])
            self.assertEqual(listed_edge["jwt_noclaim"]["email"], "fallback@example.com")
            self.assertFalse(listed_edge["corrupt_acc"]["valid"])
            self.assertIsNone(listed_edge["corrupt_acc"]["email"])

            engine.set_active_name("non_str")
            self.assertIsNone(engine.get_status()["email"])
            engine.set_active_name("jwt_noclaim")
            self.assertEqual(engine.get_status()["email"], "fallback@example.com")
            engine.set_active_name("corrupt_acc")
            self.assertIsNone(engine.get_status()["email"])

    def test_auto_migrate_if_needed_preserves_id_token_email(self) -> None:
        """auto_migrate_if_needed preserves id_token_email for legacy and harvested accounts."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            engine = AgySwitchEngine(
                vault_dir=base / ".antigravity-vault",
                gemini_dir=base / ".gemini",
                legacy_root=base / ".agy-cli-manager",
                proc_dir=base / "proc",
            )
            legacy_token = (
                engine.legacy_root
                / "accounts"
                / "work"
                / ".gemini"
                / "antigravity-cli"
                / "antigravity-oauth-token"
            )
            legacy_token.parent.mkdir(parents=True, exist_ok=True)
            legacy_payload = make_token_payload_without_identity(access_token="ya29.legacy_work")
            legacy_payload["id_token_email"] = "work@example.com"
            legacy_token.write_text(json.dumps(legacy_payload), encoding="utf-8")
            (engine.legacy_root / "state.json").write_text(json.dumps({"active": "work"}), encoding="utf-8")

            migrated = engine.auto_migrate_if_needed()
            self.assertEqual(migrated, ["work"])
            self.assertEqual(engine.get_active_name(), "work")
            migrated_token = engine.accounts_dir / "work" / "token.json"
            self.assertEqual(stat.S_IMODE(migrated_token.stat().st_mode), 0o600)
            self.assertEqual(
                json.loads(migrated_token.read_text(encoding="utf-8")).get("id_token_email"),
                "work@example.com",
            )
            self.assertEqual(engine.get_status()["email"], "work@example.com")

        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            engine = AgySwitchEngine(
                vault_dir=base / ".antigravity-vault",
                gemini_dir=base / ".gemini",
                legacy_root=base / ".agy-cli-manager",
                proc_dir=base / "proc",
            )
            live_payload = make_token_payload_without_identity(access_token="ya29.live_work")
            live_payload["id_token_email"] = "work@example.com"
            (engine.legacy_root / "accounts").mkdir(parents=True, exist_ok=True)
            engine.desktop_token_file.parent.mkdir(parents=True, exist_ok=True)
            engine.desktop_token_file.write_text(json.dumps(live_payload), encoding="utf-8")

            migrated_live = engine.auto_migrate_if_needed()
            self.assertEqual(migrated_live, ["work"])
            self.assertEqual(engine.get_status()["email"], "work@example.com")

    def test_login_account_invalid_alias_rejected_before_prompt_or_spawn(self) -> None:
        """[TTY-LOGIN-ERR-001] Invalid aliases fail validate_account_alias before prompt, resolve_agy_binary, or Popen."""
        # Create sibling directory outside accounts/ so '../bad' would exist if unvalidated
        (self.vault_dir / "bad").mkdir(parents=True, exist_ok=True)
        existing_dir = self.engine.accounts_dir / "valid-acc"
        existing_dir.mkdir(parents=True, exist_ok=True)
        (existing_dir / "token.json").write_text('{"keep": true}', encoding="utf-8")

        invalid_aliases: list[object] = [
            "../bad",
            "..",
            ".",
            "my account",
            "",
            "   ",
            "\t\n",
            "a/b",
            "a\\b",
            "bad\x00acc",
            "a" * 65,
            " " + "a" * 65 + " ",
            None,
            123,
        ]
        for alias in invalid_aliases:
            for force in (False, True):
                with self.subTest(alias=alias, force=force):
                    prompt_calls: list[str] = []

                    def fail_if_prompted(msg: str) -> str:
                        prompt_calls.append(msg)
                        return "y"

                    with patch.object(
                        self.engine,
                        "validate_account_alias",
                        wraps=self.engine.validate_account_alias,
                    ) as spy_validate, \
                         patch("builtins.input") as mock_input, \
                         patch.object(self.engine, "resolve_agy_binary") as mock_resolve, \
                         patch("subprocess.Popen") as mock_popen:
                        with self.assertRaises(AgySwitchError):
                            self.engine.login_account(cast(str, alias), force=force, prompt_fn=fail_if_prompted)
                        if not force:
                            with self.assertRaises(AgySwitchError):
                                self.engine.login_account(cast(str, alias), force=False)

                    self.assertEqual(spy_validate.call_args_list[0].args, (alias,))
                    self.assertEqual(prompt_calls, [])
                    mock_input.assert_not_called()
                    mock_resolve.assert_not_called()
                    mock_popen.assert_not_called()

        self.assertEqual(list((self.vault_dir / "bad").iterdir()), [])
        self.assertEqual(sorted(p.name for p in self.engine.accounts_dir.iterdir()), ["valid-acc"])
        self.assertEqual((existing_dir / "token.json").read_text(encoding="utf-8"), '{"keep": true}')

    def test_login_account_whitespace_normalization_and_failed_force_overwrite(self) -> None:
        """[TTY-LOGIN-EDGE-002] Valid whitespace-padded alias normalizes and failed --force preserves prior token."""
        work_payload = make_valid_token_payload("work@example.com")

        def mock_popen_success(args: list[str], **kwargs: object) -> _StubLoginProcess:
            env = kwargs.get("env")
            assert isinstance(env, dict)
            temp_home = Path(env["HOME"])
            token_target = temp_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
            token_target.parent.mkdir(parents=True, exist_ok=True)
            token_target.write_text(json.dumps(work_payload), encoding="utf-8")
            return _StubLoginProcess(returncode=0)

        with patch.object(self.engine, "resolve_agy_binary", return_value="/bin/mock_agy"), \
             patch("subprocess.Popen", side_effect=mock_popen_success):
            result = self.engine.login_account(" work-acc ")

        self.assertEqual(result["name"], "work-acc")
        self.assertEqual(result["email"], "work@example.com")
        self.assertEqual(result["status"], "standby")
        work_token = self.vault_dir / "accounts" / "work-acc" / "token.json"
        self.assertTrue(work_token.is_file())
        self.assertEqual(stat.S_IMODE(work_token.stat().st_mode), 0o600)
        self.assertFalse((self.vault_dir / "accounts" / " work-acc ").exists())
        self.assertIsNone(self.engine.get_active_name())

        # Re-login with whitespace padding and force=False prompts with normalized alias
        seen_prompts: list[str] = []
        with self.assertRaises(AgySwitchError):
            self.engine.login_account(
                " work-acc ",
                force=False,
                prompt_fn=lambda msg: seen_prompts.append(msg) or "n",
            )
        self.assertEqual(len(seen_prompts), 1)
        self.assertIn("'work-acc'", seen_prompts[0])

        # Existing account prior token must remain intact when --force fails across all failure modes
        existing_dir = self.vault_dir / "accounts" / "existing-acc"
        existing_dir.mkdir(parents=True, exist_ok=True)
        existing_token = existing_dir / "token.json"
        prior_content = json.dumps(make_valid_token_payload("prior@example.com"))
        existing_token.write_text(prior_content, encoding="utf-8")
        existing_token.chmod(0o600)

        # Case A: agy exits 0 or non-zero without producing a token
        for exit_code in (0, 1):
            with patch.object(self.engine, "resolve_agy_binary", return_value="/bin/mock_agy"), \
                 patch("subprocess.Popen", return_value=_StubLoginProcess(returncode=exit_code)):
                with self.assertRaises(AgySwitchError):
                    self.engine.login_account("existing-acc", force=True)

            self.assertEqual(existing_token.read_text(encoding="utf-8"), prior_content)
            self.assertEqual([p.name for p in existing_dir.iterdir()], ["token.json"])

        # Case B: agy times out
        class _HangingProcess:
            def __init__(self) -> None:
                self.terminated = False

            def poll(self) -> int | None:
                return None

            def terminate(self) -> None:
                self.terminated = True

            def wait(self, timeout: float | None = None) -> int:
                return -15

            def kill(self) -> None:
                return None

        hanging_proc = _HangingProcess()
        with patch.object(self.engine, "resolve_agy_binary", return_value="/bin/mock_agy"), \
             patch("subprocess.Popen", return_value=hanging_proc), \
             patch("time.sleep", return_value=None), \
             patch("time.time", side_effect=itertools.chain([0.0], itertools.repeat(100.0))):
            with self.assertRaises(AgySwitchError):
                self.engine.login_account("existing-acc", timeout=5, force=True)

        self.assertTrue(hanging_proc.terminated)
        self.assertEqual(existing_token.read_text(encoding="utf-8"), prior_content)
        self.assertEqual([p.name for p in existing_dir.iterdir()], ["token.json"])

        # Case C: KeyboardInterrupt while agy is running
        interrupted_proc = _HangingProcess()
        with patch.object(self.engine, "resolve_agy_binary", return_value="/bin/mock_agy"), \
             patch("subprocess.Popen", return_value=interrupted_proc), \
             patch("time.sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(AgySwitchError):
                self.engine.login_account("existing-acc", force=True)

        self.assertTrue(interrupted_proc.terminated)
        self.assertEqual(existing_token.read_text(encoding="utf-8"), prior_content)
        self.assertEqual([p.name for p in existing_dir.iterdir()], ["token.json"])

        # Case D: agy produces corrupt token JSON
        def mock_popen_corrupt(args: list[str], **kwargs: object) -> _StubLoginProcess:
            env = kwargs.get("env")
            assert isinstance(env, dict)
            temp_home = Path(env["HOME"])
            token_target = temp_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
            token_target.parent.mkdir(parents=True, exist_ok=True)
            token_target.write_text("{not-valid-json", encoding="utf-8")
            return _StubLoginProcess(returncode=0)

        with patch.object(self.engine, "resolve_agy_binary", return_value="/bin/mock_agy"), \
             patch("subprocess.Popen", side_effect=mock_popen_corrupt):
            with self.assertRaises(AgySwitchError):
                self.engine.login_account("existing-acc", force=True)

        self.assertEqual(existing_token.read_text(encoding="utf-8"), prior_content)
        self.assertEqual([p.name for p in existing_dir.iterdir()], ["token.json"])

        # Case E: atomic replace fails during --force write (preserves prior token & cleans temp file)
        with patch.object(self.engine, "resolve_agy_binary", return_value="/bin/mock_agy"), \
             patch("subprocess.Popen", side_effect=mock_popen_success), \
             patch("agy_switcher.security.os.replace", side_effect=OSError("simulated replace failure")):
            with self.assertRaises(AgySwitchError) as err_ctx:
                self.engine.login_account("existing-acc", force=True)

        self.assertIsInstance(err_ctx.exception.__cause__, OSError)
        self.assertEqual(existing_token.read_text(encoding="utf-8"), prior_content)
        self.assertEqual([p.name for p in existing_dir.iterdir()], ["token.json"])

    def test_login_account_overwrite_lifecycle_and_jetski_fallback(self) -> None:
        """[TTY-LOGIN-EDGE-001] Overwrite prompt decline/EOF/KeyboardInterrupt aborts before agy spawn; confirm/force accepts jetski fallback."""
        existing_dir = self.vault_dir / "accounts" / "existing-acc"
        existing_dir.mkdir(parents=True, exist_ok=True)
        existing_token = existing_dir / "token.json"
        prior_content = json.dumps(make_valid_token_payload("prior@example.com"))
        existing_token.write_text(prior_content, encoding="utf-8")
        existing_token.chmod(0o600)

        decline_inputs: list[tuple[str, object]] = [
            ("n", "n"),
            ("empty", ""),
            ("eof", EOFError()),
            ("sigint", KeyboardInterrupt()),
        ]
        for label, side_eff in decline_inputs:
            with self.subTest(decline=label):
                with patch("builtins.input", side_effect=[side_eff]) as mock_input, \
                     patch.object(self.engine, "resolve_agy_binary") as mock_resolve, \
                     patch("subprocess.Popen") as mock_popen:
                    with self.assertRaises(AgySwitchError):
                        self.engine.login_account("existing-acc", force=False)

                mock_input.assert_called_once()
                mock_resolve.assert_not_called()
                mock_popen.assert_not_called()
                self.assertEqual(existing_token.read_text(encoding="utf-8"), prior_content)
                self.assertEqual(stat.S_IMODE(existing_token.stat().st_mode), 0o600)

        jetski_confirm_payload = make_valid_token_payload("confirmed-jetski@example.com")
        jetski_force_payload = make_valid_token_payload("forced-jetski@example.com")

        def make_jetski_popen(payload: dict[str, object]) -> object:
            def _mock_popen(args: list[str], **kwargs: object) -> _StubLoginProcess:
                env = kwargs.get("env")
                assert isinstance(env, dict)
                temp_home = Path(env["HOME"])
                # Intentionally omit antigravity-oauth-token to exercise jetski fallback
                jetski_target = temp_home / ".gemini" / "jetski-standalone-oauth-token"
                jetski_target.parent.mkdir(parents=True, exist_ok=True)
                jetski_target.write_text(json.dumps(payload), encoding="utf-8")
                return _StubLoginProcess(returncode=0)

            return _mock_popen

        with patch("builtins.input", return_value="y") as mock_input, \
             patch.object(self.engine, "resolve_agy_binary", return_value="/bin/mock_agy") as mock_resolve, \
             patch("subprocess.Popen", side_effect=make_jetski_popen(jetski_confirm_payload)) as mock_popen:
            res_confirm = self.engine.login_account("existing-acc", force=False)

        mock_input.assert_called_once()
        mock_resolve.assert_called_once_with(None)
        mock_popen.assert_called_once()
        self.assertEqual(res_confirm["name"], "existing-acc")
        self.assertEqual(res_confirm["email"], "confirmed-jetski@example.com")
        self.assertEqual(res_confirm["status"], "standby")
        self.assertEqual(stat.S_IMODE(existing_token.stat().st_mode), 0o600)
        self.assertEqual(
            json.loads(existing_token.read_text(encoding="utf-8"))["token"]["access_token"],
            "ya29.sample_token_for_confirmed-jetski@example.com",
        )

        with patch("builtins.input") as mock_input_force, \
             patch.object(self.engine, "resolve_agy_binary", return_value="/bin/mock_agy") as mock_resolve_force, \
             patch("subprocess.Popen", side_effect=make_jetski_popen(jetski_force_payload)) as mock_popen_force:
            res_force = self.engine.login_account("existing-acc", force=True)

        mock_input_force.assert_not_called()
        mock_resolve_force.assert_called_once_with(None)
        mock_popen_force.assert_called_once()
        self.assertEqual(res_force["email"], "forced-jetski@example.com")
        self.assertEqual(stat.S_IMODE(existing_token.stat().st_mode), 0o600)
        self.assertEqual(
            json.loads(existing_token.read_text(encoding="utf-8"))["token"]["access_token"],
            "ya29.sample_token_for_forced-jetski@example.com",
        )

    def test_login_account_missing_binary_tokenless_exit_timeout_and_interrupt(self) -> None:
        """[TTY-LOGIN-ERR-003] Missing agy, tokenless exit, timeout, and KeyboardInterrupt clean up child and leave no partial dir."""
        fail_dir = self.vault_dir / "accounts" / "fail-acc"

        # 1. Missing agy binary (no override, no AGY_BINARY, no ~/.local/bin/agy, no PATH binary)
        clean_env = {k: v for k, v in os.environ.items() if k != "AGY_BINARY"}
        with patch.dict(os.environ, clean_env, clear=True), \
             patch("agy_switcher.vault.Path.home", return_value=self.base_path), \
             patch("agy_switcher.vault.shutil.which", return_value=None), \
             patch("subprocess.Popen") as mock_popen:
            with self.assertRaises(AgySwitchError):
                self.engine.login_account("fail-acc")
        mock_popen.assert_not_called()
        self.assertFalse(fail_dir.exists())

        # Explicit non-existent agy_binary path
        with patch("subprocess.Popen") as mock_popen_override:
            with self.assertRaises(AgySwitchError):
                self.engine.login_account("fail-acc", agy_binary=str(self.base_path / "missing-agy"))
        mock_popen_override.assert_not_called()
        self.assertFalse(fail_dir.exists())

        # 2. agy exits 0 or non-zero without producing a token file
        for exit_code in (0, 1):
            with self.subTest(exit_code=exit_code):
                with patch.object(self.engine, "resolve_agy_binary", return_value="/bin/mock_agy"), \
                     patch("subprocess.Popen", return_value=_StubLoginProcess(returncode=exit_code)):
                    with self.assertRaises(AgySwitchError):
                        self.engine.login_account("fail-acc")
                self.assertFalse(fail_dir.exists())

        # 3. Stubborn child ignoring SIGTERM on timeout -> terminate, wait(TimeoutExpired), kill, wait
        class _StubbornProcess:
            def __init__(self, escalate_to_kill: bool = True) -> None:
                self.escalate_to_kill = escalate_to_kill
                self.terminated = False
                self.killed = False
                self.wait_calls = 0

            def poll(self) -> int | None:
                return None

            def terminate(self) -> None:
                self.terminated = True

            def wait(self, timeout: float | None = None) -> int:
                self.wait_calls += 1
                if self.escalate_to_kill and self.wait_calls == 1:
                    raise subprocess.TimeoutExpired(cmd="agy", timeout=timeout or 3)
                return -9 if self.killed else -15

            def kill(self) -> None:
                self.killed = True

        timeout_proc = _StubbornProcess(escalate_to_kill=True)
        with patch.object(self.engine, "resolve_agy_binary", return_value="/bin/mock_agy"), \
             patch("subprocess.Popen", return_value=timeout_proc), \
             patch("time.sleep", return_value=None), \
             patch("time.time", side_effect=itertools.chain([0.0], itertools.repeat(100.0))):
            with self.assertRaises(AgySwitchError):
                self.engine.login_account("fail-acc", timeout=5)

        self.assertTrue(timeout_proc.terminated)
        self.assertTrue(timeout_proc.killed)
        self.assertEqual(timeout_proc.wait_calls, 2)
        self.assertFalse(fail_dir.exists())

        # 4. KeyboardInterrupt during child execution -> terminate, wait(TimeoutExpired), kill, wait
        interrupt_proc = _StubbornProcess(escalate_to_kill=True)
        with patch.object(self.engine, "resolve_agy_binary", return_value="/bin/mock_agy"), \
             patch("subprocess.Popen", return_value=interrupt_proc), \
             patch("time.sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(AgySwitchError):
                self.engine.login_account("fail-acc")

        self.assertTrue(interrupt_proc.terminated)
        self.assertTrue(interrupt_proc.killed)
        self.assertEqual(interrupt_proc.wait_calls, 2)
        self.assertFalse(fail_dir.exists())

        # 5. KeyboardInterrupt during proc.wait() escalates to kill(), reaps, and raises AgySwitchError
        class _WaitInterruptProcess:
            def __init__(self) -> None:
                self.terminated = False
                self.killed = False
                self.wait_calls = 0

            def poll(self) -> int | None:
                return None

            def terminate(self) -> None:
                self.terminated = True

            def wait(self, timeout: float | None = None) -> int:
                self.wait_calls += 1
                if self.wait_calls == 1:
                    raise KeyboardInterrupt
                return -9

            def kill(self) -> None:
                self.killed = True

        wait_int_proc = _WaitInterruptProcess()
        with patch.object(self.engine, "resolve_agy_binary", return_value="/bin/mock_agy"), \
             patch("subprocess.Popen", return_value=wait_int_proc), \
             patch("time.sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(AgySwitchError):
                self.engine.login_account("fail-acc")

        self.assertTrue(wait_int_proc.terminated)
        self.assertTrue(wait_int_proc.killed)
        self.assertEqual(wait_int_proc.wait_calls, 2)
        self.assertFalse(fail_dir.exists())

        # 6. ProcessLookupError on terminate() and kill() still reaps child cleanly
        class _VanishingProcess:
            def __init__(self) -> None:
                self.killed = False
                self.wait_calls = 0

            def poll(self) -> int | None:
                return None

            def terminate(self) -> None:
                raise ProcessLookupError("already exited")

            def wait(self, timeout: float | None = None) -> int:
                self.wait_calls += 1
                if self.wait_calls == 1:
                    raise subprocess.TimeoutExpired(cmd="agy", timeout=timeout or 3)
                return 0

            def kill(self) -> None:
                self.killed = True
                raise ProcessLookupError("already exited")

        vanish_proc = _VanishingProcess()
        with patch.object(self.engine, "resolve_agy_binary", return_value="/bin/mock_agy"), \
             patch("subprocess.Popen", return_value=vanish_proc), \
             patch("time.sleep", return_value=None), \
             patch("time.time", side_effect=itertools.chain([0.0], itertools.repeat(100.0))):
            with self.assertRaises(AgySwitchError):
                self.engine.login_account("fail-acc", timeout=5)

        self.assertTrue(vanish_proc.killed)
        self.assertEqual(vanish_proc.wait_calls, 2)
        self.assertFalse(fail_dir.exists())

    def test_login_account_corrupt_token_and_vault_write_failure_cleanup(self) -> None:
        """[TTY-LOGIN-ERR-004] Corrupt token or vault write PermissionError/OSError aborts cleanly without partial dir."""
        bad_dir = self.vault_dir / "accounts" / "bad-tok"

        # 1. Corrupt token JSON or missing OAuth fields across both token paths
        corrupt_cases = [
            ("antigravity", "{not-valid-json"),
            ("antigravity", json.dumps({})),
            ("antigravity", json.dumps({"token": {"access_token": "ya29.only"}})),
            ("jetski", "{not-valid-json"),
            ("jetski", json.dumps({"token": {"refresh_token": "1//only"}})),
        ]
        for variant, raw_content in corrupt_cases:
            with self.subTest(variant=variant, raw_content=raw_content):
                def _mock_popen_corrupt(
                    args: list[str],
                    *,
                    _variant: str = variant,
                    _content: str = raw_content,
                    **kwargs: object,
                ) -> _StubLoginProcess:
                    env = kwargs.get("env")
                    assert isinstance(env, dict)
                    temp_home = Path(env["HOME"])
                    if _variant == "jetski":
                        tok_path = temp_home / ".gemini" / "jetski-standalone-oauth-token"
                    else:
                        tok_path = temp_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
                    tok_path.parent.mkdir(parents=True, exist_ok=True)
                    tok_path.write_text(_content, encoding="utf-8")
                    return _StubLoginProcess(returncode=0)

                with patch.object(self.engine, "resolve_agy_binary", return_value="/bin/mock_agy"), \
                     patch("subprocess.Popen", side_effect=_mock_popen_corrupt):
                    with self.assertRaises(InvalidTokenError):
                        self.engine.login_account("bad-tok")
                self.assertFalse(bad_dir.exists())

        # 2. Vault write failures for a new account must never leave empty/partial accounts/bad-tok dir
        valid_payload = make_valid_token_payload("badtok@example.com")

        def _mock_popen_valid(args: list[str], **kwargs: object) -> _StubLoginProcess:
            env = kwargs.get("env")
            assert isinstance(env, dict)
            temp_home = Path(env["HOME"])
            tok_path = temp_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
            tok_path.parent.mkdir(parents=True, exist_ok=True)
            tok_path.write_text(json.dumps(valid_payload), encoding="utf-8")
            return _StubLoginProcess(returncode=0)

        for target_patch, exc in (
            ("agy_switcher.security.os.replace", PermissionError("simulated permission denied")),
            ("agy_switcher.security.tempfile.mkstemp", OSError("simulated disk write error")),
        ):
            with self.subTest(target_patch=target_patch, exc=type(exc).__name__):
                with patch.object(self.engine, "resolve_agy_binary", return_value="/bin/mock_agy"), \
                     patch("subprocess.Popen", side_effect=_mock_popen_valid), \
                     patch(target_patch, side_effect=exc):
                    with self.assertRaises(AgySwitchError):
                        self.engine.login_account("bad-tok")
                self.assertFalse(bad_dir.exists())

        # Real filesystem chmod 0o500 on accounts_dir when running non-root
        if os.geteuid() != 0:
            self.engine.accounts_dir.chmod(0o500)
            try:
                with patch.object(self.engine, "resolve_agy_binary", return_value="/bin/mock_agy"), \
                     patch("subprocess.Popen", side_effect=_mock_popen_valid):
                    with self.assertRaises(AgySwitchError):
                        self.engine.login_account("bad-tok")
                self.assertFalse(bad_dir.exists())
            finally:
                self.engine.accounts_dir.chmod(0o700)

        # 3. Pre-existing account confirmed via prompt ('y') preserves prior directory & token on write failure
        existing_dir = self.vault_dir / "accounts" / "existing-acc"
        existing_dir.mkdir(parents=True, exist_ok=True)
        existing_token = existing_dir / "token.json"
        prior_content = json.dumps(make_valid_token_payload("prior@example.com"))
        existing_token.write_text(prior_content, encoding="utf-8")
        existing_token.chmod(0o600)

        with patch.object(self.engine, "resolve_agy_binary", return_value="/bin/mock_agy"), \
             patch("subprocess.Popen", side_effect=_mock_popen_valid), \
             patch("agy_switcher.security.os.replace", side_effect=PermissionError("simulated permission denied")):
            with self.assertRaises(AgySwitchError):
                self.engine.login_account("existing-acc", force=False, prompt_fn=lambda _: "y")

        self.assertTrue(existing_dir.is_dir())
        self.assertEqual(existing_token.read_text(encoding="utf-8"), prior_content)
        self.assertEqual([p.name for p in existing_dir.iterdir()], ["token.json"])

    def test_failover_state_persistence_corrupt_and_nondict_recovery(self) -> None:
        """[AUDIT-HOOK-ERR-001] Corrupt or non-dict failover_state.json recovers as empty state and overwrites atomically at 0600."""
        failover_file = self.vault_dir / "cache" / "failover_state.json"

        # Missing file yields clean default schema without creating file
        initial = self.engine.load_failover_state()
        self.assertEqual(initial, {"last_failover": None, "auth_errors": {}})
        self.assertFalse(failover_file.exists())

        # Truncated JSON, JSON array, and scalar payloads recover safely
        for corrupt_payload in ('{"last_failover": ', "[]", '"string-payload"', "123"):
            with self.subTest(corrupt_payload=corrupt_payload):
                failover_file.write_text(corrupt_payload, encoding="utf-8")
                os.chmod(failover_file, 0o644)

                loaded = self.engine.load_failover_state()
                self.assertEqual(loaded, {"last_failover": None, "auth_errors": {}})

                # Mutating helper recovers cleanly and overwrites atomically with 0600 JSON
                self.engine.record_auth_error(
                    "automator",
                    error="HTTP 403 Forbidden",
                    error_type="auth",
                    timestamp=1791201000.0,
                )
                self.assertEqual(stat.S_IMODE(failover_file.stat().st_mode), 0o600)
                disk_state = json.loads(failover_file.read_text(encoding="utf-8"))
                self.assertIsNone(disk_state["last_failover"])
                self.assertEqual(
                    disk_state["auth_errors"]["automator"],
                    {
                        "timestamp": 1791201000.0,
                        "error": "HTTP 403 Forbidden",
                        "error_type": "auth",
                    },
                )

                # Clear and re-test clear_last_failover / clear_auth_error preservation
                self.engine.record_last_failover(
                    from_account="automator",
                    to_account="honey",
                    trigger="auth_corruption",
                    reason="HTTP 403 Forbidden",
                    timestamp=1791201005.0,
                )
                self.assertTrue(self.engine.clear_auth_error("automator"))
                self.assertFalse(self.engine.clear_auth_error("automator"))
                after_clear_auth = self.engine.load_failover_state()
                self.assertEqual(after_clear_auth["auth_errors"], {})
                self.assertEqual(after_clear_auth["last_failover"]["from_account"], "automator")
                self.assertEqual(after_clear_auth["last_failover"]["to_account"], "honey")
                self.assertEqual(after_clear_auth["last_failover"]["trigger"], "auth_corruption")
                self.assertEqual(after_clear_auth["last_failover"]["reason"], "HTTP 403 Forbidden")
                self.assertEqual(after_clear_auth["last_failover"]["timestamp"], 1791201005.0)

                self.engine.record_auth_error("other-acc", error="HTTP 401", timestamp=1791201010.0)
                self.assertTrue(self.engine.clear_last_failover())
                self.assertFalse(self.engine.clear_last_failover())
                after_clear_lf = self.engine.load_failover_state()
                self.assertIsNone(after_clear_lf["last_failover"])
                self.assertIn("other-acc", after_clear_lf["auth_errors"])

        # Symlink safety: never read or write through pre-existing symlink
        victim_file = self.base_path / "external_victim.json"
        victim_content = json.dumps({"auth_errors": {"victim": {"error": "should-not-leak"}}})
        victim_file.write_text(victim_content, encoding="utf-8")
        failover_file.unlink(missing_ok=True)
        failover_file.symlink_to(victim_file)

        self.assertEqual(self.engine.load_failover_state(), {"last_failover": None, "auth_errors": {}})
        self.engine.record_auth_error("revoked-acc", error="HTTP 403", timestamp=1791201020.0)
        self.assertFalse(failover_file.is_symlink())
        self.assertEqual(stat.S_IMODE(failover_file.stat().st_mode), 0o600)
        self.assertEqual(victim_file.read_text(encoding="utf-8"), victim_content)

    def test_failover_state_persistence_concurrent_mutations_and_oserror_resilience(self) -> None:
        """[AUDIT-HOOK-EDGE-003] Concurrent failover_state mutations serialize cleanly at 0600 without lost entries."""
        failover_file = self.vault_dir / "cache" / "failover_state.json"
        total_threads = 14
        barrier = threading.Barrier(total_threads)
        errors: list[BaseException] = []
        errors_lock = threading.Lock()

        def _keep_worker(idx: int) -> None:
            try:
                barrier.wait(timeout=5.0)
                for step in range(15):
                    self.engine.record_auth_error(
                        f"keep-{idx}",
                        error=f"HTTP 403 step-{step}",
                        error_type="auth",
                    )
            except BaseException as exc:
                with errors_lock:
                    errors.append(exc)

        def _churn_worker(idx: int) -> None:
            try:
                barrier.wait(timeout=5.0)
                alias = f"ephemeral-{idx}"
                for step in range(10):
                    self.engine.record_auth_error(alias, error=f"HTTP 401 step-{step}")
                    self.engine.clear_auth_error(alias)
            except BaseException as exc:
                with errors_lock:
                    errors.append(exc)

        def _failover_worker(idx: int) -> None:
            try:
                barrier.wait(timeout=5.0)
                for step in range(10):
                    self.engine.record_last_failover(
                        from_account=f"src-{idx}",
                        to_account=f"dst-{step}",
                        trigger="auth_corruption",
                        reason="HTTP 403",
                    )
            except BaseException as exc:
                with errors_lock:
                    errors.append(exc)

        def _reader_worker() -> None:
            try:
                barrier.wait(timeout=5.0)
                for _ in range(20):
                    snapshot = self.engine.load_failover_state()
                    self.assertIn("last_failover", snapshot)
                    self.assertIn("auth_errors", snapshot)
                    self.assertIsInstance(snapshot["auth_errors"], dict)
            except BaseException as exc:
                with errors_lock:
                    errors.append(exc)

        threads = (
            [threading.Thread(target=_keep_worker, args=(i,)) for i in range(6)]
            + [threading.Thread(target=_churn_worker, args=(i,)) for i in range(4)]
            + [threading.Thread(target=_failover_worker, args=(i,)) for i in range(2)]
            + [threading.Thread(target=_reader_worker) for _ in range(2)]
        )
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10.0)
            self.assertFalse(thread.is_alive())

        self.assertEqual(errors, [])
        self.assertTrue(failover_file.is_file())
        self.assertEqual(stat.S_IMODE(failover_file.stat().st_mode), 0o600)

        disk_state = json.loads(failover_file.read_text(encoding="utf-8"))
        self.assertIsInstance(disk_state, dict)
        auth_errors = disk_state["auth_errors"]
        for i in range(6):
            self.assertIn(f"keep-{i}", auth_errors)
            self.assertEqual(auth_errors[f"keep-{i}"]["error"], "HTTP 403 step-14")
        for i in range(4):
            self.assertNotIn(f"ephemeral-{i}", auth_errors)
        last_failover = disk_state["last_failover"]
        self.assertIsInstance(last_failover, dict)
        self.assertEqual(last_failover["trigger"], "auth_corruption")
        self.assertIn(last_failover["from_account"], {"src-0", "src-1"})

        # bool-as-int guard on timestamp falls back to positive float epoch instead of 1.0
        bool_state = self.engine.record_auth_error(
            "bool-ts-acc",
            error="HTTP 403",
            timestamp=True,  # type: ignore[arg-type]
        )
        self.assertGreater(bool_state["auth_errors"]["bool-ts-acc"]["timestamp"], 1_000_000.0)

        # OSError during persistence warns to stderr without raising unhandled exception
        # and clear helpers return False when disk persistence fails
        stderr_buf = io.StringIO()
        with patch("agy_switcher.security.os.replace", side_effect=OSError("ENOSPC simulated")), \
             patch("sys.stderr", stderr_buf):
            self.engine.record_auth_error("io-fail-acc", error="HTTP 403")
            self.assertFalse(self.engine.clear_auth_error("keep-0"))
            self.assertFalse(self.engine.clear_last_failover())
        self.assertIn("Warning:", stderr_buf.getvalue())
        reloaded_after_io_err = self.engine.load_failover_state()
        self.assertIn("keep-0", reloaded_after_io_err["auth_errors"])
        self.assertIsNotNone(reloaded_after_io_err["last_failover"])

    def test_failover_state_persistence_pending_reload_lifecycle_and_recovery(self) -> None:
        """Pending reload write/load/clear enforces 0600 permissions, expiry filtering, symlink safety, and corrupt recovery."""
        reload_file = self.vault_dir / "cache" / "pending_reload.json"

        # Missing marker returns None and clear returns False
        self.assertIsNone(self.engine.load_pending_reload())
        self.assertFalse(self.engine.clear_pending_reload())

        # Empty target_account raises AgySwitchError; bool ttl/requested_at fall back safely
        with self.assertRaises(AgySwitchError):
            self.engine.write_pending_reload("   ")
        bool_guarded = self.engine.write_pending_reload(
            "work-acc",
            ttl_seconds=True,  # type: ignore[arg-type]
            requested_at=True,  # type: ignore[arg-type]
        )
        self.assertEqual(bool_guarded["ttl_seconds"], 120)
        self.assertGreater(bool_guarded["requested_at"], 1_000_000.0)

        # Write valid marker and inspect on-disk schema & 0600 mode
        written = self.engine.write_pending_reload(
            "work-acc",
            reason="in_turn_deferred_reload",
            ttl_seconds=120,
            requested_at=1000.0,
        )
        self.assertEqual(written["target_account"], "work-acc")
        self.assertEqual(written["ttl_seconds"], 120)
        self.assertEqual(written["requested_at"], 1000.0)
        self.assertEqual(written["reason"], "in_turn_deferred_reload")
        self.assertTrue(reload_file.is_file())
        self.assertEqual(stat.S_IMODE(reload_file.stat().st_mode), 0o600)

        # Unexpired load succeeds (including exact TTL boundary); expired load returns None unless include_expired=True
        loaded = self.engine.load_pending_reload(now=1050.0)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded["target_account"], "work-acc")
        self.assertIsNotNone(self.engine.load_pending_reload(now=1120.0))
        self.assertIsNone(self.engine.load_pending_reload(now=1121.0))
        loaded_expired = self.engine.load_pending_reload(include_expired=True, now=1121.0)
        self.assertIsNotNone(loaded_expired)

        # Clear removes file cleanly
        self.assertTrue(self.engine.clear_pending_reload())
        self.assertFalse(reload_file.exists())
        self.assertFalse(self.engine.clear_pending_reload())

        # Corrupt, non-dict, or non-numeric requested_at/ttl_seconds payloads recover safely as None
        corrupt_cases = (
            "{bad-json",
            "[]",
            json.dumps({"target_account": "work-acc", "requested_at": "not-a-number", "ttl_seconds": 120}),
            json.dumps({"target_account": "work-acc", "requested_at": True, "ttl_seconds": 120}),
            json.dumps({"target_account": "work-acc", "requested_at": 1000.0, "ttl_seconds": True}),
            json.dumps({"target_account": "  ", "requested_at": 1000.0, "ttl_seconds": 120}),
            json.dumps({"target_account": "work-acc", "requested_at": 1000.0, "ttl_seconds": -5}),
        )
        for idx, raw_payload in enumerate(corrupt_cases):
            with self.subTest(case=idx):
                reload_file.write_text(raw_payload, encoding="utf-8")
                self.assertIsNone(self.engine.load_pending_reload(now=1010.0))

        # Symlink safety: load ignores symlink, write replaces symlink without touching target,
        # and clear_pending_reload unlinks symlink without touching target
        victim = self.base_path / "reload_victim.json"
        victim_payload = json.dumps({"target_account": "victim", "requested_at": 1000.0, "ttl_seconds": 120})
        victim.write_text(victim_payload, encoding="utf-8")
        reload_file.unlink(missing_ok=True)
        reload_file.symlink_to(victim)

        self.assertIsNone(self.engine.load_pending_reload(now=1010.0))
        self.assertTrue(self.engine.clear_pending_reload())
        self.assertFalse(reload_file.exists())
        self.assertTrue(victim.exists())
        self.assertEqual(victim.read_text(encoding="utf-8"), victim_payload)

        reload_file.symlink_to(victim)
        self.engine.write_pending_reload("safe-acc", requested_at=1000.0)
        self.assertFalse(reload_file.is_symlink())
        self.assertEqual(stat.S_IMODE(reload_file.stat().st_mode), 0o600)
        self.assertEqual(victim.read_text(encoding="utf-8"), victim_payload)

        # OSError during write_pending_reload warns to stderr without crashing
        stderr_buf = io.StringIO()
        with patch("agy_switcher.security.os.replace", side_effect=OSError("ENOSPC simulated")), \
             patch("sys.stderr", stderr_buf):
            self.engine.write_pending_reload("io-fail-acc", requested_at=1000.0)
        self.assertIn("Warning:", stderr_buf.getvalue())

    def _register_test_account(
        self,
        engine: AgySwitchEngine,
        alias: str,
        email: str,
        *,
        refresh_token: str | None = None,
    ) -> dict[str, object]:
        payload = make_valid_token_payload(email=email)
        if refresh_token is not None:
            token_obj = cast(dict[str, object], payload["token"])
            token_obj["refresh_token"] = refresh_token
        acc_dir = engine.accounts_dir / alias
        acc_dir.mkdir(parents=True, exist_ok=True)
        (acc_dir / "token.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return payload

    def _setup_busy_proc_server(
        self,
        proc_dir: Path,
        gemini_dir: Path,
        server_pid: int = 45000,
    ) -> Path:
        presence_dir = gemini_dir / "antigravity" / "presence"
        presence_dir.mkdir(parents=True, exist_ok=True)
        lock_file = presence_dir / "active-turn.lock"
        lock_file.write_text(f"pid: {server_pid}\n", encoding="utf-8")

        pid_dir = proc_dir / str(server_pid)
        fd_dir = pid_dir / "fd"
        fd_dir.mkdir(parents=True, exist_ok=True)
        (pid_dir / "cmdline").write_bytes(b"/opt/antigravity/bin/language_server\x00--standalone\x00")
        (pid_dir / "stat").write_text(f"{server_pid} (language_server) S 1 {server_pid} 1 0 -1\n", encoding="utf-8")
        (fd_dir / "3").symlink_to(lock_file)
        return lock_file

    def test_atomic_check_before_write_and_auth_gate_active_turn_rejection(self) -> None:
        """[AUTH-GATE-ERR-001] ActiveTurnRunningError aborts switch_tokens before mutating Desktop/CLI/active/state.json."""
        proc_dir = self.base_path / "proc_gate_err_001"
        proc_dir.mkdir(parents=True, mode=0o700)
        engine = AgySwitchEngine(
            vault_dir=self.vault_dir,
            gemini_dir=self.gemini_dir,
            legacy_root=self.legacy_root,
            proc_dir=proc_dir,
        )
        self._register_test_account(engine, "primary-acc", "primary@example.com")
        self._register_test_account(engine, "standby-acc", "standby@example.com")

        # Activate primary-acc and seed legacy state.json
        engine.switch_tokens("primary-acc", restart_server=False)
        legacy_state_file = self.legacy_root / "state.json"
        legacy_state_file.write_text(
            json.dumps({"active": "primary-acc", "accounts": {"primary-acc": {}, "standby-acc": {}}}),
            encoding="utf-8",
        )

        desktop_before = engine.desktop_token_file.read_text(encoding="utf-8")
        cli_before = engine.cli_token_file.read_text(encoding="utf-8")
        active_before = engine.active_file.read_text(encoding="utf-8")
        legacy_before = legacy_state_file.read_text(encoding="utf-8")

        # Simulate external invocation while language_server holds an active presence lock
        self._setup_busy_proc_server(proc_dir, self.gemini_dir, server_pid=45123)
        self.assertTrue(engine.supervisor.is_server_busy())
        self.assertFalse(engine.supervisor.is_invoked_by_language_server())

        with patch("urllib.request.urlopen") as mock_urlopen:
            with self.assertRaises(ActiveTurnRunningError):
                engine.switch_tokens(
                    "standby-acc",
                    restart_server=True,
                    force=False,
                    verify_auth=False,
                )
            mock_urlopen.assert_not_called()

        self.assertEqual(engine.desktop_token_file.read_text(encoding="utf-8"), desktop_before)
        self.assertEqual(engine.cli_token_file.read_text(encoding="utf-8"), cli_before)
        self.assertEqual(engine.active_file.read_text(encoding="utf-8"), active_before)
        self.assertEqual(engine.get_active_name(), "primary-acc")
        self.assertEqual(legacy_state_file.read_text(encoding="utf-8"), legacy_before)
        self.assertFalse(engine.failover_state_file.exists())

    def test_atomic_check_before_write_and_auth_gate_confirmed_rejection(self) -> None:
        """[AUTH-GATE-ERR-002][AUTH-GATE-ERR-003] Confirmed 401/403 rejects before disk write or server restart."""
        proc_dir = self.base_path / "proc_gate_err_002"
        proc_dir.mkdir(parents=True, mode=0o700)
        engine = AgySwitchEngine(
            vault_dir=self.vault_dir,
            gemini_dir=self.gemini_dir,
            legacy_root=self.legacy_root,
            proc_dir=proc_dir,
        )
        self._register_test_account(engine, "healthy-acc", "healthy@example.com")
        self._register_test_account(engine, "revoked-acc", "revoked@example.com")

        engine.switch_tokens("healthy-acc", restart_server=False)
        legacy_state_file = self.legacy_root / "state.json"
        legacy_state_file.write_text(json.dumps({"active": "healthy-acc"}), encoding="utf-8")

        desktop_before = engine.desktop_token_file.read_text(encoding="utf-8")
        cli_before = engine.cli_token_file.read_text(encoding="utf-8")
        active_before = engine.active_file.read_text(encoding="utf-8")
        legacy_before = legacy_state_file.read_text(encoding="utf-8")

        # Case 1: HTTP 403 -> refresh fails with 400 invalid_grant; force=True still enforces verify_auth=True
        for force_flag in (False, True):
            with self.subTest(case="http_403_invalid_grant", force=force_flag):
                engine.failover_state_file.unlink(missing_ok=True)
                err_403 = urllib.error.HTTPError(
                    "https://cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels",
                    403,
                    "Forbidden",
                    email.message.Message(),
                    io.BytesIO(b"Forbidden"),
                )
                err_400 = urllib.error.HTTPError(
                    "https://oauth2.googleapis.com/token",
                    400,
                    "Bad Request",
                    email.message.Message(),
                    io.BytesIO(b'{"error": "invalid_grant", "error_description": "Token has been revoked."}'),
                )
                with patch("urllib.request.urlopen", side_effect=[err_403, err_400]), \
                     patch.object(engine, "restart_language_server") as mock_restart:
                    with self.assertRaises(AccountAuthError) as ctx:
                        engine.switch_tokens(
                            "revoked-acc",
                            restart_server=True,
                            force=force_flag,
                            verify_auth=True,
                        )
                    mock_restart.assert_not_called()

                self.assertIn("agy-switch login revoked-acc", str(ctx.exception))
                self.assertEqual(engine.desktop_token_file.read_text(encoding="utf-8"), desktop_before)
                self.assertEqual(engine.cli_token_file.read_text(encoding="utf-8"), cli_before)
                self.assertEqual(engine.active_file.read_text(encoding="utf-8"), active_before)
                self.assertEqual(legacy_state_file.read_text(encoding="utf-8"), legacy_before)

                self.assertTrue(engine.failover_state_file.is_file())
                self.assertEqual(stat.S_IMODE(engine.failover_state_file.stat().st_mode), 0o600)
                failover_state = json.loads(engine.failover_state_file.read_text(encoding="utf-8"))
                self.assertIn("revoked-acc", failover_state["auth_errors"])
                self.assertEqual(failover_state["auth_errors"]["revoked-acc"]["error_type"], "auth")

        # Case 2: HTTP 401 -> 1x refresh succeeds -> re-probe still returns 403 -> fails closed
        engine.failover_state_file.unlink(missing_ok=True)
        err_401 = urllib.error.HTTPError(
            "https://cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels",
            401,
            "Unauthorized",
            email.message.Message(),
            io.BytesIO(b"Unauthorized"),
        )
        refresh_ok = _MockHTTPResponse(json.dumps({"access_token": "ya29.still_rejected", "expires_in": 3600}).encode("utf-8"))
        err_403_retry = urllib.error.HTTPError(
            "https://cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels",
            403,
            "Forbidden",
            email.message.Message(),
            io.BytesIO(b"Forbidden"),
        )
        with patch("urllib.request.urlopen", side_effect=[err_401, refresh_ok, err_403_retry]) as mock_urlopen, \
             patch.object(engine, "restart_language_server") as mock_restart:
            with self.assertRaises(AccountAuthError) as ctx:
                engine.switch_tokens("revoked-acc", restart_server=True, verify_auth=True)
            self.assertEqual(mock_urlopen.call_count, 3)
            mock_restart.assert_not_called()

        self.assertIn("agy-switch login revoked-acc", str(ctx.exception))
        self.assertEqual(engine.desktop_token_file.read_text(encoding="utf-8"), desktop_before)
        self.assertEqual(engine.cli_token_file.read_text(encoding="utf-8"), cli_before)
        self.assertEqual(engine.active_file.read_text(encoding="utf-8"), active_before)
        self.assertEqual(legacy_state_file.read_text(encoding="utf-8"), legacy_before)
        failover_state = json.loads(engine.failover_state_file.read_text(encoding="utf-8"))
        self.assertIn("revoked-acc", failover_state["auth_errors"])

    def test_atomic_check_before_write_and_auth_gate_preflight_order_refresh_and_fail_open(self) -> None:
        """[AUTH-GATE-001][AUTH-GATE-002][AUTH-GATE-003][AUTH-GATE-004][AUTH-GATE-EDGE-001] Ordering, healthy restart, refresh sync, and fail-open."""
        proc_dir = self.base_path / "proc_gate_order"
        proc_dir.mkdir(parents=True, mode=0o700)
        engine = AgySwitchEngine(
            vault_dir=self.vault_dir,
            gemini_dir=self.gemini_dir,
            legacy_root=self.legacy_root,
            proc_dir=proc_dir,
        )
        self._register_test_account(engine, "base-acc", "base@example.com")
        self._register_test_account(engine, "work-acc", "work@example.com")
        self._register_test_account(engine, "ws-refresh-acc", "ws@example.com", refresh_token="   ")
        missing_access_dir = engine.accounts_dir / "missing-access-acc"
        missing_access_dir.mkdir(parents=True, exist_ok=True)
        missing_access_payload = make_valid_token_payload("missing-access@example.com")
        del cast(dict[str, object], missing_access_payload["token"])["access_token"]
        (missing_access_dir / "token.json").write_text(
            json.dumps(missing_access_payload, indent=2) + "\n", encoding="utf-8"
        )
        corrupt_dir = engine.accounts_dir / "corrupt-acc"
        corrupt_dir.mkdir(parents=True, exist_ok=True)
        (corrupt_dir / "token.json").write_text("{not-json", encoding="utf-8")

        engine.switch_tokens("base-acc", restart_server=False)
        desktop_base = engine.desktop_token_file.read_text(encoding="utf-8")

        # 1. Missing, unsafe (path traversal / NUL byte), and invalid-token accounts fail before HTTP probe
        with patch("urllib.request.urlopen") as mock_urlopen:
            for bad_alias in ("missing-acc", "../etc", "bad\x00alias"):
                with self.assertRaises(AccountNotFoundError):
                    engine.switch_tokens(bad_alias, restart_server=False, verify_auth=True)
            with self.assertRaises(InvalidTokenError):
                engine.switch_tokens("corrupt-acc", restart_server=False, verify_auth=True)
            with self.assertRaises(InvalidTokenError):
                engine.switch_tokens("missing-access-acc", restart_server=False, verify_auth=True)
            # Without verify_auth=True, whitespace-only refresh_token still raises InvalidTokenError
            with self.assertRaises(InvalidTokenError):
                engine.switch_tokens("ws-refresh-acc", restart_server=False, verify_auth=False)
            mock_urlopen.assert_not_called()
        self.assertEqual(engine.desktop_token_file.read_text(encoding="utf-8"), desktop_base)

        # 2. ActiveTurnRunningError fails before HTTP probe when restart_server=True, force=False, verify_auth=True
        lock_file = self._setup_busy_proc_server(proc_dir, self.gemini_dir, server_pid=46000)
        with patch("urllib.request.urlopen") as mock_urlopen:
            with self.assertRaises(ActiveTurnRunningError):
                engine.switch_tokens("work-acc", restart_server=True, force=False, verify_auth=True)
            mock_urlopen.assert_not_called()
        self.assertFalse(engine.failover_state_file.exists())
        lock_file.unlink()
        (proc_dir / "46000" / "fd" / "3").unlink()

        # 3. Default verify_auth=False performs zero HTTP requests and preserves existing quarantine entries
        engine.record_auth_error("work-acc", error="prior error", error_type="auth")
        with patch("urllib.request.urlopen") as mock_urlopen:
            res_default = engine.switch_tokens("work-acc", restart_server=False)
            mock_urlopen.assert_not_called()
        self.assertEqual(res_default["active"], "work-acc")
        self.assertIn("work-acc", engine.load_failover_state()["auth_errors"])

        # [AUTH-GATE-EDGE-002]: With verify_auth=True, whitespace-only refresh_token proceeds to probe_account_auth(),
        # skips QuotaClient.refresh_access_token() on HTTP 401, fails closed with AccountAuthError, and quarantines alias.
        engine.switch_tokens("base-acc", restart_server=False)
        desktop_base_edge = engine.desktop_token_file.read_text(encoding="utf-8")
        err_401_ws = urllib.error.HTTPError(
            "https://cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels",
            401,
            "Unauthorized",
            email.message.Message(),
            io.BytesIO(b"Unauthorized"),
        )
        with patch("urllib.request.urlopen", side_effect=err_401_ws) as mock_urlopen, \
             patch("agy_switcher.telemetry.QuotaClient.refresh_access_token") as mock_refresh:
            with self.assertRaises(AccountAuthError):
                engine.switch_tokens("ws-refresh-acc", restart_server=False, verify_auth=True)
            self.assertEqual(mock_urlopen.call_count, 1)
            mock_refresh.assert_not_called()
        self.assertEqual(engine.desktop_token_file.read_text(encoding="utf-8"), desktop_base_edge)
        self.assertEqual(engine.get_active_name(), "base-acc")
        self.assertIn("ws-refresh-acc", engine.load_failover_state()["auth_errors"])

        # Switch back to base-acc before testing verify_auth=True flows
        engine.switch_tokens("base-acc", restart_server=False)

        quota_payload = json.dumps({
            "groups": [
                {
                    "displayName": "Gemini Models",
                    "buckets": [
                        {
                            "bucketId": "five_hour",
                            "remainingFraction": 0.9,
                            "resetTime": "2026-12-31T23:59:59Z",
                        }
                    ],
                }
            ]
        }).encode("utf-8")

        # 4. [AUTH-GATE-001] Healthy pre-flight auth probe (HTTP 200) permits atomic token switch and server reload
        with patch("urllib.request.urlopen", return_value=_MockHTTPResponse(quota_payload)) as mock_urlopen, \
             patch.object(engine, "restart_language_server", return_value=True) as mock_restart:
            res_healthy = engine.switch_tokens("work-acc", restart_server=True, force=False, verify_auth=True)
        self.assertEqual(mock_urlopen.call_count, 1)
        mock_restart.assert_called_once_with(force=False)
        self.assertTrue(res_healthy["server_restarted"])
        self.assertEqual(res_healthy["active"], "work-acc")
        self.assertNotIn("work-acc", engine.load_failover_state()["auth_errors"])

        # Switch back to base-acc before testing refresh sync and fail-open flows
        engine.switch_tokens("base-acc", restart_server=False)

        # 5. Expired access_token -> 401 -> refresh 200 -> re-probe 200 persists AND syncs fresh access_token
        #    and clears any prior quarantine entry for work-acc
        engine.record_auth_error("work-acc", error="HTTP 401", error_type="auth")
        err_401 = urllib.error.HTTPError(
            "https://cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels",
            401,
            "Unauthorized",
            email.message.Message(),
            io.BytesIO(b"Unauthorized"),
        )
        refresh_ok = _MockHTTPResponse(json.dumps({"access_token": "ya29.fresh_synced_token", "expires_in": 3600}).encode("utf-8"))
        quota_ok = _MockHTTPResponse(quota_payload)

        stderr_refresh = io.StringIO()
        with patch("urllib.request.urlopen", side_effect=[err_401, refresh_ok, quota_ok]) as mock_urlopen, \
             patch("sys.stderr", stderr_refresh):
            res_refreshed = engine.switch_tokens("work-acc", restart_server=False, verify_auth=True)
        self.assertEqual(mock_urlopen.call_count, 3)
        self.assertNotIn("failing open", stderr_refresh.getvalue())
        self.assertEqual(res_refreshed["active"], "work-acc")
        desktop_after_refresh = json.loads(engine.desktop_token_file.read_text(encoding="utf-8"))
        cli_after_refresh = json.loads(engine.cli_token_file.read_text(encoding="utf-8"))
        self.assertEqual(desktop_after_refresh["token"]["access_token"], "ya29.fresh_synced_token")
        self.assertEqual(cli_after_refresh["token"]["access_token"], "ya29.fresh_synced_token")
        self.assertNotIn("work-acc", engine.load_failover_state()["auth_errors"])

        # 6. Transient HTTP 503, URLError, and TimeoutError fail open with stderr warning and complete switch
        transient_errors: list[tuple[str, Exception]] = [
            (
                "http_503",
                urllib.error.HTTPError(
                    "https://cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels",
                    503,
                    "Service Unavailable",
                    email.message.Message(),
                    io.BytesIO(b"Unavailable"),
                ),
            ),
            ("url_error", urllib.error.URLError("Temporary failure in name resolution")),
            ("timeout_error", TimeoutError("timed out")),
        ]
        for label, exc in transient_errors:
            with self.subTest(transient=label):
                engine.switch_tokens("base-acc", restart_server=False)
                engine.record_auth_error("work-acc", error="prior error", error_type="auth")
                stderr_buf = io.StringIO()
                with patch("urllib.request.urlopen", side_effect=exc), \
                     patch("sys.stderr", stderr_buf):
                    res_fail_open = engine.switch_tokens("work-acc", restart_server=False, verify_auth=True)
                self.assertEqual(res_fail_open["active"], "work-acc")
                self.assertIn("Warning:", stderr_buf.getvalue())
                self.assertEqual(engine.get_active_name(), "work-acc")
                # Fail-open must NOT clear prior quarantine entry
                self.assertIn("work-acc", engine.load_failover_state()["auth_errors"])

    def _setup_in_turn_proc_lineage(
        self,
        proc_dir: Path,
        gemini_dir: Path,
        *,
        server_pid: int = 47000,
        child_pid: int = 47010,
        external_pid: int | None = None,
    ) -> Path:
        lock_file = self._setup_busy_proc_server(proc_dir, gemini_dir, server_pid=server_pid)
        init_dir = proc_dir / "1"
        init_dir.mkdir(parents=True, exist_ok=True)
        (init_dir / "cmdline").write_bytes(b"/sbin/init\x00")
        (init_dir / "stat").write_text("1 (systemd) S 0 1 1 0 -1\n", encoding="utf-8")

        child_dir = proc_dir / str(child_pid)
        child_dir.mkdir(parents=True, exist_ok=True)
        (child_dir / "cmdline").write_bytes(b"/bin/bash\x00-c\x00agy-switch\x00")
        (child_dir / "stat").write_text(f"{child_pid} (bash) S {server_pid} {child_pid} 1 0 -1\n", encoding="utf-8")

        if external_pid is not None:
            ext_dir = proc_dir / str(external_pid)
            ext_dir.mkdir(parents=True, exist_ok=True)
            (ext_dir / "cmdline").write_bytes(b"/bin/bash\x00")
            (ext_dir / "stat").write_text(f"{external_pid} (bash) S 1 {external_pid} 1 0 -1\n", encoding="utf-8")
        return lock_file

    def test_deferred_safe_turn_reload_in_turn_downgrades_to_disk_swap_and_writes_marker(self) -> None:
        """[DEFER-RELOAD-001] In-turn switch --force downgrades synchronous kill to Disk-Only Swap and pending_reload.json."""
        proc_dir = self.base_path / "proc_defer_001"
        proc_dir.mkdir(parents=True, mode=0o700)
        engine = AgySwitchEngine(
            vault_dir=self.vault_dir,
            gemini_dir=self.gemini_dir,
            legacy_root=self.legacy_root,
            proc_dir=proc_dir,
        )
        self._register_test_account(engine, "primary-acc", "primary@example.com")
        self._register_test_account(engine, "work-acc", "work@example.com")
        engine.switch_tokens("primary-acc", restart_server=False)

        legacy_state_file = self.legacy_root / "state.json"
        legacy_state_file.write_text(json.dumps({"active": "primary-acc"}), encoding="utf-8")

        self._setup_in_turn_proc_lineage(
            proc_dir,
            self.gemini_dir,
            server_pid=47000,
            child_pid=47010,
        )

        with patch("os.getpid", return_value=47010):
            self.assertTrue(engine.supervisor.is_invoked_by_language_server())
            with patch("os.kill", side_effect=AssertionError("os.kill must not be called during in-turn deferred reload")):
                res = engine.switch_tokens("work-acc", restart_server=True, force=True)

        self.assertEqual(res["active"], "work-acc")
        self.assertEqual(res["email"], "work@example.com")
        self.assertIs(res["server_restarted"], False)
        self.assertIs(res["restart_deferred"], True)

        # Atomic Disk-Only Swap completed across active marker, Desktop/CLI tokens, and legacy state
        self.assertEqual(engine.get_active_name(), "work-acc")
        desktop_payload = json.loads(engine.desktop_token_file.read_text(encoding="utf-8"))
        cli_payload = json.loads(engine.cli_token_file.read_text(encoding="utf-8"))
        self.assertEqual(desktop_payload["token"]["access_token"], "ya29.sample_token_for_work@example.com")
        self.assertEqual(cli_payload["token"]["access_token"], "ya29.sample_token_for_work@example.com")
        legacy_payload = json.loads(legacy_state_file.read_text(encoding="utf-8"))
        self.assertEqual(legacy_payload["active"], "work-acc")

        # cache/pending_reload.json atomically persisted at mode 0600 with expected schema
        pending_file = engine.pending_reload_file
        self.assertTrue(pending_file.is_file())
        self.assertFalse(pending_file.is_symlink())
        self.assertEqual(stat.S_IMODE(pending_file.stat().st_mode), 0o600)
        pending_data = json.loads(pending_file.read_text(encoding="utf-8"))
        self.assertEqual(pending_data["target_account"], "work-acc")
        self.assertEqual(pending_data["ttl_seconds"], 120)
        self.assertIsInstance(pending_data["requested_at"], (int, float))
        self.assertGreater(pending_data["requested_at"], 0)
        self.assertIsInstance(pending_data["reason"], str)
        self.assertTrue(bool(pending_data["reason"].strip()))

    def test_deferred_safe_turn_reload_external_invocation_restarts_synchronously(self) -> None:
        """[DEFER-RELOAD-003] External invocation preserves synchronous restart_language_server(force=True) without pending_reload.json."""
        proc_dir = self.base_path / "proc_defer_003"
        proc_dir.mkdir(parents=True, mode=0o700)
        engine = AgySwitchEngine(
            vault_dir=self.vault_dir,
            gemini_dir=self.gemini_dir,
            legacy_root=self.legacy_root,
            proc_dir=proc_dir,
        )
        self._register_test_account(engine, "primary-acc", "primary@example.com")
        self._register_test_account(engine, "work-acc", "work@example.com")
        engine.switch_tokens("primary-acc", restart_server=False)

        self._setup_in_turn_proc_lineage(
            proc_dir,
            self.gemini_dir,
            server_pid=48000,
            child_pid=48010,
            external_pid=48099,
        )

        sent_signals: list[tuple[int, int]] = []
        terminated_pids: set[int] = set()

        def fake_kill(pid: int, sig: int) -> None:
            if sig == 0:
                if pid in terminated_pids:
                    raise ProcessLookupError(pid)
                return
            sent_signals.append((pid, sig))
            if sig in (signal.SIGTERM, signal.SIGKILL):
                terminated_pids.add(pid)

        with patch("os.getpid", return_value=48099):
            self.assertFalse(engine.supervisor.is_invoked_by_language_server())
            with patch("os.kill", side_effect=fake_kill):
                res = engine.switch_tokens("work-acc", restart_server=True, force=True)

        self.assertEqual(sent_signals, [(48000, signal.SIGTERM)])
        self.assertEqual(res["active"], "work-acc")
        self.assertEqual(res["email"], "work@example.com")
        self.assertIs(res["server_restarted"], True)
        self.assertIs(res["restart_deferred"], False)
        self.assertFalse(engine.pending_reload_file.exists())
        self.assertEqual(engine.get_active_name(), "work-acc")

    def test_deferred_safe_turn_reload_force_restart_now_and_safety_guards(self) -> None:
        """[DEFER-RELOAD-005][DEFER-RELOAD-EDGE-002] force_restart_now overrides deferred reload and clears marker; force=False and restart_server=False preserve safety."""
        proc_dir = self.base_path / "proc_defer_005"
        proc_dir.mkdir(parents=True, mode=0o700)
        engine = AgySwitchEngine(
            vault_dir=self.vault_dir,
            gemini_dir=self.gemini_dir,
            legacy_root=self.legacy_root,
            proc_dir=proc_dir,
        )
        self._register_test_account(engine, "primary-acc", "primary@example.com")
        self._register_test_account(engine, "work-acc", "work@example.com")
        engine.switch_tokens("primary-acc", restart_server=False)

        self._setup_in_turn_proc_lineage(
            proc_dir,
            self.gemini_dir,
            server_pid=49000,
            child_pid=49010,
        )

        with patch("os.getpid", return_value=49010):
            # 1. Without --force and without --no-restart: ActiveTurnRunningError aborts before disk swap or marker
            with self.assertRaises(ActiveTurnRunningError):
                engine.switch_tokens("work-acc", restart_server=True, force=False)
            self.assertEqual(engine.get_active_name(), "primary-acc")
            self.assertFalse(engine.pending_reload_file.exists())

            # 2. With restart_server=False (--no-restart): Disk-Only Swap without writing pending_reload.json
            no_restart_res = engine.switch_tokens("work-acc", restart_server=False)
            self.assertIs(no_restart_res["server_restarted"], False)
            self.assertIs(no_restart_res["restart_deferred"], False)
            self.assertFalse(engine.pending_reload_file.exists())
            self.assertEqual(engine.get_active_name(), "work-acc")

            # 3. In-turn deferred switch creates pending_reload.json, then force_restart_now=True restarts synchronously and clears it
            engine.switch_tokens("primary-acc", restart_server=False)
            deferred_res = engine.switch_tokens("work-acc", restart_server=True, force=True)
            self.assertIs(deferred_res["restart_deferred"], True)
            self.assertTrue(engine.pending_reload_file.is_file())

            sent_signals: list[tuple[int, int]] = []
            terminated_pids: set[int] = set()

            def fake_kill(pid: int, sig: int) -> None:
                if sig == 0:
                    if pid in terminated_pids:
                        raise ProcessLookupError(pid)
                    return
                sent_signals.append((pid, sig))
                if sig in (signal.SIGTERM, signal.SIGKILL):
                    terminated_pids.add(pid)

            with patch("os.kill", side_effect=fake_kill):
                now_res = engine.switch_tokens(
                    "work-acc",
                    restart_server=True,
                    force=True,
                    force_restart_now=True,
                )

            self.assertEqual(sent_signals, [(49000, signal.SIGTERM)])
            self.assertIs(now_res["server_restarted"], True)
            self.assertIs(now_res["restart_deferred"], False)
            self.assertFalse(engine.pending_reload_file.exists())

    def test_deferred_safe_turn_reload_error_paths_cache_creation_and_switch_account_parity(self) -> None:
        """[DEFER-RELOAD-EDGE-004] Error paths write no marker; missing cache dir is created 0700; switch_account forwards force_restart_now."""
        proc_dir = self.base_path / "proc_defer_edge"
        proc_dir.mkdir(parents=True, mode=0o700)
        engine = AgySwitchEngine(
            vault_dir=self.vault_dir,
            gemini_dir=self.gemini_dir,
            legacy_root=self.legacy_root,
            proc_dir=proc_dir,
        )
        self._register_test_account(engine, "primary-acc", "primary@example.com")
        self._register_test_account(engine, "work-acc", "work@example.com")
        self._register_test_account(engine, "revoked-acc", "revoked@example.com")
        engine.switch_tokens("primary-acc", restart_server=False)

        self._setup_in_turn_proc_lineage(
            proc_dir,
            self.gemini_dir,
            server_pid=50000,
            child_pid=50010,
        )

        desktop_before = engine.desktop_token_file.read_text(encoding="utf-8")
        cli_before = engine.cli_token_file.read_text(encoding="utf-8")

        with patch("os.getpid", return_value=50010):
            # 1. force_restart_now=True without force=True still enforces ActiveTurnRunningError
            with self.assertRaises(ActiveTurnRunningError):
                engine.switch_tokens(
                    "work-acc",
                    restart_server=True,
                    force=False,
                    force_restart_now=True,
                )
            self.assertFalse(engine.pending_reload_file.exists())
            self.assertEqual(engine.get_active_name(), "primary-acc")

            # 2. Failed verify_auth=True while in-turn raises AccountAuthError without writing marker
            err_403 = urllib.error.HTTPError(
                "https://cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels",
                403,
                "Forbidden",
                email.message.Message(),
                io.BytesIO(b"Forbidden"),
            )
            err_400 = urllib.error.HTTPError(
                "https://oauth2.googleapis.com/token",
                400,
                "Bad Request",
                email.message.Message(),
                io.BytesIO(b'{"error": "invalid_grant"}'),
            )
            with patch("urllib.request.urlopen", side_effect=[err_403, err_400]):
                with self.assertRaises(AccountAuthError):
                    engine.switch_tokens(
                        "revoked-acc",
                        restart_server=True,
                        force=True,
                        verify_auth=True,
                    )
            self.assertFalse(engine.pending_reload_file.exists())
            self.assertEqual(engine.desktop_token_file.read_text(encoding="utf-8"), desktop_before)
            self.assertEqual(engine.cli_token_file.read_text(encoding="utf-8"), cli_before)

            # 3. Disk swap OSError aborts before writing pending_reload.json
            with patch.object(engine, "_sync_tokens_to_targets", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    engine.switch_tokens("work-acc", restart_server=True, force=True)
            self.assertFalse(engine.pending_reload_file.exists())
            self.assertEqual(engine.get_active_name(), "primary-acc")

            # 4. Missing cache_dir is recreated (0700) and switch_account forwards deferred/force_restart_now
            shutil.rmtree(engine.cache_dir)
            self.assertFalse(engine.cache_dir.exists())

            with patch("os.kill", side_effect=AssertionError("os.kill must not be called during deferred switch_account")):
                acc_deferred = engine.switch_account("work-acc", force=True)
            self.assertIs(acc_deferred["server_restarted"], False)
            self.assertIs(acc_deferred["restart_deferred"], True)
            self.assertTrue(engine.cache_dir.is_dir())
            self.assertEqual(stat.S_IMODE(engine.cache_dir.stat().st_mode), 0o700)
            self.assertTrue(engine.pending_reload_file.is_file())
            self.assertEqual(stat.S_IMODE(engine.pending_reload_file.stat().st_mode), 0o600)

            sent_signals: list[tuple[int, int]] = []
            terminated_pids: set[int] = set()

            def fake_kill(pid: int, sig: int) -> None:
                if sig == 0:
                    if pid in terminated_pids:
                        raise ProcessLookupError(pid)
                    return
                sent_signals.append((pid, sig))
                if sig in (signal.SIGTERM, signal.SIGKILL):
                    terminated_pids.add(pid)

            with patch("os.kill", side_effect=fake_kill):
                acc_now = engine.switch_account("primary-acc", force=True, force_restart_now=True)
            self.assertEqual(sent_signals, [(50000, signal.SIGTERM)])
            self.assertIs(acc_now["server_restarted"], True)
            self.assertIs(acc_now["restart_deferred"], False)
            self.assertFalse(engine.pending_reload_file.exists())

    def test_emergency_restart_and_no_restart_force_restart_now_bypasses_defer_and_clears_marker(self) -> None:
        """[DEFER-RELOAD-005] force_restart_now=True in-turn bypasses deferred reload, restarts synchronously, and clears/omits pending_reload.json."""
        proc_dir = self.base_path / "proc_emergency_005"
        proc_dir.mkdir(parents=True, mode=0o700)
        engine = AgySwitchEngine(
            vault_dir=self.vault_dir,
            gemini_dir=self.gemini_dir,
            legacy_root=self.legacy_root,
            proc_dir=proc_dir,
        )
        self._register_test_account(engine, "primary-acc", "primary@example.com")
        self._register_test_account(engine, "work-acc", "work@example.com")
        engine.switch_tokens("primary-acc", restart_server=False)

        self._setup_in_turn_proc_lineage(
            proc_dir,
            self.gemini_dir,
            server_pid=51000,
            child_pid=51010,
        )

        sent_signals: list[tuple[int, int]] = []
        terminated_pids: set[int] = set()

        def fake_kill(pid: int, sig: int) -> None:
            if sig == 0:
                if pid in terminated_pids:
                    raise ProcessLookupError(pid)
                return
            sent_signals.append((pid, sig))
            if sig in (signal.SIGTERM, signal.SIGKILL):
                terminated_pids.add(pid)

        with patch("os.getpid", return_value=51010):
            self.assertTrue(engine.supervisor.is_invoked_by_language_server())

            # 1. Without pre-existing marker: bypasses deferred downgrade, restarts synchronously, omits pending_reload.json
            with patch("os.kill", side_effect=fake_kill):
                res = engine.switch_tokens(
                    "work-acc",
                    restart_server=True,
                    force=True,
                    force_restart_now=True,
                )

            self.assertEqual(sent_signals, [(51000, signal.SIGTERM)])
            self.assertEqual(res["active"], "work-acc")
            self.assertEqual(res["email"], "work@example.com")
            self.assertIs(res["server_restarted"], True)
            self.assertIs(res["restart_deferred"], False)
            self.assertEqual(engine.get_active_name(), "work-acc")
            self.assertFalse(engine.pending_reload_file.exists())

            # 2. With pre-existing stale cache/pending_reload.json: clears stale marker after emergency restart
            engine.write_pending_reload("work-acc", reason="stale_deferred_reload", ttl_seconds=120)
            self.assertTrue(engine.pending_reload_file.is_file())

            sent_signals.clear()
            terminated_pids.clear()
            with patch("os.kill", side_effect=fake_kill):
                res_stale = engine.switch_tokens(
                    "primary-acc",
                    restart_server=True,
                    force=True,
                    force_restart_now=True,
                )

            self.assertEqual(sent_signals, [(51000, signal.SIGTERM)])
            self.assertEqual(res_stale["active"], "primary-acc")
            self.assertIs(res_stale["server_restarted"], True)
            self.assertIs(res_stale["restart_deferred"], False)
            self.assertEqual(engine.get_active_name(), "primary-acc")
            self.assertFalse(engine.pending_reload_file.exists())

    def test_emergency_restart_and_no_restart_active_turn_guards_and_disk_only_swap(self) -> None:
        """[DEFER-RELOAD-EDGE-002] In-turn force=False raises ActiveTurnRunningError without mutating disk; restart_server=False swaps without marker."""
        proc_dir = self.base_path / "proc_emergency_edge_002"
        proc_dir.mkdir(parents=True, mode=0o700)
        engine = AgySwitchEngine(
            vault_dir=self.vault_dir,
            gemini_dir=self.gemini_dir,
            legacy_root=self.legacy_root,
            proc_dir=proc_dir,
        )
        self._register_test_account(engine, "primary-acc", "primary@example.com")
        self._register_test_account(engine, "work-acc", "work@example.com")
        engine.switch_tokens("primary-acc", restart_server=False)

        legacy_state_file = self.legacy_root / "state.json"
        legacy_state_file.write_text(json.dumps({"active": "primary-acc"}), encoding="utf-8")

        self._setup_in_turn_proc_lineage(
            proc_dir,
            self.gemini_dir,
            server_pid=52000,
            child_pid=52010,
        )

        desktop_before = engine.desktop_token_file.read_text(encoding="utf-8")
        cli_before = engine.cli_token_file.read_text(encoding="utf-8")
        legacy_before = legacy_state_file.read_text(encoding="utf-8")

        with patch("os.getpid", return_value=52010):
            self.assertTrue(engine.supervisor.is_invoked_by_language_server())
            self.assertTrue(engine.supervisor.is_server_busy())

            with patch("os.kill", side_effect=AssertionError("os.kill must not be called when turn safety guard rejects or restart_server=False")):
                # 1. restart_server=True, force=False raises ActiveTurnRunningError with zero disk mutation
                with self.assertRaises(ActiveTurnRunningError):
                    engine.switch_tokens("work-acc", restart_server=True, force=False)
                self.assertEqual(engine.get_active_name(), "primary-acc")
                self.assertEqual(engine.desktop_token_file.read_text(encoding="utf-8"), desktop_before)
                self.assertEqual(engine.cli_token_file.read_text(encoding="utf-8"), cli_before)
                self.assertEqual(legacy_state_file.read_text(encoding="utf-8"), legacy_before)
                self.assertFalse(engine.pending_reload_file.exists())

                # 2. force_restart_now=True with force=False still honors verify_turn_safety(force=False)
                with self.assertRaises(ActiveTurnRunningError):
                    engine.switch_tokens(
                        "work-acc",
                        restart_server=True,
                        force=False,
                        force_restart_now=True,
                    )
                self.assertEqual(engine.get_active_name(), "primary-acc")
                self.assertEqual(engine.desktop_token_file.read_text(encoding="utf-8"), desktop_before)
                self.assertEqual(engine.cli_token_file.read_text(encoding="utf-8"), cli_before)
                self.assertEqual(legacy_state_file.read_text(encoding="utf-8"), legacy_before)
                self.assertFalse(engine.pending_reload_file.exists())

                # 3. restart_server=False performs Disk-Only Swap without writing pending_reload.json
                no_restart_res = engine.switch_tokens("work-acc", restart_server=False)
                self.assertEqual(no_restart_res["active"], "work-acc")
                self.assertEqual(no_restart_res["email"], "work@example.com")
                self.assertIs(no_restart_res["server_restarted"], False)
                self.assertIs(no_restart_res["restart_deferred"], False)
                self.assertEqual(engine.get_active_name(), "work-acc")
                self.assertEqual(
                    json.loads(engine.desktop_token_file.read_text(encoding="utf-8"))["token"]["access_token"],
                    "ya29.sample_token_for_work@example.com",
                )
                self.assertEqual(
                    json.loads(engine.cli_token_file.read_text(encoding="utf-8"))["token"]["access_token"],
                    "ya29.sample_token_for_work@example.com",
                )
                self.assertEqual(json.loads(legacy_state_file.read_text(encoding="utf-8"))["active"], "work-acc")
                self.assertFalse(engine.pending_reload_file.exists())

    def test_quarantine_auto_clear_and_status_window_login_and_add_clear_auth_error(self) -> None:
        """[AUDIT-HOOK-005] Successful login_account() and add_account() atomically clear auth_errors[alias] and list_accounts badge."""
        self._register_test_account(self.engine, "honey", "honey@example.com")
        self._register_test_account(self.engine, "automator", "automator@example.com")
        self._register_test_account(self.engine, "other-acc", "other@example.com")
        self.engine.set_active_name("honey")

        now = time.time()
        self.engine.record_auth_error(
            "automator",
            error="HTTP 403 Forbidden",
            error_type="auth",
            timestamp=now - 300.0,
        )
        self.engine.record_auth_error(
            "other-acc",
            error="HTTP 401 Unauthorized",
            error_type="auth",
            timestamp=now - 200.0,
        )
        self.engine.record_last_failover(
            from_account="automator",
            to_account="honey",
            trigger="auth_corruption",
            reason="HTTP 403 Forbidden",
            timestamp=now - 300.0,
        )

        listed_before = {acc["name"]: acc for acc in self.engine.list_accounts()["accounts"]}
        self.assertNotIn("auth_error", listed_before["honey"])
        self.assertIn("auth_error", listed_before["automator"])
        self.assertEqual(listed_before["automator"]["auth_error"]["error"], "HTTP 403 Forbidden")
        self.assertEqual(listed_before["automator"]["auth_error"]["error_type"], "auth")
        self.assertIn("auth_error", listed_before["other-acc"])

        # 1. Failed add_account (invalid token) must NOT clear quarantine
        bad_token_path = self.base_path / "invalid_automator_token.json"
        bad_token_path.write_text("{not-json", encoding="utf-8")
        with self.assertRaises(InvalidTokenError):
            self.engine.add_account("automator", bad_token_path)
        self.assertIn("automator", self.engine.load_failover_state()["auth_errors"])

        # 2. Failed login_account (aborted overwrite or missing token output) must NOT clear quarantine
        with self.assertRaises(AgySwitchError):
            self.engine.login_account("automator", force=False, prompt_fn=lambda _: "n")
        self.assertIn("automator", self.engine.load_failover_state()["auth_errors"])

        with patch.object(self.engine, "resolve_agy_binary", return_value="/bin/mock_agy"), \
             patch("subprocess.Popen", return_value=_StubLoginProcess(returncode=1)):
            with self.assertRaises(AgySwitchError):
                self.engine.login_account("automator", force=True)
        self.assertIn("automator", self.engine.load_failover_state()["auth_errors"])

        # 3. Successful login_account clears auth_errors["automator"] atomically while preserving other-acc & last_failover
        reauth_payload = make_valid_token_payload("automator@example.com")
        self._run_registration_path("login_account", "automator", reauth_payload)

        state_after_login = self.engine.load_failover_state()
        self.assertNotIn("automator", state_after_login["auth_errors"])
        self.assertIn("other-acc", state_after_login["auth_errors"])
        self.assertIsNotNone(state_after_login["last_failover"])
        self.assertEqual(stat.S_IMODE(self.engine.failover_state_file.stat().st_mode), 0o600)

        listed_after_login = {acc["name"]: acc for acc in self.engine.list_accounts()["accounts"]}
        self.assertNotIn("auth_error", listed_after_login["automator"])
        self.assertIn("auth_error", listed_after_login["other-acc"])

        # 4. Re-quarantine "automator" and verify successful add_account clears auth_errors["automator"] atomically
        self.engine.record_auth_error(
            "automator",
            error="HTTP 401 Unauthorized",
            error_type="auth",
            timestamp=time.time() - 60.0,
        )
        listed_requarantined = {acc["name"]: acc for acc in self.engine.list_accounts()["accounts"]}
        self.assertIn("auth_error", listed_requarantined["automator"])

        self._run_registration_path("add_account", "automator", make_valid_token_payload("automator@example.com"))

        state_after_add = self.engine.load_failover_state()
        self.assertNotIn("automator", state_after_add["auth_errors"])
        self.assertIn("other-acc", state_after_add["auth_errors"])
        self.assertEqual(stat.S_IMODE(self.engine.failover_state_file.stat().st_mode), 0o600)

        listed_after_add = {acc["name"]: acc for acc in self.engine.list_accounts()["accounts"]}
        self.assertNotIn("auth_error", listed_after_add["automator"])
        self.assertIn("auth_error", listed_after_add["other-acc"])

        # 5. Corrupt/non-dict failover_state.json or auth_errors payloads recover gracefully in list_accounts()
        corrupt_payloads = [
            "{truncated-json",
            "[]",
            json.dumps({"auth_errors": ["automator"]}),
            json.dumps({"auth_errors": {"automator": "bad-entry"}}),
            json.dumps({"auth_errors": {"automator": {}}}),
        ]
        for raw_payload in corrupt_payloads:
            with self.subTest(corrupt_failover_state=raw_payload):
                self.engine.failover_state_file.write_text(raw_payload, encoding="utf-8")
                listed_corrupt = {acc["name"]: acc for acc in self.engine.list_accounts()["accounts"]}
                self.assertIn("automator", listed_corrupt)
                self.assertNotIn("auth_error", listed_corrupt["automator"])

    def test_quarantine_auto_clear_and_status_window_24h_cutoff_and_manual_switch(self) -> None:
        """[AUDIT-HOOK-003][AUDIT-HOOK-EDGE-001] get_status() enforces 24h window and manual switch superseding while preserving auth_errors."""
        proc_dir = self.base_path / "proc_status_window"
        proc_dir.mkdir(parents=True, mode=0o700)
        engine = AgySwitchEngine(
            vault_dir=self.vault_dir,
            gemini_dir=self.gemini_dir,
            legacy_root=self.legacy_root,
            proc_dir=proc_dir,
        )
        self._register_test_account(engine, "honey", "honey@example.com")
        self._register_test_account(engine, "automator", "automator@example.com")
        engine.switch_tokens("honey", restart_server=False)

        now = 1_800_000_000.0
        engine.record_auth_error(
            "automator",
            error="HTTP 403 Forbidden",
            error_type="auth",
            timestamp=now - 7200.0,
        )

        # 1. Recent failover (2h ago <= 24h) + unexpired pending_reload surfaced in get_status()
        engine.record_last_failover(
            from_account="automator",
            to_account="honey",
            trigger="auth_corruption",
            reason="HTTP 403 Forbidden",
            timestamp=now - 7200.0,
        )
        engine.write_pending_reload("honey", reason="in_turn_deferred_reload", ttl_seconds=120, requested_at=now - 10.0)

        with patch("agy_switcher.vault.time.time", return_value=now):
            status_recent = engine.get_status()
        self.assertIsInstance(status_recent.get("last_failover"), dict)
        self.assertEqual(status_recent["last_failover"]["from_account"], "automator")
        self.assertEqual(status_recent["last_failover"]["to_account"], "honey")
        self.assertEqual(status_recent["last_failover"]["trigger"], "auth_corruption")
        self.assertIsInstance(status_recent.get("pending_reload"), dict)
        self.assertEqual(status_recent["pending_reload"]["target_account"], "honey")

        # 2. Boundary: exactly 86400s old is still surfaced; > 86400s (86401s) is omitted
        engine.record_last_failover(
            from_account="automator",
            to_account="honey",
            trigger="auth_corruption",
            reason="HTTP 403 Forbidden",
            timestamp=now - 86400.0,
        )
        with patch("agy_switcher.vault.time.time", return_value=now):
            status_at_boundary = engine.get_status()
        self.assertIsInstance(status_at_boundary.get("last_failover"), dict)

        engine.record_last_failover(
            from_account="automator",
            to_account="honey",
            trigger="auth_corruption",
            reason="HTTP 403 Forbidden",
            timestamp=now - 86401.0,
        )
        with patch("agy_switcher.vault.time.time", return_value=now):
            status_stale = engine.get_status()
        self.assertNotIn("last_failover", status_stale)
        self.assertIsNone(status_stale.get("last_failover"))
        # auth_errors quarantine persists in failover_state and list_accounts()
        self.assertIn("automator", engine.load_failover_state()["auth_errors"])
        listed_stale = {acc["name"]: acc for acc in engine.list_accounts()["accounts"]}
        self.assertIn("auth_error", listed_stale["automator"])

        # 3. Malformed, boolean, non-finite, non-positive, string, or missing last_failover.timestamp is omitted;
        #    expired or malformed pending_reload.json is also omitted safely
        invalid_last_failovers: list[tuple[str, str]] = [
            ("bool_true", json.dumps({"last_failover": {"timestamp": True, "from_account": "automator", "to_account": "honey"}, "auth_errors": {}})),
            ("bool_false", json.dumps({"last_failover": {"timestamp": False, "from_account": "automator", "to_account": "honey"}, "auth_errors": {}})),
            ("zero_ts", json.dumps({"last_failover": {"timestamp": 0, "from_account": "automator", "to_account": "honey"}, "auth_errors": {}})),
            ("negative_ts", json.dumps({"last_failover": {"timestamp": -10.0, "from_account": "automator", "to_account": "honey"}, "auth_errors": {}})),
            ("string_ts", json.dumps({"last_failover": {"timestamp": str(now), "from_account": "automator", "to_account": "honey"}, "auth_errors": {}})),
            ("missing_ts", json.dumps({"last_failover": {"from_account": "automator", "to_account": "honey"}, "auth_errors": {}})),
            ("nan_ts", '{"last_failover": {"timestamp": NaN, "from_account": "automator", "to_account": "honey"}, "auth_errors": {}}'),
            ("inf_ts", '{"last_failover": {"timestamp": Infinity, "from_account": "automator", "to_account": "honey"}, "auth_errors": {}}'),
            ("neg_inf_ts", '{"last_failover": {"timestamp": -Infinity, "from_account": "automator", "to_account": "honey"}, "auth_errors": {}}'),
        ]
        for label, raw_state in invalid_last_failovers:
            with self.subTest(invalid_last_failover=label):
                engine.failover_state_file.write_text(raw_state, encoding="utf-8")
                with patch("agy_switcher.vault.time.time", return_value=now):
                    st = engine.get_status()
                self.assertNotIn("last_failover", st)

        engine.write_pending_reload("honey", reason="in_turn_deferred_reload", ttl_seconds=60, requested_at=now - 61.0)
        with patch("agy_switcher.vault.time.time", return_value=now):
            self.assertNotIn("pending_reload", engine.get_status())
        engine.pending_reload_file.write_text("{bad-json", encoding="utf-8")
        with patch("agy_switcher.vault.time.time", return_value=now):
            self.assertNotIn("pending_reload", engine.get_status())
        engine.clear_pending_reload()

        # 4. Fresh last_failover is preserved on failed switch_tokens(), and cleared only on successful manual switch
        engine.record_auth_error(
            "automator",
            error="HTTP 403 Forbidden",
            error_type="auth",
            timestamp=now - 7200.0,
        )
        engine.record_last_failover(
            from_account="automator",
            to_account="honey",
            trigger="auth_corruption",
            reason="HTTP 403 Forbidden",
            timestamp=now - 600.0,
        )
        with patch("agy_switcher.vault.time.time", return_value=now):
            self.assertIsInstance(engine.get_status().get("last_failover"), dict)

        # 4a. Missing account does not clear last_failover
        with self.assertRaises(AccountNotFoundError):
            engine.switch_tokens("nonexistent-acc", restart_server=False)
        self.assertIsNotNone(engine.load_failover_state()["last_failover"])

        # 4b. ActiveTurnRunningError does not clear last_failover
        self._setup_busy_proc_server(proc_dir, self.gemini_dir, server_pid=53000)
        with self.assertRaises(ActiveTurnRunningError):
            engine.switch_tokens("honey", restart_server=True, force=False)
        self.assertIsNotNone(engine.load_failover_state()["last_failover"])
        with patch("agy_switcher.vault.time.time", return_value=now):
            self.assertIsInstance(engine.get_status().get("last_failover"), dict)

        # 4c. AccountAuthError (verify_auth failure) does not clear last_failover
        err_403 = urllib.error.HTTPError(
            "https://cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels",
            403,
            "Forbidden",
            email.message.Message(),
            io.BytesIO(b"Forbidden"),
        )
        with patch("urllib.request.urlopen", side_effect=err_403):
            with self.assertRaises(AccountAuthError):
                engine.switch_tokens("automator", restart_server=False, verify_auth=True)
        self.assertIsNotNone(engine.load_failover_state()["last_failover"])
        with patch("agy_switcher.vault.time.time", return_value=now):
            self.assertIsInstance(engine.get_status().get("last_failover"), dict)

        # 4d. Successful manual switch_tokens("honey", ...) supersedes last_failover
        engine.switch_tokens("honey", restart_server=False)
        with patch("agy_switcher.vault.time.time", return_value=now + 5.0):
            status_superseded = engine.get_status()
        self.assertNotIn("last_failover", status_superseded)
        self.assertIsNone(status_superseded.get("last_failover"))
        self.assertIsNone(engine.load_failover_state()["last_failover"])
        # "automator" remains quarantined in auth_errors and flagged in list_accounts()
        self.assertIn("automator", engine.load_failover_state()["auth_errors"])
        listed_superseded = {acc["name"]: acc for acc in engine.list_accounts()["accounts"]}
        self.assertIn("auth_error", listed_superseded["automator"])
        self.assertNotIn("auth_error", listed_superseded["honey"])

    def test_rejected_switch_preserves_last_failover_and_status_ignores_expired_or_malformed_pending_reload(
        self,
    ) -> None:
        """[AUDIT-HOOK-EDGE-004] Rejected switches preserve last_failover and get_status() ignores expired/malformed pending_reload.json."""
        proc_dir = self.base_path / "proc_edge_004"
        proc_dir.mkdir(parents=True, mode=0o700)
        engine = AgySwitchEngine(
            vault_dir=self.vault_dir,
            gemini_dir=self.gemini_dir,
            legacy_root=self.legacy_root,
            proc_dir=proc_dir,
        )
        self._register_test_account(engine, "primary-acc", "primary@example.com")
        self._register_test_account(engine, "honey", "honey@example.com")
        self._register_test_account(engine, "standby-acc", "standby@example.com")
        corrupt_dir = engine.accounts_dir / "corrupt-acc"
        corrupt_dir.mkdir(parents=True, exist_ok=True)
        (corrupt_dir / "token.json").write_text("{corrupt-token-json", encoding="utf-8")

        engine.switch_tokens("honey", restart_server=False)
        now = 1_800_000_000.0
        one_hour_ago = now - 3600.0

        engine.record_last_failover(
            from_account="primary-acc",
            to_account="honey",
            trigger="quota_rotation",
            reason="Remaining quota 8% <= 20%",
            timestamp=one_hour_ago,
        )

        # Case 1: Expired pending_reload.json (now - requested_at > ttl_seconds) + ActiveTurnRunningError
        self._setup_busy_proc_server(proc_dir, self.gemini_dir, server_pid=54000)
        engine.write_pending_reload(
            "honey",
            reason="in_turn_deferred_reload",
            ttl_seconds=120,
            requested_at=now - 121.0,
        )
        with self.assertRaises(ActiveTurnRunningError):
            engine.switch_tokens("standby-acc", restart_server=True, force=False)

        self.assertEqual(engine.get_active_name(), "honey")
        with patch("agy_switcher.vault.time.time", return_value=now):
            status_expired = engine.get_status()
        self.assertIsInstance(status_expired.get("last_failover"), dict)
        self.assertEqual(status_expired["last_failover"]["from_account"], "primary-acc")
        self.assertEqual(status_expired["last_failover"]["to_account"], "honey")
        self.assertEqual(status_expired["last_failover"]["trigger"], "quota_rotation")
        self.assertEqual(status_expired["last_failover"]["timestamp"], one_hour_ago)
        self.assertNotIn("pending_reload", status_expired)
        self.assertIsNone(status_expired.get("pending_reload"))

        # Case 2: Malformed / invalid pending_reload.json markers + AccountAuthError and InvalidTokenError
        external_target = self.base_path / "external_secret.json"
        external_target.write_text(
            json.dumps(
                {
                    "target_account": "standby-acc",
                    "requested_at": now - 5.0,
                    "ttl_seconds": 120,
                }
            ),
            encoding="utf-8",
        )
        malformed_markers: list[tuple[str, bytes | None]] = [
            ("truncated_json", b"{bad-json"),
            ("non_dict_list", b"[]"),
            ("non_utf8_bytes", b"\xff\xfe\x00"),
            (
                "nan_requested_at",
                b'{"target_account": "honey", "requested_at": NaN, "ttl_seconds": 120}',
            ),
            (
                "bool_ttl",
                json.dumps(
                    {
                        "target_account": "honey",
                        "requested_at": now - 5.0,
                        "ttl_seconds": True,
                    }
                ).encode("utf-8"),
            ),
            (
                "missing_target",
                json.dumps({"requested_at": now - 5.0, "ttl_seconds": 120}).encode("utf-8"),
            ),
            ("symlink_marker", None),
        ]

        err_403 = urllib.error.HTTPError(
            "https://cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels",
            403,
            "Forbidden",
            email.message.Message(),
            io.BytesIO(b"Forbidden"),
        )
        for label, marker_bytes in malformed_markers:
            with self.subTest(pending_reload_variant=label):
                engine.pending_reload_file.unlink(missing_ok=True)
                if marker_bytes is None:
                    engine.pending_reload_file.symlink_to(external_target)
                else:
                    engine.pending_reload_file.write_bytes(marker_bytes)

                with patch("urllib.request.urlopen", side_effect=err_403):
                    with self.assertRaises(AccountAuthError):
                        engine.switch_tokens("standby-acc", restart_server=False, verify_auth=True)

                with self.assertRaises(InvalidTokenError):
                    engine.switch_tokens("corrupt-acc", restart_server=False)

                self.assertEqual(engine.get_active_name(), "honey")
                with patch("agy_switcher.vault.time.time", return_value=now):
                    status_malformed = engine.get_status()
                self.assertIsInstance(status_malformed.get("last_failover"), dict)
                self.assertEqual(status_malformed["last_failover"]["from_account"], "primary-acc")
                self.assertEqual(status_malformed["last_failover"]["to_account"], "honey")
                self.assertEqual(status_malformed["last_failover"]["timestamp"], one_hour_ago)
                self.assertNotIn("pending_reload", status_malformed)
                self.assertIsNone(status_malformed.get("pending_reload"))
                self.assertIn("standby-acc", engine.load_failover_state()["auth_errors"])

    def test_automated_switch_preserves_last_failover_while_manual_switch_clears_AUDIT_HOOK_EDGE_001(
        self,
    ) -> None:
        """[AUDIT-HOOK-EDGE-001] Automated switch_tokens(..., clear_failover=False) and auto_rotate_on_exhaustion preserve last_failover while manual switch clears it."""
        from agy_switcher import api

        proc_dir = self.base_path / "proc_auto_preserve"
        proc_dir.mkdir(parents=True, mode=0o700)
        engine = AgySwitchEngine(
            vault_dir=self.vault_dir,
            gemini_dir=self.gemini_dir,
            legacy_root=self.legacy_root,
            proc_dir=proc_dir,
        )
        self._register_test_account(engine, "automator", "automator@example.com")
        self._register_test_account(engine, "honey", "honey@example.com")
        self._register_test_account(engine, "standby-acc", "standby@example.com")
        engine.switch_tokens("honey", restart_server=False)

        now = 1_800_000_000.0
        engine.record_auth_error(
            "automator",
            error="HTTP 403 Forbidden",
            error_type="auth",
            timestamp=now - 600.0,
        )
        engine.record_last_failover(
            from_account="automator",
            to_account="honey",
            trigger="auth_corruption",
            reason="HTTP 403 Forbidden",
            timestamp=now - 300.0,
        )

        # 1. Automated switch_tokens(..., clear_failover=False) preserves last_failover
        engine.switch_tokens("standby-acc", restart_server=False, force=True, clear_failover=False)
        self.assertEqual(engine.get_active_name(), "standby-acc")
        state_after_auto = engine.load_failover_state()
        self.assertIsNotNone(state_after_auto["last_failover"])
        self.assertEqual(state_after_auto["last_failover"]["from_account"], "automator")
        self.assertIn("automator", state_after_auto["auth_errors"])

        # 2. api.auto_rotate_on_exhaustion() preserves last_failover
        api._LAST_ROTATION_REGISTRY.clear()
        cache_payload = {
            "timestamp": now,
            "accounts": {
                "standby-acc": {"gemini": {"quota": 0, "reset_time": now + 3600}},
                "honey": {"gemini": {"quota": 90, "reset_time": now + 3600}},
            },
        }
        engine.cache_dir.mkdir(parents=True, exist_ok=True)
        (engine.cache_dir / "last_known_quota.json").write_text(json.dumps(cache_payload), encoding="utf-8")
        rot_res = api.auto_rotate_on_exhaustion(model="gemini", reload_server=False, engine=engine)
        self.assertTrue(rot_res.success)
        self.assertEqual(rot_res.new_account, "honey")
        state_after_api_rot = engine.load_failover_state()
        self.assertIsNotNone(state_after_api_rot["last_failover"])
        self.assertEqual(state_after_api_rot["last_failover"]["from_account"], "automator")

        # 3. Successful manual switch (default clear_failover=True) clears last_failover while preserving auth_errors
        engine.switch_tokens("standby-acc", restart_server=False)
        state_after_manual = engine.load_failover_state()
        self.assertIsNone(state_after_manual["last_failover"])
        self.assertIn("automator", state_after_manual["auth_errors"])


if __name__ == "__main__":
    unittest.main()
