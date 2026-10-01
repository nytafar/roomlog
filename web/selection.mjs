// Selection is a device/time range. Array positions only exist while dragging.
export function groupBounds(all, index, precision, rows) {
  const unit = all[index];
  if (!unit) return null;
  const own = all
    .map((u, i) => [u, i])
    .filter(([u]) => u.device_id === unit.device_id);
  if (precision === "word") return [index, index];
  const row = rows.find((r) => r.segment_id === unit.segment_id);
  let matches;
  if (precision === "session" && row?.session_id) {
    const ids = new Set(
      rows
        .filter(
          (r) =>
            r.device_id === unit.device_id && r.session_id === row.session_id,
        )
        .map((r) => r.segment_id),
    );
    matches = own.filter(([u]) => ids.has(u.segment_id));
  } else if (precision === "hour") {
    const hour = Math.floor(unit.start_utc_ms / 3600000);
    matches = own.filter(
      ([u]) => Math.floor(u.start_utc_ms / 3600000) === hour,
    );
  } else {
    matches = own.filter(([u]) => u.segment_id === unit.segment_id);
    if (precision === "sentence" && unit.kind === "word") {
      let lo = matches.findIndex(([, i]) => i === index),
        hi = lo;
      while (lo > 0 && !/[.!?…]["'»”)]?\s*$/.test(matches[lo - 1][0].text))
        lo--;
      while (
        hi < matches.length - 1 &&
        !/[.!?…]["'»”)]?\s*$/.test(matches[hi][0].text)
      )
        hi++;
      matches = matches.slice(lo, hi + 1);
    }
  }
  return matches.length ? [matches[0][1], matches.at(-1)[1]] : [index, index];
}

export function selectionUnits(all, fromIndex, toIndex, device) {
  return all
    .slice(Math.min(fromIndex, toIndex), Math.max(fromIndex, toIndex) + 1)
    .filter((u) => u.device_id === device);
}

export function intersectsSelection(unit, selection) {
  if (!unit || !selection || unit.device_id !== selection.device_id)
    return false;
  if (selection.from_ms === selection.to_ms)
    return unit.start_utc_ms === selection.from_ms;
  return (
    unit.start_utc_ms < selection.to_ms &&
    (unit.end_utc_ms > selection.from_ms ||
      (unit.start_utc_ms === unit.end_utc_ms &&
        unit.start_utc_ms >= selection.from_ms))
  );
}
