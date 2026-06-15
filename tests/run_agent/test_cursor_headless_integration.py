"""Integration tests for the cursor_headless runtime path through AIAgent."""

from __future__ import annotations

from unittest.mock import patch

import run_agent
from agent.transports.cursor_headless import CursorHeadlessResult, CursorHeadlessSession


def _make_cursor_agent():
    return run_agent.AIAgent(
        api_key="stub",
        base_url="https://stub.invalid",
        provider="custom",
        api_mode="cursor_headless",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )


def test_api_mode_is_cursor_headless():
    agent = _make_cursor_agent()
    assert agent.api_mode == "cursor_headless"


def test_run_conversation_returns_cursor_shape(monkeypatch):
    def fake_run_turn(self, prompt: str):
        self.session_id = "cursor-session-1"
        return CursorHeadlessResult(
            final_text=f"cursor: {prompt}",
            session_id=self.session_id,
            usage={
                "input_tokens": 2,
                "output_tokens": 3,
                "cache_read_tokens": 4,
                "cache_write_tokens": 5,
                "total_tokens": 14,
            },
        )

    monkeypatch.setattr(CursorHeadlessSession, "run_turn", fake_run_turn)
    agent = _make_cursor_agent()

    with patch.object(agent, "_spawn_background_review", return_value=None):
        result = agent.run_conversation("hello there")

    assert result["final_response"] == "cursor: hello there"
    assert result["completed"] is True
    assert result["partial"] is False
    assert result["error"] is None
    assert result["api_calls"] == 1
    assert result["cursor_session_id"] == "cursor-session-1"

    assert result["prompt_tokens"] == 11
    assert result["completion_tokens"] == 3
    assert result["total_tokens"] == 14
    assert result["cost_status"] == "included"

    assert agent.session_api_calls == 1
    assert agent.session_prompt_tokens == 11
    assert agent.session_completion_tokens == 3
    assert agent.session_total_tokens == 14
