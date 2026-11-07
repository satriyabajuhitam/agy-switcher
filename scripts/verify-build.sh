#!/usr/bin/env bash
# scripts/verify-build.sh
# 3-Tier Multi-Quality Verification Gate for agy-switch
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo "==> [1/3] Verifying 5-layer AST modular architecture..."
python3 -m unittest tests/test_modular_architecture.py -q

echo "==> [2/3] Verifying SDK public seam static typing (mypy --strict)..."
PYTHONPATH="${REPO_ROOT}/scripts" python3 -m mypy \
    "${REPO_ROOT}/scripts/agy_switcher/api.py" \
    "${REPO_ROOT}/scripts/agy_switcher/models.py" \
    "${REPO_ROOT}/scripts/agy_switcher/formatters.py"

echo "==> [3/3] Running full unit test suite (897 tests)..."
PYTHONWARNINGS="ignore::ResourceWarning" python3 -m unittest discover -s "${REPO_ROOT}/tests" -q
echo "✅ All verification gates passed cleanly."
