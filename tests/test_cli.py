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

from agy_switch import main, AgySwitchEngine, QuotaClient, RotationEngine

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
        
        # Verify rotation
        self.engine.switch_tokens.assert_called_once_with("acc2", restart_server=True, force=False)

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
    def test_quota_subcommand_json_sanitizes_claude(self, mock_qc_class):
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
            self.assertEqual(list(acc["quota"].keys()), ["gemini"])

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


if __name__ == '__main__':
    unittest.main()
