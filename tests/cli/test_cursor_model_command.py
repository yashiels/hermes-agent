"""Tests for Cursor-runtime /model handling in the interactive CLI."""

from __future__ import annotations

from types import SimpleNamespace


class FakeContext:
    user_providers = None
    custom_providers = None

    def with_overrides(self, **kwargs):
        return self


class FakeCursorSession:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _make_cursor_cli(
    cli_mod,
    *,
    runtime: str = "cursor_pty",
    session=None,
    open_model_picker=None,
):
    ui = object.__new__(cli_mod.HermesCLI)
    ui.model = "openai/gpt-oss-120b"
    ui.provider = "custom"
    ui.requested_provider = "custom"
    ui.base_url = ""
    ui.api_key = ""
    ui.api_mode = runtime
    ui._explicit_api_key = ""
    ui._explicit_base_url = ""
    ui._pending_model_switch_note = ""
    ui._confirm_expensive_model_switch = lambda result: True
    ui._open_model_picker = open_model_picker or (
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("normal model picker opened")
        )
    )
    ui.agent = SimpleNamespace(
        api_mode=runtime,
        _cursor_pty_session=session if runtime == "cursor_pty" else None,
        _cursor_headless_session=session if runtime == "cursor_headless" else None,
        switch_model=lambda **kwargs: None,
    )
    return ui


def _install_common_patches(monkeypatch, cli_mod, *, config=None):
    from hermes_cli.cursor_models import CursorModel

    config = config or {
        "model": {
            "default": "openai/gpt-oss-120b",
            "provider": "custom",
            "agent_runtime": "cursor_pty",
            "cursor_model": "auto",
        }
    }
    printed: list[str] = []
    saves: list[tuple[str, object]] = []

    monkeypatch.delenv("HERMES_CURSOR_MODEL", raising=False)
    monkeypatch.setattr(cli_mod, "CLI_CONFIG", config)
    monkeypatch.setattr(cli_mod, "_cprint", lambda text: printed.append(str(text)))
    monkeypatch.setattr(
        "hermes_cli.inventory.load_picker_context",
        lambda: FakeContext(),
    )
    monkeypatch.setattr(
        "hermes_cli.cursor_models.list_cursor_models",
        lambda **kwargs: [
            CursorModel("auto", "Auto"),
            CursorModel("gpt-5.3-codex-low", "Codex 5.3 Low"),
            CursorModel("gpt-5.3-codex-low-fast", "Codex 5.3 Low Fast"),
            CursorModel("gpt-5.3-codex-high", "Codex 5.3 High"),
        ],
    )
    monkeypatch.setattr(
        cli_mod,
        "save_config_value",
        lambda key, value: saves.append((key, value)) or True,
    )
    return printed, saves


def test_cursor_model_command_global_saves_cursor_model_and_retires_session(monkeypatch):
    import cli as cli_mod

    session = FakeCursorSession()
    ui = _make_cursor_cli(cli_mod, session=session)
    _, saves = _install_common_patches(monkeypatch, cli_mod)

    from hermes_cli import model_switch as model_switch_mod

    monkeypatch.setattr(
        model_switch_mod,
        "switch_model",
        lambda **kwargs: model_switch_mod.ModelSwitchResult(
            success=True,
            new_model=kwargs["raw_input"],
            target_provider="custom",
        ),
    )

    cli_mod.HermesCLI._handle_model_switch(
        ui,
        "/model gpt-5.3-codex-low --global",
    )

    assert ("model.cursor_model", "gpt-5.3-codex-low") in saves
    assert ("model.default", "gpt-5.3-codex-low") not in saves
    assert session.closed is True
    assert ui.agent._cursor_pty_session is None
    assert ui.agent._cursor_model_override == "gpt-5.3-codex-low"
    assert cli_mod.CLI_CONFIG["model"]["cursor_model"] == "gpt-5.3-codex-low"


def test_cursor_model_command_session_only_updates_live_cursor_model(monkeypatch):
    import cli as cli_mod

    session = FakeCursorSession()
    ui = _make_cursor_cli(cli_mod, session=session)
    _, saves = _install_common_patches(monkeypatch, cli_mod)

    cli_mod.HermesCLI._handle_model_switch(ui, "/model gpt-5.3-codex-low")

    assert saves == []
    assert session.closed is True
    assert ui.agent._cursor_pty_session is None
    assert ui._cursor_model_override == "gpt-5.3-codex-low"
    assert ui.agent._cursor_model_override == "gpt-5.3-codex-low"
    assert cli_mod.CLI_CONFIG["model"]["cursor_model"] == "gpt-5.3-codex-low"


def test_cursor_model_command_no_args_shows_cursor_status(monkeypatch):
    import cli as cli_mod

    ui = _make_cursor_cli(cli_mod)
    printed, saves = _install_common_patches(monkeypatch, cli_mod)

    cli_mod.HermesCLI._handle_model_switch(ui, "/model")

    out = "\n".join(printed)
    assert "Current Cursor model: auto" in out
    assert "Cursor models:" in out
    assert "gpt-5.3-codex-low - Codex 5.3 Low" in out
    assert "gpt-5.3-codex-low-fast - Codex 5.3 Low Fast" in out
    assert "gpt-5.3-codex-high - Codex 5.3 High" in out
    assert "Hermes provider models:" in out
    assert "/model --provider" in out
    assert "/codex-runtime auto" in out
    assert saves == []


def test_cursor_model_command_rejects_unknown_cursor_model(monkeypatch):
    import cli as cli_mod

    session = FakeCursorSession()
    ui = _make_cursor_cli(cli_mod, session=session)
    printed, saves = _install_common_patches(monkeypatch, cli_mod)

    from hermes_cli import cursor_models as cursor_models_mod
    from hermes_cli import model_switch as model_switch_mod

    monkeypatch.setattr(
        cursor_models_mod,
        "list_cursor_models",
        lambda **kwargs: [cursor_models_mod.CursorModel("auto", "Auto")],
    )
    monkeypatch.setattr(
        model_switch_mod,
        "switch_model",
        lambda **kwargs: model_switch_mod.ModelSwitchResult(
            success=True,
            new_model=kwargs["raw_input"],
            target_provider="custom",
        ),
    )

    cli_mod.HermesCLI._handle_model_switch(ui, "/model made-up-cursor-model")

    out = "\n".join(printed)
    assert "Unknown Cursor model: made-up-cursor-model" in out
    assert saves == []
    assert session.closed is False
    assert cli_mod.CLI_CONFIG["model"]["cursor_model"] == "auto"


def test_cursor_model_command_provider_flag_uses_normal_switch(monkeypatch):
    import cli as cli_mod

    ui = _make_cursor_cli(cli_mod)
    _, saves = _install_common_patches(monkeypatch, cli_mod)

    from hermes_cli import model_switch as model_switch_mod

    switch_calls: list[dict[str, object]] = []

    def fake_switch_model(**kwargs):
        switch_calls.append(kwargs)
        return model_switch_mod.ModelSwitchResult(
            success=True,
            new_model=kwargs["raw_input"],
            target_provider=kwargs["explicit_provider"],
            provider_changed=True,
        )

    monkeypatch.setattr(model_switch_mod, "switch_model", fake_switch_model)

    cli_mod.HermesCLI._handle_model_switch(
        ui,
        "/model openrouter/gpt-5 --provider openrouter --global",
    )

    assert switch_calls
    assert ("model.default", "openrouter/gpt-5") in saves
    assert ("model.provider", "openrouter") in saves
    assert all(key != "model.cursor_model" for key, _value in saves)


def test_cursor_model_command_bare_provider_flag_opens_normal_picker(monkeypatch):
    import cli as cli_mod

    opened = []
    ui = _make_cursor_cli(
        cli_mod,
        open_model_picker=lambda *args, **kwargs: opened.append((args, kwargs)),
    )
    printed, saves = _install_common_patches(monkeypatch, cli_mod)
    monkeypatch.setattr(
        "hermes_cli.inventory.build_models_payload",
        lambda ctx, max_models=50: {
            "providers": [
                {
                    "slug": "openrouter",
                    "name": "OpenRouter",
                    "is_current": False,
                    "models": ["openai/gpt-5"],
                    "total_models": 1,
                }
            ]
        },
    )

    cli_mod.HermesCLI._handle_model_switch(ui, "/model --provider")

    assert opened
    assert saves == []
    assert "Current Cursor model" not in "\n".join(printed)
