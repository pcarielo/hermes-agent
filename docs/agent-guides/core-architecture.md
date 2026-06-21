# Core Architecture

## What Hermes Is

Hermes is a personal AI agent that runs the same agent core across a CLI, a
messaging gateway (Telegram, Discord, Slack, and ~20 other platforms), a TUI,
and an Electron desktop app. It learns across sessions (memory + skills),
delegates to subagents, runs scheduled jobs, and drives a real terminal and
browser. It is extended primarily through **plugins and skills**, not by
growing the core.

Two properties shape almost every design decision and are the lens for
reviewing any change:

- **Per-conversation prompt caching is sacred.** A long-lived conversation
  reuses a cached prefix every turn. Anything that mutates past context,
  swaps toolsets, or rebuilds the system prompt mid-conversation invalidates
  that cache and multiplies the user's cost. We do not do it (the one
  exception is context compression).
- **The core is a narrow waist; capability lives at the edges.** Every model
  tool we add is sent on every API call, so the bar for a new *core* tool is
  high. Most new capability should arrive as a CLI command + skill, a
  service-gated tool, or a plugin — not as core surface.

## Development Environment

```bash
# Prefer .venv; fall back to venv if that's what your checkout has.
source .venv/bin/activate   # or: source venv/bin/activate
```

`scripts/run_tests.sh` probes `.venv` first, then `venv`, then
`$HOME/.hermes/hermes-agent/venv` (for worktrees that share a venv with the
main checkout).

## Project Structure

File counts shift constantly — don't treat the tree below as exhaustive.
The canonical source is the filesystem. The notes call out the load-bearing
entry points you'll actually edit.

```
hermes-agent/
├── run_agent.py          # AIAgent class — core conversation loop (~12k LOC)
├── model_tools.py        # Tool orchestration, discover_builtin_tools(), handle_function_call()
├── toolsets.py           # Toolset definitions, _HERMES_CORE_TOOLS list
├── cli.py                # HermesCLI class — interactive CLI orchestrator (~11k LOC)
├── hermes_state.py       # SessionDB — SQLite session store (FTS5 search)
├── hermes_constants.py   # get_hermes_home(), display_hermes_home() — profile-aware paths
├── hermes_logging.py     # setup_logging() — agent.log / errors.log / gateway.log (profile-aware)
├── batch_runner.py       # Parallel batch processing
├── agent/                # Agent internals (provider adapters, memory, caching, compression, etc.)
├── hermes_cli/           # CLI subcommands, setup wizard, plugins loader, skin engine
├── tools/                # Tool implementations — auto-discovered via tools/registry.py
│   └── environments/     # Terminal backends (local, docker, ssh, modal, daytona, singularity)
├── gateway/              # Messaging gateway — run.py + session.py + platforms/
│   ├── platforms/        # Adapter per platform (telegram, discord, slack, whatsapp,
│   │                     #   homeassistant, signal, matrix, mattermost, email, sms,
│   │                     #   dingtalk, wecom, weixin, feishu, qqbot, bluebubbles,
│   │                     #   yuanbao, webhook, api_server, ...). See ADDING_A_PLATFORM.md.
│   └── builtin_hooks/    # Extension point for always-registered gateway hooks (none shipped)
├── plugins/              # Plugin system (see "Plugins" section below)
│   ├── memory/           # Memory-provider plugins (honcho, mem0, supermemory, ...)
│   ├── context_engine/   # Context-engine plugins
│   ├── model-providers/  # Inference backend plugins (openrouter, anthropic, gmi, ...)
│   ├── kanban/           # Multi-agent board dispatcher + worker plugin
│   ├── hermes-achievements/  # Gamified achievement tracking
│   ├── observability/    # Metrics / traces / logs plugin
│   ├── image_gen/        # Image-generation providers
│   └── <others>/         # disk-cleanup, google_meet, platforms, spotify,
│                         #   strike-freedom-cockpit, ...
├── optional-skills/      # Heavier/niche skills shipped but NOT active by default
├── skills/               # Built-in skills bundled with the repo
├── ui-tui/               # Ink (React) terminal UI — `hermes --tui`
│   └── src/              # entry.tsx, app.tsx, gatewayClient.ts + app/components/hooks/lib
├── tui_gateway/          # Python JSON-RPC backend for the TUI
├── acp_adapter/          # ACP server (VS Code / Zed / JetBrains integration)
├── cron/                 # Scheduler — jobs.py, scheduler.py
├── scripts/              # run_tests.sh, release.py, auxiliary scripts
├── website/              # Docusaurus docs site
└── tests/                # Pytest suite (~17k tests across ~900 files as of May 2026)
```

**User config:** `~/.hermes/config.yaml` (settings), `~/.hermes/.env` (API keys only).
**Logs:** `~/.hermes/logs/` — `agent.log` (INFO+), `errors.log` (WARNING+),
`gateway.log` when running the gateway. Profile-aware via `get_hermes_home()`.
Browse with `hermes logs [--follow] [--level ...] [--session ...]`.

## File Dependency Chain

```
tools/registry.py  (no deps — imported by all tool files)
       ↑
tools/*.py  (each calls registry.register() at import time)
       ↑
model_tools.py  (imports tools/registry + triggers tool discovery)
       ↑
run_agent.py, cli.py, batch_runner.py, environments/
```

---

## AIAgent Class (run_agent.py)

The real `AIAgent.__init__` takes ~60 parameters (credentials, routing, callbacks,
session context, budget, credential pool, etc.). The signature below is the
minimum subset you'll usually touch — read `run_agent.py` for the full list.

```python
class AIAgent:
    def __init__(self,
        base_url: str = None,
        api_key: str = None,
        provider: str = None,
        api_mode: str = None,              # "chat_completions" | "codex_responses" | ...
        model: str = "",                   # empty → resolved from config/provider later
        max_iterations: int = 90,          # tool-calling iterations (shared with subagents)
        enabled_toolsets: list = None,
        disabled_toolsets: list = None,
        quiet_mode: bool = False,
        save_trajectories: bool = False,
        platform: str = None,              # "cli", "telegram", etc.
        session_id: str = None,
        skip_context_files: bool = False,
        skip_memory: bool = False,
        credential_pool=None,
        # ... plus callbacks, thread/user/chat IDs, iteration_budget, fallback_model,
        # checkpoints config, prefill_messages, service_tier, reasoning_config, etc.
    ): ...

    def chat(self, message: str) -> str:
        """Simple interface — returns final response string."""

    def run_conversation(self, user_message: str, system_message: str = None,
                         conversation_history: list = None, task_id: str = None) -> dict:
        """Full interface — returns dict with final_response + messages."""
```

### Agent Loop

The core loop is inside `run_conversation()` — entirely synchronous, with
interrupt checks, budget tracking, and a one-turn grace call:

```python
while (api_call_count < self.max_iterations and self.iteration_budget.remaining > 0) \
        or self._budget_grace_call:
    if self._interrupt_requested: break
    response = client.chat.completions.create(model=model, messages=messages, tools=tool_schemas)
    if response.tool_calls:
        for tool_call in response.tool_calls:
            result = handle_function_call(tool_call.name, tool_call.args, task_id)
            messages.append(tool_result_message(result))
        api_call_count += 1
    else:
        return response.content
```

Messages follow OpenAI format: `{"role": "system/user/assistant/tool", ...}`.
Reasoning content is stored in `assistant_msg["reasoning"]`.

---

## CLI Architecture (cli.py)

- **Rich** for banner/panels, **prompt_toolkit** for input with autocomplete
- **KawaiiSpinner** (`agent/display.py`) — animated faces during API calls, `┊` activity feed for tool results
- `load_cli_config()` in cli.py merges hardcoded defaults + user config YAML
- **Skin engine** (`hermes_cli/skin_engine.py`) — data-driven CLI theming; initialized from `display.skin` config key at startup; skins customize banner colors, spinner faces/verbs/wings, tool prefix, response box, branding text
- `process_command()` is a method on `HermesCLI` — dispatches on canonical command name resolved via `resolve_command()` from the central registry
- Skill slash commands: `agent/skill_commands.py` scans `~/.hermes/skills/`, injects as **user message** (not system prompt) to preserve prompt caching

### Slash Command Registry (`hermes_cli/commands.py`)

All slash commands are defined in a central `COMMAND_REGISTRY` list of `CommandDef` objects. Every downstream consumer derives from this registry automatically:

- **CLI** — `process_command()` resolves aliases via `resolve_command()`, dispatches on canonical name
- **Gateway** — `GATEWAY_KNOWN_COMMANDS` frozenset for hook emission, `resolve_command()` for dispatch
- **Gateway help** — `gateway_help_lines()` generates `/help` output
- **Telegram** — `telegram_bot_commands()` generates the BotCommand menu
- **Slack** — `slack_subcommand_map()` generates `/hermes` subcommand routing
- **Autocomplete** — `COMMANDS` flat dict feeds `SlashCommandCompleter`
- **CLI help** — `COMMANDS_BY_CATEGORY` dict feeds `show_help()`

### Adding a Slash Command

1. Add a `CommandDef` entry to `COMMAND_REGISTRY` in `hermes_cli/commands.py`:
```python
CommandDef("mycommand", "Description of what it does", "Session",
           aliases=("mc",), args_hint="[arg]"),
```
2. Add handler in `HermesCLI.process_command()` in `cli.py`:
```python
elif canonical == "mycommand":
    self._handle_mycommand(cmd_original)
```
3. If the command is available in the gateway, add a handler in `gateway/run.py`:
```python
if canonical == "mycommand":
    return await self._handle_mycommand(event)
```
4. For persistent settings, use `save_config_value()` in `cli.py`

**CommandDef fields:**
- `name` — canonical name without slash (e.g. `"background"`)
- `description` — human-readable description
- `category` — one of `"Session"`, `"Configuration"`, `"Tools & Skills"`, `"Info"`, `"Exit"`
- `aliases` — tuple of alternative names (e.g. `("bg",)`)
- `args_hint` — argument placeholder shown in help (e.g. `"<prompt>"`, `"[name]"`)
- `cli_only` — only available in the interactive CLI
- `gateway_only` — only available in messaging platforms
- `gateway_config_gate` — config dotpath (e.g. `"display.tool_progress_command"`); when set on a `cli_only` command, the command becomes available in the gateway if the config value is truthy. `GATEWAY_KNOWN_COMMANDS` always includes config-gated commands so the gateway can dispatch them; help/menus only show them when the gate is open.

**Adding an alias** requires only adding it to the `aliases` tuple on the existing `CommandDef`. No other file changes needed — dispatch, help text, Telegram menu, Slack mapping, and autocomplete all update automatically.

---
