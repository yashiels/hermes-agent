"""Integration tests for the cursor_pty runtime path through AIAgent."""

from __future__ import annotations

from unittest.mock import patch

import run_agent
from agent.transports.cursor_pty import CursorPtyResult
from agent.transports.cursor_pty_session import CursorPtySession


def _make_cursor_agent():
    return run_agent.AIAgent(
        api_key="stub",
        base_url="https://stub.invalid",
        provider="custom",
        api_mode="cursor_pty",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )


def test_api_mode_is_cursor_pty():
    agent = _make_cursor_agent()
    assert agent.api_mode == "cursor_pty"


def test_run_conversation_returns_cursor_pty_shape(monkeypatch):
    def fake_run_turn(self, prompt: str):
        return CursorPtyResult(
            final_text=f"cursor: {prompt}",
            cursor_chat_id="cursor-chat-1",
            usage={
                "input_tokens": 2,
                "output_tokens": 3,
                "cache_read_tokens": 4,
                "cache_write_tokens": 5,
                "total_tokens": 14,
            },
        )

    monkeypatch.setattr(CursorPtySession, "run_turn", fake_run_turn)
    agent = _make_cursor_agent()

    with patch.object(agent, "_spawn_background_review", return_value=None):
        result = agent.run_conversation("hello there")

    assert result["final_response"] == "cursor: hello there"
    assert result["completed"] is True
    assert result["partial"] is False
    assert result["error"] is None
    assert result["api_calls"] == 1
    assert result["cursor_chat_id"] == "cursor-chat-1"

    assert result["prompt_tokens"] == 11
    assert result["completion_tokens"] == 3
    assert result["total_tokens"] == 14
    assert result["cost_status"] == "included"

    assert agent.session_api_calls == 1
    assert agent.session_prompt_tokens == 11
    assert agent.session_completion_tokens == 3
    assert agent.session_total_tokens == 14
