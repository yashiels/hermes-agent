"""Tests for Cursor Agent CLI model helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def test_parse_cursor_models_marks_current():
    from hermes_cli.cursor_models import parse_cursor_models_output

    models = parse_cursor_models_output(
        "Available models\n\n"
        "auto - Auto (current)\n"
        "gpt-5.3-codex-low - Codex 5.3 Low\n"
        "composer-2.5-fast - Composer 2.5 Fast (default)\n"
    )

    assert [m.id for m in models] == [
        "auto",
        "gpt-5.3-codex-low",
        "composer-2.5-fast",
    ]
    assert models[0].label == "Auto"
    assert models[0].is_current is True
    assert models[1].label == "Codex 5.3 Low"
    assert models[2].is_default is True


def test_parse_cursor_models_ignores_non_model_lines():
    from hermes_cli.cursor_models import parse_cursor_models_output

    models = parse_cursor_models_output(
        "Available models\n"
        "Usage: agent models [options]\n"
        "auto - Auto (current)\n"
        "bad line without separator\n"
    )

    assert [m.id for m in models] == ["auto"]


def test_resolve_cursor_model_prefers_env(monkeypatch):
    from hermes_cli.cursor_models import resolve_cursor_model

    monkeypatch.setenv("HERMES_CURSOR_MODEL", "auto")

    assert (
        resolve_cursor_model({"model": {"cursor_model": "gpt-5.3-codex-low"}})
        == "auto"
    )


def test_resolve_cursor_model_uses_canonical_config(monkeypatch):
    from hermes_cli.cursor_models import resolve_cursor_model

    monkeypatch.delenv("HERMES_CURSOR_MODEL", raising=False)

    assert (
        resolve_cursor_model({"model": {"cursor_model": "gpt-5.3-codex-low"}})
        == "gpt-5.3-codex-low"
    )


def test_resolve_cursor_model_keeps_legacy_fallbacks(monkeypatch):
    from hermes_cli.cursor_models import resolve_cursor_model

    monkeypatch.delenv("HERMES_CURSOR_MODEL", raising=False)

    assert (
        resolve_cursor_model({"model": {"cursor_headless_model": "auto"}})
        == "auto"
    )
    assert (
        resolve_cursor_model({"model": {"cursor_pty_model": "gpt-5.3-codex-low"}})
        == "gpt-5.3-codex-low"
    )


def test_cursor_model_display_label_defaults_to_auto(monkeypatch):
    from hermes_cli.cursor_models import cursor_model_display_label

    monkeypatch.delenv("HERMES_CURSOR_MODEL", raising=False)

    assert cursor_model_display_label({"model": {}}) == "auto"


def test_list_cursor_models_runs_agent_models(monkeypatch):
    from hermes_cli import cursor_models

    calls = []

    def runner(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout="auto - Auto (current)\n",
            stderr="",
        )

    cursor_models.clear_cursor_models_cache()

    models = cursor_models.list_cursor_models(runner=runner)

    assert [m.id for m in models] == ["auto"]
    assert calls[0][0] == ["agent", "models"]


def test_list_cursor_models_raises_actionable_error_on_failure():
    from hermes_cli.cursor_models import CursorModelError, list_cursor_models

    def runner(cmd, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="not logged in")

    with pytest.raises(CursorModelError, match="not logged in"):
        list_cursor_models(runner=runner, use_cache=False)
