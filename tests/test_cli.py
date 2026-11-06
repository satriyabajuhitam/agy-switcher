import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure the scripts directory is in the path
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'scripts')))

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

        # Verify manual switch preserves force=False by default (turn safety)
        self.engine.switch_tokens.assert_called_once_with("acc2", restart_server=True, force=False)

    def test_manual_switch_with_force(self):
        with patch('sys.stdout'):
            exit_code = main(["switch", "acc2", "--force"], engine=self.engine)
            self.assertEqual(exit_code, 0)

        # Verify manual switch with --force passes force=True
        self.engine.switch_tokens.assert_called_once_with("acc2", restart_server=True, force=True)

    def test_manual_switch_no_restart(self):
        with patch('sys.stdout'):
            exit_code = main(["switch", "acc2", "--no-restart"], engine=self.engine)
            self.assertEqual(exit_code, 0)

        # Verify manual switch with --no-restart passes restart_server=False and force=False
        self.engine.switch_tokens.assert_called_once_with("acc2", restart_server=False, force=False)

    def test_manual_switch_active_turn_error(self):
        self.engine.switch_tokens.side_effect = ActiveTurnRunningError(
            "Active turn running on language_server PID 12345"
        )
        err_buf = io.StringIO()
        with patch('sys.stderr', err_buf):
            exit_code = main(["switch", "acc2"], engine=self.engine)
            self.assertEqual(exit_code, 2)

        self.assertIn("Warning: Active turn running on language_server PID 12345", err_buf.getvalue())
        self.engine.switch_tokens.assert_called_once_with("acc2", restart_server=True, force=False)

    def test_manual_switch_with_force_and_no_restart(self):
        with patch('sys.stdout'):
            exit_code = main(["switch", "acc2", "--force", "--no-restart"], engine=self.engine)
            self.assertEqual(exit_code, 0)

        # Verify manual switch with both --force and --no-restart
        self.engine.switch_tokens.assert_called_once_with("acc2", restart_server=False, force=True)

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
    def test_quota_subcommand_json_preserves_claude(self, mock_qc_class):
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
            self.assertIn("claude", acc["quota"])
            self.assertEqual(set(acc["quota"].keys()), {"gemini", "claude"})

    @patch('agy_switch.QuotaClient')
    def test_quota_subcommand_model_claude_sorting(self, mock_qc_class):
        """agy-switch quota --model claude --json sorts accounts by Claude quota descending."""
        self.engine.get_account_tokens.side_effect = lambda name: {
            "acc1": {"access_token": "tok1"},
            "acc2": {"access_token": "tok2"},
        }.get(name)
        mock_qc = mock_qc_class.return_value
        mock_qc.fetch_quota.side_effect = lambda tok, refresh_token=None, on_refresh=None: {
            "tok1": {
                "gemini": {"quota": 90, "reset_time": 1791201600, "cached": False, "buckets": {}},
                "claude": {"quota": 20, "reset_time": 1791201600, "cached": False, "buckets": {}},
            },
            "tok2": {
                "gemini": {"quota": 30, "reset_time": 1791201600, "cached": False, "buckets": {}},
                "claude": {"quota": 85, "reset_time": 1791201600, "cached": False, "buckets": {}},
            },
        }.get(tok, {})
        mock_qc.load_cache.return_value = {}

        buf = io.StringIO()
        with patch('sys.stdout', buf):
            exit_code = main(["quota", "--model", "claude", "--json"], engine=self.engine)
            self.assertEqual(exit_code, 0)

        data = json.loads(buf.getvalue())
        order = [a["name"] for a in data["accounts"]]
        self.assertEqual(order[0], "acc2")  # Claude 85% > 20%
        self.assertEqual(order[1], "acc1")

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
            "claude": {"quota": 45, "reset_time": 1791201600, "cached": False, "buckets": {}},
        }
        mock_qc.load_cache.return_value = {}

        buf_table = io.StringIO()
        with patch('sys.stdout', buf_table):
            exit_code = main(["quota", "--model", "claude"], engine=self.engine)
            self.assertEqual(exit_code, 0)

        output_table = buf_table.getvalue()
        self.assertIn("CLAUDE QUOTA", output_table)
        self.assertNotIn("GEMINI QUOTA", output_table)
        self.assertIn("45%", output_table)

        buf_listicle = io.StringIO()
        with patch('sys.stdout', buf_listicle):
            exit_code = main(["quota", "--model", "claude", "--format", "list"], engine=self.engine)
            self.assertEqual(exit_code, 0)

        output_listicle = buf_listicle.getvalue()
        self.assertIn("— Claude: 45%", output_listicle)
        self.assertNotIn("— Gemini:", output_listicle)

        buf_gemini = io.StringIO()
        with patch('sys.stdout', buf_gemini):
            exit_code = main(["quota", "--model", "gemini"], engine=self.engine)
            self.assertEqual(exit_code, 0)

        output_gemini = buf_gemini.getvalue()
        self.assertIn("GEMINI QUOTA", output_gemini)
        self.assertNotIn("CLAUDE QUOTA", output_gemini)
        self.assertIn("80%", output_gemini)

        self.engine.get_account_tokens.side_effect = lambda name: {
            "acc1": {"access_token": "tok1"},
            "acc2": {"access_token": "tok2"},
        }.get(name)
        mock_qc.fetch_quota.side_effect = lambda tok, refresh_token=None, on_refresh=None: {
            "tok1": {
                "gemini": {"quota": 80, "reset_time": 1791201600, "cached": False, "buckets": {}},
                "claude": {"error": "HTTP 401: Unauthorized"},
            },
            "tok2": {
                "gemini": {"quota": 70, "reset_time": 1791201600, "cached": False, "buckets": {}},
                "claude": None,
            },
        }.get(tok, {})

        buf_err = io.StringIO()
        with patch('sys.stdout', buf_err):
            exit_code = main(["quota", "--model", "claude"], engine=self.engine)
            self.assertEqual(exit_code, 0)

        output_err = buf_err.getvalue()
        self.assertIn("CLAUDE QUOTA", output_err)
        self.assertIn("Error / Unreachable", output_err)

    @patch('agy_switch.QuotaClient')
    def test_list_command_model_forwarding(self, mock_qc_class):
        """agy-switch list forwards target_model with --quota and validates --model."""
        mock_qc = mock_qc_class.return_value
        mock_qc.fetch_quota.return_value = {
            "gemini": {"quota": 80, "reset_time": 1791201600, "cached": False, "buckets": {}},
            "claude": {"quota": 65, "reset_time": 1791201600, "cached": False, "buckets": {}},
        }
        mock_qc.load_cache.return_value = {}

        buf_table = io.StringIO()
        with patch('sys.stdout', buf_table):
            exit_code = main(["list", "--quota", "--model", "claude"], engine=self.engine)
            self.assertEqual(exit_code, 0)
        self.assertIn("CLAUDE QUOTA", buf_table.getvalue())
        self.assertIn("65%", buf_table.getvalue())

        buf_listicle = io.StringIO()
        with patch('sys.stdout', buf_listicle):
            exit_code = main(["list", "--quota", "--model", "claude", "--format", "list"], engine=self.engine)
            self.assertEqual(exit_code, 0)
        self.assertIn("— Claude: 65%", buf_listicle.getvalue())

        buf_no_quota = io.StringIO()
        with patch('sys.stdout', buf_no_quota):
            exit_code = main(["list", "--model", "claude"], engine=self.engine)
            self.assertEqual(exit_code, 0)
        self.assertNotIn("CLAUDE QUOTA", buf_no_quota.getvalue())
        self.assertIn("Active", buf_no_quota.getvalue())

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
            "claude": {"quota": 55, "reset_time": 1791201600, "cached": False, "buckets": {}},
        }
        mock_qc.load_cache.return_value = {}

        buf_slug = io.StringIO()
        with patch('sys.stdout', buf_slug):
            exit_code = main(["quota", "--model", "claude-3-5-sonnet"], engine=self.engine)
            self.assertEqual(exit_code, 0)
        self.assertIn("CLAUDE QUOTA", buf_slug.getvalue())
        self.assertIn("55%", buf_slug.getvalue())

        buf_case = io.StringIO()
        with patch('sys.stdout', buf_case):
            exit_code = main(["quota", "--model", "CLAUDE"], engine=self.engine)
            self.assertEqual(exit_code, 0)
        self.assertIn("CLAUDE QUOTA", buf_case.getvalue())

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

    def test_login_headless_interactive_prompt_success(self):
        """[REQ-3-SCN-001] Interactive login displays OAuth URL, reads code from stdin, delegates to engine."""
        self.engine.has_account.return_value = False
        self.engine.accounts_dir = Path("/mock/vault/accounts")
        self.engine.get_oauth_url.return_value = "https://accounts.google.com/o/oauth2/auth?client_id=test"
        self.engine.login_headless.return_value = (True, "Account 'myacc' successfully logged in")

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf), patch("sys.stderr", stderr_buf), \
             patch("builtins.input", return_value="4/1AY0e-valid-code") as mock_input:
            ret = main(["login", "myacc", "--headless"], engine=self.engine)

        self.assertEqual(ret, 0)
        mock_input.assert_called_once_with("Enter authorization code: ")
        self.engine.login_headless.assert_called_once_with(
            alias="myacc",
            auth_code="4/1AY0e-valid-code",
            force=False,
            agy_binary=None,
            timeout=600,
        )
        self.assertIn("https://accounts.google.com/o/oauth2/auth", stdout_buf.getvalue())
        self.assertIn("Account 'myacc' successfully logged in and saved to vault.", stdout_buf.getvalue())

    def test_login_headless_non_interactive_auth_code_flag(self):
        """[REQ-3-SCN-002] Non-interactive login with --auth-code skips stdin prompt."""
        self.engine.has_account.return_value = False
        self.engine.accounts_dir = Path("/mock/vault/accounts")
        self.engine.login_headless.return_value = (True, "Account 'bot-acc' successfully logged in")

        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf), patch("builtins.input") as mock_input:
            ret = main(["login", "bot-acc", "--headless", "--auth-code", "4/1AY0e-auto-code"], engine=self.engine)

        self.assertEqual(ret, 0)
        mock_input.assert_not_called()
        self.engine.login_headless.assert_called_once_with(
            alias="bot-acc",
            auth_code="4/1AY0e-auto-code",
            force=False,
            agy_binary=None,
            timeout=600,
        )
        self.assertIn("Account 'bot-acc' successfully logged in and saved to vault.", stdout_buf.getvalue())

    def test_login_headless_collision_abort_when_declined(self):
        """[REQ-3-EDGE-001] Declining overwrite on existing account aborts safely."""
        self.engine.has_account.return_value = True
        self.engine.login_headless = MagicMock()

        stderr_buf = io.StringIO()
        with patch("sys.stderr", stderr_buf), patch("builtins.input", return_value="n"):
            ret = main(["login", "existing_acc", "--headless"], engine=self.engine)

        self.assertEqual(ret, 1)
        self.engine.login_headless.assert_not_called()
        self.assertIn("Error: Account 'existing_acc' already exists in vault. Overwrite aborted.", stderr_buf.getvalue())

    def test_login_headless_collision_abort_on_eof(self):
        """[REQ-3-EDGE-001] EOF during overwrite confirmation prompt aborts safely."""
        self.engine.has_account.return_value = True
        self.engine.login_headless = MagicMock()

        stderr_buf = io.StringIO()
        with patch("sys.stderr", stderr_buf), patch("builtins.input", side_effect=EOFError):
            ret = main(["login", "existing_acc", "--headless"], engine=self.engine)

        self.assertEqual(ret, 1)
        self.engine.login_headless.assert_not_called()
        self.assertIn("Error: Account 'existing_acc' already exists in vault. Overwrite aborted.", stderr_buf.getvalue())

    def test_login_headless_collision_confirmed_overwrites(self):
        """[REQ-3-EDGE-001] Confirming overwrite passes force=True to login_headless."""
        self.engine.has_account.return_value = True
        self.engine.accounts_dir = Path("/mock/vault/accounts")
        self.engine.login_headless.return_value = (True, "Account 'existing_acc' successfully logged in")

        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf), patch("builtins.input", return_value="y") as mock_input:
            ret = main(
                ["login", "existing_acc", "--headless", "--auth-code", "valid-code"],
                engine=self.engine,
            )

        self.assertEqual(ret, 0)
        mock_input.assert_called_once_with("Account 'existing_acc' already exists. Overwrite? [y/N]: ")
        self.engine.login_headless.assert_called_once_with(
            alias="existing_acc",
            auth_code="valid-code",
            force=True,
            agy_binary=None,
            timeout=600,
        )

    def test_login_headless_force_bypasses_prompt(self):
        """[REQ-3-EDGE-002] Providing --force bypasses overwrite confirmation prompt."""
        self.engine.has_account.return_value = True
        self.engine.accounts_dir = Path("/mock/vault/accounts")
        self.engine.login_headless.return_value = (True, "Account 'existing_acc' successfully logged in")

        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf), patch("builtins.input") as mock_input:
            ret = main(
                ["login", "existing_acc", "--headless", "--force", "--auth-code", "valid-code"],
                engine=self.engine,
            )

        self.assertEqual(ret, 0)
        mock_input.assert_not_called()
        self.engine.login_headless.assert_called_once_with(
            alias="existing_acc",
            auth_code="valid-code",
            force=True,
            agy_binary=None,
            timeout=600,
        )

    def test_login_headless_stdin_closed_eof(self):
        """[REQ-3-EDGE-003] EOF on authorization code prompt prints clean error and exits with 1."""
        self.engine.has_account.return_value = False
        self.engine.login_headless = MagicMock()

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf), patch("sys.stderr", stderr_buf), \
             patch("builtins.input", side_effect=EOFError):
            ret = main(["login", "myacc", "--headless"], engine=self.engine)

        self.assertEqual(ret, 1)
        self.engine.login_headless.assert_not_called()
        self.assertIn("Error: No authorization code provided and stdin closed.", stderr_buf.getvalue())

    def test_login_headless_exchange_failure_stderr(self):
        """[REQ-3-ERR-001] Exchange failure from engine writes error to stderr and exits with 1."""
        self.engine.has_account.return_value = False
        self.engine.login_headless.return_value = (
            False,
            "OAuth token exchange failed: invalid or expired authorization code.",
        )

        stderr_buf = io.StringIO()
        with patch("sys.stderr", stderr_buf):
            ret = main(
                ["login", "bad-acc", "--headless", "--auth-code", "invalid-expired-code"],
                engine=self.engine,
            )

        self.assertEqual(ret, 1)
        self.assertIn(
            "Error: OAuth token exchange failed: invalid or expired authorization code.",
            stderr_buf.getvalue(),
        )

    def test_login_headless_invalid_alias_path_traversal(self):
        """[REQ-3-ERR-003] Invalid alias syntax with path traversal rejects before subprocess execution."""
        from agy_switcher.exceptions import AgySwitchError
        self.engine.validate_account_alias.side_effect = AgySwitchError(
            "Account alias contains invalid path traversal element: '../bad'"
        )

        stderr_buf = io.StringIO()
        with patch("sys.stderr", stderr_buf):
            ret = main(["login", "../bad", "--headless"], engine=self.engine)

        self.assertEqual(ret, 1)
        self.assertIn("Error: Account alias contains invalid path traversal element: '../bad'", stderr_buf.getvalue())

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

    def test_account_exists_and_oauth_helpers(self):
        """Verifies _account_exists delegates directly to engine.has_account and _get_default_oauth_url uses centralized builder."""
        from agy_switcher.cli import _account_exists, _get_default_oauth_url
        from agy_switcher.telemetry import DEFAULT_CLIENT_ID

        mock_engine = MagicMock(spec=AgySwitchEngine)
        mock_engine.has_account.return_value = True
        self.assertTrue(_account_exists(mock_engine, "active_user"))
        mock_engine.has_account.assert_called_once_with("active_user")

        mock_engine.has_account.return_value = False
        self.assertFalse(_account_exists(mock_engine, "nonexistent"))

        url = _get_default_oauth_url()
        self.assertIn(DEFAULT_CLIENT_ID, url)
        self.assertIn("accounts.google.com/o/oauth2/auth", url)


if __name__ == '__main__':
    unittest.main()
