import json
import shlex
from pathlib import Path

import pytest

from hermes_cli import config, plugins
from tools import approval, approval_context


GH = "gh issue view 477 -R yashiels/agent-scripts"
SSH_HOSTS = ["forge", "atlas", "launchpad", "apollo", "iss"]


@pytest.fixture(autouse=True)
def remote_policy(tmp_path, monkeypatch):
    home = tmp_path / "owner"
    repo = home / "Developer/github/yashiels/agent-scripts"
    scratch = home / ".hermes/profiles/jarvis/cache/scratch"
    scripts = [repo / "skills/plan-forge/scripts/oracle-gate.sh", repo / "skills/oracle/scripts/oracle-ask.sh"]
    for script in scripts:
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("unused")
    scratch.mkdir(parents=True)
    policy = {"rule_keys": ["plugin_rule:terminal"], "plugins": ["jev-judge"], "gh_owners": ["yashiels"],
              "git_local": True, "ssh_hosts": SSH_HOSTS,
              "oracle": {"scripts": [str(script) for script in scripts],
                         "file_roots": [str(home / "Developer/github"), str(scratch)]}}

    def configure(value=None, deny=()):
        (tmp_path / "config.yaml").write_text(json.dumps({
            "approvals": {"mode": "manual", "plugin_rule_exemptions": policy if value is None else value, "deny": list(deny)},
        }))
        config._LOAD_CONFIG_CACHE.clear()

    configure()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    for key in ("HERMES_EXEC_ASK", "HERMES_GATEWAY_SESSION", "HERMES_SESSION_PLATFORM", "GH_HOST", "GH_REPO"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(approval, "_session_approved", {})
    manager = plugins.PluginManager()
    plugins.PluginContext(plugins.PluginManifest(name="jev-judge"), manager).register_hook(
        "pre_tool_call", lambda **kw: {"action": "approve", "message": "review"})
    monkeypatch.setattr(plugins, "invoke_hook", manager.invoke_hook)
    seen = []
    monkeypatch.setattr("tools.terminal_tool._get_approval_callback", lambda: lambda *a, **kw: seen.append(a) or "deny")
    token = approval_context.set_current_session_key("remote-exemption-test")
    yield {"home": home, "repo": repo, "scratch": scratch, "scripts": scripts, "policy": policy,
           "configure": configure, "seen": seen, "manager": manager}
    approval_context.reset_current_session_key(token)
    config._LOAD_CONFIG_CACHE.clear()


def approve(command):
    return plugins.resolve_pre_tool_block("terminal", {"command": command})


@pytest.mark.parametrize("command", [
    "ssh forge 'docker ps'", "ssh -o BatchMode=yes atlas uptime", "ssh launchpad", "ssh apollo uptime", "ssh iss uptime",
    "ssh -T -n -q -oBatchMode=yes -oConnectTimeout=10 atlas uptime",
    "ssh -o ConnectTimeout=0 forge uptime", "ssh forge 'docker ps > /tmp/list | cat &'",
    "ssh forge 'echo $(date)'", "ssh forge 'echo `date`'", "ssh forge uptime && ssh atlas uptime",
    "ssh forge uptime || ssh atlas uptime", "ssh forge uptime; " + GH, "ssh\tforge\tuptime",
])
@pytest.mark.parametrize("surface", ["chat", "cron"])
def test_ssh_exempts_only_exact_hosts_and_options(remote_policy, monkeypatch, command, surface):
    if surface == "cron":
        monkeypatch.delenv("HERMES_INTERACTIVE")
        monkeypatch.setenv("HERMES_SESSION_PLATFORM", "cron")
    assert approve(command) is None
    assert remote_policy["seen"] == []


@pytest.mark.parametrize("command", [
    "ssh orion uptime", "ssh nebula uptime", "ssh orion-opd uptime", "ssh forge-docker uptime",
    "ssh user@forge uptime", "ssh forge:22 uptime", "ssh ssh://forge uptime", "ssh FORGE uptime",
    "ssh forge.evil uptime", "ssh -- forge uptime", "/usr/bin/ssh forge uptime", "ssh",
    "ssh -o BatchMode=no forge uptime", "ssh -o batchmode=yes forge uptime", "ssh -o BatchMode=YES forge uptime",
    "ssh -o ConnectTimeout=-1 forge uptime", "ssh -o ConnectTimeout=1.5 forge uptime",
    "ssh -o ConnectTimeout= forge uptime", "ssh -o ProxyCommand=evil forge uptime",
    "ssh -oProxyJump=orion forge uptime", "ssh -oLocalCommand=evil forge uptime",
    "ssh -oPermitLocalCommand=yes forge uptime", "ssh -oRemoteCommand=evil forge uptime",
    "ssh -oInclude=/tmp/config forge uptime", "ssh -oIdentityFile=/tmp/key forge uptime",
    "ssh forge uptime -L123:host:123", "ssh forge echo -oProxyCommand=evil",
    "ssh -o=BatchMode=yes forge uptime", "ssh -o", "ssh -o BatchMode=yes", "ssh forge -oProxyCommand=evil",
    "ssh forge uptime\n" + GH,
    "ssh forge x > f", "ssh forge < f", "ssh forge $(cat f)", "ssh forge `cat f`",
    'ssh forge "echo $(cat f)"', "ssh forge <(cat f)", "ssh forge x &",
    "ssh forge x | python3", "ssh forge x | ssh atlas uptime", "ssh forge x | " + GH,
    "FOO=1 ssh forge", "env ssh forge uptime", "ssh forge x; echo no", "ssh forge 'unterminated",
    "ssh forge x &&", "ssh forge x ;; ssh atlas x",
])
@pytest.mark.parametrize("surface", ["chat", "cron"])
def test_ssh_rejects_local_shell_and_target_bypasses(monkeypatch, command, surface):
    if surface == "cron":
        monkeypatch.delenv("HERMES_INTERACTIVE")
        monkeypatch.setenv("HERMES_SESSION_PLATFORM", "cron")
    assert approve(command) is not None


@pytest.mark.parametrize("option", ["-L", "-R", "-D", "-W", "-J", "-F", "-i", "-A", "-X", "-Y", "-f", "-N", "-M", "-S", "-E", "-l", "-p"])
def test_ssh_forbidden_options_never_exempt(option):
    assert approve(f"ssh {option} forge uptime") is not None
    assert approve(f"ssh {option}payload forge uptime") is not None


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize("surface", ["chat", "cron"])
def test_real_oracle_invocation_forms_pass(remote_policy, monkeypatch, index, surface):
    if surface == "cron":
        monkeypatch.delenv("HERMES_INTERACTIVE")
        monkeypatch.setenv("HERMES_SESSION_PLATFORM", "cron")
    script = str(remote_policy["scripts"][index]).replace(str(remote_policy["home"]), "~", 1)
    repo_file = "~/Developer/github/yashiels/.fork/photoscrawl/.worktrees/remote-model/docs/remote-model-plan.md"
    command = shlex.join([script, "-p", str(remote_policy["scratch"] / "photoscrawl-plan-review-r2.md"),
                          "--model", "cursor/openai/gpt-5.6-terra", "--effort", "medium", "--file", repo_file,
                          "--file", str(remote_policy["repo"] / "docs/other-plan.md"),
                          "--log", str(remote_policy["scratch"] / "review-r2.log"), "--dry-run"])
    assert approve(command) is None
    assert remote_policy["seen"] == []


@pytest.mark.parametrize("options", ["--effort low", "--effort medium", "--effort high", "--dry-run", "-l /tmp/oracle-review.log"])
def test_oracle_allowed_options(remote_policy, options):
    assert approve(f"{remote_policy['scripts'][0]} {options}") is None


@pytest.mark.parametrize("relative_path", [
    ".ssh/id_ed25519", ".gnupg/public.txt", ".aws/notes.txt", ".iai-mcp/records.json", ".config/data.txt",
    ".hermes/config.yaml", ".env", ".env.local", ".ENV.local", "server.pem", "server.key", "server.p12",
    "id_private", "my-secret.txt", "access-token.txt", "credentials.txt", ".netrc", ".npmrc", ".pypirc",
    "nested/.ssh/notes.md", "nested/.hermes/notes.md", "nested/.CONFIG/notes.md", "**", "?.md", "[ab].md", "{a,b}.md",
])
@pytest.mark.parametrize("option", ["--file", "--prompt", "--log"])
def test_oracle_sensitive_and_glob_paths_do_not_qualify(remote_policy, relative_path, option):
    path = remote_policy["scratch"] / relative_path
    command = shlex.join([str(remote_policy["scripts"][0]), option, str(path)])
    assert approve(command) is not None


@pytest.mark.parametrize("option,value", [
    ("--unknown", "value"), ("--effort", "max"), ("--effort", "HIGH"), ("--model", "@file"),
    ("--model", "bad id"), ("--file", "!/tmp/input"), ("--file", "~/.ssh/id_ed25519"),
    ("--log", "~/.zshrc"), ("--file", "/outside-roots/plan.md"),
])
def test_oracle_unsupported_options_and_outside_paths(remote_policy, option, value):
    assert approve(shlex.join([str(remote_policy["scripts"][0]), option, value])) is not None


def test_oracle_repo_env_and_non_scratch_log_paths_rejected(remote_policy):
    script = str(remote_policy["scripts"][0])
    assert approve(shlex.join([script, "--file", str(remote_policy["repo"] / ".env")])) is not None
    assert approve(shlex.join([script, "-l", str(remote_policy["repo"] / "review.log")])) is not None


def test_oracle_sensitive_symlink_and_script_copy_rejected(remote_policy):
    home, scratch, script = remote_policy["home"], remote_policy["scratch"], remote_policy["scripts"][0]
    sensitive = home / ".ssh"
    sensitive.mkdir()
    (sensitive / "id_ed25519").write_text("synthetic")
    alias = scratch / "public-files"
    alias.symlink_to(sensitive, target_is_directory=True)
    assert approve(shlex.join([str(script), "--file", str(alias / "id_ed25519")])) is not None
    copy = scratch / "oracle-gate.sh"
    copy.write_text(script.read_text())
    assert approve(shlex.join([str(copy), "--dry-run"])) is not None
    assert approve(shlex.join(["bash", str(script), "--dry-run"])) is not None


def test_oracle_safe_symlinks_and_nonexistent_paths_pass(remote_policy):
    scratch, repo, script = remote_policy["scratch"], remote_policy["repo"], remote_policy["scripts"][0]
    plan = repo / "plan.md"
    plan.write_text("synthetic plan")
    alias = scratch / "plan.md"
    alias.symlink_to(plan)
    script_alias = scratch / "oracle-gate.sh"
    script_alias.symlink_to(script)
    assert approve(shlex.join([str(script_alias), "--file", str(alias), "--prompt", str(scratch / "missing/plan.md")])) is None


@pytest.mark.parametrize("hosts", [["forge", "orion"], ["nebula"], ["Orion"], ["forge.evil"], ["user@forge"], ["forge:22"], ["ssh://forge"], ["forge x"], [""], [None], "forge", None])
def test_invalid_host_schema_disables_all_exemptions(remote_policy, hosts):
    remote_policy["configure"]({**remote_policy["policy"], "ssh_hosts": hosts})
    assert approve(GH) is not None
    assert approve("git status") is not None
    assert approve("ssh forge uptime") is not None


@pytest.mark.parametrize("key,value", [("scripts", ["relative.sh"]), ("scripts", ["~/oracle.sh"]), ("scripts", "x"), ("scripts", [None]),
                                      ("file_roots", ["relative"]), ("file_roots", ["~/Developer/github"]), ("file_roots", None)])
def test_non_absolute_oracle_schema_disables_all_exemptions(remote_policy, key, value):
    oracle = {**remote_policy["policy"]["oracle"], key: value}
    remote_policy["configure"]({**remote_policy["policy"], "oracle": oracle})
    assert approve(GH) is not None
    assert approve("ssh forge uptime") is not None


def test_missing_additions_are_off_without_disabling_existing_policy(remote_policy):
    policy = {key: value for key, value in remote_policy["policy"].items() if key not in {"ssh_hosts", "oracle"}}
    remote_policy["configure"](policy)
    assert approve(GH) is None
    assert approve("ssh forge uptime") is not None
    assert approve(f"{remote_policy['scripts'][0]} --dry-run") is not None


@pytest.mark.parametrize("plugin_name,rule", [("other-policy", "terminal"), ("jev-judge", "other-rule")])
def test_remote_exemptions_remain_jev_terminal_only(remote_policy, plugin_name, rule):
    manager = remote_policy["manager"]
    manager._hooks.clear()
    plugins.PluginContext(plugins.PluginManifest(name=plugin_name), manager).register_hook(
        "pre_tool_call", lambda **kw: {"action": "approve", "rule_key": rule})
    remote_policy["configure"]({**remote_policy["policy"], "plugins": [plugin_name], "rule_keys": [f"plugin_rule:{rule}"]})
    assert approve(GH) is not None
    assert approve("ssh forge uptime") is not None
    assert approve(f"{remote_policy['scripts'][0]} --dry-run") is not None


def test_remote_exemptions_cannot_bypass_deny_rules(remote_policy):
    remote_policy["configure"](deny=["ssh forge*", "*oracle-gate.sh*"])
    assert approve("ssh forge uptime") is not None
    assert approve(f"{remote_policy['scripts'][0]} --dry-run") is not None
    assert remote_policy["seen"] == []


def test_oracle_symlinks_to_sensitive_files_inside_root_are_rejected(remote_policy):
    repo, scratch, script = remote_policy["repo"], remote_policy["scratch"], remote_policy["scripts"][0]
    target = repo / "credentials.json"
    target.write_text("synthetic")
    alias = scratch / "plan.md"
    alias.symlink_to(target)
    assert approve(shlex.join([str(script), "--file", str(alias)])) is not None


def test_scratch_root_symlink_keeps_its_canonical_log_boundary(remote_policy, tmp_path):
    scratch, script = remote_policy["scratch"], remote_policy["scripts"][0]
    physical = tmp_path / "physical-scratch"
    physical.mkdir()
    scratch.rmdir()
    scratch.symlink_to(physical, target_is_directory=True)
    assert approve(shlex.join([str(script), "--prompt", str(scratch / "plan.md"), "--log", str(scratch / "review.log")])) is None


def test_scratch_exception_does_not_allow_other_hermes_ancestors(remote_policy):
    root = "/virtual/.hermes/old/.hermes/profiles/jarvis/cache/scratch"
    oracle = {**remote_policy["policy"]["oracle"], "file_roots": [root]}
    remote_policy["configure"]({**remote_policy["policy"], "oracle": oracle})
    assert approve(shlex.join([str(remote_policy["scripts"][0]), "--prompt", root + "/plan.md"])) is not None


def test_environment_assignment_cannot_masquerade_as_oracle_script(remote_policy, monkeypatch):
    script = remote_policy["repo"] / "FOO=1"
    script.write_text("unused")
    oracle = {**remote_policy["policy"]["oracle"], "scripts": [str(script)]}
    remote_policy["configure"]({**remote_policy["policy"], "oracle": oracle})
    monkeypatch.chdir(remote_policy["repo"])
    assert approve("FOO=1 --dry-run") is not None
    assert approve(shlex.join([str(script), "--dry-run"])) is None


def test_cron_denies_non_exempt_oracle_without_calling_human(remote_policy, monkeypatch):
    monkeypatch.delenv("HERMES_INTERACTIVE")
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "cron")
    seen = []
    monkeypatch.setattr("tools.terminal_tool._get_approval_callback", lambda: lambda *a, **kw: seen.append(a) or "once")
    assert approve(shlex.join([str(remote_policy["scripts"][0]), "--file", "~/.ssh/id_ed25519"])) is not None
    assert seen == []


def test_hard_floors_precede_ssh_exemption(remote_policy):
    assert approve("ssh forge uptime; reboot") is not None
    assert remote_policy["seen"] == []


def test_ssh_nul_argument_is_never_exempt():
    assert approve("ssh forge 'malformed\x00argument'") is not None


@pytest.mark.parametrize("validator", ["gh", "git", "ssh", "oracle"])
@pytest.mark.parametrize("control", ["\x00", "\x01", "\n", "\r", "\x1b", "\x7f"])
def test_control_bytes_are_denied_for_every_validator(remote_policy, validator, control):
    commands = {
        "gh": shlex.join(["gh", "issue", "create", "-R", "yashiels/x", "--title", "title" + control + "text"]),
        "git": shlex.join(["git", "-C", str(remote_policy["repo"]) + control, "status"]),
        "ssh": shlex.join(["ssh", "forge", "echo" + control + "argument"]),
        "oracle": shlex.join([str(remote_policy["scripts"][0]), "--file", str(remote_policy["scratch"] / ("plan" + control + ".md"))]),
    }
    assert approve(commands[validator]) is not None


@pytest.mark.parametrize("validator", ["gh", "git", "ssh", "oracle"])
def test_tabs_remain_valid_for_every_validator(remote_policy, validator):
    commands = {
        "gh": GH.replace(" ", "\t"),
        "git": "git\tstatus",
        "ssh": "ssh\tforge\tuptime",
        "oracle": str(remote_policy["scripts"][0]) + "\t--dry-run",
    }
    assert approve(commands[validator]) is None
