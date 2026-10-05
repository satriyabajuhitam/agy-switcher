# agy-switch Roadmap

## Upcoming Features

### 1. Global Fast Quota Telemetry for CLI
**Status**: Planned (Post Auto-Rotate Merge)

**Description**:
Currently, manual quota checking commands rely on the slow Antigravity CLI subprocess (`agy -p /usage`), which can take upwards of 30 seconds to bootstrap and execute. 

With the successful implementation of the fast HTTP `QuotaClient` (using pure `urllib.request` to hit the CloudCode API) originally built for the `PreInvocation` auto-rotate hook, we will migrate all manual CLI commands to use this fast telemetry.

**Key Deliverables**:
- **Instant `list --quota`**: Ping all vaulted accounts in milliseconds to display a full quota table.
- **Real-time `status`**: Embed current active account quota health directly into the status command.
- **Smart Manual Switcher (`switch --auto`)**: Graft the `RotationEngine` logic into the manual switch command, allowing the CLI to instantly scan all accounts and switch to the optimal one based on `cache_optimized` and `drain_window` heuristics without user guesswork.
