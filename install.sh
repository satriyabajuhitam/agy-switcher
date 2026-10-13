#!/bin/bash
# install.sh - Antigravity Multi-Account Switcher (agy-switch) Installer
# Provides local and global installation of the agy-switch booster plugin.

set -e

# ---------------------------------------------------------
# Execution Mode Detection (Local Mode vs Bootstrap Mode)
# ---------------------------------------------------------
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ] && [ -f "$(dirname "${BASH_SOURCE[0]}")/scripts/agy_switch.py" ] && [ -f "$(dirname "${BASH_SOURCE[0]}")/plugin.json" ]; then
    IS_BOOTSTRAP=0
else
    IS_BOOTSTRAP=1
fi

if [ "$IS_BOOTSTRAP" -eq 1 ]; then
    echo "==> agy-switch Remote Bootstrap Installer"
    export GIT_TERMINAL_PROMPT=0

    if ! command -v git >/dev/null 2>&1; then
        echo "Error: git is required for remote installation." >&2
        exit 1
    fi

    TARGET_DIR="${AGY_SWITCH_HOME:-$HOME/agy-switcher}"
    REPO_URL="${AGY_SWITCH_REPO_URL:-https://github.com/satriyabajuhitam/agy-switcher.git}"
    BRANCH="${AGY_SWITCH_BRANCH:-dev}"

    safe_cleanup_target() {
        if [ -n "$TARGET_DIR" ] && [ "$TARGET_DIR" != "/" ] && [ "$TARGET_DIR" != "$HOME" ] && [ -d "$TARGET_DIR" ]; then
            rm -rf "$TARGET_DIR"
        fi
    }

    if [ ! -e "$TARGET_DIR" ] || ( [ -d "$TARGET_DIR" ] && [ -z "$(ls -A "$TARGET_DIR" 2>/dev/null)" ] ); then
        trap 'echo "Error: Installation interrupted or failed. Cleaning up incomplete clone..." >&2; safe_cleanup_target; exit 1' INT TERM

        git clone --single-branch -b "$BRANCH" "$REPO_URL" "$TARGET_DIR" || {
            echo "Error: Failed to clone repository from $REPO_URL" >&2
            safe_cleanup_target
            exit 1
        }
        trap - INT TERM
    fi

    has_target=0
    has_action=0
    for arg in "$@"; do
        case "$arg" in
            -l|--local|-g|--global|-a|--all)
                has_target=1
                ;;
            -u|--uninstall|-h|--help)
                has_action=1
                ;;
        esac
    done

    if [ "$has_target" -eq 1 ] || [ "$has_action" -eq 1 ]; then
        exec bash "$TARGET_DIR/install.sh" "$@"
    else
        exec bash "$TARGET_DIR/install.sh" --global "$@"
    fi
fi

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
  -l, --local           Install or uninstall for current workspace (.agents/plugins/agy-switch)
  -g, --global          Install or uninstall for user home (~/.gemini/config/plugins/agy-switch)
  -a, --all             Uninstall both local and global installations (with --uninstall)
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
  5) Uninstall All (Local & Global)
  6) Exit
EOF
}

interactive_menu() {
    echo "agy-switch Installer Menu"
    echo "1) Install Locally (Workspace: .agents/plugins/agy-switch)"
    echo "2) Install Globally (User Home: ~/.gemini/config/plugins/agy-switch)"
    echo "3) Uninstall Local Installation"
    echo "4) Uninstall Global Installation"
    echo "5) Uninstall All (Local & Global)"
    echo "6) Exit"
    printf "Select an option [1-6]: "
    read -r choice || exit 0
    case "$choice" in
        1) TARGET="local";;
        2) TARGET="global";;
        3) TARGET="local"; UNINSTALL=1;;
        4) TARGET="global"; UNINSTALL=1;;
        5) TARGET="all"; UNINSTALL=1;;
        6) exit 0;;
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
    while [ "$#" -gt 0 ]; do
        case $1 in
            -l|--local) TARGET="local" ;;
            -g|--global) TARGET="global" ;;
            -a|--all) TARGET="all" ;;
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
    if [ "$UNINSTALL" -eq 1 ]; then
        # Default uninstall without explicit target cleans up both to prevent orphaned files
        TARGET="all"
    else
        echo "Error: Must specify --local or --global (or use interactive menu)."
        exit 1
    fi
fi

get_local_dest() {
    # Find workspace root
    local root_dir
    root_dir="$(git rev-parse --show-toplevel 2>/dev/null || true)"
    if [ -z "$root_dir" ]; then
        root_dir="$(pwd)"
    fi
    echo "$root_dir/.agents/plugins/agy-switch"
}

get_global_dest() {
    echo "${HOME}/.gemini/config/plugins/agy-switch"
}

matches_dest() {
    local path="$1"
    local prefix="$2"
    [ -n "$prefix" ] && case "$path" in "$prefix"*) return 0 ;; esac
    return 1
}

matches_agy() {
    local path="$1"
    case "$path" in
        *"/agy-switch/"*|*"/agy_switch.py"*) return 0 ;;
        *) return 1 ;;
    esac
}

manage_launcher() {
    local action="$1"
    local target_dest="${2:-}"
    local bin_dir="${HOME}/.local/bin"
    local bin_link="$bin_dir/agy-switch"

    if [ "$action" = "install" ]; then
        mkdir -p "$bin_dir"
        local script_target="$target_dest/scripts/agy_switch.py"
        # Ensure executable
        chmod +x "$script_target" 2>/dev/null || true
        
        # Link
        ln -sf "$script_target" "$bin_link"
        echo "Created launcher symlink: $bin_link -> $script_target"
    elif [ "$action" = "uninstall" ]; then
        if [ -L "$bin_link" ] || [ -e "$bin_link" ]; then
            local current_target
            current_target="$(readlink "$bin_link" 2>/dev/null || true)"
            local canonical_target
            canonical_target="$(readlink -f "$bin_link" 2>/dev/null || true)"

            local should_remove=0
            if matches_dest "$current_target" "$target_dest" || matches_dest "$canonical_target" "$target_dest"; then
                should_remove=1
            fi

            if [ "$TARGET" = "all" ]; then
                if matches_agy "$current_target" || matches_agy "$canonical_target" || matches_dest "$canonical_target" "$BOOSTER_ROOT"; then
                    should_remove=1
                fi
            fi

            # Clean up broken symlink pointing to agy-switch
            if [ -L "$bin_link" ] && [ ! -e "$bin_link" ]; then
                if matches_agy "$current_target"; then
                    should_remove=1
                fi
            fi

            if [ "$should_remove" -eq 1 ]; then
                rm -f "$bin_link"
                echo "Removed launcher symlink: $bin_link"
            fi
        fi
    fi
}

uninstall_target() {
    local dest="$1"
    echo "Uninstalling agy-switch from $dest"
    manage_launcher "uninstall" "$dest"

    if [ -L "$dest" ] || [ -d "$dest" ]; then
        rm -rf "$dest"
        echo "Removed plugin at $dest"
    else
        echo "Plugin not found at $dest, nothing to do."
    fi
}

install_target() {
    local dest="$1"
    echo "Installing agy-switch to $dest (mode: $INSTALL_MODE)"

    # Clean up existing before installing
    if [ -L "$dest" ] || [ -d "$dest" ]; then
        rm -rf "$dest"
    fi
    mkdir -p "$(dirname "$dest")"

    # Ensure source script is executable
    chmod +x "$BOOSTER_ROOT/scripts/agy_switch.py"

    if [ "$INSTALL_MODE" = "symlink" ]; then
        ln -sf "$BOOSTER_ROOT" "$dest"
        echo "Symlinked: $dest"
    else
        mkdir -p "$dest"
        cp -r "$BOOSTER_ROOT/." "$dest/"
        echo "Copied to: $dest"
    fi

    manage_launcher "install" "$dest"
    echo "Installation complete."
}

if [ "$UNINSTALL" -eq 1 ]; then
    if [ "$TARGET" = "local" ]; then
        uninstall_target "$(get_local_dest)"
    elif [ "$TARGET" = "global" ]; then
        uninstall_target "$(get_global_dest)"
    elif [ "$TARGET" = "all" ]; then
        uninstall_target "$(get_local_dest)"
        uninstall_target "$(get_global_dest)"
        # Final pass to ensure launcher is gone if TARGET was all
        manage_launcher "uninstall" ""
    fi
    echo "Uninstall complete. (Note: ~/.antigravity-vault/ is intentionally kept untouched)."
else
    if [ "$TARGET" = "local" ]; then
        install_target "$(get_local_dest)"
    elif [ "$TARGET" = "global" ]; then
        install_target "$(get_global_dest)"
    fi
fi
