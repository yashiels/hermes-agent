import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, writeFile, stat, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { createHash, randomBytes } from 'node:crypto';
import { decryptPollVote, hmacSign, aesEncryptGCM, getAggregateVotesInPollMessage, proto } from '@whiskeysockets/baileys';
import { buildPollPayload, serializePollRecord, parsePollRecords, prunePollRecords, rememberSentMessage, createBoundedMessageStore } from './bridge_helpers.js';
import { createPollStore } from './poll_store.js';

const payload = count => buildPollPayload({ question: 'Which fruit?', options: ['apples, pears', 'é'], selectableCount: count });

test('poll records preserve secret, prune expired and missing names, and cap newest', () => {
  const now = Date.now();
  const original = payload(1);
  const record = serializePollRecord('p', '123@s.whatsapp.net', original, now);
  const parsed = parsePollRecords(JSON.stringify([record]), now);
  assert.deepEqual(Buffer.from(parsed[0].messageSecret, 'base64'), original.poll.messageSecret);
  assert.deepEqual(prunePollRecords([{ ...record, name: undefined }], now), []);
  assert.deepEqual(prunePollRecords([{ ...record, createdAt: now - 7 * 86400000 - 1 }], now), []);
  const records = Array.from({ length: 260 }, (_, i) => ({ ...record, id: String(i), createdAt: now - 260 + i }));
  const pruned = prunePollRecords(records, now);
  assert.equal(pruned.length, 256);
  assert.equal(pruned[0].id, '4');
  assert.throws(() => parsePollRecords('{bad'));
  assert.throws(() => parsePollRecords('{}'));
});

for (const count of [1, 2]) {
  test(`live and disk-seeded entries match and decrypted vote aggregates by name (count ${count})`, async () => {
    const dir = await mkdtemp(path.join(tmpdir(), 'wa-polls-'));
    try {
      const file = path.join(dir, 'hermes-polls.json');
      const original = payload(count);
      const pollMsgId = 'poll-123';
      const pollCreatorJid = '111@s.whatsapp.net';
      const voterJid = '222@s.whatsapp.net';
      const live = createBoundedMessageStore();
      rememberSentMessage(live, {
        key: { id: pollMsgId, remoteJid: voterJid, fromMe: true },
        message: { conversation: 'Baileys message must not override the poll payload' },
      }, original, voterJid);
      const store = await createPollStore({ file, messageStore: createBoundedMessageStore(), rememberSentId() {} });
      store.remember(pollMsgId, voterJid, original);
      await store.flush();
      assert.equal((await stat(file)).mode & 0o777, 0o600);
      assert.equal(JSON.parse(await readFile(file, 'utf8'))[0].name, original.poll.name);
      const seeded = createBoundedMessageStore();
      const sentIds = new Set();
      await createPollStore({ file, messageStore: seeded, rememberSentId: id => sentIds.add(id) });
      const creation = seeded.get(pollMsgId);
      assert.deepEqual(creation.key, { id: pollMsgId, remoteJid: voterJid, fromMe: true });
      assert.deepEqual(creation, live.get(pollMsgId));
      assert.ok(sentIds.has(pollMsgId));
      const optionHash = createHash('sha256').update('apples, pears').digest();
      const bytes = proto.Message.PollVoteMessage.encode({ selectedOptions: [optionHash] }).finish();
      const key0 = hmacSign(original.poll.messageSecret, new Uint8Array(32), 'sha256');
      const key = hmacSign(Buffer.concat([Buffer.from(pollMsgId + pollCreatorJid + voterJid + 'Poll Vote'), Buffer.from([1])]), key0, 'sha256');
      const encIv = randomBytes(12);
      const encPayload = aesEncryptGCM(bytes, key, encIv, Buffer.from(`${pollMsgId}\0${voterJid}`));
      const decoded = decryptPollVote({ encPayload, encIv }, {
        pollCreatorJid, pollMsgId, voterJid, pollEncKey: creation.message.messageContextInfo.messageSecret,
      });
      assert.deepEqual(Buffer.from(decoded.selectedOptions[0]), optionHash);
      const aggregation = getAggregateVotesInPollMessage({
        message: creation.message,
        pollUpdates: [{ pollUpdateMessageKey: { id: 'vote-123', remoteJid: voterJid, fromMe: false }, vote: decoded }],
      });
      assert.deepEqual(aggregation.filter(option => option.voters.length > 0), [
        { name: 'apples, pears', voters: [voterJid] },
      ]);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });
}

test('corrupt and unreadable stores log once and start empty; write errors do not escape', async () => {
  const dir = await mkdtemp(path.join(tmpdir(), 'wa-polls-'));
  try {
    const file = path.join(dir, 'hermes-polls.json');
    await writeFile(file, 'corrupt');
    for (const badFile of [file, dir]) {
      const logs = [];
      await createPollStore({ file: badFile, messageStore: createBoundedMessageStore(), rememberSentId() {}, log: line => logs.push(line) });
      assert.equal(logs.length, 1);
    }
    const logs = [];
    const store = await createPollStore({ file: path.join(dir, 'missing', 'polls.json'), messageStore: createBoundedMessageStore(), rememberSentId() {}, log: line => logs.push(line) });
    assert.doesNotThrow(() => store.remember('p', '123@s.whatsapp.net', payload(1)));
    await store.flush();
    assert.equal(logs.length, 1);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test('live non-poll sends retain the Baileys message and key', () => {
  const store = createBoundedMessageStore();
  const sent = { key: { id: 'text-1', remoteJid: '123@s.whatsapp.net', fromMe: true }, message: { conversation: 'hello' } };
  rememberSentMessage(store, sent, { text: 'hello' });
  assert.equal(store.get('text-1'), sent);
});
