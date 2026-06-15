"""Tests for resolving Cursor headless as an external agent runtime."""

from __future__ import annotations

from hermes_cli import runtime_provider as rp
from hermes_cli.runtime_provider import _maybe_apply_codex_app_server_runtime


def test_cursor_runtime_applies_to_any_provider():
    assert _maybe_apply_codex_app_server_runtime(
        provider="custom",
        api_mode="chat_completions",
        model_cfg={"agent_runtime": "cursor_headless"},
    ) == "cursor_headless"


def test_cursor_pty_runtime_applies_to_any_provider():
    assert _maybe_apply_codex_app_server_runtime(
        provider="custom",
        api_mode="chat_completions",
        model_cfg={"agent_runtime": "cursor_pty"},
    ) == "cursor_pty"


def test_legacy_codex_runtime_still_only_applies_to_openai_codex():
    assert _maybe_apply_codex_app_server_runtime(
        provider="custom",
        api_mode="chat_completions",
        model_cfg={"openai_runtime": "codex_app_server"},
    ) == "chat_completions"


def test_cursor_runtime_applies_to_env_config_custom_provider(monkeypatch):
    monkeypatch.setattr(
        rp,
        "load_config",
        lambda: {
            "model": {
                "provider": "custom",
                "base_url": "https://api.groq.com/openai/v1",
                "api_key": "stub-key",
                "agent_runtime": "cursor_headless",
            }
        },
    )
    monkeypatch.setattr(rp, "_try_resolve_from_custom_pool", lambda *a, **k: None)

    resolved = rp.resolve_runtime_provider(requested="custom")

    assert resolved["provider"] == "custom"
    assert resolved["api_mode"] == "cursor_headless"


def test_cursor_pty_runtime_applies_to_env_config_custom_provider(monkeypatch):
    monkeypatch.setattr(
        rp,
        "load_config",
        lambda: {
            "model": {
                "provider": "custom",
                "base_url": "https://api.groq.com/openai/v1",
                "api_key": "stub-key",
                "agent_runtime": "cursor_pty",
            }
        },
    )
    monkeypatch.setattr(rp, "_try_resolve_from_custom_pool", lambda *a, **k: None)

    resolved = rp.resolve_runtime_provider(requested="custom")

    assert resolved["provider"] == "custom"
    assert resolved["api_mode"] == "cursor_pty"
