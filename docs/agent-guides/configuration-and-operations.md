# Configuration And Operations

## Dependency Pinning Policy

All dependencies must have upper bounds to limit supply-chain attack surface.
This policy was established after the litellm compromise (PR #2796, #2810) and
reinforced after the Mini Shai-Hulud worm campaign (May 2026).

| Source type | Treatment | Example |
|---|---|---|
| PyPI package | `>=floor,<next_major` | `"httpx>=0.28.1,<1"` |
| Git URL | Commit SHA | `git+https://...@<40-char-sha>` |
| GitHub Actions | Commit SHA + comment | `uses: actions/checkout@<sha>  # v4` |
| CI-only pip | `==exact` | `pyyaml==6.0.2` |

**When adding a new dependency to `pyproject.toml`:**
1. Pin to `>=current_version,<next_major` for post-1.0 (e.g. `>=1.5.0,<2`).
2. For pre-1.0 packages, use `<0.(current_minor + 2)` (e.g. `>=0.29,<0.32`).
3. Never commit a bare `>=X.Y.Z` without a ceiling — CI and reviewers will reject it.
4. Run `uv lock` to regenerate `uv.lock` with hashes.

Reference: #2810 (bounds pass), #9801 (SHA pinning + audit CI).

---

## Adding Configuration

### config.yaml options:
1. Add to `DEFAULT_CONFIG` in `hermes_cli/config.py`
2. Bump `_config_version` (check the current value at the top of `DEFAULT_CONFIG`)
   ONLY if you need to actively migrate/transform existing user config
   (renaming keys, changing structure). Adding a new key to an existing
   section is handled automatically by the deep-merge and does NOT require
   a version bump.

### Top-level `config.yaml` sections (non-exhaustive):

`model`, `agent`, `terminal`, `compression`, `display`, `stt`, `tts`,
`memory`, `security`, `delegation`, `smart_model_routing`, `checkpoints`,
`auxiliary`, `curator`, `skills`, `gateway`, `logging`, `cron`, `profiles`,
`plugins`, `honcho`.

`auxiliary` holds per-task overrides for side-LLM work (curator, vision,
embedding, title generation, session_search, etc.) — each task can pin
its own provider/model/base_url/max_tokens/reasoning_effort. See
`agent/auxiliary_client.py::_resolve_auto` for resolution order.

`curator` holds the background skill-maintenance config —
`enabled`, `interval_hours`, `min_idle_hours`, `stale_after_days`,
`archive_after_days`, `backup` (nested).

### .env variables (SECRETS ONLY — API keys, tokens, passwords):
1. Add to `OPTIONAL_ENV_VARS` in `hermes_cli/config.py` with metadata:
```python
"NEW_API_KEY": {
    "description": "What it's for",
    "prompt": "Display name",
    "url": "https://...",
    "password": True,
    "category": "tool",  # provider, tool, messaging, setting
},
```

Non-secret settings (timeouts, thresholds, feature flags, paths, display
preferences) belong in `config.yaml`, not `.env`. If internal code needs an
env var mirror for backward compatibility, bridge it from `config.yaml` to
the env var in code (see `gateway_timeout`, `terminal.cwd` → `TERMINAL_CWD`).

### Config loaders (three paths — know which one you're in):

| Loader | Used by | Location |
|--------|---------|----------|
| `load_cli_config()` | CLI mode | `cli.py` — merges CLI-specific defaults + user YAML |
| `load_config()` | `hermes tools`, `hermes setup`, most CLI subcommands | `hermes_cli/config.py` — merges `DEFAULT_CONFIG` + user YAML |
| Direct YAML load | Gateway runtime | `gateway/run.py` + `gateway/config.py` — reads user YAML raw |

If you add a new key and the CLI sees it but the gateway doesn't (or vice
versa), you're on the wrong loader. Check `DEFAULT_CONFIG` coverage.

### Working directory:
- **CLI** — uses the process's current directory (`os.getcwd()`).
- **Messaging** — uses `terminal.cwd` from `config.yaml`. The gateway bridges this
  to the `TERMINAL_CWD` env var for child tools. **`MESSAGING_CWD` has been
  removed** — the config loader prints a deprecation warning if it's set in
  `.env`. Same for `TERMINAL_CWD` in `.env`; the canonical setting is
  `terminal.cwd` in `config.yaml`.

---

## Cron (scheduled jobs)

`cron/jobs.py` (job store) + `cron/scheduler.py` (tick loop). Agents
schedule jobs via the `cronjob` tool; users via `hermes cron <verb>`
(`list`, `add`, `edit`, `pause`, `resume`, `run`, `remove`) or the
`/cron` slash command.

Supported schedule formats:
- Duration: `"30m"`, `"2h"`, `"1d"`
- "every" phrase: `"every 2h"`, `"every monday 9am"`
- 5-field cron expression: `"0 9 * * *"`
- ISO timestamp (one-shot): `"2026-06-01T09:00:00Z"`

Per-job fields include `skills` (load specific skills), `model` /
`provider` overrides, `script` (pre-run data-collection script whose
stdout is injected into the prompt; `no_agent=True` turns the script
into the entire job), `context_from` (chain job A's last output into
job B's prompt), `workdir` (run in a specific directory with its
`AGENTS.md`/`CLAUDE.md` loaded), and multi-platform delivery.

Hardening invariants:
- **3-minute hard interrupt** on cron sessions — runaway agent loops
  cannot monopolize the scheduler.
- Catchup window: half the job's period, clamped to 120s–2h.
- Grace window: 120s for one-shot jobs whose fire time was missed.
- File lock at `~/.hermes/cron/.tick.lock` prevents duplicate ticks
  across processes.
- Cron sessions pass `skip_memory=True` by default; memory providers
  intentionally do not run during cron.

Cron deliveries are **not** mirrored into the target gateway session —
they land in their own cron session with a header/footer frame so the
main conversation's message-role alternation stays intact.

---

## Kanban (multi-agent work queue)

Durable SQLite-backed board that lets multiple profiles / workers
collaborate on shared tasks. Users drive it via `hermes kanban <verb>`;
workers spawned by the dispatcher drive it via a dedicated `kanban_*`
toolset so their schema footprint is zero when they're not inside a
kanban task.

- **CLI:** `hermes_cli/kanban.py` wires `hermes kanban` with verbs
  `init`, `create`, `list` (alias `ls`), `show`, `assign`, `link`,
  `unlink`, `comment`, `complete`, `block`, `unblock`, `archive`,
  `tail`, plus less-commonly-used `watch`, `stats`, `runs`, `log`,
  `assignees`, `heartbeat`, `notify-*`, `dispatch`, `daemon`, `gc`.
- **Worker/orchestrator toolset:** `tools/kanban_tools.py` exposes
  `kanban_show`, `kanban_complete`, `kanban_block`, `kanban_heartbeat`,
  `kanban_comment`, `kanban_create`, `kanban_link`; profiles that
  explicitly enable the `kanban` toolset outside a dispatcher-spawned
  task also get `kanban_list` and `kanban_unblock` for board routing.
- **Dispatcher:** long-lived loop that (default every 60s) reclaims
  stale claims, promotes ready tasks, atomically claims, and spawns
  assigned profiles. Runs **inside the gateway** by default via
  `kanban.dispatch_in_gateway: true`.
- **Plugin assets:** `plugins/kanban/dashboard/` (web UI) +
  `plugins/kanban/systemd/` (`hermes-kanban-dispatcher.service` for
  standalone dispatcher deployment).

Isolation model:
- **Board** is the hard boundary — workers are spawned with
  `HERMES_KANBAN_BOARD` pinned in their env so they can't see other
  boards.
- **Tenant** is a soft namespace *within* a board — one specialist
  fleet can serve multiple businesses with workspace-path + memory-key
  isolation.
- After `kanban.failure_limit` consecutive non-success attempts on the
  same task (default: 2), the dispatcher auto-blocks it to prevent spin
  loops.

Full user-facing docs: `website/docs/user-guide/features/kanban.md`.

---

## Important Policies

### Prompt Caching Must Not Break

Hermes-Agent ensures caching remains valid throughout a conversation. **Do NOT implement changes that would:**
- Alter past context mid-conversation
- Change toolsets mid-conversation
- Reload memories or rebuild system prompts mid-conversation

Cache-breaking forces dramatically higher costs. The ONLY time we alter context is during context compression.

Slash commands that mutate system-prompt state (skills, tools, memory, etc.)
must be **cache-aware**: default to deferred invalidation (change takes
effect next session), with an opt-in `--now` flag for immediate
invalidation. See `/skills install --now` for the canonical pattern.

### Background Process Notifications (Gateway)

When `terminal(background=true, notify_on_complete=true)` is used, the gateway runs a watcher that
detects process completion and triggers a new agent turn. Control verbosity of background process
messages with `display.background_process_notifications`
in config.yaml (or `HERMES_BACKGROUND_NOTIFICATIONS` env var):

- `all` — running-output updates + final message (default)
- `result` — only the final completion message
- `error` — only the final message when exit code != 0
- `off` — no watcher messages at all

---

## Profiles: Multi-Instance Support

Hermes supports **profiles** — multiple fully isolated instances, each with its own
`HERMES_HOME` directory (config, API keys, memory, sessions, skills, gateway, etc.).

The core mechanism: `_apply_profile_override()` in `hermes_cli/main.py` sets
`HERMES_HOME` before any module imports. All `get_hermes_home()` references
automatically scope to the active profile.

### Rules for profile-safe code

1. **Use `get_hermes_home()` for all HERMES_HOME paths.** Import from `hermes_constants`.
   NEVER hardcode `~/.hermes` or `Path.home() / ".hermes"` in code that reads/writes state.
   ```python
   # GOOD
   from hermes_constants import get_hermes_home
   config_path = get_hermes_home() / "config.yaml"

   # BAD — breaks profiles
   config_path = Path.home() / ".hermes" / "config.yaml"
   ```

2. **Use `display_hermes_home()` for user-facing messages.** Import from `hermes_constants`.
   This returns `~/.hermes` for default or `~/.hermes/profiles/<name>` for profiles.
   ```python
   # GOOD
   from hermes_constants import display_hermes_home
   print(f"Config saved to {display_hermes_home()}/config.yaml")

   # BAD — shows wrong path for profiles
   print("Config saved to ~/.hermes/config.yaml")
   ```

3. **Module-level constants are fine** — they cache `get_hermes_home()` at import time,
   which is AFTER `_apply_profile_override()` sets the env var. Just use `get_hermes_home()`,
   not `Path.home() / ".hermes"`.

4. **Tests that mock `Path.home()` must also set `HERMES_HOME`** — since code now uses
   `get_hermes_home()` (reads env var), not `Path.home() / ".hermes"`:
   ```python
   with patch.object(Path, "home", return_value=tmp_path), \
        patch.dict(os.environ, {"HERMES_HOME": str(tmp_path / ".hermes")}):
       ...
   ```

5. **Gateway platform adapters should use token locks** — if the adapter connects with
   a unique credential (bot token, API key), call `acquire_scoped_lock()` from
   `gateway.status` in the `connect()`/`start()` method and `release_scoped_lock()` in
   `disconnect()`/`stop()`. This prevents two profiles from using the same credential.
   See `gateway/platforms/telegram.py` for the canonical pattern.

6. **Profile operations are HOME-anchored, not HERMES_HOME-anchored** — `_get_profiles_root()`
   returns `Path.home() / ".hermes" / "profiles"`, NOT `get_hermes_home() / "profiles"`.
   This is intentional — it lets `hermes -p coder profile list` see all profiles regardless
   of which one is active.
