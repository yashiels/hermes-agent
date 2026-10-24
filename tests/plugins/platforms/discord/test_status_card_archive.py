import json
from unittest.mock import AsyncMock

import pytest

from gateway.platforms.base import SendResult
from tests.plugins.platforms.discord.test_status_card import (
    DiscordFailure,
    FakeChannel,
    FakeMessage,
    make_adapter,
    run_metadata,
    view_texts,
)


_ARCHIVED = DiscordFailure(
    "400 Bad Request (error code: 50083): Thread is archived", status=400, code=50083,
)


class ArchivableChannel(FakeChannel):
    def __init__(self, *, locked=False, unarchive_error=None):
        super().__init__()
        self.locked = locked
        self.archived = True
        self.unarchive_calls = 0
        self.unarchive_error = unarchive_error
        self.message = ArchiveMessage(self)
        self.messages = [self.message]

    async def edit(self, **kwargs):
        self.unarchive_calls += 1
        if self.unarchive_error is not None:
            raise self.unarchive_error
        if self.locked:
            raise _ARCHIVED
        if kwargs.get("archived") is False:
            self.archived = False


class ArchiveMessage(FakeMessage):
    def __init__(self, channel):
        super().__init__()
        self.channel = channel

    async def edit(self, **kwargs):
        if self.channel.archived:
            raise _ARCHIVED
        self.edits.append(kwargs)
        return self


async def _open_card(adapter):
    metadata = run_metadata()
    await adapter.send_native_task_card_progress(
        "channel-1", [{"id": "1", "title": "Inspect", "status": "running"}],
        metadata=metadata, fallback_text="Inspect · running",
    )
    return metadata


@pytest.mark.asyncio
async def test_archived_unlocked_thread_unarchives_and_retries_once(tmp_path, monkeypatch):
    channel = ArchivableChannel()
    adapter, _channel = make_adapter(tmp_path, monkeypatch, channel=channel)
    metadata = await _open_card(adapter)

    result = await adapter.send_native_task_card_progress(
        "channel-1", [{"id": "1", "title": "Inspect", "status": "complete"}],
        metadata=metadata,
    )

    assert result.success is True
    assert channel.unarchive_calls == 1
    assert channel.archived is False
    assert adapter._status_cards
    assert any("Inspect" in text for text in view_texts(channel.message.edits[-1]["view"]))


@pytest.mark.asyncio
async def test_finalize_unarchives_unlocked_thread_and_finishes_done(tmp_path, monkeypatch):
    channel = ArchivableChannel()
    adapter, _channel = make_adapter(tmp_path, monkeypatch, channel=channel)
    metadata = await _open_card(adapter)

    assert await adapter.finalize_native_task_card(
        "channel-1", outcome="done", reply_to=None, metadata=metadata,
    ) is True

    assert channel.unarchive_calls == 1
    texts = view_texts(channel.message.edits[-1]["view"])
    assert texts[0] == "**Done**"
    assert "done" in texts[1]


@pytest.mark.asyncio
async def test_locked_archived_thread_disables_the_card(tmp_path, monkeypatch):
    channel = ArchivableChannel(locked=True)
    adapter, _channel = make_adapter(tmp_path, monkeypatch, channel=channel)
    metadata = await _open_card(adapter)

    result = await adapter.send_native_task_card_progress(
        "channel-1", [{"id": "1", "title": "Inspect", "status": "complete"}],
        metadata=metadata,
    )

    assert result.success is False
    assert channel.unarchive_calls == 0
    assert channel.archived is True
    assert not adapter._status_cards


@pytest.mark.asyncio
async def test_failed_unarchive_disables_the_card(tmp_path, monkeypatch):
    channel = ArchivableChannel(unarchive_error=DiscordFailure("cannot unarchive", status=403, code=50001))
    adapter, _channel = make_adapter(tmp_path, monkeypatch, channel=channel)
    metadata = await _open_card(adapter)

    result = await adapter.send_native_task_card_progress(
        "channel-1", [{"id": "1", "title": "Inspect", "status": "complete"}],
        metadata=metadata,
    )

    assert result.success is False
    assert channel.unarchive_calls == 1
    assert channel.archived is True
    assert not adapter._status_cards


class PartialMessageable:
    id = "partial-1"


class ResolvedThread(FakeChannel):
    def __init__(self):
        super().__init__(message=OnceArchivedMessage())
        self.locked = False
        self.archived = True
        self.unarchive_calls = 0

    async def edit(self, **kwargs):
        self.unarchive_calls += 1
        if kwargs.get("archived") is False:
            self.archived = False


class OnceArchivedMessage(FakeMessage):
    def __init__(self):
        super().__init__()
        self.channel = PartialMessageable()
        self._raised = False

    async def edit(self, **kwargs):
        if not self._raised:
            self._raised = True
            raise _ARCHIVED
        self.edits.append(kwargs)
        return self


class ExhaustedArchiveMessage(FakeMessage):
    def __init__(self, channel):
        super().__init__()
        self.channel = channel

    async def edit(self, **kwargs):
        raise _ARCHIVED


class UnarchiveOnceChannel(FakeChannel):
    def __init__(self):
        super().__init__()
        self.locked = False
        self.archived = True
        self.unarchive_calls = 0
        self.message = ExhaustedArchiveMessage(self)
        self.messages = [self.message]

    async def edit(self, **kwargs):
        self.unarchive_calls += 1
        if kwargs.get("archived") is False:
            self.archived = False


@pytest.mark.asyncio
async def test_partial_message_channel_unarchives_resolved_thread(tmp_path, monkeypatch):
    thread = ResolvedThread()
    adapter, _channel = make_adapter(tmp_path, monkeypatch, channel=thread)
    metadata = await _open_card(adapter)
    assert not callable(getattr(thread.message.channel, "edit", None))

    result = await adapter.send_native_task_card_progress(
        "channel-1", [{"id": "1", "title": "Inspect", "status": "complete"}],
        metadata=metadata,
    )

    assert result.success is True
    assert thread.unarchive_calls == 1
    assert thread.archived is False
    assert adapter._resolve_channel.await_count >= 1
    assert any("Inspect" in text for text in view_texts(thread.message.edits[-1]["view"]))


@pytest.mark.asyncio
async def test_terminal_archive_recovery_happens_once(tmp_path, monkeypatch):
    channel = UnarchiveOnceChannel()
    adapter, _channel = make_adapter(tmp_path, monkeypatch, channel=channel)
    metadata = await _open_card(adapter)
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="notice-1"))

    assert await adapter.finalize_native_task_card(
        "channel-1", outcome="done", reply_to=None, metadata=metadata,
    ) is True

    assert channel.unarchive_calls == 1
    assert adapter.send.await_args.args[1] == "-# run done · status card could not be updated"
    assert not adapter._status_cards
    assert json.loads(adapter._status_card_persistence_path.read_text()) == []


@pytest.mark.asyncio
async def test_preserved_outcome_notice_and_history_keep_done(tmp_path, monkeypatch):
    channel = UnarchiveOnceChannel()
    adapter, _channel = make_adapter(tmp_path, monkeypatch, channel=channel)
    metadata = run_metadata()
    tasks = [
        {"id": str(index), "title": f"step {index}", "status": "complete"}
        for index in range(9)
    ]
    await adapter.send_native_task_card_progress(
        "channel-1", tasks, metadata=metadata, fallback_text="step · running",
    )
    card = next(iter(adapter._status_cards.values()))
    card.state = "done"
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="notice-1"))

    assert await adapter._terminalize_card_once(
        card, outcome="interrupted", metadata=metadata,
    ) is True

    assert adapter.send.await_args.args[1] == "-# run done · status card could not be updated"
    assert adapter._status_card_history
    assert {entry["state"] for entry in adapter._status_card_history.values()} == {"done"}
