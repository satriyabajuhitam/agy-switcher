# agy-switch Domain Glossary

The authoritative ubiquitious domain vocabulary for `agy-switch` multi-account management, session lifecycle, and quota failover.

## Language

**Presence Lock**:
A session marker indicating that an Antigravity workspace or command-line session is actively open.
_Avoid_: File mutex, PID file, session file

**Stale Lock**:
A presence lock whose owning process has exited or ceased operation, no longer representing an active user session.
_Avoid_: Dead file, orphan lock, zombie file

**Active Turn**:
An uninterrupted inferencing or agent execution window during which background processes must not be terminated.
_Avoid_: Task busy, prompt lock, inference lock

**Dual-Directory Presence**:
The unified tracking of active user sessions across both Desktop and command-line Antigravity environments.
_Avoid_: Multi-root scanning, hybrid presence

**Disk-Only Swap**:
A token switching operation that updates credentials strictly in disk files without restarting or checking background language server processes.
_Avoid_: Fast switch, headless rotation, silent swap

**In-Process SDK Facade**:
A programmatic standard-library interface exposing account status, quota telemetry, and rotation mechanisms directly within a consumer Python runtime.
_Avoid_: Direct engine import, internal API, library binding

**Headless Authentication**:
An OAuth credential acquisition flow designed for non-graphical server environments that captures authorization codes via terminal input or direct parameters.
_Avoid_: Terminal OAuth, manual login, non-GUI login

**Vault Bundle**:
A compressed archive encapsulating multi-account vault states with restricted POSIX permissions and cryptographic checksum verification for transport.
_Avoid_: Account export, backup archive, vault package

**Mid-Turn Failover**:
An automatic credential rotation and subprocess re-execution cycle triggered mid-turn when an inferencing engine reports quota exhaustion (HTTP 429 / RESOURCE_EXHAUSTED), preserving the active conversation context transparently.
_Avoid_: Turn retry, hot swap, prompt retry

**Fleet Exhaustion**:
An operational state where all accounts registered within the multi-account vault have exhausted their model quota or remain in active cooldown.
_Avoid_: All down, zero quota, total burnout

**Panic Event**:
A high-severity lifecycle signal emitted upon Fleet Exhaustion to record structured failure reflections into persistent memory (`agy-memory`) and notify human operators with estimated reset times.
_Avoid_: Crash dump, fatal alert, alarm trigger

