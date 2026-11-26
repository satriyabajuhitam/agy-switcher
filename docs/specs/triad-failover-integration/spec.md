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
The `TurnRunner` module in `agy-telemote` and the In-Process SDK Facade (`scripts/agy_switcher/api.py`) MUST detect model quota exhaustion during active turn execution strictly for the Google Gemini model family, normalize all model identifiers to `"gemini"`, reject non-Gemini model requests with `AgySwitchError`, and execute a **Disk-Only Swap** via `auto_rotate_on_exhaustion(model="gemini", reload_server=False)` without restarting desktop language server processes or checking non-Gemini cooldowns.

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

#### Scenario: [FAILOVER-DET-004] Safe soft-import of agy_switcher.api succeeds and invokes auto_rotate_on_exhaustion for gemini
- GIVEN `agy-switcher` is installed in the Python environment
- AND active account `"primary-acc"` is exhausted for model `"gemini-3.8-flash"`
- AND standby account `"standby-acc"` has available Gemini quota
- WHEN `TurnRunner` catches a quota exhaustion failure during prompt execution
- THEN `TurnRunner` dynamically imports `agy_switcher.api` without hard dependency errors
- AND invokes `switcher_api.auto_rotate_on_exhaustion(model="gemini", reload_server=False)`
- AND receives a `RotationResult` indicating `success=True` and `new_account="standby-acc"`.

#### Scenario: [FAILOVER-DET-005] Disk-Only Swap execution passes reload_server=False with Gemini-exclusive model
- GIVEN an active turn running with model `"gemini-3.8-pro"`
- AND desktop language server processes are running with presence locks present
- WHEN `TurnRunner` triggers failover upon quota exhaustion
- THEN it passes `model="gemini"` and `reload_server=False` to `auto_rotate_on_exhaustion`
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

#### Scenario: [FAILOVER-ERR-004] Invoking auto_rotate_on_exhaustion with non-Gemini model raises AgySwitchError
- GIVEN an external caller or agent attempts to invoke `auto_rotate_on_exhaustion(model="claude")`
- WHEN `auto_rotate_on_exhaustion` validates the model family
- THEN it rejects the call and raises `AgySwitchError` indicating that non-Gemini models are retired and unsupported
- AND no account rotation or disk mutation is performed.

#### Scenario: [FAILOVER-EDGE-005] Specific Gemini model variants normalize cleanly to gemini family
- GIVEN a turn running with specific Gemini model slugs (`"gemini-3.8-flash"`, `"gemini-3.8-pro"`, `"gemini-2.5-flash"`)
- WHEN `TurnRunner` or `auto_rotate_on_exhaustion` processes the model parameter
- THEN each model slug is normalized cleanly to `"gemini"`
- AND rotation heuristics evaluate exclusively the Gemini quota bucket.

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

### Requirement: Quota Telemetry and Rotation Heuristics
The `telemetry.py` and `rotation.py` modules MUST operate under a **Gemini-Exclusive Architecture**, restricting model normalization and quota caching strictly to Google Gemini metrics, removing all Claude/Anthropic group parsing and sub-keys from `last_known_quota.json`, and executing `RotationEngine` evaluation heuristics (`auto_highest`, `imminent_reset`, `drain_window`) exclusively against Gemini quota balances without multi-model branching or cross-model leakage.

#### Scenario: [QUOTA-GEMINI-001] Telemetry normalizes Gemini model variants to gemini key
- GIVEN model strings `"gemini"`, `"gemini-3.8-flash"`, `"gemini-3.8-pro"`, or `"GEMINI-2.5-FLASH"`
- WHEN `normalize_model_name()` is invoked
- THEN it returns `"gemini"` in lowercase
- AND passes validation without errors.

#### Scenario: [QUOTA-GEMINI-002] QuotaClient parses CloudCode summary retaining exclusively Gemini quota metrics
- GIVEN CloudCode quota API response containing user quota buckets
- WHEN `QuotaClient.parse_quota_response()` parses the API payload
- THEN it extracts weekly and five-hour quota fractions exclusively for Gemini Models under the `"gemini"` key
- AND completely ignores any third-party or Claude models.

#### Scenario: [QUOTA-GEMINI-003] RotationEngine evaluates standby accounts based strictly on Gemini quota
- GIVEN active account `"acc1"` with 0% Gemini quota
- AND standby account `"acc2"` with 80% Gemini quota
- AND standby account `"acc3"` with 40% Gemini quota
- WHEN `RotationEngine(model_name="gemini").evaluate()` evaluates candidate accounts
- THEN it selects `"acc2"` based on highest available Gemini quota
- AND ignores non-Gemini model state.

#### Scenario: [QUOTA-GEMINI-ERR-001] normalize_model_name raises AgySwitchError on non-Gemini model identifiers
- GIVEN an unsupported model name such as `"claude"`, `"claude-3-5-sonnet"`, `"gpt-4o"`, or `"anthropic"`
- WHEN `normalize_model_name(model)` is called
- THEN it raises `AgySwitchError` with an informative error message stating allowed model is strictly `'gemini'`.

#### Scenario: [QUOTA-GEMINI-ERR-002] CloudCode quota response with missing or corrupt Gemini group yields empty buckets without KeyError
- GIVEN CloudCode raw API response missing `"Gemini Models"` or containing empty group payload
- WHEN `QuotaClient.parse_quota_response()` evaluates the payload
- THEN it handles the missing group gracefully and returns an empty dictionary
- AND does not throw `KeyError` or crash the caller.

#### Scenario: [QUOTA-GEMINI-ERR-003] Unreadable or corrupted quota cache file during cache load initializes clean cache
- GIVEN `last_known_quota.json` contains corrupted binary garbage or unparseable JSON
- WHEN `QuotaClient.load_cache()` is called
- THEN it catches `json.JSONDecodeError` or `OSError`
- AND returns an empty dictionary safely without raising uncaught exceptions.

#### Scenario: [QUOTA-GEMINI-EDGE-001] Quota cache persistence strips legacy non-Gemini keys
- GIVEN an existing `last_known_quota.json` on disk containing legacy `"claude"` entries
- WHEN `QuotaClient.load_cache()` or `save_cache()` processes the cache file
- THEN it strips all legacy `"claude"` keys and retains only valid `"gemini"` quota records
- AND persists a sanitized cache adhering to the Gemini-Exclusive schema.

#### Scenario: [QUOTA-GEMINI-EDGE-002] get_fleet_quota_summary filters strictly to Gemini fleet balances
- GIVEN a vault with 3 registered accounts
- WHEN `get_fleet_quota_summary(model="gemini")` is invoked
- THEN it returns a `FleetQuotaSummary` containing quota balances and reset countdowns solely for the Gemini model family.

#### Scenario: [QUOTA-GEMINI-EDGE-003] Concurrent cache read-modify-write operations serialize under cache lock
- GIVEN multiple threads in `agy-switch` updating and reading `last_known_quota.json` concurrently
- WHEN `QuotaClient.save_cache()` and `load_cache()` are invoked simultaneously
- THEN `_CACHE_LOCK` serializes access, preventing race conditions or truncated cache writes.

#### Scenario: [QUOTA-GEMINI-EDGE-004] Model name with extra whitespace or mixed casing normalizes to gemini
- GIVEN model string `"  GeMiNi-3.8-PrO  "`
- WHEN `normalize_model_name()` is invoked
- THEN it strips whitespace and normalizes to `"gemini"`.

### Requirement: Transcript-Bounded Watching in PresenceDetector & LogWatcher
The `PresenceDetector` and `LogWatcher` modules MUST strictly bound log monitoring to active structured conversation transcript files (`transcript.jsonl`), completely cease globbing and reading unformatted CLI text log files (`~/.gemini/antigravity-cli/log/*.log`), and enforce strict JSON-first filtering where non-JSON lines and unstructured text are unconditionally discarded, while evaluating quota exhaustion regex patterns exclusively on records with `status == "ERROR"`, `type == "ERROR_MESSAGE"`, or non-empty `error` payloads, maintaining absolute immunity for records with status `DONE` or message type `USER_INPUT`.

#### Scenario: [WATCH-BOUND-001] PresenceDetector.get_watch_targets strictly omits raw CLI log files
- GIVEN an active Antigravity session with active transcript `brain/<id>/.system_generated/logs/transcript.jsonl`
- AND directory `~/.gemini/antigravity-cli/log/` contains multiple `cli-*.log` files
- WHEN `PresenceDetector.get_watch_targets()` is invoked
- THEN the returned target list contains exclusively the resolved `transcript.jsonl` path
- AND contains zero paths from `~/.gemini/antigravity-cli/log/`.

#### Scenario: [WATCH-BOUND-002] Structured JSON line with status ERROR and quota pattern triggers exhaustion dispatch
- GIVEN `LogWatcher` tailing an active `transcript.jsonl`
- WHEN a new JSON line arrives: `{"status": "ERROR", "error": "Quota exceeded for project: RESOURCE_EXHAUSTED"}`
- THEN `LogWatcher.process_line()` parses the line as valid JSON
- AND identifies `status == "ERROR"` with matching quota exhaustion regex
- AND dispatches exhaustion handling.

#### Scenario: [WATCH-BOUND-003] Structured JSON line with type ERROR_MESSAGE and 429 pattern triggers exhaustion dispatch
- GIVEN `LogWatcher` processing lines from `transcript.jsonl`
- WHEN a new JSON line arrives: `{"type": "ERROR_MESSAGE", "message": "googleapi: Error 429: Rate limit exceeded"}`
- THEN `LogWatcher.process_line()` validates `type == "ERROR_MESSAGE"` and matches HTTP 429
- AND dispatches exhaustion handling.

#### Scenario: [WATCH-BOUND-004] Immunity for status DONE even when content contains quota exhaustion keywords
- GIVEN a turn where an agent completed a task reviewing or grepping code containing `"RESOURCE_EXHAUSTED"`
- WHEN a JSON line arrives in `transcript.jsonl` with `{"status": "DONE", "content": "Found 3 matches for RESOURCE_EXHAUSTED in codebase"}`
- THEN `LogWatcher.process_line()` checks the status field and encounters `"DONE"`
- AND immediately grants immunity without inspecting content for exhaustion regex
- AND returns `False` without triggering account rotation.

#### Scenario: [WATCH-BOUND-005] Immunity for type USER_INPUT containing quota exhaustion and 429 references
- GIVEN a user prompt submitted to Antigravity discussing quota limits: `"How do I handle RESOURCE_EXHAUSTED and code 429?"`
- WHEN `transcript.jsonl` appends `{"type": "USER_INPUT", "content": "How do I handle RESOURCE_EXHAUSTED and code 429?"}`
- THEN `LogWatcher.process_line()` identifies `type == "USER_INPUT"`
- AND immediately suppresses evaluation, returning `False` without switching accounts.

#### Scenario: [WATCH-BOUND-ERR-001] Non-JSON line in transcript or plain text file is unconditionally ignored
- GIVEN an unformatted plain text line arriving from terminal stdout or corrupted transcript: `"RESOURCE_EXHAUSTED: unit test simulated failure"`
- WHEN `LogWatcher.process_line(line, is_jsonl=True)` or `LogWatcher.process_line(line, is_jsonl=False)` evaluates the line
- THEN it detects that the line does not parse as valid JSON or lacks required structured error envelopes
- AND discards the line immediately, returning `False` without triggering rotation.

#### Scenario: [WATCH-BOUND-ERR-002] Malformed or truncated JSON line does not crash watcher daemon
- GIVEN a partial or corrupt JSON line appended mid-flush: `{"status": "ERROR", "error": "RESOURCE_EX`
- WHEN `LogWatcher.process_line()` attempts JSON decoding
- THEN it catches `json.JSONDecodeError` safely
- AND ignores the line without throwing an unhandled exception or stopping the tailing loop.

#### Scenario: [WATCH-BOUND-EDGE-001] Deeply nested error payload extracted safely from content object or list
- GIVEN a JSON line with `{"status": "ERROR", "content": [{"type": "text", "text": "rpc error: code = ResourceExhausted desc = Quota limit reached"}]}`
- WHEN `LogWatcher.process_line()` evaluates the record
- THEN it traverses the nested content structure to extract textual tokens
- AND correctly matches `ResourceExhausted`
- AND triggers exhaustion handling.

#### Scenario: [WATCH-BOUND-EDGE-002] Missing or unlinked transcript target gracefully yields empty watch targets
- GIVEN no Antigravity Desktop or CLI presence locks exist on the host
- AND `PresenceDetector.get_active_transcript_path()` returns `None`
- WHEN `PresenceDetector.get_watch_targets()` is invoked
- THEN it returns an empty list `[]` without raising `FileNotFoundError` or inspecting CLI logs.

#### Scenario: [WATCH-BOUND-ERR-003] PermissionError or unreadable transcript file descriptor handled safely without crashing daemon
- GIVEN an active session transcript file path that becomes unreadable due to filesystem permissions (`EACCES` or `PermissionError`)
- WHEN `LogWatcher.poll_once()` attempts to open or read from the file
- THEN it catches `OSError` safely
- AND skips the target for that polling iteration without raising an unhandled exception or terminating the watcher loop.

#### Scenario: [WATCH-BOUND-EDGE-003] File truncation or rotation during active transcript tailing safely resets file cursor
- GIVEN `LogWatcher` has read 4096 bytes from an active `transcript.jsonl`
- WHEN the file is truncated or rotated by Antigravity such that its current size is smaller than the cursor
- THEN `LogWatcher.poll_once()` detects `current_size < cursor`
- AND resets the file cursor to 0 to resume tailing cleanly from the beginning without raising `SeekError` or missing newly appended records.

### Requirement: Pre-Switch Quota Probe and Extended Damping in LogWatcher
The `LogWatcher` module MUST execute a **Pre-Switch Quota Probe** prior to initiating credential rotation upon detecting quota exhaustion, validating whether the active account's Gemini quota fraction exceeds the configured health threshold (retrieved dynamically from `AgySwitchEngine.get_quota_threshold()`, defaulting to 20% or 25% depending on configuration) via fast telemetry or cached metrics, immediately suppressing rotation and cooldown application if the quota is healthy, and MUST enforce an extended default damping window of **60.0 seconds** (`damping_seconds = 60.0`) shared across both proactive threshold checks and reactive exhaustion failover to eliminate chain-switching cascades and rapid-fire switch storms.

#### Scenario: [PRE-PROBE-001] Pre-Switch Quota Probe suppresses rotation when active account has healthy Gemini quota
- GIVEN active account `"primary-acc"` with cached Gemini quota of 85% (`fraction = 0.85`)
- AND `LogWatcher` detects a quota exhaustion pattern in an error event
- WHEN `LogWatcher.handle_exhaustion()` executes the Pre-Switch Quota Probe
- THEN it evaluates that active account Gemini quota is greater than 25% (`0.85 > 0.25`)
- AND suppresses account rotation, treating the error as a transient RPC anomaly
- AND does not add `"primary-acc"` to `cooldown.json`
- AND returns `False`.

#### Scenario: [PRE-PROBE-002] Pre-Switch Quota Probe authorizes rotation when active account Gemini quota is depleted
- GIVEN active account `"primary-acc"` with cached Gemini quota of 10% (`fraction = 0.10 <= 0.25`)
- AND standby account `"standby-acc"` has available quota (90%)
- WHEN `LogWatcher.handle_exhaustion()` runs the Pre-Switch Quota Probe
- THEN it validates that `"primary-acc"` Gemini quota is at or below the 25% threshold
- AND authorizes account rotation
- AND places `"primary-acc"` into cooldown
- AND executes `switch_tokens("standby-acc", restart_server=True, force=True)` successfully.

#### Scenario: [PRE-PROBE-003] Extended 60-second damping window suppresses rapid successive error bursts
- GIVEN `LogWatcher` initialized with default `damping_seconds = 60.0`
- AND an auto-rotation occurred 25 seconds ago (`elapsed = 25.0s`)
- WHEN a subsequent quota error arrives in the active transcript
- THEN `LogWatcher.handle_exhaustion()` checks the elapsed time against `damping_seconds`
- AND determines `elapsed < 60.0`
- AND suppresses rotation without placing any account into cooldown.

#### Scenario: [PRE-PROBE-004] Damping window expiration re-arms failover switching
- GIVEN an auto-rotation occurred 61 seconds ago (`elapsed = 61.0s >= 60.0s`)
- AND the active account is genuinely depleted (quota <= 25%)
- WHEN a new quota error arrives
- THEN `LogWatcher.handle_exhaustion()` verifies the damping window has elapsed
- AND proceeds to evaluate standby accounts and execute failover.

#### Scenario: [PRE-PROBE-ERR-001] Cache read failure or uninitialized telemetry falls back safely to rotation
- GIVEN `last_known_quota.json` is missing or unparseable during the Pre-Switch Quota Probe
- AND no live telemetry can be fetched
- WHEN `LogWatcher.handle_exhaustion()` assesses active account quota
- THEN it treats the quota state as unverified rather than assuming healthy
- AND falls back to standard exhaustion rotation so the agent turn is not permanently blocked.

#### Scenario: [PRE-PROBE-ERR-002] Live telemetry query network failure or timeout during Pre-Switch Quota Probe falls back safely to rotation
- GIVEN active account `"primary-acc"` encounters a quota error pattern in `transcript.jsonl`
- AND cached quota is missing or stale
- AND live CloudCode telemetry request raises `urllib.error.URLError` or socket timeout
- WHEN `LogWatcher.handle_exhaustion()` executes the Pre-Switch Quota Probe
- THEN it catches the network exception gracefully without crashing the watcher daemon
- AND treats the quota state as unverified, authorizing rotation to the next standby account.

#### Scenario: [PRE-PROBE-ERR-003] Burst quota error stream arriving within 60-second damping window triggers zero redundant switches
- GIVEN an auto-rotation occurred 10 seconds ago (`elapsed = 10.0s < 60.0s`)
- WHEN 10 consecutive quota exhaustion error lines arrive within 2 seconds
- THEN each error line is evaluated against the 60.0-second damping window
- AND all 10 events are suppressed with zero additional token switches or cooldown penalties.

#### Scenario: [PRE-PROBE-EDGE-001] Pre-Switch Quota Probe at exact boundary of 25% quota
- GIVEN active account `"primary-acc"` has exactly 25.0% Gemini quota remaining (`fraction = 0.25`)
- WHEN `LogWatcher.handle_exhaustion()` executes the Pre-Switch Quota Probe
- THEN it evaluates that `fraction <= 0.25` is not healthy (>25%)
- AND authorizes account rotation to preserve agent operation before zero-token hard cutoff.

#### Scenario: [PRE-PROBE-EDGE-002] Clock skew or negative elapsed time handled safely by damping timer
- GIVEN system clock was adjusted backwards such that `now - last_switch_time < 0`
- WHEN `LogWatcher.handle_exhaustion()` checks the damping interval
- THEN it resets `last_switch_time = 0.0` safely
- AND prevents permanent suppression of rotation.

#### Scenario: [PRE-PROBE-EDGE-003] Custom damping window configuration respects user parameter
- GIVEN `LogWatcher` initialized with custom `damping_seconds = 90.0`
- WHEN an error arrives 75 seconds after a switch
- THEN `LogWatcher` suppresses rotation in accordance with the configured 90.0-second window.

#### Scenario: [PRE-PROBE-EDGE-004] Pre-Switch Quota Probe at exact 60.0-second damping boundary re-arms switching
- GIVEN an auto-rotation occurred exactly 60.0 seconds ago (`elapsed = 60.0s`)
- AND active account has depleted quota (`fraction <= 0.25`)
- WHEN a new quota error arrives in `transcript.jsonl`
- THEN `LogWatcher.handle_exhaustion()` evaluates that `elapsed >= damping_seconds` (damping window elapsed)
- AND allows Pre-Switch Quota Probe and failover to proceed.

#### Scenario: [PRE-PROBE-SYNC-001] Pre-Switch Quota Probe dynamically evaluates configured threshold from vault config
- GIVEN vault configuration has `quota_threshold` set to `0.20` (20%)
- AND active account has 22% remaining quota
- AND a reactive quota error pattern is captured in the transcript
- WHEN `LogWatcher.handle_exhaustion()` runs the Pre-Switch Quota Probe
- THEN it retrieves the configured threshold `0.20` via `AgySwitchEngine.get_quota_threshold()`
- AND evaluates that `0.22 > 0.20` constitutes healthy quota
- AND suppresses rotation, logging a transient RPC anomaly.

#### Scenario: [PRE-PROBE-SYNC-002] Proactive rotation triggers share unified 60-second damping state with reactive failover
- GIVEN a proactive rotation was triggered at Safe Turn Boundary 30 seconds ago (`elapsed = 30.0s`)
- WHEN a reactive quota exhaustion error arrives in `transcript.jsonl`
- THEN `LogWatcher.handle_exhaustion()` checks the unified `last_switch_time` against `damping_seconds = 60.0`
- AND determines that `elapsed < 60.0`
- AND suppresses reactive rotation, preventing double-switching cascades.

### Requirement: Proactive Threshold Failover and Safe Turn Boundary in LogWatcher
The `LogWatcher` daemon (`scripts/agy_switcher/watcher.py`), presence detector (`scripts/agy_switcher/presence.py`), and process supervisor (`scripts/agy_switcher/supervisor.py`) MUST evaluate active account quota in `poll_once()`, trigger proactive credential rotation when quota falls at or below `AgySwitchEngine.get_quota_threshold()` (default 20%) with healthy standby quota (>= 70%) and drain-window retention (reset <= 30m and quota > 5%), and execute account rotation and Language Server reloading (`restart_server=True, force=True`) EXCLUSIVELY at a verified **Safe Turn Boundary** governed by **Global Quiescence** (amending ADR 0016 via ADR 0022). `ProcessSupervisor.has_active_child_processes(pid=None)` and `ProcessSupervisor.is_server_busy(pid=None)` SHALL exclude zombie (`Z`) processes and persistent Node.js `sidecar_sdk` child processes (`argv[0]` basename `node`/`nodejs` invoking a `sidecar_sdk` entrypoint path under `--enable_sidecars`) when detecting active turn child processes under `language_server`, while strictly classifying non-Node tool shells (such as `bash -c "grep sidecar_sdk"`) and unreadable `/proc/<pid>/cmdline` on non-zombie children as busy (`True`). When evaluating live `presence/*.lock` file descriptors held by running processes, `ProcessSupervisor.is_server_busy()` MUST inspect each locked session's `transcript.jsonl` via Layer 0 `read_latest_jsonl_record()` (preserving 5-Layer DAG independence): a locked open tab is idle ONLY when its latest JSONL record satisfies `is_transcript_record_quiescent(record)` (`dict` with trimmed case-insensitive `type == "PLANNER_RESPONSE"`, `status == "DONE"`, and absent/empty `tool_calls`), whereas any locked session with a missing, empty, unreadable, or non-quiescent transcript (`USER_INPUT`, `GENERIC`, `MESSAGE`, `IN_PROGRESS`/`RUNNING`/`STREAMING`, or `PLANNER_RESPONSE` with non-empty `tool_calls`) MUST fail safe to busy (`True`). `LogWatcher._is_turn_quiescent(transcript_path=None)` SHALL enforce **Global Quiescence**: (a) when an explicit `transcript_path` is passed, it MUST require that transcript file to exist and satisfy `is_transcript_record_quiescent()` alongside `not is_server_busy()`; (b) when live `presence/*.lock` sessions exist, EVERY open session's latest `transcript.jsonl` record MUST satisfy `is_transcript_record_quiescent(record)` AND `language_server` MUST have zero active non-sidecar child processes; and (c) when zero live `presence/*.lock` descriptors exist and zero non-sidecar child processes exist, if a fallback active transcript discovered via `PresenceDetector` exists on disk and is non-quiescent (e.g., restricted `/proc/*/fd` environments), `_is_turn_quiescent()` MUST still fail safe to `False`, whereas when zero live locks and no non-quiescent active transcript exist, the server is globally quiescent for **Deferred Safe-Turn Reload** (while `_evaluate_proactive_rotation()` still skips safely when no active session transcript exists). In addition, `LogWatcher._process_pending_reload()` SHALL consume `cache/pending_reload.json` (default TTL **1800 seconds** / 30 minutes) at a verified **Global Quiescence** boundary and enforce the **Pre/Post-Restart Re-Sync Guard**: immediately BEFORE restarting `language_server` and immediately AFTER `restart_language_server(force=True)` returns `True`, re-verify and atomically re-sync Desktop and CLI token files from the current Vault active account to eliminate shutdown **Zombie Write-Back** races, append a structured audit log entry to `watch.log`, and unlink `cache/pending_reload.json`. If `locate_language_server_pid()` explicitly returns `None` (`language_server` is stopped) when consuming a valid marker, `_process_pending_reload()` MUST atomically re-sync tokens from Vault and unlink `cache/pending_reload.json` cleanly without retrying indefinitely; conversely, if `language_server` IS running (`pid is not None`) and `restart_language_server(force=True)` returns `False` or raises an exception, `_process_pending_reload()` MUST log to `stderr`, preserve `cache/pending_reload.json` for retry, and avoid writing a false audit entry. Expired (`now - requested_at > ttl_seconds`) or malformed/corrupt `pending_reload.json` markers MUST be unlinked and ignored safely.

#### Scenario: [WATCH-PROACTIVE-001] Proactive rotation triggered when active account quota reaches or falls below threshold at Safe Turn Boundary
- GIVEN active account `"primary-acc"` with remaining Gemini quota at 14% (`fraction = 0.14 <= 0.20`)
- AND standby account `"standby-acc"` has healthy Gemini quota (85% >= 70%)
- AND the active transcript `transcript.jsonl` has recorded a completed turn event with `type == "PLANNER_RESPONSE"`, `status == "DONE"`, and no `tool_calls`
- AND `ProcessSupervisor.is_server_busy()` returns `False` indicating the Language Server is idle
- WHEN `LogWatcher.poll_once()` evaluates proactive rotation eligibility
- THEN it recognizes the Safe Turn Boundary and initiates credential rotation to `"standby-acc"`
- AND reloads the Language Server via `switch_tokens("standby-acc", restart_server=True, force=True)`
- AND returns `True` after updating file cursors and resetting damping timer.

#### Scenario: [WATCH-PROACTIVE-002] Proactive rotation deferred while language server is busy or turn is in-flight
- GIVEN active account `"primary-acc"` has depleted quota at 14% (`<= 20%`)
- AND standby account `"standby-acc"` has healthy quota at 90%
- AND the language server is currently executing an agentic turn or `ProcessSupervisor.is_server_busy()` returns `True`
- WHEN `LogWatcher.poll_once()` evaluates proactive rotation eligibility
- THEN it detects that the system is not at a Safe Turn Boundary
- AND defers credential rotation without restarting `language_server` or modifying active tokens
- AND returns `False` until the turn concludes and the server becomes quiescent.

#### Scenario: [WATCH-PROACTIVE-003] Drain window heuristic retains active account when reset is within 30 minutes and quota exceeds 5%
- GIVEN active account `"primary-acc"` has remaining quota of 12% (`fraction = 0.12 <= 0.20`)
- AND the quota reset timestamp is in 18 minutes (`reset_delta = 18.0 <= 30.0`)
- AND the remaining balance exceeds the absolute floor of 5% (`0.12 > 0.05`)
- WHEN `LogWatcher.poll_once()` evaluates proactive rotation conditions
- THEN it applies the drain window preservation heuristic
- AND suppresses rotation, retaining `"primary-acc"` as active to utilize remaining quota before reset.

#### Scenario: [WATCH-PROACTIVE-004] Standby account qualification requires healthy quota threshold of 70% or higher
- GIVEN active account `"primary-acc"` has remaining quota of 14%
- AND standby account `"standby-1"` has only 40% quota remaining (`0.40 < 0.70`)
- AND standby account `"standby-2"` has 10% quota remaining (`0.10 < 0.70`)
- WHEN `LogWatcher.poll_once()` searches for eligible proactive rotation targets
- THEN it evaluates that no standby account meets the 70% healthy standby threshold
- AND suppresses proactive rotation, retaining `"primary-acc"` until actual exhaustion occurs or healthy standby quota becomes available.

#### Scenario: [WATCH-PROACTIVE-005] Safe Turn Boundary verified via Global Quiescence (`PLANNER_RESPONSE` DONE without `tool_calls` and zero non-sidecar children)
- GIVEN all open Antigravity sessions tracked via live `presence/*.lock` fds have latest `transcript.jsonl` records with `type == "PLANNER_RESPONSE"`, `status == "DONE"`, and no `tool_calls`
- AND `language_server` has zero active non-sidecar child processes (persistent `sidecar_sdk` Node child ignored)
- WHEN `LogWatcher._is_turn_quiescent()` and `ProcessSupervisor.is_server_busy()` are evaluated
- THEN `ProcessSupervisor.is_server_busy()` returns `False`
- AND `LogWatcher._is_turn_quiescent()` returns `True`, certifying the state as a verified **Safe Turn Boundary** under **Global Quiescence**.

#### Scenario: [WATCH-PROACTIVE-ERR-001] Telemetry fetch failure during proactive quota check retains active account without false rotation
- GIVEN `LogWatcher.poll_once()` attempts to evaluate active account quota
- AND `last_known_quota.json` is missing and live CloudCode telemetry raises `urllib.error.URLError`
- WHEN `LogWatcher` evaluates proactive rotation conditions
- THEN it catches the network exception gracefully without crashing the daemon
- AND suppresses proactive rotation, retaining the active account until valid telemetry is retrieved.

#### Scenario: [WATCH-PROACTIVE-ERR-002] Missing transcript or unresolvable active session skips proactive rotation safely
- GIVEN no Antigravity session transcript is discovered by `PresenceDetector`
- WHEN `LogWatcher.poll_once()` evaluates proactive rotation
- THEN it determines that no active session boundary can be verified
- AND skips proactive rotation evaluation safely without raising exceptions.

#### Scenario: [WATCH-PROACTIVE-ERR-003] Server reload failure on Safe Turn Boundary logged safely without crashing watcher daemon
- GIVEN `LogWatcher` triggers proactive rotation to standby account `"standby-acc"`
- AND `switch_tokens` fails to terminate or restart `language_server` due to `ProcessSupervisor` error
- WHEN `LogWatcher` receives the failed switch result
- THEN it logs the failure message to `stderr`
- AND does not advance `last_switch_time`
- AND continues its polling loop cleanly without unhandled crash.

#### Scenario: [WATCH-PROACTIVE-EDGE-001] Active quota at exact threshold boundary triggers proactive evaluation
- GIVEN active account `"primary-acc"` has exactly 20.0% Gemini quota remaining (`fraction = 0.20`)
- AND configured threshold is 20% (`threshold = 0.20`)
- AND standby account `"standby-acc"` has 85% quota
- AND the system is at a verified Safe Turn Boundary
- WHEN `LogWatcher.poll_once()` evaluates proactive rotation
- THEN it determines that `fraction <= threshold` is satisfied
- AND initiates rotation to `"standby-acc"`.

#### Scenario: [WATCH-PROACTIVE-EDGE-002] Active quota at or below 5% inside drain window overrides retention and forces rotation
- GIVEN active account `"primary-acc"` has 4% Gemini quota remaining (`fraction = 0.04 <= 0.05`)
- AND quota reset is scheduled in 10 minutes (`reset_delta = 10.0 <= 30.0`)
- AND standby account `"standby-acc"` has 95% quota
- AND the system is at a verified Safe Turn Boundary
- WHEN `LogWatcher.poll_once()` evaluates proactive rotation conditions
- THEN it evaluates that quota is at or below the 5% hard floor (`0.04 <= 0.05`)
- AND overrides the drain window retention, rotating immediately to `"standby-acc"` to prevent impending 429 exhaustion.

#### Scenario: [WATCH-PROACTIVE-EDGE-003] Damping window enforcement prevents successive proactive switches within 60 seconds
- GIVEN a proactive account rotation completed 40 seconds ago (`elapsed = 40.0s < 60.0s`)
- AND newly active account reports quota below threshold
- WHEN `LogWatcher.poll_once()` checks proactive rotation eligibility
- THEN it enforces the 60-second damping interval
- AND suppresses rotation until `elapsed >= 60.0` seconds.

#### Scenario: [WATCH-RELOAD-001] Valid `pending_reload.json` consumed at Global Quiescence executes Pre/Post-Restart Re-Sync Guard, reloads server, logs audit entry, and unlinks marker
- GIVEN `cache/pending_reload.json` contains a valid unexpired marker (`target_account: "work-acc"`, `requested_at` within `ttl_seconds: 1800`)
- AND `LogWatcher._is_turn_quiescent()` returns `True` under **Global Quiescence** and `language_server` is running
- WHEN `LogWatcher.poll_once()` runs `_process_pending_reload()`
- THEN it atomically re-syncs Desktop and CLI tokens from the current Vault active account BEFORE calling `engine.restart_language_server(force=True)`
- AND atomically re-syncs Desktop and CLI tokens again AFTER `restart_language_server(force=True)` completes
- AND appends a structured deferred-reload audit entry to `watch.log` and unlinks `cache/pending_reload.json`.

#### Scenario: [WATCH-RELOAD-002] Valid `pending_reload.json` deferred without restart while `_is_turn_quiescent()` is `False`
- GIVEN `cache/pending_reload.json` contains a valid unexpired marker
- AND `LogWatcher._is_turn_quiescent()` returns `False` because an agent turn is still running
- WHEN `LogWatcher.poll_once()` executes
- THEN it does NOT call `restart_language_server()`
- AND preserves `cache/pending_reload.json` intact for the next polling cycle.

#### Scenario: [WATCH-RELOAD-ERR-001] Malformed or corrupt `pending_reload.json` unlinked safely without crashing `poll_once()`
- GIVEN `cache/pending_reload.json` contains invalid JSON, non-dict payload, or non-numeric `requested_at`
- WHEN `LogWatcher.poll_once()` inspects the pending reload marker
- THEN it unlinks the corrupt marker safely without raising an unhandled exception and does not restart `language_server`.

#### Scenario: [WATCH-RELOAD-EDGE-001] Expired `pending_reload.json` exceeding `ttl_seconds` unlinked and ignored without restarting server
- GIVEN `cache/pending_reload.json` has `requested_at` older than `ttl_seconds` (`now - requested_at > ttl_seconds`)
- AND `LogWatcher._is_turn_quiescent()` returns `True`
- WHEN `LogWatcher.poll_once()` executes
- THEN it unlinks the expired `cache/pending_reload.json` without restarting `language_server` or writing a reload audit entry.

#### Scenario: [GLOBAL-QUIESCENCE-001] Persistent `sidecar_sdk` child process excluded while active non-sidecar tool child marks server busy
- GIVEN `language_server` (PID 500) has a persistent non-zombie child PID 501 whose `/proc/501/cmdline` invokes `node /opt/antigravity/sidecar_sdk/index.js`
- AND separately a test adds non-zombie child PID 502 whose `/proc/502/cmdline` invokes `/bin/bash -c pytest`
- WHEN `ProcessSupervisor.has_active_child_processes(500)` and `ProcessSupervisor.is_server_busy(500)` are evaluated
- THEN with only PID 501 (`sidecar_sdk`) present, both return `False`
- AND with PID 502 (`/bin/bash`) present, both return `True`.

#### Scenario: [GLOBAL-QUIESCENCE-002] Multiple open Desktop tabs with live presence locks certify quiescent only when ALL tabs end with `PLANNER_RESPONSE` DONE and no `tool_calls`
- GIVEN `language_server` holds open file descriptors to BOTH `presence/tab-a.lock` and `presence/tab-b.lock` plus a persistent `sidecar_sdk` child
- AND both `brain/tab-a/.../transcript.jsonl` and `brain/tab-b/.../transcript.jsonl` end with `{"type": "PLANNER_RESPONSE", "status": "DONE"}` without `tool_calls`
- WHEN `ProcessSupervisor.is_server_busy()` and `LogWatcher._is_turn_quiescent()` execute
- THEN `ProcessSupervisor.is_server_busy()` returns `False` and `LogWatcher._is_turn_quiescent()` returns `True`.

#### Scenario: [GLOBAL-QUIESCENCE-003] Multi-tab Global Quiescence blocks reload when one tab is idle and another open tab is mid-turn
- GIVEN `language_server` holds open file descriptors to `presence/tab-a.lock` (latest record: `PLANNER_RESPONSE`, `DONE`, no `tool_calls`) and `presence/tab-b.lock`
- AND `brain/tab-b/.../transcript.jsonl` ends with `{"type": "USER_INPUT", "content": "Deploy"}` OR `{"type": "GENERIC", "status": "DONE"}` OR `{"type": "PLANNER_RESPONSE", "status": "DONE", "tool_calls": [{"name": "run_command"}]}` OR `{"type": "PLANNER_RESPONSE", "status": "IN_PROGRESS"}`
- WHEN `ProcessSupervisor.is_server_busy()` and `LogWatcher._is_turn_quiescent()` execute
- THEN `ProcessSupervisor.is_server_busy()` returns `True` and `LogWatcher._is_turn_quiescent()` returns `False`, blocking reload until Tab B finishes its turn.

#### Scenario: [GLOBAL-QUIESCENCE-004] Zero active presence locks and zero non-sidecar child processes certify globally quiescent for pending reload
- GIVEN `language_server` is running with zero open `presence/*.lock` file descriptors (all Desktop tabs closed) and only a persistent `sidecar_sdk` child
- AND `cache/pending_reload.json` contains a valid unexpired marker for `"work-acc"`
- WHEN `LogWatcher._is_turn_quiescent()` and `LogWatcher._process_pending_reload()` execute
- THEN `_is_turn_quiescent()` returns `True`
- AND `_process_pending_reload()` re-syncs tokens from Vault, reloads `language_server`, logs the audit entry, and unlinks `pending_reload.json`.

#### Scenario: [GLOBAL-QUIESCENCE-ERR-001] Open presence lock with missing, empty, unreadable, or partial transcript fails safe to busy
- GIVEN a live process holds an open fd to `presence/session-new.lock`
- AND `brain/session-new/.system_generated/logs/transcript.jsonl` is missing, 0 bytes, unreadable (`PermissionError`), or ends with truncated streaming JSON
- WHEN `ProcessSupervisor.is_server_busy()` and `LogWatcher._is_turn_quiescent()` evaluate quiescence
- THEN `is_server_busy()` fails safe to `True` and `_is_turn_quiescent()` returns `False`.

#### Scenario: [GLOBAL-QUIESCENCE-ERR-002] Unreadable `/proc/<child_pid>/cmdline` on non-zombie child process fails safe to busy
- GIVEN `language_server` (PID 500) has a non-zombie child PID 550 whose `/proc/550/cmdline` raises `PermissionError` or is missing
- WHEN `ProcessSupervisor.has_active_child_processes(500)` and `ProcessSupervisor.is_server_busy(500)` execute
- THEN PID 550 is NOT misclassified as `sidecar_sdk` and both methods fail safe returning `True`.

#### Scenario: [GLOBAL-QUIESCENCE-EDGE-001] `USER_INPUT`, `GENERIC` (`DONE`), `MESSAGE` (`DONE`), and non-dict records strictly rejected as non-quiescent
- GIVEN transcript records `{"type": "USER_INPUT", "content": "Analyze bug"}`, `{"type": "GENERIC", "status": "DONE", "content": "Tool stdout"}`, `{"type": "MESSAGE", "status": "DONE", "content": "Legacy"}`, or non-dict values (`None`, `[]`)
- WHEN `is_transcript_record_quiescent(record)` and `LogWatcher._is_turn_quiescent(transcript_path=...)` evaluate each record
- THEN all return `False` because none represents a completed `PLANNER_RESPONSE` (`status == "DONE"`) without `tool_calls`.

#### Scenario: [GLOBAL-QUIESCENCE-EDGE-002] Legacy open presence lock without transcript preserves fail-safe busy while stale locks are still pruned
- GIVEN `antigravity/presence/stale.lock` has no open fd in `/proc` and `antigravity-cli/presence/active.lock` has a live fd in `/proc/5100/fd/11` with no corresponding `transcript.jsonl`
- WHEN `ProcessSupervisor.is_server_busy()` executes
- THEN `stale.lock` is unlinked from disk, `active.lock` is preserved, and `is_server_busy()` returns `True`.

#### Scenario: [GLOBAL-QUIESCENCE-EDGE-003] Non-Node shell tool child mentioning `"sidecar_sdk"` in its arguments is NOT excluded as sidecar and marks server busy
- GIVEN `language_server` (PID 500) has a non-zombie child PID 503 whose `/proc/503/cmdline` is `/bin/bash -c "grep -r sidecar_sdk scripts/"`
- WHEN `ProcessSupervisor.has_active_child_processes(500)` and `ProcessSupervisor.is_server_busy(500)` execute
- THEN PID 503 is classified as an active non-sidecar tool process (`True`) so agent commands referencing `"sidecar_sdk"` are never killed mid-turn.

#### Scenario: [GLOBAL-QUIESCENCE-EDGE-004] Restricted `/proc` fallback with non-quiescent active transcript and missing explicit `transcript_path` fail safe to `False`
- GIVEN Case 1: `/proc/*/fd` returns zero live presence locks (e.g., restricted permissions or unlocked session fixture) while `PresenceDetector` discovers an active transcript ending with `{"type": "PLANNER_RESPONSE", "status": "IN_PROGRESS"}`
- AND Case 2: `LogWatcher._is_turn_quiescent(transcript_path=nonexistent_or_empty_path)` is called explicitly
- WHEN `LogWatcher._is_turn_quiescent()` evaluates both cases
- THEN both return `False` safely without prematurely restarting `language_server`.

#### Scenario: [RESYNC-GUARD-001] Pre/Post-Restart Re-Sync Guard runs atomic Vault sync immediately before and after server restart
- GIVEN `cache/pending_reload.json` is valid and unexpired and `LogWatcher._is_turn_quiescent()` returns `True`
- AND `language_server` is running
- WHEN `LogWatcher._process_pending_reload()` executes
- THEN `sync_active_tokens_from_vault()` (or equivalent Vault atomic re-sync) is invoked BEFORE `restart_language_server(force=True)` and invoked a second time AFTER `restart_language_server(force=True)` succeeds.

#### Scenario: [RESYNC-GUARD-ERR-001] Pre-restart Vault re-sync failure aborts server restart safely without crashing watcher
- GIVEN `cache/pending_reload.json` is valid and `_is_turn_quiescent()` returns `True`
- AND `sync_active_tokens_from_vault()` fails (`False` or raises `OSError`) because Vault active token is missing or unreadable
- WHEN `LogWatcher._process_pending_reload()` executes
- THEN it logs a warning to `stderr`, aborts `restart_language_server(force=True)`, and returns `False` without crashing `poll_once()`.

#### Scenario: [RESYNC-GUARD-ERR-002] Running `language_server` failing `restart_language_server(force=True)` preserves `pending_reload.json` for retry without false audit log
- GIVEN `cache/pending_reload.json` is valid and unexpired, `_is_turn_quiescent()` returns `True`, and `locate_language_server_pid()` returns a running PID (`4242`)
- AND `restart_language_server(force=True)` returns `False` (e.g., `PermissionError` or exit timeout) or raises an exception
- WHEN `LogWatcher._process_pending_reload()` executes
- THEN it logs a warning to `stderr`, preserves `cache/pending_reload.json` intact for the next polling cycle, writes NO reload audit entry to `watch.log`, and returns `False` cleanly.

#### Scenario: [RESYNC-GUARD-EDGE-001] Shutdown Zombie Write-Back occurring during `restart_language_server()` is immediately healed by post-restart re-sync using latest Vault active account
- GIVEN `cache/pending_reload.json` originally targeted `"acc1"`, Vault active has since switched to `"acc2"`, and during `restart_language_server(force=True)` the exiting server overwrites `~/.gemini/jetski-standalone-oauth-token` with stale `"acc0"`
- WHEN `LogWatcher._process_pending_reload()` completes
- THEN the post-restart re-sync atomically overwrites Desktop and CLI token files with `"acc2"` (the current Vault active account), eliminating the shutdown write-back race.

#### Scenario: [RESYNC-GUARD-EDGE-002] Valid `pending_reload.json` when `language_server` is not running re-syncs tokens from Vault and unlinks marker cleanly
- GIVEN `cache/pending_reload.json` contains a valid unexpired marker for `"work-acc"`
- AND `ProcessSupervisor.locate_language_server_pid()` returns `None` (`language_server` is stopped)
- WHEN `LogWatcher._process_pending_reload()` executes
- THEN it atomically re-syncs Desktop and CLI tokens from Vault active account and unlinks `cache/pending_reload.json` without emitting endless retry loops.

### Requirement: Telemetry-Aware Cooldown Immunity and 5-Hour Duration Clamping
The `LogWatcher` and `CooldownManager` modules MUST implement **Telemetry-Aware Cooldown Immunity**, eliminating unconditional exclusions of accounts in `cooldown.json` when fresh telemetry or cached data demonstrates quota recovery (>= 70%), and MUST clamp reactive cooldown durations resulting from HTTP 429 quota exhaustion strictly to the 5-hour rolling bucket reset timestamp (or default 30-minute fallback), explicitly prohibiting the application of multi-day weekly reset timestamps to cooldown records.

#### Scenario: [COOLDOWN-IMMUNITY-001] Account in cooldown.json with healthy quota (>= 70%) is exempt from rotation penalty
- GIVEN standby account `"standby-acc"` is recorded in `cooldown.json` with an unexpired cooldown timestamp
- AND `last_known_quota.json` or live telemetry reports `"standby-acc"` has recovered to 85% Gemini quota (`0.85 >= 0.70`)
- WHEN `LogWatcher` filters valid accounts for proactive rotation or reactive failover
- THEN it evaluates that `"standby-acc"` possesses healthy quota exceeding the 70% immunity threshold
- AND grants cooldown immunity to `"standby-acc"`, including it in the list of eligible rotation candidates.

#### Scenario: [COOLDOWN-IMMUNITY-002] Account in cooldown.json with depleted quota (< 70%) remains excluded from rotation
- GIVEN standby account `"standby-acc"` is recorded in `cooldown.json` with an active cooldown
- AND telemetry reports `"standby-acc"` has only 30% Gemini quota remaining (`0.30 < 0.70`)
- WHEN `LogWatcher` evaluates eligible rotation candidates
- THEN it determines that `"standby-acc"` does not qualify for cooldown immunity
- AND excludes `"standby-acc"` from candidate selection.

#### Scenario: [COOLDOWN-CLAMP-001] Cooldown duration on HTTP 429 clamps strictly to 5-hour rolling reset timestamp
- GIVEN active account `"primary-acc"` encounters an HTTP 429 `RESOURCE_EXHAUSTED` error
- AND telemetry for `"primary-acc"` indicates the 5-hour rolling bucket resets in 2 hours (`now + 7200`)
- AND the weekly bucket resets in 5 days (`now + 432000`)
- WHEN `LogWatcher.set_cooldown()` calculates the cooldown expiration timestamp
- THEN it clamps the cooldown expiration strictly to the 5-hour rolling bucket reset timestamp (`now + 7200`)
- AND records the clamped timestamp in `cooldown.json`.

#### Scenario: [COOLDOWN-CLAMP-002] Fallback 30-minute cooldown applied when rolling bucket reset timestamp is absent or past
- GIVEN active account `"primary-acc"` triggers an HTTP 429 exhaustion error
- AND telemetry cache contains no future reset timestamp or reset timestamp is in the past (`reset_time <= now`)
- WHEN `LogWatcher.set_cooldown()` sets the cooldown duration
- THEN it applies the default 30-minute fallback interval (`cooldown_until = now + 1800`)
- AND saves the record without applying unbounded durations.

#### Scenario: [COOLDOWN-CLAMP-003] Weekly bucket reset timestamp strictly rejected as cooldown duration ceiling
- GIVEN active account `"primary-acc"` encounters quota exhaustion
- AND quota telemetry contains a weekly reset timestamp 6 days in the future
- AND no 5-hour rolling bucket timestamp is available
- WHEN `LogWatcher.set_cooldown()` evaluates candidate reset timestamps
- THEN it rejects the multi-day weekly reset timestamp
- AND caps the maximum cooldown duration at 5 hours (18,000 seconds) or the 30-minute fallback.

#### Scenario: [COOLDOWN-ERR-001] Corrupt or unparseable cooldown.json does not prevent rotation of healthy accounts
- GIVEN `cooldown.json` is corrupted with malformed JSON syntax or truncated bytes
- WHEN `LogWatcher.get_cooldowns()` reads the cooldown file
- THEN it handles `json.JSONDecodeError` safely, treating the cooldown mapping as empty
- AND allows rotation to proceed among healthy accounts without crashing.

#### Scenario: [COOLDOWN-ERR-002] Clock skew resulting in negative reset duration defaults safely to 30-minute cooldown
- GIVEN telemetry records a reset timestamp that evaluates to a negative delta due to system clock adjustment
- WHEN `LogWatcher.set_cooldown()` calculates cooldown duration
- THEN it detects that `reset_delta <= 0`
- AND defaults to the safe 30-minute duration (`now + 1800.0`).

#### Scenario: [COOLDOWN-EDGE-001] Account at exact 70% quota boundary granted cooldown immunity
- GIVEN standby account `"standby-acc"` is listed in `cooldown.json`
- AND cached telemetry reports exactly 70.0% Gemini quota remaining (`fraction = 0.70`)
- WHEN `LogWatcher` evaluates cooldown immunity
- THEN it determines that `fraction >= 0.70` is satisfied
- AND includes `"standby-acc"` as an eligible candidate.

#### Scenario: [COOLDOWN-EDGE-002] Multiple accounts in cooldown evaluated individually based on real-time telemetry
- GIVEN vault contains 3 standby accounts in `cooldown.json`: `"acc1"` (75% quota), `"acc2"` (65% quota), and `"acc3"` (90% quota)
- WHEN `LogWatcher` filters valid accounts
- THEN it grants immunity to `"acc1"` and `"acc3"`, while continuing to penalize `"acc2"`
- AND selects `"acc3"` as the highest-quota rotation target.

### Requirement: Watcher Daemon Mutual Exclusion and Service State Awareness
The `LogWatcher` daemon and `DaemonManager` in `scripts/agy_switcher/watcher.py` MUST enforce cross-process mutual exclusion via a non-blocking POSIX kernel file lock (`fcntl.flock` with `LOCK_EX | LOCK_NB` on `~/.antigravity-vault/watch.lock`). Before launching a background watcher or entering the watch polling loop, the process must acquire the lock and retain the open file descriptor for the lifetime of the process. In addition, `DaemonManager.start()` and CLI `watch start`/`watch run` must inspect whether the user-level systemd service `agy-switch-watcher.service` is actively running (via `systemctl --user is-active agy-switch-watcher.service`). If active, starting a secondary duplicate watcher must be refused with a diagnostic error directing the user to systemd or `agy-switch watch stop/start`, exiting with status code 1. Upon process termination (SIGTERM, SIGINT, or normal exit), signal handlers and exit traps must cleanly release the lock and close the file descriptor.

#### Scenario: [DAEMON-MUTEX-001] Exclusive lock acquisition on clean daemon startup
- GIVEN no existing watcher daemon process is running
- AND `watch.lock` in the active vault directory is unlocked
- WHEN `LogWatcher.watch_loop()` or `DaemonManager.start()` initializes the daemon
- THEN it successfully acquires an exclusive non-blocking lock via `fcntl.flock` on `watch.lock`
- AND retains the open lock file descriptor throughout daemon execution.

#### Scenario: [DAEMON-MUTEX-002] Secondary daemon startup rejected with exit code 1 when lock is held
- GIVEN a primary watcher daemon process is actively running and holding an exclusive lock on `watch.lock`
- WHEN a secondary watcher daemon process attempts to run `watch run` or `DaemonManager.start()`
- THEN `fcntl.flock` raises `BlockingIOError` or `IOError`
- AND the secondary process catches the exception, outputs an informative error message indicating another watcher daemon is already running
- AND terminates immediately with exit code 1 without corrupting `watch.pid` or spawning duplicate threads.

#### Scenario: [DAEMON-MUTEX-003] Startup rejected when systemd user service is actively running
- GIVEN the systemd user service `agy-switch-watcher.service` is active (`systemctl --user is-active agy-switch-watcher.service` exits 0)
- WHEN a user invokes `agy-switch watch start` or `agy-switch watch run` from the command line
- THEN `DaemonManager` detects that the systemd user service is running
- AND refuses to start an interactive or detached daemon
- AND prints a diagnostic message directing the user to manage the service via systemd or stop it first
- AND exits with status code 1.

#### Scenario: [DAEMON-ERR-001] Lock file unwriteable or permission denied handled gracefully
- GIVEN the vault directory or `watch.lock` file has restrictive permissions preventing write or lock acquisition (`PermissionError` or `OSError`)
- WHEN `LogWatcher` or `DaemonManager` attempts to acquire the mutual exclusion lock
- THEN it logs an actionable error message to stderr describing the permission failure
- AND terminates or refuses startup cleanly with exit code 1 without raising an unhandled stack trace.

#### Scenario: [DAEMON-ERR-002] Malformed or symlinked watch.lock pointing to external path is rejected
- GIVEN `watch.lock` in the vault directory is a dangling symlink or points to a path outside the vault directory
- WHEN `LogWatcher` or `DaemonManager` initializes lock acquisition
- THEN it detects that `watch.lock` is a symlink and unlinks it safely or rejects lock acquisition
- AND terminates cleanly with an error message and exit code 1, preventing symlink traversal attacks.

#### Scenario: [DAEMON-EDGE-001] Stale lock recovery when previous process died unexpectedly without file corruption
- GIVEN a previous watcher daemon terminated abruptly (SIGKILL or host power loss) leaving an existing `watch.lock` file on disk
- AND no active process holds a kernel lock on `watch.lock`
- WHEN a new watcher daemon process initializes
- THEN `fcntl.flock` successfully acquires the exclusive non-blocking lock without deadlock or requiring manual file deletion
- AND daemon startup proceeds normally.

#### Scenario: [DAEMON-EDGE-002] Missing systemctl binary in containerized environment falls back gracefully to file lock
- GIVEN a containerized or minimal host environment where `systemctl` is not installed or not found in PATH
- WHEN `DaemonManager.start()` or CLI `watch start` checks for an active systemd service
- THEN the missing command lookup is handled safely without raising `FileNotFoundError`
- AND daemon startup falls back to POSIX `fcntl.flock` mutual exclusion without interrupting startup.

#### Scenario: [DAEMON-EDGE-003] Rapid concurrent startup race condition resolves deterministically to one daemon
- GIVEN two concurrent processes or threads invoke `DaemonManager.start()` at the exact same millisecond
- WHEN both processes attempt to acquire an exclusive lock on `watch.lock`
- THEN exactly one process successfully acquires the lock and writes its PID
- AND the second process is rejected immediately upon lock contention and exits cleanly with exit code 1.

### Requirement: Strict Test Vault Sandboxing and Log Isolation
Under test suite execution or when `engine.vault_dir` is missing, `None`, unparseable, or an instance of `unittest.mock.MagicMock` / `Mock`, all logging and credential lookup operations in `scripts/agy_switcher/watcher.py` (`append_secure_log_entry`, `LogWatcher._notify_desktop`, `LogWatcher._append_audit_log`) and `scripts/agy_switcher/vault.py` (`AgySwitchEngine`) MUST strictly avoid falling back to `DEFAULT_VAULT_DIR` (`~/.antigravity-vault`). Logging operations in test environments must redirect exclusively to the isolated directory specified by `AGY_VAULT_DIR` or an ephemeral temporary directory. If no isolated directory is set and the runtime is detected as a test runner (e.g., `unittest` in `sys.modules`, `pytest` in `sys.modules`, or `PYTEST_CURRENT_TEST` in environment), log-writing operations must no-op or raise an explicit configuration error rather than polluting the user's production `~/.antigravity-vault/watch.log`.

#### Scenario: [VAULT-ISO-001] Audit log writes strictly to isolated directory when AGY_VAULT_DIR is set
- GIVEN `AGY_VAULT_DIR` is set to an isolated temporary directory path in the environment
- WHEN `LogWatcher._notify_desktop()` or audit logging writes a rotation event log entry
- THEN the log entry is written strictly to `AGY_VAULT_DIR/watch.log`
- AND no write or modification is made to `~/.antigravity-vault/watch.log`.

#### Scenario: [VAULT-ISO-002] Test mock engine without vault_dir does not write to production ~/.antigravity-vault/watch.log
- GIVEN a test execution where `engine` is a `MagicMock` or has `vault_dir=None`
- AND `AGY_VAULT_DIR` is not pointing to `~/.antigravity-vault`
- WHEN `LogWatcher._notify_desktop()` or audit logging is invoked
- THEN logging safely redirects to the configured test sandbox or no-ops without writing to `~/.antigravity-vault/watch.log`
- AND production vault files remain pristine and unpolluted.

#### Scenario: [VAULT-ERR-001] Unwriteable temp vault directory logs error without crashing parent turn runner
- GIVEN the isolated vault directory becomes read-only or raises `OSError` during log append operations
- WHEN `append_secure_log_entry` or `LogWatcher` attempts to record an audit log entry
- THEN it handles the filesystem exception gracefully without raising an uncaught error
- AND does not crash the parent turn runner, daemon polling loop, or agent workflow.

#### Scenario: [VAULT-ERR-002] Empty or whitespace-only AGY_VAULT_DIR environment variable handled safely
- GIVEN `AGY_VAULT_DIR` is set to an empty string `""` or whitespace-only string `"   "` in the environment
- AND a test runner is active (`unittest` or `pytest` present in `sys.modules`)
- WHEN `append_secure_log_entry` or `LogWatcher` resolves the audit log destination
- THEN it rejects the empty string as an invalid vault path without falling back to `DEFAULT_VAULT_DIR`
- AND redirects log writes safely to an ephemeral system temp directory or suppresses audit write operations.

#### Scenario: [VAULT-ERR-003] Filesystem disk full ENOSPC during log append caught gracefully
- GIVEN the filesystem hosting the vault log runs out of space, raising `OSError` with `ENOSPC`
- WHEN `append_secure_log_entry` attempts to flush log records
- THEN it catches the exception and logs an alert to stderr without raising an unhandled exception
- AND allows active rotation and agent execution to proceed uninterrupted.

#### Scenario: [VAULT-EDGE-001] Concurrent test processes write to independent temporary vaults without interference
- GIVEN multiple concurrent test runner processes each configured with distinct temporary `AGY_VAULT_DIR` directories
- WHEN each test process generates rotation events and audit logs simultaneously
- THEN each process writes exclusively to its assigned temporary vault directory
- AND no cross-process file locks, race conditions, or log collisions occur.

#### Scenario: [VAULT-EDGE-002] Dynamic mutation of AGY_VAULT_DIR across test cases rebinds destination hermetically
- GIVEN a test fixture dynamically alters `os.environ["AGY_VAULT_DIR"]` between successive test cases
- WHEN consecutive audit log writes or `LogWatcher` operations are executed
- THEN each operation dynamically resolves the current `AGY_VAULT_DIR` path
- AND no stale file handles or leaked paths write to previous test directories.

### Requirement: End-to-End Bootstrap Installer with Canonical Launcher Binding
The installer script `install.sh` and hook registration APIs in `scripts/agy_switcher/vault.py` (`install_hook` / `install_pre_invocation_hook`) and `scripts/agy_switcher/watcher.py` (`DaemonManager.generate_systemd`) MUST provide an end-to-end, deterministic bootstrap setup that installs the plugin, creates the canonical launcher executable symlink at `~/.local/bin/agy-switch`, registers the Antigravity `PreInvocation` lifecycle hook in `~/.gemini/config/hooks.json` bound to `~/.local/bin/agy-switch hook-pre-invocation`, and optionally generates, installs, and enables the systemd user service `agy-switch-watcher.service` referencing `~/.local/bin/agy-switch watch run`. All hooks, systemd unit files, and external integration points MUST reference the canonical launcher binary path rather than local repository clone paths, guaranteeing operational stability across workspace relocations.

#### Scenario: [BOOTSTRAP-001] Full install configures plugin, canonical launcher symlink, and hooks.json pointing to ~/.local/bin/agy-switch
- GIVEN a clean host machine or local repository clone
- WHEN `install.sh --global` (or `--all`) is executed with complete setup options
- THEN the plugin files or symlink are installed into `~/.gemini/config/plugins/agy-switch`
- AND the canonical launcher executable is symlinked at `~/.local/bin/agy-switch`
- AND `~/.gemini/config/hooks.json` is updated with `agy-switch-auto-rotate` `PreInvocation` hook referencing `~/.local/bin/agy-switch hook-pre-invocation` without repo clone paths.

#### Scenario: [BOOTSTRAP-002] Systemd service unit generation and installation references canonical launcher executable
- GIVEN systemd user services are supported on the host platform
- WHEN `agy-switch watch --install-systemd` or `install.sh --systemd` is executed
- THEN the unit file `~/.config/systemd/user/agy-switch-watcher.service` is generated
- AND the `ExecStart` directive specifies `%h/.local/bin/agy-switch watch run`
- AND references the canonical launcher rather than a Python interpreter invoking a local clone path.

#### Scenario: [BOOTSTRAP-ERR-001] Missing git or python3 fails early with clear actionable diagnostic
- GIVEN a minimal container or host environment where `git` or `python3` (version >= 3.10) is missing
- WHEN `install.sh` is invoked
- THEN the installer checks prerequisites before modifying filesystem state
- AND halts with a non-zero exit code and an informative error message specifying the missing prerequisite package.

#### Scenario: [BOOTSTRAP-ERR-002] Corrupted or unparseable hooks.json creates timestamped backup before recovery
- GIVEN `~/.gemini/config/hooks.json` exists on disk but contains syntax errors or truncated unparseable JSON
- WHEN `install.sh` or `AgySwitchEngine.install_hook()` attempts to register the `PreInvocation` hook
- THEN it detects the JSON decode error without aborting execution
- AND creates a timestamped backup copy `hooks.json.bak.<timestamp>`
- AND initializes a valid hooks schema containing the canonical launcher hook command without crashing.

#### Scenario: [BOOTSTRAP-ERR-003] Existing foreign file at canonical launcher path halts safely with error
- GIVEN a non-symlink regular file or foreign executable already exists at `~/.local/bin/agy-switch` that was not created by agy-switch
- WHEN `install.sh` attempts to install the canonical launcher
- THEN it verifies the existing file before unlinking
- AND refuses to silently overwrite foreign binaries without user confirmation, exiting with an informative error.

#### Scenario: [BOOTSTRAP-EDGE-001] Idempotent re-installation does not duplicate hooks or break existing configs
- GIVEN `~/.gemini/config/hooks.json` already contains custom user hooks alongside an existing `agy-switch-auto-rotate` hook
- AND `~/.local/bin/agy-switch` already exists
- WHEN `install.sh` is executed repeatedly
- THEN the existing non-conflicting hooks in `hooks.json` are preserved without duplication
- AND the launcher symlink is refreshed idempotently
- AND the script exits with code 0.

#### Scenario: [BOOTSTRAP-EDGE-002] Systemd installation on system without active user session bus issues non-fatal warning
- GIVEN a host environment where the systemd user manager or D-Bus session bus is unavailable
- WHEN `install.sh --systemd` or `agy-switch watch --install-systemd` attempts to execute `systemctl --user daemon-reload`
- THEN it catches the command failure and logs an actionable advisory on enabling lingering via `loginctl enable-linger`
- AND completes all other installation steps successfully with exit code 0 rather than aborting.

### Requirement: Refresh-Aware Standby Account Validation in Diagnostics
The Layer 2 token diagnostics in `agy-switch doctor` (`DoctorEngine._check_token_integrity` and `DoctorEngine.run_diagnostics` in `scripts/agy_switcher/doctor.py`) SHALL distinguish between the currently active account and secondary standby accounts while enforcing resilient **Token Identity**, **Refresh-Aware Standby Viability**, and **Failover Audit & Auth Quarantine** checks. For the active account, access token expiry continues to be verified strictly (`[FAIL]` if expired). For standby accounts, if the access token timestamp has expired, the engine inspects `refresh_token`; if a non-empty string `refresh_token` is present, the account is validated as `[PASS]` (Standby Ready), whereas missing, empty, whitespace-only, or non-string `refresh_token` on an expired secondary account returns `[WARN]`. For **Token Identity** across both active and standby accounts: (a) accounts resolving a valid email via `id_token` JWT claim or `id_token_email` evaluate cleanly to `[PASS]` (when unexpired or standby-ready); (b) valid OAuth accounts lacking all email identity (both `id_token` and `id_token_email` absent, `None`, empty `""`, or `id_token` valid JWT lacking an `"email"` claim without `id_token_email`) emit a non-fatal `[WARN]` advisory (never `[FAIL]`) unless superseded by a higher-severity `[FAIL]` such as an expired active token; and (c) accounts containing a present, non-empty `id_token` that fails JWT payload parsing with no valid `id_token_email` remain flagged as corrupt (`[FAIL]` for active, `[WARN]` for secondary). Additionally, `DoctorEngine._check_token_integrity()` SHALL inspect `cache/failover_state.json["auth_errors"]`: if the active account is quarantined in `auth_errors`, it MUST return `[FAIL]` with `agy-switch login <alias>` remediation; if only standby accounts are quarantined in `auth_errors`, it MUST emit `[WARN]` with `agy-switch login <alias>` remediation; and corrupt or unreadable `failover_state.json` MUST be ignored safely without crashing. Finally, `DoctorEngine._check_permissions()` and `DoctorEngine.fix_diagnostics()` SHALL audit and enforce POSIX `0600` permissions on `cache/failover_state.json` and `cache/pending_reload.json` alongside existing vault cache files.

#### Scenario: [DOCTOR-REFRESH-001] Standby accounts with expired access token but valid refresh token pass Layer 2 diagnosis
- GIVEN a registered standby account `"standby-1"` whose `token.json` has an expired access token timestamp (`expiry` in the past)
- AND `token.json` contains a valid, non-empty `refresh_token` string
- AND the active account `"primary-acc"` has an unexpired access token
- WHEN `DoctorEngine._check_token_integrity()` or `agy-switch doctor` executes Layer 2 diagnostics
- THEN `"standby-1"` is classified as healthy standby ready
- AND the diagnostic status for token integrity returns `[PASS]` without issuing warnings.

#### Scenario: [DOCTOR-REFRESH-002] Active account with expired access token is flagged as [FAIL]
- GIVEN the currently active account `"primary-acc"` whose `token.json` has an expired access token timestamp
- WHEN `DoctorEngine._check_token_integrity()` evaluates token integrity
- THEN it detects that the active account has an expired access token
- AND returns status `[FAIL]` with a message indicating the active account token is expired and requires refresh.

#### Scenario: [DOCTOR-ERR-001] Secondary account missing refresh_token or corrupt JSON flagged as [WARN]
- GIVEN a secondary standby account `"standby-broken"` whose access token is expired
- AND `refresh_token` is missing, empty, or `None`
- WHEN `DoctorEngine._check_token_integrity()` evaluates the account vault
- THEN it classifies `"standby-broken"` as unhealthy
- AND returns status `[WARN]` indicating that the secondary account is expired without a valid refresh token.

#### Scenario: [DOCTOR-ERR-002] Whitespace-only or non-string refresh token on expired secondary account flagged as [WARN]
- GIVEN a secondary standby account whose access token timestamp is expired
- AND `token["refresh_token"]` is a whitespace-only string `"   "`, empty string `""`, or non-string scalar (integer or boolean)
- WHEN `DoctorEngine._check_token_integrity()` evaluates token integrity
- THEN it detects that the refresh token is invalid or non-renewable
- AND returns status `[WARN]` indicating the secondary account has no renewable refresh token.

#### Scenario: [DOCTOR-ERR-003] Unreadable account directory or permission error in vault handled gracefully
- GIVEN an account subdirectory in `accounts/` has restrictive permissions causing `PermissionError` when reading `token.json`
- WHEN `DoctorEngine._check_token_integrity()` inspects vault accounts
- THEN it catches the filesystem exception cleanly without throwing an unhandled exception
- AND returns status `[FAIL]` with details specifying the inaccessible account directory.

#### Scenario: [DOCTOR-EDGE-001] Account with non-standard refresh token payload schema handled gracefully
- GIVEN a vault account where `token.json` contains non-standard whitespace, extra attributes, or nested structure around `refresh_token`
- WHEN `DoctorEngine._check_token_integrity()` parses and validates the token schema
- THEN it extracts and strips the refresh token safely without throwing unhandled exceptions
- AND correctly evaluates standby viability according to refresh token presence.

#### Scenario: [DOCTOR-EDGE-002] Active account pointer referencing non-existent account directory flagged as [FAIL]
- GIVEN the active account pointer file `active` specifies an alias `"missing-acc"` that does not exist in the `accounts/` directory
- WHEN `DoctorEngine._check_token_integrity()` evaluates token integrity
- THEN it detects that the configured active account is missing from the vault
- AND returns status `[FAIL]` with an actionable message directing the operator to select a valid active account.

#### Scenario: [DOCTOR-ID-001] Accounts with id_token_email and omitted id_token resolve identity cleanly as [PASS]
- GIVEN an active account `"primary-acc"` (unexpired) and a standby account `"standby-1510"` (unexpired or expired with valid `refresh_token`)
- AND their `token.json` payloads omit `id_token` while providing `"id_token_email": "user@example.com"`
- WHEN `DoctorEngine.run_diagnostics()` (`_check_token_integrity`) or `agy-switch doctor` executes Layer 2 diagnostics
- THEN both accounts resolve **Token Identity** cleanly without error
- AND the diagnostic status for token integrity returns `[PASS]`.

#### Scenario: [DOCTOR-ID-002] Valid OAuth account lacking both id_token and id_token_email emits non-fatal [WARN]
- GIVEN an unexpired active account or standby-ready account whose `token.json` contains valid `token` (`access_token`, `refresh_token`, `token_type`, `expiry`) and `auth_method`
- AND both `id_token` and `id_token_email` are absent, `None`, or empty `""`
- WHEN `DoctorEngine.run_diagnostics()` (`_check_token_integrity`) or `agy-switch doctor` evaluates token integrity
- THEN it classifies the missing **Token Identity** as a non-fatal advisory
- AND returns status `[WARN]` (never `[FAIL]`) noting that email identity is unavailable for the account.

#### Scenario: [DOCTOR-ID-ERR-001] Present non-empty malformed id_token JWT without id_token_email remains flagged as corrupt
- GIVEN an account whose `token.json` contains `"id_token": "malformed-not-jwt"` (unparseable JWT payload) and no valid `id_token_email`
- WHEN `DoctorEngine.run_diagnostics()` (`_check_token_integrity`) or `agy-switch doctor` evaluates token integrity
- THEN it flags the account as corrupt (`[FAIL]` if active, `[WARN]` if secondary) with details indicating an invalid `id_token` JWT.

#### Scenario: [DOCTOR-ID-EDGE-001] Non-string or whitespace-only id_token_email without id_token emits [WARN] advisory
- GIVEN an unexpired active account whose `token.json` omits `id_token` and sets `id_token_email` to `"   "` or non-string `12345`
- WHEN `DoctorEngine.run_diagnostics()` (`_check_token_integrity`) evaluates token integrity
- THEN invalid `id_token_email` values are ignored safely without crashing
- AND the missing **Token Identity** emits `[WARN]` (never `[FAIL]` and not `[PASS]`).

#### Scenario: [DOCTOR-QUARANTINE-001] Active account listed in `failover_state.json["auth_errors"]` flagged as `[FAIL]` with login remediation
- GIVEN active account `"automator"` has valid local `token.json` schema and expiry
- AND `cache/failover_state.json` contains `"automator"` under `"auth_errors"`
- WHEN `DoctorEngine._check_token_integrity()` runs Layer 2 diagnostics
- THEN it returns status `[FAIL]` (`can_fix=False`) referencing `'automator'` and advising `'agy-switch login automator'`.

#### Scenario: [DOCTOR-QUARANTINE-002] Standby account listed in `failover_state.json["auth_errors"]` flagged as `[WARN]` with login remediation
- GIVEN active account `"honey"` is healthy and standby account `"automator"` is listed under `cache/failover_state.json["auth_errors"]`
- WHEN `DoctorEngine._check_token_integrity()` runs Layer 2 diagnostics
- THEN it returns status `[WARN]` (`can_fix=False`) referencing `'automator'` and advising `'agy-switch login automator'`.

#### Scenario: [DOCTOR-QUARANTINE-003] `DoctorEngine._check_permissions()` and `fix_diagnostics()` audit and repair POSIX `0600` permissions on `failover_state.json` and `pending_reload.json`
- GIVEN `cache/failover_state.json` or `cache/pending_reload.json` exists with permissive mode `0644`
- WHEN `DoctorEngine.run_diagnostics()` runs and `DoctorEngine.fix_diagnostics()` repairs fixable items
- THEN `_check_permissions()` initially detects the insecure mode as `[FAIL]` (`can_fix=True`)
- AND `fix_diagnostics()` restores both files to mode `0600` so re-running diagnostics passes Layer 1.

#### Scenario: [DOCTOR-QUARANTINE-ERR-001] Corrupt or unreadable `cache/failover_state.json` ignored safely by `_check_token_integrity()` without crashing
- GIVEN `cache/failover_state.json` contains invalid JSON or non-dict structure
- WHEN `DoctorEngine._check_token_integrity()` evaluates vault accounts
- THEN it ignores the corrupt `failover_state.json` safely without throwing an exception and evaluates token integrity normally.

### Requirement: Resilient Token Schema Validation and Corrupt JWT Distinction
The Layer 0 credential validation primitives `validate_token_dict()` and `validate_token_file()` in `scripts/agy_switcher/security.py` MUST validate OAuth credential payloads against the `agy` 1.5.10 schema by requiring a JSON object with a non-empty `auth_method` string and a `token` dict containing non-empty `access_token`, `refresh_token`, `token_type`, and `expiry` fields, while treating `id_token` as optional (`omitempty`, `None`, or `""`). When `id_token` is omitted, `None`, or `""`, validation MUST succeed regardless of whether `id_token_email` is present. When `id_token` is present as a non-empty value, it MUST parse as a structurally valid JWT payload (at least two dot-separated segments where the second segment base64url-decodes into a JSON object, even if that JSON object omits an `"email"` claim); if `id_token` is non-empty and either non-string or fails JWT payload parsing when no valid trimmed non-empty string `id_token_email` is present, validation MUST reject the payload as corrupt by raising `InvalidTokenError` (or returning `(False, error_message)` when `return_tuple=True`).

#### Scenario: [TOKEN-ID-001] Valid OAuth token with id_token_email and omitted id_token passes validation
- GIVEN a token dictionary containing valid `token` fields (`access_token`, `refresh_token`, `token_type`, `expiry`) and `auth_method`
- AND `id_token` is omitted from the payload while `id_token_email` is `"user@example.com"`
- WHEN `validate_token_dict(data)` or `validate_token_file(path)` is invoked
- THEN validation succeeds and returns the validated token dictionary without raising `InvalidTokenError`.

#### Scenario: [TOKEN-ID-002] Valid OAuth token with None or empty id_token passes validation
- GIVEN a token dictionary containing valid `token` fields (`access_token`, `refresh_token`, `token_type`, `expiry`) and `auth_method`
- AND `id_token` is explicitly `None` or the empty string `""`
- WHEN `validate_token_dict(data, return_tuple=True)` is invoked
- THEN it returns `(True, None)`.

#### Scenario: [TOKEN-ID-003] Valid OAuth token missing both id_token and id_token_email passes structural validation
- GIVEN a token dictionary containing valid `token` fields (`access_token`, `refresh_token`, `token_type`, `expiry`) and `auth_method`
- AND neither `id_token` nor `id_token_email` is present in the payload
- WHEN `validate_token_dict(data)` is invoked
- THEN structural validation succeeds and returns the token dictionary without raising `InvalidTokenError`.

#### Scenario: [TOKEN-ID-ERR-001] Present non-empty malformed id_token JWT without valid id_token_email rejected as corrupt
- GIVEN a token dictionary containing valid `token` fields and `auth_method`
- AND `id_token` is a non-empty malformed string `"not.a.valid.jwt"` (or non-string scalar `12345`) that fails JWT payload parsing
- AND no valid trimmed non-empty string `id_token_email` field is present
- WHEN `validate_token_dict(data)` is invoked
- THEN it distinguishes the corrupt JWT from an omitted field and raises `InvalidTokenError`.

#### Scenario: [TOKEN-ID-ERR-002] Missing required OAuth token fields or auth_method still raises InvalidTokenError
- GIVEN a token dictionary missing `refresh_token` inside `token` or missing `auth_method`
- WHEN `validate_token_dict(data)` is invoked
- THEN validation fails and raises `InvalidTokenError` specifying the missing required field.

#### Scenario: [TOKEN-ID-EDGE-001] Non-string or whitespace-only id_token_email does not rescue corrupt id_token while valid JWT without email passes
- GIVEN a token dictionary with malformed `id_token` (`"not.a.valid.jwt"`) and `id_token_email` set to whitespace `"   "` or non-string `12345`
- AND a second token dictionary with a structurally valid `id_token` JWT whose base64url-decoded JSON payload is `{"sub": "no_email_user"}` without an `"email"` claim
- WHEN `validate_token_dict()` is invoked on each payload
- THEN the first payload raises `InvalidTokenError` because invalid `id_token_email` does not rescue a corrupt `id_token`
- AND the second payload passes validation cleanly.

### Requirement: Layered Token Identity Resolution and UserInfo Persistence
The security module (`scripts/agy_switcher/security.py`), vault engine (`scripts/agy_switcher/vault.py`), and In-Process SDK Facade (`scripts/agy_switcher/api.py`) MUST resolve **Token Identity** (`email`) using the pure Layer 0 public interface `resolve_token_email(data)` (zero network I/O, exported in `security.py`, `scripts/agy_switcher/__init__.py`, and `scripts/agy_switch.py`): (1) the trimmed non-empty `email` claim decoded from `id_token` JWT via `extract_email_from_jwt()` when present and valid, falling back to (2) a trimmed non-empty string `id_token_email` field (ignoring non-string or whitespace-only values). During account registration (`AgySwitchEngine.login_account()` and `AgySwitchEngine.add_account()`), when neither `id_token` nor `id_token_email` yields an email, the engine MUST attempt a best-effort HTTP GET lookup against the Google UserInfo API using `Authorization: Bearer <access_token>` (via Python stdlib `urllib.request` with a bounded timeout). If UserInfo returns a JSON object with a valid non-empty string `email`, the engine MUST persist it into the vault account's `token.json` under `"id_token_email"` via atomic write (`0600` permissions); if UserInfo fails due to network error, timeout, HTTP 4xx/5xx error, or malformed/non-JSON response, registration MUST still succeed gracefully with `email=None`. All read and transfer surfaces (`AgySwitchEngine.list_accounts()`, `AgySwitchEngine.switch_tokens()`, `AgySwitchEngine.switch_account()`, `AgySwitchEngine.get_status()`, `AgySwitchEngine.harvest_live_tokens_if_needed()`, `api.get_active_account()`, `api.list_accounts()`, `api.get_accounts()`) MUST surface **Token Identity** through `resolve_token_email()` and preserve `id_token_email` across dual-destination swaps without marking `id_token_email`-backed accounts as corrupt.

#### Scenario: [IDENTITY-RES-001] Layered Token Identity resolution prefers id_token JWT and falls back to id_token_email
- GIVEN a token payload without `id_token` (or with empty `id_token`) and with `"id_token_email": "fallback@example.com"`
- AND a second token payload containing a valid `id_token` JWT with `"email": "jwt@example.com"` alongside `"id_token_email": "legacy@example.com"`
- WHEN `resolve_token_email()` is evaluated on each payload
- THEN it returns `"fallback@example.com"` for the first payload and `"jwt@example.com"` for the second payload.

#### Scenario: [IDENTITY-RES-002] Best-effort Google UserInfo lookup persists resolved email as id_token_email during login and add
- GIVEN an account is registered via `AgySwitchEngine.login_account()` or `AgySwitchEngine.add_account()` with a valid OAuth token lacking both `id_token` and `id_token_email`
- AND the Google UserInfo API responds with `{"email": "userinfo@example.com"}` for the `access_token`
- WHEN the vault persists `token.json`
- THEN `"id_token_email": "userinfo@example.com"` is written atomically (`0600`) to `accounts/<alias>/token.json`
- AND the returned account metadata includes `"email": "userinfo@example.com"`.

#### Scenario: [IDENTITY-ERR-001] UserInfo API network failure or timeout degrades gracefully without failing registration
- GIVEN an account is added or logged in with a valid OAuth token lacking both `id_token` and `id_token_email`
- AND the Google UserInfo API request raises `urllib.error.URLError`, `TimeoutError`, or returns HTTP `503`
- WHEN `AgySwitchEngine.add_account()` or `AgySwitchEngine.login_account()` completes registration
- THEN the failure is handled gracefully without raising an unhandled exception or aborting registration
- AND the valid OAuth credentials are saved to `accounts/<alias>/token.json` with `email=None`.

#### Scenario: [IDENTITY-ERR-002] UserInfo HTTP 401/500 or non-JSON response degrades gracefully without corrupting token.json
- GIVEN an account is registered with a valid OAuth token lacking both `id_token` and `id_token_email`
- AND the Google UserInfo API returns HTTP `401`, HTTP `500`, malformed non-JSON body `"<html>error</html>"`, or `{"email": "   "}`
- WHEN `AgySwitchEngine.add_account()` or `AgySwitchEngine.login_account()` completes registration
- THEN registration succeeds with `email=None`
- AND `accounts/<alias>/token.json` is written atomically (`0600`) without inserting an invalid `id_token_email` value.

#### Scenario: [IDENTITY-EDGE-001] Vault and SDK read surfaces report id_token_email cleanly
- GIVEN an active or standby vault account `"work"` whose `token.json` has no `id_token` but contains `"id_token_email": "work@example.com"`
- WHEN `AgySwitchEngine.list_accounts()`, `AgySwitchEngine.switch_tokens()`, `AgySwitchEngine.get_status()`, `api.get_active_account()`, `api.list_accounts()`, or `api.get_accounts()` is called
- THEN each surface reports `email="work@example.com"`
- AND `api.get_active_account()` returns `AccountInfo` with `status="active"` and `is_active=True` rather than `"corrupt"`.

#### Scenario: [IDENTITY-EDGE-002] Live token harvest and dual-destination swap preserve id_token_email atomically
- GIVEN an empty vault with a live Desktop or CLI token file lacking `id_token` and containing `"id_token_email": "harvest@example.com"`
- WHEN `AgySwitchEngine.harvest_live_tokens_if_needed()` imports the token and `AgySwitchEngine.switch_tokens()` subsequently swaps credentials
- THEN `accounts/<alias>/token.json`, `desktop_token_file`, and `cli_token_file` all preserve `"id_token_email": "harvest@example.com"` intact via `0600` atomic writes
- AND the harvested alias is derived from `"harvest"` rather than falling back to `"primary"`.

### Requirement: Standardized Interactive TTY OAuth Login and Alias Validation
`build_parser()` and `handle_login()` in `scripts/agy_switcher/cli.py` alongside `AgySwitchEngine.login_account()` in `scripts/agy_switcher/vault.py` MUST establish `agy-switch login <alias>` as the sole official interactive login flow. `build_parser()` MUST register only positional `name`, `--agy-binary`, `--timeout`, and `--force` on the `login` subcommand, omitting `--headless` and `--auth-code` without deprecation stubs. `handle_login()` MUST delegate directly to `engine.login_account(alias, agy_binary=args.agy_binary, timeout=args.timeout, force=force)` and emit formatted account/vault details to `stdout` (exit code `0`) or formatted errors to `stderr` (exit code `1`) on `AgySwitchError` or `KeyboardInterrupt`. `AgySwitchEngine.login_account()` MUST call `self.validate_account_alias(name)` (normalizing leading/trailing whitespace on valid aliases) before checking vault directory existence, prompting for overwrite confirmation, resolving the `agy` binary, or spawning any subprocess—rejecting non-strings, empty strings, whitespace-only names, internal spaces, path traversal (`..`, `/`, `\`), invalid characters, or aliases exceeding 64 characters. When valid and overwrite-cleared, `login_account()` MUST spawn `agy` in an isolated temporary `HOME` directory inheriting `stdin=sys.stdin`, `stdout=sys.stdout`, and `stderr=sys.stderr` directly to the user's TTY, enforce `timeout` and `KeyboardInterrupt` termination cleanup, validate the resulting token file (`antigravity-oauth-token` or `jetski-standalone-oauth-token`) via `validate_token_file()`, resolve and persist **Token Identity**, and atomically write `accounts/<alias>/token.json` (`0600`) as Standby without altering the active account pointer or leaving partial vault directories on token validation or write failure. `scripts/agy_switcher/vault.py` MUST NOT define `login_headless`, `_run_headless_agy_session`, or headless PKCE stream-scraping helpers.

#### Scenario: [TTY-LOGIN-001] Standard interactive TTY login delegates to login_account and saves 0600 Standby token
- GIVEN no existing account `"work-acc"` in the vault and an executable `agy` binary
- WHEN `agy-switch login work-acc` (via `main(["login", "work-acc"])` / `handle_login()`) runs interactively
- THEN `handle_login()` invokes `engine.login_account("work-acc", agy_binary=None, timeout=600, force=False)`
- AND `login_account()` spawns `agy` in an isolated temporary `HOME` with `stdin=sys.stdin`, `stdout=sys.stdout`, and `stderr=sys.stderr` inherited directly
- AND validates the generated token, persists `accounts/work-acc/token.json` atomically with `0600` permissions as Standby without modifying the active account pointer, and exits with code `0`.

#### Scenario: [TTY-LOGIN-002] Retired headless flags and PKCE stream-scraping symbols are cleanly absent from CLI and vault
- GIVEN the `agy_switcher.cli` and `agy_switcher.vault` modules after headless login retirement
- WHEN `build_parser()` inspects the `login` subparser and callers inspect `AgySwitchEngine` and `agy_switcher.vault`
- THEN `--headless` and `--auth-code` are absent from `login` parser options
- AND `login_headless`, `_run_headless_agy_session`, `_extract_pkce_oauth_url`, and `_StreamEventQueue` are absent from `AgySwitchEngine` and `agy_switcher.vault`.

#### Scenario: [TTY-LOGIN-ERR-001] Invalid alias or path traversal rejected by validate_account_alias before overwrite prompt or agy spawn
- GIVEN an invalid alias such as `"../bad"`, `".."`, `"my account"`, `""`, or a 65-character string
- WHEN `AgySwitchEngine.login_account(alias)` or `main(["login", alias])` is invoked
- THEN `AgySwitchEngine.validate_account_alias()` raises `AgySwitchError` before checking vault overwrite prompts, resolving `agy`, or calling `subprocess.Popen`
- AND `main(["login", alias])` writes an error to `stderr` and exits with code `1` leaving the vault unmodified.

#### Scenario: [TTY-LOGIN-ERR-002] Retired --headless or --auth-code CLI flags rejected by argparse without deprecation stub
- GIVEN a user or script invokes `agy-switch login bot-acc --headless` or `agy-switch login bot-acc --auth-code stale`
- WHEN `build_parser()` parses the CLI arguments
- THEN `argparse` rejects the unrecognized flag directly with exit code `2` and standard unrecognized-arguments diagnostics on `stderr`
- AND neither spawns `agy` nor prints a deprecation wrapper message.

#### Scenario: [TTY-LOGIN-ERR-003] Missing agy binary, tokenless exit, timeout, or KeyboardInterrupt fails cleanly without partial vault state
- GIVEN a valid alias `"fail-acc"` where `agy` is missing, exits (zero or non-zero) without producing a token file, exceeds `timeout`, or receives `KeyboardInterrupt`
- WHEN `AgySwitchEngine.login_account("fail-acc", ...)` executes
- THEN it terminates and cleans up any running child `agy` process cleanly and raises `AgySwitchError`
- AND `main(["login", "fail-acc"])` prints the formatted error to `stderr`, exits with code `1`, and leaves no partial `accounts/fail-acc` directory in the vault.

#### Scenario: [TTY-LOGIN-ERR-004] Corrupt token output or vault write permission failure aborts without partial vault state
- GIVEN a valid alias `"bad-tok"` where `agy` writes malformed JSON / missing OAuth fields to the token file or where writing `accounts/bad-tok/token.json` encounters `PermissionError` / `OSError`
- WHEN `AgySwitchEngine.login_account("bad-tok", ...)` or `main(["login", "bad-tok"])` executes
- THEN `validate_token_file()` raises `InvalidTokenError` (or vault persistence raises `AgySwitchError`)
- AND no empty or partial `accounts/bad-tok` directory remains in the vault, and `main()` exits with code `1` after printing the error to `stderr`.

#### Scenario: [TTY-LOGIN-EDGE-001] Overwrite confirmation aborts before agy spawn on decline, EOF, or KeyboardInterrupt and replaces atomically on confirm or --force
- GIVEN an account `"existing-acc"` already exists in the vault with prior credentials
- WHEN `AgySwitchEngine.login_account("existing-acc", ...)` or `main(["login", "existing-acc"])` is invoked without `force=True` and the user declines (`"n"`, `""`, `EOFError`, or `KeyboardInterrupt`), and subsequently is invoked with `"y"` or `force=True` (including `jetski-standalone-oauth-token` fallback)
- THEN declining, EOF, or `KeyboardInterrupt` aborts before resolving `agy` or calling `subprocess.Popen`, preserving the prior `token.json` and exiting with code `1` in `main()`
- AND confirming (`"y"`) or `force=True` spawns `agy`, accepts `jetski-standalone-oauth-token` fallback if `antigravity-oauth-token` is absent, and atomically overwrites `accounts/existing-acc/token.json` with `0600` permissions.

#### Scenario: [TTY-LOGIN-EDGE-002] Valid alias whitespace normalization and failed --force overwrite preserves prior token
- GIVEN a valid alias `" work-acc "` with surrounding whitespace and an existing `"existing-acc"` vault account with prior `token.json`
- WHEN `AgySwitchEngine.login_account(" work-acc ")` succeeds and `AgySwitchEngine.login_account("existing-acc", force=True)` subsequently fails because `agy` exits without producing a token or times out
- THEN `" work-acc "` is normalized to `"work-acc"` under `accounts/work-acc/token.json`
- AND the failed `--force` overwrite preserves `accounts/existing-acc/token.json` completely intact.

### Requirement: Permanent Retirement of Legacy OOB OAuth URL Builders
The legacy Out-Of-Band OAuth URL builders `build_oauth_url()` in `scripts/agy_switcher/telemetry.py`, `_get_default_oauth_url()` in `scripts/agy_switcher/cli.py`, and `AgySwitchEngine.get_oauth_url()` in `scripts/agy_switcher/vault.py` constructing `redirect_uri=urn:ietf:wg:oauth:2.0:oob` MUST remain permanently retired so no active CLI, telemetry, or vault code path defines or emits Google-blocked OOB URLs, while retaining `DEFAULT_CLIENT_ID` in `scripts/agy_switcher/telemetry.py` for `QuotaClient`.

#### Scenario: [OOB-RETIRE-003] No active CLI or vault code path emits urn:ietf:wg:oauth:2.0:oob URLs while DEFAULT_CLIENT_ID remains
- GIVEN the `agy_switcher` codebase after retiring `build_oauth_url()`, `_get_default_oauth_url()`, and `AgySwitchEngine.get_oauth_url()`
- WHEN CLI login and telemetry flows execute
- THEN no module emits or falls back to `redirect_uri=urn:ietf:wg:oauth:2.0:oob` URLs
- AND `DEFAULT_CLIENT_ID` remains defined and non-empty in `scripts/agy_switcher/telemetry.py`.

#### Scenario: [OOB-RETIRE-ERR-002] Retired OOB helper symbols remain absent from cli, vault, and telemetry modules
- GIVEN the imported `agy_switcher.cli`, `agy_switcher.vault`, and `agy_switcher.telemetry` modules
- WHEN callers inspect module and `AgySwitchEngine` attributes
- THEN `_get_default_oauth_url`, `build_oauth_url`, and `AgySwitchEngine.get_oauth_url` are absent (`hasattr(...)` is `False`).

#### Scenario: [OOB-RETIRE-EDGE-001] Standard TTY login failure never falls back to static OOB OAuth URL output
- GIVEN `agy-switch login fail-acc` fails because `agy` is missing or exits without creating a token file
- WHEN `handle_login()` reports the failure to `stderr`
- THEN neither `stdout` nor `stderr` contains `urn:ietf:wg:oauth:2.0:oob` or any statically constructed Google OAuth URL.

### Requirement: Atomic Check-Before-Write and Pre-Flight Auth Gate in Account Switching
The vault switching engine (`AgySwitchEngine.switch_tokens` in `scripts/agy_switcher/vault.py`), telemetry probe (`probe_account_auth` and `QuotaClient` in `scripts/agy_switcher/telemetry.py`), exception hierarchy (`AccountAuthError(AgySwitchError)` in `scripts/agy_switcher/exceptions.py`), In-Process SDK Facade (`switch_account` in `scripts/agy_switcher/api.py`), and CLI dispatcher (`build_parser` and `main` in `scripts/agy_switcher/cli.py`) MUST enforce atomic check-before-write ordering and a **Pre-Flight Auth Gate**. Before `AgySwitchEngine.switch_tokens(name, restart_server=True, force=False, verify_auth=False, force_restart_now=False)` calls `_sync_tokens_to_targets()`, `set_active_name()`, or updates legacy `state.json`, it MUST complete all pre-flight checks in order: (1) verify the target account directory and `token.json` exist (`AccountNotFoundError`); (2) validate token JSON schema via `validate_token_file()` (`InvalidTokenError`); (3) verify turn safety via `verify_turn_safety(force=force)` whenever `restart_server=True` and the switch is not downgraded to deferred safe-turn reload (`ActiveTurnRunningError`); and (4) when `verify_auth=True`, run `probe_account_auth(self, name)`. Any pre-flight rejection MUST leave live Desktop/CLI token files, `~/.antigravity-vault/active`, and legacy `state.json` unmodified. Manual CLI invocations (`agy-switch switch <name>`) MUST default `verify_auth=True`, whereas `AgySwitchEngine.switch_tokens(..., verify_auth=False)` and `api.py:switch_account()` MUST retain `verify_auth=False` by default so internal callers (`hook.py`, `watcher.py`) and SDK consumers avoid redundant HTTP round-trips. On HTTP `401` or `403`, `probe_account_auth` MUST execute 1x best-effort `QuotaClient.refresh_access_token()` if a non-empty `refresh_token` is present (persisting the updated access token via `update_account_access_token()` and re-probing); if still rejected with a confirmed authentication error (`401`/`403` or `invalid_grant`), `switch_tokens()` MUST fail closed by raising `AccountAuthError` instructing `agy-switch login <alias>`, persist `auth_errors[<alias>]` in `cache/failover_state.json`, and abort before disk mutation or server restart. Transient network, timeout, or HTTP `5xx` errors MUST emit a warning to `stderr` and fail-open so offline manual switching remains operational. The CLI MUST expose `--skip-auth-check` strictly decoupled from `--force` (which governs active-turn override only).

#### Scenario: [AUTH-GATE-001] Healthy pre-flight auth probe permits atomic token switch and server reload
- GIVEN vault account `"work-acc"` with a valid `token.json` whose CloudCode quota probe returns HTTP 200
- AND `language_server` is idle outside an active turn
- WHEN the operator runs `agy-switch switch work-acc` (defaulting `verify_auth=True`)
- THEN `probe_account_auth()` validates `"work-acc"` against CloudCode before any disk write occurs
- AND `AgySwitchEngine.switch_tokens()` atomically updates Desktop and CLI tokens, updates `active` to `"work-acc"`, and restarts `language_server`
- AND the command exits with code `0`.

#### Scenario: [AUTH-GATE-002] Expired access token auto-refreshed once during pre-flight auth check succeeds and persists updated token
- GIVEN vault account `"work-acc"` has an expired `access_token` returning HTTP `401` on initial `QuotaClient.fetch_quota()` and a valid `refresh_token`
- AND `QuotaClient.refresh_access_token()` succeeds with a fresh `access_token` that passes the subsequent quota probe
- WHEN `agy-switch switch work-acc` executes with `verify_auth=True`
- THEN `update_account_access_token()` persists the refreshed token to `"work-acc/token.json"`
- AND `switch_tokens()` proceeds to activate `"work-acc"` across Desktop/CLI targets and returns exit code `0`.

#### Scenario: [AUTH-GATE-003] Explicit `--skip-auth-check` bypasses live HTTP probe while preserving turn safety check
- GIVEN vault account `"offline-acc"` with valid local token JSON schema
- AND `QuotaClient.fetch_quota()` would fail with HTTP `403` if called
- WHEN the operator runs `agy-switch switch offline-acc --skip-auth-check`
- THEN `probe_account_auth()` is not invoked (`verify_auth=False`)
- AND `verify_turn_safety()` is still enforced before writing tokens if `language_server` is busy without `--force`
- AND when the server is idle, `"offline-acc"` is activated cleanly with exit code `0`.

#### Scenario: [AUTH-GATE-004] SDK `api.py:switch_account()` and engine default `verify_auth=False` avoid redundant HTTP round-trips
- GIVEN vault account `"standby-acc"` with valid local token JSON schema and an idle `language_server`
- WHEN an in-process consumer calls `api.switch_account("standby-acc", reload_server=False)` or `engine.switch_tokens("standby-acc")` without `verify_auth=True`
- THEN no live CloudCode HTTP request is issued by `switch_tokens()`
- AND the atomic **Disk-Only Swap** or engine switch completes without extra network latency.

#### Scenario: [AUTH-GATE-ERR-001] Atomic check-before-write prevents disk mutation when `ActiveTurnRunningError` rejects switch
- GIVEN active account `"primary-acc"` with live Desktop and CLI token files matching `"primary-acc"` and legacy `state.json` active set to `"primary-acc"`
- AND `ProcessSupervisor.is_server_busy()` returns `True`
- WHEN `agy-switch switch standby-acc --skip-auth-check` is invoked without `--force` from an external terminal
- THEN `AgySwitchEngine.switch_tokens()` raises `ActiveTurnRunningError` prior to mutating any live target files or vault state
- AND live Desktop/CLI token files, `~/.antigravity-vault/active`, and legacy `state.json` remain unchanged pointing to `"primary-acc"`
- AND the CLI exits with code `2`.

#### Scenario: [AUTH-GATE-ERR-002] Confirmed HTTP 401/403 after 1x refresh failure raises `AccountAuthError`, quarantines alias, and leaves live tokens untouched
- GIVEN active account `"healthy-acc"` and standby account `"revoked-acc"` whose `token.json` has valid schema
- AND CloudCode returns HTTP `403` (or `401` followed by `invalid_grant` on the single `refresh_access_token()` attempt)
- WHEN the operator runs `agy-switch switch revoked-acc`
- THEN `switch_tokens()` raises `AccountAuthError` prior to mutating live Desktop/CLI tokens, `active`, or restarting `language_server`
- AND `cache/failover_state.json` records `"revoked-acc"` under `auth_errors` with mode `0600`
- AND stderr directs the operator to run `agy-switch login revoked-acc` and the CLI exits with code `1`.

#### Scenario: [AUTH-GATE-ERR-003] `--force` alone does not bypass Pre-Flight Auth Gate on revoked credentials
- GIVEN standby account `"revoked-acc"` whose credentials fail CloudCode auth with confirmed HTTP `403`
- WHEN the operator runs `agy-switch switch revoked-acc --force` without `--skip-auth-check`
- THEN `verify_auth=True` remains active because `--force` is strictly decoupled from `--skip-auth-check`
- AND `switch_tokens()` fails closed with `AccountAuthError` (exit code `1`), leaving live tokens untouched and `language_server` running.

#### Scenario: [AUTH-GATE-EDGE-001] Transient network timeout or HTTP 5xx during pre-flight probe fails open with stderr warning
- GIVEN standby account `"work-acc"` with valid local token JSON schema
- AND CloudCode raises `URLError`, `TimeoutError`, or returns HTTP `503` during `probe_account_auth()`
- WHEN `agy-switch switch work-acc` executes
- THEN `probe_account_auth()` emits a warning to `stderr` explaining the transient network failure and fails open
- AND `switch_tokens()` proceeds to switch live tokens to `"work-acc"` and exits with code `0`.

#### Scenario: [AUTH-GATE-EDGE-002] Missing `refresh_token` on HTTP 401/403 skips refresh attempt and fails closed immediately with `AccountAuthError`
- GIVEN standby account `"no-refresh-acc"` whose `token.json` has an empty or whitespace-only `refresh_token` and returns HTTP `401`
- WHEN `agy-switch switch no-refresh-acc` runs with `verify_auth=True`
- THEN `QuotaClient.refresh_access_token()` is not called
- AND `switch_tokens()` immediately fails closed with `AccountAuthError`, records `"no-refresh-acc"` in `cache/failover_state.json["auth_errors"]`, and leaves live tokens unchanged.

### Requirement: Ancestor Lineage Self-Kill Guard and Deferred Safe-Turn Reload
The process supervisor (`ProcessSupervisor.is_invoked_by_language_server` in `scripts/agy_switcher/supervisor.py`), security parser (`parse_pending_reload_payload` in `scripts/agy_switcher/security.py`), vault engine (`AgySwitchEngine.switch_tokens`, `AgySwitchEngine.write_pending_reload` in `scripts/agy_switcher/vault.py`), PreInvocation hook (`handle_pre_invocation_hook` in `scripts/agy_switcher/hook.py`), In-Process SDK Facade (`auto_rotate_on_exhaustion`, `switch_account` in `scripts/agy_switcher/api.py`), CLI router (`scripts/agy_switcher/cli.py`), and agent documentation (`skills/agy-switch/SKILL.md`) MUST prevent in-turn synchronous parricide and guarantee Desktop–CLI convergence via **Deferred Safe-Turn Reload** (amending ADR 0021 via ADR 0022). The default TTL for `cache/pending_reload.json` MUST be extended from `120` seconds to **`1800` seconds (30 minutes)** (`DEFAULT_PENDING_RELOAD_TTL_SECONDS = 1800`) across `write_pending_reload()`, `parse_pending_reload_payload()`, and CLI output messages so long-running agent turns do not prematurely expire queued reloads. `cache/pending_reload.json` (mode `0600`, TTL `1800` seconds) MUST be enqueued across ALL non-immediate-restart switch paths: (1) in-turn manual/auto CLI switches (`is_invoked_by_language_server() == True`, `reason="in_turn_deferred_reload"`); (2) `PreInvocation` hook rotations in `handle_pre_invocation_hook()` (`reason="hook_pre_invocation_rotation"`); and (3) In-Process SDK swaps in `api.auto_rotate_on_exhaustion()` and `api.switch_account()` when `reload_server=False` (`reason="sdk_disk_only_swap"`), keeping SDK consumers (such as `agy-telemote`) completely non-blocking (`reloaded_server=False`, zero process signals) while ensuring Desktop converges at the next **Global Quiescence** boundary via `LogWatcher`. Explicit CLI `--no-restart` MUST remain the sole opt-out suppressing `cache/pending_reload.json` creation. External terminal invocations (non-descendants of `language_server`) without `--no-restart` MUST preserve ADR 0010 synchronous `restart_language_server(force=force)` behavior (`restart_deferred=False`), and `--force-restart-now` MUST remain available as an explicit emergency mid-turn override.

#### Scenario: [DEFER-RELOAD-001] In-turn `switch --force` downgrades synchronous kill to Deferred Safe-Turn Reload and writes `pending_reload.json` with 1800s TTL
- GIVEN `ProcessSupervisor.is_invoked_by_language_server()` returns `True` (CLI executed inside an active turn sub-shell descendant of `language_server`)
- AND standby account `"work-acc"` passes pre-flight validation
- WHEN the agent executes `agy-switch switch work-acc --force`
- THEN `switch_tokens()` does NOT send `SIGTERM` or `SIGKILL` to `language_server`
- AND it completes an atomic **Disk-Only Swap** to `"work-acc"` and atomically writes `cache/pending_reload.json` with mode `0600`, `target_account: "work-acc"`, `ttl_seconds: 1800`, `requested_at`, and `reason: "in_turn_deferred_reload"`
- AND `switch_tokens()` returns `restart_deferred=True` with `server_restarted=False`, and the CLI exits `0` reporting deferred reload with 30-minute (1800s) TTL.

#### Scenario: [DEFER-RELOAD-002] In-turn `switch --auto` selects optimal standby, performs Disk-Only Swap, and writes `pending_reload.json` with 1800s TTL without killing parent server
- GIVEN `ProcessSupervisor.is_invoked_by_language_server()` returns `True`
- AND `RotationEngine` selects `"best-standby"` as optimal for `"gemini"`
- WHEN the agent executes `agy-switch switch --auto`
- THEN `switch_tokens("best-standby", restart_server=True, force=True)` downgrades to **Deferred Safe-Turn Reload**
- AND writes `cache/pending_reload.json` with `target_account: "best-standby"` and `ttl_seconds: 1800` without interrupting the active agent turn.

#### Scenario: [DEFER-RELOAD-003] External terminal `switch --force` preserves ADR 0010 synchronous `language_server` restart
- GIVEN `ProcessSupervisor.is_invoked_by_language_server()` returns `False` (invoked from an external user shell)
- AND `language_server` is running and busy
- WHEN the operator runs `agy-switch switch work-acc --force`
- THEN `switch_tokens()` synchronously calls `restart_language_server(force=True)` after atomic disk swap
- AND no `cache/pending_reload.json` marker is written and `switch_tokens()` returns `server_restarted=True` and `restart_deferred=False`.

#### Scenario: [DEFER-RELOAD-004] In-turn deferred switch when `LogWatcher` is inactive writes 1800s TTL marker and prints stdout guidance without spawning waiter subprocess
- GIVEN `ProcessSupervisor.is_invoked_by_language_server()` returns `True`
- AND `DaemonManager.get_running_pid()` returns `None` and `is_systemd_active()` returns `False`
- WHEN `agy-switch switch work-acc --force` executes in-turn
- THEN `switch_tokens()` still executes the Disk-Only Swap and writes `cache/pending_reload.json` (`ttl_seconds: 1800`)
- AND stdout prints explicit guidance noting that `LogWatcher` is inactive and the Desktop server can be reloaded after the turn completes
- AND zero background waiter subprocesses (`subprocess.Popen`) are spawned.

#### Scenario: [DEFER-RELOAD-005] Explicit `--force-restart-now` overrides ancestor guard for emergency synchronous restart
- GIVEN `ProcessSupervisor.is_invoked_by_language_server()` returns `True`
- WHEN the operator explicitly runs `agy-switch switch work-acc --force --force-restart-now`
- THEN `switch_tokens(..., force_restart_now=True)` bypasses the deferred downgrade and synchronously calls `restart_language_server(force=True)`
- AND clears or omits `cache/pending_reload.json`.

#### Scenario: [DEFER-RELOAD-ERR-001] Unreadable or missing `/proc/<pid>/stat` during ancestry walk fails safe to non-descendant without crashing
- GIVEN `self.proc_dir / str(cur_pid) / "stat"` is missing, malformed (missing `)` delimiter), or raises `PermissionError`
- WHEN `ProcessSupervisor.is_invoked_by_language_server()` walks the process ancestry
- THEN it handles the error cleanly without raising an unhandled exception and returns `False`.

#### Scenario: [DEFER-RELOAD-EDGE-001] Cyclic `ppid` chain or ancestry deeper than 64 hops terminates cleanly via visited set and depth bound
- GIVEN a mock `/proc` fixture containing a cyclic `ppid` loop (`100 -> 101 -> 100`) or an ancestry chain exceeding 64 hops
- WHEN `ProcessSupervisor.is_invoked_by_language_server(current_pid=100)` executes
- THEN the traversal terminates deterministically upon revisiting a PID or hitting the 64-hop limit and returns `False`.

#### Scenario: [DEFER-RELOAD-EDGE-002] In-turn `switch <name>` without `--force` respects Active Turn Safety Guard and `--no-restart` skips `pending_reload.json`
- GIVEN `ProcessSupervisor.is_invoked_by_language_server()` returns `True` and `language_server` is busy
- WHEN `agy-switch switch work-acc` is invoked without `--force` and without `--no-restart`
- THEN `verify_turn_safety(force=False)` raises `ActiveTurnRunningError` (exit code `2`) without mutating disk or writing `pending_reload.json`
- AND when invoked with `--no-restart`, `switch_tokens(..., restart_server=False)` performs a Disk-Only Swap without writing `pending_reload.json`.

#### Scenario: [DEFER-RELOAD-EDGE-003] `/proc/<pid>/stat` process names containing spaces and parentheses parse `ppid` accurately via `rfind(")")`
- GIVEN a mock `/proc` fixture where an intermediate process has `/proc/<pid>/stat` containing spaces and nested parentheses in `(comm)` (e.g., `200 (language server (v2)) S 100 200 200 ...`) and ancestor `100` has `cmdline` containing `language_server --standalone`
- WHEN `ProcessSupervisor.is_invoked_by_language_server(current_pid=200)` walks the ancestry chain
- THEN it anchors on the last `)` via `rfind(")")` to extract `ppid=100` cleanly and returns `True`.

#### Scenario: [DEFER-RELOAD-EDGE-004] In-turn `switch --auto --force-restart-now` forces immediate restart while `switch <name> --force-restart-now` without `--force` still honors Active Turn Safety Guard
- GIVEN `ProcessSupervisor.is_invoked_by_language_server()` returns `True` and `language_server` is busy
- WHEN `agy-switch switch --auto --force-restart-now` executes in-turn with optimal standby `"best-standby"`, and separately `agy-switch switch work-acc --force-restart-now` executes without `--force`
- THEN `switch --auto --force-restart-now` sets `force=True` and `force_restart_now=True`, performing synchronous `restart_language_server(force=True)` immediately
- AND `switch work-acc --force-restart-now` without `--force` enforces `verify_turn_safety(force=False)`, raising `ActiveTurnRunningError` (exit code `2`) without mutating disk.

#### Scenario: [CONVERGE-RELOAD-001] `PreInvocation` hook enqueues `pending_reload.json` (1800s TTL) after rotating accounts
- GIVEN active account `"primary-acc"` fails auth or drops below threshold and `handle_pre_invocation_hook(engine)` rotates to `"standby-acc"`
- WHEN `handle_pre_invocation_hook(engine)` completes the atomic **Disk-Only Swap**
- THEN it atomically writes `cache/pending_reload.json` (mode `0600`) with `target_account: "standby-acc"`, `ttl_seconds: 1800`, and `reason: "hook_pre_invocation_rotation"`
- AND does NOT synchronously restart `language_server` mid-turn.

#### Scenario: [CONVERGE-RELOAD-002] In-Process SDK `auto_rotate_on_exhaustion(reload_server=False)` enqueues `pending_reload.json` while remaining non-blocking and zero-`/proc`
- GIVEN active account `"primary-acc"` is exhausted and standby account `"standby-acc"` has available Gemini quota
- WHEN an external consumer invokes `api.auto_rotate_on_exhaustion(model="gemini", reload_server=False, engine=engine)`
- THEN it performs the atomic **Disk-Only Swap** without calling `locate_language_server_pid()`, `is_server_busy()`, or `restart_language_server()`, without modifying presence locks (`RotationResult.reloaded_server == False`)
- AND atomically writes `cache/pending_reload.json` (mode `0600`) with `target_account: "standby-acc"`, `ttl_seconds: 1800`, and `reason: "sdk_disk_only_swap"`.

#### Scenario: [CONVERGE-RELOAD-003] In-Process SDK `switch_account(name, reload_server=False)` enqueues `pending_reload.json` while remaining non-blocking and zero-`/proc`
- GIVEN vault account `"personal"` exists with valid credentials
- WHEN an external consumer invokes `api.switch_account("personal", reload_server=False, engine=engine)`
- THEN it performs the atomic **Disk-Only Swap** returning `True` without calling `locate_language_server_pid()`, `is_server_busy()`, or `restart_language_server()` and without modifying presence locks
- AND atomically writes `cache/pending_reload.json` (mode `0600`) with `target_account: "personal"`, `ttl_seconds: 1800`, and `reason: "sdk_disk_only_swap"`.

#### Scenario: [CONVERGE-RELOAD-004] Extended 1800-second TTL preserves pending reload across long-running 15-minute agent turns
- GIVEN `cache/pending_reload.json` was written 900 seconds (15 minutes) ago with default `ttl_seconds: 1800`
- WHEN `AgySwitchEngine.load_pending_reload()` and `LogWatcher._process_pending_reload()` evaluate the marker at a verified **Global Quiescence** boundary
- THEN the marker is recognized as unexpired (`900 <= 1800`) and consumed normally to reload `language_server`.

#### Scenario: [CONVERGE-RELOAD-ERR-001] Failure writing `pending_reload.json` during hook or SDK swap degrades gracefully without failing rotation
- GIVEN `engine.write_pending_reload()` encounters `PermissionError` or `ENOSPC` on `cache/pending_reload.json`
- WHEN `handle_pre_invocation_hook()`, `api.auto_rotate_on_exhaustion(reload_server=False)`, or `api.switch_account(..., reload_server=False)` executes
- THEN the disk write warning is emitted to `stderr` without raising an uncaught exception
- AND the atomic token swap still succeeds and returns its normal success result.

#### Scenario: [CONVERGE-RELOAD-ERR-002] Invalid, negative, zero, boolean, or non-finite `ttl_seconds` sanitized on write and rejected on parse
- GIVEN `ttl_seconds` values `0`, `-60`, `True`, `float("nan")`, or `float("inf")`
- WHEN passed to `AgySwitchEngine.write_pending_reload()` or parsed from disk by `parse_pending_reload_payload()`
- THEN `write_pending_reload()` falls back safely to `DEFAULT_PENDING_RELOAD_TTL_SECONDS` (`1800`)
- AND `parse_pending_reload_payload()` rejects explicitly invalid `ttl_seconds` payloads by returning `None` so corrupt markers are pruned.

#### Scenario: [CONVERGE-RELOAD-EDGE-001] Explicit CLI `--no-restart` suppresses `pending_reload.json` creation
- GIVEN an operator invokes `agy-switch switch work-acc --no-restart` (either in-turn or from an external terminal)
- WHEN `cli.main()` dispatches `engine.switch_tokens("work-acc", restart_server=False, ...)`
- THEN live Desktop/CLI tokens and Vault active pointer are updated atomically
- AND `cache/pending_reload.json` is NOT created.

#### Scenario: [CONVERGE-RELOAD-EDGE-002] Legacy `pending_reload.json` omitting `ttl_seconds` normalizes to 1800s default while preserving explicit overrides
- GIVEN a legacy `pending_reload.json` payload `{"target_account": "work-acc", "requested_at": 1700000000.0}` without `ttl_seconds` and a second payload specifying `"ttl_seconds": 300`
- WHEN `parse_pending_reload_payload()` parses both payloads
- THEN the first payload normalizes `ttl_seconds` to `1800`
- AND the second payload preserves `ttl_seconds: 300`.

#### Scenario: [CONVERGE-RELOAD-EDGE-003] Non-rotating hook Tier 1 pass and exhausted SDK `auto_rotate_on_exhaustion` never create spurious `pending_reload.json`
- GIVEN Case 1: `handle_pre_invocation_hook(engine)` exits via Tier 1 healthy cache fast-path or retains the active account
- AND Case 2: `api.auto_rotate_on_exhaustion(reload_server=False, engine=engine)` returns `success=False` (Fleet Exhaustion)
- WHEN each operation completes
- THEN `cache/pending_reload.json` is NOT created or modified.

### Requirement: Failover Audit Trail and Persistent Auth Quarantine in PreInvocation Hook
The PreInvocation hook (`handle_pre_invocation_hook` in `scripts/agy_switcher/hook.py`), secure logger (`append_secure_log_entry` in `scripts/agy_switcher/presence.py`), vault engine (`AgySwitchEngine` in `scripts/agy_switcher/vault.py`), telemetry fetcher (`fetch_account_quota` in `scripts/agy_switcher/telemetry.py`), and CLI formatters (`format_status_report`, `format_accounts_table`, `format_accounts_listicle` in `scripts/agy_switcher/formatters.py`) MUST implement **Failover Audit & Auth Quarantine**. On every hook rotation/failover (`target_account != active_name`), `handle_pre_invocation_hook()` MUST perform dual persistence: (1) append a structured chronological audit line (`trigger: auth_corruption` or `trigger: quota_rotation`) to `watch.log` via `presence.py:append_secure_log_entry()` (mode `0600`, symlink-safe, isolated under **Mock Vault Isolation**); and (2) atomically update `cache/failover_state.json` (mode `0600`, separate from `last_known_quota.json` to preserve ADR 0020 `<40ms` Tier 1 fast-path and cache sanitization compatibility) recording `last_failover` (`timestamp`, `from_account`, `to_account`, `trigger`, `reason`) and, when `auth_corruption` occurred, persisting `auth_errors[<alias>]` (`timestamp`, `error`, `error_type`). On `auth_corruption`, the hook's `injectSteps[0].ephemeralMessage` MUST explicitly identify the failed account, the auth failure reason, and `agy-switch login <alias>` remediation while preserving the backward-compatible substring `"Auto-rotated to **{target_account}**"`. Entries in `auth_errors[<alias>]` MUST persist until explicitly cleared by a successful `login_account()`, `add_account()`, or a successful live quota probe (`fetch_account_quota()` returning valid quota without error). `AgySwitchEngine.get_status()` and `format_status_report()` MUST surface `Last Failover` whenever `last_failover` is `<= 24 hours` old and has not been superseded by a subsequent successful manual switch, and MUST surface `Pending Reload` whenever an active unexpired `cache/pending_reload.json` marker exists. `AgySwitchEngine.list_accounts()`, `format_accounts_table()`, and `format_accounts_listicle()` MUST expose `auth_error` status and render an `(Auth Error)` badge for quarantined accounts.

#### Scenario: [AUDIT-HOOK-001] Auth corruption ejection appends `watch.log`, persists `failover_state.json`, and emits differentiated `ephemeralMessage`
- GIVEN active account `"automator"` whose Tier 2 CloudCode probe returns `error_type: "auth"` (`HTTP 403 Forbidden`)
- AND standby account `"honey"` has healthy Gemini quota (85%)
- WHEN `handle_pre_invocation_hook(engine)` executes in an isolated vault
- THEN it switches active tokens to `"honey"` (`restart_server=False`) and appends a structured `trigger: auth_corruption` audit entry to `watch.log` via `append_secure_log_entry()`
- AND atomically writes `cache/failover_state.json` (mode `0600`) with `last_failover` (`from_account: "automator"`, `to_account: "honey"`, `trigger: "auth_corruption"`) and `auth_errors["automator"]`
- AND `last_known_quota.json` contains no failover metadata keys
- AND stdout JSON `ephemeralMessage` contains `"Auto-rotated to **honey**"`, names `"automator"`, and instructs `"agy-switch login automator"`.

#### Scenario: [AUDIT-HOOK-002] Quota rotation ejection records `trigger: quota_rotation` in `watch.log` and `failover_state.json`
- GIVEN active account `"primary-acc"` with remaining Gemini quota at 10% (`< threshold`) and no auth error
- AND standby account `"standby-acc"` has 90% quota
- WHEN `handle_pre_invocation_hook(engine)` rotates to `"standby-acc"`
- THEN `watch.log` receives an audit entry with `trigger: quota_rotation`
- AND `cache/failover_state.json` records `last_failover` with `trigger: "quota_rotation"` without adding `"primary-acc"` to `auth_errors`.

#### Scenario: [AUDIT-HOOK-003] `agy-switch status` displays `Last Failover` within 24 hours and active `Pending Reload` state
- GIVEN `cache/failover_state.json` contains a `last_failover` event recorded 2 hours ago (`from_account: "automator"`, `to_account: "honey"`, `trigger: "auth_corruption"`)
- AND `cache/pending_reload.json` contains an unexpired marker (`target_account: "honey"`, `ttl_seconds: 120`)
- WHEN `agy-switch status` executes (`get_status()` + `format_status_report()`)
- THEN the output renders a `Last Failover` line summarizing the recent ejection (`automator -> honey`) and trigger
- AND renders a `Pending Reload` line indicating `"honey"` is awaiting safe-turn server reload.

#### Scenario: [AUDIT-HOOK-004] `agy-switch list` displays `(Auth Error)` badge for quarantined accounts in table and listicle formats
- GIVEN vault accounts `"honey"` (active, healthy) and `"automator"` (standby, present in `cache/failover_state.json["auth_errors"]`)
- WHEN `agy-switch list` (`--format table` and `--format list`) or `agy-switch list --json` executes
- THEN `engine.list_accounts()` includes `"auth_error"` metadata on `"automator"`
- AND both `format_accounts_table()` and `format_accounts_listicle()` display `(Auth Error)` on `"automator"`'s status line.

#### Scenario: [AUDIT-HOOK-005] Quarantine clears automatically upon successful `login_account`, `add_account`, or healthy live `fetch_account_quota` probe
- GIVEN `"automator"` is recorded in `cache/failover_state.json["auth_errors"]`
- WHEN `"automator"` completes a successful `AgySwitchEngine.login_account("automator")`, `AgySwitchEngine.add_account("automator", ...)`, or `fetch_account_quota(engine, "automator")` returns a valid quota dict without `"error"`
- THEN `"automator"` is removed from `cache/failover_state.json["auth_errors"]` atomically
- AND subsequent `agy-switch list` no longer displays `(Auth Error)` for `"automator"`.

#### Scenario: [AUDIT-HOOK-ERR-001] Corrupt or non-dict `cache/failover_state.json` recovers gracefully without breaking hook rotation or CLI output
- GIVEN `cache/failover_state.json` contains truncated JSON or a JSON array `[]`
- WHEN `handle_pre_invocation_hook()`, `engine.get_status()`, or `engine.list_accounts()` reads failover state
- THEN it safely treats the corrupt payload as empty state without raising an unhandled exception
- AND hook rotations overwrite it atomically with valid `0600` JSON schema.

#### Scenario: [AUDIT-HOOK-ERR-002] Symlink tampering, read-only vault, or `ENOSPC` during audit log or `failover_state.json` write fails safe without breaking hook rotation
- GIVEN `watch.log` or `cache/failover_state.json` is a pre-existing symlink to an external path, or the vault cache directory raises `OSError` (`EACCES` / `ENOSPC`) during failover persistence
- WHEN `handle_pre_invocation_hook(engine)` rotates from `"automator"` to `"honey"`
- THEN symlink targets are never followed (unlinked or atomically replaced via `os.replace`) and I/O failures log a warning to `stderr`
- AND `handle_pre_invocation_hook()` still completes the token swap and emits valid `injectSteps` JSON to `stdout` with exit code `0`.

#### Scenario: [AUDIT-HOOK-EDGE-001] `last_failover` older than 24 hours or superseded by successful manual switch is suppressed from `status` while `auth_errors` quarantine persists
- GIVEN `cache/failover_state.json` contains `"automator"` in `auth_errors` and a `last_failover` event either older than 24 hours (`> 86400s`) OR followed by a successful manual `agy-switch switch honey`
- WHEN `agy-switch status` and `agy-switch list` run
- THEN `status` omits the stale or superseded `Last Failover` line
- AND `"automator"` remains quarantined in `auth_errors` and flagged as `(Auth Error)` in `list` until re-authenticated or live-probed healthy.

#### Scenario: [AUDIT-HOOK-EDGE-002] Tier 1 healthy cache fast-path in `hook-pre-invocation` performs zero writes to `watch.log` or `failover_state.json`
- GIVEN active account `"honey"` has fresh `last_known_quota.json` cache above `tier1_threshold` (`>= 35%`)
- WHEN `scripts/agy_switch.py:hook_pre_invocation_dispatch()` or `handle_pre_invocation_hook(engine)` executes
- THEN it exits immediately via the Tier 1 fast-path returning `{}` without importing or reading `cache/failover_state.json`
- AND performs zero writes to `watch.log` or `cache/failover_state.json`.

#### Scenario: [AUDIT-HOOK-EDGE-003] Concurrent `failover_state.json` mutations serialize safely without truncated JSON or lost quarantine entries
- GIVEN multiple threads concurrently invoke `record_auth_error()`, `clear_auth_error()`, and `record_last_failover()` on `AgySwitchEngine`
- WHEN all concurrent updates complete
- THEN `cache/failover_state.json` remains valid `0600` JSON with zero truncated writes or lost concurrent account entries.

#### Scenario: [AUDIT-HOOK-EDGE-004] Rejected manual switch preserves `last_failover` and `status` ignores expired or malformed `pending_reload.json`
- GIVEN `cache/failover_state.json` has a valid `last_failover` recorded 1 hour ago and `cache/pending_reload.json` is either expired (`now - requested_at > ttl_seconds`) or malformed JSON
- AND a manual `agy-switch switch standby-acc` fails pre-flight with `ActiveTurnRunningError` or `AccountAuthError`
- WHEN `agy-switch status` executes
- THEN `Last Failover` remains surfaced because the failed manual switch did not supersede it
- AND `Pending Reload` is omitted safely without crashing on the expired or malformed marker.

### Requirement: Vault-Authoritative Sync and Token Identity Drift Detection
The security primitives (`extract_token_identity`, `token_credentials_match` in `scripts/agy_switcher/security.py`), data models (`TokenIdentity`, `TokenSyncStatus`, `TargetSyncSnapshot` in `scripts/agy_switcher/models.py`), vault engine (`AgySwitchEngine.check_target_sync_status`, `AgySwitchEngine.sync_active_tokens_from_vault`, `AgySwitchEngine.update_account_access_token` in `scripts/agy_switcher/vault.py`), CLI parser (`build_parser` in `scripts/agy_switcher/cli.py`), and **Self-Healing Diagnostics** (`DoctorEngine.run_diagnostics`, `DoctorEngine.fix_diagnostics` in `scripts/agy_switcher/doctor.py`) MUST enforce **Vault-Authoritative Sync** as the single source of truth. `~/.antigravity-vault/active` and `accounts/<alias>/token.json` MUST be the sole canonical authority; bidirectional sync, implicit desktop-follow adoption, and any `adopt-desktop` CLI subcommand or flag MUST NOT exist. `token_credentials_match(vault_data, target_data)` MUST verify credential identity locally (zero HTTP) by comparing non-empty trimmed `token.refresh_token`, `auth_method`, and case-insensitive trimmed resolved email identity (`resolve_token_email()` when both sides expose an email), treating benign runtime differences (such as Desktop Go `writer_id`, nanosecond `expiry` formatting, a same-account refreshed `access_token`, or one-sided omitted email when `refresh_token` and `auth_method` match) as synced while classifying any missing/empty required identity field or `refresh_token`, `auth_method`, or email mismatch (including **Zombie Write-Back**) as drift. `AgySwitchEngine.check_target_sync_status()` MUST inspect `desktop_token_file` and `cli_token_file` via symlink-safe `read_json_dict_nofollow()` without following external symlinks or making HTTP requests. `AgySwitchEngine.sync_active_tokens_from_vault()` MUST validate the Vault active account's `token.json` before atomically repairing requested live target files (`0600`) via `os.replace` (replacing symlinked target paths without mutating external symlink targets, and returning `False` with a `stderr` warning on `OSError` / `PermissionError` / `ENOSPC`). `AgySwitchEngine.update_account_access_token()` MUST only sync live target files when `name == self.get_active_name()`, and if `desktop_token_file` had drifted to a different account identity while `language_server` is running prior to syncing, it MUST enqueue `cache/pending_reload.json` (`reason="desktop_drift_on_token_refresh"`, `ttl_seconds=1800`) so token refresh never masks in-memory Desktop drift. `DoctorEngine.run_diagnostics()` Layer 3 (`dual_sync`) MUST align with `token_credentials_match()` so routine same-account Desktop `access_token` refreshes report `[PASS]`, and when `DoctorEngine.fix_diagnostics()` repairs a `dual_sync` anomaly where Desktop credentials were missing, corrupt, or drifted from Vault and `language_server` is running (whether idle or busy), it MUST heal live targets from Vault via **Atomic Swap** and enqueue `cache/pending_reload.json` (`reason="doctor_dual_sync_repair"`, `ttl_seconds=1800`) without synchronously restarting or killing `language_server`.

#### Scenario: [VAULT-SYNC-001] Matching Desktop and CLI credential identities report synced despite benign Go runtime metadata
- GIVEN Vault active account `"primary-acc"` has `refresh_token="rt-primary"`, `auth_method="oauth"`, and email `"primary@example.com"`
- AND `~/.gemini/jetski-standalone-oauth-token` contains the identical `refresh_token="rt-primary"`, `auth_method="oauth"`, and `"primary@example.com"` alongside extra Desktop Go fields (`"writer_id": "ls-991"`, nanosecond `expiry`, and a freshly rotated `access_token`)
- AND `~/.gemini/antigravity-cli/antigravity-oauth-token` matches `"primary-acc"`
- WHEN `token_credentials_match()` and `AgySwitchEngine.check_target_sync_status()` execute
- THEN both Desktop and CLI targets are classified as synced (`in_sync=True`, `drifted_targets=[]`)
- AND zero disk writes or HTTP calls occur.

#### Scenario: [VAULT-SYNC-002] Desktop Zombie Write-Back detected as drift and healed atomically back to Vault
- GIVEN Vault active account is `"work-acc"` (`refresh_token="rt-work"`, email `"work@example.com"`)
- AND `~/.gemini/jetski-standalone-oauth-token` was overwritten by `language_server` **Zombie Write-Back** with stale account `"old-acc"` (`refresh_token="rt-old"`, email `"old@example.com"`, `"writer_id": "go-ls"`)
- WHEN `AgySwitchEngine.check_target_sync_status()` runs followed by `AgySwitchEngine.sync_active_tokens_from_vault()`
- THEN `check_target_sync_status()` reports `in_sync=False`, `desktop_synced=False`, `drifted_targets=["desktop"]`, and `desktop_email="old@example.com"`
- AND `sync_active_tokens_from_vault()` atomically overwrites `~/.gemini/jetski-standalone-oauth-token` with `"work-acc"`'s Vault payload at mode `0600` without modifying `~/.antigravity-vault/active`.

#### Scenario: [VAULT-SYNC-003] `DoctorEngine.fix_diagnostics()` repairs Desktop drift from Vault and enqueues `pending_reload.json` when `language_server` is running
- GIVEN Vault active account is `"work-acc"` and `language_server` is running (`locate_language_server_pid()` returns PID `4242`)
- AND `~/.gemini/jetski-standalone-oauth-token` drifted to stale account `"old-acc"` while CLI matches `"work-acc"`
- WHEN `DoctorEngine.run_diagnostics()` and `DoctorEngine.fix_diagnostics(report)` execute
- THEN the `dual_sync` `DiagnosticItem` initially flags `[FAIL]` (`can_fix=True`) naming Desktop desync
- AND `fix_diagnostics()` atomically re-syncs Desktop and CLI from Vault `"work-acc"` without calling `restart_language_server()`
- AND writes `cache/pending_reload.json` (mode `0600`) with `target_account="work-acc"`, `ttl_seconds=1800`, and `reason="doctor_dual_sync_repair"`.

#### Scenario: [VAULT-SYNC-004] Strict `agy-switch` workflow exposes zero `adopt-desktop` or bidirectional sync CLI surfaces
- GIVEN the `agy_switcher.cli` argument parser built by `build_parser()`
- WHEN callers inspect registered subcommands and flags or invoke `agy-switch adopt-desktop`
- THEN no `adopt-desktop` or `sync --from-desktop` subcommand or flag exists in `build_parser()`
- AND invoking `agy-switch adopt-desktop` fails cleanly via `argparse` with exit code `2`.

#### Scenario: [VAULT-SYNC-005] `update_account_access_token()` detects pre-existing Desktop drift on active account refresh and enqueues `pending_reload.json`
- GIVEN Vault active account is `"work-acc"` and `language_server` is running
- AND `~/.gemini/jetski-standalone-oauth-token` currently contains drifted **Zombie Write-Back** credentials for `"old-acc"` (`refresh_token="rt-old"`)
- WHEN `AgySwitchEngine.update_account_access_token("work-acc", "ya29.fresh-token", expiry="2027-01-01T00:00:00Z")` executes
- THEN `accounts/work-acc/token.json`, Desktop token, and CLI token are all updated atomically with `"work-acc"`'s refreshed access token
- AND `cache/pending_reload.json` is atomically written (`target_account="work-acc"`, `ttl_seconds=1800`, `reason="desktop_drift_on_token_refresh"`) so `language_server` RAM drift is not masked.

#### Scenario: [VAULT-SYNC-ERR-001] Corrupt, truncated, or non-dict Desktop/CLI token JSON flagged as drift without crashing
- GIVEN Vault active account `"work-acc"` has valid `token.json`
- AND `~/.gemini/jetski-standalone-oauth-token` contains truncated JSON `{"token":` or a JSON list `[]`
- WHEN `AgySwitchEngine.check_target_sync_status()` executes
- THEN it catches parse errors cleanly without raising exceptions
- AND reports `in_sync=False`, `desktop_synced=False`, and `drifted_targets=["desktop"]`.

#### Scenario: [VAULT-SYNC-ERR-002] Symlinked or unreadable target token files rejected via `O_NOFOLLOW` without reading or writing external targets
- GIVEN `~/.gemini/jetski-standalone-oauth-token` is a symlink pointing to an external file `/tmp/external-secret` (or has mode `0000` raising `PermissionError`)
- WHEN `AgySwitchEngine.check_target_sync_status()` runs
- THEN `read_json_dict_nofollow()` refuses to follow the symlink and catches `OSError` safely
- AND reports `desktop_synced=False` with `"desktop"` in `drifted_targets` without leaking or mutating `/tmp/external-secret`.

#### Scenario: [VAULT-SYNC-ERR-003] `sync_active_tokens_from_vault()` aborts without mutating live targets when Vault active token is missing or invalid
- GIVEN `~/.antigravity-vault/active` points to `"broken-acc"` whose `token.json` is missing or fails `validate_token_file()`
- AND existing live Desktop and CLI token files exist on disk
- WHEN `AgySwitchEngine.sync_active_tokens_from_vault()` is invoked
- THEN it returns `False` without overwriting or deleting the live Desktop or CLI token files.

#### Scenario: [VAULT-SYNC-ERR-004] `sync_active_tokens_from_vault()` handles `PermissionError` / `ENOSPC` safely and replaces symlinked targets atomically without mutating external files
- GIVEN Vault active account `"work-acc"` has valid `token.json`
- AND Case 1: `~/.gemini/jetski-standalone-oauth-token` is a symlink pointing to `/tmp/external-secret`
- AND Case 2: writing `~/.gemini/jetski-standalone-oauth-token` raises `PermissionError` or `OSError(ENOSPC)`
- WHEN `AgySwitchEngine.sync_active_tokens_from_vault()` executes
- THEN in Case 1 `os.replace()` replaces the symlink itself with a regular `0600` file for `"work-acc"` while leaving `/tmp/external-secret` completely unmodified
- AND in Case 2 it logs a diagnostic warning to `stderr` and returns `False` without raising an unhandled exception.

#### Scenario: [VAULT-SYNC-EDGE-001] CLI-only drift repaired by `DoctorEngine.fix_diagnostics()` does not enqueue Desktop `pending_reload.json`
- GIVEN Vault active account `"work-acc"` matches `~/.gemini/jetski-standalone-oauth-token` and `language_server` is running
- AND only `~/.gemini/antigravity-cli/antigravity-oauth-token` drifted to `"old-acc"`
- WHEN `DoctorEngine.run_diagnostics()` and `DoctorEngine.fix_diagnostics(report)` execute
- THEN CLI token is atomically restored to `"work-acc"`
- AND `cache/pending_reload.json` is NOT created because Desktop credentials were already synchronized.

#### Scenario: [VAULT-SYNC-EDGE-002] `DoctorEngine.fix_diagnostics()` omits `pending_reload.json` when Desktop drifted while `language_server` is stopped
- GIVEN Vault active account `"work-acc"` has drifted Desktop token on disk
- AND `locate_language_server_pid()` returns `None` (`language_server` is not running)
- WHEN `DoctorEngine.fix_diagnostics(report)` repairs `dual_sync`
- THEN Desktop and CLI tokens are atomically restored from Vault `"work-acc"`
- AND no `cache/pending_reload.json` marker is written because no running server holds stale memory.

#### Scenario: [VAULT-SYNC-EDGE-003] Refreshing a standby account via `update_account_access_token()` never mutates live Desktop or CLI tokens
- GIVEN Vault active account is `"work-acc"` and standby account is `"standby-acc"`
- WHEN `AgySwitchEngine.update_account_access_token("standby-acc", "ya29.standby-fresh")` executes
- THEN only `accounts/standby-acc/token.json` is updated atomically
- AND live Desktop token, live CLI token, and `cache/pending_reload.json` remain completely untouched.

#### Scenario: [VAULT-SYNC-EDGE-004] `token_credentials_match()` normalizes email case/whitespace, tolerates one-sided missing email, and rejects missing required identity fields
- GIVEN a Vault token with `refresh_token="rt-1"`, `auth_method="oauth"`, and email `"Honey@Example.COM"`
- WHEN `token_credentials_match()` compares it against (a) a target with `" honey@example.com "` and matching `refresh_token`/`auth_method`, (b) a target omitting both `id_token` and `id_token_email` with matching `refresh_token`/`auth_method`, (c) a target with matching `refresh_token`/`auth_method` but differing email `"other@example.com"`, and (d) a target with empty/missing `refresh_token` or `auth_method`
- THEN cases (a) and (b) return `True`
- AND cases (c) and (d) return `False`.

#### Scenario: [VAULT-SYNC-EDGE-005] `DoctorEngine.run_diagnostics()` passes on same-account refreshed `access_token` and `fix_diagnostics()` never synchronously restarts a busy `language_server`
- GIVEN Vault active account `"work-acc"` (`refresh_token="rt-work"`, `auth_method="oauth"`, `"work@example.com"`)
- AND Case 1: Desktop token contains the same `refresh_token`, `auth_method`, and email with a newer `access_token` and `"writer_id"`
- AND Case 2: Desktop token drifted to `"old-acc"` while `language_server` is running and busy (`is_server_busy() == True`)
- WHEN `DoctorEngine.run_diagnostics()` and `DoctorEngine.fix_diagnostics(report)` execute
- THEN Case 1 reports `dual_sync` status `[PASS]` (`can_fix=False`)
- AND Case 2 repairs disk atomically, enqueues `cache/pending_reload.json` (`reason="doctor_dual_sync_repair"`, `ttl_seconds=1800`), and NEVER invokes `restart_language_server()` mid-turn.

### Requirement: Continuous Drift Guard in LogWatcher
The background watcher daemon (`LogWatcher.check_and_heal_token_drift` and `LogWatcher.poll_once` in `scripts/agy_switcher/watcher.py`, plus `format_drift_guard_audit_line` in `scripts/agy_switcher/presence.py`) MUST implement a lightweight, throttled, local-only (zero HTTP) **Continuous Drift Guard**. On each `poll_once()` iteration, once `drift_check_interval` seconds have elapsed since the previous check (default `5.0` seconds, configurable via constructor parameter; non-finite, negative, or boolean values sanitized safely to `5.0`, while `0.0` disables throttling for deterministic tests), `LogWatcher.check_and_heal_token_drift(force=False)` MUST compare Desktop (`jetski-standalone-oauth-token`) and CLI (`antigravity-oauth-token`) credential identities against the current Vault active account via `check_target_sync_status()` / `token_credentials_match()`. When `engine` lacks `check_target_sync_status()` or returns a non-mapping/mocked status lacking concrete `in_sync` (`bool`) and `drifted_targets` (`list`/`tuple`), `check_and_heal_token_drift()` MUST no-op safely without modifying files or writing `watch.log`. When concrete drift is detected (including **Zombie Write-Back**), `LogWatcher` MUST: (1) re-verify the current Vault active account immediately before writing so concurrent switches (including same-cycle reactive 429 failovers) are never reverted; (2) atomically repair only the diverged live file(s) via **Atomic Swap** (`0600`) from Vault without following symlinks; (3) if the Desktop token drifted and `language_server` is running (`locate_language_server_pid() is not None`), atomically enqueue `cache/pending_reload.json` with `target_account=active`, `reason="desktop_drift_guard"`, and `ttl_seconds=1800` without interrupting active turns; (4) if only the CLI token drifted while Desktop remains in sync, repair CLI without enqueueing a Desktop server reload; and (5) append a structured drift-healing audit line to `watch.log` (mode `0600`, strictly honoring **Mock Vault Isolation**). Missing/corrupt/unreadable target files, symlink tampering, `PermissionError`/`ENOSPC`, or an unconfigured Vault active account MUST fail safe with `stderr` warnings and MUST NEVER crash `LogWatcher.poll_once()`.

#### Scenario: [DRIFT-GUARD-001] Desktop Zombie Write-Back healed atomically with `pending_reload.json` and `watch.log` audit entry
- GIVEN `LogWatcher` is monitoring an isolated vault with active account `"honey"` and `language_server` is running
- AND `~/.gemini/jetski-standalone-oauth-token` was overwritten by **Zombie Write-Back** containing `"automator"` (`refresh_token="rt-auto"`, `"writer_id": "go-ls"`)
- WHEN `LogWatcher.poll_once()` runs after `drift_check_interval` has elapsed
- THEN `check_and_heal_token_drift()` atomically restores `~/.gemini/jetski-standalone-oauth-token` to `"honey"` (mode `0600`)
- AND writes `cache/pending_reload.json` with `target_account="honey"`, `reason="desktop_drift_guard"`, and `ttl_seconds=1800`
- AND appends a structured drift-guard audit entry naming `"honey"` and `"desktop"` to the isolated `watch.log` (mode `0600`) without making any HTTP calls.

#### Scenario: [DRIFT-GUARD-002] CLI-only token drift healed atomically without queueing Desktop server reload
- GIVEN active account `"honey"` matches `~/.gemini/jetski-standalone-oauth-token` and `language_server` is running
- AND `~/.gemini/antigravity-cli/antigravity-oauth-token` drifted to `"stale-acc"`
- WHEN `LogWatcher.poll_once()` runs `check_and_heal_token_drift()`
- THEN `~/.gemini/antigravity-cli/antigravity-oauth-token` is atomically repaired to `"honey"` (mode `0600`)
- AND `watch.log` records the CLI drift repair
- AND `cache/pending_reload.json` is NOT written and `language_server` is not restarted.

#### Scenario: [DRIFT-GUARD-003] Drift guard throttling skips redundant filesystem checks within `drift_check_interval` with zero HTTP calls
- GIVEN `LogWatcher` initialized with `drift_check_interval=5.0` has already completed a drift check 1.0 second ago
- WHEN `LogWatcher.poll_once()` executes again before 5.0 seconds have elapsed
- THEN `check_and_heal_token_drift()` exits immediately as a throttled no-op without re-reading Desktop/CLI token files or issuing any network requests.

#### Scenario: [DRIFT-GUARD-004] Benign same-account Desktop access-token refresh triggers zero repairs and zero reloads
- GIVEN active account `"honey"` has `refresh_token="rt-honey"` and email `"honey@example.com"`
- AND `language_server` refreshed `"honey"`'s access token in `~/.gemini/jetski-standalone-oauth-token` with a new `access_token`, `"writer_id"`, and nanosecond `expiry` while preserving `refresh_token="rt-honey"` and `"honey@example.com"`
- WHEN `LogWatcher.check_and_heal_token_drift()` executes
- THEN it recognizes matching credential identity, returns `False`, performs zero token overwrites, writes no `pending_reload.json`, and appends no drift audit entry.

#### Scenario: [DRIFT-GUARD-ERR-001] Missing or corrupt JSON target token files healed atomically without crashing `poll_once()`
- GIVEN active account `"honey"` has valid `accounts/honey/token.json`
- AND `~/.gemini/jetski-standalone-oauth-token` is missing or contains corrupt binary/truncated JSON
- WHEN `LogWatcher.poll_once()` runs `check_and_heal_token_drift()`
- THEN it heals the missing/corrupt Desktop token atomically from Vault `"honey"` without raising an uncaught exception.

#### Scenario: [DRIFT-GUARD-ERR-002] Symlinked or read-only target token (`PermissionError` / `ENOSPC`) fails safe without terminating watcher daemon
- GIVEN `~/.gemini/jetski-standalone-oauth-token` is a symlink pointing outside `~/.gemini` or atomic replacement raises `PermissionError` / `OSError(ENOSPC)`
- WHEN `LogWatcher.poll_once()` executes `check_and_heal_token_drift()`
- THEN external symlink targets are never written through, I/O errors are logged to `stderr`, and `poll_once()` completes cleanly without crashing the daemon.

#### Scenario: [DRIFT-GUARD-ERR-003] Missing Vault active account or invalid Vault token causes drift guard to no-op safely
- GIVEN `~/.antigravity-vault/active` is absent or points to an account with invalid `token.json`
- WHEN `LogWatcher.check_and_heal_token_drift()` executes
- THEN it returns `False` immediately without modifying Desktop or CLI files or writing `pending_reload.json`.

#### Scenario: [DRIFT-GUARD-ERR-004] Invalid `drift_check_interval` sanitized and minimal/mocked engine degrades safely to no-op
- GIVEN `LogWatcher` initialized with invalid `drift_check_interval` (`-5.0`, `float("nan")`, or `True`) OR bound to a `MagicMock` / minimal engine returning non-mapping sync status
- WHEN `LogWatcher.check_and_heal_token_drift()` and `LogWatcher.poll_once()` execute
- THEN invalid `drift_check_interval` falls back to `5.0`
- AND non-mapping/mocked sync responses return `False` without raising exceptions, writing `pending_reload.json`, or appending spurious `watch.log` entries.

#### Scenario: [DRIFT-GUARD-EDGE-001] Desktop drift while `language_server` is stopped heals disk without creating `pending_reload.json`
- GIVEN active account `"honey"` has drifted Desktop token on disk
- AND `ProcessSupervisor.locate_language_server_pid()` returns `None`
- WHEN `LogWatcher.check_and_heal_token_drift()` runs
- THEN `~/.gemini/jetski-standalone-oauth-token` is atomically repaired from Vault `"honey"`
- AND `cache/pending_reload.json` is NOT written.

#### Scenario: [DRIFT-GUARD-EDGE-002] Repeated Desktop Zombie Write-Back during active turn heals disk and preserves deferred reload without killing active turn
- GIVEN `language_server` is executing an **Active Turn** (`_is_turn_quiescent() == False`) and repeatedly rewrites `~/.gemini/jetski-standalone-oauth-token` with stale `"automator"` credentials
- WHEN `LogWatcher.poll_once()` runs across successive drift intervals
- THEN each check heals `~/.gemini/jetski-standalone-oauth-token` back to `"honey"` and maintains a valid `cache/pending_reload.json` (`ttl_seconds=1800`)
- AND `language_server` is NOT restarted until **Global Quiescence** is reached.

#### Scenario: [DRIFT-GUARD-EDGE-003] Drift guard audit logging strictly respects Mock Vault Isolation during test execution
- GIVEN `LogWatcher` executes `check_and_heal_token_drift()` inside `unittest` with an isolated sandbox vault or mocked engine
- WHEN a drift repair is logged
- THEN the audit entry is written strictly inside the isolated sandbox vault (or suppressed when no isolated vault is configured) and NEVER touches `~/.antigravity-vault/watch.log`.

#### Scenario: [DRIFT-GUARD-EDGE-004] Concurrent account switch during drift inspection uses fresh Vault active pointer and never reverts newer switch
- GIVEN Vault active account transitions concurrently from `"acc1"` to `"acc2"` immediately before drift healing writes to disk
- WHEN `check_and_heal_token_drift()` invokes `sync_active_tokens_from_vault()`
- THEN the repair re-reads the current Vault active pointer (`"acc2"`) before performing the **Atomic Swap**, ensuring `"acc2"` is written and never reverted to `"acc1"`.

#### Scenario: [DRIFT-GUARD-EDGE-005] Drift guard skips same-cycle interference after reactive 429 failover and avoids thrashing existing `pending_reload.json`
- GIVEN Case 1: a reactive 429 error triggers account rotation in `poll_once()` during the same polling cycle
- AND Case 2: Desktop drifted during an active turn when an unexpired `cache/pending_reload.json` for `"honey"` already exists
- WHEN `LogWatcher.poll_once()` executes
- THEN in Case 1 `check_and_heal_token_drift()` does not overwrite or revert the newly rotated account
- AND in Case 2 the Desktop token is healed and `pending_reload.json` remains valid for `"honey"` (`ttl_seconds=1800`) without triggering duplicate server restarts.

### Requirement: CLI Desync Visibility in Status and List Commands
The vault read surfaces (`AgySwitchEngine.get_status` and `AgySwitchEngine.list_accounts` in `scripts/agy_switcher/vault.py`) and terminal formatters (`format_status_report`, `format_accounts_table`, `format_accounts_listicle` in `scripts/agy_switcher/formatters.py`) MUST perform local-only (zero HTTP) **Vault-Authoritative Sync** verification comparing `~/.gemini/jetski-standalone-oauth-token` (Desktop) and `~/.gemini/antigravity-cli/antigravity-oauth-token` (CLI) against the Vault active account. `AgySwitchEngine.get_status()` and `AgySwitchEngine.list_accounts()` MUST include structured `sync_status` metadata (`in_sync`, `desktop_synced`, `cli_synced`, `drifted_targets`, `desktop_email`, `cli_email`), include unexpired `pending_reload` metadata when `cache/pending_reload.json` is active, and preserve JSON compatibility for `agy-switch list --json`. Whenever any target surface is out of sync with Vault, `format_status_report()`, `format_accounts_table()`, and `format_accounts_listicle()` MUST render an explicit desync warning naming the diverged surface(s) (e.g., `Desktop`, `CLI`), detected drifted email when available, remediation guidance (`agy-switch doctor --fix` or `agy-switch switch <active>`), and any queued `Pending Reload` state rather than silently presenting only `~/.antigravity-vault/active`. Formatters MUST also degrade gracefully without crashing or emitting spurious warnings when `sync_status` or `pending_reload` is omitted, `None`, or malformed.

#### Scenario: [SYNC-VIS-001] `agy-switch status` verifies local sync and displays synced indicator with zero HTTP calls
- GIVEN Vault active account `"honey"` matches both Desktop and CLI token credential identities on disk
- WHEN `AgySwitchEngine.get_status()` and `format_status_report()` run
- THEN `status["sync_status"]["in_sync"]` is `True` with `drifted_targets=[]`
- AND formatted output displays synced credential status without emitting any desync warning or performing HTTP requests.

#### Scenario: [SYNC-VIS-002] `agy-switch status` displays explicit Desktop desync warning, drifted identity, remediation, and `Pending Reload`
- GIVEN Vault active account is `"honey"` (`honey@example.com`)
- AND `~/.gemini/jetski-standalone-oauth-token` drifted to `"automator@example.com"` while CLI matches `"honey"`
- AND `cache/pending_reload.json` holds an unexpired reload marker for `"honey"` (`ttl_seconds=1800`)
- WHEN `agy-switch status` runs (`get_status()` + `format_status_report()`)
- THEN output renders an explicit desync warning naming `Desktop` and `automator@example.com`, recommends `agy-switch doctor --fix` (or `agy-switch switch honey`), and renders the `Pending Reload` line for `"honey"`.

#### Scenario: [SYNC-VIS-003] `agy-switch list` surfaces structured `sync_status` in JSON and explicit desync warning in table and listicle formats
- GIVEN Vault active account `"honey"` with drifted Desktop token (`"old@example.com"`)
- WHEN `agy-switch list` (`--format table`, `--format list`) and `agy-switch list --json` execute
- THEN `engine.list_accounts()` and `--json` output include `"sync_status"` with `"in_sync": false` and `"drifted_targets": ["desktop"]`
- AND both `format_accounts_table()` and `format_accounts_listicle()` display an explicit desync warning naming `Desktop` and remediation guidance.

#### Scenario: [SYNC-VIS-ERR-001] Corrupt, missing, or symlinked Desktop/CLI token files emit clear desync warning without crashing `status` or `list`
- GIVEN Vault active account `"honey"` where `~/.gemini/jetski-standalone-oauth-token` is corrupt JSON and `~/.gemini/antigravity-cli/antigravity-oauth-token` is missing or a symlink
- WHEN `agy-switch status` and `agy-switch list` execute
- THEN both commands exit cleanly with code `0` without unhandled exceptions
- AND display explicit desync warnings identifying the affected surface(s).

#### Scenario: [SYNC-VIS-ERR-002] Local desync verification in `status` and `list` performs strictly zero network calls even when offline
- GIVEN network/HTTP sockets are blocked or unavailable in the runtime environment
- WHEN `engine.get_status()`, `engine.list_accounts()`, `format_status_report()`, and `format_accounts_table()` execute
- THEN sync verification completes strictly via local file reads with zero `urllib` or network calls.

#### Scenario: [SYNC-VIS-ERR-003] Omitted, `None`, or malformed `sync_status` / `pending_reload` payloads handled gracefully by formatters
- GIVEN caller dictionaries passed to `format_status_report()`, `format_accounts_table()`, or `format_accounts_listicle()` omit `"sync_status"` / `"pending_reload"` or set them to `None`, `"invalid"`, or `{"in_sync": "bad", "drifted_targets": 123}`
- WHEN each formatter executes
- THEN it renders cleanly without raising `AttributeError`/`TypeError` and emits no spurious desync warning.

#### Scenario: [SYNC-VIS-EDGE-001] Vault with no active account omits spurious desync warnings in `status` and `list`
- GIVEN a vault with no active account configured (`active` file absent or empty)
- WHEN `agy-switch status` and `agy-switch list` run
- THEN no false desync warning is rendered and output gracefully indicates no account is currently active.

#### Scenario: [SYNC-VIS-EDGE-002] Dual-surface drift names both Desktop and CLI explicitly across `status`, table, listicle, and JSON outputs
- GIVEN Vault active account `"honey"` where BOTH `~/.gemini/jetski-standalone-oauth-token` and `~/.gemini/antigravity-cli/antigravity-oauth-token` hold credentials from `"stale-acc"`
- WHEN `agy-switch status`, `agy-switch list --format table`, `agy-switch list --format list`, and `agy-switch list --json` execute
- THEN `sync_status["drifted_targets"]` includes both `"desktop"` and `"cli"`
- AND every human-readable output explicitly warns that both `Desktop` and `CLI` are out of sync with `"honey"`.

#### Scenario: [SYNC-VIS-EDGE-003] `agy-switch list` surfaces queued `Pending Reload` across table, listicle, and JSON even after disk tokens are healed
- GIVEN Vault active account `"honey"` matches both Desktop and CLI tokens on disk (`in_sync=True`)
- AND `cache/pending_reload.json` contains an unexpired reload marker (`target_account="honey"`, `ttl_seconds=1800`) awaiting Desktop safe-turn reload
- WHEN `agy-switch list --format table`, `agy-switch list --format list`, and `agy-switch list --json` execute
- THEN `list --json` includes `"pending_reload"` with `"target_account": "honey"`
- AND both table and listicle views render the queued `Pending Reload` notice for `"honey"` without emitting a false disk desync warning.
