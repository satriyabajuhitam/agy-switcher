"""Unit tests for AgySwitchEngine vault operations, focusing on headless OAuth login (Ticket 06)."""

from __future__ import annotations

import base64
import email.message
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from agy_switcher.exceptions import AgySwitchError
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


class MockSubprocess:
    """Mock subprocess simulating agy CLI behavior for testing token exchange."""

    def __init__(
        self,
        temp_home: Path,
        should_succeed: bool = True,
        token_payload: dict[str, object] | None = None,
        stderr_output: bytes = b"",
        exit_code: int = 0,
        delay: float = 0.0,
    ) -> None:
        self.temp_home = temp_home
        self.should_succeed = should_succeed
        self.token_payload = token_payload or make_valid_token_payload()
        self.stderr_output = stderr_output
        self.returncode = exit_code
        self.delay = delay
        self.stdin_received = b""

    def communicate(
        self,
        input: bytes | None = None,
        timeout: float | None = None,
    ) -> tuple[bytes, bytes]:
        if input:
            self.stdin_received = input

        if self.delay > 0:
            if timeout is not None and timeout < self.delay:
                time.sleep(timeout + 0.05)
                raise subprocess.TimeoutExpired(cmd=["mock_agy"], timeout=timeout)
            time.sleep(self.delay)

        if self.should_succeed and self.returncode == 0:
            token_target = self.temp_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
            token_target.parent.mkdir(parents=True, exist_ok=True)
            token_target.write_text(json.dumps(self.token_payload), encoding="utf-8")
            return b"Login successful\n", b""

        return b"", self.stderr_output or b"OAuth token exchange failed: invalid or expired authorization code."

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode


class TestAgySwitchEngine(unittest.TestCase):
    """Test suite for AgySwitchEngine covering headless OAuth login operations."""

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

    def test_login_headless_success(self) -> None:
        """[REQ-3-SCN-002] Headless login via interactive stdin writes token with 0600 permissions."""
        def mock_popen(args: list[str], **kwargs: object) -> MockSubprocess:
            env = kwargs.get("env")
            assert isinstance(env, dict)
            temp_home = Path(env["HOME"])
            return MockSubprocess(temp_home=temp_home, should_succeed=True)

        with patch.object(self.engine, "resolve_agy_binary", return_value="/bin/mock_agy"), \
             patch("subprocess.Popen", side_effect=mock_popen), \
             patch("builtins.input", return_value="4/0AWgav-valid-code"):
            success, msg = self.engine.login_headless(
                "remote_acc",
                force=False,
            )

        self.assertTrue(success, f"login_headless failed: {msg}")
        self.assertEqual(msg, "Account 'remote_acc' successfully logged in")

        token_path = self.vault_dir / "accounts" / "remote_acc" / "token.json"
        self.assertTrue(token_path.is_file(), "Token file was not written to vault")

        # POSIX 0600 mode verification
        token_mode = stat.S_IMODE(token_path.stat().st_mode)
        self.assertEqual(token_mode, 0o600, f"Expected 0600 permissions, got {oct(token_mode)}")

        # Token content verification
        data = json.loads(token_path.read_text(encoding="utf-8"))
        self.assertIn("token", data)
        self.assertEqual(data["auth_method"], "oauth")

    def test_login_headless_invalid_code(self) -> None:
        """[REQ-3-ERR-001] Invalid or expired authorization code returns (False, msg) and cleans up disk."""
        def mock_popen(args: list[str], **kwargs: object) -> MockSubprocess:
            env = kwargs.get("env")
            assert isinstance(env, dict)
            temp_home = Path(env["HOME"])
            return MockSubprocess(
                temp_home=temp_home,
                should_succeed=False,
                exit_code=1,
                stderr_output=b"OAuth token exchange failed: invalid or expired authorization code.",
            )

        with patch.object(self.engine, "resolve_agy_binary", return_value="/bin/mock_agy"), \
             patch("subprocess.Popen", side_effect=mock_popen):
            success, msg = self.engine.login_headless(
                "remote_acc",
                force=False,
                prompt_fn=lambda _: "invalid_code",
            )

        self.assertFalse(success)
        self.assertIn("OAuth token exchange failed", msg)

        # Ensure no account folder or token is left on disk
        acc_dir = self.vault_dir / "accounts" / "remote_acc"
        self.assertFalse(acc_dir.exists(), f"Account directory {acc_dir} was left on disk")

    def test_login_headless_malformed_code(self) -> None:
        """[REQ-3-ERR-006] Malformed authorization codes with newlines/control chars rejected before subprocess."""
        # Contains newline
        success, msg = self.engine.login_headless("remote_acc", prompt_fn=lambda _: "malformed\ncode")
        self.assertFalse(success)
        self.assertIn("Invalid authorization code format", msg)

        # Empty string
        success, msg = self.engine.login_headless("remote_acc", prompt_fn=lambda _: "")
        self.assertFalse(success)
        self.assertIn("Authorization code must be a non-empty string", msg)

        # Exceeds 1024 characters
        long_code = "a" * 1025
        success, msg = self.engine.login_headless("remote_acc", prompt_fn=lambda _: long_code)
        self.assertFalse(success)
        self.assertIn("Invalid authorization code format", msg)

        # Verify no account created
        self.assertFalse((self.vault_dir / "accounts" / "remote_acc").exists())

    def test_login_headless_invalid_alias(self) -> None:
        """[REQ-3-ERR-003] Path traversal or invalid characters in alias rejected defensively."""
        success, msg = self.engine.login_headless("../bad", prompt_fn=lambda _: "4/0AWgav-valid-code")
        self.assertFalse(success)
        self.assertIn("invalid path traversal element", msg)

        success, msg = self.engine.login_headless("my account", prompt_fn=lambda _: "4/0AWgav-valid-code")
        self.assertFalse(success)
        self.assertIn("Invalid account alias", msg)

        # Traversal with dots
        success, msg = self.engine.login_headless("..", prompt_fn=lambda _: "4/0AWgav-valid-code")
        self.assertFalse(success)
        self.assertIn("invalid path traversal element", msg)

    def test_login_headless_collision_without_force(self) -> None:
        """Account already existing in vault returns failure when force=False."""
        acc_dir = self.vault_dir / "accounts" / "existing_acc"
        acc_dir.mkdir(parents=True, exist_ok=True)
        (acc_dir / "token.json").write_text(json.dumps(make_valid_token_payload()), encoding="utf-8")

        success, msg = self.engine.login_headless(
            "existing_acc",
            force=False,
            prompt_fn=lambda _: "4/0AWgav-valid-code",
        )
        self.assertFalse(success)
        self.assertIn("already exists in vault", msg)

    def test_login_headless_collision_with_force(self) -> None:
        """Account already existing in vault is overwritten when force=True."""
        acc_dir = self.vault_dir / "accounts" / "existing_acc"
        acc_dir.mkdir(parents=True, exist_ok=True)
        (acc_dir / "token.json").write_text(json.dumps(make_valid_token_payload("old@example.com")), encoding="utf-8")

        new_payload = make_valid_token_payload("new@example.com")

        def mock_popen(args: list[str], **kwargs: object) -> MockSubprocess:
            env = kwargs.get("env")
            assert isinstance(env, dict)
            temp_home = Path(env["HOME"])
            return MockSubprocess(temp_home=temp_home, should_succeed=True, token_payload=new_payload)

        with patch.object(self.engine, "resolve_agy_binary", return_value="/bin/mock_agy"), \
             patch("subprocess.Popen", side_effect=mock_popen):
            success, msg = self.engine.login_headless(
                "existing_acc",
                force=True,
                prompt_fn=lambda _: "4/0AWgav-valid-code",
            )

        self.assertTrue(success, f"login_headless failed: {msg}")
        token_data = json.loads((acc_dir / "token.json").read_text(encoding="utf-8"))
        self.assertEqual(token_data["token"]["access_token"], "ya29.sample_token_for_new@example.com")

    def test_login_headless_missing_agy_binary(self) -> None:
        """[REQ-3-ERR-004] Antigravity CLI binary missing returns failure cleanly."""
        with patch.object(
            self.engine,
            "resolve_agy_binary",
            side_effect=AgySwitchError("Antigravity CLI binary ('agy') not found in ~/.local/bin/agy or system PATH."),
        ):
            success, msg = self.engine.login_headless(
                "remote_acc",
                prompt_fn=lambda _: "4/0AWgav-valid-code",
            )
        self.assertFalse(success)
        self.assertIn("not found in ~/.local/bin/agy or system PATH", msg)

    def test_login_headless_timeout(self) -> None:
        """[REQ-3-ERR-002] Headless login process timeout returns clean failure."""
        def mock_popen(args: list[str], **kwargs: object) -> MockSubprocess:
            env = kwargs.get("env")
            assert isinstance(env, dict)
            temp_home = Path(env["HOME"])
            return MockSubprocess(temp_home=temp_home, delay=0.5)

        with patch.object(self.engine, "resolve_agy_binary", return_value="/bin/mock_agy"), \
             patch("subprocess.Popen", side_effect=mock_popen):
            success, msg = self.engine.login_headless(
                "remote_acc",
                timeout=0.1,  # 100ms timeout
                prompt_fn=lambda _: "4/0AWgav-valid-code",
            )

        self.assertFalse(success)
        self.assertIn("timed out", msg)
        self.assertFalse((self.vault_dir / "accounts" / "remote_acc").exists())

    def test_login_headless_prompt_fn_and_named_params(self) -> None:
        """Verifies prompt_fn invocation and legacy name parameter fallback."""
        def mock_popen(args: list[str], **kwargs: object) -> MockSubprocess:
            env = kwargs.get("env")
            assert isinstance(env, dict)
            temp_home = Path(env["HOME"])
            return MockSubprocess(temp_home=temp_home, should_succeed=True)

        prompt_called = []
        def custom_prompt(msg: str) -> str:
            prompt_called.append(msg)
            return "4/0AWgav-prompt-code"

        with patch.object(self.engine, "resolve_agy_binary", return_value="/bin/mock_agy"), \
             patch("subprocess.Popen", side_effect=mock_popen):
            success, msg = self.engine.login_headless(
                alias="prompt_acc",
                prompt_fn=custom_prompt,
            )
            self.assertTrue(success, f"login_headless failed: {msg}")
            self.assertEqual(prompt_called, ["Enter authorization code: "])

            success2, msg2 = self.engine.login_headless(
                name="named_acc",
                prompt_fn=custom_prompt,
            )
            self.assertTrue(success2, f"login_headless with name failed: {msg2}")

    def test_oob_oauth_retired_and_vault_structure(self) -> None:
        """[OOB-RETIRE-003] Verifies OOB OAuth builders are retired while DEFAULT_CLIENT_ID remains."""
        import agy_switcher.telemetry as telemetry_mod
        from agy_switcher.telemetry import DEFAULT_CLIENT_ID

        self.assertFalse(hasattr(self.engine, "get_oauth_url"))
        self.assertFalse(hasattr(telemetry_mod, "build_oauth_url"))
        self.assertTrue(bool(DEFAULT_CLIENT_ID))
        self.assertFalse(hasattr(self.engine, "vault"))

    def test_login_headless_rejects_presupplied_auth_code(self) -> None:
        """[OOB-RETIRE-002] Pre-supplied auth_code or legacy code is rejected with PKCE guidance."""
        acc_dir = self.vault_dir / "accounts" / "bot-acc"
        acc_dir.mkdir(parents=True, exist_ok=True)
        existing_payload = make_valid_token_payload("existing@example.com")
        token_path = acc_dir / "token.json"
        token_path.write_text(json.dumps(existing_payload), encoding="utf-8")

        cases: list[dict[str, object]] = [
            {"alias": "bot-acc", "auth_code": "4/1AY0e-stale"},
            {"alias": "bot-acc", "auth_code": ""},
            {"alias": "bot-acc", "auth_code": "   "},
            {"alias": "bot-acc", "auth_code": "4/1AY0e-stale", "force": True},
            {"alias": "bot-acc", "code": "4/1AY0e-legacy"},
            {"name": "bot-acc", "code": ""},
            {"alias": "bot-acc", "code": "4/1AY0e-legacy", "force": True},
            {"alias": "bot-acc", "auth_code": "4/1AY0e-stale", "code": "4/1AY0e-legacy"},
            {"alias": "bot-acc", "auth_code": "4/1AY0e-stale", "use_prompt_fn": False},
        ]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                prompt_calls: list[str] = []
                prompt_cb = (
                    None
                    if kwargs.get("use_prompt_fn") is False
                    else (lambda p: (prompt_calls.append(p) or "unused"))
                )
                with patch.object(self.engine, "resolve_agy_binary") as mock_resolve, \
                     patch("subprocess.Popen") as mock_popen, \
                     patch("builtins.input") as mock_input:
                    success, msg = self.engine.login_headless(
                        alias=str(kwargs.get("alias", "")),
                        name=str(kwargs["name"]) if "name" in kwargs else None,
                        auth_code=str(kwargs["auth_code"]) if "auth_code" in kwargs else None,
                        code=str(kwargs["code"]) if "code" in kwargs else None,
                        force=bool(kwargs.get("force", False)),
                        prompt_fn=prompt_cb,
                    )

                self.assertFalse(success)
                self.assertIn("PKCE", msg)
                self.assertIn("stdin", msg)
                self.assertIn("prompt_fn", msg)
                self.assertEqual(prompt_calls, [])
                mock_input.assert_not_called()
                mock_resolve.assert_not_called()
                mock_popen.assert_not_called()

        self.assertEqual(json.loads(token_path.read_text(encoding="utf-8")), existing_payload)

    def _run_registration_path(
        self,
        method: str,
        alias: str,
        payload: dict[str, object],
    ) -> dict[str, object] | tuple[bool, str]:
        """Executes add_account, login_account, or login_headless with subprocess mocked."""
        if method == "add_account":
            src_token = self.base_path / f"{alias}_src_token.json"
            src_token.write_text(json.dumps(payload), encoding="utf-8")
            return self.engine.add_account(alias, src_token)

        def mock_popen(args: list[str], **kwargs: object) -> MockSubprocess:
            env = kwargs.get("env")
            assert isinstance(env, dict)
            temp_home = Path(env["HOME"])
            if method == "login_account":
                token_target = temp_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
                token_target.parent.mkdir(parents=True, exist_ok=True)
                token_target.write_text(json.dumps(payload), encoding="utf-8")
            return MockSubprocess(temp_home=temp_home, should_succeed=True, token_payload=payload)

        with patch.object(self.engine, "resolve_agy_binary", return_value="/bin/mock_agy"), \
             patch("subprocess.Popen", side_effect=mock_popen):
            if method == "login_account":
                return self.engine.login_account(alias, force=True)
            return self.engine.login_headless(
                alias=alias,
                force=True,
                prompt_fn=lambda _p: "4/0AWgav-valid-code",
            )

    def test_add_account_and_login_userinfo_persists_id_token_email(self) -> None:
        """[IDENTITY-RES-002] Missing id_token/id_token_email resolves email via Google UserInfo and writes 0600 id_token_email."""
        for method in ("add_account", "login_account", "login_headless"):
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
                elif method == "login_account":
                    self.assertEqual(
                        result,
                        {"name": alias, "email": "userinfo@example.com", "status": "standby"},
                    )
                else:
                    self.assertEqual(result, (True, f"Account '{alias}' successfully logged in"))

    def test_add_account_and_login_skip_userinfo_when_id_token_email_present(self) -> None:
        """Registration resolves existing id_token_email without calling Google UserInfo."""
        for method in ("add_account", "login_account", "login_headless"):
            with self.subTest(method=method):
                alias = f"existing_email_{method}"
                payload = make_token_payload_without_identity()
                payload["id_token_email"] = "  preset@example.com  "

                with patch("urllib.request.urlopen") as mock_urlopen:
                    result = self._run_registration_path(method, alias, payload)

                mock_urlopen.assert_not_called()
                if method in ("add_account", "login_account"):
                    assert isinstance(result, dict)
                    self.assertEqual(result.get("email"), "preset@example.com")
                else:
                    self.assertEqual(result, (True, f"Account '{alias}' successfully logged in"))

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
            for method in ("add_account", "login_account", "login_headless"):
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
                    elif method == "login_account":
                        self.assertEqual(result, {"name": alias, "email": None, "status": "standby"})
                    else:
                        self.assertEqual(result, (True, f"Account '{alias}' successfully logged in"))

    def test_add_account_and_login_skip_userinfo_when_id_token_has_email(self) -> None:
        """Registration resolves existing id_token JWT email without calling Google UserInfo."""
        for method in ("add_account", "login_account", "login_headless"):
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
                elif method == "login_account":
                    self.assertEqual(
                        result,
                        {"name": alias, "email": "jwt_user@example.com", "status": "standby"},
                    )
                else:
                    self.assertEqual(result, (True, f"Account '{alias}' successfully logged in"))

    def test_add_account_and_login_userinfo_fallback_when_jwt_lacks_email_claim(self) -> None:
        """Valid JWT without email claim falls back to Google UserInfo lookup during registration."""
        no_email_jwt_payload = base64.urlsafe_b64encode(b'{"sub":"no_email_user"}').decode("utf-8").rstrip("=")
        for method in ("add_account", "login_account", "login_headless"):
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
                elif method == "login_account":
                    self.assertEqual(
                        result,
                        {"name": alias, "email": "from_userinfo@example.com", "status": "standby"},
                    )
                else:
                    self.assertEqual(result, (True, f"Account '{alias}' successfully logged in"))

    def test_add_account_and_login_userinfo_incomplete_read_and_control_char_token_degrade(self) -> None:
        """http.client.IncompleteRead and control-char access_token degrade cleanly without aborting or leaking tokens."""
        import http.client

        for method in ("add_account", "login_account", "login_headless"):
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
                elif method == "login_account":
                    self.assertEqual(result, {"name": alias, "email": None, "status": "standby"})
                else:
                    self.assertEqual(result, (True, f"Account '{alias}' successfully logged in"))

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
            for method in ("add_account", "login_account", "login_headless"):
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
                    elif method == "login_account":
                        self.assertEqual(result, {"name": alias, "email": None, "status": "standby"})
                    else:
                        self.assertEqual(result, (True, f"Account '{alias}' successfully logged in"))

        # Pre-populated invalid id_token_email values are stripped across all registration methods
        invalid_presets: list[tuple[str, object]] = [
            ("ws", "   "),
            ("int", 123),
            ("none", None),
            ("ctrl", "bad\n@example.com"),
        ]
        for label, bad_preset in invalid_presets:
            for method in ("add_account", "login_account", "login_headless"):
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
                    elif method == "login_account":
                        self.assertEqual(result, {"name": alias, "email": None, "status": "standby"})
                    else:
                        self.assertEqual(result, (True, f"Account '{alias}' successfully logged in"))

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


if __name__ == "__main__":
    unittest.main()
