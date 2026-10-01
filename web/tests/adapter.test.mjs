import { test } from "node:test";
import assert from "node:assert/strict";
import {
  HttpRecall,
  WindowStore,
  pages,
  boundedPages,
  units,
  clipFromUnits,
} from "../adapter.mjs";
import { dayRange, shiftDate } from "../calendar.mjs";
const row = {
  segment_id: "1",
  device_id: "nyta",
  start_utc_ms: 100,
  end_utc_ms: 200,
  text: "Authoritative text",
  timing: "segment",
  words: null,
};
test("auth stays in header; recipient content is encoded; additive fields are tolerated", async () => {
  let request;
  const client = new HttpRecall("secret", {
    fetcher: async (url, options) => {
      request = { url, options };
      return {
        ok: true,
        json: async () => ({ items: [row], new_field: true }),
      };
    },
  });
  assert.equal(
    (
      await client.read("transcript", {
        recipient_window: "<script>&",
        device_id: "nyta",
      })
    ).items[0],
    row,
  );
  assert.equal(request.options.headers.Authorization, "Bearer secret");
  assert.ok(!request.url.includes("secret"));
  assert.ok(request.url.includes("%3Cscript%3E%26"));
  assert.equal(request.options.cache, "no-store");
});
test("paging deduplicates IDs and keeps tied IDs, complete refresh replaces removals", async () => {
  const client = {
    read: async (_route, p) =>
      p.cursor
        ? { items: [row, { ...row, segment_id: "2" }], next_cursor: null }
        : { items: [row], next_cursor: "next" },
  };
  assert.equal((await pages(client, "transcript", {})).items.length, 2);
  const store = new WindowStore(client, { capacity: 1 });
  await store.load("a", async () => [row]);
  await store.load("a", async () => []);
  assert.deepEqual(store.cache.get("a"), []);
  await store.load("b", async () => []);
  assert.equal(store.cache.size, 1);
});
test("failed refresh retains prior value; navigation rejects obsolete completion", async () => {
  const store = new WindowStore({});
  await store.load("a", async () => [row]);
  const failed = await store.load("a", async () => {
    throw Error("offline");
  });
  assert.equal(failed.stale, true);
  assert.deepEqual(failed.value, [row]);
  let resolve;
  const pending = store.load("a", () => new Promise((r) => (resolve = r)));
  await store.load("b", async () => []);
  resolve([row]);
  assert.equal((await pending).obsolete, true);
});
test("expired cursors restart complete load once; unbounded pagination fails visibly", async () => {
  const store = new WindowStore({});
  let count = 0;
  assert.deepEqual(
    (
      await store.load("a", async () => {
        if (!count++) {
          const e = Error();
          e.code = "cursor_expired";
          throw e;
        }
        return [];
      })
    ).value,
    [],
  );
  await assert.rejects(
    pages(
      { read: async () => ({ items: [row], next_cursor: "loop" }) },
      "transcript",
      {},
    ),
    /repeated/,
  );
});
test("untimed dictation stays a segment, invalid words fall back; clips stay source bound", () => {
  assert.equal(units(row)[0].kind, "segment");
  assert.equal(
    units({
      ...row,
      timing: "word",
      words: [{ text: "a", start_utc_ms: 99, end_utc_ms: 120 }],
    }).length,
    1,
  );
  assert.equal(clipFromUnits(units(row), [row]).quoted_text, row.text);
  assert.throws(
    () =>
      clipFromUnits(
        [...units(row), ...units({ ...row, device_id: "phone" })],
        [row],
      ),
    /one source/,
  );
  const timed = {
    ...row,
    timing: "word",
    words: [
      { text: "A ", start_utc_ms: 100, end_utc_ms: 120 },
      { text: "word", start_utc_ms: 120, end_utc_ms: 200 },
    ],
  };
  assert.equal(clipFromUnits(units(timed), [timed]).quoted_text, row.text);
});
test("Oslo local days have actual DST lengths and adjacent calendar navigation", () => {
  const spring = dayRange("2026-03-29"),
    autumn = dayRange("2026-10-25");
  assert.equal(spring.to_ms - spring.from_ms, 23 * 3600000);
  assert.equal(autumn.to_ms - autumn.from_ms, 25 * 3600000);
  assert.equal(shiftDate("2026-12-31", 1), "2027-01-01");
});
test("autumn detail windows split at the advertised limit and deduplicate overlap", async () => {
  const calls = [];
  const adapter = {
    read: async (_route, p) => {
      calls.push(p);
      return { items: [row], as_of_ms: 1, next_cursor: null };
    },
  };
  const range = dayRange("2026-10-25");
  const result = await boundedPages(
    adapter,
    "transcript",
    range,
    undefined,
    86400000,
  );
  assert.equal(calls.length, 2);
  assert.equal(calls[0].to_ms, calls[1].from_ms);
  assert.equal(calls[1].to_ms, range.to_ms);
  assert.equal(result.items.length, 1);
});
test("word selection preserves authoritative whitespace and partial quotes", () => {
  const r = {
    ...row,
    text: "Hei, verden!",
    timing: "word",
    words: [
      { text: " Hei,", start_utc_ms: 100, end_utc_ms: 130 },
      { text: "verden!", start_utc_ms: 130, end_utc_ms: 200 },
    ],
  };
  assert.equal(units(r).length, 2);
  assert.equal(
    units(r)
      .map((w) => w.text)
      .join(""),
    r.text,
  );
  assert.equal(clipFromUnits([units(r)[1]], [r]).quoted_text, "verden!");
  assert.equal(units({ ...r, text: "Different text" })[0].kind, "segment");
});
