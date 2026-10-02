import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run_adapters import GatewayAdapterLifecycleMixin
from gateway.run_turn import GatewayTurnMixin
from gateway.session import SessionSource


class HookAdapter(BasePlatformAdapter):
    def __init__(self, send_success=True):
        super().__init__(
            PlatformConfig(enabled=True, token="x", typing_indicator=False),
            Platform.DISCORD,
        )
        self.send_success = send_success
        self.hooks = []

    async def connect(self, *, is_reconnect=False):
        return True

    async def disconnect(self):
        return None

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(
            success=self.send_success,
            message_id="final-1" if self.send_success else None,
            error=None if self.send_success else "refused",
        )

    async def finalize_native_task_card(self, chat_id, *, outcome, reply_to, metadata):
        self.hooks.append(("finalize", outcome, metadata["hermes_run"]))
        return True

    async def on_turn_end(self, chat_id, *, metadata, outcome):
        self.hooks.append(("end", outcome, metadata["hermes_run"]))


@pytest.mark.asyncio
async def test_run_token_propagates_to_progress_and_status_metadata():
    token = {"session_key": "agent:main:discord:dm:1", "generation": 7, "nonce": "0123456789abcdef"}
    runner = SimpleNamespace(
        _run_agent_progress_threading=lambda *_args: (
            {"non_conversational": True}, "reply-1", {"thread_id": "thread-1"}
        ),
        hooks=object(),
    )
    callback = lambda *_args: None
    turn_runner = SimpleNamespace(
        _step_callback_sync=callback,
        _event_callback_sync=callback,
        _status_callback_sync=callback,
    )
    turn_ctx = SimpleNamespace()
    source = SessionSource(platform=Platform.DISCORD, chat_id="1", chat_type="dm")
    runner._delivery_adapter_for = lambda _source: object()

    status_metadata = GatewayTurnMixin._run_agent_bind_turn_wiring(
        runner, turn_ctx, turn_runner, source, "reply-1", True, token,
    )

    assert turn_ctx._progress_metadata["hermes_run"] == token
    assert status_metadata["hermes_run"] == token
    assert turn_ctx._progress_metadata["hermes_run"] is not token
    assert status_metadata["hermes_run"] is not token


def test_generic_native_card_gate_accepts_discord_and_keeps_explicit_off():
    adapter = SimpleNamespace(native_task_cards_enabled=lambda: True)

    assert GatewayTurnMixin._native_task_cards_enabled_for_turn(adapter, False, "off") is True
    assert GatewayTurnMixin._native_task_cards_enabled_for_turn(adapter, True, "off") is False
    assert GatewayTurnMixin._native_task_cards_enabled_for_turn(adapter, True, "all") is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("preliminary", "interrupt", "send_success", "expected"),
    [
        ("done", False, True, "done"),
        ("failed", False, True, "failed"),
        ("done", True, True, "interrupted"),
        ("done", False, False, "failed"),
    ],
)
async def test_outer_delivery_cleanup_finalizes_once_with_outcome(
    preliminary, interrupt, send_success, expected,
):
    adapter = HookAdapter(send_success=send_success)
    token = {"session_key": "session-1", "generation": 3, "nonce": "0123456789abcdef"}

    async def handler(event):
        event._hermes_run = token
        event._hermes_turn_outcome = preliminary
        if interrupt:
            adapter._active_sessions["session-1"].set()
        return "final response"

    adapter.set_message_handler(handler)
    source = SessionSource(platform=Platform.DISCORD, chat_id="1", chat_type="dm", user_id="2")
    event = MessageEvent(text="hello", message_type=MessageType.TEXT, source=source, message_id="input-1")
    adapter._active_sessions["session-1"] = asyncio.Event()

    await adapter._process_message_background(event, "session-1")

    assert [(name, outcome) for name, outcome, _token in adapter.hooks] == [
        ("finalize", expected),
        ("end", expected),
    ]
    assert all(seen_token == token for _name, _outcome, seen_token in adapter.hooks)


@pytest.mark.asyncio
async def test_stop_control_checks_generation_and_uses_stored_source(monkeypatch):
    source = SessionSource(platform=Platform.DISCORD, chat_id="1", chat_type="dm")
    state = SimpleNamespace(
        turn=SimpleNamespace(agent=object(), event=SimpleNamespace(source=source)),
        persistent=SimpleNamespace(run_generation=4),
    )
    runner = SimpleNamespace(
        _peek_session_state=lambda _key: state,
        _interrupt_and_clear_session=AsyncMock(),
    )
    monkeypatch.setitem(sys.modules, "gateway.run", SimpleNamespace(_INTERRUPT_REASON_STOP="Stop requested"))

    assert await GatewayAdapterLifecycleMixin._gateway_stop_if_current(runner, "session-1", 3) is False
    assert await GatewayAdapterLifecycleMixin._gateway_stop_if_current(runner, "session-1", 4) is True
    runner._interrupt_and_clear_session.assert_awaited_once()
    assert runner._interrupt_and_clear_session.await_args.args[:2] == ("session-1", source)


@pytest.mark.asyncio
async def test_retry_control_requires_idle_matching_last_message():
    source = SessionSource(
        platform=Platform.DISCORD, chat_id="1", chat_type="dm", user_id="2", user_name="Owner",
    )

    async def handle_message(event):
        event._gateway_accepted = True

    adapter = SimpleNamespace(handle_message=handle_message)
    runner = SimpleNamespace(
        _last_final_message_ids={"session-1": "final-1"},
        _is_session_running=lambda _key: False,
        _get_cached_session_source=lambda _key: source,
        _delivery_adapter_for=lambda _source: adapter,
    )

    assert await GatewayAdapterLifecycleMixin._gateway_retry_if_last(
        runner, "session-1", "stale",
    ) is False
    assert await GatewayAdapterLifecycleMixin._gateway_retry_if_last(
        runner, "session-1", "final-1",
    ) is True


@pytest.mark.asyncio
async def test_final_message_recording_precedes_adapter_hook():
    seen = []

    async def on_final_message(chat_id, message_id, *, metadata):
        seen.append((chat_id, message_id, metadata))

    adapter = SimpleNamespace(on_final_message=on_final_message)
    runner = SimpleNamespace()
    metadata = {"hermes_run": {"session_key": "session-1", "generation": 1, "nonce": "a" * 16}}

    await GatewayAdapterLifecycleMixin._record_final_message(
        runner, adapter, "channel-1", "final-1", session_key="session-1", metadata=metadata,
    )

    assert runner._last_final_message_ids["session-1"] == "final-1"
    assert seen == [("channel-1", "final-1", metadata)]
