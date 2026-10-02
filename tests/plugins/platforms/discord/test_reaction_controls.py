import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from tests.gateway import conftest as gateway_conftest

gateway_conftest._ensure_discord_mock()

from plugins.platforms.discord.reaction_controls import (
    EMOJI_APPROVE,
    EMOJI_DENY,
    EMOJI_RETRY,
    EMOJI_STOP,
    ReactionControlRegistry,
    add_approval_hint_reactions,
    handle_raw_reaction_add,
    resolve_approval_prompt,
)


def _payload(user_id=42, message_id="m1", emoji=EMOJI_APPROVE, member=None, guild_id=None):
    return SimpleNamespace(user_id=user_id, message_id=message_id, emoji=emoji, member=member, guild_id=guild_id)


class FakeEmbed:
    def __init__(self):
        self.color = None
        self.footer = None

    def set_footer(self, *, text):
        self.footer = text


class FakeMessage:
    def __init__(self):
        self.embeds = [FakeEmbed()]
        self.edit = AsyncMock()
        self.add_reaction = AsyncMock()


class FakeView:
    def __init__(self, approval_state_callback=None):
        self.resolved = False
        self.children = [SimpleNamespace(disabled=False)]
        self.approval_state_callback = approval_state_callback

    def _disable_all(self):
        for child in self.children:
            child.disabled = True


class FakeAdapter:
    def __init__(self, *, reaction_controls=True):
        self._client = SimpleNamespace(user=SimpleNamespace(id=999))
        self._allowed_user_ids = {"42"}
        self._allowed_role_ids = set()
        self._reaction_registry = ReactionControlRegistry()
        self._gateway_controls = None
        self._gateway_controls_warning_emitted = False
        self._last_final_messages = {}
        self._reaction_controls = reaction_controls

    def reaction_controls_enabled(self):
        return self._reaction_controls


def test_registry_lru_evicts_oldest():
    registry = ReactionControlRegistry()
    for i in range(registry.MAX_ACTIVE + 5):
        registry.register_run_message(str(i), session_key="s", generation=1, nonce="n")
    assert registry.get("0") is None
    assert registry.get(str(registry.MAX_ACTIVE + 4)) is not None
    assert len(registry._active) == registry.MAX_ACTIVE


def test_registry_approval_expires_lazily():
    registry = ReactionControlRegistry()
    registry.register_approval(
        "m1", session_key="s", require_admin=False, admin_user_ids=set(),
        expires_at=time.time() - 1, message=FakeMessage(), view=FakeView(),
    )
    assert registry.get("m1") is None


def test_registry_clear_run_only_matches_session_and_generation():
    registry = ReactionControlRegistry()
    registry.register_run_message("a", session_key="s1", generation=1, nonce="n")
    registry.register_run_message("b", session_key="s1", generation=2, nonce="n")
    registry.register_run_message("c", session_key="s2", generation=1, nonce="n")
    registry.clear_run("s1", 1)
    assert registry.get("a") is None
    assert registry.get("b") is not None
    assert registry.get("c") is not None


@pytest.mark.asyncio
async def test_resolve_approval_prompt_finalizes_and_calls_state_callback():
    callback_calls = {"n": 0}

    async def cb():
        callback_calls["n"] += 1

    view = FakeView(approval_state_callback=cb)
    finalized = {}

    async def finalize(color, footer):
        finalized["color"] = color
        finalized["footer"] = footer

    with patch("tools.approval.resolve_gateway_approval", return_value=1):
        count = await resolve_approval_prompt("session-1", "once", "alice", view=view, finalize=finalize)
    assert count == 1
    assert "alice" in finalized["footer"]
    assert callback_calls == {"n": 1}


@pytest.mark.asyncio
async def test_resolve_approval_prompt_already_resolved_skips_finalize_and_callback():
    callback_calls = {"n": 0}

    async def cb():
        callback_calls["n"] += 1

    view = FakeView(approval_state_callback=cb)
    finalize = AsyncMock()

    with patch("tools.approval.resolve_gateway_approval", return_value=0):
        count = await resolve_approval_prompt("session-1", "once", "alice", view=view, finalize=finalize)
    assert count == 0
    finalize.assert_not_awaited()
    assert callback_calls == {"n": 0}


@pytest.mark.asyncio
async def test_resolve_approval_prompt_calls_callback_even_when_finalize_raises():
    callback_calls = {"n": 0}

    async def cb():
        callback_calls["n"] += 1

    view = FakeView(approval_state_callback=cb)

    async def finalize(color, footer):
        raise RuntimeError("boom")

    with patch("tools.approval.resolve_gateway_approval", return_value=1):
        with pytest.raises(RuntimeError):
            await resolve_approval_prompt("session-1", "once", "alice", view=view, finalize=finalize)
    assert callback_calls == {"n": 1}


@pytest.mark.asyncio
async def test_bot_own_reaction_ignored():
    adapter = FakeAdapter()
    message = FakeMessage()
    view = FakeView()
    adapter._reaction_registry.register_approval(
        "m1", session_key="s", require_admin=False, admin_user_ids=set(),
        expires_at=time.time() + 60, message=message, view=view,
    )
    with patch("tools.approval.resolve_gateway_approval") as mock_resolve:
        await handle_raw_reaction_add(adapter, _payload(user_id=999))
    mock_resolve.assert_not_called()
    message.edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_flag_disabled_ignores_reaction():
    adapter = FakeAdapter(reaction_controls=False)
    message = FakeMessage()
    view = FakeView()
    adapter._reaction_registry.register_approval(
        "m1", session_key="s", require_admin=False, admin_user_ids=set(),
        expires_at=time.time() + 60, message=message, view=view,
    )
    await handle_raw_reaction_add(adapter, _payload())
    message.edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_message_ignored():
    adapter = FakeAdapter()
    with patch("tools.approval.resolve_gateway_approval") as mock_resolve:
        await handle_raw_reaction_add(adapter, _payload(message_id="does-not-exist"))
    mock_resolve.assert_not_called()


@pytest.mark.asyncio
async def test_unauthorized_user_reaction_ignored():
    adapter = FakeAdapter()
    message = FakeMessage()
    view = FakeView()
    adapter._reaction_registry.register_approval(
        "m1", session_key="s", require_admin=False, admin_user_ids=set(),
        expires_at=time.time() + 60, message=message, view=view,
    )
    await handle_raw_reaction_add(adapter, _payload(user_id=1234))
    message.edit.assert_not_awaited()
    assert view.resolved is False


@pytest.mark.asyncio
async def test_role_authorized_reaction_resolves():
    adapter = FakeAdapter()
    adapter._allowed_user_ids = set()
    adapter._allowed_role_ids = {"role-1"}
    message = FakeMessage()
    view = FakeView()
    adapter._reaction_registry.register_approval(
        "m1", session_key="s", require_admin=False, admin_user_ids=set(),
        expires_at=time.time() + 60, message=message, view=view,
    )
    member = SimpleNamespace(roles=[SimpleNamespace(id="role-1")], display_name="bob")
    with patch("tools.approval.resolve_gateway_approval", return_value=1):
        await handle_raw_reaction_add(adapter, _payload(user_id=1234, member=member))
    message.edit.assert_awaited_once()
    assert view.resolved is True
    assert all(child.disabled for child in view.children)
    assert message.edit.await_args.kwargs["view"] is view


@pytest.mark.asyncio
async def test_admin_required_blocks_non_admin_reaction():
    adapter = FakeAdapter()
    message = FakeMessage()
    view = FakeView()
    adapter._reaction_registry.register_approval(
        "m1", session_key="s", require_admin=True, admin_user_ids={"999999"},
        expires_at=time.time() + 60, message=message, view=view,
    )
    with patch("tools.approval.resolve_gateway_approval") as mock_resolve:
        await handle_raw_reaction_add(adapter, _payload(user_id=42))
    mock_resolve.assert_not_called()
    message.edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_admin_required_allows_admin_reaction():
    adapter = FakeAdapter()
    message = FakeMessage()
    view = FakeView()
    adapter._reaction_registry.register_approval(
        "m1", session_key="s", require_admin=True, admin_user_ids={"42"},
        expires_at=time.time() + 60, message=message, view=view,
    )
    with patch("tools.approval.resolve_gateway_approval", return_value=1):
        await handle_raw_reaction_add(adapter, _payload(user_id=42))
    message.edit.assert_awaited_once()


@pytest.mark.asyncio
async def test_duplicate_and_replayed_approval_is_noop():
    adapter = FakeAdapter()
    message = FakeMessage()
    view = FakeView()
    adapter._reaction_registry.register_approval(
        "m1", session_key="s", require_admin=False, admin_user_ids=set(),
        expires_at=time.time() + 60, message=message, view=view,
    )
    with patch("tools.approval.resolve_gateway_approval", return_value=1):
        await handle_raw_reaction_add(adapter, _payload())
    assert message.edit.await_count == 1
    await handle_raw_reaction_add(adapter, _payload())
    assert message.edit.await_count == 1


@pytest.mark.asyncio
async def test_approval_already_resolved_elsewhere_logs_debug_no_edit(caplog):
    adapter = FakeAdapter()
    message = FakeMessage()
    view = FakeView()
    adapter._reaction_registry.register_approval(
        "m1", session_key="s", require_admin=False, admin_user_ids=set(),
        expires_at=time.time() + 60, message=message, view=view,
    )
    with caplog.at_level(logging.DEBUG, logger="plugins.platforms.discord.reaction_controls"):
        with patch("tools.approval.resolve_gateway_approval", return_value=0):
            await handle_raw_reaction_add(adapter, _payload(emoji=EMOJI_DENY))
    message.edit.assert_not_awaited()
    assert any("no-op" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_stop_reaction_stale_generation_logs_debug(caplog):
    adapter = FakeAdapter()
    adapter._gateway_controls = SimpleNamespace(stop_if_current=AsyncMock(return_value=False))
    adapter._reaction_registry.register_run_message("m1", session_key="s", generation=3, nonce="n")
    with caplog.at_level(logging.DEBUG, logger="plugins.platforms.discord.reaction_controls"):
        await handle_raw_reaction_add(adapter, _payload(emoji=EMOJI_STOP))
    adapter._gateway_controls.stop_if_current.assert_awaited_once_with("s", 3)
    assert any("stale or not current" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_stop_reaction_without_controls_warns_once(caplog):
    adapter = FakeAdapter()
    adapter._reaction_registry.register_run_message("m1", session_key="s", generation=1, nonce="n")
    with caplog.at_level(logging.WARNING, logger="plugins.platforms.discord.reaction_controls"):
        await handle_raw_reaction_add(adapter, _payload(emoji=EMOJI_STOP))
        await handle_raw_reaction_add(adapter, _payload(emoji=EMOJI_STOP))
    warnings = [rec for rec in caplog.records if rec.levelno == logging.WARNING]
    assert len(warnings) == 1


@pytest.mark.asyncio
async def test_retry_reaction_ignored_when_not_last_answer():
    adapter = FakeAdapter()
    adapter._gateway_controls = SimpleNamespace(retry_if_last=AsyncMock(return_value=True))
    await handle_raw_reaction_add(adapter, _payload(message_id="not-final", emoji=EMOJI_RETRY))
    adapter._gateway_controls.retry_if_last.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_reaction_when_busy_logs_debug(caplog):
    adapter = FakeAdapter()
    adapter._last_final_messages = {"s": "m1"}
    adapter._gateway_controls = SimpleNamespace(retry_if_last=AsyncMock(return_value=False))
    with caplog.at_level(logging.DEBUG, logger="plugins.platforms.discord.reaction_controls"):
        await handle_raw_reaction_add(adapter, _payload(message_id="m1", emoji=EMOJI_RETRY))
    adapter._gateway_controls.retry_if_last.assert_awaited_once_with("s", "m1")
    assert any("busy or no longer the last answer" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_retry_reaction_when_idle_triggers_controls():
    adapter = FakeAdapter()
    adapter._last_final_messages = {"s": "m1"}
    adapter._gateway_controls = SimpleNamespace(retry_if_last=AsyncMock(return_value=True))
    await handle_raw_reaction_add(adapter, _payload(message_id="m1", emoji=EMOJI_RETRY))
    adapter._gateway_controls.retry_if_last.assert_awaited_once_with("s", "m1")


@pytest.mark.asyncio
async def test_add_approval_hint_reactions_adds_both_emoji():
    message = FakeMessage()
    await add_approval_hint_reactions(message)
    assert message.add_reaction.await_args_list == [
        ((EMOJI_APPROVE,),), ((EMOJI_DENY,),),
    ]
