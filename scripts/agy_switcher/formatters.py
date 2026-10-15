"""Terminal output formatters and status reporting for agy-switch.

Layer 1 Core Services module: Depends only on Layer 0 (models) and standard library.
Pure Python 3.10+ standard library. Zero external dependencies.
"""

from __future__ import annotations

from datetime import datetime, timezone
import math
import re
import time
from typing import Any

from agy_switcher.models import DiagnosticReport


def _parse_timestamp_defensive(val: Any) -> float:
    """Safely extracts a positive float UNIX epoch timestamp in UTC or returns float('inf')."""
    if val is None or isinstance(val, bool):
        return float("inf")
    if isinstance(val, (int, float)):
        return float(val) if math.isfinite(val) and val > 0 else float("inf")
    if isinstance(val, str):
        cleaned = val.strip()
        try:
            num = float(cleaned)
            if math.isfinite(num) and num > 0:
                return num
            return float("inf")
        except ValueError:
            pass
        try:
            iso_str = cleaned.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            ts = dt.timestamp()
            return ts if math.isfinite(ts) and ts > 0 else float("inf")
        except Exception:
            pass
    return float("inf")


def _parse_quota_defensive(val: Any) -> int | float:
    """Safely extracts a non-negative quota percentage or returns -1."""
    if val is None or isinstance(val, bool):
        return -1
    if isinstance(val, (int, float)):
        return val if math.isfinite(val) and val >= 0 else -1
    if isinstance(val, str):
        try:
            num = float(val.strip().rstrip("%"))
            if math.isfinite(num) and num >= 0:
                return num
        except ValueError:
            pass
    return -1


def format_countdown(reset_timestamp: int | float | str | None, current_timestamp: float | None = None) -> str:
    """Converts a UNIX epoch reset timestamp into a relative human-readable countdown string.

    Rules:
    - If reset_timestamp is None, non-numeric/unparseable, or <= 0: returns "-"
    - If delta <= 0: returns "expired"
    - If delta < 3600: returns f"{max(1, delta // 60)}m" (e.g. "45m", "1m")
    - If 3600 <= delta < 86400: returns f"{h}h {m}m" if m > 0 else f"{h}h" (e.g. "2h 15m", "2h")
    - If delta >= 86400: returns f"{d}d {h}h" if h > 0 else f"{d}d" (e.g. "4d 2h", "3d")
    """
    ts = _parse_timestamp_defensive(reset_timestamp)
    if math.isinf(ts) or ts <= 0:
        return "-"

    curr = current_timestamp if current_timestamp is not None else time.time()
    if isinstance(curr, bool) or not isinstance(curr, (int, float)) or not math.isfinite(curr):
        curr = time.time()

    delta = int(ts - curr)

    if delta <= 0:
        return "expired"

    if delta < 3600:
        minutes = max(1, delta // 60)
        return f"{minutes}m"

    if delta < 86400:
        h = delta // 3600
        m = (delta % 3600) // 60
        return f"{h}h {m}m" if m > 0 else f"{h}h"

    d = delta // 86400
    h = (delta % 86400) // 3600
    return f"{d}d {h}h" if h > 0 else f"{d}d"


def _format_bucket_countdown(reset_ts: Any, current_timestamp: float | None = None) -> str:
    """Helper to format countdown badge for bucket summaries without broken strings."""
    cd = format_countdown(reset_ts, current_timestamp=current_timestamp)
    if cd == "-":
        return ""
    if cd == "expired":
        return " (expired)"
    return f" (in {cd})"


def _format_gemini_quota_summary(acc: dict[str, Any], current_timestamp: float | None = None) -> str:
    """Extracts and formats Gemini quota summary for display across table and listicle views."""
    if not isinstance(acc, dict):
        return "Error / Unreachable"

    q_root = acc.get("quota") if isinstance(acc.get("quota"), dict) else {}
    gemini_data = q_root.get("gemini") if isinstance(q_root.get("gemini"), dict) else None

    if q_root.get("error") or gemini_data is None or (isinstance(gemini_data, dict) and gemini_data.get("error")):
        return "Error / Unreachable"

    if not isinstance(gemini_data, dict):
        return "Error / Unreachable"

    raw_cached = gemini_data.get("cached")
    is_cached = (
        raw_cached.strip().lower() in ("true", "1")
        if isinstance(raw_cached, str)
        else bool(raw_cached)
    )

    raw_quota = gemini_data.get("quota")
    parsed_q = _parse_quota_defensive(raw_quota)
    quota = int(round(parsed_q)) if parsed_q >= 0 else 0

    if is_cached:
        return f"{quota}% (Cached)"

    buckets = gemini_data.get("buckets") if isinstance(gemini_data.get("buckets"), dict) else {}
    b_5h = buckets.get("5h") if isinstance(buckets.get("5h"), dict) else {}
    b_wk = buckets.get("weekly") if isinstance(buckets.get("weekly"), dict) else (buckets.get("wk") if isinstance(buckets.get("wk"), dict) else {})

    def _render_bucket(tag: str, b_data: dict[str, Any]) -> str:
        raw_b = b_data.get("quota")
        parsed_b = _parse_quota_defensive(raw_b)
        q_b = int(round(parsed_b)) if parsed_b >= 0 else quota
        ts_b = b_data.get("reset_time")
        cd_b = _format_bucket_countdown(ts_b, current_timestamp=current_timestamp)
        return f"{tag}: {q_b}%{cd_b}"

    parts: list[str] = []
    if b_5h:
        parts.append(_render_bucket("5h", b_5h))
    if b_wk:
        parts.append(_render_bucket("wk", b_wk))

    if parts:
        return f"{quota}% [{' | '.join(parts)}]"
    return f"{quota}%"


def format_accounts_listicle(
    result: dict[str, Any] | None,
    show_quota: bool = True,
    current_timestamp: float | None = None,
) -> str:
    """Formats accounts list as a compact, single-line bulleted listicle for LLMs and narrow terminals."""
    if not isinstance(result, dict):
        return "No accounts registered in vault (~/.antigravity-vault/accounts)."

    accounts = result.get("accounts", [])
    if not isinstance(accounts, list) or not accounts:
        return "No accounts registered in vault (~/.antigravity-vault/accounts)."

    lines: list[str] = []
    for acc in accounts:
        if not isinstance(acc, dict):
            continue
        marker = "• [*]" if acc.get("is_active") else "• [ ]"
        email_str = str(acc.get("email") or "").strip() or "-"
        name = str(acc.get("name") or "").strip()
        prefix = f"{marker} {name} ({email_str})"

        if show_quota:
            quota_summary = _format_gemini_quota_summary(acc, current_timestamp=current_timestamp)
            lines.append(f"{prefix} — Gemini: {quota_summary}")
        else:
            status = "Active" if acc.get("is_active") else "Standby"
            if not acc.get("valid", True):
                status += " (Invalid)"
            lines.append(f"{prefix} [{status}]")

    active_acc = result.get("active")
    if active_acc:
        lines.append(f"Active Account: {active_acc}")
    else:
        lines.append("No account currently active. Run: agy-switch switch <account>")

    return "\n".join(lines)


def format_accounts_table(
    result: dict[str, Any] | None,
    show_quota: bool = False,
    current_timestamp: float | None = None,
) -> str:
    """Formats accounts list as a clean, human-readable terminal table focusing exclusively on Gemini."""
    if not isinstance(result, dict):
        return "No accounts registered in vault (~/.antigravity-vault/accounts)."

    accounts = result.get("accounts", [])
    if not isinstance(accounts, list) or not accounts:
        return "No accounts registered in vault (~/.antigravity-vault/accounts)."

    header = f"{'':2} {'ACCOUNT':<20} {'EMAIL':<30} {'STATUS':<15}"
    if show_quota:
        header += " GEMINI QUOTA"

    lines = [
        "Antigravity Accounts (~/.antigravity-vault):",
        "-" * len(header),
        header,
        "-" * len(header),
    ]

    for acc in accounts:
        if not isinstance(acc, dict):
            continue
        marker = "* " if acc.get("is_active") else "  "
        status = "Active" if acc.get("is_active") else "Standby"
        if not acc.get("valid", True):
            status += " (Invalid)"
        email_str = str(acc.get("email") or "").strip() or "-"
        line = f"{marker}{str(acc.get('name') or ''):<20} {email_str:<30} {status:<15}"

        if show_quota:
            quota_summary = _format_gemini_quota_summary(acc, current_timestamp=current_timestamp)
            line += f" {quota_summary}"

        lines.append(line)

    lines.append("-" * len(header))
    active_acc = result.get("active")
    if active_acc:
        lines.append(f"Active Account: {active_acc}")
    else:
        lines.append("No account currently active. Run: agy-switch switch <account>")

    return "\n".join(lines)


def format_status_report(status: dict[str, Any]) -> str:
    """Formats system and server status into a clean terminal report."""
    lines = [
        "Antigravity System Status:",
        "-------------------------------------------------------------",
    ]
    active = status.get("active")
    email = status.get("email") or "unknown"
    if active:
        lines.append(f"Active Account    : {active} ({email})")
        if "cached_quota" in status and isinstance(status["cached_quota"], dict):
            q_data = status["cached_quota"]
            if isinstance(q_data.get("gemini"), dict):
                lines.append(f"Cached Quota      : {q_data['gemini'].get('quota', 0)}% (Gemini)")
            elif isinstance(q_data.get("claude"), dict):
                lines.append(f"Cached Quota      : {q_data['claude'].get('quota', 0)}% (Claude)")
    else:
        lines.append("Active Account    : None (No account active)")

    if status.get("server_running"):
        pid = status.get("server_pid")
        lines.append(f"Language Server   : Running (PID: {pid})")
        if status.get("server_busy"):
            lines.append("Server State      : Busy (Active task running - requires --force to restart)")
        else:
            lines.append("Server State      : Idle (Safe to restart)")
    else:
        lines.append("Language Server   : Stopped / Not Found")

    lines.append("-------------------------------------------------------------")
    return "\n".join(lines)


def format_diagnostic_report(report: DiagnosticReport) -> str:
    """Formats 5-layer diagnostic report as a numbered terminal listicle with visual status indicators."""
    lines: list[str] = [
        "Antigravity System Diagnostics:",
        "-------------------------------------------------------------",
    ]

    unfixable_accounts: list[str] = []

    for idx, item in enumerate(report.items, start=1):
        check_name = item.title or item.name
        lines.append(f"{idx}. [{item.status}] {check_name}: {item.message}")
        if item.status in ("WARN", "FAIL") and not item.can_fix:
            if item.category in ("token_integrity", "dual_sync") or item.layer in (2, 3):
                matches = re.findall(r"'([a-zA-Z0-9._-]+)'", item.message)
                if matches:
                    for acc in matches:
                        lines.append(f"   -> Action required: Run 'agy-switch login {acc}' to re-authenticate.")
                        if acc not in unfixable_accounts:
                            unfixable_accounts.append(acc)
                else:
                    lines.append("   -> Action required: Run 'agy-switch login <alias>' to re-authenticate.")
                    if "<alias>" not in unfixable_accounts:
                        unfixable_accounts.append("<alias>")

    lines.append("-------------------------------------------------------------")

    rep_dict = report.to_dict()
    summary = rep_dict.get("summary", {})
    lines.append("Diagnostic Summary:")
    lines.append(f"  • Total Checks : {summary.get('total', len(report.items))}")
    lines.append(f"  • Passed       : {summary.get('passed', 0)}")
    lines.append(f"  • Warnings     : {summary.get('warned', 0)}")
    lines.append(f"  • Failures     : {summary.get('failed', 0)}")
    lines.append(f"  • Overall      : {report.overall_status}")

    if unfixable_accounts:
        lines.append("-------------------------------------------------------------")
        lines.append("Action Required for Unfixable Items:")
        for acc in unfixable_accounts:
            lines.append(f"  • Run 'agy-switch login {acc}' to re-authenticate or restore credentials.")

    return "\n".join(lines)


__all__ = [
    "format_countdown",
    "format_accounts_listicle",
    "format_accounts_table",
    "format_status_report",
    "format_diagnostic_report",
    "_format_bucket_countdown",
    "_format_gemini_quota_summary",
    "_parse_timestamp_defensive",
    "_parse_quota_defensive",
]
