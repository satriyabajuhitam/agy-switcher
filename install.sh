#!/bin/bash
# install.sh - Antigravity Multi-Account Switcher (agy-switch) Installer
# Provides local and global installation of the agy-switch booster plugin.

set -e

# Default settings
INSTALL_MODE="symlink"
TARGET=""
UNINSTALL=0
INTERACTIVE=0

# Determine script directory robustly
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOOSTER_ROOT="$SCRIPT_DIR"

usage() {
    cat <<EOF
Usage:
  bash install.sh [OPTIONS]

Options:
  -l, --local           Install to current workspace (.agents/plugins/agy-switch)
  -g, --global          Install to global Antigravity config (~/.gemini/config/plugins/agy-switch)
  -c, --copy            Copy files physically instead of symlinking (default is symlink)
  -s, --symlink         Explicitly force symlink installation (default behavior)
  -u, --uninstall       Remove installed plugin and clean up launcher
  -h, --help            Show this usage information

Interactive Mode:
  When run with no options and stdin is an interactive TTY, display:
  1) Install Locally (Workspace: .agents/plugins/agy-switch)
  2) Install Globally (User Home: ~/.gemini/config/plugins/agy-switch)
  3) Uninstall Local Installation
  4) Uninstall Global Installation
  5) Exit
EOF
}

interactive_menu() {
    echo "agy-switch Installer Menu"
    echo "1) Install Locally (Workspace: .agents/plugins/agy-switch)"
    echo "2) Install Globally (User Home: ~/.gemini/config/plugins/agy-switch)"
    echo "3) Uninstall Local Installation"
    echo "4) Uninstall Global Installation"
    echo "5) Exit"
    read -p "Select an option [1-5]: " choice
    case "$choice" in
        1) TARGET="local";;
        2) TARGET="global";;
        3) TARGET="local"; UNINSTALL=1;;
        4) TARGET="global"; UNINSTALL=1;;
        5) exit 0;;
        *) echo "Invalid option"; exit 1;;
    esac
}

# Parse Arguments
if [ "$#" -eq 0 ]; then
    if [ -t 0 ]; then
        interactive_menu
    else
        usage
        exit 1
    fi
else
    while [[ "$#" -gt 0 ]]; do
        case $1 in
            -l|--local) TARGET="local" ;;
            -g|--global) TARGET="global" ;;
            -c|--copy) INSTALL_MODE="copy" ;;
            -s|--symlink) INSTALL_MODE="symlink" ;;
            -u|--uninstall) UNINSTALL=1 ;;
            -h|--help) usage; exit 0 ;;
            *) echo "Unknown parameter passed: $1"; usage; exit 1 ;;
        esac
        shift
    done
fi

if [ -z "$TARGET" ]; then
    echo "Error: Must specify --local or --global (or use interactive menu)."
    exit 1
fi

get_local_dest() {
    # Find workspace root
    local root_dir
    root_dir="$(git rev-parse --show-toplevel 2>/dev/null || true)"
    if [ -z "$root_dir" ]; then
        root_dir="$(cd "$SCRIPT_DIR/../.." && pwd)"
    fi
    echo "$root_dir/.agents/plugins/agy-switch"
}

get_global_dest() {
    echo "${HOME}/.gemini/config/plugins/agy-switch"
}

manage_launcher() {
    local action="$1"
    local bin_dir="${HOME}/.local/bin"
    local bin_link="$bin_dir/agy-switch"

    if [ "$action" = "install" ]; then
        mkdir -p "$bin_dir"
        local script_target="$DEST/scripts/agy_switch.py"
        # Ensure executable
        chmod +x "$script_target"
        
        # Link
        ln -sf "$script_target" "$bin_link"
        echo "Created launcher symlink: $bin_link -> $script_target"
    elif [ "$action" = "uninstall" ]; then
        if [ -L "$bin_link" ]; then
            local current_target
            current_target="$(readlink "$bin_link")"
            # If the current target starts with our DEST, remove the link
            if [[ "$current_target" == "$DEST"* ]]; then
                rm -f "$bin_link"
                echo "Removed launcher symlink: $bin_link"
            fi
        fi
    fi
}

if [ "$TARGET" = "local" ]; then
    DEST="$(get_local_dest)"
elif [ "$TARGET" = "global" ]; then
    DEST="$(get_global_dest)"
fi

if [ "$UNINSTALL" -eq 1 ]; then
    echo "Uninstalling agy-switch from $DEST"
    manage_launcher "uninstall"
    
    if [ -L "$DEST" ] || [ -d "$DEST" ]; then
        rm -rf "$DEST"
        echo "Removed plugin at $DEST"
    else
        echo "Plugin not found at $DEST, nothing to do."
    fi
    echo "Uninstall complete. (Note: ~/.antigravity-vault/ is intentionally kept untouched)."
else
    echo "Installing agy-switch to $DEST (mode: $INSTALL_MODE)"
    
    # Clean up existing before installing
    if [ -L "$DEST" ] || [ -d "$DEST" ]; then
        rm -rf "$DEST"
    fi
    mkdir -p "$(dirname "$DEST")"

    if [ "$INSTALL_MODE" = "symlink" ]; then
        ln -s "$BOOSTER_ROOT" "$DEST"
        echo "Symlinked: $DEST"
    else
        cp -r "$BOOSTER_ROOT" "$DEST"
        echo "Copied to: $DEST"
    fi

    # Set executable on the script in the source BEFORE copying/linking, 
    # but since it's already there or copied, let's just make it executable in source 
    # so the copied version also has it, or if symlink, it works.
    chmod +x "$BOOSTER_ROOT/scripts/agy_switch.py"

    manage_launcher "install"
    echo "Installation complete."
fi
