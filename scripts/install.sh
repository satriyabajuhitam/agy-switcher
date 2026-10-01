#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_PY="${SCRIPT_DIR}/agy_switch.py"
BIN_DIR="${HOME}/.local/bin"
SYMLINK_PATH="${BIN_DIR}/agy-switch"
VAULT_DIR="${HOME}/.antigravity-vault"
ACCOUNTS_DIR="${VAULT_DIR}/accounts"

echo "==> [agy-switch] Initializing Antigravity Switcher Plugin..."

# 1. Ensure agy_switch.py is executable
chmod +x "${TARGET_PY}"

# 2. Ensure ~/.antigravity-vault/accounts exists with strict permissions
mkdir -p "${ACCOUNTS_DIR}"
chmod 0700 "${VAULT_DIR}" "${ACCOUNTS_DIR}"

# 3. Ensure ~/.local/bin exists
mkdir -p "${BIN_DIR}"

# 4. Create or update symlink ~/.local/bin/agy-switch pointing to plugin script
ln -sfn "${TARGET_PY}" "${SYMLINK_PATH}"
echo "==> [agy-switch] Linked launcher: ${SYMLINK_PATH} -> ${TARGET_PY}"

# 5. Execute auto-migration and live token harvesting
python3 "${TARGET_PY}" migrate || true

# 6. Verify installation and print status
echo ""
echo "==> [agy-switch] Current System Status:"
"${SYMLINK_PATH}" status || python3 "${TARGET_PY}" status

# 7. PATH verification
if [[ ":$PATH:" != *":${BIN_DIR}:"* ]]; then
    echo ""
    echo "Notice: ${BIN_DIR} is not in your current shell PATH."
    echo "To access it globally in interactive shells, run:"
    echo '  source ~/.bashrc'
fi

echo ""
echo "==> [agy-switch] Installation complete!"
echo "    Run 'agy-switch list' to view registered accounts."
echo "    Run 'agy-switch switch <name>' to switch accounts."
