import { test } from "node:test";
import assert from "node:assert/strict";
import {
  groupBounds,
  selectionUnits,
  intersectsSelection,
} from "../selection.mjs";
const all = [
  {
    device_id: "a",
    segment_id: "1",
    text: "First.",
    kind: "word",
    start_utc_ms: 0,
    end_utc_ms: 10,
  },
  {
    device_id: "a",
    segment_id: "1",
    text: " Second",
    kind: "word",
    start_utc_ms: 10,
    end_utc_ms: 20,
  },
  {
    device_id: "a",
    segment_id: "1",
    text: " sentence.",
    kind: "word",
    start_utc_ms: 20,
    end_utc_ms: 30,
  },
  {
    device_id: "b",
    segment_id: "2",
    text: "Other source.",
    kind: "segment",
    start_utc_ms: 0,
    end_utc_ms: 30,
  },
  {
    device_id: "a",
    segment_id: "3",
    text: "Untimed.",
    kind: "segment",
    start_utc_ms: 30,
    end_utc_ms: 40,
  },
];
const rows = [
  { device_id: "a", segment_id: "1", session_id: "session" },
  { device_id: "b", segment_id: "2", session_id: "different" },
  { device_id: "a", segment_id: "3", session_id: "session" },
];
test("precision chooses actual words, sentences or complete segments", () => {
  assert.deepEqual(groupBounds(all, 1, "word", rows), [1, 1]);
  assert.deepEqual(groupBounds(all, 1, "sentence", rows), [1, 2]);
  assert.deepEqual(groupBounds(all, 1, "segment", rows), [0, 2]);
  assert.deepEqual(groupBounds(all, 4, "sentence", rows), [4, 4]);
});
test("session and backward selections keep source identity despite interleaving", () => {
  assert.deepEqual(groupBounds(all, 0, "session", rows), [0, 4]);
  assert.equal(selectionUnits(all, 4, 0, "a").length, 4);
  assert.ok(selectionUnits(all, 0, 4, "a").every((u) => u.device_id === "a"));
});
test("range selections respect half-open edges and point events", () => {
  const range = { device_id: "a", from_ms: 10, to_ms: 30 };
  assert.equal(intersectsSelection(all[0], range), false);
  assert.equal(intersectsSelection(all[1], range), true);
  assert.equal(intersectsSelection(all[3], range), false);
  assert.equal(
    intersectsSelection({ ...all[1], start_utc_ms: 10, end_utc_ms: 10 }, range),
    true,
  );
  assert.equal(
    intersectsSelection({ ...all[1], start_utc_ms: 30, end_utc_ms: 30 }, range),
    false,
  );
});
