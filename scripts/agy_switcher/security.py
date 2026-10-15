"""Layer 0 Foundation: Security & Filesystem Atomic Primitives.

Provides atomic filesystem write helpers (os.replace with 0700/0600 POSIX permissions),
pure Base64 OAuth JWT parsing, and token schema validation primitives.

Zero external dependencies. Pure Python 3.10+ standard library.
Strictly Layer 0: Only depends on Python standard library and Layer 0 exceptions.
Does not import from Layer >= 1.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from agy_switcher.exceptions import InvalidTokenError


def _atomic_write_file(target_path: Path | str, content: str | bytes, mode: int = 0o600) -> None:
    """Writes content (str or bytes) to a target path atomically with specified permissions."""
    target_path = Path(target_path)
    target_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(target_path.parent, 0o700)
    except OSError:
        pass
    tmp_fd, tmp_file = tempfile.mkstemp(dir=target_path.parent, prefix=f".{target_path.name}.tmp.")
    try:
        if isinstance(content, bytes):
            with os.fdopen(tmp_fd, "wb") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
        else:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
        os.chmod(tmp_file, mode)
        os.replace(tmp_file, target_path)
        tmp_file = None
    finally:
        if tmp_file and os.path.exists(tmp_file):
            try:
                os.unlink(tmp_file)
            except OSError:
                pass


def _atomic_write_json(target_path: Path | str, data: Any, mode: int = 0o600, indent: int = 2) -> None:
    """Writes JSON to a target path atomically with specified permissions and indentation."""
    _atomic_write_file(Path(target_path), json.dumps(data, indent=indent) + "\n", mode=mode)


def extract_email_from_jwt(id_token: str | None) -> str | None:
    """Decodes JWT payload to extract user email without external dependencies."""
    if not id_token or not isinstance(id_token, str):
        return None
    try:
        parts = id_token.split(".")
        if len(parts) >= 2:
            payload = parts[1]
            payload += "=" * (-len(payload) % 4)
            decoded = json.loads(base64.urlsafe_b64decode(payload.encode("utf-8")))
            if isinstance(decoded, dict):
                email = decoded.get("email")
                if isinstance(email, str) and email.strip():
                    return email.strip()
    except Exception:
        return None
    return None


def validate_token_dict(data: Any, *, return_tuple: bool = False) -> Any:
    """Validates that data complies with Google OAuth token JSON schema.

    If return_tuple=True, returns tuple[bool, str | None].
    Otherwise, returns validated token dictionary or raises InvalidTokenError.
    """
    try:
        if not isinstance(data, dict):
            raise InvalidTokenError("Token payload must be a JSON object.")

        token_obj = data.get("token")
        if not isinstance(token_obj, dict):
            raise InvalidTokenError("Missing required 'token' object in payload.")

        required_token_keys = ("access_token", "refresh_token", "token_type", "expiry")
        missing_keys = [k for k in required_token_keys if not token_obj.get(k)]
        if missing_keys:
            raise InvalidTokenError(f"Missing required token fields: {', '.join(missing_keys)}")

        if not data.get("auth_method"):
            raise InvalidTokenError("Missing required 'auth_method' field.")

        id_token = data.get("id_token")
        if not id_token or not isinstance(id_token, str):
            raise InvalidTokenError("Missing or invalid 'id_token' JWT field.")

        if return_tuple:
            return True, None
        return data
    except InvalidTokenError as exc:
        if return_tuple:
            return False, str(exc)
        raise


def validate_token_file(token_path: Path | str, *, return_tuple: bool = False) -> Any:
    """Reads and validates a token file from disk.

    If return_tuple=True, returns tuple[bool, str | None, dict | None].
    Otherwise, returns validated token dictionary or raises InvalidTokenError.
    """
    token_path = Path(token_path)
    if not token_path.is_file():
        err = f"Token file does not exist: {token_path}"
        if return_tuple:
            return False, err, None
        raise InvalidTokenError(err)
    try:
        raw = token_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as e:
        err = f"Failed to read/parse token JSON: {e}"
        if return_tuple:
            return False, err, None
        raise InvalidTokenError(err) from e

    if return_tuple:
        ok, msg = validate_token_dict(data, return_tuple=True)
        return ok, msg, data if ok else None

    return validate_token_dict(data)


def check_token_dict(data: Any) -> tuple[bool, str | None]:
    """Validates token dictionary returning (is_valid, error_message)."""
    return validate_token_dict(data, return_tuple=True)


def check_token_file(token_path: Path | str) -> tuple[bool, str | None, dict | None]:
    """Validates token file returning (is_valid, error_message, token_dict)."""
    return validate_token_file(token_path, return_tuple=True)


__all__ = [
    "_atomic_write_file",
    "_atomic_write_json",
    "extract_email_from_jwt",
    "validate_token_dict",
    "validate_token_file",
    "check_token_dict",
    "check_token_file",
]
