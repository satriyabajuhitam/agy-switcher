# Spec: Triad Failover Integration and Self-Healing Engine

Domain: triad-failover
Status: ready-for-agent

## Context & Motivation
In building a 24/7 autonomous personal assistant (OpenClaw-style) within the Google Antigravity CLI ecosystem, three independent and decoupled subsystems operate in synergy:
1. **`agy-switcher`**: Multi-account OAuth vault manager, quota telemetry engine, and failover switcher.
2. **`agy-telemote`**: Telegram Bot gateway (Topic/Forum routing, concurrency queues, Whisper STT) and asynchronous TurnRunner.
3. **`agy-memory`**: Assistant persona (`SOUL.md`), user directives (`USER.md`), and durable knowledge engine backed by SQLite FTS5 with failure reflection.

The primary operational challenge in continuous 24/7 agent operations is handling model quota exhaustion (`RESOURCE_EXHAUSTED`, HTTP 429) across LLM inference calls:
- **Mid-Turn Failures**: Token quota depletion frequently occurs mid-turn during multi-step reasoning, large prompt submissions, or extended agentic tool calling loops. Proactively checking quota balances before every single turn introduces a mandatory 1–2 second network roundtrip latency to CloudCode APIs, deteriorating user responsiveness while remaining incapable of catching mid-stream quota exhaustion.
- **Conversation Context Preservation**: When a turn fails due to quota exhaustion, the user's ongoing dialogue context must not be dropped or reset. Because Antigravity CLI session state (`~/.gemini/antigravity-cli/brain/<uuid>/`) is stored locally on disk and decoupled from any specific OAuth identity, swapping credentials and re-executing `agy -p "..." --conversation <uuid>` preserves the entire conversation transcript seamlessly.
- **Strict Loose Coupling Invariant**: All three subsystems must maintain 100% operational independence. No package may list another in its `pyproject.toml` dependencies. Interactions must occur exclusively through safe runtime soft-imports (`try: import agy_switcher.api as switcher_api except ImportError: None`) or dynamic filesystem probing with graceful degradation when optional components are absent.
- **Fleet Exhaustion & Panic Escalation**: When all registered vault accounts are exhausted or placed into cooldown, the assistant must self-reflect on this operational limitation by logging a structured Panic Event into `agy-memory` and calculating the earliest recovery ETA to inform the human user transparently.
- **Interactive Control & Background Visibility**: Operators must have instant Telegram commands (`/quota`, `/switch`) for inspecting fleet balances and manually selecting accounts, while the background `LogWatcher` daemon must support non-blocking alerts to notify administrators when automatic rotations occur.

Governing decisions and domain standards:
- Architecture Decision Record: [`docs/adr/0014-triad-failover-integration-and-self-healing-engine.md`](docs/adr/0014-triad-failover-integration-and-self-healing-engine.md) (Accepted).
- Authoritative Domain Glossary: [`GLOSSARY.md`](GLOSSARY.md) (`Mid-Turn Failover`, `Fleet Exhaustion`, `Panic Event`, `Disk-Only Swap`, `In-Process SDK Facade`).
- Strategic Roadmap: [`ROADMAP.md`](ROADMAP.md) (Siklus 3: Integrasi Triad & Self-Healing Failover Engine).
- System Architecture Blueprint: [`.scratch/triad-personal-assistant/spec.md`](.scratch/triad-personal-assistant/spec.md).

---

## Requirements
### Requirement: Reactive Mid-Turn Failover Detection in TurnRunner
The `TurnRunner` module in `agy-telemote` MUST detect model quota exhaustion during active turn execution via dual-inspection detection (JSON stream result event and subprocess non-zero exit code with stderr), dynamically probe for `agy_switcher.api` via safe soft-import, and execute a **Disk-Only Swap** via `auto_rotate_on_exhaustion(model=mapped_model, reload_server=False)` without restarting desktop language server processes.

#### Scenario: [FAILOVER-DET-001] Dual-inspection detection on JSON stream result event with quota error status
- GIVEN an active turn executing `agy` subprocess with model `"gemini-3.8-flash"`
- AND the subprocess emits a stream JSON event `{"event": "result", "result": {"status": "ERROR", "error": "Quota exceeded for model gemini-3.8-flash: RESOURCE_EXHAUSTED"}}`
- WHEN `TurnRunner` inspects the stream result event
- THEN `TurnRunner` classifies the failure as a model quota exhaustion error
- AND initiates the in-process failover procedure before returning an error to the caller.

#### Scenario: [FAILOVER-DET-002] Dual-inspection detection on subprocess non-zero exit code with RESOURCE_EXHAUSTED in stderr
- GIVEN an active turn where the `agy` subprocess abruptly terminates with exit code `1`
- AND the captured stderr contains `"rpc error: code = ResourceExhausted desc = Quota limit reached for project"`
- AND the JSON event stream did not emit a successful completion event
- WHEN `TurnRunner` evaluates the process completion status
- THEN `TurnRunner` detects the non-zero exit code and recognizes `RESOURCE_EXHAUSTED` in stderr
- AND classifies the failure as a reactive mid-turn quota exhaustion requiring failover.

#### Scenario: [FAILOVER-DET-003] Dual-inspection detection on HTTP 429 status pattern in stderr
- GIVEN an active turn where the `agy` subprocess exits with returncode `2`
- AND stderr contains `"googleapi: Error 429: Rate limit exceeded or quota exhausted, rateLimitExceeded"`
- WHEN `TurnRunner` processes the failure outcome
- THEN `TurnRunner` matches the canonical HTTP `429` indicator in stderr
- AND flags the execution for automatic credential rotation.

#### Scenario: [FAILOVER-DET-004] Safe soft-import of agy_switcher.api succeeds and invokes auto_rotate_on_exhaustion
- GIVEN `agy-switcher` is installed in the Python environment
- AND active account `"primary-acc"` is exhausted for model `"gemini-3.8-flash"`
- AND standby account `"standby-acc"` has available quota
- WHEN `TurnRunner` catches a quota exhaustion failure during prompt execution
- THEN `TurnRunner` dynamically imports `agy_switcher.api` without hard dependency errors
- AND invokes `switcher_api.auto_rotate_on_exhaustion(model="gemini", reload_server=False)`
- AND receives a `RotationResult` indicating `success=True` and `new_account="standby-acc"`.

#### Scenario: [FAILOVER-DET-005] Disk-Only Swap execution passes reload_server=False and mapped model family
- GIVEN an active turn running with model `"claude-sonnet-4-6"`
- AND desktop language server processes are running with presence locks present
- WHEN `TurnRunner` triggers failover upon quota exhaustion
- THEN it passes `model="claude"` and `reload_server=False` to `auto_rotate_on_exhaustion`
- AND the token swap updates `~/.gemini/antigravity-cli/antigravity-oauth-token` atomically
- AND no desktop language server processes or presence locks are modified or terminated.

#### Scenario: [FAILOVER-ERR-001] Graceful fallback when agy_switcher is not installed
- GIVEN `agy-switcher` is not installed in the Python runtime (`ModuleNotFoundError` on `import agy_switcher.api`)
- AND a turn encounters a `RESOURCE_EXHAUSTED` quota error
- WHEN `TurnRunner` attempts to perform automatic rotation
- THEN it catches the `ImportError` gracefully without unhandled exceptions
- AND emits an error message to the Telegram user explaining that quota has been exhausted and manual re-login or installing `agy-switcher` is required.

#### Scenario: [FAILOVER-ERR-002] Non-quota subprocess error bypasses failover and returns standard agent error
- GIVEN an active turn where the `agy` subprocess fails with exit code `1`
- AND stderr contains `"SyntaxError in user script: unexpected EOF while parsing"`
- AND neither stream result nor stderr contains quota keywords or 429 indicators
- WHEN `TurnRunner` evaluates the error conditions
- THEN it determines the failure is not quota-related
- AND bypasses `auto_rotate_on_exhaustion` entirely
- AND returns the agent execution error directly to the progress sink.

#### Scenario: [FAILOVER-ERR-003] Malformed or unparseable JSON stream line does not crash failover detection
- GIVEN an active turn where stdout contains corrupt, truncated, or non-JSON lines
- AND the process subsequently exits with code `1` and stderr containing `"RESOURCE_EXHAUSTED"`
- WHEN `TurnRunner` parses each stream line
- THEN it safely ignores the malformed JSON without raising uncaught exceptions
- AND falls back to inspecting the process returncode and stderr to trigger failover successfully.

#### Scenario: [FAILOVER-EDGE-001] Mixed casing and localized variants of quota exhaustion keywords in stream or stderr
- GIVEN an `agy` failure where stderr contains mixed case `"resource_exhausted: Daily Quota Limit Reached"`
- WHEN `TurnRunner` matches the error content against canonical patterns
- THEN it performs case-insensitive regex evaluation matching `"quota"`, `"resource_exhausted"`, and `"429"`
- AND correctly triggers the rotation procedure.

#### Scenario: [FAILOVER-EDGE-002] Truncated or empty stderr on non-zero exit code handled gracefully
- GIVEN an active turn where the subprocess exits with code `137` (SIGKILL)
- AND stderr is completely empty due to immediate buffer truncation
- WHEN `TurnRunner` processes the exit code
- THEN it notes the absence of quota keywords in stderr
- AND handles the termination as an abnormal exit without triggering infinite false-rotation loops.

#### Scenario: [FAILOVER-EDGE-003] Partial output delivered before mid-stream connection reset and quota failure
- GIVEN an active turn where 5 milestones and partial responses were already delivered to the user
- AND the model abruptly cuts off mid-stream with `"error": {"message": "Resource has been exhausted"}`
- WHEN `TurnRunner` detects the mid-turn failure
- THEN it preserves previously delivered milestones
- AND triggers auto-rotation for the remaining turn retry.

#### Scenario: [FAILOVER-EDGE-004] Concurrent failover rotation invocations execute safely under atomic disk swap
- GIVEN two concurrent turns encounter quota exhaustion on active account `"primary-acc"` simultaneously
- WHEN both turn tasks invoke `switcher_api.auto_rotate_on_exhaustion(reload_server=False)` concurrently
- THEN the underlying atomic disk swap operations (`os.replace`) serialize safely without race conditions
- AND the active account transitions cleanly to a standby account without corrupted token files or double-hopping.

### Requirement: Bounded Retry Loop and Conversation Context Preservation
The `TurnRunner` retry engine MUST enforce a bounded retry budget of at most 2 attempts (or `min(2, total_accounts - 1)`), preserve the exact conversation ID (`--conversation <id>`) across retries to maintain continuous Antigravity CLI brain transcript history, and emit transient status updates via `ProgressSink.on_status()` during rotation pauses without polluting user chat history.

#### Scenario: [RETRY-LOOP-001] First-attempt failover retry succeeds with rotated account
- GIVEN a turn execution where the initial attempt fails with quota exhaustion
- AND `auto_rotate_on_exhaustion()` successfully swaps credentials to standby account `"acc2"`
- AND the retry counter is currently `0` (budget = 2)
- WHEN `TurnRunner` initiates retry attempt `1`
- THEN it re-launches `agy` with the identical prompt, model, and `--conversation <uuid>`
- AND the second subprocess succeeds with returncode `0`
- AND `TurnRunner` delivers the completed response to the user with `is_error=False`.

#### Scenario: [RETRY-LOOP-002] Second-attempt failover retry succeeds after consecutive quota exhaustion
- GIVEN a turn where initial attempt fails on account `"acc1"` and triggers rotation to `"acc2"`
- AND retry attempt `1` also fails due to quota exhaustion on `"acc2"`
- AND `auto_rotate_on_exhaustion()` successfully rotates from `"acc2"` to standby account `"acc3"`
- WHEN `TurnRunner` executes retry attempt `2`
- THEN it launches the subprocess with account `"acc3"` using the continuous `--conversation <uuid>`
- AND when attempt `2` completes successfully, `TurnRunner` marks the turn as resolved.

#### Scenario: [RETRY-LOOP-003] Conversation ID preserved across failover retries maintaining Antigravity brain transcript
- GIVEN a user session with active conversation ID `"conv-uuid-1234"`
- AND the initial turn fails mid-stream after appending 3 steps into `~/.gemini/antigravity-cli/brain/conv-uuid-1234/`
- WHEN `TurnRunner` rotates account credentials and executes the retry subprocess
- THEN the subprocess command strictly includes `--conversation conv-uuid-1234`
- AND the Antigravity CLI resumes reading and appending to `conv-uuid-1234/` without creating a split or orphaned conversation.

#### Scenario: [RETRY-LOOP-004] Transient status notification emitted to ProgressSink during retry pause
- GIVEN a turn encountering quota exhaustion during inferencing
- WHEN `TurnRunner` performs rotation and prepares the retry attempt
- THEN it calls `sink.on_status("🔄 Kuota akun habis, beralih ke akun cadangan...")`
- AND the Telegram bot UI reflects the transient status indicator to inform the user
- AND the transient message is replaced upon turn completion without leaving duplicate chat history messages.

#### Scenario: [RETRY-LOOP-005] Dynamic retry budget bounded by total registered vault accounts
- GIVEN a vault configured with exactly 2 accounts (`"acc1"`, `"acc2"`)
- WHEN `TurnRunner` calculates the retry limit
- THEN it evaluates `max_retries = min(2, max(0, total_accounts - 1))` yielding `1`
- AND prevents redundant retry attempts when no further unexhausted standby accounts exist.

#### Scenario: [RETRY-ERR-001] Max retry budget exhaustion aborts turn and reports failure
- GIVEN a turn where initial attempt, retry 1, and retry 2 all fail due to repeated quota exhaustion
- WHEN the retry counter exceeds the maximum budget of `2`
- THEN `TurnRunner` breaks the retry loop immediately
- AND does not spawn a third subprocess attempt
- AND transitions to the Fleet Exhaustion handling protocol.

#### Scenario: [RETRY-ERR-002] Single-account vault with zero standby accounts terminates immediately without retry
- GIVEN a vault containing only 1 registered account (`total_accounts = 1`)
- WHEN an active turn fails with `RESOURCE_EXHAUSTED`
- THEN `TurnRunner` detects that `min(2, total_accounts - 1) == 0`
- AND halts immediately without executing redundant failover attempts
- AND informs the user that no alternative accounts are registered in the vault.

#### Scenario: [RETRY-ERR-003] Non-quota error during retry attempt halts execution immediately
- GIVEN retry attempt `1` was launched after a successful credential rotation
- AND during retry `1`, the subprocess crashes with an application panic (`"Panic: invalid memory address"`)
- WHEN `TurnRunner` inspects the outcome of retry `1`
- THEN it recognizes the error is non-quota related
- AND does not consume retry attempt `2`
- AND terminates the turn returning the application error details.

#### Scenario: [RETRY-EDGE-001] Stale conversation ID gracefully recovered if brain directory was deleted
- GIVEN a retry invocation where the underlying Antigravity brain directory for `--conversation <id>` was purged
- AND the `agy` binary returns an error indicating conversation ID not found
- WHEN `TurnRunner` catches the invalid conversation error during failover
- THEN it clears `sess.conversation_id = None`
- AND launches a fresh conversation turn rather than crashing the session.

#### Scenario: [RETRY-EDGE-002] ProgressSink exception on on_status does not break failover retry execution
- GIVEN a Telegram network timeout occurs when `sink.on_status(...)` is called
- WHEN `TurnRunner` catches the exception raised by the sink
- THEN it logs a warning without interrupting the credential swap or subprocess re-invocation
- AND proceeds with the retry execution normally.

#### Scenario: [RETRY-EDGE-003] Subprocess cancellation during retry pause propagates cleanly without orphaned processes
- GIVEN a user sends `/stop` or cancels the turn while `TurnRunner` is waiting between failover retries
- WHEN an `asyncio.CancelledError` is raised in the turn task
- THEN `TurnRunner` immediately terminates any active or spawning subprocess
- AND cleans up process group descriptors without leaving dangling zombie processes.

### Requirement: Fleet Exhaustion and Panic Event Escalation
When `auto_rotate_on_exhaustion` returns `success=False` indicating all vault accounts are exhausted or in cooldown, the failover coordinator MUST trigger a **Panic Event**, record a structured Failure Report (`category="failure"`, `confidence=0.9`, content starting with `[Fleet Quota Exhaustion]`) into `agy-memory` SQLite FTS5 store if available, and return a consolidated user message with the earliest fleet reset countdown calculated from `get_fleet_quota_summary()`.

#### Scenario: [PANIC-ESC-001] Fleet Exhaustion triggers when auto_rotate_on_exhaustion returns success=False
- GIVEN all accounts in the vault have exhausted their model quotas or remain in active cooldown
- WHEN `TurnRunner` invokes `switcher_api.auto_rotate_on_exhaustion(model="gemini")`
- THEN the call returns `RotationResult(success=False, new_account=None, message="All accounts exhausted for model 'gemini'")`
- AND `TurnRunner` declares a state of **Fleet Exhaustion**
- AND triggers the Panic Event escalation protocol.

#### Scenario: [PANIC-ESC-002] Panic Event recorded in agy-memory with structured Failure Report and 0.9 confidence
- GIVEN a Fleet Exhaustion state is reached
- AND `agy-memory` is installed with SQLite database located at `~/.gemini/antigravity/memory/store.db`
- WHEN the Panic Event dispatcher executes
- THEN it creates a `MemoryRecord` with `category="failure"`, `confidence=0.9`, `tags=["fleet_exhaustion", "quota", "panic_event"]`, and `content="[Fleet Quota Exhaustion] All registered vault accounts are exhausted or in cooldown."`
- AND inserts the record into `agy-memory` store so it can be recalled during post-recovery self-reflection.

#### Scenario: [PANIC-ESC-003] Consolidated user message displays earliest reset ETA and countdown
- GIVEN all vault accounts are exhausted
- AND `switcher_api.get_fleet_quota_summary(model="gemini")` indicates account `"acc1"` resets at `14:30 UTC` (20 minutes remaining) and `"acc2"` resets at `17:00 UTC`
- WHEN `TurnRunner` formats the user-facing failure response
- THEN the returned message informs the user in polite, clear language:
- AND avoids cryptic technical stack traces or raw JSON dump outputs.

#### Scenario: [PANIC-ESC-004] Panic Event debouncing within cooldown window prevents duplicate failure records
- GIVEN a Panic Event was recorded 30 seconds ago for Fleet Exhaustion
- AND another incoming Telegram message immediately encounters Fleet Exhaustion
- WHEN the Panic Event dispatcher is invoked
- THEN it checks the recent failure timestamps in `agy-memory`
- AND debounces the insertion, updating the existing failure record's timestamp rather than polluting the database with duplicate rows.

#### Scenario: [PANIC-ERR-001] Graceful degradation when agy-memory is not installed or SQLite store is absent
- GIVEN `agy-memory` is not installed on the host system
- WHEN Fleet Exhaustion occurs and the Panic Event is dispatched
- THEN the dispatcher safely logs the exhaustion event to local logs (`logger.warning`)
- AND proceeds to deliver the ETA countdown message to the user without raising import or file errors.

#### Scenario: [PANIC-ERR-002] Database lock or I/O error writing Panic Event to agy-memory store handled safely
- GIVEN `store.db` in `agy-memory` is locked by another process (`sqlite3.OperationalError: database is locked`)
- WHEN the Panic Event dispatcher attempts to insert the failure record
- THEN it catches the database exception with a timeout guard
- AND logs a warning without blocking or crashing the turn runner completion flow.

#### Scenario: [PANIC-ERR-003] Corrupted quota cache during fleet summary calculation falls back to default wait notice
- GIVEN `~/.antigravity-vault/cache/last_known_quota.json` contains corrupt or empty data
- WHEN `get_fleet_quota_summary()` fails to calculate an exact numeric reset timestamp
- THEN the system falls back to a safe default message:
- AND logs the cache parsing warning.

#### Scenario: [PANIC-EDGE-001] Multiple model families exhausted concurrently in fleet summary
- GIVEN both `"gemini"` and `"claude"` model quotas are exhausted across all accounts
- WHEN `get_fleet_quota_summary(model="all")` is queried during a Panic Event
- THEN the summary calculates reset countdowns independently for both families
- AND displays the relevant reset time for the currently requested turn model.

#### Scenario: [PANIC-EDGE-002] All accounts exhausted with indefinite or missing reset timestamps in telemetry
- GIVEN CloudCode quota API returns `0%` quota without providing a specific reset countdown timestamp
- WHEN the Fleet Exhaustion message is synthesized
- THEN the formatter indicates that quota is exhausted and will refresh on the standard rolling window schedule
- AND does not display `"None"` or negative countdown durations.

#### Scenario: [PANIC-EDGE-003] Subsequent turn recovery after earliest reset ETA elapses
- GIVEN a previous turn triggered Fleet Exhaustion with reset ETA `14:30`
- AND a new turn arrives at `14:35` after quota has naturally replenished
- WHEN `TurnRunner` executes the turn
- THEN the initial invocation succeeds without errors
- AND no failover or panic events are triggered.

#### Scenario: [PANIC-EDGE-004] Concurrent fleet exhaustion turns debounce panic event without race conditions
- GIVEN two concurrent turns both experience Fleet Exhaustion within 10ms of each other
- WHEN both turn tasks dispatch the Panic Event to `agy-memory`
- THEN the debounce logic serializes the check against `store.db`
- AND exactly one Panic Event record is persisted without primary key collisions or duplicate logs.

### Requirement: In-Process Telegram Commands and Watcher Daemon Alerts
The Telegram command interface in `agy-telemote` MUST provide `/quota` and `/switch` handlers utilizing `switcher_api.get_fleet_quota_summary()` and `switcher_api.switch_account(name, reload_server=False)` with inline keyboard selection and graceful fallbacks when `agy-switcher` is missing, while `LogWatcher` in `agy-switcher` MUST support optional non-blocking alert dispatches via Telegram Bot API or webhook upon background auto-rotations.

#### Scenario: [CMD-ALERT-001] Telegram /quota handler displays active account, standby fleet capacity, and reset countdowns
- GIVEN an authorized Telegram user sends `/quota`
- AND `agy-switcher` is installed with 3 registered vault accounts (`"alpha"`, `"beta"`, `"gamma"`)
- WHEN `quota_handler` processes the command
- THEN it calls `switcher_api.get_fleet_quota_summary(model="all")`
- AND replies with a formatted message displaying:

#### Scenario: [CMD-ALERT-002] Telegram /switch handler presents inline keyboard buttons for account selection
- GIVEN an authorized Telegram user sends `/switch` without arguments
- AND the vault contains accounts `["work", "personal", "backup"]` with `"work"` currently active
- WHEN `switch_handler` executes
- THEN it queries registered accounts via `switcher_api`
- AND generates an `InlineKeyboardMarkup` where each button displays the account name and status (e.g., `["✅ work (Active)", "🔄 personal", "🔄 backup"]`)
- AND sends the interactive keyboard to the user.

#### Scenario: [CMD-ALERT-003] Telegram switch callback invokes switcher_api.switch_account with reload_server=False
- GIVEN the user taps the inline keyboard button for `"personal"`
- WHEN `switch_callback_handler` receives the callback query
- THEN it verifies user authorization
- AND invokes `switcher_api.switch_account(name="personal", reload_server=False)`
- AND updates the message text to `"✅ Berhasil beralih ke akun: personal (Disk-Only Swap)"`
- AND acknowledges the callback query via `query.answer()`.

#### Scenario: [CMD-ALERT-004] LogWatcher sends non-blocking Telegram alert via bot API upon background auto-rotation
- GIVEN `LogWatcher` in `agy-switcher` is running as a background daemon
- AND environment variables `TELEGRAM_ALERT_BOT_TOKEN` and `TELEGRAM_ALERT_CHAT_ID` are configured
- WHEN `LogWatcher.handle_exhaustion()` successfully rotates credentials from `"acc1"` to `"acc2"`
- THEN it dispatches an asynchronous HTTP POST request to `https://api.telegram.org/bot<TOKEN>/sendMessage`
- AND the notification message contains the rotated accounts, model hint, and timestamp
- AND the HTTP call is non-blocking with a strict 3.0s timeout so the watcher loop is never delayed.

#### Scenario: [CMD-ALERT-005] LogWatcher sends non-blocking webhook alert via WATCHER_WEBHOOK_URL upon background rotation
- GIVEN `WATCHER_WEBHOOK_URL` is set to `"https://alerts.example.com/agy-rotation"`
- AND a background auto-rotation completes
- WHEN `LogWatcher` triggers its notification routine
- THEN it emits a JSON payload `{"event": "auto_rotation", "previous_account": "acc1", "new_account": "acc2", "timestamp": "..."}` to the webhook endpoint
- AND handles standard HTTP 200/204 responses cleanly.

#### Scenario: [CMD-ALERT-ERR-001] Telegram /quota command provides graceful guidance when agy-switcher is missing
- GIVEN a user sends `/quota` in an environment where `agy-switcher` is not installed
- WHEN `quota_handler` catches `ImportError` on `agy_switcher.api`
- THEN it replies with an informative message:
- AND does not crash or raise uncaught server exceptions.

#### Scenario: [CMD-ALERT-ERR-002] Telegram /switch command provides graceful guidance when agy-switcher is missing
- GIVEN a user sends `/switch` in an environment where `agy-switcher` is not installed
- WHEN `switch_handler` executes
- THEN it detects the absence of `switcher_api`
- AND sends a message explaining that multi-account switching requires `agy-switcher`.

#### Scenario: [CMD-ALERT-ERR-003] Switching to non-existent account via callback reports friendly error and refreshes buttons
- GIVEN an inline switch button was pressed for an account that was deleted from the vault
- WHEN `switch_callback_handler` calls `switcher_api.switch_account("deleted-acc", reload_server=False)`
- THEN `switcher_api` returns `False` or raises `AccountNotFoundError`
- AND the handler catches the error and edits the message to `"⚠️ Akun 'deleted-acc' tidak ditemukan di vault."`
- AND does not corrupt the active vault pointer.

#### Scenario: [CMD-ALERT-ERR-004] Watcher alert dispatch network timeout or HTTP error does not block rotation engine
- GIVEN `LogWatcher` attempts to send a Telegram alert but the network is unreachable (e.g., DNS failure or 504 Gateway Timeout)
- WHEN the alert dispatch function executes
- THEN it catches `urllib.error.URLError` and timeout exceptions cleanly
- AND logs a non-fatal warning to stderr
- AND completes the account rotation successfully without rolling back or stalling.

#### Scenario: [CMD-ALERT-EDGE-001] Watcher alert dispatch with unconfigured alert environment variables is a clean no-op
- GIVEN neither `TELEGRAM_ALERT_BOT_TOKEN` nor `WATCHER_WEBHOOK_URL` is set in the environment
- WHEN `LogWatcher.handle_exhaustion()` executes
- THEN the alert dispatch function exits immediately as a no-op (0ms overhead)
- AND no network connections are attempted.

#### Scenario: [CMD-ALERT-EDGE-002] Malformed webhook URL or SSL handshake failure silently logged without crashing watcher
- GIVEN `WATCHER_WEBHOOK_URL` is configured with an invalid URL scheme or broken SSL certificate
- WHEN `LogWatcher` posts an alert payload
- THEN it catches the SSL and connection errors safely
- AND the watcher daemon remains active and running.

#### Scenario: [CMD-ALERT-EDGE-003] Unauthorized Telegram user invoking /quota or /switch blocked by check_auth
- GIVEN an unauthenticated or unauthorized user sends `/quota` or `/switch` in a Telegram chat
- WHEN the command handler invokes `check_auth(update)`
- THEN `check_auth` evaluates to `False`
- AND the handler returns immediately without executing SDK functions or exposing fleet metadata.

#### Scenario: [CMD-ALERT-EDGE-004] Rapid repeated callback taps for same account handled idempotently
- GIVEN an active account is `"work"` and the user rapidly taps the `"personal"` inline button twice within 100ms
- WHEN `switch_callback_handler` receives the duplicate callback queries
- THEN the first callback switches the account and answers the query
- AND the second callback detects that `"personal"` is already active
- AND acknowledges the query with an idempotent `"Akun sudah aktif"` status without duplicate disk writes.
