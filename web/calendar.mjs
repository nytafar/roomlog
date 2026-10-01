export const TIMEZONE = "Europe/Oslo";
const partsFormatter = new Intl.DateTimeFormat("en-CA", {
  timeZone: TIMEZONE,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hourCycle: "h23",
});
export function localDate(ms) {
  const p = Object.fromEntries(
    partsFormatter.formatToParts(ms).map((p) => [p.type, p.value]),
  );
  return `${p.year}-${p.month}-${p.day}`;
}
export function shiftDate(date, days) {
  const d = new Date(date + "T12:00:00Z");
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}
export function midnight(date) {
  const target = Date.parse(date + "T00:00:00Z");
  let result = target;
  for (let i = 0; i < 4; i++) {
    const p = Object.fromEntries(
      partsFormatter.formatToParts(result).map((p) => [p.type, p.value]),
    );
    const represented = Date.UTC(
      +p.year,
      +p.month - 1,
      +p.day,
      +p.hour,
      +p.minute,
      +p.second,
    );
    result += target - represented;
  }
  return result;
}
export function dayRange(date) {
  return { from_ms: midnight(date), to_ms: midnight(shiftDate(date, 1)) };
}
export function weekDates(date) {
  const weekday = new Date(date + "T12:00:00Z").getUTCDay();
  const start = shiftDate(date, -((weekday + 6) % 7));
  return Array.from({ length: 7 }, (_, i) => shiftDate(start, i));
}
export function time(ms) {
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: TIMEZONE,
    hour: "2-digit",
    minute: "2-digit",
  }).format(ms);
}
export function dateLabel(date) {
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: TIMEZONE,
    weekday: "short",
    day: "numeric",
    month: "short",
  }).format(midnight(date));
}
