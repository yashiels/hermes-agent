import asyncio
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from tests.plugins.platforms.discord.test_status_card import (
    FakeChannel,
    make_adapter,
    run_metadata,
    view_texts,
)


def _outcome(runner, session_key, generation, agent_result):
    return (
        "interrupted"
        if not runner._is_session_run_current(session_key, generation)
        or bool(isinstance(agent_result, dict) and agent_result.get("interrupted"))
        else "failed"
        if bool(isinstance(agent_result, dict) and agent_result.get("failed"))
        else "done"
    )


def _event(metadata):
    source = SessionSource(
        platform=Platform.DISCORD, chat_id="channel-1", chat_type="thread", thread_id="thread-1",
    )
    event = MessageEvent(text="status", source=source, message_id="message-1")
    event._hermes_run = metadata["hermes_run"]
    return event


async def _open_card(adapter, metadata):
    await adapter.send_native_task_card_progress(
        "channel-1", [{"id": "1", "title": "Inspect", "status": "running"}], metadata=metadata,
    )


@pytest.mark.asyncio
async def test_normal_completion_stays_done_after_orphan_sweep(tmp_path, monkeypatch):
    adapter, channel = make_adapter(tmp_path, monkeypatch)
    metadata = run_metadata()
    token = metadata["hermes_run"]
    runner = GatewayRunner(config=GatewayConfig())
    generation = runner._begin_session_run_generation(token["session_key"])
    token["generation"] = generation
    agent_result = {"final_response": "shipped", "interrupted": False, "failed": False}
    preliminary = _outcome(runner, token["session_key"], generation, agent_result)
    assert preliminary == "done"
    await _open_card(adapter, metadata)
    channel.message.failures = [ConnectionError("offline")] * 12
    adapter.send = AsyncMock(return_value=SendResult(success=False, error="offline", retryable=True))
    event = _event(metadata)
    event._hermes_turn_outcome = preliminary

    await adapter._run_gateway_turn_end_hooks(
        event, token["session_key"], asyncio.Event(), metadata, True, True,
    )
    card = next(iter(adapter._status_cards.values()))
    assert card.state == "done"
    assert card.terminal_completed is False
    channel.message.failures.clear()

    await adapter._sweep_orphaned_status_cards("other-nonce")

    assert view_texts(channel.message.edits[-1]["view"])[0] == "**Done**"


@pytest.mark.asyncio
async def test_stopped_run_terminalizes_interrupted(tmp_path, monkeypatch):
    adapter, channel = make_adapter(tmp_path, monkeypatch)
    metadata = run_metadata()
    token = metadata["hermes_run"]
    runner = GatewayRunner(config=GatewayConfig())
    generation = runner._begin_session_run_generation(token["session_key"])
    token["generation"] = generation
    runner._invalidate_session_run_generation(token["session_key"], reason="native_stop_control")
    agent_result = {"final_response": "", "interrupted": True, "failed": False}
    preliminary = _outcome(runner, token["session_key"], generation, agent_result)
    assert preliminary == "interrupted"
    await _open_card(adapter, metadata)
    event = _event(metadata)
    event._hermes_turn_outcome = preliminary
    interrupt = asyncio.Event()
    interrupt.set()

    await adapter._run_gateway_turn_end_hooks(
        event, token["session_key"], interrupt, metadata, True, True,
    )

    assert view_texts(channel.message.edits[-1]["view"])[0] == "**Stopped**"


@pytest.mark.asyncio
async def test_failed_run_stays_failed_after_orphan_sweep(tmp_path, monkeypatch):
    adapter, channel = make_adapter(tmp_path, monkeypatch)
    metadata = run_metadata()
    token = metadata["hermes_run"]
    runner = GatewayRunner(config=GatewayConfig())
    generation = runner._begin_session_run_generation(token["session_key"])
    token["generation"] = generation
    agent_result = {"final_response": "", "interrupted": False, "failed": True}
    preliminary = _outcome(runner, token["session_key"], generation, agent_result)
    assert preliminary == "failed"
    await _open_card(adapter, metadata)
    channel.message.failures = [ConnectionError("offline")] * 12
    adapter.send = AsyncMock(return_value=SendResult(success=False, error="offline", retryable=True))
    event = _event(metadata)
    event._hermes_turn_outcome = preliminary

    await adapter._run_gateway_turn_end_hooks(
        event, token["session_key"], asyncio.Event(), metadata, True, False,
    )
    card = next(iter(adapter._status_cards.values()))
    assert card.state == "failed"
    channel.message.failures.clear()

    await adapter._sweep_orphaned_status_cards("other-nonce")

    assert view_texts(channel.message.edits[-1]["view"])[0] == "**Failed**"


@pytest.mark.asyncio
async def test_superseded_running_card_stays_interrupted(tmp_path, monkeypatch):
    channel = FakeChannel(new_message_each_send=True)
    adapter, _channel = make_adapter(tmp_path, monkeypatch, channel=channel)
    first = run_metadata(nonce="1111111111111111")
    second = run_metadata(nonce="2222222222222222", generation=2)
    await _open_card(adapter, first)
    original = channel.messages[0]

    await adapter.send_native_task_card_progress(
        "channel-1", [{"id": "2", "title": "Next", "status": "running"}], metadata=second,
    )

    assert view_texts(original.edits[-1]["view"])[0] == "**Stopped**"
