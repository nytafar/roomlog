// The token is intentionally private, memory-only, and never part of a URL/cache key.
export class RecallError extends Error {
  constructor(message, status, code) {
    super(message);
    this.status = status;
    this.code = code;
  }
}
export class HttpRecall {
  #token;
  constructor(token, { fetcher = fetch, base = "/v1/recall" } = {}) {
    this.#token = token;
    this.fetcher = (...args) => fetcher(...args);
    this.base = base;
  }
  async read(route, params = {}, signal) {
    const query = new URLSearchParams(
      Object.entries(params).filter(([, v]) => v !== "" && v != null),
    );
    const response = await this.fetcher(
      `${this.base}/${route}${query.size ? "?" + query : ""}`,
      {
        signal,
        cache: "no-store",
        headers: { Authorization: `Bearer ${this.#token}` },
      },
    );
    let body;
    try {
      body = await response.json();
    } catch {
      throw new RecallError(
        "The reader returned an invalid response.",
        response.status,
        "invalid_response",
      );
    }
    if (!response.ok)
      throw new RecallError(
        body.error?.message || "The reader could not complete the request.",
        response.status,
        body.error?.code,
      );
    return body;
  }
  // Chunk audio as a Blob. Kept out of the HTTP cache like reads, so room audio never
  // outlives logout on disk; the player's in-memory blobs cover replay.
  async audio(chunkId, signal) {
    const response = await this.fetcher(
      `${this.base}/audio?${new URLSearchParams({ chunk_id: chunkId })}`,
      {
        signal,
        cache: "no-store",
        headers: { Authorization: `Bearer ${this.#token}` },
      },
    );
    if (response.ok) return response.blob();
    let error;
    try {
      error = (await response.json()).error;
    } catch {}
    throw new RecallError(
      error?.message || "The reader could not load this audio.",
      response.status,
      error?.code,
    );
  }
}
// Dedupe on the route's own identity. Span reports also carry a (possibly null) segment_id,
// and span, segment and session IDs are separate spaces, so never fall back across them.
const ROUTE_ID = { spans: "span_id", sessions: "session_id" };
export function itemKey(route, item) {
  const kind = ROUTE_ID[route] ?? "segment_id";
  return `${item.device_id}:${kind}:${item[kind]}`;
}
export async function pages(
  adapter,
  route,
  params,
  signal,
  { maxItems = 20000, maxPages = 200 } = {},
) {
  const items = new Map(),
    seen = new Set();
  let cursor, as_of_ms;
  for (let page = 0; page < maxPages; page++) {
    const response = await adapter.read(route, { ...params, cursor }, signal);
    if (!Array.isArray(response.items))
      throw new RecallError("Invalid list response.", 502, "invalid_response");
    as_of_ms = response.as_of_ms;
    for (const item of response.items) {
      items.set(itemKey(route, item), item);
      if (items.size > maxItems)
        throw new RecallError(
          "This window has too much text. Choose a shorter range.",
          422,
          "window_too_large",
        );
    }
    cursor = response.next_cursor;
    if (!cursor) return { items: [...items.values()], as_of_ms };
    if (seen.has(cursor))
      throw new RecallError(
        "The reader repeated a page cursor.",
        502,
        "invalid_cursor",
      );
    seen.add(cursor);
  }
  throw new RecallError(
    "This window needs too many pages. Choose a shorter range.",
    422,
    "window_too_large",
  );
}
// A local autumn day can exceed the server's 24-hour detail budget.
export async function boundedPages(
  adapter,
  route,
  params,
  signal,
  maxWindow = 86400000,
) {
  if (!Number.isFinite(maxWindow) || maxWindow < 1)
    throw new Error("Invalid window limit");
  const items = new Map();
  let as_of_ms = 0;
  for (let from = params.from_ms; from < params.to_ms; from += maxWindow) {
    const result = await pages(
      adapter,
      route,
      {
        ...params,
        from_ms: from,
        to_ms: Math.min(params.to_ms, from + maxWindow),
      },
      signal,
    );
    as_of_ms = Math.max(as_of_ms, result.as_of_ms || 0);
    for (const item of result.items)
      items.set(itemKey(route, item), item);
    if (items.size > 20000)
      throw new RecallError("Choose a shorter range.", 422, "window_too_large");
  }
  return { items: [...items.values()], as_of_ms };
}
// Complete windows replace old snapshots. Navigation may reuse them; refresh always reads.
export class WindowStore {
  constructor(adapter, { capacity = 8, maxBytes = 16 * 1024 * 1024 } = {}) {
    this.adapter = adapter;
    this.capacity = capacity;
    this.maxBytes = maxBytes;
    this.cache = new Map();
    this.sizes = new Map();
    this.errors = new Map();
    this.bytes = 0;
    this.generation = 0;
  }
  cancel() {
    this.generation++;
    this.controller?.abort();
  }
  remove(key) {
    this.bytes -= this.sizes.get(key) || 0;
    this.sizes.delete(key);
    this.errors.delete(key);
    this.cache.delete(key);
  }
  lookup(key, reuse) {
    if (this.cache.has(key))
      return { key, value: this.cache.get(key), error: this.errors.get(key) };
    if (reuse) {
      for (const [candidate, value] of [...this.cache].reverse()) {
        const projected = reuse(candidate, value, key);
        if (projected !== undefined)
          return { key: candidate, value: projected, error: this.errors.get(candidate) };
      }
    }
  }
  async load(key, loader, { refresh = true, reuse, invalidates } = {}) {
    this.cancel();
    const generation = this.generation;
    const prior = this.lookup(key, reuse);
    if (!refresh && prior) {
      // Touch the original covering window, without storing duplicated subranges.
      const value = this.cache.get(prior.key);
      this.cache.delete(prior.key);
      this.cache.set(prior.key, value);
      return { value: prior.value, cached: true, stale: !!prior.error, error: prior.error };
    }
    const controller = (this.controller = new AbortController());
    try {
      let value;
      try {
        value = await loader(this.adapter, controller.signal);
      } catch (error) {
        if (error.code !== "cursor_expired") throw error;
        value = await loader(this.adapter, controller.signal);
      }
      if (generation !== this.generation) return { obsolete: true };
      if (invalidates)
        for (const candidate of [...this.cache.keys()])
          if (invalidates(candidate, key)) this.remove(candidate);
      this.remove(key);
      const size = new TextEncoder().encode(JSON.stringify(value)).byteLength;
      this.cache.set(key, value);
      this.sizes.set(key, size);
      this.bytes += size;
      while (this.cache.size > this.capacity || this.bytes > this.maxBytes)
        this.remove(this.cache.keys().next().value);
      return { value, stale: false };
    } catch (error) {
      if (generation !== this.generation || error.name === "AbortError")
        return { obsolete: true };
      if (prior) this.errors.set(prior.key, error);
      return { value: prior?.value, stale: true, error };
    }
  }
}
export function units(row) {
  const words =
    row.timing === "word" && Array.isArray(row.words) && row.words.length
      ? row.words
      : null;
  let previous = row.start_utc_ms;
  const valid = words?.every((word) => {
    const ok =
      typeof word.text === "string" &&
      Number.isFinite(word.start_utc_ms) &&
      Number.isFinite(word.end_utc_ms) &&
      word.start_utc_ms >= previous &&
      word.end_utc_ms >= word.start_utc_ms &&
      word.end_utc_ms <= row.end_utc_ms;
    previous = word.start_utc_ms;
    return ok;
  });
  const fallback = [
    {
      text: row.text,
      start_utc_ms: row.start_utc_ms,
      end_utc_ms: row.end_utc_ms,
      device_id: row.device_id,
      segment_id: row.segment_id,
      index: 0,
      kind: "segment",
    },
  ];
  if (!valid) return fallback;
  let offset = 0;
  const aligned = [];
  for (const [index, word] of words.entries()) {
    const token = word.text.trim(),
      start = row.text.indexOf(token, offset);
    if (!token || start < 0 || row.text.slice(offset, start).trim())
      return fallback;
    const end = start + token.length;
    aligned.push({
      ...word,
      text: row.text.slice(offset, end),
      device_id: row.device_id,
      segment_id: row.segment_id,
      index,
      kind: "word",
    });
    offset = end;
  }
  if (row.text.slice(offset).trim()) return fallback;
  aligned.at(-1).text += row.text.slice(offset);
  return aligned;
}
export function clipFromUnits(selected, rows, now = Date.now()) {
  if (
    !selected.length ||
    new Set(selected.map((unit) => unit.device_id)).size !== 1
  )
    throw new Error("Selections must stay within one source.");
  // Full-row quotes use the authoritative transcript, not reconstructed word tokens.
  const quoted = [];
  for (const row of rows) {
    const part = selected.filter((unit) => unit.segment_id === row.segment_id);
    if (!part.length) continue;
    quoted.push(
      part.length === units(row).length
        ? row.text
        : part
            .map((unit) => unit.text)
            .join("")
            .trim(),
    );
  }
  return {
    device_id: selected[0].device_id,
    from_ms: Math.min(...selected.map((unit) => unit.start_utc_ms)),
    to_ms: Math.max(...selected.map((unit) => unit.end_utc_ms)),
    quoted_text: quoted.join("\n"),
    captured_at_ms: now,
  };
}
