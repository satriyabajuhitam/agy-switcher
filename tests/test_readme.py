import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
README_PATH = REPO_ROOT / "README.md"


class TestReadmeDocumentation(unittest.TestCase):

    def setUp(self):
        self.assertTrue(README_PATH.exists(), "README.md must exist at repo root")
        self.content = README_PATH.read_text(encoding="utf-8")

    def test_badges_present(self):
        """README must contain standard badges: MIT License, Python >= 3.10, and Zero Dependencies."""
        self.assertIn(
            "License-MIT",
            self.content,
            "README.md must include MIT License badge",
        )
        self.assertIn(
            "3.10",
            self.content,
            "README.md must include Python >= 3.10 badge",
        )
        self.assertIn(
            "dependencies",
            self.content.lower(),
            "README.md must include Zero Dependencies badge",
        )

    def test_prohibitions_no_pipx_or_packaging_install(self):
        """ADR 0003 prohibits mentioning pipx or pip-based packaging installation."""
        self.assertNotIn(
            "pipx",
            self.content.lower(),
            "README.md must not mention pipx as an installation method (ADR 0003)",
        )
        self.assertNotIn(
            "pip install",
            self.content.lower(),
            "README.md must not mention pip install as an installation method",
        )

    def test_installation_instructions(self):
        """README must clearly explain unified install.sh with --global and --local."""
        self.assertIn(
            "install.sh --local",
            self.content,
            "README.md must document 'install.sh --local'",
        )
        self.assertIn(
            "install.sh --global",
            self.content,
            "README.md must document 'install.sh --global'",
        )
        # Should NOT use obsolete booster/agy-switch/ path
        self.assertNotIn(
            "booster/agy-switch/install.sh",
            self.content,
            "README.md should reference root install.sh, not booster/agy-switch/install.sh",
        )

    def test_uninstallation_instructions(self):
        """README must document clean uninstallation via install.sh --uninstall."""
        self.assertIn(
            "install.sh --uninstall",
            self.content,
            "README.md must document 'install.sh --uninstall'",
        )

    def test_zero_dependency_and_privacy_guarantees(self):
        """README must explicitly highlight zero-dependency, 100% offline vault, and privacy guarantees."""
        content_lower = self.content.lower()
        for keyword in ("zero", "offline", "telemetry", "privacy"):
            self.assertIn(
                keyword,
                content_lower,
                f"README.md must mention '{keyword}' in privacy/dependency guarantees",
            )


if __name__ == "__main__":
    unittest.main()
