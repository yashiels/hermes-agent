import pytest

from tools import clarify_gateway as cg


@pytest.fixture(autouse=True)
def clarify_state():
    with cg._lock:
        cg._entries.clear()
        cg._session_index.clear()
    yield
    with cg._lock:
        cg._entries.clear()
        cg._session_index.clear()


def test_mark_awaiting_text_if_pending():
    entry = cg.register('pending', 'session', 'Choose?', ['A', 'B'])
    assert cg.mark_awaiting_text_if_pending('pending') is True
    assert entry.awaiting_text
    assert not entry.event.is_set()


def test_mark_awaiting_text_if_pending_rejects_resolved():
    entry = cg.register('resolved', 'session', 'Choose?', ['A', 'B'])
    assert cg.resolve_gateway_clarify('resolved', 'A') is True
    assert cg.mark_awaiting_text_if_pending('resolved') is False
    assert not entry.awaiting_text
    assert entry.response == 'A'
    assert cg.mark_awaiting_text('resolved') is True
    assert entry.awaiting_text


def test_mark_awaiting_text_if_pending_rejects_unknown():
    assert cg.mark_awaiting_text_if_pending('unknown') is False
