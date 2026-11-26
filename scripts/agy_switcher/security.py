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
import math
import os
from pathlib import Path
import stat
import tempfile
from typing import Any, Mapping, cast

from agy_switcher.exceptions import InvalidTokenError
from agy_switcher.models import (
    DEFAULT_PENDING_RELOAD_TTL_SECONDS,
    PendingReloadPayload,
    TokenIdentity,
)


def _atomic_write_file(target_path: Path | str, content: str | bytes, mode: int = 0o600) -> None:
    """Writes content (str or bytes) to a target path atomically with specified permissions."""
    target_path = Path(target_path)
    target_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(target_path.parent, 0o700)
    except OSError:
        pass
    tmp_file: str | None = None
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


def read_json_dict_nofollow(path: Path | str) -> tuple[dict[str, object] | None, str]:
    """Reads a regular JSON object file atomically without following symlinks.

    Returns (decoded_dict_or_none, status) where status is one of:
    'ok', 'missing', 'non_regular', 'io_error', 'parse_error', 'not_dict'.
    """
    target = Path(path)
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        return None, "missing"
    except OSError:
        return None, "io_error"

    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        return None, "non_regular"

    open_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target, open_flags)
    except OSError:
        return None, "io_error"

    try:
        fd_stat = os.fstat(fd)
    except OSError:
        try:
            os.close(fd)
        except OSError:
            pass
        return None, "io_error"

    if not stat.S_ISREG(fd_stat.st_mode):
        try:
            os.close(fd)
        except OSError:
            pass
        return None, "non_regular"

    try:
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            loaded: object = json.load(handle)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError, OverflowError):
        return None, "parse_error"
    except OSError:
        return None, "io_error"

    if not isinstance(loaded, dict):
        return None, "not_dict"
    return cast(dict[str, object], loaded), "ok"


def read_json_dict_safe(path: Path | str) -> tuple[dict[str, object] | None, bool]:
    """Reads a regular JSON object file without following symlinks.

    Returns (decoded_dict_or_none, path_exists_on_disk).
    """
    loaded, status = read_json_dict_nofollow(path)
    return loaded, status != "missing"


def is_safe_account_alias(name: object) -> bool:
    """Returns True when name is a non-empty account alias free of path traversal or NUL bytes."""
    if not isinstance(name, str):
        return False
    clean = name.strip()
    return bool(
        clean
        and "/" not in clean
        and "\\" not in clean
        and ".." not in clean
        and "\x00" not in clean
        and clean not in (".", "..")
    )


def parse_positive_finite_float(value: object) -> float | None:
    """Returns a finite float > 0, rejecting bools, non-numeric values, and overflows."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return parsed


def parse_pending_reload_payload(raw_payload: object) -> PendingReloadPayload | None:
    """Validates pending_reload.json schema, returning a normalized PendingReloadPayload or None."""
    if not isinstance(raw_payload, dict):
        return None

    raw_target = raw_payload.get("target_account")
    if not isinstance(raw_target, str) or not raw_target.strip():
        return None

    requested_at = parse_positive_finite_float(raw_payload.get("requested_at"))
    if requested_at is None:
        return None

    raw_ttl = raw_payload.get("ttl_seconds")
    ttl_val: float | int
    if raw_ttl is None:
        ttl_val = DEFAULT_PENDING_RELOAD_TTL_SECONDS
    else:
        parsed_ttl = parse_positive_finite_float(raw_ttl)
        if parsed_ttl is None:
            return None
        ttl_val = raw_ttl if isinstance(raw_ttl, int) else parsed_ttl

    raw_reason = raw_payload.get("reason")
    reason = (
        raw_reason.strip()
        if isinstance(raw_reason, str) and raw_reason.strip()
        else "in_turn_deferred_reload"
    )
    return {
        "target_account": raw_target.strip(),
        "requested_at": requested_at,
        "ttl_seconds": ttl_val,
        "reason": reason,
    }


def _atomic_write_json(target_path: Path | str, data: Any, mode: int = 0o600, indent: int = 2) -> None:
    """Writes JSON to a target path atomically with specified permissions and indentation."""
    _atomic_write_file(Path(target_path), json.dumps(data, indent=indent) + "\n", mode=mode)


_BASE64URL_TRANS = str.maketrans("-_", "+/")


def _decode_jwt_payload(id_token: object) -> dict[str, object] | None:
    """Decodes the second dot-separated JWT segment into a JSON object, or returns None."""
    if not isinstance(id_token, str) or not id_token.strip():
        return None
    parts = id_token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1]
    if not payload or "+" in payload or "/" in payload:
        return None
    padded = payload + ("=" * (-len(payload) % 4))
    try:
        raw_bytes = base64.b64decode(padded.translate(_BASE64URL_TRANS), validate=True)
        decoded = json.loads(raw_bytes.decode("utf-8"))
    except ValueError:
        return None
    if not isinstance(decoded, dict):
        return None
    return cast(dict[str, object], decoded)


def extract_email_from_jwt(id_token: str | None) -> str | None:
    """Decodes JWT payload to extract user email without external dependencies."""
    decoded = _decode_jwt_payload(id_token)
    if decoded is None:
        return None
    email = decoded.get("email")
    if isinstance(email, str) and email.strip():
        return email.strip()
    return None


def resolve_token_email(data: object) -> str | None:
    """Resolves account email from a token payload without network I/O.

    Prefers a trimmed non-empty email claim from id_token JWT via
    extract_email_from_jwt(), falling back to a trimmed non-empty string
    id_token_email field.
    """
    if not isinstance(data, Mapping):
        return None

    id_token = data.get("id_token")
    if isinstance(id_token, str):
        jwt_email = extract_email_from_jwt(id_token)
        if jwt_email is not None:
            return jwt_email

    fallback_email = data.get("id_token_email")
    if isinstance(fallback_email, str):
        trimmed = fallback_email.strip()
        if trimmed:
            return trimmed
    return None


def extract_token_identity(data: object) -> TokenIdentity | None:
    """Extracts normalized credential identity from an in-memory token payload."""
    if not isinstance(data, dict):
        return None

    token_obj = data.get("token")
    if not isinstance(token_obj, dict):
        return None

    raw_refresh_token = token_obj.get("refresh_token")
    if not isinstance(raw_refresh_token, str):
        return None
    refresh_token = raw_refresh_token.strip()
    if not refresh_token:
        return None

    raw_auth_method = data.get("auth_method")
    if not isinstance(raw_auth_method, str):
        return None
    auth_method = raw_auth_method.strip()
    if not auth_method:
        return None

    resolved_email = resolve_token_email(data)
    email: str | None = None
    if resolved_email is not None:
        normalized_email = resolved_email.strip().lower()
        if normalized_email:
            email = normalized_email

    return {
        "refresh_token": refresh_token,
        "auth_method": auth_method,
        "email": email,
    }


def token_credentials_match(vault_data: object, target_data: object) -> bool:
    """Compares Vault and target token payloads purely by normalized credential identity."""
    vault_identity = extract_token_identity(vault_data)
    target_identity = extract_token_identity(target_data)
    if vault_identity is None or target_identity is None:
        return False

    if vault_identity["refresh_token"] != target_identity["refresh_token"]:
        return False
    if vault_identity["auth_method"] != target_identity["auth_method"]:
        return False

    vault_email = vault_identity["email"]
    target_email = target_identity["email"]
    if vault_email is not None and target_email is not None:
        return vault_email == target_email
    return True


def _is_non_empty_str(value: object) -> bool:
    """Returns True when value is a string containing non-whitespace characters."""
    return isinstance(value, str) and bool(value.strip())


def has_valid_id_token_or_fallback(data: Mapping[str, object]) -> bool:
    """Returns True when id_token is omitted/empty, a valid JWT object, or backed by id_token_email."""
    # agy 1.5.10 omits id_token or sets it to null/"" in favor of id_token_email.
    # Present non-empty id_token values must parse as a JWT JSON object unless a
    # valid trimmed non-empty string id_token_email is provided.
    id_token = data.get("id_token")
    if id_token is None or id_token == "":
        return True
    if _decode_jwt_payload(id_token) is not None:
        return True
    return _is_non_empty_str(data.get("id_token_email"))


def validate_token_dict(
    data: object,
    *,
    return_tuple: bool = False,
    require_refresh_token: bool = True,
) -> Any:
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

        required_token_keys: tuple[str, ...] = (
            ("access_token", "refresh_token", "token_type", "expiry")
            if require_refresh_token
            else ("access_token", "token_type", "expiry")
        )
        missing_keys = [k for k in required_token_keys if not _is_non_empty_str(token_obj.get(k))]
        if missing_keys:
            raise InvalidTokenError(f"Missing required token fields: {', '.join(missing_keys)}")

        if not _is_non_empty_str(data.get("auth_method")):
            raise InvalidTokenError("Missing required 'auth_method' field.")

        if not has_valid_id_token_or_fallback(data):
            raise InvalidTokenError("Invalid 'id_token' JWT field.")

        if return_tuple:
            return True, None
        return data
    except InvalidTokenError as exc:
        if return_tuple:
            return False, str(exc)
        raise


def validate_token_file(
    token_path: Path | str,
    *,
    return_tuple: bool = False,
    require_refresh_token: bool = True,
) -> Any:
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
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        err = f"Failed to read/parse token JSON: {e}"
        if return_tuple:
            return False, err, None
        raise InvalidTokenError(err) from e

    if return_tuple:
        ok, msg = validate_token_dict(
            data,
            return_tuple=True,
            require_refresh_token=require_refresh_token,
        )
        return ok, msg, data if ok else None

    return validate_token_dict(data, require_refresh_token=require_refresh_token)


def check_token_dict(data: object) -> tuple[bool, str | None]:
    """Validates token dictionary returning (is_valid, error_message)."""
    return cast(tuple[bool, str | None], validate_token_dict(data, return_tuple=True))


def check_token_file(token_path: Path | str) -> tuple[bool, str | None, dict[str, Any] | None]:
    """Validates token file returning (is_valid, error_message, token_dict)."""
    return cast(tuple[bool, str | None, dict[str, Any] | None], validate_token_file(token_path, return_tuple=True))


def read_latest_jsonl_record(target_path: Path | str) -> dict[str, Any] | None:
    """Reads and parses the trailing JSON object record from a JSONL transcript file."""
    path = Path(target_path)
    try:
        size = path.stat().st_size
        if size == 0:
            return None
        max_read = 4 * 1024 * 1024
        chunk_size = min(size, 65536)
        with open(path, "rb") as f:
            while True:
                f.seek(max(0, size - chunk_size))
                data = f.read(min(chunk_size, size))
                last_line_str: str | None = None
                for line in reversed(data.splitlines()):
                    s = line.strip().decode("utf-8", errors="replace")
                    if s:
                        last_line_str = s
                        break
                if not last_line_str:
                    if chunk_size < size and chunk_size < max_read:
                        chunk_size = min(size, chunk_size * 4, max_read)
                        continue
                    return None
                if not last_line_str.startswith("{") and chunk_size < size and chunk_size < max_read:
                    chunk_size = min(size, chunk_size * 4, max_read)
                    continue
                if not last_line_str.startswith("{"):
                    return None
                try:
                    parsed = json.loads(last_line_str)
                    return cast(dict[str, Any], parsed) if isinstance(parsed, dict) else None
                except (json.JSONDecodeError, ValueError, RecursionError):
                    if chunk_size < size and chunk_size < max_read:
                        chunk_size = min(size, chunk_size * 4, max_read)
                        continue
                    return None
    except OSError:
        return None


__all__ = [
    "_atomic_write_file",
    "_atomic_write_json",
    "read_json_dict_nofollow",
    "read_json_dict_safe",
    "is_safe_account_alias",
    "parse_positive_finite_float",
    "parse_pending_reload_payload",
    "extract_email_from_jwt",
    "has_valid_id_token_or_fallback",
    "resolve_token_email",
    "extract_token_identity",
    "token_credentials_match",
    "validate_token_dict",
    "validate_token_file",
    "check_token_dict",
    "check_token_file",
    "read_latest_jsonl_record",
]
