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

## Agent skills

### Matt Pocock engineering skills

26 engineering workflow skills packaged as a plugin at `.agents/plugins/matt-pocock/`. Entry point: `ask-matt` skill — use it to navigate to the right flow.

The suite is hardened with Antigravity-native capabilities across all engineering workflows:
- **Slash Command Router**: Contextual recommendations for Antigravity native slash commands:
  - `/boost` for deep-reasoning, multi-perspective strategic design and complex bug diagnosis (`grill-with-docs`, `wayfinder`, `research`, `diagnosing-bugs`).
  - `/teamwork-preview` for autonomous parallel multi-agent swarms across independent ticket slices (`to-tickets`, `wayfinder`).
  - `/goal` for autonomous, continuous milestone and ticket implementation until green (`implement`).
  - `/browser` for live visual UI validation and documentation lookups (`prototype`, `research`).
  - `/learn` for durable post-mortem failure learning and rule persistence into long-term memory (`diagnosing-bugs`).
- **Interactive Decision Engine (`ask_question`)**: Replaces cumbersome text polling with native GUI modals, structured option trees, `(Recommended)` defaults, and write-in fallbacks (`grill-with-docs`, `grill-me`, `to-questionnaire`).
- **Subagent Workspace Isolation & Model Tiering**:
  - `Workspace: "branch"` isolates destructive bug reproductions (`diagnosing-bugs`) and experimental throwaway spikes (`prototype`) into clean worktree branches, protecting the main working tree.
- **Single-Source Storage (`.scratch/`)**: Key design outputs, specifications, design plans, and tickets must be written EXCLUSIVELY to persistent repo storage (`.scratch/<feature-slug>/`). Do NOT emit or create artifacts in `<appDataDir>/brain/`. Keep all project work unified and versioned under `.scratch/`.
- **Persistent Memory Integration**: Explicit memory recall and storage protocols (`memory_search`, `memory_add`, `memory_audit`).
- **Zero-Dependency Execution**: Standard library and POSIX compliant.

### Issue tracker

Issues and specs are tracked locally as markdown files in `.scratch/<feature-slug>/`. See `docs/agents/issue-tracker.md`.

### Triage labels

Canonical 5-role triage vocabulary mapped 1:1 (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout (`CONTEXT.md` and `docs/adr/` at repo root). See `docs/agents/domain.md`.

