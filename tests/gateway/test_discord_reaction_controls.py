import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import PlatformConfig

import plugins.platforms.discord.adapter as discord_platform  # noqa: E402
from plugins.platforms.discord.adapter import DiscordAdapter, ExecApprovalView  # noqa: E402
from plugins.platforms.discord.reaction_controls import (  # noqa: E402
    EMOJI_APPROVE,
    handle_raw_reaction_add,
)


class FakeBot:
    def __init__(self, *, intents, proxy=None, allowed_mentions=None, **_):
        self.intents = intents
        self.user = SimpleNamespace(id=999, name="Hermes")
        self._events = {}

    def event(self, fn):
        self._events[fn.__name__] = fn
        return fn

    def add_dynamic_items(self, *_a, **_k):
        return None

    async def start(self, token):
        if "on_ready" in self._events:
            await self._events["on_ready"]()

    async def close(self):
        return None


class FakeEmbed:
    def __init__(self):
        self.color = None
        self.footer = None

    def set_footer(self, *, text):
        self.footer = text


class FakeMessage:
    def __init__(self, message_id="m1"):
        self.id = message_id
        self.embeds = [FakeEmbed()]
        self.edits = []
        self.reactions = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)


class FakeChannel:
    def __init__(self, message=None):
        self.message = message or FakeMessage()

    async def send(self, **kwargs):
        return self.message


def _run_metadata(generation=1, nonce="0123456789abcdef", session_key="session-1"):
    return {"hermes_run": {"session_key": session_key, "generation": generation, "nonce": nonce}}


def _make_adapter(*, reaction_controls=True):
    adapter = DiscordAdapter(PlatformConfig(
        enabled=True, token="x", extra={"reaction_controls": reaction_controls},
    ))
    adapter._client = SimpleNamespace(get_channel=lambda _cid: FakeChannel(), fetch_channel=AsyncMock())
    return adapter


@pytest.mark.asyncio
async def test_connect_registers_raw_reaction_handler(monkeypatch):
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="t", extra={"reaction_controls": True}))
    adapter._slash_commands = False
    monkeypatch.setattr("gateway.status.acquire_scoped_lock", lambda *a, **k: (True, None))
    monkeypatch.setattr("gateway.status.release_scoped_lock", lambda *a, **k: None)
    intents = SimpleNamespace(
        message_content=False, dm_messages=False, guild_messages=False, members=False, voice_states=False,
    )
    monkeypatch.setattr(discord_platform.Intents, "default", lambda: intents)
    monkeypatch.setattr(discord_platform.commands, "Bot", FakeBot)
    monkeypatch.setattr(adapter, "_resolve_allowed_usernames", AsyncMock())

    assert await adapter.connect() is True
    assert "on_raw_reaction_add" in adapter._client._events

    payload = SimpleNamespace(user_id=1, message_id="missing", emoji=EMOJI_APPROVE, member=None, guild_id=None)
    await adapter._client._events["on_raw_reaction_add"](payload)

    await adapter.disconnect()


@pytest.mark.asyncio
async def test_send_registers_run_message_when_enabled_and_hermes_run_present():
    adapter = _make_adapter(reaction_controls=True)
    channel = FakeChannel(FakeMessage("turn-1"))
    adapter._client.get_channel = lambda _cid: channel

    result = await adapter.send("123", "hello", metadata=_run_metadata(session_key="s1", generation=2))

    assert result.success is True
    entry = adapter._reaction_registry.get("turn-1")
    assert entry == {"kind": "run", "session_key": "s1", "generation": 2, "nonce": "0123456789abcdef"}


@pytest.mark.asyncio
async def test_send_does_not_register_when_flag_disabled():
    adapter = _make_adapter(reaction_controls=False)
    channel = FakeChannel(FakeMessage("turn-2"))
    adapter._client.get_channel = lambda _cid: channel

    await adapter.send("123", "hello", metadata=_run_metadata())

    assert adapter._reaction_registry.get("turn-2") is None


@pytest.mark.asyncio
async def test_send_does_not_register_without_hermes_run():
    adapter = _make_adapter(reaction_controls=True)
    channel = FakeChannel(FakeMessage("turn-3"))
    adapter._client.get_channel = lambda _cid: channel

    await adapter.send("123", "hello")

    assert adapter._reaction_registry.get("turn-3") is None


@pytest.mark.asyncio
async def test_send_exec_approval_prompt_registers_approval_and_hint_reactions():
    adapter = _make_adapter(reaction_controls=True)
    message = FakeMessage("approval-1")
    channel = FakeChannel(message)
    adapter._client.get_channel = lambda _cid: channel

    result = await adapter.send_exec_approval(
        chat_id="555", command="rm -rf /tmp/x", session_key="discord:555",
    )

    assert result.success is True
    entry = adapter._reaction_registry.get("approval-1")
    assert entry is not None
    assert entry["kind"] == "approval"
    assert entry["session_key"] == "discord:555"
    assert message.reactions == ["✅", "❌"]


@pytest.mark.asyncio
async def test_send_exec_approval_prompt_skips_registration_when_flag_disabled():
    adapter = _make_adapter(reaction_controls=False)
    message = FakeMessage("approval-2")
    channel = FakeChannel(message)
    adapter._client.get_channel = lambda _cid: channel

    await adapter.send_exec_approval(chat_id="555", command="ls", session_key="discord:555")

    assert adapter._reaction_registry.get("approval-2") is None
    assert message.reactions == []


@pytest.mark.asyncio
async def test_on_turn_end_clears_run_registry_entries():
    adapter = _make_adapter(reaction_controls=True)
    adapter._reaction_registry.register_run_message(
        "turn-4", session_key="s1", generation=5, nonce="0123456789abcdef",
    )

    await adapter.on_turn_end("chat-1", metadata=_run_metadata(session_key="s1", generation=5), outcome="done")

    assert adapter._reaction_registry.get("turn-4") is None


def _interaction(user_id=42, display_name="alice"):
    embed = FakeEmbed()
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id, display_name=display_name, roles=[]),
        response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()),
        message=SimpleNamespace(embeds=[embed]),
    ), embed


@pytest.mark.asyncio
async def test_button_and_reaction_parity_for_approval_once():
    callback_calls = {"button": 0, "reaction": 0}

    async def button_cb():
        callback_calls["button"] += 1

    async def reaction_cb():
        callback_calls["reaction"] += 1

    button_view = ExecApprovalView(
        session_key="button-session", allowed_user_ids={"42"},
        approval_state_callback=button_cb,
    )
    interaction, button_embed = _interaction()

    adapter = _make_adapter(reaction_controls=True)
    adapter._allowed_user_ids = {"42"}
    reaction_message = FakeMessage("reaction-msg")
    reaction_view = ExecApprovalView(
        session_key="reaction-session", allowed_user_ids={"42"},
        approval_state_callback=reaction_cb,
    )
    adapter._reaction_registry.register_approval(
        "reaction-msg", session_key="reaction-session", require_admin=False, admin_user_ids=set(),
        expires_at=time.time() + 60, message=reaction_message, view=reaction_view,
    )

    reactor = SimpleNamespace(roles=[], display_name="alice")
    with patch("tools.approval.resolve_gateway_approval", return_value=1):
        await button_view._resolve(interaction, "once")
        payload = SimpleNamespace(user_id=42, message_id="reaction-msg", emoji=EMOJI_APPROVE, member=reactor, guild_id=None)
        await handle_raw_reaction_add(adapter, payload)

    reaction_embed = reaction_message.embeds[0]
    assert button_view.resolved is True
    assert reaction_view.resolved is True
    assert button_embed.color == reaction_embed.color
    assert button_embed.footer == reaction_embed.footer
    assert callback_calls == {"button": 1, "reaction": 0}
    assert adapter._reaction_registry.get("reaction-msg") is None


@pytest.mark.asyncio
async def test_button_expired_elsewhere_sends_ephemeral_without_editing():
    callback_calls = {"n": 0}

    async def cb():
        callback_calls["n"] += 1

    view = ExecApprovalView(session_key="s", allowed_user_ids={"42"}, approval_state_callback=cb)
    interaction, embed = _interaction()

    with patch("tools.approval.resolve_gateway_approval", return_value=0):
        await view._resolve(interaction, "once")

    interaction.response.edit_message.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once()
    assert callback_calls == {"n": 0}
