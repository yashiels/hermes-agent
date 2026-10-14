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
