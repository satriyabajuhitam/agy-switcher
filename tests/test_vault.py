"""Unit tests for AgySwitchEngine vault operations and Token Identity."""

from __future__ import annotations

import base64
import email.message
import io
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from agy_switcher.vault import AgySwitchEngine


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


if __name__ == "__main__":
    unittest.main()
