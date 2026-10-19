import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from agent.i18n import t
from gateway.config import PlatformConfig
from plugins.platforms.whatsapp.adapter import WhatsAppAdapter
from tools import clarify_gateway as cg

PEER = '123@s.whatsapp.net'


class BridgeSession:
    def __init__(self):
        self.posts = []
        self.status = 200
        self.missing_id = False

    @asynccontextmanager
    async def post(self, url, *, json, **kwargs):
        self.posts.append((url.rsplit('/', 1)[-1], json))
        yield self

    async def json(self):
        return {'messageId': None if self.missing_id else f'poll-{len(self.posts)}'}

    async def text(self):
        return 'transport failure'


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    monkeypatch.setenv('WHATSAPP_ALLOW_ALL_USERS', 'true')
    instance = WhatsAppAdapter(PlatformConfig(enabled=True, extra={
        'session_path': str(tmp_path), 'dm_policy': 'open', 'group_policy': 'open',
    }))
    instance._bridge_unavailable = AsyncMock(return_value=None)
    instance._http_session = BridgeSession()
    with cg._lock:
        cg._entries.clear()
        cg._session_index.clear()
    yield instance
    with cg._lock:
        cg._entries.clear()
        cg._session_index.clear()


async def prompt(adapter, choices=None, multi=False, peer=PEER, cid='clarify'):
    choices = ['A', 'B'] if choices is None else choices
    entry = cg.register(cid, 'session', 'Choose?', choices, multi_select=multi)
    result = await adapter.send_clarify(peer, 'Choose?', choices, cid, 'session')
    return entry, result.message_id


async def vote(adapter, poll_id, selected, voter=PEER, **overrides):
    data = {'chatId': PEER, 'senderId': voter, 'messageId': 'vote', 'isGroup': False,
            'body': ', '.join(selected) or '[Poll update]', 'nativeType': 'pollUpdateMessage',
            'nativeMetadata': {'pollUpdate': {'pollId': poll_id, 'selectedOptions': selected}}}
    data.update(overrides)
    return await adapter._build_message_event(data)


@pytest.mark.asyncio
async def test_single_vote_and_late_deselect(adapter):
    entry, pid = await prompt(adapter)
    assert await vote(adapter, pid, ['B'], body='untrusted') is None
    assert entry.response == 'B'
    assert pid not in adapter._poll_bindings
    assert await vote(adapter, pid, []) is None
    assert await vote(adapter, pid, ['A']) is None
    assert entry.response == 'B'


@pytest.mark.asyncio
async def test_multi_first_tick_must_not_resolve(adapter):
    entry, pid = await prompt(adapter, multi=True)
    event = await vote(adapter, pid, ['A'])
    if event is not None:
        cg.attempt_text_response_for_session('session', event.text)
    assert not entry.event.is_set()
    assert event is None


@pytest.mark.asyncio
async def test_comma_label_resolves_intact(adapter):
    entry, pid = await prompt(adapter, choices=['apples, pears', 'bread'], multi=True)
    event = await vote(adapter, pid, ['apples, pears', '✅ Done'])
    if event is not None:
        cg.attempt_text_response_for_session('session', event.text)
    assert entry.response == json.dumps(['apples, pears'], ensure_ascii=False)
    assert event is None


@pytest.mark.asyncio
async def test_multi_replaces_selection_and_resolves_option_order(adapter):
    entry, pid = await prompt(adapter, ['A', 'B', 'é'], multi=True)
    for labels in [['A'], ['A', 'B'], ['B'], ['B']]:
        assert await vote(adapter, pid, labels) is None
        assert adapter._poll_bindings[pid].state.selection == labels
        assert not entry.event.is_set()
    await vote(adapter, pid, ['é', 'B', '✅ Done'])
    assert entry.response == json.dumps(['B', 'é'], ensure_ascii=False)
    assert adapter._http_session.posts[0][1]['selectableCount'] == 5


@pytest.mark.asyncio
async def test_done_only_skips(adapter):
    entry, pid = await prompt(adapter, multi=True)
    await vote(adapter, pid, ['✅ Done'])
    assert entry.response == cg.SKIPPED


@pytest.mark.asyncio
@pytest.mark.parametrize('multi,selection', [(False, ['✏️ Other']), (True, ['✅ Done', '✏️ Other'])])
async def test_other_awaits_text_once_and_can_change_vote(adapter, multi, selection):
    entry, pid = await prompt(adapter, multi=multi)
    await vote(adapter, pid, selection)
    assert entry.awaiting_text and not entry.event.is_set()
    assert adapter._http_session.posts[-1][1]['message'] == t('platform.whatsapp.clarify_type_answer')
    count = len(adapter._http_session.posts)
    await vote(adapter, pid, selection)
    assert len(adapter._http_session.posts) == count
    await vote(adapter, pid, ['A', '✅ Done'] if multi else ['A'])
    assert entry.event.is_set()


@pytest.mark.asyncio
async def test_empty_and_other_without_done_do_not_resolve(adapter):
    entry, pid = await prompt(adapter, multi=True)
    await vote(adapter, pid, ['✏️ Other'])
    await vote(adapter, pid, [])
    assert not entry.awaiting_text and not entry.event.is_set()
    assert adapter._poll_bindings[pid].state.selection == []
    assert len(adapter._http_session.posts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('finished', ['timeout', 'typed', 'reset'])
async def test_expired_and_text_answered_votes_retire(adapter, finished):
    entry, pid = await prompt(adapter)
    if finished == 'timeout':
        cg._entries.pop(entry.clarify_id)
    elif finished == 'typed':
        assert cg.resolve_text_response_for_session('session', 'A')
    else:
        cg.clear_session('session')
    assert await vote(adapter, pid, ['B']) is None
    assert pid in adapter._retired_poll_ids
    if finished == 'typed':
        assert entry.response == 'A'


@pytest.mark.asyncio
async def test_unknown_poll_flows_as_message(adapter):
    event = await vote(adapter, 'unknown', ['A'])
    assert event.text == 'A'
    assert event.metadata['whatsapp_native']['pollUpdate']['pollId'] == 'unknown'


@pytest.mark.asyncio
@pytest.mark.parametrize('peer,voter,accepted', [
    ('999@lid', PEER, True), (PEER, '999@lid', True),
    (PEER, '123:4@s.whatsapp.net', True), (PEER, '456@s.whatsapp.net', False),
])
async def test_voter_identity_uses_custom_session(adapter, peer, voter, accepted):
    (adapter._session_path / 'lid-mapping-999.json').write_text(json.dumps('123'))
    (adapter._session_path / 'lid-mapping-123_reverse.json').write_text(json.dumps('999'))
    entry, pid = await prompt(adapter, peer=peer)
    assert await vote(adapter, pid, ['A'], voter=voter, chatId=peer) is None
    assert entry.event.is_set() is accepted
    assert (pid in adapter._poll_bindings) is not accepted


@pytest.mark.asyncio
async def test_group_vote_cannot_resolve_bound_dm(adapter):
    entry, pid = await prompt(adapter)
    adapter._whatsapp_require_mention = lambda: False
    assert await vote(adapter, pid, ['A'], isGroup=True, chatId='group@g.us') is None
    assert not entry.event.is_set()


@pytest.mark.asyncio
async def test_eviction_ttl_and_retirement_bounds(adapter):
    for index in range(770):
        await prompt(adapter, cid=f'clarify-{index}')
    assert len(adapter._poll_bindings) == 256
    assert len(adapter._retired_poll_ids) == 512
    assert 'poll-514' in adapter._retired_poll_ids
    pid = next(iter(adapter._poll_bindings))
    adapter._poll_bindings[pid].created_at -= 86401
    assert await vote(adapter, pid, ['A']) is None
    assert pid in adapter._retired_poll_ids


@pytest.mark.asyncio
async def test_retire_card_sends_nothing(adapter):
    entry, pid = await prompt(adapter)
    await adapter.retire_clarify_card(entry.clarify_id, 'expired')
    assert pid in adapter._retired_poll_ids
    assert len(adapter._http_session.posts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('choices,multi', [
    ([str(i) for i in range(12)], False), ([str(i) for i in range(11)], True),
    (['✅ Done', 'A'], False), (['✏️ Other', 'A'], True), ([], False),
])
async def test_text_fallback_limits_reserved_and_open_ended(adapter, choices, multi):
    await prompt(adapter, choices=choices, multi=multi)
    assert adapter._http_session.posts[0][0] == 'send'
    assert not adapter._poll_bindings


@pytest.mark.asyncio
async def test_group_keeps_unbound_single_select_poll(adapter):
    await prompt(adapter, multi=True, peer='123@g.us')
    endpoint, payload = adapter._http_session.posts[0]
    assert endpoint == 'send-poll'
    assert payload['options'] == ['A', 'B']
    assert payload['selectableCount'] == 1
    assert not adapter._poll_bindings


@pytest.mark.asyncio
async def test_trim_and_deduplicate(adapter):
    await prompt(adapter, choices=[' A ', 'A', 'B', ' '])
    assert adapter._http_session.posts[0][1]['options'] == ['A', 'B', '✏️ Other']


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['missing_id', 'transport'])
async def test_failed_poll_binding_falls_back_to_text(adapter, failure):
    if failure == 'missing_id':
        adapter._http_session.missing_id = True
    else:
        adapter._http_session.status = 500
    await prompt(adapter)
    assert adapter._http_session.posts[-1][0] == 'send'
    assert not adapter._poll_bindings


@pytest.mark.asyncio
async def test_resolver_false_and_other_false_retire(adapter, monkeypatch):
    _entry, pid = await prompt(adapter)
    monkeypatch.setattr(cg, 'resolve_gateway_clarify', lambda *args: False)
    await vote(adapter, pid, ['A'])
    assert pid in adapter._retired_poll_ids
    _entry, pid = await prompt(adapter, cid='other')
    monkeypatch.setattr(cg, 'mark_awaiting_text_if_pending', lambda *args: False)
    count = len(adapter._http_session.posts)
    await vote(adapter, pid, ['✏️ Other'])
    assert pid in adapter._retired_poll_ids
    assert len(adapter._http_session.posts) == count



@pytest.mark.asyncio
@pytest.mark.parametrize('multi,count', [(False, 11), (True, 10)])
async def test_maximum_native_poll_choices(adapter, multi, count):
    await prompt(adapter, choices=[str(i) for i in range(count)], multi=multi)
    endpoint, payload = adapter._http_session.posts[0]
    assert endpoint == 'send-poll'
    assert len(payload['options']) == 12
    assert payload['selectableCount'] == (12 if multi else 1)


@pytest.mark.asyncio
async def test_intake_policy_precedes_vote_handler(adapter):
    entry, pid = await prompt(adapter)
    adapter._dm_policy = 'disabled'
    assert await vote(adapter, pid, ['A']) is None
    assert not entry.event.is_set()
    assert pid in adapter._poll_bindings


@pytest.mark.asyncio
async def test_reserved_labels_use_sent_poll_language(adapter, monkeypatch):
    from plugins.platforms.whatsapp import polls

    translations = {'platform.whatsapp.clarify_done': '✅ Terminé', 'platform.whatsapp.clarify_other': '✏️ Autre'}
    monkeypatch.setattr(polls, 't', lambda key: translations[key])
    entry, pid = await prompt(adapter, multi=True)
    monkeypatch.setattr(polls, 't', t)
    await vote(adapter, pid, ['A', '✅ Terminé'])
    assert entry.response == '["A"]'
    monkeypatch.setattr(polls, 't', lambda key: translations.get(key, t(key)))
    await prompt(adapter, choices=['✅ Terminé', 'A'], cid='collision')
    assert adapter._http_session.posts[-1][0] == 'send'


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,state_kind', [('clarify', 'approval'), ('approval', 'clarify'), ('approval', 'approval')])
async def test_kind_state_mismatch_and_unimplemented_kind_are_consumed(adapter, kind, state_kind, caplog):
    from plugins.platforms.whatsapp.polls import ApprovalState

    entry, pid = await prompt(adapter)
    binding = adapter._poll_bindings[pid]
    binding.kind = kind
    if state_kind == 'approval':
        binding.state = ApprovalState('request', {'A': 'once'})
    with caplog.at_level('DEBUG', logger='plugins.platforms.whatsapp.polls'):
        assert await vote(adapter, pid, ['A']) is None
    assert not entry.event.is_set()
    assert pid in adapter._poll_bindings
    assert 'unsupported poll kind or state mismatch' in caplog.text


@pytest.mark.asyncio
async def test_planned_poll_state_types_construct():
    from plugins.platforms.whatsapp.polls import ApprovalState, ChoiceState, ClarifyState, SlashConfirmState

    callback = AsyncMock(return_value='selected')
    states = [
        ClarifyState('clarify', True, ['A']),
        ApprovalState('request', {'Allow': 'once'}),
        SlashConfirmState('confirm', {'Cancel': 'cancel'}),
        ChoiceState('Choose', [{'label': 'A', 'value': 'a'}], 0, 10, {'A': 'a'}, callback),
    ]
    assert states[0].selection == ['A']
    assert states[1].label_to_choice['Allow'] == 'once'
    assert states[2].confirm_id == 'confirm'
    assert states[3].label_to_value['A'] == 'a'
    assert await states[3].on_choice_selected(PEER, 'a') == 'selected'
    callback.assert_awaited_once_with(PEER, 'a')


@pytest.mark.asyncio
@pytest.mark.parametrize('multi,selected', [(False, ['✏️ Other']), (True, ['✅ Done', '✏️ Other'])])
async def test_other_loses_to_typed_answer_atomically(adapter, monkeypatch, multi, selected):
    entry, pid = await prompt(adapter, multi=multi)
    atomic_mark = cg.mark_awaiting_text_if_pending

    def resolve_before_mark(clarify_id):
        assert cg.resolve_text_response_for_session('session', 'A')
        return atomic_mark(clarify_id)

    monkeypatch.setattr(cg, 'mark_awaiting_text_if_pending', resolve_before_mark)
    count = len(adapter._http_session.posts)
    assert await vote(adapter, pid, selected) is None
    assert len(adapter._http_session.posts) == count
    assert pid in adapter._retired_poll_ids
    assert pid not in adapter._poll_bindings
    assert not entry.awaiting_text
    assert entry.response == ('["A"]' if multi else 'A')
