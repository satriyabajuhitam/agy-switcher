# 02: Fast Quota Telemetry Client

## Primary Sources & Contract References
- **Master Specification**: [.scratch/auto-rotate/spec.md](file:///srv/agy-workspaces/booster/agy-switcher/.scratch/auto-rotate/spec.md)
- **Governing ADRs**: [docs/adr/0001-auto-rotate-architecture.md](file:///srv/agy-workspaces/booster/agy-switcher/docs/adr/0001-auto-rotate-architecture.md)

## Target Layout & Architectural Constraints
- **Target Files / Directory**: `scripts/quota_client.py` (New), `scripts/agy_switch.py`
- **Prohibitions (What NOT to do)**:
  - DO NOT use the `requests` library. You must strictly use `urllib.request`.
  - DO NOT block indefinitely on network calls; enforce a 2.0s timeout and fail gracefully (`{}`).

## Execution Context & Model Recommendation
- **Capability Tier**: Tier 2: Deep Reasoning
- **Recommended Model**: Gemini Pro
- **Model Rationale & Fallback**: Requires careful raw `urllib` handling, OAuth token refresh logic, and exception catching for network failure edge cases.

## What to build
Implement the fast HTTP client that fetches real quotas from Google CloudCode API.
1. Build `QuotaClient` using pure standard-library `urllib.request`.
2. Implement `POST /v1internal:retrieveUserQuotaSummary`.
3. Handle strict `timeout=2.0` and gracefully catch `URLError` / `TimeoutError` (fail-open).
4. Implement OAuth `refresh_token` flow if the access token returns a 401. If the refresh token itself is revoked/invalid, catch the error, log to `stderr`, and fail-open.
5. Wire this client into the `hook-pre-invocation` skeleton to print fetched quota (dummy evaluation for now) to `stderr`.

**Blocked by:** 01-basic-hook-lifecycle.md

**Status:** resolved

## Acceptance Criteria
- [x] `QuotaClient` successfully fetches and parses the quota JSON from CloudCode.
- [x] Network partitions or timeouts >2s throw caught exceptions and fail gracefully.
- [x] Expired access tokens correctly trigger the refresh token flow.
- [x] Unit tests correctly mock `urllib.request.urlopen`.
