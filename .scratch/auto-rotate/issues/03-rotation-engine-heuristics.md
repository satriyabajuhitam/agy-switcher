# 03: Rotation Engine & Heuristics

## Primary Sources & Contract References
- **Master Specification**: [.scratch/auto-rotate/spec.md](file:///srv/agy-workspaces/booster/agy-switcher/.scratch/auto-rotate/spec.md)
- **Governing ADRs**: [docs/adr/0001-auto-rotate-architecture.md](file:///srv/agy-workspaces/booster/agy-switcher/docs/adr/0001-auto-rotate-architecture.md)

## Target Layout & Architectural Constraints
- **Target Files / Directory**: `scripts/rotation_engine.py` (New), `scripts/agy_switch.py`
- **Prohibitions (What NOT to do)**:
  - DO NOT hardcode `datetime.now()` directly inside the evaluation logic. Time must be injectable for deterministic testing.

## Execution Context & Model Recommendation
- **Capability Tier**: Tier 2: Deep Reasoning
- **Recommended Model**: Gemini Pro
- **Model Rationale & Fallback**: Complex conditional heuristics and strict deterministic testing constraints require deep reasoning.

## What to build
Implement the algorithmic engine that decides whether a token rotation should occur based on fetched quotas and the requested model (`modelName`).
1. Create `RotationEngine` class.
2. Implement Worst-Case Bucket evaluation (min fraction between 5-hour and weekly buckets).
3. Implement `cache_optimized` logic (retain if > 15%).
4. Implement `drain_window` logic (retain if reset <= 30m and > 5%).
5. Implement `imminent_reset` logic (prioritize candidates resetting in <= 45m and > 10%).
6. Expose the `evaluate(accounts, current_time=None)` method.
7. Wire the engine into the hook command to make a decision (but do not execute the filesystem swap yet).

**Blocked by:** 02-fast-quota-telemetry.md

**Status:** resolved

## Acceptance Criteria
- [x] Engine correctly targets Gemini vs Claude quotas based on `modelName`.
- [x] Worst-case evaluation correctly triggers on the most restrictive bucket.
- [x] Heuristics (cache, drain, imminent) are deterministically tested with mock times and payloads.
