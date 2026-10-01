import { test } from "node:test";
import assert from "node:assert/strict";
import { WindowStore } from "../adapter.mjs";
import { historyKey, reuseHistory, overlapsHistory, coveringDetailKey } from "../history-window.mjs";
const params = { from_ms: 0, to_ms: 100, channel: "ambient" };
const row = { device_id: "a", segment_id: "1", start_utc_ms: 10, end_utc_ms: 20, text: "first" };
const value = {
  transcript: [row, { ...row, segment_id: "point", start_utc_ms: 20, end_utc_ms: 20 },
    { ...row, device_id: "b" }],
  sessions: { items: [{ ...row, session_id: "session" }] }, reports: [],
};
const detail = historyKey("text", params);
test("warm zoom and focused source/subrange reuse loaded history without reads", async () => {
  const store = new WindowStore({});
  await store.load(detail, async () => value);
  const loader = () => { throw Error("reader must not be called"); };
  const options = { refresh: false, reuse: reuseHistory };
  assert.equal((await store.load(historyKey("session", params), loader, options)).cached, true);
  assert.equal((await store.load(historyKey("day", params), loader, options)).value.transcript.length, 3);
  const focus = historyKey("text", { ...params, device_id: "a", from_ms: 15, to_ms: 20 });
  assert.deepEqual((await store.load(focus, loader, options)).value.transcript, [row]);
  assert.equal(store.cache.size, 1);
  const point = historyKey("text", { ...params, device_id: "a", from_ms: 20, to_ms: 21 });
  assert.equal((await store.load(point, loader, options)).value.transcript[0].segment_id, "point");
  assert.equal(reuseHistory(detail, value, historyKey("text", { ...params, channel: "dictation" })), undefined);
  assert.equal(reuseHistory(detail, value, historyKey("text", { ...params, to_ms: 101 })), undefined);
  assert.equal(reuseHistory(historyKey("day", params), value, detail), undefined);
});
test("refresh replaces deletions and invalidates overlapping views; failed refresh stays visibly stale", async () => {
  const store = new WindowStore({});
  await store.load(detail, async () => value);
  const week = historyKey("week", { ...params, to_ms: 700 });
  await store.load(week, async () => ({ timeline: [] }));
  const other = historyKey("text", { ...params, from_ms: 100, to_ms: 200 });
  await store.load(other, async () => value);
  const latest = { ...value, transcript: [] };
  await store.load(detail, async () => latest, { invalidates: overlapsHistory });
  assert.equal(store.cache.has(week), false);
  assert.equal(store.cache.has(other), true);
  const focus = historyKey("session", { ...params, device_id: "a", from_ms: 10, to_ms: 30 });
  assert.deepEqual((await store.load(focus, () => { throw Error(); }, { refresh: false, reuse: reuseHistory })).value.transcript, []);
  await store.load(detail, async () => { throw Error("offline"); });
  const stale = await store.load(focus, () => { throw Error(); }, { refresh: false, reuse: reuseHistory });
  assert.equal(stale.stale, true);
  assert.equal(stale.error.message, "offline");
});
test("warm navigation cancels an in-flight refresh before it can overwrite history", async () => {
  const store = new WindowStore({});
  await store.load(detail, async () => value);
  let resolve;
  const pending = store.load(detail, () => new Promise((r) => resolve = r));
  const warm = await store.load(historyKey("day", params), () => { throw Error(); }, { refresh: false, reuse: reuseHistory });
  assert.equal(warm.cached, true);
  resolve({ ...value, transcript: [] });
  assert.equal((await pending).obsolete, true);
  assert.equal(store.cache.get(detail), value);
});
test("cache evicts by byte budget as well as entry count", async () => {
  const store = new WindowStore({}, { maxBytes: 10 });
  await store.load("small", async () => "one");
  await store.load("next", async () => "two");
  assert.equal(store.bytes, 10);
  await store.load("large", async () => "exceeds the budget");
  assert.equal(store.cache.size, 0);
  assert.equal(store.bytes, 0);
  assert.equal(store.errors.size, 0);
});

test("day and focused refresh prefer complete loaded detail over an exact preview/subrange", async () => {
  const store = new WindowStore({});
  const day = historyKey("day", params);
  const focus = historyKey("text", { ...params, device_id: "a", from_ms: 10, to_ms: 30 });
  await store.load(day, async () => value);
  await store.load(focus, async () => value);
  await store.load(detail, async () => value);
  assert.equal(store.lookup(day, reuseHistory).key, day);
  assert.equal(coveringDetailKey(store.cache, day), detail);
  assert.equal(coveringDetailKey(store.cache, focus), detail);
  const refreshed = { ...value, transcript: [] };
  await store.load(coveringDetailKey(store.cache, day), async () => refreshed, { invalidates: overlapsHistory });
  assert.equal(store.cache.size, 1);
  assert.deepEqual(store.lookup(focus, reuseHistory).value.transcript, []);
  assert.deepEqual(store.lookup(day, reuseHistory).value.transcript, []);
  assert.equal(coveringDetailKey(store.cache, historyKey("week", params)), undefined);
});
