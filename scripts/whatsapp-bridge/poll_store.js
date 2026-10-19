import { readFile, writeFile, rename } from 'node:fs/promises';
import { parsePollRecords, prunePollRecords, serializePollRecord, pollPayloadFromRecord, pollStoreEntry } from './bridge_helpers.js';

export async function createPollStore({ file, messageStore, rememberSentId, log = console.warn }) {
  let records = [];
  try {
    records = parsePollRecords(await readFile(file, 'utf8'));
  } catch (err) {
    if (err.code !== 'ENOENT') log(`[bridge] poll store load failed: ${err.message}`);
  }
  for (const record of records) {
    const entry = pollStoreEntry(record.id, record.chatId, pollPayloadFromRecord(record));
    if (!entry) {
      log('[bridge] skipped poll store record: creation message unavailable');
      continue;
    }
    messageStore.remember(entry);
    rememberSentId(record.id);
  }
  let pending = Promise.resolve();
  function remember(id, chatId, payload) {
    const record = serializePollRecord(id, chatId, payload);
    if (!record) return;
    records = prunePollRecords([...records, record]);
    const text = JSON.stringify(records);
    pending = pending.then(async () => {
      const temporary = `${file}.${process.pid}.tmp`;
      await writeFile(temporary, text, { mode: 0o600 });
      await rename(temporary, file);
    }).catch(err => log(`[bridge] poll store write failed: ${err.message}`));
  }
  return { remember, flush: () => pending };
}
