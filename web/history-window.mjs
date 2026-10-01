// View-independent keys let day/session/text reuse one bounded history snapshot.
export function historyKey(level, params, query = "", mode = "literal") {
  const kind = query ? "search" : level === "week" ? "week" : level === "day" ? "day" : "detail";
  const normalized = Object.fromEntries(
    Object.entries(params).filter(([, value]) => value !== "" && value != null)
      .sort(([a], [b]) => a.localeCompare(b)),
  );
  return JSON.stringify({ kind, params: normalized, query, mode: query ? mode : "" });
}
function overlaps(item, params) {
  return (!params.device_id || item.device_id === params.device_id) &&
    (item.start_utc_ms === item.end_utc_ms
      ? item.start_utc_ms >= params.from_ms && item.start_utc_ms < params.to_ms
      : item.start_utc_ms < params.to_ms && item.end_utc_ms > params.from_ms);
}
export function reuseHistory(candidateKey, value, requestKey) {
  const candidate = JSON.parse(candidateKey), request = JSON.parse(requestKey);
  if (!["day", "detail"].includes(request.kind) ||
      !["day", "detail"].includes(candidate.kind) ||
      (request.kind === "detail" && candidate.kind !== "detail")) return;
  const from = candidate.params, to = request.params;
  if (from.from_ms > to.from_ms || from.to_ms < to.to_ms ||
      (from.device_id && from.device_id !== to.device_id)) return;
  const filters = (params) => Object.entries(params).filter(([key]) =>
    !["from_ms", "to_ms", "device_id"].includes(key));
  if (JSON.stringify(filters(from)) !== JSON.stringify(filters(to))) return;
  const project = (items) => items.filter((item) => overlaps(item, to));
  return {
    ...value,
    sessions: { ...value.sessions, items: project(value.sessions.items) },
    ...(value.transcript ? { transcript: project(value.transcript) } : {}),
    reports: project(value.reports || []),
  };
}
// Refresh invalidates overlapping snapshots, including other filters/zoom summaries.
export function overlapsHistory(candidateKey, refreshedKey) {
  const a = JSON.parse(candidateKey).params, b = JSON.parse(refreshedKey).params;
  return a.from_ms < b.to_ms && a.to_ms > b.from_ms &&
    (!a.device_id || !b.device_id || a.device_id === b.device_id);
}
