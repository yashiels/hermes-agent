"""Tests for Cursor Agent CLI PTY runtime support."""

from __future__ import annotations

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
