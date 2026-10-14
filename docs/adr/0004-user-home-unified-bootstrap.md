# 0004: User Home Unified Bootstrap

## Status
Accepted

## Context
When installing `agy-switch` on a new VPS or host machine, users often encounter ambiguous workspace topologies. In existing setups, `agy-switch` was cloned under arbitrary or deep paths such as `/srv/agy-workspaces/booster/agy-switcher`, causing confusion as to whether such system paths were the tool's hardcoded defaults.

Furthermore, ADR 0003 established `install.sh` as the single entrypoint for distribution, but running `install.sh` previously assumed a pre-cloned local repository on disk. Users desiring a quick-install experience on clean VPS instances need a frictionless one-liner (`curl -fsSL ... | bash`) that installs `agy-switcher` into the user's home directory (`~/agy-switcher`) by default.

## Decision Drivers & Seam Invariants
1. **Predictable Default Location**: The default installation target on VPS and standard Linux/macOS environments must reside in `$HOME/agy-switcher` (`~/agy-switcher`).
2. **Unified Single-File Entrypoint**: Maintain the core principle of ADR 0003—avoid proliferating separate bootstrap scripts (e.g. `scripts/bootstrap.sh`); `install.sh` must serve as both the standalone local installer and the remote pipe-executable bootstrap script.
3. **Zero-Dependency & POSIX Bash**: Strictly standard POSIX bash and Git commands; no external package managers, `curl` wrappers, or third-party utilities.
4. **Idempotency & Non-Destructive Updates**: If `~/agy-switcher` already exists, gracefully detect valid git status, pull latest changes, and proceed without corrupting local vaults or configurations.

## Decision
We adopt **User Home Unified Bootstrap** implemented directly inside `install.sh`:
- **Pipe / Remote Detection**: `install.sh` checks if it is executed without a local repository context (e.g., when piped via `curl -fsSL https://.../install.sh | bash` or when `${BASH_SOURCE[0]}` is not located within a valid `agy-switcher` source tree).
- **Default Clone Target (`~/agy-switcher`)**: When remote/piped execution is detected, the script automatically clones the repository into `${AGY_SWITCH_HOME:-$HOME/agy-switcher}` (defaulting to the active development/release branch).
- **Idempotent Repository Synchronization**: If `$HOME/agy-switcher` already exists and is a valid git repository, it issues a non-destructive `git pull --ff-only` (or fetch/checkout) to bring it up to date.
- **Automatic Global Installation**: After ensuring the local repository in `~/agy-switcher` is prepared, the bootstrap re-executes `$HOME/agy-switcher/install.sh --global`, linking `~/.local/bin/agy-switch` and registering the Antigravity global plugin in `~/.gemini/config/plugins/agy-switch`.
- **Preserve Local Invocation Flexibility**: When executed directly from within an existing cloned repository, `install.sh` retains its existing interactive menu and flag-based workflow (`--local`, `--global`, `--copy`, `--uninstall`).

## Considered Options & Rejected Alternatives
- **Alternative A: Dedicated `scripts/bootstrap.sh` script**: Rejected. Violates ADR 0003's single unified distribution entrypoint and increases maintenance overhead across multiple installer scripts.
- **Alternative B: Hidden XDG Directory (`~/.local/share/agy-switcher`)**: Rejected. Obscures the repository from user view, making manual configuration inspection, branch switching, and local contributions unintuitive for VPS users.
- **Alternative C: Pure physical copy without Git repository**: Rejected. Prevents users from updating via `git pull` and complicates ongoing development and issue diagnosis.

## Consequences & Failure Modes
- **Positive**: Clean VPS setup is reduced to a single standard command:
  ```bash
  curl -fsSL https://raw.githubusercontent.com/satriyabajuhitam/agy-switcher/dev/install.sh | bash
  ```
- **Positive**: Eliminates confusion regarding system-level workspace paths (`/srv/agy-workspaces/...`).
- **Failure Mode (Dirty Worktree)**: If an existing `~/agy-switcher` repository contains local uncommitted modifications, `git pull --ff-only` will safely halt without discarding user changes.
- **Requirement**: The host system must have `git` installed to execute the remote bootstrap clone.
