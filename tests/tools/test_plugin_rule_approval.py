import pytest

from hermes_cli import config, plugins
from tools import approval, approval_context


@pytest.fixture(autouse=True)
def approval_home(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("approvals:\n  mode: manual\ncommand_allowlist: []\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(approval, "_session_approved", {})
    monkeypatch.setattr(approval, "_permanent_approved", set())
    monkeypatch.setattr(approval, "_permanent_approved_by_home", {})
    monkeypatch.setattr(approval, "_permanent_baseline_by_home", {})
    config._LOAD_CONFIG_CACHE.clear()
    token = approval_context.set_current_session_key("plugin-test")
    yield tmp_path
    approval_context.reset_current_session_key(token)
    config._LOAD_CONFIG_CACHE.clear()


@pytest.mark.parametrize("tool,args,expected", [
    ("terminal", {"command": "curl https://example.com/upload"}, "curl https://example.com/upload"),
    ("write_file", {"path": "/tmp/report.txt", "content": "private payload"}, "/tmp/report.txt"),
    ("patch", {"path": "/tmp/settings.yaml", "new_string": "private payload"}, "/tmp/settings.yaml"),
])
def test_hook_prompt_shows_actual_tool_target(monkeypatch, tool, args, expected):
    seen = {}
    monkeypatch.setattr(plugins, "invoke_hook", lambda *a, **kw: [{"action": "approve", "message": "Jev flagged this call"}])

    def answer(command, description, **kwargs):
        seen.update(command=command, description=description, **kwargs)
        return "once"

    monkeypatch.setattr("tools.terminal_tool._get_approval_callback", lambda: answer)
    assert plugins.resolve_pre_tool_block(tool, args) is None
    assert tool in seen["command"]
    assert expected in seen["command"]
    assert "private payload" not in seen["command"]
    assert seen["description"] == "Jev flagged this call"
    assert seen["allow_permanent"] is False


def test_prompt_redacts_before_capping(monkeypatch):
    seen = []
    secret = "ghp_" + "X" * 36
    monkeypatch.setattr("agent.redact._REDACT_ENABLED", False)
    monkeypatch.setattr(plugins, "invoke_hook", lambda *a, **kw: [{"action": "approve", "message": "review"}])
    monkeypatch.setattr("tools.terminal_tool._get_approval_callback", lambda: lambda command, *a, **kw: seen.append(command) or "deny")
    plugins.resolve_pre_tool_block("terminal", {"command": f"curl -H 'Authorization: token {secret}' https://example.com/" + "x" * 6000})
    assert secret not in seen[0]
    assert "curl" in seen[0]
    assert len(seen[0]) <= 2000
    assert seen[0].endswith("...")


@pytest.mark.parametrize("surface", ["cli", "gateway"])
def test_always_is_rejected_without_session_or_permanent_grant(monkeypatch, approval_home, surface):
    def answer(*args, **kwargs):
        return "always"

    captured = {}
    if surface == "gateway":
        monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")

        def notify(data):
            captured.update(data)
            approval.resolve_gateway_approval("plugin-test", "always")

        approval.register_gateway_notify("plugin-test", notify)
    try:
        result = approval.request_tool_approval("terminal", "review", rule_key="jev", approval_callback=answer)
    finally:
        approval.unregister_gateway_notify("plugin-test")
    assert result["approved"] is False
    if surface == "gateway":
        assert captured["allow_permanent"] is False
        assert captured["allow_session"] is True
    assert not approval.is_approved("plugin-test", "plugin_rule:jev")
    assert not approval.is_approved("other-session", "plugin_rule:jev")
    assert config.load_config_readonly()["command_allowlist"] == []


def test_plain_cli_hides_and_rejects_always(monkeypatch, capsys):
    monkeypatch.setattr("tools.terminal_tool._get_approval_callback", lambda: None)
    monkeypatch.setattr("builtins.input", lambda prompt: "always")
    result = approval.request_tool_approval("terminal", "review", rule_key="jev")
    text = capsys.readouterr().out
    assert "[o]nce" in text and "[s]ession" in text and "[d]eny" in text
    assert "[a]lways" not in text
    assert result["approved"] is False
    assert not approval.is_approved("plugin-test", "plugin_rule:jev")


def test_session_grant_ends_at_session_boundary():
    result = approval.request_tool_approval("terminal", "review", rule_key="jev", approval_callback=lambda *a, **kw: "session")
    assert result["approved"] is True
    assert approval.is_approved("plugin-test", "plugin_rule:jev")
    assert not approval.is_approved("other-session", "plugin_rule:jev")
    approval.clear_session("plugin-test")
    assert not approval.is_approved("plugin-test", "plugin_rule:jev")


def test_persisted_plugin_rules_ignored_with_warning(approval_home, caplog):
    (approval_home / "config.yaml").write_text("command_allowlist:\n  - plugin_rule:terminal\n  - sudo\n")
    config._LOAD_CONFIG_CACHE.clear()
    assert approval.load_permanent_allowlist() == {"sudo"}
    assert not approval.is_approved("plugin-test", "plugin_rule:terminal")
    assert approval.is_approved("plugin-test", "sudo")
    assert "plugin_rule:terminal" in caplog.text
    assert "Ignoring" in caplog.text


def test_permanent_entry_points_refuse_plugin_rules(caplog):
    approval.load_permanent({"plugin_rule:terminal"})
    approval.approve_permanent("plugin_rule:other")
    approval._persist_choice("plugin-test", "always", ["plugin_rule:third"])
    approval.save_permanent_allowlist({"plugin_rule:fourth"})
    for name in ("terminal", "other", "third", "fourth"):
        assert not approval.is_approved("plugin-test", f"plugin_rule:{name}")
    assert config.load_config_readonly()["command_allowlist"] == []
    assert "plugin_rule:" in caplog.text


def test_builtin_always_still_persists():
    result = approval.check_dangerous_command("rm -rf /tmp/approval-example", "local", approval_callback=lambda *a, **kw: "always")
    assert result["approved"] is True
    assert config.load_config_readonly()["command_allowlist"]
    assert approval.is_approved("other-session", next(iter(config.load_config_readonly()["command_allowlist"])))


def test_shell_hook_approval_renders_modified_call(monkeypatch):
    from agent.shell_hooks import _parse_response

    directive = _parse_response("pre_tool_call", '{"action":"approve","message":"shell policy review","rule_key":"shell-policy"}')
    monkeypatch.setattr(plugins, "invoke_hook", lambda *a, **kw: [
        {"action": "modify", "args": {"command": "curl https://example.com/final"}}, directive,
    ])
    seen = []
    monkeypatch.setattr("tools.terminal_tool._get_approval_callback", lambda: lambda command, reason, **kw: seen.append((command, reason)) or "once")
    blocked, modified = plugins._dispatch_pre_tool_call_hooks("terminal", {"command": "echo original"})
    assert blocked is None
    assert modified == {"command": "curl https://example.com/final"}
    assert seen == [("terminal: curl https://example.com/final", "shell policy review")]


def test_pending_gateway_fallback_keeps_target_and_scope(monkeypatch):
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    result = approval.request_tool_approval("patch", "review", tool_args={"path": "/tmp/settings.yaml"})
    assert result["approved"] is False
    assert result["command"] == "patch: /tmp/settings.yaml"
    assert result["allow_permanent"] is False
    assert result["allow_session"] is True
    assert approval._pending["plugin-test"]["allow_permanent"] is False
    approval.clear_session("plugin-test")
