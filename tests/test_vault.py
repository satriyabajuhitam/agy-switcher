"""Unit tests for AgySwitchEngine vault operations, focusing on headless OAuth login (Ticket 06)."""

from __future__ import annotations

import base64
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
        """[REQ-3-SCN-002] Non-interactive headless login with valid auth code writes token with 0600 permissions."""
        def mock_popen(args: list[str], **kwargs: object) -> MockSubprocess:
            env = kwargs.get("env")
            assert isinstance(env, dict)
            temp_home = Path(env["HOME"])
            return MockSubprocess(temp_home=temp_home, should_succeed=True)

        with patch.object(self.engine, "resolve_agy_binary", return_value="/bin/mock_agy"), \
             patch("subprocess.Popen", side_effect=mock_popen):
            success, msg = self.engine.login_headless(
                "remote_acc",
                auth_code="4/0AWgav-valid-code",
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
                auth_code="invalid_code",
                force=False,
            )

        self.assertFalse(success)
        self.assertIn("OAuth token exchange failed", msg)

        # Ensure no account folder or token is left on disk
        acc_dir = self.vault_dir / "accounts" / "remote_acc"
        self.assertFalse(acc_dir.exists(), f"Account directory {acc_dir} was left on disk")

    def test_login_headless_malformed_code(self) -> None:
        """[REQ-3-ERR-006] Malformed authorization codes with newlines/control chars rejected before subprocess."""
        # Contains newline
        success, msg = self.engine.login_headless("remote_acc", auth_code="malformed\ncode")
        self.assertFalse(success)
        self.assertIn("Invalid authorization code format", msg)

        # Empty string
        success, msg = self.engine.login_headless("remote_acc", auth_code="")
        self.assertFalse(success)
        self.assertIn("Authorization code must be a non-empty string", msg)

        # Exceeds 1024 characters
        long_code = "a" * 1025
        success, msg = self.engine.login_headless("remote_acc", auth_code=long_code)
        self.assertFalse(success)
        self.assertIn("Invalid authorization code format", msg)

        # Verify no account created
        self.assertFalse((self.vault_dir / "accounts" / "remote_acc").exists())

    def test_login_headless_invalid_alias(self) -> None:
        """[REQ-3-ERR-003] Path traversal or invalid characters in alias rejected defensively."""
        success, msg = self.engine.login_headless("../bad", auth_code="4/0AWgav-valid-code")
        self.assertFalse(success)
        self.assertIn("invalid path traversal element", msg)

        success, msg = self.engine.login_headless("my account", auth_code="4/0AWgav-valid-code")
        self.assertFalse(success)
        self.assertIn("Invalid account alias", msg)

        # Traversal with dots
        success, msg = self.engine.login_headless("..", auth_code="4/0AWgav-valid-code")
        self.assertFalse(success)
        self.assertIn("invalid path traversal element", msg)

    def test_login_headless_collision_without_force(self) -> None:
        """Account already existing in vault returns failure when force=False."""
        acc_dir = self.vault_dir / "accounts" / "existing_acc"
        acc_dir.mkdir(parents=True, exist_ok=True)
        (acc_dir / "token.json").write_text(json.dumps(make_valid_token_payload()), encoding="utf-8")

        success, msg = self.engine.login_headless("existing_acc", auth_code="4/0AWgav-valid-code", force=False)
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
            success, msg = self.engine.login_headless("existing_acc", auth_code="4/0AWgav-valid-code", force=True)

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
            success, msg = self.engine.login_headless("remote_acc", auth_code="4/0AWgav-valid-code")
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
                auth_code="4/0AWgav-valid-code",
                timeout=0.1,  # 100ms timeout
            )

        self.assertFalse(success)
        self.assertIn("timed out", msg)
        self.assertFalse((self.vault_dir / "accounts" / "remote_acc").exists())

    def test_login_headless_prompt_fn_and_named_params(self) -> None:
        """Verifies prompt_fn invocation and name/code named parameter fallback."""
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
                code="4/0AWgav-named-code",
            )
            self.assertTrue(success2, f"login_headless with name/code failed: {msg2}")

    def test_get_oauth_url_and_vault_structure(self) -> None:
        """Verifies get_oauth_url contains DEFAULT_CLIENT_ID and engine does not retain redundant self.vault."""
        from agy_switcher.telemetry import DEFAULT_CLIENT_ID
        oauth_url = self.engine.get_oauth_url()
        self.assertIn(DEFAULT_CLIENT_ID, oauth_url)
        self.assertIn("accounts.google.com/o/oauth2/auth", oauth_url)
        self.assertFalse(hasattr(self.engine, "vault"))


if __name__ == "__main__":
    unittest.main()
