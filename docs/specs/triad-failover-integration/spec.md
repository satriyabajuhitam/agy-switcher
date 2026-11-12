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
The `LogWatcher` daemon MUST actively evaluate the active account's remaining quota in `poll_once()`, trigger proactive credential rotation when the quota falls at or below the configured quota threshold (defaulting to 20% or `AgySwitchEngine.get_quota_threshold()`), ensure candidate standby accounts have healthy quota (>= 70%), evaluate the drain window heuristic (retaining active accounts if reset is within 30 minutes and remaining quota exceeds 5%), and execute account rotation and Language Server reloading (`restart_server=True, force=True`) EXCLUSIVELY at a verified **Safe Turn Boundary** where the structured conversation transcript indicates turn completion (`status == "DONE"` or `type == "USER_INPUT"`) and `ProcessSupervisor.is_server_busy()` confirms the language server is quiescent, guaranteeing that in-flight prompts, streaming inferences, and tool executions are never interrupted.

#### Scenario: [WATCH-PROACTIVE-001] Proactive rotation triggered when active account quota reaches or falls below threshold at Safe Turn Boundary
- GIVEN active account `"primary-acc"` with remaining Gemini quota at 14% (`fraction = 0.14 <= 0.20`)
- AND standby account `"standby-acc"` has healthy Gemini quota (85% >= 70%)
- AND the active transcript `transcript.jsonl` has recorded a completed turn event with `status == "DONE"`
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

#### Scenario: [WATCH-PROACTIVE-005] Safe Turn Boundary verified via structured transcript record and process supervisor quiescence
- GIVEN an active transcript tailing loop where the latest record has `type == "USER_INPUT"`
- AND no open presence locks or active child processes exist in `/proc` for `language_server`
- WHEN `LogWatcher._is_turn_quiescent()` or `ProcessSupervisor.is_server_busy()` is evaluated
- THEN it confirms that the agent turn has completed and the system is waiting for user input
- AND certifies the state as a verified Safe Turn Boundary.

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
