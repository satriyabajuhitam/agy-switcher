# agy-switch Domain Glossary

The authoritative ubiquitious domain vocabulary for `agy-switch` multi-account management, session lifecycle, and quota failover.

## Language

**Presence Lock**:
A session marker indicating that an Antigravity workspace or command-line session is actively open.
_Avoid_: File mutex, PID file, session file

**Stale Lock**:
A presence lock whose owning process has exited or ceased operation, no longer representing an active user session.
_Avoid_: Dead file, orphan lock, zombie file

**Active Turn**:
An uninterrupted inferencing or agent execution window during which background processes must not be terminated.
_Avoid_: Task busy, prompt lock, inference lock

**Dual-Directory Presence**:
The unified tracking of active user sessions across both Desktop and command-line Antigravity environments.
_Avoid_: Multi-root scanning, hybrid presence

**Disk-Only Swap**:
A token switching operation that updates credentials strictly in disk files without restarting or checking background language server processes.
_Avoid_: Fast switch, headless rotation, silent swap

**In-Process SDK Facade**:
A programmatic standard-library interface exposing account status, quota telemetry, and rotation mechanisms directly within a consumer Python runtime.
_Avoid_: Direct engine import, internal API, library binding

**Vault Bundle**:
A compressed archive encapsulating multi-account vault states with restricted POSIX permissions and cryptographic checksum verification for transport.
_Avoid_: Account export, backup archive, vault package

**Mid-Turn Failover**:
An automatic credential rotation and subprocess re-execution cycle triggered mid-turn when an inferencing engine reports quota exhaustion (HTTP 429 / RESOURCE_EXHAUSTED), preserving the active conversation context transparently.
_Avoid_: Turn retry, hot swap, prompt retry

**Fleet Exhaustion**:
An operational state where all accounts registered within the multi-account vault have exhausted their model quota or remain in active cooldown.
_Avoid_: All down, zero quota, total burnout

**Panic Event**:
A high-severity lifecycle signal emitted upon Fleet Exhaustion to record structured failure reflections into persistent memory (`agy-memory`) and notify human operators with estimated reset times.
_Avoid_: Crash dump, fatal alert, alarm trigger

**Transcript-Bounded Watching**:
The strict restriction of log tailing to structured session JSONL files (`transcript.jsonl`), completely excluding unformatted CLI and terminal logs to eliminate false-positive quota triggers.
_Avoid_: Log globbing, raw CLI log tailing, unparsed stream tailing

**Gemini-Exclusive Architecture**:
The singular architectural and operational focus on Google Gemini model quotas and rotation, permanently retiring non-Gemini (Claude/Anthropic) model branches, cross-model leakage checks, and legacy flags.
_Avoid_: Multi-model routing, Claude failover, hybrid family tracking

**Pre-Switch Quota Probe**:
An active, high-speed telemetry verification executed before mid-turn failover that validates whether the active account's Gemini quota is genuinely depleted (e.g. <=25%) before allowing account rotation.
_Avoid_: Blind regex failover, unvalidated switch

**Proactive Threshold Failover**:
An automated account rotation and language server reload cycle triggered between agent turns when the active account's quota drops below a configured threshold (e.g. <=20%) and healthy standby accounts are available, preempting mid-turn 429 exhaustion errors.
_Avoid_: Early switch, preemptive rotation, threshold hop

**Safe Turn Boundary**:
A quiescent operational interval verified by Global Quiescence—every open Antigravity session transcript has concluded its latest turn (`PLANNER_RESPONSE` with `status: "DONE"` and no `tool_calls`) and `language_server` has no active non-sidecar child processes—where credential rotation and server reloads can execute without aborting running tool calls.
_Avoid_: Turn pause, idle window, prompt gap

**Canonical Launcher**:
The unified, user-level entrypoint executable located at `~/.local/bin/agy-switch`, ensuring all Antigravity lifecycle hooks (`hooks.json`) and background services reference a single immutable PATH binary rather than transient repository clone directories.
_Avoid_: Direct script execution, clone path binding, workspace script pointer

**Daemon Mutual Exclusion**:
A cross-process concurrency control mechanism utilizing file locking (`watch.lock`) and service state inspection (`systemd`) that strictly prevents multiple background watcher daemons (`watch run`) from concurrently monitoring the same vault and racing during account failovers.
_Avoid_: Unbounded watcher, duplicate daemon, multi-tail race

**Mock Vault Isolation**:
The mandatory hermetic sandboxing of test suites and build gates into ephemeral temporary directories (`tmp`), strictly prohibiting test fixtures from reading or mutating the user's production vault (`~/.antigravity-vault`) or audit log (`watch.log`).
_Avoid_: Global fallback, production log leaking, test pollution

**Refresh-Aware Standby Viability**:
A diagnostic validation standard that evaluates standby accounts by verifying the structural presence of an OAuth `refresh_token`, categorizing accounts with expired 1-hour access tokens as healthy standby rather than issuing premature authentication failure warnings.
_Avoid_: Blind timestamp expiry, false warning, superficial token check

**Token Identity**:
The email identifier associated with a registered OAuth credential profile, resolved from embedded identity claims, credential metadata, or provider userinfo without invalidating otherwise renewable credentials when omitted.
_Avoid_: Mandatory JWT identity, rigid id_token schema

**Workspace Guard**:
A zero-overhead pre-execution validation gate within lifecycle hooks or verification scripts that inspects caller session metadata or directory paths, instantly terminating as a no-op (exit 0) when invoked outside the target repository.
_Avoid_: CWD check, path filter, repo detector

**Plugin Hook Scoping**:
The architectural separation between machine-wide plugin lifecycle hooks (such as global multi-account quota rotation) and workspace-specific quality gates (which must remain disabled by default in plugin bundles to avoid cross-project execution delays).
_Avoid_: Hook filtering, selective execution

**Pre-Flight Auth Gate**:
A live credential verification step executed before manual account switching mutates live token files or restarts the language server; fails closed on confirmed authentication rejections (HTTP 401/403 after a single best-effort refresh attempt), fails open with a warning on transient network errors, and allows explicit bypass only via `--skip-auth-check`.
_Avoid_: Schema-only check, silent 403 switch, blind credential swap

**Deferred Safe-Turn Reload**:
A non-disruptive reload strategy that performs an atomic token swap and records a 30-minute TTL `pending_reload.json` marker (used by in-turn switches, `PreInvocation` hook rotations, In-Process SDK swaps, and Drift Guard repairs) so `LogWatcher` re-syncs tokens from Vault and reloads `language_server` at the next verified Global Quiescence boundary without aborting active turns.
_Avoid_: Mid-turn parricide, forced in-turn kill, ad-hoc waiter subprocess

**Failover Audit & Auth Quarantine**:
A dual-persistence transparency mechanism where `PreInvocation` hook ejections append structured entries to `watch.log` and atomically record `last_failover` plus per-account `auth_errors` in `cache/failover_state.json`, surfacing recent ejections in `status` (for up to 24 hours or until the next successful manual switch) and persisting auth quarantine in `list`/`doctor` until cleared by re-login or a successful live probe.
_Avoid_: Silent rollback, stderr-only failover, ephemeral-only audit

**Vault-Authoritative Sync**:
The single-source-of-truth invariant establishing the Vault active pointer as the sole canonical authority for the active account, treating any divergence in Desktop or CLI token files as drift to be healed back toward the Vault rather than implicitly adopted.
_Avoid_: Bidirectional sync, desktop-follow, implicit adopt

**Zombie Write-Back**:
A credential desynchronization hazard where a `language_server` process that was not restarted after an account switch retains the previous account in memory and overwrites the Desktop token file back to the stale account upon OAuth access-token refresh.
_Avoid_: Ghost overwrite, stale token clobber, silent revert

**Continuous Drift Guard**:
A lightweight periodic integrity loop within `LogWatcher` that compares Desktop and CLI credential identities against the Vault active account, automatically repairing diverged files via Atomic Swap and scheduling a Deferred Safe-Turn Reload whenever Desktop credentials drifted.
_Avoid_: Aggressive overwrite loop, passive doctor-only, blind file watcher

**Global Quiescence**:
The multi-session idle invariant requiring all open Antigravity Desktop sessions (tracked via live `presence/*.lock` descriptors) to be at a completed turn (`PLANNER_RESPONSE` with `status: "DONE"` and no pending `tool_calls`) and `language_server` to have zero active non-sidecar child processes (excluding persistent `sidecar_sdk` runtimes) before executing a server reload.
_Avoid_: Single-tab idle, raw child-count check, naive lock-absence check





