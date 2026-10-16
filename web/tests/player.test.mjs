import { test } from "node:test";
import assert from "node:assert/strict";
import { Player, ChunkCache, playable, unitAt, clock } from "../player.mjs";
import { HttpRecall, RecallError } from "../adapter.mjs";

class FakeAudio extends EventTarget {
  paused = true;
  ended = false;
  currentTime = 0;
  duration = NaN;
  readyState = 0;
  playbackRate = 1;
  #src = "";
  get src() {
    return this.#src;
  }
  broken = false;
  set src(url) {
    this.#src = url;
    this.readyState = 0;
    this.duration = NaN;
    const broken = this.broken;
    setTimeout(() => {
      this.error = broken ? { code: 3 } : null;
      if (broken) return this.dispatchEvent(new Event("error"));
      this.readyState = 1;
      this.duration = 6;
      this.dispatchEvent(new Event("loadedmetadata"));
    });
  }
  removeAttribute() {
    this.#src = "";
  }
  async play() {
    this.paused = false;
  }
  pause() {
    this.paused = true;
  }
}
const row = (id, chunk, start, extra = {}) => ({
  segment_id: id,
  device_id: "nyta",
  chunk_id: chunk,
  chunk_start_utc_ms: 1000,
  start_utc_ms: start,
  end_utc_ms: start + 1000,
  ...extra,
});
const cache = () => {
  let n = 0;
  const revoked = [];
  return {
    revoked,
    cache: new ChunkCache(2, { create: () => `blob:${++n}`, revoke: (u) => revoked.push(u) }),
  };
};

test("playability is per chunk", () => {
  assert.equal(playable(row("1", "1", 1000)), true);
  assert.equal(playable(row("1", null, 1000)), false, "dictation without a chunk");
  assert.equal(playable(row("1", "1", 1000, { audio_available: false })), false);
  assert.equal(playable(row("1", "1", 1000, { chunk_start_utc_ms: null })), false);
  assert.equal(playable(row("1", "1", 1000), new Set(["1"])), false);
});

test("unitAt finds the unit under the playhead", () => {
  const list = [{ start_utc_ms: 10 }, { start_utc_ms: 20 }, { start_utc_ms: 30 }];
  assert.equal(unitAt(list, 5), -1);
  assert.equal(unitAt(list, 10), 0);
  assert.equal(unitAt(list, 29), 1);
  assert.equal(unitAt(list, 99), 2);
  assert.equal(clock(65.9), "1:05");
});

test("chunk cache keeps recent blobs and revokes evicted URLs", () => {
  const { cache: c, revoked } = cache();
  c.set("a", {});
  c.set("b", {});
  c.get("a");
  c.set("c", {});
  assert.deepEqual(revoked, ["blob:2"], "least recently used is revoked");
  assert.equal(c.get("a"), "blob:1");
  c.clear();
  assert.deepEqual(revoked.sort(), ["blob:1", "blob:2", "blob:3"]);
});

test("play seeks to the word offset inside its chunk and reuses the blob", async () => {
  const audio = new FakeAudio(),
    fetched = [];
  const player = new Player(audio, async (id) => (fetched.push(id), {}), cache());
  assert.equal(await player.play(row("1", "7", 3000), 3500), "playing");
  assert.equal(audio.currentTime, 2.5, "word start minus chunk start");
  assert.equal(audio.paused, false);
  assert.equal(await player.play(row("2", "7", 4000), 4000), "playing");
  assert.equal(audio.currentTime, 3);
  assert.deepEqual(fetched, ["7"], "same chunk is not fetched again");
});

test("a clip pauses at its end and replays from its start", async () => {
  const audio = new FakeAudio();
  const player = new Player(audio, async () => ({}), cache());
  await player.play(row("1", "7", 2000), 2000, 2600);
  audio.currentTime = 1.7;
  assert.equal(player.tick(), 2600, "playhead clamps to the clip end");
  assert.equal(audio.paused, true);
  assert.equal(audio.currentTime, 1.6);
  player.toggle();
  assert.equal(audio.currentTime, 1, "replay starts the clip again");
  assert.equal(audio.paused, false);
});

test("pausing before play() settles keeps the chunk loaded", async () => {
  const audio = new FakeAudio();
  audio.play = async () => {
    throw new DOMException("interrupted by pause", "AbortError");
  };
  const player = new Player(audio, async () => ({}), cache());
  assert.equal(await player.play(row("1", "7", 1000)), "paused");
  assert.equal(player.current.chunkId, "7");
});

test("a clip also ends from timeupdate, which runs while the screen is locked", async () => {
  const audio = new FakeAudio();
  const player = new Player(audio, async () => ({}), cache());
  await player.play(row("1", "7", 2000), 2000, 2600);
  audio.currentTime = 1.8;
  audio.dispatchEvent(new Event("timeupdate"));
  assert.equal(audio.paused, true);
  assert.equal(audio.currentTime, 1.6);
});

test("an undecodable chunk is dropped so playing it again refetches", async () => {
  const audio = new FakeAudio(),
    { cache: c, revoked } = cache();
  let fetches = 0;
  const player = new Player(audio, async () => (fetches++, {}), { cache: c });
  audio.broken = true;
  await assert.rejects(player.play(row("1", "7", 1000)), /decoded/);
  assert.equal(player.current, null);
  assert.deepEqual(revoked, ["blob:1"]);
  audio.broken = false;
  assert.equal(await player.play(row("1", "7", 1000)), "playing");
  assert.equal(fetches, 2);
});

test("the newest play wins over a slower fetch", async () => {
  const audio = new FakeAudio();
  let release;
  const slow = new Promise((r) => (release = r));
  const player = new Player(
    audio,
    (id, signal) =>
      id === "slow"
        ? slow.then(() => {
            if (signal.aborted) throw new DOMException("aborted", "AbortError");
            return {};
          })
        : Promise.resolve({}),
    cache(),
  );
  const first = player.play(row("1", "slow", 1000));
  const second = player.play(row("2", "fast", 2000));
  release();
  assert.equal(await first, "aborted");
  assert.equal(await second, "playing");
  assert.equal(player.current.chunkId, "fast");
});

test("audio_unavailable is remembered per chunk; other errors surface", async () => {
  const audio = new FakeAudio();
  let calls = 0;
  const player = new Player(
    audio,
    async (id) => {
      calls++;
      if (id === "gone") throw new RecallError("x", 404, "audio_unavailable");
      if (id === "stale") throw new RecallError("x", 404, "not_found");
      throw new RecallError("Read access rejected", 403, "forbidden");
    },
    cache(),
  );
  const r = row("1", "gone", 1000);
  assert.equal(await player.play(r), "unavailable");
  assert.equal(player.canPlay(r), false);
  assert.equal(player.current, null, "no player opens for missing audio");
  assert.equal(await player.play(row("2", "stale", 1000)), "missing");
  await assert.rejects(player.play(row("3", "x", 1000)), /Read access/);
  player.reset();
  assert.equal(player.canPlay(r), true, "logout forgets");
  assert.equal(calls, 3);
});

test("adapter fetches chunk audio with the bearer header, outside the URL", async () => {
  const seen = [];
  const recall = new HttpRecall("secret", {
    fetcher: async (url, init) => {
      seen.push([url, init.headers.Authorization, init.cache]);
      return url.includes("chunk_id=9")
        ? new Response(new Blob(["ogg"]), { status: 200 })
        : new Response(
            JSON.stringify({ error: { code: "audio_unavailable", message: "No file" } }),
            { status: 404 },
          );
    },
  });
  assert.equal(await (await recall.audio("9")).text(), "ogg");
  await assert.rejects(recall.audio("10"), (e) => e.code === "audio_unavailable" && e.status === 404);
  assert.deepEqual(
    seen[0],
    ["/v1/recall/audio?chunk_id=9", "Bearer secret", "no-store"],
    "token stays out of the URL and audio stays out of the disk cache",
  );
});
