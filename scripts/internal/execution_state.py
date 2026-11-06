#!/usr/bin/env python3
"""
scripts/internal/execution_state.py
Zero-dependency Ticket Execution State Machine engine for Track 28 ([STATE-01], [STATE-02]).

Manages persistent stage progression within .scratch/<feature-slug>/execution-state.json:
STAGE_1_PREFLIGHT -> STAGE_2_BUILDER -> STAGE_3_ADAPTIVE_REVIEW -> STAGE_4_DOUBLE_VERIFY -> STAGE_5_COMMIT_AND_RESOLVE
"""

import argparse
import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable, Dict, Optional
import uuid


STAGE_1_PREFLIGHT = "STAGE_1_PREFLIGHT"
STAGE_2_BUILDER = "STAGE_2_BUILDER"
STAGE_3_ADAPTIVE_REVIEW = "STAGE_3_ADAPTIVE_REVIEW"
STAGE_4_DOUBLE_VERIFY = "STAGE_4_DOUBLE_VERIFY"
STAGE_5_COMMIT_AND_RESOLVE = "STAGE_5_COMMIT_AND_RESOLVE"

VALID_STAGES = (
    STAGE_1_PREFLIGHT,
    STAGE_2_BUILDER,
    STAGE_3_ADAPTIVE_REVIEW,
    STAGE_4_DOUBLE_VERIFY,
    STAGE_5_COMMIT_AND_RESOLVE,
)

DEFAULT_STAGE_STATUSES = {
    "1_preflight": "COMPLETED",
    "2_builder": "IN_PROGRESS",
    "3_adaptive_review": "PENDING",
    "4_double_verification": "PENDING",
    "5_commit_and_resolve": "PENDING",
}


def _get_utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _resolve_feature_scratch_dir(scratch_dir: Path | str) -> Path:
    """
    Resolves the canonical feature scratch directory root (.scratch/<feature-slug>).
    Gracefully handles paths pointing to session.json, task-graph.md, tickets, or subdirectories like issues/.
    """
    target = Path(scratch_dir).resolve()
    if target.is_file():
        target = target.parent
    if target.name in ("issues", "reports"):
        target = target.parent
    return target


def _atomic_write_json(target_file: Path | str, data: Dict[str, Any], ignore_errors: bool = False) -> None:
    """
    [ERR-LEAN-01] Writes JSON payload atomically via a temporary file with safe rollback.
    Validates write permissions on existing files and safely cleans up partial temp files on failure.
    """
    path = Path(target_file).resolve()
    if path.is_file() and not os.access(path, os.W_OK):
        exc = PermissionError(13, f"Permission denied: '{path}'")
        if not ignore_errors:
            raise exc
        return
    temp_file = path.parent / f".{path.name}.tmp.{os.getpid()}_{uuid.uuid4().hex}"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(temp_file, path)
    except OSError as exc:
        if not ignore_errors:
            raise exc
    finally:
        if temp_file.exists():
            try:
                temp_file.unlink()
            except OSError:
                pass


def init_execution_state(scratch_dir: Path | str, ticket_id: str) -> Dict[str, Any]:
    """Initialize persistent execution state file on Turn 1 prior to subagent dispatch."""
    scratch_path = _resolve_feature_scratch_dir(scratch_dir)
    scratch_path.mkdir(parents=True, exist_ok=True)
    state_file = scratch_path / "execution-state.json"

    state_data: Dict[str, Any] = {
        "ticketId": str(ticket_id).strip(),
        "activeStage": STAGE_2_BUILDER,
        "stages": dict(DEFAULT_STAGE_STATUSES),
        "updatedAt": _get_utc_now_iso(),
    }

    _atomic_write_json(state_file, state_data)
    return state_data


def load_execution_state(scratch_dir: Path | str) -> Optional[Dict[str, Any]]:
    """Read and deserialize execution-state.json if present on disk."""
    state_file = Path(scratch_dir).resolve() / "execution-state.json"
    if not state_file.is_file():
        return None
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError):
        return None
    return None


def transition_execution_state(
    scratch_dir: Path | str,
    next_stage: str,
    stage_updates: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Transition state machine to next active stage and update stage statuses."""
    scratch_path = _resolve_feature_scratch_dir(scratch_dir)
    state_file = scratch_path / "execution-state.json"
    current_state = load_execution_state(scratch_path)
    if not current_state:
        raise FileNotFoundError(f"execution-state.json not found in {scratch_path}")

    current_state["activeStage"] = next_stage
    stages = current_state.setdefault("stages", {})

    if next_stage == STAGE_3_ADAPTIVE_REVIEW:
        stages["2_builder"] = "COMPLETED"
        if stages.get("3_adaptive_review") == "PENDING":
            stages["3_adaptive_review"] = "IN_PROGRESS"
    elif next_stage == STAGE_4_DOUBLE_VERIFY:
        if stages.get("3_adaptive_review") in ("PENDING", "IN_PROGRESS"):
            stages["3_adaptive_review"] = "COMPLETED"
        stages["4_double_verification"] = "IN_PROGRESS"
    elif next_stage == STAGE_5_COMMIT_AND_RESOLVE:
        stages["4_double_verification"] = "COMPLETED"
        stages["5_commit_and_resolve"] = "IN_PROGRESS"

    if stage_updates:
        stages.update(stage_updates)

    current_state["updatedAt"] = _get_utc_now_iso()

    _atomic_write_json(state_file, current_state)
    return current_state


def complete_execution_state(
    scratch_dir: Path | str,
    require_stage_5: bool = True,
) -> Dict[str, Any]:
    """[RUN-02] Marks 5_commit_and_resolve as COMPLETED and preserves the completed execution record for auditability."""
    scratch_path = _resolve_feature_scratch_dir(scratch_dir)
    state_file = scratch_path / "execution-state.json"
    current_state = load_execution_state(scratch_path)
    if not current_state:
        raise FileNotFoundError(f"execution-state.json not found in {scratch_path}")

    active_stage = str(current_state.get("activeStage") or "").strip()
    if require_stage_5 and active_stage != STAGE_5_COMMIT_AND_RESOLVE:
        raise ValueError(
            f"Cannot complete execution state: activeStage is '{active_stage}', expected '{STAGE_5_COMMIT_AND_RESOLVE}' [RUN-02]"
        )

    stages = current_state.setdefault("stages", {})
    stages["5_commit_and_resolve"] = "COMPLETED"
    current_state["updatedAt"] = _get_utc_now_iso()

    _atomic_write_json(state_file, current_state)
    return current_state


def init_guarded_pipeline(
    scratch_dir: Path | str,
    git_root: Optional[Path | str] = None,
    allow_dirty: bool = False,
) -> tuple[bool, Dict[str, Any], list[str]]:
    """
    Validates active Command Center task graph existence, checks working tree hygiene,
    and initializes the guarded autonomous pipeline recording executionMode: 'guarded-pipeline'.
    Implements [INGEST-01], [INGEST-02], and [ERR-INGEST-01] per ADR 0036.
    """
    scratch_path = _resolve_feature_scratch_dir(scratch_dir)
    feature_slug = scratch_path.name

    root_to_check: Optional[Path] = Path(git_root).resolve() if git_root else None
    if not root_to_check:
        cur = scratch_path
        while cur != cur.parent:
            if (cur / ".git").exists():
                root_to_check = cur
                break
            cur = cur.parent

    # Enforce preflight working tree hygiene gate unless explicitly bypassed
    if not allow_dirty and root_to_check and (root_to_check / ".git").exists():
        try:
            status_res = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=root_to_check,
                capture_output=True,
                text=True,
                check=False,
            )
            if status_res.returncode == 0 and status_res.stdout.strip():
                return (
                    False,
                    {},
                    [
                        "[INGEST-02] Dirty git working tree detected: uncommitted changes are present in the workspace. "
                        "Commit or stash uncommitted changes before launching the guarded autonomous pipeline."
                    ],
                )
        except OSError:
            pass

    issues_dir = scratch_path / "issues"
    if not issues_dir.is_dir():
        return (
            False,
            {},
            [
                f"[INGEST-02] No active task graph or issues directory found in '{scratch_path}'. "
                "Direct execution on uninspected specifications is barred. "
                "Run /to-tickets first to deconstruct the specification into tickets before launching /implement-spec."
            ],
        )

    valid, session_data, diags = validate_session_anchor(scratch_path)
    if not valid:
        return False, {}, diags

    session_file = scratch_path / "session.json"
    feature_name = str(session_data.get("feature") or feature_slug).strip()
    integration_branch = f"feat/spec-{feature_name}"

    session_data["executionMode"] = "guarded-pipeline"
    session_data["integrationBranch"] = integration_branch
    session_data["updatedAt"] = _get_utc_now_iso()

    init_diags = [f"[INGEST-01] Guarded pipeline initialized for '{feature_name}' on branch '{integration_branch}'."]
    if session_data.get("circuitBreaker", {}).get("tripped"):
        cb = session_data["circuitBreaker"]
        prior_breaker = cb.get("breaker", 0)
        prior_reason = cb.get("reason", session_data.get("escalation", ""))
        cb["tripped"] = False
        cb["resumed"] = True
        cb["resumedAt"] = _get_utc_now_iso()
        cb["prohibit_pr"] = False
        session_data.pop("escalation", None)
        resumption_record = {
            "resumed": True,
            "resumedAt": _get_utc_now_iso(),
            "priorBreaker": prior_breaker,
            "priorReason": prior_reason,
        }
        session_data["resumption"] = resumption_record
        history = session_data.setdefault("resumptionHistory", [])
        if isinstance(history, list):
            history.append(dict(resumption_record))
        init_diags.append(
            f"[EDGE-BREAKER-01] Resuming pipeline from prior Circuit Breaker {prior_breaker} ({prior_reason})."
        )

    _atomic_write_json(session_file, session_data)

    return (
        True,
        session_data,
        init_diags,
    )


def validate_session_anchor(
    scratch_dir_or_session_file: Path | str,
    task_graph_path: Optional[Path | str] = None,
) -> tuple[bool, Dict[str, Any], list[str]]:
    """
    Validates session anchor (.scratch/<feature-slug>/session.json) and
    task graph (.scratch/<feature-slug>/task-graph.md) schema integrity.
    Implements [ERR-INGEST-01] per ADR 0036 and Track 40 Guarded Pipeline.
    """
    target = Path(scratch_dir_or_session_file).resolve()
    if target.is_file() and target.name == "session.json":
        session_file = target
        scratch_path = target.parent
    elif target.is_dir():
        scratch_path = target
        session_file = scratch_path / "session.json"
    else:
        if target.name == "session.json":
            session_file = target
            scratch_path = target.parent
        else:
            scratch_path = target
            session_file = scratch_path / "session.json"

    if task_graph_path:
        task_graph_file = Path(task_graph_path).resolve()
    else:
        task_graph_file = scratch_path / "task-graph.md"

    if not session_file.is_file():
        return (
            False,
            {},
            [
                f"[INGEST-02] Missing session anchor '{session_file}'. "
                "Run /to-tickets first to initialize the Command Center session."
            ],
        )

    try:
        raw_content = session_file.read_text(encoding="utf-8")
    except OSError as exc:
        return (
            False,
            {},
            [
                f"[ERR-INGEST-01] Corrupted session anchor in '{session_file}': {exc}. Aborting execution."
            ],
        )

    if not raw_content.strip():
        return (
            False,
            {},
            [
                f"[ERR-INGEST-01] Malformed session anchor JSON in '{session_file}': file is empty. "
                "Schema validation diagnostic: empty session anchor file at line 1, column 1. Aborting execution."
            ],
        )

    try:
        session_data = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        return (
            False,
            {},
            [
                f"[ERR-INGEST-01] Malformed session anchor JSON in '{session_file}' at line {exc.lineno}, column {exc.colno}: {exc.msg}. "
                f"Schema validation diagnostic: invalid JSON syntax pinpointed at line {exc.lineno}, column {exc.colno}. Aborting execution."
            ],
        )

    if not isinstance(session_data, dict):
        return (
            False,
            {},
            [
                f"[ERR-INGEST-01] Session anchor in '{session_file}' must be a JSON dictionary object, got {type(session_data).__name__}. "
                "Schema validation diagnostic: root is not an object. Aborting execution."
            ],
        )

    required_keys = ("feature", "createdAt")
    for req in required_keys:
        if req not in session_data:
            return (
                False,
                {},
                [
                    f"[ERR-INGEST-01] Malformed session anchor schema in '{session_file}': missing required key '{req}'. "
                    f"Schema validation diagnostic: required anchor field '{req}' omitted. Aborting execution."
                ],
            )
        val = session_data[req]
        if not isinstance(val, str) or not val.strip():
            return (
                False,
                {},
                [
                    f"[ERR-INGEST-01] Malformed session anchor schema in '{session_file}': field '{req}' must be a non-empty string. "
                    f"Schema validation diagnostic: required anchor field '{req}' is empty or invalid. Aborting execution."
                ],
            )

    if "commandCenterConversationId" in session_data:
        return (
            False,
            {},
            [
                f"[ERR-INGEST-01] Malformed session anchor schema in '{session_file}': legacy key 'commandCenterConversationId' is forbidden. "
                "Schema validation diagnostic: obsolete ConnectRPC signaling key present per ADR 0036. Aborting execution."
            ],
        )

    if not task_graph_file.is_file():
        return (
            False,
            {},
            [
                f"[INGEST-02] No active task graph found at '{task_graph_file}'. "
                "Direct execution on uninspected specifications is barred. "
                "Run /to-tickets first to deconstruct the specification into tickets before launching /implement-spec."
            ],
        )

    try:
        tg_content = task_graph_file.read_text(encoding="utf-8")
    except OSError as exc:
        return (
            False,
            {},
            [
                f"[ERR-INGEST-01] Corrupted task graph in '{task_graph_file}': {exc}. Aborting execution."
            ],
        )

    if not tg_content.strip():
        return (
            False,
            {},
            [
                f"[ERR-INGEST-01] Corrupted task graph in '{task_graph_file}': file is empty. "
                "Schema validation diagnostic: empty task graph at line 1. Aborting execution."
            ],
        )

    lines = tg_content.splitlines()
    in_fence = False
    fence_start_line: Optional[int] = None
    fence_pattern = ""
    mermaid_block_lines: list[tuple[int, str]] = []
    in_mermaid = False

    for idx, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("```"):
            m = re.match(r"^(`{3,})", stripped)
            if m:
                backticks = m.group(1)
                tag = stripped[len(backticks):].strip().lower()
                if not in_fence:
                    in_fence = True
                    fence_start_line = idx
                    fence_pattern = backticks
                    if tag == "mermaid":
                        in_mermaid = True
                        mermaid_block_lines = []
                elif stripped.startswith(fence_pattern):
                    in_fence = False
                    fence_start_line = None
                    if in_mermaid:
                        in_mermaid = False
                        non_empty_m_lines = [l.strip() for _, l in mermaid_block_lines if l.strip()]
                        if not non_empty_m_lines:
                            return (
                                False,
                                {},
                                [
                                    f"[ERR-INGEST-01] Corrupted task graph in '{task_graph_file}': empty mermaid diagram block. "
                                    f"Schema validation diagnostic: mermaid block opened at line {idx - len(mermaid_block_lines) - 1} contains no diagram definition. Aborting execution."
                                ],
                            )
        elif in_mermaid:
            mermaid_block_lines.append((idx, line))

    if in_fence and fence_start_line is not None:
        return (
            False,
            {},
            [
                f"[ERR-INGEST-01] Corrupted task graph in '{task_graph_file}' at line {fence_start_line}: unclosed code fence block. "
                f"Schema validation diagnostic: unclosed markdown code fence opened at line {fence_start_line} without terminating fence. Aborting execution."
            ],
        )

    try:
        from scripts.internal.resolve_ticket import detect_dag_cycles
    except ImportError:
        try:
            from resolve_ticket import detect_dag_cycles  # type: ignore[no-redef]
        except ImportError:
            detect_dag_cycles = None  # type: ignore[assignment]

    if detect_dag_cycles is not None:
        cycle_res = detect_dag_cycles(scratch_path)
        if cycle_res.has_cycle:
            return (
                False,
                {},
                [cycle_res.diagnostic],
            )

    return True, session_data, [f"[INGEST-01] Session anchor and task graph verified in '{scratch_path}'."]


def parse_ticket_blockers(blocked_by_raw: Any) -> list[str]:
    """
    [EDGE-DAG-SLUGS-01] Parse blocker ticket IDs from raw 'Blocked by' field string or list.
    Normalizes sentinels (none, null, n/a, na, unblocked, frontier, []) to [] while retaining
    numeric IDs (padded to 2 digits) and non-numeric slug tokens.
    """
    if blocked_by_raw is None:
        return []

    sentinels = {
        "none",
        "null",
        "n/a",
        "na",
        "unblocked",
        "frontier",
        "[]",
    }

    if isinstance(blocked_by_raw, (int, float)):
        return [f"{int(blocked_by_raw):02d}"]

    items: list[object] = []
    if isinstance(blocked_by_raw, (list, tuple, set)):
        items = list(blocked_by_raw)
    else:
        raw_str = str(blocked_by_raw).strip()
        raw_lower = raw_str.lower().strip("`* \t")
        if (
            not raw_str
            or raw_lower in sentinels
            or raw_lower.startswith("none")
            or raw_lower.startswith("unblocked")
            or raw_lower.startswith("frontier")
        ):
            return []
        if raw_str.startswith("[") and raw_str.endswith("]"):
            raw_str = raw_str[1:-1]
        items = [p.strip() for p in re.split(r"[\n,]+", raw_str) if p.strip()]

    clean_ids: list[str] = []
    for item in items:
        if item is None:
            continue
        if isinstance(item, (int, float)):
            val = f"{int(item):02d}"
            if val not in clean_ids:
                clean_ids.append(val)
            continue
        s = str(item).strip().lstrip("-* \t").strip("`'\"* \t")
        if not s:
            continue
        s_clean = s.lstrip("#")
        s_lower = s_clean.lower()
        if (
            s_lower in sentinels
            or s_lower.startswith("none")
            or s_lower.startswith("unblocked")
            or s_lower.startswith("frontier")
            or s_clean.strip("[] \t") == ""
            or s_clean == "-"
        ):
            continue
        match = re.match(r"^(?:ticket[-_]?)?#?([0-9]{1,3})\b", s_clean, re.IGNORECASE)
        if match:
            clean_id = f"{int(match.group(1)):02d}"
            if clean_id not in clean_ids:
                clean_ids.append(clean_id)
        else:
            candidate = re.sub(r"[:(\[].*$", "", s_clean).strip().strip("`'\"* \t")
            cand_lower = candidate.lower()
            if (
                candidate
                and cand_lower not in sentinels
                and not cand_lower.startswith("none")
                and not cand_lower.startswith("unblocked")
                and not cand_lower.startswith("frontier")
                and candidate not in clean_ids
            ):
                clean_ids.append(candidate)
    return clean_ids


def is_ticket_resolved(status: str) -> bool:
    """Check if a ticket status represents resolved/completed."""
    clean = str(status).strip().lower().replace(" ", "-")
    return clean in ("resolved", "closed", "completed", "done", "status:-resolved")


def scan_scratch_tickets(scratch_dir: Path | str) -> list[Dict[str, Any]]:
    """Scan all ticket markdown files in .scratch/<feature-slug>/issues/."""
    scratch_path = _resolve_feature_scratch_dir(scratch_dir)
    issues_dir = scratch_path / "issues"
    if not issues_dir.is_dir():
        if scratch_path.name == "issues":
            issues_dir = scratch_path
        else:
            return []

    tickets: list[Dict[str, Any]] = []
    for f in sorted(issues_dir.glob("*.md")):
        if not f.is_file():
            continue
        try:
            content = f.read_text(encoding="utf-8")
        except OSError:
            continue

        m_id = re.match(r"^(\d+)", f.name)
        ticket_id = f"{int(m_id.group(1)):02d}" if m_id else f.stem

        status = "ready-for-agent"
        m_status = re.search(r"\*\*Status:\*\*\s*([^\n\r]+)", content, re.IGNORECASE)
        if not m_status:
            m_status = re.search(r"^status:\s*([^\n\r]+)", content, re.IGNORECASE | re.MULTILINE)
        if m_status:
            status = m_status.group(1).strip()

        blocked_by: list[str] = []
        m_block = re.search(r"\*\*Blocked by:\*\*\s*([^\n\r]+)", content, re.IGNORECASE)
        if not m_block:
            m_block = re.search(r"^blocked[-_]by:\s*([^\n\r]+)", content, re.IGNORECASE | re.MULTILINE)
        if m_block:
            blocked_by = parse_ticket_blockers(m_block.group(1))

        title = f.stem
        m_title = re.search(r"^#\s+(?:(\d+):?\s*)?([^\n\r]+)", content, re.MULTILINE)
        if m_title:
            title = m_title.group(2).strip()

        tickets.append({
            "id": ticket_id,
            "title": title,
            "status": status,
            "blockers": blocked_by,
            "path": str(f),
        })
    return tickets


def compute_sequential_frontier(scratch_dir_or_tickets: Path | str | list[Dict[str, Any]]) -> list[str]:
    """
    [DRAIN-01] [DRAIN-02] Computes active unblocked frontier tickets in topological dependency order.
    Dependent tickets remain locked in BLOCKED status until all prerequisite tickets have reached Status: resolved.
    """
    if isinstance(scratch_dir_or_tickets, list):
        tickets = scratch_dir_or_tickets
    else:
        tickets = scan_scratch_tickets(scratch_dir_or_tickets)

    try:
        from scripts.internal.resolve_ticket import detect_dag_cycles
    except ImportError:
        try:
            from resolve_ticket import detect_dag_cycles  # type: ignore[no-redef]
        except ImportError:
            detect_dag_cycles = None  # type: ignore[assignment]

    if detect_dag_cycles is not None:
        cycle_res = detect_dag_cycles(tickets, ignore_resolved=True)
        if cycle_res.has_cycle:
            raise ValueError(cycle_res.diagnostic)

    resolved_ids: set[str] = set()
    for t in tickets:
        tid = str(t.get("id", "")).strip().lstrip("#")
        if tid.isdigit():
            tid = f"{int(tid):02d}"
        if is_ticket_resolved(str(t.get("status", ""))):
            resolved_ids.add(tid)

    frontier: list[str] = []
    for t in tickets:
        tid = str(t.get("id", "")).strip().lstrip("#")
        if tid.isdigit():
            tid = f"{int(tid):02d}"
        if is_ticket_resolved(str(t.get("status", ""))):
            continue

        raw_blockers = t.get("blockers") or t.get("blocked_by") or []
        blockers = parse_ticket_blockers(raw_blockers)
        # [DRAIN-02] Topological dependency ordering: all blockers must be resolved
        if all(b in resolved_ids for b in blockers):
            frontier.append(tid)

    return sorted(frontier)


def select_next_sequential_ticket(scratch_dir: Path | str) -> Optional[Dict[str, Any]]:
    """
    [DRAIN-01] Selects the next unblocked ticket along the DAG frontier for execution
    directly on integration branch HEAD. Returns None if all tickets are resolved or blocked.
    """
    tickets = scan_scratch_tickets(scratch_dir)
    frontier_ids = compute_sequential_frontier(tickets)
    if not frontier_ids:
        return None

    next_id = frontier_ids[0]
    for t in tickets:
        tid = str(t.get("id", "")).strip().lstrip("#")
        if tid.isdigit():
            tid = f"{int(tid):02d}"
        if tid == next_id:
            return t
    return None


def can_advance_to_next_ticket(
    current_ticket_id: str,
    verified_clean: bool,
    committed_to_head: bool,
    step_mode: bool = False,
) -> tuple[bool, str]:
    """
    [DRAIN-01] Verifies that Ticket N+1 begins implementation strictly after Ticket N
    has verified clean exit code 0, executed Double-Verification, and committed its changes to HEAD.
    [CHECK-03] Single-step execution under --step halts execution immediately with Recommend-and-Stop.
    """
    if step_mode:
        return (
            False,
            f"[CHECK-03] Single-step execution (--step): Ticket {current_ticket_id} resolved on disk. Halting execution immediately with Recommend-and-Stop without claiming subsequent frontier tickets.",
        )
    if not verified_clean:
        return (
            False,
            f"[DRAIN-01] Cannot advance past ticket {current_ticket_id}: ticket has not passed Double-Verification Gate with exit code 0.",
        )
    if not committed_to_head:
        return (
            False,
            f"[DRAIN-01] Cannot advance past ticket {current_ticket_id}: verified changes have not been committed to integration branch HEAD.",
        )
    return (
        True,
        f"[DRAIN-01] Ticket {current_ticket_id} verified clean and committed to HEAD. Ready for sequential advancement.",
    )


def evaluate_step_execution(
    current_ticket_id: str,
    step_mode: bool = True,
) -> tuple[bool, Dict[str, Any], str]:
    """
    [CHECK-03] Evaluates single-step execution under --step flag.
    When --step is active, claims and executes exactly 1 ticket, resolving on disk,
    and concludes immediately with Recommend-and-Stop without claiming subsequent frontier tickets.
    """
    if not step_mode:
        return True, {"step_mode": False, "halt_required": False}, "[CHECK-03] Continuous mode active: proceeding to subsequent frontier tickets."
    payload: Dict[str, Any] = {
        "step_mode": True,
        "halt_required": True,
        "ticket_id": str(current_ticket_id).strip(),
        "status": "STEP_COMPLETED",
        "recommend_and_stop": True,
    }
    diag = (
        f"🛑 [CHECK-03] Single-step execution (--step): Ticket {current_ticket_id} resolved on disk.\n"
        "Halting execution immediately with Recommend-and-Stop without claiming subsequent frontier tickets."
    )
    return False, payload, diag


def validate_execution_flags(
    auto: bool = False,
    step: bool = False,
) -> tuple[bool, Dict[str, bool], str]:
    """
    [CHECK-02] [CHECK-03] [ERR-CHECK-01] Validates mutually exclusive execution flags (--auto and --step).
    """
    flags = {"auto": bool(auto), "step": bool(step)}
    if auto and step:
        diag = (
            "🛑 [ERR-CHECK-01] Mutually exclusive flags: '--auto' (full lifecycle cruise control) "
            "and '--step' (single-ticket pause) cannot be used together. "
            "Select either '--auto' for automated progression or '--step' for single-ticket step execution."
        )
        return False, flags, diag
    if auto:
        diag = "🚀 [CHECK-02] Autonomous advancement mode (--auto) active."
        return True, flags, diag
    if step:
        diag = "🛑 [CHECK-03] Single-step execution mode (--step) active."
        return True, flags, diag
    return True, flags, "Default sequential execution mode active."


def parse_execution_flags(argv: Optional[list[str]] = None) -> tuple[bool, Dict[str, bool], str]:
    """Parses and validates CLI execution flags from argument list or sys.argv."""
    args_list = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(description="Parse execution flags", add_help=False)
    parser.add_argument("--auto", action="store_true")
    parser.add_argument("--step", action="store_true")
    parsed, _ = parser.parse_known_args(args_list)
    return validate_execution_flags(auto=parsed.auto, step=parsed.step)


def build_in_band_stage1_builder_brief(
    ticket: Dict[str, Any],
    feature_slug: str,
    branch: Optional[str] = None,
    shared_notes_path: Optional[str] = None,
    type_name: str = "self",
) -> Dict[str, Any]:
    """
    [SWARM-01] Generates Structured Subagent Brief for Stage 1 Builder (spec-implementer)
    with pristine context window (0 prior tokens), Fast Workspace Mode (Workspace: 'inherit'),
    and strict send_message completion protocol without cross-conversation IPC.
    """
    if not isinstance(ticket, dict):
        raise TypeError(f"ticket must be a dictionary, got {type(ticket).__name__}")
    if not feature_slug or not str(feature_slug).strip():
        raise ValueError("feature_slug must be a non-empty string")

    ticket_id = str(ticket.get("id", "")).strip().lstrip("#")
    if ticket_id.isdigit():
        ticket_id = f"{int(ticket_id):02d}"
    if not ticket_id:
        raise ValueError("Ticket dictionary must contain a non-empty 'id'")

    ticket_path = str(ticket.get("path") or f".scratch/{feature_slug}/issues/{ticket_id}.md")
    context_anchor = str(ticket.get("context") or f".scratch/{feature_slug}/spec.md")
    seam = str(ticket.get("seam") or "Specified in ticket execution notes")
    verification = str(ticket.get("verification") or "npm test || pytest || python3 -m unittest")
    guardrails = str(ticket.get("guardrails") or "Strict TDD, zero any, host execution protocol")
    branch_name = branch or f"feat/spec-{feature_slug}"
    notes = shared_notes_path or f".scratch/{feature_slug}/exploration-notes.md"

    prompt = f"""<role_and_objective>
Implement ticket {ticket_id} adhering strictly to agents/spec-implementer.md and the vertical slice TDD Red-Green-Refactor loop.
</role_and_objective>

<context_pointers>
- Ticket: {ticket_path}
- Spec Section: {context_anchor}
- Branch: {branch_name}
- Shared Notes: {notes}
</context_pointers>

<architectural_seam>
{seam}
</architectural_seam>

<verification_gate>
Deterministic test command and compiler check: {verification}. Exit code 0 required.
</verification_gate>

<strict_guardrails>
- Zero tolerance on `any` and blind type assertions (`as TargetType`).
- No mocking internals; stub only external I/O boundaries.
- Given/When/Then Scenario Binding: Every scenario tagged in the ticket MUST map 1:1 to an automated behavior test.
- Ticket Mutation Prohibition: NEVER edit ticket files in .scratch/ or set Status: resolved. Only the Pipeline Coordinator executes resolve-ticket.sh in Step 5.
- Zero Cross-Conversation IPC: All communication flows strictly via native send_message; cross-conversation IPC is strictly prohibited.
- Subagents report stage completion without ticket-level resolution status.
- Prohibition of Intermediate Commits [PRUNE-01]: Subagents MUST NOT execute git add or git commit; report stage completion via send_message with changes staged or in the working tree without committing to HEAD.
- Anti-Lazy-Truncation: Full file replacements only.
- Terminal output hygiene: observe [HYG-01] (targeted test runs) and [HYG-02] (line-bounded file inspection).
- Host execution protocol: Run terminal commands with `BypassSandbox: true`.
- Guardrails summary: {guardrails}.
</strict_guardrails>

<completion_protocol>
Report via send_message strictly to Command Center:
{{
  "ticket_id": "{ticket_id}",
  "stage_status": "BUILDER_GREEN",
  "summary": "<1-2 sentence summary of implemented behavior>",
  "tests_added": ["<path-to-test-file>"]
}}
Include mandatory Skepticism Disclaimer and 3-Tier Verification Record (Deep, Shallow, Unverified).
</completion_protocol>"""

    return {
        "TypeName": type_name,
        "Role": f"Ticket Implementer ({ticket_id})",
        "Workspace": "inherit",
        "Prompt": prompt.strip(),
    }


def build_in_band_stage2_active_worker_brief(
    ticket: Dict[str, Any],
    stage1_report: Dict[str, Any],
    feature_slug: str,
    type_name: str = "self",
) -> Dict[str, Any]:
    """
    [SWARM-01] Generates Three-Tag Contract brief for Stage 2 Active Improvement Worker (code-reviewer)
    with pristine context window (0 prior tokens from Stage 1), break-and-fix directives,
    and strict send_message completion protocol without cross-conversation IPC.
    """
    if not isinstance(ticket, dict):
        raise TypeError(f"ticket must be a dictionary, got {type(ticket).__name__}")
    if not isinstance(stage1_report, dict):
        raise TypeError(f"stage1_report must be a dictionary, got {type(stage1_report).__name__}")
    if not feature_slug or not str(feature_slug).strip():
        raise ValueError("feature_slug must be a non-empty string")

    ticket_id = str(ticket.get("id", "")).strip().lstrip("#")
    if ticket_id.isdigit():
        ticket_id = f"{int(ticket_id):02d}"
    if not ticket_id:
        raise ValueError("Ticket dictionary must contain a non-empty 'id'")
    if not stage1_report:
        raise ValueError("stage1_report must be a non-empty dictionary")

    ticket_path = str(ticket.get("path") or f".scratch/{feature_slug}/issues/{ticket_id}.md")
    context_anchor = str(ticket.get("context") or f".scratch/{feature_slug}/spec.md")
    seam = str(ticket.get("seam") or "Specified in ticket execution notes")
    verification = str(ticket.get("verification") or "npm test || pytest || python3 -m unittest")

    stage1_summary = str(stage1_report.get("summary") or "Stage 1 Builder completed implementation.")
    tests_added = stage1_report.get("tests_added", [])
    tests_str = ", ".join(tests_added) if isinstance(tests_added, list) else str(tests_added)

    v_rec = stage1_report.get("verification_record")
    if isinstance(v_rec, dict):
        deep_v = v_rec.get("deep", [])
        shallow_v = v_rec.get("shallow", [])
        unverified_v = v_rec.get("unverified", [])
        v_rec_str = f"Deep: {deep_v}, Shallow: {shallow_v}, Unverified: {unverified_v}"
    elif v_rec is not None:
        v_rec_str = str(v_rec)
    else:
        v_rec_str = "Deep: [], Shallow: [], Unverified: []"

    prompt = f"""<role_and_objective>
Act as an Active Improvement Worker for ticket {ticket_id}. Understand independently, break the prior attempt, patch defects and code smells in-place on the active workspace, and re-verify the test suite.
</role_and_objective>

<original_task>
- Ticket: {ticket_path}
- Context & Seam: {context_anchor} | Seam: {seam}
- Verification: {verification}
- Guardrails: Strict TDD, zero any, host execution protocol
</original_task>

<prior_attempt>
- Builder Completion Summary: {stage1_summary}
- Tests Added by Builder: {tests_str}
- Modified Files: Inspect git diff in the workspace
- Skepticism Disclaimer & 3-Tier Record: {v_rec_str}
</prior_attempt>

<instructions>
A prior worker attempted this task. Its code changes are already present on disk, and its report is provided in the <prior_attempt> tag. Follow your Step 1–4 workflow ([IMPR-01]):
1. Understand independently: Comprehend the required public seam behavior from <original_task> without assuming the Stage 1 implementation is correct.
2. Break the prior attempt: Actively test hypotheses and write new edge-case tests to break the prior attempt, exposing unhandled boundary conditions, race conditions, type flaws (`any`, loose casts), or Fowler code smells (especially Smell #13 Obvious Comments).
3. Fix in-place: Directly modify code, tests, and types in-place on the active workspace (Workspace: "inherit") to remediate discovered defects.
4. Re-verify: Re-execute the test suite and compiler checks ({verification}) to confirm all tests pass cleanly with exit code 0 before reporting completion.

Passing constraints:
- Pass only structured markdown summaries and file pointers ([TAG-02]).
- Strictly prohibited from dumping raw terminal test runner outputs or verbose compiler logs.
</instructions>

<strict_guardrails>
- Zero tolerance on `any` and blind type assertions (`as TargetType`).
- No mocking internals; stub only external I/O boundaries.
- Anti-Lazy-Truncation: Full file replacements only.
- Clean comment discipline (Fowler Smell #13 ban): Never write "What-comments" repeating syntax or narrating code.
- Ticket Mutation Prohibition: NEVER edit ticket files in .scratch/ or set Status: resolved. Only the Pipeline Coordinator executes resolve-ticket.sh in Step 5.
- Zero Cross-Conversation IPC: All communication flows strictly via native send_message; cross-conversation IPC is strictly prohibited.
- Subagents report stage completion without ticket-level resolution status.
- Prohibition of Intermediate Commits [PRUNE-01]: Subagents MUST NOT execute git add or git commit; report stage completion via send_message with changes staged or in the working tree without committing to HEAD.
- Terminal output hygiene: observe [HYG-01] (targeted test runs) and [HYG-02] (line-bounded file inspection).
- Host execution protocol: Run terminal commands with `BypassSandbox: true`.
</strict_guardrails>

<completion_protocol>
Report via send_message strictly to Command Center adhering to structured failure lineage:
- What the prior attempt got wrong (Input, Expected, Actual)
- Summary of improvements and patches made
- Verification status and exit code 0
</completion_protocol>"""

    return {
        "TypeName": type_name,
        "Role": f"Active Improvement Worker ({ticket_id})",
        "Workspace": "inherit",
        "Prompt": prompt.strip(),
    }


def validate_coordinator_continuation(
    ticket_id: str,
    worker_report: Dict[str, Any],
    double_verify_exit_code: int,
    committed_to_head: bool,
) -> tuple[bool, str]:
    """
    [SWARM-02] Enforces Coordinator Continuation Mandate and Atomic Ticket Resolution.
    Receiving worker send_message report does NOT conclude the turn or resolve the ticket.
    The primary coordinator must independently run Double-Verification Gate (exit code 0 required)
    and commit verified changes to HEAD via scripts/resolve-ticket.sh.
    """
    clean_id = str(ticket_id or "").strip().lstrip("#")
    if clean_id.isdigit():
        clean_id = f"{int(clean_id):02d}"
    if not clean_id:
        return (
            False,
            "[SWARM-02] Coordinator continuation required: Invalid or empty ticket ID.",
        )

    if not isinstance(worker_report, dict) or not worker_report:
        return (
            False,
            f"[SWARM-02] Coordinator continuation required: Missing or invalid worker report for ticket {clean_id}.",
        )

    raw_report_tid = worker_report.get("ticket_id")
    if raw_report_tid is not None:
        report_tid = str(raw_report_tid).strip().lstrip("#")
        if report_tid.isdigit():
            report_tid = f"{int(report_tid):02d}"
        if report_tid and report_tid != clean_id:
            return (
                False,
                f"[SWARM-02] Coordinator continuation required: Worker report ticket ID '{report_tid}' does not match target ticket '{clean_id}'.",
            )

    status_val = str(worker_report.get("status", "")).strip().upper()
    stage_status_val = str(worker_report.get("stage_status", "")).strip().upper()
    error_val = worker_report.get("error")

    if error_val or status_val in ("FAILED", "ERROR") or stage_status_val in ("FAILED", "ERROR", "BUILDER_FAILED"):
        detail = error_val or status_val or stage_status_val
        return (
            False,
            f"[SWARM-02] Coordinator continuation required: Worker report indicates failure ({detail}). Ticket {clean_id} cannot be resolved.",
        )

    if double_verify_exit_code != 0:
        return (
            False,
            f"[SWARM-02] Coordinator continuation required: Double-Verification Gate failed with exit code {double_verify_exit_code}. Ticket {clean_id} cannot be resolved.",
        )

    if not committed_to_head:
        return (
            False,
            f"[SWARM-02] Coordinator continuation required: Double-Verification passed, but verified changes for ticket {clean_id} have not been committed to integration branch HEAD via scripts/resolve-ticket.sh.",
        )

    return (
        True,
        f"[SWARM-02] Ticket {clean_id} passed Double-Verification Gate with exit code 0 and committed to HEAD via scripts/resolve-ticket.sh.",
    )


def handle_subagent_error(
    git_root: "Path | str",
    ticket_id: str,
    stage: str,
    error_context: Dict[str, Any],
) -> Dict[str, Any]:
    """
    [ERR-SWARM-01] Safely recover from ephemeral subagent crash or git index lock contention.

    Inspects for a stale .git/index.lock, removes it without touching the working tree,
    then emits a structured diagnostic record describing the failure and whether a retry
    is eligible (at most 1 retry per ticket cycle).
    """
    root = Path(git_root).resolve()
    lock_path = root / ".git" / "index.lock"

    index_lock_removed = False
    if lock_path.is_file():
        try:
            lock_path.unlink()
            index_lock_removed = True
        except OSError:
            pass

    retry_count = int(error_context.get("retry_count", 0))
    retry_eligible = retry_count < 1

    clean_id = str(ticket_id).strip().lstrip("#")
    if clean_id.isdigit():
        clean_id = f"{int(clean_id):02d}"

    reason = str(error_context.get("reason", "unknown")).strip()
    lock_clause = " Stale index.lock detected and removed." if index_lock_removed else ""
    retry_clause = (
        " Stage will be retried (retry 1 of 1)."
        if retry_eligible
        else " Retry limit reached; verification circuit breaker tripped."
    )

    message = (
        f"[ERR-SWARM-01] Subagent error on ticket {clean_id} at stage {stage}: {reason}."
        f"{lock_clause}{retry_clause}"
    )

    return {
        "tag": "[ERR-SWARM-01]",
        "ticket_id": clean_id,
        "stage": stage,
        "retry_eligible": retry_eligible,
        "index_lock_removed": index_lock_removed,
        "message": message,
    }


def record_circuit_breaker_trip(
    scratch_dir: Path | str,
    breaker_type: int | str,
    reason: str,
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    [BREAKER-01] [BREAKER-02] [BREAKER-03] Atomically records a tripped circuit breaker in session.json.
    Halts autonomous execution, updates escalation state, and preserves session context.
    """
    scratch_path = _resolve_feature_scratch_dir(scratch_dir)
    session_file = scratch_path / "session.json"

    if session_file.is_file():
        try:
            session_data = json.loads(session_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            session_data = {
                "feature": scratch_path.name,
                "executionMode": "guarded-pipeline",
                "createdAt": _get_utc_now_iso(),
            }
    else:
        scratch_path.mkdir(parents=True, exist_ok=True)
        session_data = {
            "feature": scratch_path.name,
            "executionMode": "guarded-pipeline",
            "createdAt": _get_utc_now_iso(),
        }

    clean_breaker = int(breaker_type) if str(breaker_type).isdigit() else breaker_type

    circuit_breaker_payload: Dict[str, Any] = {
        "tripped": True,
        "breaker": clean_breaker,
        "reason": reason,
        "trippedAt": _get_utc_now_iso(),
        "prohibit_pr": details.get("prohibit_pr", True if clean_breaker == 3 else False) if details else (clean_breaker == 3),
        "details": details or {},
    }

    session_data["circuitBreaker"] = circuit_breaker_payload
    session_data["escalation"] = reason
    session_data["lastEscalation"] = reason
    session_data["updatedAt"] = _get_utc_now_iso()

    _atomic_write_json(session_file, session_data)
    return session_data


def evaluate_verification_circuit_breaker(
    scratch_dir: Optional[Path | str] = None,
    ticket_id: str = "",
    cycle_count: int = 1,
    test_exit_code: int = 0,
    compiler_exit_code: int = 0,
    failure_output: str = "",
) -> tuple[bool, Dict[str, Any], str]:
    """
    [BREAKER-01] Evaluates two-cycle verification failure circuit breaker (Circuit Breaker 1).
    Halts pipeline and records ESCALATION_VERIFICATION_FAILURE in session.json when
    cycle_count >= 2 and either test or compiler verification failed (exit code != 0).
    """
    clean_id = str(ticket_id or "unknown").strip().lstrip("#")
    if clean_id.isdigit():
        clean_id = f"{int(clean_id):02d}"

    failed = (test_exit_code != 0) or (compiler_exit_code != 0)
    if not failed:
        return (
            False,
            {
                "tripped": False,
                "ticket_id": clean_id,
                "cycle_count": cycle_count,
                "test_exit_code": test_exit_code,
                "compiler_exit_code": compiler_exit_code,
            },
            f"[BREAKER-01] Verification clean for ticket {clean_id}: tests and compiler exited 0.",
        )

    # Circuit Breaker 1 activates strictly after 2 consecutive cycles (Builder + Active Worker).
    if cycle_count >= 2:
        details: Dict[str, Any] = {
            "ticket_id": clean_id,
            "cycle_count": cycle_count,
            "test_exit_code": test_exit_code,
            "compiler_exit_code": compiler_exit_code,
            "failure_output": failure_output,
        }
        if scratch_dir:
            try:
                record_circuit_breaker_trip(
                    scratch_dir=scratch_dir,
                    breaker_type=1,
                    reason="ESCALATION_VERIFICATION_FAILURE",
                    details=details,
                )
            except Exception:
                pass
        diag = (
            f"🚨 [BREAKER-01] Circuit Breaker 1 Activated: Ticket {clean_id} failed Double-Verification Gate "
            f"after {cycle_count} consecutive cycles (test exit {test_exit_code}, compiler exit {compiler_exit_code}). "
            f"Autonomous pipeline halted. Escalation 'ESCALATION_VERIFICATION_FAILURE' recorded in session.json.\n"
            f"Failure Details:\n{failure_output}\n"
            f"Control surrendered to operator for manual investigation."
        )
        return (
            True,
            {
                "tripped": True,
                "breaker": 1,
                "reason": "ESCALATION_VERIFICATION_FAILURE",
                "ticket_id": clean_id,
                "cycle_count": cycle_count,
                "test_exit_code": test_exit_code,
                "compiler_exit_code": compiler_exit_code,
                "details": details,
            },
            diag,
        )

    return (
        False,
        {
            "tripped": False,
            "ticket_id": clean_id,
            "cycle_count": cycle_count,
            "test_exit_code": test_exit_code,
            "compiler_exit_code": compiler_exit_code,
            "retry_eligible": True,
        },
        f"[BREAKER-01] Ticket {clean_id} failed cycle {cycle_count}. Eligible for Stage 2 Active Worker patch.",
    )


def evaluate_git_collision_circuit_breaker(
    git_root: Path | str,
    scratch_dir: Optional[Path | str] = None,
    allow_dirty: bool = False,
) -> tuple[bool, Dict[str, Any], str]:
    """
    [BREAKER-02] Evaluates git merge collision, uncommitted dirty working tree, and index lock (Circuit Breaker 2).
    Halts immediately on conflict detection, preserves local workspace state, and prompts manual operator resolution.
    """
    root = Path(git_root).resolve()

    # Detect git index lock in both standard git repositories and isolated worktrees.
    lock_file = root / ".git" / "index.lock"
    if not lock_file.is_file():
        try:
            lock_proc = subprocess.run(
                ["git", "rev-parse", "--git-path", "index.lock"],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
            if lock_proc.returncode == 0 and lock_proc.stdout.strip():
                candidate = Path(lock_proc.stdout.strip())
                if not candidate.is_absolute():
                    candidate = root / candidate
                if candidate.is_file():
                    lock_file = candidate
        except OSError:
            pass

    if lock_file.is_file():
        details = {
            "type": "index_lock",
            "lock_file": str(lock_file),
            "message": f"Git index lock detected ({lock_file})",
        }
        if scratch_dir:
            try:
                record_circuit_breaker_trip(
                    scratch_dir=scratch_dir,
                    breaker_type=2,
                    reason="ESCALATION_GIT_COLLISION",
                    details=details,
                )
            except Exception:
                pass
        diag = (
            f"🚨 [BREAKER-02] Circuit Breaker 2 Activated: Git index lock detected at '{lock_file}'. "
            "Pipeline halted immediately. Workspace state preserved. "
            "Operator action required: verify no concurrent git processes and remove stale index lock."
        )
        return True, {"tripped": True, "breaker": 2, "collision_type": "index_lock", "details": details}, diag

    try:
        status_proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        if status_proc.returncode != 0:
            err_msg = status_proc.stderr.strip() or f"git status exited with code {status_proc.returncode}"
            details = {
                "type": "git_error",
                "message": err_msg,
            }
            if scratch_dir:
                try:
                    record_circuit_breaker_trip(
                        scratch_dir=scratch_dir,
                        breaker_type=2,
                        reason="ESCALATION_GIT_COLLISION",
                        details=details,
                    )
                except Exception:
                    pass
            diag = (
                f"🚨 [BREAKER-02] Circuit Breaker 2 Activated: Git error encountered in '{root}': {err_msg}. "
                "Pipeline halted immediately. Workspace state preserved. "
                "Operator action required: inspect git repository status."
            )
            return True, {"tripped": True, "breaker": 2, "collision_type": "git_error", "details": details}, diag

        porcelain_lines = [line for line in status_proc.stdout.splitlines() if line.strip()]

        # Git porcelain 2-character prefix detects all unmerged/conflict statuses (UU, AA, DD, AU, UA, UD, DU).
        unmerged_files = []
        for line in porcelain_lines:
            status_code = line[:2]
            if "U" in status_code or status_code in ("AA", "DD"):
                unmerged_files.append(line[3:].strip().strip('"'))

        if unmerged_files:
            details = {
                "type": "merge_conflict",
                "unmerged_files": unmerged_files,
                "message": f"Git merge conflict detected across {len(unmerged_files)} file(s)",
            }
            if scratch_dir:
                try:
                    record_circuit_breaker_trip(
                        scratch_dir=scratch_dir,
                        breaker_type=2,
                        reason="ESCALATION_GIT_COLLISION",
                        details=details,
                    )
                except Exception:
                    pass
            diag = (
                f"🚨 [BREAKER-02] Circuit Breaker 2 Activated: Unresolved git merge conflict detected in files: "
                f"{', '.join(unmerged_files)}. Pipeline halted immediately without speculative rebases. "
                "Workspace state preserved. Operator action required: resolve merge conflicts manually."
            )
            return True, {"tripped": True, "breaker": 2, "collision_type": "merge_conflict", "details": details}, diag

        if not allow_dirty and porcelain_lines:
            dirty_files = [line[3:].strip().strip('"') for line in porcelain_lines]
            details = {
                "type": "dirty_working_tree",
                "dirty_files": dirty_files,
                "message": f"Uncommitted working tree collision detected ({len(dirty_files)} modified file(s))",
            }
            if scratch_dir:
                try:
                    record_circuit_breaker_trip(
                        scratch_dir=scratch_dir,
                        breaker_type=2,
                        reason="ESCALATION_GIT_COLLISION",
                        details=details,
                    )
                except Exception:
                    pass
            diag = (
                f"🚨 [BREAKER-02] Circuit Breaker 2 Activated: Dirty working tree collision detected: "
                f"{len(dirty_files)} uncommitted file(s) present in workspace. "
                "Pipeline halted immediately. Workspace state preserved. "
                "Operator action required: commit or stash changes before continuing."
            )
            return True, {"tripped": True, "breaker": 2, "collision_type": "dirty_working_tree", "details": details}, diag
    except OSError as exc:
        details = {
            "type": "os_error",
            "message": str(exc),
        }
        if scratch_dir:
            try:
                record_circuit_breaker_trip(
                    scratch_dir=scratch_dir,
                    breaker_type=2,
                    reason="ESCALATION_GIT_COLLISION",
                    details=details,
                )
            except Exception:
                pass
        diag = (
            f"🚨 [BREAKER-02] Circuit Breaker 2 Activated: OS error executing git in '{root}': {exc}. "
            "Pipeline halted immediately. Workspace state preserved."
        )
        return True, {"tripped": True, "breaker": 2, "collision_type": "os_error", "details": details}, diag

    return False, {"tripped": False}, "Clean git working tree: no collisions or conflicts detected."


def evaluate_evaluation_circuit_breaker(
    scratch_dir: Optional[Path | str] = None,
    phase: str = "code-review",
    verdict: str = "FAIL",
    findings: Optional[list[Dict[str, Any] | str]] = None,
    raw_report: str = "",
) -> tuple[bool, Dict[str, Any], str]:
    """
    [BREAKER-03] Evaluates blocking evaluation findings circuit breaker (Circuit Breaker 3).
    Halts feature progression and prohibits pull request generation when post-implementation
    review or audit reports a FAIL verdict with un-remediated CRITICAL or HIGH findings.
    """
    clean_phase = str(phase or "code-review").strip().lower()
    clean_verdict = str(verdict or "FAIL").strip().upper()

    scratch_path: Optional[Path] = _resolve_feature_scratch_dir(scratch_dir) if scratch_dir else None

    if not raw_report and not findings and scratch_path:
        for candidate_name in (f"{clean_phase}.md", "security-audit.md", "code-review.md"):
            cand = scratch_path / candidate_name
            if cand.is_file():
                try:
                    raw_report = cand.read_text(encoding="utf-8")
                    break
                except OSError:
                    pass

    if raw_report and "\n" not in raw_report:
        report_path = Path(raw_report)
        if report_path.is_file():
            try:
                raw_report = report_path.read_text(encoding="utf-8")
            except OSError:
                pass

    if clean_verdict == "FAIL" and raw_report:
        m_verdict = re.search(r"(?:^|\n)[#*\s]*Verdict\s*:\s*([A-Za-z_-]+)", raw_report, re.IGNORECASE)
        if m_verdict:
            clean_verdict = m_verdict.group(1).strip().upper()

    blocking_findings: list[Dict[str, Any]] = []

    zero_desc_pat = re.compile(
        r"^(?:0|none|none\s+detected|no\s+findings|no\s+vulnerabilities|not\s+detected|n/a|nil|false|zero|0\s+findings)[\s.]*$",
        re.IGNORECASE,
    )
    negation_pat = re.compile(
        r"\b(?:no|zero|0)\s+(?:critical|high|blocking)\b",
        re.IGNORECASE,
    )
    raw_finding_pat = re.compile(
        r"^(?:[*\-#>]|\d+\.)*\s*(?:\[\s*|\*\*|__)?(?:severity\s*:\s*)?(CRITICAL|HIGH|BLOCKING)(?:\s*\]|\*\*|__)?\s*[:\-–—]?\s*(.*)$",
        re.IGNORECASE,
    )

    if findings:
        for item in findings:
            if isinstance(item, dict):
                sev = str(item.get("severity") or item.get("level") or item.get("priority") or "").strip().upper()
                if sev in ("CRITICAL", "HIGH", "BLOCKING"):
                    desc = str(item.get("description") or item.get("title") or item.get("message") or "").strip()
                    if desc and not zero_desc_pat.match(desc):
                        blocking_findings.append({
                            "severity": sev,
                            "description": desc,
                            "file": str(item.get("file") or item.get("path") or ""),
                        })
            elif isinstance(item, str):
                item_str = item.strip()
                if negation_pat.search(item_str):
                    continue
                m_match = raw_finding_pat.match(item_str)
                if m_match:
                    sev = m_match.group(1).upper()
                    desc = m_match.group(2).strip()
                    if desc.startswith("]"):
                        desc = desc.lstrip("] :-\t").strip()
                    if desc and not zero_desc_pat.match(desc):
                        blocking_findings.append({
                            "severity": sev,
                            "description": desc,
                        })
                else:
                    m_sev = re.search(r"\b(CRITICAL|HIGH|BLOCKING)\b", item_str, re.IGNORECASE)
                    if m_sev:
                        blocking_findings.append({
                            "severity": m_sev.group(1).upper(),
                            "description": item_str,
                        })

    if raw_report:
        lines = raw_report.splitlines()
        for line in lines:
            trimmed = line.strip()
            if not trimmed or negation_pat.search(trimmed):
                continue
            m_raw = raw_finding_pat.match(trimmed)
            if m_raw:
                sev = m_raw.group(1).upper()
                desc = m_raw.group(2).strip()
                if desc.startswith("]"):
                    desc = desc.lstrip("] :-\t").strip()
                if desc and not zero_desc_pat.match(desc):
                    blocking_findings.append({
                        "severity": sev,
                        "description": desc,
                    })

    has_blocking = len(blocking_findings) > 0
    failing_verdicts = {
        "FAIL", "FAILED", "REJECTED", "BLOCKING", "ACTION_REQUIRED", "BLOCKED",
        "NON_CONFORMANT", "VIOLATIONS_DETECTED",
    }
    is_failing_verdict = clean_verdict in failing_verdicts or (clean_verdict not in ("PASS", "PASSED", "APPROVED", "CLEAN", "CONFORMANT") and has_blocking)

    if is_failing_verdict and has_blocking:
        details = {
            "phase": clean_phase,
            "verdict": clean_verdict,
            "blocking_findings": blocking_findings,
            "blocking_count": len(blocking_findings),
            "prohibit_pr": True,
        }
        if scratch_path:
            try:
                record_circuit_breaker_trip(
                    scratch_dir=scratch_path,
                    breaker_type=3,
                    reason="ESCALATION_BLOCKING_FINDINGS",
                    details=details,
                )
            except Exception:
                pass

        findings_bullets = "\n".join(
            f"  - [{f.get('severity')}] {f.get('description')}" + (f" ({f.get('file')})" if f.get('file') else "")
            for f in blocking_findings
        )
        diag = (
            f"🚨 [BREAKER-03] Circuit Breaker 3 Activated: Un-remediated CRITICAL/HIGH findings detected in {clean_phase} "
            f"(verdict: {clean_verdict}). Pipeline progression halted. Pull request generation (gh pr create) prohibited. "
            f"Escalation 'ESCALATION_BLOCKING_FINDINGS' recorded in session.json.\n"
            f"Blocking Findings ({len(blocking_findings)}):\n{findings_bullets}\n"
            f"Control surrendered to operator for manual remediation."
        )
        return (
            True,
            {
                "tripped": True,
                "breaker": 3,
                "reason": "ESCALATION_BLOCKING_FINDINGS",
                "phase": clean_phase,
                "verdict": clean_verdict,
                "blocking_count": len(blocking_findings),
                "blocking_findings": blocking_findings,
                "prohibit_pr": True,
                "details": details,
            },
            diag,
        )

    if clean_verdict in ("PASS", "PASSED", "APPROVED", "CLEAN", "CONFORMANT"):
        return (
            False,
            {
                "tripped": False,
                "phase": clean_phase,
                "verdict": clean_verdict,
                "blocking_count": 0,
            },
            f"[BREAKER-03] Clean evaluation pass: {clean_phase} passed with verdict {clean_verdict}.",
        )

    return (
        False,
        {
            "tripped": False,
            "phase": clean_phase,
            "verdict": clean_verdict,
            "blocking_count": len(blocking_findings),
        },
        f"[BREAKER-03] Evaluation verdict {clean_verdict} contains no un-remediated CRITICAL or HIGH severity findings.",
    )


def is_pr_generation_prohibited(scratch_dir: Path | str) -> tuple[bool, str]:
    """
    [BREAKER-03] Evaluates whether pull request generation is prohibited due to
    an active circuit breaker trip (specifically Circuit Breaker 3 or any tripped breaker).
    """
    scratch_path = _resolve_feature_scratch_dir(scratch_dir)
    session_file = scratch_path / "session.json"
    if not session_file.is_file():
        return False, "No active session anchor found; PR generation not prohibited."

    try:
        session_data = json.loads(session_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return True, f"Pull request generation prohibited: Session anchor at '{session_file}' is invalid or unreadable."

    cb = session_data.get("circuitBreaker", {})
    escalation = session_data.get("escalation", "")
    if cb.get("tripped") or escalation == "ESCALATION_BLOCKING_FINDINGS":
        breaker_num = cb.get("breaker", 3 if escalation == "ESCALATION_BLOCKING_FINDINGS" else 0)
        reason = cb.get("reason", escalation or "CIRCUIT_BREAKER_TRIPPED")
        if breaker_num == 3 or cb.get("prohibit_pr") or escalation == "ESCALATION_BLOCKING_FINDINGS":
            return (
                True,
                f"Pull request generation prohibited: Circuit Breaker 3 is active due to un-remediated CRITICAL/HIGH evaluation findings ({reason}).",
            )
        return (
            True,
            f"Pull request generation prohibited: Circuit Breaker {breaker_num} is active ({reason}).",
        )

    return False, "Pull request generation is eligible."


def record_circuit_breaker_resumption(
    scratch_dir: Path | str,
    notes: str = "",
) -> Dict[str, Any]:
    """
    [EDGE-BREAKER-01] Atomically records idempotent circuit breaker resumption in session.json.
    Resets tripped flag, clears active escalation, preserves audit lineage and resolved tickets.
    """
    scratch_path = _resolve_feature_scratch_dir(scratch_dir)
    session_file = scratch_path / "session.json"

    if not session_file.is_file():
        init_data = init_execution_state(scratch_path, "resumption")
        return init_data

    try:
        session_data = json.loads(session_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        session_data = {
            "feature": scratch_path.name,
            "executionMode": "guarded-pipeline",
            "createdAt": _get_utc_now_iso(),
        }

    cb = session_data.get("circuitBreaker", {})
    prior_breaker = cb.get("breaker", 0)
    prior_reason = cb.get("reason", session_data.get("escalation", ""))

    cb["tripped"] = False
    cb["resumed"] = True
    cb["resumedAt"] = _get_utc_now_iso()
    cb["prohibit_pr"] = False
    session_data["circuitBreaker"] = cb

    session_data.pop("escalation", None)

    resumption_payload = {
        "resumed": True,
        "resumedAt": _get_utc_now_iso(),
        "priorBreaker": prior_breaker,
        "priorReason": prior_reason,
        "notes": notes,
    }
    session_data["resumption"] = resumption_payload

    history = session_data.setdefault("resumptionHistory", [])
    if isinstance(history, list):
        history.append(dict(resumption_payload))

    session_data["updatedAt"] = _get_utc_now_iso()

    _atomic_write_json(session_file, session_data)
    return session_data


def resume_guarded_pipeline(
    scratch_dir: Path | str,
    git_root: Optional[Path | str] = None,
    allow_dirty: bool = False,
    notes: str = "",
) -> tuple[bool, Dict[str, Any], list[str]]:
    """
    [EDGE-BREAKER-01] Resumes guarded autonomous pipeline execution following a circuit breaker halt.
    Validates workspace hygiene, updates session resumption state, and resumes draining strictly
    from unresolved frontier tickets without re-executing resolved tickets.
    """
    target_dir = _resolve_feature_scratch_dir(scratch_dir)

    root_to_check: Optional[Path] = Path(git_root).resolve() if git_root else None
    if not root_to_check:
        cur = target_dir
        while cur != cur.parent:
            if (cur / ".git").exists():
                root_to_check = cur
                break
            cur = cur.parent

    if not allow_dirty and root_to_check and (root_to_check / ".git").exists():
        tripped, collision_payload, diag = evaluate_git_collision_circuit_breaker(
            git_root=root_to_check,
            scratch_dir=target_dir,
            allow_dirty=False,
        )
        if tripped:
            return False, collision_payload, [diag]

    valid, session_data, diags = validate_session_anchor(target_dir)
    if not valid:
        return False, {}, diags

    resumed_session = record_circuit_breaker_resumption(target_dir, notes=notes)

    frontier = compute_sequential_frontier(target_dir)
    diags_out = [
        f"[EDGE-BREAKER-01] Guarded pipeline successfully resumed for '{target_dir.name}'.",
        f"Prior breaker cleared. Unresolved frontier tickets to drain: {frontier}.",
    ]
    return True, resumed_session, diags_out


def parse_operator_input(val: Any) -> Optional[bool]:
    """Normalize boolean or string operator responses for gate transitions."""
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        v = val.strip().lower()
        if v in ("true", "1", "yes", "y", "approve", "approved", "ok", "confirm"):
            return True
        if v in ("false", "0", "no", "n", "reject", "rejected", "deny", "halt", "quit", "q"):
            return False
    return None


def prompt_operator_checkpoint_1(
    prompt_text: str = "Checkpoint 1 Sign-Off: Approve advancement to review and audit? [y/N]: ",
    input_func: Optional[Callable[[str], str]] = None,
    max_attempts: int = 3,
) -> Optional[bool]:
    """
    Prompt operator interactively for Checkpoint 1 sign-off.
    Gracefully handles EOFError and KeyboardInterrupt with a clean False (rejection/halt).
    """
    fn = input_func if input_func is not None else input
    current_prompt = prompt_text
    for _ in range(max_attempts):
        try:
            raw = fn(current_prompt)
            parsed = parse_operator_input(raw)
            if parsed is not None:
                return parsed
            current_prompt = "Please enter 'yes' (y) to approve or 'no' (n) to reject: "
        except (EOFError, KeyboardInterrupt):
            # Non-interactive stdin or SIGINT terminates advancement cleanly.
            return False
    return None


def evaluate_checkpoint_1_post_implementation(
    scratch_dir: Path | str,
    git_root: Optional[Path | str] = None,
    base_ref: Optional[str] = None,
    operator_approved: Optional[bool | str] = None,
    interactive: bool = False,
    prompt_func: Optional[Callable[[str], str]] = None,
    auto_advance: bool = False,
) -> tuple[bool, Dict[str, Any], str]:
    """
    [CHECK-01] [EDGE-CHECK-01] Evaluates Checkpoint 1 (Post-Implementation Gate).
    Triggers when 100% of DAG tickets reach Status: resolved.
    Displays cumulative diff statistics and pauses for operator sign-off before advancing
    to /code-review and /security-audit.
    Gracefully handles operator rejection by halting execution cleanly, preserving
    workspace state and task graph progress, and displaying instructions for inspection
    or running /implement <NN> hotfixes.
    """
    scratch_path = _resolve_feature_scratch_dir(scratch_dir)
    tickets = scan_scratch_tickets(scratch_path)
    if not tickets:
        return False, {
            "checkpoint": 1,
            "status": "NO_TICKETS",
            "dag_complete": False,
            "total_tickets": 0,
            "resolved_tickets": 0,
        }, "No tickets found in scratch directory."

    total_tickets = len(tickets)
    resolved_tickets = [t for t in tickets if is_ticket_resolved(str(t.get("status", "")))]
    dag_complete = (len(resolved_tickets) == total_tickets and total_tickets > 0)

    if not dag_complete:
        remaining = [t.get("id") for t in tickets if not is_ticket_resolved(str(t.get("status", "")))]
        payload = {
            "checkpoint": 1,
            "status": "DAG_INCOMPLETE",
            "dag_complete": False,
            "total_tickets": total_tickets,
            "resolved_tickets": len(resolved_tickets),
            "remaining_tickets": remaining,
        }
        diag = f"DAG implementation in progress: {len(resolved_tickets)}/{total_tickets} tickets resolved. Checkpoint 1 inactive."
        return False, payload, diag

    root: Optional[Path] = Path(git_root).resolve() if git_root else None
    if not root:
        cur = scratch_path
        while cur != cur.parent:
            if (cur / ".git").exists():
                root = cur
                break
            cur = cur.parent
        if not root and Path(".git").exists():
            root = Path.cwd().resolve()

    diff_summary = ""
    untracked_files: list[str] = []
    uncommitted_changes = False
    git_error_detail = ""

    if root and (root / ".git").exists():
        try:
            status_proc = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=root,
                capture_output=True,
                text=True,
            )
            if status_proc.returncode == 0 and status_proc.stdout.strip():
                for line in status_proc.stdout.splitlines():
                    cleaned_line = line.strip()
                    if cleaned_line.startswith("??"):
                        untracked_files.append(cleaned_line[2:].strip())
                    elif cleaned_line:
                        uncommitted_changes = True
        except (subprocess.SubprocessError, OSError):
            pass

        target_base = base_ref.strip() if (base_ref and str(base_ref).strip()) else None
        if target_base and (target_base.startswith("-") or not re.match(r"^[A-Za-z0-9_./@~^{}]+$", target_base)):
            target_base = None
        if not target_base:
            session_file = scratch_path / "session.json"
            if session_file.is_file():
                try:
                    sdata = json.loads(session_file.read_text(encoding="utf-8"))
                    cand_base = sdata.get("baseBranch") or sdata.get("baseRef")
                    if cand_base and str(cand_base).strip():
                        target_base = str(cand_base).strip()
                        if target_base and (target_base.startswith("-") or not re.match(r"^[A-Za-z0-9_./@~^{}]+$", target_base)):
                            target_base = None
                except Exception:
                    pass

        if not target_base:
            try:
                branch_proc = subprocess.run(["git", "branch"], cwd=root, capture_output=True, text=True)
                if branch_proc.returncode == 0:
                    branches = [b.strip("* ").strip() for b in branch_proc.stdout.splitlines()]
                    for cand in ("master", "main", "trunk", "develop", "dev"):
                        if cand in branches:
                            target_base = cand
                            break
                    if not target_base and branches:
                        target_base = branches[0]
            except (subprocess.SubprocessError, OSError):
                pass

        if not target_base:
            target_base = "main"

        try:
            diff_proc = subprocess.run(
                ["git", "diff", "--stat", f"{target_base}...HEAD"],
                cwd=root,
                capture_output=True,
                text=True,
            )
            if diff_proc.returncode == 0:
                diff_summary = diff_proc.stdout.strip()
            else:
                diff_proc2 = subprocess.run(
                    ["git", "diff", "--stat", target_base],
                    cwd=root,
                    capture_output=True,
                    text=True,
                )
                if diff_proc2.returncode == 0:
                    diff_summary = diff_proc2.stdout.strip()
                else:
                    diff_proc3 = subprocess.run(
                        ["git", "diff", "--stat", f"HEAD~{len(resolved_tickets)}"],
                        cwd=root,
                        capture_output=True,
                        text=True,
                    )
                    if diff_proc3.returncode == 0:
                        diff_summary = diff_proc3.stdout.strip()
                    else:
                        root_rev_proc = subprocess.run(
                            ["git", "rev-list", "--max-parents=0", "HEAD"],
                            cwd=root,
                            capture_output=True,
                            text=True,
                        )
                        if root_rev_proc.returncode == 0 and root_rev_proc.stdout.strip():
                            root_sha = root_rev_proc.stdout.splitlines()[-1].strip()
                            diff_proc4 = subprocess.run(
                                ["git", "diff", "--stat", root_sha, "HEAD"],
                                cwd=root,
                                capture_output=True,
                                text=True,
                            )
                            if diff_proc4.returncode == 0:
                                diff_summary = diff_proc4.stdout.strip()
                            else:
                                git_error_detail = diff_proc.stderr.strip() or diff_proc3.stderr.strip() or "git diff command failed"
                        else:
                            git_error_detail = diff_proc.stderr.strip() or diff_proc3.stderr.strip() or f"unknown base revision '{target_base}'"
        except (subprocess.SubprocessError, OSError) as e:
            git_error_detail = str(e)

    if git_error_detail:
        diff_summary = f"Diff statistics unavailable (git diff error: {git_error_detail})."
    elif not diff_summary:
        diff_summary = "(No file diff detected against base ref)"

    if untracked_files:
        diff_summary += f"\n\nUntracked files ({len(untracked_files)} uncommitted):\n" + "\n".join(f"  ?? {u}" for u in untracked_files)

    decision = parse_operator_input(operator_approved)
    if decision is None and interactive:
        decision = prompt_operator_checkpoint_1(input_func=prompt_func)

    session_file = scratch_path / "session.json"
    session_data: Dict[str, Any] = {}
    if session_file.is_file():
        try:
            session_data = json.loads(session_file.read_text(encoding="utf-8"))
        except Exception:
            session_data = {}

    if auto_advance:
        session_data["checkpoint1"] = {
            "status": "AUTO_ADVANCED",
            "autoAdvanced": True,
            "nextPhase": "code-review",
            "updatedAt": _get_utc_now_iso(),
        }
        _atomic_write_json(session_file, session_data, ignore_errors=True)

        payload = {
            "checkpoint": 1,
            "status": "AUTO_ADVANCED",
            "dag_complete": True,
            "advance_permitted": True,
            "auto_advance": True,
            "next_phase": "code-review",
            "security_audit_required": True,
            "spec_reconciliation_required": True,
            "next_checkpoint": 2,
            "diff_summary": diff_summary,
            "untracked_files": untracked_files,
            "uncommitted_changes": uncommitted_changes,
        }
        diag = (
            "🚀 [CHECK-02] Checkpoint 1: Autonomous Advancement (--auto)\n"
            f"100% DAG tickets completed ({len(resolved_tickets)}/{total_tickets} resolved).\n"
            "Advancing past Checkpoint 1 without pausing to execute Two-Axis Code Review (/code-review).\n"
            "Pipeline cruise control active: /code-review -> /security-audit -> living spec reconciliation -> Checkpoint 2 (Pre-PR Release Gate)."
        )
        return False, payload, diag

    if decision is False:
        session_data["checkpoint1"] = {
            "status": "OPERATOR_REJECTED",
            "operatorApproved": False,
            "updatedAt": _get_utc_now_iso(),
        }
        _atomic_write_json(session_file, session_data, ignore_errors=True)

        instructions = (
            "Execution halted cleanly per operator rejection at Checkpoint 1.\n"
            "Integration branch HEAD and verified commits are preserved intact.\n"
            "Options:\n"
            "1. Inspect changes manually: git diff <base_ref> or git log -n 5\n"
            "2. Execute targeted single-ticket hotfixes: /implement <NN>\n"
            "3. Re-launch pipeline when ready: /implement-spec"
        )
        payload = {
            "checkpoint": 1,
            "status": "OPERATOR_REJECTED",
            "dag_complete": True,
            "advance_permitted": False,
            "workspace_preserved": True,
            "instructions": instructions,
            "untracked_files": untracked_files,
            "uncommitted_changes": uncommitted_changes,
        }
        diag = (
            "🛑 [EDGE-CHECK-01] Checkpoint 1: Operator Rejection\n"
            "Pipeline halted cleanly without advancing to /code-review or /security-audit.\n"
            "Integration branch HEAD intact; verified ticket commits and task graph progress preserved.\n\n"
            f"{instructions}"
        )
        return False, payload, diag

    if decision is True:
        session_data["checkpoint1"] = {
            "status": "OPERATOR_APPROVED",
            "operatorApproved": True,
            "updatedAt": _get_utc_now_iso(),
        }
        _atomic_write_json(session_file, session_data, ignore_errors=True)

        payload = {
            "checkpoint": 1,
            "status": "OPERATOR_APPROVED",
            "dag_complete": True,
            "advance_permitted": True,
            "next_phase": "code-review",
            "untracked_files": untracked_files,
            "uncommitted_changes": uncommitted_changes,
        }
        diag = (
            "✅ [CHECK-01] Checkpoint 1 Sign-Off Confirmed\n"
            "Operator sign-off received. Advancing pipeline to /code-review and /security-audit."
        )
        return False, payload, diag

    session_data["checkpoint1"] = {
        "status": "PAUSED_AWAITING_SIGNOFF",
        "totalTickets": total_tickets,
        "resolvedTickets": len(resolved_tickets),
        "updatedAt": _get_utc_now_iso(),
    }
    _atomic_write_json(session_file, session_data, ignore_errors=True)

    payload = {
        "checkpoint": 1,
        "status": "PAUSED_AWAITING_SIGNOFF",
        "dag_complete": True,
        "total_tickets": total_tickets,
        "resolved_tickets": len(resolved_tickets),
        "diff_summary": diff_summary,
        "untracked_files": untracked_files,
        "uncommitted_changes": uncommitted_changes,
        "prompt": (
            "Checkpoint 1 (Post-Implementation Gate): 100% DAG completed. "
            "Operator sign-off requested before advancing to /code-review and /security-audit."
        ),
    }
    diag = (
        "⏸️ [CHECK-01] Checkpoint 1: Post-Implementation Gate\n"
        f"100% DAG tickets completed ({len(resolved_tickets)}/{total_tickets} resolved).\n\n"
        "Cumulative Diff Summary:\n"
        f"{diff_summary}\n\n"
        "Action Required: Operator sign-off requested before advancing to /code-review and /security-audit."
    )
    return True, payload, diag


def evaluate_checkpoint_2_pre_pr(
    scratch_dir: Path | str,
    pr_title: Optional[str] = None,
    pr_body: Optional[str] = None,
    operator_approved: Optional[bool | str] = None,
) -> tuple[bool, Dict[str, Any], str]:
    """
    [CHECK-02] Checkpoint 2: Pre-PR Release Gate.
    Pauses pipeline execution awaiting final operator sign-off before executing 'gh pr create'.
    """
    scratch_path = _resolve_feature_scratch_dir(scratch_dir)
    decision = parse_operator_input(operator_approved)
    session_file = scratch_path / "session.json"
    session_data: Dict[str, Any] = {}
    if session_file.is_file():
        try:
            session_data = json.loads(session_file.read_text(encoding="utf-8"))
        except Exception:
            session_data = {}

    if decision is True:
        session_data["checkpoint2"] = {
            "status": "OPERATOR_APPROVED",
            "operatorApproved": True,
            "updatedAt": _get_utc_now_iso(),
        }
        _atomic_write_json(session_file, session_data, ignore_errors=True)
        payload = {
            "checkpoint": 2,
            "status": "OPERATOR_APPROVED",
            "ready_for_pr": True,
            "advance_permitted": True,
            "pr_title": pr_title or "",
            "pr_body": pr_body or "",
        }
        diag = (
            "✅ [CHECK-02] Checkpoint 2 Sign-Off Confirmed\n"
            "Operator release approval received. Ready for pull request publication (/pr or 'gh pr create')."
        )
        return False, payload, diag

    if decision is False:
        session_data["checkpoint2"] = {
            "status": "OPERATOR_REJECTED",
            "operatorApproved": False,
            "updatedAt": _get_utc_now_iso(),
        }
        _atomic_write_json(session_file, session_data, ignore_errors=True)
        payload = {
            "checkpoint": 2,
            "status": "OPERATOR_REJECTED",
            "ready_for_pr": False,
            "advance_permitted": False,
        }
        diag = (
            "🛑 Checkpoint 2: Operator Rejection\n"
            "Pre-PR release sign-off rejected by operator. Pull request generation halted."
        )
        return False, payload, diag

    session_data["checkpoint2"] = {
        "status": "PAUSED_AWAITING_PR_SIGNOFF",
        "updatedAt": _get_utc_now_iso(),
    }
    _atomic_write_json(session_file, session_data, ignore_errors=True)

    payload = {
        "checkpoint": 2,
        "status": "PAUSED_AWAITING_PR_SIGNOFF",
        "ready_for_pr": False,
        "advance_permitted": False,
        "pr_title": pr_title or "",
        "pr_body": pr_body or "",
        "prompt": "Checkpoint 2 (Pre-PR Release Gate): Review, audit, and spec reconciliation complete. Operator confirmation requested before 'gh pr create'.",
    }
    diag = (
        "⏸️ [CHECK-02] Checkpoint 2: Pre-PR Release Gate\n"
        "Autonomous pipeline advancement complete (Two-Axis Review, Security Audit, Spec Reconciliation passed).\n"
        "Paused awaiting final operator confirmation before pull request publication ('gh pr create')."
    )
    return True, payload, diag


def main() -> None:

    parser = argparse.ArgumentParser(description="Ticket Execution State Machine Helper")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Initialize execution-state.json")
    init_parser.add_argument("scratch_dir", help="Path to feature scratch directory")
    init_parser.add_argument("ticket_id", help="Ticket ID (e.g. 02)")

    trans_parser = subparsers.add_parser("transition", help="Transition activeStage")
    trans_parser.add_argument("scratch_dir", help="Path to feature scratch directory")
    trans_parser.add_argument("next_stage", choices=VALID_STAGES, help="Target active stage")

    complete_parser = subparsers.add_parser("complete", help="Mark 5_commit_and_resolve as COMPLETED")
    complete_parser.add_argument("scratch_dir", help="Path to feature scratch directory")

    show_parser = subparsers.add_parser("show", help="Display current execution state")
    show_parser.add_argument("scratch_dir", help="Path to feature scratch directory")

    ingest_parser = subparsers.add_parser("ingest", help="Ingest session and initialize guarded pipeline")
    ingest_parser.add_argument("scratch_dir", help="Path to feature scratch directory")
    ingest_parser.add_argument("--allow-dirty", action="store_true", help="Allow dirty working tree")

    validate_parser = subparsers.add_parser("validate", help="Validate session anchor and task graph schema")
    validate_parser.add_argument("scratch_dir", help="Path to feature scratch directory")

    frontier_parser = subparsers.add_parser("frontier", help="Compute active unblocked frontier tickets")
    frontier_parser.add_argument("scratch_dir", help="Path to feature scratch directory")

    cb_parser = subparsers.add_parser("circuit-breaker", help="Evaluate or record circuit breaker trip")
    cb_parser.add_argument("scratch_dir", help="Path to feature scratch directory")
    cb_parser.add_argument("--breaker", choices=["1", "2", "3"], default="1", help="Circuit breaker tier")
    cb_parser.add_argument("--ticket", default="", help="Ticket ID")
    cb_parser.add_argument("--cycles", type=int, default=2, help="Failed cycle count")
    cb_parser.add_argument("--test-exit", type=int, default=1, help="Test exit code")
    cb_parser.add_argument("--compiler-exit", type=int, default=0, help="Compiler exit code")
    cb_parser.add_argument("--output", default="", help="Failure output trace")
    cb_parser.add_argument("--git-root", default=".", help="Path to git root for breaker 2")
    cb_parser.add_argument("--allow-dirty", action="store_true", help="Allow dirty working tree for breaker 2")
    cb_parser.add_argument("--phase", default="code-review", help="Evaluation phase for breaker 3")
    cb_parser.add_argument("--verdict", default="FAIL", help="Audit verdict for breaker 3")
    cb_parser.add_argument("--findings", default="", help="JSON string or finding description for breaker 3")
    cb_parser.add_argument("--report", default="", help="Raw report text for breaker 3")

    resume_parser = subparsers.add_parser("resume", help="Resume pipeline from circuit breaker trip")
    resume_parser.add_argument("scratch_dir", help="Path to feature scratch directory")
    resume_parser.add_argument("--git-root", default=None, help="Path to git root repository")
    resume_parser.add_argument("--allow-dirty", action="store_true", help="Allow dirty working tree")
    resume_parser.add_argument("--notes", default="", help="Resumption notes")

    cp1_parser = subparsers.add_parser("checkpoint-1", help="Evaluate Checkpoint 1 post-implementation gate")
    cp1_parser.add_argument("scratch_dir", help="Path to feature scratch directory")
    cp1_parser.add_argument("--git-root", default=None, help="Path to git root repository")
    cp1_parser.add_argument("--base-ref", default=None, help="Base git ref for diff summary")
    cp1_group = cp1_parser.add_mutually_exclusive_group()
    cp1_group.add_argument("--approve", action="store_true", help="Operator approve sign-off")
    cp1_group.add_argument("--reject", action="store_true", help="Operator reject advancement")
    cp1_group.add_argument("--interactive", action="store_true", help="Prompt operator interactively for sign-off")
    cp1_group.add_argument("--auto", action="store_true", help="Autonomous advancement bypassing pause")

    cp2_parser = subparsers.add_parser("checkpoint-2", help="Evaluate Checkpoint 2 pre-PR release gate")
    cp2_parser.add_argument("scratch_dir", help="Path to feature scratch directory")
    cp2_parser.add_argument("--title", default="", help="PR title")
    cp2_parser.add_argument("--body", default="", help="PR body")
    cp2_group = cp2_parser.add_mutually_exclusive_group()
    cp2_group.add_argument("--approve", action="store_true", help="Operator approve release")
    cp2_group.add_argument("--reject", action="store_true", help="Operator reject release")

    flags_parser = subparsers.add_parser("flags", help="Validate and parse execution flags (--auto, --step)")
    flags_parser.add_argument("--auto", action="store_true", help="Full autonomous pipeline advancement")
    flags_parser.add_argument("--step", action="store_true", help="Single-step ticket execution")

    args = parser.parse_args()

    if args.command == "init":
        state = init_execution_state(args.scratch_dir, args.ticket_id)
        print(json.dumps(state, indent=2))
    elif args.command == "transition":
        state = transition_execution_state(args.scratch_dir, args.next_stage)
        print(json.dumps(state, indent=2))
    elif args.command == "complete":
        state = complete_execution_state(args.scratch_dir)
        print(json.dumps(state, indent=2))
    elif args.command == "show":
        state = load_execution_state(args.scratch_dir)
        if not state:
            sys.exit(1)
        print(json.dumps(state, indent=2))
    elif args.command == "ingest":
        success, state, diag = init_guarded_pipeline(args.scratch_dir, allow_dirty=args.allow_dirty)
        if not success:
            for d in diag:
                print(d, file=sys.stderr)
            sys.exit(1)
        print(json.dumps(state, indent=2))
    elif args.command == "validate":
        success, state, diag = validate_session_anchor(args.scratch_dir)
        if not success:
            for d in diag:
                print(d, file=sys.stderr)
            sys.exit(1)
        print(json.dumps(state, indent=2))
    elif args.command == "frontier":
        try:
            frontier = compute_sequential_frontier(args.scratch_dir)
            print(json.dumps(frontier, indent=2))
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(1)
    elif args.command == "circuit-breaker":
        if args.breaker == "1":
            tripped, payload, diag = evaluate_verification_circuit_breaker(
                scratch_dir=args.scratch_dir,
                ticket_id=args.ticket,
                cycle_count=args.cycles,
                test_exit_code=args.test_exit,
                compiler_exit_code=args.compiler_exit,
                failure_output=args.output,
            )
            print(diag)
            print(json.dumps(payload, indent=2))
            if tripped:
                sys.exit(1)
        elif args.breaker == "2":
            tripped, payload, diag = evaluate_git_collision_circuit_breaker(
                git_root=args.git_root,
                scratch_dir=args.scratch_dir,
                allow_dirty=args.allow_dirty,
            )
            print(diag)
            print(json.dumps(payload, indent=2))
            if tripped:
                sys.exit(1)
        elif args.breaker == "3":
            findings_data = []
            if args.findings:
                try:
                    loaded = json.loads(args.findings)
                    if isinstance(loaded, list):
                        findings_data = loaded
                    elif isinstance(loaded, dict):
                        findings_data = [loaded]
                    else:
                        findings_data = [str(loaded)]
                except json.JSONDecodeError:
                    findings_data = [args.findings]
            tripped, payload, diag = evaluate_evaluation_circuit_breaker(
                scratch_dir=args.scratch_dir,
                phase=args.phase,
                verdict=args.verdict,
                findings=findings_data,
                raw_report=args.report,
            )
            print(diag)
            print(json.dumps(payload, indent=2))
            if tripped:
                sys.exit(1)
    elif args.command == "resume":
        success, state, diags = resume_guarded_pipeline(
            scratch_dir=args.scratch_dir,
            git_root=args.git_root,
            allow_dirty=args.allow_dirty,
            notes=args.notes,
        )
        for d in diags:
            print(d)
        print(json.dumps(state, indent=2))
        if not success:
            sys.exit(1)
    elif args.command == "checkpoint-1":
        operator_approved = True if args.approve else (False if args.reject else None)
        paused, payload, diag = evaluate_checkpoint_1_post_implementation(
            scratch_dir=args.scratch_dir,
            git_root=args.git_root,
            base_ref=args.base_ref,
            operator_approved=operator_approved,
            interactive=args.interactive,
            auto_advance=args.auto,
        )
        print(diag)
        print(json.dumps(payload, indent=2))
        if paused or payload.get("status") == "OPERATOR_REJECTED":
            sys.exit(0)
    elif args.command == "checkpoint-2":
        operator_approved = True if args.approve else (False if args.reject else None)
        paused, payload, diag = evaluate_checkpoint_2_pre_pr(
            scratch_dir=args.scratch_dir,
            pr_title=args.title,
            pr_body=args.body,
            operator_approved=operator_approved,
        )
        print(diag)
        print(json.dumps(payload, indent=2))
        if paused or payload.get("status") == "OPERATOR_REJECTED":
            sys.exit(0)
    elif args.command == "flags":
        ok, flags, diag = validate_execution_flags(auto=args.auto, step=args.step)
        if not ok:
            print(diag, file=sys.stderr)
            sys.exit(1)
        print(json.dumps(flags, indent=2))
        print(diag)


if __name__ == "__main__":
    main()

