# Hermes Agent - Development Guide

**Never give up on the right solution.**

This root file is intentionally compact because it is auto-loaded into every
Hermes agent session in this repo. Detailed guidance was moved to
`docs/agent-guides/`. Read the relevant guide before editing a subsystem.

## What Hermes Is

Hermes is a personal AI agent that runs the same core across CLI, messaging
gateway, TUI, Electron desktop, IDE/ACP, cron, and subagents. It learns through
memory + skills, can delegate to subagents, and uses plugins/skills for most new
capability.

Two design constraints dominate reviews:

1. **Per-conversation prompt caching is sacred.** Do not mutate past context,
   swap toolsets, rebuild the system prompt, or reload prompt-affecting state
   mid-conversation. The exception is context compression.
2. **The core is a narrow waist.** Every core model tool is sent on every API
   call. Prefer extending existing code, CLI+skill, gated tools, plugins, or MCP
   before adding core tool surface.

## Detailed Guides

Before touching these areas, read the corresponding guide:

| Area | Read |
|---|---|
| Contribution intent, PR acceptance/rejection, footprint ladder | `docs/agent-guides/contribution-rubric.md` |
| Project structure, AIAgent loop, CLI architecture | `docs/agent-guides/core-architecture.md` |
| TUI, dashboard embedded TUI, desktop app, TypeScript style, skins | `docs/agent-guides/ui-and-typescript.md` |
| Tools, plugins, skills, toolsets, delegation, curator | `docs/agent-guides/extension-surfaces.md` |
| Config, dependency pinning, cron, Kanban, policies, profiles | `docs/agent-guides/configuration-and-operations.md` |
| Known pitfalls and full testing policy | `docs/agent-guides/pitfalls-and-testing.md` |

If a detailed guide and this root file disagree, this root file defines the
high-priority rule, and the detailed guide should be updated in the same change.

## Contribution Rubric Summary

### What we want

- Fix real bugs with a reproduction on current `main` and a line-level account
  of where the fix acts.
- Expand product reach at the edges: platform adapters, providers, models,
  desktop/TUI/dashboard features, plugins, MCP catalog entries.
- Refactor god-files into focused modules when the refactor itself is the
  declared scope.
- Preserve prompt caching, role alternation, and stable system prompts.
- Write behavior/invariant tests, not snapshots of data expected to change.
- Validate real paths with real imports and temp `HERMES_HOME`, especially for
  config propagation, security boundaries, remote backends, and file/network IO.
- Preserve contributor credit when salvaging external work.

### What we do not want

- Speculative hooks or extension points with no concrete consumer.
- New `HERMES_*` env vars for non-secret user config. Secrets go in `.env`;
  behavioral settings go in `config.yaml`. Internal env mirrors are allowed when
  bridged from config.
- New core tools when terminal+file, a skill, a plugin, or MCP would work.
- Pagination on instructional tools the agent must read fully, such as skills.
- Security fixes that destroy the feature they secure.
- Outbound telemetry/attribution without explicit opt-in gating.
- Plugins that modify core files for plugin-specific behavior.

### Verify the premise before fixing

Before calling something a bug, trace the real runtime and original intent.
Limitations may be deliberate isolation, not gaps. Read `git log -p -S` for
load-bearing omissions or restrictions. If you cannot point to the line where
the bug manifests and show how the change affects that line, keep investigating.

### Footprint ladder for new capability

Choose the least permanent surface that works:

1. Extend existing code.
2. CLI command + skill.
3. Service-gated tool with `check_fn`.
4. Plugin.
5. MCP server/catalog entry.
6. New core tool, only as last resort.

When 3+ PRs integrate the same category, design an ABC + orchestrator and make
providers/plugins implement it instead of merging one-offs.

## Development Environment

```bash
# Prefer .venv; fall back to venv if that is what the checkout has.
source .venv/bin/activate   # or: source venv/bin/activate
```

`scripts/run_tests.sh` probes `.venv`, then `venv`, then
`$HOME/.hermes/hermes-agent/venv`.

## Project Map

Canonical source is the filesystem. Key entry points:

| Path | Purpose |
|---|---|
| `run_agent.py` | `AIAgent`, core conversation loop |
| `model_tools.py` | Tool orchestration/discovery/dispatch |
| `toolsets.py` | Toolset definitions and core tool list |
| `cli.py`, `hermes_cli/` | CLI, setup, config, slash commands |
| `agent/` | Prompt builder, model routing, memory, compression, providers |
| `tools/` | Built-in tool implementations via `tools.registry` |
| `gateway/` | Messaging gateway and platform adapters |
| `plugins/` | General plugins, memory/model/image/context providers |
| `cron/` | Scheduler |
| `ui-tui/`, `tui_gateway/` | Ink TUI and Python JSON-RPC backend |
| `apps/desktop/` | Electron desktop chat app |
| `tests/` | Pytest suite |
| `website/` | Docusaurus docs |

User config is `~/.hermes/config.yaml`; secrets are `~/.hermes/.env`; logs are
`~/.hermes/logs/`. Use `get_hermes_home()` for code paths and
`display_hermes_home()` for user-facing path text.

## Architecture Invariants

### Prompt caching

Do not change loaded tools, memories, skills, or system prompt inside an active
conversation unless using the established compression/invalidation path. Slash
commands that alter prompt-affecting state must default to deferred invalidation
(next session) with an explicit `--now` style escape hatch.

### Message role alternation

Do not create two same-role messages in a row, and do not inject synthetic user
messages mid-loop. Use existing tool/steering/queue mechanisms.

### Config and secrets

- `config.yaml`: behavior, thresholds, paths, feature flags, display prefs.
- `.env`: credentials only: API keys, tokens, passwords.
- If legacy code needs an env var, bridge from config to env at startup and
  document config as canonical.

### Profile-safe paths

```python
from hermes_constants import get_hermes_home, display_hermes_home

state_path = get_hermes_home() / "state.json"      # code path
print(f"Saved to {display_hermes_home()}/state.json")  # user-facing
```

Never hardcode `Path.home() / ".hermes"` for profile-scoped state. Profile
operations themselves are HOME-anchored so any profile can list all profiles.

## Adding or Changing Surface

### Slash commands

All slash commands originate in `hermes_cli/commands.py` as `CommandDef`.
Downstream consumers derive from the registry: CLI dispatch/help, gateway known
commands/help, Telegram menu, Slack mapping, autocomplete.

To add a command:

1. Add `CommandDef` to `COMMAND_REGISTRY`.
2. Add CLI handler in `HermesCLI.process_command()` when CLI-visible.
3. Add gateway handler in `gateway/run.py` when gateway-visible.
4. Use config-gated gateway exposure when a CLI-only command should be
   available in messaging only under an opt-in config.

### Built-in tools

Before adding a built-in/core tool, apply the footprint ladder. If a core tool
is truly necessary:

1. Create `tools/your_tool.py` and register with `tools.registry.registry`.
2. Return JSON strings from handlers.
3. Add a `check_fn` and `requires_env` when applicable so unavailable tools do
   not appear.
4. Expose the tool in `toolsets.py`; auto-discovery imports the file but does
   not expose it to agents by itself.
5. Use `get_hermes_home()` / `display_hermes_home()` for paths in state/schema.

### Plugins

Plugins live under `plugins/`, `~/.hermes/plugins/`, `.hermes/plugins/`, or pip
entry points. Plugins must not hardcode plugin-specific logic into core files.
If a plugin needs a missing capability, widen the generic plugin surface.

Memory providers are a closed in-tree set. New memory backends must ship as
standalone plugins, not new directories under `plugins/memory/`.

Model providers live under `plugins/model-providers/` and are discovered lazily
by the providers registry, separately from the general plugin manager.

### Skills

Built-in skills live under `skills/`; heavier/niche official skills live under
`optional-skills/` and are installed explicitly. Skill slash commands are loaded
as user messages, not system prompt mutations, to preserve caching.

Skill authoring basics:

- Frontmatter starts at byte 0 and includes `name` + `description`.
- Keep descriptions specific and short enough for validators.
- Long details go in `references/`; reusable scripts/templates go under the
  skill directory.
- Prefer extending an umbrella skill over creating one-session micro-skills.

## TypeScript and UI Rules

Applies to desktop, TUI, website, and future TS packages.

- Prefer small nanostores for shared state; colocate atoms with the feature.
- Keep route roots thin; avoid monolithic hooks.
- Components subscribe with `useStore`; non-rendering actions read `$atom.get()`.
- Prefer interfaces for public props and shared object shapes.
- Extend React primitives for props: `React.ComponentProps<'button'>`,
  `React.ComponentProps<typeof Dialog>`, `Omit`, `Pick`.
- Prefer table-driven mappings over condition ladders.
- `src/app` owns routes/pages; `src/store` shared atoms; `src/lib` pure helpers.

Dashboard `/chat` embeds the real `hermes --tui` via PTY. Do not reimplement the
primary chat transcript/composer in React dashboard; extend Ink so dashboard gets
it automatically. Supporting React panels are fine when they do not become a
second chat surface.

Desktop app (`apps/desktop/`) is separate from CLI/TUI and uses Electron + React
+ nanostores + `tui_gateway` JSON-RPC. Desktop slash palette curation must not
hide user extensions: skill commands and `quick_commands` must pass through
suggestion and execution gates.

## Dependency Policy

All dependencies must have upper bounds.

| Source | Policy | Example |
|---|---|---|
| PyPI post-1.0 | `>=floor,<next_major` | `httpx>=0.28.1,<1` |
| PyPI pre-1.0 | cap within next 1-2 minor bands | `pkg>=0.29,<0.32` |
| Git URL | pin commit SHA | `git+https://...@<40-char-sha>` |
| GitHub Actions | SHA + version comment | `uses: actions/checkout@<sha>  # v4` |
| CI-only pip | exact pin | `pyyaml==6.0.2` |

Run `uv lock` after changing `pyproject.toml` dependencies.

## Testing Policy

**Always use `scripts/run_tests.sh`; do not call `pytest` directly for final
validation.** The wrapper enforces CI parity: credential env vars unset,
`HERMES_HOME` isolated, UTC timezone, `C.UTF-8`, xdist, and subprocess-per-test
isolation.

Examples:

```bash
scripts/run_tests.sh                                  # full suite
scripts/run_tests.sh tests/gateway/                   # directory
scripts/run_tests.sh tests/agent/test_foo.py::test_x  # one test
scripts/run_tests.sh -v --tb=long                     # pass-through pytest args
scripts/run_tests.sh --no-isolate tests/foo/          # debug-only faster path
```

Tests must never write to real `~/.hermes/`. For profile tests, mock both
`HERMES_HOME` and `Path.home()` so profile roots stay inside temp dirs.

Do not write change-detector tests that fail when expected catalogs/lists change.
Test behavior and invariants instead, such as “every model has a context length”
or “migration bumps to current config version,” not exact model names or counts.

## Common Pitfalls

- Do not hardcode `~/.hermes`; use profile-safe helpers.
- Do not introduce new `simple_term_menu` usage; prefer `hermes_cli/curses_ui.py`.
- Do not use ANSI `\033[K` in spinner/display code; use space padding.
- `_last_resolved_tool_names` is process-global and saved/restored around
  subagent execution.
- Tool schemas must not hardcode cross-tool references. Add dynamic guidance in
  `get_tool_definitions()` when a cross-reference is needed.
- Gateway control/approval commands must bypass both the base adapter active
  session queue and the gateway runner running-agent guard.
- Before squash-merging stale branches, reset to current `origin/main` and
  reapply commits so unrelated stale files do not revert main.
- Do not wire in dead code without E2E validation of the real import/runtime
  chain against a temp `HERMES_HOME`.
- NGINX/site backups must not remain in `sites-enabled/`; NGINX loads them.
- If `AGENTS.md` or any context file grows, prefer moving detail into
  subsystem guides or skills before raising `context_file_max_chars`.

## Operational Notes

### Cron

Use `hermes cron ...` or the cron tool for scheduled work. Recurring prompts must
be self-contained; cron runs in fresh sessions. Script-only watchdogs should be
quiet when there is nothing to report.

### Kanban

Kanban is a board-scoped multi-agent work queue. Board is the hard isolation
boundary; tenant is a soft namespace within a board. After repeated failures on
the same task, the dispatcher auto-blocks to avoid spin loops.

### Background processes

For long bounded commands, use `terminal(background=true, notify_on_complete=true)`.
For long-lived servers/watchers, use `background=true` and verify readiness via
health checks or rare watch patterns. Do not use shell-level `nohup`, `disown`,
trailing `&`, or daemon wrappers in foreground mode.

## If You Need More Detail

This file is an always-loaded router. The extracted guides under
`docs/agent-guides/` preserve the detailed reference material. Keep this file
small enough to load without truncation, and move subsystem-specific depth into
those guides or into skills.
