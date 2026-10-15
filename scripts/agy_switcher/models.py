"""Data models and dataclasses for agy-switch.

Layer 0 Foundation module: Zero internal dependencies.
Pure Python 3.10+ standard library.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

_LAYER_TO_CATEGORY: dict[int, str] = {
    1: "permissions",
    2: "token_integrity",
    3: "dual_sync",
    4: "hooks",
    5: "process",
}

_CATEGORY_TO_LAYER: dict[str, int] = {
    "permissions": 1,
    "token_integrity": 2,
    "dual_sync": 3,
    "hooks": 4,
    "process": 5,
}


@dataclass
class DiagnosticItem:
    """Represents a single diagnostic inspection finding across the 5 doctor layers."""

    category: str = ""                  # "permissions", "token_integrity", "dual_sync", "hooks", "process"
    name: str = ""                      # Human-readable check title
    status: str = "PASS"                # "PASS", "WARN", "FAIL"
    message: str = ""                   # Detailed finding
    can_fix: bool = False               # True if --fix can automatically resolve
    fixed: bool = False                 # True if repaired during this execution
    fix_action: str | None = None       # Human-readable description of repair performed
    layer: int = 0                      # Layer index (1-5)
    title: str = ""                     # Canonical check title (alias for name)

    def __post_init__(self) -> None:
        # Reconcile name and title
        if self.title and not self.name:
            self.name = self.title
        elif self.name and not self.title:
            self.title = self.name

        # Reconcile category and layer
        if self.layer and not self.category:
            self.category = _LAYER_TO_CATEGORY.get(self.layer, "")
        elif self.category and not self.layer:
            self.layer = _CATEGORY_TO_LAYER.get(self.category, 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "name": self.name,
            "status": self.status,
            "message": self.message,
            "can_fix": self.can_fix,
            "fixed": self.fixed,
            "fix_action": self.fix_action,
            "layer": self.layer,
            "title": self.title,
        }


@dataclass
class DiagnosticReport:
    """Represents the complete aggregation of diagnostic inspection items."""

    items: list[DiagnosticItem]
    overall_status: str | None = None   # "PASS", "WARN", "FAIL"
    exit_code: int | None = None        # 0 (PASS), 1 (WARN), 2 (FAIL)

    def __post_init__(self) -> None:
        if self.overall_status is None or self.exit_code is None:
            if any(item.status == "FAIL" for item in self.items):
                calc_status = "FAIL"
                calc_code = 2
            elif any(item.status == "WARN" for item in self.items):
                calc_status = "WARN"
                calc_code = 1
            else:
                calc_status = "PASS"
                calc_code = 0

            if self.overall_status is None:
                self.overall_status = calc_status
            if self.exit_code is None:
                self.exit_code = calc_code

    def to_dict(self) -> dict[str, Any]:
        total = len(self.items)
        passed = sum(1 for item in self.items if item.status == "PASS")
        warned = sum(1 for item in self.items if item.status == "WARN")
        failed = sum(1 for item in self.items if item.status == "FAIL")
        return {
            "overall_status": self.overall_status,
            "exit_code": self.exit_code,
            "summary": {
                "total": total,
                "passed": passed,
                "warned": warned,
                "failed": failed,
            },
            "checks": [item.to_dict() for item in self.items],
        }


@dataclass
class QuotaStatus:
    """Encapsulates account quota fraction and reset calculation."""

    fraction: float
    reset_time: datetime

    def get_reset_delta_minutes(self, current_time: datetime) -> float:
        """Calculates reset delta in minutes relative to current_time."""
        return (self.reset_time - current_time).total_seconds() / 60.0

    def is_exhausted(self) -> bool:
        """Returns True if the quota is exhausted (<= 0% remaining)."""
        return self.fraction <= 0.0

    def has_available_quota(self) -> bool:
        """Returns True if there is positive quota remaining (> 0%)."""
        return self.fraction > 0.0

    def remaining_percentage(self) -> int:
        """Returns remaining quota as an integer percentage (0-100)."""
        return int(self.fraction * 100)


@dataclass
class EvaluationCandidate:
    """Candidate account evaluation record for intelligent rotation."""

    account_id: str
    status: QuotaStatus
    reset_delta: float


__all__ = [
    "DiagnosticItem",
    "DiagnosticReport",
    "QuotaStatus",
    "EvaluationCandidate",
]
