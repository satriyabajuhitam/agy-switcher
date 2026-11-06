"""Unit and behavioral tests for cryptographic vault migration utilities.

Validates:
- [REQ-4-SCN-001]: Gzipped archive generation with SHA-256 manifest.
- [REQ-4-SCN-002]: Strict POSIX 0600 bundle permissions and member mode enforcement.
- Handling existing bundle paths with force=False and force=True.
- Handling empty vaults.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tarfile
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from agy_switcher.exceptions import AgySwitchError
from agy_switcher.vault import AgySwitchEngine


def make_dummy_token(email: str = "test@example.com") -> dict[str, object]:
    """Helper creating a minimal valid-looking token payload."""
    import base64

    payload = json.dumps({"email": email, "sub": "12345", "aud": "test_aud"}).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(payload).decode("utf-8").rstrip("=")
    return {
        "token": {
            "access_token": f"ya29.sample_token_for_{email}",
            "token_type": "Bearer",
            "refresh_token": f"1//sample_refresh_for_{email}",
            "expiry": "2026-10-30T12:00:00Z",
        },
        "auth_method": "oauth",
        "id_token": f"header.{payload_b64}.signature",
    }


class TestVaultMigration(unittest.TestCase):
    """Test suite for vault bundle export and import cryptographic migration."""

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

    def _seed_account(self, name: str, email: str) -> Path:
        acc_dir = self.vault_dir / "accounts" / name
        acc_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        token_path = acc_dir / "token.json"
        data = make_dummy_token(email)
        token_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.chmod(token_path, 0o600)
        return token_path

    def test_export_creates_bundle_with_manifest(self) -> None:
        """[REQ-4-SCN-001] vault export creates gzipped archive with SHA-256 manifest."""
        token_path1 = self._seed_account("acc1", "acc1@example.com")
        token_path2 = self._seed_account("acc2", "acc2@example.com")
        self.engine.set_active_name("acc1")

        config_path = self.vault_dir / "config.json"
        config_path.write_text(json.dumps({"auto_heal": True}), encoding="utf-8")
        os.chmod(config_path, 0o600)

        bundle_path = self.base_path / "vault-backup.tar.gz"
        success, msg = self.engine.export_vault_bundle(bundle_path)

        self.assertTrue(success, f"export_vault_bundle failed: {msg}")
        self.assertTrue(bundle_path.is_file(), "Bundle file was not created")
        self.assertIn("2 accounts", msg)

        # Inspect tarball contents
        with tarfile.open(bundle_path, "r:gz") as tar:
            member_names = tar.getnames()
            self.assertIn("manifest.json", member_names)
            self.assertIn("active", member_names)
            self.assertIn("config.json", member_names)
            self.assertIn("accounts/acc1/token.json", member_names)
            self.assertIn("accounts/acc2/token.json", member_names)

            # Read and verify manifest
            manifest_file = tar.extractfile("manifest.json")
            self.assertIsNotNone(manifest_file)
            assert manifest_file is not None
            manifest = json.loads(manifest_file.read().decode("utf-8"))

            self.assertEqual(manifest.get("version"), 1)
            self.assertEqual(manifest.get("active_account"), "acc1")
            self.assertIn("created_at", manifest)
            # Verify created_at is valid ISO timestamp
            created_at = datetime.fromisoformat(manifest["created_at"])
            self.assertIsNotNone(created_at)

            files = manifest.get("files", {})
            self.assertIn("accounts/acc1/token.json", files)
            self.assertIn("accounts/acc2/token.json", files)
            self.assertIn("active", files)
            self.assertIn("config.json", files)

            # Verify checksums
            expected_acc1_sha = hashlib.sha256(token_path1.read_bytes()).hexdigest()
            expected_acc2_sha = hashlib.sha256(token_path2.read_bytes()).hexdigest()
            expected_active_sha = hashlib.sha256((self.vault_dir / "active").read_bytes()).hexdigest()
            expected_config_sha = hashlib.sha256(config_path.read_bytes()).hexdigest()

            self.assertEqual(files["accounts/acc1/token.json"], expected_acc1_sha)
            self.assertEqual(files["accounts/acc2/token.json"], expected_acc2_sha)
            self.assertEqual(files["active"], expected_active_sha)
            self.assertEqual(files["config.json"], expected_config_sha)

    def test_export_enforces_posix_permissions(self) -> None:
        """[REQ-4-SCN-002] vault export enforces POSIX 0600 on bundle and members."""
        self._seed_account("acc1", "acc1@example.com")
        self.engine.set_active_name("acc1")

        bundle_path = self.base_path / "secure-vault.tar.gz"
        success, msg = self.engine.export_vault_bundle(bundle_path)
        self.assertTrue(success, f"export_vault_bundle failed: {msg}")

        # Check bundle file permissions on disk (must be 0600)
        bundle_mode = stat.S_IMODE(bundle_path.stat().st_mode)
        self.assertEqual(
            bundle_mode,
            0o600,
            f"Expected bundle permissions 0600, got {oct(bundle_mode)}",
        )

        # Check inside archive member permissions
        with tarfile.open(bundle_path, "r:gz") as tar:
            for member in tar.getmembers():
                member_mode = stat.S_IMODE(member.mode)
                if member.isdir():
                    self.assertEqual(
                        member_mode,
                        0o700,
                        f"Directory member {member.name} must have 0700 mode, got {oct(member_mode)}",
                    )
                else:
                    self.assertEqual(
                        member_mode,
                        0o600,
                        f"File member {member.name} must have 0600 mode, got {oct(member_mode)}",
                    )

    def test_export_existing_bundle_without_force(self) -> None:
        """Export fails safely when target bundle already exists and force=False."""
        self._seed_account("acc1", "acc1@example.com")
        bundle_path = self.base_path / "existing.tar.gz"
        bundle_path.write_text("existing content", encoding="utf-8")

        success, msg = self.engine.export_vault_bundle(bundle_path, force=False)
        self.assertFalse(success)
        self.assertEqual(
            msg,
            f"Bundle already exists at {bundle_path}. Use --force to overwrite.",
        )
        self.assertEqual(bundle_path.read_text(encoding="utf-8"), "existing content")

    def test_export_existing_bundle_with_force(self) -> None:
        """Export overwrites safely when target bundle already exists and force=True."""
        self._seed_account("acc1", "acc1@example.com")
        bundle_path = self.base_path / "overwrite.tar.gz"
        bundle_path.write_text("existing content", encoding="utf-8")

        success, msg = self.engine.export_vault_bundle(bundle_path, force=True)
        self.assertTrue(success, f"export failed with force=True: {msg}")
        self.assertTrue(tarfile.is_tarfile(bundle_path))

    def test_export_empty_vault(self) -> None:
        """Export handles empty vault correctly, creating manifest with 0 accounts."""
        bundle_path = self.base_path / "empty-vault.tar.gz"
        success, msg = self.engine.export_vault_bundle(bundle_path)

        self.assertTrue(success, f"export empty vault failed: {msg}")
        self.assertIn("0 accounts", msg)

        with tarfile.open(bundle_path, "r:gz") as tar:
            self.assertIn("manifest.json", tar.getnames())
            manifest_file = tar.extractfile("manifest.json")
            self.assertIsNotNone(manifest_file)
            assert manifest_file is not None
            manifest = json.loads(manifest_file.read().decode("utf-8"))
            self.assertEqual(manifest["version"], 1)
            self.assertIsNone(manifest["active_account"])
            self.assertEqual(manifest["files"], {})

    def test_import_valid_bundle_restores_accounts_and_permissions(self) -> None:
        """[REQ-4-SCN-003] Valid vault bundle archive extracts accounts and sets 0600/0700 permissions."""
        with tempfile.TemporaryDirectory() as src_dir:
            src_path = Path(src_dir)
            src_engine = AgySwitchEngine(
                vault_dir=src_path / ".antigravity-vault",
                gemini_dir=src_path / ".gemini",
                legacy_root=src_path / ".agy-cli-manager",
            )
            # Seed source accounts
            acc1_dir = src_engine.vault_dir / "accounts" / "acc1"
            acc1_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
            token1_data = make_dummy_token("acc1@example.com")
            (acc1_dir / "token.json").write_text(json.dumps(token1_data, indent=2), encoding="utf-8")
            os.chmod(acc1_dir / "token.json", 0o600)

            acc2_dir = src_engine.vault_dir / "accounts" / "acc2"
            acc2_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
            token2_data = make_dummy_token("acc2@example.com")
            (acc2_dir / "token.json").write_text(json.dumps(token2_data, indent=2), encoding="utf-8")
            os.chmod(acc2_dir / "token.json", 0o600)

            src_engine.set_active_name("acc1")
            src_engine.save_config({"auto_switch": True})

            bundle_path = self.base_path / "valid-bundle.tar.gz"
            src_engine.export_vault_bundle(bundle_path)

            # Import into self.engine (starts empty)
            success, msg = self.engine.import_vault_bundle(bundle_path)
            self.assertTrue(success, f"Import failed: {msg}")
            self.assertIn("2 accounts", msg)

            # Verify active account pointer
            self.assertEqual(self.engine.get_active_name(), "acc1")

            # Verify accounts exist on disk
            restored_acc1 = self.vault_dir / "accounts" / "acc1"
            restored_acc2 = self.vault_dir / "accounts" / "acc2"
            self.assertTrue(restored_acc1.is_dir())
            self.assertTrue(restored_acc2.is_dir())

            # Verify permissions (0700 for directories, 0600 for files)
            self.assertEqual(stat.S_IMODE(restored_acc1.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(restored_acc2.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((restored_acc1 / "token.json").stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((restored_acc2 / "token.json").stat().st_mode), 0o600)

            active_file = self.vault_dir / "active"
            self.assertTrue(active_file.is_file())
            self.assertEqual(stat.S_IMODE(active_file.stat().st_mode), 0o600)

            config_file = self.vault_dir / "config.json"
            self.assertTrue(config_file.is_file())
            self.assertEqual(stat.S_IMODE(config_file.stat().st_mode), 0o600)
            self.assertEqual(self.engine.load_config().get("auto_switch"), True)

    def test_import_path_traversal_zip_slip_rejected(self) -> None:
        """[REQ-4-ERR-003] Zip Slip path traversal member aborts extraction with security violation."""
        # Test relative traversal member: ../evil.txt
        for malicious_name in ["../evil.txt", "/tmp/pwn", "accounts/../../outside.txt"]:
            with self.subTest(malicious_name=malicious_name):
                bundle_path = self.base_path / f"malicious_{abs(hash(malicious_name))}.tar.gz"
                dummy_payload = b"payload"
                digest = hashlib.sha256(dummy_payload).hexdigest()
                manifest = {
                    "version": 1,
                    "created_at": datetime.now().isoformat(),
                    "active_account": None,
                    "files": {malicious_name: digest},
                }
                manifest_bytes = json.dumps(manifest).encode("utf-8")

                with tarfile.open(bundle_path, "w:gz") as tar:
                    m_info = tarfile.TarInfo(name="manifest.json")
                    m_info.type = tarfile.REGTYPE
                    m_info.size = len(manifest_bytes)
                    tar.addfile(m_info, io.BytesIO(manifest_bytes))

                    bad_info = tarfile.TarInfo(name=malicious_name)
                    bad_info.type = tarfile.REGTYPE
                    bad_info.size = len(dummy_payload)
                    tar.addfile(bad_info, io.BytesIO(dummy_payload))

                with self.assertRaises(AgySwitchError) as ctx:
                    self.engine.import_vault_bundle(bundle_path)

                self.assertIn("Security violation: archive member contains illegal path traversal", str(ctx.exception))
                self.assertFalse((self.base_path / "evil.txt").exists())
                self.assertFalse(Path("/tmp/pwn").exists())

    def test_import_symlink_and_hardlink_attacks_rejected(self) -> None:
        """[REQ-4-ERR-007] Symlink or hardlink archive members raise security violation error."""
        # Test symlink
        bundle_sym = self.base_path / "symlink_attack.tar.gz"
        manifest_bytes = json.dumps({"version": 1, "created_at": "now", "files": {}}).encode("utf-8")
        with tarfile.open(bundle_sym, "w:gz") as tar:
            m_info = tarfile.TarInfo(name="manifest.json")
            m_info.type = tarfile.REGTYPE
            m_info.size = len(manifest_bytes)
            tar.addfile(m_info, io.BytesIO(manifest_bytes))

            sym_info = tarfile.TarInfo(name="accounts/evil_link")
            sym_info.type = tarfile.SYMTYPE
            sym_info.linkname = "/etc/passwd"
            tar.addfile(sym_info)

        with self.assertRaises(AgySwitchError) as ctx:
            self.engine.import_vault_bundle(bundle_sym)
        self.assertIn("Security violation: archive member is a symbolic or hard link", str(ctx.exception))

        # Test hardlink
        bundle_lnk = self.base_path / "hardlink_attack.tar.gz"
        with tarfile.open(bundle_lnk, "w:gz") as tar:
            m_info = tarfile.TarInfo(name="manifest.json")
            m_info.type = tarfile.REGTYPE
            m_info.size = len(manifest_bytes)
            tar.addfile(m_info, io.BytesIO(manifest_bytes))

            lnk_info = tarfile.TarInfo(name="accounts/evil_hardlink")
            lnk_info.type = tarfile.LNKTYPE
            lnk_info.linkname = "manifest.json"
            tar.addfile(lnk_info)

        with self.assertRaises(AgySwitchError) as ctx:
            self.engine.import_vault_bundle(bundle_lnk)
        self.assertIn("Security violation: archive member is a symbolic or hard link", str(ctx.exception))

    def test_import_missing_or_corrupt_manifest_rejected(self) -> None:
        """[REQ-4-ERR-002] Missing or malformed manifest.json aborts with exact error."""
        # Missing manifest
        no_manifest_bundle = self.base_path / "no_manifest.tar.gz"
        with tarfile.open(no_manifest_bundle, "w:gz") as tar:
            info = tarfile.TarInfo(name="accounts/acc1/token.json")
            info.type = tarfile.REGTYPE
            info.size = 4
            tar.addfile(info, io.BytesIO(b"data"))

        with self.assertRaises(AgySwitchError) as ctx:
            self.engine.import_vault_bundle(no_manifest_bundle)
        self.assertEqual(str(ctx.exception), "Invalid vault bundle: missing or malformed manifest.json.")

        # Corrupt JSON in manifest
        bad_json_bundle = self.base_path / "bad_json.tar.gz"
        with tarfile.open(bad_json_bundle, "w:gz") as tar:
            info = tarfile.TarInfo(name="manifest.json")
            info.type = tarfile.REGTYPE
            info.size = 11
            tar.addfile(info, io.BytesIO(b"{not: valid"))

        with self.assertRaises(AgySwitchError) as ctx:
            self.engine.import_vault_bundle(bad_json_bundle)
        self.assertEqual(str(ctx.exception), "Invalid vault bundle: missing or malformed manifest.json.")

        # Manifest lacking 'files' dict
        missing_files_bundle = self.base_path / "missing_files_key.tar.gz"
        with tarfile.open(missing_files_bundle, "w:gz") as tar:
            m_data = json.dumps({"version": 1}).encode("utf-8")
            info = tarfile.TarInfo(name="manifest.json")
            info.type = tarfile.REGTYPE
            info.size = len(m_data)
            tar.addfile(info, io.BytesIO(m_data))

        with self.assertRaises(AgySwitchError) as ctx:
            self.engine.import_vault_bundle(missing_files_bundle)
        self.assertEqual(str(ctx.exception), "Invalid vault bundle: missing or malformed manifest.json.")

    def test_import_checksum_mismatch_aborts_and_writes_nothing(self) -> None:
        """[REQ-4-ERR-001] Checksum mismatch aborts extraction and writes zero files (atomic all-or-nothing)."""
        bundle_path = self.base_path / "tampered.tar.gz"
        token_payload = b'{"token": "clean"}'
        tampered_payload = b'{"token": "tampered"}'
        expected_sha = hashlib.sha256(token_payload).hexdigest()

        manifest = {
            "version": 1,
            "created_at": datetime.now().isoformat(),
            "active_account": "acc1",
            "files": {"accounts/acc1/token.json": expected_sha},
        }
        manifest_bytes = json.dumps(manifest).encode("utf-8")

        with tarfile.open(bundle_path, "w:gz") as tar:
            m_info = tarfile.TarInfo(name="manifest.json")
            m_info.type = tarfile.REGTYPE
            m_info.size = len(manifest_bytes)
            tar.addfile(m_info, io.BytesIO(manifest_bytes))

            # Store tampered payload inside archive
            t_info = tarfile.TarInfo(name="accounts/acc1/token.json")
            t_info.type = tarfile.REGTYPE
            t_info.size = len(tampered_payload)
            tar.addfile(t_info, io.BytesIO(tampered_payload))

        with self.assertRaises(AgySwitchError) as ctx:
            self.engine.import_vault_bundle(bundle_path)

        self.assertIn("Vault bundle integrity verification failed: checksum mismatch for 'accounts/acc1/token.json'", str(ctx.exception))
        # Ensure zero accounts written
        accounts_found = [p for p in (self.vault_dir / "accounts").iterdir() if p.is_dir()]
        self.assertEqual(len(accounts_found), 0, "No accounts must be written on checksum mismatch")

    def test_import_non_tar_or_truncated_archive_rejected(self) -> None:
        """[REQ-4-ERR-004] 0-byte, plain text, or truncated gzip archives raise exact error."""
        # 0-byte file
        empty_bundle = self.base_path / "empty.tar.gz"
        empty_bundle.write_bytes(b"")
        with self.assertRaises(AgySwitchError) as ctx:
            self.engine.import_vault_bundle(empty_bundle)
        self.assertEqual(str(ctx.exception), "Invalid vault bundle: file is not a valid gzip-compressed tar archive.")

        # Plain text file
        text_bundle = self.base_path / "plain.tar.gz"
        text_bundle.write_text("plain text", encoding="utf-8")
        with self.assertRaises(AgySwitchError) as ctx:
            self.engine.import_vault_bundle(text_bundle)
        self.assertEqual(str(ctx.exception), "Invalid vault bundle: file is not a valid gzip-compressed tar archive.")

        # Truncated gzip header
        trunc_bundle = self.base_path / "trunc.tar.gz"
        trunc_bundle.write_bytes(b"\x1f\x8b\x08\x00truncated")
        with self.assertRaises(AgySwitchError) as ctx:
            self.engine.import_vault_bundle(trunc_bundle)
        self.assertEqual(str(ctx.exception), "Invalid vault bundle: file is not a valid gzip-compressed tar archive.")

    def test_import_merge_and_force_flags_and_active_account(self) -> None:
        """[REQ-4-EDGE-001] Restoring active account pointer and existing vault merge."""
        # Seed local vault with "local1"
        self._seed_account("local1", "local1@example.com")
        self.engine.set_active_name("local1")
        self.assertEqual(self.engine.get_active_name(), "local1")

        # Create bundle containing "cloud1" and "cloud2" with active "cloud1"
        with tempfile.TemporaryDirectory() as cloud_dir:
            cloud_path = Path(cloud_dir)
            cloud_engine = AgySwitchEngine(
                vault_dir=cloud_path / ".antigravity-vault",
                gemini_dir=cloud_path / ".gemini",
                legacy_root=cloud_path / ".agy-cli-manager",
            )
            c1_dir = cloud_engine.vault_dir / "accounts" / "cloud1"
            c1_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
            (c1_dir / "token.json").write_text(json.dumps(make_dummy_token("cloud1@example.com")), encoding="utf-8")
            os.chmod(c1_dir / "token.json", 0o600)

            c2_dir = cloud_engine.vault_dir / "accounts" / "cloud2"
            c2_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
            (c2_dir / "token.json").write_text(json.dumps(make_dummy_token("cloud2@example.com")), encoding="utf-8")
            os.chmod(c2_dir / "token.json", 0o600)

            cloud_engine.set_active_name("cloud1")

            bundle_path = self.base_path / "cloud_bundle.tar.gz"
            cloud_engine.export_vault_bundle(bundle_path)

            # 1. Import with default (merge=True, force=False): local1 preserved, cloud1 and cloud2 added, active remains local1
            success, msg = self.engine.import_vault_bundle(bundle_path, merge=True, force=False)
            self.assertTrue(success)
            self.assertTrue((self.vault_dir / "accounts" / "local1").is_dir())
            self.assertTrue((self.vault_dir / "accounts" / "cloud1").is_dir())
            self.assertTrue((self.vault_dir / "accounts" / "cloud2").is_dir())
            self.assertEqual(self.engine.get_active_name(), "local1", "Active account must not be overwritten when force=False")

            # 2. Import with force=True: active account is updated to cloud1
            success, msg = self.engine.import_vault_bundle(bundle_path, merge=True, force=True)
            self.assertTrue(success)
            self.assertEqual(self.engine.get_active_name(), "cloud1", "Active account must update to bundle active when force=True")

            # 3. Import with merge=False: clears existing accounts not in bundle (local1 removed)
            success, msg = self.engine.import_vault_bundle(bundle_path, merge=False)
            self.assertTrue(success)
            self.assertFalse((self.vault_dir / "accounts" / "local1").exists(), "Existing account must be removed when merge=False")
            self.assertTrue((self.vault_dir / "accounts" / "cloud1").is_dir())
            self.assertTrue((self.vault_dir / "accounts" / "cloud2").is_dir())

    def test_import_permission_error_rollback(self) -> None:
        """[REQ-4-ERR-006] Write permission failure raises AgySwitchError and leaves existing accounts unharmed."""
        self._seed_account("existing1", "existing1@example.com")
        self.engine.set_active_name("existing1")

        bundle_path = self.base_path / "new_account.tar.gz"
        token_payload = json.dumps(make_dummy_token("new@example.com")).encode("utf-8")
        manifest = {
            "version": 1,
            "created_at": datetime.now().isoformat(),
            "active_account": "new1",
            "files": {"accounts/new1/token.json": hashlib.sha256(token_payload).hexdigest()},
        }
        manifest_bytes = json.dumps(manifest).encode("utf-8")
        with tarfile.open(bundle_path, "w:gz") as tar:
            m_info = tarfile.TarInfo(name="manifest.json")
            m_info.type = tarfile.REGTYPE
            m_info.size = len(manifest_bytes)
            tar.addfile(m_info, io.BytesIO(manifest_bytes))

            t_info = tarfile.TarInfo(name="accounts/new1/token.json")
            t_info.type = tarfile.REGTYPE
            t_info.size = len(token_payload)
            tar.addfile(t_info, io.BytesIO(token_payload))

        # Make accounts directory read-only (0500)
        os.chmod(self.engine.accounts_dir, 0o500)
        try:
            with self.assertRaises(AgySwitchError):
                self.engine.import_vault_bundle(bundle_path)
        finally:
            os.chmod(self.engine.accounts_dir, 0o700)

        # Verify existing account is completely unharmed
        self.assertTrue((self.vault_dir / "accounts" / "existing1" / "token.json").is_file())
        self.assertEqual(self.engine.get_active_name(), "existing1")
        self.assertFalse((self.vault_dir / "accounts" / "new1").exists())


if __name__ == "__main__":
    unittest.main()
