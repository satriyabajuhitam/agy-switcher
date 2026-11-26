---
name: agy-switch
description: "Inspect Antigravity multi-account status, check quota capacities, and switch active accounts across Antigravity 2.0 and CLI."
---

# Antigravity Multi-Account Switcher (`agy-switch`)

`agy-switch` is a high-reliability, zero-dependency account manager and token switcher for Google Antigravity 2.0 (Desktop Electron / WSL Remote-Control) and Antigravity CLI (`agy`).

It maintains an isolated, encrypted-style credential vault at `~/.antigravity-vault/` and handles atomic dual-emission to:
- `~/.gemini/jetski-standalone-oauth-token` (Antigravity 2.0 Desktop app)
- `~/.gemini/antigravity-cli/antigravity-oauth-token` (Antigravity CLI)

It also manages graceful `language_server` auto-restart via `SIGTERM` with safety guards that prevent killing the server during active agent turns.

---

## 1. Quick CLI Reference

| Action | Command | Description |
| :--- | :--- | :--- |
| **List Accounts** | `agy-switch list` | Lists all vault accounts and marks the currently active account |
| **List with Quota** | `agy-switch list --quota` | Evaluates all vaulted accounts concurrently for real-time quota (Fast Telemetry) |
| **System Status** | `agy-switch status` | Displays active account, zero-lag cache status, and server health |
| **Switch Account** | `agy-switch switch <name>` | Atomically updates tokens and restarts `language_server` safely |
| **Force Switch** | `agy-switch switch <name> --force` | Overrides active turn guard; queues deferred safe-turn reload when invoked in-turn |
| **Auto Switch** | `agy-switch switch --auto` | Intelligently rotates account (uses deferred safe-turn reload when invoked in-turn) |
| **Login Account** | `agy-switch login <name>` | Interactive Google OAuth login in an isolated sandbox |
| **Add Account** | `agy-switch add <name> --token-file <path>` | Registers account from existing token JSON file |
| **Remove Account** | `agy-switch remove <name>` | Removes standby account from vault |

---

## 2. Standard Operating Procedures for Agents

### Procedure A: When User asks to check accounts or status
Run via `run_command`:
```bash
~/.local/bin/agy-switch list
```
or:
```bash
~/.local/bin/agy-switch status
```
Report the active account and available standby accounts clearly to the user.

### Procedure B: When User asks to check quota (e.g. "Cek kuota", "Berapa sisa kuota?")
Run via `run_command`:
```bash
~/.local/bin/agy-switch list --quota
```
Report the output directly as plain conversational text.

> [!IMPORTANT]
> **NO MARKDOWN TABLES IN CHAT**: Never render quota reports as Markdown tables in conversational chat—tables wrap destructively on mobile screens.
> Render quota reports as a clean bulleted list where EVERY line starts with `- `, account names are styled with backticks (``` `<name>` ```) for syntax highlighting/color, and emails are omitted:
> `- `<name>` — 5h: <quota_5h>% (in <reset_5h>) | wk: <quota_wk>% (in <reset_wk>)`
> For the active account, highlight it:
> `- **[ACTIVE]** `<name>` — 5h: <quota_5h>% (in <reset_5h>) | wk: <quota_wk>% (in <reset_wk>)`

### Procedure C: When User asks to switch accounts
Run:
```bash
~/.local/bin/agy-switch switch <account_name>
```
Or to let the engine auto-rotate based on intelligent thresholds:
```bash
~/.local/bin/agy-switch switch --auto
```

> [!CAUTION]
> **Active Turn Safety & Deferred Safe-Turn Reload**:
> - If Antigravity is actively processing an agent turn or background task, `agy-switch switch <name>` (without `--force`) will return exit code 2. Never pass `--force` unless the user explicitly requests a forced switch or confirms the switch.
> - When `agy-switch switch <name> --force` or `agy-switch switch --auto` is invoked **inside an active agent turn** (descendant of `language_server`), `agy-switch` automatically activates **Deferred Safe-Turn Reload**: it performs an atomic **Disk-Only Swap** and queues `cache/pending_reload.json` (TTL 120s) without killing the parent `language_server` mid-turn.
> - Do **NOT** pass `--force-restart-now` during an active agent turn—forcing an immediate mid-turn server kill terminates your own `language_server` ancestor and aborts the turn (`--force-restart-now` is strictly reserved as an emergency user override).
> - If `LogWatcher` is inactive when a deferred switch completes, inform the user that credentials have been updated on disk and advise reloading the Desktop server (or running `agy-switch watch start`) after the turn completes.

### Procedure D: When User hits quota exhaustion or requests best account
1. Run `~/.local/bin/agy-switch switch --auto`.
2. When invoked mid-turn, `switch --auto` safely completes a Disk-Only Swap and writes `cache/pending_reload.json` (TTL 120s) so `LogWatcher` reloads `language_server` at the Safe Turn Boundary—never pass `--force-restart-now` or kill `language_server` mid-turn.
3. Inform the user that the rotation engine has evaluated the quotas and selected the optimal account based on `cache_optimized`, `drain_window`, and `imminent_reset` heuristics. If CLI output notes that `LogWatcher` is inactive, advise the user to reload the Desktop server after the turn completes.

---

## 3. Negative Constraints (What NOT To Do)

* ❌ **DO NOT edit token files directly**: Never overwrite `~/.gemini/jetski-standalone-oauth-token` or `~/.gemini/antigravity-cli/antigravity-oauth-token` manually. Always use `agy-switch switch`.
* ❌ **DO NOT kill language_server blindly**: Always let `agy-switch` handle `SIGTERM` signals and respawn polling.
* ❌ **DO NOT delete ~/.antigravity-vault**: Credentials and active markers reside there with `0600` permissions.
