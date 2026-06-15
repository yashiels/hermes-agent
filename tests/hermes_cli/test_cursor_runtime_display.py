"""Display labels for external agent runtimes."""

from __future__ import annotations

import io
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from rich.console import Console


def _cursor_config():
    return {
        "HERMES_CURSOR_MODEL": "auto",
        "model": {
            "default": "openai/gpt-oss-120b",
            "provider": "custom",
            "agent_runtime": "cursor_headless",
        },
    }


def test_banner_shows_cursor_runtime_model_label(tmp_path):
    from hermes_cli import banner

    hh = tmp_path / ".hermes"
    hh.mkdir()
    (hh / ".install_method").write_text("git\n")

    with (
        patch("hermes_cli.config.get_hermes_home", return_value=hh),
        patch("hermes_constants.get_hermes_home", return_value=hh),
        patch("hermes_cli.config.load_config", return_value=_cursor_config()),
    ):
        buf = io.StringIO()
        console = Console(file=buf, width=400, force_terminal=False, color_system=None)
        banner.build_welcome_banner(
            console,
            model="openai/gpt-oss-120b",
            cwd="/tmp",
            tools=[{"function": {"name": "terminal"}}],
            enabled_toolsets=["terminal"],
        )

    out = buf.getvalue()
    assert "cursor:auto" in out
    assert "Cursor headless" in out
    assert "gpt-oss-120b" not in out


def test_status_bar_snapshot_shows_cursor_runtime_model_label(monkeypatch):
    import cli

    monkeypatch.setattr(cli, "CLI_CONFIG", _cursor_config())
    ui = object.__new__(cli.HermesCLI)
    ui.model = "openai/gpt-oss-120b"
    ui.session_start = datetime.now()
    ui._prompt_start_time = None
    ui._prompt_duration = 0.0
    ui._last_turn_finished_at = None
    ui._background_tasks = {}
    ui.agent = SimpleNamespace(
        model="gpt-oss-120b",
        api_mode="cursor_headless",
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

    snapshot = ui._get_status_bar_snapshot()

    assert snapshot["model_name"] == "cursor:auto"
    assert snapshot["model_short"] == "cursor:auto"


def test_startup_status_shows_cursor_runtime_model_label(monkeypatch):
    import cli

    monkeypatch.setenv("HERMES_DEFER_AGENT_STARTUP", "1")
    monkeypatch.setattr(cli, "CLI_CONFIG", _cursor_config())

    ui = object.__new__(cli.HermesCLI)
    ui.model = "openai/gpt-oss-120b"
    ui.api_mode = "chat_completions"
    ui.enabled_toolsets = []
    ui.api_key = "not-used-by-cursor"
    ui.provider = "custom"
    ui._provider_source = "config"
    lines = []
    ui._console_print = lines.append

    ui._show_status()

    out = "\n".join(lines)
    assert "cursor:auto" in out
    assert "provider: Cursor" in out
    assert "gpt-oss-120b" not in out
