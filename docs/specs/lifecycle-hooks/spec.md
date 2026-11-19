# Spec: Lifecycle Hooks Scoping, Workspace Guard, and PreInvocation Latency Optimization

Domain: lifecycle-hooks
Status: ready-for-agent

## Context & Motivation
The `agy-switch` plugin is designed to run globally across the user's Antigravity system (`~/.gemini/config/plugins/agy-switch/`, Canonical Launcher at `~/.local/bin/agy-switch`, and the background watcher daemon `agy-switch-watcher.service`). Its primary mission is ensuring continuous, transparent multi-account quota rotation across Google Gemini model families without user friction.

However, continuous deployment of lifecycle hooks across multi-project environments introduced critical operational friction:
1. **Unscoped Quality Gate in Global Plugin Distribution**:
   In `hooks.json` and the global `~/.gemini/config/hooks.json`, the Stop hook `build-quality-gate` was enabled by default (`"enabled": true`), binding to `scripts/verify-build.sh`. Because Antigravity executes configured Stop hooks on **every turn completion (turn stop event)** across all user workspaces and active conversations, any turn completed in an external project triggers a full 3-tier build verification of `agy-switcher` (AST architecture checks, `mypy --strict`, and complete unit test discovery). This causes noticeable UI pauses (spinning "working" state for several seconds) in unrelated repositories and fails outright if external environments lack required Python dependencies.
2. **Missing Workspace Guard in Build Verification**:
   `scripts/verify-build.sh` previously lacked contextual awareness of its caller. When invoked via a Stop hook, Antigravity passes a JSON payload on `stdin` containing session metadata such as `cwd` and `workspacePaths`. Without an early inspection gate, the script unconditionally executed the verification suite regardless of where it was called.
3. **Global Hook Pollution by Installer and Vault Engine**:
   Both `AgySwitchEngine.install_hook()` in `scripts/agy_switcher/vault.py` and the fallback hook installer in `install.sh` retained legacy entries during JSON merges. When installing or upgrading `agy-switch`, obsolete `build-quality-gate` configurations were preserved in `~/.gemini/config/hooks.json` rather than being pruned. Global hook configuration must strictly manage `agy-switch-auto-rotate`.
4. **PreInvocation Cold-Start Latency**:
   The PreInvocation hook (`agy-switch-auto-rotate`) runs before every model invocation turn. When `scripts/agy_switch.py hook-pre-invocation` executes, top-level eager imports of non-critical modules (`doctor`, `telemetry.formatters`, `urllib`, `argparse`, and diagnostic tools) result in cold-start latencies of ~110–145ms. For an active turn fast-path check where Tier 1 quota cache is valid (<5 minutes old), the target response time is <40ms.

To resolve these issues cleanly, this specification formalizes a **Defense-in-Depth** architecture governed by **Plugin Hook Scoping** and a zero-overhead **Workspace Guard**:
- Setting `build-quality-gate` to disabled by default in plugin distribution (`"enabled": false`), while enabling local developers to opt-in via workspace-level hooks (`.agents/hooks.json`).
- Adding a high-speed Workspace Guard to `scripts/verify-build.sh` that exits immediately (`exit 0` in <5ms) if the execution target is outside `agy-switcher`.
- Updating `AgySwitchEngine.install_hook()` and `install.sh` to automatically prune stale `build-quality-gate` entries from machine-wide `~/.gemini/config/hooks.json`.
- Restructuring `scripts/agy_switch.py` to defer heavy imports on the `hook-pre-invocation` execution path, reducing cold invocation latency to <40ms while maintaining 100% symbol re-export parity and mock bridge compatibility.

Governing decisions and domain standards:
- Architecture Decision Record: [`docs/adr/0020-scoped-lifecycle-hooks-and-workspace-guard.md`](docs/adr/0020-scoped-lifecycle-hooks-and-workspace-guard.md) (Accepted).
- Authoritative Domain Glossary: [`GLOSSARY.md`](GLOSSARY.md) (`Workspace Guard`, `Plugin Hook Scoping`, `Canonical Launcher`, `Presence Lock`, `Active Turn`).
- Repository Invariants: Zero external pip dependencies, pure POSIX bash and Python 3.10+ standard library.

---

## Requirements
### Requirement: Disabled-by-Default Quality Gate in Plugin Distribution (hooks.json)
The plugin distribution and repository root `hooks.json` MUST configure the `build-quality-gate` hook definition with `"enabled": false` while maintaining `"enabled": true` for `agy-switch-auto-rotate`, enforcing **Plugin Hook Scoping** so that workspace quality gates do not execute by default across external user repositories or active turns. Local developers may explicitly opt-in via workspace-level hook configurations (`.agents/hooks.json` or `.gemini/hooks.json`).

#### Scenario: [HOOK-SCOPE-001] Disabled build-quality-gate in root hooks.json
- GIVEN the repository root `hooks.json` configuration file
- WHEN the hook definitions are parsed by the Antigravity plugin loader
- THEN the `build-quality-gate` entry MUST have `"enabled": false`
- AND the `agy-switch-auto-rotate` entry MUST have `"enabled": true`
- AND `build-quality-gate` must not be scheduled or invoked automatically on agent turn Stop events.

#### Scenario: [HOOK-SCOPE-EDGE-001] Workspace-level local opt-in override preservation
- GIVEN a local developer workspace configured with `.agents/hooks.json` or `.gemini/hooks.json` setting `build-quality-gate` to `"enabled": true`
- WHEN Antigravity evaluates hook hierarchies for an active turn within the `agy-switcher` repository
- THEN the workspace-level configuration overrides the plugin-level default
- AND executes `scripts/verify-build.sh` on Stop events for that workspace only.

#### Scenario: [HOOK-SCOPE-ERR-001] Corrupted or unreadable plugin hooks.json fallback
- GIVEN a missing, unreadable, or syntactically invalid `hooks.json` file in the plugin root
- WHEN Antigravity discovers or loads plugin lifecycle hooks during agent startup
- THEN the system logs a diagnostic warning instead of raising an uncaught exception
- AND treats the unparsable `build-quality-gate` as disabled by default
- AND prevents any disruptive build verification processes from halting the session.

### Requirement: Zero-Overhead Workspace Guard in Build Verification (scripts/verify-build.sh)
`scripts/verify-build.sh` MUST implement a **Workspace Guard** at the very top of the script that inspects standard input (`stdin`) JSON payload (evaluating `cwd` and `workspacePaths`) and `$PWD`. If the execution target does not match the `agy-switcher` repository root (verified by directory path or the presence of marker `pyproject.toml` containing `name = "agy-switch"`), `scripts/verify-build.sh` MUST terminate immediately with exit code 0 (`exit 0`) within <5 milliseconds without executing AST architecture checks, `mypy --strict`, or Python unit tests.

#### Scenario: [WS-GUARD-001] Immediate exit 0 when stdin indicates foreign workspace cwd
- GIVEN `scripts/verify-build.sh` is invoked by an Antigravity Stop lifecycle hook
- AND the hook passes a JSON payload on `stdin` with `{"cwd": "/home/user/projects/other-app", "workspacePaths": ["/home/user/projects/other-app"]}`
- WHEN the Workspace Guard inspects the stdin payload
- THEN the script immediately exits with returncode `0` in less than 5ms
- AND does not execute `tests/test_modular_architecture.py`, `mypy`, or `unittest discover`.

#### Scenario: [WS-GUARD-002] Full build verification when executed inside agy-switcher repository
- GIVEN the current working directory `$PWD` or stdin `cwd` is the `agy-switcher` repository containing `pyproject.toml` with `name = "agy-switch"`
- WHEN `scripts/verify-build.sh` executes
- THEN the Workspace Guard allows execution to continue
- AND runs Tier 1 AST modular architecture tests, Tier 2 mypy strict type checking, and Tier 3 full unit test suite
- AND exits with code 0 upon green verification.

#### Scenario: [WS-GUARD-ERR-001] Non-blocking exit 0 on empty, malformed, or non-JSON stdin outside repo
- GIVEN `scripts/verify-build.sh` receives malformed JSON, truncated bytes, or EOF on `stdin`
- AND `$PWD` is an external directory `/tmp` without `pyproject.toml` matching `agy-switch`
- WHEN the Workspace Guard parses stdin and directory markers
- THEN the script handles the malformed input gracefully without crashing
- AND immediately exits with returncode `0` to prevent blocking the active turn.

#### Scenario: [WS-GUARD-ERR-002] Non-object JSON array or primitive payload on stdin
- GIVEN `scripts/verify-build.sh` is invoked with valid JSON that is not an object or contains non-string `cwd`
- AND `$PWD` is an external workspace directory lacking the `agy-switch` marker
- WHEN the Workspace Guard parses standard input
- THEN the guard catches the unexpected JSON schema without triggering bash pipefail or syntax errors
- AND terminates immediately with returncode `0` in less than 5ms.

#### Scenario: [WS-GUARD-EDGE-001] Symlinked or subpath workspace execution handling
- GIVEN `scripts/verify-build.sh` is invoked from a sub-directory inside the repository (e.g., `tests/` or `scripts/`) or via a symlink
- AND the parent hierarchy contains the repository marker `pyproject.toml`
- WHEN the Workspace Guard evaluates the execution boundary
- THEN the script resolves the realpath of the repository root
- AND permits the full verification suite to run normally.

#### Scenario: [WS-GUARD-EDGE-002] Multi-root workspace containing agy-switcher in workspacePaths
- GIVEN `scripts/verify-build.sh` is invoked by a Stop hook in a multi-root workspace session
- AND the stdin payload specifies foreign `cwd` but includes `agy-switcher` in `workspacePaths`
- WHEN the Workspace Guard evaluates the `workspacePaths` array
- THEN the guard identifies the presence of the `agy-switcher` repository root within the active workspace set
- AND permits Tier 1, Tier 2, and Tier 3 build verifications to execute.

### Requirement: Automated Global Hooks Sanitation in Installer and Vault Engine (install.sh, vault.py)
`AgySwitchEngine.install_hook()` in `scripts/agy_switcher/vault.py` and the global installer script `install.sh` MUST maintain strictly the `agy-switch-auto-rotate` PreInvocation hook in machine-wide `~/.gemini/config/hooks.json` referencing the **Canonical Launcher** (`~/.local/bin/agy-switch hook-pre-invocation`), and MUST automatically prune any legacy, foreign, or obsolete `build-quality-gate` entries from `~/.gemini/config/hooks.json` during installation or upgrade, ensuring global hooks remain sanitized and isolated from workspace verification tasks.

#### Scenario: [HOOK-CLEAN-001] Pruning legacy build-quality-gate from global hooks.json during engine install_hook
- GIVEN a machine-wide `hooks.json` containing both `agy-switch-auto-rotate` and a legacy `build-quality-gate` entry
- WHEN `AgySwitchEngine.install_hook(hooks_file=target_hooks)` is executed
- THEN `install_hook` registers or updates `agy-switch-auto-rotate` with `PreInvocation` configuration
- AND explicitly deletes the `build-quality-gate` key from the dictionary
- AND atomically writes the cleaned configuration to disk with `0o600` permissions.

#### Scenario: [HOOK-CLEAN-EDGE-001] Installer shell script automated hooks sanitation without Python engine
- GIVEN `install.sh` executes the fallback inline Python hook registration block for global installation
- AND the existing `~/.gemini/config/hooks.json` contains a legacy `build-quality-gate` configuration
- WHEN `install.sh` writes the updated hooks configuration
- THEN the fallback logic removes `build-quality-gate` from the target JSON
- AND leaves third-party or unrelated hooks intact while preserving `agy-switch-auto-rotate`.

#### Scenario: [HOOK-CLEAN-ERR-001] Permission denied or read-only directory on global hooks.json write
- GIVEN `~/.gemini/config/hooks.json` or its enclosing directory is read-only preventing file modifications
- WHEN `AgySwitchEngine.install_hook()` or `install.sh` attempts to prune `build-quality-gate` and write the updated hook configuration
- THEN the failure is caught cleanly and reported to `stderr` with a descriptive permission error message
- AND any temporary files are removed from disk without leaving dangling artifacts
- AND the existing `hooks.json` file is left unmodified.

#### Scenario: [HOOK-CLEAN-EDGE-002] Idempotent repeated installation cycles without duplicate backups
- GIVEN a machine-wide `hooks.json` that has already been sanitized with `agy-switch-auto-rotate` and no `build-quality-gate` entry
- WHEN `AgySwitchEngine.install_hook()` or `install.sh` is invoked multiple times in succession across upgrade or re-install cycles
- THEN the hook configuration remains valid and stable without duplicate keys
- AND no redundant backup files are generated for already-valid configurations
- AND all file operations execute via atomic temporary file replacement with strict `0o600` permissions.

### Requirement: PreInvocation Lazy-Import Latency Optimization (scripts/agy_switch.py)
`scripts/agy_switch.py` MUST optimize the cold-start dispatch path for the `hook-pre-invocation` subcommand by deferring heavy module imports (`doctor`, `telemetry.formatters`, diagnostic suites, and CLI parsers) until explicitly required, executing fast-path quota evaluations (Tier 1 memory/disk cache hits within 5 minutes) in less than 40 milliseconds, while maintaining 100% symbol re-export parity and Dynamic Module Proxy Bridge compatibility for test mocking.

#### Scenario: [PREINV-FAST-001] Sub-40ms execution on Tier 1 cache hit for hook-pre-invocation
- GIVEN valid Tier 1 quota cache exists on disk (`~/.antigravity-vault/quota_summary.json` updated <5 minutes ago)
- AND an active turn fires a `PreInvocation` hook with stdin payload `{"modelName": "gemini-2.5-pro"}`
- WHEN `scripts/agy_switch.py hook-pre-invocation` executes via the Canonical Launcher
- THEN the script loads only essential Layer 0 and Layer 3 modules
- AND emits `{}` on stdout within 40 milliseconds of process spawn
- AND exits with returncode `0`.

#### Scenario: [PREINV-FAST-EDGE-001] Full module lazy-loading when quota refresh or doctor healing is required
- GIVEN Tier 1 quota cache is stale (>5 minutes old) or active account quota is below threshold requiring rotation
- WHEN `scripts/agy_switch.py hook-pre-invocation` determines that network quota retrieval or vault rotation is necessary
- THEN the execution dynamically imports `QuotaClient` and `RotationEngine` on demand
- AND executes the rotation or fetch without module import errors
- AND outputs the resulting hook decision JSON payload.

#### Scenario: [PREINV-FAST-ERR-001] Fallback on corrupted or unparseable Tier 1 quota cache file
- GIVEN the disk quota cache `~/.antigravity-vault/quota_summary.json` contains malformed JSON or truncated content
- AND `scripts/agy_switch.py hook-pre-invocation` is invoked with valid stdin payload `{"modelName": "gemini-2.5-pro"}`
- WHEN the fast-path execution detects the corrupted cache file
- THEN it logs a diagnostic warning to `stderr` without raising an unhandled exception
- AND seamlessly triggers lazy-loading of telemetry modules to perform Tier 2 probing or Tier 3 failover
- AND outputs a valid JSON hook payload on `stdout` with exit code 0.

#### Scenario: [PREINV-FAST-EDGE-002] Symbol re-export and mock bridge compatibility preservation
- GIVEN existing test suites and external integrations import symbols directly from `agy_switch` or apply `unittest.mock.patch` to `agy_switch.<symbol>`
- WHEN `scripts/agy_switch.py` is imported into an integration or unit test runner
- THEN all symbols enumerated in `__all__` remain resolvable and callable with 100% backward compatibility
- AND mock monkeypatching against `scripts/agy_switch.py` correctly redirects or binds to the underlying implementation modules.
