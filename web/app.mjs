import {
  HttpRecall,
  WindowStore,
  pages,
  boundedPages,
  units,
  clipFromUnits,
} from "./adapter.mjs";
import {
  TIMEZONE,
  localDate,
  shiftDate,
  dayRange,
  weekDates,
  midnight,
  time,
  dateLabel,
} from "./calendar.mjs";
import {
  groupBounds,
  selectionUnits,
  intersectsSelection,
} from "./selection.mjs";
const $ = (selector) => document.querySelector(selector);
const el = (tag, text, className) => {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  if (className) node.className = className;
  return node;
};
const colors = ["#ffd166", "#71c9bd", "#a1a0f0", "#f4978e", "#90bd7e"];
let adapter,
  store,
  info,
  data,
  generation = 0,
  loading = false;
const state = {
  level: "text",
  date: localDate(Date.now()),
  source: "",
  channel: "ambient",
  lang: "",
  recipients: {},
  focus: null,
  query: "",
  selection: null,
  clips: [],
  activeClip: null,
};
let allUnits = [],
  rows = [];
function sourceLabel(id) {
  return info?.sources.find((source) => source.device_id === id)?.label || id;
}
function color(id) {
  return colors[
    Math.max(
      0,
      info?.sources.findIndex((source) => source.device_id === id) ?? 0,
    ) % colors.length
  ];
}
function filters() {
  return {
    channel: state.channel,
    device_id: state.source || undefined,
    lang: state.lang,
    ...state.recipients,
  };
}
function status(message) {
  $("#status").textContent = message;
}
function sourceOptions() {
  const select = $("#source");
  select.replaceChildren(el("option", "All sources"));
  select.firstChild.value = "";
  for (const source of info.sources) {
    const option = el("option", source.label);
    option.value = source.device_id;
    select.append(option);
  }
  select.value = state.source;
}
function toast(message) {
  $("#toast").textContent = message;
  $("#toast").classList.add("show");
  setTimeout(() => $("#toast").classList.remove("show"), 2500);
}
async function login(event) {
  event.preventDefault();
  $("#login-error").textContent = "";
  const token = $("#token").value;
  $("#token").value = "";
  const client = new HttpRecall(token);
  try {
    const setup = await client.read("info");
    adapter = client;
    info = setup;
    store = new WindowStore(adapter);
    sourceOptions();
    const last = Math.max(
      0,
      ...info.sources.map((source) => source.last_known_ms || 0),
    );
    if (last) state.date = localDate(Math.max(0, last - 1));
    if (info.sources.length === 1) state.source = info.sources[0].device_id;
    $("#source").value = state.source;
    $("#login").hidden = true;
    $("#app").hidden = false;
    await load();
  } catch (error) {
    $("#login-error").textContent = error.message;
  }
}
$("#login").addEventListener("submit", login);
$("#logout").onclick = () => {
  generation++;
  store?.cancel();
  adapter = null;
  store = null;
  info = null;
  data = null;
  loading = false;
  state.selection = null;
  state.clips = [];
  state.source = "";
  state.focus = null;
  rows = [];
  allUnits = [];
  renderTray();
  paintSelection();
  closeSheet();
  $("#view").replaceChildren();
  $("#app").hidden = true;
  $("#login").hidden = false;
  $("#token").focus();
};
function requestedRange() {
  if (state.level === "week" || state.query) {
    const dates = weekDates(state.date);
    return {
      from_ms: midnight(dates[0]),
      to_ms: midnight(shiftDate(dates[6], 1)),
    };
  }
  // Never fetch more than a local day at text level; long sessions are navigated by day.
  const day = dayRange(state.date);
  if (state.focus && (state.level === "text" || state.level === "session"))
    return {
      from_ms: Math.max(day.from_ms, state.focus.from_ms),
      to_ms: Math.min(
        day.to_ms,
        Math.max(state.focus.from_ms + 1, state.focus.to_ms),
      ),
    };
  return day;
}
async function load() {
  if (!store) return;
  const mine = ++generation;
  const range = requestedRange();
  const params = { ...range, ...filters() };
  const snapshot = { ...state, recipients: { ...state.recipients } };
  $("#date").value = state.date;
  document
    .querySelectorAll("[data-level]")
    .forEach((button) =>
      button.classList.toggle("cur", button.dataset.level === state.level),
    );
  $("#range-label").textContent =
    state.level === "week" || state.query
      ? "Week of " + dateLabel(weekDates(state.date)[0])
      : dateLabel(state.date);
  loading = true;
  status("Loading…");
  let nextInfo = info;
  const searchMode = $("#search-mode").value;
  const key = JSON.stringify({
    level: state.level,
    query: state.query,
    mode: searchMode,
    params,
  });
  const result = await store.load(key, async (client, signal) => {
    nextInfo = await client.read("info", {}, signal);
    const detail = (route, p) =>
      boundedPages(
        client,
        route,
        p,
        signal,
        nextInfo.limits?.[route + "_window_ms"] || 86400000,
      );
    if (snapshot.query)
      return {
        search: await pages(
          client,
          "search",
          { ...params, q: snapshot.query, mode: searchMode },
          signal,
        ),
      };
    if (snapshot.level === "week")
      return {
        timeline: await client.read(
          "timeline",
          { ...params, tz: TIMEZONE, bucket: "hour" },
          signal,
        ),
      };
    const sources = snapshot.source
      ? [snapshot.source]
      : nextInfo.sources.map((source) => source.device_id);
    const sessionsPromise = pages(client, "sessions", params, signal);
    if (snapshot.level === "day") {
      const reportsPromise =
        snapshot.channel === "ambient"
          ? Promise.resolve([])
          : readSources(sources, (device_id) =>
              detail("spans", {
                ...range,
                device_id,
                lang: snapshot.lang,
                ...snapshot.recipients,
              }),
            );
      const [sessions, reports] = await Promise.all([
        sessionsPromise,
        reportsPromise,
      ]);
      return { sessions, reports: reports.flatMap((page) => page.items) };
    }
    // Overlapping sources stay distinct; all detail requests remain bounded by source/day.
    const [sessions, transcripts, reports] = await Promise.all([
      sessionsPromise,
      readSources(sources, (device_id) =>
        detail("transcript", { ...params, device_id }),
      ),
      snapshot.channel === "ambient"
        ? Promise.resolve([])
        : readSources(sources, (device_id) =>
            detail("spans", {
              ...range,
              device_id,
              lang: snapshot.lang,
              ...snapshot.recipients,
            }),
          ),
    ]);
    return {
      sessions,
      transcript: transcripts
        .flatMap((page) => page.items)
        .sort(
          (a, b) =>
            a.start_utc_ms - b.start_utc_ms ||
            a.device_id.localeCompare(b.device_id) ||
            a.segment_id.localeCompare(b.segment_id),
        ),
      reports: reports.flatMap((page) => page.items),
      as_of_ms: Math.max(0, ...transcripts.map((page) => page.as_of_ms)),
    };
  });
  if (mine !== generation || result.obsolete) return;
  loading = false;
  if (result.value) {
    info = nextInfo;
    sourceOptions();
    data = result.value;
    render();
  } else {
    data = null;
    $("#view").replaceChildren(
      el("p", "No successful load for this window.", "empty"),
    );
  }
  if (result.error) {
    status(
      `${result.value ? "Stale history · " : ""}${result.error.message} · use ↻ to retry`,
    );
    if (result.error.status === 401 || result.error.status === 403)
      toast("Read access rejected. Log out and enter the owner read token.");
  } else status(`Updated ${time(Date.now())}`);
}
// One request per category at a time stays within the reader's concurrency budget.
async function readSources(sources, read) {
  const result = [];
  for (const source of sources) result.push(await read(source));
  return result;
}
function navigate(
  level,
  date = state.date,
  source = state.source,
  focus = null,
) {
  state.level = level;
  state.date = date;
  state.source = source;
  state.focus = focus;
  state.query = "";
  $("#query").value = "";
  $("#source").value = source;
  load();
  window.scrollTo({ top: 0 });
}
document
  .querySelectorAll("[data-level]")
  .forEach((button) => (button.onclick = () => navigate(button.dataset.level)));
$("#prev").onclick = () =>
  navigate(
    state.level,
    shiftDate(state.date, state.level === "week" ? -7 : -1),
    state.source,
  );
$("#next").onclick = () =>
  navigate(
    state.level,
    shiftDate(state.date, state.level === "week" ? 7 : 1),
    state.source,
  );
$("#date").onchange = () => {
  if ($("#date").value) navigate(state.level, $("#date").value, state.source);
};
$("#latest").onclick = async () => {
  const current = adapter;
  try {
    const setup = await current.read("info");
    if (adapter !== current) return;
    info = setup;
    sourceOptions();
    const latest = Math.max(
      0,
      ...info.sources.map((source) => source.last_known_ms || 0),
    );
    navigate(
      state.level,
      localDate(latest ? latest - 1 : Date.now()),
      state.source,
    );
  } catch (error) {
    status(error.message);
  }
};
$("#refresh").onclick = () => load();
for (const id of ["source", "channel", "lang"])
  $("#" + id).onchange = () => {
    state[id] = $("#" + id).value.trim();
    state.focus = null;
    state.selection = null;
    paintSelection();
    load();
  };
document.querySelectorAll("[data-filter]").forEach(
  (input) =>
    (input.onchange = () => {
      if (input.value) state.recipients[input.dataset.filter] = input.value;
      else delete state.recipients[input.dataset.filter];
      state.focus = null;
      state.selection = null;
      paintSelection();
      load();
    }),
);
$("#searchform").onsubmit = (event) => {
  event.preventDefault();
  state.query = $("#query").value.trim();
  load();
};
$("#clear-search").onclick = () => {
  state.query = "";
  $("#query").value = "";
  load();
};
function render() {
  const view = $("#view");
  view.replaceChildren();
  rows = [];
  allUnits = [];
  paintSelection();
  if (data.search) {
    renderSearch(view);
    return;
  }
  if (data.timeline) {
    renderWeek(view);
    return;
  }
  if (state.level === "day") {
    renderDay(view);
    return;
  }
  renderText(view, state.level === "session");
  paintSelection();
}
function renderWeek(view) {
  const buckets = data.timeline.rows || [];
  for (const date of weekDates(state.date)) {
    const day = el("button", null, "day");
    day.dataset.date = date;
    const dd = el("div", null, "dd");
    dd.append(
      el("div", dateLabel(date).split(" ")[0], "dn"),
      el("div", date.slice(-2), "dnum"),
    );
    day.append(dd);
    const lanes = el("div", null, "sp");
    const range = dayRange(date);
    let count = 0;
    for (const source of info.sources.filter(
      (source) => !state.source || source.device_id === state.source,
    )) {
      lanes.append(el("div", source.label, "lane-label"));
      const lane = el("div", null, "lane");
      lane.style.setProperty("--rc", color(source.device_id));
      const sourceBuckets = buckets.filter(
        (bucket) =>
          bucket.device_id === source.device_id &&
          bucket.from_ms >= range.from_ms &&
          bucket.from_ms < range.to_ms,
      );
      for (const bucket of sourceBuckets) {
        const bar = el("i");
        const duration = bucket.to_ms - bucket.from_ms;
        bar.style.height =
          Math.max(2, 22 * Math.min(1, bucket.text_ms / duration)) + "px";
        bar.style.opacity = bucket.n_segments ? ".95" : ".15";
        bar.title = `${time(bucket.from_ms)} · ${bucket.n_segments} segments · ${Math.round(bucket.text_ms / 1000)}s text coverage`;
        lane.append(bar);
        count += bucket.n_segments;
      }
      lanes.append(lane);
      const evidence = sourceBuckets.reduce(
        (sum, b) => {
          for (const k of Object.keys(sum)) sum[k] += b.evidence?.[k] || 0;
          return sum;
        },
        {
          captured_ms: 0,
          pending_ms: 0,
          failed_ms: 0,
          no_detected_speech_ms: 0,
          unknown_ms: 0,
        },
      );
      lanes.append(
        el(
          "div",
          `Capture ${durationLabel(evidence.captured_ms)} · pending ${durationLabel(evidence.pending_ms)} · failed ${durationLabel(evidence.failed_ms)} · VAD no speech ${durationLabel(evidence.no_detected_speech_ms)} · unknown ${durationLabel(evidence.unknown_ms)}`,
          "evidence",
        ),
      );
    }
    const axis = el("div", null, "ax");
    axis.append(el("span", "00"), el("span", "12"), el("span", "24"));
    lanes.append(axis);
    day.append(lanes);
    day.onclick = () => navigate("day", date);
    view.append(day);
  }
  view.append(
    el(
      "p",
      "Bars show transcript-covered time per source. Capture evidence is independent of text filters; categories can overlap. Missing capture history is unknown.",
      "empty",
    ),
  );
}
function durationLabel(ms) {
  return ms >= 3600000
    ? `${(ms / 3600000).toFixed(1)}h`
    : `${Math.round(ms / 60000)}m`;
}
function sessionBlock(session, expanded = false) {
  const block = el("button", null, "blk");
  block.style.setProperty("--rc", color(session.device_id));
  const meta = el("div", null, "m");
  meta.append(
    el("b", time(session.start_utc_ms)),
    el("span", sourceLabel(session.device_id), "room"),
    el("span", durationLabel(session.end_utc_ms - session.start_utc_ms)),
    el("span", session.closed ? "closed" : "open"),
  );
  block.append(
    meta,
    el(
      "div",
      session.opening || "No current text · processing may be pending",
      "o",
    ),
  );
  if (expanded)
    block.append(
      el(
        "div",
        `${session.n_visible_segments} visible segments · gap rule ${session.gap_s}s`,
        "counts",
      ),
    );
  block.onclick = () =>
    navigate(expanded ? "text" : "session", state.date, session.device_id, {
      session_id: session.session_id,
      device_id: session.device_id,
      from_ms: session.start_utc_ms,
      to_ms: session.end_utc_ms,
    });
  return block;
}
function renderDay(view) {
  view.append(el("h2", dateLabel(state.date), "dayh"));
  for (const session of data.sessions.items) view.append(sessionBlock(session));
  if (!data.sessions.items.length)
    view.append(
      el("p", "No sessions with current matching text in this day.", "empty"),
    );
  if (state.channel !== "ambient") {
    view.append(el("h3", "Dictation reports", "dayh"));
    for (const report of data.reports) view.append(reportBlock(report, true));
  }
}
function renderText(view, expanded) {
  view.append(el("h2", dateLabel(state.date), "dayh"));
  if (expanded) {
    for (const session of data.sessions.items)
      view.append(sessionBlock(session, true));
  }
  let visible = data.transcript || [];
  if (state.focus) {
    const focused = visible.filter(
      (row) =>
        row.device_id === state.focus.device_id &&
        ((row.start_utc_ms < state.focus.to_ms &&
          row.end_utc_ms > state.focus.from_ms) ||
          (row.start_utc_ms === row.end_utc_ms &&
            row.start_utc_ms >= state.focus.from_ms &&
            row.start_utc_ms < state.focus.to_ms)),
    );
    visible = focused;
    view.append(
      el(
        "p",
        `${time(state.focus.from_ms)}–${time(state.focus.to_ms)} · ${sourceLabel(state.focus.device_id)} · this day`,
        "empty",
      ),
    );
  }
  rows = visible;
  let lastSource, lastSession;
  for (const row of visible) {
    if (row.device_id !== lastSource || row.session_id !== lastSession) {
      view.append(
        el(
          "div",
          `${sourceLabel(row.device_id)} · ${row.session_id ? "Session" : "Ungrouped dictation"}`,
          "convh",
        ),
      );
      lastSource = row.device_id;
      lastSession = row.session_id;
    }
    const turn = el("article", null, "turn");
    turn.dataset.segment = row.segment_id;
    turn.dataset.device = row.device_id;
    turn.style.setProperty("--rc", color(row.device_id));
    const body = el("div", null, "b"),
      meta = el("div", null, "ts");
    const inspect = el("button", "details", "inspect");
    inspect.onclick = () => inspectRow(row);
    meta.append(
      el(
        "span",
        `${time(row.start_utc_ms)} · ${row.channel} · ${row.lang || "language unknown"}`,
      ),
      inspect,
    );
    body.append(meta);
    const text = el("p");
    const effective = units(row);
    for (const unit of effective) {
      const word = el("span", unit.text, "w");
      word.dataset.unit = allUnits.length;
      allUnits.push(unit);
      text.append(word);
    }
    body.append(text);
    if (effective[0]?.kind === "segment")
      body.append(
        el("div", "Segment selection · word timing unavailable", "counts"),
      );
    turn.append(body);
    view.append(turn);
  }
  if (!visible.length)
    view.append(
      el("p", "No matching text. Capture history may be incomplete.", "empty"),
    );
  if (state.channel !== "ambient" && data.reports?.length) {
    view.append(el("h3", "Original dictation reports", "dayh"));
    for (const report of data.reports) view.append(reportBlock(report));
  }
}
function reportBlock(report, open = false) {
  const block = el("article", null, "report");
  block.append(
    el(
      "div",
      `${time(report.start_utc_ms)} · ${sourceLabel(report.device_id)} · ${report.status || "unknown status"}${report.cancelled ? " · cancelled" : ""}`,
      "meta",
    ),
    el("p", report.text || "No reported text"),
  );
  const target = Object.entries(report.target || {})
    .map(([key, value]) => `${key}: ${value}`)
    .join(" · ");
  if (target) block.append(el("div", target, "meta"));
  const actions = el("div", null, "acts"),
    inspect = el("button", "Inspect report");
  inspect.onclick = () => inspectReport(report);
  actions.append(inspect);
  if (open) {
    const jump = el("button", "Open text");
    jump.onclick = () =>
      navigate("text", localDate(report.start_utc_ms), report.device_id);
    actions.append(jump);
  }
  block.append(actions);
  return block;
}
function renderSearch(view) {
  view.append(el("h2", `Matches · ${state.query}`, "dayh"));
  for (const row of data.search.items) {
    const block = el("button", null, "blk");
    block.style.setProperty("--rc", color(row.device_id));
    block.append(
      el(
        "div",
        `${dateLabel(localDate(row.start_utc_ms))} ${time(row.start_utc_ms)} · ${sourceLabel(row.device_id)} · ${row.channel}`,
        "m",
      ),
      el("div", row.text, "o"),
    );
    block.onclick = () =>
      navigate("text", localDate(row.start_utc_ms), row.device_id, {
        device_id: row.device_id,
        from_ms: row.start_utc_ms,
        to_ms: Math.max(row.start_utc_ms + 1, row.end_utc_ms),
      });
    view.append(block);
  }
  if (!data.search.items.length)
    view.append(el("p", "No matching current text.", "empty"));
}
function showSheet(title, fields, actions = []) {
  const sheet = $("#sheet");
  sheet.replaceChildren(el("h2", title));
  const dl = el("dl");
  for (const [key, value] of fields) {
    dl.append(
      el("dt", key),
      el(
        "dd",
        typeof value === "object"
          ? JSON.stringify(value, null, 2)
          : String(value ?? "unknown"),
      ),
    );
  }
  sheet.append(dl);
  const buttons = el("div", null, "acts");
  for (const [label, action] of actions) {
    const b = el("button", label);
    b.onclick = action;
    buttons.append(b);
  }
  const close = el("button", "Close");
  close.onclick = closeSheet;
  buttons.append(close);
  sheet.append(buttons);
  $("#scrim").style.display = "block";
  sheet.classList.add("open");
  close.focus();
}
function closeSheet() {
  $("#sheet").classList.remove("open");
  $("#scrim").style.display = "none";
}
$("#scrim").onclick = closeSheet;
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeSheet();
});
function inspectRow(row) {
  const overlapping = (data.reports || []).filter(
    (report) =>
      report.device_id === row.device_id &&
      ((report.start_utc_ms < row.end_utc_ms &&
        report.end_utc_ms > row.start_utc_ms) ||
        report.start_utc_ms === row.start_utc_ms),
  );
  showSheet(
    "Transcript provenance",
    [
      ["Source", `${sourceLabel(row.device_id)} · device ${row.device_id}`],
      ["Range UTC ms", [row.start_utc_ms, row.end_utc_ms]],
      ["Text", row.text],
      ["Channel", row.channel],
      ["Producer", row.producer],
      [
        "Session / chunk / producing report",
        [row.session_id, row.chunk_id, row.span_id],
      ],
      ["Timing", row.timing],
      ["Tags (effective flags retain provenance)", row.tags],
      [
        "Overlapping reports (overlap does not establish production)",
        overlapping.map((report) => ({
          span_id: report.span_id,
          status: report.status,
          cancelled: report.cancelled,
          producing: row.span_id === report.span_id,
        })),
      ],
    ],
    overlapping.map((report) => [
      "Report " + report.span_id,
      () => inspectReport(report),
    ]),
  );
}
function inspectReport(report) {
  showSheet("Original dictation report", [
    ["Device", report.device_id],
    ["Range UTC ms", [report.start_utc_ms, report.end_utc_ms]],
    ["Report ID", report.span_id],
    ["Reported text", report.text],
    ["Status", report.status || "unknown"],
    ["Cancelled", report.cancelled],
    ["Associated segment", report.segment_id],
    ["Engine / mode / origin", [report.engine, report.mode, report.origin]],
    ["Recipient target", report.target],
    ["Device/time scoped tags (not report foreign keys)", report.tags],
  ]);
}
$("#help-button").onclick = () =>
  showSheet("Recall", [
    [
      "Browse",
      "Tap week → day → sessions → text. The selected local day bounds detail loading. Dates use Europe/Oslo, including DST. Use source/channel/language/recipient filters to narrow history.",
    ],
    [
      "Select",
      "Hold text to place IN, drag to extend OUT. Move across the text to choose segment or word precision where timings exist. Drag the IN/OUT bracket to adjust; copy or move the quote to the tray. Selections never span devices.",
    ],
    [
      "Tray",
      "Local to this page session. Reloading clears it. Saved quotes remain the text seen when selected, even if the transcript is rewritten.",
    ],
    [
      "Evidence",
      "Density is transcript-covered time. Missing capture evidence means unknown; VAD no speech is not proof of silence. Audio playback and speaker identity are not available.",
    ],
    [
      "Mobile",
      "The archived prototype’s gesture issues remain unverified on a real phone. Explicit navigation controls remain available.",
    ],
  ]);

function paintSelection() {
  const selected = [];
  document.querySelectorAll("[data-unit]").forEach((node) => {
    const match = intersectsSelection(
      allUnits[Number(node.dataset.unit)],
      state.selection,
    );
    node.classList.toggle("sel", match);
    if (match) selected.push(node);
  });
  $("#clipbar").hidden = !state.selection;
  if (state.selection)
    $("#clip-label").textContent =
      `${sourceLabel(state.selection.device_id)} · ${time(state.selection.from_ms)}–${time(state.selection.to_ms)} · ${state.selection.quoted_text.length} characters`;
  positionMarkers(selected);
}
function positionMarkers(selected = [...document.querySelectorAll(".w.sel")]) {
  const marks = [$("#mark-in"), $("#mark-out")];
  const top = $("#top").getBoundingClientRect().bottom;
  marks.forEach((mark, i) => {
    const node = i ? selected.at(-1) : selected[0];
    if (!node) {
      mark.hidden = true;
      return;
    }
    const rects = node.getClientRects(),
      rect = i ? rects[rects.length - 1] : rects[0];
    const y = i ? rect.bottom + 2 : rect.top - 19;
    mark.hidden = y < top || y > innerHeight - 120;
    mark.style.top = y + "px";
  });
}
function clearSelection() {
  state.selection = null;
  paintSelection();
}
function traySelection(notify = true) {
  if (!state.selection) return;
  state.clips.push({ ...state.selection });
  clearSelection();
  renderTray();
  if (notify) toast("Quote added to tray");
}
function renderTray() {
  const tray = $("#tray");
  tray.replaceChildren();
  document.body.classList.toggle("has-tray", state.clips.length > 0);
  state.clips.forEach((clip, index) => {
    const chip = el("button", null, "chip");
    chip.append(
      el("b", `${sourceLabel(clip.device_id)} · ${time(clip.from_ms)}`),
      el("span", clip.quoted_text),
    );
    chip.onclick = () =>
      showSheet(
        "Saved quote",
        [
          ["Source", sourceLabel(clip.device_id)],
          ["Captured quote", clip.quoted_text],
          ["Range", [clip.from_ms, clip.to_ms]],
        ],
        [
          ["Copy", () => copyQuote(clip)],
          [
            "Open range",
            () => {
              closeSheet();
              navigate("text", localDate(clip.from_ms), clip.device_id, {
                ...clip,
                to_ms: Math.max(clip.from_ms + 1, clip.to_ms),
              });
            },
          ],
          [
            "Remove",
            () => {
              state.clips.splice(index, 1);
              renderTray();
              closeSheet();
            },
          ],
        ],
      );
    tray.append(chip);
  });
}
async function copyQuote(clip = state.selection) {
  if (!clip) return;
  try {
    await navigator.clipboard.writeText(clip.quoted_text);
    toast("Quote copied");
  } catch {
    showSheet("Copy quote", [["Select this text to copy", clip.quoted_text]]);
  }
}
$("#copy").onclick = () => copyQuote();
$("#save-clip").onclick = () => traySelection();
$("#clear-selection").onclick = clearSelection;

// Keep the prototype's horizontal gears and off-text IN/OUT handles. No inferred
// word times are needed: untimed rows naturally stop at segment precision.
let gesture = null;
const levels = ["week", "day", "session", "text"];
function browseZone(x) {
  const f = x / innerWidth;
  return f < 0.1 ? 0 : f < 0.28 ? 1 : f < 0.5 ? 2 : 3;
}
function precision(x, landing = false) {
  const f = x / innerWidth;
  return landing
    ? f < 0.28
      ? "segment"
      : f < 0.78
        ? "sentence"
        : "word"
    : f < 0.08
      ? "hour"
      : f < 0.22
        ? "session"
        : f < 0.45
          ? "segment"
          : f < 0.78
            ? "sentence"
            : "word";
}
function indexAt(x, y, device) {
  const hit = document.elementFromPoint(x, y)?.closest("[data-unit]");
  if (
    hit &&
    (!device || allUnits[Number(hit.dataset.unit)]?.device_id === device)
  )
    return Number(hit.dataset.unit);
  let best = null,
    distance = Infinity;
  for (const node of document.querySelectorAll("[data-unit]")) {
    const unit = allUnits[Number(node.dataset.unit)];
    if (device && unit.device_id !== device) continue;
    const bounds = node.getBoundingClientRect();
    if (bounds.bottom < 0 || bounds.top > innerHeight) continue;
    for (const r of node.getClientRects()) {
      const dy = Math.max(r.top - y, 0, y - r.bottom),
        dx = Math.max(r.left - x, 0, x - r.right),
        d = dy * 4 + dx;
      if (d < distance) {
        distance = d;
        best = Number(node.dataset.unit);
      }
    }
  }
  return best;
}
function setSelection(lo, hi, device) {
  const selected = selectionUnits(allUnits, lo, hi, device);
  if (!selected.length) return;
  state.selection = clipFromUnits(selected, rows);
  paintSelection();
}
function startSelection(g) {
  if (gesture !== g || !allUnits[g.index]) return;
  const bounds = groupBounds(allUnits, g.index, precision(g.x, true), rows);
  traySelection(false);
  g.selecting = true;
  g.device = allUnits[g.index].device_id;
  g.anchor = bounds;
  g.handle = "out";
  setSelection(bounds[0], bounds[1], g.device);
  navigator.vibrate?.(20);
}
document.addEventListener("pointerdown", (event) => {
  if (event.button !== 0 || $("#sheet").classList.contains("open")) return;
  const handle = event.target.closest("[data-handle]");
  if (handle && state.selection) {
    const indices = allUnits
      .map((u, i) => (intersectsSelection(u, state.selection) ? i : -1))
      .filter((i) => i >= 0);
    if (!indices.length) return;
    gesture = {
      id: event.pointerId,
      x: event.clientX,
      y: event.clientY,
      lastY: event.clientY,
      selecting: true,
      device: state.selection.device_id,
      anchor: [indices[0], indices.at(-1)],
      handle: handle.dataset.handle,
    };
    event.preventDefault();
    return;
  }
  if (
    !event.target.closest("#vp") ||
    event.target.closest("button,a,input,select,summary")
  )
    return;
  const index = indexAt(event.clientX, event.clientY);
  const g = (gesture = {
    id: event.pointerId,
    x: event.clientX,
    y: event.clientY,
    lastY: event.clientY,
    index,
    zone: browseZone(event.clientX),
    moved: false,
    selecting: false,
  });
  if (index != null) g.timer = setTimeout(() => startSelection(g), 430);
  if (event.pointerType === "mouse") event.preventDefault();
});
document.addEventListener(
  "pointermove",
  (event) => {
    const g = gesture;
    if (!g || event.pointerId !== g.id) return;
    const dx = event.clientX - g.x,
      dy = event.clientY - g.y;
    if (!g.selecting && Math.hypot(dx, dy) > 8) {
      clearTimeout(g.timer);
      g.moved = true;
    }
    if (g.selecting) {
      event.preventDefault();
      const mode = precision(event.clientX);
      const index = indexAt(event.clientX, event.clientY - 32, g.device);
      if (index != null) {
        const bounds = groupBounds(allUnits, index, mode, rows);
        if (g.handle === "in") setSelection(bounds[0], g.anchor[1], g.device);
        else setSelection(g.anchor[0], bounds[1], g.device);
      }
      $("#gearhint").textContent =
        `${mode} selection · ${sourceLabel(g.device)}`;
      const top = $("#top").getBoundingClientRect().bottom;
      if (event.clientY > innerHeight - 130) window.scrollBy(0, 12);
      else if (event.clientY < top + 40) window.scrollBy(0, -12);
    } else if (g.moved) {
      event.preventDefault();
      window.scrollBy(0, g.lastY - event.clientY);
      const zone = browseZone(event.clientX);
      if (zone !== g.zone && Math.abs(dx) > 22) {
        g.zone = zone;
        const level = levels[zone];
        if (level !== state.level) {
          state.level = level;
          state.focus = null;
          state.query = "";
          $("#query").value = "";
          load();
        }
      }
    }
    g.lastY = event.clientY;
  },
  { passive: false },
);
function endGesture(event) {
  const g = gesture;
  if (!g || event.pointerId !== g.id) return;
  clearTimeout(g.timer);
  if (event.type === "pointerup" && !g.moved && !g.selecting && g.index != null)
    startSelection(g);
  gesture = null;
  $("#gearhint").textContent =
    "week ◀ · day · sessions · text ▶ · hold text to select";
}
document.addEventListener("pointerup", endGesture);
document.addEventListener("pointercancel", endGesture);
let scrollFrame;
window.addEventListener(
  "scroll",
  () => {
    cancelAnimationFrame(scrollFrame);
    scrollFrame = requestAnimationFrame(() => positionMarkers());
  },
  { passive: true },
);
window.addEventListener("resize", () => positionMarkers());
document.addEventListener("keydown", (event) => {
  if (event.target.closest("input,select,textarea") || !adapter) return;
  if (event.key === "Escape") {
    clearSelection();
    return;
  }
  if (
    (event.ctrlKey || event.metaKey) &&
    event.key === "c" &&
    state.selection
  ) {
    event.preventDefault();
    copyQuote();
  }
  if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
    event.preventDefault();
    const index = Math.max(
      0,
      Math.min(
        3,
        levels.indexOf(state.level) + (event.key === "ArrowLeft" ? -1 : 1),
      ),
    );
    navigate(levels[index]);
  }
});
setInterval(() => {
  if (
    adapter &&
    !document.hidden &&
    !loading &&
    !gesture &&
    !$("#sheet").classList.contains("open")
  )
    load();
}, 10000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && adapter && !loading && !gesture) load();
});
window.addEventListener("focus", () => {
  if (adapter && !loading && !gesture) load();
});
