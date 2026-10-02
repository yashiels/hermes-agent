import asyncio
import json
import queue
import sys
import time
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

from gateway.config import PlatformConfig
from gateway.platforms.base import SendResult
from gateway.run_turn_runner import TurnRunner
from plugins.platforms.discord.adapter import DiscordAdapter
from plugins.platforms.discord.status_card import (
    StatusCardCoalescer,
    StatusCardFrame,
    StatusCardView,
    StopCardButton,
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
    channel = channel or FakeChannel()
    monkeypatch.setattr(adapter, "_resolve_channel", AsyncMock(return_value=channel))
    return adapter, channel


def view_texts(view):
    return [item.content for item in view.walk_children() if hasattr(item, "content")]


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
    await coalescer.stop(flush=False)
    await asyncio.sleep(0.07)

    assert edits == ["running"]


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
