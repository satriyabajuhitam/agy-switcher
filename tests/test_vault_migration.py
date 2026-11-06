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


if __name__ == "__main__":
    unittest.main()
