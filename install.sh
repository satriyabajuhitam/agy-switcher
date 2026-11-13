#!/bin/bash
# install.sh - Antigravity Multi-Account Switcher (agy-switch) Installer
# Provides local and global installation of the agy-switch booster plugin.

set -e

usage() {
    cat <<EOF
Usage:
  bash install.sh [OPTIONS]

Options:
  -l, --local           Install or uninstall for current workspace (.agents/plugins/agy-switch)
  -g, --global          Install or uninstall for user home (~/.gemini/config/plugins/agy-switch)
  -a, --all             Install or uninstall both local and global installations
  -c, --copy            Copy files physically instead of symlinking (default is symlink)
  -s, --symlink         Explicitly force symlink installation (default behavior)
  -u, --uninstall       Remove installed plugin and clean up launcher
  -h, --help            Show this usage information

Environment Variables (Remote Bootstrap):
  AGY_SWITCH_HOME       Destination directory (default: \$HOME/agy-switcher)
  AGY_SWITCH_BRANCH     Git branch to clone (default: main)
  AGY_SWITCH_REPO_URL   Upstream repository URL (default: https://github.com/satriyabajuhitam/agy-switcher.git)

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

# Early check for help options
for arg in "$@"; do
    case "$arg" in
        -h|--help)
            usage
            exit 0
            ;;
    esac
done

# Determine script directory robustly if available
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    BOOSTER_ROOT="$SCRIPT_DIR"
else
    SCRIPT_DIR=""
    BOOSTER_ROOT=""
fi

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

is_foreign_binary() {
    local link="$1"
    # Non-existent paths or symlinks are safe to install/refresh
    if [ ! -e "$link" ] || [ -L "$link" ]; then
        return 1
    fi
    # Non-symlink regular files or foreign executables
    if [ -f "$link" ]; then
        if grep -q "agy_switch" "$link" 2>/dev/null || grep -q "agy-switch" "$link" 2>/dev/null; then
            return 1
        fi
        return 0
    fi
    # Any other non-symlink filesystem object (e.g. dir, socket)
    return 0
}

verify_prerequisites() {
    # 1. Verify git is installed
    if ! command -v git >/dev/null 2>&1; then
        if [ "${IS_BOOTSTRAP:-0}" -eq 1 ]; then
            echo "Error: git is required for remote installation." >&2
        else
            echo "Error: git is required to install agy-switch but is not installed or not in PATH." >&2
            echo "Please install git and try again." >&2
        fi
        return 1
    fi

    # 2. Verify python3 is installed
    if ! command -v python3 >/dev/null 2>&1; then
        echo "Error: python3 (>= 3.10) is required to install agy-switch but is not installed or not in PATH." >&2
        echo "Please install python3 and try again." >&2
        return 1
    fi

    # 3. Verify python3 version >= 3.10
    if ! python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >/dev/null 2>&1; then
        local py_ver
        py_ver="$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')" 2>/dev/null || true)"
        echo "Error: python3 version 3.10 or higher is required to install agy-switch (detected Python ${py_ver:-unknown})." >&2
        echo "Please upgrade python3 and try again." >&2
        return 1
    fi

    # 4. Check for launcher collision (skip if uninstalling)
    local is_uninstall=0
    for arg in "$@"; do
        case "$arg" in
            -u|--uninstall)
                is_uninstall=1
                ;;
        esac
    done

    if [ "$is_uninstall" -eq 0 ]; then
        local bin_dir="${HOME}/.local/bin"
        local bin_link="$bin_dir/agy-switch"
        if is_foreign_binary "$bin_link"; then
            echo "Error: Existing foreign file or executable already exists at '$bin_link' that was not created by agy-switch." >&2
            echo "Refusing to silently overwrite foreign binaries without user confirmation." >&2
            echo "Please backup, move, or remove '$bin_link' and run installer again." >&2
            return 1
        fi
    fi

    return 0
}

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
fi

# Run pre-flight prerequisites and foreign binary collision check
verify_prerequisites "$@" || exit 1

if [ "$IS_BOOTSTRAP" -eq 1 ]; then

    TARGET_DIR="${AGY_SWITCH_HOME:-$HOME/agy-switcher}"
    while [ "$TARGET_DIR" != "/" ] && [ "${TARGET_DIR%/}" != "$TARGET_DIR" ]; do
        TARGET_DIR="${TARGET_DIR%/}"
    done
    REPO_URL="${AGY_SWITCH_REPO_URL:-https://github.com/satriyabajuhitam/agy-switcher.git}"
    BRANCH="${AGY_SWITCH_BRANCH:-main}"

    safe_cleanup_target() {
        local target="${TARGET_DIR}"
        while [ "$target" != "/" ] && [ "${target%/}" != "$target" ]; do
            target="${target%/}"
        done
        [ -z "$target" ] && return 0
        [ "$target" = "/" ] && return 0
        [ "$target" = "$HOME" ] && return 0

        local canonical_target=""
        if command -v realpath >/dev/null 2>&1; then
            canonical_target="$(realpath -m "$target" 2>/dev/null || true)"
        elif command -v readlink >/dev/null 2>&1; then
            canonical_target="$(readlink -m "$target" 2>/dev/null || true)"
        fi
        [ -z "$canonical_target" ] && return 0
        [ "$canonical_target" = "/" ] && return 0

        local canonical_home=""
        if command -v realpath >/dev/null 2>&1; then
            canonical_home="$(realpath -m "$HOME" 2>/dev/null || true)"
        elif command -v readlink >/dev/null 2>&1; then
            canonical_home="$(readlink -m "$HOME" 2>/dev/null || true)"
        else
            canonical_home="${HOME%/}"
        fi

        if [ -n "$canonical_home" ] && [ "$canonical_target" = "$canonical_home" ]; then
            return 0
        fi

        if [ -d "$target" ]; then
            rm -rf "$target"
        fi
    }

    if [ ! -e "$TARGET_DIR" ] || [ -d "$TARGET_DIR" ] && [ -z "$(ls -A "$TARGET_DIR" 2>/dev/null)" ]; then
        trap 'echo "Error: Installation interrupted or failed. Cleaning up incomplete clone..." >&2; safe_cleanup_target; exit 1' INT TERM

        git clone --single-branch -b "$BRANCH" "$REPO_URL" "$TARGET_DIR" || {
            echo "Error: Failed to clone repository from $REPO_URL" >&2
            safe_cleanup_target
            exit 1
        }
        trap - INT TERM
    elif [ ! -e "$TARGET_DIR/.git" ]; then
        echo "Error: Destination directory '$TARGET_DIR' already exists and is not an agy-switch repository." >&2
        exit 1
    elif [ ! -f "$TARGET_DIR/scripts/agy_switch.py" ] || [ ! -f "$TARGET_DIR/plugin.json" ]; then
        echo "Error: Existing git repository at '$TARGET_DIR' is not an agy-switch repository." >&2
        exit 1
    else
        if [ -n "$(git -C "$TARGET_DIR" status --porcelain 2>/dev/null)" ]; then
            echo "Error: Working directory at $TARGET_DIR has uncommitted changes." >&2
            echo "Please commit, stash, or reset your local modifications before running bootstrap." >&2
            exit 1
        fi

        git -C "$TARGET_DIR" fetch origin "$BRANCH" || {
            echo "Error: Failed to fetch branch '$BRANCH' from origin." >&2
            exit 1
        }
        if git -C "$TARGET_DIR" show-ref --verify --quiet "refs/heads/$BRANCH"; then
            git -C "$TARGET_DIR" checkout "$BRANCH" || {
                echo "Error: Failed to checkout branch '$BRANCH'." >&2
                exit 1
            }
            git -C "$TARGET_DIR" merge --ff-only FETCH_HEAD || {
                echo "Error: Cannot fast-forward branch '$BRANCH'. Local branch has diverged." >&2
                exit 1
            }
        else
            git -C "$TARGET_DIR" checkout -b "$BRANCH" FETCH_HEAD || {
                echo "Error: Failed to checkout branch '$BRANCH'." >&2
                exit 1
            }
        fi
    fi

    has_target=0
    has_action=0
    for arg in "$@"; do
        case "$arg" in
            -l|--local|-g|--global|-a|--all)
                has_target=1
                ;;
            -u|--uninstall)
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
        echo "Error: Must specify --local, --global, or --all (or use interactive menu)."
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

manage_launcher() {
    local action="$1"
    local target_dest="${2:-}"
    local bin_dir="${HOME}/.local/bin"
    local bin_link="$bin_dir/agy-switch"

    if [ "$action" = "install" ]; then
        if is_foreign_binary "$bin_link"; then
            echo "Error: Existing foreign file or executable already exists at '$bin_link' that was not created by agy-switch." >&2
            echo "Refusing to silently overwrite foreign binaries without user confirmation." >&2
            echo "Please backup, move, or remove '$bin_link' and run installer again." >&2
            exit 1
        fi
        if [ -e "$bin_link" ] || [ -L "$bin_link" ]; then
            # Unlink verified existing launcher symlink before replacing
            rm -f "$bin_link"
        fi

        mkdir -p "$bin_dir"
        local script_target="$target_dest/scripts/agy_switch.py"
        # Ensure executable
        chmod +x "$script_target" 2>/dev/null || true
        
        # Link
        ln -sf "$script_target" "$bin_link"
        echo "Created launcher symlink: $bin_link -> $script_target"

        # Register PreInvocation hook in ~/.gemini/config/hooks.json for global/all installations
        if [ "$TARGET" = "global" ] || [ "$TARGET" = "all" ] || [ "$target_dest" = "$(get_global_dest)" ]; then
            local hooks_dir="${HOME}/.gemini/config"
            local hooks_file="$hooks_dir/hooks.json"
            python3 -c "
import sys, os
from pathlib import Path

target_dest = sys.argv[1]
hooks_path = Path(sys.argv[2])
hooks_path.parent.mkdir(parents=True, exist_ok=True)

installed = False
try:
    sys.path.insert(0, os.path.join(target_dest, 'scripts'))
    from agy_switcher.vault import AgySwitchEngine
    engine = AgySwitchEngine(gemini_dir=hooks_path.parent.parent)
    engine.install_hook(hooks_file=hooks_path, launcher_bin='~/.local/bin/agy-switch')
    installed = True
except Exception:
    pass

if not installed:
    import json
    existing = {}
    if hooks_path.is_file():
        try:
            content = hooks_path.read_text(encoding='utf-8').strip()
            if content:
                loaded = json.loads(content)
                if isinstance(loaded, dict):
                    existing = loaded
        except Exception:
            existing = {}

    existing['agy-switch-auto-rotate'] = {
        'enabled': True,
        'PreInvocation': [
            {
                'type': 'command',
                'command': '~/.local/bin/agy-switch hook-pre-invocation',
                'timeout': 5,
            }
        ],
    }

    tmp_path = hooks_path.with_name(f'.{hooks_path.name}.tmp.{os.getpid()}')
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(existing, f, indent=2)
        f.write('\n')
    os.replace(tmp_path, hooks_path)
" "$target_dest" "$hooks_file"
            echo "Registered PreInvocation hook: $hooks_file"
        fi
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
    elif [ "$TARGET" = "all" ]; then
        install_target "$(get_local_dest)"
        install_target "$(get_global_dest)"
    fi
fi
