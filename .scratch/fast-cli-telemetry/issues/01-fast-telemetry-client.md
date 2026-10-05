# 01: Fast Telemetry Client (QuotaClient) & Secure Cache

## Primary Sources & Contract References
- **Master Specification**: [.scratch/fast-cli-telemetry/spec.md](file:///srv/agy-workspaces/booster/agy-switcher/.scratch/fast-cli-telemetry/spec.md)
- **Relevant Research / Docs**: [docs/research/auto-rotate-account-analysis.md](file:///srv/agy-workspaces/booster/agy-switcher/docs/research/auto-rotate-account-analysis.md)
- **ADRs**: [docs/adr/0002-cli-fast-telemetry.md](file:///srv/agy-workspaces/booster/agy-switcher/docs/adr/0002-cli-fast-telemetry.md)
- **Domain Glossary**: [CONTEXT.md](file:///srv/agy-workspaces/booster/agy-switcher/CONTEXT.md)

## Target Layout & Architectural Constraints
- **Target Files / Directory**: `scripts/agy_switch.py`, `tests/test_quota_client.py`
- **Prohibitions (What NOT to do)**: 
  - Do NOT use `requests` or `aiohttp` (strictly use Python's built-in `urllib.request`).
  - Do NOT omit the network timeout; all requests must have a strict `timeout=2.0`.
  - Do NOT log OAuth tokens during API requests.

## Execution Context & Model Recommendation
- **Capability Tier**: Tier 1: Fast / Standard
- **Recommended Model**: Gemini Flash (e.g. Gemini 3.8 Flash)
- **Model Rationale & Fallback**: Bounded slice, straightforward standard library HTTP & OS path manipulation.

## What to build
The foundational `QuotaClient` module. It uses pure `urllib.request` to hit the CloudCode API and parses the response. It also provides secure caching by idempotently creating `~/.antigravity-vault/cache/` with strictly `0700` permissions and the cache JSON file with `0600` permissions.

**Blocked by:** None (can start immediately)

**Status:** resolved

## Acceptance Criteria
- [x] `QuotaClient` can successfully hit CloudCode internal API and parse quota and `reset_time` per the new cache schema.
- [x] Network requests enforce a strict `timeout=2.0` seconds.
- [x] The vault cache directory `~/.antigravity-vault/cache/` is created with `0700` permissions if it doesn't exist.
- [x] The `last_known_quota.json` file is written with `0600` permissions.
- [x] Exhaustive unit tests added in `tests/test_quota_client.py` using `unittest.mock`.
