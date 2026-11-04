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
