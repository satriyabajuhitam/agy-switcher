# 06: Structural Refactoring & Fowler Smells

## Primary Sources & Contract References
- **Master Specification**: [.scratch/auto-rotate/spec.md](file:///srv/agy-workspaces/booster/agy-switcher/.scratch/auto-rotate/spec.md)
- **Governing ADRs**: [docs/adr/0001-auto-rotate-architecture.md](file:///srv/agy-workspaces/booster/agy-switcher/docs/adr/0001-auto-rotate-architecture.md)
- **Code Review**: `06-structural-refactoring-fowler-smells` corresponds to Axis 1 Fowler smell findings from `/code-review`.

## Target Layout & Architectural Constraints
- **Target Files / Directory**: `scripts/agy_switch.py`, `scripts/rotation_engine.py`
- **Prohibitions (What NOT to do)**:
  - DO NOT break existing unit tests. Modify the tests to align with the new structural refactors.

## Execution Context & Model Recommendation
- **Capability Tier**: Tier 2: Deep Reasoning
- **Recommended Model**: Gemini Pro
- **Model Rationale & Fallback**: Deep reasoning is required to safely execute cross-module refactoring without breaking existing contracts.

## What to build
Clean up the Martin Fowler code smells identified in the codebase:
1. **Divergent Change**: Extract the 60-line `hook-pre-invocation` logic out of the `main()` function in `agy_switch.py` into a dedicated handler function (e.g., `handle_pre_invocation_hook(args, engine)`).
2. **Primitive Obsession / Data Clumps**: In `rotation_engine.py`, replace the raw tuples `(lowest_fraction, earliest_reset)` with a cohesive dataclass `QuotaStatus`.
3. **Feature Envy**: Stop reaching into `_get_worst_case` from `agy_switch.py`. Expose a public method on `RotationEngine` (e.g., `is_exhausted(quota_data)`) to encapsulate this check.
4. **Message Chains**: Clean up the deep dictionary accesses `token_data.get("token", {}).get("access_token")` in `agy_switch.py` by using safer, encapsulated extraction methods or default `.get()` chaining safely.

**Blocked by:** 05-critical-bug-architectural-fixes.md

**Status:** resolved

## Acceptance Criteria
- [x] `main()` function is slim and delegates hook execution to a dedicated function.
- [x] `QuotaStatus` dataclass encapsulates fraction and reset time logic.
- [x] `RotationEngine` exposes a public method for exhaustion checking.
- [x] All unit tests pass after refactoring.
