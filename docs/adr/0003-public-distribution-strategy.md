# 0003: Public Distribution Strategy

## Status
Accepted

## Context
As `agy-switch` prepares for public release on GitHub, we need to decide on the optimal distribution and packaging strategy.
The tool serves two distinct ecosystems:
1. Terminal-native users (Linux/macOS/WSL) who want a standalone CLI tool.
2. Antigravity Agent/IDE users who need it integrated as an intelligent skill/booster.

Because `agy-switch` is a Python tool, standard Python packaging (e.g., `pyproject.toml` and `pipx`) was considered to facilitate isolated installations. Furthermore, standard open-source governance files (like `SECURITY.md`, `CONTRIBUTING.md`, and automated GitHub Actions CI) are typically included in public repositories.

However, a core architectural tenet of `agy-switch` is **Zero External Pip Dependencies**, prioritizing raw standard POSIX bash and Python standard library execution.

## Decision
We choose **Option 1 (All-in-One Plugin + Terminal CLI via `install.sh`)** as our exclusive distribution method.
- **Reject `pyproject.toml`**: We will not provide Python packaging files to keep the root directory absolutely minimal and strictly adhere to the zero-dependency, script-first ethos.
- **Unified Installer**: `install.sh` acts as the single entry point, bridging the gap by configuring both the Antigravity Plugin registry (`.agents/plugins/` or `~/.gemini/config/plugins/`) and the standalone CLI wrapper (`~/.local/bin/agy-switch`) simultaneously.
- **Defer Heavy Governance Files**: We will defer generating files like `SECURITY.md`, `CONTRIBUTING.md`, and GitHub Actions workflows, focusing purely on a clean directory structure and essential licensing (MIT License) for now.

## Consequences
- The repository remains extremely clean and approachable.
- Users who strongly prefer `pipx` or standard Python packaging will be forced to use `install.sh`, which deviates from modern Python norms but guarantees consistency across Antigravity and standalone environments.
- The absence of immediate CI and governance files means we rely on manual repository hygiene checks before the initial public release.
