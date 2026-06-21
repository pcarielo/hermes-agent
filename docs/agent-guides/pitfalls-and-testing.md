# Pitfalls And Testing

## Known Pitfalls

### DO NOT hardcode `~/.hermes` paths
Use `get_hermes_home()` from `hermes_constants` for code paths. Use `display_hermes_home()`
for user-facing print/log messages. Hardcoding `~/.hermes` breaks profiles — each profile
has its own `HERMES_HOME` directory. This was the source of 5 bugs fixed in PR #3575.

### DO NOT introduce new `simple_term_menu` usage
Existing call sites in `hermes_cli/main.py` remain for legacy fallback only;
the preferred UI is curses (stdlib) because `simple_term_menu` has
ghost-duplication rendering bugs in tmux/iTerm2 with arrow keys. New
interactive menus must use `hermes_cli/curses_ui.py` — see
`hermes_cli/tools_config.py` for the canonical pattern.

### DO NOT use `\033[K` (ANSI erase-to-EOL) in spinner/display code
Leaks as literal `?[K` text under `prompt_toolkit`'s `patch_stdout`. Use space-padding: `f"\r{line}{' ' * pad}"`.

### `_last_resolved_tool_names` is a process-global in `model_tools.py`
`_run_single_child()` in `delegate_tool.py` saves and restores this global around subagent execution. If you add new code that reads this global, be aware it may be temporarily stale during child agent runs.

### DO NOT hardcode cross-tool references in schema descriptions
Tool schema descriptions must not mention tools from other toolsets by name (e.g., `browser_navigate` saying "prefer web_search"). Those tools may be unavailable (missing API keys, disabled toolset), causing the model to hallucinate calls to non-existent tools. If a cross-reference is needed, add it dynamically in `get_tool_definitions()` in `model_tools.py` — see the `browser_navigate` / `execute_code` post-processing blocks for the pattern.

### The gateway has TWO message guards — both must bypass approval/control commands
When an agent is running, messages pass through two sequential guards:
(1) **base adapter** (`gateway/platforms/base.py`) queues messages in
`_pending_messages` when `session_key in self._active_sessions`, and
(2) **gateway runner** (`gateway/run.py`) intercepts `/stop`, `/new`,
`/queue`, `/status`, `/approve`, `/deny` before they reach
`running_agent.interrupt()`. Any new command that must reach the runner
while the agent is blocked (e.g. approval prompts) MUST bypass BOTH
guards and be dispatched inline, not via `_process_message_background()`
(which races session lifecycle).

### Squash merges from stale branches silently revert recent fixes
Before squash-merging a PR, ensure the branch is up to date with `main`
(`git fetch origin main && git reset --hard origin/main` in the worktree,
then re-apply the PR's commits). A stale branch's version of an unrelated
file will silently overwrite recent fixes on main when squashed. Verify
with `git diff HEAD~1..HEAD` after merging — unexpected deletions are a
red flag.

### Don't wire in dead code without E2E validation
Unused code that was never shipped was dead for a reason. Before wiring an
unused module into a live code path, E2E test the real resolution chain
with actual imports (not mocks) against a temp `HERMES_HOME`.

### Tests must not write to `~/.hermes/`
The `_isolate_hermes_home` autouse fixture in `tests/conftest.py` redirects `HERMES_HOME` to a temp dir. Never hardcode `~/.hermes/` paths in tests.

**Profile tests**: When testing profile features, also mock `Path.home()` so that
`_get_profiles_root()` and `_get_default_hermes_home()` resolve within the temp dir.
Use the pattern from `tests/hermes_cli/test_profiles.py`:
```python
@pytest.fixture
def profile_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home
```

---

## Testing

**ALWAYS use `scripts/run_tests.sh`** — do not call `pytest` directly. The script enforces
hermetic environment parity with CI (unset credential vars, TZ=UTC, LANG=C.UTF-8,
`-n auto` xdist workers, in-tree subprocess-isolation plugin). Direct `pytest`
on a 16+ core developer machine with API keys set diverges from CI in ways
that have caused multiple "works locally, fails in CI" incidents (and the reverse).

```bash
scripts/run_tests.sh                                  # full suite, CI-parity
scripts/run_tests.sh tests/gateway/                   # one directory
scripts/run_tests.sh tests/agent/test_foo.py::test_x  # one test
scripts/run_tests.sh -v --tb=long                     # pass-through pytest flags
scripts/run_tests.sh --no-isolate tests/foo/          # disable subprocess isolation (faster, for debugging)
```

### Subprocess-per-test isolation

Every test runs in a freshly-spawned Python subprocess via the in-tree plugin
at `tests/_isolate_plugin.py`. This means module-level dicts/sets and
ContextVars from one test cannot leak into the next — the historic
`_reset_module_state` autouse fixture is gone.

Implementation notes:

- The plugin uses `multiprocessing.get_context("spawn")`, which works on
  Linux, macOS, and Windows alike (POSIX `fork` is not used).
- Per-test overhead is ~0.5–1.0s (Python startup + pytest collection). xdist
  parallelism amortizes this across cores; on a 20-core box the full suite
  finishes in roughly the same wall time as before, but flake-free.
- `isolate_timeout` (configured in `pyproject.toml`) caps each test at 30s.
  Hangs are killed and surfaced as a failure report.
- Pass `--no-isolate` to disable isolation — useful when debugging a single
  test interactively, or when you specifically want to verify state leakage.
- The plugin disables itself in child processes (sentinel envvar
  `HERMES_ISOLATE_CHILD=1`), so there's no fork-bomb risk.

### Why the wrapper (and why the old "just call pytest" doesn't work)

Five real sources of local-vs-CI drift the script closes:

| | Without wrapper | With wrapper |
|---|---|---|
| Provider API keys | Whatever is in your env (auto-detects pool) | All `*_API_KEY`/`*_TOKEN`/etc. unset |
| HOME / `~/.hermes/` | Your real config+auth.json | Temp dir per test |
| Timezone | Local TZ (PDT etc.) | UTC |
| Locale | Whatever is set | C.UTF-8 |
| xdist workers | `-n auto` = all cores | `-n auto` (safe — subprocess isolation prevents cross-worker flakes) |

`tests/conftest.py` also enforces points 1-4 as an autouse fixture so ANY pytest
invocation (including IDE integrations) gets hermetic behavior — but the wrapper
is belt-and-suspenders.

### Running without the wrapper (only if you must)

If you can't use the wrapper (e.g. inside an IDE that shells pytest directly),
at minimum activate the venv. The isolation plugin loads automatically from
`addopts` in `pyproject.toml`, so you get the same per-test process isolation
either way.

```bash
source .venv/bin/activate   # or: source venv/bin/activate
python -m pytest tests/ -q
```

If you need to bypass isolation for fast feedback while debugging:

```bash
python -m pytest tests/agent/test_foo.py -q --no-isolate
```

Always run the full suite before pushing changes.

### Don't write change-detector tests

A test is a **change-detector** if it fails whenever data that is **expected
to change** gets updated — model catalogs, config version numbers,
enumeration counts, hardcoded lists of provider models. These tests add no
behavioral coverage; they just guarantee that routine source updates break
CI and cost engineering time to "fix."

**Do not write:**

```python
# catalog snapshot — breaks every model release
assert "gemini-2.5-pro" in _PROVIDER_MODELS["gemini"]
assert "MiniMax-M2.7" in models

# config version literal — breaks every schema bump
assert DEFAULT_CONFIG["_config_version"] == 21

# enumeration count — breaks every time a skill/provider is added
assert len(_PROVIDER_MODELS["huggingface"]) == 8
```

**Do write:**

```python
# behavior: does the catalog plumbing work at all?
assert "gemini" in _PROVIDER_MODELS
assert len(_PROVIDER_MODELS["gemini"]) >= 1

# behavior: does migration bump the user's version to current latest?
assert raw["_config_version"] == DEFAULT_CONFIG["_config_version"]

# invariant: no plan-only model leaks into the legacy list
assert not (set(moonshot_models) & coding_plan_only_models)

# invariant: every model in the catalog has a context-length entry
for m in _PROVIDER_MODELS["huggingface"]:
    assert m.lower() in DEFAULT_CONTEXT_LENGTHS_LOWER
```

The rule: if the test reads like a snapshot of current data, delete it. If
it reads like a contract about how two pieces of data must relate, keep it.
When a PR adds a new provider/model and you want a test, make the test
assert the relationship (e.g. "catalog entries all have context lengths"),
not the specific names.

Reviewers should reject new change-detector tests; authors should convert
them into invariants before re-requesting review.
