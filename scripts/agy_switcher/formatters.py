"""Terminal output formatters and status reporting for agy-switch.

Layer 1 Core Services module: Depends only on Layer 0 (models) and standard library.
Pure Python 3.10+ standard library. Zero external dependencies.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
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


def _normalize_target_model(target_model: str | None) -> str:
    """Normalizes target model identifier to lowercase standard string."""
    if not isinstance(target_model, str) or not target_model.strip():
        return "gemini"
    return target_model.strip().lower()


def _format_model_quota_summary(
    acc: dict[str, Any],
    model: str = "gemini",
    current_timestamp: float | None = None,
) -> str:
    """Extracts and formats model quota summary for display across table and listicle views."""
    if not isinstance(acc, dict):
        return "Error / Unreachable"

    model_key = _normalize_target_model(model)
    q_root = acc.get("quota") if isinstance(acc.get("quota"), dict) else {}
    model_data = q_root.get(model_key) if isinstance(q_root.get(model_key), dict) else None

    if q_root.get("error") or model_data is None or (isinstance(model_data, dict) and model_data.get("error")):
        return "Error / Unreachable"

    if not isinstance(model_data, dict):
        return "Error / Unreachable"

    raw_cached = model_data.get("cached")
    is_cached = (
        raw_cached.strip().lower() in ("true", "1")
        if isinstance(raw_cached, str)
        else bool(raw_cached)
    )

    raw_quota = model_data.get("quota")
    parsed_q = _parse_quota_defensive(raw_quota)
    if parsed_q < 0:
        return "Error / Unreachable"
    quota = int(round(parsed_q))

    if is_cached:
        return f"{quota}% (Cached)"

    buckets = model_data.get("buckets") if isinstance(model_data.get("buckets"), dict) else {}
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


def _format_gemini_quota_summary(acc: dict[str, Any], current_timestamp: float | None = None) -> str:
    """Extracts and formats Gemini quota summary for display across table and listicle views."""
    return _format_model_quota_summary(acc, model="gemini", current_timestamp=current_timestamp)


def format_accounts_listicle(
    result: dict[str, Any] | None,
    show_quota: bool = True,
    current_timestamp: float | None = None,
    target_model: str = "gemini",
) -> str:
    """Formats accounts list as a compact, single-line bulleted listicle for LLMs and narrow terminals."""
    if not isinstance(result, dict):
        return "No accounts registered in vault (~/.antigravity-vault/accounts)."

    accounts = result.get("accounts", [])
    if not isinstance(accounts, list) or not accounts:
        return "No accounts registered in vault (~/.antigravity-vault/accounts)."

    model_clean = _normalize_target_model(target_model)
    model_label = model_clean.capitalize()

    lines: list[str] = []
    for acc in accounts:
        if not isinstance(acc, dict):
            continue
        marker = "• [*]" if acc.get("is_active") else "• [ ]"
        email_str = str(acc.get("email") or "").strip() or "-"
        name = str(acc.get("name") or "").strip()
        prefix = f"{marker} {name} ({email_str})"

        if show_quota:
            quota_summary = _format_model_quota_summary(acc, model=model_clean, current_timestamp=current_timestamp)
            lines.append(f"{prefix} — {model_label}: {quota_summary}")
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
    target_model: str = "gemini",
) -> str:
    """Formats accounts list as a clean, human-readable terminal table."""
    if not isinstance(result, dict):
        return "No accounts registered in vault (~/.antigravity-vault/accounts)."

    accounts = result.get("accounts", [])
    if not isinstance(accounts, list) or not accounts:
        return "No accounts registered in vault (~/.antigravity-vault/accounts)."

    model_clean = _normalize_target_model(target_model)
    header = f"{'':2} {'ACCOUNT':<20} {'EMAIL':<30} {'STATUS':<15}"
    if show_quota:
        header += f" {model_clean.upper()} QUOTA"

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
            quota_summary = _format_model_quota_summary(acc, model=model_clean, current_timestamp=current_timestamp)
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


def format_fleet_exhaustion_message(
    summary: Any = None,
    model: str | None = None,
    current_time: float | datetime | None = None,
) -> str:
    """Formats a user-facing failure response displaying earliest reset ETA.

    Adheres to [PANIC-ESC-003], [PANIC-ERR-003], [PANIC-EDGE-001], [PANIC-EDGE-002].
    """
    default_msg = (
        "⚠️ Seluruh kuota akun sedang habis. Silakan coba kembali dalam beberapa saat atau periksa status dengan /quota."
    )

    if summary is None:
        return default_msg

    accounts_map = getattr(summary, "accounts", None)
    if accounts_map is None and isinstance(summary, dict):
        accounts_map = summary.get("accounts")

    if not isinstance(accounts_map, dict) or not accounts_map:
        return default_msg

    curr_dt: datetime
    if current_time is None:
        summary_ts = getattr(summary, "timestamp", None)
        if summary_ts is None and isinstance(summary, dict):
            summary_ts = summary.get("timestamp")
        if isinstance(summary_ts, (int, float)) and not isinstance(summary_ts, bool) and summary_ts > 0:
            try:
                curr_dt = datetime.fromtimestamp(float(summary_ts), tz=timezone.utc)
            except Exception:
                curr_dt = datetime.now(timezone.utc)
        else:
            curr_dt = datetime.now(timezone.utc)
    elif isinstance(current_time, (int, float)) and not isinstance(current_time, bool):
        try:
            curr_dt = datetime.fromtimestamp(float(current_time), tz=timezone.utc)
        except Exception:
            curr_dt = datetime.now(timezone.utc)
    elif isinstance(current_time, datetime):
        curr_dt = current_time.astimezone(timezone.utc) if current_time.tzinfo else current_time.replace(tzinfo=timezone.utc)
    else:
        curr_dt = datetime.now(timezone.utc)

    norm_target = model.strip().lower() if (isinstance(model, str) and model.strip()) else "gemini"
    family = "claude" if "claude" in norm_target else ("gemini" if "gemini" in norm_target else norm_target)

    earliest_candidate: tuple[float, str, int] | None = None

    for acc_name, acc_data in accounts_map.items():
        if not isinstance(acc_data, dict):
            continue

        source_dict = acc_data
        if family and family in acc_data and isinstance(acc_data[family], dict):
            source_dict = acc_data[family]
        elif norm_target and norm_target in acc_data and isinstance(acc_data[norm_target], dict):
            source_dict = acc_data[norm_target]

        raw_val = (
            source_dict.get("reset_time")
            or source_dict.get("resetTime")
            or source_dict.get("resets_at")
            or source_dict.get("reset_eta")
            or source_dict.get("reset")
            or acc_data.get("reset_time")
            or acc_data.get("resetTime")
            or acc_data.get("resets_at")
            or acc_data.get("reset_eta")
            or acc_data.get("reset")
        )

        if raw_val is None or raw_val == "" or str(raw_val).strip().lower() in ("none", "null", "-", "undefined"):
            continue

        explicit_minutes: int | None = None
        for k in ("remaining_minutes", "remaining", "reset_delta", "delta_minutes", "minutes_remaining"):
            val = source_dict.get(k) if k in source_dict else acc_data.get(k)
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                if math.isfinite(val) and val > 0:
                    explicit_minutes = max(1, int(round(val)))
                    break
            elif isinstance(val, str) and val.strip():
                m = re.search(r"(\d+)", val)
                if m:
                    parsed_m = int(m.group(1))
                    if parsed_m > 0:
                        explicit_minutes = parsed_m
                        break

        time_str: str | None = None
        sort_key: float | None = None
        delta_minutes: int | None = explicit_minutes

        if isinstance(raw_val, datetime):
            try:
                dt = raw_val.astimezone(timezone.utc) if raw_val.tzinfo else raw_val.replace(tzinfo=timezone.utc)
                if dt.year < 9000:
                    delta_seconds = dt.timestamp() - curr_dt.timestamp()
                    if delta_seconds > 0 or (delta_minutes is not None and delta_minutes > 0):
                        sort_key = dt.timestamp()
                        time_str = dt.strftime("%H:%M")
                        if delta_minutes is None:
                            delta_minutes = max(1, int(round(delta_seconds / 60)))
            except Exception:
                pass
        elif isinstance(raw_val, (int, float)) and not isinstance(raw_val, bool):
            try:
                val_f = float(raw_val)
                if math.isfinite(val_f) and 0 < val_f < 32503680000.0:
                    delta_seconds = val_f - curr_dt.timestamp()
                    if delta_seconds > 0 or (delta_minutes is not None and delta_minutes > 0):
                        sort_key = val_f
                        dt = datetime.fromtimestamp(val_f, tz=timezone.utc)
                        time_str = dt.strftime("%H:%M")
                        if delta_minutes is None:
                            delta_minutes = max(1, int(round(delta_seconds / 60)))
            except Exception:
                pass
        elif isinstance(raw_val, str) and raw_val.strip():
            clean_str = raw_val.strip()
            if "t" in clean_str.lower():
                try:
                    dt = datetime.fromisoformat(clean_str.replace("Z", "+00:00")).astimezone(timezone.utc)
                    if dt.year < 9000:
                        delta_seconds = dt.timestamp() - curr_dt.timestamp()
                        if delta_seconds > 0 or (delta_minutes is not None and delta_minutes > 0):
                            sort_key = dt.timestamp()
                            time_str = dt.strftime("%H:%M")
                            if delta_minutes is None:
                                delta_minutes = max(1, int(round(delta_seconds / 60)))
                except Exception:
                    pass
            elif clean_str.replace(".", "", 1).isdigit():
                try:
                    val_f = float(clean_str)
                    if math.isfinite(val_f) and 0 < val_f < 32503680000.0:
                        delta_seconds = val_f - curr_dt.timestamp()
                        if delta_seconds > 0 or (delta_minutes is not None and delta_minutes > 0):
                            sort_key = val_f
                            dt = datetime.fromtimestamp(val_f, tz=timezone.utc)
                            time_str = dt.strftime("%H:%M")
                            if delta_minutes is None:
                                delta_minutes = max(1, int(round(delta_seconds / 60)))
                except Exception:
                    pass

            if time_str is None:
                time_match = re.search(r"\b(\d{1,2}):(\d{2})\b", clean_str)
                if time_match:
                    try:
                        th = int(time_match.group(1))
                        tm = int(time_match.group(2))
                        if 0 <= th <= 23 and 0 <= tm <= 59:
                            time_str = f"{th:02d}:{tm:02d}"
                            reset_dt = curr_dt.replace(hour=th, minute=tm, second=0, microsecond=0)
                            if delta_minutes is None:
                                if reset_dt < curr_dt:
                                    reset_dt += timedelta(days=1)
                                delta_seconds = (reset_dt - curr_dt).total_seconds()
                                if delta_seconds > 0:
                                    delta_minutes = max(1, int(round(delta_seconds / 60)))
                            if delta_minutes is not None and delta_minutes > 0:
                                sort_key = curr_dt.timestamp() + delta_minutes * 60
                    except Exception:
                        pass

        if time_str is not None and sort_key is not None and delta_minutes is not None and delta_minutes > 0:
            candidate = (sort_key, time_str, delta_minutes)
            if earliest_candidate is None or candidate[0] < earliest_candidate[0]:
                earliest_candidate = candidate

    if earliest_candidate is None:
        return default_msg

    _, time_str, delta_minutes = earliest_candidate

    if delta_minutes < 60:
        countdown_str = f"{delta_minutes} menit lagi"
    else:
        h = delta_minutes // 60
        rem_minutes = delta_minutes % 60
        countdown_str = f"{h} jam {rem_minutes} menit lagi" if rem_minutes > 0 else f"{h} jam lagi"

    return f"⚠️ Seluruh kuota akun sedang dalam masa tunggu. Kuota diperkirakan pulih pada pk {time_str} ({countdown_str})."


format_fleet_exhaustion_notice = format_fleet_exhaustion_message


__all__ = [
    "format_countdown",
    "format_accounts_listicle",
    "format_accounts_table",
    "format_status_report",
    "format_diagnostic_report",
    "format_fleet_exhaustion_message",
    "format_fleet_exhaustion_notice",
    "_format_bucket_countdown",
    "_format_gemini_quota_summary",
    "_format_model_quota_summary",
    "_parse_timestamp_defensive",
    "_parse_quota_defensive",
]
