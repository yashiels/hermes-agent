"""Tests for Cursor Agent CLI headless runtime support."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from agent.transports.cursor_headless import (
    CursorHeadlessError,
    CursorHeadlessSession,
)


class FakeRunner:
    def __init__(self, *, stdout: str | None = None, stderr: str = "", returncode: int = 0):
        self.calls = []
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        stdout = self.stdout
        if stdout is None:
            stdout = json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "result": "hello",
                    "session_id": "cursor-session-1",
                    "usage": {
                        "inputTokens": 2,
                        "outputTokens": 3,
                        "cacheReadTokens": 4,
                        "cacheWriteTokens": 5,
                    },
                }
            )
        return SimpleNamespace(
            returncode=self.returncode,
            stdout=stdout,
            stderr=self.stderr,
        )


def test_first_turn_runs_cursor_print_json_with_workspace(tmp_path):
    runner = FakeRunner()
    session = CursorHeadlessSession(workspace=str(tmp_path), runner=runner)

    result = session.run_turn("Say hello")

    cmd = runner.calls[0][0]
    assert cmd[:4] == ["agent", "-p", "--output-format", "json"]
    assert "--workspace" in cmd
    assert str(tmp_path) in cmd
    assert "--trust" in cmd
    assert "--resume" not in cmd
    assert cmd[-1] == "Say hello"
    assert result.final_text == "hello"
    assert result.session_id == "cursor-session-1"
    assert result.usage == {
        "input_tokens": 2,
        "output_tokens": 3,
        "cache_read_tokens": 4,
        "cache_write_tokens": 5,
        "total_tokens": 14,
    }


def test_second_turn_resumes_cursor_session(tmp_path):
    runner = FakeRunner()
    session = CursorHeadlessSession(workspace=str(tmp_path), runner=runner)

    session.run_turn("one")
    session.run_turn("two")

    assert "--resume" in runner.calls[1][0]
    assert "cursor-session-1" in runner.calls[1][0]


def test_force_and_model_flags_are_optional(tmp_path):
    runner = FakeRunner()
    session = CursorHeadlessSession(
        workspace=str(tmp_path),
        runner=runner,
        force=True,
        model="claude-opus-4-8-thinking-high",
    )

    session.run_turn("work")

    cmd = runner.calls[0][0]
    assert "--force" in cmd
    assert "--model" in cmd
    assert "claude-opus-4-8-thinking-high" in cmd


def test_workspace_trust_flag_can_be_disabled(tmp_path):
    runner = FakeRunner()
    session = CursorHeadlessSession(
        workspace=str(tmp_path),
        runner=runner,
        trust=False,
    )

    session.run_turn("work")

    assert "--trust" not in runner.calls[0][0]


def test_nonzero_exit_raises_actionable_error(tmp_path):
    runner = FakeRunner(stdout="", stderr="not logged in", returncode=1)
    session = CursorHeadlessSession(workspace=str(tmp_path), runner=runner)

    with pytest.raises(CursorHeadlessError, match="agent login"):
        session.run_turn("work")


def test_invalid_json_raises_actionable_error(tmp_path):
    runner = FakeRunner(stdout="not json")
    session = CursorHeadlessSession(workspace=str(tmp_path), runner=runner)

    with pytest.raises(CursorHeadlessError, match="unparseable"):
        session.run_turn("work")


def test_timeout_raises_actionable_error(tmp_path):
    def timeout_runner(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=1)

    session = CursorHeadlessSession(workspace=str(tmp_path), runner=timeout_runner)

    with pytest.raises(CursorHeadlessError, match="timed out"):
        session.run_turn("work")


def test_run_cursor_headless_turn_appends_projected_messages():
    from agent.codex_runtime import run_cursor_headless_turn

    fake_result = SimpleNamespace(
        final_text="done",
        session_id="cursor-session-1",
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
        _cursor_headless_session=fake_session,
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
        session_id=None,
        _session_db_created=False,
        model="cursor",
        provider="cursor",
        base_url="",
        api_key="",
    )
    messages = [{"role": "user", "content": "work"}]

    out = run_cursor_headless_turn(
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
    assert out["cursor_session_id"] == "cursor-session-1"


def test_run_cursor_headless_turn_uses_configured_cursor_model(monkeypatch):
    from agent.codex_runtime import run_cursor_headless_turn
    from agent.transports import cursor_headless as cursor_transport

    captured = {}

    class FakeCursorSession:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run_turn(self, prompt: str):
            return SimpleNamespace(
                final_text="done",
                session_id="cursor-session-1",
                usage={},
            )

    monkeypatch.delenv("HERMES_CURSOR_MODEL", raising=False)
    monkeypatch.setattr(cursor_transport, "CursorHeadlessSession", FakeCursorSession)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"HERMES_CURSOR_MODEL": "auto"},
    )
    agent = SimpleNamespace(
        _cursor_headless_session=None,
        session_cwd="/tmp/work",
        session_api_calls=0,
        _iters_since_skill=0,
        _skill_nudge_interval=0,
        valid_tool_names=set(),
        _session_db=None,
        session_id=None,
    )

    run_cursor_headless_turn(
        agent,
        user_message="work",
        original_user_message="work",
        messages=[],
        effective_task_id="t",
    )

    assert captured["model"] == "auto"


def test_run_cursor_headless_turn_uses_configured_cursor_workspace(monkeypatch, tmp_path):
    from agent.codex_runtime import run_cursor_headless_turn
    from agent.transports import cursor_headless as cursor_transport

    captured = {}
    project_workspace = tmp_path / "exipay"
    project_workspace.mkdir()

    class FakeCursorSession:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run_turn(self, prompt: str):
            return SimpleNamespace(
                final_text="done",
                session_id="cursor-session-1",
                usage={},
            )

    monkeypatch.delenv("HERMES_CURSOR_WORKSPACE", raising=False)
    monkeypatch.setattr(cursor_transport, "CursorHeadlessSession", FakeCursorSession)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"HERMES_CURSOR_WORKSPACE": str(project_workspace)},
    )
    agent = SimpleNamespace(
        _cursor_headless_session=None,
        session_cwd="/Users/yashielsookdeo",
        session_api_calls=0,
        _iters_since_skill=0,
        _skill_nudge_interval=0,
        valid_tool_names=set(),
        _session_db=None,
        session_id=None,
    )

    run_cursor_headless_turn(
        agent,
        user_message="work",
        original_user_message="work",
        messages=[],
        effective_task_id="t",
    )

    assert captured["workspace"] == str(project_workspace)
