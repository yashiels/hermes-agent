"""Tests for Cursor Agent CLI PTY runtime support."""

from __future__ import annotations


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
