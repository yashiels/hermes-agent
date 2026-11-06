import copy
import hashlib
import json
import logging
import os
import shlex

import pytest

from hermes_cli import config, plugins
from tools import approval, approval_context
from tools.approval_plugin_rules import plugin_rule_exemption


MESSAGE_ID = "0123456789abcdef"
GH = "gh auth status"
OWNED = "gh issue view 579 -R yashiels/agent-scripts"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    script = tmp_path.resolve() / "work_google.py"
    script.write_bytes(b"raise RuntimeError('fixture must never execute')\n")
    entry = {"script": str(script), "sha256": hashlib.sha256(script.read_bytes()).hexdigest(), "max_chars": 8000}
    policy = {"rule_keys": ["plugin_rule:terminal"], "plugins": ["jev-judge"],
              "gh_owners": ["yashiels"], "git_local": True,
              "readonly_commands": {"gh_auth_status": True, "gmail_get": [entry]}}

    def configure(value=policy, deny=()):
        (tmp_path / "config.yaml").write_text(json.dumps({"approvals": {
            "mode": "manual", "plugin_rule_exemptions": value, "deny": list(deny)}, "command_allowlist": []}))
        config._LOAD_CONFIG_CACHE.clear()

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    for key in ("GH_HOST", "GH_REPO", "HERMES_EXEC_ASK", "HERMES_GATEWAY_SESSION", "HERMES_SESSION_PLATFORM"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(approval, "_session_approved", {})
    manager = plugins.PluginManager()
    plugins.PluginContext(plugins.PluginManifest(name="jev-judge"), manager).register_hook(
        "pre_tool_call", lambda **kw: {"action": "approve", "message": "synthetic Jev review"})
    monkeypatch.setattr(plugins, "invoke_hook", manager.invoke_hook)
    calls = []
    monkeypatch.setattr("tools.terminal_tool._get_approval_callback", lambda: lambda *a, **kw: calls.append(a) or "deny")
    token = approval_context.set_current_session_key("readonly-test")
    configure()
    yield {"script": script, "entry": entry, "policy": policy, "configure": configure, "manager": manager, "calls": calls}
    approval_context.reset_current_session_key(token)
    config._LOAD_CONFIG_CACHE.clear()


def gmail(workspace, *args):
    return shlex.join(["python3", "-I", str(workspace["script"]), "gmail-get", MESSAGE_ID, *args])


def exempt(command):
    return plugin_rule_exemption("terminal", {"command": command}, "terminal", "jev-judge")


@pytest.fixture(params=["chat", "cron"])
def surface(request, monkeypatch):
    if request.param == "cron":
        monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
        monkeypatch.setenv("HERMES_SESSION_PLATFORM", "cron")
    return request.param


@pytest.mark.parametrize("case", ["gh", "default", "one", "bound", "quoted", "zeros"])
@pytest.mark.platforms("posix")
def test_exact_commands_cross_real_approval_boundary(workspace, surface, caplog, case):
    commands = {"gh": GH, "default": gmail(workspace), "one": gmail(workspace, "--chars", "1"),
                "bound": gmail(workspace, "--chars", "8000"), "quoted": "'gh'\tauth  'status'",
                "zeros": gmail(workspace, "--chars", "00001")}
    command = commands[case]
    with caplog.at_level(logging.INFO, logger="tools.approval"):
        assert plugins.resolve_pre_tool_block("terminal", {"command": command}) is None
    assert workspace["calls"] == []
    logs = [r.message for r in caplog.records if "exemption:" in r.message]
    assert logs == ["Plugin-rule exemption: readonly_commands"]
    assert MESSAGE_ID not in logs[0] and command not in logs[0]


@pytest.mark.parametrize("suffix", [";", "; ", " && gh auth status", " || gh auth status", " | gh auth status",
                                    " &", " > output", " 2>&1", " < input", "\n", " # comment", " $(true)", " `true`"])
@pytest.mark.parametrize("kind", ["gh", "gmail"])
def test_shell_composition_never_exempts(workspace, surface, suffix, kind):
    command = (GH if kind == "gh" else gmail(workspace)) + suffix
    assert plugins.resolve_pre_tool_block("terminal", {"command": command}) is not None


@pytest.mark.parametrize("command", [
    "gh auth token", "gh auth login", "gh auth logout", "gh auth switch", "gh auth refresh",
    *[GH + " " + flag for flag in ["--show-token", "-t", "--active", "--json", "--hostname github.com", "-h", "--help", "--"]],
    "/usr/bin/gh auth status", "./gh auth status", "env gh auth status", "command gh auth status",
    "sudo gh auth status", "GH_HOST=evil gh auth status", "VAR=x gh auth status", "bash -c 'gh auth status'",
    "gh auth status && git status", "git status; gh auth status",
])
def test_gh_does_not_widen(workspace, surface, command):
    assert plugins.resolve_pre_tool_block("terminal", {"command": command}) is not None


@pytest.mark.parametrize("case", ["no_isolation", "python", "absolute_python", "extra_flag", "module", "code", "send", "alias",
                                  "id_short", "id_upper", "id_long", "id_unicode", "zero", "negative", "over", "unicode_chars",
                                  "decimal", "duplicate", "equals", "order", "extra", "output", "huge_chars"])
def test_gmail_shape_is_closed(workspace, surface, case):
    argv = ["python3", "-I", str(workspace["script"]), "gmail-get", MESSAGE_ID]
    variants = {
        "no_isolation": [argv[0], *argv[2:]], "python": ["python", *argv[1:]],
        "absolute_python": ["/usr/bin/python3", *argv[1:]], "extra_flag": [*argv[:2], "-B", *argv[2:]],
        "module": ["python3", "-I", "-m", "work_google", *argv[3:]], "code": ["python3", "-I", "-c", "pass"],
        "send": [*argv[:3], "gmail-send", MESSAGE_ID], "alias": [*argv[:2], str(workspace["script"].parent / "alias.py"), *argv[3:]],
        "id_short": [*argv[:4], "abc"], "id_upper": [*argv[:4], MESSAGE_ID.upper()],
        "id_long": [*argv[:4], MESSAGE_ID + "0"], "id_unicode": [*argv[:4], "０" * 16],
        "order": [*argv[:4], "--chars", "1", MESSAGE_ID], "extra": [*argv, "extra"],
        "output": [*argv, "--output", "file"], "duplicate": [*argv, "--chars", "1", "--chars", "2"],
        "equals": [*argv, "--chars=1"],
    }
    values = {"zero": "0", "negative": "-1", "over": "8001", "unicode_chars": "１", "decimal": "1.5", "huge_chars": "9" * 4301}
    command = shlex.join(variants[case] if case in variants else [*argv, "--chars", values[case]])
    assert plugins.resolve_pre_tool_block("terminal", {"command": command}) is not None


@pytest.mark.parametrize("value", [None, [], True, "on", {"unknown": True}, {"gh_auth_status": 1}, {"gmail_get": {}},
                                  {"gmail_get": [None]}, {"gmail_get": [{}]}])
def test_malformed_readonly_policy_fails_all_exemptions_closed(workspace, value):
    policy = {**workspace["policy"], "readonly_commands": value}
    workspace["configure"](policy)
    for command in [GH, gmail(workspace), OWNED, "git status"]:
        assert exempt(command) is None


@pytest.mark.parametrize("case", ["unknown", "duplicate", "relative", "noncanonical", "nul", "surrogate", "digest_upper", "digest_short",
                                  "chars_bool", "chars_zero", "chars_large", "chars_float", "missing_field"])
def test_malformed_helper_policy_fails_all_exemptions_closed(workspace, case):
    policy = copy.deepcopy(workspace["policy"])
    entry = policy["readonly_commands"]["gmail_get"][0]
    changes = {"relative": ("script", "relative.py"), "noncanonical": ("script", str(workspace["script"].parent) + "/./work_google.py"),
               "nul": ("script", "/a\x00b"), "surrogate": ("script", "/a\ud800"),
               "digest_upper": ("sha256", "A" * 64), "digest_short": ("sha256", "a" * 63),
               "chars_bool": ("max_chars", True), "chars_zero": ("max_chars", 0),
               "chars_large": ("max_chars", 8001), "chars_float": ("max_chars", 1.0), "unknown": ("extra", True)}
    if case in changes:
        key, value = changes[case]
        entry[key] = value
    elif case == "duplicate":
        policy["readonly_commands"]["gmail_get"].append(dict(entry))
    else:
        del entry["sha256"]
    workspace["configure"](policy)
    assert exempt(OWNED) is None
    assert exempt(gmail(workspace)) is None


@pytest.mark.parametrize("value", [{}, {"gh_auth_status": False, "gmail_get": []}, "absent"])
def test_readonly_policy_is_opt_in(workspace, value):
    policy = copy.deepcopy(workspace["policy"])
    if value == "absent":
        del policy["readonly_commands"]
    else:
        policy["readonly_commands"] = value
    workspace["configure"](policy)
    assert exempt(GH) is None
    assert exempt(gmail(workspace)) is None
    assert exempt(OWNED)["approved"] is True


@pytest.mark.parametrize("case", ["missing", "directory", "symlink", "drift", "oversize", "fifo", "parent_symlink"])
@pytest.mark.platforms("posix")
def test_helper_file_must_match_reviewed_bytes(workspace, surface, case):
    script = workspace["script"]
    original = script.read_bytes()
    script.unlink()

    def symlink():
        target = script.with_suffix(".saved")
        target.write_bytes(original)
        script.symlink_to(target)

    def oversize():
        script.write_bytes(b"x" * (1024 * 1024 + 1))
        workspace["entry"]["sha256"] = hashlib.sha256(script.read_bytes()).hexdigest()
        workspace["configure"]()

    def parent_symlink():
        directory = script.parent / "real"
        directory.mkdir()
        (directory / script.name).write_bytes(original)
        alias = script.parent / "alias"
        alias.symlink_to(directory, target_is_directory=True)
        workspace["script"] = alias / script.name
        workspace["entry"]["script"] = str(workspace["script"])
        workspace["configure"]()
    actions = {"missing": lambda: None, "directory": script.mkdir, "symlink": symlink,
               "drift": lambda: script.write_bytes(original + b"pass\n"), "oversize": oversize,
               "fifo": lambda: os.mkfifo(script), "parent_symlink": parent_symlink}
    actions[case]()
    assert plugins.resolve_pre_tool_block("terminal", {"command": gmail(workspace)}) is not None


@pytest.mark.platforms("posix")
def test_small_character_limit_requires_explicit_chars(workspace):
    workspace["entry"]["max_chars"] = 10
    workspace["configure"]()
    assert exempt(gmail(workspace)) is None
    assert exempt(gmail(workspace, "--chars", "11")) is None
    assert exempt(gmail(workspace, "--chars", "10"))["approved"] is True


@pytest.mark.parametrize("name", ["work;google.py", "work[1].py"])
@pytest.mark.platforms("posix")
def test_quoted_script_metacharacters_are_literal(workspace, name):
    script = workspace["script"]
    renamed = script.with_name(name)
    script.rename(renamed)
    workspace["script"] = renamed
    workspace["entry"]["script"] = str(renamed)
    workspace["configure"]()
    assert exempt(gmail(workspace))["approved"] is True
    assert exempt(gmail(workspace).replace("'", "")) is None


def test_missing_script_is_valid_policy_but_cannot_match(workspace):
    workspace["script"].unlink()
    assert exempt(OWNED)["approved"] is True
    assert exempt(gmail(workspace)) is None


def test_helper_io_errors_fail_closed(workspace, monkeypatch):
    original = os.open

    def refuse_script(path, *args, **kwargs):
        if path == str(workspace["script"]):
            raise PermissionError("synthetic read failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", refuse_script)
    assert plugins.resolve_pre_tool_block("terminal", {"command": gmail(workspace)}) is not None


@pytest.mark.parametrize("source", ["process", "profile", "other_profile"])
def test_gh_host_uses_served_profile(workspace, monkeypatch, source):
    from agent.secret_scope import reset_multiplex_context, reset_secret_scope, set_multiplex_context, set_secret_scope

    monkeypatch.setenv("GH_HOST", "launch-host")
    context = set_multiplex_context(source != "process")
    scope = set_secret_scope({"GH_HOST": "served-host"} if source == "profile" else {})
    try:
        result = plugins.resolve_pre_tool_block("terminal", {"command": GH})
        assert (result is None) == (source == "other_profile")
    finally:
        reset_secret_scope(scope)
        reset_multiplex_context(context)


@pytest.mark.parametrize("kind", ["gh", "gmail"])
def test_deny_and_independent_plugin_remain_stronger(workspace, surface, kind):
    command = GH if kind == "gh" else gmail(workspace)
    workspace["configure"](deny=[command])
    result = approval.request_tool_approval("terminal", "synthetic", rule_key="terminal", plugin_name="jev-judge", tool_args={"command": command})
    assert result["approved"] is False and result["user_deny"] is True
    workspace["configure"]()
    plugins.PluginContext(plugins.PluginManifest(name="independent-policy"), workspace["manager"]).register_hook(
        "pre_tool_call", lambda **kw: {"action": "approve", "message": "synthetic independent review"})
    assert plugins.resolve_pre_tool_block("terminal", {"command": command}) is not None


@pytest.mark.parametrize("plugin,rule,tool", [("other", "terminal", "terminal"), ("jev-judge", "other", "terminal"),
                                            ("jev-judge", "terminal", "write_file")])
def test_registered_identity_and_terminal_rule_required(workspace, plugin, rule, tool):
    workspace["manager"]._hooks.clear()
    plugins.PluginContext(plugins.PluginManifest(name=plugin), workspace["manager"]).register_hook(
        "pre_tool_call", lambda **kw: {"action": "approve", "rule_key": rule, "plugin_name": "jev-judge"})
    assert plugins.resolve_pre_tool_block(tool, {"command": GH}) is not None


@pytest.mark.parametrize("command", ["reboot", "sudo -S gh auth status"])
def test_unconditional_floors_remain(workspace, command):
    result = exempt(command)
    assert result["approved"] is False
    assert "BLOCKED" in result["message"]
