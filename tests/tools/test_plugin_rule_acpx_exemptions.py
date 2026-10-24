import copy
import json
import logging
import shlex
import subprocess
from pathlib import Path

import pytest

from hermes_cli import config, plugins
from tools import approval, approval_context
from tools.approval_plugin_rules import plugin_rule_exemption


PROMPT = "Read AGENT-BRIEF.md in this directory and execute it"
IMPLEMENTER = "You ARE the implementer. Edit files yourself. Do NOT spawn or delegate. Read AGENT-BRIEF.md in this directory and execute the full task it describes."
MODEL = "grok[subscription]"
GH = "gh issue view 525 -R yashiels/agent-scripts"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path.resolve() / "repos"
    repo = root / "yashiels/project"
    repo.mkdir(parents=True)
    git = ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "-C", str(repo)]
    for args in (["init"], ["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "--allow-empty", "-m", "initial"]):
        subprocess.run([*git, *args], check=True, capture_output=True)
    cwd = repo / ".worktrees/task"
    subprocess.run([*git, "worktree", "add", "-b", "task", str(cwd)], check=True, capture_output=True)
    (cwd / "AGENT-BRIEF.md").write_text("Implement the task")
    metadata = Path((cwd / ".git").read_text().strip()[8:])
    policy = {
        "rule_keys": ["plugin_rule:terminal"], "plugins": ["jev-judge"], "gh_owners": ["yashiels"],
        "git_local": True, "ssh_hosts": ["forge"],
        "oracle": {"scripts": [str(repo / "oracle.sh")], "file_roots": [str(root)]},
        "acpx": {"agents": ["codex", "cursor"], "repo_roots": [str(root)],
                 "prompts": [PROMPT, IMPLEMENTER], "prompt_round_files": True, "cursor_models": [MODEL]},
    }

    def configure(value=policy, deny=()):
        (tmp_path / "config.yaml").write_text(json.dumps({"approvals": {
            "mode": "manual", "plugin_rule_exemptions": value, "deny": list(deny),
        }}))
        config._LOAD_CONFIG_CACHE.clear()

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    for key in ("GH_HOST", "GH_REPO", "HERMES_EXEC_ASK", "HERMES_GATEWAY_SESSION", "HERMES_SESSION_PLATFORM"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(approval, "_session_approved", {})
    manager = plugins.PluginManager()
    plugins.PluginContext(plugins.PluginManifest(name="jev-judge"), manager).register_hook(
        "pre_tool_call", lambda **kw: {"action": "approve", "message": "review"})
    monkeypatch.setattr(plugins, "invoke_hook", manager.invoke_hook)
    monkeypatch.setattr("tools.terminal_tool._get_approval_callback", lambda: lambda *a, **kw: "deny")
    token = approval_context.set_current_session_key("acpx-test")
    configure()
    yield {"root": root, "repo": repo, "cwd": cwd, "metadata": metadata, "policy": policy, "configure": configure}
    approval_context.reset_current_session_key(token)
    config._LOAD_CONFIG_CACHE.clear()


def launch(workspace, *, cwd=None, agent="codex", options=(), sub=None, executable="acpx"):
    return shlex.join([executable, "--cwd", str(cwd or workspace["cwd"]), *options, agent,
                       *(sub if sub is not None else ["-s", "worker-1", PROMPT])])


def exempt(command):
    return plugin_rule_exemption("terminal", {"command": command}, "terminal", "jev-judge")


@pytest.mark.parametrize("case", ["ensure_launch", "cursor", "close", "history", "history_default", "round", "mixed", "implementer"])
def test_allowed_commands(workspace, caplog, case):
    commands = {
        "ensure_launch": launch(workspace, sub=["sessions", "ensure", "--name", "worker-1"]) + " && " + launch(workspace, options=["--approve-all", "--format", "json", "--timeout", "3000"]),
        "cursor": launch(workspace, agent="cursor", options=["--model", MODEL]),
        "close": launch(workspace, sub=["sessions", "close", "worker-1"]),
        "history": launch(workspace, sub=["sessions", "history", "worker-1", "--limit", "2"]),
        "history_default": launch(workspace, sub=["sessions", "history", "worker-1"]),
        "round": launch(workspace, sub=["-s", "worker-1", "Read AGENT-BRIEF-R3.md in this directory and execute it"]),
        "mixed": launch(workspace) + " && " + shlex.join(["git", "-C", str(workspace["cwd"]), "status"]),
        "implementer": launch(workspace, sub=["-s", "worker-1", IMPLEMENTER]),
    }
    (workspace["cwd"] / "AGENT-BRIEF-R3.md").write_text("Review round three")
    with caplog.at_level(logging.INFO, logger="tools.approval"):
        assert exempt(commands[case]) == {"approved": True, "message": None}
        assert plugins.resolve_pre_tool_block("terminal", {"command": commands[case]}) is None
    assert any("owner=acpx:yashiels/project" in record.message for record in caplog.records)


@pytest.mark.parametrize("case", ["cd", "pwd", "redirect", "background", "semicolon", "or", "pipe", "trailing_semicolon"])
def test_shell_forms_rejected(workspace, case):
    command = launch(workspace)
    forms = {
        "cd": shlex.join(["cd", str(workspace["cwd"])]) + " && " + command,
        "pwd": 'acpx --cwd "$PWD" codex sessions close worker',
        "redirect": command + " > log 2>&1", "background": command + " &",
        "semicolon": command + "; " + command, "or": command + " || true",
        "pipe": command + " | tee log", "trailing_semicolon": command + ";",
    }
    assert exempt(forms[case]) is None


@pytest.mark.parametrize("option", [
    ["--agent", "sh"], ["--permission-policy", "x"], ["--policy", "x"], ["--mcp-config", "x"],
    ["--append-system-prompt", "x"], ["--system-prompt", "x"], ["--allowed-tools", "x"],
    ["--deny-all"], ["--non-interactive-permissions", "x"], ["--verbose"],
    ["--approve-reads"], ["--suppress-reads"], ["--auth-policy", "skip"], ["--max-turns", "5"],
    ["--prompt-retries", "1"], ["--json-strict"], ["--no-fs"], ["--no-terminal"],
    ["--format", "quiet"], ["--ttl", "0"], ["--cwd", "/other"], ["--format=json"], ["--"],
    ["--approve-all", "--approve-all"], ["--timeout", "0"], ["--timeout", "86401"], ["--timeout", "1.5"],
    ["--timeout", "1", "--timeout", "2"], ["--format", "json", "--format", "text"],
])
def test_global_options_rejected(workspace, option):
    assert exempt(launch(workspace, options=option)) is None


@pytest.mark.parametrize("agent", ["claude", "pi", "@codex"])
def test_agent_rejected(workspace, agent):
    assert exempt(launch(workspace, agent=agent)) is None


@pytest.mark.parametrize("executable", ["/opt/x/acpx", "./acpx"])
def test_executable_rejected(workspace, executable):
    assert exempt(launch(workspace, executable=executable)) is None


def test_equals_cwd_rejected(workspace):
    assert exempt(shlex.join(["acpx", "--cwd=" + str(workspace["cwd"]), "codex", "sessions", "close", "worker"])) is None


@pytest.mark.parametrize("sub", [
    ["-s", "worker", "unlisted"], ["-s", "worker", PROMPT + " appended"], ["-s", "worker", PROMPT, "extra"],
    ["exec", PROMPT], ["set-mode", "code"], ["set", "x"], ["cancel"],
    ["-s", "bad/name", PROMPT], ["-s", "-bad", PROMPT], ["-s", "a" * 65, PROMPT],
    ["sessions", "close", "bad/name"], ["sessions", "history", "worker", "--limit", "0"],
    ["sessions", "history", "worker", "--limit", "1001"], ["sessions", "ensure", "--name", "worker", "extra"],
    *[["-s", "worker", f"Read AGENT-BRIEF-{name}.md in this directory and execute it"] for name in ["R0", "R100", "R01", "R1.md; x"]],
    *[["-s", "worker", PROMPT + control] for control in ["\x00", "\n", "\r", "\x01", "\x7f"]],
])
def test_subcommand_rejected(workspace, sub):
    assert exempt(launch(workspace, sub=sub)) is None


@pytest.mark.parametrize("case", ["canonical", "unowned", "parent", "symlink", "missing", "spoof"])
def test_cwd_rejected(workspace, case):
    cwd, repo = workspace["cwd"], workspace["repo"]
    alias = cwd.parent / "alias"
    alias.symlink_to(cwd, target_is_directory=True)
    fake = cwd.parent / "fake"
    fake.mkdir()
    (fake / ".git").write_text((cwd / ".git").read_text())
    paths = {"canonical": repo, "unowned": cwd, "parent": str(cwd) + "/../task",
             "symlink": alias, "missing": cwd.parent / "missing", "spoof": fake}
    if case == "unowned":
        workspace["policy"]["gh_owners"] = ["different-owner"]
        workspace["configure"]()
    assert exempt(launch(workspace, cwd=paths[case])) is None


def test_git_directory_rejected(workspace):
    marker = workspace["cwd"] / ".git"
    marker.unlink()
    marker.mkdir()
    assert exempt(launch(workspace)) is None


def test_other_repo_metadata_rejected(workspace):
    other = workspace["root"] / "yashiels/other/.git/worktrees/task"
    other.mkdir(parents=True)
    (other / "gitdir").write_text(str(workspace["cwd"] / ".git"))
    (workspace["cwd"] / ".git").write_text(f"gitdir: {other}\n")
    assert exempt(launch(workspace)) is None


@pytest.mark.parametrize("target", ["marker", "backref", "common"])
def test_metadata_symlinks_rejected(workspace, target):
    path = {"marker": workspace["cwd"] / ".git", "backref": workspace["metadata"] / "gitdir",
            "common": workspace["metadata"] / "commondir"}[target]
    saved = path.with_name(path.name + "-saved")
    path.rename(saved)
    path.symlink_to(saved)
    assert exempt(launch(workspace)) is None


@pytest.mark.parametrize("target,data", [
    ("commondir", b"/different/repo/.git"), ("gitdir", b"x" * 4097),
    ("marker", b"gitdir: /one\nextra\n"), ("marker", b"gitdir: \xff"),
])
def test_invalid_metadata_rejected(workspace, target, data):
    path = workspace["cwd"] / ".git" if target == "marker" else workspace["metadata"] / target
    path.write_bytes(data)
    assert exempt(launch(workspace)) is None


@pytest.mark.parametrize("case", ["missing", "symlink", "directory", "large"])
def test_brief_rejected(workspace, case):
    brief = workspace["cwd"] / "AGENT-BRIEF.md"
    brief.unlink()
    actions = {"missing": lambda: None, "symlink": lambda: brief.symlink_to(workspace["repo"] / ".git/HEAD"),
               "directory": brief.mkdir, "large": lambda: brief.write_bytes(b"x" * (256 * 1024 + 1))}
    actions[case]()
    assert exempt(launch(workspace)) is None


def test_round_brief_must_exist(workspace):
    assert exempt(launch(workspace, sub=["-s", "worker", "Read AGENT-BRIEF-R2.md in this directory and execute it"])) is None


@pytest.mark.parametrize("agent,models,model", [("codex", [MODEL], MODEL), ("cursor", [MODEL], "unknown"), ("cursor", [], MODEL)])
def test_model_rejected(workspace, agent, models, model):
    workspace["policy"]["acpx"]["cursor_models"] = models
    workspace["configure"]()
    assert exempt(launch(workspace, agent=agent, options=["--model", model])) is None


@pytest.mark.parametrize("value", [None, [], "on", {}, {"agents": "codex"}, {"repo_roots": ["relative"]},
                                  {"prompts": [None]}, {"agents": ["claude"]}, {"cursor_models": [None]},
                                  {"prompt_round_files": 1}, {"repo_roots": None}])
def test_malformed_policy_disables_all_exemptions(workspace, value):
    policy = copy.deepcopy(workspace["policy"])
    policy["acpx"] = {**policy["acpx"], **value} if isinstance(value, dict) and value else value
    workspace["configure"](policy)
    for command in [launch(workspace), GH, "git status", "ssh forge uptime", str(workspace["repo"] / "oracle.sh") + " --dry-run"]:
        assert exempt(command) is None


def test_absent_policy_preserves_existing_exemptions(workspace):
    policy = {key: value for key, value in workspace["policy"].items() if key != "acpx"}
    workspace["configure"](policy)
    chain = launch(workspace, sub=["sessions", "ensure", "--name", "worker-1"]) + " && " + launch(workspace)
    assert exempt(chain) is None
    for command in [GH, "git status", "ssh forge uptime", str(workspace["repo"] / "oracle.sh") + " --dry-run"]:
        assert exempt(command)["approved"] is True


@pytest.mark.parametrize("operator", [";", "||", "|"])
def test_existing_operators_preserved(workspace, operator):
    assert exempt(f"{GH} {operator} git status")["approved"] is True


def test_deny_floor_precedes_acpx(workspace):
    workspace["configure"](deny=["acpx *"])
    result = exempt(launch(workspace))
    assert result["approved"] is False and result["user_deny"] is True


def test_round_policy_is_opt_in(workspace):
    workspace["policy"]["acpx"]["prompt_round_files"] = False
    workspace["configure"]()
    (workspace["cwd"] / "AGENT-BRIEF-R1.md").write_text("task")
    assert exempt(launch(workspace, sub=["-s", "worker", "Read AGENT-BRIEF-R1.md in this directory and execute it"])) is None


@pytest.mark.parametrize("number", [1, 99])
def test_round_boundaries(workspace, number):
    name = f"AGENT-BRIEF-R{number}.md"
    (workspace["cwd"] / name).write_text("task")
    assert exempt(launch(workspace, sub=["-s", "worker", f"Read {name} in this directory and execute it"]))["approved"] is True


@pytest.mark.parametrize("timeout,limit", [("1", "1"), ("86400", "1000"), ("000001", "000002")])
def test_numeric_boundaries(workspace, timeout, limit):
    command = launch(workspace, options=["--timeout", timeout, "--format", "text"],
                     sub=["sessions", "history", "worker", "--limit", limit])
    assert exempt(command)["approved"] is True


def test_metadata_relative_paths_and_optional_commondir(workspace):
    cwd, metadata = workspace["cwd"], workspace["metadata"]
    (cwd / ".git").write_text("gitdir: ../../.git/worktrees/task\n")
    (metadata / "gitdir").write_text("../../../.worktrees/task/.git\n")
    (metadata / "commondir").unlink()
    assert exempt(launch(workspace))["approved"] is True


@pytest.mark.parametrize("section,key", [("acpx", "repo_roots"), ("oracle", "file_roots"), ("oracle", "scripts")])
def test_surrogate_policy_paths_disable_exemptions(workspace, section, key):
    workspace["policy"][section][key] = ["/x/\ud800"]
    workspace["configure"]()
    assert exempt(GH) is None
    assert exempt(launch(workspace)) is None
