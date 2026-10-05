# agy-switch Workspace Agent Information

## System Identity
This workspace hosts `agy-switch` (Antigravity Multi-Account Switcher & Vault Manager), paired with `agy-memory` as its persistent workspace memory operating system.

## Commands
- **Run tests**: `python3 -m unittest discover tests/`
- **Verify switcher install**: `bash install.sh --local`

## Operational Rules
1. **Zero External Pip Dependencies**: Rely strictly on standard POSIX bash and the Python 3.10+ standard library.
2. **Dual-Destination Atomic Swaps**: Token updates must be emitted atomically (`os.replace`) to Desktop and CLI token files.
3. **Turn Safety & Process Supervisor**: Never restart or interrupt `language_server` during an active agent turn without checking process status.
4. **Absolute Credential Protection**: Never expose, log, or commit OAuth tokens from `~/.antigravity-vault/`.
5. **Persistent Memory**: Use `memory_search`, `memory_add`, and `memory_audit` MCP tools. Workspace identity files live in `.agents/identity/`.
6. **Branching & Git Workflow**: All feature development and modifications must occur on the `dev` branch. Direct commits or pushes to `main` are strictly forbidden; `main` is reserved only for tested, stable releases via merge.

## Plugins & Skills

### Matt Pocock Engineering Skills
26 engineering workflow skills maintained in standalone booster repository at `/srv/agy-workspaces/booster/agy-pocock/` and imported declaratively via `.agents/plugins.json`. Entry point: `ask-matt` skill — use it to navigate to the right flow.

The suite is hardened with Antigravity-native capabilities across all engineering workflows:
- **Slash Command Router**: Contextual recommendations for Antigravity native slash commands (`/grill-me`, `/boost`, `/plan`, `/goal`, `/learn`).
- **Persistent Memory Integration**: Explicit memory recall and storage protocols (`memory_search`, `memory_add`, `memory_audit`).
- **Zero-Dependency Execution**: Standard library and POSIX compliant.

