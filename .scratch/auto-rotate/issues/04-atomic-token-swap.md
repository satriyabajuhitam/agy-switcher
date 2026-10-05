# 04: Atomic Token Swap & User Notification

## Primary Sources & Contract References
- **Master Specification**: [.scratch/auto-rotate/spec.md](file:///srv/agy-workspaces/booster/agy-switcher/.scratch/auto-rotate/spec.md)
- **Governing ADRs**: [docs/adr/0001-auto-rotate-architecture.md](file:///srv/agy-workspaces/booster/agy-switcher/docs/adr/0001-auto-rotate-architecture.md)

## Target Layout & Architectural Constraints
- **Target Files / Directory**: `scripts/agy_switch.py`
- **Prohibitions (What NOT to do)**:
  - DO NOT break the CLI `sys.path`. Ensure `sys.path.insert(0, os.path.dirname(__file__))` is present to prevent `ModuleNotFoundError`.
  - DO NOT restart the `language_server` during the hook execution. Use atomic `os.replace` silently.

## Execution Context & Model Recommendation
- **Capability Tier**: Tier 2: Deep Reasoning
- **Recommended Model**: Gemini Pro
- **Model Rationale & Fallback**: Critical file I/O operations and strict invariant enforcement regarding dual-destination swapping.

## What to build
Integrate the final token swapping action and feedback mechanisms into the lifecycle hook.
1. Implement the actual token swap action inside the hook when `RotationEngine` returns a new account.
2. Perform the swap using `os.replace` to BOTH dual-destinations (`~/.gemini/jetski-standalone-oauth-token` and `~/.gemini/antigravity-cli/antigravity-oauth-token`).
3. Construct and emit the valid JSON `ephemeralMessage` to `stdout` notifying the user of the rotation.
4. Implement the edge-case fallback: if all accounts are exhausted, abort the swap and emit a warning `ephemeralMessage`.
5. Ensure `Import Safety` for peer modules inside `agy_switch.py`.

**Blocked by:** 03-rotation-engine-heuristics.md

**Status:** resolved

## Acceptance Criteria
- [x] A rotation decision correctly writes the new token to both destinations atomically.
- [x] The hook returns a properly formatted `injectSteps` JSON payload with the `ephemeralMessage`.
- [x] "All accounts exhausted" edge case properly triggers the warning message without swapping.
