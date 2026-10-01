# Antigravity Multi-Account Switcher (`agy-switch`)

`agy-switch` is a zero-dependency, high-reliability account manager, credential vault, and quota inspector for **Google Antigravity 2.0** (Desktop Electron / WSL Remote-Control) and **Antigravity CLI** (`agy`).

It completely supersedes legacy token managers with an isolated vault, atomic dual-destination sync, active-turn process guards, interactive sandboxed onboarding, and real-time quota decision ranking.

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
   - Inspects real-time quotas (Gemini & Claude/GPT 5-Hour and Weekly limits) across all vault accounts.
   - Deterministic sorting: Highest Gemini Weekly Limit followed by highest Gemini 5-Hour Limit.
   - Computes reset times and countdowns (e.g. `~3 jam 20 menit lagi`).
   - Actionable recommendation banner highlighting the highest-capacity standby account.
   - Persistent cache fallback (`~/.antigravity-vault/cache/last_known_quota.json`) when network probes temporarily timeout.

6. **Zero Dependencies**:
   - Built entirely on Python 3.10+ standard libraries (`pathlib`, `json`, `shutil`, `signal`, `subprocess`, `tempfile`, `argparse`, `re`, `datetime`). Zero `pip install` required.

---

## Installation & Setup

To install the `agy-switch` booster and bootstrap the vault:

```bash
# Interactive menu (recommended for manual installation)
bash booster/agy-switch/install.sh

# Install locally to the current workspace (.agents/plugins/agy-switch)
bash booster/agy-switch/install.sh --local

# Install globally for all workspaces (~/.gemini/config/plugins/agy-switch)
bash booster/agy-switch/install.sh --global

# Physical copy instead of symlink
bash booster/agy-switch/install.sh --local --copy

# Uninstall (safely cleans up links and binaries, keeps vault intact)
bash booster/agy-switch/install.sh --uninstall --local
```

This sets up:
- Directory: `~/.antigravity-vault/accounts/` (`0700`)
- Active marker: `~/.antigravity-vault/active`
- Cache directory: `~/.antigravity-vault/cache/`
- Symlink: `~/.local/bin/agy-switch -> .../agy_switch.py`

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
agy-switch login my-new-account
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
python3 -m unittest discover -s booster/agy-switch/tests -q
```

