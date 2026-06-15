# Cursor PTY Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `cursor_pty`, an experimental per-Hermes-session persistent Cursor Agent CLI runtime with strict Cursor chat isolation.

**Architecture:** The runtime adds a state store keyed by Hermes `session_id`, a PTY transport that runs one interactive Cursor CLI process, a session adapter that owns one PTY per Hermes session, and runtime/display wiring alongside the existing `cursor_headless` path. The implementation keeps `cursor_headless` untouched as the stable fallback.

**Tech Stack:** Python stdlib (`pty`, `selectors`, `subprocess`, `json`, `tempfile`, `os`, `time`, `uuid`), existing Hermes runtime modules, pytest via `scripts/run_tests.sh`.

---

## File Structure

- Create `agent/transports/cursor_pty.py`: PTY process transport, ANSI stripping, prompt marker generation, Cursor CLI version pinning.
- Create `agent/transports/cursor_pty_session.py`: state store and per-Hermes-session lifecycle wrapper.
- Modify `agent/codex_runtime.py`: add `run_cursor_pty_turn`, reuse Cursor model/workspace helpers and usage accounting.
- Modify `agent/conversation_loop.py`: route `api_mode == "cursor_pty"`.
- Modify `run_agent.py`: add `_run_cursor_pty_turn` forwarder.
- Modify `agent/agent_init.py`: accept `cursor_pty` as an API mode.
- Modify `hermes_cli/codex_runtime_switch.py`: parse/select/persist `cursor_pty`.
- Modify `hermes_cli/runtime_provider.py`: resolve `cursor_pty` from `model.agent_runtime`.
- Modify `hermes_cli/agent_runtime_display.py`: render `cursor:pty`.
- Modify `hermes_cli/banner.py` and `cli.py`: display the runtime through existing display helpers.
- Create `tests/agent/test_cursor_pty_runtime.py`: state store, transport, session, runtime hook tests.
- Modify `tests/hermes_cli/test_agent_runtime_switch.py`, `tests/hermes_cli/test_cursor_runtime_provider.py`, and `tests/hermes_cli/test_cursor_runtime_display.py`: selection/display coverage.
- Optionally create `tests/run_agent/test_cursor_pty_integration.py`: forwarder coverage.

---

### Task 1: Runtime State Store

**Files:**
- Create: `agent/transports/cursor_pty_session.py`
- Test: `tests/agent/test_cursor_pty_runtime.py`

- [ ] **Step 1: Write failing state store tests**

Add tests:

```python
def test_cursor_pty_state_store_round_trips_mapping(tmp_path):
    from agent.transports.cursor_pty_session import CursorPtyStateStore

    path = tmp_path / "cursor_pty_sessions.json"
    store = CursorPtyStateStore(path=path)

    store.save(
        "hermes-a",
        cursor_chat_id="cursor-a",
        workspace="/Users/yashielsookdeo",
        model="auto",
        cursor_cli_version="2026.06.15-03-48-54-da23e37",
    )

    assert CursorPtyStateStore(path=path).load("hermes-a").cursor_chat_id == "cursor-a"


def test_cursor_pty_state_store_ignores_corrupt_json(tmp_path):
    from agent.transports.cursor_pty_session import CursorPtyStateStore

    path = tmp_path / "cursor_pty_sessions.json"
    path.write_text("{not-json", encoding="utf-8")

    assert CursorPtyStateStore(path=path).load("hermes-a") is None
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
scripts/run_tests.sh tests/agent/test_cursor_pty_runtime.py
```

Expected: import failure for `agent.transports.cursor_pty_session`.

- [ ] **Step 3: Implement state store**

Create `CursorPtyState` and `CursorPtyStateStore`:

```python
@dataclass
class CursorPtyState:
    hermes_session_id: str
    cursor_chat_id: str
    workspace: str
    model: str | None
    cursor_cli_version: str
    updated_at: str


class CursorPtyStateStore:
    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self._path = Path(path) if path is not None else _default_state_path()

    def load(self, hermes_session_id: str | None) -> CursorPtyState | None:
        if not hermes_session_id:
            return None
        data = self._read()
        item = data.get(hermes_session_id)
        if not isinstance(item, dict):
            return None
        cursor_chat_id = item.get("cursor_chat_id")
        if not isinstance(cursor_chat_id, str) or not cursor_chat_id:
            return None
        return CursorPtyState(
            hermes_session_id=hermes_session_id,
            cursor_chat_id=cursor_chat_id,
            workspace=str(item.get("workspace") or ""),
            model=item.get("model") if isinstance(item.get("model"), str) else None,
            cursor_cli_version=str(item.get("cursor_cli_version") or ""),
            updated_at=str(item.get("updated_at") or ""),
        )

    def save(self, hermes_session_id: str, *, cursor_chat_id: str, workspace: str, model: str | None, cursor_cli_version: str) -> None:
        data = self._read()
        data[hermes_session_id] = {
            "cursor_chat_id": cursor_chat_id,
            "workspace": workspace,
            "model": model,
            "cursor_cli_version": cursor_cli_version,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._write(data)
```

- [ ] **Step 4: Run tests**

Run:

```bash
scripts/run_tests.sh tests/agent/test_cursor_pty_runtime.py
```

Expected: state store tests pass.

- [ ] **Step 5: Commit**

```bash
git add agent/transports/cursor_pty_session.py tests/agent/test_cursor_pty_runtime.py
git commit -m "feat(cursor): add PTY session state store"
```

---

### Task 2: PTY Transport and Parser

**Files:**
- Create: `agent/transports/cursor_pty.py`
- Modify: `tests/agent/test_cursor_pty_runtime.py`

- [ ] **Step 1: Write failing transport tests**

Add tests:

```python
def test_cursor_pty_marker_generation_avoids_user_text():
    from agent.transports.cursor_pty import build_prompt_with_marker

    framed, marker = build_prompt_with_marker("hello")

    assert "hello" in framed
    assert marker.startswith("HERMES_CURSOR_TURN_DONE:")
    assert marker in framed


def test_cursor_pty_command_never_uses_continue(tmp_path):
    from agent.transports.cursor_pty import CursorPtyTransport

    transport = CursorPtyTransport(
        workspace=str(tmp_path),
        model="auto",
        cursor_chat_id="cursor-a",
        spawn_factory=lambda *a, **k: None,
        version_checker=lambda: "2026.06.15-03-48-54-da23e37",
    )

    cmd = transport.build_command()

    assert "--resume" in cmd
    assert "cursor-a" in cmd
    assert "--continue" not in cmd


def test_cursor_pty_extracts_final_text_from_marker():
    from agent.transports.cursor_pty import extract_response_until_marker

    text = "Cursor intro\nactual answer\nHERMES_CURSOR_TURN_DONE:abc\nprompt tail"

    assert extract_response_until_marker(text, "HERMES_CURSOR_TURN_DONE:abc") == "actual answer"
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
scripts/run_tests.sh tests/agent/test_cursor_pty_runtime.py
```

Expected: import failure for `agent.transports.cursor_pty`.

- [ ] **Step 3: Implement transport skeleton**

Create:

```python
PINNED_CURSOR_CLI_VERSION = "2026.06.15-03-48-54-da23e37"


class CursorPtyError(RuntimeError):
    pass


@dataclass
class CursorPtyResult:
    final_text: str
    cursor_chat_id: str | None = None
    usage: dict[str, int] = field(default_factory=dict)


def build_prompt_with_marker(user_text: str) -> tuple[str, str]:
    while True:
        marker = f"HERMES_CURSOR_TURN_DONE:{uuid.uuid4()}"
        if marker not in user_text:
            break
    framed = (
        f"{user_text.rstrip()}\n\n"
        "When you are completely finished, print exactly this line on its own:\n"
        f"{marker}\n"
    )
    return framed, marker
```

- [ ] **Step 4: Implement ANSI stripping and marker extraction**

Add:

```python
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def strip_terminal_control(text: str) -> str:
    text = ANSI_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


def extract_response_until_marker(output: str, marker: str) -> str:
    cleaned = strip_terminal_control(output)
    before, _, _ = cleaned.partition(marker)
    lines = [line.rstrip() for line in before.splitlines()]
    useful = [
        line for line in lines
        if line.strip()
        and not line.strip().startswith(("Welcome to Cursor", "Available Tools", "Available Skills"))
        and not line.strip().startswith((">", "❯", "●"))
    ]
    return "\n".join(useful).strip()
```

- [ ] **Step 5: Implement command building and version check**

Add `CursorPtyTransport.build_command()`:

```python
def build_command(self) -> list[str]:
    cmd = [self._cursor_bin, "--workspace", self._workspace]
    if self._model:
        cmd.extend(["--model", self._model])
    if self._cursor_chat_id:
        cmd.extend(["--resume", self._cursor_chat_id])
    return cmd
```

Add a `check_cursor_cli_version()` helper that runs `agent about`, parses `CLI Version`, and enforces the pin unless `HERMES_CURSOR_PTY_ALLOW_UNPINNED=1`.

- [ ] **Step 6: Run transport tests**

Run:

```bash
scripts/run_tests.sh tests/agent/test_cursor_pty_runtime.py
```

Expected: transport tests pass.

- [ ] **Step 7: Commit**

```bash
git add agent/transports/cursor_pty.py tests/agent/test_cursor_pty_runtime.py
git commit -m "feat(cursor): add PTY transport primitives"
```

---

### Task 3: Session Adapter

**Files:**
- Modify: `agent/transports/cursor_pty_session.py`
- Modify: `tests/agent/test_cursor_pty_runtime.py`

- [ ] **Step 1: Write failing session tests**

Add tests:

```python
class FakeCursorPtyTransport:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False
        FakeCursorPtyTransport.instances.append(self)

    def run_turn(self, prompt):
        return SimpleNamespace(final_text=f"answer:{prompt}", cursor_chat_id=self.kwargs.get("cursor_chat_id") or "cursor-new", usage={})

    def close(self):
        self.closed = True


def test_cursor_pty_session_reuses_transport_for_same_hermes_session(tmp_path):
    from agent.transports.cursor_pty_session import CursorPtySession, CursorPtyStateStore

    FakeCursorPtyTransport.instances.clear()
    session = CursorPtySession(
        hermes_session_id="hermes-a",
        workspace=str(tmp_path),
        model="auto",
        state_store=CursorPtyStateStore(path=tmp_path / "state.json"),
        transport_factory=FakeCursorPtyTransport,
    )

    session.run_turn("one")
    session.run_turn("two")

    assert len(FakeCursorPtyTransport.instances) == 1


def test_cursor_pty_sessions_are_isolated_by_hermes_session_id(tmp_path):
    from agent.transports.cursor_pty_session import CursorPtySession, CursorPtyStateStore

    FakeCursorPtyTransport.instances.clear()
    store = CursorPtyStateStore(path=tmp_path / "state.json")
    CursorPtySession(hermes_session_id="a", workspace=str(tmp_path), model="auto", state_store=store, transport_factory=FakeCursorPtyTransport).run_turn("one")
    CursorPtySession(hermes_session_id="b", workspace=str(tmp_path), model="auto", state_store=store, transport_factory=FakeCursorPtyTransport).run_turn("one")

    assert len(FakeCursorPtyTransport.instances) == 2
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
scripts/run_tests.sh tests/agent/test_cursor_pty_runtime.py
```

Expected: missing `CursorPtySession`.

- [ ] **Step 3: Implement `CursorPtySession`**

Add:

```python
class CursorPtySession:
    def __init__(self, *, hermes_session_id: str | None, workspace: str, model: str | None = None, cursor_bin: str = "agent", state_store: CursorPtyStateStore | None = None, transport_factory: Callable = CursorPtyTransport) -> None:
        self._hermes_session_id = hermes_session_id or ""
        self._workspace = workspace
        self._model = model
        self._cursor_bin = cursor_bin
        self._state_store = state_store or CursorPtyStateStore()
        self._transport_factory = transport_factory
        self._transport = None

    def run_turn(self, prompt: str) -> CursorPtyResult:
        transport = self._ensure_transport()
        result = transport.run_turn(prompt)
        if self._hermes_session_id and result.cursor_chat_id:
            self._state_store.save(
                self._hermes_session_id,
                cursor_chat_id=result.cursor_chat_id,
                workspace=self._workspace,
                model=self._model,
                cursor_cli_version=PINNED_CURSOR_CLI_VERSION,
            )
        return result
```

- [ ] **Step 4: Run session tests**

Run:

```bash
scripts/run_tests.sh tests/agent/test_cursor_pty_runtime.py
```

Expected: state, transport, and session tests pass.

- [ ] **Step 5: Commit**

```bash
git add agent/transports/cursor_pty_session.py tests/agent/test_cursor_pty_runtime.py
git commit -m "feat(cursor): add per-session PTY adapter"
```

---

### Task 4: Runtime Hook

**Files:**
- Modify: `agent/codex_runtime.py`
- Modify: `agent/conversation_loop.py`
- Modify: `run_agent.py`
- Test: `tests/agent/test_cursor_pty_runtime.py`
- Test: `tests/run_agent/test_cursor_pty_integration.py`

- [ ] **Step 1: Write failing runtime hook test**

Add:

```python
def test_run_cursor_pty_turn_appends_projected_messages(monkeypatch, tmp_path):
    from agent.codex_runtime import run_cursor_pty_turn
    from agent.transports import cursor_pty_session as cursor_session_mod

    class FakeSession:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def run_turn(self, prompt):
            return SimpleNamespace(final_text="pty done", cursor_chat_id="cursor-a", usage={})

    monkeypatch.setattr(cursor_session_mod, "CursorPtySession", FakeSession)
    agent = SimpleNamespace(
        _cursor_pty_session=None,
        session_cwd=str(tmp_path),
        session_id="hermes-a",
        session_api_calls=0,
        _iters_since_skill=0,
        _skill_nudge_interval=0,
        valid_tool_names=set(),
        _session_db=None,
    )
    messages = [{"role": "user", "content": "work"}]

    result = run_cursor_pty_turn(
        agent,
        user_message="work",
        original_user_message="work",
        messages=messages,
        effective_task_id="t",
    )

    assert result["completed"] is True
    assert result["final_response"] == "pty done"
    assert messages[-1] == {"role": "assistant", "content": "pty done"}
```

- [ ] **Step 2: Run test to verify failure**

Run:

```bash
scripts/run_tests.sh tests/agent/test_cursor_pty_runtime.py
```

Expected: missing `run_cursor_pty_turn`.

- [ ] **Step 3: Implement runtime hook**

Add `run_cursor_pty_turn` beside `run_cursor_headless_turn`. It should lazily create `agent._cursor_pty_session`, call `run_turn`, append `final_text`, call `_record_cursor_headless_usage`, and return the standard dict with `cursor_session_id`.

- [ ] **Step 4: Add conversation-loop and `run_agent.py` forwarders**

Add an early branch:

```python
if agent.api_mode == "cursor_pty":
    return agent._run_cursor_pty_turn(
        user_message=user_message,
        original_user_message=original_user_message,
        messages=messages,
        effective_task_id=effective_task_id,
        should_review_memory=_should_review_memory,
    )
```

Add `AIAgent._run_cursor_pty_turn` forwarder in `run_agent.py`.

- [ ] **Step 5: Run runtime tests**

Run:

```bash
scripts/run_tests.sh tests/agent/test_cursor_pty_runtime.py tests/run_agent/test_cursor_pty_integration.py
```

Expected: runtime hook and forwarder tests pass.

- [ ] **Step 6: Commit**

```bash
git add agent/codex_runtime.py agent/conversation_loop.py run_agent.py tests/agent/test_cursor_pty_runtime.py tests/run_agent/test_cursor_pty_integration.py
git commit -m "feat(cursor): wire PTY runtime turn path"
```

---

### Task 5: CLI Runtime Selection and Display

**Files:**
- Modify: `agent/agent_init.py`
- Modify: `hermes_cli/codex_runtime_switch.py`
- Modify: `hermes_cli/runtime_provider.py`
- Modify: `hermes_cli/agent_runtime_display.py`
- Modify: `tests/hermes_cli/test_agent_runtime_switch.py`
- Modify: `tests/hermes_cli/test_cursor_runtime_provider.py`
- Modify: `tests/hermes_cli/test_cursor_runtime_display.py`

- [ ] **Step 1: Write failing CLI/display tests**

Add:

```python
def test_parse_cursor_pty_runtime_aliases():
    assert runtime_switch.parse_args("cursor_pty") == ("cursor_pty", [])
    assert runtime_switch.parse_args("cursor-pty") == ("cursor_pty", [])
```

Add provider/display assertions:

```python
assert _maybe_apply_codex_app_server_runtime(
    provider="custom",
    api_mode="chat_completions",
    model_cfg={"agent_runtime": "cursor_pty"},
) == "cursor_pty"
```

```python
ui.agent = SimpleNamespace(
    model="gpt-oss-120b",
    api_mode="cursor_pty",
    context_compressor=None,
    session_input_tokens=0,
    session_output_tokens=0,
    session_cache_read_tokens=0,
    session_cache_write_tokens=0,
    session_prompt_tokens=0,
    session_completion_tokens=0,
    session_total_tokens=0,
    session_api_calls=0,
)
assert snapshot["model_name"] == "cursor:pty:auto"
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
scripts/run_tests.sh tests/hermes_cli/test_agent_runtime_switch.py tests/hermes_cli/test_cursor_runtime_provider.py tests/hermes_cli/test_cursor_runtime_display.py
```

Expected: runtime rejected or displayed as the default model.

- [ ] **Step 3: Implement runtime selection**

Add `cursor_pty` to `VALID_RUNTIMES`, parse aliases `cursor_pty` and `cursor-pty`, persist it through `model.agent_runtime`, and check `agent --version` with the Cursor binary check.

- [ ] **Step 4: Implement display**

Update helpers so:

```python
active_model_display_label(
    "openai/gpt-oss-120b",
    api_mode="cursor_pty",
    config={"HERMES_CURSOR_MODEL": "auto"},
) == "cursor:pty:auto"
active_provider_display_label(
    "custom",
    api_mode="cursor_pty",
    config={"HERMES_CURSOR_MODEL": "auto"},
) == "Cursor"
active_runtime_summary(config_with_cursor_pty) == "Cursor PTY (model: auto)"
```

- [ ] **Step 5: Run CLI/display tests**

Run:

```bash
scripts/run_tests.sh tests/hermes_cli/test_agent_runtime_switch.py tests/hermes_cli/test_cursor_runtime_provider.py tests/hermes_cli/test_cursor_runtime_display.py
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```bash
git add agent/agent_init.py hermes_cli/codex_runtime_switch.py hermes_cli/runtime_provider.py hermes_cli/agent_runtime_display.py tests/hermes_cli/test_agent_runtime_switch.py tests/hermes_cli/test_cursor_runtime_provider.py tests/hermes_cli/test_cursor_runtime_display.py
git commit -m "feat(cursor): expose PTY runtime selector"
```

---

### Task 6: Verification and Live Smoke

**Files:**
- Modify test files only if verification exposes an implementation mismatch.

- [ ] **Step 1: Run focused default tests**

Run:

```bash
scripts/run_tests.sh tests/agent/test_cursor_pty_runtime.py tests/agent/test_cursor_headless_runtime.py tests/run_agent/test_cursor_headless_integration.py tests/run_agent/test_cursor_pty_integration.py tests/hermes_cli/test_agent_runtime_switch.py tests/hermes_cli/test_codex_runtime_switch.py tests/hermes_cli/test_cursor_runtime_provider.py tests/hermes_cli/test_cursor_runtime_display.py tests/agent/test_usage_pricing.py
```

Expected: all tests pass.

- [ ] **Step 2: Run optional live smoke only when enabled**

Run only if local Cursor login is valid and `HERMES_CURSOR_PTY_LIVE=1` is set:

```bash
HERMES_CURSOR_PTY_LIVE=1 scripts/run_tests.sh tests/agent/test_cursor_pty_runtime.py::test_cursor_pty_live_two_turn_smoke
```

Expected: either skipped when the env var is absent or pass when enabled.

- [ ] **Step 3: Check git status**

Run:

```bash
git status --short --branch
```

Expected: clean except intentional commits already made.

- [ ] **Step 4: Final commit if verification fixes were needed**

```bash
git status --short
git add agent/transports/cursor_pty.py agent/transports/cursor_pty_session.py agent/codex_runtime.py agent/conversation_loop.py run_agent.py agent/agent_init.py hermes_cli/codex_runtime_switch.py hermes_cli/runtime_provider.py hermes_cli/agent_runtime_display.py tests/agent/test_cursor_pty_runtime.py tests/run_agent/test_cursor_pty_integration.py tests/hermes_cli/test_agent_runtime_switch.py tests/hermes_cli/test_cursor_runtime_provider.py tests/hermes_cli/test_cursor_runtime_display.py
git commit -m "test(cursor): verify PTY runtime"
```

---

## Self-Review

- Spec coverage: the plan covers per-session isolation, no `--continue`, state mapping, version pinning, PTY parsing, optional tmux debug mode, runtime hook, CLI/display, and fake/live testing.
- Open-marker scan: this plan contains no unfinished work markers.
- Type consistency: runtime name is `cursor_pty`; transport class is `CursorPtyTransport`; session class is `CursorPtySession`; state classes are `CursorPtyState` and `CursorPtyStateStore`; turn hook is `run_cursor_pty_turn`.
