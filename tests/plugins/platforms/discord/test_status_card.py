import asyncio
import json
import queue
import re
import sys
import threading
import time
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from tests.gateway import conftest as gateway_conftest


gateway_conftest._ensure_discord_mock()
discord = sys.modules["discord"]


class FakeContainer:
    def __init__(self, *children, accent_colour=None, **_kwargs):
        self.children = list(children)
        self.accent_colour = accent_colour


class FakeTextDisplay:
    def __init__(self, content, **_kwargs):
        self.content = content
        self.children = []


class FakeSeparator:
    def __init__(self, **_kwargs):
        self.children = []


class FakeActionRow:
    def __init__(self, *children, **_kwargs):
        self.children = list(children)


class FakeDynamicItem:
    def __init_subclass__(cls, **kwargs):
        cls.template = kwargs.get("template")

    def __class_getitem__(cls, _item):
        return cls

    def __init__(self, item, **_kwargs):
        self.item = item
        self.children = []


class FakeLayoutView:
    def __init__(self, *, timeout=None):
        self.timeout = timeout
        self.children = []

    def add_item(self, item):
        self.children.append(item)

    def walk_children(self):
        def walk(item):
            yield item
            for child in getattr(item, "children", []):
                yield from walk(child)
            wrapped = getattr(item, "item", None)
            if wrapped is not None:
                yield wrapped

        for child in self.children:
            yield from walk(child)

    def content_length(self):
        return sum(len(item.content) for item in self.walk_children() if hasattr(item, "content"))

    @property
    def total_children_count(self):
        return sum(1 for _item in self.walk_children())


discord.ui.Container = FakeContainer
discord.ui.TextDisplay = FakeTextDisplay
discord.ui.Separator = FakeSeparator
discord.ui.ActionRow = FakeActionRow
discord.ui.DynamicItem = FakeDynamicItem
discord.ui.LayoutView = FakeLayoutView

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType
from gateway.platforms.base import SendResult
from gateway.run import GatewayRunner
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource
from gateway.turn_context import TurnContext
from plugins.platforms.discord.adapter import DiscordAdapter
from plugins.platforms.discord.status_card import (
    ShowAllCardButton,
    StatusCardCoalescer,
    StatusCardFrame,
    StatusCardView,
    StopCardButton,
    build_status_card_history_markdown,
    prune_status_card_history,
    status_card_history_rows,
)


class DiscordFailure(Exception):
    def __init__(self, message, *, status=None, code=None):
        super().__init__(message)
        self.status = status
        self.code = code


class FakeMessage:
    def __init__(self, message_id="100"):
        self.id = message_id
        self.edits = []
        self.failures = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        if self.failures:
            raise self.failures.pop(0)
        return self


class FakeChannel:
    def __init__(self, message=None, *, new_message_each_send=False):
        self.message = message or FakeMessage()
        self.messages = [self.message]
        self.sends = []
        self.failure = None
        self.new_message_each_send = new_message_each_send

    async def send(self, **kwargs):
        self.sends.append(kwargs)
        if self.failure is not None:
            raise self.failure
        if self.new_message_each_send and len(self.sends) > 1:
            self.message = FakeMessage(str(99 + len(self.sends)))
            self.messages.append(self.message)
        return self.message

    def get_partial_message(self, message_id):
        return next(message for message in self.messages if str(message.id) == str(message_id))


class RedirectingAgent:
    _supports_active_turn_redirect = True

    def __init__(self):
        self._model_request_active = threading.Event()
        self._model_request_active.set()
        self._interrupt_requested = False
        self._pending_redirect = None
        self.abort_count = 0
        self.steer_count = 0

    @property
    def is_interrupted(self):
        return self._interrupt_requested

    def redirect(self, text):
        if not text.strip() or not self._model_request_active.is_set():
            return False
        self._pending_redirect = text.strip()
        self._interrupt_requested = True
        self.abort_count += 1
        return True

    def retry(self):
        redirected = self._pending_redirect
        self._pending_redirect = None
        self._interrupt_requested = False
        return redirected

    def steer(self, text):
        if not text.strip():
            return False
        self.steer_count += 1
        return True


def run_metadata(generation=1, nonce="0123456789abcdef", session_key="session-1", thread_id="thread-1"):
    return {
        "thread_id": thread_id,
        "hermes_run": {
            "session_key": session_key,
            "generation": generation,
            "nonce": nonce,
        },
    }


def make_adapter(tmp_path, monkeypatch, *, enabled=True, channel=None):
    adapter = DiscordAdapter(PlatformConfig(
        enabled=True,
        token="x",
        extra={"native_task_cards": enabled},
    ))
    adapter._client = object()
    adapter._status_card_persistence_path = tmp_path / "state" / "discord_status_cards.json"
    adapter._status_card_history_path = tmp_path / "state" / "discord_status_card_history.json"
    channel = channel or FakeChannel()
    monkeypatch.setattr(adapter, "_resolve_channel", AsyncMock(return_value=channel))
    return adapter, channel


async def wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition did not become true")
        await asyncio.sleep(0.005)


def view_texts(view):
    return [item.content for item in view.walk_children() if hasattr(item, "content")]


def view_buttons(view):
    return [item for item in view.walk_children() if hasattr(item, "custom_id")]


def show_all_interaction(adapter, *, user_id=7, roles=None, order=None):
    async def defer(**_kwargs):
        if order is not None:
            order.append("defer")

    async def send(*_args, **_kwargs):
        if order is not None:
            order.append("followup")

    return SimpleNamespace(
        client=SimpleNamespace(_hermes_discord_adapter=adapter),
        user=SimpleNamespace(id=user_id, roles=list(roles or [])),
        response=SimpleNamespace(defer=AsyncMock(side_effect=defer)),
        followup=SimpleNamespace(send=AsyncMock(side_effect=send)),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["redirect", "steer", "slow-final-only"])
async def test_status_card_survives_progress_cleanup_before_final_delivery(
    tmp_path, monkeypatch, mode,
):
    adapter, channel = make_adapter(tmp_path, monkeypatch)
    adapter.config.typing_indicator = False
    adapter._status_card_refresh_interval = 0.01
    session_key = "agent:main:discord:thread:thread-1"
    metadata = run_metadata(session_key=session_key)
    token = metadata["hermes_run"]
    source = SessionSource(
        platform=Platform.DISCORD, chat_id="channel-1", chat_type="thread",
        thread_id="thread-1", user_id="owner-1",
    )
    opening = MessageEvent(
        text="start", message_type=MessageType.TEXT, source=source, message_id="message-a",
    )
    incoming = MessageEvent(
        text="correct it", message_type=MessageType.TEXT, source=source, message_id="message-b",
    )
    agent = RedirectingAgent()
    ctx = TurnContext(
        source=source, session_key=session_key, run_generation=token["generation"],
        event_message_id="message-a", inbound_message_id="message-a",
        progress_queue=queue.Queue(), _progress_reply_to="message-a",
        _progress_metadata=metadata, agent_holder=[agent],
    )
    current = True
    ctx._run_still_current = lambda: current
    redirect_runner = GatewayRunner(config=GatewayConfig())
    active_turn = redirect_runner._session_state(session_key).turn
    active_turn.agent = agent
    active_turn.event = opening
    active_turn.ctx = ctx
    sent = []
    final_sent = asyncio.Event()

    async def send(chat_id, content, reply_to=None, metadata=None):
        if content == "final answer":
            await asyncio.sleep(0.04)
            sent.append(("answer", content))
            final_sent.set()
            return SendResult(success=True, message_id="final-1")
        await final_sent.wait()
        sent.append(("fallback", content))
        return SendResult(success=True, message_id="fallback-1")

    adapter.send = send

    async def handler(event):
        nonlocal current
        event._hermes_run = token
        event._hermes_turn_outcome = "done"
        lane = asyncio.create_task(TurnRunner(None, ctx)._send_native_task_card_progress(adapter))
        ctx.progress_queue.put({
            "type": "tool.started", "tool_call_id": "call-1", "tool_name": "terminal",
        })
        await wait_until(lambda: bool(channel.sends))
        if mode == "redirect":
            assert redirect_runner._redirect_active_turn(
                agent, incoming.text, session_key, incoming,
            ) is True
            assert agent.retry().endswith(incoming.text)
        elif mode == "steer":
            outcome = await redirect_runner._resolve_busy_steer_or_redirect(
                incoming, session_key, "steer", agent,
            )
            assert outcome.steered is True
        ctx.progress_queue.put({
            "type": "tool.completed", "tool_call_id": "call-1", "tool_name": "terminal",
        })
        await wait_until(
            lambda: next(iter(adapter._status_cards.values())).tasks[0]["status"] == "complete"
        )
        current = False
        lane.cancel()
        with suppress(asyncio.CancelledError):
            await lane
        return "final answer"

    adapter.set_message_handler(handler)
    adapter._active_sessions[session_key] = asyncio.Event()
    await asyncio.wait_for(adapter._process_message_background(opening, session_key), timeout=2.0)
    await asyncio.sleep(0.06)

    final_view = channel.message.edits[-1]["view"]
    assert (
        [kind for kind, _content in sent],
        len(channel.sends),
        any("done" in text for text in view_texts(final_view)),
        [button.label for button in view_buttons(final_view)],
        ctx.event_message_id,
        agent.abort_count,
    ) == (
        ["answer"],
        1,
        True,
        [],
        "message-b" if mode == "redirect" else "message-a",
        1 if mode == "redirect" else 0,
    )
    assert agent.steer_count == (1 if mode == "steer" else 0)


def test_status_card_view_trims_text_and_components():
    tasks = [
        {"id": str(index), "title": f"task {index} " + ("x" * 1000), "status": "running"}
        for index in range(12)
    ]
    view = StatusCardView(
        tasks, nonce="0123456789abcdef", title="Hermes run", state="running",
        elapsed_s=65, iteration=3, max_iterations=10,
    )

    assert view.content_length() <= 3800
    assert view.total_children_count <= 40
    assert any("+4 earlier" in text for text in view_texts(view))


def test_show_all_button_only_appears_past_display_limit():
    eight = [{"title": f"Task {index}", "status": "complete"} for index in range(8)]
    nine = eight + [{"title": "Task 8", "status": "complete"}]
    without_button = StatusCardView(
        eight, nonce=None, title="Hermes run", state="done", elapsed_s=1,
        iteration=0, max_iterations=0, history_nonce="a" * 32,
    )
    with_button = StatusCardView(
        nine, nonce=None, title="Hermes run", state="done", elapsed_s=1,
        iteration=0, max_iterations=0, history_nonce="b" * 32,
    )

    assert [button.label for button in view_buttons(without_button)] == []
    assert [button.label for button in view_buttons(with_button)] == ["Show all"]
    assert view_buttons(with_button)[0].custom_id == f"hermes:card:all:{'b' * 32}"


def test_run_control_capability_follows_native_or_reaction_flag(tmp_path, monkeypatch):
    adapter, _channel = make_adapter(tmp_path, monkeypatch, enabled=False)

    assert adapter.gateway_run_controls_enabled() is False
    adapter.config.extra["reaction_controls"] = True
    assert adapter.gateway_run_controls_enabled() is True


@pytest.mark.asyncio
async def test_status_card_send_uses_only_components_view(tmp_path, monkeypatch):
    adapter, channel = make_adapter(tmp_path, monkeypatch)

    result = await adapter.send_native_task_card_progress(
        "channel-1", [{"id": "1", "title": "Inspect", "status": "running"}],
        metadata=run_metadata(), title="Hermes run",
    )

    assert result.success is True
    assert set(channel.sends[0]) == {"view"}
    assert isinstance(channel.sends[0]["view"], StatusCardView)


@pytest.mark.asyncio
async def test_discord_task_card_publish_keeps_all_rows_for_terminal_history():
    adapter = SimpleNamespace(
        native_task_card_full_history=True,
        send_native_task_card_progress=AsyncMock(return_value=SendResult(success=True, message_id="1")),
    )
    context = SimpleNamespace(
        source=SimpleNamespace(chat_id="channel-1"),
        _progress_reply_to=None,
        _progress_metadata=run_metadata(),
        tool_progress_enabled=False,
    )
    runner = TurnRunner(None, context)
    state = runner._TaskCardState(adapter)
    for index in range(350):
        state.apply_event({
            "type": "tool.started", "tool_call_id": str(index), "tool_name": f"tool-{index}",
        })

    await runner._task_card_publish(state)

    sent_tasks = adapter.send_native_task_card_progress.await_args.kwargs["tasks"]
    sent_metadata = adapter.send_native_task_card_progress.await_args.kwargs["metadata"]
    assert len(sent_tasks) == 300
    assert sent_tasks[0]["title"] == "tool-50"
    assert sent_metadata["status_card_rows_omitted"] == 50
    assert len(state.visible_tasks()) == 8
    rows, rows_omitted = status_card_history_rows(
        sent_tasks, "done", earlier_rows_omitted=sent_metadata["status_card_rows_omitted"],
    )
    history_file = build_status_card_history_markdown({
        "title": "Hermes run",
        "state": "done",
        "elapsed": 1,
        "finished_at": 2_000_000_000.0,
        "rows": rows,
        "rows_omitted": rows_omitted,
    }).decode("utf-8")
    assert rows_omitted == 50
    assert "50 earlier rows omitted" in history_file


@pytest.mark.asyncio
async def test_old_finalize_cannot_pop_new_card(tmp_path, monkeypatch):
    channel = FakeChannel(new_message_each_send=True)
    adapter, _channel = make_adapter(tmp_path, monkeypatch, channel=channel)
    old = run_metadata(generation=1, nonce="1111111111111111")
    new = run_metadata(generation=2, nonce="2222222222222222")

    await adapter.send_native_task_card_progress(
        "channel-1", [{"id": "1", "title": "Old", "status": "running"}], metadata=old,
    )
    old_message = channel.message
    await adapter.send_native_task_card_progress(
        "channel-1", [{"id": "2", "title": "New", "status": "running"}], metadata=new,
    )

    assert await adapter.finalize_native_task_card(
        "channel-1", outcome="done", reply_to=None, metadata=old,
    ) is False
    key = adapter._status_card_keys_by_nonce["2222222222222222"]
    assert adapter._status_cards[key].nonce == "2222222222222222"
    assert "interrupted" in view_texts(old_message.edits[-1]["view"])[1]
    persisted = json.loads(adapter._status_card_persistence_path.read_text())
    assert {entry["nonce"] for entry in persisted} == {"2222222222222222"}


@pytest.mark.asyncio
async def test_failed_supersession_keeps_old_card_and_aborts_replacement(
    tmp_path, monkeypatch, caplog,
):
    channel = FakeChannel(new_message_each_send=True)
    adapter, _channel = make_adapter(tmp_path, monkeypatch, channel=channel)
    old = run_metadata(generation=1, nonce="1111111111111111")
    new = run_metadata(generation=2, nonce="2222222222222222")
    await adapter.send_native_task_card_progress(
        "channel-1", [{"id": "1", "title": "Old", "status": "running"}], metadata=old,
    )
    channel.message.failures = [ConnectionError("offline") for _ in range(4)]
    adapter.send = AsyncMock(return_value=SendResult(success=False, error="offline", retryable=True))

    result = await adapter.send_native_task_card_progress(
        "channel-1", [{"id": "2", "title": "New", "status": "running"}], metadata=new,
    )

    assert result == SendResult(success=False, error="superseded card not terminalized")
    key = adapter._status_card_keys_by_nonce["1111111111111111"]
    assert adapter._status_cards[key].nonce == "1111111111111111"
    assert "2222222222222222" not in adapter._status_card_keys_by_nonce
    assert len(channel.sends) == 1
    persisted = json.loads(adapter._status_card_persistence_path.read_text())
    assert {entry["nonce"] for entry in persisted} == {"1111111111111111"}
    assert "restart reconciliation will retry" in caplog.text


@pytest.mark.asyncio
async def test_coalescer_cancellation_drops_throttled_frame():
    edits = []

    async def edit(frame):
        edits.append(frame.state)
        return SendResult(success=True)

    coalescer = StatusCardCoalescer(edit, interval=0.05)
    first = StatusCardFrame([], "Run", "running", 0, 0, 0)
    second = StatusCardFrame([], "Run", "waiting-approval", 0, 0, 0)

    await coalescer.submit(first)
    await coalescer.submit(second)
    await coalescer.close()
    await asyncio.sleep(0.07)

    assert edits == ["running"]


@pytest.mark.asyncio
async def test_coalescer_flush_resumes_with_interval_and_close_drops_frames():
    edits = []

    async def edit(frame):
        edits.append(frame.state)
        return SendResult(success=True)

    coalescer = StatusCardCoalescer(edit, interval=0.05)
    running = StatusCardFrame([], "Run", "running", 0, 0, 0)
    waiting = StatusCardFrame([], "Run", "waiting-approval", 0, 0, 0)
    resumed = StatusCardFrame([], "Run", "running", 1, 0, 0)

    await coalescer.submit(running)
    await coalescer.submit(waiting)
    await coalescer.flush()
    await coalescer.submit(resumed)
    await asyncio.sleep(0.02)
    assert edits == ["running", "waiting-approval"]
    await asyncio.sleep(0.05)
    assert edits == ["running", "waiting-approval", "running"]
    await coalescer.submit(waiting)
    await coalescer.close()
    result = await coalescer.submit(running)
    await asyncio.sleep(0.06)
    assert result.success is True
    assert result.error == "status card closed"
    assert edits == ["running", "waiting-approval", "running"]


@pytest.mark.asyncio
async def test_lane_restart_reuses_card_and_terminalizes_once(tmp_path, monkeypatch):
    adapter, channel = make_adapter(tmp_path, monkeypatch)
    metadata = run_metadata()
    first = [{"id": "1", "title": "Inspect", "status": "running"}]
    second = [{"id": "1", "title": "Inspect", "status": "complete"}]

    await adapter.send_native_task_card_progress("channel-1", first, metadata=metadata)
    await adapter.stop_native_task_card_progress("channel-1", metadata=metadata)
    await adapter.send_native_task_card_progress("channel-1", second, metadata=metadata)
    await adapter.stop_native_task_card_progress("channel-1", metadata=metadata)
    await adapter.finalize_native_task_card(
        "channel-1", outcome="done", reply_to=None, metadata=metadata,
    )
    await adapter.on_turn_end("channel-1", metadata=metadata, outcome="done")

    terminal_edits = [
        edit for edit in channel.message.edits
        if "done" in view_texts(edit["view"])[1]
    ]
    assert len(channel.sends) == 1
    assert len(terminal_edits) == 1
    assert not adapter._status_cards


@pytest.mark.asyncio
async def test_on_turn_end_terminalizes_when_finalize_was_skipped(tmp_path, monkeypatch):
    adapter, channel = make_adapter(tmp_path, monkeypatch)
    metadata = run_metadata()
    await adapter.send_native_task_card_progress(
        "channel-1", [{"id": "1", "title": "Inspect", "status": "complete"}],
        metadata=metadata,
    )

    await adapter.on_turn_end("channel-1", metadata=metadata, outcome="done")

    assert "done" in view_texts(channel.message.edits[-1]["view"])[1]
    assert view_buttons(channel.message.edits[-1]["view"]) == []
    assert not adapter._status_cards


@pytest.mark.asyncio
async def test_interrupt_terminalizes_without_text_fallback(tmp_path, monkeypatch):
    adapter, channel = make_adapter(tmp_path, monkeypatch)
    metadata = run_metadata()
    token = metadata["hermes_run"]
    source = SessionSource(
        platform=Platform.DISCORD, chat_id="channel-1", chat_type="thread",
        thread_id="thread-1",
    )
    event = MessageEvent(text="stop", source=source, message_id="message-1")
    event._hermes_run = token
    event._hermes_turn_outcome = "done"
    await adapter.send_native_task_card_progress(
        "channel-1", [{"id": "1", "title": "Inspect", "status": "running"}],
        metadata=metadata, fallback_text="fallback",
    )
    interrupt = asyncio.Event()
    interrupt.set()
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="fallback-1"))

    await adapter._run_gateway_turn_end_hooks(
        event, token["session_key"], interrupt, metadata, False, False,
    )

    assert "interrupted" in view_texts(channel.message.edits[-1]["view"])[1]
    assert view_buttons(channel.message.edits[-1]["view"]) == []
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_edit_wins_over_delayed_frame(tmp_path, monkeypatch):
    adapter, channel = make_adapter(tmp_path, monkeypatch)
    metadata = run_metadata()
    await adapter.send_native_task_card_progress(
        "channel-1", [{"id": "1", "title": "Inspect", "status": "running"}], metadata=metadata,
    )
    card = next(iter(adapter._status_cards.values()))
    card.coalescer._interval = 0.05
    await card.coalescer.submit(card.frame())
    card.state = "waiting-approval"
    await card.coalescer.submit(card.frame())

    assert await adapter.finalize_native_task_card(
        "channel-1", outcome="done", reply_to=None, metadata=metadata,
    ) is True
    await asyncio.sleep(0.07)

    assert "done" in view_texts(channel.message.edits[-1]["view"])[1]


@pytest.mark.asyncio
async def test_elapsed_refresh_task_is_cancelled_on_terminal(tmp_path, monkeypatch):
    adapter, channel = make_adapter(tmp_path, monkeypatch)
    adapter._status_card_refresh_interval = 0.01
    metadata = run_metadata()
    await adapter.send_native_task_card_progress(
        "channel-1", [{"id": "1", "title": "Inspect", "status": "running"}], metadata=metadata,
    )
    card = next(iter(adapter._status_cards.values()))
    card.started_at -= 16
    refresh_task = card.refresh_task
    await asyncio.sleep(0.03)

    assert any("16s" in text or "17s" in text for text in view_texts(channel.message.edits[-1]["view"]))
    assert await adapter.finalize_native_task_card(
        "channel-1", outcome="done", reply_to=None, metadata=metadata,
    ) is True
    assert card.refresh_task is None
    assert refresh_task.cancelled()


@pytest.mark.asyncio
async def test_elapsed_refreshes_advance_card_state_and_rendered_time(tmp_path, monkeypatch):
    import plugins.platforms.discord.adapter as discord_adapter

    adapter, channel = make_adapter(tmp_path, monkeypatch)
    metadata = run_metadata()
    await adapter.send_native_task_card_progress(
        "channel-1", [{"id": "1", "title": "Inspect", "status": "running"}], metadata=metadata,
    )
    card = next(iter(adapter._status_cards.values()))
    await adapter._stop_status_card_refresh(card)
    card.started_at = 100.0
    card.elapsed_s = 0.0
    elapsed_frames = []

    async def submit(frame):
        elapsed_frames.append(frame.elapsed_s)
        return await adapter._edit_status_card_frame(card, frame)

    card.coalescer = SimpleNamespace(submit=submit)
    clock = [100.0]

    async def advance(_delay):
        clock[0] += 15.0
        if clock[0] > 130.0:
            raise asyncio.CancelledError

    monkeypatch.setattr(discord_adapter.time, "time", lambda: clock[0])
    monkeypatch.setattr(discord_adapter.asyncio, "sleep", advance)

    with pytest.raises(asyncio.CancelledError):
        await adapter._refresh_status_card(card)

    assert elapsed_frames == [15.0, 30.0]
    assert card.elapsed_s == 30.0
    assert [view_texts(edit["view"])[1] for edit in channel.message.edits[-2:]] == [
        "-# running · 15s",
        "-# running · 30s",
    ]


@pytest.mark.asyncio
async def test_elapsed_refresh_does_not_edit_after_terminal(tmp_path, monkeypatch):
    adapter, channel = make_adapter(tmp_path, monkeypatch)
    adapter._status_card_refresh_interval = 0.01
    metadata = run_metadata()
    await adapter.send_native_task_card_progress(
        "channel-1", [{"id": "1", "title": "Inspect", "status": "running"}], metadata=metadata,
    )
    await asyncio.sleep(0.02)
    assert await adapter.finalize_native_task_card(
        "channel-1", outcome="interrupted", reply_to=None, metadata=metadata,
    ) is True
    terminal_edit_count = len(channel.message.edits)

    await asyncio.sleep(0.03)

    assert len(channel.message.edits) == terminal_edit_count
    assert "interrupted" in view_texts(channel.message.edits[-1]["view"])[1]


@pytest.mark.asyncio
async def test_terminal_edit_retries_then_removes_persistence(tmp_path, monkeypatch):
    adapter, channel = make_adapter(tmp_path, monkeypatch)
    metadata = run_metadata()
    await adapter.send_native_task_card_progress(
        "channel-1", [{"id": "1", "title": "Inspect", "status": "running"}], metadata=metadata,
    )
    channel.message.failures = [
        DiscordFailure("server", status=500),
        DiscordFailure("server", status=502),
    ]

    assert await adapter.finalize_native_task_card(
        "channel-1", outcome="done", reply_to=None, metadata=metadata,
    ) is True
    assert len(channel.message.edits) == 3
    assert json.loads(adapter._status_card_persistence_path.read_text()) == []


@pytest.mark.asyncio
async def test_terminal_edit_failure_removes_controls_and_sends_notice(tmp_path, monkeypatch):
    adapter, channel = make_adapter(tmp_path, monkeypatch)
    metadata = run_metadata()
    await adapter.send_native_task_card_progress(
        "channel-1", [{"id": "1", "title": "Inspect", "status": "running"}], metadata=metadata,
    )
    channel.message.failures = [DiscordFailure("server", status=500) for _ in range(3)]
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="notice-1"))

    assert await adapter.finalize_native_task_card(
        "channel-1", outcome="failed", reply_to=None, metadata=metadata,
    ) is True
    assert channel.message.edits[-1] == {"view": None}
    assert "status card could not be updated" in adapter.send.await_args.args[1]
    assert json.loads(adapter._status_card_persistence_path.read_text()) == []


@pytest.mark.asyncio
async def test_terminal_edit_and_notice_failure_keep_persistence(tmp_path, monkeypatch):
    adapter, channel = make_adapter(tmp_path, monkeypatch)
    metadata = run_metadata()
    await adapter.send_native_task_card_progress(
        "channel-1", [{"id": "1", "title": "Inspect", "status": "running"}], metadata=metadata,
    )
    channel.message.failures = [DiscordFailure("server", status=500) for _ in range(4)]
    adapter.send = AsyncMock(return_value=SendResult(success=False, error="offline", retryable=True))

    assert await adapter.finalize_native_task_card(
        "channel-1", outcome="failed", reply_to=None, metadata=metadata,
    ) is False
    assert json.loads(adapter._status_card_persistence_path.read_text())


@pytest.mark.asyncio
async def test_view_removal_success_clears_persistence_when_notice_fails(tmp_path, monkeypatch):
    adapter, channel = make_adapter(tmp_path, monkeypatch)
    metadata = run_metadata()
    await adapter.send_native_task_card_progress(
        "channel-1", [{"id": "1", "title": "Inspect", "status": "running"}], metadata=metadata,
    )
    channel.message.failures = [ConnectionError("offline") for _ in range(3)]
    adapter.send = AsyncMock(return_value=SendResult(success=False, error="offline", retryable=True))

    assert await adapter.finalize_native_task_card(
        "channel-1", outcome="failed", reply_to=None, metadata=metadata,
    ) is True
    assert channel.message.edits[-1] == {"view": None}
    assert json.loads(adapter._status_card_persistence_path.read_text()) == []


@pytest.mark.asyncio
async def test_components_v2_validation_failure_returns_text_fallback_signal(tmp_path, monkeypatch):
    channel = FakeChannel()
    channel.failure = DiscordFailure("Invalid Form Body", status=400, code=50035)
    adapter, _channel = make_adapter(tmp_path, monkeypatch, channel=channel)

    result = await adapter.send_native_task_card_progress(
        "channel-1", [{"id": "1", "title": "Inspect", "status": "running"}],
        metadata=run_metadata(),
    )

    assert result.success is False
    assert result.retryable is False
    assert not adapter._status_cards


@pytest.mark.asyncio
async def test_components_v2_validation_failure_falls_back_to_text(tmp_path, monkeypatch):
    channel = FakeChannel()
    channel.failure = DiscordFailure("Invalid Form Body", status=400, code=50035)
    adapter, _channel = make_adapter(tmp_path, monkeypatch, channel=channel)
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="fallback-1"))
    events = queue.Queue()
    events.put({"type": "tool.started", "tool_call_id": "call-1", "tool_name": "terminal"})
    context = SimpleNamespace(
        source=SimpleNamespace(chat_id="channel-1"),
        _progress_reply_to=None,
        _progress_metadata=run_metadata(),
        _cleanup_progress=False,
        progress_queue=events,
        tool_progress_enabled=True,
        _run_still_current=lambda: not events.empty(),
        agent_holder=[None],
    )

    await TurnRunner(None, context)._send_native_task_card_progress(adapter)

    adapter.send.assert_awaited_once()
    assert "terminal" in adapter.send.await_args.kwargs["content"]


@pytest.mark.asyncio
async def test_retryable_initial_publish_preserves_native_card_lane(tmp_path, monkeypatch):
    adapter, channel = make_adapter(tmp_path, monkeypatch)
    channel.failure = ConnectionError("offline")
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="fallback-1"))
    context = SimpleNamespace(
        source=SimpleNamespace(chat_id="channel-1"),
        _progress_reply_to=None,
        _progress_metadata=run_metadata(),
        _cleanup_progress=False,
        _cleanup_msg_ids=[],
        tool_progress_enabled=True,
    )
    runner = TurnRunner(None, context)
    state = runner._TaskCardState(adapter)
    state.apply_event({
        "type": "tool.started", "tool_call_id": "call-1", "tool_name": "terminal",
    })

    await runner._task_card_publish(state)

    assert state.native_failed is False
    adapter.send.assert_not_awaited()
    channel.failure = None
    await runner._task_card_publish(state)
    assert adapter._status_cards
    adapter.send.assert_not_awaited()
    await adapter._stop_status_card_refresh(next(iter(adapter._status_cards.values())))


@pytest.mark.asyncio
async def test_delayed_permanent_edit_failure_disables_card_and_sends_fallback(tmp_path, monkeypatch):
    adapter, channel = make_adapter(tmp_path, monkeypatch)
    metadata = run_metadata()
    await adapter.send_native_task_card_progress(
        "channel-1", [{"id": "1", "title": "Inspect", "status": "running"}],
        metadata=metadata, fallback_text="Inspect · running",
    )
    card = next(iter(adapter._status_cards.values()))
    card.coalescer._interval = 0.02
    card.coalescer._last_edit = time.monotonic()
    channel.message.failures = [DiscordFailure("forbidden", status=403)]
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="fallback-1"))

    result = await adapter.send_native_task_card_progress(
        "channel-1", [{"id": "1", "title": "Inspect", "status": "running"}],
        metadata=metadata, fallback_text="Inspect · running",
    )
    await asyncio.sleep(0.05)

    assert result.success is True
    assert not adapter._status_cards
    assert json.loads(adapter._status_card_persistence_path.read_text()) == []
    adapter.send.assert_awaited_once_with(
        "channel-1", "Inspect · running", metadata=metadata,
    )


@pytest.mark.asyncio
async def test_delayed_unknown_edit_failure_retries_on_next_frame(tmp_path, monkeypatch):
    adapter, channel = make_adapter(tmp_path, monkeypatch)
    metadata = run_metadata()
    await adapter.send_native_task_card_progress(
        "channel-1", [{"id": "1", "title": "Inspect", "status": "running"}], metadata=metadata,
    )
    card = next(iter(adapter._status_cards.values()))
    card.coalescer._interval = 0.02
    card.coalescer._last_edit = time.monotonic()
    channel.message.failures = [ConnectionError("offline")]
    adapter.send = AsyncMock()

    first = await adapter.send_native_task_card_progress(
        "channel-1", [{"id": "1", "title": "Inspect", "status": "running"}], metadata=metadata,
    )
    await asyncio.sleep(0.05)
    second = await adapter.send_native_task_card_progress(
        "channel-1", [{"id": "1", "title": "Inspect", "status": "completed"}], metadata=metadata,
    )

    assert first.success is True
    assert second.success is True
    assert adapter._status_cards
    adapter.send.assert_not_awaited()
    assert len(channel.message.edits) == 2


@pytest.mark.asyncio
async def test_restart_reconciliation_edits_and_removes_leftover(tmp_path, monkeypatch):
    adapter, channel = make_adapter(tmp_path, monkeypatch)
    path = adapter._status_card_persistence_path
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps([{
        "channel_id": "thread-1",
        "message_id": "100",
        "nonce": "0123456789abcdef",
        "started_at": time.time() - 10,
    }]))

    await adapter._reconcile_status_cards()

    assert "gateway restarted" in view_texts(channel.message.edits[-1]["view"])[1]
    assert json.loads(path.read_text()) == []


@pytest.mark.asyncio
async def test_reconnect_reconciliation_skips_live_card(tmp_path, monkeypatch):
    adapter, channel = make_adapter(tmp_path, monkeypatch)
    metadata = run_metadata()
    await adapter.send_native_task_card_progress(
        "channel-1", [{"id": "1", "title": "Inspect", "status": "running"}], metadata=metadata,
    )
    channel.message.edits.clear()

    await adapter._reconcile_status_cards()

    assert channel.message.edits == []
    assert adapter._status_cards
    assert json.loads(adapter._status_card_persistence_path.read_text())


@pytest.mark.asyncio
async def test_turn_end_sweeps_orphans_without_touching_current_cards(tmp_path, monkeypatch):
    channel = FakeChannel(new_message_each_send=True)
    adapter, _channel = make_adapter(tmp_path, monkeypatch, channel=channel)
    orphan = run_metadata(
        generation=1, nonce="1111111111111111", session_key="session-orphan", thread_id="thread-1",
    )
    current = run_metadata(
        generation=2, nonce="2222222222222222", session_key="session-current", thread_id="thread-2",
    )
    ended = run_metadata(
        generation=3, nonce="3333333333333333", session_key="session-ended", thread_id="thread-3",
    )
    await adapter.send_native_task_card_progress(
        "channel-1", [{"id": "1", "title": "Old", "status": "running"}], metadata=orphan,
    )
    orphan_message = channel.message
    await adapter.send_native_task_card_progress(
        "channel-1", [{"id": "2", "title": "Live", "status": "running"}], metadata=current,
    )
    current_message = channel.message
    persisted_message = FakeMessage("102")
    channel.messages.append(persisted_message)
    adapter._status_card_persisted["4444444444444444"] = {
        "channel_id": "thread-4", "message_id": "102", "nonce": "4444444444444444",
        "session_key": "session-persisted", "generation": 4, "started_at": time.time(),
    }
    await adapter._persist_status_cards()
    adapter.set_gateway_controls(SimpleNamespace(
        is_current=lambda session_key, generation: (
            session_key, generation
        ) == ("session-current", 2),
    ))

    await adapter.on_turn_end("channel-1", metadata=ended, outcome="done")

    assert "interrupted" in view_texts(orphan_message.edits[-1]["view"])[1]
    assert current_message.edits == []
    assert "running" in view_texts(channel.sends[1]["view"])[1]
    assert "gateway restarted" in view_texts(persisted_message.edits[-1]["view"])[1]
    assert set(adapter._status_card_keys_by_nonce) == {"2222222222222222"}
    assert set(adapter._status_card_persisted) == {"2222222222222222"}


@pytest.mark.asyncio
async def test_reconciliation_failures_accumulate_across_connects(tmp_path, monkeypatch):
    adapter, channel = make_adapter(tmp_path, monkeypatch)
    path = adapter._status_card_persistence_path
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps([{
        "channel_id": "thread-1",
        "message_id": "100",
        "nonce": "0123456789abcdef",
        "started_at": time.time() - 10,
        "reconcile_attempts": 0,
    }]))
    channel.message.failures = [ConnectionError("offline") for _ in range(3)]

    await adapter._reconcile_status_cards()
    assert json.loads(path.read_text())[0]["reconcile_attempts"] == 1
    await adapter._reconcile_status_cards()
    assert json.loads(path.read_text())[0]["reconcile_attempts"] == 2
    await adapter._reconcile_status_cards()
    assert json.loads(path.read_text()) == []


@pytest.mark.asyncio
async def test_stale_stop_defers_before_followup(tmp_path, monkeypatch):
    adapter, _channel = make_adapter(tmp_path, monkeypatch)
    order = []
    interaction = SimpleNamespace(
        client=SimpleNamespace(_hermes_discord_adapter=adapter),
        user=SimpleNamespace(id=7, roles=[]),
        response=SimpleNamespace(defer=AsyncMock(side_effect=lambda **_kwargs: order.append("defer"))),
        followup=SimpleNamespace(send=AsyncMock(side_effect=lambda *_args, **_kwargs: order.append("followup"))),
    )

    await StopCardButton("aaaaaaaaaaaaaaaa").callback(interaction)

    assert order == ["defer", "followup"]
    assert interaction.followup.send.await_args.args == ("nothing running",)


@pytest.mark.asyncio
async def test_unauthorized_stop_never_calls_gateway_control(tmp_path, monkeypatch):
    adapter, _channel = make_adapter(tmp_path, monkeypatch)
    metadata = run_metadata()
    await adapter.send_native_task_card_progress(
        "channel-1", [{"id": "1", "title": "Inspect", "status": "running"}], metadata=metadata,
    )
    controls = SimpleNamespace(stop_if_current=AsyncMock(return_value=True))
    adapter.set_gateway_controls(controls)
    interaction = SimpleNamespace(
        client=SimpleNamespace(_hermes_discord_adapter=adapter),
        user=SimpleNamespace(id=7, roles=[]),
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )

    with patch("gateway.pairing.PairingStore") as store:
        store.return_value.is_approved.return_value = False
        await StopCardButton("0123456789abcdef").callback(interaction)

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    controls.stop_if_current.assert_not_awaited()


@pytest.mark.asyncio
async def test_authorized_stop_defers_then_calls_current_run_control(tmp_path, monkeypatch):
    adapter, _channel = make_adapter(tmp_path, monkeypatch)
    adapter._allowed_user_ids = {"7"}
    metadata = run_metadata()
    await adapter.send_native_task_card_progress(
        "channel-1", [{"id": "1", "title": "Inspect", "status": "running"}], metadata=metadata,
    )
    order = []

    async def stop_if_current(*_args):
        order.append("control")
        return True

    adapter.set_gateway_controls(SimpleNamespace(stop_if_current=stop_if_current))
    interaction = SimpleNamespace(
        client=SimpleNamespace(_hermes_discord_adapter=adapter),
        user=SimpleNamespace(id=7, roles=[]),
        response=SimpleNamespace(defer=AsyncMock(side_effect=lambda **_kwargs: order.append("defer"))),
        followup=SimpleNamespace(send=AsyncMock(side_effect=lambda *_args, **_kwargs: order.append("followup"))),
    )

    await StopCardButton("0123456789abcdef").callback(interaction)

    assert order == ["defer", "control", "followup"]


@pytest.mark.asyncio
async def test_show_all_nonce_is_generated_separately_on_terminalization(tmp_path, monkeypatch):
    adapter, channel = make_adapter(tmp_path, monkeypatch)
    adapter._allowed_user_ids = {"7"}
    metadata = run_metadata()
    tasks = [{"title": f"Task {index}", "status": "complete"} for index in range(9)]
    await adapter.send_native_task_card_progress("channel-1", tasks, metadata=metadata)

    assert await adapter.finalize_native_task_card(
        "channel-1", outcome="done", reply_to=None, metadata=metadata,
    ) is True

    button = view_buttons(channel.message.edits[-1]["view"])[0]
    history_nonce = button.custom_id.rsplit(":", 1)[-1]
    assert button.label == "Show all"
    assert re.fullmatch(r"[0-9a-f]{32}", history_nonce)
    assert history_nonce != metadata["hermes_run"]["nonce"]
    stored = json.loads(adapter._status_card_history_path.read_text())
    assert stored[0]["nonce"] == history_nonce
    assert stored[0]["allowed_user_ids"] == ["7"]


@pytest.mark.asyncio
async def test_show_all_callback_defers_before_adapter_io(tmp_path, monkeypatch):
    adapter, _channel = make_adapter(tmp_path, monkeypatch)
    order = []

    async def handle(_interaction, _nonce):
        order.append("io")

    adapter.handle_status_card_show_all = handle
    interaction = show_all_interaction(adapter, order=order)

    await ShowAllCardButton("a" * 32).callback(interaction)

    assert order == ["defer", "io"]
    interaction.response.defer.assert_awaited_once_with(ephemeral=True)


@pytest.mark.asyncio
async def test_show_all_rejects_unauthorized_principal(tmp_path, monkeypatch):
    adapter, _channel = make_adapter(tmp_path, monkeypatch)
    adapter._allowed_user_ids = {"42"}
    metadata = run_metadata()
    tasks = [{"title": f"Task {index}", "status": "complete"} for index in range(9)]
    await adapter.send_native_task_card_progress("channel-1", tasks, metadata=metadata)
    await adapter.finalize_native_task_card(
        "channel-1", outcome="done", reply_to=None, metadata=metadata,
    )
    nonce = next(iter(adapter._status_card_history))
    interaction = show_all_interaction(adapter, user_id=7)

    with patch("gateway.pairing.PairingStore") as store:
        store.return_value.is_approved.return_value = False
        await ShowAllCardButton(nonce).callback(interaction)

    interaction.followup.send.assert_awaited_once()
    assert "allowed list" in interaction.followup.send.await_args.args[0].lower()
    assert "file" not in interaction.followup.send.await_args.kwargs


@pytest.mark.asyncio
async def test_show_all_uses_current_acl_after_owner_revocation(tmp_path, monkeypatch):
    adapter, _channel = make_adapter(tmp_path, monkeypatch)
    adapter._allowed_user_ids = {"7"}
    metadata = run_metadata()
    metadata["owner_user_id"] = "7"
    tasks = [{"title": f"Task {index}", "status": "complete"} for index in range(9)]
    await adapter.send_native_task_card_progress("channel-1", tasks, metadata=metadata)
    await adapter.finalize_native_task_card(
        "channel-1", outcome="done", reply_to=None, metadata=metadata,
    )
    nonce = next(iter(adapter._status_card_history))
    adapter._allowed_user_ids = {"42"}
    former_owner = show_all_interaction(adapter, user_id=7)
    current_principal = show_all_interaction(adapter, user_id=42)

    with patch("gateway.pairing.PairingStore") as store:
        store.return_value.is_approved.return_value = False
        await ShowAllCardButton(nonce).callback(former_owner)
        await ShowAllCardButton(nonce).callback(current_principal)

    assert "allowed list" in former_owner.followup.send.await_args.args[0].lower()
    assert "file" in current_principal.followup.send.await_args.kwargs


@pytest.mark.asyncio
async def test_show_all_empty_current_acl_fails_closed_for_owner(tmp_path, monkeypatch):
    adapter, _channel = make_adapter(tmp_path, monkeypatch)
    adapter._allowed_user_ids = {"7"}
    metadata = run_metadata()
    metadata["owner_user_id"] = "7"
    tasks = [{"title": f"Task {index}", "status": "complete"} for index in range(9)]
    await adapter.send_native_task_card_progress("channel-1", tasks, metadata=metadata)
    await adapter.finalize_native_task_card(
        "channel-1", outcome="done", reply_to=None, metadata=metadata,
    )
    nonce = next(iter(adapter._status_card_history))
    adapter._allowed_user_ids = set()
    adapter._allowed_role_ids = set()
    interaction = show_all_interaction(adapter, user_id=7)

    with patch("gateway.pairing.PairingStore") as store:
        store.return_value.is_approved.return_value = True
        await ShowAllCardButton(nonce).callback(interaction)

    assert "allowed list" in interaction.followup.send.await_args.args[0].lower()
    assert "file" not in interaction.followup.send.await_args.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize("expired", [False, True])
async def test_show_all_reports_missing_or_expired_history(tmp_path, monkeypatch, expired):
    adapter, _channel = make_adapter(tmp_path, monkeypatch)
    nonce = "c" * 32
    if expired:
        path = adapter._status_card_history_path
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps([{
            "nonce": nonce,
            "finished_at": time.time() - 31 * 24 * 60 * 60,
            "rows": ["✓ Old"],
        }]))
    interaction = show_all_interaction(adapter)

    await ShowAllCardButton(nonce).callback(interaction)

    interaction.followup.send.assert_awaited_once_with("Detailed history expired.", ephemeral=True)


@pytest.mark.asyncio
async def test_show_all_sends_utf8_markdown_with_header_and_rows(tmp_path, monkeypatch):
    adapter, _channel = make_adapter(tmp_path, monkeypatch)
    adapter._allowed_user_ids = {"7"}
    metadata = run_metadata()
    tasks = [{"title": f"Task {index}", "status": "complete"} for index in range(9)]
    await adapter.send_native_task_card_progress(
        "channel-1", tasks, metadata=metadata, title="Hermes run",
    )
    await adapter.finalize_native_task_card(
        "channel-1", outcome="failed", reply_to=None, metadata=metadata,
    )
    nonce = next(iter(adapter._status_card_history))
    captured = {}

    def make_file(stream, *, filename):
        captured["data"] = stream.getvalue()
        captured["filename"] = filename
        return SimpleNamespace(filename=filename)

    monkeypatch.setattr(discord, "File", make_file)
    interaction = show_all_interaction(adapter)

    await ShowAllCardButton(nonce).callback(interaction)

    text = captured["data"].decode("utf-8")
    assert text.startswith("# Failed")
    assert "- State: failed" in text
    assert "- Elapsed:" in text
    assert "- Finished:" in text
    assert "- ✓ Task 0" in text
    assert "- ✓ Task 8" in text
    assert re.fullmatch(r"hermes-run-\d{8}T\d{6}Z\.md", captured["filename"])
    interaction.followup.send.assert_awaited_once()
    assert interaction.followup.send.await_args.kwargs["ephemeral"] is True


@pytest.mark.asyncio
async def test_fresh_adapter_loads_history_and_serves_show_all(tmp_path, monkeypatch):
    first, _channel = make_adapter(tmp_path, monkeypatch)
    first._allowed_user_ids = {"7"}
    metadata = run_metadata()
    tasks = [{"title": f"Task {index}", "status": "complete"} for index in range(9)]
    await first.send_native_task_card_progress("channel-1", tasks, metadata=metadata)
    await first.finalize_native_task_card(
        "channel-1", outcome="done", reply_to=None, metadata=metadata,
    )
    nonce = next(iter(first._status_card_history))

    fresh, _fresh_channel = make_adapter(tmp_path, monkeypatch)
    fresh._allowed_user_ids = {"7"}
    captured = {}

    def make_file(stream, *, filename):
        captured["data"] = stream.getvalue()
        return SimpleNamespace(filename=filename)

    monkeypatch.setattr(discord, "File", make_file)
    interaction = show_all_interaction(fresh)

    await ShowAllCardButton(nonce).callback(interaction)

    assert "Task 0" in captured["data"].decode("utf-8")
    assert nonce in fresh._status_card_history


@pytest.mark.asyncio
async def test_status_updates_edit_within_turn_and_isolate_generations(tmp_path, monkeypatch):
    adapter, _channel = make_adapter(tmp_path, monkeypatch)
    adapter.send = AsyncMock(side_effect=[
        SendResult(success=True, message_id="status-1"),
        SendResult(success=True, message_id="status-2"),
    ])
    adapter.edit_message = AsyncMock(return_value=SendResult(success=True, message_id="status-1"))
    first = run_metadata(generation=1)
    second = run_metadata(generation=2, nonce="2222222222222222")

    await adapter.send_or_update_status("channel-1", "fallback", "one", metadata=first)
    await adapter.send_or_update_status("channel-1", "fallback", "two", metadata=first)
    await adapter.on_turn_end("channel-1", metadata=first, outcome="done")
    late = await adapter.send_or_update_status("channel-1", "fallback", "late", metadata=first)
    await adapter.send_or_update_status("channel-1", "fallback", "new", metadata=second)

    assert adapter.send.await_count == 2
    adapter.edit_message.assert_awaited_once()
    assert late.success is False


@pytest.mark.asyncio
async def test_status_updates_delegate_to_send_when_disabled(tmp_path, monkeypatch):
    adapter, _channel = make_adapter(tmp_path, monkeypatch, enabled=False)
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="plain-1"))

    result = await adapter.send_or_update_status(
        "channel-1", "fallback", "plain", metadata=run_metadata(),
    )

    assert result.success is True
    adapter.send.assert_awaited_once_with("channel-1", "plain", metadata=run_metadata())


@pytest.mark.asyncio
async def test_approval_state_returns_to_running(tmp_path, monkeypatch):
    adapter, _channel = make_adapter(tmp_path, monkeypatch)
    metadata = run_metadata()
    await adapter.send_native_task_card_progress(
        "channel-1", [{"id": "1", "title": "Inspect", "status": "running"}], metadata=metadata,
    )

    await adapter._set_status_card_approval(metadata, True)
    card = next(iter(adapter._status_cards.values()))
    assert card.state == "waiting-approval"
    await adapter._set_status_card_approval(metadata, False)
    assert card.state == "running"


@pytest.mark.asyncio
async def test_activity_hook_reports_only_live_accepted_cards(tmp_path, monkeypatch):
    adapter, _channel = make_adapter(tmp_path, monkeypatch)
    metadata = run_metadata()

    assert await adapter.update_native_task_card_activity(
        "channel-1", elapsed_s=3, iteration=1, max_iterations=5, metadata=metadata,
    ) is False
    await adapter.send_native_task_card_progress(
        "channel-1", [{"id": "1", "title": "Inspect", "status": "running"}], metadata=metadata,
    )
    assert await adapter.update_native_task_card_activity(
        "channel-1", elapsed_s=3, iteration=1, max_iterations=5, metadata=metadata,
    ) is True


def test_complete_task_status_renders_check_marker():
    from plugins.platforms.discord.status_card import _TASK_MARKERS
    assert _TASK_MARKERS["complete"] == "✓"


def test_card_frame_elapsed_tracks_wall_clock(monkeypatch):
    import plugins.platforms.discord.status_card as status_card
    card = status_card.CardState.__new__(status_card.CardState)
    card.tasks, card.title, card.state, card.elapsed_s = [], "t", "running", 0.0
    card.iteration, card.max_iterations, card.started_at = 0, 0, 1000.0
    monkeypatch.setattr(status_card.time, "time", lambda: 1095.0)
    assert status_card.CardState.frame(card).elapsed_s == 95.0


def test_terminal_states_replace_the_running_title():
    from plugins.platforms.discord.status_card import render_status_card_texts
    kwargs = {"tasks": [], "title": "Hermes is working", "elapsed_s": 5, "iteration": 0, "max_iterations": 0}
    assert render_status_card_texts(state="running", **kwargs)[0] == "Hermes is working"
    assert render_status_card_texts(state="done", **kwargs)[0] == "**Done**"
    assert render_status_card_texts(state="failed", **kwargs)[0] == "**Failed**"
    assert render_status_card_texts(state="interrupted", **kwargs)[0] == "**Stopped**"
    assert render_status_card_texts(state="interrupted-restart", **kwargs)[0] == "**Interrupted**"


@pytest.mark.parametrize(
    ("state", "marker"),
    [("interrupted", "⏹"), ("interrupted-restart", "⏹"), ("failed", "✗")],
)
def test_terminal_states_replace_active_markers_without_changing_completed(state, marker):
    from plugins.platforms.discord.status_card import render_status_card_texts
    texts = render_status_card_texts(
        [
            {"title": "Active", "status": "running"},
            {"title": "Finished", "status": "completed"},
        ],
        title="Hermes run", state=state, elapsed_s=5, iteration=0, max_iterations=0,
    )

    assert f"{marker} Active" in texts
    assert "✓ Finished" in texts


def test_history_rows_redact_secrets_strip_queries_and_keep_compaction():
    hex_secret = "a" * 40
    base64_secret = "Zy9vK2Jhc2U2NF9Ub2tlbl9XaXRoXzMyQ2hhcnM="
    tasks = [
        {"title": "use sk-liveSecretValue", "status": "complete"},
        {"title": "use ghp_abcdefghijklmnopqrstuvwxyz", "status": "complete"},
        {"title": "use xoxb-123456789-secret", "status": "complete"},
        {"title": f"use {hex_secret}", "status": "complete"},
        {"title": f"use {base64_secret}", "status": "complete"},
        {"title": "Authorization Bearer abc.def-ghi", "status": "complete"},
        {"title": "fetch https://example.com/path?token=secret&x=1#section", "status": "complete"},
        {"title": "space\n" + "x" * 200, "status": "complete"},
    ]

    rows, omitted = status_card_history_rows(tasks, "done")
    joined = "\n".join(rows)

    assert omitted == 0
    assert joined.count("[REDACTED]") >= 6
    assert "liveSecretValue" not in joined
    assert "token=secret" not in joined
    assert "https://example.com/path" in joined
    assert "section" not in joined
    assert all(len(row) <= 122 for row in rows)


@pytest.mark.parametrize(
    ("raw", "forbidden", "expected"),
    [
        ("github_pat_11AA22BB33CC44DD55EE", ["11AA22BB"], "[REDACTED]"),
        ("gho_abcdefghijklmnopqrstuvwxyz", ["abcdefghijklmnopqrstuvwxyz"], "[REDACTED]"),
        ("ghu_abcdefghijklmnopqrstuvwxyz", ["abcdefghijklmnopqrstuvwxyz"], "[REDACTED]"),
        ("ghs_abcdefghijklmnopqrstuvwxyz", ["abcdefghijklmnopqrstuvwxyz"], "[REDACTED]"),
        ("ghr_abcdefghijklmnopqrstuvwxyz", ["abcdefghijklmnopqrstuvwxyz"], "[REDACTED]"),
        ("AKIA1234567890ABCDEF", ["AKIA1234567890ABCDEF"], "[REDACTED]"),
        ("ASIA1234567890ABCDEF", ["ASIA1234567890ABCDEF"], "[REDACTED]"),
        ("aws_secret=ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890abcd", ["ABCDEFGHIJKLMNOPQRSTUVWXYZ"], "aws_secret=[REDACTED]"),
        ("token=live-token", ["live-token"], "token=[REDACTED]"),
        ("API_KEY=live-api-key", ["live-api-key"], "API_KEY=[REDACTED]"),
        ("apikey=live-apikey", ["live-apikey"], "apikey=[REDACTED]"),
        ("secret=live-secret", ["live-secret"], "secret=[REDACTED]"),
        ("password=live-password", ["live-password"], "password=[REDACTED]"),
        ("passwd=live-passwd", ["live-passwd"], "passwd=[REDACTED]"),
        ("auth=live-auth", ["live-auth"], "auth=[REDACTED]"),
        (
            "https://alice:live-password@example.com/path",
            ["alice", "live-password"],
            "https://[REDACTED]@example.com/path",
        ),
        (
            "https://example.com/path?token=live#private",
            ["token=live", "private"],
            "https://example.com/path",
        ),
        (
            "https://discord.com/api/webhooks/123456/live-webhook-token",
            ["123456", "live-webhook-token"],
            "https://discord.com/api/webhooks/[REDACTED]",
        ),
    ],
)
def test_history_redaction_patterns(raw, forbidden, expected):
    rows, omitted = status_card_history_rows([{"title": raw, "status": "complete"}], "done")

    assert omitted == 0
    assert expected in rows[0]
    assert all(secret not in rows[0] for secret in forbidden)


def test_history_pruning_enforces_age_card_and_row_bounds():
    now = 2_000_000_000.0
    entries = [{
        "nonce": "expired",
        "finished_at": now - 31 * 24 * 60 * 60,
        "rows": ["old"],
    }]
    entries.extend({
        "nonce": f"recent-{index}",
        "finished_at": now - 1000 + index,
        "rows": [f"row-{row}" for row in range(301)],
    } for index in range(201))

    pruned = prune_status_card_history(entries, now=now)

    assert len(pruned) == 200
    assert pruned[0]["nonce"] == "recent-1"
    assert pruned[-1]["nonce"] == "recent-200"
    assert all(len(entry["rows"]) == 300 for entry in pruned)
    assert all(entry["rows_omitted"] == 1 for entry in pruned)


def test_history_markdown_stays_below_cap_with_truncation_notice():
    entry = {
        "title": "Hermes run",
        "state": "done",
        "elapsed": 12,
        "finished_at": 2_000_000_000.0,
        "rows": ["✓ " + "界" * 120 for _ in range(300)],
        "rows_omitted": 50,
    }

    data = build_status_card_history_markdown(entry, max_bytes=512)

    assert len(data) < 512
    assert "50 earlier rows omitted" in data.decode("utf-8")
    assert "Detailed history truncated." in data.decode("utf-8")


@pytest.mark.asyncio
async def test_history_persistence_uses_atomic_write(tmp_path, monkeypatch):
    adapter, _channel = make_adapter(tmp_path, monkeypatch)
    metadata = run_metadata()
    metadata["owner_user_id"] = "7"
    metadata["status_card_rows_omitted"] = 50
    tasks = [{"title": f"Task {index}", "status": "complete"} for index in range(9)]
    tasks[0]["title"] = "terminal token=persisted-secret"

    with patch("plugins.platforms.discord.adapter.atomic_json_write") as atomic_write:
        await adapter.send_native_task_card_progress("channel-1", tasks, metadata=metadata)
        await adapter.finalize_native_task_card(
            "channel-1", outcome="done", reply_to=None, metadata=metadata,
        )

    history_calls = [
        call for call in atomic_write.call_args_list
        if call.args[0] == adapter._status_card_history_path
    ]
    assert len(history_calls) == 1
    stored = history_calls[0].args[1][0]
    assert stored["owner_user_id"] == "7"
    assert len(stored["rows"]) == 9
    assert stored["rows_omitted"] == 50
    assert "persisted-secret" not in "\n".join(stored["rows"])
    assert "token=[REDACTED]" in stored["rows"][0]
    assert "interaction_token" not in stored


def test_history_markdown_heading_uses_terminal_title():
    from plugins.platforms.discord.status_card import build_status_card_history_markdown
    entry = {"title": "Hermes is working", "state": "done", "elapsed": 39, "finished_at": 1790969132.0, "rows": []}
    assert build_status_card_history_markdown(entry).decode("utf-8").startswith("# Done\n")
