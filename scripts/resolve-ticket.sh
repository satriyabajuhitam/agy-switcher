#!/usr/bin/env bash
# ==============================================================================
# scripts/resolve-ticket.sh
# Atomic Ticket Resolution CLI Helper
# ==============================================================================
# Coordinates ticket resolution arguments, on-disk metadata updates,
# and session anchor commit tracking.
# Implements [RES-01], [RES-02], and [PRUNE-01].
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

find_scratch_root() {
    local cur="$(pwd)"
    while [ "$cur" != "/" ] && [ -n "$cur" ]; do
        if [ -d "$cur/.scratch" ]; then
            echo "$cur"
            return 0
        fi
        cur="$(dirname "$cur")"
    done
    if [ -d "${SCRIPT_DIR}/../../../../.scratch" ]; then
        (cd "${SCRIPT_DIR}/../../../.." && pwd)
        return 0
    fi
    return 1
}

REPO_ROOT="$(find_scratch_root 2>/dev/null || git rev-parse --show-toplevel 2>/dev/null || (cd "${SCRIPT_DIR}/.." && pwd))"

if ! command -v python3 >/dev/null 2>&1; then
    echo "Error: python3 is required for resolve-ticket.sh." >&2
    exit 1
fi

show_usage() {
    cat << 'USAGE_EOF'
Usage: ./scripts/resolve-ticket.sh <ticket-id> <commit-sha> [OPTIONS]

Atomic Ticket Resolution CLI Helper.

Arguments:
  <ticket-id>                 Ticket identifier (e.g. "01", "#01", "ticket-01", "01-name.md")
  <commit-sha>                Git commit SHA associated with the verified slice

Options:
  --summary <text>            Optional 1-sentence summary of the completed work
  --branch <name>             Active git branch name (default: auto-detected)
  --root <path>               Repository or workspace root directory (default: auto-detected)
  --tracker-dir <path>        Explicit path to issues tracker directory
  --lean, --lean-summary      Format handoff under Lean 3-Line Summary Protocol
  --stream, --lean-stream     Format handoff under Lean 2-Line Progress Stream Protocol ([LEAN-01])
  --claim                     Claim a ticket atomically (mark in-progress on disk)
  --reconcile                 Reconcile ticket status against Git commit evidence on HEAD ([RECON-01])
  --batch                     Batch reconciliation of tickets in Command Center mode ([EDGE-RECON-01], [EDGE-SNTZ-01])
  --emit-briefs               Emit downstream frontier starter briefs (Command Center mode)
  --brief                     Generate self-contained worker starter brief(s) ([BRIEF-01], [BRIEF-02], [EDGE-BRIEF-01])
  --final-frontier-brief      Generate starter brief for final frontier ticket with completion reminders ([EDGE-BRIEF-01])
  --final-frontier-worker-brief Generate starter brief for final frontier ticket with completion reminders ([EDGE-BRIEF-01])
  --eval-brief=<phase>        Generate contextual evaluation brief (/code-review, /security-audit, /pr) ([EVAL-01], [EVAL-02])
  --circuit-breaker           Evaluate evaluation remediation circuit breaker threshold ([ERR-EVAL-01])
  --eval-doc-diff, --doc-diff Evaluate git diff for documentation-only fast path ([EDGE-EVAL-01])
  --dashboard                 Render visual task dashboard artifact and contextual chat stream ([DASH-01], [DASH-02])
  --validate-phase-prerequisites <phase> Validate phase boundary preconditions ([ERR-EXCL-02])
  --validate-concurrency <ticket-id>     Validate ticket concurrency protection ([ERR-EXCL-03])
  --check-dag-only            Inspect DAG completion status without resolving
  --auto                      Full autonomous pipeline advancement ([CHECK-02])
  --step                      Single-step ticket execution mode ([CHECK-03])
  -h, --help                  Show this usage guide and exit
USAGE_EOF
}

check_dag_completion() {
    local args=()
    local has_flag=false
    for a in "$@"; do
        if [[ "$a" == "--check-dag-only" ]]; then
            has_flag=true
        fi
        args+=("$a")
    done
    if [[ "$has_flag" == "false" ]]; then
        args=(--check-dag-only "${args[@]}")
    fi
    python3 "${SCRIPT_DIR}/internal/resolve_ticket.py" "${args[@]}"
}

main() {
    for arg in "$@"; do
        if [[ "$arg" == "-h" || "$arg" == "--help" ]]; then
            show_usage
            exit 0
        fi
    done

    FORWARD_ARGS=("$@")
    HAS_ROOT=false
    for arg in "$@"; do
        if [[ "$arg" == "--root" || "$arg" == --root=* ]]; then
            HAS_ROOT=true
            break
        fi
    done

    if [[ "$HAS_ROOT" == "false" ]]; then
        FORWARD_ARGS+=(--root "${REPO_ROOT}")
    fi

    for arg in "$@"; do
        case "$arg" in
            --check-dag-only)
                check_dag_completion "${FORWARD_ARGS[@]}"
                exit $?
                ;;
            --claim | --reconcile | --batch | --batch=* | --emit-briefs | --brief | --final-frontier-brief | --final-frontier-worker-brief | \
            --eval-brief | --eval-brief=* | \
            --reject-eval-pr-prompt | --eval-completion-report | --eval-completion-report=* | \
            --circuit-breaker | --circuit-breaker=* | --eval-circuit-breaker | --eval-circuit-breaker=* | \
            --doc-diff | --doc-diff=* | --eval-doc-diff | --eval-doc-diff=* | \
            --dashboard | --render-dashboard | --recover-dashboard | --heal-dashboard | \
            --validate-phase-prerequisites | --validate-phase-prerequisites=* | --check-phase | --check-phase=* | \
            --validate-concurrency | --validate-concurrency=* | --check-concurrency | --check-concurrency=* | \
            --validate-flags-only)
                exec python3 "${SCRIPT_DIR}/internal/resolve_ticket.py" "${FORWARD_ARGS[@]}"
                ;;
        esac
    done

    if [[ $# -lt 2 ]]; then
        echo "Error: Both <ticket-id> and <commit-sha> are required arguments." >&2
        show_usage >&2
        exit 1
    fi

    exec python3 "${SCRIPT_DIR}/internal/resolve_ticket.py" "${FORWARD_ARGS[@]}"
}

main "$@"
