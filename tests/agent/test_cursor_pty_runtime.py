"""Tests for Cursor Agent CLI PTY runtime support."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


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

    loaded = CursorPtyStateStore(path=path).load("hermes-a")

    assert loaded is not None
    assert loaded.hermes_session_id == "hermes-a"
    assert loaded.cursor_chat_id == "cursor-a"
    assert loaded.workspace == "/Users/yashielsookdeo"
    assert loaded.model == "auto"
    assert loaded.cursor_cli_version == "2026.06.15-03-48-54-da23e37"


def test_cursor_pty_state_store_ignores_corrupt_json(tmp_path):
    from agent.transports.cursor_pty_session import CursorPtyStateStore

    path = tmp_path / "cursor_pty_sessions.json"
    path.write_text("{not-json", encoding="utf-8")

    assert CursorPtyStateStore(path=path).load("hermes-a") is None


def test_cursor_pty_session_reuses_transport_for_same_hermes_session(tmp_path):
    from agent.transports.cursor_pty import CursorPtyResult
    from agent.transports.cursor_pty_session import CursorPtySession, CursorPtyStateStore

    created = []

    class FakeTransport:
        cursor_cli_version = "2026.06.15-03-48-54-da23e37"

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.prompts = []
            created.append(self)

        def run_turn(self, prompt: str):
            self.prompts.append(prompt)
            return CursorPtyResult(final_text=f"ok:{prompt}", cursor_chat_id="cursor-a")

        def close(self):
            pass

    store = CursorPtyStateStore(path=tmp_path / "state.json")
    session = CursorPtySession(
        hermes_session_id="hermes-a",
        workspace=str(tmp_path),
        model="auto",
        state_store=store,
        transport_factory=FakeTransport,
    )

    assert session.run_turn("one").final_text == "ok:one"
    assert session.run_turn("two").final_text == "ok:two"

    assert len(created) == 1
    assert created[0].prompts == ["one", "two"]
    assert store.load("hermes-a").cursor_chat_id == "cursor-a"


def test_cursor_pty_session_isolates_different_hermes_sessions(tmp_path):
    from agent.transports.cursor_pty import CursorPtyResult
    from agent.transports.cursor_pty_session import CursorPtySession, CursorPtyStateStore

    created = []

    class FakeTransport:
        cursor_cli_version = "2026.06.15-03-48-54-da23e37"

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            created.append(self)

        def run_turn(self, prompt: str):
            return CursorPtyResult(final_text="ok", cursor_chat_id=f"cursor-{prompt}")

    store = CursorPtyStateStore(path=tmp_path / "state.json")

    CursorPtySession(
        hermes_session_id="hermes-a",
        workspace=str(tmp_path),
        state_store=store,
        transport_factory=FakeTransport,
    ).run_turn("a")
    CursorPtySession(
        hermes_session_id="hermes-b",
        workspace=str(tmp_path),
        state_store=store,
        transport_factory=FakeTransport,
    ).run_turn("b")

    assert len(created) == 2
    assert store.load("hermes-a").cursor_chat_id == "cursor-a"
    assert store.load("hermes-b").cursor_chat_id == "cursor-b"


def test_cursor_pty_session_passes_matching_persisted_chat_id(tmp_path):
    from agent.transports.cursor_pty import CursorPtyResult
    from agent.transports.cursor_pty_session import CursorPtySession, CursorPtyStateStore

    captured = {}

    class FakeTransport:
        cursor_cli_version = "2026.06.15-03-48-54-da23e37"

        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run_turn(self, prompt: str):
            return CursorPtyResult(final_text="ok", cursor_chat_id="cursor-existing")

    store = CursorPtyStateStore(path=tmp_path / "state.json")
    store.save(
        "hermes-a",
        cursor_chat_id="cursor-existing",
        workspace=str(tmp_path),
        model="auto",
        cursor_cli_version="2026.06.15-03-48-54-da23e37",
    )

    CursorPtySession(
        hermes_session_id="hermes-a",
        workspace=str(tmp_path),
        model="auto",
        state_store=store,
        transport_factory=FakeTransport,
    ).run_turn("resume")

    assert captured["cursor_chat_id"] == "cursor-existing"


def test_cursor_pty_session_ignores_persisted_chat_id_for_other_workspace(tmp_path):
    from agent.transports.cursor_pty import CursorPtyResult
    from agent.transports.cursor_pty_session import CursorPtySession, CursorPtyStateStore

    captured = {}
    other_workspace = tmp_path / "other"
    other_workspace.mkdir()

    class FakeTransport:
        cursor_cli_version = "2026.06.15-03-48-54-da23e37"

        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run_turn(self, prompt: str):
            return CursorPtyResult(final_text="ok", cursor_chat_id="cursor-new")

    store = CursorPtyStateStore(path=tmp_path / "state.json")
    store.save(
        "hermes-a",
        cursor_chat_id="cursor-existing",
        workspace=str(other_workspace),
        model="auto",
        cursor_cli_version="2026.06.15-03-48-54-da23e37",
    )

    CursorPtySession(
        hermes_session_id="hermes-a",
        workspace=str(tmp_path),
        model="auto",
        state_store=store,
        transport_factory=FakeTransport,
    ).run_turn("resume")

    assert captured["cursor_chat_id"] is None


def test_cursor_pty_marker_generation_avoids_user_text(monkeypatch):
    from agent.transports import cursor_pty
    from agent.transports.cursor_pty import build_prompt_with_marker

    class FakeUuid:
        def __init__(self, value: str) -> None:
            self._value = value

        def __str__(self) -> str:
            return self._value

    uuids = iter([FakeUuid("collision"), FakeUuid("unique")])
    monkeypatch.setattr(cursor_pty.uuid, "uuid4", lambda: next(uuids))

    framed, marker = build_prompt_with_marker(
        "The literal marker HERMES_CURSOR_DONE_collision appears here."
    )

    assert marker == "HERMES_CURSOR_DONE_unique"
    assert marker not in framed
    assert "HERMES_CURSOR_DONE_" in framed
    assert "unique" in framed


def test_cursor_pty_command_never_uses_continue(tmp_path):
    from agent.transports.cursor_pty import CursorPtyTransport

    transport = CursorPtyTransport(
        workspace=str(tmp_path),
        model="auto",
        cursor_chat_id="cursor-chat-1",
        version_checker=lambda cursor_bin: "2026.06.15-03-48-54-da23e37",
    )

    cmd = transport.build_command()

    assert cmd[:3] == ["agent", "--workspace", str(tmp_path)]
    assert "--resume" in cmd
    assert "cursor-chat-1" in cmd
    assert "--continue" not in cmd
    assert "-p" not in cmd


def test_cursor_pty_extracts_final_text_from_last_marker():
    from agent.transports.cursor_pty import extract_response_until_marker

    marker = "HERMES_CURSOR_DONE_abc"
    raw_output = (
        "\x1b[?25lWelcome to Cursor Agent\n"
        "● Please end with HERMES_CURSOR_DONE_abc\n"
        "Thinking...\n\n"
        "final line one\n"
        "final line two\n"
        "HERMES_CURSOR_DONE_abc\n"
        "● next prompt"
    )

    assert extract_response_until_marker(raw_output, marker) == (
        "final line one\nfinal line two"
    )


def test_cursor_pty_creates_chat_and_runs_print_json_resume(tmp_path):
    from agent.transports.cursor_pty import CursorPtyTransport

    runner_calls = []

    def runner(cmd, **kwargs):
        runner_calls.append((cmd, kwargs))
        if cmd == ["agent", "create-chat"]:
            return SimpleNamespace(
                returncode=0,
                stdout="cursor-chat-created\n",
                stderr="",
            )
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": "answer",
                    "session_id": "cursor-chat-created",
                    "usage": {
                        "inputTokens": 2,
                        "outputTokens": 3,
                        "cacheReadTokens": 4,
                        "cacheWriteTokens": 5,
                    },
                }
            ),
            stderr="",
        )

    def spawn_factory(cmd):
        raise AssertionError("print-json runtime should not spawn a PTY process")

    transport = CursorPtyTransport(
        workspace=str(tmp_path),
        version_checker=lambda cursor_bin: "2026.06.15-03-48-54-da23e37",
        runner=runner,
        spawn_factory=spawn_factory,
        model="auto",
    )

    result = transport.run_turn("hello")

    assert result.final_text == "answer"
    assert result.cursor_chat_id == "cursor-chat-created"
    assert runner_calls[0][0] == ["agent", "create-chat"]
    assert runner_calls[1][0] == [
        "agent",
        "-p",
        "--output-format",
        "json",
        "--workspace",
        str(tmp_path),
        "--trust",
        "--model",
        "auto",
        "--resume",
        "cursor-chat-created",
        "hello",
    ]
    assert result.usage == {
        "input_tokens": 2,
        "output_tokens": 3,
        "cache_read_tokens": 4,
        "cache_write_tokens": 5,
        "total_tokens": 14,
    }


def test_cursor_pty_uses_existing_chat_id_without_create_chat(tmp_path):
    from agent.transports.cursor_pty import CursorPtyTransport

    runner_calls = []

    def runner(cmd, **kwargs):
        runner_calls.append((cmd, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": "answer",
                    "session_id": "cursor-existing",
                    "usage": {},
                }
            ),
            stderr="",
        )

    def spawn_factory(cmd):
        raise AssertionError("print-json runtime should not spawn a PTY process")

    transport = CursorPtyTransport(
        workspace=str(tmp_path),
        cursor_chat_id="cursor-existing",
        version_checker=lambda cursor_bin: "2026.06.15-03-48-54-da23e37",
        runner=runner,
        spawn_factory=spawn_factory,
    )

    result = transport.run_turn("hello")

    assert result.cursor_chat_id == "cursor-existing"
    assert len(runner_calls) == 1
    assert runner_calls[0][0][-2:] == ["cursor-existing", "hello"]


def test_cursor_pty_parses_about_version():
    from agent.transports.cursor_pty import parse_cursor_cli_version

    about_output = (
        "Cursor Agent CLI\n"
        "Version             0.50.5\n"
        "CLI Version         2026.06.15-03-48-54-da23e37\n"
    )

    assert parse_cursor_cli_version(about_output) == "2026.06.15-03-48-54-da23e37"


def test_cursor_pty_rejects_unpinned_cli_version(monkeypatch):
    from agent.transports.cursor_pty import CursorPtyError, check_cursor_cli_version

    def runner(cmd, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout="CLI Version         2099.01.01-bad\n",
            stderr="",
        )

    monkeypatch.delenv("HERMES_CURSOR_PTY_ALLOW_UNPINNED", raising=False)

    with pytest.raises(CursorPtyError, match="unsupported Cursor CLI version"):
        check_cursor_cli_version("agent", runner=runner)


def test_cursor_pty_allows_unpinned_cli_version_with_escape_hatch(monkeypatch):
    from agent.transports.cursor_pty import check_cursor_cli_version

    def runner(cmd, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout="CLI Version         2099.01.01-bad\n",
            stderr="",
        )

    monkeypatch.setenv("HERMES_CURSOR_PTY_ALLOW_UNPINNED", "1")

    assert check_cursor_cli_version("agent", runner=runner) == "2099.01.01-bad"


def test_run_cursor_pty_turn_appends_projected_messages():
    from agent.codex_runtime import run_cursor_pty_turn

    fake_result = SimpleNamespace(
        final_text="done",
        cursor_chat_id="cursor-chat-1",
        usage={
            "input_tokens": 2,
            "output_tokens": 3,
            "cache_read_tokens": 4,
            "cache_write_tokens": 5,
            "total_tokens": 14,
        },
    )
    fake_session = SimpleNamespace(run_turn=lambda user_input: fake_result)
    agent = SimpleNamespace(
        _cursor_pty_session=fake_session,
        session_prompt_tokens=0,
        session_completion_tokens=0,
        session_total_tokens=0,
        session_input_tokens=0,
        session_output_tokens=0,
        session_cache_read_tokens=0,
        session_cache_write_tokens=0,
        session_reasoning_tokens=0,
        session_api_calls=0,
        session_estimated_cost_usd=0.0,
        session_cost_status=None,
        session_cost_source=None,
        _session_db=None,
        session_id="hermes-a",
        _session_db_created=False,
        model="cursor",
        provider="cursor",
        base_url="",
        api_key="",
    )
    messages = [{"role": "user", "content": "work"}]

    out = run_cursor_pty_turn(
        agent,
        user_message="work",
        original_user_message="work",
        messages=messages,
        effective_task_id="t",
    )

    assert out["completed"] is True
    assert out["partial"] is False
    assert out["final_response"] == "done"
    assert messages[-1] == {"role": "assistant", "content": "done"}
    assert agent.session_api_calls == 1
    assert agent.session_prompt_tokens == 11
    assert agent.session_completion_tokens == 3
    assert agent.session_total_tokens == 14
    assert out["cursor_chat_id"] == "cursor-chat-1"


def test_run_cursor_pty_turn_creates_session_with_hermes_session_id(monkeypatch, tmp_path):
    from agent.codex_runtime import run_cursor_pty_turn
    from agent.transports import cursor_pty_session as cursor_session_module

    captured = {}

    class FakeCursorPtySession:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run_turn(self, prompt: str):
            return SimpleNamespace(
                final_text="done",
                cursor_chat_id="cursor-chat-1",
                usage={},
            )

    monkeypatch.setenv("HERMES_CURSOR_MODEL", "auto")
    monkeypatch.setattr(
        cursor_session_module,
        "CursorPtySession",
        FakeCursorPtySession,
    )
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

    run_cursor_pty_turn(
        agent,
        user_message="work",
        original_user_message="work",
        messages=[],
        effective_task_id="t",
    )

    assert captured["hermes_session_id"] == "hermes-a"
    assert captured["workspace"] == str(tmp_path)
    assert captured["model"] == "auto"


def test_run_cursor_pty_turn_uses_cursor_pty_model_fallback(monkeypatch, tmp_path):
    from agent.codex_runtime import run_cursor_pty_turn
    from agent.transports import cursor_pty_session as cursor_session_module

    captured = {}

    class FakeCursorPtySession:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run_turn(self, prompt: str):
            return SimpleNamespace(
                final_text="done",
                cursor_chat_id="cursor-chat-1",
                usage={},
            )

    monkeypatch.delenv("HERMES_CURSOR_MODEL", raising=False)
    monkeypatch.setattr(
        cursor_session_module,
        "CursorPtySession",
        FakeCursorPtySession,
    )
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"model": {"cursor_pty_model": "gpt-5.3-codex-low"}},
    )
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

    run_cursor_pty_turn(
        agent,
        user_message="work",
        original_user_message="work",
        messages=[],
        effective_task_id="t",
    )

    assert captured["model"] == "gpt-5.3-codex-low"


def test_run_cursor_pty_turn_prefers_agent_cursor_model_override(monkeypatch, tmp_path):
    from agent.codex_runtime import run_cursor_pty_turn
    from agent.transports import cursor_pty_session as cursor_session_module

    captured = {}

    class FakeCursorPtySession:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run_turn(self, prompt: str):
            return SimpleNamespace(
                final_text="done",
                cursor_chat_id="cursor-chat-1",
                usage={},
            )

    monkeypatch.delenv("HERMES_CURSOR_MODEL", raising=False)
    monkeypatch.setattr(
        cursor_session_module,
        "CursorPtySession",
        FakeCursorPtySession,
    )
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"model": {"cursor_model": "auto"}},
    )
    agent = SimpleNamespace(
        _cursor_pty_session=None,
        _cursor_model_override="gpt-5.3-codex-low",
        session_cwd=str(tmp_path),
        session_id="hermes-a",
        session_api_calls=0,
        _iters_since_skill=0,
        _skill_nudge_interval=0,
        valid_tool_names=set(),
        _session_db=None,
    )

    run_cursor_pty_turn(
        agent,
        user_message="work",
        original_user_message="work",
        messages=[],
        effective_task_id="t",
    )

    assert captured["model"] == "gpt-5.3-codex-low"
