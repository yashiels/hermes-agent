from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import sys

import pytest

from gateway.config import PlatformConfig


def _ensure_context_menu_mock() -> None:
    discord_mod = sys.modules["discord"]
    if hasattr(discord_mod, "__file__"):
        return
    if not hasattr(discord_mod.app_commands, "ContextMenu"):
        class _FakeContextMenu:
            def __init__(self, *, name, callback, type=3, **_):
                self.name = name
                self.callback = callback
                self.type = type

            def to_dict(self, _tree=None):
                return {"name": self.name, "type": int(self.type), "description": ""}

        discord_mod.app_commands.ContextMenu = _FakeContextMenu
    discord_mod.AppCommandType = SimpleNamespace(message=3, user=2, chat_input=1)
    discord_mod.Forbidden = type("Forbidden", (Exception,), {})
    discord_mod.NotFound = type("NotFound", (Exception,), {})
    discord_mod.TextStyle = SimpleNamespace(paragraph=2, short=1)
    if not hasattr(discord_mod.ui, "Modal") or not hasattr(discord_mod.ui, "TextInput"):
        class _FakeModal:
            def __init__(self, *, title=None, timeout=None):
                self.title = title
                self.timeout = timeout
                self._children = []

            def add_item(self, item):
                self._children.append(item)

        class _FakeTextInput:
            def __init__(self, *, label=None, style=None, required=True, max_length=None,
                         default=None, **_):
                self.label = label
                self.style = style
                self.required = required
                self.max_length = max_length
                self.value = default

        discord_mod.ui.Modal = _FakeModal
        discord_mod.ui.TextInput = _FakeTextInput


_ensure_context_menu_mock()

import discord  # noqa: E402

import plugins.platforms.discord.context_menus as context_menus  # noqa: E402
from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: E402


async def _async_iter(items):
    for item in items:
        yield item


def _msg(author_name: str, content: str, *, msg_id: int = 1, created_at: int = 0, channel=None):
    return SimpleNamespace(
        id=msg_id, content=content, clean_content=content, attachments=[],
        author=SimpleNamespace(display_name=author_name, name=author_name),
        created_at=created_at, channel=channel,
    )


class _ThreadChannel(discord.Thread):
    def __init__(self, messages, raises=None):
        self._messages = list(messages)
        self._raises = list(raises or [])
        self.calls = []

    def history(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises:
            raise self._raises.pop(0)
        return _async_iter(self._messages)


class _DMChannelFake(discord.DMChannel):
    def __init__(self, messages):
        self._messages = list(messages)
        self.calls = []

    def history(self, **kwargs):
        self.calls.append(kwargs)
        return _async_iter(self._messages)


class _GuildChannel:
    def __init__(self, messages, raises=None):
        self._messages = list(messages)
        self._raises = list(raises or [])
        self.calls = []

    def history(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises:
            raise self._raises.pop(0)
        return _async_iter(self._messages)


class _FakeTree:
    def __init__(self, initial=None):
        self._commands = list(initial or [])

    def get_commands(self):
        return list(self._commands)

    def add_command(self, cmd):
        self._commands.append(cmd)

    def command(self, *, name, description):
        def decorator(fn):
            self._commands.append(SimpleNamespace(name=name, callback=fn))
            return fn
        return decorator


def _interaction(*, channel=None, allowed=True, with_delete=True):
    return SimpleNamespace(
        response=SimpleNamespace(defer=AsyncMock(), send_modal=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
        delete_original_response=AsyncMock() if with_delete else None,
        user=SimpleNamespace(id=1, name="operator"),
        channel=channel, channel_id="chan-1",
    )



@pytest.mark.asyncio
async def test_ask_jarvis_opens_modal_first_without_auth_check():
    adapter = SimpleNamespace(
        _evaluate_slash_authorization=MagicMock(
            side_effect=AssertionError("auth must not run before the modal is sent")
        ),
    )
    interaction = _interaction()
    message = _msg("Bob", "hi there")
    callback = context_menus._make_ask_jarvis_callback(adapter)

    await callback(interaction, message)

    interaction.response.send_modal.assert_awaited_once()
    modal = interaction.response.send_modal.call_args.args[0]
    assert isinstance(modal, context_menus.AskJarvisModal)


@pytest.mark.asyncio
async def test_ask_jarvis_modal_submit_defers_then_authorizes_then_dispatches():
    order = []

    def _auth(_interaction):
        order.append("auth")
        return (True, None)

    async def _defer(**_kwargs):
        order.append("defer")

    captured = {}

    async def _handle_message(event):
        captured["text"] = event

    adapter = SimpleNamespace(
        _evaluate_slash_authorization=_auth,
        _build_slash_event=lambda _interaction, text: text,
        handle_message=_handle_message,
    )
    message = _msg("Carol", "the quoted message")
    modal = context_menus.AskJarvisModal(adapter, message)
    modal.instruction.value = "please explain this"
    interaction = _interaction()
    interaction.response.defer = AsyncMock(side_effect=_defer)

    await modal.on_submit(interaction)

    assert order == ["defer", "auth"]
    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    assert context_menus._UNTRUSTED_HEADER in captured["text"]
    assert context_menus._UNTRUSTED_FOOTER in captured["text"]
    assert "the quoted message" in captured["text"]
    assert "please explain this" in captured["text"]
    interaction.delete_original_response.assert_awaited_once()


@pytest.mark.asyncio
async def test_ask_jarvis_modal_submit_unauthorized_sends_followup_refusal():
    adapter = SimpleNamespace(
        _evaluate_slash_authorization=MagicMock(return_value=(False, "blocked")),
        handle_message=AsyncMock(),
    )
    message = _msg("Carol", "the quoted message")
    modal = context_menus.AskJarvisModal(adapter, message)
    modal.instruction.value = "please explain"
    interaction = _interaction()

    await modal.on_submit(interaction)

    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    interaction.followup.send.assert_awaited_once()
    _args, kwargs = interaction.followup.send.call_args
    assert kwargs.get("ephemeral") is True
    adapter.handle_message.assert_not_awaited()



@pytest.mark.asyncio
async def test_summarize_thread_defers_before_history_fetch():
    order = []

    async def _defer(**_kwargs):
        order.append("defer")

    channel = _ThreadChannel([_msg("Alice", "hello", created_at=1), _msg("Bob", "world", created_at=2)])
    captured = {}

    async def _handle_message(event):
        captured["text"] = event

    adapter = SimpleNamespace(
        _evaluate_slash_authorization=MagicMock(return_value=(True, None)),
        _nonconversational_messages=set(),
        _build_slash_event=lambda _interaction, text: text,
        handle_message=_handle_message,
    )
    interaction = _interaction(channel=channel)
    interaction.response.defer = AsyncMock(side_effect=_defer)
    original_history = channel.history

    def _wrapped_history(**kwargs):
        order.append("history")
        return original_history(**kwargs)
    channel.history = _wrapped_history

    callback = context_menus._make_summarize_thread_callback(adapter)
    await callback(interaction, _msg("Target", "target message"))

    assert order == ["defer", "history"]
    assert "Alice: hello" in captured["text"]
    assert "Bob: world" in captured["text"]
    interaction.delete_original_response.assert_awaited_once()


@pytest.mark.asyncio
async def test_summarize_thread_forbidden_history_sends_ephemeral_notice():
    channel = _ThreadChannel([], raises=[discord.Forbidden("no access")])
    adapter = SimpleNamespace(
        _evaluate_slash_authorization=MagicMock(return_value=(True, None)),
        _nonconversational_messages=set(),
        handle_message=AsyncMock(),
    )
    interaction = _interaction(channel=channel)

    callback = context_menus._make_summarize_thread_callback(adapter)
    await callback(interaction, _msg("Target", "target message"))

    interaction.followup.send.assert_awaited_once_with(context_menus._CANT_READ_HISTORY, ephemeral=True)
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_summarize_thread_deleted_target_falls_back_to_plain_history():
    channel = _GuildChannel(
        [_msg("Alice", "hi", created_at=1)], raises=[discord.NotFound("gone")],
    )
    captured = {}

    async def _handle_message(event):
        captured["text"] = event

    adapter = SimpleNamespace(
        _evaluate_slash_authorization=MagicMock(return_value=(True, None)),
        _nonconversational_messages=set(),
        _build_slash_event=lambda _interaction, text: text,
        handle_message=_handle_message,
    )
    interaction = _interaction(channel=channel)
    target = _msg("Target", "target message")

    callback = context_menus._make_summarize_thread_callback(adapter)
    await callback(interaction, target)

    assert len(channel.calls) == 2
    assert channel.calls[0].get("around") is target
    assert channel.calls[1].get("oldest_first") is True
    assert "Alice: hi" in captured["text"]


@pytest.mark.asyncio
async def test_summarize_thread_dm_uses_oldest_first_history_without_around():
    channel = _DMChannelFake([_msg("Alice", "hi", created_at=1)])
    captured = {}

    async def _handle_message(event):
        captured["text"] = event

    adapter = SimpleNamespace(
        _evaluate_slash_authorization=MagicMock(return_value=(True, None)),
        _nonconversational_messages=set(),
        _build_slash_event=lambda _interaction, text: text,
        handle_message=_handle_message,
    )
    interaction = _interaction(channel=channel)

    callback = context_menus._make_summarize_thread_callback(adapter)
    await callback(interaction, _msg("Target", "target message"))

    assert channel.calls == [{"limit": context_menus.MAX_SUMMARY_MESSAGES, "oldest_first": True}]
    assert "Alice: hi" in captured["text"]


@pytest.mark.asyncio
async def test_summarize_thread_unauthorized_sends_followup_refusal():
    channel = _GuildChannel([_msg("Alice", "hi")])
    adapter = SimpleNamespace(
        _evaluate_slash_authorization=MagicMock(return_value=(False, "blocked")),
        handle_message=AsyncMock(),
    )
    interaction = _interaction(channel=channel)

    callback = context_menus._make_summarize_thread_callback(adapter)
    await callback(interaction, _msg("Target", "target message"))

    interaction.followup.send.assert_awaited_once()
    _args, kwargs = interaction.followup.send.call_args
    assert kwargs.get("ephemeral") is True
    adapter.handle_message.assert_not_awaited()
    assert not channel.calls


@pytest.mark.asyncio
async def test_summarize_thread_skips_bot_tool_noise():
    noisy = _msg("Jarvis", "-# running tool", msg_id=42, created_at=1)
    clean = _msg("Alice", "hello", msg_id=43, created_at=2)
    channel = _ThreadChannel([noisy, clean])
    captured = {}

    async def _handle_message(event):
        captured["text"] = event

    adapter = SimpleNamespace(
        _evaluate_slash_authorization=MagicMock(return_value=(True, None)),
        _nonconversational_messages={"42"},
        _build_slash_event=lambda _interaction, text: text,
        handle_message=_handle_message,
    )
    interaction = _interaction(channel=channel)

    callback = context_menus._make_summarize_thread_callback(adapter)
    await callback(interaction, _msg("Target", "target message"))

    assert "running tool" not in captured["text"]
    assert "Alice: hello" in captured["text"]



def test_register_context_menus_caps_at_five(monkeypatch):
    fake_specs = [(f"Menu {i}", AsyncMock()) for i in range(8)]
    monkeypatch.setattr(context_menus, "_raw_context_menu_specs", lambda _adapter: fake_specs)
    tree = _FakeTree()

    registered = context_menus.register_context_menus(SimpleNamespace(), tree)

    assert registered == context_menus.MAX_MESSAGE_CONTEXT_MENUS
    assert len(tree.get_commands()) == context_menus.MAX_MESSAGE_CONTEXT_MENUS


def test_register_context_menus_respects_total_command_cap():
    tree = _FakeTree(initial=[SimpleNamespace(name=f"existing{i}") for i in range(99)])

    registered = context_menus.register_context_menus(SimpleNamespace(), tree)

    assert registered == 1
    assert len(tree.get_commands()) == 100


def test_message_context_menu_count_matches_registered_specs():
    assert context_menus.message_context_menu_count() == 2



def _build_registered_adapter(context_menus_enabled: bool, fake_registry, monkeypatch):
    import hermes_cli.commands as hermes_commands
    import plugins.platforms.discord.adapter as discord_adapter

    monkeypatch.setattr(hermes_commands, "COMMAND_REGISTRY", fake_registry)
    monkeypatch.setattr(discord_adapter, "_native_slash_commands", lambda: [])
    adapter = DiscordAdapter(
        PlatformConfig(enabled=True, token="***", extra={"context_menus": context_menus_enabled})
    )
    adapter._client = SimpleNamespace(tree=_FakeTree())
    adapter._register_skill_group = lambda _tree: None
    adapter._register_slash_commands()
    return adapter


def test_slot_reservation_accounts_for_context_menus(monkeypatch):
    import hermes_cli.commands as hermes_commands

    fake_registry = [
        hermes_commands.CommandDef(name=f"autocmd{i:03d}", description="d", category="Test")
        for i in range(100)
    ]

    adapter_off = _build_registered_adapter(False, fake_registry, monkeypatch)
    adapter_on = _build_registered_adapter(True, fake_registry, monkeypatch)

    auto_off = [c for c in adapter_off._client.tree.get_commands() if c.name.startswith("autocmd")]
    auto_on = [c for c in adapter_on._client.tree.get_commands() if c.name.startswith("autocmd")]
    assert len(auto_off) == 99
    assert len(auto_on) == 97

    on_names = {c.name for c in adapter_on._client.tree.get_commands()}
    assert {context_menus.ASK_JARVIS_NAME, context_menus.SUMMARIZE_THREAD_NAME} <= on_names


@pytest.mark.asyncio
async def test_second_safe_sync_reports_unchanged(monkeypatch):
    import plugins.platforms.discord.adapter as discord_adapter

    monkeypatch.setattr(discord_adapter, "_native_slash_commands", lambda: [])
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***", extra={"context_menus": True}))
    tree = _FakeTree()
    http = SimpleNamespace()
    adapter._client = SimpleNamespace(tree=tree, http=http, application_id="app-1")
    adapter._sleep_between_command_sync_mutations = AsyncMock()

    registered = context_menus.register_context_menus(adapter, tree)
    assert registered == 2

    store: dict = {}

    async def _upsert(_app_id, payload):
        record = dict(payload)
        record["id"] = f"id-{payload['name']}"
        store[payload["name"]] = record
        return record

    http.upsert_global_command = AsyncMock(side_effect=_upsert)
    http.delete_global_command = AsyncMock()
    http.edit_global_command = AsyncMock()
    http.get_global_commands = AsyncMock(return_value=[])

    first = await adapter._safe_sync_slash_commands()
    assert first["created"] == 2
    assert first["unchanged"] == 0

    http.get_global_commands = AsyncMock(return_value=list(store.values()))
    second = await adapter._safe_sync_slash_commands()

    assert second["unchanged"] == 2
    assert second["created"] == 0
    assert second["updated"] == 0
    assert second["recreated"] == 0
    assert second["deleted"] == 0
