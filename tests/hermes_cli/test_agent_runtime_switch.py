"""Tests for external agent runtime selection.

These cover the runtime switch state machine shared by CLI and gateway
surfaces. Codex keeps its legacy selector, while Cursor uses the new
provider-neutral ``model.agent_runtime`` key.
"""

from __future__ import annotations

from unittest.mock import patch

from hermes_cli import codex_runtime_switch as runtime_switch


def test_parse_cursor_runtime_aliases():
    assert runtime_switch.parse_args("cursor") == ("cursor_headless", [])
    assert runtime_switch.parse_args("cursor_headless") == ("cursor_headless", [])
    assert runtime_switch.parse_args("cursor_pty") == ("cursor_pty", [])
    assert runtime_switch.parse_args("cursor-pty") == ("cursor_pty", [])


def test_get_current_runtime_prefers_agent_runtime():
    cfg = {
        "model": {
            "openai_runtime": "codex_app_server",
            "agent_runtime": "cursor_headless",
        }
    }
    assert runtime_switch.get_current_runtime(cfg) == "cursor_headless"


def test_set_runtime_writes_agent_runtime():
    cfg = {}
    old = runtime_switch.set_runtime(cfg, "cursor_headless")
    assert old == "auto"
    assert cfg["model"]["agent_runtime"] == "cursor_headless"


def test_set_cursor_pty_runtime_writes_agent_runtime():
    cfg = {}
    old = runtime_switch.set_runtime(cfg, "cursor_pty")
    assert old == "auto"
    assert cfg["model"]["agent_runtime"] == "cursor_pty"


def test_enable_cursor_checks_agent_binary_not_codex():
    cfg = {}
    with patch.object(
        runtime_switch,
        "check_cursor_binary_ok",
        return_value=(True, "Cursor Agent 1"),
    ) as cursor_check, patch.object(
        runtime_switch,
        "check_codex_binary_ok",
    ) as codex_check:
        result = runtime_switch.apply(cfg, "cursor_headless")
    assert result.success
    assert result.new_value == "cursor_headless"
    cursor_check.assert_called_once()
    codex_check.assert_not_called()


def test_enable_cursor_pty_checks_pinned_agent_binary_not_codex():
    cfg = {}
    with patch.object(
        runtime_switch,
        "check_cursor_pty_binary_ok",
        return_value=(True, "2026.06.15-03-48-54-da23e37"),
    ) as cursor_check, patch.object(
        runtime_switch,
        "check_codex_binary_ok",
    ) as codex_check:
        result = runtime_switch.apply(cfg, "cursor_pty")
    assert result.success
    assert result.new_value == "cursor_pty"
    assert cfg["model"]["agent_runtime"] == "cursor_pty"
    cursor_check.assert_called_once()
    codex_check.assert_not_called()
