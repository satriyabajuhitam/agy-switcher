#!/usr/bin/env bash
# scripts/verify-build.sh
# 3-Tier Multi-Quality Verification Gate for agy-switch
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd -P)"
LOGICAL_REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ==============================================================================
# Fast-Path Zero-Overhead Workspace Guard (<5ms)
# ==============================================================================
is_agy_switcher_path() {
    local target="$1"
    [ -z "$target" ] && return 1

    # Check 1: Direct match against known REPO_ROOT (realpath or logical)
    if [ "$target" = "$REPO_ROOT" ] || [ "$target" = "$LOGICAL_REPO_ROOT" ]; then
        return 0
    fi
    case "$target" in
        "$REPO_ROOT"/*|"$LOGICAL_REPO_ROOT"/*)
            return 0
            ;;
    esac

    # Check 2: Directory name matching common repo aliases
    local base="${target##*/}"
    if [ "$base" = "agy-switcher" ] || [ "$base" = "agy-switch" ]; then
        return 0
    fi

    # Check 3: If target exists on disk, resolve realpath and check for marker pyproject.toml
    if [ -e "$target" ]; then
        local cur="$target"
        if [ ! -d "$cur" ]; then
            cur="${cur%/*}"
            [ -z "$cur" ] && cur="/"
        fi
        while [ -n "$cur" ] && [ "$cur" != "/" ]; do
            if [ -f "$cur/pyproject.toml" ]; then
                if grep -F -q 'name = "agy-switch"' "$cur/pyproject.toml" 2>/dev/null; then
                    return 0
                fi
                break
            fi
            local parent="${cur%/*}"
            [ -z "$parent" ] && parent="/"
            [ "$parent" = "$cur" ] && break
            cur="$parent"
        done
    fi

    return 1
}

workspace_guard() {
    local STDIN_PAYLOAD=""
    local HAS_STDIN=0

    # Non-blocking read of stdin when piped or redirected
    if [ ! -t 0 ]; then
        read -r -d '' STDIN_PAYLOAD || true
        if [ -n "$STDIN_PAYLOAD" ]; then
            HAS_STDIN=1
        fi
    fi

    local TARGET_IDENTIFIED=0
    local EXPLICIT_FOREIGN=0

    if [ "$HAS_STDIN" -eq 1 ]; then
        local HOOK_CWD=""
        local HAS_CWD=0
        if [[ "$STDIN_PAYLOAD" =~ \"cwd\"[[:space:]]*:[[:space:]]*\"([^\"]+)\" ]]; then
            HOOK_CWD="${BASH_REMATCH[1]}"
            HAS_CWD=1
            if is_agy_switcher_path "$HOOK_CWD"; then
                TARGET_IDENTIFIED=1
            fi
        fi

        local HAS_WP=0
        if [[ "$STDIN_PAYLOAD" =~ \"workspacePaths\"[[:space:]]*:[[:space:]]*\[([^\]]*)\] ]]; then
            HAS_WP=1
            local WP_CONTENT="${BASH_REMATCH[1]}"
            while [[ "$WP_CONTENT" =~ ^[^\"]*\"([^\"]+)\"(.*)$ ]]; do
                local WP_PATH="${BASH_REMATCH[1]}"
                WP_CONTENT="${BASH_REMATCH[2]}"
                if is_agy_switcher_path "$WP_PATH"; then
                    TARGET_IDENTIFIED=1
                    break
                fi
            done
        fi

        # If stdin payload provided explicit cwd or workspacePaths, and none matched agy-switcher:
        if { [ "$HAS_CWD" -eq 1 ] || [ "$HAS_WP" -eq 1 ]; } && [ "$TARGET_IDENTIFIED" -eq 0 ]; then
            EXPLICIT_FOREIGN=1
        fi
    fi

    if [ "$EXPLICIT_FOREIGN" -eq 1 ]; then
        exit 0
    fi

    if [ "$TARGET_IDENTIFIED" -eq 1 ]; then
        return 0
    fi

    # Fall back to inspecting $PWD (current working directory)
    if is_agy_switcher_path "$PWD"; then
        return 0
    fi

    # Outside agy-switcher: terminate cleanly with exit 0
    exit 0
}

workspace_guard

if [ "${AGY_VERIFY_DRY_RUN:-0}" = "1" ]; then
    echo "==> [GUARD_PASSED] Execution permitted for agy-switcher"
    exit 0
fi

cd "${REPO_ROOT}"

echo "==> [1/3] Verifying 5-layer AST modular architecture..."
python3 -m unittest tests/test_modular_architecture.py -q

if [ "${AGY_VERIFY_MAX_TIER:-3}" -eq 1 ]; then
    echo "✅ Tier 1 gate passed cleanly."
    exit 0
fi

echo "==> [2/3] Verifying SDK and daemon static typing (mypy --strict)..."
PYTHONPATH="${REPO_ROOT}/scripts" python3 -m mypy \
    "${REPO_ROOT}/scripts/agy_switcher"

if [ "${AGY_VERIFY_MAX_TIER:-3}" -eq 2 ]; then
    echo "✅ Tier 1 & 2 gates passed cleanly."
    exit 0
fi

echo "==> [3/3] Running full unit test suite..."
AGY_DISABLE_HOST_SYSTEMD=1 PYTHONWARNINGS="ignore::ResourceWarning" python3 -m unittest discover -s "${REPO_ROOT}/tests" -q
echo "✅ All verification gates passed cleanly."
