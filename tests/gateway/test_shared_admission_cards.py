import asyncio
from contextlib import suppress
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, HomeChannel, Platform
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session_authority import initialize_session_authority
from tests.plugins.platforms.discord.test_status_card import make_adapter, view_texts
from tests.waiting import async_wait_until


async def card_owner(tmp_path, monkeypatch, mode="normal"):
    monkeypatch.setenv("DISCORD_ALLOWED_USERS", "42")
    runner = GatewayRunner(GatewayConfig())
    adapter, channel = make_adapter(tmp_path, monkeypatch)
    adapter.config.typing_indicator = False
    adapter.config.home_channel = HomeChannel(Platform.DISCORD, "123", "fixture")
    runner.config.platforms[Platform.DISCORD] = adapter.config
    channel.new_message_each_send = True
    adapter.gateway_runner = runner
    runner.adapters = {Platform.DISCORD: adapter}
    authority = await initialize_session_authority(runner, profile_id="default", instance_id="card-owner")
    runner._wire_adapter_handlers(adapter)
    source = adapter.build_source(chat_id="123", chat_type="dm", user_id="42")
    key = runner.session_store._generate_session_key(source)
    cards, sends, tokens = [], [], []

    async def send(chat_id, content, reply_to=None, metadata=None):
        sends.append(content)
        return SendResult(success=mode != "delivery-failed", message_id="reply-1", error="offline" if mode == "delivery-failed" else None)

    async def infer(**kwargs):
        token = kwargs["hermes_run"]
        tokens.append(token)
        await adapter.send_native_task_card_progress(
            "123", [{"id": "inspect", "title": "Inspect", "status": "running"}],
            metadata={"hermes_run": token},
        )
        cards.append(next(iter(adapter._status_cards.values())))
        stopped = mode == "stopped" or (mode == "mixed" and kwargs["message"] == "second")
        if stopped:
            assert await runner._gateway_stop_if_current(key, token["generation"])
        return {"final_response": "reply", "messages": [], "interrupted": stopped}

    monkeypatch.setattr(adapter, "send", send)
    monkeypatch.setattr(runner, "_run_agent", infer)
    return runner, authority, adapter, channel, source, key, cards, sends, tokens


async def drain_owner(authority):
    for live in list(authority.sessions.values()):
        if live.task is not None:
            await asyncio.wait_for(asyncio.shield(live.task), 10)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,title", [("normal", "**Done**"), ("stopped", "**Stopped**"), ("delivery-failed", "**Failed**")])
async def test_shared_waiter_finishes_rendered_card(tmp_path, monkeypatch, mode, title):
    _runner, authority, adapter, channel, source, key, cards, sends, tokens = await card_owner(tmp_path, monkeypatch, mode)
    finalize = AsyncMock(wraps=adapter.finalize_native_task_card)
    monkeypatch.setattr(adapter, "finalize_native_task_card", finalize)
    original = MessageEvent(text="inspect", source=source, message_id="input-1")
    try:
        assert adapter._start_session_processing(original, key)
        await asyncio.wait_for(adapter._session_tasks[key], 15)
        await drain_owner(authority)
        assert len(cards) == 1
        card = cards[0]
        assert card.terminal_completed
        assert card.refresh_task is None
        assert view_texts(channel.message.edits[-1]["view"])[0] == title
        assert original._hermes_run == tokens[0]
        assert len(sends) == {"normal": 1, "stopped": 0, "delivery-failed": 2}[mode]
        edits = len(channel.message.edits)
        await adapter.on_turn_end("123", metadata={"hermes_run": tokens[0]}, outcome="done")
        await adapter._sweep_orphaned_status_cards("other")
        await adapter._run_gateway_turn_end_hooks(original, key, asyncio.Event(), None, True, True)
        assert len(channel.message.edits) == edits
        assert finalize.await_count == 1
    finally:
        for card in cards:
            await adapter._stop_status_card_refresh(card)
        await adapter._cancel_card_hook_tasks()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,title", [("normal", "**Done**"), ("stopped", "**Stopped**"), ("delivery-failed", "**Failed**")])
async def test_shared_owner_without_waiter_finishes_rendered_card(tmp_path, monkeypatch, mode, title):
    from gateway.session_ingress import deliver_settled

    _runner, authority, adapter, _channel, source, _key, cards, sends, _tokens = await card_owner(tmp_path, monkeypatch, mode)
    try:
        receipt = await authority.admit_native(MessageEvent(text="inspect", source=source, message_id="input-1"))
        await drain_owner(authority)
        assert len(cards) == 1
        card = cards[0]
        assert card.terminal_completed
        assert card.refresh_task is None
        assert view_texts(card.message.edits[-1]["view"])[0] == title
        assert len(sends) == {"normal": 1, "stopped": 0, "delivery-failed": 2}[mode]
        edits = len(card.message.edits)
        await deliver_settled(authority, receipt.admission_id)
        assert len(card.message.edits) == edits
        assert not getattr(authority, "card_runs", {})
    finally:
        for card in cards:
            await adapter._stop_status_card_refresh(card)
        await adapter._cancel_card_hook_tasks()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,title", [("normal", "**Done**"), ("stopped", "**Stopped**"), ("delivery-failed", "**Failed**"), ("mixed", None)])
async def test_busy_observer_finishes_each_admissions_card(tmp_path, monkeypatch, mode, title):
    from gateway.session_ingress import dispatch_shared_busy

    runner, authority, adapter, _channel, source, key, cards, sends, tokens = await card_owner(tmp_path, monkeypatch, mode)
    entered, released = asyncio.Event(), asyncio.Event()
    infer = runner._run_agent

    async def gated(**kwargs):
        if kwargs["message"] == "first":
            entered.set()
            await released.wait()
        return await infer(**kwargs)

    monkeypatch.setattr(runner, "_run_agent", gated)
    assert adapter._start_session_processing(
        MessageEvent(text="first", source=source, message_id="input-1"), key,
    )
    first = adapter._session_tasks[key]
    try:
        await asyncio.wait_for(entered.wait(), 10)
        follower = MessageEvent(text="second", source=source, message_id="input-2")
        follower._fifo_only = True
        follower._stop_epoch = runner._route_stop_epoch(key)
        await asyncio.wait_for(dispatch_shared_busy(adapter, follower, key), 10)
        released.set()
        await asyncio.wait_for(first, 15)
        await drain_owner(authority)
        await async_wait_until(lambda: not adapter._background_tasks, timeout=10)
        assert len(cards) == 2
        assert tokens[0]["nonce"] != tokens[1]["nonce"]
        for index, card in enumerate(cards):
            assert card.terminal_completed
            assert card.refresh_task is None
            expected = title if mode != "mixed" else ("**Done**", "**Stopped**")[index]
            assert view_texts(card.message.edits[-1]["view"])[0] == expected
        assert len(sends) == {"normal": 2, "stopped": 0, "delivery-failed": 4, "mixed": 1}[mode]
        assert not getattr(authority, "card_runs", {})
    finally:
        released.set()
        if not first.done():
            first.cancel()
        with suppress(asyncio.CancelledError):
            await first
        for card in cards:
            await adapter._stop_status_card_refresh(card)
        await adapter._cancel_card_hook_tasks()


@pytest.mark.asyncio
async def test_cancel_during_shared_card_finalization_keeps_owned_hook(tmp_path, monkeypatch):
    _runner, authority, adapter, _channel, source, key, cards, sends, _tokens = await card_owner(tmp_path, monkeypatch)
    editing, release = asyncio.Event(), asyncio.Event()
    edit = adapter._edit_status_card_frame

    async def blocked(card, frame):
        editing.set()
        await release.wait()
        return await edit(card, frame)

    monkeypatch.setattr(adapter, "_edit_status_card_frame", blocked)
    assert adapter._start_session_processing(
        MessageEvent(text="inspect", source=source, message_id="input-1"), key,
    )
    parent = adapter._session_tasks[key]
    try:
        await asyncio.wait_for(editing.wait(), 10)
        parent.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(parent), 2)
        assert adapter._card_hook_tasks
        release.set()
        await async_wait_until(lambda: not adapter._card_hook_tasks, timeout=7)
        await drain_owner(authority)
        assert cards[0].terminal_completed
        assert cards[0].refresh_task is None
        assert view_texts(cards[0].message.edits[-1]["view"])[0] == "**Done**"
        assert sends == ["reply"]
    finally:
        release.set()
        if not parent.done():
            parent.cancel()
        with suppress(asyncio.CancelledError):
            await parent
        for card in cards:
            await adapter._stop_status_card_refresh(card)
        await adapter._cancel_card_hook_tasks()


@pytest.mark.asyncio
async def test_cancelled_shared_execution_stops_card_without_delivering(tmp_path, monkeypatch):
    runner, authority, adapter, _channel, source, _key, cards, sends, _tokens = await card_owner(tmp_path, monkeypatch)
    entered, never = asyncio.Event(), asyncio.Event()
    infer = runner._run_agent

    async def blocked(**kwargs):
        await infer(**kwargs)
        entered.set()
        await never.wait()

    monkeypatch.setattr(runner, "_run_agent", blocked)
    receipt = await authority.admit_native(MessageEvent(text="inspect", source=source, message_id="input-1"))
    await asyncio.wait_for(entered.wait(), 10)
    task = authority.sessions[receipt.ref.session_id].task
    try:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), 7)
        assert cards[0].terminal_completed
        assert cards[0].refresh_task is None
        assert view_texts(cards[0].message.edits[-1]["view"])[0] == "**Stopped**"
        assert not sends
        assert not getattr(authority, "card_runs", {})
        from hermes_state_runtime import get_session_admission
        assert get_session_admission(authority.db, admission_id=receipt.admission_id)["status"] == "started"
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        for card in cards:
            await adapter._stop_status_card_refresh(card)
        await adapter._cancel_card_hook_tasks()


@pytest.mark.asyncio
async def test_restored_delivery_without_live_run_does_not_invent_identity(tmp_path, monkeypatch):
    from gateway.session_envelope import prepare_native, restore_native
    from gateway.session_ingress import deliver_response

    runner, _authority, adapter, _channel, source, key, cards, sends, tokens = await card_owner(tmp_path, monkeypatch)
    original = MessageEvent(text="inspect", source=source, message_id="input-1")
    restored = restore_native(await prepare_native(runner, original), runner)
    await deliver_response(adapter, restored, key, "persisted reply")
    assert sends == ["persisted reply"]
    assert not hasattr(restored, "_hermes_run")
    assert not cards and not tokens
    assert not adapter._status_card_history
    assert not adapter._status_cards


@pytest.mark.asyncio
async def test_cancelled_waiter_is_finished_by_its_admission_owner(tmp_path, monkeypatch):
    runner, authority, adapter, _channel, source, key, cards, sends, _tokens = await card_owner(tmp_path, monkeypatch)
    entered, released = asyncio.Event(), asyncio.Event()
    infer = runner._run_agent

    async def blocked(**kwargs):
        result = await infer(**kwargs)
        entered.set()
        await released.wait()
        return result

    monkeypatch.setattr(runner, "_run_agent", blocked)
    assert adapter._start_session_processing(
        MessageEvent(text="inspect", source=source, message_id="input-1"), key,
    )
    parent = adapter._session_tasks[key]
    try:
        await asyncio.wait_for(entered.wait(), 10)
        parent.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(parent), 2)
        released.set()
        await drain_owner(authority)
        assert cards[0].terminal_completed
        assert cards[0].refresh_task is None
        assert view_texts(cards[0].message.edits[-1]["view"])[0] == "**Stopped**"
        assert not sends
        assert not getattr(authority, "card_runs", {})
    finally:
        released.set()
        if not parent.done():
            parent.cancel()
        with suppress(asyncio.CancelledError):
            await parent
        for card in cards:
            await adapter._stop_status_card_refresh(card)
        await adapter._cancel_card_hook_tasks()


@pytest.mark.asyncio
async def test_cancelled_queued_owner_releases_unexecuted_card_context(tmp_path, monkeypatch):
    from gateway.session_contract import Principal, SessionRef
    from gateway.session_ingress import dispatch_shared_busy
    from hermes_state_runtime import list_session_admissions

    runner, authority, adapter, _channel, source, key, cards, sends, _tokens = await card_owner(tmp_path, monkeypatch)
    entered, released = asyncio.Event(), asyncio.Event()
    infer = runner._run_agent

    async def blocked(**kwargs):
        entered.set()
        await released.wait()
        return await infer(**kwargs)

    monkeypatch.setattr(runner, "_run_agent", blocked)
    assert adapter._start_session_processing(MessageEvent(text="first", source=source, message_id="input-1"), key)
    first = adapter._session_tasks[key]
    try:
        await asyncio.wait_for(entered.wait(), 10)
        follower = MessageEvent(text="second", source=source, message_id="input-2")
        follower._fifo_only = True
        follower._stop_epoch = runner._route_stop_epoch(key)
        await dispatch_shared_busy(adapter, follower, key)
        observer = next(task for task in adapter._background_tasks if task is not first)
        observer.cancel()
        with suppress(asyncio.CancelledError):
            await observer
        sid = next(iter(authority.sessions))
        rows = list_session_admissions(authority.db, session_id=sid, pending_only=False)
        queued = next(row for row in rows if row["payload"]["text"] == "second")
        actor = Principal("human", authority.profile_id, frozenset({"session:submit", "session:control"}), "fixture")
        await authority.cancel_queued(actor, SessionRef(authority.profile_id, sid), queued["admission_id"])
        await async_wait_until(lambda: queued["admission_id"] not in getattr(authority, "card_runs", {}), timeout=2)
        released.set()
        await asyncio.wait_for(first, 15)
        await drain_owner(authority)
        assert len(cards) == 1
        assert cards[0].terminal_completed
        assert cards[0].refresh_task is None
        assert view_texts(cards[0].message.edits[-1]["view"])[0] == "**Done**"
        assert sends == ["reply"]
        assert not getattr(authority, "card_runs", {})
    finally:
        released.set()
        if not first.done():
            first.cancel()
        with suppress(asyncio.CancelledError):
            await first
        for card in cards:
            await adapter._stop_status_card_refresh(card)
        await adapter._cancel_card_hook_tasks()


@pytest.mark.asyncio
async def test_late_waiter_does_not_claim_the_settled_delivery_card(tmp_path, monkeypatch):
    _runner, authority, adapter, _channel, source, key, cards, sends, _tokens = await card_owner(tmp_path, monkeypatch, "delivery-failed")
    sending, release = asyncio.Event(), asyncio.Event()
    send = adapter.send
    admit = authority.admit_native

    async def blocked_send(*args, **kwargs):
        sending.set()
        await release.wait()
        return await send(*args, **kwargs)

    async def late_receipt(event):
        receipt = await admit(event)
        await sending.wait()
        return receipt

    monkeypatch.setattr(adapter, "send", blocked_send)
    monkeypatch.setattr(authority, "admit_native", late_receipt)
    original = MessageEvent(text="inspect", source=source, message_id="input-1")
    assert adapter._start_session_processing(original, key)
    parent = adapter._session_tasks[key]
    try:
        await asyncio.wait_for(sending.wait(), 10)
        await asyncio.wait_for(parent, 10)
        assert not hasattr(original, "_hermes_run")
        release.set()
        await drain_owner(authority)
        assert cards[0].terminal_completed
        assert cards[0].refresh_task is None
        assert view_texts(cards[0].message.edits[-1]["view"])[0] == "**Failed**"
        assert len(sends) == 2
        assert not getattr(authority, "card_runs", {})
    finally:
        release.set()
        if not parent.done():
            parent.cancel()
        with suppress(asyncio.CancelledError):
            await parent
        await drain_owner(authority)
        for card in cards:
            await adapter._stop_status_card_refresh(card)
        await adapter._cancel_card_hook_tasks()
