// The token is intentionally private, memory-only, and never part of a URL/cache key.
export class RecallError extends Error {
  constructor(message, status, code) { super(message); this.status = status; this.code = code; }
}
export class HttpRecall {
  #token;
  constructor(token, { fetcher = fetch, base = '/v1/recall' } = {}) { this.#token = token; this.fetcher = fetcher; this.base = base; }
  async read(route, params = {}, signal) {
    const query = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== '' && v != null));
    const response = await this.fetcher(`${this.base}/${route}${query.size ? '?' + query : ''}`, {
      signal, cache: 'no-store', headers: { Authorization: `Bearer ${this.#token}` },
    });
    let body;
    try { body = await response.json(); } catch { throw new RecallError('The reader returned an invalid response.', response.status, 'invalid_response'); }
    if (!response.ok) throw new RecallError(body.error?.message || 'The reader could not complete the request.', response.status, body.error?.code);
    return body;
  }
}
export async function pages(adapter, route, params, signal, { maxItems = 20000, maxPages = 200 } = {}) {
  const items = new Map(), seen = new Set(); let cursor, as_of_ms;
  for (let page = 0; page < maxPages; page++) {
    const response = await adapter.read(route, { ...params, cursor }, signal);
    if (!Array.isArray(response.items)) throw new RecallError('Invalid list response.', 502, 'invalid_response');
    as_of_ms = response.as_of_ms;
    for (const item of response.items) {
      const id = item.segment_id ?? item.span_id ?? item.session_id;
      items.set(`${item.device_id}:${id}`, item);
      if (items.size > maxItems) throw new RecallError('This window has too much text. Choose a shorter range.', 422, 'window_too_large');
    }
    cursor = response.next_cursor;
    if (!cursor) return { items: [...items.values()], as_of_ms };
    if (seen.has(cursor)) throw new RecallError('The reader repeated a page cursor.', 502, 'invalid_cursor');
    seen.add(cursor);
  }
  throw new RecallError('This window needs too many pages. Choose a shorter range.', 422, 'window_too_large');
}
// A local autumn day can exceed the server's 24-hour detail budget.
export async function boundedPages(adapter, route, params, signal, maxWindow = 86400000) {
  if (!Number.isFinite(maxWindow) || maxWindow < 1) throw new Error('Invalid window limit');
  const items = new Map(); let as_of_ms = 0;
  for (let from = params.from_ms; from < params.to_ms; from += maxWindow) {
    const result = await pages(adapter, route, { ...params, from_ms: from, to_ms: Math.min(params.to_ms, from + maxWindow) }, signal);
    as_of_ms = Math.max(as_of_ms, result.as_of_ms || 0);
    for (const item of result.items) items.set(`${item.device_id}:${item.segment_id ?? item.span_id ?? item.session_id}`, item);
    if (items.size > 20000) throw new RecallError('Choose a shorter range.', 422, 'window_too_large');
  }
  return { items: [...items.values()], as_of_ms };
}
// One successful bounded window replaces the old one; incomplete refreshes never leak.
export class WindowStore {
  constructor(adapter, { capacity = 8 } = {}) { this.adapter = adapter; this.capacity = capacity; this.cache = new Map(); this.generation = 0; }
  cancel() { this.generation++; this.controller?.abort(); }
  async load(key, loader) {
    this.cancel(); const generation = this.generation; const controller = this.controller = new AbortController();
    const prior = this.cache.get(key);
    try {
      let value;
      try { value = await loader(this.adapter, controller.signal); }
      catch (error) { if (error.code !== 'cursor_expired') throw error; value = await loader(this.adapter, controller.signal); }
      if (generation !== this.generation) return { obsolete: true };
      this.cache.delete(key); this.cache.set(key, value);
      while (this.cache.size > this.capacity) this.cache.delete(this.cache.keys().next().value);
      return { value, stale: false };
    } catch (error) {
      if (generation !== this.generation || error.name === 'AbortError') return { obsolete: true };
      return { value: prior, stale: true, error };
    }
  }
}
export function units(row) {
  const words = row.timing === 'word' && Array.isArray(row.words) && row.words.length ? row.words : null;
  let previous = row.start_utc_ms;
  const valid = words?.every(word => {
    const ok = typeof word.text === 'string' && Number.isFinite(word.start_utc_ms) && Number.isFinite(word.end_utc_ms) && word.start_utc_ms >= previous && word.end_utc_ms >= word.start_utc_ms && word.end_utc_ms <= row.end_utc_ms;
    previous = word.start_utc_ms; return ok;
  });
  const fallback = [{ text: row.text, start_utc_ms: row.start_utc_ms, end_utc_ms: row.end_utc_ms, device_id: row.device_id, segment_id: row.segment_id, index: 0, kind: 'segment' }];
  if (!valid) return fallback;
  let offset = 0;
  const aligned = [];
  for (const [index, word] of words.entries()) {
    const token = word.text.trim(), start = row.text.indexOf(token, offset);
    if (!token || start < 0 || row.text.slice(offset, start).trim()) return fallback;
    const end = start + token.length;
    aligned.push({ ...word, text: row.text.slice(offset, end), device_id: row.device_id, segment_id: row.segment_id, index, kind: 'word' });
    offset = end;
  }
  if (row.text.slice(offset).trim()) return fallback;
  aligned.at(-1).text += row.text.slice(offset);
  return aligned;
}
export function clipFromUnits(selected, rows, now = Date.now()) {
  if (!selected.length || new Set(selected.map(unit => unit.device_id)).size !== 1) throw new Error('Selections must stay within one source.');
  // Full-row quotes use the authoritative transcript, not reconstructed word tokens.
  const quoted = [];
  for (const row of rows) {
    const part = selected.filter(unit => unit.segment_id === row.segment_id);
    if (!part.length) continue;
    quoted.push(part.length === units(row).length ? row.text : part.map(unit => unit.text).join('').trim());
  }
  return { device_id: selected[0].device_id, from_ms: Math.min(...selected.map(unit => unit.start_utc_ms)), to_ms: Math.max(...selected.map(unit => unit.end_utc_ms)), quoted_text: quoted.join('\n'), captured_at_ms: now };
}
