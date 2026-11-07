# Repository Coding Standards: agy-switch

> Canonical reviewer and implementer standards for `agy-switch` (Antigravity Multi-Account Switcher & Vault Manager).
> Enforces Python standard library rigor, boundary validation, and security invariants.

---

## 1. Zero External Dependencies Invariant

- **Python Standard Library Only**: Rely strictly on Python 3.10+ built-in modules (`os`, `sys`, `json`, `pathlib`, `urllib`, `ssl`, `sqlite3`, `threading`, `subprocess`, `dataclasses`, `unittest`).
- **External Pip Ban**: No third-party pip dependencies (`requests`, `pydantic`, `cryptography`, etc.) are permitted in production source code.

---

## 2. In-Process SDK & Disk-Only Swap Discipline

- **Disk-Only Swap Default**: Programmatic failovers via `agy_switcher.api.auto_rotate_on_exhaustion` or `switch_account` MUST default to `reload_server=False`.
- **Active Turn Preservation**: Never restart or interrupt `language_server` during an active agent turn unless explicitly forced (`force=True` and verified inactive).
- **Atomic Dual-Destination Swaps**: All credential file updates must be emitted atomically using `os.replace` to prevent partial reads or corrupt states.

---

## 3. Strict Type Annotations & Seam Verification (`mypy --strict`)

- **Strict Seam Typing**: All public module functions, dataclass attributes, and parameters must be explicitly type-annotated (`[POLY-RULE-02]`, `[TYPE-01]`).
- **Zero Unchecked `Any`**: Unchecked `Any` is strictly banned on public API signatures and models (`[ERR-POLY-RULE-03]`). Use explicit domain models, typed tuples, or `object` with runtime type narrowing.
- **Verification Gate**: Any new public function or model must pass `mypy --strict` before review:
  ```bash
  PYTHONPATH=scripts python3 -m mypy scripts/agy_switcher/api.py scripts/agy_switcher/models.py scripts/agy_switcher/formatters.py
  ```

---

## 4. Network Boundaries & Credential Protection

- **URL Scheme & Host Validation (SSRF Prevention)**:
  Outbound HTTP requests executed via `urllib.request.urlopen` MUST validate protocol scheme and network location:
  ```python
  parsed = urllib.parse.urlsplit(url)
  if parsed.scheme not in ("http", "https") or not parsed.netloc:
      raise ValueError(f"Invalid URL scheme '{parsed.scheme}'. Only http:// and https:// are permitted.")
  ```
  Arbitrary URI schemes (`file://`, `ftp://`) must never reach standard library HTTP handlers.
- **Credential Redaction in Logs**:
  API tokens (such as Telegram bot tokens `https://api.telegram.org/bot<token>/...` or OAuth bearer tokens) MUST NOT be printed to `sys.stderr` or logged in unhandled exception strings. Sanitize URLs prior to logging:
  ```python
  safe_url = re.sub(r"/bot[^/]+/", "/bot<REDACTED>/", url)
  ```
- **POSIX Permission Boundaries**:
  Token files must be written with `0600` permissions (`os.chmod(p, 0o600)`), and account vault directories must enforce `0700` (`0o700`).

---

## 5. Code Smell Discipline (Fowler Baseline)

- **Fowler Smell #13: Obvious Comments**:
  Strictly prohibited. Never write what-comments that repeat syntax (e.g. `# Iterate through accounts`, `# Send http request`). Reserve comments solely for architectural invariants, bug workarounds, or RFC references.
- **Fowler Smell #1: Mysterious Name / Parameter Aliases**:
  Whenever backwards-compatible parameter aliases are supported (e.g. `alias or name`), explicitly document the legacy mapping in the method docstring.
- **Fowler Smell #2: Duplicated Code**:
  Centralize shared logic (such as model normalization or quota bucket parsing) in utility modules (`formatters.py`, `telemetry.py`).

---

## 6. Testing & Concurrency Invariants

- **Host `/proc` Safety Latch**:
  Test fixtures instantiating `ProcessSupervisor` or `AgySwitchEngine` MUST supply an isolated `proc_dir` to prevent sending signals to host processes.
- **Thread Safety**:
  Alert dispatchers and background watchers must operate on non-blocking daemon threads with bounded timeouts (<= 3.0s) and clean exception handling.
