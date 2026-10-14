import pytest

from hermes_cli import plugins


@pytest.mark.parametrize("async_dispatch", [False, True])
@pytest.mark.parametrize("async_callback", [False, True])
@pytest.mark.asyncio
async def test_hook_provenance_is_registered_identity_and_disposes(monkeypatch, async_dispatch, async_callback):
    monkeypatch.setattr(plugins, "_resolve_hook_callback_timeout", lambda: 0)
    manager = plugins.PluginManager()
    context = plugins.PluginContext(plugins.PluginManifest(name="independent-policy"), manager)
    returned = {"action": "approve", "plugin_name": "jev-judge"}

    def callback(**kwargs):
        return returned

    async def async_hook(**kwargs):
        return returned

    selected = async_hook if async_callback else callback
    selected._hermes_plugin_name = "jev-judge"
    handle = context.register_hook("pre_tool_call", selected)
    results = await manager.ainvoke_hook("pre_tool_call") if async_dispatch else manager.invoke_hook("pre_tool_call")
    assert results == [returned]
    assert results[0].plugin_name == "independent-policy"
    assert not hasattr(returned, "plugin_name")
    handle.dispose()
    assert manager.invoke_hook("pre_tool_call") == []


def test_unregistered_shell_hook_cannot_claim_plugin_identity(monkeypatch):
    monkeypatch.setattr(plugins, "_resolve_hook_callback_timeout", lambda: 0)
    manager = plugins.PluginManager()
    manager._hooks["pre_tool_call"] = [lambda **kwargs: {"action": "approve", "plugin_name": "jev-judge"}]
    result = manager.invoke_hook("pre_tool_call")[0]
    assert result.plugin_name == ""
