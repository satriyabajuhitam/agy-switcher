import base64
import email.message
import io
import itertools
import json
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure the scripts directory is in the path
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'scripts')))

# Initialize test patterns and environment hooks
import tests.test_agy_switch  # noqa: F401

from agy_switch import (
    main,
    AgySwitchEngine,
    QuotaClient,
    RotationEngine,
    InvalidThresholdError,
    ActiveTurnRunningError,
)

class TestCLI(unittest.TestCase):

    def setUp(self):
        self.engine = MagicMock(spec=AgySwitchEngine)
        self.engine.list_accounts.return_value = {
            "active": "acc1",
            "accounts": [
                {"name": "acc1", "is_active": True, "valid": True, "email": "a@b.com"},
                {"name": "acc2", "is_active": False, "valid": True, "email": "c@d.com"}
            ]
        }
        self.engine.get_active_name.return_value = "acc1"
        self.engine.get_account_tokens.return_value = {"access_token": "token1", "refresh_token": "refresh1"}
        self.engine.get_status.return_value = {
            "active": "acc1",
            "email": "a@b.com",
            "server_running": False,
            "server_pid": None,
            "server_busy": False
        }

    @patch('agy_switch.QuotaClient')
    def test_list_quota(self, mock_qc_class):
        mock_qc = mock_qc_class.return_value
        mock_qc.fetch_quota.return_value = {"gemini": {"quota": 85, "reset_time": 1700000000}}

        with patch('sys.stdout') as mock_stdout:
            exit_code = main(["list", "--quota"], engine=self.engine)
            self.assertEqual(exit_code, 0)
            
        # Verify it uses threaded execution by checking fetch_quota was called multiple times
        self.assertEqual(mock_qc.fetch_quota.call_count, 2)

    @patch('agy_switch.datetime')
    @patch('agy_switch.QuotaClient')
    @patch('subprocess.Popen')
    def test_status_cache_expired(self, mock_popen, mock_qc_class, mock_datetime):
        mock_qc = mock_qc_class.return_value
        # Cache is 100 seconds old
        mock_qc.load_cache.return_value = {"timestamp": 1000, "accounts": {"acc1": {"gemini": {"quota": 85}}}}
        from datetime import timezone
        mock_datetime.now.return_value.timestamp.return_value = 1101
        mock_datetime.timezone = timezone
        
        with patch('sys.stdout'):
            exit_code = main(["status"], engine=self.engine)
            self.assertEqual(exit_code, 0)
        
        # Verify background probe was launched
        mock_popen.assert_called_once()
        args = mock_popen.call_args[0][0]
        self.assertIn("internal-probe", args)

    @patch('agy_switch.datetime')
    @patch('agy_switch.QuotaClient')
    @patch('subprocess.Popen')
    def test_status_cache_valid(self, mock_popen, mock_qc_class, mock_datetime):
        mock_qc = mock_qc_class.return_value
        # Cache is 30 seconds old
        mock_qc.load_cache.return_value = {"timestamp": 1000, "accounts": {"acc1": {"gemini": {"quota": 85}}}}
        from datetime import timezone
        mock_datetime.now.return_value.timestamp.return_value = 1030
        mock_datetime.timezone = timezone
        
        with patch('sys.stdout'):
            exit_code = main(["status"], engine=self.engine)
            self.assertEqual(exit_code, 0)
        
        # Verify no probe
        mock_popen.assert_not_called()

    @patch('agy_switch.RotationEngine')
    @patch('agy_switch.QuotaClient')
    def test_switch_auto_optimal(self, mock_qc_class, mock_re_class):
        mock_qc = mock_qc_class.return_value
        mock_qc.fetch_quota.return_value = {"gemini": {"quota": 85}}
        
        mock_re = mock_re_class.return_value
        mock_re.evaluate.return_value = None  # Current active is optimal (returns None)
        
        with patch('sys.stdout') as mock_stdout:
            exit_code = main(["switch", "--auto"], engine=self.engine)
            self.assertEqual(exit_code, 0)
        
        # Verify os.replace/switch_tokens wasn't called because it's optimal
        self.engine.switch_tokens.assert_not_called()

    @patch('agy_switch.RotationEngine')
    @patch('agy_switch.QuotaClient')
    def test_switch_auto_rotate(self, mock_qc_class, mock_re_class):
        mock_qc = mock_qc_class.return_value
        mock_qc.fetch_quota.return_value = {"gemini": {"quota": 10}}
        
        mock_re = mock_re_class.return_value
        mock_re.evaluate.return_value = "acc2" # Needs rotation
        
        with patch('sys.stdout'):
            exit_code = main(["switch", "--auto"], engine=self.engine)
            self.assertEqual(exit_code, 0)
        
        # Verify rotation defaults force=True under ADR 0010
        self.engine.switch_tokens.assert_called_once_with("acc2", restart_server=True, force=True)

    @patch('agy_switch.RotationEngine')
    @patch('agy_switch.QuotaClient')
    def test_switch_auto_rotate_no_restart(self, mock_qc_class, mock_re_class):
        mock_qc = mock_qc_class.return_value
        mock_qc.fetch_quota.return_value = {"gemini": {"quota": 10}}

        mock_re = mock_re_class.return_value
        mock_re.evaluate.return_value = "acc2"

        with patch('sys.stdout'):
            exit_code = main(["switch", "--auto", "--no-restart"], engine=self.engine)
            self.assertEqual(exit_code, 0)

        # Verify auto-switch with --no-restart passes restart_server=False and force=True
        self.engine.switch_tokens.assert_called_once_with("acc2", restart_server=False, force=True)

    def test_manual_switch_default_force_false(self):
        with patch('sys.stdout'):
            exit_code = main(["switch", "acc2"], engine=self.engine)
            self.assertEqual(exit_code, 0)

        # Verify manual switch preserves force=False by default (turn safety) and enables verify_auth=True
        self.engine.switch_tokens.assert_called_once_with(
            "acc2", restart_server=True, force=False, verify_auth=True
        )

    def test_manual_switch_with_force(self):
        with patch('sys.stdout'):
            exit_code = main(["switch", "acc2", "--force"], engine=self.engine)
            self.assertEqual(exit_code, 0)

        # Verify manual switch with --force passes force=True while keeping verify_auth=True
        self.engine.switch_tokens.assert_called_once_with(
            "acc2", restart_server=True, force=True, verify_auth=True
        )

    def test_manual_switch_no_restart(self):
        with patch('sys.stdout'):
            exit_code = main(["switch", "acc2", "--no-restart"], engine=self.engine)
            self.assertEqual(exit_code, 0)

        # Verify manual switch with --no-restart passes restart_server=False, force=False, verify_auth=True
        self.engine.switch_tokens.assert_called_once_with(
            "acc2", restart_server=False, force=False, verify_auth=True
        )

    def test_manual_switch_active_turn_error(self):
        self.engine.switch_tokens.side_effect = ActiveTurnRunningError(
            "Active turn running on language_server PID 12345"
        )
        err_buf = io.StringIO()
        with patch('sys.stderr', err_buf):
            exit_code = main(["switch", "acc2"], engine=self.engine)
            self.assertEqual(exit_code, 2)

        self.assertIn("Warning: Active turn running on language_server PID 12345", err_buf.getvalue())
        self.engine.switch_tokens.assert_called_once_with(
            "acc2", restart_server=True, force=False, verify_auth=True
        )

    def test_manual_switch_with_force_and_no_restart(self):
        with patch('sys.stdout'):
            exit_code = main(["switch", "acc2", "--force", "--no-restart"], engine=self.engine)
            self.assertEqual(exit_code, 0)

        # Verify manual switch with both --force and --no-restart keeps verify_auth=True
        self.engine.switch_tokens.assert_called_once_with(
            "acc2", restart_server=False, force=True, verify_auth=True
        )

    @patch('agy_switch.RotationEngine')
    @patch('agy_switch.QuotaClient')
    def test_switch_auto_with_explicit_force(self, mock_qc_class, mock_re_class):
        mock_qc = mock_qc_class.return_value
        mock_qc.fetch_quota.return_value = {"gemini": {"quota": 10}}

        mock_re = mock_re_class.return_value
        mock_re.evaluate.return_value = "acc2"

        with patch('sys.stdout'):
            exit_code = main(["switch", "--auto", "--force"], engine=self.engine)
            self.assertEqual(exit_code, 0)

        # Verify auto-switch with explicit --force preserves force=True
        self.engine.switch_tokens.assert_called_once_with("acc2", restart_server=True, force=True)

    @patch('agy_switch.QuotaClient')
    def test_quota_subcommand_table(self, mock_qc_class):
        mock_qc = mock_qc_class.return_value
        mock_qc.fetch_quota.return_value = {
            "gemini": {
                "quota": 85,
                "reset_time": 1700000000,
                "cached": False,
                "buckets": {"5h": {"quota": 85, "reset_time": 1700000000}},
            }
        }
        mock_qc.load_cache.return_value = {}

        buf = io.StringIO()
        with patch('sys.stdout', buf):
            exit_code = main(["quota"], engine=self.engine)
            self.assertEqual(exit_code, 0)

        output = buf.getvalue()
        self.assertIn("ACCOUNT", output)
        self.assertIn("GEMINI QUOTA", output)
        self.assertIn("acc1", output)
        self.assertIn("acc2", output)
        self.assertEqual(mock_qc.fetch_quota.call_count, 2)

    @patch('agy_switch.QuotaClient')
    def test_quota_subcommand_list_format(self, mock_qc_class):
        mock_qc = mock_qc_class.return_value
        mock_qc.fetch_quota.return_value = {
            "gemini": {
                "quota": 80,
                "reset_time": 1700000000,
                "cached": False,
                "buckets": {"5h": {"quota": 80, "reset_time": 1700000000}},
            }
        }
        mock_qc.load_cache.return_value = {}

        buf = io.StringIO()
        with patch('sys.stdout', buf):
            exit_code = main(["quota", "--format", "list"], engine=self.engine)
            self.assertEqual(exit_code, 0)

        output = buf.getvalue()
        self.assertIn("• [*] acc1 (a@b.com) — Gemini: ", output)
        self.assertIn("• [ ] acc2 (c@d.com) — Gemini: ", output)
        self.assertIn("Active Account: acc1", output)

    @patch('agy_switch.QuotaClient')
    def test_quota_subcommand_json(self, mock_qc_class):
        mock_qc = mock_qc_class.return_value
        mock_qc.fetch_quota.return_value = {
            "gemini": {
                "quota": 80,
                "reset_time": 1791201600,
                "cached": False,
                "buckets": {
                    "5h": {"quota": 80, "reset_time": 1791201600},
                    "weekly": {"quota": 95, "reset_time": 1791633600},
                },
            }
        }
        mock_qc.load_cache.return_value = {}

        buf = io.StringIO()
        with patch('sys.stdout', buf):
            exit_code = main(["quota", "--json"], engine=self.engine)
            self.assertEqual(exit_code, 0)

        data = json.loads(buf.getvalue())
        self.assertEqual(data["active"], "acc1")
        self.assertEqual(len(data["accounts"]), 2)
        acc1 = data["accounts"][0]
        self.assertEqual(acc1["name"], "acc1")
        self.assertEqual(acc1["email"], "a@b.com")
        self.assertTrue(acc1["is_active"])
        self.assertTrue(acc1["valid"])
        self.assertIn("gemini", acc1["quota"])
        self.assertEqual(acc1["quota"]["gemini"]["quota"], 80)
        self.assertFalse(acc1["quota"]["gemini"]["cached"])
        self.assertIn("buckets", acc1["quota"]["gemini"])

    @patch('agy_switch.QuotaClient')
    def test_quota_subcommand_json_excises_claude(self, mock_qc_class):
        """[QUOTA-GEMINI-EDGE-001] agy-switch quota --json strictly retains Gemini and strips Claude."""
        mock_qc = mock_qc_class.return_value
        mock_qc.fetch_quota.return_value = {
            "gemini": {
                "quota": 80,
                "reset_time": 1791201600,
                "cached": False,
                "buckets": {"5h": {"quota": 80, "reset_time": 1791201600}},
            },
            "claude": {
                "quota": 60,
                "reset_time": 1791201600,
                "cached": False,
                "buckets": {"5h": {"quota": 60, "reset_time": 1791201600}},
            },
        }
        mock_qc.load_cache.return_value = {}

        buf = io.StringIO()
        with patch('sys.stdout', buf):
            exit_code = main(["quota", "--json"], engine=self.engine)
            self.assertEqual(exit_code, 0)

        data = json.loads(buf.getvalue())
        for acc in data["accounts"]:
            self.assertIn("gemini", acc["quota"])
            self.assertNotIn("claude", acc["quota"])
            self.assertEqual(set(acc["quota"].keys()), {"gemini"})

    def test_quota_subcommand_model_claude_sorting(self):
        """agy-switch quota --model claude is rejected with exit code 1."""
        buf = io.StringIO()
        with patch('sys.stderr', buf):
            exit_code = main(["quota", "--model", "claude", "--json"], engine=self.engine)
            self.assertEqual(exit_code, 1)
        self.assertIn("Unsupported model 'claude'", buf.getvalue())
        self.assertIn("Allowed model is strictly 'gemini'", buf.getvalue())

    def test_quota_subcommand_invalid_model_error(self):
        """agy-switch quota --model invalid outputs error and returns exit code 1."""
        buf = io.StringIO()
        with patch('sys.stderr', buf):
            exit_code = main(["quota", "--model", "unsupported"], engine=self.engine)
            self.assertEqual(exit_code, 1)
        self.assertIn("Unsupported model 'unsupported'", buf.getvalue())

    @patch('agy_switch.QuotaClient')
    def test_quota_model_forwarding(self, mock_qc_class):
        """[REQ-3-SCN-005] and [REQ-3-ERR-001]: CLI forwards target_model to formatters and handles errors."""
        mock_qc = mock_qc_class.return_value
        mock_qc.fetch_quota.return_value = {
            "gemini": {"quota": 80, "reset_time": 1791201600, "cached": False, "buckets": {}},
        }
        mock_qc.load_cache.return_value = {}

        buf_table = io.StringIO()
        with patch('sys.stdout', buf_table):
            exit_code = main(["quota", "--model", "gemini"], engine=self.engine)
            self.assertEqual(exit_code, 0)

        output_table = buf_table.getvalue()
        self.assertIn("GEMINI QUOTA", output_table)
        self.assertIn("80%", output_table)

        buf_listicle = io.StringIO()
        with patch('sys.stdout', buf_listicle):
            exit_code = main(["quota", "--model", "gemini-3.8-pro", "--format", "list"], engine=self.engine)
            self.assertEqual(exit_code, 0)

        output_listicle = buf_listicle.getvalue()
        self.assertIn("— Gemini: 80%", output_listicle)

        buf_err = io.StringIO()
        with patch('sys.stderr', buf_err):
            exit_code = main(["quota", "--model", "claude"], engine=self.engine)
            self.assertEqual(exit_code, 1)

        self.assertIn("Unsupported model 'claude'", buf_err.getvalue())

    @patch('agy_switch.QuotaClient')
    def test_list_command_model_forwarding(self, mock_qc_class):
        """agy-switch list forwards target_model with --quota and validates --model."""
        mock_qc = mock_qc_class.return_value
        mock_qc.fetch_quota.return_value = {
            "gemini": {"quota": 80, "reset_time": 1791201600, "cached": False, "buckets": {}},
        }
        mock_qc.load_cache.return_value = {}

        buf_table = io.StringIO()
        with patch('sys.stdout', buf_table):
            exit_code = main(["list", "--quota", "--model", "gemini"], engine=self.engine)
            self.assertEqual(exit_code, 0)
        self.assertIn("GEMINI QUOTA", buf_table.getvalue())
        self.assertIn("80%", buf_table.getvalue())

        buf_claude = io.StringIO()
        with patch('sys.stderr', buf_claude):
            exit_code = main(["list", "--quota", "--model", "claude"], engine=self.engine)
            self.assertEqual(exit_code, 1)
        self.assertIn("Unsupported model 'claude'", buf_claude.getvalue())

        buf_err = io.StringIO()
        with patch('sys.stderr', buf_err):
            exit_code = main(["list", "--model", "unsupported"], engine=self.engine)
            self.assertEqual(exit_code, 1)
        self.assertIn("Unsupported model 'unsupported'", buf_err.getvalue())

    @patch('agy_switch.QuotaClient')
    def test_quota_model_slug_normalization(self, mock_qc_class):
        """agy-switch quota normalizes model slugs and case variants."""
        mock_qc = mock_qc_class.return_value
        mock_qc.fetch_quota.return_value = {
            "gemini": {"quota": 80, "reset_time": 1791201600, "cached": False, "buckets": {}},
        }
        mock_qc.load_cache.return_value = {}

        buf_slug = io.StringIO()
        with patch('sys.stdout', buf_slug):
            exit_code = main(["quota", "--model", "gemini-3.8-flash"], engine=self.engine)
            self.assertEqual(exit_code, 0)
        self.assertIn("GEMINI QUOTA", buf_slug.getvalue())
        self.assertIn("80%", buf_slug.getvalue())

        buf_case = io.StringIO()
        with patch('sys.stdout', buf_case):
            exit_code = main(["quota", "--model", "GEMINI"], engine=self.engine)
            self.assertEqual(exit_code, 0)
        self.assertIn("GEMINI QUOTA", buf_case.getvalue())

        buf_claude = io.StringIO()
        with patch('sys.stderr', buf_claude):
            exit_code = main(["quota", "--model", "claude-3-5-sonnet"], engine=self.engine)
            self.assertEqual(exit_code, 1)
        self.assertIn("Unsupported model 'claude-3-5-sonnet'", buf_claude.getvalue())

    def test_list_format_listicle(self):
        buf = io.StringIO()
        with patch('sys.stdout', buf):
            exit_code = main(["list", "--format", "list"], engine=self.engine)
            self.assertEqual(exit_code, 0)

        output = buf.getvalue()
        self.assertIn("• [*] acc1 (a@b.com) [Active]", output)
        self.assertIn("• [ ] acc2 (c@d.com) [Standby]", output)
        self.assertIn("Active Account: acc1", output)
        self.assertNotIn("----------------", output)
        self.assertNotIn("GEMINI QUOTA", output)

    @patch('agy_switch.QuotaClient')
    def test_list_quota_backward_compatible(self, mock_qc_class):
        mock_qc = mock_qc_class.return_value
        mock_qc.fetch_quota.return_value = {
            "gemini": {
                "quota": 85,
                "reset_time": 1700000000,
                "cached": False,
                "buckets": {"5h": {"quota": 85, "reset_time": 1700000000}},
            }
        }
        mock_qc.load_cache.return_value = {}

        # 1. Table format (default)
        buf_list = io.StringIO()
        with patch('sys.stdout', buf_list):
            exit_code_list = main(["list", "--quota"], engine=self.engine)
            self.assertEqual(exit_code_list, 0)

        buf_quota = io.StringIO()
        with patch('sys.stdout', buf_quota):
            exit_code_quota = main(["quota"], engine=self.engine)
            self.assertEqual(exit_code_quota, 0)

        self.assertEqual(buf_list.getvalue(), buf_quota.getvalue())

        # 2. Listicle format (--format list)
        buf_list_fmt = io.StringIO()
        with patch('sys.stdout', buf_list_fmt):
            exit_code_list_fmt = main(["list", "--quota", "--format", "list"], engine=self.engine)
            self.assertEqual(exit_code_list_fmt, 0)

        buf_quota_fmt = io.StringIO()
        with patch('sys.stdout', buf_quota_fmt):
            exit_code_quota_fmt = main(["quota", "--format", "list"], engine=self.engine)
            self.assertEqual(exit_code_quota_fmt, 0)

        self.assertEqual(buf_list_fmt.getvalue(), buf_quota_fmt.getvalue())

        # 3. JSON format (--json)
        buf_list_json = io.StringIO()
        with patch('sys.stdout', buf_list_json):
            exit_code_list_json = main(["list", "--quota", "--json"], engine=self.engine)
            self.assertEqual(exit_code_list_json, 0)

        buf_quota_json = io.StringIO()
        with patch('sys.stdout', buf_quota_json):
            exit_code_quota_json = main(["quota", "--json"], engine=self.engine)
            self.assertEqual(exit_code_quota_json, 0)

        self.assertEqual(buf_list_json.getvalue(), buf_quota_json.getvalue())

    def test_cache_merge_invariant_on_partial_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            engine = AgySwitchEngine(vault_dir=tmp_path / "vault")
            
            # Setup two accounts in engine
            acc1_dir = engine.accounts_dir / "acc1"
            acc1_dir.mkdir(parents=True)
            token_payload = {
                "token": {"access_token": "tok1", "refresh_token": "ref1", "token_type": "Bearer", "expiry": "2026-10-15T00:00:00Z"},
                "auth_method": "oauth",
                "id_token": "eyJhbGciOiJIUzI1NiJ9.eyJlbWFpbCI6ImExQGIuY29tIn0.sig",
            }
            (acc1_dir / "token.json").write_text(json.dumps(token_payload))

            acc2_dir = engine.accounts_dir / "acc2"
            acc2_dir.mkdir(parents=True)
            token_payload2 = {
                "token": {"access_token": "tok2", "refresh_token": "ref2", "token_type": "Bearer", "expiry": "2026-10-15T00:00:00Z"},
                "auth_method": "oauth",
                "id_token": "eyJhbGciOiJIUzI1NiJ9.eyJlbWFpbCI6ImEyQGIuY29tIn0.sig",
            }
            (acc2_dir / "token.json").write_text(json.dumps(token_payload2))
            engine.set_active_name("acc1")

            # Seed existing cache on disk with acc1 (60%) and acc2 (40%)
            cached_initial = {
                "timestamp": 1000.0,
                "accounts": {
                    "acc1": {"gemini": {"quota": 60, "reset_time": 1700000000, "buckets": {"5h": {"quota": 60, "reset_time": 1700000000}}}},
                    "acc2": {"gemini": {"quota": 40, "reset_time": 1700005000, "buckets": {"5h": {"quota": 40, "reset_time": 1700005000}}}},
                }
            }
            client = QuotaClient(timeout=1.0, engine=engine)
            client.save_cache(cached_initial, merge=False)

            # Live fetch: acc1 succeeds (90%), acc2 fails (raises exception/error)
            def mock_fetch(tok, **kwargs):
                if tok == "tok1":
                    return {
                        "gemini": {
                            "quota": 90,
                            "reset_time": 1700001000,
                            "cached": False,
                            "buckets": {"5h": {"quota": 90, "reset_time": 1700001000}},
                        }
                    }
                raise TimeoutError("Network timeout")

            with patch.object(QuotaClient, 'fetch_quota', side_effect=mock_fetch):
                buf = io.StringIO()
                with patch('sys.stdout', buf):
                    exit_code = main(["quota", "--format", "list"], engine=engine)
                    self.assertEqual(exit_code, 0)

            # Verify Cache Merge Invariant in cache file
            saved_cache = client.load_cache()
            self.assertIn("acc1", saved_cache["accounts"])
            self.assertIn("acc2", saved_cache["accounts"])
            self.assertEqual(saved_cache["accounts"]["acc1"]["gemini"]["quota"], 90)
            # acc2 must NOT be purged! Preserved historical entry
            self.assertEqual(saved_cache["accounts"]["acc2"]["gemini"]["quota"], 40)

            # Verify CLI display: acc2 shows (Cached)
            output = buf.getvalue()
            self.assertIn("acc1", output)
            self.assertIn("90%", output)
            self.assertIn("acc2", output)
            self.assertIn("40% (Cached)", output)

    def test_offline_accounts_with_and_without_cache(self):
        engine = MagicMock(spec=AgySwitchEngine)
        engine.list_accounts.return_value = {
            "active": "acc1",
            "accounts": [
                {"name": "acc1", "is_active": True, "valid": True, "email": "a@b.com"},
                {"name": "acc2", "is_active": False, "valid": True, "email": "c@d.com"},
                {"name": "acc3", "is_active": False, "valid": True, "email": "e@f.com"},
            ]
        }
        engine.get_account_tokens.side_effect = lambda name: {"access_token": f"tok_{name}", "refresh_token": f"ref_{name}"}
        engine.get_active_name.return_value = "acc1"

        with patch('agy_switch.QuotaClient') as mock_qc_class:
            mock_qc = mock_qc_class.return_value
            def mock_fetch(tok, **kwargs):
                if "tok_acc1" in tok:
                    return {
                        "gemini": {
                            "quota": 80,
                            "reset_time": 1700000000,
                            "cached": False,
                            "buckets": {"5h": {"quota": 80, "reset_time": 1700000000}},
                        }
                    }
                return {"error": "Connection refused"}

            mock_qc.fetch_quota.side_effect = mock_fetch
            mock_qc.load_cache.return_value = {
                "accounts": {
                    "acc2": {
                        "gemini": {
                            "quota": 50,
                            "reset_time": 1700002000,
                            "buckets": {"5h": {"quota": 50, "reset_time": 1700002000}},
                        }
                    }
                }
            }

            buf = io.StringIO()
            with patch('sys.stdout', buf):
                exit_code = main(["quota", "--format", "list"], engine=engine)
                self.assertEqual(exit_code, 0)

            output = buf.getvalue()
            # acc1 online
            self.assertIn("acc1 (a@b.com) — Gemini: 80%", output)
            # acc2 offline with cache
            self.assertIn("acc2 (c@d.com) — Gemini: 50% (Cached)", output)
            # acc3 offline without cache
            self.assertIn("acc3 (e@f.com) — Gemini: Error / Unreachable", output)

            # Check sort order: acc1 (80%) before acc2 (50%) before acc3 (Error)
            pos_acc1 = output.find("acc1")
            pos_acc2 = output.find("acc2")
            pos_acc3 = output.find("acc3")
            self.assertTrue(pos_acc1 < pos_acc2 < pos_acc3)

    def test_quota_json_with_offline_and_cached(self):
        engine = MagicMock(spec=AgySwitchEngine)
        engine.list_accounts.return_value = {
            "active": "acc1",
            "accounts": [
                {"name": "acc1", "is_active": True, "valid": True, "email": "a@b.com"},
                {"name": "acc2", "is_active": False, "valid": True, "email": "c@d.com"},
                {"name": "acc3", "is_active": False, "valid": True, "email": "e@f.com"},
            ]
        }
        engine.get_account_tokens.side_effect = lambda name: {"access_token": f"tok_{name}", "refresh_token": f"ref_{name}"}
        engine.get_active_name.return_value = "acc1"

        with patch('agy_switch.QuotaClient') as mock_qc_class:
            mock_qc = mock_qc_class.return_value
            def mock_fetch(tok, **kwargs):
                if "tok_acc1" in tok:
                    return {
                        "gemini": {
                            "quota": 80,
                            "reset_time": 1700000000,
                            "cached": False,
                            "buckets": {"5h": {"quota": 80, "reset_time": 1700000000}},
                        }
                    }
                return {"error": "Connection refused"}

            mock_qc.fetch_quota.side_effect = mock_fetch
            mock_qc.load_cache.return_value = {
                "accounts": {
                    "acc2": {
                        "gemini": {
                            "quota": 50,
                            "reset_time": 1700002000,
                            "buckets": {"5h": {"quota": 50, "reset_time": 1700002000}},
                        }
                    }
                }
            }

            buf = io.StringIO()
            with patch('sys.stdout', buf):
                exit_code = main(["quota", "--json"], engine=engine)
                self.assertEqual(exit_code, 0)

            data = json.loads(buf.getvalue())
            self.assertEqual(data["active"], "acc1")
            acc_by_name = {a["name"]: a for a in data["accounts"]}

            # acc1 (live)
            self.assertFalse(acc_by_name["acc1"]["quota"]["gemini"]["cached"])
            self.assertEqual(acc_by_name["acc1"]["quota"]["gemini"]["quota"], 80)

            # acc2 (cached)
            self.assertTrue(acc_by_name["acc2"]["quota"]["gemini"]["cached"])
            self.assertEqual(acc_by_name["acc2"]["quota"]["gemini"]["quota"], 50)

            # acc3 (offline without cache)
            self.assertIsNone(acc_by_name["acc3"]["quota"]["gemini"])
            self.assertEqual(acc_by_name["acc3"]["quota"]["error"], "Unreachable")

    def test_empty_vault_quota(self):
        engine = MagicMock(spec=AgySwitchEngine)
        engine.list_accounts.return_value = {"active": None, "accounts": []}

        buf = io.StringIO()
        with patch('sys.stdout', buf):
            exit_code = main(["quota"], engine=engine)
            self.assertEqual(exit_code, 0)

        output = buf.getvalue().strip()
        self.assertEqual(output, "No accounts registered in vault (~/.antigravity-vault/accounts).")

    def test_cache_merge_all_network_failure_preserves_cache(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            engine = AgySwitchEngine(vault_dir=tmp_path / "vault")

            # Setup two accounts in engine
            for acc_name, tok in [("acc1", "tok1"), ("acc2", "tok2")]:
                acc_dir = engine.accounts_dir / acc_name
                acc_dir.mkdir(parents=True)
                email_payload = json.dumps({"email": f"{acc_name}@b.com"}).encode("utf-8")
                import base64
                b64_part = base64.urlsafe_b64encode(email_payload).decode("utf-8").rstrip("=")
                payload = {
                    "token": {"access_token": tok, "refresh_token": f"ref_{acc_name}", "token_type": "Bearer", "expiry": "2026-10-15T00:00:00Z"},
                    "auth_method": "oauth",
                    "id_token": f"eyJhbGciOiJIUzI1NiJ9.{b64_part}.sig",
                }
                (acc_dir / "token.json").write_text(json.dumps(payload))
            engine.set_active_name("acc1")

            # Seed cache on disk
            cached_initial = {
                "timestamp": 1000.0,
                "accounts": {
                    "acc1": {"gemini": {"quota": 70, "reset_time": 1700000000, "buckets": {"5h": {"quota": 70, "reset_time": 1700000000}}}},
                    "acc2": {"gemini": {"quota": 30, "reset_time": 1700005000, "buckets": {"5h": {"quota": 30, "reset_time": 1700005000}}}},
                }
            }
            client = QuotaClient(timeout=1.0, engine=engine)
            client.save_cache(cached_initial, merge=False)

            # Both accounts fail network fetch (e.g. offline / airplane mode)
            def mock_fail(tok, **kwargs):
                raise TimeoutError("Network unreachable")

            with patch.object(QuotaClient, 'fetch_quota', side_effect=mock_fail):
                buf = io.StringIO()
                with patch('sys.stdout', buf):
                    exit_code = main(["quota", "--format", "list"], engine=engine)
                    self.assertEqual(exit_code, 0)

            # Invariant: Cache on disk was NOT cleared or corrupted
            saved_cache = client.load_cache()
            self.assertEqual(saved_cache["accounts"]["acc1"]["gemini"]["quota"], 70)
            self.assertEqual(saved_cache["accounts"]["acc2"]["gemini"]["quota"], 30)

            output = buf.getvalue()
            self.assertIn("acc1", output)
            self.assertIn("70% (Cached)", output)
            self.assertIn("acc2", output)
            self.assertIn("30% (Cached)", output)

    def test_legacy_flat_cache_fallback(self):
        engine = MagicMock(spec=AgySwitchEngine)
        engine.list_accounts.return_value = {
            "active": "acc1",
            "accounts": [
                {"name": "acc1", "is_active": True, "valid": True, "email": "a@b.com"},
            ]
        }
        engine.get_account_tokens.return_value = {"access_token": "tok1", "refresh_token": "ref1"}
        engine.get_active_name.return_value = "acc1"

        with patch('agy_switch.QuotaClient') as mock_qc_class:
            mock_qc = mock_qc_class.return_value
            mock_qc.fetch_quota.side_effect = TimeoutError("Timed out")
            # Legacy flat schema in cache
            mock_qc.load_cache.return_value = {
                "accounts": {
                    "acc1": {
                        "quota": 45,
                        "reset_time": 1700002000,
                    }
                }
            }

            buf = io.StringIO()
            with patch('sys.stdout', buf):
                exit_code = main(["quota", "--json"], engine=engine)
                self.assertEqual(exit_code, 0)

            data = json.loads(buf.getvalue())
            acc1 = data["accounts"][0]
            # Schema must adhere to hybrid quota schema: "gemini" object with cached=True
            self.assertIn("gemini", acc1["quota"])
            self.assertTrue(acc1["quota"]["gemini"]["cached"])
            self.assertEqual(acc1["quota"]["gemini"]["quota"], 45)
            self.assertEqual(acc1["quota"]["gemini"]["reset_time"], 1700002000)
            # Must NOT have legacy flat fields at root of acc["quota"]
            self.assertNotIn("reset_time", acc1["quota"])

    def test_save_cache_tempfile_cleanup_on_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            engine = AgySwitchEngine(vault_dir=tmp_path / "vault")
            client = QuotaClient(timeout=1.0, engine=engine)

            # Simulate os.replace failure (e.g. PermissionError or disk failure)
            with patch('os.replace', side_effect=OSError("Disk write error")):
                client.save_cache({"accounts": {"acc1": {"gemini": {"quota": 50}}}})

            # Verify no orphaned .tmp files remain in cache directory
            leftover_tmp_files = list(client.cache_dir.glob("*.tmp"))
            self.assertEqual(leftover_tmp_files, [])

    def test_run_quota_pipeline_defensive_data_handling(self):
        from agy_switch import run_quota_pipeline
        engine = MagicMock(spec=AgySwitchEngine)

        # None input
        res_none = run_quota_pipeline(engine, None)
        self.assertEqual(res_none, {"active": None, "accounts": []})

        # Non-dict input
        res_str = run_quota_pipeline(engine, "invalid")
        self.assertEqual(res_str, {"active": None, "accounts": []})

        # Empty accounts list
        res_empty = run_quota_pipeline(engine, {"active": "acc1", "accounts": []})
        self.assertEqual(res_empty, {"active": "acc1", "accounts": []})

    def test_format_status_report_with_null_cached_quota(self):
        from agy_switch import format_status_report
        status_with_unreachable = {
            "active": "acc1",
            "email": "a@b.com",
            "cached_quota": {"gemini": None, "error": "Unreachable"},
            "server_running": True,
            "server_pid": 1234,
            "server_busy": False,
        }
        # Must not raise AttributeError: 'NoneType' object has no attribute 'get'
        report = format_status_report(status_with_unreachable)
        self.assertIn("Active Account    : acc1 (a@b.com)", report)
        self.assertIn("Language Server   : Running (PID: 1234)", report)

    def test_config_threshold_get_and_set(self):
        engine = MagicMock(spec=AgySwitchEngine)
        engine.get_quota_threshold.return_value = 0.25

        # 1. Get threshold via CLI
        buf = io.StringIO()
        with patch('sys.stdout', buf):
            code = main(["config", "threshold"], engine=engine)
            self.assertEqual(code, 0)
            self.assertIn("quota threshold is currently 25%.", buf.getvalue())

        # 2. Set threshold via CLI
        buf = io.StringIO()
        with patch('sys.stdout', buf):
            code = main(["config", "threshold", "30"], engine=engine)
            self.assertEqual(code, 0)
            self.assertIn("quota threshold set to 30%.", buf.getvalue())
            engine.set_quota_threshold.assert_called_with(30.0)

        # 3. Invalid threshold via CLI
        engine.set_quota_threshold.side_effect = InvalidThresholdError("Invalid threshold")
        err_buf = io.StringIO()
        with patch('sys.stderr', err_buf):
            code = main(["config", "threshold", "999"], engine=engine)
            self.assertEqual(code, 1)
            self.assertIn("Error setting threshold", err_buf.getvalue())

    def test_engine_threshold_validation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_dir = Path(tmpdir) / "vault"
            engine = AgySwitchEngine(vault_dir=vault_dir)

            # Default is 0.25 (25%)
            self.assertEqual(engine.get_quota_threshold(), 0.25)

            # Valid percentage
            engine.set_quota_threshold(35)
            self.assertEqual(engine.get_quota_threshold(), 0.35)

            # Valid fraction
            engine.set_quota_threshold(0.20)
            self.assertEqual(engine.get_quota_threshold(), 0.20)

            # Invalid thresholds raise InvalidThresholdError
            with self.assertRaises(InvalidThresholdError):
                engine.set_quota_threshold(-1)
            with self.assertRaises(InvalidThresholdError):
                engine.set_quota_threshold(150)
            with self.assertRaises(InvalidThresholdError):
                engine.set_quota_threshold("not_a_number")

    def test_login_standard_browser_dispatch(self):
        """Standard login without --headless preserves backward compatibility with login_account."""
        self.engine.login_account.return_value = {
            "name": "browseracc",
            "email": "browser@example.com",
            "status": "standby",
        }
        self.engine.accounts_dir = Path("/mock/vault/accounts")

        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            ret = main(["login", "browseracc"], engine=self.engine)

        self.assertEqual(ret, 0)
        self.engine.login_account.assert_called_once_with(
            "browseracc",
            agy_binary=None,
            timeout=600,
            force=False,
        )
        self.assertIn("Account 'browseracc' (browser@example.com) successfully logged in", stdout_buf.getvalue())

    def test_login_retired_headless_flags_absent_from_parser(self):
        """[TTY-LOGIN-002] Retired --headless and --auth-code flags are absent from login subparser."""
        import argparse
        from agy_switcher.cli import build_parser

        parser = build_parser()
        subparsers_actions = [
            action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
        ]
        self.assertEqual(len(subparsers_actions), 1)
        login_parser = subparsers_actions[0].choices["login"]
        option_strings = {opt for action in login_parser._actions for opt in action.option_strings}

        self.assertNotIn("--headless", option_strings)
        self.assertNotIn("--auth-code", option_strings)
        self.assertEqual(option_strings, {"-h", "--help", "--agy-binary", "--timeout", "--force"})

    def test_login_rejects_retired_headless_and_auth_code_flags(self):
        """[TTY-LOGIN-ERR-002] argparse rejects --headless and --auth-code with exit code 2 without spawning agy or deprecation stub."""
        from agy_switcher.cli import build_parser

        cases = [
            ["login", "bot-acc", "--headless"],
            ["login", "bot-acc", "--auth-code", "stale"],
            ["login", "bot-acc", "--auth-code=stale"],
            ["login", "bot-acc", "--headless", "--auth-code", "stale"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                parser = build_parser()
                stderr_parser = io.StringIO()
                with patch("sys.stderr", stderr_parser), \
                     patch("subprocess.Popen") as mock_popen:
                    with self.assertRaises(SystemExit) as ctx:
                        parser.parse_args(argv)
                self.assertEqual(ctx.exception.code, 2)
                err_parser = stderr_parser.getvalue()
                self.assertIn("unrecognized arguments", err_parser)
                self.assertNotIn("deprecated", err_parser.lower())
                mock_popen.assert_not_called()

                stderr_main = io.StringIO()
                with patch("sys.stderr", stderr_main), \
                     patch("subprocess.Popen") as mock_popen_main:
                    with self.assertRaises(SystemExit) as main_ctx:
                        main(argv, engine=self.engine)
                self.assertEqual(main_ctx.exception.code, 2)
                err_main = stderr_main.getvalue()
                self.assertIn("unrecognized arguments", err_main)
                self.assertNotIn("deprecated", err_main.lower())
                mock_popen_main.assert_not_called()
                self.engine.login_account.assert_not_called()

    def test_login_invalid_alias_cli_exits_one_and_preserves_vault(self):
        """[TTY-LOGIN-ERR-001] main(['login', alias]) rejects invalid aliases to stderr with exit 1 without touching vault."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            real_engine = AgySwitchEngine(
                vault_dir=base / ".antigravity-vault",
                gemini_dir=base / ".gemini",
                legacy_root=base / ".agy-cli-manager",
            )
            (real_engine.vault_dir / "bad").mkdir(parents=True, exist_ok=True)
            valid_dir = real_engine.accounts_dir / "valid-acc"
            valid_dir.mkdir(parents=True, exist_ok=True)
            (valid_dir / "token.json").write_text('{"keep": true}', encoding="utf-8")

            invalid_aliases = ["../bad", "..", ".", "my account", "", "   ", "a/b", "a\\b", "bad\x00acc", "a" * 65]
            for alias in invalid_aliases:
                with self.subTest(alias=alias):
                    stdout_buf = io.StringIO()
                    stderr_buf = io.StringIO()
                    with patch("sys.stdout", stdout_buf), \
                         patch("sys.stderr", stderr_buf), \
                         patch("builtins.input") as mock_input, \
                         patch.object(real_engine, "resolve_agy_binary") as mock_resolve, \
                         patch("subprocess.Popen") as mock_popen:
                        ret = main(["login", alias], engine=real_engine)

                    self.assertEqual(ret, 1)
                    self.assertEqual(stdout_buf.getvalue(), "")
                    self.assertIn("Error:", stderr_buf.getvalue())
                    mock_input.assert_not_called()
                    mock_resolve.assert_not_called()
                    mock_popen.assert_not_called()

            self.assertEqual(list((real_engine.vault_dir / "bad").iterdir()), [])
            self.assertEqual(sorted(p.name for p in real_engine.accounts_dir.iterdir()), ["valid-acc"])
            self.assertEqual((valid_dir / "token.json").read_text(encoding="utf-8"), '{"keep": true}')

    def test_login_account_cli_whitespace_and_failed_force_overwrite(self):
        """[TTY-LOGIN-EDGE-002] main(['login', ' work-acc ']) normalizes alias and failed --force preserves prior token."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            real_engine = AgySwitchEngine(
                vault_dir=base / ".antigravity-vault",
                gemini_dir=base / ".gemini",
                legacy_root=base / ".agy-cli-manager",
            )

            class _StubProc:
                def poll(self):
                    return 0

                def wait(self, timeout=None):
                    return 0

            def mock_popen_success(args, **kwargs):
                temp_home = Path(kwargs["env"]["HOME"])
                tok = temp_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
                tok.parent.mkdir(parents=True, exist_ok=True)
                tok.write_text(
                    json.dumps({
                        "token": {
                            "access_token": "ya29.work",
                            "token_type": "Bearer",
                            "refresh_token": "1//work",
                            "expiry": "2026-12-31T23:59:59Z",
                        },
                        "auth_method": "oauth",
                        "id_token_email": "work@example.com",
                    }),
                    encoding="utf-8",
                )
                return _StubProc()

            stdout_buf = io.StringIO()
            with patch("sys.stdout", stdout_buf), \
                 patch.object(real_engine, "resolve_agy_binary", return_value="/bin/mock_agy"), \
                 patch("subprocess.Popen", side_effect=mock_popen_success):
                ret = main(["login", " work-acc "], engine=real_engine)

            self.assertEqual(ret, 0)
            stdout_text = stdout_buf.getvalue()
            self.assertIn("Account 'work-acc' (work@example.com) successfully logged in", stdout_text)
            self.assertIn("agy-switch switch work-acc", stdout_text)
            self.assertIn(str(real_engine.accounts_dir / "work-acc" / "token.json"), stdout_text)
            self.assertNotIn(" work-acc ", stdout_text)
            self.assertTrue((real_engine.accounts_dir / "work-acc" / "token.json").is_file())
            self.assertFalse((real_engine.accounts_dir / " work-acc ").exists())

            existing_dir = real_engine.accounts_dir / "existing-acc"
            existing_dir.mkdir(parents=True, exist_ok=True)
            existing_token = existing_dir / "token.json"
            prior_json = '{"prior": "token"}'
            existing_token.write_text(prior_json, encoding="utf-8")

            stderr_buf = io.StringIO()
            with patch("sys.stderr", stderr_buf), \
                 patch.object(real_engine, "resolve_agy_binary", return_value="/bin/mock_agy"), \
                 patch("subprocess.Popen", return_value=_StubProc()):
                ret_fail = main(["login", "existing-acc", "--force"], engine=real_engine)

            self.assertEqual(ret_fail, 1)
            self.assertIn("Error:", stderr_buf.getvalue())
            self.assertEqual(existing_token.read_text(encoding="utf-8"), prior_json)
            self.assertEqual([p.name for p in existing_dir.iterdir()], ["token.json"])

    def test_login_standard_interactive_tty_end_to_end(self):
        """[TTY-LOGIN-001] main(['login', 'work-acc']) invokes login_account, inherits TTY stdio in temp HOME, and saves 0600 Standby token."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            real_engine = AgySwitchEngine(
                vault_dir=base / ".antigravity-vault",
                gemini_dir=base / ".gemini",
                legacy_root=base / ".agy-cli-manager",
            )
            # Pre-populate an active account to prove login leaves active pointer untouched
            active_dir = real_engine.accounts_dir / "active-acc"
            active_dir.mkdir(parents=True, exist_ok=True)
            (active_dir / "token.json").write_text('{"active": true}', encoding="utf-8")
            real_engine.active_file.write_text("active-acc\n", encoding="utf-8")

            bin_dir = base / "bin"
            bin_dir.mkdir(parents=True, exist_ok=True)
            agy_bin = bin_dir / "agy"
            agy_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            agy_bin.chmod(0o755)

            observed_popen = {}

            class _StubProc:
                def poll(self):
                    return 0

                def wait(self, timeout=None):
                    return 0

            def mock_popen(args, **kwargs):
                temp_home = Path(kwargs["env"]["HOME"])
                observed_popen["args"] = args
                observed_popen["stdin_is_sys"] = kwargs.get("stdin") is sys.stdin
                observed_popen["stdout_is_sys"] = kwargs.get("stdout") is sys.stdout
                observed_popen["stderr_is_sys"] = kwargs.get("stderr") is sys.stderr
                observed_popen["cwd"] = kwargs.get("cwd")
                observed_popen["temp_home"] = temp_home
                observed_popen["temp_home_exists_during_spawn"] = temp_home.is_dir()

                tok = temp_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
                tok.parent.mkdir(parents=True, exist_ok=True)
                tok.write_text(
                    json.dumps({
                        "token": {
                            "access_token": "ya29.tty_work",
                            "token_type": "Bearer",
                            "refresh_token": "1//tty_work",
                            "expiry": "2026-12-31T23:59:59Z",
                        },
                        "auth_method": "oauth",
                        "id_token_email": "work@example.com",
                    }),
                    encoding="utf-8",
                )
                return _StubProc()

            stdout_buf = io.StringIO()
            stderr_buf = io.StringIO()
            clean_env = {k: v for k, v in os.environ.items() if k != "AGY_BINARY"}
            clean_env["PATH"] = str(bin_dir)
            clean_env["HOME"] = str(base)

            with patch.dict(os.environ, clean_env, clear=True), \
                 patch.object(real_engine, "login_account", wraps=real_engine.login_account) as spy_login, \
                 patch("sys.stdout", stdout_buf), \
                 patch("sys.stderr", stderr_buf), \
                 patch("subprocess.Popen", side_effect=mock_popen):
                ret = main(["login", "work-acc"], engine=real_engine)

            self.assertEqual(ret, 0)
            self.assertEqual(stderr_buf.getvalue(), "")
            spy_login.assert_called_once_with("work-acc", agy_binary=None, timeout=600, force=False)
            self.assertEqual(observed_popen["args"], [str(agy_bin)])
            self.assertTrue(observed_popen["stdin_is_sys"])
            self.assertTrue(observed_popen["stdout_is_sys"])
            self.assertTrue(observed_popen["stderr_is_sys"])
            self.assertTrue(observed_popen["temp_home_exists_during_spawn"])
            self.assertNotEqual(observed_popen["temp_home"], base)
            self.assertEqual(observed_popen["cwd"], str(observed_popen["temp_home"]))
            self.assertFalse(observed_popen["temp_home"].exists())

            saved_token = real_engine.accounts_dir / "work-acc" / "token.json"
            self.assertTrue(saved_token.is_file())
            self.assertEqual(stat.S_IMODE(saved_token.stat().st_mode), 0o600)
            saved_data = json.loads(saved_token.read_text(encoding="utf-8"))
            self.assertEqual(saved_data["token"]["access_token"], "ya29.tty_work")
            self.assertEqual(saved_data["id_token_email"], "work@example.com")
            self.assertEqual(real_engine.get_active_name(), "active-acc")
            self.assertIn("Status: Standby", stdout_buf.getvalue())
            self.assertIn(str(saved_token), stdout_buf.getvalue())

    def test_login_overwrite_confirmation_cli_lifecycle_and_jetski_fallback(self):
        """[TTY-LOGIN-EDGE-001] CLI overwrite decline/EOF/KeyboardInterrupt exits 1 before agy spawn; confirm/force accepts jetski fallback."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            real_engine = AgySwitchEngine(
                vault_dir=base / ".antigravity-vault",
                gemini_dir=base / ".gemini",
                legacy_root=base / ".agy-cli-manager",
            )
            existing_dir = real_engine.accounts_dir / "existing-acc"
            existing_dir.mkdir(parents=True, exist_ok=True)
            existing_token = existing_dir / "token.json"
            prior_json = json.dumps({
                "token": {
                    "access_token": "ya29.prior",
                    "token_type": "Bearer",
                    "refresh_token": "1//prior",
                    "expiry": "2026-12-31T23:59:59Z",
                },
                "auth_method": "oauth",
                "id_token_email": "prior@example.com",
            })
            existing_token.write_text(prior_json, encoding="utf-8")
            existing_token.chmod(0o600)

            for label, side_eff in (("n", "n"), ("empty", ""), ("eof", EOFError()), ("sigint", KeyboardInterrupt())):
                with self.subTest(decline=label):
                    stdout_buf = io.StringIO()
                    stderr_buf = io.StringIO()
                    with patch("sys.stdout", stdout_buf), \
                         patch("sys.stderr", stderr_buf), \
                         patch("builtins.input", side_effect=[side_eff]) as mock_input, \
                         patch.object(real_engine, "resolve_agy_binary") as mock_resolve, \
                         patch("subprocess.Popen") as mock_popen:
                        ret = main(["login", "existing-acc"], engine=real_engine)

                    self.assertEqual(ret, 1)
                    self.assertEqual(stdout_buf.getvalue(), "")
                    self.assertIn("Error:", stderr_buf.getvalue())
                    mock_input.assert_called_once()
                    mock_resolve.assert_not_called()
                    mock_popen.assert_not_called()
                    self.assertEqual(existing_token.read_text(encoding="utf-8"), prior_json)
                    self.assertEqual(stat.S_IMODE(existing_token.stat().st_mode), 0o600)

            class _StubProc:
                def poll(self):
                    return 0

                def wait(self, timeout=None):
                    return 0

            def make_jetski_popen(access_tok: str, email: str):
                def _mock_popen(args, **kwargs):
                    temp_home = Path(kwargs["env"]["HOME"])
                    jetski_tok = temp_home / ".gemini" / "jetski-standalone-oauth-token"
                    jetski_tok.parent.mkdir(parents=True, exist_ok=True)
                    jetski_tok.write_text(
                        json.dumps({
                            "token": {
                                "access_token": access_tok,
                                "token_type": "Bearer",
                                "refresh_token": f"1//{access_tok}",
                                "expiry": "2026-12-31T23:59:59Z",
                            },
                            "auth_method": "oauth",
                            "id_token_email": email,
                        }),
                        encoding="utf-8",
                    )
                    return _StubProc()

                return _mock_popen

            stdout_confirm = io.StringIO()
            stderr_confirm = io.StringIO()
            with patch("sys.stdout", stdout_confirm), \
                 patch("sys.stderr", stderr_confirm), \
                 patch("builtins.input", return_value="y") as mock_input_yes, \
                 patch.object(real_engine, "resolve_agy_binary", return_value="/bin/mock_agy") as mock_resolve_yes, \
                 patch("subprocess.Popen", side_effect=make_jetski_popen("ya29.cli_confirm", "cli-confirm@example.com")) as mock_popen_yes:
                ret_yes = main(["login", "existing-acc"], engine=real_engine)

            self.assertEqual(ret_yes, 0)
            self.assertEqual(stderr_confirm.getvalue(), "")
            mock_input_yes.assert_called_once()
            mock_resolve_yes.assert_called_once_with(None)
            mock_popen_yes.assert_called_once()
            self.assertEqual(stat.S_IMODE(existing_token.stat().st_mode), 0o600)
            self.assertEqual(json.loads(existing_token.read_text(encoding="utf-8"))["token"]["access_token"], "ya29.cli_confirm")

            stdout_force = io.StringIO()
            stderr_force = io.StringIO()
            with patch("sys.stdout", stdout_force), \
                 patch("sys.stderr", stderr_force), \
                 patch("builtins.input") as mock_input_force, \
                 patch.object(real_engine, "resolve_agy_binary", return_value="/bin/mock_agy") as mock_resolve_force, \
                 patch("subprocess.Popen", side_effect=make_jetski_popen("ya29.cli_force", "cli-force@example.com")) as mock_popen_force:
                ret_force = main(["login", "existing-acc", "--force"], engine=real_engine)

            self.assertEqual(ret_force, 0)
            self.assertEqual(stderr_force.getvalue(), "")
            mock_input_force.assert_not_called()
            mock_resolve_force.assert_called_once_with(None)
            mock_popen_force.assert_called_once()
            self.assertEqual(stat.S_IMODE(existing_token.stat().st_mode), 0o600)
            self.assertEqual(json.loads(existing_token.read_text(encoding="utf-8"))["token"]["access_token"], "ya29.cli_force")

    def test_login_cli_missing_binary_tokenless_timeout_interrupt_and_corrupt_cleanup(self):
        """[TTY-LOGIN-ERR-003][TTY-LOGIN-ERR-004] main(['login', ...]) exits 1 to stderr and leaves no partial account dir."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            real_engine = AgySwitchEngine(
                vault_dir=base / ".antigravity-vault",
                gemini_dir=base / ".gemini",
                legacy_root=base / ".agy-cli-manager",
            )
            fail_dir = real_engine.accounts_dir / "fail-acc"
            bad_dir = real_engine.accounts_dir / "bad-tok"

            # 1. Missing agy binary
            clean_env = {k: v for k, v in os.environ.items() if k != "AGY_BINARY"}
            stdout_missing = io.StringIO()
            stderr_missing = io.StringIO()
            with patch.dict(os.environ, clean_env, clear=True), \
                 patch("agy_switcher.vault.Path.home", return_value=base), \
                 patch("agy_switcher.vault.shutil.which", return_value=None), \
                 patch("sys.stdout", stdout_missing), \
                 patch("sys.stderr", stderr_missing), \
                 patch("subprocess.Popen") as mock_popen:
                ret_missing = main(["login", "fail-acc"], engine=real_engine)

            self.assertEqual(ret_missing, 1)
            self.assertEqual(stdout_missing.getvalue(), "")
            self.assertIn("Error:", stderr_missing.getvalue())
            mock_popen.assert_not_called()
            self.assertFalse(fail_dir.exists())

            class _StubProc:
                def __init__(self, returncode=0):
                    self.returncode = returncode

                def poll(self):
                    return self.returncode

                def wait(self, timeout=None):
                    return self.returncode

            # 2. agy exits 0 or non-zero without producing a token file
            for exit_code in (0, 1):
                with self.subTest(exit_code=exit_code):
                    stdout_buf = io.StringIO()
                    stderr_buf = io.StringIO()
                    with patch("sys.stdout", stdout_buf), \
                         patch("sys.stderr", stderr_buf), \
                         patch.object(real_engine, "resolve_agy_binary", return_value="/bin/mock_agy"), \
                         patch("subprocess.Popen", return_value=_StubProc(returncode=exit_code)):
                        ret = main(["login", "fail-acc"], engine=real_engine)
                    self.assertEqual(ret, 1)
                    self.assertEqual(stdout_buf.getvalue(), "")
                    self.assertIn("Error:", stderr_buf.getvalue())
                    self.assertFalse(fail_dir.exists())

            # 3. Timeout and KeyboardInterrupt terminate/kill/reap stubborn child cleanly
            class _StubbornProc:
                def __init__(self):
                    self.terminated = False
                    self.killed = False
                    self.wait_calls = 0

                def poll(self):
                    return None

                def terminate(self):
                    self.terminated = True

                def wait(self, timeout=None):
                    self.wait_calls += 1
                    if self.wait_calls == 1:
                        raise subprocess.TimeoutExpired(cmd="agy", timeout=timeout or 3)
                    return -9

                def kill(self):
                    self.killed = True

            timeout_proc = _StubbornProc()
            stdout_to = io.StringIO()
            stderr_to = io.StringIO()
            with patch("sys.stdout", stdout_to), \
                 patch("sys.stderr", stderr_to), \
                 patch.object(real_engine, "resolve_agy_binary", return_value="/bin/mock_agy"), \
                 patch("subprocess.Popen", return_value=timeout_proc), \
                 patch("time.sleep", return_value=None), \
                 patch("time.time", side_effect=itertools.chain([0.0], itertools.repeat(100.0))):
                ret_to = main(["login", "fail-acc", "--timeout", "5"], engine=real_engine)

            self.assertEqual(ret_to, 1)
            self.assertEqual(stdout_to.getvalue(), "")
            self.assertIn("Error:", stderr_to.getvalue())
            self.assertTrue(timeout_proc.terminated)
            self.assertTrue(timeout_proc.killed)
            self.assertEqual(timeout_proc.wait_calls, 2)
            self.assertFalse(fail_dir.exists())

            interrupt_proc = _StubbornProc()
            stdout_int = io.StringIO()
            stderr_int = io.StringIO()
            with patch("sys.stdout", stdout_int), \
                 patch("sys.stderr", stderr_int), \
                 patch.object(real_engine, "resolve_agy_binary", return_value="/bin/mock_agy"), \
                 patch("subprocess.Popen", return_value=interrupt_proc), \
                 patch("time.sleep", side_effect=KeyboardInterrupt):
                ret_int = main(["login", "fail-acc"], engine=real_engine)

            self.assertEqual(ret_int, 1)
            self.assertEqual(stdout_int.getvalue(), "")
            self.assertIn("Error:", stderr_int.getvalue())
            self.assertTrue(interrupt_proc.terminated)
            self.assertTrue(interrupt_proc.killed)
            self.assertEqual(interrupt_proc.wait_calls, 2)
            self.assertFalse(fail_dir.exists())

            # 4. Corrupt token JSON / missing OAuth fields
            for bad_body in ("{not-json", json.dumps({"token": {"access_token": "ya29.only"}})):
                with self.subTest(bad_body=bad_body):
                    def _mock_popen_bad_tok(args, *, _body=bad_body, **kwargs):
                        temp_home = Path(kwargs["env"]["HOME"])
                        tok = temp_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
                        tok.parent.mkdir(parents=True, exist_ok=True)
                        tok.write_text(_body, encoding="utf-8")
                        return _StubProc(returncode=0)

                    stdout_bad = io.StringIO()
                    stderr_bad = io.StringIO()
                    with patch("sys.stdout", stdout_bad), \
                         patch("sys.stderr", stderr_bad), \
                         patch.object(real_engine, "resolve_agy_binary", return_value="/bin/mock_agy"), \
                         patch("subprocess.Popen", side_effect=_mock_popen_bad_tok):
                        ret_bad = main(["login", "bad-tok"], engine=real_engine)

                    self.assertEqual(ret_bad, 1)
                    self.assertEqual(stdout_bad.getvalue(), "")
                    self.assertIn("Error:", stderr_bad.getvalue())
                    self.assertFalse(bad_dir.exists())

            # 5. Vault write PermissionError / OSError aborts cleanly without partial accounts/bad-tok dir
            def _mock_popen_valid_tok(args, **kwargs):
                temp_home = Path(kwargs["env"]["HOME"])
                tok = temp_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
                tok.parent.mkdir(parents=True, exist_ok=True)
                tok.write_text(
                    json.dumps({
                        "token": {
                            "access_token": "ya29.badtok",
                            "token_type": "Bearer",
                            "refresh_token": "1//badtok",
                            "expiry": "2026-12-31T23:59:59Z",
                        },
                        "auth_method": "oauth",
                        "id_token_email": "badtok@example.com",
                    }),
                    encoding="utf-8",
                )
                return _StubProc(returncode=0)

            for patch_target, err in (
                ("agy_switcher.security.os.replace", PermissionError("simulated permission denied")),
                ("agy_switcher.security.tempfile.mkstemp", OSError("simulated os error")),
            ):
                with self.subTest(patch_target=patch_target):
                    stdout_perm = io.StringIO()
                    stderr_perm = io.StringIO()
                    with patch("sys.stdout", stdout_perm), \
                         patch("sys.stderr", stderr_perm), \
                         patch.object(real_engine, "resolve_agy_binary", return_value="/bin/mock_agy"), \
                         patch("subprocess.Popen", side_effect=_mock_popen_valid_tok), \
                         patch(patch_target, side_effect=err):
                        ret_perm = main(["login", "bad-tok"], engine=real_engine)

                    self.assertEqual(ret_perm, 1)
                    self.assertEqual(stdout_perm.getvalue(), "")
                    self.assertIn("Error:", stderr_perm.getvalue())
                    self.assertFalse(bad_dir.exists())


    def test_vault_export_and_import_routing(self):
        """Subcommand routing: Add vault parser with export and import subcommands."""
        self.engine.export_vault_bundle.return_value = (True, "Successfully exported vault bundle (1 account) to /tmp/bundle.tar.gz")
        self.engine.import_vault_bundle.return_value = (True, "Successfully imported vault bundle (1 account) from /tmp/bundle.tar.gz")

        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            ret = main(["vault", "export", "/tmp/bundle.tar.gz"], engine=self.engine)
        self.assertEqual(ret, 0)
        self.engine.export_vault_bundle.assert_called_with("/tmp/bundle.tar.gz", force=False)
        self.assertIn("Successfully exported vault bundle", stdout_buf.getvalue())

        self.engine.export_vault_bundle.reset_mock()
        with patch("sys.stdout", io.StringIO()):
            ret = main(["vault", "export", "--bundle", "/tmp/bundle2.tar.gz", "--force"], engine=self.engine)
        self.assertEqual(ret, 0)
        self.engine.export_vault_bundle.assert_called_with("/tmp/bundle2.tar.gz", force=True)

        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            ret = main(["vault", "import", "/tmp/bundle.tar.gz"], engine=self.engine)
        self.assertEqual(ret, 0)
        self.engine.import_vault_bundle.assert_called_with("/tmp/bundle.tar.gz", force=False)
        self.assertIn("Successfully imported vault bundle", stdout_buf.getvalue())

        self.engine.import_vault_bundle.reset_mock()
        with patch("sys.stdout", io.StringIO()):
            ret = main(["vault", "import", "--bundle", "/tmp/bundle2.tar.gz", "--force"], engine=self.engine)
        self.assertEqual(ret, 0)
        self.engine.import_vault_bundle.assert_called_with("/tmp/bundle2.tar.gz", force=True)

        stderr_buf = io.StringIO()
        with patch("sys.stderr", stderr_buf):
            ret = main(["vault", "export"], engine=self.engine)
        self.assertEqual(ret, 1)
        self.assertIn("bundle path must be provided", stderr_buf.getvalue().lower())

        stderr_buf = io.StringIO()
        with patch("sys.stderr", stderr_buf):
            ret = main(["vault", "import"], engine=self.engine)
        self.assertEqual(ret, 1)
        self.assertIn("bundle path must be provided", stderr_buf.getvalue().lower())

        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            ret = main(["vault"], engine=self.engine)
        self.assertEqual(ret, 0)

        self.engine.export_vault_bundle.return_value = (False, "Bundle already exists at /tmp/bundle.tar.gz. Use --force to overwrite.")
        stderr_buf = io.StringIO()
        with patch("sys.stderr", stderr_buf):
            ret = main(["vault", "export", "/tmp/bundle.tar.gz"], engine=self.engine)
        self.assertEqual(ret, 1)
        self.assertIn("already exists", stderr_buf.getvalue())

    def test_vault_export_and_import_edge_cases(self):
        """Validates conflicting flags, whitespace paths, and directory arguments."""
        self.engine.export_vault_bundle.return_value = (True, "Exported")
        self.engine.import_vault_bundle.return_value = (True, "Imported")

        stderr_buf = io.StringIO()
        with patch("sys.stderr", stderr_buf):
            ret = main(["vault", "export", "/tmp/a.tar.gz", "--bundle", "/tmp/b.tar.gz"], engine=self.engine)
        self.assertEqual(ret, 1)
        self.assertIn("conflicting bundle paths", stderr_buf.getvalue().lower())

        stderr_buf = io.StringIO()
        with patch("sys.stderr", stderr_buf):
            ret = main(["vault", "import", "/tmp/a.tar.gz", "--bundle", "/tmp/b.tar.gz"], engine=self.engine)
        self.assertEqual(ret, 1)
        self.assertIn("conflicting bundle paths", stderr_buf.getvalue().lower())

        with patch("sys.stdout", io.StringIO()):
            ret = main(["vault", "export", "/tmp/same.tar.gz", "--bundle", "/tmp/same.tar.gz"], engine=self.engine)
        self.assertEqual(ret, 0)

        stderr_buf = io.StringIO()
        with patch("sys.stderr", stderr_buf):
            ret = main(["vault", "export", "   "], engine=self.engine)
        self.assertEqual(ret, 1)
        self.assertIn("bundle path must be provided", stderr_buf.getvalue().lower())

        with tempfile.TemporaryDirectory() as tmp_dir:
            stderr_buf = io.StringIO()
            with patch("sys.stderr", stderr_buf):
                ret = main(["vault", "export", tmp_dir], engine=self.engine)
            self.assertEqual(ret, 1)
            self.assertIn("is a directory", stderr_buf.getvalue().lower())

            stderr_buf = io.StringIO()
            with patch("sys.stderr", stderr_buf):
                ret = main(["vault", "import", tmp_dir], engine=self.engine)
            self.assertEqual(ret, 1)
            self.assertIn("is a directory", stderr_buf.getvalue().lower())

    def test_vault_unknown_subcommand(self):
        """Tests that unrecognized vault subcommands return non-zero exit and helpful error."""
        import argparse
        from agy_switcher.cli import handle_vault

        stderr_buf = io.StringIO()
        with patch("sys.stderr", stderr_buf):
            ret = handle_vault(argparse.Namespace(vault_command="bogus"), self.engine)
        self.assertEqual(ret, 1)
        self.assertIn("unknown vault subcommand: 'bogus'", stderr_buf.getvalue().lower())

    def test_format_cli_error_normalization(self):
        """Tests that _format_cli_error normalizes redundant prefixes idempotently."""
        from agy_switcher.cli import _format_cli_error

        self.assertEqual(_format_cli_error("Something failed"), "Error: [agy-switch] Something failed")
        self.assertEqual(_format_cli_error("Error: Something failed"), "Error: [agy-switch] Something failed")
        self.assertEqual(_format_cli_error("[agy-switch] Something failed"), "Error: [agy-switch] Something failed")
        self.assertEqual(_format_cli_error("Error: [agy-switch] Something failed"), "Error: [agy-switch] Something failed")
        self.assertEqual(_format_cli_error("[agy-switch] Error: Something failed"), "Error: [agy-switch] Something failed")

    def test_vault_export_and_import_req_4_err_001_corrupt_checksum(self):
        """[REQ-4-ERR-001] GIVEN a corrupt vault bundle archive with mismatched SHA-256 checksums,
        WHEN agy-switch vault import corrupt.tar.gz is executed,
        THEN the CLI exits with non-zero code and displays a checksum mismatch error without importing corrupt tokens.
        """
        import hashlib
        import tarfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            vault_dir = tmp_path / ".antigravity-vault"
            gemini_dir = tmp_path / ".gemini"
            legacy_root = tmp_path / ".agy-cli-manager"
            vault_dir.mkdir(mode=0o700)
            gemini_dir.mkdir(mode=0o700)
            legacy_root.mkdir(mode=0o700)

            real_engine = AgySwitchEngine(
                vault_dir=vault_dir,
                gemini_dir=gemini_dir,
                legacy_root=legacy_root,
            )

            corrupt_bundle = tmp_path / "corrupt.tar.gz"
            genuine_content = b'{"token": "valid"}'
            corrupt_content = b'{"token": "TAMPERED"}'
            manifest_data = {
                "version": 1,
                "created_at": "2026-11-06T00:00:00Z",
                "active_account": "acc1",
                "files": {
                    "accounts/acc1/token.json": hashlib.sha256(genuine_content).hexdigest()
                }
            }
            manifest_bytes = json.dumps(manifest_data).encode("utf-8")

            with tarfile.open(corrupt_bundle, "w:gz") as tar:
                m_info = tarfile.TarInfo(name="manifest.json")
                m_info.type = tarfile.REGTYPE
                m_info.size = len(manifest_bytes)
                m_info.mode = 0o600
                tar.addfile(m_info, io.BytesIO(manifest_bytes))

                c_info = tarfile.TarInfo(name="accounts/acc1/token.json")
                c_info.type = tarfile.REGTYPE
                c_info.size = len(corrupt_content)
                c_info.mode = 0o600
                tar.addfile(c_info, io.BytesIO(corrupt_content))

            stderr_buf = io.StringIO()
            with patch("sys.stderr", stderr_buf):
                exit_code = main(["vault", "import", str(corrupt_bundle)], engine=real_engine)

            self.assertNotEqual(exit_code, 0)
            self.assertIn("checksum mismatch", stderr_buf.getvalue().lower())
            corrupt_token_dest = vault_dir / "accounts" / "acc1" / "token.json"
            self.assertFalse(corrupt_token_dest.exists(), "Corrupt token must not be written to accounts")

    def test_vault_export_and_import_req_4_edge_003_restored_permissions(self):
        """[REQ-4-EDGE-003] GIVEN successful execution of agy-switch vault import backup.tar.gz,
        WHEN vault accounts are inspected,
        THEN all restored token files are verified to have strict POSIX 0600 permissions.
        """
        import stat

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            src_vault = tmp_path / "src_vault"
            src_gemini = tmp_path / "src_gemini"
            src_vault.mkdir(mode=0o700)
            src_gemini.mkdir(mode=0o700)

            src_engine = AgySwitchEngine(
                vault_dir=src_vault,
                gemini_dir=src_gemini,
                legacy_root=tmp_path / "src_legacy",
            )
            acc_dir = src_vault / "accounts" / "backup_acc"
            acc_dir.mkdir(parents=True, mode=0o700)
            token_path = acc_dir / "token.json"
            token_path.write_text(json.dumps({"token": "secret"}), encoding="utf-8")
            os.chmod(token_path, 0o600)
            src_engine.set_active_name("backup_acc")

            backup_bundle = tmp_path / "backup.tar.gz"

            with patch("sys.stdout", io.StringIO()):
                ret = main(["vault", "export", str(backup_bundle)], engine=src_engine)
            self.assertEqual(ret, 0)
            self.assertTrue(backup_bundle.is_file())

            dest_vault = tmp_path / "dest_vault"
            dest_gemini = tmp_path / "dest_gemini"
            dest_vault.mkdir(mode=0o700)
            dest_gemini.mkdir(mode=0o700)

            dest_engine = AgySwitchEngine(
                vault_dir=dest_vault,
                gemini_dir=dest_gemini,
                legacy_root=tmp_path / "dest_legacy",
            )

            stdout_buf = io.StringIO()
            with patch("sys.stdout", stdout_buf):
                ret = main(["vault", "import", str(backup_bundle)], engine=dest_engine)
            self.assertEqual(ret, 0)

            restored_token = dest_vault / "accounts" / "backup_acc" / "token.json"
            self.assertTrue(restored_token.is_file(), "Restored token file must exist")
            file_mode = stat.S_IMODE(os.stat(restored_token).st_mode)
            self.assertEqual(file_mode, 0o600, f"Token file must have 0600 permissions, got {oct(file_mode)}")

            restored_acc_dir = dest_vault / "accounts" / "backup_acc"
            dir_mode = stat.S_IMODE(os.stat(restored_acc_dir).st_mode)
            self.assertEqual(dir_mode, 0o700, f"Account dir must have 0700 permissions, got {oct(dir_mode)}")

    def test_vault_real_engine_lifecycle_and_edge_cases(self):
        """Tests nonexistent import bundle, export conflict without force, and nested dir export."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            vault_dir = tmp_path / "vault"
            gemini_dir = tmp_path / "gemini"
            vault_dir.mkdir(mode=0o700)
            gemini_dir.mkdir(mode=0o700)

            engine = AgySwitchEngine(
                vault_dir=vault_dir,
                gemini_dir=gemini_dir,
                legacy_root=tmp_path / "legacy",
            )
            acc_dir = vault_dir / "accounts" / "testacc"
            acc_dir.mkdir(parents=True, mode=0o700)
            (acc_dir / "token.json").write_text(json.dumps({"token": "xyz"}), encoding="utf-8")
            engine.set_active_name("testacc")

            stderr_buf = io.StringIO()
            with patch("sys.stderr", stderr_buf):
                ret = main(["vault", "import", str(tmp_path / "nonexistent.tar.gz")], engine=engine)
            self.assertEqual(ret, 1)
            self.assertIn("not found", stderr_buf.getvalue().lower())

            existing_bundle = tmp_path / "existing.tar.gz"
            existing_bundle.write_bytes(b"dummy")
            stderr_buf = io.StringIO()
            with patch("sys.stderr", stderr_buf):
                ret = main(["vault", "export", str(existing_bundle)], engine=engine)
            self.assertEqual(ret, 1)
            self.assertIn("already exists", stderr_buf.getvalue().lower())

            with patch("sys.stdout", io.StringIO()):
                ret = main(["vault", "export", str(existing_bundle), "--force"], engine=engine)
            self.assertEqual(ret, 0)

            nested_bundle = tmp_path / "nested" / "sub" / "bundle.tar.gz"
            self.assertFalse(nested_bundle.parent.exists())
            with patch("sys.stdout", io.StringIO()):
                ret = main(["vault", "export", str(nested_bundle)], engine=engine)
            self.assertEqual(ret, 0)
            self.assertTrue(nested_bundle.is_file())

    def test_account_exists_and_oob_oauth_retired(self):
        """[OOB-RETIRE-003] Verifies _account_exists delegates to engine.has_account and OOB helpers are retired."""
        import agy_switcher.cli as cli_mod
        import agy_switcher.telemetry as telemetry_mod
        from agy_switcher.cli import _account_exists
        from agy_switcher.telemetry import DEFAULT_CLIENT_ID

        mock_engine = MagicMock(spec=AgySwitchEngine)
        mock_engine.has_account.return_value = True
        self.assertTrue(_account_exists(mock_engine, "active_user"))
        mock_engine.has_account.assert_called_once_with("active_user")

        mock_engine.has_account.return_value = False
        self.assertFalse(_account_exists(mock_engine, "nonexistent"))

        self.assertFalse(hasattr(cli_mod, "_get_default_oauth_url"))
        self.assertFalse(hasattr(telemetry_mod, "build_oauth_url"))
        self.assertTrue(bool(DEFAULT_CLIENT_ID))

    def test_login_failure_never_emits_oob_or_static_oauth_urls_OOB_RETIRE_EDGE_001(self):
        """[OOB-RETIRE-EDGE-001] TTY login failure never falls back to OOB URLs or static OAuth URLs on stdout/stderr."""
        import agy_switcher.cli as cli_mod
        import agy_switcher.telemetry as telemetry_mod
        from agy_switcher.telemetry import DEFAULT_CLIENT_ID

        self.assertTrue(bool(DEFAULT_CLIENT_ID))
        self.assertFalse(hasattr(cli_mod, "_get_default_oauth_url"))
        self.assertFalse(hasattr(telemetry_mod, "build_oauth_url"))
        self.assertFalse(hasattr(AgySwitchEngine, "get_oauth_url"))

        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            real_engine = AgySwitchEngine(
                vault_dir=base / ".antigravity-vault",
                gemini_dir=base / ".gemini",
                legacy_root=base / ".agy-cli-manager",
            )
            self.assertFalse(hasattr(real_engine, "get_oauth_url"))

            clean_env = {k: v for k, v in os.environ.items() if k != "AGY_BINARY"}
            stdout_missing = io.StringIO()
            stderr_missing = io.StringIO()
            with patch.dict(os.environ, clean_env, clear=True), \
                 patch("agy_switcher.vault.Path.home", return_value=base), \
                 patch("agy_switcher.vault.shutil.which", return_value=None), \
                 patch("sys.stdout", stdout_missing), \
                 patch("sys.stderr", stderr_missing):
                ret_missing = main(["login", "fail-acc"], engine=real_engine)

            self.assertEqual(ret_missing, 1)
            stdout_missing_val = stdout_missing.getvalue()
            stderr_missing_val = stderr_missing.getvalue()
            self.assertNotIn("urn:ietf:wg:oauth:2.0:oob", stdout_missing_val)
            self.assertNotIn("urn:ietf:wg:oauth:2.0:oob", stderr_missing_val)
            self.assertNotIn("accounts.google.com/o/oauth2", stdout_missing_val)
            self.assertNotIn("accounts.google.com/o/oauth2", stderr_missing_val)

            class _StubProc:
                def __init__(self, returncode: int = 0) -> None:
                    self.returncode = returncode

                def poll(self) -> int:
                    return self.returncode

                def wait(self, timeout: float | None = None) -> int:
                    return self.returncode

            for exit_code in (0, 1):
                with self.subTest(exit_code=exit_code):
                    stdout_buf = io.StringIO()
                    stderr_buf = io.StringIO()
                    with patch("sys.stdout", stdout_buf), \
                         patch("sys.stderr", stderr_buf), \
                         patch.object(real_engine, "resolve_agy_binary", return_value="/bin/mock_agy"), \
                         patch("subprocess.Popen", return_value=_StubProc(returncode=exit_code)):
                        ret = main(["login", "fail-acc"], engine=real_engine)

                    self.assertEqual(ret, 1)
                    stdout_val = stdout_buf.getvalue()
                    stderr_val = stderr_buf.getvalue()
                    self.assertNotIn("urn:ietf:wg:oauth:2.0:oob", stdout_val)
                    self.assertNotIn("urn:ietf:wg:oauth:2.0:oob", stderr_val)
                    self.assertNotIn("accounts.google.com/o/oauth2", stdout_val)
                    self.assertNotIn("accounts.google.com/o/oauth2", stderr_val)


class TestCLISwitchPreflightAuth(unittest.TestCase):
    """CLI public seam tests for pre-flight auth gating and --skip-auth-check."""

    @staticmethod
    def _make_token_payload(email_addr: str) -> dict[str, object]:
        payload_json = json.dumps(
            {"email": email_addr, "sub": "12345", "aud": "test_client"}
        ).encode("utf-8")
        payload_b64 = base64.urlsafe_b64encode(payload_json).decode("utf-8").rstrip("=")
        return {
            "token": {
                "access_token": f"ya29.token_for_{email_addr}",
                "token_type": "Bearer",
                "refresh_token": f"1//refresh_for_{email_addr}",
                "expiry": "2026-12-31T23:59:59Z",
            },
            "auth_method": "oauth",
            "id_token": f"header.{payload_b64}.signature",
        }

    @staticmethod
    def _register_account(engine: AgySwitchEngine, alias: str, email_addr: str) -> dict[str, object]:
        payload = TestCLISwitchPreflightAuth._make_token_payload(email_addr)
        acc_dir = engine.accounts_dir / alias
        acc_dir.mkdir(parents=True, exist_ok=True)
        (acc_dir / "token.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return payload

    @staticmethod
    def _setup_proc_server(
        proc_dir: Path,
        gemini_dir: Path,
        *,
        server_pid: int,
        busy: bool = False,
    ) -> Path:
        pid_dir = proc_dir / str(server_pid)
        fd_dir = pid_dir / "fd"
        fd_dir.mkdir(parents=True, exist_ok=True)
        (pid_dir / "cmdline").write_bytes(b"/opt/antigravity/bin/language_server\x00--standalone\x00")
        (pid_dir / "stat").write_text(
            f"{server_pid} (language_server) S 1 {server_pid} 1 0 -1\n",
            encoding="utf-8",
        )
        lock_file = gemini_dir / "antigravity" / "presence" / "active-turn.lock"
        if busy:
            lock_file.parent.mkdir(parents=True, exist_ok=True)
            lock_file.write_text(f"pid: {server_pid}\n", encoding="utf-8")
            (fd_dir / "3").symlink_to(lock_file)
        return lock_file

    @staticmethod
    def _make_quota_response(fraction: float = 0.9) -> object:
        payload = json.dumps({
            "groups": [
                {
                    "displayName": "Gemini Models",
                    "buckets": [
                        {
                            "bucketId": "five_hour",
                            "remainingFraction": fraction,
                            "resetTime": "2026-12-31T23:59:59Z",
                        }
                    ],
                }
            ]
        }).encode("utf-8")

        class _FakeResponse:
            def __init__(self, body: bytes) -> None:
                self._body = body
                self.status = 200

            def read(self) -> bytes:
                return self._body

            def __enter__(self) -> "_FakeResponse":
                return self

            def __exit__(self, *_args: object) -> bool:
                return False

        return _FakeResponse(payload)

    def test_switch_preflight_auth_healthy_probe_AUTH_GATE_001(self) -> None:
        """[AUTH-GATE-001] Manual CLI switch defaults verify_auth=True, probes before disk write, activates account, and restarts idle server."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            proc_dir = base / "proc"
            gemini_dir = base / ".gemini"
            engine = AgySwitchEngine(
                vault_dir=base / ".antigravity-vault",
                gemini_dir=gemini_dir,
                legacy_root=base / ".agy-cli-manager",
                proc_dir=proc_dir,
            )
            self._register_account(engine, "primary-acc", "primary@example.com")
            self._register_account(engine, "work-acc", "work@example.com")
            engine.switch_tokens("primary-acc", restart_server=False)

            desktop_before = engine.desktop_token_file.read_text(encoding="utf-8")
            cli_before = engine.cli_token_file.read_text(encoding="utf-8")
            active_before = engine.get_active_name()

            server_pid = 48100
            self._setup_proc_server(proc_dir, gemini_dir, server_pid=server_pid, busy=False)

            probe_snapshots: list[tuple[str, str, str | None]] = []
            signals_sent: list[tuple[int, int]] = []
            terminated = False

            def fake_urlopen(*_args: object, **_kwargs: object) -> object:
                # Assert pre-flight probe executes BEFORE any live token or active-pointer disk write
                probe_snapshots.append((
                    engine.desktop_token_file.read_text(encoding="utf-8"),
                    engine.cli_token_file.read_text(encoding="utf-8"),
                    engine.get_active_name(),
                ))
                return self._make_quota_response()

            def fake_kill(pid: int, sig: int) -> None:
                nonlocal terminated
                if sig == 0:
                    if terminated:
                        raise ProcessLookupError()
                    return
                signals_sent.append((pid, sig))
                terminated = True

            stdout_buf = io.StringIO()
            stderr_buf = io.StringIO()
            with patch("urllib.request.urlopen", side_effect=fake_urlopen) as mock_urlopen, \
                 patch("os.kill", side_effect=fake_kill), \
                 patch("sys.stdout", stdout_buf), \
                 patch("sys.stderr", stderr_buf):
                exit_code = main(["switch", "work-acc"], engine=engine)

            self.assertEqual(exit_code, 0)
            self.assertEqual(mock_urlopen.call_count, 1)
            self.assertEqual(probe_snapshots, [(desktop_before, cli_before, active_before)])
            self.assertEqual(engine.get_active_name(), "work-acc")
            self.assertEqual(
                json.loads(engine.desktop_token_file.read_text(encoding="utf-8"))["token"]["access_token"],
                "ya29.token_for_work@example.com",
            )
            self.assertEqual(
                json.loads(engine.cli_token_file.read_text(encoding="utf-8"))["token"]["access_token"],
                "ya29.token_for_work@example.com",
            )
            self.assertEqual(len(signals_sent), 1)
            self.assertEqual(signals_sent[0][0], server_pid)
            self.assertIn("Restarted language_server", stdout_buf.getvalue())

    def test_switch_preflight_auth_skip_flag_preserves_turn_safety_AUTH_GATE_003(self) -> None:
        """[AUTH-GATE-003] --skip-auth-check sets verify_auth=False, bypasses probe_account_auth(), preserves busy protection without --force, and activates when idle."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            proc_dir = base / "proc"
            gemini_dir = base / ".gemini"
            engine = AgySwitchEngine(
                vault_dir=base / ".antigravity-vault",
                gemini_dir=gemini_dir,
                legacy_root=base / ".agy-cli-manager",
                proc_dir=proc_dir,
            )
            self._register_account(engine, "primary-acc", "primary@example.com")
            self._register_account(engine, "offline-acc", "offline@example.com")
            engine.switch_tokens("primary-acc", restart_server=False)

            desktop_before = engine.desktop_token_file.read_text(encoding="utf-8")
            cli_before = engine.cli_token_file.read_text(encoding="utf-8")

            server_pid = 48200
            lock_file = self._setup_proc_server(proc_dir, gemini_dir, server_pid=server_pid, busy=True)

            err_403 = urllib.error.HTTPError(
                "https://cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels",
                403,
                "Forbidden",
                email.message.Message(),
                io.BytesIO(b"Forbidden"),
            )

            # 1. Busy server without --force still raises ActiveTurnRunningError -> exit 2, no HTTP call, no disk write
            stderr_busy = io.StringIO()
            with patch("urllib.request.urlopen", side_effect=err_403) as mock_urlopen, \
                 patch("sys.stderr", stderr_busy):
                busy_code = main(["switch", "offline-acc", "--skip-auth-check"], engine=engine)

            self.assertEqual(busy_code, 2)
            mock_urlopen.assert_not_called()
            self.assertEqual(engine.get_active_name(), "primary-acc")
            self.assertEqual(engine.desktop_token_file.read_text(encoding="utf-8"), desktop_before)
            self.assertEqual(engine.cli_token_file.read_text(encoding="utf-8"), cli_before)

            # 2. Clear active turn -> idle server activates offline-acc with --skip-auth-check and zero HTTP calls
            lock_file.unlink()
            (proc_dir / str(server_pid) / "fd" / "3").unlink()

            signals_sent: list[tuple[int, int]] = []
            terminated = False

            def fake_kill(pid: int, sig: int) -> None:
                nonlocal terminated
                if sig == 0:
                    if terminated:
                        raise ProcessLookupError()
                    return
                signals_sent.append((pid, sig))
                terminated = True

            stdout_idle = io.StringIO()
            stderr_idle = io.StringIO()
            with patch("urllib.request.urlopen", side_effect=err_403) as mock_urlopen, \
                 patch("os.kill", side_effect=fake_kill), \
                 patch("sys.stdout", stdout_idle), \
                 patch("sys.stderr", stderr_idle):
                idle_code = main(["switch", "offline-acc", "--skip-auth-check"], engine=engine)

            self.assertEqual(idle_code, 0)
            mock_urlopen.assert_not_called()
            self.assertEqual(engine.get_active_name(), "offline-acc")
            self.assertEqual(
                json.loads(engine.desktop_token_file.read_text(encoding="utf-8"))["token"]["access_token"],
                "ya29.token_for_offline@example.com",
            )
            self.assertEqual(
                json.loads(engine.cli_token_file.read_text(encoding="utf-8"))["token"]["access_token"],
                "ya29.token_for_offline@example.com",
            )
            self.assertEqual(len(signals_sent), 1)

    def test_switch_preflight_auth_error_exits_one_and_decouples_force(self) -> None:
        """Manual switch fails closed with exit 1 on AccountAuthError even with --force unless --skip-auth-check is passed."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            proc_dir = base / "proc"
            gemini_dir = base / ".gemini"
            engine = AgySwitchEngine(
                vault_dir=base / ".antigravity-vault",
                gemini_dir=gemini_dir,
                legacy_root=base / ".agy-cli-manager",
                proc_dir=proc_dir,
            )
            self._register_account(engine, "healthy-acc", "healthy@example.com")
            self._register_account(engine, "revoked-acc", "revoked@example.com")
            engine.switch_tokens("healthy-acc", restart_server=False)

            desktop_before = engine.desktop_token_file.read_text(encoding="utf-8")
            cli_before = engine.cli_token_file.read_text(encoding="utf-8")

            def make_403(*_args: object, **_kwargs: object) -> object:
                raise urllib.error.HTTPError(
                    "https://cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels",
                    403,
                    "Forbidden",
                    email.message.Message(),
                    io.BytesIO(b"Forbidden"),
                )

            stderr_buf = io.StringIO()
            with patch("urllib.request.urlopen", side_effect=make_403) as mock_urlopen, \
                 patch("sys.stderr", stderr_buf):
                code = main(["switch", "revoked-acc", "--force"], engine=engine)

            self.assertEqual(code, 1)
            self.assertEqual(mock_urlopen.call_count, 2)  # initial 403 + 1x refresh 403
            self.assertIn("agy-switch login revoked-acc", stderr_buf.getvalue())
            self.assertEqual(engine.get_active_name(), "healthy-acc")
            self.assertEqual(engine.desktop_token_file.read_text(encoding="utf-8"), desktop_before)
            self.assertEqual(engine.cli_token_file.read_text(encoding="utf-8"), cli_before)

    @staticmethod
    def _make_http_403(*_args: object, **_kwargs: object) -> object:
        raise urllib.error.HTTPError(
            "https://cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels",
            403,
            "Forbidden",
            email.message.Message(),
            io.BytesIO(b"Forbidden"),
        )

    def test_force_auth_decoupling_and_sdk_default_cli_force_preserves_verify_auth_AUTH_GATE_ERR_003(self) -> None:
        """[AUTH-GATE-ERR-003] agy-switch switch revoked-acc --force keeps verify_auth=True, exits 1 on 403, leaves live tokens untouched, and keeps language_server running."""
        for busy_state in (False, True):
            for no_restart in (False, True):
                with self.subTest(server_busy=busy_state, no_restart=no_restart), tempfile.TemporaryDirectory() as tmp_dir:
                    base = Path(tmp_dir)
                    proc_dir = base / "proc"
                    gemini_dir = base / ".gemini"
                    engine = AgySwitchEngine(
                        vault_dir=base / ".antigravity-vault",
                        gemini_dir=gemini_dir,
                        legacy_root=base / ".agy-cli-manager",
                        proc_dir=proc_dir,
                    )
                    self._register_account(engine, "healthy-acc", "healthy@example.com")
                    self._register_account(engine, "revoked-acc", "revoked@example.com")
                    engine.switch_tokens("healthy-acc", restart_server=False)

                    desktop_before = engine.desktop_token_file.read_text(encoding="utf-8")
                    cli_before = engine.cli_token_file.read_text(encoding="utf-8")
                    server_pid = 48300
                    self._setup_proc_server(proc_dir, gemini_dir, server_pid=server_pid, busy=busy_state)

                    signals_sent: list[tuple[int, int]] = []

                    def fake_kill(pid: int, sig: int) -> None:
                        signals_sent.append((pid, sig))

                    argv = ["switch", "revoked-acc", "--force"]
                    if no_restart:
                        argv.append("--no-restart")

                    stderr_buf = io.StringIO()
                    with patch("urllib.request.urlopen", side_effect=self._make_http_403) as mock_urlopen, \
                         patch("os.kill", side_effect=fake_kill), \
                         patch("sys.stderr", stderr_buf):
                        exit_code = main(argv, engine=engine)

                    self.assertEqual(exit_code, 1)
                    self.assertEqual(mock_urlopen.call_count, 2)  # initial 403 + 1x refresh 403
                    self.assertIn("agy-switch login revoked-acc", stderr_buf.getvalue())
                    self.assertEqual(engine.get_active_name(), "healthy-acc")
                    self.assertEqual(engine.desktop_token_file.read_text(encoding="utf-8"), desktop_before)
                    self.assertEqual(engine.cli_token_file.read_text(encoding="utf-8"), cli_before)
                    self.assertEqual(signals_sent, [])
                    self.assertEqual(engine.locate_language_server_pid(), server_pid)
                    failover_state = json.loads(engine.failover_state_file.read_text(encoding="utf-8"))
                    self.assertIn("revoked-acc", failover_state.get("auth_errors", {}))

    def test_switch_missing_refresh_token_401_skips_refresh_and_fails_closed_AUTH_GATE_EDGE_002(self) -> None:
        """[AUTH-GATE-EDGE-002] agy-switch switch no-refresh-acc with empty/whitespace refresh_token and HTTP 401 skips refresh, exits 1, quarantines alias, and preserves live tokens."""
        for bad_refresh in ("", "   "):
            with self.subTest(refresh_token=bad_refresh), tempfile.TemporaryDirectory() as tmp_dir:
                base = Path(tmp_dir)
                proc_dir = base / "proc"
                gemini_dir = base / ".gemini"
                engine = AgySwitchEngine(
                    vault_dir=base / ".antigravity-vault",
                    gemini_dir=gemini_dir,
                    legacy_root=base / ".agy-cli-manager",
                    proc_dir=proc_dir,
                )
                self._register_account(engine, "healthy-acc", "healthy@example.com")
                no_refresh_payload = self._make_token_payload("norefresh@example.com")
                tok_dict = no_refresh_payload["token"]
                assert isinstance(tok_dict, dict)
                tok_dict["refresh_token"] = bad_refresh
                no_ref_dir = engine.accounts_dir / "no-refresh-acc"
                no_ref_dir.mkdir(parents=True, exist_ok=True)
                (no_ref_dir / "token.json").write_text(
                    json.dumps(no_refresh_payload, indent=2) + "\n", encoding="utf-8"
                )
                engine.switch_tokens("healthy-acc", restart_server=False)

                desktop_before = engine.desktop_token_file.read_text(encoding="utf-8")
                cli_before = engine.cli_token_file.read_text(encoding="utf-8")

                err_401 = urllib.error.HTTPError(
                    "https://cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels",
                    401,
                    "Unauthorized",
                    email.message.Message(),
                    io.BytesIO(b"Unauthorized"),
                )
                stderr_buf = io.StringIO()
                with patch("urllib.request.urlopen", side_effect=err_401) as mock_urlopen, \
                     patch("agy_switcher.telemetry.QuotaClient.refresh_access_token") as mock_refresh, \
                     patch("sys.stderr", stderr_buf):
                    exit_code = main(["switch", "no-refresh-acc"], engine=engine)

                self.assertEqual(exit_code, 1)
                self.assertEqual(mock_urlopen.call_count, 1)
                mock_refresh.assert_not_called()
                self.assertIn("agy-switch login no-refresh-acc", stderr_buf.getvalue())
                self.assertEqual(engine.get_active_name(), "healthy-acc")
                self.assertEqual(engine.desktop_token_file.read_text(encoding="utf-8"), desktop_before)
                self.assertEqual(engine.cli_token_file.read_text(encoding="utf-8"), cli_before)
                failover_state = json.loads(engine.failover_state_file.read_text(encoding="utf-8"))
                self.assertIn("no-refresh-acc", failover_state.get("auth_errors", {}))

    def test_force_auth_decoupling_and_sdk_default_cli_force_with_skip_auth_check(self) -> None:
        """Combining --force and --skip-auth-check overrides busy turn guard and skips CloudCode auth check independently."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            proc_dir = base / "proc"
            gemini_dir = base / ".gemini"
            engine = AgySwitchEngine(
                vault_dir=base / ".antigravity-vault",
                gemini_dir=gemini_dir,
                legacy_root=base / ".agy-cli-manager",
                proc_dir=proc_dir,
            )
            self._register_account(engine, "primary-acc", "primary@example.com")
            self._register_account(engine, "offline-acc", "offline@example.com")
            engine.switch_tokens("primary-acc", restart_server=False)

            server_pid = 48301
            self._setup_proc_server(proc_dir, gemini_dir, server_pid=server_pid, busy=True)

            signals_sent: list[tuple[int, int]] = []
            terminated = False

            def fake_kill(pid: int, sig: int) -> None:
                nonlocal terminated
                if sig == 0:
                    if terminated:
                        raise ProcessLookupError()
                    return
                signals_sent.append((pid, sig))
                terminated = True

            stdout_buf = io.StringIO()
            stderr_buf = io.StringIO()
            with patch("urllib.request.urlopen", side_effect=self._make_http_403) as mock_urlopen, \
                 patch("os.kill", side_effect=fake_kill), \
                 patch("sys.stdout", stdout_buf), \
                 patch("sys.stderr", stderr_buf):
                exit_code = main(["switch", "offline-acc", "--force", "--skip-auth-check"], engine=engine)

            self.assertEqual(exit_code, 0)
            mock_urlopen.assert_not_called()
            self.assertEqual(engine.get_active_name(), "offline-acc")
            self.assertEqual(
                json.loads(engine.desktop_token_file.read_text(encoding="utf-8"))["token"]["access_token"],
                "ya29.token_for_offline@example.com",
            )
            self.assertEqual(
                json.loads(engine.cli_token_file.read_text(encoding="utf-8"))["token"]["access_token"],
                "ya29.token_for_offline@example.com",
            )
            self.assertEqual(len(signals_sent), 1)
            self.assertEqual(signals_sent[0][0], server_pid)


class TestCLIInTurnDeferredReload(unittest.TestCase):
    """CLI public seam tests for Ancestor Lineage Self-Kill Guard and Deferred Safe-Turn Reload."""

    @staticmethod
    def _register_account(engine: AgySwitchEngine, alias: str, email_addr: str) -> None:
        TestCLISwitchPreflightAuth._register_account(engine, alias, email_addr)

    @staticmethod
    def _setup_in_turn_proc_lineage(
        proc_dir: Path,
        gemini_dir: Path,
        *,
        server_pid: int = 49100,
        child_pid: int = 49110,
        busy: bool = True,
    ) -> Path:
        lock_file = TestCLISwitchPreflightAuth._setup_proc_server(
            proc_dir, gemini_dir, server_pid=server_pid, busy=busy
        )
        init_dir = proc_dir / "1"
        init_dir.mkdir(parents=True, exist_ok=True)
        (init_dir / "cmdline").write_bytes(b"/sbin/init\x00")
        (init_dir / "stat").write_text("1 (systemd) S 0 1 1 0 -1\n", encoding="utf-8")

        child_dir = proc_dir / str(child_pid)
        child_dir.mkdir(parents=True, exist_ok=True)
        (child_dir / "cmdline").write_bytes(b"/bin/bash\x00-c\x00agy-switch\x00")
        (child_dir / "stat").write_text(
            f"{child_pid} (bash) S {server_pid} {child_pid} 1 0 -1\n",
            encoding="utf-8",
        )
        return lock_file

    @staticmethod
    def _make_quota_response(fraction: float) -> object:
        return TestCLISwitchPreflightAuth._make_quota_response(fraction=fraction)

    def test_in_turn_deferred_reload_auto_switch_DEFER_RELOAD_002(self) -> None:
        """[DEFER-RELOAD-002] In-turn switch --auto downgrades to Disk-Only Swap and writes pending_reload.json."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            proc_dir = base / "proc"
            gemini_dir = base / ".gemini"
            engine = AgySwitchEngine(
                vault_dir=base / ".antigravity-vault",
                gemini_dir=gemini_dir,
                legacy_root=base / ".agy-cli-manager",
                proc_dir=proc_dir,
            )
            self._register_account(engine, "primary-acc", "primary@example.com")
            self._register_account(engine, "best-standby", "best@example.com")
            engine.switch_tokens("primary-acc", restart_server=False)

            self._setup_in_turn_proc_lineage(
                proc_dir, gemini_dir, server_pid=49100, child_pid=49110, busy=True
            )

            def fake_urlopen(req: object, *_args: object, **_kwargs: object) -> object:
                headers = getattr(req, "headers", {}) or {}
                auth = headers.get("Authorization", "")
                if "best@example.com" in auth:
                    return self._make_quota_response(0.95)
                return self._make_quota_response(0.05)

            stdout_buf = io.StringIO()
            stderr_buf = io.StringIO()
            with patch.object(engine, "switch_tokens", wraps=engine.switch_tokens) as spy_switch, \
                 patch("os.getpid", return_value=49110), \
                 patch("urllib.request.urlopen", side_effect=fake_urlopen), \
                 patch("os.kill", side_effect=AssertionError("os.kill must not be called in-turn")), \
                 patch("subprocess.Popen", side_effect=AssertionError("subprocess.Popen must not be spawned")), \
                 patch("sys.stdout", stdout_buf), \
                 patch("sys.stderr", stderr_buf):
                exit_code = main(["switch", "--auto"], engine=engine)

            self.assertEqual(exit_code, 0)
            spy_switch.assert_called_once_with("best-standby", restart_server=True, force=True)
            self.assertEqual(engine.get_active_name(), "best-standby")
            self.assertEqual(
                json.loads(engine.desktop_token_file.read_text(encoding="utf-8"))["token"]["access_token"],
                "ya29.token_for_best@example.com",
            )
            self.assertEqual(
                json.loads(engine.cli_token_file.read_text(encoding="utf-8"))["token"]["access_token"],
                "ya29.token_for_best@example.com",
            )

            pending_file = engine.pending_reload_file
            self.assertTrue(pending_file.is_file())
            self.assertEqual(stat.S_IMODE(pending_file.stat().st_mode), 0o600)
            pending_data = json.loads(pending_file.read_text(encoding="utf-8"))
            self.assertEqual(pending_data["target_account"], "best-standby")
            self.assertEqual(pending_data["ttl_seconds"], 120)

            out = stdout_buf.getvalue()
            self.assertIn("Deferred", out)
            self.assertIn("pending_reload.json", out)
            self.assertNotIn("not running or no restart needed", out)

    def test_in_turn_deferred_reload_inactive_watcher_guidance_DEFER_RELOAD_004(self) -> None:
        """[DEFER-RELOAD-004] In-turn deferred switch with inactive LogWatcher prints reload guidance without spawning waiter subprocess."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            proc_dir = base / "proc"
            gemini_dir = base / ".gemini"
            engine = AgySwitchEngine(
                vault_dir=base / ".antigravity-vault",
                gemini_dir=gemini_dir,
                legacy_root=base / ".agy-cli-manager",
                proc_dir=proc_dir,
            )
            self._register_account(engine, "primary-acc", "primary@example.com")
            self._register_account(engine, "work-acc", "work@example.com")
            engine.switch_tokens("primary-acc", restart_server=False)

            self._setup_in_turn_proc_lineage(
                proc_dir, gemini_dir, server_pid=49200, child_pid=49210, busy=True
            )
            from agy_switcher.watcher import DaemonManager

            dm = DaemonManager(
                vault_dir=engine.vault_dir,
                proc_dir=proc_dir,
                gemini_dir=gemini_dir,
            )

            stdout_buf = io.StringIO()
            stderr_buf = io.StringIO()
            with patch("os.getpid", return_value=49210), \
                 patch("urllib.request.urlopen", return_value=self._make_quota_response(0.9)), \
                 patch("os.kill", side_effect=AssertionError("os.kill must not be called in-turn")), \
                 patch("subprocess.Popen", side_effect=AssertionError("subprocess.Popen must not be spawned")) as mock_popen, \
                 patch.dict(os.environ, {"AGY_DISABLE_HOST_SYSTEMD": "1"}), \
                 patch("sys.stdout", stdout_buf), \
                 patch("sys.stderr", stderr_buf):
                self.assertIsNone(dm.get_running_pid())
                self.assertFalse(dm.is_systemd_active())
                exit_code = main(["switch", "work-acc", "--force"], engine=engine)

            self.assertEqual(exit_code, 0)
            mock_popen.assert_not_called()
            self.assertEqual(engine.get_active_name(), "work-acc")
            self.assertEqual(
                json.loads(engine.desktop_token_file.read_text(encoding="utf-8"))["token"]["access_token"],
                "ya29.token_for_work@example.com",
            )
            self.assertEqual(
                json.loads(engine.cli_token_file.read_text(encoding="utf-8"))["token"]["access_token"],
                "ya29.token_for_work@example.com",
            )

            pending_file = engine.pending_reload_file
            self.assertTrue(pending_file.is_file())
            pending_data = json.loads(pending_file.read_text(encoding="utf-8"))
            self.assertEqual(pending_data["target_account"], "work-acc")
            self.assertEqual(pending_data["ttl_seconds"], 120)

            out = stdout_buf.getvalue()
            self.assertIn("Deferred", out)
            self.assertIn("LogWatcher is inactive", out)
            self.assertIn("after the turn completes", out)

    def test_in_turn_deferred_reload_active_watcher_omits_inactive_guidance(self) -> None:
        """When LogWatcher is active (systemd or PID), deferred reload output omits inactive watcher warning."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            proc_dir = base / "proc"
            gemini_dir = base / ".gemini"
            engine = AgySwitchEngine(
                vault_dir=base / ".antigravity-vault",
                gemini_dir=gemini_dir,
                legacy_root=base / ".agy-cli-manager",
                proc_dir=proc_dir,
            )
            self._register_account(engine, "primary-acc", "primary@example.com")
            self._register_account(engine, "work-acc", "work@example.com")
            engine.switch_tokens("primary-acc", restart_server=False)

            self._setup_in_turn_proc_lineage(
                proc_dir, gemini_dir, server_pid=49300, child_pid=49310, busy=True
            )

            stdout_buf = io.StringIO()
            stderr_buf = io.StringIO()
            with patch("os.getpid", return_value=49310), \
                 patch("urllib.request.urlopen", return_value=self._make_quota_response(0.9)), \
                 patch("os.kill", side_effect=AssertionError("os.kill must not be called in-turn")), \
                 patch("subprocess.Popen", side_effect=AssertionError("subprocess.Popen must not be spawned")), \
                 patch("agy_switcher.watcher.DaemonManager.is_systemd_active", return_value=True), \
                 patch("sys.stdout", stdout_buf), \
                 patch("sys.stderr", stderr_buf):
                exit_code = main(["switch", "work-acc", "--force"], engine=engine)

            self.assertEqual(exit_code, 0)
            out = stdout_buf.getvalue()
            self.assertIn("Deferred", out)
            self.assertNotIn("LogWatcher is inactive", out)

    def test_in_turn_deferred_reload_force_restart_now_override_DEFER_RELOAD_005(self) -> None:
        """[DEFER-RELOAD-005] --force-restart-now bypasses in-turn deferred reload and restarts language_server synchronously."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            proc_dir = base / "proc"
            gemini_dir = base / ".gemini"
            engine = AgySwitchEngine(
                vault_dir=base / ".antigravity-vault",
                gemini_dir=gemini_dir,
                legacy_root=base / ".agy-cli-manager",
                proc_dir=proc_dir,
            )
            self._register_account(engine, "primary-acc", "primary@example.com")
            self._register_account(engine, "work-acc", "work@example.com")
            engine.switch_tokens("primary-acc", restart_server=False)

            server_pid = 49400
            self._setup_in_turn_proc_lineage(
                proc_dir, gemini_dir, server_pid=server_pid, child_pid=49410, busy=True
            )

            signals_sent: list[tuple[int, int]] = []
            terminated = False

            def fake_kill(pid: int, sig: int) -> None:
                nonlocal terminated
                if sig == 0:
                    if terminated:
                        raise ProcessLookupError()
                    return
                signals_sent.append((pid, sig))
                terminated = True

            stdout_buf = io.StringIO()
            stderr_buf = io.StringIO()
            with patch("os.getpid", return_value=49410), \
                 patch("urllib.request.urlopen", return_value=self._make_quota_response(0.9)), \
                 patch("os.kill", side_effect=fake_kill), \
                 patch("sys.stdout", stdout_buf), \
                 patch("sys.stderr", stderr_buf):
                exit_code = main(
                    ["switch", "work-acc", "--force", "--force-restart-now"],
                    engine=engine,
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(len(signals_sent), 1)
            self.assertEqual(signals_sent[0][0], server_pid)
            self.assertFalse(engine.pending_reload_file.exists())
            self.assertIn("Restarted language_server", stdout_buf.getvalue())

    def test_in_turn_force_restart_now_precedence_matrix_DEFER_RELOAD_EDGE_004(self) -> None:
        """[DEFER-RELOAD-EDGE-004] switch --auto --force-restart-now restarts immediately; manual switch --force-restart-now without --force honors ActiveTurnRunningError (exit 2)."""
        from agy_switcher.cli import build_parser

        parser = build_parser()
        auto_args = parser.parse_args(["switch", "--auto", "--force-restart-now"])
        self.assertTrue(auto_args.auto)
        self.assertTrue(auto_args.force_restart_now)
        self.assertFalse(auto_args.force)

        manual_args = parser.parse_args(["switch", "work-acc", "--force-restart-now"])
        self.assertEqual(manual_args.account, "work-acc")
        self.assertTrue(manual_args.force_restart_now)
        self.assertFalse(manual_args.force)

        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            proc_dir = base / "proc"
            gemini_dir = base / ".gemini"
            engine = AgySwitchEngine(
                vault_dir=base / ".antigravity-vault",
                gemini_dir=gemini_dir,
                legacy_root=base / ".agy-cli-manager",
                proc_dir=proc_dir,
            )
            self._register_account(engine, "primary-acc", "primary@example.com")
            self._register_account(engine, "work-acc", "work@example.com")
            self._register_account(engine, "best-standby", "best@example.com")
            engine.switch_tokens("primary-acc", restart_server=False)

            server_pid = 49500
            child_pid = 49510
            self._setup_in_turn_proc_lineage(
                proc_dir, gemini_dir, server_pid=server_pid, child_pid=child_pid, busy=True
            )

            desktop_before = engine.desktop_token_file.read_text(encoding="utf-8")
            cli_before = engine.cli_token_file.read_text(encoding="utf-8")

            # 1. Manual switch <name> --force-restart-now WITHOUT --force enforces verify_turn_safety(force=False) -> exit 2
            manual_stdout = io.StringIO()
            manual_stderr = io.StringIO()
            with patch.object(engine, "switch_tokens", wraps=engine.switch_tokens) as spy_switch_manual, \
                 patch.object(engine, "verify_turn_safety", wraps=engine.verify_turn_safety) as spy_safety, \
                 patch("os.getpid", return_value=child_pid), \
                 patch("urllib.request.urlopen", side_effect=AssertionError("Pre-flight auth probe must not run when ActiveTurnRunningError fires")), \
                 patch("os.kill", side_effect=AssertionError("os.kill must not run when ActiveTurnRunningError fires")), \
                 patch("sys.stdout", manual_stdout), \
                 patch("sys.stderr", manual_stderr):
                self.assertTrue(engine.is_invoked_by_language_server())
                manual_exit = main(["switch", "work-acc", "--force-restart-now"], engine=engine)

            self.assertEqual(manual_exit, 2)
            spy_switch_manual.assert_called_once_with(
                "work-acc",
                restart_server=True,
                force=False,
                verify_auth=True,
                force_restart_now=True,
            )
            spy_safety.assert_called_once_with(force=False)
            self.assertIn("Warning:", manual_stderr.getvalue())
            self.assertEqual(engine.get_active_name(), "primary-acc")
            self.assertEqual(engine.desktop_token_file.read_text(encoding="utf-8"), desktop_before)
            self.assertEqual(engine.cli_token_file.read_text(encoding="utf-8"), cli_before)
            self.assertFalse(engine.pending_reload_file.exists())

            # 2. Auto switch --auto --force-restart-now sets force=True & force_restart_now=True -> immediate restart
            def fake_urlopen(req: object, *_args: object, **_kwargs: object) -> object:
                headers = getattr(req, "headers", {}) or {}
                auth = headers.get("Authorization", "")
                if "best@example.com" in auth:
                    return self._make_quota_response(0.95)
                return self._make_quota_response(0.05)

            signals_sent: list[tuple[int, int]] = []
            terminated = False

            def fake_kill(pid: int, sig: int) -> None:
                nonlocal terminated
                if sig == 0:
                    if terminated:
                        raise ProcessLookupError()
                    return
                signals_sent.append((pid, sig))
                terminated = True

            auto_stdout = io.StringIO()
            auto_stderr = io.StringIO()
            with patch.object(engine, "switch_tokens", wraps=engine.switch_tokens) as spy_switch_auto, \
                 patch.object(engine, "restart_language_server", wraps=engine.restart_language_server) as spy_restart, \
                 patch("os.getpid", return_value=child_pid), \
                 patch("urllib.request.urlopen", side_effect=fake_urlopen), \
                 patch("os.kill", side_effect=fake_kill), \
                 patch("sys.stdout", auto_stdout), \
                 patch("sys.stderr", auto_stderr):
                auto_exit = main(["switch", "--auto", "--force-restart-now"], engine=engine)

            self.assertEqual(auto_exit, 0)
            spy_switch_auto.assert_called_once_with(
                "best-standby",
                restart_server=True,
                force=True,
                force_restart_now=True,
            )
            spy_restart.assert_called_once_with(force=True)
            self.assertEqual(len(signals_sent), 1)
            self.assertEqual(signals_sent[0][0], server_pid)
            self.assertEqual(engine.get_active_name(), "best-standby")
            self.assertEqual(
                json.loads(engine.desktop_token_file.read_text(encoding="utf-8"))["token"]["access_token"],
                "ya29.token_for_best@example.com",
            )
            self.assertEqual(
                json.loads(engine.cli_token_file.read_text(encoding="utf-8"))["token"]["access_token"],
                "ya29.token_for_best@example.com",
            )
            self.assertFalse(engine.pending_reload_file.exists())
            self.assertIn("Restarted language_server", auto_stdout.getvalue())


if __name__ == '__main__':
    unittest.main()
