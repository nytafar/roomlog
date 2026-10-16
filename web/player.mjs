// One reused <audio> element plays one chunk at a time. A plain src cannot carry
// the bearer token, so chunk bytes are fetched by the adapter and played from blob
// URLs. Chunks are small (Ogg Opus, at most ~30 s), so the whole file is in memory
// and seeks are exact.
export function playable(row, unavailable = new Set()) {
  return (
    row?.chunk_id != null &&
    row.audio_available !== false &&
    Number.isFinite(row.chunk_start_utc_ms) &&
    !unavailable.has(String(row.chunk_id))
  );
}
// Index of the last unit starting at or before t, or -1.
export function unitAt(list, t) {
  let lo = 0,
    hi = list.length - 1,
    found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (list[mid].start_utc_ms <= t) {
      found = mid;
      lo = mid + 1;
    } else hi = mid - 1;
  }
  return found;
}
export function clock(seconds) {
  const s = Math.max(0, Math.floor(seconds || 0));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}
// Recently played chunks, so replaying or jumping inside a chunk never refetches.
// Evicted URLs are revoked; the playing chunk is always the newest entry.
export class ChunkCache {
  #entries = new Map();
  constructor(
    limit = 8,
    { create = (b) => URL.createObjectURL(b), revoke = (u) => URL.revokeObjectURL(u) } = {},
  ) {
    this.limit = limit;
    this.create = create;
    this.revoke = revoke;
  }
  get(id) {
    const url = this.#entries.get(id);
    if (url) {
      this.#entries.delete(id);
      this.#entries.set(id, url);
    }
    return url;
  }
  set(id, blob) {
    const url = this.create(blob);
    this.#entries.set(id, url);
    for (const [old, oldUrl] of this.#entries) {
      if (this.#entries.size <= this.limit) break;
      this.#entries.delete(old);
      this.revoke(oldUrl);
    }
    return url;
  }
  delete(id) {
    const url = this.#entries.get(id);
    if (url) this.revoke(url);
    this.#entries.delete(id);
  }
  clear() {
    for (const url of this.#entries.values()) this.revoke(url);
    this.#entries.clear();
  }
}
export class Player {
  #request = null;
  #loaded = null;
  constructor(audio, fetchAudio, { cache = new ChunkCache() } = {}) {
    this.audio = audio;
    this.fetchAudio = fetchAudio;
    this.cache = cache;
    this.unavailable = new Set();
    this.current = null;
    this.pending = null;
    this.onchange = () => {};
    audio.preload = "auto";
    for (const type of ["play", "pause", "ended", "loadedmetadata", "seeked"])
      audio.addEventListener(type, () => this.#position());
    // rAF stops in hidden tabs and on a locked screen; this keeps clip ends there.
    audio.addEventListener("timeupdate", () => this.tick());
    const session = globalThis.navigator?.mediaSession;
    if (session)
      for (const [action, handler] of [
        ["play", () => this.toggle(true)],
        ["pause", () => this.toggle(false)],
        ["stop", () => this.stop()],
        ["seekto", (d) => this.seek(d.seekTime)],
        ["seekbackward", (d) => this.seek(audio.currentTime - (d.seekOffset || 5))],
        ["seekforward", (d) => this.seek(audio.currentTime + (d.seekOffset || 5))],
      ])
        try {
          session.setActionHandler(action, handler);
        } catch {}
  }
  canPlay(row) {
    return playable(row, this.unavailable);
  }
  // Plays the row's chunk from fromMs; toMs (optional) pauses there. The newest call
  // wins. Resolves to "playing", "paused" (paused before playback began), "blocked"
  // (autoplay refused; the bar can resume),
  // "unavailable" (no file for this chunk), "missing" (unknown chunk) or "aborted".
  async play(row, fromMs = row.start_utc_ms, toMs = null, label = "") {
    this.#request?.abort();
    const request = (this.#request = new AbortController());
    this.audio.pause();
    const id = String(row.chunk_id),
      start = row.chunk_start_utc_ms;
    const target = {
      row,
      chunkId: id,
      chunkStart: start,
      from: Math.max(0, (fromMs - start) / 1000),
      stopAt: toMs == null ? Infinity : (toMs - start) / 1000,
      label,
    };
    let url = this.cache.get(id);
    if (!url) {
      this.pending = target;
      this.onchange();
      let blob;
      try {
        blob = await this.fetchAudio(id, request.signal);
      } catch (error) {
        if (request.signal.aborted) return "aborted";
        this.pending = null;
        this.onchange();
        if (error.code === "audio_unavailable") {
          this.unavailable.add(id);
          return "unavailable";
        }
        if (error.code === "not_found") return "missing";
        throw error;
      }
      if (request.signal.aborted) return "aborted";
      url = this.cache.set(id, blob);
    }
    this.pending = null;
    this.current = target;
    this.#metadata(label);
    if (this.#loaded !== url) {
      this.#loaded = url;
      this.audio.src = url;
    }
    try {
      if (this.audio.readyState < 1) await this.#once("loadedmetadata");
      if (request.signal.aborted) return "aborted";
      this.audio.currentTime = target.from;
      this.onchange();
      await this.audio.play();
    } catch (error) {
      if (request.signal.aborted) return "aborted";
      if (error?.name === "NotAllowedError") {
        this.onchange();
        return "blocked";
      }
      // A pause before play() settled rejects it; the player simply stays paused.
      if (error?.name === "AbortError" && !this.audio.error) {
        this.onchange();
        return "paused";
      }
      if (!this.audio.error) throw error;
      // An undecodable chunk must not stay cached as the loaded source, or the
      // next play of it would wait for metadata forever.
      this.cache.delete(id);
      this.#loaded = null;
      this.audio.removeAttribute("src");
      this.audio.load?.();
      this.current = null;
      this.onchange();
      throw error;
    }
    return request.signal.aborted ? "aborted" : "playing";
  }
  toggle(play = this.audio.paused) {
    const current = this.current;
    if (!current) return;
    if (!play) return this.audio.pause();
    // Replaying a clip that stopped at its end starts the clip again.
    if (this.audio.currentTime >= current.stopAt - 0.05 || this.audio.ended)
      this.audio.currentTime = current.from;
    this.audio.play().catch(() => {});
  }
  seek(seconds) {
    if (!this.current || !Number.isFinite(seconds)) return;
    const max = Number.isFinite(this.audio.duration) ? this.audio.duration : seconds;
    this.audio.currentTime = Math.min(Math.max(0, seconds), max);
    // Seeking past a clip's end releases the clip bound.
    if (this.audio.currentTime >= this.current.stopAt) this.current.stopAt = Infinity;
  }
  // Called every frame while playing: enforces the clip end and returns the
  // playhead as UTC ms, or null when nothing is loaded.
  tick() {
    const current = this.current;
    if (!current) return null;
    if (!this.audio.paused && this.audio.currentTime >= current.stopAt) {
      this.audio.pause();
      this.audio.currentTime = current.stopAt;
    }
    return current.chunkStart + this.audio.currentTime * 1000;
  }
  stop() {
    this.#request?.abort();
    this.#request = null;
    this.audio.pause();
    this.current = null;
    this.pending = null;
    const session = globalThis.navigator?.mediaSession;
    if (session) session.metadata = null;
    this.onchange();
  }
  reset() {
    this.stop();
    this.audio.removeAttribute("src");
    this.audio.load?.();
    this.#loaded = null;
    this.cache.clear();
    this.unavailable.clear();
  }
  #once(type) {
    return new Promise((resolve, reject) => {
      const done = () => {
        this.audio.removeEventListener(type, done);
        this.audio.removeEventListener("error", fail);
        resolve();
      };
      const fail = () => {
        this.audio.removeEventListener(type, done);
        this.audio.removeEventListener("error", fail);
        reject(Error("This audio could not be decoded."));
      };
      this.audio.addEventListener(type, done);
      this.audio.addEventListener("error", fail);
    });
  }
  #metadata(title) {
    const session = globalThis.navigator?.mediaSession;
    if (session && globalThis.MediaMetadata)
      session.metadata = new MediaMetadata({ title, artist: "roomlog" });
  }
  #position() {
    const session = globalThis.navigator?.mediaSession,
      audio = this.audio;
    if (!session?.setPositionState || !Number.isFinite(audio.duration)) return;
    try {
      session.setPositionState({
        duration: audio.duration,
        position: Math.min(audio.currentTime, audio.duration),
        playbackRate: audio.playbackRate || 1,
      });
    } catch {}
  }
}
