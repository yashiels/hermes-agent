from dataclasses import dataclass
from functools import wraps


class PluginHookResult(dict):
    def __init__(self, result: dict, plugin_name: str):
        super().__init__(result)
        self.plugin_name = plugin_name


def bind_plugin_hook(callback, plugin_name: str):
    @wraps(callback)
    def bound(*args, **kwargs):
        return callback(*args, **kwargs)

    bound._hermes_plugin_name = plugin_name
    return bound


def attributed_hook_result(hook_name: str, result, callback):
    if hook_name == "pre_tool_call" and isinstance(result, dict):
        return PluginHookResult(result, getattr(callback, "_hermes_plugin_name", ""))
    return result


@dataclass(frozen=True)
class PreToolCallDirective:
    action: str | None = None
    message: str | None = None
    rule_key: str | None = None
    plugin_name: str = ""
    modified_args: dict | None = None
