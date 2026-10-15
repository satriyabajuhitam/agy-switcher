#!/usr/bin/env python3
"""Comprehensive unit tests for Layer 2 Vault Engine and Layer 3 PreInvocation Hook modularization."""

import base64
import io
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from agy_switcher.exceptions import (
    AccountNotFoundError,
    ActiveTurnRunningError,
    AgySwitchError,
    InvalidTokenError,
)
from agy_switcher.doctor import DoctorEngine, DEFAULT_GEMINI_DIR
from agy_switcher.supervisor import ProcessSupervisor
from agy_switcher.vault import (
    AgySwitchEngine,
    _atomic_write_file,
    _atomic_write_json,
    extract_email_from_jwt,
    validate_token_dict,
    validate_token_file,
)
import agy_switcher.telemetry as telemetry_mod
from agy_switcher.hook import (
    handle_pre_invocation_hook,
    _is_stale_exhausted_cache,
    _resolve_symbol,
    _get_telemetry_fetchers,
    _get_quota_client_class,
    _get_rotation_engine_class,
)



def create_sample_token(email: str = "test@example.com") -> dict:
    import base64
    payload = json.dumps({"email": email, "sub": "12345", "aud": "test_aud"}).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(payload).decode("utf-8").rstrip("=")
    return {
        "token": {
            "access_token": f"ya29.sample_access_token_for_{email}",
            "token_type": "Bearer",
            "refresh_token": f"1//sample_refresh_token_for_{email}",
            "expiry": "2026-10-30T12:00:00Z",
        },
        "auth_method": "oauth",
        "id_token": f"header.{payload_b64}.signature",
    }


class TestVaultEngineModular(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)
        self.vault_dir = self.base_path / ".antigravity-vault"
        self.gemini_dir = self.base_path / ".gemini"
        self.legacy_root = self.base_path / ".agy-cli-manager"
        self.proc_dir = self.base_path / "proc"
        self.proc_dir.mkdir(parents=True, exist_ok=True)
        self.engine = AgySwitchEngine(
            vault_dir=self.vault_dir,
            gemini_dir=self.gemini_dir,
            legacy_root=self.legacy_root,
            proc_dir=self.proc_dir,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_unified_facade_composition(self):
        """AgySwitchEngine must compose ProcessSupervisor and DoctorEngine."""
        self.assertIsInstance(self.engine.supervisor, ProcessSupervisor)
        self.assertIsInstance(self.engine.doctor, DoctorEngine)
        self.assertEqual(self.engine.supervisor.proc_dir, self.proc_dir)
        self.assertEqual(self.engine.supervisor.gemini_dir, self.gemini_dir)

    def test_posix_permissions_enforced(self):
        """Vault directories must have 0700 permissions and token files 0600."""
        self.assertEqual(stat.S_IMODE(self.vault_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.engine.accounts_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.engine.cache_dir.stat().st_mode), 0o700)

        tok_file = self.base_path / "test_tok.json"
        _atomic_write_json(tok_file, create_sample_token())
        self.assertEqual(stat.S_IMODE(tok_file.stat().st_mode), 0o600)

    def test_dual_destination_atomic_swap(self):
        """switch_tokens and switch_account must write atomically to desktop and cli token files with 0600."""
        # Add account
        tok_data = create_sample_token("user1@example.com")
        acc_dir = self.engine.accounts_dir / "user1"
        acc_dir.mkdir(parents=True, exist_ok=True)
        (acc_dir / "token.json").write_text(json.dumps(tok_data), encoding="utf-8")

        # Mock supervisor restart to avoid real process signals
        with patch.object(self.engine.supervisor, "restart_language_server", return_value=True):
            res = self.engine.switch_tokens("user1", restart_server=True)

        self.assertEqual(res["active"], "user1")
        self.assertEqual(res["email"], "user1@example.com")
        self.assertTrue(res["server_restarted"])

        desktop_file = self.engine.desktop_token_file
        cli_file = self.engine.cli_token_file
        self.assertTrue(desktop_file.is_file())
        self.assertTrue(cli_file.is_file())
        self.assertEqual(stat.S_IMODE(desktop_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(cli_file.stat().st_mode), 0o600)

        # Also test switch_account alias
        with patch.object(self.engine.supervisor, "restart_language_server", return_value=False):
            res_alias = self.engine.switch_account("user1", no_restart=True)
        self.assertEqual(res_alias["active"], "user1")
        self.assertFalse(res_alias["server_restarted"])

    def test_transparent_delegation_supervisor_and_doctor(self):
        """Engine must delegate supervisor and doctor methods transparently."""
        with patch.object(self.engine.supervisor, "locate_language_server_pid", return_value=4242):
            self.assertEqual(self.engine.locate_language_server_pid(), 4242)

        with patch.object(self.engine.supervisor, "is_server_busy", return_value=True):
            self.assertTrue(self.engine.is_server_busy(4242))

        with patch.object(self.engine.doctor, "run_diagnostics") as mock_diag:
            mock_diag.return_value = "report_mock"
            self.assertEqual(self.engine.run_diagnostics(), "report_mock")
            self.assertEqual(self.engine.run_doctor(), "report_mock")

        with patch.object(self.engine.doctor, "fix_diagnostics") as mock_fix:
            mock_fix.return_value = ["fixed_something"]
            self.assertEqual(self.engine.fix_diagnostics("report"), ["fixed_something"])
            self.assertEqual(self.engine.auto_heal("report"), ["fixed_something"])

        # Expiry checks
        self.assertFalse(self.engine._is_valid_expiry(True))
        self.assertFalse(self.engine._is_valid_expiry(False))
        self.assertTrue(self.engine._is_valid_expiry(1700000000))
        self.assertTrue(self.engine._is_valid_expiry("2026-10-15T00:00:00Z"))

    def test_install_hook_points_to_agy_switch_launcher(self):
        """install_hook must point to scripts/agy_switch.py launcher."""
        hooks_file = self.gemini_dir / "config" / "hooks.json"
        res_path = self.engine.install_hook(hooks_file=hooks_file)
        self.assertEqual(res_path, hooks_file)
        self.assertTrue(hooks_file.is_file())
        data = json.loads(hooks_file.read_text(encoding="utf-8"))
        self.assertIn("agy-switch-auto-rotate", data)
        cmd = data["agy-switch-auto-rotate"]["PreInvocation"][0]["command"]
        self.assertIn("agy_switch.py", cmd)
        self.assertIn("hook-pre-invocation", cmd)


class TestHookLifecycleModular(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)
        self.vault_dir = self.base_path / ".antigravity-vault"
        self.gemini_dir = self.base_path / ".gemini"
        self.legacy_root = self.base_path / ".agy-cli-manager"
        self.engine = AgySwitchEngine(
            vault_dir=self.vault_dir,
            gemini_dir=self.gemini_dir,
            legacy_root=self.legacy_root,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_hook_auto_switch_disabled_returns_empty_json(self):
        """When auto-switch is disabled, hook must output {} and return 0."""
        self.engine.set_auto_switch(False)
        out = io.StringIO()
        with patch("sys.stdin", io.StringIO('{"modelName": "gemini-3.8-pro"}')):
            with patch("sys.stdout", out):
                ret = handle_pre_invocation_hook(self.engine)
        self.assertEqual(ret, 0)
        self.assertEqual(json.loads(out.getvalue()), {})

    def test_hook_dynamic_proxy_bridge_resolves_mock(self):
        """Dynamic Module Proxy Bridge must resolve mocks on agy_switch if patched."""
        import agy_switch
        self.engine.set_auto_switch(True)
        acc_dir = self.engine.accounts_dir / "acc1"
        acc_dir.mkdir(parents=True, exist_ok=True)
        (acc_dir / "token.json").write_text(json.dumps(create_sample_token("acc1@example.com")), encoding="utf-8")
        self.engine.set_active_name("acc1")

        mock_fetch = MagicMock(return_value=("acc1", {"gemini": {"quota": 80}}))
        with patch.object(agy_switch, "fetch_account_quota", mock_fetch):
            out = io.StringIO()
            with patch("sys.stdin", io.StringIO('{"modelName": "gemini-3.8-pro"}')):
                with patch("sys.stdout", out):
                    ret = handle_pre_invocation_hook(self.engine)
            self.assertEqual(ret, 0)
            mock_fetch.assert_called_once()

    def test_hook_stale_cache_deduplicated_from_telemetry(self):
        """_is_stale_exhausted_cache in hook must be imported directly from telemetry."""
        self.assertIs(_is_stale_exhausted_cache, telemetry_mod._is_stale_exhausted_cache)


class TestSecurityLayer0(unittest.TestCase):
    """Unit tests for Layer 0 security.py module and token primitives."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_security_layer0_direct_import(self):
        """Security primitives must be importable from agy_switcher.security."""
        from agy_switcher.security import (
            _atomic_write_file,
            _atomic_write_json,
            extract_email_from_jwt,
            validate_token_dict,
            validate_token_file,
        )
        self.assertTrue(callable(_atomic_write_file))
        self.assertTrue(callable(_atomic_write_json))
        self.assertTrue(callable(extract_email_from_jwt))
        self.assertTrue(callable(validate_token_dict))
        self.assertTrue(callable(validate_token_file))

    def test_security_layer0_no_higher_layer_imports(self):
        """security.py must not import modules above Layer 0."""
        import ast
        security_file = REPO_ROOT / "scripts" / "agy_switcher" / "security.py"
        self.assertTrue(security_file.is_file())
        tree = ast.parse(security_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertFalse(alias.name.startswith("agy_switcher.supervisor"))
                    self.assertFalse(alias.name.startswith("agy_switcher.telemetry"))
                    self.assertFalse(alias.name.startswith("agy_switcher.vault"))
                    self.assertFalse(alias.name.startswith("agy_switcher.rotation"))
                    self.assertFalse(alias.name.startswith("agy_switcher.doctor"))
                    self.assertFalse(alias.name.startswith("agy_switcher.hook"))
                    self.assertFalse(alias.name.startswith("agy_switcher.cli"))
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                self.assertFalse(mod.startswith("agy_switcher.supervisor"))
                self.assertFalse(mod.startswith("agy_switcher.telemetry"))
                self.assertFalse(mod.startswith("agy_switcher.vault"))
                self.assertFalse(mod.startswith("agy_switcher.rotation"))
                self.assertFalse(mod.startswith("agy_switcher.doctor"))
                self.assertFalse(mod.startswith("agy_switcher.hook"))
                self.assertFalse(mod.startswith("agy_switcher.cli"))

    def test_atomic_write_file_permissions(self):
        from agy_switcher.security import _atomic_write_file
        target = self.base_path / "sub" / "secret.txt"
        _atomic_write_file(target, "secret_content", mode=0o600)
        self.assertTrue(target.is_file())
        self.assertEqual(target.read_text(encoding="utf-8"), "secret_content")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(target.parent.stat().st_mode), 0o700)

    def test_atomic_write_json_custom_mode_and_indent(self):
        from agy_switcher.security import _atomic_write_json
        target = self.base_path / "custom.json"
        _atomic_write_json(target, {"key": "value"}, mode=0o644, indent=4)
        self.assertTrue(target.is_file())
        data = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(data, {"key": "value"})
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)

    def test_extract_email_from_jwt_edge_cases(self):
        from agy_switcher.security import extract_email_from_jwt
        tok = create_sample_token("user@example.com")
        self.assertEqual(extract_email_from_jwt(tok["id_token"]), "user@example.com")
        self.assertIsNone(extract_email_from_jwt(""))
        self.assertIsNone(extract_email_from_jwt("corrupt"))
        self.assertIsNone(extract_email_from_jwt("a.b"))
        self.assertIsNone(extract_email_from_jwt("a.not_json.c"))

    def test_validate_token_dict_and_file_edge_cases(self):
        from agy_switcher.security import validate_token_dict, validate_token_file
        tok = create_sample_token("valid@example.com")
        self.assertEqual(validate_token_dict(tok), tok)

        with self.assertRaises(InvalidTokenError):
            validate_token_dict("not-a-dict")
        with self.assertRaises(InvalidTokenError):
            validate_token_dict({})
        with self.assertRaises(InvalidTokenError):
            validate_token_dict({"token": "not-a-dict"})
        with self.assertRaises(InvalidTokenError):
            validate_token_dict({"token": {"access_token": "a"}, "auth_method": "oauth"})
        with self.assertRaises(InvalidTokenError):
            validate_token_dict({
                "token": {"access_token": "a", "refresh_token": "r", "token_type": "Bearer", "expiry": "e"},
                "id_token": "jwt",
            })

        token_file = self.base_path / "token.json"
        with self.assertRaises(InvalidTokenError):
            validate_token_file(token_file)

        token_file.write_text("invalid json", encoding="utf-8")
        with self.assertRaises(InvalidTokenError):
            validate_token_file(token_file)

        token_file.write_text(json.dumps(tok), encoding="utf-8")
        self.assertEqual(validate_token_file(token_file), tok)


class TestDoctorHardening(unittest.TestCase):
    """Unit tests for DoctorEngine hardening (positional 3-arg init & watch.log audit)."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)
        self.vault_dir = self.base_path / ".antigravity-vault"
        self.accounts_dir = self.vault_dir / "accounts"
        self.cache_dir = self.vault_dir / "cache"
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        self.accounts_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.vault_dir, 0o700)
        os.chmod(self.accounts_dir, 0o700)
        os.chmod(self.cache_dir, 0o700)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_doctor_engine_init_positional_3_arguments(self):
        """DoctorEngine(vault_dir, accounts_dir, cache_dir) maps directories accurately."""
        custom_accounts = self.base_path / "custom_accounts"
        custom_cache = self.base_path / "custom_cache"
        doc = DoctorEngine(self.vault_dir, custom_accounts, custom_cache)

        self.assertEqual(doc.vault_dir, self.vault_dir)
        self.assertEqual(doc.accounts_dir, custom_accounts)
        self.assertEqual(doc.cache_dir, custom_cache)
        # Must NOT map custom_accounts to gemini_dir or custom_cache to proc_dir
        self.assertEqual(doc.gemini_dir, DEFAULT_GEMINI_DIR)
        self.assertEqual(doc.proc_dir, Path("/proc"))

    def test_doctor_engine_watch_log_permissions_audit_and_fix(self):
        """DoctorEngine._check_permissions audits watch.log (0600) and fixes insecure modes."""
        watch_log = self.vault_dir / "watch.log"
        watch_log.write_text("2026-10-15 [watch] started\n", encoding="utf-8")
        os.chmod(watch_log, 0o644)

        doc = DoctorEngine(vault_dir=self.vault_dir)
        report = doc.run_diagnostics()
        perm_item = next(it for it in report.items if it.category == "permissions")
        self.assertEqual(perm_item.status, "FAIL")
        self.assertTrue(perm_item.can_fix)
        self.assertIn("watch.log", perm_item.message)

        fixes = doc.fix_diagnostics(report)
        self.assertTrue(len(fixes) > 0)
        self.assertEqual(stat.S_IMODE(watch_log.stat().st_mode), 0o600)

        # Re-check after fix
        report2 = doc.run_diagnostics()
        perm_item2 = next(it for it in report2.items if it.category == "permissions")
        self.assertEqual(perm_item2.status, "PASS")

    def test_doctor_engine_init_positional_4_arguments_with_supervisor(self):
        """DoctorEngine(vault, gemini, proc, supervisor) handles legacy 4-arg signature cleanly without TypeError."""
        from agy_switcher.supervisor import ProcessSupervisor
        sup = ProcessSupervisor()
        doc = DoctorEngine(self.vault_dir, self.base_path / "custom_gemini", Path("/proc"), sup)

        self.assertEqual(doc.vault_dir, self.vault_dir)
        self.assertEqual(doc.gemini_dir, self.base_path / "custom_gemini")
        self.assertEqual(doc.proc_dir, Path("/proc"))
        self.assertIs(doc.supervisor, sup)
        self.assertEqual(doc.accounts_dir, self.vault_dir / "accounts")
        self.assertEqual(doc.cache_dir, self.vault_dir / "cache")
        self.assertEqual(doc.config_file, self.vault_dir / "config.json")

    def test_doctor_engine_init_positional_3_arguments_with_proc(self):
        """DoctorEngine(vault, gemini, /proc) maps /proc to proc_dir, NOT cache_dir."""
        doc = DoctorEngine(self.vault_dir, self.base_path / "custom_gemini", Path("/proc"))

        self.assertEqual(doc.vault_dir, self.vault_dir)
        self.assertEqual(doc.gemini_dir, self.base_path / "custom_gemini")
        self.assertEqual(doc.proc_dir, Path("/proc"))
        self.assertEqual(doc.accounts_dir, self.vault_dir / "accounts")
        self.assertEqual(doc.cache_dir, self.vault_dir / "cache")

    def test_doctor_engine_property_setters(self):
        """DoctorEngine property setters update custom paths dynamically."""
        doc = DoctorEngine(vault_dir=self.vault_dir)
        new_acc = self.base_path / "new_acc"
        new_cache = self.base_path / "new_cache"
        new_cfg = self.base_path / "new_cfg.json"
        new_desktop = self.base_path / "new_desktop"
        new_cli = self.base_path / "new_cli"

        doc.accounts_dir = new_acc
        doc.cache_dir = new_cache
        doc.config_file = new_cfg
        doc.desktop_token_file = new_desktop
        doc.cli_token_file = new_cli

        self.assertEqual(doc.accounts_dir, new_acc)
        self.assertEqual(doc.cache_dir, new_cache)
        self.assertEqual(doc.config_file, new_cfg)
        self.assertEqual(doc.desktop_token_file, new_desktop)
        self.assertEqual(doc.cli_token_file, new_cli)


class TestSecurityEdgeCasesAndTupleValidators(unittest.TestCase):
    """Unit tests for string paths, tuple validators, and defensive JWT edge cases in security.py."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_security_string_paths_support(self):
        """_atomic_write_file, _atomic_write_json, validate_token_file accept string paths."""
        from agy_switcher.security import _atomic_write_file, _atomic_write_json, validate_token_file
        str_file = str(self.base_path / "str_write.txt")
        _atomic_write_file(str_file, "string_path_content")
        self.assertTrue(Path(str_file).is_file())
        self.assertEqual(Path(str_file).read_text(encoding="utf-8"), "string_path_content")

        str_json = str(self.base_path / "str_write.json")
        _atomic_write_json(str_json, {"hello": "world"})
        self.assertTrue(Path(str_json).is_file())

        tok = create_sample_token("str@example.com")
        tok_file = str(self.base_path / "tok.json")
        Path(tok_file).write_text(json.dumps(tok), encoding="utf-8")
        val = validate_token_file(tok_file)
        self.assertEqual(val["auth_method"], "oauth")

    def test_security_tuple_validators(self):
        """check_token_dict and check_token_file return non-raising tuples."""
        from agy_switcher.security import check_token_dict, check_token_file, validate_token_dict, validate_token_file
        tok = create_sample_token("tuple@example.com")

        # Valid cases
        ok, err = check_token_dict(tok)
        self.assertTrue(ok)
        self.assertIsNone(err)

        ok2, err2 = validate_token_dict(tok, return_tuple=True)
        self.assertTrue(ok2)
        self.assertIsNone(err2)

        tok_file = self.base_path / "tuple_tok.json"
        tok_file.write_text(json.dumps(tok), encoding="utf-8")
        ok_f, err_f, data = check_token_file(tok_file)
        self.assertTrue(ok_f)
        self.assertIsNone(err_f)
        self.assertEqual(data, tok)

        ok_f2, err_f2, data2 = validate_token_file(tok_file, return_tuple=True)
        self.assertTrue(ok_f2)
        self.assertIsNone(err_f2)
        self.assertEqual(data2, tok)

        # Invalid cases
        ok_bad, err_bad = check_token_dict({"invalid": "schema"})
        self.assertFalse(ok_bad)
        self.assertIsNotNone(err_bad)

        bad_file = self.base_path / "nonexistent.json"
        ok_bad_f, err_bad_f, bad_data = check_token_file(bad_file)
        self.assertFalse(ok_bad_f)
        self.assertIsNotNone(err_bad_f)
        self.assertIsNone(bad_data)

    def test_extract_email_from_jwt_extra_defensive(self):
        """extract_email_from_jwt handles None, non-strings, and malformed email payloads."""
        from agy_switcher.security import extract_email_from_jwt
        self.assertIsNone(extract_email_from_jwt(None))
        self.assertIsNone(extract_email_from_jwt(12345))  # type: ignore

        # JWT with whitespace email or non-string email
        header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').decode("utf-8").rstrip("=")
        body_empty = base64.urlsafe_b64encode(b'{"email":""}').decode("utf-8").rstrip("=")
        body_ws = base64.urlsafe_b64encode(b'{"email":"   "}').decode("utf-8").rstrip("=")
        body_num = base64.urlsafe_b64encode(b'{"email":999}').decode("utf-8").rstrip("=")
        body_arr = base64.urlsafe_b64encode(b'[1, 2, 3]').decode("utf-8").rstrip("=")

        self.assertIsNone(extract_email_from_jwt(f"{header}.{body_empty}.sig"))
        self.assertIsNone(extract_email_from_jwt(f"{header}.{body_ws}.sig"))
        self.assertIsNone(extract_email_from_jwt(f"{header}.{body_num}.sig"))
        self.assertIsNone(extract_email_from_jwt(f"{header}.{body_arr}.sig"))


if __name__ == "__main__":
    unittest.main()

