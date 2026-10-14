import logging

from agent.redact import redact_sensitive_text

logger = logging.getLogger("tools.approval")


def allows_permanent_approval(pattern_key: str) -> bool:
    return not pattern_key.startswith("plugin_rule:")


def permanent_approval_keys(patterns) -> set[str]:
    allowed = set()
    for pattern in patterns:
        if allows_permanent_approval(pattern):
            allowed.add(pattern)
        else:
            logger.warning("Ignoring permanent approval for plugin rule %r; only once/session approval is supported", pattern)
    return allowed


def format_tool_approval_target(tool_name: str, tool_args: dict | None) -> str:
    args = tool_args or {}
    target = args.get("command") if tool_name == "terminal" else args.get("path") or args.get("target")
    text = f"{tool_name}: {target}" if target is not None else tool_name
    redacted = redact_sensitive_text(text, force=True, redact_url_credentials=True)
    return redacted if len(redacted) <= 2000 else redacted[:1997] + "..."


def plugin_rule_exemption(tool_name: str, tool_args: dict | None, rule_key: str, plugin_name: str = "") -> dict | None:
    from tools import approval, approval_context
    from tools.approval_plugin_commands import command_segments, executable_name, gh_owner, git_local_command

    from tools.approval_plugin_remote import remote_command_owner, remote_exemption_policy

    policy = approval_context._get_approval_config().get("plugin_rule_exemptions", {})
    command = (tool_args or {}).get("command")
    if tool_name != "terminal" or not isinstance(policy, dict) or not isinstance(command, str):
        return None
    blocked = approval._floor_block(command, sudo_guard=True) if policy else None
    if blocked is not None:
        return blocked
    remote_policy = remote_exemption_policy(policy)
    if remote_policy is None:
        return None
    lists = [policy.get(key, []) for key in ("rule_keys", "plugins", "gh_owners")]
    if any(not isinstance(items, list) or any(not isinstance(item, str) for item in items) for items in lists):
        return None
    rules, plugins, owners = lists
    matched_rule = rule_key if rule_key.startswith("plugin_rule:") else f"plugin_rule:{rule_key}"
    if matched_rule != "plugin_rule:terminal" or plugin_name != "jev-judge" or matched_rule not in rules or plugin_name not in plugins:
        return None
    segments = command_segments(command)
    if not segments:
        return None
    if any(argv[0] == "ssh" for argv in segments) and command_segments(command, forbid_pipes=True) is None:
        return None
    matched_owners = set()
    for argv in segments:
        executable = executable_name(argv[0])
        if executable == "gh":
            owner = gh_owner(argv, {owner.lower() for owner in owners})
            if owner is None:
                return None
            matched_owners.add(owner)
        elif executable == "git" and policy.get("git_local") is True and git_local_command(argv):
            matched_owners.add("local")
        else:
            remote_owner = remote_command_owner(argv, remote_policy)
            if remote_owner is None:
                return None
            matched_owners.add(remote_owner)
    logger.info("Plugin-rule exemption: rule=%s owner=%s", matched_rule, ",".join(sorted(matched_owners)))
    return {"approved": True, "message": None}
