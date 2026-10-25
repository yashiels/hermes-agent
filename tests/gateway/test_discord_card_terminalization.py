import asyncio
import time
from contextlib import suppress
from unittest.mock import AsyncMock

import pytest

from tests.waiting import async_wait_until
from gateway.platforms.base import SendResult
from tests.gateway.test_discord_card_outcome import _event, _open_card
from tests.plugins.platforms.discord.test_status_card import (
    DiscordFailure,
    make_adapter,
    run_metadata,
    view_texts,
)
from tests.plugins.platforms.discord.test_status_card_archive import ArchivableChannel


@pytest.mark.asyncio
@pytest.mark.parametrize("hook", ["finalize", "end"])
async def test_t1_ending_nonce_finishes_divergent_card(tmp_path, monkeypatch, caplog, hook):
    caplog.set_level("INFO")
    adapter, channel = make_adapter(tmp_path, monkeypatch)
    metadata = run_metadata()
    await _open_card(adapter, metadata)
    card = next(iter(adapter._status_cards.values()))
    refresh = card.refresh_task
    ending = run_metadata(session_key="redirected", generation=99)
    if hook == "finalize":
        await adapter.finalize_native_task_card(
            "channel-1", outcome="done", reply_to=None, metadata=ending,
        )
    else:
        await adapter.on_turn_end("channel-1", outcome="done", metadata=ending)
    try:
        assert card.terminal_completed
        assert "Status-card run mismatch" in caplog.text
        assert "card=(session-1, 1) run=(redirected, 99)" in caplog.text
        assert refresh.done()
        assert view_texts(channel.message.edits[-1]["view"])[0] == "**Done**"
    finally:
        await adapter._stop_status_card_refresh(card)


@pytest.mark.asyncio
async def test_t2_terminal_failure_has_one_owner(tmp_path, monkeypatch):
    adapter, channel = make_adapter(tmp_path, monkeypatch)
    metadata = run_metadata()
    await _open_card(adapter, metadata)
    card = next(iter(adapter._status_cards.values()))
    channel.message.edit = AsyncMock(side_effect=DiscordFailure("offline", status=500))
    adapter.send = AsyncMock(return_value=SendResult(success=False, error="offline"))
    await adapter.finalize_native_task_card(
        "channel-1", outcome="done", reply_to=None, metadata=metadata,
    )
    edits = channel.message.edit.await_count
    await adapter.on_turn_end("channel-1", outcome="done", metadata=metadata)
    await adapter._terminalize_card_once(card, outcome="done", metadata=metadata)
    assert channel.message.edit.await_count == edits
    assert adapter.send.await_count == 1
    assert card.refresh_task is None
    assert not adapter._status_cards
    assert card.nonce in adapter._status_card_persisted


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["running", "waiting-approval"])
async def test_t3_refresh_requires_own_nonce_end(tmp_path, monkeypatch, state):
    adapter, _channel = make_adapter(tmp_path, monkeypatch)
    metadata = run_metadata()
    await _open_card(adapter, metadata)
    card = next(iter(adapter._status_cards.values()))
    await adapter._stop_status_card_refresh(card)
    card.state = state
    card.started_at = time.time() - 86400
    adapter._gateway_controls = type("Controls", (), {"is_current": staticmethod(lambda *_: False)})()
    ticks = asyncio.Queue()
    submitted = asyncio.Queue()
    real_sleep = asyncio.sleep
    adapter._status_card_refresh_interval = 12345

    async def sleep(delay):
        if delay == 12345:
            await ticks.get()
        else:
            await real_sleep(delay)

    async def submit(frame):
        submitted.put_nowait(frame)
        return SendResult(success=True)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    card.coalescer.submit = submit
    adapter._start_status_card_refresh(card)
    try:
        for _ in range(3):
            ticks.put_nowait(None)
            await asyncio.wait_for(submitted.get(), 2)
            assert card.state == state
        if not hasattr(adapter, "_ended_status_nonces"):
            adapter._ended_status_nonces = {}
        adapter._ended_status_nonces[card.nonce] = "done"
        ticks.put_nowait(None)
        await asyncio.wait_for(asyncio.shield(card.refresh_task), 2)
        assert card.state == "done"
        assert card.terminal_completed
    finally:
        await adapter._stop_status_card_refresh(card)


@pytest.mark.asyncio
@pytest.mark.parametrize("attempted,succeeded,expected", [(True, True, "done"), (True, False, "interrupted"), (False, False, "interrupted")])
async def test_t4_delivered_done_survives_interrupt(tmp_path, monkeypatch, attempted, succeeded, expected):
    adapter, channel = make_adapter(tmp_path, monkeypatch)
    metadata = run_metadata()
    await _open_card(adapter, metadata)
    event = _event(metadata)
    event._hermes_turn_outcome = "done"
    interrupt = asyncio.Event()
    interrupt.set()
    await adapter._run_gateway_turn_end_hooks(
        event, "session-1", interrupt, metadata, attempted, succeeded,
    )
    assert ("**Done**" if expected == "done" else "**Stopped**") == view_texts(channel.message.edits[-1]["view"])[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["cancel", "hang", "repeat", "exception"])
async def test_t5_cancelled_background_owns_bounded_hook(tmp_path, monkeypatch, mode):
    adapter, _channel = make_adapter(tmp_path, monkeypatch)
    adapter.config.typing_indicator = False
    metadata = run_metadata()
    await _open_card(adapter, metadata)
    card = next(iter(adapter._status_cards.values()))
    event = _event(metadata)
    started = asyncio.Event()
    hook_started = asyncio.Event()
    hook_finished = asyncio.Event()
    never = asyncio.Event()
    errors = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: errors.append(context))

    async def handler(event):
        started.set()
        await never.wait()

    async def edit(*args):
        hook_started.set()
        try:
            await never.wait()
        finally:
            hook_finished.set()

    if mode in {"hang", "repeat"}:
        adapter._edit_status_card_frame = edit
    elif mode == "exception":
        adapter._run_gateway_turn_end_hooks = AsyncMock(side_effect=RuntimeError("hook failed"))
    adapter.set_message_handler(handler)
    parent = asyncio.create_task(adapter._process_message_background(event, "session-1"))
    try:
        await asyncio.wait_for(started.wait(), 2)
        began = loop.time()
        parent.cancel()
        if mode == "repeat":
            await asyncio.wait_for(hook_started.wait(), 2)
            parent.cancel()
            await asyncio.sleep(0)
            parent.cancel()
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(parent), 7)
        if mode in {"hang", "repeat"}:
            await asyncio.wait_for(hook_finished.wait(), 7)
        if hasattr(adapter, "_card_hook_tasks"):
            await async_wait_until(lambda: not adapter._card_hook_tasks, timeout=2)
        assert loop.time() - began < 7
        if mode == "cancel":
            assert card.state == "interrupted"
            assert card.terminal_completed
        assert not adapter._card_hook_tasks
        assert not errors
    finally:
        parent.cancel()
        with suppress(asyncio.CancelledError):
            await parent
        await adapter._stop_status_card_refresh(card)
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
async def test_t6_rearchived_card_recovers_each_edit(tmp_path, monkeypatch):
    channel = ArchivableChannel()
    adapter, _channel = make_adapter(tmp_path, monkeypatch, channel=channel)
    metadata = run_metadata()
    await _open_card(adapter, metadata)
    card = next(iter(adapter._status_cards.values()))
    try:
        for expected in (1, 2):
            channel.archived = True
            result = await adapter._edit_status_card_frame(card, card.frame())
            assert result.success
            assert channel.unarchive_calls == expected
    finally:
        await adapter._stop_status_card_refresh(card)


@pytest.mark.asyncio
async def test_r1_sweep_requires_own_nonce_end(tmp_path, monkeypatch):
    from types import SimpleNamespace

    adapter, channel = make_adapter(tmp_path, monkeypatch)
    metadata = run_metadata()
    await _open_card(adapter, metadata)
    card = next(iter(adapter._status_cards.values()))
    refresh = card.refresh_task
    adapter.set_gateway_controls(SimpleNamespace(is_current=lambda *_args: False))
    other = run_metadata(nonce="2222222222222222", generation=2, thread_id="thread-2")
    try:
        assert not adapter._status_card_run_is_current(card.session_key, card.generation)
        await adapter.on_turn_end("channel-1", outcome="done", metadata=other)

        assert card.state == "running"
        assert card.refresh_task is refresh
        assert not refresh.done()
        assert not card.terminal_completed
        assert channel.message.edits == []
        assert adapter._status_cards[(card.chat_id, card.thread_key)] is card

        await adapter.on_turn_end("channel-1", outcome="done", metadata=metadata)

        assert card.state == "done"
        assert card.terminal_completed
        assert refresh.done()
        assert view_texts(channel.message.edits[-1]["view"])[0] == "**Done**"
    finally:
        await adapter._stop_status_card_refresh(card)


async def _assert_cancel_during_hook_propagates_after_cleanup(
    tmp_path, monkeypatch, *, suppress_handler_cancel=False,
):
    from unittest.mock import Mock

    adapter, _channel = make_adapter(tmp_path, monkeypatch)
    adapter.config.typing_indicator = False
    metadata = run_metadata()
    await _open_card(adapter, metadata)
    card = next(iter(adapter._status_cards.values()))
    event = _event(metadata)
    event._hermes_turn_outcome = "done"
    hook_started = asyncio.Event()
    hook_finished = asyncio.Event()
    never = asyncio.Event()
    handler_started = asyncio.Event()
    handler_suppressed_cancel = asyncio.Event()

    async def handler(_event):
        handler_started.set()
        try:
            await never.wait()
        except asyncio.CancelledError:
            handler_suppressed_cancel.set()

    async def edit(*args):
        hook_started.set()
        try:
            await never.wait()
        finally:
            hook_finished.set()

    adapter._edit_status_card_frame = edit
    adapter.set_message_handler(handler if suppress_handler_cancel else AsyncMock(return_value=None))
    cleanups = {}
    for name in ("_release_turn_marker", "_stop_typing_refresh", "_fire_post_delivery_callback",
                 "_flush_text_debounce_now"):
        cleanups[name] = AsyncMock(wraps=getattr(adapter, name))
        monkeypatch.setattr(adapter, name, cleanups[name])
    finish_session = Mock(wraps=adapter._finish_session_task)
    monkeypatch.setattr(adapter, "_finish_session_task", finish_session)
    parent = asyncio.create_task(adapter._process_message_background(event, "session-1"))
    try:
        if suppress_handler_cancel:
            await asyncio.wait_for(handler_started.wait(), 2)
            parent.cancel()
            await asyncio.wait_for(handler_suppressed_cancel.wait(), 2)
        await asyncio.wait_for(hook_started.wait(), 2)
        assert parent.cancelling() == int(suppress_handler_cancel)
        for cleanup in cleanups.values():
            cleanup.reset_mock()
        began = asyncio.get_running_loop().time()
        parent.cancel()

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(parent), 2)

        assert parent.cancelled()
        cleanups["_release_turn_marker"].assert_awaited_once()
        assert cleanups["_stop_typing_refresh"].await_count == 2
        cleanups["_fire_post_delivery_callback"].assert_awaited_once()
        cleanups["_flush_text_debounce_now"].assert_awaited_once()
        finish_session.assert_called_once()
        assert event._turn_marker_handoff is False
        assert adapter._card_hook_tasks
        assert not hook_finished.is_set()
        await asyncio.wait_for(hook_finished.wait(), 7)
        await async_wait_until(lambda: not adapter._card_hook_tasks, timeout=2)
        assert asyncio.get_running_loop().time() - began < 7
    finally:
        parent.cancel()
        with suppress(asyncio.CancelledError):
            await parent
        await adapter._cancel_card_hook_tasks()
        await adapter._stop_status_card_refresh(card)


@pytest.mark.asyncio
async def test_r1_first_cancel_during_hook_propagates_after_cleanup(tmp_path, monkeypatch):
    await _assert_cancel_during_hook_propagates_after_cleanup(tmp_path, monkeypatch)


@pytest.mark.asyncio
async def test_r2_cancel_during_hook_after_handler_suppressed_cancel(tmp_path, monkeypatch):
    await _assert_cancel_during_hook_propagates_after_cleanup(
        tmp_path, monkeypatch, suppress_handler_cancel=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["cancel", "done", "cancelled_done"])
async def test_r3_partial_adapter_card_hook_cleanup(operation):
    from unittest.mock import Mock

    from plugins.platforms.discord.adapter import DiscordAdapter

    adapter = object.__new__(DiscordAdapter)
    if operation == "cancel":
        await adapter._cancel_card_hook_tasks()
    else:
        task = Mock()
        task.cancelled.return_value = operation == "cancelled_done"
        adapter._card_hook_done(task)
        if operation == "done":
            task.exception.assert_called_once_with()
        else:
            task.exception.assert_not_called()
    assert adapter._card_hook_tasks == set()
    other = object.__new__(DiscordAdapter)
    assert other._card_hook_task_set() is not adapter._card_hook_tasks


@pytest.mark.asyncio
async def test_r4_background_turn_lazily_tracks_card_hook_tasks(tmp_path, monkeypatch):
    from unittest.mock import Mock

    adapter, _channel = make_adapter(tmp_path, monkeypatch)
    adapter.config.typing_indicator = False
    del adapter._card_hook_tasks
    event = _event(run_metadata())
    event._hermes_turn_outcome = "done"
    adapter.set_message_handler(AsyncMock(return_value=None))
    hook_started = asyncio.Event()
    hook_released = asyncio.Event()
    hook_completed = asyncio.Event()

    async def hook(*args):
        hook_started.set()
        await hook_released.wait()
        hook_completed.set()

    monkeypatch.setattr(adapter, "_run_gateway_turn_end_hooks", hook)
    cleanups = {}
    for name in ("_release_turn_marker", "_stop_typing_refresh", "_fire_post_delivery_callback",
                 "_flush_text_debounce_now"):
        cleanups[name] = AsyncMock(wraps=getattr(adapter, name))
        monkeypatch.setattr(adapter, name, cleanups[name])
    finish_session = Mock(wraps=adapter._finish_session_task)
    monkeypatch.setattr(adapter, "_finish_session_task", finish_session)
    parent = asyncio.create_task(adapter._process_message_background(event, "session-1"))
    try:
        await asyncio.wait_for(hook_started.wait(), 2)
        if parent.done():
            await parent
        assert len(adapter._card_hook_tasks) == 1
        owned_task = next(iter(adapter._card_hook_tasks))
        assert not owned_task.done()
        for cleanup in cleanups.values():
            cleanup.reset_mock()
        hook_released.set()
        await asyncio.wait_for(parent, 2)

        assert hook_completed.is_set()
        assert owned_task.done()
        assert not adapter._card_hook_tasks
        cleanups["_release_turn_marker"].assert_awaited_once()
        assert cleanups["_stop_typing_refresh"].await_count == 2
        cleanups["_fire_post_delivery_callback"].assert_awaited_once()
        cleanups["_flush_text_debounce_now"].assert_awaited_once()
        finish_session.assert_called_once()
        assert event._turn_marker_handoff is False
    finally:
        hook_released.set()
        if not parent.done():
            parent.cancel()
        with suppress(asyncio.CancelledError, AttributeError):
            await parent
        await asyncio.wait_for(hook_completed.wait(), 2)
        await adapter._cancel_card_hook_tasks()
