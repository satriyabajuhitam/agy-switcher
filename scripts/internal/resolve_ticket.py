#!/usr/bin/env python3
"""
scripts/internal/resolve_ticket.py
Core execution engine for Atomic Ticket Resolution CLI Helper.
Zero third-party dependencies; implements [RES-01], [RES-02], and [PRUNE-01].
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Iterator, Mapping, NamedTuple, Sequence

# Ensure repository root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from scripts.internal.verify_integrity import (
        ManifestValidator,
        parse_frontmatter,
        FrontmatterParseError,
        validate_task_graph_template_contract,
        DEFAULT_TASK_GRAPH_TEMPLATE,
        load_task_graph_template,
    )
except ImportError:
    try:
        from verify_integrity import (  # type: ignore[no-redef]
            ManifestValidator,
            parse_frontmatter,
            FrontmatterParseError,
            validate_task_graph_template_contract,
            DEFAULT_TASK_GRAPH_TEMPLATE,
            load_task_graph_template,
        )
    except ImportError:
        ManifestValidator = None  # type: ignore[assignment]
        parse_frontmatter = None  # type: ignore[assignment]
        validate_task_graph_template_contract = None  # type: ignore[assignment]
        DEFAULT_TASK_GRAPH_TEMPLATE = ""  # type: ignore[assignment]
        load_task_graph_template = None  # type: ignore[assignment]
        class FrontmatterParseError(Exception):  # type: ignore[no-redef]
            pass

try:
    from scripts.internal.execution_state import (
        STAGE_5_COMMIT_AND_RESOLVE,
        complete_execution_state,
        load_execution_state,
        parse_ticket_blockers,
        scan_scratch_tickets,
    )
except ImportError:
    try:
        from execution_state import (  # type: ignore[no-redef]
            STAGE_5_COMMIT_AND_RESOLVE,
            complete_execution_state,
            load_execution_state,
            parse_ticket_blockers,
            scan_scratch_tickets,
        )
    except ImportError:
        STAGE_5_COMMIT_AND_RESOLVE = "STAGE_5_COMMIT_AND_RESOLVE"
        load_execution_state = None  # type: ignore[assignment]
        complete_execution_state = None  # type: ignore[assignment]
        parse_ticket_blockers = None  # type: ignore[assignment]
        scan_scratch_tickets = None  # type: ignore[assignment]


class CliFlagValidationResult:
    """
    [ERR-CHECK-01] Structured outcome of CLI flag validation for /implement-spec and resolve-ticket.
    Validates mutually exclusive execution flags (--auto and --step) and provides actionable usage diagnostics.
    """
    is_valid: bool
    exit_code: int
    flags: dict[str, bool]
    diagnostic: str

    def __init__(
        self,
        is_valid: bool,
        exit_code: int,
        flags: dict[str, bool],
        diagnostic: str,
    ) -> None:
        self.is_valid = is_valid
        self.exit_code = exit_code
        self.flags = flags
        self.diagnostic = diagnostic

    @property
    def message(self) -> str:
        return self.diagnostic

    @property
    def ok(self) -> bool:
        return self.is_valid

    def __bool__(self) -> bool:
        return self.is_valid

    def __iter__(self) -> Iterator[bool | dict[str, Any] | str]:
        return iter((self.is_valid, self.flags, self.diagnostic))

    def __getitem__(self, index: int) -> bool | dict[str, Any] | str:
        return (self.is_valid, self.flags, self.diagnostic)[index]

    def __len__(self) -> int:
        return 3

    def __repr__(self) -> str:
        return (
            f"CliFlagValidationResult(is_valid={self.is_valid}, "
            f"exit_code={self.exit_code}, flags={self.flags}, diagnostic={self.diagnostic!r})"
        )


class DagCycleError(ValueError):
    """
    [ERR-DAG-CYCLE] Raised when a circular blocking dependency or deadlock
    is detected across task graph tickets during topological sorting.
    """
    cycle_tickets: list[str]

    def __init__(self, message: str, cycle_tickets: Sequence[str] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.cycle_tickets = list(cycle_tickets) if cycle_tickets else []


class ScaffoldEmptyError(ValueError):
    """
    [ERR-SCAFFOLD-EMPTY] Raised when rendering or scaffolding a task dashboard for a feature slug
    lacking ticket files under .scratch/<feature-slug>/issues/.
    """
    pass


def validate_cli_flags(
    argv_or_args: Sequence[str] | argparse.Namespace | dict[str, Any] | None = None,
    *,
    auto: bool | None = None,
    step: bool | None = None,
    exit_on_error: bool = False,
) -> CliFlagValidationResult:
    """
    [ERR-CHECK-01] Validates CLI execution flags for mutual exclusion.
    Specifically rejects conflicting combinations (e.g. passing both --auto and --step)
    with exit code 1 and actionable usage guidance.
    """
    auto_val: bool = False
    step_val: bool = False

    if auto is not None or step is not None:
        auto_val = bool(auto)
        step_val = bool(step)
    elif argv_or_args is not None:
        if isinstance(argv_or_args, argparse.Namespace):
            auto_val = bool(getattr(argv_or_args, "auto", False))
            step_val = bool(getattr(argv_or_args, "step", False))
        elif isinstance(argv_or_args, dict):
            auto_val = bool(argv_or_args.get("auto", False))
            step_val = bool(argv_or_args.get("step", False))
        elif isinstance(argv_or_args, (list, tuple)):
            for item in argv_or_args:
                if not isinstance(item, str):
                    continue
                if item == "--auto" or item.startswith("--auto="):
                    auto_val = True
                elif item == "--step" or item.startswith("--step="):
                    step_val = True
        elif isinstance(argv_or_args, str):
            tokens = argv_or_args.split()
            auto_val = "--auto" in tokens
            step_val = "--step" in tokens
    else:
        for arg in sys.argv[1:]:
            if arg == "--auto" or arg.startswith("--auto="):
                auto_val = True
            elif arg == "--step" or arg.startswith("--step="):
                step_val = True

    flags: dict[str, bool] = {"auto": auto_val, "step": step_val}

    if auto_val and step_val:
        diagnostic = (
            "🛑 [ERR-CHECK-01] Mutually exclusive flags: '--auto' (full lifecycle cruise control) "
            "and '--step' (single-ticket pause) cannot be used together. "
            "Select either '--auto' for automated progression or '--step' for single-ticket step execution."
        )
        if exit_on_error:
            sys.stderr.write(diagnostic + "\n")
            sys.exit(1)
        return CliFlagValidationResult(
            is_valid=False,
            exit_code=1,
            flags=flags,
            diagnostic=diagnostic,
        )

    if auto_val:
        diagnostic = "🚀 [CHECK-02] Autonomous advancement mode (--auto) active."
        return CliFlagValidationResult(
            is_valid=True,
            exit_code=0,
            flags=flags,
            diagnostic=diagnostic,
        )

    if step_val:
        diagnostic = "🛑 [CHECK-03] Single-step execution mode (--step) active."
        return CliFlagValidationResult(
            is_valid=True,
            exit_code=0,
            flags=flags,
            diagnostic=diagnostic,
        )

    diagnostic = "Default sequential execution mode active."
    return CliFlagValidationResult(
        is_valid=True,
        exit_code=0,
        flags=flags,
        diagnostic=diagnostic,
    )


class DagCycleResult(NamedTuple):
    has_cycle: bool
    cycles: list[list[str]]
    diagnostic: str

    def __bool__(self) -> bool:
        return self.has_cycle


def detect_dag_cycles(
    target: Path | str | Sequence[dict[str, Any]] | None = None,
    ignore_resolved: bool = False,
) -> DagCycleResult:
    """
    [ERR-DRAIN-01] Deterministic DAG cycle detection for task graphs.
    Identifies dependency cycles and returns structured DagCycleResult with actionable
    cycle trace diagnostics.
    """
    tickets: list[dict[str, Any]] = []
    if target is None:
        target = REPO_ROOT

    if isinstance(target, (list, tuple)):
        tickets = [dict(t) for t in target if isinstance(t, dict)]
    else:
        p = Path(target).resolve()
        if p.is_file():
            tg_tickets = parse_task_graph_tickets(p)
            if tg_tickets:
                tickets = tg_tickets
            elif (p.parent / "issues").is_dir() and scan_scratch_tickets is not None:
                tickets = scan_scratch_tickets(p.parent)
        elif p.is_dir():
            if (p / "issues").is_dir() and scan_scratch_tickets is not None:
                tickets = scan_scratch_tickets(p)
            elif p.name == "issues" and scan_scratch_tickets is not None:
                tickets = scan_scratch_tickets(p.parent)
            elif (p / "task-graph.md").is_file():
                tg_tickets = parse_task_graph_tickets(p / "task-graph.md")
                if tg_tickets:
                    tickets = tg_tickets
            elif (p / ".scratch").is_dir() and scan_scratch_tickets is not None:
                for feat_issues in sorted((p / ".scratch").glob("*/issues")):
                    if feat_issues.is_dir():
                        scanned = scan_scratch_tickets(feat_issues.parent)
                        if scanned:
                            tickets.extend(scanned)

    graph: dict[str, list[str]] = {}
    for t in tickets:
        raw_id = str(t.get("id", "")).strip().lstrip("#")
        tid = f"{int(raw_id):02d}" if raw_id.isdigit() else raw_id
        if not tid:
            continue
        status = str(t.get("status", "")).strip().lower().replace(" ", "-")
        if ignore_resolved and ("resolved" in status or "completed" in status):
            continue

        raw_blockers = t.get("blockers")
        if raw_blockers is None:
            raw_blockers = t.get("blocked_by")
        if raw_blockers is None:
            raw_blockers = []

        if parse_ticket_blockers is not None:
            clean_blockers = parse_ticket_blockers(raw_blockers)
        elif isinstance(raw_blockers, list):
            clean_blockers = [str(b).strip() for b in raw_blockers]
        else:
            clean_blockers = [str(raw_blockers).strip()]

        normalized_blockers: list[str] = []
        for b in clean_blockers:
            b_clean = str(b).strip().lstrip("#")
            if b_clean.isdigit():
                b_clean = f"{int(b_clean):02d}"
            if b_clean:
                normalized_blockers.append(b_clean)

        graph[tid] = normalized_blockers

    for node, edges in list(graph.items()):
        for target_node in edges:
            if target_node not in graph:
                graph[target_node] = []

    visited: set[str] = set()
    rec_stack: list[str] = []
    rec_set: set[str] = set()
    detected_cycles: list[list[str]] = []
    seen_cycle_signatures: set[frozenset[str]] = set()

    def dfs(node: str) -> None:
        visited.add(node)
        rec_stack.append(node)
        rec_set.add(node)

        for neighbor in sorted(graph.get(node, [])):
            if neighbor in rec_set:
                cycle_start = rec_stack.index(neighbor)
                cycle_path = rec_stack[cycle_start:] + [neighbor]
                sig = frozenset(rec_stack[cycle_start:])
                if sig not in seen_cycle_signatures:
                    seen_cycle_signatures.add(sig)
                    detected_cycles.append(cycle_path)
            elif neighbor not in visited:
                dfs(neighbor)

        rec_stack.pop()
        rec_set.remove(node)

    for start_node in sorted(graph.keys()):
        if start_node not in visited:
            dfs(start_node)

    if detected_cycles:
        traces = [" -> ".join(c) for c in detected_cycles]
        trace_msg = ", ".join(traces)
        diag = (
            f"[ERR-DRAIN-01] Circular dependency detected in task graph DAG: "
            f"cycle found along path ({trace_msg}). Aborting execution."
        )
        return DagCycleResult(True, detected_cycles, diag)

    return DagCycleResult(False, [], "")


def is_dag_completed(
    tracker_dir_or_feature_dir: Path | str,
    validator: ManifestValidator | None = None,
) -> bool:
    """
    Evaluates whether all tickets in the target tracker directory or feature DAG
    have reached 'Status: resolved'. Returns True if at least one ticket exists and all
    tickets are resolved; returns False if unresolved tickets remain or no tickets exist.
    """
    v = validator or ManifestValidator()
    base_path = Path(tracker_dir_or_feature_dir).resolve()

    tickets: list[dict] = []
    scratch_dir = base_path / ".scratch" if (base_path / ".scratch").is_dir() else (base_path if base_path.name == ".scratch" else None)
    if scratch_dir and scratch_dir.is_dir() and not (base_path / "issues").is_dir() and base_path.name != "issues":
        feature_issue_dirs = [p for p in sorted(scratch_dir.glob("*/issues")) if p.is_dir()]
        active_branch = v._detect_active_branch(base_path)
        if active_branch:
            matched = [d for d in feature_issue_dirs if d.parent.name in active_branch]
            if len(matched) == 1:
                feature_issue_dirs = matched
        for feature_issues in feature_issue_dirs:
            sub_tickets = v.scan_tickets_from_tracker(feature_issues)
            if sub_tickets:
                tickets.extend(sub_tickets)
    else:
        tickets = v.scan_tickets_from_tracker(base_path)

    if not tickets or not all(v._is_status_resolved(t.get("status", "")) for t in tickets):
        return False

    # [ERR-SNTZ-01] Validate against task-graph.md if present to prevent premature completion on missing tickets
    tg_candidates = [
        base_path if base_path.is_file() and base_path.name.lower() in ("task-graph.md", "task-graph.markdown") else None,
        base_path / "task-graph.md" if base_path.is_dir() else None,
        base_path.parent / "task-graph.md" if base_path.parent.is_dir() else None,
    ]
    if scratch_dir and scratch_dir.is_dir():
        for feat in sorted(scratch_dir.iterdir()):
            if feat.is_dir() and (feat / "task-graph.md").is_file():
                tg_candidates.append(feat / "task-graph.md")

    tg_file = next((c for c in tg_candidates if c and c.is_file()), None)
    if tg_file:
        try:
            tg_ticks = parse_task_graph_tickets(tg_file)
            if tg_ticks:
                resolved_disk_ids = {
                    str(t.get("id", "")).strip().lstrip("#").zfill(2)
                    for t in tickets
                    if v._is_status_resolved(t.get("status", ""))
                }
                for t in tg_ticks:
                    tid = str(t.get("id", "")).strip().lstrip("#").zfill(2)
                    is_resolved_in_tg = v._is_status_resolved(t.get("status", ""))
                    is_resolved_on_disk = tid in resolved_disk_ids
                    if not is_resolved_in_tg and not is_resolved_on_disk:
                        return False
                if len(tickets) < len(tg_ticks):
                    return False
        except Exception:
            pass

    return True


def _resolve_session_anchor_file(validator: ManifestValidator, feature_dir: Path) -> Path | None:
    try:
        anchor_file = validator._resolve_session_anchor_path(feature_dir)
        if anchor_file:
            return anchor_file
    except Exception:
        pass
    if feature_dir.name == "issues":
        return feature_dir.parent / "session.json"
    return feature_dir / "session.json"


def _read_session_anchor_data(validator: ManifestValidator, feature_dir: Path) -> dict[str, Any] | None:
    """
    [IDEMP-01] Reads session anchor (.scratch/<feature-slug>/session.json).
    Decoupled from commandCenterConversationId to support pure on-disk schema.
    """
    try:
        data = validator.read_session_anchor(feature_dir)
        if isinstance(data, dict):
            return data
    except Exception:
        pass

    session_file = _resolve_session_anchor_file(validator, feature_dir)
    if session_file and session_file.is_file():
        try:
            raw = json.loads(session_file.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
        except (OSError, json.JSONDecodeError, ValueError):
            pass
    return None


def _atomic_write_file(target_path: Path | str, content: str, raise_on_error: bool = True) -> bool:
    """
    [ERR-LEAN-01] Writes text content atomically via a temporary file with safe rollback.
    Validates write permissions on existing files, replaces atomically without in-place truncation,
    and safely unlinks temporary files on failure.
    """
    path = Path(target_path).resolve()
    if path.is_file() and not os.access(path, os.W_OK):
        exc = PermissionError(13, f"Permission denied: '{path}'")
        if raise_on_error:
            raise exc
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.parent / f".{path.name}.tmp.{os.getpid()}_{uuid.uuid4().hex}"
    try:
        temp_path.write_text(content, encoding="utf-8")
        os.replace(temp_path, path)
        return True
    except OSError as exc:
        if raise_on_error:
            raise exc
        return False
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _normalize_failing_path(target_path: Path | str, exc: BaseException | None = None) -> Path:
    """
    [ERR-LEAN-01] Resolves exact failing file path, stripping internal temporary file prefixes.
    """
    fn = getattr(exc, "filename", None) if exc else None
    candidate = Path(fn).resolve() if fn else Path(target_path).resolve()
    if candidate.name.startswith(".") and ".tmp." in candidate.name:
        clean_name = candidate.name.lstrip(".").split(".tmp.")[0]
        return candidate.parent / clean_name
    return candidate


def _record_verified_commit_in_anchor(
    validator: ManifestValidator,
    feature_dir: Path,
    clean_sha: str,
    active_phase: str | None = None,
    raise_on_error: bool = False,
) -> None:
    """
    [STATE-01] [IDEMP-01] [ZERO-02] Records verified slice commit SHA and synchronizes phase in session.json.
    Works purely on disk with zero legacy commandCenterConversationId coupling.
    Propagates OSError when raise_on_error=True for atomic error resilience ([ERR-LEAN-01]).
    """
    try:
        anchor_file = _resolve_session_anchor_file(validator, feature_dir)
        if not anchor_file:
            return

        clean_sha_val = clean_sha.strip()
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        if not anchor_file.is_file():
            anchor_data: dict[str, Any] = {
                "feature": feature_dir.name,
                "createdAt": now_utc,
                "activePhase": active_phase or "implementation",
                "executionMode": "manual",
                "fixLoopCount": 0,
                "reviewedCommits": [clean_sha_val] if clean_sha_val else [],
            }
            _atomic_write_file(anchor_file, json.dumps(anchor_data, indent=2) + "\n", raise_on_error=raise_on_error)
            return

        existing_anchor = _read_session_anchor_data(validator, feature_dir) or {}
        existing_commits: list[str] = []
        if isinstance(existing_anchor.get("reviewedCommits"), list):
            existing_commits = [str(c).strip() for c in existing_anchor.get("reviewedCommits", [])]

        needs_update = False
        norm_sha = clean_sha_val.lower()
        existing_norms = [c.lower() for c in existing_commits]
        if norm_sha and norm_sha not in existing_norms:
            existing_commits.append(clean_sha_val)
            needs_update = True

        existing_phase = existing_anchor.get("activePhase")
        can_update_phase = existing_phase in ("implementation", "implement", "", None)

        if can_update_phase and active_phase is not None and existing_phase != active_phase:
            needs_update = True

        # Purge legacy commandCenterConversationId to strictly satisfy [ZERO-02] pure schema
        if "commandCenterConversationId" in existing_anchor:
            needs_update = True

        if needs_update:
            try:
                data = json.loads(anchor_file.read_text(encoding="utf-8")) if anchor_file.is_file() else {}
            except (OSError, json.JSONDecodeError, ValueError):
                data = {}
            if not isinstance(data, dict):
                data = {}

            data.pop("commandCenterConversationId", None)
            if "feature" not in data:
                data["feature"] = feature_dir.name
            if "createdAt" not in data:
                data["createdAt"] = now_utc
            if can_update_phase:
                if active_phase is not None:
                    data["activePhase"] = active_phase
                elif "activePhase" not in data:
                    data["activePhase"] = "implementation"
            if "executionMode" not in data:
                data["executionMode"] = "manual"
            if "fixLoopCount" not in data:
                data["fixLoopCount"] = 0
            data["reviewedCommits"] = existing_commits

            _atomic_write_file(anchor_file, json.dumps(data, indent=2) + "\n", raise_on_error=raise_on_error)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        if raise_on_error and isinstance(exc, OSError):
            raise


def _mark_execution_state_completed(state_dir: Path) -> None:
    """
    [RUN-02] Marks 5_commit_and_resolve: COMPLETED in execution-state.json
    and preserves the completed execution record for auditability.
    """
    try:
        if complete_execution_state:
            complete_execution_state(state_dir)
        else:
            state_file = state_dir / "execution-state.json"
            if state_file.is_file():
                data = json.loads(state_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    data.setdefault("stages", {})["5_commit_and_resolve"] = "COMPLETED"
                    _atomic_write_file(state_file, json.dumps(data, indent=2) + "\n", raise_on_error=False)
    except (json.JSONDecodeError, OSError, ValueError):
        pass


class ResolutionResult(tuple):
    """
    Structured outcome of resolve_ticket_atomic.
    Subclasses 2-tuple (exit_code, message) for backward compatibility with tuple unpacking,
    while exposing structured status attributes to eliminate string-matching Fowler smells.
    """
    exit_code: int
    message: str
    dispatched: bool
    suppressed: bool

    def __new__(
        cls,
        exit_code: int,
        message: str,
        dispatched: bool = False,
        suppressed: bool = False,
    ) -> ResolutionResult:
        instance = super().__new__(cls, (exit_code, message))
        instance.exit_code = exit_code
        instance.message = message
        instance.dispatched = dispatched
        instance.suppressed = suppressed
        return instance


class ReconciliationResult(tuple):
    """
    [RECON-01] [ERR-RECON-01] Structured outcome of verify_commit_and_reconcile.
    Subclasses 2-tuple (exit_code, message) for backward compatibility with tuple unpacking,
    while exposing structured status attributes to eliminate string-matching Fowler smells.
    """
    exit_code: int
    message: str
    reconciled: bool
    verified_commit: str
    unblocked_frontier: list[str]
    ticket_id: str
    is_rejected: bool

    def __new__(
        cls,
        exit_code: int,
        message: str,
        reconciled: bool = False,
        verified_commit: str = "",
        unblocked_frontier: list[str] | None = None,
        ticket_id: str = "",
        is_rejected: bool = False,
    ) -> ReconciliationResult:
        instance = super().__new__(cls, (exit_code, message))
        instance.exit_code = exit_code
        instance.message = message
        instance.reconciled = reconciled
        instance.verified_commit = verified_commit
        instance.unblocked_frontier = unblocked_frontier if unblocked_frontier is not None else []
        instance.ticket_id = ticket_id
        instance.is_rejected = is_rejected
        return instance

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class BatchReconciliationResult(tuple):
    """
    [EDGE-RECON-01] [EDGE-RECON-02] Structured outcome of batch_reconcile_tickets.
    Subclasses 2-tuple (exit_code, message) for backward compatibility,
    exposing structured status attributes for multi-ticket reconciliation.
    """
    exit_code: int
    message: str
    results: list[ReconciliationResult]
    reconciled_tickets: list[str]
    unblocked_frontier: list[str]
    briefs: str
    is_rejected: bool

    def __new__(
        cls,
        exit_code: int,
        message: str,
        results: list[ReconciliationResult] | None = None,
        reconciled_tickets: list[str] | None = None,
        unblocked_frontier: list[str] | None = None,
        briefs: str = "",
        is_rejected: bool = False,
    ) -> BatchReconciliationResult:
        instance = super().__new__(cls, (exit_code, message))
        instance.exit_code = exit_code
        instance.message = message
        instance.results = results if results is not None else []
        instance.reconciled_tickets = reconciled_tickets if reconciled_tickets is not None else []
        instance.unblocked_frontier = unblocked_frontier if unblocked_frontier is not None else []
        instance.briefs = briefs
        instance.is_rejected = is_rejected
        return instance

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    @property
    def reconciled(self) -> bool:
        return len(self.reconciled_tickets) > 0

    @property
    def ticket_id(self) -> str:
        if self.results:
            return self.results[0].ticket_id
        return ""

    @property
    def verified_commit(self) -> str:
        if self.results:
            return self.results[0].verified_commit
        return ""


class GitRepositoryStateResult(tuple):
    """
    [ERR-RECON-02] Structured outcome of Git repository lock and detached HEAD state inspection.
    """
    is_valid: bool
    diagnostic: str
    lock_file: Path | None
    is_detached_head: bool

    def __new__(
        cls,
        is_valid: bool,
        diagnostic: str = "",
        lock_file: Path | None = None,
        is_detached_head: bool = False,
    ) -> GitRepositoryStateResult:
        instance = super().__new__(cls, (is_valid, diagnostic))
        instance.is_valid = is_valid
        instance.diagnostic = diagnostic
        instance.lock_file = lock_file
        instance.is_detached_head = is_detached_head
        return instance

    @property
    def ok(self) -> bool:
        return self.is_valid

    def __bool__(self) -> bool:
        return self.is_valid


class TaskGraphWriteResult(tuple):
    """
    [ERR-RECON-03] Structured outcome of atomic task-graph.md update operations.
    Subclasses 2-tuple (ok, message) for backward compatibility and tuple unpacking,
    while exposing structured fields: ok, message, error, target_path, tag.
    """
    ok: bool
    message: str
    error: str | None
    target_path: Path | None
    tag: str

    def __new__(
        cls,
        ok: bool,
        message: str = "",
        error: str | None = None,
        target_path: Path | str | None = None,
        tag: str = "[ERR-RECON-03]",
    ) -> TaskGraphWriteResult:
        instance = super().__new__(cls, (ok, message))
        instance.ok = ok
        instance.message = message
        instance.error = error
        instance.target_path = Path(target_path) if target_path else None
        instance.tag = tag
        return instance

    def __bool__(self) -> bool:
        return self.ok

    @property
    def success(self) -> bool:
        return self.ok


class CircuitBreakerResult(tuple):
    """
    [ERR-EVAL-01] Structured outcome of remediation circuit breaker evaluation.
    Halts automated evaluation remediation loops when findings persist across
    consecutive remediation attempts (fixLoopCount >= 2), marks task-graph.md
    as '⚠️ escalated-to-human', and directs the operator to manual intervention.
    """
    triggered: bool
    eval_type: str
    fix_loop_count: int
    threshold: int
    status: str
    log_message: str
    directive: str
    task_graph_path: Path | None
    task_graph_updated: bool
    diagnostic: str

    def __new__(
        cls,
        triggered: bool,
        eval_type: str = "code-review",
        fix_loop_count: int = 0,
        threshold: int = 2,
        status: str = "",
        log_message: str = "",
        directive: str = "",
        task_graph_path: Path | str | None = None,
        task_graph_updated: bool = False,
        diagnostic: str = "",
    ) -> CircuitBreakerResult:
        instance = super().__new__(cls, (triggered, log_message or diagnostic))
        instance.triggered = triggered
        instance.eval_type = eval_type
        instance.fix_loop_count = fix_loop_count
        instance.threshold = threshold
        instance.status = status
        instance.log_message = log_message
        instance.directive = directive
        instance.task_graph_path = Path(task_graph_path) if task_graph_path else None
        instance.task_graph_updated = task_graph_updated
        instance.diagnostic = diagnostic
        return instance

    @property
    def is_triggered(self) -> bool:
        return self.triggered

    @property
    def activated(self) -> bool:
        return self.triggered

    @property
    def halt_remediation(self) -> bool:
        return self.triggered

    @property
    def escalated(self) -> bool:
        return self.triggered

    @property
    def escalated_to_human(self) -> bool:
        return self.triggered

    @property
    def loop_count(self) -> int:
        return self.fix_loop_count

    @property
    def ok(self) -> bool:
        return not self.triggered

    def __bool__(self) -> bool:
        return self.triggered

    def format_notice(self) -> str:
        if self.triggered:
            return (
                f"⚠️ [ERR-REMED-02] [ERR-EVAL-01] ESCALATION_CIRCUIT_BREAKER_TRIGGERED: Automated remediation halted for "
                f"{self.eval_type} after {self.fix_loop_count} successive failed attempts "
                f"(fixLoopCount={self.fix_loop_count} >= {self.threshold}).\n"
                f"Status: '{self.status}'.\n"
                f"Directive: {self.directive}"
            )
        return (
            f"ℹ️ Circuit breaker inactive for {self.eval_type} "
            f"(fixLoopCount={self.fix_loop_count} < threshold={self.threshold}). Remediation may proceed."
        )


class RemediationCommitResult(tuple):
    """
    [ERR-REMED-03] Structured outcome of remediation commit execution and resiliency checks.
    Guarantees that Git lock contention (.git/index.lock) or filesystem IO failures
    do not force or corrupt repository state, logs REMEDIATION_COMMIT_FAILED,
    avoids marking the phase as PASS, and directs the operator to manual workspace resolution.
    """
    ok: bool
    exit_code: int
    commit_sha: str
    error: str
    log_message: str
    directive: str
    lock_file: Path | None
    is_lock_failure: bool
    is_io_failure: bool

    def __new__(
        cls,
        ok: bool,
        exit_code: int = 0,
        commit_sha: str = "",
        error: str = "",
        log_message: str = "",
        directive: str = "",
        lock_file: Path | str | None = None,
        is_lock_failure: bool = False,
        is_io_failure: bool = False,
    ) -> RemediationCommitResult:
        instance = super().__new__(cls, (ok, log_message or error))
        instance.ok = ok
        instance.exit_code = exit_code
        instance.commit_sha = commit_sha
        instance.error = error
        instance.log_message = log_message
        instance.directive = directive
        instance.lock_file = Path(lock_file) if lock_file else None
        instance.is_lock_failure = is_lock_failure
        instance.is_io_failure = is_io_failure
        return instance

    @property
    def success(self) -> bool:
        return self.ok

    def __bool__(self) -> bool:
        return self.ok


class DocDiffEvaluationResult(tuple):
    """
    [EDGE-EVAL-01] Structured outcome of documentation-only git diff evaluation.
    Identifies if a specification diff contains zero executable application code or attack surface seams,
    enabling expedited fast-path evaluation briefs.
    """
    is_docs_only: bool
    changed_files: list[str]
    non_doc_files: list[str]
    is_expedited: bool
    diagnostic: str

    def __new__(
        cls,
        is_docs_only: bool,
        changed_files: Sequence[str] | None = None,
        non_doc_files: Sequence[str] | None = None,
        diagnostic: str = "",
    ) -> DocDiffEvaluationResult:
        c_files = list(changed_files or [])
        nd_files = list(non_doc_files or [])
        instance = super().__new__(cls, (is_docs_only, c_files))
        instance.is_docs_only = is_docs_only
        instance.changed_files = c_files
        instance.non_doc_files = nd_files
        instance.is_expedited = is_docs_only
        instance.diagnostic = diagnostic
        return instance

    @property
    def ok(self) -> bool:
        return self.is_docs_only

    @property
    def all_files(self) -> list[str]:
        return self.changed_files

    @property
    def code_files(self) -> list[str]:
        return self.non_doc_files

    def format_notice(self) -> str:
        if self.is_docs_only:
            return (
                f"✅ [EDGE-EVAL-01] Documentation-only diff verified: zero executable application code or attack "
                f"surface seams changed ({len(self.changed_files)} files). Expedited evaluation active."
            )
        return (
            f"⚠️ [EDGE-EVAL-01] Non-documentation or executable files detected ({len(self.non_doc_files)} files): "
            f"{', '.join(self.non_doc_files[:3])}. Standard evaluation required."
        )


class PhasePrerequisiteResult(tuple):
    """
    [ERR-EXCL-02] Structured outcome of phase prerequisite validation.
    Subclasses 2-tuple (is_valid, diagnostic) for backward compatibility,
    while exposing structured status attributes.
    """
    is_valid: bool
    diagnostic: str
    phase: str
    missing_prerequisites: list[str]
    target_failing: Path | None

    def __new__(
        cls,
        is_valid: bool,
        diagnostic: str = "",
        phase: str = "",
        missing_prerequisites: list[str] | None = None,
        target_failing: Path | str | None = None,
    ) -> PhasePrerequisiteResult:
        instance = super().__new__(cls, (is_valid, diagnostic))
        instance.is_valid = is_valid
        instance.diagnostic = diagnostic
        instance.phase = phase
        instance.missing_prerequisites = list(missing_prerequisites or [])
        instance.target_failing = Path(target_failing) if target_failing else None
        return instance

    @property
    def ok(self) -> bool:
        return self.is_valid

    def __bool__(self) -> bool:
        return self.is_valid


class TicketConcurrencyResult(tuple):
    """
    [ERR-EXCL-03] Structured outcome of ticket concurrency validation for worker sessions.
    Subclasses 2-tuple (is_valid, diagnostic) for backward compatibility,
    while exposing structured status attributes.
    """
    is_valid: bool
    diagnostic: str
    ticket_id: str
    status: str
    ticket_path: Path | None
    is_conflict: bool

    def __new__(
        cls,
        is_valid: bool,
        diagnostic: str = "",
        ticket_id: str = "",
        status: str = "",
        ticket_path: Path | str | None = None,
        is_conflict: bool = False,
    ) -> TicketConcurrencyResult:
        instance = super().__new__(cls, (is_valid, diagnostic))
        instance.is_valid = is_valid
        instance.diagnostic = diagnostic
        instance.ticket_id = ticket_id
        instance.status = status
        instance.ticket_path = Path(ticket_path) if ticket_path else None
        instance.is_conflict = is_conflict
        return instance

    @property
    def ok(self) -> bool:
        return self.is_valid

    def __bool__(self) -> bool:
        return self.is_valid


class FilesystemIoErrorResult(ResolutionResult):
    """
    [ERR-LEAN-01] Structured outcome of filesystem write failure or permission denial handling.
    Inherits from ResolutionResult (2-tuple (exit_code, message)) for seamless backward compatibility,
    while exposing structured fields: tag, escalation, target_path, system_error, commit_sha, recovery_instructions.
    """
    tag: str
    escalation: str
    target_path: Path
    system_error: str
    commit_sha: str
    ticket_id: str
    recovery_instructions: str

    def __new__(
        cls,
        exit_code: int,
        message: str,
        target_path: Path | str,
        system_error: str,
        commit_sha: str = "",
        ticket_id: str = "",
        recovery_instructions: str = "",
        tag: str = "[ERR-LEAN-01]",
        escalation: str = "ESCALATION_IO_ERROR",
        dispatched: bool = False,
        suppressed: bool = False,
    ) -> FilesystemIoErrorResult:
        instance = super().__new__(cls, exit_code, message, dispatched=dispatched, suppressed=suppressed)
        instance.target_path = Path(target_path)
        instance.system_error = str(system_error)
        instance.commit_sha = str(commit_sha)
        instance.ticket_id = str(ticket_id)
        instance.recovery_instructions = str(recovery_instructions)
        instance.tag = str(tag)
        instance.escalation = str(escalation)
        return instance

    def get(self, key: str, default: object = None) -> object:
        return getattr(self, key, default)

    def __getitem__(self, key: str | int | slice) -> object:
        if isinstance(key, str):
            if hasattr(self, key):
                return getattr(self, key)
            raise KeyError(key)
        return super().__getitem__(key)


class FilesystemSyncError(OSError):
    """
    [ERR-LEAN-01] Raised when synchronizing task graph or session anchor on disk encounters a filesystem error.
    Subclasses OSError for standard exception handling while carrying the structured FilesystemIoErrorResult.
    """
    result: FilesystemIoErrorResult

    def __init__(self, message: str, result: FilesystemIoErrorResult) -> None:
        super().__init__(message)
        self.result = result


def handle_filesystem_io_error(
    target_path: Path | str,
    exc: BaseException | str,
    *,
    ticket_id: str | int | None = None,
    commit_sha: str | None = None,
    feature_dir: Path | str | None = None,
    action: str = "synchronize ticket status",
    app_data_dir: Path | str | None = None,
) -> FilesystemIoErrorResult:
    """
    [ERR-LEAN-01] Handles filesystem write failure, permissions denial (EACCES), or storage exhaustion.
    Immediately halts with exit code 1, logs ESCALATION_IO_ERROR, preserves git commit references,
    and outputs actionable recovery instructions to the operator.
    Zero Brain UI artifact mirroring.
    """
    path = _normalize_failing_path(target_path, exc if isinstance(exc, BaseException) else None)
    clean_sha = str(commit_sha or "").strip()
    if clean_sha.startswith("commit:"):
        clean_sha = clean_sha.split(":", 1)[1].strip()
    clean_sha = clean_sha.strip("'\"")

    raw_id = str(ticket_id or "").strip()
    clean_id = raw_id[:-3].strip() if raw_id.lower().endswith(".md") else raw_id
    m = re.match(r"^(?:ticket-|issue-|#)?([0-9]{1,3})", clean_id, re.IGNORECASE)
    if m:
        clean_id = f"{int(m.group(1)):02d}"

    sys_err_str = str(exc)
    err_lower = sys_err_str.lower()
    is_rofs = getattr(exc, "errno", None) == 30 or "read-only" in err_lower
    is_perm = isinstance(exc, PermissionError) or "permission" in err_lower or getattr(exc, "errno", None) == 13 or is_rofs
    is_nospc = "no space" in err_lower or "disk full" in err_lower or getattr(exc, "errno", None) == 28

    if is_perm:
        diag_cause = "Permission denial (EACCES / read-only filesystem)"
        fix_step = f"chmod u+w \"{path}\" (or chmod -R u+w \"{path.parent}\")"
    elif is_nospc:
        diag_cause = "Storage exhaustion (ENOSPC / disk full)"
        fix_step = f"Free up storage space on the volume hosting \"{path}\" (check df -h)"
    else:
        diag_cause = f"Filesystem I/O failure ({type(exc).__name__ if isinstance(exc, BaseException) else 'Error'})"
        fix_step = f"Verify filesystem accessibility and write permissions for \"{path}\""

    recovery_cmd = (
        f"./scripts/resolve-ticket.sh {clean_id} {clean_sha}"
        if clean_id and clean_sha
        else (f"./scripts/resolve-ticket.sh {clean_id}" if clean_id else "./scripts/resolve-ticket.sh")
    )

    instructions = (
        f"Actionable Recovery Instructions:\n"
        f"1. Diagnose root cause: {diag_cause}.\n"
        f"2. Fix filesystem state: {fix_step}.\n"
        f"3. Verified git commit reference '{clean_sha or '<sha>'}' remains preserved on integration branch HEAD.\n"
        f"4. Re-run ticket resolution to synchronize on-disk state:\n"
        f"   {recovery_cmd}\n"
        f"5. Or resume autonomous draining: /implement-spec"
    )

    log_msg = (
        f"🚨 [ERR-LEAN-01] Filesystem I/O Error: ESCALATION_IO_ERROR\n"
        f"Failed to {action} at target path: '{path}'\n"
        f"System Error: {sys_err_str}\n"
        f"Preserved Git Commit: '{clean_sha}'\n"
        f"Ticket ID: '{clean_id}'\n\n"
        f"{instructions}\n"
        f"(Zero Brain UI artifact mirroring; exit code 1)."
    )

    if feature_dir:
        resolved_feat = Path(feature_dir).resolve()
        session_file = resolved_feat / "session.json"
        if session_file != path:
            try:
                if session_file.is_file():
                    s_data = json.loads(session_file.read_text(encoding="utf-8"))
                else:
                    s_data = {"feature": resolved_feat.name}
                s_data["escalation"] = "ESCALATION_IO_ERROR"
                s_data["lastEscalation"] = "ESCALATION_IO_ERROR"
                _atomic_write_file(session_file, json.dumps(s_data, indent=2) + "\n", raise_on_error=False)
            except Exception:
                pass

    return FilesystemIoErrorResult(
        exit_code=1,
        message=log_msg,
        target_path=path,
        system_error=sys_err_str,
        commit_sha=clean_sha,
        ticket_id=clean_id,
        recovery_instructions=instructions,
        tag="[ERR-LEAN-01]",
        escalation="ESCALATION_IO_ERROR",
    )


def format_dag_completion_notice(dag_complete: bool) -> str:
    """Returns downstream review phase guidance when the entire DAG is complete."""
    if not dag_complete:
        return ""
    border = "=" * 80
    return (
        f"\n{border}\n"
        "🎉 FEATURE MILESTONE COMPLETE: 100% of tickets in the DAG are resolved!\n"
        "All tickets in the feature DAG are resolved (100% complete)!\n"
        f"{border}\n\n"
        "Next Steps:\n"
        "Proceed directly to downstream review phases:\n"
        "  /code-review\n"
        "  /pr"
    )


def parse_task_graph_tickets(task_graph_path: Path | str) -> list[dict[str, Any]]:
    """Parses ticket entries from .scratch/<feature-slug>/task-graph.md progress table."""
    path = Path(task_graph_path).resolve()
    if not path.is_file():
        return []
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return []

    tickets: list[dict[str, Any]] = []
    status_table_match = re.search(
        r"#{2,3}\s*Status\s*Progress\s*Table.*?(?=\n##\s+|\Z)",
        content,
        re.DOTALL | re.IGNORECASE,
    )
    search_content = status_table_match.group(0) if status_table_match else content

    table_row_regex = re.compile(
        r"^\s*\|\s*\*{0,2}(?:\[\s*)?(?:ticket-|issue-|#)?([0-9]{1,3})(?:\s*\]\([^)]+\))?\*{0,2}\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|(?:\s*([^|\r\n]+?)\s*\|)?",
        re.MULTILINE,
    )
    for match in table_row_regex.finditer(search_content):
        tid_num = match.group(1).strip()
        tid = f"{int(tid_num):02d}"
        title = match.group(2).strip()
        blockers_raw = match.group(3).strip()
        status_raw = match.group(4).strip()
        commit_raw = match.group(5).strip() if match.group(5) else ""

        blockers: list[str] = []
        if not re.search(r"\bnone\b", blockers_raw, re.IGNORECASE):
            for b_match in re.finditer(r"\b([0-9]{1,3})\b", blockers_raw):
                blockers.append(f"{int(b_match.group(1)):02d}")

        tickets.append({
            "id": tid,
            "title": title,
            "status": status_raw,
            "blockers": blockers,
            "verified_commit": commit_raw,
        })
    return tickets


def is_status_ready_for_agent(status: str) -> bool:
    """
    [CLAIM-02] Evaluates whether ticket status represents an unworked ready-for-agent ticket.
    Filters out non-agent roles (ready-for-human, wontfix, needs-info, needs-triage),
    in-progress states, and resolved states.
    """
    s = status.lower().strip("`* \t")
    if s.startswith("⚪"):
        s = s[1:].strip("`* \t")
    if any(neg in s for neg in ("human", "wontfix", "won't fix", "triage", "info", "blocked", "in-progress", "in_progress", "resolved", "closed", "done")):
        return False
    tokens = set(re.findall(r"[a-z0-9_-]+", s))
    ready_tokens = {"ready-for-agent", "ready_for_agent", "ready"}
    return bool((tokens & ready_tokens) or not s)


def format_operator_handoff(
    tracker_dir_or_feature_dir: Path | str,
    validator: ManifestValidator | None = None,
    dag_complete: bool | None = None,
    task_graph_path: Path | str | None = None,
    prefer_paths: bool = True,
    lean_summary: bool = False,
    artifact_path: Path | str | None = None,
    lean: bool = False,
) -> str:
    """
    [HAND-01] [HAND-02] [API-01] [API-02] [ADR-0047] Formats operator hand-off and terminal execution recommendations:
    - If lean is True, strictly emits standalone 1-line execution command blocks in 4-backtick bash blocks per ADR 0047.
    - If lean_summary is True, strictly outputs the standardized Lean 3-Line Summary Protocol ([API-02], [SUMM-01]).
    - If uncompleted downstream tickets exist, inspects the feature DAG and prints
      a clean copy-pasteable '/implement <NN>' command for the next unblocked frontier ticket,
      directing the operator to execute it at their discretion ([HAND-01]).
    - When 100% of tickets in the DAG are resolved, prints a milestone completion banner
      directing the operator to proceed to '/code-review' ([HAND-02]).
    The artifact_path parameter is deprecated as a safe no-op for backward compatibility ([API-01]).
    """
    v = validator or ManifestValidator()
    base_path = Path(tracker_dir_or_feature_dir).resolve()

    tg_file: Path | None = None
    if task_graph_path:
        p = Path(task_graph_path).resolve()
        if p.is_file():
            tg_file = p
    elif base_path.is_file() and base_path.name.lower() == "task-graph.md":
        tg_file = base_path
    else:
        for cand in [
            base_path / "task-graph.md",
            base_path.parent / "task-graph.md",
            base_path / "issues" / "task-graph.md",
        ]:
            if cand.is_file():
                tg_file = cand
                break

    tg_tickets: list[dict] | None = None
    if dag_complete is None:
        if base_path.is_file() and base_path.name.lower() == "task-graph.md":
            tg_tickets = parse_task_graph_tickets(base_path)
            dag_complete = len(tg_tickets) > 0 and all(
                ("resolved" in t["status"].lower() or "✅" in t["status"])
                for t in tg_tickets
            )
        else:
            dag_complete = is_dag_completed(base_path, validator=v)

    if base_path.is_file():
        if (base_path.parent / "issues").is_dir():
            target_issues_dir = base_path.parent / "issues"
        else:
            target_issues_dir = base_path.parent
    else:
        if (base_path / "issues").is_dir():
            target_issues_dir = base_path / "issues"
        else:
            target_issues_dir = base_path

    if dag_complete:
        if lean_summary and not lean:
            total_t = len(tg_tickets) if tg_tickets else None
            resolved_t = len(tg_tickets) if tg_tickets else None
            return v.format_chat_handoff(
                [],
                tracker_dir=target_issues_dir,
                prefer_paths=prefer_paths,
                lean_summary=True,
                total_tickets=total_t,
                resolved_tickets=resolved_t,
                task_graph_path=tg_file,
                phase="Code Review",
            )

        target_f_dir = tg_file.parent if tg_file else (target_issues_dir.parent if target_issues_dir.name == "issues" else target_issues_dir)
        cr_artifact = target_f_dir / "code-review.md" if target_f_dir else None
        cr_has_pass = False
        if cr_artifact and cr_artifact.is_file():
            try:
                cr_txt = cr_artifact.read_text(encoding="utf-8").upper()
                if "PASS" in cr_txt and "FAIL" not in cr_txt:
                    cr_has_pass = True
            except Exception:
                pass

        sa_artifact = target_f_dir / "security-audit.md" if target_f_dir else None
        sa_has_pass = False
        if sa_artifact and sa_artifact.is_file():
            try:
                sa_txt = sa_artifact.read_text(encoding="utf-8").upper()
                if "PASS" in sa_txt and "FAIL" not in sa_txt:
                    sa_has_pass = True
            except Exception:
                pass

        if cr_has_pass and not sa_has_pass:
            target_phase = "security-audit"
        elif cr_has_pass and sa_has_pass:
            target_phase = "pr"
        else:
            target_phase = "code-review"

        if lean:
            return format_evaluation_brief(
                target_phase,
                tracker_dir_or_feature_dir=target_issues_dir,
                task_graph_path=tg_file,
                root_dir=getattr(v, "repo_root", None),
                validator=v,
                include_heading=False,
                lean=True,
            )

        notice = format_dag_completion_notice(True)
        try:
            eval_brief = format_evaluation_brief(
                target_phase,
                tracker_dir_or_feature_dir=target_issues_dir,
                task_graph_path=tg_file,
                root_dir=getattr(v, "repo_root", None),
                validator=v,
                lean=True,
            )
            return f"{notice}\n\n{eval_brief}"
        except Exception:
            return notice

    scanned_status_map: dict[str, str] = {}
    frontier: list[str] = []
    scan_source = target_issues_dir if target_issues_dir.is_dir() else (base_path if base_path.is_dir() else None)
    if scan_source:
        try:
            tickets = v.scan_tickets_from_tracker(scan_source)
            if tickets:
                scanned_status_map = {
                    str(t.get("id", "")).strip().lstrip("#").zfill(2): str(t.get("status", ""))
                    for t in tickets
                }
                frontier = v.calculate_unblocked_frontier(tickets)
        except Exception:
            frontier = []

    tg_status_map: dict[str, str] = {}
    if tg_file and tg_file.is_file():
        tg_tickets = parse_task_graph_tickets(tg_file)
        if tg_tickets:
            tg_status_map = {
                str(t.get("id", "")).strip().lstrip("#").zfill(2): str(t.get("status", ""))
                for t in tg_tickets
            }
            if not frontier:
                resolved_ids = {
                    t["id"] for t in tg_tickets
                    if ("resolved" in t["status"].lower() or "✅" in t["status"])
                }
                for t in tg_tickets:
                    if t["id"] in resolved_ids:
                        continue
                    if all(b in resolved_ids for b in t["blockers"]):
                        frontier.append(t["id"])

    if lean_summary and not lean:
        enriched_frontier: list[dict[str, Any]] = []
        for raw_id in frontier:
            clean_id = str(raw_id).strip().lstrip("#")
            if clean_id.lower().startswith("ticket-"):
                clean_id = clean_id[7:].strip()
            if clean_id.isdigit():
                clean_id = f"{int(clean_id):02d}"

            target_cmd = clean_id
            candidate_file: Path | None = None
            if target_issues_dir.is_dir():
                matches = sorted(target_issues_dir.glob(f"{clean_id}-*.md")) or sorted(target_issues_dir.glob(f"{clean_id}.md"))
                if matches:
                    candidate_file = matches[0]
                    try:
                        target_cmd = candidate_file.resolve().relative_to(Path.cwd().resolve()).as_posix()
                    except Exception:
                        target_cmd = candidate_file.as_posix()

            on_disk_status = ""
            if candidate_file and candidate_file.is_file():
                try:
                    on_disk_status = v._extract_ticket_status_from_text(candidate_file.read_text(encoding="utf-8"))
                except Exception:
                    on_disk_status = ""
            if not on_disk_status and target_issues_dir.is_dir():
                on_disk_status = v._lookup_ticket_status_in_tracker(target_issues_dir, clean_id)
            if not on_disk_status and clean_id in scanned_status_map:
                on_disk_status = scanned_status_map[clean_id]

            tg_status = tg_status_map.get(clean_id, "")
            scanned_status = scanned_status_map.get(clean_id, "")

            if (
                v._is_status_resolved(on_disk_status)
                or v._is_status_resolved(tg_status)
                or v._is_status_resolved(scanned_status)
            ):
                continue

            if (
                v._is_status_in_progress(on_disk_status)
                or v._is_status_in_progress(tg_status)
                or v._is_status_in_progress(scanned_status)
            ):
                item_status = "in-progress"
            else:
                item_status = on_disk_status or tg_status or scanned_status or "ready-for-agent"

            enriched_frontier.append({
                "id": clean_id,
                "path": target_cmd,
                "status": item_status,
            })

        return v.format_chat_handoff(
            enriched_frontier,
            tracker_dir=target_issues_dir,
            prefer_paths=prefer_paths,
            lean_summary=True,
            task_graph_path=tg_file,
        )

    if frontier:
        executable_commands: list[str] = []
        in_progress_count = 0
        for raw_id in frontier:
            clean_id = str(raw_id).strip().lstrip("#")
            if clean_id.lower().startswith("ticket-"):
                clean_id = clean_id[7:].strip()
            if clean_id.isdigit():
                clean_id = f"{int(clean_id):02d}"

            target_cmd = clean_id
            candidate_file: Path | None = None
            if target_issues_dir.is_dir():
                matches = sorted(target_issues_dir.glob(f"{clean_id}-*.md")) or sorted(target_issues_dir.glob(f"{clean_id}.md"))
                if matches:
                    candidate_file = matches[0]
                    try:
                        target_cmd = candidate_file.resolve().relative_to(Path.cwd().resolve()).as_posix()
                    except Exception:
                        target_cmd = candidate_file.as_posix()

            on_disk_status = ""
            if candidate_file and candidate_file.is_file():
                try:
                    on_disk_status = v._extract_ticket_status_from_text(candidate_file.read_text(encoding="utf-8"))
                except Exception:
                    on_disk_status = ""
            if not on_disk_status and target_issues_dir.is_dir():
                on_disk_status = v._lookup_ticket_status_in_tracker(target_issues_dir, clean_id)
            if not on_disk_status and clean_id in scanned_status_map:
                on_disk_status = scanned_status_map[clean_id]

            tg_status = tg_status_map.get(clean_id, "")
            scanned_status = scanned_status_map.get(clean_id, "")

            if (
                v._is_status_resolved(on_disk_status)
                or v._is_status_resolved(tg_status)
                or v._is_status_resolved(scanned_status)
            ):
                continue

            # [CLAIM-02] Filter out tickets in-progress in other sessions to prevent duplicate dispatch.
            if (
                v._is_status_in_progress(on_disk_status)
                or v._is_status_in_progress(tg_status)
                or v._is_status_in_progress(scanned_status)
            ):
                in_progress_count += 1
                continue

            # [CLAIM-02] Emit /implement commands exclusively for unworked tickets (Status: ready-for-agent)
            effective_status = on_disk_status or tg_status or scanned_status
            ready_check = getattr(v, "_is_status_ready_for_agent", is_status_ready_for_agent)
            if not ready_check(effective_status):
                continue

            executable_commands.append(target_cmd)

        if executable_commands:
            if lean:
                blocks: list[str] = []
                for cmd_target in executable_commands:
                    disp_path = str(cmd_target)
                    if not disp_path.startswith(".scratch/") and target_issues_dir.is_dir():
                        clean_target_id = Path(disp_path).stem.split("-")[0] if "-" in Path(disp_path).stem else Path(disp_path).stem
                        matches = sorted(target_issues_dir.glob(f"{clean_target_id}-*.md")) or sorted(target_issues_dir.glob(f"{clean_target_id}.md"))
                        if matches:
                            disp_path = _normalize_ticket_display_path(matches[0], root_dir=getattr(v, "repo_root", None))
                    elif not disp_path.startswith(".scratch/") and tg_file:
                        slug = tg_file.parent.name
                        disp_path = f".scratch/{slug}/issues/{cmd_target}.md"
                    blocks.append(f"````bash\n/implement {disp_path}\n````")
                return "\n\n".join(blocks)

            next_commands = "\n".join(f"  /implement {tid}" for tid in executable_commands)
            return (
                "\n\nNext Steps:\n"
                "Execute the next unblocked ticket at your discretion:\n"
                f"{next_commands}"
            )

        if in_progress_count > 0:
            standby_msg = "> ⏳ Standby: All active frontier tickets are currently in-progress in other sessions."
            if lean:
                return standby_msg
            return f"\n\n{standby_msg}"

    return ""


def format_next_step_guidance(
    issues_dir: Path,
    validator: ManifestValidator,
    dag_complete: bool,
    lean_summary: bool = False,
    artifact_path: Path | str | None = None,
    lean: bool = False,
) -> str:
    """
    [RES-01] [API-01] [API-02] Formats next-step execution commands.
    Delegates to format_operator_handoff for backward compatibility.
    """
    return format_operator_handoff(
        issues_dir,
        validator=validator,
        dag_complete=dag_complete,
        lean_summary=lean_summary,
        artifact_path=artifact_path,
        lean=lean,
    )


def extract_ticket_scenario_tags(content: str) -> list[str]:
    """
    Extracts Living Spec scenario tags (e.g. ['BRIEF-01', 'BRIEF-02']) from ticket content.
    Scans acceptance criteria checkboxes, bold scenario markers, and scenario citations.
    """
    tags: list[str] = []
    for match in re.finditer(r"-\s*\[[ xX]\]\s*(?:\*\*)?\[([A-Za-z0-9_\-]+)\](?:\*\*)?", content):
        tag = match.group(1).strip()
        if tag and tag not in tags:
            tags.append(tag)

    if not tags:
        ac_idx = content.find("## Acceptance Criteria")
        search_slice = content[ac_idx:] if ac_idx != -1 else content
        for match in re.finditer(r"\*\*\[([A-Za-z0-9_\-]+)\]\*\*", search_slice):
            tag = match.group(1).strip()
            if tag and tag not in tags:
                tags.append(tag)

    if not tags:
        for match in re.finditer(r"(?://\s*)?Scenario:\s*\[([A-Za-z0-9_\-]+)\]", content, re.IGNORECASE):
            tag = match.group(1).strip()
            if tag and tag not in tags:
                tags.append(tag)

    return tags


def _normalize_ticket_display_path(
    path_val: Path | str,
    root_dir: Path | str | None = None,
) -> str:
    """Normalizes ticket path relative to workspace or .scratch anchor."""
    p = Path(path_val)
    parts = p.parts
    if ".scratch" in parts:
        idx = parts.index(".scratch")
        return "/".join(parts[idx:])

    if root_dir:
        try:
            rel = p.resolve().relative_to(Path(root_dir).resolve())
            return rel.as_posix()
        except ValueError:
            pass

    try:
        rel = p.resolve().relative_to(REPO_ROOT.resolve())
        return rel.as_posix()
    except ValueError:
        pass

    try:
        rel = p.resolve().relative_to(Path.cwd().resolve())
        return rel.as_posix()
    except ValueError:
        pass

    return p.as_posix()


def _parse_ticket_file_metadata(content: str, filename: str = "") -> dict[str, Any]:
    """Parses ticket frontmatter, execution pointers, and scenario tags from markdown text."""
    meta: dict[str, Any] = {}

    title_m = re.search(r"^#+\s+(?:[0-9]{1,3}\s*:\s*)?(.+)$", content, re.MULTILINE)
    if title_m and title_m.group(1).strip():
        meta["title"] = title_m.group(1).strip()
    else:
        meta["title"] = ""

    m_id = re.match(r"^([0-9]{1,3})", filename)
    if m_id:
        meta["id"] = f"{int(m_id.group(1)):02d}"
    else:
        h1_id = re.search(r"^#+\s+([0-9]{1,3})\s*:", content, re.MULTILINE)
        if h1_id:
            meta["id"] = f"{int(h1_id.group(1)):02d}"
        else:
            meta["id"] = ""

    for match in re.finditer(
        r"^[ \t]*(?:[-*][ \t]+)?\*\*([A-Za-z0-9_\s-]+?)\s*(?::\*\*|\*\*\s*:)[ \t]*(.*?)$",
        content,
        re.MULTILINE,
    ):
        k = match.group(1).strip().lower()
        v = match.group(2).strip()
        v = re.sub(r"<!--.*?-->", "", v).strip()
        meta[k] = v

    if content.strip().startswith("---"):
        try:
            fm_match = re.search(r"^---\s*\n(.*?)\n---", content.strip(), re.DOTALL)
            if fm_match:
                for line in fm_match.group(1).splitlines():
                    if ":" in line:
                        yk, yv = line.split(":", 1)
                        meta[yk.strip().lower()] = yv.strip().strip('"\'')
        except Exception:
            pass

    return {
        "id": meta.get("id", ""),
        "title": meta.get("title", ""),
        "seam": meta.get("seam", ""),
        "verification": meta.get("verification", ""),
        "context": meta.get("context", ""),
        "status": meta.get("status", "ready-for-agent"),
        "blocked_by": meta.get("blocked by", meta.get("blocked_by", "")),
        "scenario_tags": extract_ticket_scenario_tags(content),
    }


def format_single_starter_prompt(
    ticket: dict[str, Any],
    is_final_frontier: bool = False,
    lean: bool = False,
) -> str:
    """Formats a single self-contained worker starter prompt enclosed in a markdown code fence or 4-backtick bash block."""
    target_path = ticket.get("path") or f".scratch/<feature-slug>/issues/{ticket.get('id', 'NN')}.md"
    if ".scratch" in Path(target_path).parts:
        target_path = _normalize_ticket_display_path(target_path)
    elif ticket.get("task_graph_path") and ".scratch" in Path(ticket["task_graph_path"]).parts:
        tg_parts = Path(ticket["task_graph_path"]).parts
        s_idx = tg_parts.index(".scratch")
        if len(tg_parts) > s_idx + 1:
            slug = tg_parts[s_idx + 1]
            ticket_filename = Path(target_path).name
            target_path = f".scratch/{slug}/issues/{ticket_filename}"

    if lean:
        return f"````bash\n/implement {target_path}\n````"

    seam = ticket.get("seam") or "<seam-path-or-public-interface>"
    verification = ticket.get("verification") or "<npm test ... / tsc --noEmit>"

    tags = ticket.get("scenario_tags") or []
    if tags:
        clean_tags = ", ".join(f"[{t.strip('[]')}]" for t in tags)
        scenario_tags_val = f"// Scenario: {clean_tags}"
    else:
        scenario_tags_val = "// Scenario: [TAG-xx]"

    tg_path = ticket.get("task_graph_path") or ".scratch/<feature-slug>/task-graph.md"

    if is_final_frontier:
        completion_rule = (
            "5. IMPORTANT: This is the final ticket on the DAG. Do not draft prompts for subsequent tickets "
            "(jangan membuat prompt untuk tiket selanjutnya, downstream frontier calculation is reserved exclusively for the Command Center session). "
            "Conclude turn with Recommend-and-Stop: confirm ticket resolution and "
            "recommend the operator report that all tickets are resolved (\"semua tiket selesai\") "
            "to the Command Center (CC) session to initiate downstream evaluation phases (fase evaluasi lanjutan, such as /code-review or /pr)."
        )
    else:
        completion_rule = (
            "5. IMPORTANT: Do not draft prompts for subsequent tickets or scan next tickets "
            "(jangan membuat prompt untuk tiket selanjutnya, downstream frontier calculation is reserved exclusively for the Command Center session). "
            "Conclude turn with Recommend-and-Stop: confirm ticket resolution and "
            "recommend reporting back to the Command Center (CC) session without drafting downstream prompts."
        )

    prompt = f"""```markdown
Target Ticket: {target_path}
Architectural Seam: {seam}
Spec Scenario Tags: {scenario_tags_val}
Verification Gate: {verification}

Execution Instructions:
1. Jalankan /implement {target_path}
2. Apply TDD Red-Green-Refactor and ensure spec scenarios are covered.
3. Satisfy Double-Verification Gate with exit code 0.
4. Perform atomic commit to HEAD, update ticket frontmatter to Status: resolved, and update {tg_path}.
{completion_rule}
```"""
    return prompt


class TicketBriefValidationResult:
    """
    [ERR-BRIEF-01] [ERR-BRIEF-02] Structured outcome of ticket validation for worker starter brief synthesis.
    Encapsulates schema validation status, boundary error diagnostics, and resolved execution contracts.
    """
    is_valid: bool
    errors: list[str]
    diagnostic: str
    ticket_data: dict[str, Any]

    def __init__(
        self,
        is_valid: bool,
        errors: Sequence[str] | None = None,
        diagnostic: str = "",
        ticket_data: dict[str, Any] | None = None,
    ) -> None:
        self.is_valid = is_valid
        self.errors = list(errors or [])
        self.diagnostic = diagnostic
        self.ticket_data = dict(ticket_data or {})

    @property
    def ok(self) -> bool:
        return self.is_valid

    @property
    def seam(self) -> str:
        return str(self.ticket_data.get("seam", ""))

    def __bool__(self) -> bool:
        return self.is_valid

    def __iter__(self) -> Iterator[bool | list[str] | str]:
        return iter((self.is_valid, self.errors, self.diagnostic))

    def __getitem__(self, index: int) -> bool | list[str] | str:
        return (self.is_valid, self.errors, self.diagnostic)[index]

    def __len__(self) -> int:
        return 3

    def __repr__(self) -> str:
        return (
            f"TicketBriefValidationResult(is_valid={self.is_valid}, "
            f"errors={self.errors!r}, diagnostic={self.diagnostic!r})"
        )


def _resolve_seam_from_spec(
    context_ref: str,
    ticket_content: str = "",
    root_dir: Path | str | None = None,
    ticket_path: Path | str | None = None,
    spec_path: Path | str | None = None,
) -> str:
    """
    [ERR-BRIEF-01] Resolves architectural seam from ticket body or referenced specification section.
    """
    if ticket_content:
        m_body = re.search(
            r"(?:Target\s+Seam|Architectural\s+Seam|Seam)\s*:\s*[`'\"]?([a-zA-Z0-9_/\\.-]+#[a-zA-Z0-9_.]+|[a-zA-Z0-9_/\\.-]+\.(?:py|ts|js|tsx|jsx|sh|rs|go))[`'\"]?",
            ticket_content,
            re.IGNORECASE,
        )
        if m_body:
            val = m_body.group(1).strip()
            if val and val.lower() not in ("none", "<seam-path-or-public-interface>"):
                return val

    target_spec_file: Path | None = None
    line_range: tuple[int, int] | None = None

    if spec_path:
        p_cand = Path(spec_path)
        if p_cand.is_file():
            target_spec_file = p_cand

    if not target_spec_file and context_ref:
        parts = context_ref.split("#", 1)
        file_part = parts[0].strip().strip("`'\"")
        file_part = re.sub(r"^\[.*?\]\(", "", file_part).rstrip(")")
        anchor_part = parts[1].strip() if len(parts) > 1 else ""

        if anchor_part:
            m_lines = re.match(r"^L?(\d+)(?:-L?(\d+))?$", anchor_part, re.IGNORECASE)
            if m_lines:
                start_l = int(m_lines.group(1))
                end_l = int(m_lines.group(2)) if m_lines.group(2) else start_l
                line_range = (start_l, end_l)

        if file_part:
            candidates: list[Path] = []
            if root_dir:
                candidates.append(Path(root_dir) / file_part)
            if ticket_path:
                t_p = Path(ticket_path)
                candidates.append(t_p.parent / file_part)
                candidates.append(t_p.parent.parent / file_part)
                if ".scratch" in t_p.parts:
                    s_idx = t_p.parts.index(".scratch")
                    slug = t_p.parts[s_idx + 1] if len(t_p.parts) > s_idx + 1 else None
                    if slug:
                        if root_dir:
                            candidates.append(Path(root_dir) / ".scratch" / slug / "spec.md")
                        candidates.append(REPO_ROOT / ".scratch" / slug / "spec.md")
            candidates.append(Path(file_part))
            candidates.append(REPO_ROOT / file_part)
            for c in candidates:
                if c.is_file():
                    target_spec_file = c
                    break

    if not target_spec_file and ticket_path:
        t_p = Path(ticket_path)
        if t_p.parent.name == "issues" and (t_p.parent.parent / "spec.md").is_file():
            target_spec_file = t_p.parent.parent / "spec.md"

    if target_spec_file and target_spec_file.is_file():
        try:
            spec_text = target_spec_file.read_text(encoding="utf-8")
            if line_range:
                lines = spec_text.splitlines()
                s_idx = max(0, line_range[0] - 1)
                e_idx = min(len(lines), line_range[1])
                target_text = "\n".join(lines[s_idx:e_idx])
            else:
                target_text = spec_text

            m_spec = re.search(
                r"(?:Target\s+Seam|Architectural\s+Seam|Seam)\s*:\s*[`'\"]?([a-zA-Z0-9_/\\.-]+#[a-zA-Z0-9_.]+|[a-zA-Z0-9_/\\.-]+\.(?:py|ts|js|tsx|jsx|sh|rs|go))[`'\"]?",
                target_text,
                re.IGNORECASE,
            )
            if m_spec:
                val = m_spec.group(1).strip()
                if val and val.lower() not in ("none", "<seam-path-or-public-interface>"):
                    return val

            m_symbol = re.search(
                r"[`'\"]([a-zA-Z0-9_/\\.-]+\.(?:py|ts|js|tsx|jsx|sh|rs|go)#[a-zA-Z0-9_.]+)[`'\"]",
                target_text,
            )
            if m_symbol:
                return m_symbol.group(1).strip()
        except Exception:
            pass

    return ""


def validate_ticket_brief_schema(
    ticket_input: Path | str | dict[str, Any],
    root_dir: Path | str | None = None,
    spec_path: Path | str | None = None,
) -> TicketBriefValidationResult:
    """
    [ERR-BRIEF-01] [ERR-BRIEF-02] Boundary schema validator for Command Center worker starter brief synthesis.
    Validates ticket file existence, YAML/Markdown frontmatter integrity (mandatory 'Status:' and 'Blocked by:'),
    and resolves architectural seams (from ticket frontmatter or fallback specification section).
    """
    base_root = Path(root_dir) if root_dir else None
    errors: list[str] = []

    ticket_file_path: Path | None = None
    raw_content: str = ""
    ticket_name: str = "ticket"
    existing_meta: dict[str, Any] = {}

    if isinstance(ticket_input, dict):
        existing_meta = dict(ticket_input)
        ticket_name = str(existing_meta.get("title") or existing_meta.get("id") or "ticket")
        p_val = existing_meta.get("path")
        if p_val:
            cand = Path(p_val)
            if not cand.is_file() and base_root:
                cand_root = base_root / cand
                if cand_root.is_file():
                    cand = cand_root
                else:
                    matches = list(base_root.glob(f"**/{cand.name}"))
                    if matches:
                        cand = matches[0]
            if not cand.is_file():
                matches = list(REPO_ROOT.glob(f"**/{cand.name}"))
                if matches:
                    cand = matches[0]

            if not cand.is_file():
                search_anchor = base_root or REPO_ROOT
                try:
                    v_tmp = ManifestValidator()
                    resolved = v_tmp.resolve_ticket_shorthand(search_anchor, str(p_val))
                    if resolved and resolved.is_file():
                        cand = resolved
                except Exception:
                    pass

            if not cand.is_file():
                err = f"Ticket file does not exist or unanchored path: '{p_val}'"
                diag = (
                    f"> ⚠️ Warning: [ERR-BRIEF-01] Malformed ticket '{cand.name or p_val}': "
                    f"Ticket file does not exist or has an unanchored path ('{p_val}'). "
                    "Prompt generation halted for this ticket. "
                    "Please inspect '.scratch/<feature-slug>/issues/' or re-scaffold tickets."
                )
                return TicketBriefValidationResult(
                    is_valid=False,
                    errors=[err],
                    diagnostic=diag,
                    ticket_data={"path": str(p_val), "id": str(existing_meta.get("id", "NN"))},
                )
            ticket_file_path = cand
            ticket_name = cand.name
            try:
                raw_content = cand.read_text(encoding="utf-8")
            except OSError as exc:
                err = f"Failed to read ticket file '{cand}': {exc}"
                diag = (
                    f"> ⚠️ Warning: [ERR-BRIEF-01] Malformed ticket '{cand.name}': "
                    f"Cannot read ticket file on disk ({exc}). Prompt generation halted for this ticket. "
                    "Please inspect '.scratch/<feature-slug>/issues/' or re-scaffold tickets."
                )
                return TicketBriefValidationResult(
                    is_valid=False,
                    errors=[err],
                    diagnostic=diag,
                    ticket_data={"path": str(cand), "id": str(existing_meta.get("id", "NN"))},
                )
    elif isinstance(ticket_input, (str, Path)):
        p_str = str(ticket_input)
        if "\n" in p_str or p_str.strip().startswith("#") or p_str.strip().startswith("---"):
            raw_content = p_str
            ticket_name = "ticket"
        else:
            cand = Path(ticket_input)
            if not cand.is_file() and base_root:
                cand_root = base_root / cand
                if cand_root.is_file():
                    cand = cand_root
                else:
                    matches = list(base_root.glob(f"**/{cand.name}"))
                    if matches:
                        cand = matches[0]
            if not cand.is_file():
                matches = list(REPO_ROOT.glob(f"**/{cand.name}"))
                if matches:
                    cand = matches[0]

            if not cand.is_file():
                search_anchor = base_root or REPO_ROOT
                try:
                    v_tmp = ManifestValidator()
                    resolved = v_tmp.resolve_ticket_shorthand(search_anchor, str(ticket_input))
                    if resolved and resolved.is_file():
                        cand = resolved
                except Exception:
                    pass

            if not cand.is_file():
                err = f"Ticket file does not exist or unanchored path: '{ticket_input}'"
                diag = (
                    f"> ⚠️ Warning: [ERR-BRIEF-01] Malformed ticket '{cand.name or ticket_input}': "
                    f"Ticket file does not exist or has an unanchored path ('{ticket_input}'). "
                    "Prompt generation halted for this ticket. "
                    "Please inspect '.scratch/<feature-slug>/issues/' or re-scaffold tickets."
                )
                return TicketBriefValidationResult(
                    is_valid=False,
                    errors=[err],
                    diagnostic=diag,
                    ticket_data={"path": str(ticket_input), "id": "NN"},
                )
            ticket_file_path = cand
            ticket_name = cand.name
            try:
                raw_content = cand.read_text(encoding="utf-8")
            except OSError as exc:
                err = f"Failed to read ticket file '{cand}': {exc}"
                diag = (
                    f"> ⚠️ Warning: [ERR-BRIEF-01] Malformed ticket '{cand.name}': "
                    f"Cannot read ticket file on disk ({exc}). Prompt generation halted for this ticket. "
                    "Please inspect '.scratch/<feature-slug>/issues/' or re-scaffold tickets."
                )
                return TicketBriefValidationResult(
                    is_valid=False,
                    errors=[err],
                    diagnostic=diag,
                    ticket_data={"path": str(cand), "id": "NN"},
                )

    found_keys: set[str] = set()
    parsed_meta: dict[str, Any] = {}

    if raw_content:
        if raw_content.strip().startswith("---"):
            if parse_frontmatter is not None:
                try:
                    fm_dict = parse_frontmatter(raw_content.strip())
                    for k, v in fm_dict.items():
                        norm_k = str(k).strip().lower().replace("-", "_")
                        if norm_k in ("blocked_by", "blocked by"):
                            found_keys.add("blocked_by")
                        elif norm_k == "status":
                            found_keys.add("status")
                        elif norm_k == "seam":
                            found_keys.add("seam")
                        elif norm_k == "context":
                            found_keys.add("context")
                        elif norm_k == "verification":
                            found_keys.add("verification")
                        parsed_meta[norm_k] = v
                except Exception as exc:
                    err = f"Unparseable YAML frontmatter: {exc}"
                    diag = (
                        f"> ⚠️ Warning: [ERR-BRIEF-01] [ERR-BRIEF-02] Malformed ticket '{ticket_name}': "
                        f"Boundary schema validation failed due to unparseable metadata lines ({exc}). "
                        "Starter brief emission halted."
                    )
                    return TicketBriefValidationResult(
                        is_valid=False,
                        errors=[err],
                        diagnostic=diag,
                        ticket_data={"path": str(ticket_file_path or ticket_input)},
                    )

        for match in re.finditer(
            r"^[ \t]*(?:[-*][ \t]+)?\*\*([A-Za-z0-9_\s-]+?)\s*(?::\*\*|\*\*\s*:)[ \t]*(.*?)$",
            raw_content,
            re.MULTILINE,
        ):
            k = match.group(1).strip().lower().replace("-", "_")
            v = match.group(2).strip()
            v = re.sub(r"<!--.*?-->", "", v).strip()
            if k in ("blocked_by", "blocked by"):
                found_keys.add("blocked_by")
                parsed_meta["blocked_by"] = v
            elif k == "status":
                found_keys.add("status")
                parsed_meta["status"] = v
            elif k == "seam":
                if v:
                    found_keys.add("seam")
                parsed_meta["seam"] = v
            elif k == "context":
                found_keys.add("context")
                parsed_meta["context"] = v
            elif k == "verification":
                found_keys.add("verification")
                parsed_meta["verification"] = v
            else:
                parsed_meta[k] = v

        full_meta = _parse_ticket_file_metadata(raw_content, filename=ticket_name)
        full_meta.update(parsed_meta)
        if existing_meta:
            full_meta.update(existing_meta)
    else:
        full_meta = dict(existing_meta)
        if "status" in full_meta:
            found_keys.add("status")
        if "blocked_by" in full_meta or "blocked by" in full_meta:
            found_keys.add("blocked_by")
        if full_meta.get("seam"):
            found_keys.add("seam")

    if ticket_file_path:
        full_meta["path"] = _normalize_ticket_display_path(ticket_file_path, root_dir=base_root)

    missing_fields: list[str] = []
    if raw_content:
        if "status" not in found_keys:
            missing_fields.append("Status")
        if "blocked_by" not in found_keys:
            missing_fields.append("Blocked by")
    elif existing_meta:
        if "status" not in found_keys:
            missing_fields.append("Status")
        if "blocked_by" not in found_keys:
            missing_fields.append("Blocked by")

    id_val = str(full_meta.get("id") or "").strip()
    title_val = str(full_meta.get("title") or "").strip()
    if not id_val or id_val.lower() in ("nn", "none"):
        missing_fields.append("Ticket ID")
    if not title_val:
        missing_fields.append("Title")

    if missing_fields:
        for f in missing_fields:
            errors.append(f"Missing mandatory frontmatter field: '{f}'")
        diag = (
            f"> ⚠️ Warning: [ERR-BRIEF-01] [ERR-BRIEF-02] Malformed ticket '{ticket_name}': "
            f"Missing mandatory frontmatter field(s): {', '.join(missing_fields)}. "
            "Boundary schema validation failed; starter brief emission halted."
        )
        return TicketBriefValidationResult(
            is_valid=False,
            errors=errors,
            diagnostic=diag,
            ticket_data=full_meta,
        )

    seam_val = str(full_meta.get("seam", "")).strip()
    if not seam_val or seam_val.lower() in ("none", "<seam-path-or-public-interface>", "tbd"):
        context_ref = str(full_meta.get("context", "")).strip()
        resolved_seam = _resolve_seam_from_spec(
            context_ref=context_ref,
            ticket_content=raw_content,
            root_dir=base_root,
            ticket_path=ticket_file_path,
            spec_path=spec_path,
        )
        if resolved_seam:
            full_meta["seam"] = resolved_seam
            seam_val = resolved_seam
        else:
            err = (
                "Missing architectural seam: Ticket lacks explicit 'Seam:' and "
                "seam cannot be resolved from referenced specification section"
            )
            errors.append(err)
            diag = (
                f"> ⚠️ Warning: [ERR-BRIEF-01] Malformed ticket '{ticket_name}': "
                "Architectural seam cannot be resolved from ticket frontmatter or referenced specification section. "
                "Prompt generation halted for this ticket."
            )
            return TicketBriefValidationResult(
                is_valid=False,
                errors=errors,
                diagnostic=diag,
                ticket_data=full_meta,
            )

    return TicketBriefValidationResult(
        is_valid=True,
        errors=[],
        diagnostic="",
        ticket_data=full_meta,
    )


validate_ticket_for_brief = validate_ticket_brief_schema


def format_worker_brief(
    ticket_input: Path | str | dict[str, Any] | Sequence[Path | str | dict[str, Any]],
    root_dir: Path | str | None = None,
    validator: ManifestValidator | None = None,
    feature_slug: str | None = None,
    include_heading: bool = True,
    is_final_frontier: bool | None = None,
    lean: bool = False,
) -> str:
    """
    [BRIEF-01] [BRIEF-02] [EDGE-BRIEF-01] Synthesizes structured Self-Contained Worker Starter Brief(s)
    for unblocked tickets on the active DAG frontier.
    Guarantees that briefs include Target Ticket, Architectural Seam, Spec Scenario Tags,
    Verification Gate, and strict completion instructions prohibiting downstream prompt generation.
    Supports multi-frontier presentation for parallel or serial operator execution,
    and automatically enriches final frontier tickets with completion protocol directives ('semua tiket selesai').
    """
    v = validator or ManifestValidator()
    base_root = Path(root_dir) if root_dir else None

    if isinstance(ticket_input, int):
        ticket_input = f"{ticket_input:02d}"

    is_ticket_file: bool = False
    p: Path | None = None

    if isinstance(ticket_input, (str, Path)):
        p = Path(ticket_input)
        is_ticket_file = (
            p.suffix.lower() in (".md", ".markdown")
            and p.name.lower() not in ("task-graph.md", "task-graph.markdown")
        )
        if not is_ticket_file and not p.is_dir() and p.name.lower() not in ("task-graph.md", "task-graph.markdown"):
            search_anchor = base_root or REPO_ROOT
            try:
                resolved_ticket = v.resolve_ticket_shorthand(search_anchor, str(ticket_input))
                if resolved_ticket and resolved_ticket.is_file():
                    p = resolved_ticket
                    is_ticket_file = True
            except Exception:
                pass

        if not is_ticket_file and (p.name.lower() in ("task-graph.md", "task-graph.markdown") or p.is_dir() or not p.exists()):
            target_search = p
            if not target_search.exists() and base_root:
                cand = base_root / p
                if cand.exists():
                    target_search = cand

            scanned = v.scan_tickets_from_tracker(target_search)
            if not scanned and (target_search / ".scratch").is_dir():
                scratch_dir = target_search / ".scratch"
                feat_dirs = [d for d in sorted(scratch_dir.iterdir()) if d.is_dir() and (d / "issues").is_dir()]
                if len(feat_dirs) == 1:
                    scanned = v.scan_tickets_from_tracker(feat_dirs[0])
                elif len(feat_dirs) > 1:
                    active_branch = v._detect_active_branch(target_search)
                    matching = [d for d in feat_dirs if d.name in active_branch] if active_branch else []
                    if len(matching) == 1:
                        scanned = v.scan_tickets_from_tracker(matching[0])

            cycle_nodes, orphan_ids, dag_diagnostics = detect_ticket_dag_anomalies(scanned, validator=v, root_dir=base_root)

            frontier = v.calculate_unblocked_frontier(scanned)

            canon_cycle_nodes = {_canonical_ticket_id(x) for x in cycle_nodes}
            canon_orphan_ids = {_canonical_ticket_id(x) for x in orphan_ids}

            frontier_set = {
                _canonical_ticket_id(x) for x in frontier
                if _canonical_ticket_id(x) not in canon_cycle_nodes and _canonical_ticket_id(x) not in canon_orphan_ids
            }
            unblocked_tickets: list[dict[str, Any]] = []
            has_in_progress_frontier = False
            for t in scanned:
                tid = _canonical_ticket_id(_extract_ticket_id(t))
                if tid in frontier_set:
                    st = str(t.get("status", ""))
                    if v._is_status_in_progress(st):
                        has_in_progress_frontier = True
                    elif not v._is_status_resolved(st):
                        unblocked_tickets.append(t)

            if not unblocked_tickets:
                if dag_diagnostics:
                    return "\n\n".join(dag_diagnostics)
                if has_in_progress_frontier or any(v._is_status_in_progress(str(t.get("status", ""))) for t in scanned):
                    return (
                        "> ⏳ Standby: All active frontier tickets are currently in-progress. "
                        "Awaiting worker completion before calculating downstream unblocked tickets."
                    )
                return "> ⏳ Standby: All active frontier tickets are currently in-progress or no unblocked tickets remain."

            final_detected = is_final_frontier
            unresolved = [t for t in scanned if not v._is_status_resolved(t.get("status", ""))]
            if final_detected is None:
                if len(scanned) > 0 and len(unresolved) == 1 and len(unblocked_tickets) == 1:
                    final_detected = True
                else:
                    final_detected = False
            elif final_detected is True:
                if len(scanned) > 0 and (len(unresolved) > 1 or len(unblocked_tickets) > 1):
                    unresolved_ids = ", ".join(str(t.get("id", "")) for t in unresolved)
                    diag = (
                        f"> ⚠️ [EDGE-BRIEF-01] Final frontier brief requested, but DAG contains {len(unresolved)} "
                        f"unresolved tickets ({unresolved_ids}). Emitting standard multi-frontier briefs."
                    )
                    sub_briefs = format_worker_brief(
                        unblocked_tickets,
                        root_dir=base_root,
                        validator=v,
                        feature_slug=feature_slug,
                        include_heading=include_heading,
                        is_final_frontier=False,
                        lean=lean,
                    )
                    combined_brief = f"{diag}\n\n{sub_briefs}"
                    if dag_diagnostics:
                        return f"{'\n\n'.join(dag_diagnostics)}\n\n{combined_brief}"
                    return combined_brief

            sub_briefs = format_worker_brief(
                unblocked_tickets,
                root_dir=base_root,
                validator=v,
                feature_slug=feature_slug,
                include_heading=include_heading,
                is_final_frontier=final_detected,
                lean=lean,
            )
            if dag_diagnostics:
                return f"{'\n\n'.join(dag_diagnostics)}\n\n{sub_briefs}"
            return sub_briefs

    if is_ticket_file:
        items = [p] if p is not None else [ticket_input]
        item_cycle_nodes: set[str] = set()
        item_orphan_ids: set[str] = set()
        item_dag_diags: list[str] = []
    elif isinstance(ticket_input, (list, tuple)):
        items = list(ticket_input)
        item_cycle_nodes, item_orphan_ids, item_dag_diags = detect_ticket_dag_anomalies(items, validator=v, root_dir=base_root)
    else:
        items = [ticket_input]
        item_cycle_nodes, item_orphan_ids, item_dag_diags = detect_ticket_dag_anomalies(items, validator=v, root_dir=base_root)

    canon_item_cycle_nodes = {_canonical_ticket_id(x) for x in item_cycle_nodes}
    canon_item_orphan_ids = {_canonical_ticket_id(x) for x in item_orphan_ids}

    normalized_tickets: list[dict[str, Any]] = []
    warning_diagnostics: list[str] = list(item_dag_diags)

    for item in items:
        val_res = validate_ticket_brief_schema(item, root_dir=base_root)
        if not val_res.is_valid:
            if val_res.diagnostic:
                warning_diagnostics.append(val_res.diagnostic)
            continue

        t_data = dict(val_res.ticket_data)
        item_tid = _canonical_ticket_id(_extract_ticket_id(t_data))
        if item_tid in canon_item_cycle_nodes or item_tid in canon_item_orphan_ids:
            continue
        p_val = t_data.get("path", "")
        if p_val and not t_data.get("task_graph_path"):
            parts = Path(p_val).parts
            if ".scratch" in parts:
                s_idx = parts.index(".scratch")
                slug = parts[s_idx + 1] if len(parts) > s_idx + 1 else "feature"
                t_data["task_graph_path"] = f".scratch/{slug}/task-graph.md"
            elif Path(p_val).parent.name == "issues":
                t_data["task_graph_path"] = _normalize_ticket_display_path(
                    Path(p_val).parent.parent / "task-graph.md", root_dir=base_root
                )
        if not t_data.get("scenario_tags") and p_val and Path(p_val).is_file():
            try:
                t_data["scenario_tags"] = extract_ticket_scenario_tags(Path(p_val).read_text(encoding="utf-8"))
            except Exception:
                pass

        normalized_tickets.append(t_data)

    warnings_prefix = "\n\n".join(warning_diagnostics) if warning_diagnostics else ""

    if not is_ticket_file:
        ready_tickets = [t for t in normalized_tickets if not v._is_status_in_progress(str(t.get("status", "")))]
        if normalized_tickets and not ready_tickets:
            standby_notice = (
                "> ⏳ Standby: All active frontier tickets are currently in-progress. "
                "Awaiting worker completion before calculating downstream unblocked tickets."
            )
            if warnings_prefix:
                return f"{warnings_prefix}\n\n{standby_notice}"
            return standby_notice
        normalized_tickets = ready_tickets

    if not normalized_tickets:
        if warnings_prefix:
            return warnings_prefix
        return "> ⏳ Standby: No valid frontier tickets could be formatted for starter brief emission."

    final_flag = bool(is_final_frontier)
    if final_flag and len(normalized_tickets) > 1:
        final_flag = False
        warning_diagnostics.append(
            f"> ⚠️ [EDGE-BRIEF-01] Final frontier brief requires exactly one final frontier ticket, but received {len(normalized_tickets)} tickets. Emitting standard multi-frontier briefs."
        )
        warnings_prefix = "\n\n".join(warning_diagnostics) if warning_diagnostics else ""

    if len(normalized_tickets) == 1:
        t = normalized_tickets[0]
        prompt = format_single_starter_prompt(t, is_final_frontier=final_flag, lean=lean)
        if lean:
            brief_content = prompt
        elif include_heading:
            tid = t.get("id", "01")
            title = t.get("title", "")
            brief_content = f"### 🎯 Starter Brief: Ticket {tid} - {title}\n\n{prompt}"
        else:
            brief_content = prompt

        if warnings_prefix:
            return f"{warnings_prefix}\n\n{brief_content}"
        return brief_content

    brief_blocks: list[str] = []
    for idx, t in enumerate(normalized_tickets, start=1):
        tid = t.get("id", f"{idx:02d}")
        title = t.get("title", f"Ticket {tid}")
        prompt = format_single_starter_prompt(t, is_final_frontier=final_flag, lean=lean)
        if lean:
            brief_blocks.append(prompt)
        elif include_heading:
            heading = f"### 🎯 Frontier Option {idx}: Ticket {tid} - {title}"
            brief_blocks.append(f"{heading}\n\n{prompt}")
        else:
            brief_blocks.append(prompt)

    multi_briefs = "\n\n".join(brief_blocks)
    if warnings_prefix:
        return f"{warnings_prefix}\n\n{multi_briefs}"
    return multi_briefs


def format_final_frontier_worker_brief(
    ticket_input: Path | str | dict[str, Any] | Sequence[Path | str | dict[str, Any]],
    root_dir: Path | str | None = None,
    validator: ManifestValidator | None = None,
    feature_slug: str | None = None,
    include_heading: bool = True,
    lean: bool = False,
) -> str:
    """
    [EDGE-BRIEF-01] Synthesizes a specialized Self-Contained Worker Starter Brief for the final frontier ticket
    when only one unblocked ticket remains before full DAG resolution.
    Enriches execution instructions with explicit completion protocol directives reminding the worker
    and operator to report final DAG completion ('semua tiket selesai') to Command Center (CC)
    to initiate downstream evaluation phases (/code-review, /security-audit, /pr).
    """
    return format_worker_brief(
        ticket_input,
        root_dir=root_dir,
        validator=validator,
        feature_slug=feature_slug,
        include_heading=include_heading,
        is_final_frontier=True,
        lean=lean,
    )


format_final_frontier_brief = format_final_frontier_worker_brief



class TaskDashboardResult(str):
    """
    [DASH-01] [DASH-02] Structured outcome of Command Center visual task dashboard rendering
    and contextual chat stream generation. Inherits from str returning the markdown artifact
    content directly for full backward compatibility, while exposing structured properties
    for chat stream and frontier orchestration.
    """
    artifact: str
    chat_stream: str
    frontier: list[str]
    all_resolved: bool
    total_tickets: int
    resolved_tickets: int
    recovery_diagnostic: str

    def __new__(
        cls,
        artifact: str,
        chat_stream: str = "",
        frontier: list[str] | None = None,
        all_resolved: bool = False,
        total_tickets: int = 0,
        resolved_tickets: int = 0,
        recovery_diagnostic: str = "",
    ) -> TaskDashboardResult:
        instance = super().__new__(cls, artifact)
        instance.artifact = artifact
        instance.chat_stream = chat_stream
        instance.frontier = list(frontier or [])
        instance.all_resolved = all_resolved
        instance.total_tickets = total_tickets
        instance.resolved_tickets = resolved_tickets
        instance.recovery_diagnostic = recovery_diagnostic
        return instance

    @property
    def dashboard(self) -> str:
        return self.artifact

    @property
    def chat_handoff(self) -> str:
        return self.chat_stream

    def __iter__(self) -> Iterator[str]:
        return iter((self.artifact, self.chat_stream))


class DashboardRecoveryResult(TaskDashboardResult):
    """
    [ERR-DASH-01] Structured outcome of Command Center task dashboard self-healing recovery.
    Inherits from TaskDashboardResult (and str) for full backward compatibility,
    while exposing recovery diagnostics and status properties.
    """
    is_recovered: bool
    was_malformed: bool
    diagnostic: str
    errors: list[str]
    task_graph_path: Path | None
    feature_dir: Path | None
    tickets: list[dict[str, Any]]

    def __new__(
        cls,
        artifact: str,
        chat_stream: str = "",
        frontier: list[str] | None = None,
        all_resolved: bool = False,
        total_tickets: int = 0,
        resolved_tickets: int = 0,
        recovery_diagnostic: str = "",
        *,
        is_recovered: bool = False,
        was_malformed: bool = False,
        diagnostic: str = "",
        errors: Sequence[str] | None = None,
        task_graph_path: Path | str | None = None,
        feature_dir: Path | str | None = None,
        tickets: Sequence[dict[str, Any]] | None = None,
    ) -> DashboardRecoveryResult:
        diag = diagnostic or recovery_diagnostic
        instance = super().__new__(
            cls,
            artifact=artifact,
            chat_stream=chat_stream,
            frontier=frontier,
            all_resolved=all_resolved,
            total_tickets=total_tickets,
            resolved_tickets=resolved_tickets,
            recovery_diagnostic=diag,
        )
        instance.is_recovered = is_recovered
        instance.was_malformed = was_malformed
        instance.diagnostic = diag
        instance.errors = list(errors or [])
        instance.task_graph_path = Path(task_graph_path) if task_graph_path else None
        instance.feature_dir = Path(feature_dir) if feature_dir else None
        instance.tickets = list(tickets or [])
        return instance

    @property
    def recovered(self) -> bool:
        return self.is_recovered

    @property
    def is_valid(self) -> bool:
        return bool(self.artifact and not self.errors)

    @property
    def message(self) -> str:
        return self.diagnostic


class LegacyUpgradeResult(DashboardRecoveryResult):
    """
    [EDGE-UPGRADE-01] Structured outcome of legacy flat task graph upgrade to Clustered Subgraphs.
    Inherits from DashboardRecoveryResult, TaskDashboardResult, and str for 100% backward compatibility.
    """
    is_upgraded: bool

    def __new__(
        cls,
        artifact: str,
        chat_stream: str = "",
        frontier: list[str] | None = None,
        all_resolved: bool = False,
        total_tickets: int = 0,
        resolved_tickets: int = 0,
        recovery_diagnostic: str = "",
        *,
        is_upgraded: bool = True,
        is_recovered: bool = True,
        was_malformed: bool = False,
        diagnostic: str = "",
        errors: Sequence[str] | None = None,
        task_graph_path: Path | str | None = None,
        feature_dir: Path | str | None = None,
        tickets: Sequence[dict[str, Any]] | None = None,
    ) -> LegacyUpgradeResult:
        diag = diagnostic or recovery_diagnostic
        instance = super().__new__(
            cls,
            artifact=artifact,
            chat_stream=chat_stream,
            frontier=frontier,
            all_resolved=all_resolved,
            total_tickets=total_tickets,
            resolved_tickets=resolved_tickets,
            recovery_diagnostic=diag,
            is_recovered=is_recovered,
            was_malformed=was_malformed,
            diagnostic=diag,
            errors=errors,
            task_graph_path=task_graph_path,
            feature_dir=feature_dir,
            tickets=tickets,
        )
        instance.is_upgraded = is_upgraded
        return instance


def is_legacy_task_graph(content: str) -> bool:
    """
    [EDGE-UPGRADE-01] Detects if a task-graph.md artifact uses pre-ADR-0040 legacy formatting
    (flat unclustered Mermaid nodes, missing canonical S_* subgraphs, or CSS classDef styling).
    """
    if not content or not content.strip():
        return False

    mermaid_m = re.search(r"```mermaid\s*\n(.*?)```", content, re.DOTALL | re.IGNORECASE)
    if not mermaid_m:
        return False

    body = mermaid_m.group(1)

    if re.search(r"\bclassDef\b", body) or re.search(r"\bclass\s+[a-zA-Z0-9_-]+\s+[a-zA-Z0-9_-]+", body):
        return True
    if re.search(r":::[a-zA-Z0-9_-]+", body):
        return True

    canonical_subgraphs = (
        "subgraph S_RESOLVED",
        "subgraph S_IN_PROGRESS",
        "subgraph S_FRONTIER",
        "subgraph S_BLOCKED",
    )
    for sg in canonical_subgraphs:
        if sg not in body:
            return True

    return False


def _normalize_title_for_identity(raw_title: str) -> str:
    """
    [DAG-UPG-01] [ERR-DAG-STALE-01] Normalizes a ticket title or Mermaid node label
    for deterministic identity matching:
    - Strips markdown links: [text](url) -> text
    - Strips markdown formatting (*, _, `, #)
    - Strips status suffixes: (Resolved), [Resolved], (Frontier), (In Progress), etc.
    - Strips ticket ID prefixes: Ticket 01:, 01:, #01:
    - Collapses all whitespace and lowercases.
    """
    if not raw_title:
        return ""
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", raw_title)
    t = re.sub(r"[*_`#]", "", t)
    t = re.sub(
        r"[\(\[]\s*(?:Resolved|In\s+Progress|Frontier|Blocked|Ready|Completed)\s*[\)\]]",
        "",
        t,
        flags=re.IGNORECASE,
    )
    t = re.sub(r"^(?:ticket\s*)?[#0-9]{1,3}\s*:\s*", "", t, flags=re.IGNORECASE)
    t = re.sub(r"^ticket\s+[#0-9]{1,3}\b", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+", " ", t).strip().lower()
    return t


def ticket_identity_matches(
    on_disk_ticket: object,
    recorded_identities: Mapping[str, Mapping[str, Sequence[str]]] | None = None,
) -> bool:
    """
    [DAG-UPG-01] [ERR-DAG-STALE-01] [RETRO-C2] Compares an on-disk ticket dictionary
    against recorded identities (titles and relative issue links) from task-graph.md.
    Returns True if either:
    1. The normalized on-disk title matches any normalized recorded title for this ticket ID.
    2. The on-disk filename or relative path matches any recorded relative issue link path.
    """
    if not isinstance(on_disk_ticket, dict) or not recorded_identities or not isinstance(recorded_identities, Mapping):
        return False

    raw_id = str(on_disk_ticket.get("id", "") or "").strip().lstrip("#")
    norm_id = f"{int(raw_id):02d}" if raw_id.isdigit() else raw_id
    if not norm_id or norm_id not in recorded_identities:
        return False

    entry = recorded_identities.get(norm_id) if isinstance(recorded_identities, Mapping) else None
    if not isinstance(entry, Mapping):
        return False
    rec_titles = entry.get("titles", []) or []
    rec_links = entry.get("links", []) or []
    if not isinstance(rec_titles, (list, tuple, set)):
        rec_titles = [rec_titles] if rec_titles else []
    if not isinstance(rec_links, (list, tuple, set)):
        rec_links = [rec_links] if rec_links else []

    disk_title = str(on_disk_ticket.get("title", "") or "").strip()
    norm_disk_title = _normalize_title_for_identity(disk_title)

    if norm_disk_title and norm_disk_title in rec_titles:
        return True

    disk_path_str = str(on_disk_ticket.get("path", "") or "").strip().replace("\\", "/").lower()
    disk_file_name = Path(disk_path_str).name if disk_path_str else ""

    for lk in rec_links:
        clean_lk = str(lk).strip().replace("\\", "/").lower()
        lk_name = Path(clean_lk).name
        if disk_file_name and lk_name and disk_file_name == lk_name:
            return True
        if clean_lk and (clean_lk in disk_path_str or disk_path_str.endswith(clean_lk)):
            return True

    return False


class TaskGraphEvidence(tuple):
    """
    [DAG-UPG-01] [EDGE-UPGRADE-01] 4-tuple subclass preserving 100% backward compatibility
    for 4-tuple return unpacking on extract_task_graph_evidence():
    commits, statuses, phases, deliverables = extract_task_graph_evidence(content)
    while carrying recorded ticket identities (titles and relative issue links) as an attribute.
    """
    recorded_identities: dict[str, dict[str, list[str]]]

    def __new__(
        cls,
        existing_commits: dict[str, str],
        existing_statuses: dict[str, str],
        phase_overrides: dict[int, str],
        deliverable_overrides: dict[int, str],
        recorded_identities: dict[str, dict[str, list[str]]] | None = None,
    ) -> "TaskGraphEvidence":
        instance = super().__new__(
            cls,
            (existing_commits, existing_statuses, phase_overrides, deliverable_overrides),
        )
        instance.recorded_identities = recorded_identities if recorded_identities is not None else {}
        return instance


def extract_task_graph_evidence(
    content: str,
) -> tuple[dict[str, str], dict[str, str], dict[int, str], dict[int, str]]:
    """
    [EDGE-UPGRADE-01] [EDGE-UPGRADE-CONFLICT] [DAG-UPG-01] Extracts historical evidence from existing task-graph.md
    prior to rebuilding or upgrading:
    - existing_commits: historical verified commit SHAs mapped by normalized ticket ID (e.g. '01': 'abc1234')
    - existing_statuses: historical ticket statuses mapped by normalized ticket ID (e.g. '01': 'resolved')
    - phase_overrides: manual phase status overrides from Layer 1 table or annotations (e.g. 1: '⏭️ bypassed')
    - deliverable_overrides: custom deliverable paths from Layer 1 table (e.g. 1: 'custom/glossary.md')
    - recorded_identities: normalized ticket titles and relative issue links accessible via evidence.recorded_identities
    """
    existing_commits: dict[str, str] = {}
    existing_statuses: dict[str, str] = {}
    phase_overrides: dict[int, str] = {}
    deliverable_overrides: dict[int, str] = {}
    recorded_identities: dict[str, dict[str, list[str]]] = {}

    def _record_id_evidence(t_id: str, title: str | None = None, link: str | None = None) -> None:
        if not t_id:
            return
        if t_id not in recorded_identities:
            recorded_identities[t_id] = {"titles": [], "links": []}
        if title:
            norm_t = _normalize_title_for_identity(title)
            if norm_t and norm_t not in recorded_identities[t_id]["titles"]:
                recorded_identities[t_id]["titles"].append(norm_t)
        if link:
            clean_l = link.strip().replace("\\", "/").lower()
            if clean_l and clean_l not in recorded_identities[t_id]["links"]:
                recorded_identities[t_id]["links"].append(clean_l)

    if not content or not content.strip():
        return TaskGraphEvidence(
            existing_commits,
            existing_statuses,
            phase_overrides,
            deliverable_overrides,
            recorded_identities,
        )

    in_status_table = False
    in_lifecycle_table = False

    for raw_line in content.splitlines():
        line_s = raw_line.strip()
        lower_l = line_s.lower()
        if (
            "main flow lifecycle" in lower_l
            or "lifecycle tracker" in lower_l
            or "stage i:" in lower_l
            or "stage iii:" in lower_l
            or "stage iv:" in lower_l
            or "stage 1" in lower_l
            or "stage 3" in lower_l
            or "stage 4" in lower_l
            or "evaluation pipeline" in lower_l
        ):
            in_lifecycle_table = True
            in_status_table = False
            continue
        elif "status progress table" in lower_l or ("status progress" in lower_l and line_s.startswith("#")):
            in_status_table = True
            in_lifecycle_table = False
            continue
        elif line_s.startswith("#") or "dependency dag" in lower_l:
            in_lifecycle_table = False
            in_status_table = False
            continue

        if in_status_table and line_s.startswith("|"):
            parts = [c.strip() for c in line_s.split("|")]
            if len(parts) >= 7:
                col1 = parts[1]
                col2_title = parts[2]
                col3_blockers = parts[3]
                col4_status = parts[4]
                col5_commit = parts[5].strip("` \t")

                lower_col1 = col1.lower()
                if any(h in lower_col1 for h in ("ticket", "---", ":---", ":---:")) or ("status" in col4_status.lower() and "blocker" in col3_blockers.lower()):
                    continue

                m_id = re.search(r"\b([0-9]{1,3})\b", col1)
                if not m_id:
                    m_id = re.search(r"\[([0-9]{1,3})\]", col1)
                if m_id:
                    norm_id = f"{int(m_id.group(1)):02d}"
                else:
                    norm_id = re.sub(r"[\[\]\(\)*`#]", "", col1).strip()
                    if not norm_id:
                        continue

                link_m = re.search(r"\[([^\]]+)\]\(([^)]+)\)", col1)
                col1_link = link_m.group(2) if link_m else None
                _record_id_evidence(norm_id, title=col2_title, link=col1_link)

                if col5_commit and col5_commit != "-":
                    existing_commits[norm_id] = col5_commit

                if col4_status:
                    clean_s = col4_status.strip("`* \t")
                    if clean_s.startswith("✅"):
                        clean_s = clean_s[1:].strip("`* \t")
                    elif clean_s.startswith("🟡"):
                        clean_s = clean_s[1:].strip("`* \t")
                    existing_statuses[norm_id] = clean_s

        elif in_lifecycle_table and line_s.startswith("|"):
            parts = [c.strip() for c in line_s.split("|")]
            if len(parts) >= 7:
                col1 = parts[1]
                col3_status = parts[3]
                col5_deliv = parts[5]

                m_p = re.match(r"^([1-8])$", col1)
                if m_p:
                    p_num = int(m_p.group(1))
                    if col3_status:
                        if any(k in col3_status.lower() for k in ("bypassed", "fix-loop", "escalated", "skipped")):
                            phase_overrides[p_num] = col3_status
                        elif p_num in (1, 2, 3) and "completed" not in col3_status.lower():
                            phase_overrides[p_num] = col3_status
                        elif p_num in (5, 6) and any(k in col3_status.lower() for k in ("in-progress", "completed")):
                            phase_overrides[p_num] = col3_status

                    if col5_deliv and col5_deliv != "-":
                        deliverable_overrides[p_num] = col5_deliv

    # 3. Check for text annotations: Phase <N>: <status> (e.g. <!-- Phase 1: bypassed --> or - Phase 1: bypassed)
    for text_m in re.finditer(
        r"(?:^|[\n\r*-]|<!--|\b)Phase\s*([1-8])\s*:\s*([^\n\r,|>-]+)",
        content,
        re.IGNORECASE,
    ):
        p_num = int(text_m.group(1))
        raw_status = text_m.group(2).strip("`* \t")
        if raw_status:
            if "bypassed" in raw_status.lower() and not raw_status.startswith("⏭️"):
                phase_overrides[p_num] = f"⏭️ {raw_status}"
            else:
                phase_overrides[p_num] = raw_status

    # 4. Fallback status and title evidence from Mermaid node labels or CSS classes
    mermaid_m = re.search(r"```mermaid\s*\n(.*?)```", content, re.DOTALL | re.IGNORECASE)
    if mermaid_m:
        body = mermaid_m.group(1)
        for node_m in re.finditer(r'T?([0-9]{1,3})\["([^"]+)"\]', body):
            n_id = f"{int(node_m.group(1)):02d}"
            label = node_m.group(2)
            _record_id_evidence(n_id, title=label)
            if "(Resolved)" in label and n_id not in existing_statuses:
                existing_statuses[n_id] = "resolved"
            elif "(In Progress)" in label and n_id not in existing_statuses:
                existing_statuses[n_id] = "in-progress"

        for cls_m in re.finditer(r"class\s+T?([0-9]{1,3})\s+([a-zA-Z0-9_-]+)", body):
            n_id = f"{int(cls_m.group(1)):02d}"
            c_name = cls_m.group(2).lower()
            if "resolved" in c_name and n_id not in existing_statuses:
                existing_statuses[n_id] = "resolved"
            elif ("in_progress" in c_name or "in-progress" in c_name) and n_id not in existing_statuses:
                existing_statuses[n_id] = "in-progress"

    return TaskGraphEvidence(
        existing_commits,
        existing_statuses,
        phase_overrides,
        deliverable_overrides,
        recorded_identities,
    )


def validate_task_graph_schema(
    content: str,
    validator: ManifestValidator | None = None,
) -> tuple[bool, list[str]]:
    """
    [ERR-DASH-01] [MOD-COCKPIT-01] Validates task-graph.md schema integrity:
    1. Modular Four-Stage Cockpit:
       - Requires Top-level Mission block ('> **Mission**: <Feature description>')
       - Requires 4 stage headers (Stage I, Stage II, Stage III, Stage IV)
       - Requires valid status progress table with columns: Ticket, Title, Blockers, Status
       - Does NOT require interactive Mermaid DAG blocks ('graph TD')
    2. Table formatting: Must contain valid status progress table header and separator row.
       Rejects corrupted pipe formatting or truncated columns.
    3. Mermaid DAG (optional): If present, must contain closed code fence, valid 'graph TD' orientation,
       and parseable Mermaid node/link definitions (no unbalanced quotes/brackets or broken arrows).
    4. Content hygiene: No prohibited starter prompt blocks containing '/implement'.
    """
    errors: list[str] = []
    if not content or not content.strip():
        return False, ["Task graph content is empty"]

    mermaid_match = re.search(r"```mermaid\s*\n(.*?)```", content, re.DOTALL | re.IGNORECASE)
    if mermaid_match:
        mermaid_body = mermaid_match.group(1).strip()
        sg_count = len(re.findall(r"\bsubgraph\b", mermaid_body))
        end_count = len(re.findall(r"\bend\b", mermaid_body))
        if sg_count != end_count:
            errors.append(f"Damaged Mermaid subgraph fences: subgraph count ({sg_count}) does not match end count ({end_count})")
        lines = [l.strip() for l in mermaid_body.splitlines() if l.strip()]
        if not lines or not any("graph " in l for l in lines[:3]):
            errors.append("Mermaid block missing 'graph TD' orientation declaration")
        for line in lines:
            if line.startswith("%%") or line.startswith("graph ") or line.startswith("classDef ") or line.startswith("class "):
                continue
            if line.startswith("subgraph "):
                sub_unquoted = line.replace("#quot;", "")
                if sub_unquoted.count('"') % 2 != 0:
                    errors.append(f"Damaged Mermaid subgraph fence declaration with unbalanced quotes: '{line}'")
                if sub_unquoted.count("[") != sub_unquoted.count("]"):
                    errors.append(f"Damaged Mermaid subgraph fence declaration with unbalanced brackets: '{line}'")
                continue
            if line == "end":
                continue
            unquoted = line.replace("#quot;", "")
            if unquoted.count('"') % 2 != 0:
                errors.append(f"Unparseable Mermaid node definition with unbalanced quotes: '{line}'")
            if unquoted.count("[") != unquoted.count("]"):
                errors.append(f"Unparseable Mermaid node definition with unbalanced brackets: '{line}'")
            if re.search(r"--+>\s*--+>", line) or re.search(r"--+>\s*>", line) or re.search(r"-\s*-\s*>\s*>", line):
                errors.append(f"Unparseable Mermaid arrow definition with broken syntax: '{line}'")
            elif line.endswith("-->") or line.endswith("-.->"):
                errors.append(f"Unparseable Mermaid arrow definition with missing target: '{line}'")

    has_stage_1 = bool(re.search(r"^##\s*Stage\s+I\s*:", content, re.MULTILINE | re.IGNORECASE))
    has_stage_2 = bool(re.search(r"^##\s*Stage\s+II\s*:", content, re.MULTILINE | re.IGNORECASE))
    has_stage_3 = bool(re.search(r"^##\s*Stage\s+III\s*:", content, re.MULTILINE | re.IGNORECASE))
    has_stage_4 = bool(re.search(r"^##\s*Stage\s+IV\s*:", content, re.MULTILINE | re.IGNORECASE))
    has_lifecycle_table = bool(re.search(r"\|\s*#\s*\|\s*Phase\s*\|\s*Status\s*\|\s*Command\s*\|\s*Deliverable\s*\|", content, re.IGNORECASE))

    if not (has_lifecycle_table and mermaid_match):
        if not re.search(r">\s*\*\*Mission\*\*:", content, re.IGNORECASE):
            errors.append("Missing Mission block ('> **Mission**: <Feature description>')")
        if not has_stage_1:
            errors.append("Missing Stage I header ('## Stage I: Inception & Architecture (Command Center)')")
        if not has_stage_2:
            errors.append("Missing Stage II header ('## Stage II: Implementation Swarm')")
        if not has_stage_3:
            errors.append("Missing Stage III header ('## Stage III: Verification & Delivery (Evaluation Pipeline)')")
        if not has_stage_4:
            errors.append("Missing Stage IV header ('## Stage IV: Retrospective')")

    header_match = re.search(
        r"\|\s*Ticket\s*\|\s*Title\s*\|\s*Blockers\s*\|\s*Status\s*\|",
        content,
        re.IGNORECASE,
    )
    if not header_match:
        errors.append("Missing status progress table with columns: Ticket, Title, Blockers, Status")
    else:
        sep_match = re.search(
            r"\|\s*:?-+:?\s*\|\s*:?-+:?\s*\|\s*:?-+:?\s*\|\s*:?-+:?\s*\|",
            content,
        )
        if not sep_match:
            errors.append("Missing or corrupted markdown table separator row in status progress table")

    content_lines = content.splitlines()
    in_status_section = False
    in_lifecycle_section = False
    status_header_pipes: int | None = None
    lifecycle_header_pipes: int | None = None

    for raw_l in content_lines:
        line_str = raw_l.strip()
        lower_line = line_str.lower()
        if "main flow lifecycle" in lower_line or "lifecycle tracker" in lower_line or "stage i:" in lower_line or "stage iii:" in lower_line or "stage iv:" in lower_line:
            in_lifecycle_section = True
            in_status_section = False
            continue
        elif "status progress table" in lower_line or "ticket progress table" in lower_line or ("status progress" in lower_line and line_str.startswith("#")) or ("ticket progress" in lower_line and line_str.startswith("#")):
            in_status_section = True
            in_lifecycle_section = False
            continue
        elif line_str.startswith("#") or "dependency dag" in lower_line:
            in_lifecycle_section = False
            in_status_section = False
            continue

        if in_status_section:
            if not line_str:
                continue
            if line_str.startswith("```"):
                in_status_section = False
                continue
            if "|" in line_str:
                clean_pipes = line_str.replace(r"\|", "")
                pipe_count = clean_pipes.count("|")
                if not line_str.startswith("|") or not line_str.endswith("|"):
                    errors.append(f"Mangled Markdown table syntax in status progress table (truncated line or missing terminating pipe): '{line_str}'")
                elif status_header_pipes is None and ("ticket" in lower_line and "status" in lower_line):
                    status_header_pipes = pipe_count
                elif status_header_pipes is not None and not re.match(r"^\|(?:\s*:?-+:?\s*\|)+$", line_str):
                    if pipe_count != status_header_pipes:
                        errors.append(f"Mangled Markdown table syntax in status progress table (corrupted column count {pipe_count} vs {status_header_pipes}): '{line_str}'")
            elif not line_str.startswith(">") and not line_str.startswith("<!--"):
                errors.append(f"Malformed Markdown table syntax in status progress table (corrupted line without table markup): '{line_str}'")

        if in_lifecycle_section:
            if not line_str:
                continue
            if line_str.startswith("```"):
                in_lifecycle_section = False
                continue
            if "|" in line_str:
                clean_pipes = line_str.replace(r"\|", "")
                pipe_count = clean_pipes.count("|")
                if not line_str.startswith("|") or not line_str.endswith("|"):
                    errors.append(f"Mangled Markdown table syntax in lifecycle table (truncated line or missing terminating pipe): '{line_str}'")
                elif lifecycle_header_pipes is None and ("phase" in lower_line and "status" in lower_line):
                    lifecycle_header_pipes = pipe_count
                elif lifecycle_header_pipes is not None and not re.match(r"^\|(?:\s*:?-+:?\s*\|)+$", line_str):
                    if pipe_count != lifecycle_header_pipes:
                        errors.append(f"Mangled Markdown table syntax in lifecycle table (corrupted column count {pipe_count} vs {lifecycle_header_pipes}): '{line_str}'")
            elif not line_str.startswith(">") and not line_str.startswith("<!--"):
                errors.append(f"Malformed Markdown table syntax in lifecycle table (corrupted line without table markup): '{line_str}'")

    code_blocks = re.findall(r"(?:`{3,}|~{3,})[a-zA-Z0-9_-]*\s*\n(.*?)[\s\n]*(?:`{3,}|~{3,})", content, re.DOTALL)
    if any("/implement" in block for block in code_blocks):
        errors.append("Task graph artifact contains prohibited starter prompt code block with '/implement'")

    if re.search(r"^#+\s*(?:Ready-to-Run\s+)?Starter\s+Prompt", content, re.MULTILINE | re.IGNORECASE):
        errors.append("Task graph artifact contains prohibited 'Ready-to-Run Starter Prompt' section")

    if re.search(r"^#+\s*Layer\s+3", content, re.MULTILINE | re.IGNORECASE):
        errors.append("Task graph artifact contains prohibited 'Layer 3' handoff section")

    return len(errors) == 0, errors


def recover_malformed_dashboard(
    target_or_path: Path | str,
    *,
    root_dir: Path | str | None = None,
    validator: ManifestValidator | None = None,
    force_heal: bool = False,
    write_back: bool = True,
    lean: bool = False,
) -> DashboardRecoveryResult:
    """
    [ERR-DASH-01] Self-heals corrupted task-graph.md files by detecting schema/syntax corruptions
    (broken markdown tables or unparseable Mermaid node definitions), rejecting malformed schemas
    with actionable validation diagnostics, and regenerating the status table and Mermaid DAG
    directly from authoritative ticket files under .scratch/<feature-slug>/issues/ without data loss.
    """
    v = validator or ManifestValidator()
    base_root = Path(root_dir) if root_dir else None

    raw_path = Path(str(target_or_path).strip())
    if not raw_path.is_absolute() and base_root:
        cand = base_root / raw_path
        if cand.exists():
            raw_path = cand

    tg_file: Path | None = None
    feature_dir: Path | None = None

    if raw_path.is_file() and raw_path.name.lower() in ("task-graph.md", "task-graph.markdown"):
        tg_file = raw_path
        feature_dir = raw_path.parent
    elif raw_path.is_file():
        feature_dir = raw_path.parent.parent if raw_path.parent.name == "issues" else raw_path.parent
        tg_file = feature_dir / "task-graph.md"
    elif raw_path.is_dir():
        feature_dir = raw_path.parent if raw_path.name == "issues" else raw_path
        tg_file = feature_dir / "task-graph.md"
    else:
        parts = raw_path.parts
        if ".scratch" in parts:
            idx = parts.index(".scratch")
            slug = parts[idx + 1] if len(parts) > idx + 1 else "feature"
            feature_dir = (base_root / ".scratch" / slug) if base_root else Path(f".scratch/{slug}")
        else:
            feature_dir = raw_path
        tg_file = feature_dir / "task-graph.md"

    issues_dir = feature_dir / "issues" if (feature_dir / "issues").is_dir() else feature_dir

    existing_content = ""
    file_existed = False
    if tg_file and tg_file.is_file():
        file_existed = True
        try:
            existing_content = tg_file.read_text(encoding="utf-8")
        except OSError:
            existing_content = ""

    is_valid, errors = validate_task_graph_schema(existing_content, validator=v)
    is_legacy = is_legacy_task_graph(existing_content)
    was_malformed = (not is_valid) or (not file_existed) or is_legacy

    evidence = extract_task_graph_evidence(existing_content)
    existing_commits, existing_statuses, phase_overrides, deliverable_overrides = evidence
    recorded_identities = getattr(evidence, "recorded_identities", {})

    scanned_for_check = v.scan_tickets_from_tracker(issues_dir)
    has_identity_divergence = False
    if scanned_for_check and recorded_identities:
        for st in scanned_for_check:
            st_id = str(st.get("id", "")).strip().lstrip("#")
            norm_id = f"{int(st_id):02d}" if st_id.isdigit() else st_id
            if norm_id in recorded_identities:
                if not ticket_identity_matches(st, recorded_identities):
                    has_identity_divergence = True
                    break

    if has_identity_divergence:
        was_malformed = True

    if not was_malformed and not force_heal:
        scanned = scanned_for_check
        frontier = v.calculate_unblocked_frontier(scanned)
        resolved_count = sum(1 for t in scanned if v._is_status_resolved(t.get("status", "")))
        all_res = len(scanned) > 0 and resolved_count >= len(scanned)
        return DashboardRecoveryResult(
            artifact=existing_content,
            chat_stream="",
            frontier=frontier,
            all_resolved=all_res,
            total_tickets=len(scanned),
            resolved_tickets=resolved_count,
            is_recovered=False,
            was_malformed=False,
            diagnostic=f"Task graph schema in {tg_file} is valid; zero healing required.",
            errors=[],
            task_graph_path=tg_file,
            feature_dir=feature_dir,
            tickets=scanned,
        )

    md_files = [f for f in issues_dir.glob("*.md") if f.is_file()] if issues_dir.is_dir() else []
    if not issues_dir.is_dir() or len(md_files) == 0:
        slug_name = feature_dir.name if feature_dir else "unknown"
        raise ScaffoldEmptyError(
            f"[ERR-SCAFFOLD-EMPTY] No ticket files found in .scratch/{slug_name}/issues/. Deconstruct spec before rendering dashboard."
        )

    error_summary = "; ".join(errors) if errors else "file missing on disk"
    tg_display = _normalize_ticket_display_path(tg_file, root_dir=base_root) if tg_file else "task-graph.md"
    issues_display = _normalize_ticket_display_path(issues_dir, root_dir=base_root) if issues_dir else "issues"
    if not is_valid:
        diagnostic = (
            f"[ERR-DASH-01] Corrupted task-graph schema detected, initiating automatic on-disk recovery. "
            f"Malformed task graph schema rejected in {tg_display}: {error_summary}. "
            f"Actionable Recovery: Regenerated Dual-Layer Clustered Subgraph Cockpit from authoritative ticket files in {issues_display}."
        )
    elif is_legacy:
        diagnostic = (
            f"[UPGRADE-01] Legacy flat task graph detected in {tg_display}, "
            f"automatically upgraded to native Clustered Subgraphs preserving historical commits and lifecycle state."
        )
    else:
        diagnostic = (
            f"[ERR-DASH-01] Corrupted task-graph schema detected, initiating automatic on-disk recovery. "
            f"Malformed task graph schema rejected in {tg_display}: {error_summary}. "
            f"Actionable Recovery: Regenerated Dual-Layer Clustered Subgraph Cockpit from authoritative ticket files in {issues_display}."
        )

    scanned_tickets = scanned_for_check or v.scan_tickets_from_tracker(issues_dir)
    if scanned_tickets:
        topological_sort_tickets(scanned_tickets, validator=v, root_dir=base_root)
        for st in scanned_tickets:
            st_id = str(st.get("id", "")).strip().lstrip("#")
            norm_id = f"{int(st_id):02d}" if st_id.isdigit() else st_id
            curr_c = str(st.get("commit") or st.get("commit_sha") or "").strip()
            curr_s = str(st.get("status", "")).strip()

            has_disk_commit = bool(curr_c and curr_c != "-")
            is_disk_unresolved = bool(curr_s and not v._is_status_resolved(curr_s))
            identity_matches = ticket_identity_matches(st, recorded_identities) if recorded_identities else False
            can_upgrade_legacy = bool(is_legacy and identity_matches)

            if can_upgrade_legacy:
                ex_c = existing_commits.get(norm_id, "")
                ex_s = existing_statuses.get(norm_id, "")
                if ex_c and ex_c != "-":
                    st["commit"] = ex_c
                elif has_disk_commit:
                    st["commit"] = curr_c

                if v._is_status_resolved(ex_s) or (ex_c and ex_c != "-"):
                    st["status"] = "resolved"
                elif v._is_status_in_progress(ex_s):
                    st["status"] = "in-progress"
                elif has_disk_commit:
                    st["status"] = "resolved"
                elif curr_s:
                    st["status"] = curr_s
                else:
                    st["status"] = "ready-for-agent"
            else:
                if has_disk_commit:
                    st["status"] = "resolved"
                    st["commit"] = curr_c
                elif is_disk_unresolved:
                    st["status"] = curr_s
                    st["commit"] = "-"
                    if identity_matches and norm_id in existing_statuses and v._is_status_in_progress(existing_statuses[norm_id]):
                        st["status"] = "in-progress"
                else:
                    if identity_matches and (not curr_c or curr_c == "-") and norm_id in existing_commits:
                        st["commit"] = existing_commits[norm_id]
                    elif has_disk_commit:
                        st["commit"] = curr_c

                    if norm_id in existing_statuses and v._is_status_resolved(existing_statuses[norm_id]):
                        st["status"] = "resolved"
                    elif curr_s and v._is_status_resolved(curr_s):
                        st["status"] = "resolved"
                    elif norm_id in existing_commits and existing_commits[norm_id] != "-":
                        if identity_matches:
                            st["status"] = "resolved"

    frontier_ids = v.calculate_unblocked_frontier(scanned_tickets)
    resolved_count = sum(1 for t in scanned_tickets if v._is_status_resolved(t.get("status", "")))
    all_resolved = len(scanned_tickets) > 0 and resolved_count >= len(scanned_tickets)

    detected_slug = feature_dir.name if feature_dir else None
    has_mermaid = bool(re.search(r"```mermaid|subgraph\s+S_", existing_content))
    healed_dashboard = v.render_task_dashboard(
        tickets=scanned_tickets,
        active_frontier=frontier_ids,
        feature_slug=detected_slug,
        phase_overrides=phase_overrides if phase_overrides else None,
        session_anchor=feature_dir,
        deliverable_overrides=deliverable_overrides if deliverable_overrides else None,
        include_mermaid=has_mermaid,
    )

    if write_back and tg_file:
        _atomic_write_file(tg_file, healed_dashboard, raise_on_error=False)

    if all_resolved:
        target_f_dir = issues_dir.parent if issues_dir.name == "issues" else issues_dir
        p5_status = str(phase_overrides.get(5, "")).lower() if phase_overrides else ""
        cr_artifact = target_f_dir / "code-review.md" if target_f_dir else None
        cr_has_pass = False
        if cr_artifact and cr_artifact.is_file():
            try:
                cr_txt = cr_artifact.read_text(encoding="utf-8").upper()
                if "PASS" in cr_txt and "FAIL" not in cr_txt:
                    cr_has_pass = True
            except Exception:
                pass
        phase5_passed = (
            any(k in p5_status for k in ("completed", "✅", "bypassed", "⏭️"))
            or cr_has_pass
        )

        p6_status = str(phase_overrides.get(6, "")).lower() if phase_overrides else ""
        sa_artifact = target_f_dir / "security-audit.md" if target_f_dir else None
        sa_has_pass = False
        if sa_artifact and sa_artifact.is_file():
            try:
                sa_txt = sa_artifact.read_text(encoding="utf-8").upper()
                if "PASS" in sa_txt and "FAIL" not in sa_txt:
                    sa_has_pass = True
            except Exception:
                pass
        phase6_passed = (
            any(k in p6_status for k in ("completed", "✅", "bypassed", "⏭️"))
            or sa_has_pass
        )

        is_p5_escalated = any(k in p5_status for k in ("escalated", "⚠️ escalated-to-human"))
        is_p6_escalated = any(k in p6_status for k in ("escalated", "⚠️ escalated-to-human"))

        if is_p5_escalated:
            chat_stream = (
                "⚠️ [ERR-EVAL-01] Circuit Breaker Active: Phase 5 (Two-Axis Code Review) has escalated to human "
                "(status: '⚠️ escalated-to-human'). Automated remediation halted due to persistent findings across successive attempts.\n\n"
                "Please resolve the defect manually in the Command Center (CC) before proceeding."
            )
        elif is_p6_escalated:
            chat_stream = (
                "⚠️ [ERR-EVAL-01] Circuit Breaker Active: Phase 6 (Semantic Security Audit) has escalated to human "
                "(status: '⚠️ escalated-to-human'). Automated remediation halted due to persistent vulnerabilities across successive attempts.\n\n"
                "Please resolve the defect manually in the Command Center (CC) before proceeding."
            )
        elif phase5_passed and not phase6_passed:
            eval_brief = format_evaluation_brief(
                "security-audit",
                feature_dir_or_target=issues_dir,
                root_dir=base_root,
                validator=v,
                include_heading=True,
                lean=lean,
            )
            if any(err in eval_brief for err in ("[ERR-EVAL-03]", "[ERR-LEAN-EVAL-02]", "[ERR-LEAN-EVAL-01]")):
                chat_stream = (
                    "🎉 Phase 5: Code Review is complete (verdict: PASS)!\n\n"
                    f"{eval_brief}"
                )
            else:
                chat_stream = (
                    "🎉 Phase 5: Code Review is complete (verdict: PASS)!\n\n"
                    "Proceed to Phase 6: Semantic Security Audit.\n\n"
                    f"{eval_brief}"
                )
        elif phase5_passed and phase6_passed:
            eval_brief = format_evaluation_brief(
                "pr",
                feature_dir_or_target=issues_dir,
                root_dir=base_root,
                validator=v,
                include_heading=True,
                lean=lean,
            )
            if any(err in eval_brief for err in ("[ERR-EVAL-03]", "[ERR-LEAN-EVAL-02]", "[ERR-LEAN-EVAL-01]")):
                chat_stream = (
                    "🎉 Phase 6: Semantic Security Audit is complete (verdict: PASS)!\n\n"
                    f"{eval_brief}"
                )
            else:
                chat_stream = (
                    "🎉 Phase 6: Semantic Security Audit is complete (verdict: PASS)!\n\n"
                    "Proceed to Phase 7: Pull Request.\n\n"
                    f"{eval_brief}"
                )
        else:
            eval_brief = format_evaluation_brief(
                "code-review",
                feature_dir_or_target=issues_dir,
                root_dir=base_root,
                validator=v,
                include_heading=True,
                lean=lean,
            )
            if any(err in eval_brief for err in ("[ERR-EVAL-03]", "[ERR-LEAN-EVAL-02]", "[ERR-LEAN-EVAL-01]")):
                chat_stream = (
                    "🎉 All tickets in the task graph are resolved (100% complete)!\n\n"
                    f"{eval_brief}"
                )
            else:
                chat_stream = (
                    "🎉 All tickets in the task graph are resolved (100% complete)!\n\n"
                    "Phase 4 Implementation is complete. Proceed to Phase 5: Code Review.\n\n"
                    f"{eval_brief}"
                )
    elif not frontier_ids:
        has_in_progress = any(v._is_status_in_progress(str(t.get("status", ""))) for t in scanned_tickets)
        if has_in_progress:
            chat_stream = (
                "> ⏳ Standby: All active frontier tickets are currently in-progress. "
                "Awaiting worker completion before calculating downstream unblocked tickets."
            )
        else:
            chat_stream = "> ⚠️ No unblocked frontier tickets available in the task graph."
    else:
        chat_stream = format_worker_brief(
            frontier_ids,
            root_dir=base_root,
            validator=v,
            feature_slug=detected_slug,
        )

    res_cls = LegacyUpgradeResult if (is_legacy and is_valid) else DashboardRecoveryResult
    return res_cls(
        artifact=healed_dashboard,
        chat_stream=chat_stream,
        frontier=frontier_ids,
        all_resolved=all_resolved,
        total_tickets=len(scanned_tickets),
        resolved_tickets=resolved_count,
        is_recovered=True,
        was_malformed=True,
        diagnostic=diagnostic,
        errors=errors,
        task_graph_path=tg_file,
        feature_dir=feature_dir,
        tickets=scanned_tickets,
    )


def upgrade_legacy_task_graph(
    target_or_path: Path | str,
    *,
    root_dir: Path | str | None = None,
    validator: ManifestValidator | None = None,
    write_back: bool = True,
) -> LegacyUpgradeResult:
    """
    [EDGE-UPGRADE-01] [EDGE-UPGRADE-CONFLICT] Seamless legacy upgrade in scripts/internal/resolve_ticket.py
    allowing older pre-ADR-0040 feature directories containing flat unclustered Mermaid graphs
    or CSS classes to be automatically upgraded to native Clustered Subgraphs (S_RESOLVED,
    S_IN_PROGRESS, S_FRONTIER, S_BLOCKED) upon rescan, while preserving all historical verified
    commit SHAs, phase overrides, deliverable paths, and ticket statuses.
    """
    rec_res = recover_malformed_dashboard(
        target_or_path,
        root_dir=root_dir,
        validator=validator,
        force_heal=True,
        write_back=write_back,
    )
    if isinstance(rec_res, LegacyUpgradeResult):
        return rec_res
    return LegacyUpgradeResult(
        artifact=rec_res.artifact,
        chat_stream=rec_res.chat_stream,
        frontier=rec_res.frontier,
        all_resolved=rec_res.all_resolved,
        total_tickets=rec_res.total_tickets,
        resolved_tickets=rec_res.resolved_tickets,
        is_upgraded=True,
        is_recovered=rec_res.is_recovered,
        was_malformed=rec_res.was_malformed,
        diagnostic=rec_res.diagnostic,
        errors=rec_res.errors,
        task_graph_path=rec_res.task_graph_path,
        feature_dir=rec_res.feature_dir,
        tickets=rec_res.tickets,
    )


def _find_cycle_nodes(nodes: set[str], blockers_map: dict[str, set[str]]) -> set[str]:
    """
    Finds all nodes belonging to circular dependency cycles within the unresolved nodes.
    Uses Tarjan's strongly connected components algorithm to strictly isolate the cycle nodes
    from downstream tickets.
    """
    adj: dict[str, list[str]] = {u: [] for u in nodes}
    for u in nodes:
        for b in blockers_map.get(u, set()):
            if b in nodes:
                adj[b].append(u)

    index = 0
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: dict[str, bool] = {u: False for u in nodes}
    stack: list[str] = []
    sccs: list[list[str]] = []

    def strongconnect(v: str) -> None:
        nonlocal index
        indices[v] = index
        lowlink[v] = index
        index += 1
        stack.append(v)
        on_stack[v] = True

        for w in adj.get(v, []):
            if w not in indices:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif on_stack[w]:
                lowlink[v] = min(lowlink[v], indices[w])

        if lowlink[v] == indices[v]:
            scc: list[str] = []
            while True:
                w = stack.pop()
                on_stack[w] = False
                scc.append(w)
                if w == v:
                    break
            sccs.append(scc)

    for node in sorted(nodes):
        if node not in indices:
            strongconnect(node)

    cycle_nodes: set[str] = set()
    for scc in sccs:
        if len(scc) > 1:
            cycle_nodes.update(scc)
        elif len(scc) == 1:
            u = scc[0]
            if u in blockers_map.get(u, set()):
                cycle_nodes.add(u)

    return cycle_nodes if cycle_nodes else set(nodes)


def _node_sort_key(node_id: str) -> tuple[int, str]:
    clean = node_id.strip().lstrip("#")
    if clean.lower().startswith("ticket-"):
        clean = clean[7:].strip()
    return (int(clean), node_id) if clean.isdigit() else (9999, node_id)


def _normalize_scanned_tickets(
    tickets_or_tracker: Path | str | Sequence[dict[str, Any] | Path | str] | dict[str, Any],
    validator: ManifestValidator | None = None,
    root_dir: Path | str | None = None,
) -> list[dict[str, Any]]:
    v = validator or ManifestValidator()
    base_root = Path(root_dir) if root_dir else None

    scanned_tickets: list[dict[str, Any]] = []
    if isinstance(tickets_or_tracker, (str, Path)):
        p = Path(tickets_or_tracker)
        if not p.is_absolute() and base_root:
            cand = base_root / p
            if cand.exists():
                p = cand

        if p.is_dir():
            target_issues = p / "issues" if (p / "issues").is_dir() else p
            scanned_tickets = v.scan_tickets_from_tracker(target_issues)
        elif p.is_file():
            if p.name.lower() in ("task-graph.md", "task-graph.markdown"):
                parent_issues = p.parent / "issues" if (p.parent / "issues").is_dir() else p.parent
                scanned_tickets = v.scan_tickets_from_tracker(parent_issues)
            else:
                try:
                    t_dict = v._read_ticket_with_retry_and_fallback(p)
                    if t_dict:
                        scanned_tickets = [t_dict]
                except Exception:
                    pass
    elif isinstance(tickets_or_tracker, (list, tuple)):
        for item in tickets_or_tracker:
            if isinstance(item, dict):
                scanned_tickets.append(dict(item))
            elif isinstance(item, (str, Path)):
                p = Path(item)
                if not p.is_absolute() and base_root:
                    cand = base_root / p
                    if cand.exists():
                        p = cand
                if p.is_file():
                    try:
                        t_dict = v._read_ticket_with_retry_and_fallback(p)
                        if t_dict:
                            scanned_tickets.append(t_dict)
                    except Exception:
                        pass
                elif p.is_dir():
                    scanned_tickets.extend(v.scan_tickets_from_tracker(p))
    elif isinstance(tickets_or_tracker, dict):
        scanned_tickets = [dict(tickets_or_tracker)]

    return scanned_tickets


class BlockerValidationResult:
    """
    [EDGE-DAG-SELF-REF] [ERR-DAG-ORPHAN] Structured outcome of ticket blocker integrity validation.
    Detects self-referential blocker loops and orphan blocker references across decomposed tickets.
    """
    is_valid: bool
    self_referential_tickets: list[str]
    orphan_blockers: list[tuple[str, str]]
    warnings: list[str]
    errors: list[str]
    diagnostic: str

    def __init__(
        self,
        is_valid: bool,
        self_referential_tickets: Sequence[str] | None = None,
        orphan_blockers: Sequence[tuple[str, str]] | None = None,
        warnings: Sequence[str] | None = None,
        errors: Sequence[str] | None = None,
        diagnostic: str = "",
    ) -> None:
        self.is_valid = is_valid
        self.self_referential_tickets = list(self_referential_tickets or [])
        self.orphan_blockers = list(orphan_blockers or [])
        self.warnings = list(warnings or [])
        self.errors = list(errors or [])
        self.diagnostic = diagnostic

    @property
    def ok(self) -> bool:
        return self.is_valid

    @property
    def has_orphans(self) -> bool:
        return bool(self.orphan_blockers)

    @property
    def has_self_ref(self) -> bool:
        return bool(self.self_referential_tickets)

    def format_warnings(self) -> str:
        return "\n".join(self.warnings)

    def format_errors(self) -> str:
        return "\n".join(self.errors)

    def __bool__(self) -> bool:
        return self.is_valid

    def __iter__(self) -> Iterator[Any]:
        return iter((self.is_valid, self.warnings, self.errors))

    def __getitem__(self, index: int) -> Any:
        return (self.is_valid, self.warnings, self.errors)[index]

    def __len__(self) -> int:
        return 3

    def __repr__(self) -> str:
        return (
            f"BlockerValidationResult(is_valid={self.is_valid}, "
            f"self_referential_tickets={self.self_referential_tickets!r}, "
            f"orphan_blockers={self.orphan_blockers!r}, "
            f"warnings={self.warnings!r}, errors={self.errors!r})"
        )


def _canonical_ticket_id(val: Any) -> str:
    s = str(val or "").strip().lstrip("#")
    if s.lower().startswith("ticket-"):
        s = s[7:].strip()
    if s.isdigit():
        return f"{int(s):02d}"
    return s.lower()


def _extract_ticket_id(t: dict[str, Any]) -> str:
    raw_id = (
        t.get("id")
        if t.get("id") is not None
        else (t.get("number") if t.get("number") is not None else t.get("ticket"))
    )
    raw_id_s = str(raw_id or "").strip().lstrip("#")
    if raw_id_s.lower().startswith("ticket-"):
        raw_id_s = raw_id_s[7:].strip()
    if raw_id_s.isdigit():
        return f"{int(raw_id_s):02d}"
    if raw_id_s:
        return raw_id_s
    if t.get("path"):
        stem = Path(t["path"]).stem
        m = re.match(r"^(?:ticket[-_]?)?#?([0-9]{1,3})\b", stem, re.IGNORECASE)
        return f"{int(m.group(1)):02d}" if m else stem
    raw_title = str(t.get("title", ""))
    m = re.match(r"^(?:ticket[-_]?)?#?([0-9]{1,3})\b", raw_title, re.IGNORECASE)
    return f"{int(m.group(1)):02d}" if m else raw_title


def validate_ticket_blockers(
    tickets_or_tracker: Path | str | Sequence[dict[str, Any] | Path | str] | dict[str, Any],
    *,
    validator: ManifestValidator | None = None,
    root_dir: Path | str | None = None,
    raise_on_cycle: bool = True,
    emit_warnings: bool = True,
) -> BlockerValidationResult:
    """
    [EDGE-DAG-SELF-REF] [ERR-DAG-ORPHAN] Validates ticket blocker dependencies for:
    1. Single-node self-referential blocker loops (Ticket 02 blocked by 02).
       Halts immediately with DagCycleError and diagnostic:
       '[ERR-DAG-CYCLE] Deadlock detected: Self-referential blocker loop detected for ticket <id>. A ticket cannot block itself.'
    2. Orphan blocker references (referencing ticket IDs that do not exist in the feature directory).
       Emits actionable warning diagnostic:
       '[ERR-DAG-ORPHAN] Warning: Ticket <id> references non-existent blocker ticket '<blocker>'. Blocker reference ignored.'
    """
    v = validator or ManifestValidator()
    scanned_tickets = _normalize_scanned_tickets(tickets_or_tracker, validator=v, root_dir=root_dir)

    ticket_map: dict[str, dict[str, Any]] = {}
    blockers_map: dict[str, list[str]] = {}
    canon_to_tid: dict[str, str] = {}

    for t in scanned_tickets:
        tid = _extract_ticket_id(t)
        canon_tid = _canonical_ticket_id(tid)
        ticket_map[tid] = t
        canon_to_tid[canon_tid] = tid
        raw_blockers = t.get("blockers") or t.get("blocked_by") or t.get("Blocked by") or []
        blockers_map[tid] = list(v._parse_blocker_ids(raw_blockers))

    all_ids = sorted(ticket_map.keys(), key=_node_sort_key)
    self_referential_tickets: list[str] = []
    errors: list[str] = []

    for tid in all_ids:
        canon_tid = _canonical_ticket_id(tid)
        clean_blockers = blockers_map.get(tid, [])
        for b in clean_blockers:
            if _canonical_ticket_id(b) == canon_tid:
                if tid not in self_referential_tickets:
                    self_referential_tickets.append(tid)
                    err_msg = (
                        f"[ERR-DAG-CYCLE] Deadlock detected: Self-referential blocker loop "
                        f"detected for ticket {tid}. A ticket cannot block itself."
                    )
                    errors.append(err_msg)
                break

    orphan_blockers: list[tuple[str, str]] = []
    warnings: list[str] = []

    for tid in all_ids:
        canon_tid = _canonical_ticket_id(tid)
        for b in blockers_map.get(tid, []):
            canon_b = _canonical_ticket_id(b)
            if canon_b != canon_tid and canon_b not in canon_to_tid:
                orphan_blockers.append((tid, b))
                warn_msg = (
                    f"[ERR-DAG-ORPHAN] Warning: Ticket {tid} references non-existent "
                    f"blocker ticket '{b}'. Blocker reference ignored."
                )
                warnings.append(warn_msg)

    if emit_warnings and warnings:
        for w in warnings:
            sys.stderr.write(w + "\n")

    if self_referential_tickets and raise_on_cycle:
        first_tid = self_referential_tickets[0]
        cycle_str = ", ".join(sorted(self_referential_tickets, key=_node_sort_key))
        msg = (
            f"[ERR-DAG-CYCLE] Deadlock detected: Self-referential blocker loop "
            f"detected for ticket {first_tid}. A ticket cannot block itself. "
            f"Deadlock detected: Circular dependency detected involving ticket(s): {cycle_str}. Please resolve blockers."
        )
        raise DagCycleError(msg, cycle_tickets=sorted(self_referential_tickets, key=_node_sort_key))

    is_valid = len(errors) == 0
    diagnostic = "\n".join(errors + warnings)
    return BlockerValidationResult(
        is_valid=is_valid,
        self_referential_tickets=self_referential_tickets,
        orphan_blockers=orphan_blockers,
        warnings=warnings,
        errors=errors,
        diagnostic=diagnostic,
    )


def detect_ticket_dag_anomalies(
    tickets_or_tracker: Path | str | Sequence[dict[str, Any] | Path | str] | dict[str, Any],
    validator: ManifestValidator | None = None,
    root_dir: Path | str | None = None,
) -> tuple[set[str], set[str], list[str]]:
    """
    [ERR-BRIEF-04] Evaluates graph dependencies across ticket collections to detect:
    1. Circular dependency cycles (both single-node self-references and multi-node cycles such as 02 <-> 03).
    2. Orphaned blocker references (tickets referencing non-existent blocker IDs).
    Returns (cycle_nodes, orphan_ticket_ids, diagnostic_errors).
    """
    v = validator or ManifestValidator()
    scanned_tickets = _normalize_scanned_tickets(tickets_or_tracker, validator=v, root_dir=root_dir)

    ticket_map: dict[str, dict[str, Any]] = {}
    blockers_map: dict[str, list[str]] = {}
    canon_to_tid: dict[str, str] = {}

    for t in scanned_tickets:
        tid = _extract_ticket_id(t)
        canon_tid = _canonical_ticket_id(tid)
        ticket_map[tid] = t
        canon_to_tid[canon_tid] = tid
        raw_b = t.get("blockers") or t.get("blocked_by") or t.get("Blocked by") or []
        blockers_map[tid] = list(v._parse_blocker_ids(raw_b))

    all_tids = sorted(ticket_map.keys(), key=_node_sort_key)

    # 1. Orphaned blockers detection
    orphan_ticket_ids: set[str] = set()
    orphan_diagnostics: list[str] = []

    # If any tickets have paths on disk, query peer issue files in case only a subset was passed
    peer_ids: set[str] = set(canon_to_tid.keys())
    for t in scanned_tickets:
        p_val = t.get("path")
        if p_val:
            p_obj = Path(p_val)
            if not p_obj.is_file() and root_dir:
                cand_p = Path(root_dir) / p_obj
                if cand_p.is_file():
                    p_obj = cand_p
            if p_obj.is_file() and p_obj.parent.name == "issues":
                try:
                    for peer in p_obj.parent.glob("*.md"):
                        stem = peer.stem
                        m = re.match(r"^(?:ticket[-_]?)?#?([0-9]{1,3})\b", stem, re.IGNORECASE)
                        if m:
                            peer_ids.add(f"{int(m.group(1)):02d}")
                        else:
                            peer_ids.add(stem.lower())
                except Exception:
                    pass

    for tid in all_tids:
        clean_blockers = blockers_map.get(tid, [])
        for b in clean_blockers:
            canon_b = _canonical_ticket_id(b)
            if canon_b not in peer_ids and canon_b != _canonical_ticket_id(tid):
                orphan_ticket_ids.add(tid)
                warn_msg = (
                    f"> ⚠️ [ERR-BRIEF-04] [ERR-DAG-ORPHAN] Warning: Ticket {tid} references non-existent "
                    f"blocker ticket '{b}'. Starter brief emission halted for ticket {tid} to prevent deadlock."
                )
                if warn_msg not in orphan_diagnostics:
                    orphan_diagnostics.append(warn_msg)

    # 2. Self-referential loop detection
    cycle_nodes: set[str] = set()
    cycle_diagnostics: list[str] = []
    for tid in all_tids:
        canon_tid = _canonical_ticket_id(tid)
        clean_blockers = blockers_map.get(tid, [])
        for b in clean_blockers:
            if _canonical_ticket_id(b) == canon_tid:
                cycle_nodes.add(tid)
                msg = (
                    f"> ⚠️ [ERR-BRIEF-04] [ERR-DAG-CYCLE] Deadlock detected: Self-referential blocker loop "
                    f"detected for ticket {tid}. A ticket cannot block itself. "
                    f"Starter brief emission halted for ticket {tid} to prevent deadlock. Please resolve blockers."
                )
                if msg not in cycle_diagnostics:
                    cycle_diagnostics.append(msg)
                break

    # 3. Multi-node circular dependency detection using Tarjan's SCC
    # Construct adjacency: source_tid -> dependent_tid (dependent_tid is blocked by source_tid)
    adj: dict[str, list[str]] = {tid: [] for tid in all_tids}
    for tid in all_tids:
        for b in blockers_map.get(tid, []):
            canon_b = _canonical_ticket_id(b)
            if canon_b in canon_to_tid and canon_b != _canonical_ticket_id(tid):
                source_tid = canon_to_tid[canon_b]
                adj[source_tid].append(tid)

    idx = 0
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: dict[str, bool] = {tid: False for tid in all_tids}
    stack: list[str] = []
    sccs: list[list[str]] = []

    def sc(u: str) -> None:
        nonlocal idx
        indices[u] = idx
        lowlink[u] = idx
        idx += 1
        stack.append(u)
        on_stack[u] = True

        for v in adj.get(u, []):
            if v not in indices:
                sc(v)
                lowlink[u] = min(lowlink[u], lowlink[v])
            elif on_stack[v]:
                lowlink[u] = min(lowlink[u], indices[v])

        if lowlink[u] == indices[u]:
            component: list[str] = []
            while True:
                w = stack.pop()
                on_stack[w] = False
                component.append(w)
                if w == u:
                    break
            sccs.append(component)

    for tid in all_tids:
        if tid not in indices:
            sc(tid)

    for scc in sccs:
        if len(scc) > 1:
            sorted_scc = sorted(scc, key=_node_sort_key)
            cycle_nodes.update(sorted_scc)
            cycle_str = ", ".join(sorted_scc)
            msg = (
                f"> ⚠️ [ERR-BRIEF-04] [ERR-DAG-CYCLE] Deadlock detected: Circular dependency detected "
                f"involving ticket(s): {cycle_str}. Starter brief emission halted for affected cycle "
                f"to prevent deadlock. Please resolve blockers."
            )
            if msg not in cycle_diagnostics:
                cycle_diagnostics.append(msg)

    diagnostics = cycle_diagnostics + orphan_diagnostics
    return cycle_nodes, orphan_ticket_ids, diagnostics


def topological_sort_tickets(
    tickets_or_tracker: Path | str | Sequence[dict[str, Any] | Path | str] | dict[str, Any],
    *,
    validator: ManifestValidator | None = None,
    root_dir: Path | str | None = None,
) -> list[str]:
    """
    [DAG-01] [ERR-DAG-CYCLE] Computes deterministic topological sort order of tickets using Kahn's algorithm
    and validates that the task graph is a directed acyclic graph (DAG) with zero cycles.
    If a circular dependency cycle is detected, immediately halts with DagCycleError and diagnostic:
    '[ERR-DAG-CYCLE] Deadlock detected: Circular dependency detected involving ticket(s): <cycle-ticket-ids>. Please resolve blockers.'
    """
    v = validator or ManifestValidator()
    scanned_tickets = _normalize_scanned_tickets(tickets_or_tracker, validator=v, root_dir=root_dir)

    if not scanned_tickets:
        return []

    validate_ticket_blockers(
        scanned_tickets,
        validator=v,
        root_dir=root_dir,
        raise_on_cycle=True,
        emit_warnings=True,
    )

    ticket_map: dict[str, dict[str, Any]] = {}
    blockers_map: dict[str, set[str]] = {}
    canon_to_tid: dict[str, str] = {}

    for t in scanned_tickets:
        tid = _extract_ticket_id(t)
        canon_tid = _canonical_ticket_id(tid)
        ticket_map[tid] = t
        canon_to_tid[canon_tid] = tid
        raw_blockers = t.get("blockers") or t.get("blocked_by") or t.get("Blocked by") or []
        blockers_map[tid] = set(v._parse_blocker_ids(raw_blockers))

    all_node_ids = set(ticket_map.keys())
    in_degree: dict[str, int] = {
        tid: len([
            b for b in blockers_map[tid]
            if _canonical_ticket_id(b) in canon_to_tid and _canonical_ticket_id(b) != _canonical_ticket_id(tid)
        ])
        for tid in all_node_ids
    }
    dependents: dict[str, list[str]] = {tid: [] for tid in all_node_ids}
    for tid in all_node_ids:
        for b in blockers_map[tid]:
            canon_b = _canonical_ticket_id(b)
            if canon_b in canon_to_tid and canon_b != _canonical_ticket_id(tid):
                target_tid = canon_to_tid[canon_b]
                dependents[target_tid].append(tid)

    ready_queue: list[str] = sorted(
        [tid for tid in all_node_ids if in_degree[tid] == 0],
        key=_node_sort_key,
    )

    topological_order: list[str] = []
    while ready_queue:
        curr = ready_queue.pop(0)
        topological_order.append(curr)
        for dep in sorted(dependents[curr], key=_node_sort_key):
            in_degree[dep] -= 1
            if in_degree[dep] == 0:
                ready_queue.append(dep)
                ready_queue.sort(key=_node_sort_key)

    if len(topological_order) < len(all_node_ids):
        unresolved = all_node_ids - set(topological_order)
        cycle_nodes = _find_cycle_nodes(unresolved, blockers_map)
        cycle_ids_sorted = sorted(cycle_nodes, key=_node_sort_key)
        cycle_str = ", ".join(cycle_ids_sorted)
        msg = f"[ERR-DAG-CYCLE] Deadlock detected: Circular dependency detected involving ticket(s): {cycle_str}. Please resolve blockers."
        raise DagCycleError(msg, cycle_tickets=cycle_ids_sorted)

    return topological_order



def render_task_dashboard(
    tickets_or_tracker: Path | str | Sequence[dict[str, Any] | Path | str] | dict[str, Any],
    active_frontier: Sequence[str | int] | None = None,
    *,
    feature_slug: str | None = None,
    feature_dir: Path | str | None = None,
    phase_overrides: dict[int | str, str] | None = None,
    session_anchor: dict[str, Any] | Path | str | None = None,
    remediation_subrow: dict[str, Any] | None = None,
    deliverable_overrides: dict[int | str, str] | None = None,
    validator: ManifestValidator | None = None,
    root_dir: Path | str | None = None,
    include_heading: bool = True,
    write_back: bool = True,
    feature_title: str | None = None,
    mission: str | None = None,
    include_mermaid: bool = False,
    lean: bool = False,
) -> TaskDashboardResult:
    """
    [DASH-01] [DASH-02] [DASH-03] [ERR-DASH-01] [EDGE-DASH-01] [LINK-COCKPIT-02] Command Center visual task dashboard renderer and contextual chat handoff stream.
    Restricts the task graph artifact purely to interactive visual Mermaid DAGs (`graph TD`)
    and status progress tables (banning raw starter prompt code blocks) [DASH-01],
    while emitting Self-Contained Worker Starter Briefs enclosed in markdown code fences
    containing Target Ticket, Architectural Seam, Spec Scenario Tags, and Verification Gate in the chat stream [DASH-02].
    Detects and self-heals malformed task-graph.md schemas directly from authoritative ticket files under issues/ [ERR-DASH-01],
    and renders completed node styling with full DAG completion evaluation briefs upon 100% resolution [EDGE-DASH-01].
    """
    v = validator or ManifestValidator()
    base_root = Path(root_dir) if root_dir else None

    scanned_tickets: list[dict[str, Any] | Path | str] = []
    tracker_dir: Path | None = None
    detected_slug = feature_slug
    tg_disk_file: Path | None = None
    recovery_diagnostic = ""

    if feature_dir:
        fd_p = Path(feature_dir)
        if not fd_p.is_absolute() and base_root:
            fd_p = base_root / fd_p
        if not detected_slug:
            detected_slug = fd_p.name
        tracker_dir = fd_p / "issues" if (fd_p / "issues").is_dir() else fd_p
        tg_disk_file = fd_p / "task-graph.md" if fd_p.is_dir() else fd_p

    if isinstance(tickets_or_tracker, (str, Path)):
        p = Path(tickets_or_tracker)
        if not p.is_absolute() and base_root:
            p = base_root / p

        if not detected_slug:
            parts = Path(tickets_or_tracker).parts
            if ".scratch" in parts:
                idx = parts.index(".scratch")
                if idx + 1 < len(parts):
                    detected_slug = parts[idx + 1]
            if not detected_slug:
                detected_slug = p.name if p.name != "issues" else p.parent.name

        if p.is_dir():
            target_issues = p / "issues" if (p / "issues").is_dir() else (p if p.name == "issues" else p / "issues")
            tracker_dir = target_issues
            tg_disk_file = p / "task-graph.md" if p.name != "issues" else p.parent / "task-graph.md"
        elif p.is_file():
            if p.name.lower() in ("task-graph.md", "task-graph.markdown"):
                tg_disk_file = p
                target_issues = p.parent / "issues" if (p.parent / "issues").is_dir() else p.parent
                tracker_dir = target_issues
                if not detected_slug:
                    detected_slug = p.parent.name
            else:
                tracker_dir = p.parent
                target_issues = tracker_dir
                if not detected_slug:
                    detected_slug = p.parent.parent.name if p.parent.name == "issues" else p.parent.name
        else:
            target_issues = p / "issues"
            tracker_dir = target_issues
            tg_disk_file = p / "task-graph.md"

        md_files = [f for f in target_issues.glob("*.md") if f.is_file()] if target_issues.is_dir() else []
        if not target_issues.is_dir() or len(md_files) == 0:
            slug_name = detected_slug or "unknown"
            raise ScaffoldEmptyError(
                f"[ERR-SCAFFOLD-EMPTY] No ticket files found in .scratch/{slug_name}/issues/. Deconstruct spec before rendering dashboard."
            )

        scanned_tickets = v.scan_tickets_from_tracker(target_issues)
        if not scanned_tickets:
            slug_name = detected_slug or "unknown"
            raise ScaffoldEmptyError(
                f"[ERR-SCAFFOLD-EMPTY] No ticket files found in .scratch/{slug_name}/issues/. Deconstruct spec before rendering dashboard."
            )
    elif isinstance(tickets_or_tracker, (list, tuple)):
        scanned_tickets = list(tickets_or_tracker)
    else:
        scanned_tickets = [tickets_or_tracker]

    existing_commits: dict[str, str] = {}
    existing_statuses: dict[str, str] = {}
    extracted_phase_overrides: dict[int, str] = {}
    extracted_deliverables: dict[int, str] = {}
    recorded_identities: dict[str, dict[str, list[str]]] = {}
    is_legacy = False

    if tg_disk_file and tg_disk_file.is_file():
        try:
            tg_raw = tg_disk_file.read_text(encoding="utf-8")
            if not include_mermaid and re.search(r"```mermaid|subgraph\s+S_", tg_raw):
                include_mermaid = True
            evidence = extract_task_graph_evidence(tg_raw)
            existing_commits, existing_statuses, extracted_phase_overrides, extracted_deliverables = evidence
            recorded_identities = getattr(evidence, "recorded_identities", {})
            is_tg_valid, _ = validate_task_graph_schema(tg_raw, validator=v)
            is_legacy = is_legacy_task_graph(tg_raw)

            has_id_divergence = False
            if scanned_tickets and recorded_identities:
                for st in scanned_tickets:
                    if isinstance(st, dict):
                        st_id = str(st.get("id", "")).strip().lstrip("#")
                        norm_id = f"{int(st_id):02d}" if st_id.isdigit() else st_id
                        if norm_id in recorded_identities:
                            if not ticket_identity_matches(st, recorded_identities):
                                has_id_divergence = True
                                break

            if not is_tg_valid or is_legacy or has_id_divergence:
                rec_res = recover_malformed_dashboard(
                    tg_disk_file,
                    root_dir=base_root,
                    validator=v,
                    force_heal=has_id_divergence,
                    write_back=True,
                )
                recovery_diagnostic = rec_res.diagnostic
                scanned_tickets = rec_res.tickets
        except DagCycleError:
            raise
        except Exception:
            pass

    if not detected_slug and scanned_tickets:
        for t in scanned_tickets:
            if isinstance(t, dict) and t.get("path"):
                parts = Path(t["path"]).parts
                if ".scratch" in parts:
                    idx = parts.index(".scratch")
                    if idx + 1 < len(parts):
                        detected_slug = parts[idx + 1]
                        break

    if scanned_tickets:
        topological_sort_tickets(scanned_tickets, validator=v, root_dir=base_root)
        for st in scanned_tickets:
            if isinstance(st, dict):
                st_id = str(st.get("id", "")).strip().lstrip("#")
                norm_id = f"{int(st_id):02d}" if st_id.isdigit() else st_id
                curr_c = str(st.get("commit") or st.get("commit_sha") or "").strip()
                curr_s = str(st.get("status", "")).strip()

                has_disk_commit = bool(curr_c and curr_c != "-")
                is_disk_unresolved = bool(curr_s and not v._is_status_resolved(curr_s))
                identity_matches = ticket_identity_matches(st, recorded_identities) if recorded_identities else False
                can_upgrade_legacy = bool(is_legacy and identity_matches)

                if can_upgrade_legacy:
                    ex_c = existing_commits.get(norm_id, "")
                    ex_s = existing_statuses.get(norm_id, "")
                    if ex_c and ex_c != "-":
                        st["commit"] = ex_c
                    elif has_disk_commit:
                        st["commit"] = curr_c

                    if v._is_status_resolved(ex_s) or (ex_c and ex_c != "-"):
                        st["status"] = "resolved"
                    elif v._is_status_in_progress(ex_s):
                        st["status"] = "in-progress"
                    elif has_disk_commit:
                        st["status"] = "resolved"
                    elif curr_s:
                        st["status"] = curr_s
                    else:
                        st["status"] = "ready-for-agent"
                else:
                    if not is_legacy:
                        if has_disk_commit:
                            st["status"] = "resolved"
                            st["commit"] = curr_c
                        elif v._is_status_resolved(curr_s):
                            st["status"] = "resolved"
                            ex_c = existing_commits.get(norm_id, "")
                            st["commit"] = ex_c if (ex_c and ex_c != "-") else "-"
                        else:
                            st["status"] = curr_s if curr_s else "ready-for-agent"
                            st["commit"] = "-"
                    else:
                        if has_disk_commit:
                            st["status"] = "resolved"
                            st["commit"] = curr_c
                        elif is_disk_unresolved:
                            st["status"] = curr_s
                            st["commit"] = "-"
                            if identity_matches and norm_id in existing_statuses and v._is_status_in_progress(existing_statuses[norm_id]):
                                st["status"] = "in-progress"
                        else:
                            if identity_matches and (not curr_c or curr_c == "-") and norm_id in existing_commits:
                                st["commit"] = existing_commits[norm_id]
                            elif has_disk_commit:
                                st["commit"] = curr_c

                            if norm_id in existing_statuses and v._is_status_resolved(existing_statuses[norm_id]):
                                st["status"] = "resolved"
                            elif curr_s and v._is_status_resolved(curr_s):
                                st["status"] = "resolved"
                            elif norm_id in existing_commits and existing_commits[norm_id] != "-":
                                if identity_matches:
                                    st["status"] = "resolved"

    frontier_ids: list[str] = []
    if active_frontier is not None:
        for f in active_frontier:
            f_s = str(f).strip().lstrip("#")
            if f_s.lower().startswith("ticket-"):
                f_s = f_s[7:].strip()
            if f_s.isdigit():
                frontier_ids.append(f"{int(f_s):02d}")
            else:
                frontier_ids.append(f_s)
    else:
        frontier_ids = v.calculate_unblocked_frontier(scanned_tickets)

    total_tickets = len(scanned_tickets)
    resolved_count = 0
    for t in scanned_tickets:
        if isinstance(t, dict):
            if v._is_status_resolved(str(t.get("status", ""))):
                resolved_count += 1
        elif isinstance(t, (str, Path)) and Path(t).is_file():
            try:
                content = Path(t).read_text(encoding="utf-8")
                st = v._extract_ticket_status_from_text(content)
                if v._is_status_resolved(st):
                    resolved_count += 1
            except Exception:
                pass

    all_resolved = total_tickets > 0 and resolved_count >= total_tickets

    merged_phase_overrides: dict[int | str, str] = dict(extracted_phase_overrides)
    if phase_overrides:
        merged_phase_overrides.update(phase_overrides)

    merged_deliverables: dict[int | str, str] = dict(extracted_deliverables)
    if deliverable_overrides:
        merged_deliverables.update(deliverable_overrides)

    artifact = v.render_task_dashboard(
        tickets=scanned_tickets,
        active_frontier=frontier_ids,
        feature_slug=detected_slug,
        phase_overrides=merged_phase_overrides if merged_phase_overrides else None,
        session_anchor=session_anchor,
        remediation_subrow=remediation_subrow,
        deliverable_overrides=merged_deliverables if merged_deliverables else None,
        feature_title=feature_title,
        mission=mission,
        include_mermaid=include_mermaid,
    )

    if tg_disk_file and (write_back or recovery_diagnostic):
        try:
            tg_disk_file.parent.mkdir(parents=True, exist_ok=True)
            tg_disk_file.write_text(artifact, encoding="utf-8")
        except Exception:
            pass

    chat_stream = ""
    standby_msg = (
        "> ⏳ Standby: All active frontier tickets are currently in-progress. "
        "Awaiting worker completion before calculating downstream unblocked tickets."
    )

    if all_resolved:
        eval_target = tracker_dir or (detected_slug and (base_root / ".scratch" / detected_slug if base_root else Path(f".scratch/{detected_slug}")))
        target_f_dir = tg_disk_file.parent if tg_disk_file else None
        if not target_f_dir and tracker_dir:
            target_f_dir = tracker_dir.parent if tracker_dir.name == "issues" else tracker_dir
        if not target_f_dir and base_root and detected_slug:
            target_f_dir = base_root / ".scratch" / detected_slug
        elif not target_f_dir and detected_slug:
            target_f_dir = Path(f".scratch/{detected_slug}")

        p5_status = str(merged_phase_overrides.get(5, "")).lower() if merged_phase_overrides else ""
        cr_artifact = target_f_dir / "code-review.md" if target_f_dir else None
        cr_has_pass = False
        if cr_artifact and cr_artifact.is_file():
            try:
                cr_txt = cr_artifact.read_text(encoding="utf-8").upper()
                if "PASS" in cr_txt and "FAIL" not in cr_txt:
                    cr_has_pass = True
            except Exception:
                pass
        phase5_passed = (
            any(k in p5_status for k in ("completed", "✅", "bypassed", "⏭️"))
            or cr_has_pass
        )

        p6_status = str(merged_phase_overrides.get(6, "")).lower() if merged_phase_overrides else ""
        sa_artifact = target_f_dir / "security-audit.md" if target_f_dir else None
        sa_has_pass = False
        if sa_artifact and sa_artifact.is_file():
            try:
                sa_txt = sa_artifact.read_text(encoding="utf-8").upper()
                if "PASS" in sa_txt and "FAIL" not in sa_txt:
                    sa_has_pass = True
            except Exception:
                pass
        phase6_passed = (
            any(k in p6_status for k in ("completed", "✅", "bypassed", "⏭️"))
            or sa_has_pass
        )

        is_p5_escalated = any(k in p5_status for k in ("escalated", "⚠️ escalated-to-human"))
        is_p6_escalated = any(k in p6_status for k in ("escalated", "⚠️ escalated-to-human"))

        if is_p5_escalated:
            chat_stream = (
                "⚠️ [ERR-EVAL-01] Circuit Breaker Active: Phase 5 (Two-Axis Code Review) has escalated to human "
                "(status: '⚠️ escalated-to-human'). Automated remediation halted due to persistent findings across successive attempts.\n\n"
                "Please resolve the defect manually in the Command Center (CC) before proceeding."
            )
        elif is_p6_escalated:
            chat_stream = (
                "⚠️ [ERR-EVAL-01] Circuit Breaker Active: Phase 6 (Semantic Security Audit) has escalated to human "
                "(status: '⚠️ escalated-to-human'). Automated remediation halted due to persistent vulnerabilities across successive attempts.\n\n"
                "Please resolve the defect manually in the Command Center (CC) before proceeding."
            )
        elif phase5_passed and not phase6_passed:
            eval_brief = format_evaluation_brief(
                "security-audit",
                feature_dir_or_target=eval_target,
                root_dir=base_root,
                validator=v,
                include_heading=False if lean else True,
                lean=lean,
            )
            if lean:
                chat_stream = eval_brief
            elif any(err in eval_brief for err in ("[ERR-EVAL-03]", "[ERR-LEAN-EVAL-02]", "[ERR-LEAN-EVAL-01]")):
                chat_stream = (
                    "🎉 Phase 5: Code Review is complete (verdict: PASS)!\n\n"
                    f"{eval_brief}"
                )
            else:
                chat_stream = (
                    "🎉 Phase 5: Code Review is complete (verdict: PASS)!\n\n"
                    "Proceed to Phase 6: Semantic Security Audit.\n\n"
                    f"{eval_brief}"
                )
        elif phase5_passed and phase6_passed:
            eval_brief = format_evaluation_brief(
                "pr",
                feature_dir_or_target=eval_target,
                root_dir=base_root,
                validator=v,
                include_heading=False if lean else True,
                lean=lean,
            )
            if lean:
                chat_stream = eval_brief
            elif any(err in eval_brief for err in ("[ERR-EVAL-03]", "[ERR-LEAN-EVAL-02]", "[ERR-LEAN-EVAL-01]")):
                chat_stream = (
                    "🎉 Phase 6: Semantic Security Audit is complete (verdict: PASS)!\n\n"
                    f"{eval_brief}"
                )
            else:
                chat_stream = (
                    "🎉 Phase 6: Semantic Security Audit is complete (verdict: PASS)!\n\n"
                    "Proceed to Phase 7: Pull Request.\n\n"
                    f"{eval_brief}"
                )
        else:
            eval_brief = format_evaluation_brief(
                "code-review",
                feature_dir_or_target=eval_target,
                root_dir=base_root,
                validator=v,
                include_heading=False if lean else True,
                lean=lean,
            )
            if lean:
                chat_stream = eval_brief
            elif any(err in eval_brief for err in ("[ERR-EVAL-03]", "[ERR-LEAN-EVAL-02]", "[ERR-LEAN-EVAL-01]")):
                chat_stream = (
                    "🎉 All tickets in the task graph are resolved (100% complete)!\n\n"
                    f"{eval_brief}"
                )
            else:
                chat_stream = (
                    "🎉 All tickets in the task graph are resolved (100% complete)!\n\n"
                    "Phase 4 Implementation is complete. Proceed to Phase 5: Code Review.\n\n"
                    f"{eval_brief}"
                )
    elif not frontier_ids:
        has_in_progress = False
        for t in scanned_tickets:
            if isinstance(t, dict) and v._is_status_in_progress(str(t.get("status", ""))):
                has_in_progress = True
                break
        if has_in_progress:
            chat_stream = standby_msg
        else:
            chat_stream = "> ⚠️ No unblocked frontier tickets available in the task graph."
    else:
        frontier_set = set(frontier_ids)
        frontier_ticket_items: list[dict[str, Any] | Path | str] = []
        all_frontier_in_progress = True

        for t in scanned_tickets:
            tid = ""
            status = ""
            if isinstance(t, dict):
                tid = str(t.get("id", "")).strip().lstrip("#")
                if tid.lower().startswith("ticket-"):
                    tid = tid[7:].strip()
                if tid.isdigit():
                    tid = f"{int(tid):02d}"
                status = str(t.get("status", ""))
            elif isinstance(t, (str, Path)) and Path(t).is_file():
                p_t = Path(t)
                m = re.match(r"^([0-9]{1,3})", p_t.stem)
                tid = f"{int(m.group(1)):02d}" if m else p_t.stem
                try:
                    status = v._extract_ticket_status_from_text(p_t.read_text(encoding="utf-8"))
                except Exception:
                    status = ""

            if tid in frontier_set:
                if not v._is_status_in_progress(status) and not v._is_status_resolved(status):
                    all_frontier_in_progress = False
                    frontier_ticket_items.append(t)

        if all_frontier_in_progress or not frontier_ticket_items:
            chat_stream = standby_msg
        else:
            total_unresolved = total_tickets - resolved_count
            is_final = (
                total_tickets > 0
                and total_unresolved == 1
                and len(frontier_ticket_items) == 1
            )
            chat_stream = format_worker_brief(
                frontier_ticket_items,
                root_dir=base_root,
                validator=v,
                feature_slug=detected_slug,
                include_heading=include_heading,
                is_final_frontier=is_final,
                lean=lean,
            )

    res = TaskDashboardResult(
        artifact=artifact,
        chat_stream=chat_stream,
        frontier=frontier_ids,
        all_resolved=all_resolved,
        total_tickets=total_tickets,
        resolved_tickets=resolved_count,
        recovery_diagnostic=recovery_diagnostic,
    )
    return res


def is_doc_file(filepath: str | Path) -> bool:
    """
    [EDGE-EVAL-01] Evaluates whether a single file is purely a documentation or markdown artifact.
    Rejects executable code, scripts, configs, database schemas, binaries, and credentials/secrets
    (even when located within docs/ or .scratch/ directories).
    """
    p = Path(str(filepath).strip())
    name = p.name.lower()
    suffix = p.suffix.lower()

    # Rejection of credential, secret, certificate, or cryptographic key patterns
    if (
        name.startswith(".env")
        or suffix in (".pem", ".key", ".p12", ".pfx", ".crt", ".der", ".keystore")
        or name.startswith(("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"))
        or any(tok in name for tok in ("credential", "secret", "token", "password", "auth_token"))
    ):
        return False

    # Rejection of executable code, scripts, configurations, and schemas
    if suffix in (
        ".py", ".ts", ".js", ".tsx", ".jsx", ".mjs", ".cjs", ".go", ".rs", ".sh",
        ".bash", ".zsh", ".rb", ".php", ".java", ".c", ".cpp", ".h", ".cs", ".json",
        ".yaml", ".yml", ".toml", ".sql", ".graphql", ".proto", ".exe", ".bin", ".elf",
        ".bat", ".cmd", ".ps1", ".wasm",
    ):
        return False

    # Canonical documentation markup extensions
    if suffix in (".md", ".markdown", ".txt", ".rst", ".adoc", ".asciidoc", ".mermaid"):
        return True

    parts = [part.lower() for part in p.parts]

    # Canonical extensionless documentation files
    if suffix == "" and name in ("readme", "license", "licence", "notice", "copying", "authors", "changelog", "history"):
        return True

    # Documentation images and visual artifacts located strictly within documentation directories
    if any(d in parts for d in ("docs", "documentation", ".scratch")):
        if suffix in (".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".html"):
            return True

    return False


def evaluate_documentation_diff(
    diff_or_files: Sequence[str] | str | None = None,
    *,
    diff_range: str | None = None,
    root_dir: Path | str | None = None,
    feature_dir: Path | str | None = None,
) -> DocDiffEvaluationResult:
    """
    [EDGE-EVAL-01] Evaluates git diff or changed files against documentation-only heuristics.
    Returns DocDiffEvaluationResult asserting whether all changed files are documentation artifacts
    (zero executable application code or attack surface seams).
    """
    base_root = Path(root_dir).resolve() if root_dir else None
    diff_val = diff_range or "main...HEAD"
    all_files: list[str] = []
    provided_explicit_files = False

    if diff_or_files is not None:
        if isinstance(diff_or_files, (list, tuple, set)):
            provided_explicit_files = True
            all_files = [str(f).strip() for f in diff_or_files if str(f).strip()]
        elif isinstance(diff_or_files, str):
            s = diff_or_files.strip()
            if not s:
                diff_val = diff_range or "main...HEAD"
            elif "\n" in s:
                provided_explicit_files = True
                all_files = [line.strip() for line in s.splitlines() if line.strip()]
            elif ".." in s or "..." in s:
                diff_val = s
            elif not s.startswith("-") and (Path(s).is_file() or (not any(c in s for c in " ~^") and ("/" in s or Path(s).suffix))):
                provided_explicit_files = True
                all_files = [s]
            else:
                diff_val = s

    if not diff_val or diff_val.startswith("-") or not re.match(r"^[A-Za-z0-9_./@~^{}-]+$", diff_val):
        diff_val = "main...HEAD"

    if not provided_explicit_files and not all_files:
        try:
            repo_target = base_root or (feature_dir.parent.parent if feature_dir and ".scratch" in feature_dir.parts else REPO_ROOT)
            res = subprocess.run(
                ["git", "diff", "--name-only", diff_val],
                cwd=str(repo_target),
                capture_output=True,
                text=True,
                timeout=5,
            )
            if res.returncode == 0 and res.stdout.strip():
                all_files = [line.strip() for line in res.stdout.splitlines() if line.strip()]
        except Exception:
            all_files = []

    if not all_files:
        return DocDiffEvaluationResult(False, [], [], diagnostic="No files changed in diff range.")

    non_docs: list[str] = [f for f in all_files if not is_doc_file(f)]
    is_docs_only = (len(non_docs) == 0 and len(all_files) > 0)
    diag = (
        "Documentation-only diff (zero application code changed)."
        if is_docs_only
        else f"Diff contains executable/code files: {', '.join(non_docs[:3])}"
    )
    return DocDiffEvaluationResult(is_docs_only, all_files, non_docs, diagnostic=diag)


class EvaluationPromptRejectionResult:
    """
    [ERR-EVAL-02] Structured outcome of evaluation tab prompt emission validation.
    Enforces that evaluation sessions (/code-review and /security-audit) do NOT emit /pr starter briefs
    and direct the operator to return to Command Center for release readiness evaluation.
    """
    rejected: bool
    phase: str
    target_phase: str
    verdict: str
    directive: str
    diagnostic: str

    def __init__(
        self,
        rejected: bool,
        phase: str,
        target_phase: str,
        verdict: str,
        directive: str,
        diagnostic: str,
    ) -> None:
        self.rejected = rejected
        self.phase = phase
        self.target_phase = target_phase
        self.verdict = verdict
        self.directive = directive
        self.diagnostic = diagnostic

    @property
    def is_rejected(self) -> bool:
        return self.rejected

    def format_notice(self) -> str:
        return self.diagnostic

    def __str__(self) -> str:
        return self.diagnostic


class EvaluationTabPromptRejectionError(ValueError):
    """[ERR-EVAL-02] [ERR-LEAN-EVAL-03] Raised when an evaluation session attempts to draft or emit a /pr starter brief."""
    pass


def reject_evaluation_tab_pr_prompt(
    caller_phase: str = "code-review",
    verdict: str = "PASS",
    target_phase: str = "pr",
    *,
    feature_dir: Path | str | None = None,
    task_graph_path: Path | str | None = None,
    root_dir: Path | str | None = None,
) -> EvaluationPromptRejectionResult:
    """
    [ERR-EVAL-02] [ERR-LEAN-EVAL-03] Evaluates and rejects attempts by evaluation sessions (/code-review or /security-audit)
    to draft or emit the contextual /pr starter brief. Directs operator to return to Command Center
    so CC can evaluate release readiness and synthesize the /pr starter brief.
    """
    clean_caller = str(caller_phase or "code-review").strip().lower().lstrip("/")
    clean_target = str(target_phase or "pr").strip().lower().lstrip("/")
    clean_verdict = str(verdict or "PASS").strip().upper()

    directive = (
        "Evaluation sessions must seal upon completion and conclude immediately with Recommend-and-Stop. "
        "Operator MUST return to the Command Center (CC) so CC can evaluate release readiness and synthesize the /pr starter brief."
    )
    diagnostic = (
        f"❌ [ERR-EVAL-02] [ERR-LEAN-EVAL-03] Evaluation Tab Prompt Emission Rejected: "
        f"Drafting or emitting the contextual '/{clean_target}' starter brief from an evaluation session ('/{clean_caller}') is strictly prohibited. "
        f"{directive}"
    )

    return EvaluationPromptRejectionResult(
        rejected=True,
        phase=clean_caller,
        target_phase=clean_target,
        verdict=clean_verdict,
        directive=directive,
        diagnostic=diagnostic,
    )


def format_evaluation_completion_report(
    phase: str,
    verdict: str = "PASS",
    feature_dir_or_target: Path | str | None = None,
    *,
    report_path: Path | str | None = None,
    feature_dir: Path | str | None = None,
    task_graph_path: Path | str | None = None,
    root_dir: Path | str | None = None,
    summary: str = "",
    findings: str = "",
    attempt_pr_brief: bool = False,
    remediated: bool = False,
    commit_sha: str = "",
) -> str:
    """
    [ERR-EVAL-02] Generates completion report for evaluation sessions (/code-review or /security-audit).
    When completing with verdict PASS, strictly suppresses /pr starter brief emission and directs
    the operator to return to the Command Center so CC can evaluate release readiness and synthesize the /pr brief.
    """
    clean_phase = str(phase or "code-review").strip().lower().lstrip("/")
    clean_verdict = str(verdict or "PASS").strip().upper()
    base_root = Path(root_dir).resolve() if root_dir else None

    f_dir = None
    target_raw = feature_dir_or_target or feature_dir
    if target_raw:
        fp = Path(target_raw)
        if not fp.is_absolute() and base_root and (base_root / fp).exists():
            fp = base_root / fp
        if fp.is_dir():
            f_dir = fp.parent if fp.name == "issues" else fp
        elif fp.is_file():
            f_dir = fp.parent if fp.parent.name != "issues" else fp.parent.parent

    if not f_dir:
        if base_root and (base_root / ".scratch").is_dir():
            candidates = [d for d in (base_root / ".scratch").iterdir() if d.is_dir() and not d.name.startswith(".")]
            f_dir = candidates[0] if candidates else base_root / ".scratch" / "feature"
        else:
            f_dir = Path(".scratch/feature")

    slug = f_dir.name
    is_sec = any(k in clean_phase for k in ("security", "audit"))
    phase_name = "Semantic Security Audit" if is_sec else "Two-Axis Code Review"
    doc_name = "security-audit.md" if is_sec else "code-review.md"

    if report_path:
        rep_p = Path(report_path)
        if not rep_p.is_absolute() and base_root and (base_root / rep_p).exists():
            rep_p = base_root / rep_p
        rep_display = _normalize_ticket_display_path(rep_p, root_dir=base_root)
    else:
        rep_file = f_dir / doc_name
        rep_display = _normalize_ticket_display_path(rep_file, root_dir=base_root) if rep_file.is_file() else f".scratch/{slug}/{doc_name}"

    summary_clean = str(summary or "").strip()
    findings_clean = str(findings or "").strip()

    details: list[str] = []
    if summary_clean:
        details.append(f"Summary: {summary_clean}")
    if findings_clean:
        details.append(f"Findings: {findings_clean}")
    elif clean_verdict == "PASS":
        details.append("Findings: Zero blocking violations detected.")
    if remediated:
        rem_str = "Remediation: Completed (remediated: true)"
        if commit_sha:
            rem_str += f" (commit: {commit_sha})"
        details.append(rem_str)

    details_block = "\n".join(details) + "\n" if details else ""

    rejection_block = ""
    if attempt_pr_brief:
        rej = reject_evaluation_tab_pr_prompt(caller_phase=clean_phase, verdict=clean_verdict)
        rejection_block = f"\n{rej.diagnostic}\n"

    report = (
        f"✅ Phase '{phase_name}' completed with verdict '{clean_verdict}'.\n"
        f"📁 Report: {rep_display}\n"
        f"{details_block}"
        f"{rejection_block}\n"
        "Return to the Command Center (CC) session to report this result.\n"
        "The Command Center will evaluate release readiness and synthesize the /pr starter brief.\n"
        "Conclude your turn immediately with Recommend-and-Stop."
    )
    return report


class GitDiffStateResult:
    """
    [ERR-EVAL-03] Structured outcome of Git diff range and repository branch state validation.
    """
    valid: bool
    diagnostic: str
    is_detached_head: bool
    missing_base_branch: bool
    zero_common_ancestors: bool
    diff_range: str
    base_ref: str
    target_ref: str

    def __init__(
        self,
        valid: bool,
        diagnostic: str = "",
        *,
        is_detached_head: bool = False,
        missing_base_branch: bool = False,
        zero_common_ancestors: bool = False,
        diff_range: str = "main...HEAD",
        base_ref: str = "main",
        target_ref: str = "HEAD",
    ) -> None:
        self.valid = valid
        self.diagnostic = diagnostic
        self.is_detached_head = is_detached_head
        self.missing_base_branch = missing_base_branch
        self.zero_common_ancestors = zero_common_ancestors
        self.diff_range = diff_range
        self.base_ref = base_ref
        self.target_ref = target_ref

    @property
    def is_valid(self) -> bool:
        return self.valid

    def __bool__(self) -> bool:
        return self.valid

    def __str__(self) -> str:
        return self.diagnostic


class EvaluationBriefGitError(ValueError):
    """
    [ERR-EVAL-03] Raised when Command Center attempts to synthesize evaluation briefs
    on a missing base branch or detached HEAD state with zero common ancestors.
    """
    pass


class EvaluationPhaseValidationError(ValueError):
    """
    [ERR-LEAN-EVAL-01] Raised when Command Center attempts to synthesize an evaluation brief
    with an unrecognized or invalid downstream phase identifier.
    """
    pass


VALID_EVAL_PHASES: dict[str, str] = {
    "code-review": "code-review",
    "review": "code-review",
    "codereview": "code-review",
    "security-audit": "security-audit",
    "security": "security-audit",
    "audit": "security-audit",
    "securityaudit": "security-audit",
    "pr": "pr",
    "pull-request": "pr",
    "ship": "pr",
}


def validate_git_diff_state(
    diff_range: str | None = "main...HEAD",
    *,
    root_dir: Path | str | None = None,
    feature_dir: Path | str | None = None,
    raise_on_error: bool = False,
) -> GitDiffStateResult:
    """
    [ERR-EVAL-03] Validates that the git repository is in a valid state to compute the evaluation diff range.
    Rejects missing base branch (e.g. 'main' does not exist) or detached HEAD state with zero common ancestors.
    Prompts the operator to fetch the base branch or checkout a valid feature branch before proceeding.
    """
    repo_target = None
    if root_dir:
        p = Path(root_dir).resolve()
        if p.exists():
            repo_target = p
    elif feature_dir:
        p = Path(feature_dir).resolve()
        if p.exists():
            repo_target = p.parent.parent if ".scratch" in p.parts else p
    else:
        if not diff_range or diff_range == "main...HEAD":
            repo_target = REPO_ROOT

    if not repo_target or not Path(repo_target).exists():
        return GitDiffStateResult(True, "Target repository path does not exist; skipping diff validation.")

    try:
        proc_check = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(repo_target),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc_check.returncode != 0 or proc_check.stdout.strip() != "true":
            return GitDiffStateResult(True, "Not inside a git repository; skipping diff validation.")
    except Exception:
        return GitDiffStateResult(True, "Git command unavailable; skipping diff validation.")

    clean_range = str(diff_range or "main...HEAD").strip()
    if not clean_range or clean_range.startswith("-") or not re.match(r"^[A-Za-z0-9_./@~^{}-]+$", clean_range):
        clean_range = "main...HEAD"

    if "..." in clean_range:
        parts = clean_range.split("...", 1)
        base_ref = parts[0].strip() or "main"
        target_ref = parts[1].strip() or "HEAD"
    elif ".." in clean_range:
        parts = clean_range.split("..", 1)
        base_ref = parts[0].strip() or "main"
        target_ref = parts[1].strip() or "HEAD"
    else:
        base_ref = clean_range or "main"
        target_ref = "HEAD"

    try:
        rev_proc = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{base_ref}^{{commit}}"],
            cwd=str(repo_target),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if rev_proc.returncode != 0:
            rev_proc2 = subprocess.run(
                ["git", "rev-parse", "--verify", "--quiet", base_ref],
                cwd=str(repo_target),
                capture_output=True,
                text=True,
                timeout=5,
            )
            if rev_proc2.returncode != 0:
                diag = (
                    f"⚠️ [ERR-LEAN-EVAL-02] [ERR-EVAL-03] Evaluation Brief Synthesis Failed: Git base branch '{base_ref}' does not exist in repository. "
                    f"Cannot compute diff range '{clean_range}'. "
                    f"Please verify branch tracking and merge-base (e.g. 'git fetch origin {base_ref}') or checkout a valid feature branch before proceeding."
                )
                res = GitDiffStateResult(
                    False,
                    diagnostic=diag,
                    missing_base_branch=True,
                    diff_range=clean_range,
                    base_ref=base_ref,
                    target_ref=target_ref,
                )
                if raise_on_error:
                    raise EvaluationBriefGitError(diag)
                return res

        sym_proc = subprocess.run(
            ["git", "symbolic-ref", "-q", "HEAD"],
            cwd=str(repo_target),
            capture_output=True,
            text=True,
            timeout=5,
        )
        is_detached = (sym_proc.returncode != 0)
        if not is_detached:
            abbrev_proc = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=str(repo_target),
                capture_output=True,
                text=True,
                timeout=5,
            )
            if abbrev_proc.returncode == 0 and abbrev_proc.stdout.strip() == "HEAD":
                is_detached = True

        mb_proc = subprocess.run(
            ["git", "merge-base", base_ref, target_ref],
            cwd=str(repo_target),
            capture_output=True,
            text=True,
            timeout=5,
        )
        has_common_ancestor = (mb_proc.returncode == 0 and bool(mb_proc.stdout.strip()))

        if is_detached:
            diag = (
                f"⚠️ [ERR-LEAN-EVAL-02] [ERR-EVAL-03] Evaluation Brief Synthesis Failed: Git repository is in a detached HEAD state. "
                f"Cannot compute diff range '{clean_range}' reliably. "
                f"Please verify branch tracking and merge-base, fetch the base branch, or checkout a valid feature branch before proceeding."
            )
            res = GitDiffStateResult(
                False,
                diagnostic=diag,
                is_detached_head=True,
                zero_common_ancestors=(not has_common_ancestor),
                diff_range=clean_range,
                base_ref=base_ref,
                target_ref=target_ref,
            )
            if raise_on_error:
                raise EvaluationBriefGitError(diag)
            return res

        if not has_common_ancestor:
            diag = (
                f"⚠️ [ERR-LEAN-EVAL-02] [ERR-EVAL-03] Evaluation Brief Synthesis Failed: Zero common ancestors detected "
                f"between '{base_ref}' and '{target_ref}'. "
                f"Cannot compute diff range '{clean_range}'. "
                f"Please verify branch tracking and merge-base, fetch the base branch, or checkout a valid feature branch before proceeding."
            )
            res = GitDiffStateResult(
                False,
                diagnostic=diag,
                is_detached_head=is_detached,
                zero_common_ancestors=True,
                diff_range=clean_range,
                base_ref=base_ref,
                target_ref=target_ref,
            )
            if raise_on_error:
                raise EvaluationBriefGitError(diag)
            return res

        diff_proc = subprocess.run(
            ["git", "diff", "--name-only", clean_range],
            cwd=str(repo_target),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if diff_proc.returncode != 0:
            err_msg = diff_proc.stderr.strip()
            diag = (
                f"⚠️ [ERR-LEAN-EVAL-02] [ERR-EVAL-03] Evaluation Brief Synthesis Failed: Git diff failed for range '{clean_range}' ({err_msg}). "
                f"Please verify branch tracking and merge-base, fetch the base branch, or checkout a valid feature branch before proceeding."
            )
            res = GitDiffStateResult(
                False,
                diagnostic=diag,
                is_detached_head=is_detached,
                diff_range=clean_range,
                base_ref=base_ref,
                target_ref=target_ref,
            )
            if raise_on_error:
                raise EvaluationBriefGitError(diag)
            return res

    except (subprocess.SubprocessError, OSError) as exc:
        diag = (
            f"⚠️ [ERR-LEAN-EVAL-02] [ERR-EVAL-03] Evaluation Brief Synthesis Failed: Git execution error ({exc}). "
            f"Cannot compute diff range '{clean_range}'. "
            f"Please verify branch tracking and merge-base, fetch the base branch, or checkout a valid feature branch before proceeding."
        )
        res = GitDiffStateResult(
            False,
            diagnostic=diag,
            diff_range=clean_range,
            base_ref=base_ref,
            target_ref=target_ref,
        )
        if raise_on_error:
            raise EvaluationBriefGitError(diag)
        return res

    return GitDiffStateResult(
        True,
        diagnostic=f"Git diff state is valid for '{clean_range}'.",
        is_detached_head=is_detached,
        diff_range=clean_range,
        base_ref=base_ref,
        target_ref=target_ref,
    )


def format_evaluation_brief(
    eval_type: str,
    feature_dir_or_target: Path | str | None = None,
    tracker_dir_or_feature_dir: Path | str | None = None,
    *,
    feature_dir: Path | str | None = None,
    task_graph_path: Path | str | None = None,
    spec_path: Path | str | None = None,
    living_spec_path: Path | str | None = None,
    delta_spec_path: Path | str | None = None,
    diff_range: str | None = None,
    git_diff_range: str | None = None,
    attack_surfaces: list[str] | str | None = None,
    attack_surface_seams: list[str] | str | None = None,
    changed_files: Sequence[str] | str | None = None,
    root_dir: Path | str | None = None,
    validator: ManifestValidator | None = None,
    include_heading: bool = True,
    caller_phase: str | None = None,
    session_type: str | None = None,
    from_evaluation_tab: bool = False,
    is_evaluation_tab: bool = False,
    verdict: str | None = None,
    raise_on_rejection: bool = False,
    raise_on_error: bool = False,
    lean: bool = True,
) -> str:
    """
    [EVAL-01] [EVAL-02] [EDGE-EVAL-01] [LEAN-EVAL-01] [ERR-EVAL-01] [ERR-EVAL-02] [ERR-LEAN-EVAL-03] Generates copy-pasteable contextual evaluation starter brief(s)
    for downstream phases (/code-review and /security-audit) in fresh tabs, or ultra-lean 1-line command for /pr.
    Embeds target living specification path, delta spec path, exact git diff range,
    attack surface pointers, circuit breaker instructions, and in-session '--fix' remediation instructions.
    Rejects direct /pr starter brief drafting when requested from an evaluation session tab [ERR-EVAL-02] [ERR-LEAN-EVAL-03].
    """
    clean_type = str(eval_type or "").strip().strip("/").strip().lower().replace("_", "-")
    base_root = Path(root_dir).resolve() if root_dir else None

    if clean_type not in VALID_EVAL_PHASES:
        diag = (
            f"❌ [ERR-LEAN-EVAL-01] Evaluation Phase Validation Failed: Unrecognized phase '{eval_type}'. "
            f"Valid evaluation targets are: 'code-review', 'security-audit', 'pr'."
        )
        if raise_on_error or raise_on_rejection:
            raise EvaluationPhaseValidationError(diag)
        return diag

    canonical_type = VALID_EVAL_PHASES[clean_type]

    norm_caller = str(caller_phase or session_type or "").strip().lower().lstrip("/")
    is_eval_caller = (
        from_evaluation_tab
        or is_evaluation_tab
        or any(k in norm_caller for k in ("code-review", "code_review", "security-audit", "security_audit", "evaluation"))
    )

    if is_eval_caller and canonical_type == "pr":
        rejection_res = reject_evaluation_tab_pr_prompt(
            caller_phase=norm_caller or "code-review",
            verdict=verdict or "PASS",
            target_phase=clean_type,
            feature_dir=feature_dir,
            task_graph_path=task_graph_path,
            root_dir=base_root,
        )
        if raise_on_rejection:
            raise EvaluationTabPromptRejectionError(rejection_res.diagnostic)
        return rejection_res.diagnostic

    raw_target = feature_dir_or_target if feature_dir_or_target is not None else (tracker_dir_or_feature_dir if tracker_dir_or_feature_dir is not None else feature_dir)

    target_p = None
    if task_graph_path:
        target_p = Path(task_graph_path)
        if not target_p.is_absolute() and base_root and (base_root / target_p).exists():
            target_p = base_root / target_p
    elif raw_target:
        target_p = Path(raw_target)
        if not target_p.is_absolute() and base_root and (base_root / target_p).exists():
            target_p = base_root / target_p
        elif not target_p.exists() and base_root and (base_root / ".scratch" / raw_target).exists():
            target_p = base_root / ".scratch" / raw_target

    feature_dir = None
    tg_file: Path | None = None

    if target_p:
        if target_p.is_file() and target_p.name.lower() in ("task-graph.md", "task-graph.markdown"):
            tg_file = target_p
            feature_dir = target_p.parent
        elif target_p.is_file():
            feature_dir = target_p.parent if target_p.parent.name != "issues" else target_p.parent.parent
            tg_file = feature_dir / "task-graph.md"
        elif target_p.is_dir():
            if target_p.name == "issues":
                feature_dir = target_p.parent
            else:
                feature_dir = target_p
            tg_file = feature_dir / "task-graph.md"
        else:
            parts = target_p.parts
            if ".scratch" in parts:
                idx = parts.index(".scratch")
                slug = parts[idx + 1] if len(parts) > idx + 1 else "feature"
            else:
                slug = str(feature_dir_or_target).strip("/")
            if base_root:
                feature_dir = base_root / ".scratch" / slug
            else:
                feature_dir = Path(f".scratch/{slug}")
            tg_file = feature_dir / "task-graph.md"
    else:
        if base_root and (base_root / ".scratch").is_dir():
            candidates = [d for d in (base_root / ".scratch").iterdir() if d.is_dir() and not d.name.startswith(".")]
            feature_dir = candidates[0] if candidates else base_root / ".scratch" / "feature"
        else:
            feature_dir = Path(".scratch/operator-command-center-flow")
        tg_file = feature_dir / "task-graph.md"

    feature_slug = "feature"
    if feature_dir:
        feature_slug = feature_dir.name

    tg_display = f".scratch/{feature_slug}/task-graph.md"
    if tg_file:
        tg_display = _normalize_ticket_display_path(tg_file, root_dir=base_root)

    if delta_spec_path:
        delta_display = _normalize_ticket_display_path(delta_spec_path, root_dir=base_root)
    else:
        cand_spec = feature_dir / "spec.md" if feature_dir else None
        if cand_spec and cand_spec.is_file():
            delta_display = _normalize_ticket_display_path(cand_spec, root_dir=base_root)
        else:
            delta_display = f".scratch/{feature_slug}/spec.md"

    chosen_spec = living_spec_path or spec_path
    if chosen_spec:
        living_display = _normalize_ticket_display_path(chosen_spec, root_dir=base_root)
    else:
        extracted_spec = None
        cand_spec = feature_dir / "spec.md" if feature_dir else None
        if cand_spec and cand_spec.is_file():
            try:
                spec_txt = cand_spec.read_text(encoding="utf-8")
                m_target = re.search(
                    r"^\s*(?:[-*]\s*)?(?:\*\*)?Target\s+(?:Living\s+)?Spec(?:ification)?(?:\*\*)?:?\s*([^\r\n]+)",
                    spec_txt,
                    re.MULTILINE | re.IGNORECASE,
                )
                if not m_target:
                    m_target = re.search(
                        r"^\s*(?:[-*]\s*)?(?:\*\*)?Living\s+Spec(?:ification)?(?:\*\*)?:?\s*([^\r\n]+)",
                        spec_txt,
                        re.MULTILINE | re.IGNORECASE,
                    )
                if m_target:
                    raw_spec = m_target.group(1).strip()
                    m_link = re.search(r"\[`?([^`\]]+)`?\]\([^\)]+\)", raw_spec)
                    if m_link:
                        raw_spec = m_link.group(1).strip()
                    raw_spec = raw_spec.strip("`*\"' \t")
                    if raw_spec:
                        extracted_spec = raw_spec
                else:
                    m_dom = re.search(
                        r"^\s*(?:[-*]\s*)?(?:\*\*)?Target\s+Domain(?:\*\*)?:?\s*([^\r\n]+)",
                        spec_txt,
                        re.MULTILINE | re.IGNORECASE,
                    )
                    if m_dom:
                        dom = m_dom.group(1).strip().strip("`*\"' \t")
                        if dom:
                            extracted_spec = f"docs/specs/{dom}/spec.md"
            except Exception:
                pass
        if extracted_spec:
            living_display = extracted_spec
        else:
            living_display = f"docs/specs/{feature_slug}/spec.md"

    diff_val = git_diff_range or diff_range or "main...HEAD"
    if not diff_val or diff_val.startswith("-") or not re.match(r"^[A-Za-z0-9_./@~^{}-]+$", diff_val):
        diff_val = "main...HEAD"

    # [ERR-EVAL-03] Detached HEAD and missing base branch diff protection
    diff_state = validate_git_diff_state(
        diff_range=diff_val,
        root_dir=base_root,
        feature_dir=feature_dir,
        raise_on_error=(raise_on_error or raise_on_rejection),
    )
    if not diff_state.valid:
        if raise_on_error or raise_on_rejection:
            raise EvaluationBriefGitError(diff_state.diagnostic)
        return diff_state.diagnostic

    if canonical_type == "security-audit":
        cmd_name = "security-audit --fix"
    elif canonical_type == "pr":
        cmd_name = "pr"
    else:
        cmd_name = "code-review --fix"
    return f"````bash\n/{cmd_name}\n````"


def update_task_graph_evaluation_status(
    task_graph_path: Path | str,
    eval_type: str,
    status: str = "⚠️ escalated-to-human",
    *,
    raise_on_error: bool = False,
) -> bool:
    """
    [ERR-EVAL-01] Updates the status of the specified evaluation phase (/code-review or /security-audit)
    in .scratch/<feature-slug>/task-graph.md. Atomically replaces matching lifecycle or evaluation table rows
    while preserving ticket status and progress tables without corruption.
    """
    path = Path(task_graph_path).resolve()
    if not path.is_file():
        if raise_on_error:
            raise FileNotFoundError(f"Task graph file not found: {path}")
        return False

    clean_type = str(eval_type or "").strip().strip("/").strip().lower().replace("_", "-")
    is_security = "security" in clean_type or "audit" in clean_type

    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        if raise_on_error:
            raise
        return False

    newline = "\r\n" if "\r\n" in content else "\n"
    lines = content.splitlines()
    updated_lines: list[str] = []
    updated = False

    normalized_status = status.strip()
    if "escalated" in normalized_status.lower() and "⚠️" not in normalized_status:
        normalized_status = f"⚠️ {normalized_status}"

    current_section = ""
    status_col_idx = -1
    phase_col_idx = -1

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            current_section = stripped.lower()

        if stripped.startswith("|"):
            if re.match(r"^\|(?:\s*:?-+:?\s*\|)+$", stripped):
                updated_lines.append(line)
                continue

            cols = line.split("|")
            lower_cols = [c.strip().lower() for c in cols]

            if any("status" in c for c in lower_cols) and any(k in c for c in lower_cols for k in ("phase", "stage", "evaluation", "#")):
                status_col_idx = -1
                phase_col_idx = -1
                for i, col in enumerate(cols):
                    c_clean = col.strip().lower()
                    if "status" in c_clean:
                        status_col_idx = i
                    elif any(k in c_clean for k in ("phase", "stage", "evaluation")):
                        phase_col_idx = i
                updated_lines.append(line)
                continue

            is_ticket_table = "ticket" in current_section or "progress" in current_section or any("ticket" in c for c in lower_cols)
            row_matches = False

            if not updated and not is_ticket_table:
                if phase_col_idx > 0 and phase_col_idx < len(cols):
                    phase_text = cols[phase_col_idx].strip().lower()
                    if is_security:
                        row_matches = any(k in phase_text for k in ("security", "audit"))
                    else:
                        row_matches = any(k in phase_text for k in ("code review", "code-review")) and not any(k in phase_text for k in ("security", "audit"))

                if not row_matches and len(cols) >= 3:
                    col1_val = cols[1].strip()
                    if is_security and col1_val in ("6", "06"):
                        row_matches = True
                    elif not is_security and col1_val in ("5", "05"):
                        row_matches = True

            effective_status_idx = status_col_idx if status_col_idx > 0 else 2
            if row_matches and len(cols) > effective_status_idx:
                cols[effective_status_idx] = f" {normalized_status} "
                line = "|".join(cols)
                updated = True

            updated_lines.append(line)
        else:
            status_col_idx = -1
            phase_col_idx = -1
            updated_lines.append(line)

    if not updated:
        display_phase = "Semantic Security Audit" if is_security else "Two-Axis Code Review"
        eval_section = (
            f"\n## Evaluation Status\n"
            f"| Phase | Status |\n"
            f"| :--- | :--- |\n"
            f"| {display_phase} | {normalized_status} |\n"
        )
        updated_lines.append(eval_section)
        updated = True

    joined_text = newline.join(updated_lines)
    if not joined_text.endswith(newline):
        joined_text += newline

    return _atomic_write_file(path, joined_text, raise_on_error=raise_on_error)


def evaluate_circuit_breaker(
    eval_type_or_count: str | int | dict[str, Any] = "code-review",
    count_or_eval_type: int | str | None = None,
    *,
    eval_type: str | None = None,
    fix_loop_count: int | str | None = None,
    fixLoopCount: int | str | None = None,
    verdict: str | None = None,
    task_graph_path: Path | str | None = None,
    feature_dir: Path | str | None = None,
    tracker_dir_or_feature_dir: Path | str | None = None,
    root_dir: Path | str | None = None,
    findings: Sequence[str] | str | None = None,
    threshold: int | str = 2,
    sync_task_graph: bool = True,
    validator: ManifestValidator | None = None,
) -> CircuitBreakerResult:
    """
    [ERR-EVAL-01] Evaluates whether an automated evaluation session (/code-review or /security-audit)
    has exceeded the maximum remediation attempt threshold (fixLoopCount >= 2).
    When triggered, halts automated remediation, logs ESCALATION_CIRCUIT_BREAKER_TRIGGERED,
    marks status as '⚠️ escalated-to-human' in task-graph.md, and directs the operator to manual resolution in Command Center.
    """
    actual_count: int = 0
    actual_eval_type: str = "code-review"
    actual_verdict: str | None = verdict

    if isinstance(eval_type_or_count, dict):
        if "fixLoopCount" in eval_type_or_count:
            fixLoopCount = eval_type_or_count.get("fixLoopCount")
        elif "fix_loop_count" in eval_type_or_count:
            fix_loop_count = eval_type_or_count.get("fix_loop_count")
        if "activePhase" in eval_type_or_count:
            eval_type = eval_type_or_count.get("activePhase")
        elif "phase" in eval_type_or_count:
            eval_type = eval_type_or_count.get("phase")
        if "verdict" in eval_type_or_count and actual_verdict is None:
            actual_verdict = str(eval_type_or_count.get("verdict"))
    elif isinstance(eval_type_or_count, int):
        actual_count = eval_type_or_count
        if isinstance(count_or_eval_type, str):
            if count_or_eval_type.strip().isdigit():
                actual_count = int(count_or_eval_type.strip())
            else:
                actual_eval_type = count_or_eval_type
    elif isinstance(eval_type_or_count, str):
        s_val = eval_type_or_count.strip()
        if s_val.isdigit():
            actual_count = int(s_val)
            if isinstance(count_or_eval_type, str):
                actual_eval_type = count_or_eval_type
        else:
            actual_eval_type = s_val
            if isinstance(count_or_eval_type, (int, float)):
                actual_count = int(count_or_eval_type)
            elif isinstance(count_or_eval_type, str) and count_or_eval_type.strip().isdigit():
                actual_count = int(count_or_eval_type.strip())

    if eval_type is not None:
        actual_eval_type = str(eval_type)
    if fixLoopCount is not None:
        try:
            actual_count = int(fixLoopCount)
        except (ValueError, TypeError):
            actual_count = 0
    if fix_loop_count is not None:
        try:
            actual_count = int(fix_loop_count)
        except (ValueError, TypeError):
            actual_count = 0

    actual_count = max(0, actual_count)

    try:
        eff_threshold = max(1, int(threshold))
    except (ValueError, TypeError):
        eff_threshold = 2

    clean_type = str(actual_eval_type or "").strip().lstrip("/").lower().replace("_", "-")
    is_security = "security" in clean_type or "audit" in clean_type
    norm_type = "security-audit" if is_security else "code-review"
    display_name = "Security Audit" if is_security else "Code Review"

    findings_persist = True
    if actual_verdict is not None and str(actual_verdict).strip().upper() == "PASS":
        findings_persist = False

    triggered = (actual_count >= eff_threshold) and findings_persist
    base_root = Path(root_dir).resolve() if root_dir else None

    target_tg: Path | None = None
    if task_graph_path:
        p = Path(task_graph_path)
        if not p.is_absolute() and base_root and (base_root / p).exists():
            target_tg = base_root / p
        else:
            target_tg = p
    else:
        target_f = feature_dir or tracker_dir_or_feature_dir
        if target_f:
            fp = Path(target_f)
            if not fp.is_absolute() and base_root and (base_root / fp).exists():
                fp = base_root / fp
            if fp.is_file() and fp.name.lower() in ("task-graph.md", "task-graph.markdown"):
                target_tg = fp
            elif fp.is_dir():
                cand = fp / "task-graph.md"
                if cand.is_file():
                    target_tg = cand
                elif fp.parent.name == "issues" and (fp.parent.parent / "task-graph.md").is_file():
                    target_tg = fp.parent.parent / "task-graph.md"
                else:
                    target_tg = fp / "task-graph.md"
        elif base_root and (base_root / ".scratch").is_dir():
            for d in (base_root / ".scratch").iterdir():
                if d.is_dir() and (d / "task-graph.md").is_file():
                    target_tg = d / "task-graph.md"
                    break

    formatted_findings = ""
    if findings:
        if isinstance(findings, (list, tuple, set)):
            formatted_findings = "; ".join(str(f).strip() for f in findings if str(f).strip())
        elif isinstance(findings, dict):
            formatted_findings = "; ".join(f"{k}: {v}".strip() for k, v in findings.items() if str(v).strip())
        else:
            formatted_findings = str(findings).strip()

    tg_updated = False
    if triggered:
        status = "⚠️ escalated-to-human"
        findings_suffix = f"\nFindings: {formatted_findings}" if formatted_findings else ""
        log_msg = (
            f"[ERR-REMED-02] [ERR-EVAL-01] ESCALATION_CIRCUIT_BREAKER_TRIGGERED: Evaluation findings persisted after "
            f"{actual_count} successive remediation attempts (fixLoopCount={actual_count} >= {eff_threshold}) "
            f"for {display_name}.{findings_suffix}"
        )
        directive = (
            f"Automated remediation halted by circuit breaker for {display_name}. "
            f"Defect escalated to human operator. Return to Command Center for manual architectural intervention."
        )
        diagnostic = (
            f"{log_msg}\n"
            f"Status marked as '{status}' in task-graph.md.\n"
            f"Directive: {directive}"
        )
        if sync_task_graph and target_tg and target_tg.is_file():
            tg_updated = update_task_graph_evaluation_status(
                target_tg,
                norm_type,
                status=status,
                raise_on_error=False,
            )
    else:
        status = "in-progress"
        log_msg = ""
        directive = f"Automated remediation in progress (attempt {actual_count}/{eff_threshold})."
        diagnostic = f"Circuit breaker inactive: fixLoopCount={actual_count} < threshold={eff_threshold}."

    return CircuitBreakerResult(
        triggered=triggered,
        eval_type=norm_type,
        fix_loop_count=actual_count,
        threshold=eff_threshold,
        status=status,
        log_message=log_msg,
        directive=directive,
        task_graph_path=target_tg,
        task_graph_updated=tg_updated,
        diagnostic=diagnostic,
    )


def evaluate_evaluation_circuit_breaker(
    eval_type_or_count: str | int | dict[str, Any] = "code-review",
    count_or_eval_type: int | str | None = None,
    *,
    eval_type: str | None = None,
    phase: str | None = None,
    fix_loop_count: int | str | None = None,
    fixLoopCount: int | str | None = None,
    verdict: str | None = None,
    task_graph_path: Path | str | None = None,
    feature_dir: Path | str | None = None,
    scratch_dir: Path | str | None = None,
    tracker_dir_or_feature_dir: Path | str | None = None,
    root_dir: Path | str | None = None,
    findings: Sequence[str] | str | None = None,
    threshold: int | str = 2,
    sync_task_graph: bool = True,
    validator: ManifestValidator | None = None,
    raw_report: str = "",
) -> CircuitBreakerResult:
    """
    [ERR-REMED-02] [ERR-EVAL-01] Evaluates whether an automated evaluation session (/code-review or /security-audit)
    has exceeded the maximum remediation attempt threshold (fixLoopCount >= 2).
    When triggered, halts automated remediation, logs ESCALATION_CIRCUIT_BREAKER_TRIGGERED,
    marks status as '⚠️ escalated-to-human' in task-graph.md, and directs the operator to return to Command Center
    for manual architectural intervention.
    """
    effective_eval_type = eval_type or phase
    effective_feature_dir = feature_dir or scratch_dir
    return evaluate_circuit_breaker(
        eval_type_or_count,
        count_or_eval_type,
        eval_type=effective_eval_type,
        fix_loop_count=fix_loop_count,
        fixLoopCount=fixLoopCount,
        verdict=verdict,
        task_graph_path=task_graph_path,
        feature_dir=effective_feature_dir,
        tracker_dir_or_feature_dir=tracker_dir_or_feature_dir,
        root_dir=root_dir,
        findings=findings,
        threshold=threshold,
        sync_task_graph=sync_task_graph,
        validator=validator,
    )


def _resolve_git_lock_file(repo_dir: Path | str) -> Path | None:
    """
    [ERR-REMED-03] Resolves the path to an active .git/index.lock file if present in repo_dir or parent worktree gitdir.
    """
    root = Path(repo_dir).resolve()
    for curr in [root, root.parent, root.parent.parent]:
        git_entry = curr / ".git"
        if git_entry.is_file():
            try:
                gd_text = git_entry.read_text(encoding="utf-8").strip()
                if gd_text.startswith("gitdir:"):
                    gd_raw = gd_text.split(":", 1)[1].strip()
                    gd_path = Path(gd_raw)
                    if not gd_path.is_absolute():
                        gd_path = (curr / gd_path).resolve()
                    cand = gd_path / "index.lock"
                    if cand.is_file():
                        return cand
            except Exception:
                pass
        elif git_entry.is_dir():
            cand = git_entry / "index.lock"
            if cand.is_file():
                return cand
    return None


def evaluate_remediation_commit_resiliency(
    repo_dir: Path | str,
    *,
    lock_file: Path | str | None = None,
    exc: BaseException | None = None,
    phase: str = "code-review",
) -> RemediationCommitResult:
    """
    [ERR-REMED-03] Evaluates Git repository resiliency before or after attempting a remediation commit.
    Detects active .git/index.lock contention or filesystem IO errors without forcing or corrupting repository state.
    Logs REMEDIATION_COMMIT_FAILED, prevents false green PASS marking, and directs the operator to manual workspace resolution.
    """
    root = Path(repo_dir).resolve()
    clean_phase = str(phase or "code-review").strip().lower()

    if exc is not None:
        sys_err_str = str(exc)
        log_msg = (
            f"🚨 [ERR-REMED-03] REMEDIATION_COMMIT_FAILED: Filesystem IO failure encountered during remediation commit "
            f"for '{clean_phase}': {sys_err_str}.\n"
            f"Repository state preserved without corruption.\n"
            f"Phase '{clean_phase}' will NOT be marked as PASS."
        )
        directive = "Please resolve the Git lock or IO condition manually in the workspace."
        return RemediationCommitResult(
            ok=False,
            exit_code=1,
            commit_sha="",
            error=sys_err_str,
            log_message=log_msg,
            directive=directive,
            is_io_failure=True,
        )

    active_lock = Path(lock_file).resolve() if lock_file else _resolve_git_lock_file(root)
    if active_lock and active_lock.exists():
        log_msg = (
            f"🚨 [ERR-REMED-03] REMEDIATION_COMMIT_FAILED: Active Git lock (.git/index.lock) detected at '{active_lock}' "
            f"during remediation commit for '{clean_phase}'.\n"
            f"Repository state preserved without forcing lock removal.\n"
            f"Phase '{clean_phase}' will NOT be marked as PASS."
        )
        directive = "Please resolve the Git lock (.git/index.lock) or IO condition manually in the workspace."
        return RemediationCommitResult(
            ok=False,
            exit_code=1,
            commit_sha="",
            error=f"Active Git lock detected at '{active_lock}'",
            log_message=log_msg,
            directive=directive,
            lock_file=active_lock,
            is_lock_failure=True,
        )

    return RemediationCommitResult(
        ok=True,
        exit_code=0,
        commit_sha="",
        log_message=f"Git repository is clean and ready for remediation commit in '{clean_phase}'.",
        directive="",
    )


def execute_remediation_commit(
    repo_dir: Path | str,
    commit_message: str = "fix(review): remediate review findings",
    *,
    files: Sequence[str] | None = None,
    phase: str = "code-review",
    feature_dir: Path | str | None = None,
    task_graph_path: Path | str | None = None,
    raise_on_error: bool = False,
) -> RemediationCommitResult:
    """
    [ERR-REMED-03] Executes an atomic remediation git commit on HEAD with resiliency against
    Git lock contention (.git/index.lock) and filesystem IO failures.
    Catches errors gracefully, never forces lock deletion or corrupts state, logs REMEDIATION_COMMIT_FAILED,
    and directs the operator to manual workspace resolution.
    """
    root = Path(repo_dir).resolve()

    lock_check = evaluate_remediation_commit_resiliency(root, phase=phase)
    if not lock_check.ok:
        if raise_on_error:
            raise RuntimeError(lock_check.log_message)
        return lock_check

    try:
        if files:
            add_cmd = ["git", "add"] + list(files)
            add_proc = subprocess.run(add_cmd, cwd=str(root), capture_output=True, text=True, timeout=10)
            if add_proc.returncode != 0:
                lock_file = _resolve_git_lock_file(root)
                if lock_file and lock_file.exists():
                    return evaluate_remediation_commit_resiliency(root, lock_file=lock_file, phase=phase)
                log_msg = (
                    f"🚨 [ERR-REMED-03] REMEDIATION_COMMIT_FAILED: git add failed: {add_proc.stderr.strip()}\n"
                    f"Phase '{phase}' will NOT be marked as PASS."
                )
                return RemediationCommitResult(
                    ok=False,
                    exit_code=1,
                    error=add_proc.stderr.strip(),
                    log_message=log_msg,
                    directive="Please resolve the Git lock or IO condition manually in the workspace.",
                )

        commit_proc = subprocess.run(
            ["git", "commit", "-m", commit_message],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=15,
        )
        if commit_proc.returncode != 0:
            lock_file = _resolve_git_lock_file(root)
            if (lock_file and lock_file.exists()) or "index.lock" in commit_proc.stderr:
                return evaluate_remediation_commit_resiliency(root, lock_file=lock_file, phase=phase)
            log_msg = (
                f"🚨 [ERR-REMED-03] REMEDIATION_COMMIT_FAILED: git commit failed: {commit_proc.stderr.strip()}\n"
                f"Phase '{phase}' will NOT be marked as PASS."
            )
            return RemediationCommitResult(
                ok=False,
                exit_code=commit_proc.returncode,
                error=commit_proc.stderr.strip(),
                log_message=log_msg,
                directive="Please resolve the Git lock or IO condition manually in the workspace.",
            )

        sha_proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=5,
        )
        commit_sha = sha_proc.stdout.strip()[:7] if sha_proc.returncode == 0 else ""
        return RemediationCommitResult(
            ok=True,
            exit_code=0,
            commit_sha=commit_sha,
            log_message=f"Remediation commit created successfully: {commit_sha}",
            directive="",
        )
    except (subprocess.SubprocessError, OSError) as exc:
        if raise_on_error:
            raise
        return evaluate_remediation_commit_resiliency(root, exc=exc, phase=phase)


def _validate_phase_prerequisites(
    phase: str,
    feature_dir: Path | str | None = None,
    task_graph_path: Path | str | None = None,
    root_dir: Path | str | None = None,
    tracker_dir: Path | str | None = None,
    validator: ManifestValidator | None = None,
) -> PhasePrerequisiteResult:
    """
    [ERR-EXCL-02] Validates phase boundary preconditions for downstream evaluation phases
    (/code-review, /security-audit, /pr).
    Detects the absence of prerequisite phase clearance in on-disk state (task-graph.md and phase artifacts),
    halting execution with an actionable diagnostic notice directing the operator to return to Command Center.
    """
    clean_phase = str(phase or "").strip().lstrip("/").lower().replace("_", "-")
    base_root = Path(root_dir).resolve() if root_dir else None

    target_tg: Path | None = None
    f_dir: Path | None = None

    if task_graph_path:
        fp = Path(task_graph_path)
        if not fp.is_absolute() and base_root and (base_root / fp).exists():
            fp = base_root / fp
        if fp.is_file():
            target_tg = fp
            f_dir = fp.parent
    elif feature_dir:
        fp = Path(feature_dir)
        if not fp.is_absolute() and base_root and (base_root / fp).exists():
            fp = base_root / fp
        if fp.is_file() and fp.name.lower() in ("task-graph.md", "task-graph.markdown"):
            target_tg = fp
            f_dir = fp.parent
        elif fp.is_dir():
            cand = fp / "task-graph.md"
            if cand.is_file():
                target_tg = cand
                f_dir = fp
            elif fp.name == "issues" and (fp.parent / "task-graph.md").is_file():
                target_tg = fp.parent / "task-graph.md"
                f_dir = fp.parent
            elif fp.parent.name == "issues" and (fp.parent.parent / "task-graph.md").is_file():
                target_tg = fp.parent.parent / "task-graph.md"
                f_dir = fp.parent.parent
            elif (fp / ".scratch").is_dir():
                scratch_dir = fp / ".scratch"
                active_br = validator._detect_active_branch(fp) if validator else None
                matched_dir = None
                if active_br:
                    clean_br = active_br.replace("feat/", "").replace("fix/", "").strip()
                    if (scratch_dir / clean_br / "task-graph.md").is_file():
                        matched_dir = scratch_dir / clean_br
                    else:
                        for d in scratch_dir.iterdir():
                            if d.is_dir() and d.name in active_br and (d / "task-graph.md").is_file():
                                matched_dir = d
                                break
                if not matched_dir:
                    for d in scratch_dir.iterdir():
                        if d.is_dir() and (d / "task-graph.md").is_file():
                            matched_dir = d
                            break
                if matched_dir:
                    target_tg = matched_dir / "task-graph.md"
                    f_dir = matched_dir
                else:
                    target_tg = fp / "task-graph.md"
                    f_dir = fp
            else:
                target_tg = fp / "task-graph.md"
                f_dir = fp
    elif tracker_dir:
        t_dir = Path(tracker_dir)
        if not t_dir.is_absolute() and base_root and (base_root / t_dir).exists():
            t_dir = base_root / t_dir
        f_dir = t_dir.parent if t_dir.name == "issues" else t_dir
        target_tg = f_dir / "task-graph.md"
    elif base_root and (base_root / ".scratch").is_dir():
        for d in (base_root / ".scratch").iterdir():
            if d.is_dir() and (d / "task-graph.md").is_file():
                target_tg = d / "task-graph.md"
                f_dir = d
                break
        if not target_tg:
            cands = [d for d in (base_root / ".scratch").iterdir() if d.is_dir() and not d.name.startswith(".")]
            f_dir = cands[0] if cands else base_root / ".scratch" / "feature"
            target_tg = f_dir / "task-graph.md"
    else:
        cwd = Path.cwd().resolve()
        if (cwd / ".scratch").is_dir():
            for d in (cwd / ".scratch").iterdir():
                if d.is_dir() and (d / "task-graph.md").is_file():
                    target_tg = d / "task-graph.md"
                    f_dir = d
                    break
            if not target_tg:
                cands = [d for d in (cwd / ".scratch").iterdir() if d.is_dir() and not d.name.startswith(".")]
                f_dir = cands[0] if cands else cwd / ".scratch" / "feature"
                target_tg = f_dir / "task-graph.md"
        else:
            f_dir = cwd
            target_tg = f_dir / "task-graph.md"

    if not target_tg or not target_tg.is_file():
        diagnostic = (
            f"❌ [ERR-EXCL-02] Prerequisite phase clearance missing for '/{clean_phase}': "
            f"task-graph.md not found in feature directory '{f_dir}'. "
            f"Out-of-sequence invocation blocked. Return to Command Center to obtain a contextual evaluation brief."
        )
        return PhasePrerequisiteResult(
            False,
            diagnostic=diagnostic,
            phase=clean_phase,
            missing_prerequisites=["task-graph.md"],
            target_failing=target_tg,
        )

    try:
        tg_text = target_tg.read_text(encoding="utf-8")
    except Exception as exc:
        diagnostic = (
            f"❌ [ERR-EXCL-02] Prerequisite phase clearance missing for '/{clean_phase}': "
            f"unable to read task-graph.md ({exc}). "
            f"Out-of-sequence invocation blocked. Return to Command Center to obtain a contextual evaluation brief."
        )
        return PhasePrerequisiteResult(
            False,
            diagnostic=diagnostic,
            phase=clean_phase,
            missing_prerequisites=["task-graph.md (unreadable)"],
            target_failing=target_tg,
        )

    lifecycle_statuses: dict[int, str] = {}
    for line in tg_text.splitlines():
        line_clean = line.strip()
        if line_clean.startswith("|") and line_clean.endswith("|"):
            cols = [c.strip() for c in line_clean.split("|")[1:-1]]
            if len(cols) >= 3 and cols[0].isdigit():
                phase_num = int(cols[0])
                phase_status = cols[2]
                lifecycle_statuses[phase_num] = phase_status

    has_unresolved_tickets = False
    in_status_table = False
    for line in tg_text.splitlines():
        line_str = line.strip()
        if "Status Progress Table" in line_str or "Progress & Status Table" in line_str:
            in_status_table = True
            continue
        if in_status_table:
            if line_str.startswith("#"):
                in_status_table = False
                continue
            if line_str.startswith("|") and not line_str.startswith("| :-") and not line_str.startswith("| Ticket") and not line_str.startswith("| #"):
                cols = [c.strip() for c in line_str.split("|")[1:-1]]
                if len(cols) >= 4:
                    t_status = cols[3].lower()
                    if "resolved" not in t_status and "✅" not in t_status:
                        has_unresolved_tickets = True

    p4_status = lifecycle_statuses.get(4, "").lower()
    if 4 in lifecycle_statuses:
        phase4_complete = ("completed" in p4_status or "✅" in p4_status) and not has_unresolved_tickets
    else:
        has_stage_2 = "stage ii" in tg_text.lower() or "implementation swarm" in tg_text.lower()
        phase4_complete = has_stage_2 and not has_unresolved_tickets

    p5_status = lifecycle_statuses.get(5, "").lower()
    cr_artifact = f_dir / "code-review.md" if f_dir else None
    cr_has_pass = False
    if cr_artifact and cr_artifact.is_file():
        try:
            cr_content = cr_artifact.read_text(encoding="utf-8").upper()
            if "PASS" in cr_content and "FAIL" not in cr_content:
                cr_has_pass = True
        except Exception:
            pass
    phase5_complete = (
        ("completed" in p5_status or "✅" in p5_status or "bypassed" in p5_status or "⏭️" in p5_status)
        or cr_has_pass
    )

    p6_status = lifecycle_statuses.get(6, "").lower()
    sa_artifact = f_dir / "security-audit.md" if f_dir else None
    sa_has_pass = False
    if sa_artifact and sa_artifact.is_file():
        try:
            sa_content = sa_artifact.read_text(encoding="utf-8").upper()
            if "PASS" in sa_content and "FAIL" not in sa_content:
                sa_has_pass = True
        except Exception:
            pass
    phase6_complete = (
        ("completed" in p6_status or "✅" in p6_status or "bypassed" in p6_status or "⏭️" in p6_status)
        or sa_has_pass
    )

    missing: list[str] = []
    is_security_audit = any(k in clean_phase for k in ("security", "audit"))
    is_pr = clean_phase in ("pr", "pull-request", "ship")
    is_code_review = any(k in clean_phase for k in ("code", "review"))
    is_retro = any(k in clean_phase for k in ("retro", "retrospective"))

    if is_code_review:
        if not phase4_complete:
            missing.append("Phase 4: Implementation Swarm")
    elif is_security_audit:
        if not phase4_complete:
            missing.append("Phase 4: Implementation Swarm")
        if not phase5_complete:
            missing.append("Phase 5: Two-Axis Code Review")
    elif is_pr:
        if not phase4_complete:
            missing.append("Phase 4: Implementation Swarm")
        if not phase5_complete:
            missing.append("Phase 5: Two-Axis Code Review")
        if not phase6_complete:
            missing.append("Phase 6: Semantic Security Audit")
    elif is_retro:
        if not phase4_complete:
            missing.append("Phase 4: Implementation Swarm")
        if not phase5_complete:
            missing.append("Phase 5: Two-Axis Code Review")
        if not phase6_complete:
            missing.append("Phase 6: Semantic Security Audit")

    if missing:
        missing_str = ", ".join(missing)
        diagnostic = (
            f"❌ [ERR-EXCL-02] Prerequisite phase clearance missing for '/{clean_phase}': "
            f"Prior phase completion ({missing_str}) not recorded in task graph. "
            f"Out-of-sequence invocation blocked. Return to Command Center to obtain a contextual evaluation brief."
        )
        return PhasePrerequisiteResult(
            False,
            diagnostic=diagnostic,
            phase=clean_phase,
            missing_prerequisites=missing,
            target_failing=target_tg,
        )

    diagnostic = f"✅ Prerequisite phase clearance verified for '/{clean_phase}'."
    return PhasePrerequisiteResult(
        True,
        diagnostic=diagnostic,
        phase=clean_phase,
        missing_prerequisites=[],
        target_failing=target_tg,
    )


validate_phase_prerequisites = _validate_phase_prerequisites


def _validate_ticket_concurrency(
    ticket_ref: str,
    feature_dir: Path | str | None = None,
    tracker_dir: Path | str | None = None,
    root_dir: Path | str | None = None,
    validator: ManifestValidator | None = None,
) -> TicketConcurrencyResult:
    """
    [ERR-EXCL-03] Validates ticket concurrency protection for worker sessions (/implement <ticket>).
    Rejects execution if ticket frontmatter status is already 'in-progress' (claimed by another session)
    or 'resolved', directing the operator to return to Command Center to refresh the active frontier.
    """
    if not ticket_ref or not str(ticket_ref).strip():
        return TicketConcurrencyResult(
            False,
            diagnostic="Error: ticket-id must be a non-empty string.",
            ticket_id="",
            is_conflict=False,
        )

    clean_ref = str(ticket_ref).strip()
    v = validator or ManifestValidator()

    if tracker_dir and str(tracker_dir).strip():
        search_dir = Path(tracker_dir).resolve()
    elif feature_dir and str(feature_dir).strip():
        f_path = Path(feature_dir).resolve()
        search_dir = f_path / "issues" if (f_path / "issues").is_dir() else f_path
    elif root_dir and str(root_dir).strip():
        search_dir = Path(root_dir).resolve()
    else:
        cwd = Path.cwd().resolve()
        if (cwd / ".scratch").is_dir() or (cwd / "issues").is_dir() or cwd.name == "issues":
            search_dir = cwd
        elif (REPO_ROOT / ".scratch").is_dir() or (REPO_ROOT / "issues").is_dir():
            search_dir = REPO_ROOT
        else:
            search_dir = cwd

    if not search_dir.exists():
        return TicketConcurrencyResult(
            False,
            diagnostic=f"Error: Search directory does not exist: {search_dir}",
            ticket_id=clean_ref,
            is_conflict=False,
        )

    try:
        ticket_path = v.resolve_ticket_shorthand(search_dir, clean_ref)
    except FileNotFoundError as fnf_err:
        return TicketConcurrencyResult(
            False,
            diagnostic=f"Error: Ticket '{clean_ref}' not found in {search_dir}: {fnf_err}",
            ticket_id=clean_ref,
            is_conflict=False,
        )
    except Exception as exc:
        return TicketConcurrencyResult(
            False,
            diagnostic=f"Error resolving ticket shorthand '{clean_ref}': {exc}",
            ticket_id=clean_ref,
            is_conflict=False,
        )

    m = re.match(r"^(?:ticket-|issue-|#)?([0-9]{1,3})", ticket_path.stem)
    canonical_id = f"{int(m.group(1)):02d}" if m else ticket_path.stem

    try:
        raw_ticket_text = ticket_path.read_text(encoding="utf-8")
        meta = v._parse_ticket_markdown(raw_ticket_text, ticket_path.stem)
        existing_status = meta.get("Status", meta.get("status", "")).strip()
    except Exception:
        existing_status = ""

    if v._is_status_in_progress(existing_status):
        diagnostic = (
            f"⚠️ [ERR-EXCL-03] Concurrency Advisory: Ticket '{canonical_id}' is already 'in-progress' "
            f"(claimed by another session). Worker execution rejected. "
            f"Do not proceed with code modifications or test generation. "
            f"Return to the Command Center to refresh the active frontier, "
            f"or run './scripts/claim-ticket.sh {canonical_id} --unclaim' if prior session was interrupted."
        )
        return TicketConcurrencyResult(
            False,
            diagnostic=diagnostic,
            ticket_id=canonical_id,
            status=existing_status,
            ticket_path=ticket_path,
            is_conflict=True,
        )

    if v._is_status_resolved(existing_status):
        diagnostic = (
            f"⚠️ [ERR-EXCL-03] Concurrency Advisory: Ticket '{canonical_id}' is already 'resolved'. "
            f"Worker execution rejected. "
            f"Do not proceed with code modifications or test generation. "
            f"Return to the Command Center to refresh the active frontier."
        )
        return TicketConcurrencyResult(
            False,
            diagnostic=diagnostic,
            ticket_id=canonical_id,
            status=existing_status,
            ticket_path=ticket_path,
            is_conflict=True,
        )

    diagnostic = f"✅ Ticket '{canonical_id}' is available for implementation (status: '{existing_status}')."
    return TicketConcurrencyResult(
        True,
        diagnostic=diagnostic,
        ticket_id=canonical_id,
        status=existing_status,
        ticket_path=ticket_path,
        is_conflict=False,
    )


validate_ticket_concurrency = _validate_ticket_concurrency


def _migrate_clustered_subgraph_node(
    text: str,
    canonical_id: str,
    num_val: int | None = None,
    target_subgraph: str = "S_IN_PROGRESS",
    new_suffix: str = "(In Progress)",
    scanned_tickets: Sequence[dict[str, Any]] | None = None,
) -> tuple[str, bool]:
    """
    [CLAIM-01] [RES-01] Migrates a ticket node declaration between clustered Mermaid subgraphs
    (S_RESOLVED, S_IN_PROGRESS, S_FRONTIER, S_BLOCKED) and ensures empty subgraphs contain
    clean '%% None' placeholder comments.
    """
    mermaid_pattern = re.compile(r"(```mermaid[ \t]*\r?\n)(.*?)(\r?\n[ \t]*```)", re.DOTALL)
    m = mermaid_pattern.search(text)
    if not m:
        return text, False

    body = m.group(2)
    if not re.search(r"^[ \t]*subgraph\s+S_", body, re.MULTILINE):
        return text, False

    target_ids: list[str] = []
    if num_val is not None:
        target_ids.append(rf"T0*{num_val}")
        target_ids.append(rf"ticket-0*{num_val}")
        target_ids.append(rf"ticket_0*{num_val}")
        target_ids.append(rf"0*{num_val}")
    target_ids.append(rf"{re.escape(canonical_id)}")
    combined_prefix = "|".join(target_ids)
    node_decl_pattern = re.compile(
        rf'^[ \t]*((?:{combined_prefix})[a-zA-Z0-9_-]*)\s*\[(["\']?)(.*?)\2\][ \t]*$',
        re.IGNORECASE,
    )

    lines = body.splitlines()
    top_lines: list[str] = []
    subgraph_order: list[str] = []
    subgraph_headers: dict[str, str] = {}
    subgraph_nodes: dict[str, list[str]] = {}
    trailing_lines: list[str] = []

    current_sg: str | None = None
    seen_any_sg = False
    found_node_line: str | None = None
    matched_node_var: str | None = None

    for line in lines:
        stripped = line.strip()
        m_sg = re.match(r"^[ \t]*subgraph\s+([A-Za-z0-9_]+)(\[.*?\])?[ \t]*$", line)
        if m_sg:
            current_sg = m_sg.group(1)
            seen_any_sg = True
            if current_sg not in subgraph_order:
                subgraph_order.append(current_sg)
                subgraph_nodes[current_sg] = []
            subgraph_headers[current_sg] = line
            continue

        if re.match(r"^[ \t]*end\b", line) and current_sg:
            current_sg = None
            continue

        if current_sg is not None:
            if stripped.startswith("%% None") or (stripped.startswith("empty_") and '["(None)"]' in stripped):
                continue
            m_decl = node_decl_pattern.match(stripped)
            if m_decl:
                found_node_line = stripped
                matched_node_var = m_decl.group(1)
                continue
            if stripped:
                subgraph_nodes[current_sg].append(stripped)
        else:
            if not seen_any_sg:
                if stripped:
                    top_lines.append(line)
            else:
                m_decl = node_decl_pattern.match(stripped)
                if m_decl:
                    found_node_line = stripped
                    matched_node_var = m_decl.group(1)
                    continue
                if stripped:
                    if "~~~" in stripped and ("empty_" in stripped or any(x in stripped for x in ("empty_resolved", "empty_in_progress", "empty_frontier", "empty_blocked"))):
                        continue
                    trailing_lines.append(stripped)

    if found_node_line:
        m_decl = node_decl_pattern.match(found_node_line)
        raw_node_var = matched_node_var or (m_decl.group(1) if m_decl else "")
        if raw_node_var.isdigit():
            node_var = f"T{int(raw_node_var):02d}"
        else:
            node_var = raw_node_var
        raw_label = m_decl.group(3) if m_decl else ""
        clean_label = re.sub(
            r"\s*[\(\[](?:Frontier|Ready|In[- ]Progress|Resolved|Blocked)[\)\]]\s*$",
            "",
            raw_label,
            flags=re.IGNORECASE,
        ).strip()
    else:
        node_var = f"T{num_val:02d}" if num_val is not None else f"T{canonical_id}"
        ticket_title = ""
        if scanned_tickets:
            for st in scanned_tickets:
                st_id = str(st.get("id", "")).strip().lstrip("#")
                if st_id.isdigit():
                    st_id = f"{int(st_id):02d}"
                if st_id == canonical_id or (num_val is not None and st_id == f"{num_val:02d}"):
                    raw_title = str(st.get("title", ""))
                    if ":" in raw_title:
                        ticket_title = raw_title.split(":", 1)[1].strip()
                    else:
                        ticket_title = raw_title.strip()
                    break
        clean_label = f"{canonical_id}: {ticket_title}".strip() if ticket_title else canonical_id

    new_label = f"{clean_label} {new_suffix}".strip()
    new_node_decl = f'{node_var}["{new_label}"]'

    canonical_headers = {
        "S_RESOLVED": 'subgraph S_RESOLVED["✅ Completed & Verified"]',
        "S_IN_PROGRESS": 'subgraph S_IN_PROGRESS["🟡 In Progress"]',
        "S_FRONTIER": 'subgraph S_FRONTIER["🎯 Active Frontier (Ready to Start)"]',
        "S_BLOCKED": 'subgraph S_BLOCKED["⏳ Blocked (Waiting Dependencies)"]',
    }

    if target_subgraph not in subgraph_order:
        subgraph_order.append(target_subgraph)
        subgraph_nodes[target_subgraph] = []
        subgraph_headers[target_subgraph] = f"  {canonical_headers.get(target_subgraph, f'subgraph {target_subgraph}')}"

    subgraph_nodes[target_subgraph].append(new_node_decl)

    def _sort_key(node_str: str) -> tuple[int, str]:
        m_k = re.search(r'\b(?:T|ticket[-_]?)?([0-9]{1,4})\b', node_str, re.IGNORECASE)
        return (int(m_k.group(1)), node_str) if m_k else (9999, node_str)

    subgraph_nodes[target_subgraph].sort(key=_sort_key)

    if target_subgraph == "S_RESOLVED":
        resolved_ids: set[str] = {canonical_id}
        if num_val is not None:
            resolved_ids.add(f"{num_val:02d}")
            resolved_ids.add(str(num_val))

        for rn in subgraph_nodes.get("S_RESOLVED", []):
            m_r = re.search(r'\b(?:T|ticket[-_]?)?([0-9]{1,4})\b', rn, re.IGNORECASE)
            if m_r:
                r_num = int(m_r.group(1))
                resolved_ids.add(f"{r_num:02d}")
                resolved_ids.add(str(r_num))

        ticket_blockers: dict[str, list[str]] = {}
        if scanned_tickets:
            for st in scanned_tickets:
                st_id = str(st.get("id", "")).strip().lstrip("#")
                if st_id.isdigit():
                    norm_tid = f"{int(st_id):02d}"
                    raw_b = st.get("blockers") or st.get("blocked_by") or st.get("Blocked by")
                    b_list: list[str] = []
                    if raw_b is not None:
                        if isinstance(raw_b, list):
                            for b in raw_b:
                                b_s = str(b).strip().lstrip("#")
                                if b_s.isdigit():
                                    b_list.append(f"{int(b_s):02d}")
                        else:
                            for b_match in re.finditer(r"\b([0-9]{1,3})\b", str(raw_b)):
                                b_list.append(f"{int(b_match.group(1)):02d}")
                    ticket_blockers[norm_tid] = b_list

        table_row_pattern = re.compile(
            r"^[ \t]*\|[ \t]*([^|]+?)[ \t]*\|[ \t]*([^|]+?)[ \t]*\|[ \t]*([^|]+?)[ \t]*\|[ \t]*([^|]+?)[ \t]*\|",
            re.MULTILINE,
        )
        for match in table_row_pattern.finditer(text):
            c1 = match.group(1).strip()
            c3 = match.group(3).strip()
            c4 = match.group(4).strip()
            if re.match(r"^:?-+:?$", c1):
                continue
            c1_clean = re.sub(r"^\[(.*?)\](?:\(.*\))?$", r"\1", c1).strip("*`# \t")
            m_row_id = re.match(r"^(?:ticket-|issue-|#)?([0-9]{1,3})\b", c1_clean, re.IGNORECASE)
            if not m_row_id:
                continue
            row_num = int(m_row_id.group(1))
            norm_row_id = f"{row_num:02d}"

            if "resolved" in c4.lower():
                resolved_ids.add(norm_row_id)
                resolved_ids.add(str(row_num))

            if norm_row_id not in ticket_blockers:
                b_list_row: list[str] = []
                if not re.search(r"\bnone\b", c3, re.IGNORECASE):
                    for b_match in re.finditer(r"\b([0-9]{1,3})\b", c3):
                        b_list_row.append(f"{int(b_match.group(1)):02d}")
                ticket_blockers[norm_row_id] = b_list_row

        edge_pattern = re.compile(
            r"^[ \t]*(?:T|ticket[-_]?)?([0-9]{1,4})\s*(?:-->|-.->|==>)(?:\|[^|]*\|)?\s*(?:T|ticket[-_]?)?([0-9]{1,4})\b",
            re.IGNORECASE,
        )
        for tr in lines:
            m_edge = edge_pattern.search(tr.strip())
            if m_edge:
                src_tid = f"{int(m_edge.group(1)):02d}"
                dst_tid = f"{int(m_edge.group(2)):02d}"
                if dst_tid not in ticket_blockers:
                    ticket_blockers[dst_tid] = []
                if src_tid not in ticket_blockers[dst_tid]:
                    ticket_blockers[dst_tid].append(src_tid)

        if "S_FRONTIER" not in subgraph_order:
            subgraph_order.append("S_FRONTIER")
            subgraph_nodes["S_FRONTIER"] = []
            subgraph_headers["S_FRONTIER"] = f"  {canonical_headers['S_FRONTIER']}"

        remaining_blocked: list[str] = []
        for blk_decl in subgraph_nodes.get("S_BLOCKED", []):
            m_blk_tid = re.search(r'\b(?:T|ticket[-_]?)?([0-9]{1,4})\b', blk_decl, re.IGNORECASE)
            blk_tid = f"{int(m_blk_tid.group(1)):02d}" if m_blk_tid else ""
            blk_blockers = ticket_blockers.get(blk_tid, [])
            if blk_tid and blk_blockers and all(b in resolved_ids for b in blk_blockers):
                m_nd = re.match(r'^[ \t]*([A-Za-z0-9_-]+)\s*\[(["\']?)(.*?)\2\]', blk_decl)
                if m_nd:
                    blk_var = m_nd.group(1)
                    raw_blk_lbl = m_nd.group(3)
                    clean_blk_lbl = re.sub(
                        r"\s*[\(\[](?:Frontier|Ready|In[- ]Progress|Resolved|Blocked)[\)\]]\s*$",
                        "",
                        raw_blk_lbl,
                        flags=re.IGNORECASE,
                    ).strip()
                    new_blk_decl = f'{blk_var}["{clean_blk_lbl} (Frontier)"]'
                    subgraph_nodes["S_FRONTIER"].append(new_blk_decl)
                else:
                    subgraph_nodes["S_FRONTIER"].append(blk_decl)
            else:
                remaining_blocked.append(blk_decl)

        subgraph_nodes["S_BLOCKED"] = remaining_blocked
        subgraph_nodes["S_FRONTIER"].sort(key=_sort_key)
        subgraph_nodes["S_BLOCKED"].sort(key=_sort_key)

    canonical_sg_order = ["S_RESOLVED", "S_IN_PROGRESS", "S_FRONTIER", "S_BLOCKED"]
    for sg_key in canonical_sg_order:
        if sg_key in (target_subgraph, "S_RESOLVED", "S_FRONTIER") and sg_key not in subgraph_order:
            subgraph_order.append(sg_key)
            subgraph_nodes[sg_key] = []
            subgraph_headers[sg_key] = f"  {canonical_headers.get(sg_key, f'subgraph {sg_key}')}"

    subgraph_order.sort(key=lambda s: canonical_sg_order.index(s) if s in canonical_sg_order else 99)

    out_body_lines: list[str] = []
    if top_lines:
        out_body_lines.extend(top_lines)
    else:
        out_body_lines.append("graph TD")

    sg_empty_var_map = {
        "S_RESOLVED": "empty_resolved",
        "S_IN_PROGRESS": "empty_in_progress",
        "S_FRONTIER": "empty_frontier",
        "S_BLOCKED": "empty_blocked",
    }
    cluster_representatives: list[tuple[str, bool]] = []
    for sg in subgraph_order:
        hdr = subgraph_headers.get(sg, f"  subgraph {sg}").strip()
        out_body_lines.append(f"  {hdr}")
        nodes = subgraph_nodes.get(sg, [])
        empty_var = sg_empty_var_map.get(sg, f"empty_{sg.lower()}")
        if not nodes:
            out_body_lines.append("    %% None")
            out_body_lines.append(f'    {empty_var}["(None)"]')
            cluster_representatives.append((empty_var, True))
        else:
            for n in nodes:
                out_body_lines.append(f"    {n.strip()}")
            first_node_line = nodes[0].strip()
            m_var = re.match(r"^([A-Za-z0-9_-]+)", first_node_line)
            rep_var = m_var.group(1) if m_var else first_node_line.split("[", 1)[0].strip()
            cluster_representatives.append((rep_var, False))
        out_body_lines.append("  end")
        out_body_lines.append("")

    hierarchy_links: list[str] = []
    for i in range(len(cluster_representatives) - 1):
        curr_rep, curr_is_empty = cluster_representatives[i]
        next_rep, next_is_empty = cluster_representatives[i + 1]
        if curr_is_empty or next_is_empty:
            hierarchy_links.append(f"  {curr_rep} ~~~ {next_rep}")

    if trailing_lines:
        for tr in trailing_lines:
            out_body_lines.append(f"  {tr.strip()}")
    if hierarchy_links:
        for hl in hierarchy_links:
            out_body_lines.append(hl)

    new_body = "\n".join(out_body_lines).rstrip()
    if new_body == body.strip():
        return text, False

    new_text = text[:m.start(2)] + new_body + text[m.end(2):]
    return new_text, True


def update_task_graph_progress(
    task_graph_path: Path | str,
    ticket_ref: str,
    new_status: str = "✅ resolved",
    dag_complete: bool = False,
    commit_sha: str = "",
    validator: ManifestValidator | None = None,
    raise_on_error: bool = False,
) -> bool:
    """
    [RES-01] Refreshes the corresponding ticket entry in .scratch/<feature-slug>/task-graph.md
    with '✅ resolved' and the verified commit SHA, updates Mermaid DAG nodes from [Frontier]/(Frontier)
    to [Resolved]/(Resolved), and advances Layer 1 Phase 4 and Phase 5 lifecycle statuses.
    """
    path = Path(task_graph_path)
    if not path.is_file():
        return False

    raw_id = str(ticket_ref).strip()
    id_without_ext = raw_id[:-3].strip() if raw_id.lower().endswith(".md") else raw_id
    m = re.match(r"^(?:ticket-|issue-|#)?([0-9]{1,3})", id_without_ext, re.IGNORECASE)
    canonical_id = f"{int(m.group(1)):02d}" if m else id_without_ext
    num_val = int(m.group(1)) if m else None

    v = validator or ManifestValidator()
    issues_dir = path.parent / "issues"
    scanned_tickets: list[dict[str, Any]] = []
    if issues_dir.is_dir():
        try:
            scanned_tickets = v.scan_tickets_from_tracker(issues_dir)
        except Exception:
            scanned_tickets = []

    is_in_progress = "in-progress" in new_status.lower()
    is_unclaim = any(k in new_status.lower() for k in ("ready", "pending", "unclaim"))
    if scanned_tickets:
        total_tickets = len(scanned_tickets)
        resolved_count = 0
        for t in scanned_tickets:
            t_id = str(t.get("id", "")).strip().lstrip("#")
            if t_id.isdigit():
                t_id = f"{int(t_id):02d}"
            if not is_in_progress and not is_unclaim and (t_id == canonical_id or (num_val is not None and t_id == f"{num_val:02d}")):
                resolved_count += 1
            elif v._is_status_resolved(t.get("status", "")) and not (is_unclaim and (t_id == canonical_id or (num_val is not None and t_id == f"{num_val:02d}"))):
                resolved_count += 1
    else:
        tg_tickets = parse_task_graph_tickets(path)
        total_tickets = len(tg_tickets)
        resolved_count = 0
        for t in tg_tickets:
            t_id = t.get("id", "")
            if not is_in_progress and not is_unclaim and (t_id == canonical_id or (num_val is not None and t_id == f"{num_val:02d}")):
                resolved_count += 1
            elif v._is_status_resolved(t.get("status", "")) and not (is_unclaim and (t_id == canonical_id or (num_val is not None and t_id == f"{num_val:02d}"))):
                resolved_count += 1

    all_resolved = not is_in_progress and not is_unclaim and (dag_complete or (total_tickets > 0 and resolved_count >= total_tickets))

    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return False

    has_clustered_subgraphs = bool(
        re.search(r"^[ \t]*subgraph\s+S_", content, re.MULTILINE)
    )

    newline = "\r\n" if "\r\n" in content else "\n"
    lines = content.splitlines()
    updated_lines: list[str] = []
    in_lifecycle_table = False
    in_ticket_table = False
    ticket_table_header_found = False
    ticket_col_idx = 1
    status_col_idx = -2
    commit_col_idx: int | None = None
    table_updated = False
    graph_updated = False
    lifecycle_updated = False

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("##"):
            lower_heading = stripped.lower()
            if "lifecycle" in lower_heading or "stage i" in lower_heading or "stage iii" in lower_heading or "stage iv" in lower_heading:
                in_lifecycle_table = True
                in_ticket_table = False
            elif any(k in lower_heading for k in ("status", "progress", "table", "ticket", "stage ii")):
                in_ticket_table = True
                in_lifecycle_table = False
            else:
                in_lifecycle_table = False
                in_ticket_table = False

        if stripped.lower().startswith("*progress:"):
            icon = "✅" if all_resolved else "🟡"
            line = f"*Progress: {icon} {resolved_count} dari {total_tickets} tiket selesai*"
            lifecycle_updated = True

        if stripped.lower().startswith("> **current stage**:"):
            if all_resolved:
                line = f"> **Current Stage**: Stage III — Verification & Delivery ({resolved_count}/{total_tickets} resolved)"
            else:
                line = f"> **Current Stage**: Stage II — Implementation Swarm ({resolved_count}/{total_tickets} resolved)"
            lifecycle_updated = True

        if stripped.startswith("|"):
            if re.match(r"^\|(?:\s*:?-+:?\s*\|)+$", stripped):
                updated_lines.append(line)
                continue

            cols = line.split("|")
            lower_cols = [c.strip().lower() for c in cols]

            if any("phase" in c for c in lower_cols) and any("status" in c for c in lower_cols):
                in_lifecycle_table = True
                in_ticket_table = False
                updated_lines.append(line)
                continue

            if any(any(k in c for k in ("ticket", "id", "issue", "number")) for c in lower_cols) and any("status" in c for c in lower_cols):
                in_ticket_table = True
                in_lifecycle_table = False
                ticket_table_header_found = True
                for i, col in enumerate(cols):
                    c_low = col.strip().lower()
                    if any(k in c_low for k in ("ticket", "number", "id")):
                        ticket_col_idx = i
                    elif "status" in c_low:
                        status_col_idx = i
                    elif any(k in c_low for k in ("commit", "sha")):
                        commit_col_idx = i
                updated_lines.append(line)
                continue

            if in_lifecycle_table and len(cols) >= 3:
                if len(cols) >= 4 and (cols[1].strip() in ("4", "5") or any(k in cols[2].lower() for k in ("implementation", "code review"))):
                    status_idx = 3
                    phase_ident = (cols[1].strip() + " " + cols[2].strip()).lower()
                else:
                    status_idx = 2
                    phase_ident = cols[1].strip().lower()

                if "4" in phase_ident or "implementation" in phase_ident:
                    if all_resolved:
                        phase_4_status = f"✅ completed ({resolved_count}/{total_tickets} resolved)" if total_tickets > 0 else "✅ completed"
                    else:
                        phase_4_status = f"🟡 in-progress ({resolved_count}/{total_tickets} resolved)" if total_tickets > 0 else "🟡 in-progress"
                    cols[status_idx] = f" {phase_4_status} "
                    line = "|".join(cols)
                    lifecycle_updated = True
                elif "5" in phase_ident or "code review" in phase_ident:
                    if all_resolved:
                        curr_phase_5_status = cols[status_idx].strip()
                        if curr_phase_5_status in ("⬜ pending", "⚪ pending") or "pending" in curr_phase_5_status.lower():
                            cols[status_idx] = " 🟡 in-progress "
                            line = "|".join(cols)
                            lifecycle_updated = True
                    elif is_in_progress:
                        curr_phase_5_status = cols[status_idx].strip()
                        if "in-progress" in curr_phase_5_status.lower():
                            cols[status_idx] = " ⬜ pending "
                            line = "|".join(cols)
                            lifecycle_updated = True
                updated_lines.append(line)
                continue

            if in_ticket_table and ticket_table_header_found:
                effective_status_idx = status_col_idx if status_col_idx >= 0 else len(cols) + status_col_idx
                if len(cols) > ticket_col_idx and len(cols) > effective_status_idx:
                    raw_cell = cols[ticket_col_idx].strip()
                    clean_cell = raw_cell.strip("*`# \t")
                    link_m = re.match(r"^\[(.*?)\](?:\(.*\))?$", clean_cell)
                    if link_m:
                        clean_cell = link_m.group(1).strip("*`# \t")
                    cell_m = re.match(r"^(?:ticket-|issue-|#)?([0-9]{1,3})", clean_cell, re.IGNORECASE)
                    cell_num = int(cell_m.group(1)) if cell_m else None

                    matches = False
                    if clean_cell.lower() == canonical_id.lower():
                        matches = True
                    elif num_val is not None and cell_num is not None and num_val == cell_num:
                        matches = True
                    elif num_val is not None and re.match(rf"^(?:ticket-|issue-|#)?0*{num_val}\b", clean_cell, re.IGNORECASE):
                        matches = True

                    if matches:
                        curr_status = cols[effective_status_idx].strip()
                        has_backticks = curr_status.startswith("`") and curr_status.endswith("`")
                        clean_new = re.sub(r"[\r\n|]", "", new_status).strip().strip("`")
                        if not clean_new.startswith("✅") and "resolved" in clean_new.lower():
                            clean_new = f"✅ {clean_new}"
                        elif not clean_new.startswith("🟡") and "in-progress" in clean_new.lower():
                            clean_new = f"🟡 {clean_new}"
                        elif not clean_new.startswith("⏳") and any(k in clean_new.lower() for k in ("ready", "pending")):
                            clean_new = f"⏳ {clean_new}"
                        if has_backticks:
                            cols[effective_status_idx] = f" `{clean_new}` "
                        else:
                            cols[effective_status_idx] = f" {clean_new} "

                        if commit_sha and commit_col_idx is not None and len(cols) > commit_col_idx:
                            clean_commit = re.sub(r"[\r\n|]", "", commit_sha).strip()
                            cols[commit_col_idx] = f" {clean_commit} "
                        elif is_unclaim and commit_col_idx is not None and len(cols) > commit_col_idx:
                            cols[commit_col_idx] = " - "

                        line = "|".join(cols)
                        table_updated = True
                updated_lines.append(line)
                continue

        if not stripped.startswith("|") and not has_clustered_subgraphs:
            target_ids = []
            if num_val is not None:
                target_ids.append(rf"T0*{num_val}")
                target_ids.append(rf"ticket-0*{num_val}")
            target_ids.append(rf"{re.escape(canonical_id)}")
            combined_prefix = "|".join(target_ids)
            node_pattern = re.compile(rf'\b((?:{combined_prefix})\s*\[)(["\']?)(.*?)\2(\])', re.IGNORECASE)

            def _replace_node_label(match: re.Match[str]) -> str:
                nonlocal graph_updated
                open_bracket = match.group(1)
                quote = match.group(2)
                label = match.group(3)
                close_bracket = match.group(4)
                clean_label = re.sub(
                    r"\s*[\(\[](?:Frontier|Ready|In[- ]Progress|Resolved|Blocked)[\)\]]\s*$",
                    "",
                    label,
                    flags=re.IGNORECASE,
                ).strip()
                if is_in_progress:
                    new_label = f"{clean_label} (In Progress)"
                elif is_unclaim:
                    new_label = clean_label
                else:
                    new_label = f"{clean_label} (Resolved)"
                if new_label != label:
                    label = new_label
                    graph_updated = True
                return f"{open_bracket}{quote}{label}{quote}{close_bracket}"

            line, sub_count = node_pattern.subn(_replace_node_label, line)
            if sub_count > 0:
                graph_updated = True

        updated_lines.append(line)

    joined_text = newline.join(updated_lines) + newline
    if all_resolved and "100% complete" not in joined_text:
        joined_text = re.sub(
            r"^(#\s+Task\s+(?:Dashboard|Graph)[^\r\n]*)",
            r"\1 (100% complete)",
            joined_text,
            count=1,
            flags=re.MULTILINE,
        )
    elif (is_in_progress or is_unclaim) and "(100% complete)" in joined_text:
        joined_text = re.sub(
            r"^(#\s+Task\s+(?:Dashboard|Graph)[^\r\n]*?)\s*\(100%\s+complete\)",
            r"\1",
            joined_text,
            flags=re.MULTILINE,
        )

    if has_clustered_subgraphs:
        if is_in_progress:
            target_subgraph = "S_IN_PROGRESS"
            new_suffix = "(In Progress)"
        elif is_unclaim:
            target_subgraph = "S_FRONTIER"
            new_suffix = "(Frontier)"
        else:
            target_subgraph = "S_RESOLVED"
            new_suffix = "(Resolved)"
        migrated_text, did_migrate = _migrate_clustered_subgraph_node(
            joined_text,
            canonical_id=canonical_id,
            num_val=num_val,
            target_subgraph=target_subgraph,
            new_suffix=new_suffix,
            scanned_tickets=scanned_tickets,
        )
        if did_migrate:
            joined_text = migrated_text
            graph_updated = True

    if joined_text != content:
        return _atomic_write_file(path, joined_text, raise_on_error=raise_on_error)

    return False


def safe_task_graph_write(
    task_graph_path: Path | str,
    ticket_ref: str,
    new_status: str = "✅ resolved",
    *,
    dag_complete: bool = False,
    commit_sha: str = "",
    validator: ManifestValidator | None = None,
    raise_on_error: bool = False,
    feature_dir: Path | str | None = None,
) -> TaskGraphWriteResult:
    """
    [ERR-RECON-03] [RES-01] Resilient, atomic on-disk task graph synchronization wrapper.
    Catches filesystem I/O failures, logs IO_WRITE_ERROR diagnostics with actionable recovery steps,
    and guarantees zero corruption of existing on-disk task graph artifacts.
    """
    path = Path(task_graph_path).resolve()
    if not path.is_file():
        msg = f"Task graph file not found: '{path}'"
        if raise_on_error:
            raise FileNotFoundError(msg)
        return TaskGraphWriteResult(False, msg, target_path=path)

    raw_id = str(ticket_ref).strip()
    id_without_ext = raw_id[:-3].strip() if raw_id.lower().endswith(".md") else raw_id
    m = re.match(r"^(?:ticket-|issue-|#)?([0-9]{1,3})", id_without_ext, re.IGNORECASE)
    clean_id = f"{int(m.group(1)):02d}" if m else id_without_ext
    clean_sha = str(commit_sha or "").strip().strip("'\"")

    v = validator or ManifestValidator()

    try:
        updated = update_task_graph_progress(
            task_graph_path=path,
            ticket_ref=clean_id,
            new_status=new_status,
            dag_complete=dag_complete,
            commit_sha=clean_sha,
            validator=v,
            raise_on_error=True,
        )
        if updated:
            msg = f"✅ Successfully synchronized task graph '{path.name}' for ticket '{clean_id}'."
        else:
            msg = f"Task graph '{path.name}' already reflects desired status for ticket '{clean_id}'."
        return TaskGraphWriteResult(True, msg, target_path=path)
    except OSError as exc:
        failing_target = _normalize_failing_path(path, exc)
        sys_err_str = str(exc)
        err_lower = sys_err_str.lower()
        is_rofs = getattr(exc, "errno", None) == 30 or "read-only" in err_lower
        is_perm = isinstance(exc, PermissionError) or "permission" in err_lower or getattr(exc, "errno", None) == 13 or is_rofs
        is_nospc = "no space" in err_lower or "disk full" in err_lower or getattr(exc, "errno", None) == 28

        if is_perm:
            diag_cause = "Permission denial (EACCES / read-only filesystem)"
            fix_step = f"chmod u+w \"{failing_target}\" (or chmod -R u+w \"{failing_target.parent}\")"
        elif is_nospc:
            diag_cause = "Storage exhaustion (ENOSPC / disk full)"
            fix_step = f"Free up storage space on the volume hosting \"{failing_target}\" (check df -h)"
        else:
            diag_cause = f"Filesystem I/O failure ({type(exc).__name__})"
            fix_step = f"Verify filesystem accessibility and write permissions for \"{failing_target}\""

        recovery_cmd = (
            f"./scripts/resolve-ticket.sh {clean_id} {clean_sha}"
            if clean_id and clean_sha
            else (f"./scripts/resolve-ticket.sh {clean_id}" if clean_id else "./scripts/resolve-ticket.sh")
        )

        diag_msg = (
            f"🚨 [ERR-RECON-03] IO_WRITE_ERROR: Filesystem write failure encountered when updating task graph.\n"
            f"Target Path: '{failing_target}'\n"
            f"System Error: {sys_err_str}\n"
            f"Preserved Git Commit: '{clean_sha}'\n"
            f"Ticket ID: '{clean_id}'\n\n"
            f"Actionable Recovery Instructions:\n"
            f"1. Diagnose root cause: {diag_cause}.\n"
            f"2. Fix filesystem state: {fix_step}.\n"
            f"3. Verified git commit reference '{clean_sha or '<sha>'}' remains preserved on integration branch HEAD.\n"
            f"4. Re-run ticket resolution to synchronize on-disk state:\n"
            f"   {recovery_cmd}\n"
            f"5. Existing disk artifacts preserved uncorrupted (zero in-place truncation)."
        )

        if raise_on_error:
            raise FilesystemSyncError(
                diag_msg,
                FilesystemIoErrorResult(
                    exit_code=1,
                    message=diag_msg,
                    target_path=failing_target,
                    system_error=sys_err_str,
                    commit_sha=clean_sha,
                    ticket_id=clean_id,
                    recovery_instructions=diag_msg,
                    tag="[ERR-RECON-03]",
                    escalation="IO_WRITE_ERROR",
                ),
            ) from exc

        return TaskGraphWriteResult(
            False,
            message=diag_msg,
            error=sys_err_str,
            target_path=failing_target,
        )



def format_ticket_resolved_signal(
    ticket_id: str,
    commit_sha: str,
    branch: str = "",
    dag_complete: bool = False,
    summary: str = "",
) -> str:
    """
    Standardized payload formatting preserved for backward compatibility.
    """
    if ticket_id is None:
        raise ValueError("ticket_id must be a non-empty string")
    raw_id = str(ticket_id).strip()
    if not raw_id or raw_id.lower() in ("none", "null"):
        raise ValueError("ticket_id must be a non-empty string")

    if commit_sha is None:
        raise ValueError("commit_sha must be a non-empty string")
    clean_sha = str(commit_sha).strip()
    if not clean_sha or clean_sha.lower() in ("none", "null"):
        raise ValueError("commit_sha must be a non-empty string")

    id_without_ext = raw_id[:-3].strip() if raw_id.lower().endswith(".md") else raw_id
    m = re.match(r"^(?:ticket-|issue-|#)?([0-9]{1,3})(?:-.*)?$", id_without_ext, re.IGNORECASE)
    clean_id = f"{int(m.group(1)):02d}" if m else id_without_ext

    clean_branch = " ".join(str(branch).strip().split()) if branch is not None else ""
    clean_summary = " ".join(str(summary).strip().split()) if summary is not None else ""

    if isinstance(dag_complete, str):
        is_complete = dag_complete.strip().lower() in ("true", "1", "yes")
    else:
        is_complete = bool(dag_complete)
    dag_str = "true" if is_complete else "false"

    return (
        "TICKET_RESOLVED_SIGNAL\n"
        f"ticket_id: {clean_id}\n"
        f"commit_sha: {clean_sha}\n"
        f"branch: {clean_branch}\n"
        f"dag_complete: {dag_str}\n"
        f"summary: {clean_summary}"
    )


def format_manual_report_banner(ticket_id: str, commit_sha: str) -> str:
    """
    Formats manual report banner directing operator to return to Command Center.
    Preserved for backward compatibility and manual report notifications.
    """
    border = "=" * 80
    return (
        f"{border}\n"
        f"Ticket {ticket_id} completed (commit: {commit_sha})\n"
        f"{border}"
    )


def sync_task_graph_on_disk(
    feature_dir_or_task_graph: Path | str,
    ticket_id: str | int,
    commit_sha: str = "",
    status: str = "✅ resolved",
    validator: ManifestValidator | None = None,
    app_data_dir: Path | str | None = None,
) -> Path:
    """
    [LEAN-02] [BAN-01] Authoritative on-disk task graph synchronization without brain mirroring.
    Updates .scratch/<feature-slug>/task-graph.md and .scratch/<feature-slug>/session.json directly
    on the local workspace filesystem. Zero Brain UI artifact mirroring.
    """
    p = Path(feature_dir_or_task_graph).resolve()
    if p.is_file() or p.suffix.lower() == ".md" or p.name.lower() == "task-graph.md":
        task_graph_file = p
        feature_dir = p.parent
    else:
        feature_dir = p
        task_graph_file = feature_dir / "task-graph.md"

    v = validator or ManifestValidator()
    raw_id = str(ticket_id).strip()
    id_without_ext = raw_id[:-3].strip() if raw_id.lower().endswith(".md") else raw_id
    m = re.match(r"^(?:ticket-|issue-|#)?([0-9]{1,3})", id_without_ext, re.IGNORECASE)
    canonical_id = f"{int(m.group(1)):02d}" if m else id_without_ext

    clean_sha = str(commit_sha or "").strip()
    if clean_sha.startswith("commit:"):
        clean_sha = clean_sha.split(":", 1)[1].strip()
    clean_sha = clean_sha.strip("'\"")

    issues_dir = feature_dir / "issues"
    dag_complete = False
    if issues_dir.is_dir():
        dag_complete = is_dag_completed(issues_dir, validator=v)

    target_failing = task_graph_file
    try:
        if task_graph_file.is_file():
            target_failing = task_graph_file
            update_task_graph_progress(
                task_graph_path=task_graph_file,
                ticket_ref=canonical_id,
                new_status=status,
                dag_complete=dag_complete,
                commit_sha=clean_sha,
                validator=v,
                raise_on_error=True,
            )

        target_failing = feature_dir / "session.json"
        _record_verified_commit_in_anchor(
            validator=v,
            feature_dir=feature_dir,
            clean_sha=clean_sha,
            active_phase="code-review" if dag_complete else None,
            raise_on_error=True,
        )
    except OSError as exc:
        failing_target = _normalize_failing_path(target_failing, exc)
        err_res = handle_filesystem_io_error(
            target_path=failing_target,
            exc=exc,
            ticket_id=canonical_id,
            commit_sha=clean_sha,
            feature_dir=feature_dir,
            action="synchronize task graph and session anchor on disk",
            app_data_dir=app_data_dir,
        )
        raise FilesystemSyncError(err_res.message, err_res) from exc

    return task_graph_file


def format_lean_progress_stream(
    ticket_id: str | int,
    commit_sha: str | None = "",
    task_graph_path: Path | str | None = None,
    *,
    feature_dir: Path | str | None = None,
    feature_slug: str | None = None,
    total_tickets: int | None = None,
    resolved_tickets: int | None = None,
    status: str = "resolved",
    raw_output: str | None = None,
    raw_trace: str | None = None,
    sync_on_disk: bool = False,
    validator: ManifestValidator | None = None,
    app_data_dir: Path | str | None = None,
) -> str:
    """
    [LEAN-01] [LEAN-02] Emits strictly 2-line bounded chat progress stream notification.
    Line 1: [<resolved>/<total>] ✅ Ticket '<id>' resolved (commit: '<sha>')
    Line 2: 📁 **Task Graph**: [`<rel_path>`](file://<abs_path>)
    Strictly bars raw compiler traces, Markdown task tables, or Mermaid diagrams from transcripts.
    Optionally synchronizes task graph on disk when sync_on_disk=True.
    """
    v = validator or ManifestValidator()
    raw_id = str(ticket_id).strip().replace("\n", "").replace("\r", "")
    id_without_ext = raw_id[:-3].strip() if raw_id.lower().endswith(".md") else raw_id
    m = re.match(r"^(?:ticket-|issue-|#)?([0-9]{1,3})", id_without_ext, re.IGNORECASE)
    canonical_id = f"{int(m.group(1)):02d}" if m else id_without_ext

    clean_sha = str(commit_sha or "").strip().replace("\n", "").replace("\r", "")
    if clean_sha.startswith("commit:"):
        clean_sha = clean_sha.split(":", 1)[1].strip()
    clean_sha = clean_sha.strip("'\"")

    tg_file: Path | None = None
    resolved_feature_dir: Path | None = None

    if feature_dir:
        resolved_feature_dir = Path(feature_dir).resolve()

    if task_graph_path:
        p = Path(task_graph_path).resolve()
        if p.is_file() or p.suffix.lower() == ".md" or p.name.lower() == "task-graph.md":
            tg_file = p
            if resolved_feature_dir is None:
                resolved_feature_dir = p.parent
        elif p.is_dir():
            tg_file = p / "task-graph.md"
            if resolved_feature_dir is None:
                resolved_feature_dir = p
        else:
            tg_file = p
            if resolved_feature_dir is None:
                resolved_feature_dir = p.parent
    elif resolved_feature_dir:
        tg_file = resolved_feature_dir / "task-graph.md"
    elif feature_slug:
        resolved_feature_dir = REPO_ROOT / ".scratch" / feature_slug
        tg_file = resolved_feature_dir / "task-graph.md"
    else:
        for cand in [
            Path(".scratch/task-graph.md"),
            REPO_ROOT / ".scratch/task-graph.md",
        ]:
            if cand.is_file():
                tg_file = cand.resolve()
                resolved_feature_dir = tg_file.parent
                break
        if not tg_file:
            resolved_feature_dir = REPO_ROOT / ".scratch"
            tg_file = resolved_feature_dir / "task-graph.md"

    is_current_resolved = "resolved" in status.lower() or "✅" in status
    sync_status = "✅ resolved" if is_current_resolved else (
        "🟡 in-progress" if ("in-progress" in status.lower() or "🟡" in status) else status
    )

    if sync_on_disk and resolved_feature_dir:
        sync_task_graph_on_disk(
            feature_dir_or_task_graph=tg_file,
            ticket_id=canonical_id,
            commit_sha=clean_sha,
            status=sync_status,
            validator=v,
            app_data_dir=app_data_dir,
        )

    if total_tickets is None or resolved_tickets is None:
        current_num = int(canonical_id) if canonical_id.isdigit() else None
        if tg_file.is_file():
            tg_parsed = parse_task_graph_tickets(tg_file)
            if tg_parsed:
                if total_tickets is None:
                    total_tickets = len(tg_parsed)
                if resolved_tickets is None:
                    count = 0
                    current_matched = False
                    for t in tg_parsed:
                        t_id = str(t.get("id", "")).strip()
                        t_status = str(t.get("status", "")).lower()
                        t_resolved = "resolved" in t_status or "✅" in t_status
                        is_target = (t_id == canonical_id) or (current_num is not None and t_id.isdigit() and int(t_id) == current_num)
                        if is_target:
                            current_matched = True
                            if is_current_resolved or t_resolved:
                                count += 1
                        elif t_resolved:
                            count += 1
                    if not current_matched and is_current_resolved:
                        count += 1
                        total_tickets += 1
                    resolved_tickets = count
        elif resolved_feature_dir and (resolved_feature_dir / "issues").is_dir():
            try:
                scanned = v.scan_tickets_from_tracker(resolved_feature_dir / "issues")
                if scanned:
                    if total_tickets is None:
                        total_tickets = len(scanned)
                    if resolved_tickets is None:
                        count = 0
                        current_matched = False
                        for t in scanned:
                            t_id = str(t.get("id", "")).strip()
                            t_status = str(t.get("status", "")).lower()
                            t_resolved = v._is_status_resolved(t_status)
                            is_target = (t_id == canonical_id) or (current_num is not None and t_id.isdigit() and int(t_id) == current_num)
                            if is_target:
                                current_matched = True
                                if is_current_resolved or t_resolved:
                                    count += 1
                            elif t_resolved:
                                count += 1
                        if not current_matched and is_current_resolved:
                            count += 1
                            total_tickets += 1
                        resolved_tickets = count
            except Exception:
                pass

    if total_tickets is not None and total_tickets > 0 and resolved_tickets is not None:
        resolved_tickets = min(resolved_tickets, total_tickets)
        progress_prefix = f"[{resolved_tickets}/{total_tickets}] "
    else:
        progress_prefix = ""

    if is_current_resolved:
        line1 = f"{progress_prefix}✅ Ticket '{canonical_id}' resolved (commit: '{clean_sha}')"
    elif "in-progress" in status.lower() or "🟡" in status:
        line1 = f"{progress_prefix}🟡 Ticket '{canonical_id}' in-progress" + (f" (commit: '{clean_sha}')" if clean_sha else "")
    else:
        line1 = f"{progress_prefix}{status} Ticket '{canonical_id}'" + (f" (commit: '{clean_sha}')" if clean_sha else "")

    abs_tg = tg_file.resolve()
    abs_uri = abs_tg.as_uri()

    tg_posix = abs_tg.as_posix()
    if "/.scratch/" in tg_posix:
        rel_label = ".scratch/" + tg_posix.split("/.scratch/", 1)[1]
    elif tg_posix.startswith(".scratch/"):
        rel_label = tg_posix
    else:
        try:
            rel_label = abs_tg.relative_to(REPO_ROOT).as_posix()
        except Exception:
            try:
                rel_label = abs_tg.relative_to(Path.cwd().resolve()).as_posix()
            except Exception:
                rel_label = abs_tg.name

    rel_label = rel_label.replace("\n", "").replace("\r", "")
    line2 = f"📁 **Task Graph**: [`{rel_label}`]({abs_uri})"
    return f"{line1}\n{line2}"


def claim_ticket(
    ticket_ref: str,
    root_dir: Path | str | None = None,
    tracker_dir: Path | str | None = None,
    branch: str = "",
    lean_stream: bool = False,
) -> ResolutionResult:
    """
    [CLAIM-01] [ERR-CLAIM-01] Atomically claims a ticket on disk.
    Updates ticket markdown frontmatter to '**Status:** in-progress' and
    synchronizes .scratch/<feature-slug>/task-graph.md (row marked '🟡 in-progress',
    Mermaid node marked '(In Progress)'), with safe fallback when task-graph.md is missing.
    """
    if not ticket_ref or not str(ticket_ref).strip():
        return ResolutionResult(1, "Error: ticket-id must be a non-empty string.")

    clean_ref = str(ticket_ref).strip()
    validator = ManifestValidator()

    if tracker_dir and str(tracker_dir).strip():
        search_dir = Path(tracker_dir).resolve()
    elif root_dir and str(root_dir).strip():
        search_dir = Path(root_dir).resolve()
    else:
        cwd = Path.cwd().resolve()
        if (cwd / ".scratch").is_dir() or (cwd / "issues").is_dir() or cwd.name == "issues":
            search_dir = cwd
        elif (REPO_ROOT / ".scratch").is_dir() or (REPO_ROOT / "issues").is_dir():
            search_dir = REPO_ROOT
        else:
            search_dir = cwd

    if not search_dir.exists():
        return ResolutionResult(1, f"Error: Search directory does not exist: {search_dir}")

    try:
        ticket_path = validator.resolve_ticket_shorthand(search_dir, clean_ref)
    except FileNotFoundError as fnf_err:
        return ResolutionResult(1, f"Error: Ticket '{clean_ref}' not found in {search_dir}: {fnf_err}")
    except Exception as exc:
        return ResolutionResult(1, f"Error resolving ticket shorthand '{clean_ref}': {exc}")

    m = re.match(r"^(?:ticket-|issue-|#)?([0-9]{1,3})", ticket_path.stem)
    canonical_id = f"{int(m.group(1)):02d}" if m else ticket_path.stem

    issues_dir = ticket_path.parent
    feature_dir = issues_dir.parent if issues_dir.name == "issues" else issues_dir

    try:
        raw_ticket_text = ticket_path.read_text(encoding="utf-8")
        meta = validator._parse_ticket_markdown(raw_ticket_text, ticket_path.stem)
        existing_status = meta.get("Status", meta.get("status", "")).strip()
    except Exception:
        existing_status = ""

    if validator._is_status_resolved(existing_status):
        diagnostic = f"ℹ️ [EDGE-CLAIM-RACE] Ticket '{canonical_id}' is already resolved (status: '{existing_status}'). Claim skipped."
        return ResolutionResult(0, diagnostic, dispatched=False, suppressed=False)

    if validator._is_status_in_progress(existing_status):
        diagnostic = f"ℹ️ [EDGE-CLAIM-RACE] Ticket '{canonical_id}' is already in-progress (status: '{existing_status}'). Claim is idempotent."
        return ResolutionResult(0, diagnostic, dispatched=False, suppressed=False)

    # [ERR-DRAIN-01] Detect circular dependencies in task graph DAG before claiming
    cycle_check = detect_dag_cycles(feature_dir)
    if cycle_check.has_cycle:
        return ResolutionResult(1, cycle_check.diagnostic, dispatched=False, suppressed=False)

    try:
        validator.update_ticket_status(ticket_path, "in-progress")
    except Exception as exc:
        return ResolutionResult(1, f"Error updating ticket status on disk for '{ticket_path}': {exc}")

    task_graph_candidates = [
        feature_dir / "task-graph.md",
        issues_dir / "task-graph.md",
        search_dir / "task-graph.md",
        search_dir / ".scratch" / feature_dir.name / "task-graph.md",
        REPO_ROOT / ".scratch" / feature_dir.name / "task-graph.md",
    ]

    seen_tg_paths: set[Path] = set()
    target_failing = next((c for c in task_graph_candidates if c.is_file()), feature_dir / "task-graph.md")
    try:
        for tg_candidate in task_graph_candidates:
            if tg_candidate.is_file():
                resolved_tg = tg_candidate.resolve()
                if resolved_tg not in seen_tg_paths:
                    seen_tg_paths.add(resolved_tg)
                    target_failing = tg_candidate
                    try:
                        tg_raw = resolved_tg.read_text(encoding="utf-8")
                        is_valid, _ = validate_task_graph_schema(tg_raw, validator=validator)
                        if not is_valid:
                            sys.stdout.write("[ERR-DASH-01] Corrupted task-graph schema detected, initiating automatic on-disk recovery\n")
                            sys.stdout.flush()
                            recover_malformed_dashboard(
                                resolved_tg,
                                root_dir=search_dir,
                                validator=validator,
                                write_back=True,
                                force_heal=True,
                            )
                    except Exception:
                        pass
                    update_task_graph_progress(
                        resolved_tg,
                        canonical_id,
                        "🟡 in-progress",
                        dag_complete=False,
                        validator=validator,
                        raise_on_error=True,
                    )
    except OSError as exc:
        try:
            validator.update_ticket_status(ticket_path, existing_status or "ready-for-agent")
        except Exception:
            pass
        failing_target = _normalize_failing_path(target_failing, exc)
        return handle_filesystem_io_error(
            target_path=failing_target,
            exc=exc,
            ticket_id=canonical_id,
            feature_dir=feature_dir,
            action="claim ticket and synchronize task graph on disk",
        )

    confirm_msg = f"✅ Ticket '{canonical_id}' claimed on disk (status: 'in-progress')."
    return ResolutionResult(0, confirm_msg, dispatched=False, suppressed=False)


def unclaim_ticket(
    ticket_ref: str,
    root_dir: Path | str | None = None,
    tracker_dir: Path | str | None = None,
    target_status: str = "ready-for-agent",
) -> ResolutionResult:
    """
    [UNCLAIM-01] [ERR-UNCLAIM-01] Atomically releases an in-progress ticket claim on disk.
    Resets ticket markdown frontmatter to '**Status:** ready-for-agent' and
    synchronizes .scratch/<feature-slug>/task-graph.md (row marked '⏳ ready-for-agent',
    Mermaid node reset to ready state), recovering from stale session crashes or interrupted sessions.
    """
    if not ticket_ref or not str(ticket_ref).strip():
        return ResolutionResult(1, "Error: ticket-id must be a non-empty string.")

    clean_ref = str(ticket_ref).strip()
    validator = ManifestValidator()

    if tracker_dir and str(tracker_dir).strip():
        search_dir = Path(tracker_dir).resolve()
    elif root_dir and str(root_dir).strip():
        search_dir = Path(root_dir).resolve()
    else:
        cwd = Path.cwd().resolve()
        if (cwd / ".scratch").is_dir() or (cwd / "issues").is_dir() or cwd.name == "issues":
            search_dir = cwd
        elif (REPO_ROOT / ".scratch").is_dir() or (REPO_ROOT / "issues").is_dir():
            search_dir = REPO_ROOT
        else:
            search_dir = cwd

    if not search_dir.exists():
        return ResolutionResult(1, f"Error: Search directory does not exist: {search_dir}")

    try:
        ticket_path = validator.resolve_ticket_shorthand(search_dir, clean_ref)
    except FileNotFoundError as fnf_err:
        return ResolutionResult(1, f"Error: Ticket '{clean_ref}' not found in {search_dir}: {fnf_err}")
    except Exception as exc:
        return ResolutionResult(1, f"Error resolving ticket shorthand '{clean_ref}': {exc}")

    m = re.match(r"^(?:ticket-|issue-|#)?([0-9]{1,3})", ticket_path.stem)
    canonical_id = f"{int(m.group(1)):02d}" if m else ticket_path.stem

    issues_dir = ticket_path.parent
    feature_dir = issues_dir.parent if issues_dir.name == "issues" else issues_dir

    try:
        raw_ticket_text = ticket_path.read_text(encoding="utf-8")
        meta = validator._parse_ticket_markdown(raw_ticket_text, ticket_path.stem)
        existing_status = meta.get("Status", meta.get("status", "")).strip()
    except Exception:
        existing_status = ""

    if validator._is_status_resolved(existing_status):
        diagnostic = f"⚠️ Cannot unclaim ticket '{canonical_id}' because it is already resolved (status: '{existing_status}')."
        return ResolutionResult(1, diagnostic, dispatched=False, suppressed=False)

    if not validator._is_status_in_progress(existing_status) and existing_status.lower() in ("ready-for-agent", "pending", "ready"):
        diagnostic = f"ℹ️ Ticket '{canonical_id}' is already unclaimed (status: '{existing_status}'). Unclaim is idempotent."
        return ResolutionResult(0, diagnostic, dispatched=False, suppressed=False)

    try:
        validator.update_ticket_status(ticket_path, target_status)
    except Exception as exc:
        return ResolutionResult(1, f"Error updating ticket status on disk for '{ticket_path}': {exc}")

    task_graph_candidates = [
        feature_dir / "task-graph.md",
        issues_dir / "task-graph.md",
        search_dir / "task-graph.md",
        search_dir / ".scratch" / feature_dir.name / "task-graph.md",
        REPO_ROOT / ".scratch" / feature_dir.name / "task-graph.md",
    ]

    seen_tg_paths: set[Path] = set()
    target_failing = next((c for c in task_graph_candidates if c.is_file()), feature_dir / "task-graph.md")
    try:
        for tg_candidate in task_graph_candidates:
            if tg_candidate.is_file():
                resolved_tg = tg_candidate.resolve()
                if resolved_tg not in seen_tg_paths:
                    seen_tg_paths.add(resolved_tg)
                    target_failing = tg_candidate
                    update_task_graph_progress(
                        resolved_tg,
                        canonical_id,
                        f"⏳ {target_status}",
                        dag_complete=False,
                        validator=validator,
                        raise_on_error=True,
                    )
    except OSError as exc:
        try:
            validator.update_ticket_status(ticket_path, existing_status or "in-progress")
        except Exception:
            pass
        failing_target = _normalize_failing_path(target_failing, exc)
        return handle_filesystem_io_error(
            target_path=failing_target,
            exc=exc,
            ticket_id=canonical_id,
            feature_dir=feature_dir,
            action="unclaim ticket and synchronize task graph on disk",
        )

    confirm_msg = f"✅ Ticket '{canonical_id}' unclaimed on disk (status reset to '{target_status}')."
    return ResolutionResult(0, confirm_msg, dispatched=False, suppressed=False)


def resolve_ticket_atomic(
    ticket_ref: str,
    commit_sha: str,
    root_dir: Path | str | None = None,
    tracker_dir: Path | str | None = None,
    client_path: Path | str | None = None,
    timeout_seconds: float = 15.0,
    summary: str = "",
    branch: str = "",
    current_conversation_id: str | None = None,
    app_data_dir: Path | str | None = None,
    lean_summary: bool = False,
    artifact_path: Path | str | None = None,
    lean_stream: bool = False,
    emit_briefs: bool = False,
) -> ResolutionResult:
    """
    [RES-01] [RES-02] [IDEMP-01] [RUN-02] [PRUNE-01] [SNTZ-01] [EDGE-SNTZ-02] [API-01] [API-02]
    Atomically updates ticket status to resolved on disk.
    Executes without referencing socket ports or network timeouts; ConnectRPC signal dispatch
    is decommissioned in favor of localized on-disk state updates and terminal output guidance.
    When emit_briefs is False (default in single-ticket mode), suppresses downstream /implement
    commands and /code-review starter briefs to prevent worker tab prompt bleed.
    The artifact_path parameter is deprecated as a safe no-op for backward compatibility ([API-01]).
    """
    if not ticket_ref or not str(ticket_ref).strip():
        return ResolutionResult(1, "Error: ticket-id must be a non-empty string.")

    clean_ref = str(ticket_ref).strip()
    clean_sha = str(commit_sha).strip()

    if not clean_sha or clean_sha.lower() in ("none", "null", "undefined") or re.search(r"[\s|]", clean_sha):
        return ResolutionResult(1, f"Error: Invalid commit-sha '{commit_sha}'. A valid non-empty git commit SHA or reference is required.")

    validator = ManifestValidator()

    if tracker_dir and str(tracker_dir).strip():
        search_dir = Path(tracker_dir).resolve()
    elif root_dir and str(root_dir).strip():
        search_dir = Path(root_dir).resolve()
    else:
        cwd = Path.cwd().resolve()
        if (cwd / ".scratch").is_dir() or (cwd / "issues").is_dir() or cwd.name == "issues":
            search_dir = cwd
        elif (REPO_ROOT / ".scratch").is_dir() or (REPO_ROOT / "issues").is_dir():
            search_dir = REPO_ROOT
        else:
            search_dir = cwd

    if not search_dir.exists():
        return ResolutionResult(1, f"Error: Search directory does not exist: {search_dir}")

    try:
        ticket_path = validator.resolve_ticket_shorthand(search_dir, clean_ref)
    except FileNotFoundError as fnf_err:
        return ResolutionResult(1, f"Error: Ticket '{clean_ref}' not found in {search_dir}: {fnf_err}")
    except Exception as exc:
        return ResolutionResult(1, f"Error resolving ticket shorthand '{clean_ref}': {exc}")

    m = re.match(r"^(?:ticket-|issue-|#)?([0-9]{1,3})", ticket_path.stem)
    canonical_id = f"{int(m.group(1)):02d}" if m else ticket_path.stem

    issues_dir = ticket_path.parent
    feature_dir = issues_dir.parent if issues_dir.name == "issues" else issues_dir

    try:
        raw_ticket_text = ticket_path.read_text(encoding="utf-8")
        meta = validator._parse_ticket_markdown(raw_ticket_text, ticket_path.stem)
        existing_status = meta.get("Status", meta.get("status", "")).strip()
    except Exception:
        existing_status = "in-progress"

    # [RUN-02] Verify execution state machine presence and stage gating before resolving
    state_dir = feature_dir if (feature_dir / "execution-state.json").is_file() else (
        search_dir if (search_dir / "execution-state.json").is_file() else (
            issues_dir if (issues_dir / "execution-state.json").is_file() else feature_dir
        )
    )
    exec_state: dict[str, Any] | None = None
    if load_execution_state:
        try:
            exec_state = load_execution_state(state_dir)
        except (json.JSONDecodeError, OSError, ValueError):
            exec_state = None
    if not exec_state and (state_dir / "execution-state.json").is_file():
        try:
            raw_s = json.loads((state_dir / "execution-state.json").read_text(encoding="utf-8"))
            if isinstance(raw_s, dict):
                exec_state = raw_s
        except (json.JSONDecodeError, OSError, ValueError):
            exec_state = None

    if not exec_state:
        return ResolutionResult(
            1,
            f"Error: execution-state.json not found in '{state_dir}'. "
            f"Ticket resolution requires active execution state machine at {STAGE_5_COMMIT_AND_RESOLVE} [RUN-02].",
        )

    active_stage = str(exec_state.get("activeStage") or "").strip()
    if active_stage != STAGE_5_COMMIT_AND_RESOLVE:
        return ResolutionResult(
            1,
            f"Error: Invalid activeStage '{active_stage}' in execution-state.json. "
            f"Ticket resolution requires activeStage == '{STAGE_5_COMMIT_AND_RESOLVE}' [RUN-02].",
        )

    # [IDEMP-01] Commit-Based Idempotency Guard (ADR 0027):
    # Idempotency is anchored strictly to verified git commit SHAs in session.json (reviewedCommits),
    # rather than mutable on-disk markdown frontmatter.
    anchor_data = _read_session_anchor_data(validator, feature_dir)

    existing_commits: list[str] = []
    if anchor_data and isinstance(anchor_data.get("reviewedCommits"), list):
        existing_commits = [str(c).strip() for c in anchor_data.get("reviewedCommits", [])]

    clean_sha_norm = clean_sha.lower()
    existing_commits_norm = [c.lower() for c in existing_commits]
    is_commit_recorded = (
        clean_sha_norm in existing_commits_norm
        or any(
            (len(c) >= 7 and len(clean_sha_norm) >= 7 and (clean_sha_norm.startswith(c) or c.startswith(clean_sha_norm)))
            for c in existing_commits_norm
        )
    )

    task_graph_candidates = [
        feature_dir / "task-graph.md",
        issues_dir / "task-graph.md",
        search_dir / "task-graph.md",
        search_dir / ".scratch" / feature_dir.name / "task-graph.md",
    ]

    seen_tg_paths: set[Path] = set()

    if is_commit_recorded:
        try:
            validator.update_ticket_status(ticket_path, "resolved")
        except (OSError, IOError) as exc:
            return handle_filesystem_io_error(
                target_path=ticket_path,
                exc=exc,
                ticket_id=canonical_id,
                commit_sha=clean_sha,
                feature_dir=feature_dir,
                action="update ticket status frontmatter",
                app_data_dir=app_data_dir,
            )
        except Exception:
            pass

        target_failing = next((c for c in task_graph_candidates if c.is_file()), feature_dir / "task-graph.md")
        try:
            dag_complete = is_dag_completed(issues_dir, validator=validator)
            for tg_candidate in task_graph_candidates:
                if tg_candidate.is_file():
                    resolved_tg = tg_candidate.resolve()
                    if resolved_tg not in seen_tg_paths:
                        seen_tg_paths.add(resolved_tg)
                        target_failing = tg_candidate
                        try:
                            tg_raw = resolved_tg.read_text(encoding="utf-8")
                            is_valid, _ = validate_task_graph_schema(tg_raw, validator=validator)
                            if not is_valid:
                                if not lean_stream:
                                    sys.stdout.write("[ERR-DASH-01] Corrupted task-graph schema detected, initiating automatic on-disk recovery\n")
                                    sys.stdout.flush()
                                recover_malformed_dashboard(
                                    resolved_tg,
                                    root_dir=search_dir,
                                    validator=validator,
                                    write_back=True,
                                    force_heal=True,
                                )
                        except Exception:
                            pass
                        update_task_graph_progress(
                            tg_candidate,
                            canonical_id,
                            "✅ resolved",
                            dag_complete=dag_complete,
                            commit_sha=clean_sha,
                            validator=validator,
                            raise_on_error=True,
                        )
            target_failing = feature_dir / "session.json"
            _record_verified_commit_in_anchor(
                validator,
                feature_dir,
                clean_sha,
                active_phase="code-review" if dag_complete else None,
                raise_on_error=True,
            )
        except OSError as exc:
            try:
                validator.update_ticket_status(ticket_path, existing_status or "in-progress")
            except Exception:
                pass
            failing_target = _normalize_failing_path(target_failing, exc)
            return handle_filesystem_io_error(
                target_path=failing_target,
                exc=exc,
                ticket_id=canonical_id,
                commit_sha=clean_sha,
                feature_dir=feature_dir,
                action="synchronize task graph and session anchor on disk",
                app_data_dir=app_data_dir,
            )

        if lean_stream:
            tg_cand_stream = next((c for c in task_graph_candidates if c.is_file()), feature_dir / "task-graph.md")
            idemp_msg = format_lean_progress_stream(
                ticket_id=canonical_id,
                commit_sha=clean_sha,
                task_graph_path=tg_cand_stream,
                feature_dir=feature_dir,
                validator=validator,
            )
        elif emit_briefs:
            completion_extra = format_operator_handoff(
                issues_dir,
                validator=validator,
                dag_complete=dag_complete,
                lean_summary=lean_summary,
                artifact_path=artifact_path,
            )
            sep = "\n\n" if lean_summary else ""
            idemp_msg = (
                f"ℹ️ Commit '{clean_sha}' for ticket '{canonical_id}' is already recorded in session anchor (already marked resolved).\n"
                f"Ticket resolution skipped (idempotent).{sep}{completion_extra}"
            )
        else:
            return_directive = "👉 Return to the Command Center (CC) session to report completion."
            idemp_msg = (
                f"ℹ️ Commit '{clean_sha}' for ticket '{canonical_id}' is already recorded in session anchor (already marked resolved).\n"
                f"Ticket resolution skipped (idempotent).\n\n"
                f"{return_directive}"
            )
        _mark_execution_state_completed(state_dir)
        return ResolutionResult(0, idemp_msg, dispatched=False, suppressed=True)

    try:
        validator.update_ticket_status(ticket_path, "resolved")
    except (OSError, IOError) as exc:
        return handle_filesystem_io_error(
            target_path=ticket_path,
            exc=exc,
            ticket_id=canonical_id,
            commit_sha=clean_sha,
            feature_dir=feature_dir,
            action="update ticket status frontmatter",
            app_data_dir=app_data_dir,
        )
    except Exception as exc:
        return ResolutionResult(1, f"Error updating ticket status on disk for '{ticket_path}': {exc}")

    target_failing = next((c for c in task_graph_candidates if c.is_file()), feature_dir / "task-graph.md")
    try:
        dag_complete = is_dag_completed(issues_dir, validator=validator)
        for tg_candidate in task_graph_candidates:
            if tg_candidate.is_file():
                resolved_tg = tg_candidate.resolve()
                if resolved_tg not in seen_tg_paths:
                    seen_tg_paths.add(resolved_tg)
                    target_failing = tg_candidate
                    try:
                        tg_raw = resolved_tg.read_text(encoding="utf-8")
                        is_valid, _ = validate_task_graph_schema(tg_raw, validator=validator)
                        if not is_valid:
                            if not lean_stream:
                                sys.stdout.write("[ERR-DASH-01] Corrupted task-graph schema detected, initiating automatic on-disk recovery\n")
                                sys.stdout.flush()
                            recover_malformed_dashboard(
                                resolved_tg,
                                root_dir=search_dir,
                                validator=validator,
                                write_back=True,
                                force_heal=True,
                            )
                    except Exception:
                        pass
                    update_task_graph_progress(
                        tg_candidate,
                        canonical_id,
                        "✅ resolved",
                        dag_complete=dag_complete,
                        commit_sha=clean_sha,
                        validator=validator,
                        raise_on_error=True,
                    )

        target_failing = feature_dir / "session.json"
        _record_verified_commit_in_anchor(
            validator,
            feature_dir,
            clean_sha,
            active_phase="code-review" if dag_complete else None,
            raise_on_error=True,
        )
    except OSError as exc:
        try:
            validator.update_ticket_status(ticket_path, existing_status or "in-progress")
        except Exception:
            pass
        failing_target = _normalize_failing_path(target_failing, exc)
        return handle_filesystem_io_error(
            target_path=failing_target,
            exc=exc,
            ticket_id=canonical_id,
            commit_sha=clean_sha,
            feature_dir=feature_dir,
            action="synchronize task graph and session anchor on disk",
            app_data_dir=app_data_dir,
        )

    # Structural discovery validation hook: discover_command_center availability check on validator
    _ = getattr(validator, "discover_command_center", None)

    if lean_stream:
        tg_cand_stream = next((c for c in task_graph_candidates if c.is_file()), feature_dir / "task-graph.md")
        confirm_msg = format_lean_progress_stream(
            ticket_id=canonical_id,
            commit_sha=clean_sha,
            task_graph_path=tg_cand_stream,
            feature_dir=feature_dir,
            validator=validator,
        )
    elif emit_briefs:
        completion_extra = format_operator_handoff(
            issues_dir,
            validator=validator,
            dag_complete=dag_complete,
            lean_summary=lean_summary,
            artifact_path=artifact_path,
        )
        sep = "\n\n" if lean_summary else ""
        confirm_msg = (
            f"✅ Ticket '{canonical_id}' marked resolved on disk (commit: '{clean_sha}').{sep}{completion_extra}"
        )
    else:
        return_directive = "👉 Return to the Command Center (CC) session to report completion."
        confirm_msg = (
            f"✅ Ticket '{canonical_id}' marked resolved on disk (commit: '{clean_sha}').\n\n"
            f"{return_directive}"
        )
    _mark_execution_state_completed(state_dir)
    return ResolutionResult(0, confirm_msg, dispatched=False, suppressed=False)


def inspect_git_repository_state(
    git_root: Path | str,
    ticket_id: str = "",
) -> GitRepositoryStateResult:
    """
    [ERR-RECON-02] Inspects Git repository state for active lock files (.git/index.lock)
    and detached HEAD conditions. Returns structured diagnostic result with instructions.
    """
    root = Path(git_root).resolve()
    git_entry = root / ".git"
    lock_file: Path | None = None
    if git_entry.is_file():
        try:
            gd_text = git_entry.read_text(encoding="utf-8").strip()
            if gd_text.startswith("gitdir:"):
                gd_raw = gd_text.split(":", 1)[1].strip()
                gd_path = Path(gd_raw)
                if not gd_path.is_absolute():
                    gd_path = (root / gd_path).resolve()
                lock_file = gd_path / "index.lock"
        except Exception:
            pass
    elif git_entry.is_dir():
        lock_file = git_entry / "index.lock"

    if lock_file and lock_file.exists():
        warning_msg = (
            f"⚠️ Repository Lock Alert: An active Git lock file was detected at '{lock_file}'. "
            f"Another Git process may be running, or a stale lock remains. Remove the lock file or "
            f"wait for concurrent processes to complete before reconciling ticket '{ticket_id}'. "
            f"Ticket status remains unmutated."
        )
        return GitRepositoryStateResult(False, warning_msg, lock_file=lock_file)

    sym_proc = subprocess.run(
        ["git", "symbolic-ref", "-q", "HEAD"],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=5,
    )
    if sym_proc.returncode != 0:
        warning_msg = (
            f"⚠️ Detached HEAD Alert: Git repository is in a detached HEAD state. "
            f"Reconciliation requires an active branch to verify and record commits for ticket '{ticket_id}'. "
            f"Please switch to or create a branch before proceeding. Ticket status remains unmutated."
        )
        return GitRepositoryStateResult(False, warning_msg, is_detached_head=True)

    return GitRepositoryStateResult(True, "Git repository state is valid and consistent.")


def verify_commit_and_reconcile(
    ticket_ref: str,
    root_dir: Path | str | None = None,
    tracker_dir: Path | str | None = None,
    commit_sha: str | None = None,
    branch: str = "",
    app_data_dir: Path | str | None = None,
    lean_summary: bool = False,
    git_dir: Path | str | None = None,
    lean_stream: bool = False,
) -> ReconciliationResult:
    """
    [RECON-01] [ERR-RECON-01] [ERR-RECON-02] Verifies physical git commit evidence on HEAD and self-heals task graph state.
    - Inspects Git repository state for active index lock or detached HEAD condition ([ERR-RECON-02]).
    - Inspects Git HEAD (or specified commit SHA) to verify physical git commit evidence.
    - If zero new commits or code modifications associated with the ticket exist on HEAD,
      rejects the claim, returns an explicit rejection alert warning, and keeps ticket status unmutated ([ERR-RECON-01]).
    - If new git commit evidence exists, verifies ticket frontmatter and task-graph.md status;
      if either is un-resolved, repairs on-disk state in-place and re-evaluates the DAG to identify newly unblocked frontier tickets ([RECON-01]).
    """
    if not ticket_ref or not str(ticket_ref).strip():
        return ReconciliationResult(1, "Error: ticket-id must be a non-empty string.", is_rejected=True)

    clean_ref = str(ticket_ref).strip()
    validator = ManifestValidator()

    if tracker_dir and str(tracker_dir).strip():
        search_dir = Path(tracker_dir).resolve()
    elif root_dir and str(root_dir).strip():
        search_dir = Path(root_dir).resolve()
    else:
        cwd = Path.cwd().resolve()
        if (cwd / ".scratch").is_dir() or (cwd / "issues").is_dir() or cwd.name == "issues":
            search_dir = cwd
        elif (REPO_ROOT / ".scratch").is_dir() or (REPO_ROOT / "issues").is_dir():
            search_dir = REPO_ROOT
        else:
            search_dir = cwd

    if not search_dir.exists():
        return ReconciliationResult(
            1,
            f"Error: Search directory does not exist: {search_dir}",
            ticket_id=clean_ref,
            is_rejected=True,
        )

    try:
        ticket_path = validator.resolve_ticket_shorthand(search_dir, clean_ref)
    except FileNotFoundError as fnf_err:
        return ReconciliationResult(
            1,
            f"Error: Ticket '{clean_ref}' not found in {search_dir}: {fnf_err}",
            ticket_id=clean_ref,
            is_rejected=True,
        )
    except Exception as exc:
        return ReconciliationResult(
            1,
            f"Error resolving ticket shorthand '{clean_ref}': {exc}",
            ticket_id=clean_ref,
            is_rejected=True,
        )

    m = re.match(r"^(?:ticket-|issue-|#)?([0-9]{1,3})", ticket_path.stem)
    canonical_id = f"{int(m.group(1)):02d}" if m else ticket_path.stem
    numeric_id = int(m.group(1)) if m else None

    issues_dir = ticket_path.parent
    feature_dir = issues_dir.parent if issues_dir.name == "issues" else issues_dir

    target_repo = Path(git_dir).resolve() if git_dir else (
        Path(root_dir).resolve() if root_dir else feature_dir
    )
    git_root: Path | None = None
    curr = target_repo
    while True:
        if (curr / ".git").exists():
            git_root = curr
            break
        if curr.parent == curr:
            break
        curr = curr.parent

    if not git_root:
        try:
            p = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=str(target_repo),
                capture_output=True,
                text=True,
                timeout=5,
            )
            if p.returncode == 0 and p.stdout.strip():
                git_root = Path(p.stdout.strip())
        except Exception:
            pass

    if not git_root:
        git_root = target_repo

    # [ERR-RECON-02] Active Git lock file and detached HEAD state check
    repo_state = inspect_git_repository_state(git_root, ticket_id=canonical_id)
    if not repo_state.ok:
        return ReconciliationResult(1, repo_state.diagnostic, ticket_id=canonical_id, is_rejected=True)

    head_proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(git_root),
        capture_output=True,
        text=True,
        timeout=5,
    )
    if head_proc.returncode != 0 or not head_proc.stdout.strip():
        warning_msg = (
            f"⚠️ Rejection Alert: No physical git commit evidence was found on HEAD for ticket '{canonical_id}'. "
            f"The completion claim is rejected; ticket status remains unmutated."
        )
        return ReconciliationResult(1, warning_msg, ticket_id=canonical_id, is_rejected=True)

    head_sha = head_proc.stdout.strip()

    ticket_content = ticket_path.read_text(encoding="utf-8")
    ticket_meta = _parse_ticket_file_metadata(ticket_content, ticket_path.name)
    scenario_tags = ticket_meta.get("scenario_tags", [])

    associated_paths: list[str] = []
    raw_seam = str(ticket_meta.get("seam", "")).strip()
    if raw_seam:
        seam_path = raw_seam.split("#", 1)[0].strip()
        if seam_path:
            associated_paths.append(seam_path.replace("\\", "/"))
    raw_ctx = str(ticket_meta.get("context", "")).strip()
    if raw_ctx:
        ctx_path = raw_ctx.split("#", 1)[0].strip()
        if ctx_path:
            associated_paths.append(ctx_path.replace("\\", "/"))
    raw_ver = str(ticket_meta.get("verification", "")).strip()
    if raw_ver:
        for token in raw_ver.split():
            if ("/" in token or "." in token) and not token.startswith("-"):
                associated_paths.append(token.strip("'\";,").replace("\\", "/"))

    verified_sha: str | None = None
    if commit_sha and str(commit_sha).strip():
        clean_sha = str(commit_sha).strip()
        if clean_sha.startswith("-") or not re.match(r"^[A-Za-z0-9_./@~^{}-]+$", clean_sha):
            warning_msg = (
                f"⚠️ Rejection Alert: Invalid commit SHA format '{clean_sha}' for ticket '{canonical_id}'. "
                f"The completion claim is rejected; ticket status remains unmutated."
            )
            return ReconciliationResult(1, warning_msg, ticket_id=canonical_id, is_rejected=True)
        verify_proc = subprocess.run(
            ["git", "rev-parse", "--verify", f"{clean_sha}^{{commit}}"],
            cwd=str(git_root),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if verify_proc.returncode != 0 or not verify_proc.stdout.strip():
            warning_msg = (
                f"⚠️ Rejection Alert: Specified commit '{clean_sha}' does not exist on Git HEAD or repository history for ticket '{canonical_id}'. "
                f"The completion claim is rejected; ticket status remains unmutated."
            )
            return ReconciliationResult(1, warning_msg, ticket_id=canonical_id, is_rejected=True)
        verified_sha = verify_proc.stdout.strip()
    else:
        anchor_data = _read_session_anchor_data(validator, feature_dir)
        reviewed_commits: set[str] = set()
        if anchor_data and isinstance(anchor_data.get("reviewedCommits"), list):
            for c in anchor_data.get("reviewedCommits", []):
                cs = str(c).strip().lower()
                if cs:
                    reviewed_commits.add(cs)

        task_graph_candidates = [
            feature_dir / "task-graph.md",
            issues_dir / "task-graph.md",
            search_dir / "task-graph.md",
            search_dir / ".scratch" / feature_dir.name / "task-graph.md",
            REPO_ROOT / ".scratch" / feature_dir.name / "task-graph.md",
        ]
        tg_file = next((c for c in task_graph_candidates if c.is_file()), None)
        other_ticket_commits: dict[str, str] = {}
        if tg_file:
            try:
                tg_ticks = parse_task_graph_tickets(tg_file)
                for t in tg_ticks:
                    t_id = str(t.get("id", "")).strip().lstrip("#")
                    if t_id.isdigit():
                        t_id = f"{int(t_id):02d}"
                    c_sha = str(t.get("verified_commit", "")).strip()
                    if c_sha and c_sha != "-":
                        other_ticket_commits[t_id] = c_sha
                        if t_id != canonical_id:
                            reviewed_commits.add(c_sha.lower())
            except Exception:
                pass

        if canonical_id in other_ticket_commits:
            cand_sha = other_ticket_commits[canonical_id]
            if cand_sha and (cand_sha.startswith("-") or not re.match(r"^[A-Za-z0-9_./@~^{}-]+$", cand_sha)):
                cand_sha = None
            if cand_sha:
                check_proc = subprocess.run(
                    ["git", "rev-parse", "--verify", f"{cand_sha}^{{commit}}"],
                    cwd=str(git_root),
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if check_proc.returncode == 0 and check_proc.stdout.strip():
                    verified_sha = check_proc.stdout.strip()

        if not verified_sha:
            log_proc = subprocess.run(
                ["git", "log", "-n", "30", "--format=%H|%s"],
                cwd=str(git_root),
                capture_output=True,
                text=True,
                timeout=5,
            )
            if log_proc.returncode == 0 and log_proc.stdout.strip():
                lines = [ln.strip() for ln in log_proc.stdout.strip().splitlines() if ln.strip()]
                for line in lines:
                    parts = line.split("|", 1)
                    c_hash = parts[0].strip()
                    c_subject = parts[1].strip() if len(parts) > 1 else ""

                    if c_hash.lower() in reviewed_commits:
                        continue

                    matches_ticket = False
                    if numeric_id is not None:
                        pat = rf"(?i)\b(?:ticket|issue|feat|fix|refactor|test)?[^a-zA-Z0-9]*(?:0*{numeric_id}|{re.escape(canonical_id)})\b"
                        if re.search(pat, c_subject):
                            matches_ticket = True
                    if not matches_ticket and (f"#{canonical_id}" in c_subject or f"({canonical_id})" in c_subject or canonical_id in c_subject.split()):
                        matches_ticket = True
                    if not matches_ticket and scenario_tags:
                        for tag in scenario_tags:
                            if tag.lower() in c_subject.lower():
                                matches_ticket = True
                                break

                    if not matches_ticket:
                        mentions_other_ticket = False
                        other_m = re.search(r"(?i)\b(?:ticket|issue|feat|fix)[^a-zA-Z0-9]*([0-9]{1,3})\b", c_subject)
                        if other_m:
                            other_id = int(other_m.group(1))
                            if numeric_id is None or other_id != numeric_id:
                                mentions_other_ticket = True

                        if not mentions_other_ticket and associated_paths:
                            diff_proc = subprocess.run(
                                ["git", "show", "--name-only", "--format=", c_hash],
                                cwd=str(git_root),
                                capture_output=True,
                                text=True,
                                timeout=5,
                            )
                            if diff_proc.returncode == 0 and diff_proc.stdout.strip():
                                touched_files = [
                                    tf.strip().replace("\\", "/")
                                    for tf in diff_proc.stdout.strip().splitlines()
                                    if tf.strip()
                                ]
                                for assoc in associated_paths:
                                    if any(assoc in tf or tf in assoc for tf in touched_files):
                                        matches_ticket = True
                                        break

                    if matches_ticket:
                        verified_sha = c_hash
                        break

    if not verified_sha:
        warning_msg = (
            f"⚠️ Rejection Alert: No physical git commit evidence was found on HEAD for ticket '{canonical_id}'. "
            f"The completion claim is rejected; ticket status remains unmutated."
        )
        return ReconciliationResult(1, warning_msg, ticket_id=canonical_id, is_rejected=True)

    curr_status = validator._extract_ticket_status_from_text(ticket_content)
    is_frontmatter_resolved = validator._is_status_resolved(curr_status)

    task_graph_candidates = [
        feature_dir / "task-graph.md",
        issues_dir / "task-graph.md",
        search_dir / "task-graph.md",
        search_dir / ".scratch" / feature_dir.name / "task-graph.md",
        REPO_ROOT / ".scratch" / feature_dir.name / "task-graph.md",
    ]
    tg_file = next((c for c in task_graph_candidates if c.is_file()), None)

    is_tg_resolved = False
    if tg_file:
        try:
            tg_ticks = parse_task_graph_tickets(tg_file)
            for t in tg_ticks:
                t_id = str(t.get("id", "")).strip().lstrip("#")
                if t_id.isdigit():
                    t_id = f"{int(t_id):02d}"
                if t_id == canonical_id:
                    st = str(t.get("status", "")).strip()
                    if validator._is_status_resolved(st):
                        is_tg_resolved = True
                    break
        except Exception:
            pass

    needs_repair = (not is_frontmatter_resolved) or (not is_tg_resolved)

    if needs_repair:
        orig_status = curr_status
        try:
            validator.update_ticket_status(ticket_path, "resolved")
        except OSError as exc:
            diag_msg = (
                f"🚨 [ERR-RECON-03] IO_WRITE_ERROR: Filesystem write failure updating ticket frontmatter at '{ticket_path}'.\n"
                f"System Error: {exc}\n"
                f"Preserved Git Commit: '{verified_sha}'\n"
                f"Ticket ID: '{canonical_id}'\n\n"
                f"Actionable Recovery Instructions: verify write permissions and retry ./scripts/resolve-ticket.sh {canonical_id} {verified_sha}"
            )
            return ReconciliationResult(1, diag_msg, ticket_id=canonical_id, is_rejected=True)

        dag_complete = is_dag_completed(issues_dir, validator=validator)
        seen_tg: set[Path] = set()
        for tg_cand in task_graph_candidates:
            if tg_cand.is_file():
                res_tg = tg_cand.resolve()
                if res_tg not in seen_tg:
                    seen_tg.add(res_tg)
                    tg_res = safe_task_graph_write(
                        res_tg,
                        canonical_id,
                        "✅ resolved",
                        dag_complete=dag_complete,
                        commit_sha=verified_sha,
                        validator=validator,
                    )
                    if not tg_res.ok:
                        if not is_frontmatter_resolved:
                            validator.update_ticket_status(ticket_path, orig_status)
                        return ReconciliationResult(
                            1,
                            tg_res.message,
                            ticket_id=canonical_id,
                            is_rejected=True,
                        )

        _record_verified_commit_in_anchor(
            validator,
            feature_dir,
            verified_sha,
            active_phase="code-review" if dag_complete else None,
        )

        state_dir = feature_dir if (feature_dir / "execution-state.json").is_file() else issues_dir
        _mark_execution_state_completed(state_dir)
        reconciled = True
    else:
        dag_complete = is_dag_completed(issues_dir, validator=validator)
        _record_verified_commit_in_anchor(
            validator,
            feature_dir,
            verified_sha,
            active_phase="code-review" if dag_complete else None,
        )
        if dag_complete:
            seen_tg = set()
            for tg_cand in task_graph_candidates:
                if tg_cand.is_file():
                    res_tg = tg_cand.resolve()
                    if res_tg not in seen_tg:
                        seen_tg.add(res_tg)
                        tg_res = safe_task_graph_write(
                            res_tg,
                            canonical_id,
                            "✅ resolved",
                            dag_complete=True,
                            commit_sha=verified_sha,
                            validator=validator,
                        )
                        if not tg_res.ok:
                            return ReconciliationResult(
                                1,
                                tg_res.message,
                                ticket_id=canonical_id,
                                is_rejected=True,
                            )
        reconciled = False

    scanned_tickets = validator.scan_tickets_from_tracker(issues_dir)
    unblocked_frontier = validator.calculate_unblocked_frontier(scanned_tickets)

    if lean_stream:
        tg_cand_stream = tg_file or (feature_dir / "task-graph.md")
        msg = format_lean_progress_stream(
            ticket_id=canonical_id,
            commit_sha=verified_sha,
            task_graph_path=tg_cand_stream,
            feature_dir=feature_dir,
            validator=validator,
        )
    else:
        if reconciled:
            msg = (
                f"✅ [RECON-01] Reconciled ticket '{canonical_id}' on disk (commit: '{verified_sha}'). "
                f"DAG re-evaluated: newly unblocked frontier: {unblocked_frontier or 'None (All resolved)'}."
            )
        else:
            msg = (
                f"✅ [RECON-01] Verified ticket '{canonical_id}' on Git HEAD (commit: '{verified_sha}'). "
                f"DAG re-evaluated: active frontier: {unblocked_frontier or 'None (All resolved)'}."
            )

        if lean_summary:
            tg_target = tg_file or (feature_dir / "task-graph.md")
            completion_extra = format_operator_handoff(
                issues_dir,
                validator=validator,
                task_graph_path=tg_target,
                lean_summary=True,
                dag_complete=dag_complete,
            )
            if completion_extra:
                msg = f"{msg}\n\n{completion_extra}"

    return ReconciliationResult(
        0,
        msg,
        reconciled=reconciled,
        verified_commit=verified_sha,
        unblocked_frontier=unblocked_frontier,
        ticket_id=canonical_id,
        is_rejected=False,
    )


def _parse_batch_ticket_refs(ticket_input: str | Path | Sequence[str | Path]) -> list[str]:
    """
    [EDGE-RECON-01] [EDGE-RECON-02] Parses declared ticket references from diverse operator input formats:
    - Lists/tuples of ticket IDs or file paths (e.g. ['02', '03'], [Path('02-service.md'), Path('03-ui.md')])
    - Comma/semicolon delimited strings (e.g. '02, 03', '02,03', '01; 02')
    - Natural language phrases (e.g. 'tiket 02 dan 03 selesai', 'ticket 01 selesai', 'tiket 02, 03 dan 04 selesai')
    """
    if isinstance(ticket_input, (list, tuple, set)):
        refs: list[str] = []
        for item in ticket_input:
            refs.extend(_parse_batch_ticket_refs(item))
        return refs

    text = str(ticket_input).strip()
    if not text:
        return []

    p = Path(text)
    if p.is_file():
        return [text]

    if "/" in text or "\\" in text:
        return [part.strip() for part in re.split(r"[,;]+", text) if part.strip()]

    tokens = re.findall(r"(?:ticket-|issue-|#)?\b\d{1,3}(?:-[a-zA-Z0-9_-]+\.md)?\b", text, re.IGNORECASE)
    if tokens:
        return tokens

    if "," in text or ";" in text:
        return [part.strip() for part in re.split(r"[,;]+", text) if part.strip()]

    return [part.strip() for part in text.split() if part.strip()]


def batch_reconcile_tickets(
    ticket_refs: str | Path | Sequence[str | Path],
    root_dir: Path | str | None = None,
    tracker_dir: Path | str | None = None,
    commit_shas: dict[str, str] | Sequence[str] | None = None,
    branch: str = "",
    app_data_dir: Path | str | None = None,
    lean_summary: bool = False,
    git_dir: Path | str | None = None,
    lean_stream: bool = False,
    emit_briefs: bool = True,
) -> BatchReconciliationResult:
    """
    [EDGE-RECON-01] [EDGE-RECON-02] Batch reconciliation engine for concurrent multi-ticket reports
    and idempotent handling of duplicate completion report-backs.
    - Iterates through each declared ticket in topological dependency order ([EDGE-RECON-01]).
    - Independently verifies Git commit evidence and frontmatter for each ticket ([EDGE-RECON-01]).
    - Synchronizes task-graph.md for all verified tickets in a single batched reconciliation turn before emitting downstream frontier briefs ([EDGE-RECON-01]).
    - Idempotently handles duplicate resolution report-backs without duplicate audit logs or resetting downstream frontier progress ([EDGE-RECON-02]).
    - Re-emits current active unblocked frontier briefs to keep the operator workflow moving forward without error ([EDGE-RECON-02]).
    """
    raw_refs = _parse_batch_ticket_refs(ticket_refs)
    if not raw_refs:
        return BatchReconciliationResult(
            1,
            "Error: No valid ticket references found for batch reconciliation.",
            is_rejected=True,
        )

    validator = ManifestValidator()

    if tracker_dir and str(tracker_dir).strip():
        search_dir = Path(tracker_dir).resolve()
    elif root_dir and str(root_dir).strip():
        search_dir = Path(root_dir).resolve()
    else:
        cwd = Path.cwd().resolve()
        if (cwd / ".scratch").is_dir() or (cwd / "issues").is_dir() or cwd.name == "issues":
            search_dir = cwd
        elif (REPO_ROOT / ".scratch").is_dir() or (REPO_ROOT / "issues").is_dir():
            search_dir = REPO_ROOT
        else:
            search_dir = cwd

    if not search_dir.exists():
        return BatchReconciliationResult(
            1,
            f"Error: Search directory does not exist: {search_dir}",
            is_rejected=True,
        )

    # [EDGE-RECON-01] Resolve ticket paths, canonical IDs, and pre-bind commit SHAs
    resolved_candidates: list[tuple[str, str, Path, str | None]] = []
    seen_ids: set[str] = set()
    issues_dir: Path | None = None
    feature_dir: Path | None = None

    for idx, ref in enumerate(raw_refs):
        clean_ref = str(ref).strip()
        try:
            t_path = validator.resolve_ticket_shorthand(search_dir, clean_ref)
        except Exception as exc:
            return BatchReconciliationResult(
                1,
                f"Error resolving ticket shorthand '{clean_ref}': {exc}",
                is_rejected=True,
            )

        m = re.match(r"^(?:ticket-|issue-|#)?([0-9]{1,3})", t_path.stem)
        canonical_id = f"{int(m.group(1)):02d}" if m else t_path.stem

        c_sha: str | None = None
        if isinstance(commit_shas, dict):
            c_sha = (
                commit_shas.get(canonical_id)
                or commit_shas.get(clean_ref)
                or (commit_shas.get(str(int(canonical_id))) if canonical_id.isdigit() else None)
            )
        elif isinstance(commit_shas, (list, tuple)) and idx < len(commit_shas):
            c_sha = str(commit_shas[idx]).strip() or None

        if canonical_id not in seen_ids:
            seen_ids.add(canonical_id)
            resolved_candidates.append((canonical_id, clean_ref, t_path, c_sha))

        if issues_dir is None:
            issues_dir = t_path.parent
            feature_dir = issues_dir.parent if issues_dir.name == "issues" else issues_dir

    if not issues_dir or not feature_dir:
        issues_dir = search_dir / "issues" if (search_dir / "issues").is_dir() else search_dir
        feature_dir = issues_dir.parent if issues_dir.name == "issues" else issues_dir

    # [EDGE-RECON-01] Build dependency graph to compute topological dependency order
    scanned_all = validator.scan_tickets_from_tracker(issues_dir)
    deps_map: dict[str, set[str]] = {}
    for t in scanned_all:
        tid = str(t.get("id", "")).strip().lstrip("#")
        if tid.lower().startswith("ticket-"):
            tid = tid[7:].strip()
        if tid.isdigit():
            tid = f"{int(tid):02d}"

        raw_blockers = t.get("blockers") or t.get("blocked_by") or []
        blocker_ids = set(validator._parse_blocker_ids(raw_blockers))
        deps_map[tid] = blocker_ids

    transitive_deps: dict[str, set[str]] = {tid: set(b_set) for tid, b_set in deps_map.items()}
    changed = True
    while changed:
        changed = False
        for tid, b_set in list(transitive_deps.items()):
            new_set = set(b_set)
            for b in b_set:
                if b in transitive_deps:
                    new_set.update(transitive_deps[b])
            if len(new_set) > len(b_set):
                transitive_deps[tid] = new_set
                changed = True

    in_degree: dict[str, int] = {}
    candidate_map: dict[str, tuple[str, str, Path, str | None]] = {}
    declared_ids = [c[0] for c in resolved_candidates]

    for cid, ref, p, c_sha in resolved_candidates:
        candidate_map[cid] = (cid, ref, p, c_sha)
        dep_count = sum(1 for other_id in declared_ids if other_id != cid and other_id in transitive_deps.get(cid, set()))
        in_degree[cid] = dep_count

    queue = [cid for cid in declared_ids if in_degree[cid] == 0]
    queue.sort(key=lambda x: int(x) if x.isdigit() else x)

    ordered_declared_ids: list[str] = []
    while queue:
        curr_id = queue.pop(0)
        ordered_declared_ids.append(curr_id)
        for other_id in declared_ids:
            if other_id not in ordered_declared_ids and curr_id in transitive_deps.get(other_id, set()):
                in_degree[other_id] -= 1
                if in_degree[other_id] == 0:
                    queue.append(other_id)
                    queue.sort(key=lambda x: int(x) if x.isdigit() else x)

    for cid in declared_ids:
        if cid not in ordered_declared_ids:
            ordered_declared_ids.append(cid)

    ordered_declared = [candidate_map[cid] for cid in ordered_declared_ids]

    # [EDGE-RECON-01] Iterate through declared tickets in dependency order with pre-bound commit evidence
    results: list[ReconciliationResult] = []
    reconciled_tickets: list[str] = []
    already_resolved_tickets: list[str] = []
    verified_commits: dict[str, str] = {}
    has_failure = False

    for cid, ref, t_path, c_sha in ordered_declared:
        res = verify_commit_and_reconcile(
            ticket_ref=ref,
            root_dir=root_dir,
            tracker_dir=tracker_dir,
            commit_sha=c_sha,
            branch=branch,
            app_data_dir=app_data_dir,
            lean_summary=False,
            git_dir=git_dir,
            lean_stream=False,
        )
        results.append(res)

        if res.exit_code != 0:
            has_failure = True
        else:
            if res.reconciled:
                reconciled_tickets.append(cid)
            else:
                already_resolved_tickets.append(cid)
            if res.verified_commit:
                verified_commits[cid] = res.verified_commit

    # [EDGE-RECON-01] Single batched reconciliation sync: evaluate final DAG state & unblocked frontier
    scanned_final = validator.scan_tickets_from_tracker(issues_dir)
    unblocked_frontier = validator.calculate_unblocked_frontier(scanned_final)
    dag_complete = is_dag_completed(issues_dir, validator=validator)

    if dag_complete and reconciled_tickets:
        task_graph_candidates = [
            feature_dir / "task-graph.md",
            issues_dir / "task-graph.md",
            search_dir / "task-graph.md",
            search_dir / ".scratch" / feature_dir.name / "task-graph.md",
            REPO_ROOT / ".scratch" / feature_dir.name / "task-graph.md",
        ]
        seen_tg: set[Path] = set()
        for tg_cand in task_graph_candidates:
            if tg_cand.is_file():
                res_tg = tg_cand.resolve()
                if res_tg not in seen_tg:
                    seen_tg.add(res_tg)
                    safe_task_graph_write(
                        res_tg,
                        reconciled_tickets[-1],
                        "✅ resolved",
                        dag_complete=True,
                        commit_sha=verified_commits.get(reconciled_tickets[-1]),
                        validator=validator,
                    )

    # [EDGE-RECON-01] [EDGE-RECON-02] Downstream frontier starter brief generation
    briefs_output = ""
    if emit_briefs:
        if dag_complete:
            briefs_output = format_operator_handoff(
                issues_dir,
                validator=validator,
                dag_complete=True,
                lean_summary=lean_summary,
            )
        elif unblocked_frontier:
            briefs_output = format_worker_brief(
                issues_dir,
                root_dir=root_dir,
                validator=validator,
            )
        else:
            briefs_output = "> ⏳ Standby: All active frontier tickets are currently in-progress or no unblocked tickets remain."

    if not reconciled_tickets and already_resolved_tickets and not has_failure:
        already_str = ", ".join(f"'{cid}'" for cid in already_resolved_tickets)
        msg = (
            f"ℹ️ [EDGE-RECON-02] Ticket {already_str} is already marked resolved with existing commit evidence in task-graph.md. "
            f"Idempotent no-op executed. Active frontier: {unblocked_frontier or 'None (All resolved)'}."
        )
    elif has_failure:
        err_msgs = [f"Ticket '{r.ticket_id}': {r.message}" for r in results if r.exit_code != 0]
        msg = "⚠️ [EDGE-RECON-01] Partial failure during batched reconciliation:\n" + "\n".join(err_msgs)
    else:
        rec_str = ", ".join(f"'{tid}'" for tid in reconciled_tickets)
        msg = (
            f"✅ [EDGE-RECON-01] Batched reconciliation completed for tickets: {rec_str}. "
            f"DAG re-evaluated: active frontier: {unblocked_frontier or 'None (All resolved)'}."
        )
        if already_resolved_tickets:
            already_str = ", ".join(f"'{cid}'" for cid in already_resolved_tickets)
            msg += f" (Already resolved: {already_str})."

    if lean_stream:
        stream_lines: list[str] = []
        for cid in reconciled_tickets:
            c_sha = verified_commits.get(cid, "-")
            stream_lines.append(f"✓ {cid} ({c_sha[:7] if c_sha != '-' else '-'})")
        frontier_str = ", ".join(unblocked_frontier) if unblocked_frontier else "None"
        msg = f"Reconciled: {' '.join(stream_lines)}\nFrontier: {frontier_str}"
    elif lean_summary:
        completion_extra = format_operator_handoff(
            issues_dir,
            validator=validator,
            lean_summary=True,
            dag_complete=dag_complete,
        )
        if completion_extra:
            msg = f"{msg}\n\n{completion_extra}"

    exit_code = 1 if has_failure else 0
    return BatchReconciliationResult(
        exit_code,
        msg,
        results=results,
        reconciled_tickets=reconciled_tickets,
        unblocked_frontier=unblocked_frontier,
        briefs=briefs_output,
        is_rejected=has_failure,
    )


def _collect_cli_tokens(raw_arg: Any, args: argparse.Namespace) -> list[str]:
    """
    Consolidates positional and optional CLI token arguments into a unified list of strings.
    Handles raw argument sequences, ticket IDs, commit SHAs, and extra ticket arguments.
    """
    tokens: list[str] = []
    if isinstance(raw_arg, list):
        tokens.extend(str(t) for t in raw_arg)
    elif raw_arg is not None and not isinstance(raw_arg, bool) and str(raw_arg).strip():
        tokens.append(str(raw_arg).strip())

    if getattr(args, "ticket_id", ""):
        tokens.append(str(args.ticket_id))
    if getattr(args, "commit_sha", ""):
        tokens.append(str(args.commit_sha))
    if getattr(args, "extra_tickets", None):
        tokens.extend(str(t) for t in args.extra_tickets)
    return tokens


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="resolve-ticket.sh",
        description="Atomic Ticket Resolution CLI Helper",
    )
    parser.add_argument("ticket_id", nargs="?", default="", help="Ticket identifier (e.g. '01', '#01', '01-name.md')")
    parser.add_argument("commit_sha", nargs="?", default="", help="Git commit SHA of the verified slice")
    parser.add_argument("extra_tickets", nargs="*", default=[], help="Additional ticket identifiers for batch reconciliation")
    parser.add_argument("--summary", default="", help="Optional summary of completed work")
    parser.add_argument("--branch", default="", help="Active git branch (auto-detected if omitted)")
    parser.add_argument("--root", default="", help="Repository or workspace root directory")
    parser.add_argument("--tracker-dir", default="", help="Explicit path to issues tracker directory")
    parser.add_argument("--lean", "--lean-summary", dest="lean_summary", action="store_true", help="Format handoff under Lean 3-Line Summary Protocol")
    parser.add_argument("--stream", "--lean-stream", dest="lean_stream", action="store_true", help="Format handoff under Lean 2-Line Progress Stream Protocol ([LEAN-01])")
    parser.add_argument("--auto", action="store_true", help="Autonomous advancement mode ([CHECK-02])")
    parser.add_argument("--step", action="store_true", help="Single-step ticket execution mode ([CHECK-03])")
    parser.add_argument("--reconcile", action="store_true", help="Reconcile ticket status against Git commit evidence on HEAD ([RECON-01])")
    parser.add_argument("--batch", dest="batch_mode", action="store_true", help="Batch reconciliation of tickets in Command Center mode ([EDGE-RECON-01], [EDGE-SNTZ-01])")
    parser.add_argument("--brief", action="store_true", help="Generate self-contained worker starter brief(s) for active frontier tickets ([BRIEF-01], [BRIEF-02], [EDGE-BRIEF-01])")
    parser.add_argument("--emit-briefs", dest="emit_briefs", action="store_true", help="Emit downstream frontier starter briefs (Command Center mode)")
    parser.add_argument("--final-frontier-brief", "--final-frontier-worker-brief", dest="final_frontier_brief", action="store_true", help="Generate starter brief for final frontier ticket with completion reminders ([EDGE-BRIEF-01])")
    parser.add_argument("--eval-brief", dest="eval_brief", nargs="?", const="code-review", default=None, help="Generate contextual evaluation starter brief for fresh evaluation tab ([EVAL-01], [EVAL-02])")
    parser.add_argument("--circuit-breaker", "--eval-circuit-breaker", dest="circuit_breaker", nargs="*", default=None, help="Evaluate evaluation remediation circuit breaker ([ERR-EVAL-01])")
    parser.add_argument("--fix-loop-count", "--loop-count", dest="fix_loop_count", type=int, default=None, help="Fix loop iteration count for circuit breaker ([ERR-EVAL-01])")
    parser.add_argument("--doc-diff", "--eval-doc-diff", dest="eval_doc_diff", nargs="*", default=None, help="Evaluate documentation-only diff heuristic ([EDGE-EVAL-01])")
    parser.add_argument("--dashboard", "--render-dashboard", dest="render_dashboard", action="store_true", help="Render Command Center visual task dashboard and contextual stream ([DASH-01], [DASH-02])")
    parser.add_argument("--recover-dashboard", "--heal-dashboard", dest="recover_dashboard", action="store_true", help="Self-heal corrupted task-graph.md directly from issues/ ([ERR-DASH-01])")
    parser.add_argument("--validate-flags-only", action="store_true", help="Validate CLI flags mutual exclusion and exit")
    parser.add_argument("--validate-phase-prerequisites", "--check-phase", dest="validate_phase", nargs="?", const="security-audit", default=None, help="Validate phase boundary preconditions ([ERR-EXCL-02])")
    parser.add_argument("--validate-concurrency", "--check-concurrency", dest="validate_concurrency", nargs="?", const="", default=None, help="Validate ticket concurrency protection ([ERR-EXCL-03])")
    parser.add_argument("--caller-phase", dest="caller_phase", default=None, help="Calling evaluation phase for brief context ([ERR-EVAL-02])")
    parser.add_argument("--reject-eval-pr-prompt", dest="reject_eval_pr_prompt", action="store_true", help="Reject evaluation tab PR prompt emission ([ERR-EVAL-02])")
    parser.add_argument("--eval-completion-report", dest="eval_completion_report", default=None, help="Format evaluation phase completion report ([ERR-EVAL-02])")
    parser.add_argument("--artifact-path", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--check-dag-only", action="store_true", help="Inspect DAG completion status without resolving")
    parser.add_argument("--claim", action="store_true", help="Claim a ticket atomically (mark in-progress on disk)")
    parser.add_argument("--unclaim", action="store_true", help="Release a ticket claim atomically (reset to ready-for-agent on disk)")
    parser.add_argument("--reset", action="store_true", dest="unclaim", help="Alias for --unclaim")
    parser.add_argument("--validate-template", dest="validate_template", action="store_true", help="Validate canonical task graph template contract schema integrity ([TMPL-01], [ERR-TMPL-CORRUPT])")
    parser.add_argument("--client-path", default="", help=argparse.SUPPRESS)
    parser.add_argument("--timeout", type=float, default=15.0, help=argparse.SUPPRESS)
    parser.add_argument("--current-conversation-id", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--app-data-dir", default=None, help=argparse.SUPPRESS)

    args = parser.parse_args(argv)

    flag_res = validate_cli_flags(args)
    if not flag_res.is_valid:
        sys.stderr.write(flag_res.diagnostic + "\n")
        return flag_res.exit_code

    if args.validate_flags_only:
        sys.stdout.write(flag_res.diagnostic + "\n")
        return flag_res.exit_code

    validator = ManifestValidator()
    root_arg = Path(args.root) if args.root else None
    tracker_arg = Path(args.tracker_dir) if args.tracker_dir else None

    if getattr(args, "validate_template", False):
        target_root = tracker_arg or root_arg or REPO_ROOT
        is_ok, tmpl_errors = validate_task_graph_template_contract(root_dir=target_root)
        if is_ok:
            sys.stdout.write("✅ [TMPL-01] Canonical task graph template adheres to Dual-Layer Cockpit contract.\n")
            return 0
        else:
            for err in tmpl_errors:
                sys.stderr.write(f"❌ {err}\n")
            return 1

    if getattr(args, "recover_dashboard", False):
        search_target = args.ticket_id or tracker_arg or root_arg or REPO_ROOT
        try:
            rec_res = recover_malformed_dashboard(search_target, root_dir=root_arg, validator=validator)
            sys.stdout.write(rec_res.diagnostic + "\n\n" + rec_res.artifact + "\n")
            return 0
        except DagCycleError as exc:
            sys.stderr.write(str(exc) + "\n")
            return 1
        except ScaffoldEmptyError as exc:
            sys.stderr.write(str(exc) + "\n")
            return 1

    if getattr(args, "render_dashboard", False):
        search_target = args.ticket_id or tracker_arg or root_arg or REPO_ROOT
        try:
            dash_res = render_task_dashboard(
                search_target,
                root_dir=root_arg,
                validator=validator,
                lean=True,
            )
            sys.stdout.write(dash_res.artifact + "\n\n" + dash_res.chat_stream + "\n")
            return 0
        except DagCycleError as exc:
            sys.stderr.write(str(exc) + "\n")
            return 1
        except ScaffoldEmptyError as exc:
            sys.stderr.write(str(exc) + "\n")
            return 1

    if args.check_dag_only:
        search_target = tracker_arg or root_arg or REPO_ROOT
        try:
            all_resolved = is_dag_completed(search_target, validator=validator)
            status_label = "all_resolved" if all_resolved else "unresolved_tickets_remain"
            sys.stdout.write(f"DAG Status: {status_label}\n")
            return 0 if all_resolved else 1
        except Exception as exc:
            sys.stderr.write(f"Error checking DAG completion: {exc}\n")
            return 2

    if getattr(args, "brief", False):
        search_target = args.ticket_id or tracker_arg or root_arg or REPO_ROOT
        brief_output = format_worker_brief(
            search_target,
            root_dir=root_arg,
            validator=validator,
            lean=bool(getattr(args, "lean_summary", False)),
        )
        sys.stdout.write(brief_output + "\n")
        return 0

    if getattr(args, "final_frontier_brief", False):
        search_target = args.ticket_id or tracker_arg or root_arg or REPO_ROOT
        brief_output = format_final_frontier_brief(
            search_target,
            root_dir=root_arg,
            validator=validator,
            lean=bool(getattr(args, "lean_summary", False)),
        )
        sys.stdout.write(brief_output + "\n")
        return 0

    if getattr(args, "reject_eval_pr_prompt", False):
        search_target = args.ticket_id or tracker_arg or root_arg or REPO_ROOT
        caller_p = getattr(args, "caller_phase", None) or "code-review"
        rej_res = reject_evaluation_tab_pr_prompt(
            caller_phase=caller_p,
            feature_dir=search_target,
            root_dir=root_arg,
        )
        sys.stdout.write(rej_res.diagnostic + "\n")
        return 0

    if getattr(args, "eval_completion_report", None):
        search_target = args.ticket_id or tracker_arg or root_arg or REPO_ROOT
        rep_output = format_evaluation_completion_report(
            args.eval_completion_report,
            verdict=getattr(args, "verdict", "PASS") or "PASS",
            feature_dir_or_target=search_target,
            root_dir=root_arg,
        )
        sys.stdout.write(rep_output + "\n")
        return 0

    if getattr(args, "eval_brief", None):
        eval_phase = args.eval_brief
        search_target = args.ticket_id or tracker_arg or root_arg or REPO_ROOT
        clean_eval_type = str(eval_phase).strip().strip("/").strip().lower().replace("_", "-")
        is_path_target = False
        p_cand = Path(eval_phase)
        if p_cand.exists():
            is_path_target = True
        elif root_arg and (Path(root_arg) / eval_phase).exists():
            is_path_target = True
        elif (REPO_ROOT / eval_phase).exists():
            is_path_target = True
        elif (Path(".scratch") / eval_phase).exists():
            is_path_target = True

        if clean_eval_type not in VALID_EVAL_PHASES and is_path_target:
            search_target = eval_phase
            eval_phase = "code-review"
        brief_output = format_evaluation_brief(
            eval_phase,
            search_target,
            root_dir=root_arg,
            validator=validator,
            caller_phase=getattr(args, "caller_phase", None),
            lean=True,
        )
        sys.stdout.write(brief_output + "\n")
        return 0

    if getattr(args, "eval_doc_diff", None) is not None:
        all_diff_tokens = _collect_cli_tokens(args.eval_doc_diff, args)
        diff_target = all_diff_tokens[0] if all_diff_tokens else "main...HEAD"
        search_target = tracker_arg or root_arg or REPO_ROOT
        doc_res = evaluate_documentation_diff(
            diff_target,
            root_dir=root_arg,
            feature_dir=search_target,
        )
        sys.stdout.write(doc_res.format_notice() + "\n")
        return 0

    if getattr(args, "validate_phase", None) is not None:
        raw_phase = args.validate_phase or (args.ticket_id if args.ticket_id and not args.ticket_id.isdigit() else "security-audit")
        search_target = tracker_arg or root_arg or REPO_ROOT
        phase_res = _validate_phase_prerequisites(
            raw_phase,
            feature_dir=search_target,
            root_dir=root_arg,
            tracker_dir=tracker_arg,
            validator=validator,
        )
        if phase_res.is_valid:
            sys.stdout.write(phase_res.diagnostic + "\n")
            return 0
        else:
            sys.stderr.write(phase_res.diagnostic + "\n")
            return 1

    if getattr(args, "validate_concurrency", None) is not None:
        target_ref = args.validate_concurrency or args.ticket_id
        if not target_ref:
            parser.print_usage(sys.stderr)
            sys.stderr.write("Error: <ticket-id> is required for --validate-concurrency.\n")
            return 1
        search_target = tracker_arg or root_arg or REPO_ROOT
        conc_res = _validate_ticket_concurrency(
            target_ref,
            feature_dir=search_target,
            root_dir=root_arg,
            tracker_dir=tracker_arg,
            validator=validator,
        )
        if conc_res.is_valid:
            sys.stdout.write(conc_res.diagnostic + "\n")
            return 0
        else:
            sys.stderr.write(conc_res.diagnostic + "\n")
            return 1

    if getattr(args, "circuit_breaker", None) is not None:
        raw_cb = args.circuit_breaker
        cb_type = "code-review"
        cb_count = args.fix_loop_count if getattr(args, "fix_loop_count", None) is not None else 0

        all_tokens = _collect_cli_tokens(raw_cb, args)

        for tok in all_tokens:
            s_tok = str(tok).strip()
            if s_tok.isdigit():
                cb_count = int(s_tok)
            elif any(k in s_tok.lower() for k in ("security", "audit")):
                cb_type = "security-audit"
            elif any(k in s_tok.lower() for k in ("code", "review")):
                cb_type = "code-review"

        search_target = tracker_arg or root_arg or REPO_ROOT
        cb_res = evaluate_circuit_breaker(
            cb_type,
            fix_loop_count=cb_count,
            task_graph_path=getattr(args, "artifact_path", None),
            feature_dir=search_target,
            root_dir=root_arg,
        )
        sys.stdout.write(cb_res.format_notice() + "\n")
        return 0

    if args.claim:
        if not args.ticket_id:
            parser.print_usage(sys.stderr)
            sys.stderr.write("Error: <ticket-id> is required for --claim.\n")
            return 1
        res = claim_ticket(
            ticket_ref=args.ticket_id,
            root_dir=root_arg,
            tracker_dir=tracker_arg,
            branch=args.branch,
        )
        if res.exit_code == 0:
            sys.stdout.write(res.message + "\n")
        else:
            sys.stderr.write(res.message + "\n")
        return res.exit_code

    if getattr(args, "unclaim", False):
        if not args.ticket_id:
            parser.print_usage(sys.stderr)
            sys.stderr.write("Error: <ticket-id> is required for --unclaim.\n")
            return 1
        res = unclaim_ticket(
            ticket_ref=args.ticket_id,
            root_dir=root_arg,
            tracker_dir=tracker_arg,
        )
        if res.exit_code == 0:
            sys.stdout.write(res.message + "\n")
        else:
            sys.stderr.write(res.message + "\n")
        return res.exit_code

    is_batch_or_reconcile = args.reconcile or getattr(args, "batch_mode", False)
    if is_batch_or_reconcile:
        cmd_name = "--batch" if getattr(args, "batch_mode", False) else "--reconcile"
        if not args.ticket_id:
            parser.print_usage(sys.stderr)
            sys.stderr.write(f"Error: <ticket-id> is required for {cmd_name}.\n")
            return 1

        raw_candidates = [args.ticket_id]
        explicit_sha: str | None = None
        if args.commit_sha:
            sha_cand = str(args.commit_sha).strip()
            if getattr(args, "extra_tickets", []) or re.match(r"^(?:ticket-|issue-|#)?[0-9]{1,3}(?:-[a-zA-Z0-9_-]+\.md)?$", sha_cand, re.IGNORECASE):
                raw_candidates.append(sha_cand)
            else:
                explicit_sha = sha_cand

        if getattr(args, "extra_tickets", []):
            raw_candidates.extend(args.extra_tickets)

        parsed_tickets: list[str] = []
        for cand in raw_candidates:
            parsed_tickets.extend(_parse_batch_ticket_refs(cand))

        # [EDGE-SNTZ-01] Command Center mode (--batch, or --reconcile multi-ticket, or --emit-briefs):
        # Preserve full frontier visibility and downstream starter brief emission
        is_batch_mode = getattr(args, "batch_mode", False) or len(parsed_tickets) > 1
        if is_batch_mode:
            batch_res = batch_reconcile_tickets(
                ticket_refs=parsed_tickets,
                root_dir=root_arg,
                tracker_dir=tracker_arg,
                commit_shas={parsed_tickets[0]: explicit_sha} if (explicit_sha and len(parsed_tickets) == 1) else None,
                branch=args.branch,
                lean_summary=args.lean_summary,
                app_data_dir=args.app_data_dir,
                lean_stream=getattr(args, "lean_stream", False),
                emit_briefs=True,
            )
            if batch_res.exit_code == 0:
                if batch_res.briefs and not getattr(args, "lean_stream", False):
                    out_msg = f"{batch_res.message}\n\n{batch_res.briefs}"
                else:
                    out_msg = batch_res.message
                sys.stdout.write(out_msg + "\n")
            else:
                sys.stderr.write(batch_res.message + "\n")
            return batch_res.exit_code
        else:
            single_ref = parsed_tickets[0] if parsed_tickets else args.ticket_id
            recon_res = verify_commit_and_reconcile(
                ticket_ref=single_ref,
                root_dir=root_arg,
                tracker_dir=tracker_arg,
                commit_sha=explicit_sha,
                branch=args.branch,
                lean_summary=args.lean_summary,
                app_data_dir=args.app_data_dir,
                lean_stream=getattr(args, "lean_stream", False),
            )
            if recon_res.exit_code == 0:
                recon_out = recon_res.message
                if getattr(args, "emit_briefs", False) and not getattr(args, "lean_stream", False):
                    target_issues = tracker_arg or root_arg or REPO_ROOT
                    dag_done = is_dag_completed(target_issues, validator=validator)
                    if dag_done:
                        recon_briefs = format_operator_handoff(target_issues, validator=validator, dag_complete=True)
                    elif recon_res.unblocked_frontier:
                        recon_briefs = format_worker_brief(target_issues, root_dir=root_arg, validator=validator)
                    else:
                        recon_briefs = "> ⏳ Standby: All active frontier tickets are currently in-progress or no unblocked tickets remain."
                    if recon_briefs:
                        recon_out = f"{recon_out}\n\n{recon_briefs}"
                sys.stdout.write(recon_out + "\n")
            else:
                sys.stderr.write(recon_res.message + "\n")
            return recon_res.exit_code

    if not args.ticket_id or not args.commit_sha:
        parser.print_usage(sys.stderr)
        sys.stderr.write("Error: Both <ticket-id> and <commit-sha> are required arguments.\n")
        return 1

    exit_code, output_msg = resolve_ticket_atomic(
        ticket_ref=args.ticket_id,
        commit_sha=args.commit_sha,
        root_dir=root_arg,
        tracker_dir=tracker_arg,
        summary=args.summary,
        branch=args.branch,
        lean_summary=args.lean_summary,
        artifact_path=args.artifact_path,
        lean_stream=args.lean_stream,
        app_data_dir=args.app_data_dir,
        emit_briefs=getattr(args, "emit_briefs", False),
    )

    if exit_code == 0:
        sys.stdout.write(output_msg + "\n")
    else:
        sys.stderr.write(output_msg + "\n")

    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
