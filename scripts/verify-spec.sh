#!/usr/bin/env bash
# scripts/verify-spec.sh
# Zero-dependency entrypoint for the Living Specifications & Brownfield Delta Engine.
# Parses, validates, and reconciles Living and Delta Specifications using Python 3 stdlib.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

if ! command -v python3 >/dev/null 2>&1; then
    echo "Error: python3 is required to execute the specification engine." >&2
    exit 1
fi

exec python3 "${REPO_ROOT}/scripts/internal/verify_spec.py" "$@"
