# Antigravity Multi-Account Switcher (`agy-switch`)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python: 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Zero Dependencies](https://img.shields.io/badge/dependencies-0-brightgreen.svg)](#zero-dependencies--privacy-guarantees)

`agy-switch` is a zero-dependency, high-reliability account manager, credential vault, and quota inspector for **Google Antigravity 2.0** (Desktop Electron / WSL Remote-Control) and **Antigravity CLI** (`agy`).

It completely supersedes legacy token managers with an isolated local vault, atomic dual-destination sync, active-turn process guards, interactive sandboxed onboarding, intelligent auto-rotation heuristics, and real-time quota decision ranking.

---

## Zero-Dependencies & Privacy Guarantees

`agy-switch` is engineered from the ground up for absolute confidentiality, operational safety, and zero external baggage:

- **Zero External Dependencies**: Built strictly on the Python 3.10+ standard library and standard POSIX bash. No external pip packages, no npm modules, no virtual environment overhead, and no background daemon managers.
- **100% Local Offline Vault (`~/.antigravity-vault/`)**: All credentials and configuration reside strictly on your local machine. Directory permissions are locked to `0700` and credential JSON files to `0600` (`chmod` enforced at every access).
- **Absolute Privacy & Zero Telemetry**:
  - **No Telemetry or Tracking**: `agy-switch` never phones home or collects analytics.
  - **Direct Google Authentication Only**: Zero cloud token intermediaries or proxies. OAuth credentials communicate directly with official Google endpoints solely when refreshing tokens or probing quota limits.
- **Atomic Credential Swaps**: Credential propagation uses POSIX atomic filesystem replacement (`os.replace`), preventing partial writes, race conditions, or token corruption between Antigravity 2.0 Desktop and the Antigravity CLI.
- **Process Turn Safety Guard**: The process supervisor monitors running `language_server` instances and active agent turns before triggering server restarts, ensuring in-flight agent tasks are never prematurely killed or corrupted.

---

## Key Features

1. **Secure Isolated Vault (`~/.antigravity-vault/`)**:
   - Vault directories locked down with `0700` permissions.
   - Token credentials stored per-account (`accounts/<name>/token.json`) with strict `0600` file modes.
   - Idempotent auto-migration from legacy stores (`~/.agy-cli-manager/`) with zero data destruction.

2. **Atomic Dual-Destination Emission**:
   - Updates both Antigravity credentials simultaneously in a single atomic filesystem swap (`os.replace`):
     - `~/.gemini/jetski-standalone-oauth-token` (Antigravity 2.0 Desktop app)
     - `~/.gemini/antigravity-cli/antigravity-oauth-token` (Antigravity CLI)

3. **Process Supervisor & Turn Safety Guard**:
   - Gracefully stops `language_server` via `SIGTERM` so WSL Relay automatically respawns it with new credentials.
   - Inspects process tree and network sockets to detect active agent turns.
   - Blocks accidental server restarts with exit code `2` unless explicitly overridden via `--force`.

4. **Interactive Sandbox Login (`login`)**:
   - Spawns Google OAuth browser authentication in an isolated temporary `$HOME`.
   - Never alters active credentials during login.
   - Validates JWT tokens, extracts email identity, guards against collisions (`[y/N]`), and saves new accounts in `Standby` status.

5. **Real-Time Quota Overview & Decision Ranking (`quota`)**:
   - Inspects real-time quotas (Gemini & Claude/GPT 5-Hour and Weekly limits) across all vault accounts via fast direct telemetry (`QuotaClient`).
   - Deterministic sorting: Highest Gemini Weekly Limit followed by highest Gemini 5-Hour Limit.
   - Computes reset times and countdowns (e.g. `~3 jam 20 menit lagi`).
   - Actionable recommendation banner highlighting the highest-capacity standby account.
   - Persistent cache fallback (`~/.antigravity-vault/cache/last_known_quota.json`) when network probes temporarily timeout.

6. **Automatic Rotation Engine & PreInvocation Hook (`hook`)**:
   - Evaluates remaining model quota before each agent turn via Antigravity lifecycle hooks (`PreInvocation`).
   - Supports intelligent rotation strategies (`cache_optimized`, `drain_window`, `auto_highest`).
   - Seamlessly auto-switches to the healthiest account when capacity drops below threshold.

---

## Installation & Setup

`agy-switch` uses a unified installer script (`install.sh`) that installs the Antigravity Booster Plugin and registers the standalone terminal CLI wrapper (`~/.local/bin/agy-switch`) simultaneously in a single step.

### Quick Start (Remote Bootstrap One-Liner)

For fresh VPS instances, Linux hosts, and terminal environments, install directly using the remote bootstrap one-liner:

```bash
curl -fsSL https://raw.githubusercontent.com/satriyabajuhitam/agy-switcher/dev/install.sh | bash
```

**What the bootstrap one-liner does automatically:**
- **Clones into `~/agy-switcher`**: Clones the repository into `$HOME/agy-switcher` tracking the active `dev` branch.
- **Installs Global Plugin**: Registers the booster plugin globally in `~/.gemini/config/plugins/agy-switch` so all workspaces and AI agent sessions have immediate access.
- **Configures CLI Launcher**: Symlinks `~/.local/bin/agy-switch` directly to `scripts/agy_switch.py`.
- **Vault Protection**: Leaves existing credentials in `~/.antigravity-vault/` untouched; strict `0700` directory and `0600` file permissions are enforced by `agy-switch` at runtime.
- **Idempotent Updates**: Re-running the command safely synchronizes updates (`git pull --ff-only`) while protecting uncommitted working tree changes and preserving existing vault credentials.

#### Passing Installer Flags via Pipe
To pass flags (such as `--help` or `--copy`) directly through the remote pipeline, pass `-s --` to Bash:

```bash
# View usage help without cloning
curl -fsSL https://raw.githubusercontent.com/satriyabajuhitam/agy-switcher/dev/install.sh | bash -s -- --help
```

#### Optional Environment Variable Overrides
You can customize the destination directory, git branch, or repository URL by setting environment variables before piping to `bash`:

```bash
# Example: Custom directory and branch
curl -fsSL https://raw.githubusercontent.com/satriyabajuhitam/agy-switcher/dev/install.sh | AGY_SWITCH_HOME="$HOME/tools/agy-switcher" AGY_SWITCH_BRANCH="dev" bash
```

| Variable | Default Value | Description |
| :--- | :--- | :--- |
| `AGY_SWITCH_HOME` | `$HOME/agy-switcher` | Destination directory for the cloned repository |
| `AGY_SWITCH_BRANCH` | `dev` | Git branch to clone and synchronize |
| `AGY_SWITCH_REPO_URL` | `https://github.com/satriyabajuhitam/agy-switcher.git` | Upstream Git repository URL |

---

### Manual Clone Installation (Alternative)

If you prefer to clone manually or are already inside a cloned repository:

```bash
# Clone the repository (active dev branch)
git clone -b dev https://github.com/satriyabajuhitam/agy-switcher.git
cd agy-switcher

# Interactive Menu (Recommended for manual installation)
bash install.sh
```

### Scripted Installation Options

When running directly from a cloned repository:

```bash
# Option A: Local Workspace Installation (Default for workspace-isolated agents)
# Installs plugin to .agents/plugins/agy-switch and links CLI to ~/.local/bin/agy-switch
bash install.sh --local

# Option B: Global Installation (Available to all workspaces and user sessions)
# Installs plugin to ~/.gemini/config/plugins/agy-switch and links CLI to ~/.local/bin/agy-switch
bash install.sh --global

# Optional: Physical Copy Mode (copies files instead of symlinking)
bash install.sh --local --copy
# or
bash install.sh --global --copy
```

> [!NOTE]
> Ensure `~/.local/bin` is present in your shell `$PATH` (e.g. `export PATH="$HOME/.local/bin:$PATH"` in `~/.bashrc` or `~/.zshrc`).

### What `install.sh` Configures
- **Plugin Registry**: Registers `agy-switch` as an active Antigravity booster plugin (`.agents/plugins/agy-switch` or `~/.gemini/config/plugins/agy-switch`).
- **CLI Launcher**: Symlinks `~/.local/bin/agy-switch` directly to `scripts/agy_switch.py`.
- **Vault Management**: Secure vault directories (`~/.antigravity-vault/`) and `0700`/`0600` permissions are managed lazily at runtime by `scripts/agy_switch.py`, keeping installation zero-touch for existing vaults.

---

## Clean Uninstallation

`install.sh` provides dedicated uninstallation flags to cleanly remove registered plugins and CLI symlinks without leaving orphaned files:

```bash
# Uninstall local workspace plugin and launcher
bash install.sh --uninstall --local

# Uninstall global user plugin and launcher
bash install.sh --uninstall --global

# Uninstall all (cleans up both local and global installations)
bash install.sh --uninstall --all
# or simply:
bash install.sh --uninstall
```

> [!TIP]
> **Credential Preservation**: Uninstallation intentionally leaves `~/.antigravity-vault/` untouched so your configured accounts and OAuth tokens are never lost accidentally. If you wish to completely purge all local tokens, delete the vault directory manually:
> ```bash
> rm -rf ~/.antigravity-vault
> ```

---

## CLI Reference

| Command | Arguments / Flags | Description |
| :--- | :--- | :--- |
| `agy-switch list` | `[--quota] [--json] [--format table\|list]` | List registered accounts and current active account |
| `agy-switch status` | None | Show active account, `language_server` PID, and server busy state |
| `agy-switch quota` | `[--format table\|list] [--json] [--timeout <sec>]` | Inspect real-time quota usage across all accounts |
| `agy-switch switch <name>` | `[--force] [--no-restart]` | Switch active account and restart daemon safely |
| `agy-switch login <name>` | `[--force] [--timeout <sec>]` | Interactive Google OAuth login in an isolated sandbox |
| `agy-switch add <name>` | `--token-file <path>` | Register account from an existing token JSON file |
| `agy-switch remove <name>` | `[--force]` | Remove an account from vault (`--force` if active) |
| `agy-switch config` | `[auto-switch <on\|off>]` | View or configure automatic rotation settings |
| `agy-switch hook` | `[install\|uninstall]` | Manage Antigravity PreInvocation lifecycle hooks |
| `agy-switch migrate` | None | Manually trigger import from legacy `~/.agy-cli-manager` |

---

## Usage Examples

### 1. Inspect Quotas Across All Accounts
```bash
# Terminal table view
agy-switch quota

# Mobile / chat friendly listicle
agy-switch quota --format list

# Machine-readable JSON
agy-switch quota --json
```

### 2. Switch Active Account
```bash
# Graceful switch (blocks if language_server is busy)
agy-switch switch credible

# Force switch even during active turn
agy-switch switch credible --force

# Update token files only without restarting server
agy-switch switch credible --no-restart
```

### 3. Log In a New Account
```bash
# Launches an isolated browser session to authenticate
agy-switch login my-new-account
```

### 4. Enable Automatic Rotation & Lifecycle Hooks
```bash
# Register PreInvocation lifecycle hook
agy-switch hook install

# Turn on auto-switch rotation
agy-switch config auto-switch on
```

---

## Agent Skill Integration

This plugin includes an Antigravity Agent Skill at:
`skills/agy-switch/SKILL.md`

Autonomous pair-programming agents can inspect multi-account capacity and execute account switches upon user request. When communicating quota status in chat, agents format outputs as numbered listicles (`agy-switch quota --format list`) to avoid destructive table wrapping on mobile viewports.

---

## Running Automated Tests

Run the full unit test suite:

```bash
python3 -m unittest discover tests/
```

Verify switcher installation:

```bash
bash install.sh --local
```

---

## License

Distributed under the [MIT License](LICENSE).
