// Actual browser integration over a local HTTP fixture adapter. No production data.
import { createServer } from "node:http";
import { spawn } from "node:child_process";
import { readFile, writeFile, mkdtemp, mkdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve, extname } from "node:path";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { localDate } from "../calendar.mjs";
const root = resolve(fileURLToPath(new URL("..", import.meta.url)));
const fixture = JSON.parse(
  await readFile(join(root, "../contract/recall/transcript.json"), "utf8"),
);
const standalone = JSON.parse(
  await readFile(join(root, "../contract/recall/standalone-dictation.json"), "utf8"),
);
fixture.items.push(...standalone.items);
fixture.items.sort((a, b) => a.start_utc_ms - b.start_utc_ms);
const timeline = JSON.parse(
  await readFile(
    join(root, "../contract/recall/unknown-timeline.json"),
    "utf8",
  ),
);
const base = fixture.items[0].start_utc_ms;
const chunkAudio = await readFile(join(root, "tests/fixtures/chunk.opus"));
const audioRequests = [];
let latestKnown = base + 5000;
let current = fixture.items,
  fail = false,
  requests = [];
const sourceIds = [...new Set(fixture.items.map((row) => row.device_id))];
const server = createServer(async (req, res) => {
  try {
    const url = new URL(req.url, "http://localhost");
    if (url.pathname.startsWith("/v1/recall/")) {
      requests.push(url);
      res.setHeader("Content-Type", "application/json");
      if (req.headers.authorization !== "Bearer browser-test") {
        res.writeHead(401);
        res.end(
          JSON.stringify({
            error: {
              code: "unauthorized",
              message: "Read credential required",
            },
          }),
        );
        return;
      }
      if (fail) {
        res.writeHead(503);
        res.end(
          JSON.stringify({
            error: { code: "read_budget_exhausted", message: "Try again" },
          }),
        );
        return;
      }
      const route = url.pathname.split("/").at(-1),
        p = url.searchParams;
      let payload;
      const selected = current.filter(
        (row) =>
          row.start_utc_ms < Number(p.get("to_ms")) &&
          (row.end_utc_ms > Number(p.get("from_ms")) ||
            (row.end_utc_ms === row.start_utc_ms && row.start_utc_ms >= Number(p.get("from_ms")))) &&
          (!p.get("device_id") || row.device_id === p.get("device_id")) &&
          (!p.get("channel") ||
            p.get("channel") === "all" ||
            (p.get("channel") === "ambient"
              ? row.channel !== "dictation"
              : row.channel === "dictation")),
      );
      if (route === "audio") {
        const id = p.get("chunk_id");
        audioRequests.push(id);
        if (id === "1") {
          res.setHeader("Content-Type", "audio/ogg");
          res.end(chunkAudio);
        } else {
          res.writeHead(404);
          res.end(JSON.stringify({ error: { code: "audio_unavailable", message: "No file" } }));
        }
        return;
      }
      if (route === "info")
        payload = {
          server_time_ms: base + 10000,
          capabilities: { audio: true },
          limits: { transcript_window_ms: 86400000, spans_window_ms: 86400000 },
          sources: sourceIds.map((id) => ({
            device_id: id,
            label: id === "nyta" ? "Office" : "Phone",
            display_as: id === "nyta" ? "room" : "device",
            first_known_ms: base,
            last_known_ms: latestKnown,
          })),
        };
      else if (route === "transcript" || route === "search")
        payload = { ...fixture, items: selected };
      else if (route === "timeline") payload = timeline;
      else if (route === "sessions")
        payload = {
          items: [
            {
              session_id: "session-1",
              device_id: "nyta",
              start_utc_ms: base,
              end_utc_ms: base + 5000,
              gap_s: 300,
              closed: true,
              opening: "Hei verden.",
              n_visible_segments: 1,
            },
          ],
          next_cursor: null,
        };
      else if (route === "spans") payload = { items: [], next_cursor: null };
      else throw Error("Unknown fixture route");
      res.end(JSON.stringify(payload));
      return;
    }
    const path = resolve(
      root,
      "." + (url.pathname === "/" ? "/index.html" : url.pathname),
    );
    if (!path.startsWith(root + "/")) {
      res.writeHead(404);
      res.end();
      return;
    }
    res.setHeader(
      "Content-Type",
      { ".html": "text/html", ".mjs": "text/javascript", ".css": "text/css" }[
        extname(path)
      ] || "text/plain",
    );
    res.end(await readFile(path));
  } catch (error) {
    res.writeHead(500);
    res.end(error.message);
  }
});
await new Promise((r) => server.listen(0, "127.0.0.1", r));
const profile = await mkdtemp(join(tmpdir(), "roomlog-browser-"));
const chrome = spawn(
  "nice",
  [
    "-n",
    "15",
    "chromium",
    "--headless=new",
    "--disable-gpu",
    "--no-sandbox",
    "--remote-debugging-port=0",
    `--user-data-dir=${profile}`,
    "about:blank",
  ],
  { stdio: "ignore" },
);
let ws;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
try {
  let port;
  for (let i = 0; i < 80; i++) {
    try {
      port = +(
        await readFile(join(profile, "DevToolsActivePort"), "utf8")
      ).split("\n")[0];
      break;
    } catch {}
    await sleep(100);
  }
  assert.ok(port, "Chromium started");
  const targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
  ws = new WebSocket(
    targets.find((t) => t.type === "page").webSocketDebuggerUrl,
  );
  await new Promise((r) => (ws.onopen = r));
  let sequence = 0;
  const pending = new Map(),
    errors = [];
  ws.onmessage = (event) => {
    const message = JSON.parse(event.data);
    if (message.id) {
      const resolve = pending.get(message.id);
      pending.delete(message.id);
      resolve?.(message);
    }
    if (message.method === "Runtime.exceptionThrown")
      errors.push(
        message.params.exceptionDetails.exception?.description ||
          message.params.exceptionDetails.text,
      );
  };
  const cmd = (method, params = {}) =>
    new Promise((resolve, reject) => {
      const id = ++sequence;
      pending.set(id, (response) =>
        response.error
          ? reject(Error(JSON.stringify(response.error)))
          : resolve(response.result),
      );
      ws.send(JSON.stringify({ id, method, params }));
    });
  const evaluate = async (expression) => {
    const result = await cmd("Runtime.evaluate", {
      expression,
      awaitPromise: true,
      returnByValue: true,
    });
    if (result.exceptionDetails)
      throw Error(
        result.exceptionDetails.exception?.description ||
          result.exceptionDetails.text,
      );
    return result.result.value;
  };
  const waitFor = async (expression) => {
    for (let i = 0; i < 100; i++) {
      if (await evaluate(expression)) return;
      await sleep(50);
    }
    throw Error(
      "Browser condition timed out: " +
        expression +
        "; " +
        (await evaluate(
          `JSON.stringify({status:document.querySelector('#status')?.textContent,login:document.querySelector('#login-error')?.textContent})`,
        )) +
        "; " +
        errors.join("\n"),
    );
  };
  const change = async (selector, value) =>
    evaluate(
      `(()=>{const el=document.querySelector(${JSON.stringify(selector)});el.value=${JSON.stringify(value)};el.dispatchEvent(new Event('change'));})()`,
    );
  await cmd("Runtime.enable");
  await cmd("Page.enable");
  await cmd("Emulation.setDeviceMetricsOverride", {
    width: 390,
    height: 844,
    deviceScaleFactor: 1,
    mobile: true,
  });
  await cmd("Page.navigate", {
    url: `http://127.0.0.1:${server.address().port}/`,
  });
  await waitFor(
    `document.readyState==='complete'&&!!document.querySelector('#token')`,
  );
  await evaluate(
    `document.querySelector('#token').value='browser-test';document.querySelector('#login').requestSubmit()`,
  );
  await waitFor(
    `document.querySelectorAll('[data-unit]').length===2&&!document.querySelector('#status').textContent.includes('Loading')`,
  );
  assert.equal(
    await evaluate(`document.querySelector('.turn p').textContent`),
    "Hei verden.",
  );
  assert.equal(
    await evaluate(`document.documentElement.scrollWidth<=innerWidth`),
    true,
    "mobile layout fits",
  );
  assert.equal(
    await evaluate(`localStorage.length`),
    0,
    "credentials and transcripts not persisted",
  );
  // Real touch input over a long history window must move the document.
  await cmd("Emulation.setTouchEmulationEnabled", {enabled: true, maxTouchPoints: 1});
  await evaluate(`window.scrollProbe=document.createElement('div');scrollProbe.style.height='2400px';document.querySelector('#vp').append(scrollProbe);window.scrollTo(0,0)`);
  const scrollPoint = await evaluate(`(()=>{const r=scrollProbe.getBoundingClientRect();return {x:195,y:Math.max(450,r.top+30)}})()`);
  await cmd("Input.dispatchTouchEvent", {type:"touchStart",touchPoints:[scrollPoint]});
  for(let i=1;i<=10;i++) {
    await cmd("Input.dispatchTouchEvent", {type:"touchMove",touchPoints:[{x:scrollPoint.x,y:scrollPoint.y-i*20}]});
    await sleep(20);
  }
  await cmd("Input.dispatchTouchEvent", {type:"touchEnd",touchPoints:[]});
  await sleep(200);
  const scrolled = await evaluate(`({window:scrollY,body:document.body.scrollTop,root:document.documentElement.scrollTop,bodyHeight:document.body.scrollHeight,rootHeight:document.documentElement.scrollHeight})`);
  assert.ok(scrolled.window > 50, "swiping history scrolls the document down");
  await cmd("Input.dispatchTouchEvent", {type:"touchStart",touchPoints:[{x:195,y:400}]});
  for(let i=1;i<=5;i++) {
    await cmd("Input.dispatchTouchEvent", {type:"touchMove",touchPoints:[{x:195,y:400+i*20}]});
    await sleep(20);
  }
  await cmd("Input.dispatchTouchEvent", {type:"touchEnd",touchPoints:[]});
  await sleep(100);
  assert.ok(await evaluate(`scrollY`) < scrolled.window - 50, "swiping back scrolls the document up");
  await evaluate(`scrollProbe.remove();window.scrollTo(0,0)`);
  // Loaded day detail is reusable at session/text zoom and focused ranges.
  const warmReadCount = requests.length;
  await evaluate(`window.warmFirstTurn=document.querySelector(".turn")`);
  for (const level of ["session", "text"]) {
    await evaluate(`document.querySelector('[data-level="${level}"]').click()`);
    await waitFor(`document.querySelector('[data-level="${level}"]').classList.contains('cur')&&!document.querySelector('#status').textContent.includes('Loading')`);
  }
  assert.equal(requests.length, warmReadCount, "warm zoom never waits on HTTP");
  assert.equal(await evaluate(`document.querySelector(".turn")===window.warmFirstTurn`),true,"warm zoom retains the rendered transcript tree");
  await evaluate(`document.querySelector('[data-level="session"]').click()`);
  await waitFor(`document.querySelector('.blk')!==null&&!document.querySelector('#status').textContent.includes('Loading')`);
  await evaluate(`document.querySelector('.blk').click()`);
  await waitFor(`document.querySelector('[data-level="text"]').classList.contains('cur')&&!document.querySelector('#status').textContent.includes('Loading')`);
  assert.equal(requests.length, warmReadCount, "focused session uses covering day detail");
  await change("#source", "");
  await waitFor(`document.querySelectorAll("[data-unit]").length===2&&!document.querySelector("#status").textContent.includes("Loading")`);
  // Select the real timed word through a browser pointer event.
  const xy = await evaluate(
    `(()=>{const n=document.querySelector('[data-unit="1"]');n.scrollIntoView({block:'center'});const r=n.getBoundingClientRect();return [n.closest(".turn").getBoundingClientRect().right-2,r.top+r.height/2];})()`,
  );
  assert.equal(await evaluate(`document.elementFromPoint(${xy[0]},${xy[1]})?.closest("[data-unit]")==null`),true,"gutter selection exercises visible-row hit testing");
  const press = async ([x, y], hold = 0) => {
    await cmd("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", clickCount: 1 });
    if (hold) await sleep(hold);
    await cmd("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", clickCount: 1 });
  };
  // A tap on a word plays its chunk from that word; the player docks at the bottom.
  const word = await evaluate(
    `(()=>{const r=document.querySelector('[data-unit="1"]').getBoundingClientRect();return [r.left+r.width/2,r.top+r.height/2];})()`,
  );
  await press(word);
  await waitFor(`!document.querySelector('#player').hidden&&Number(document.querySelector('#seek').value)>0`);
  assert.equal(await evaluate(`document.querySelector('#clipbar').hidden`), true, "a tap plays, it does not select");
  assert.ok(
    (await evaluate(`Number(document.querySelector('#seek').value)`)) >= 0.85,
    "playback starts at the word (0.9 s into the chunk)",
  );
  await waitFor(`document.querySelector('.w.playing')?.dataset.unit==="1"&&document.querySelector('.turn.playing')!==null`);
  if (process.env.ROOMLOG_SCREENSHOT_DIR) {
    await mkdir(process.env.ROOMLOG_SCREENSHOT_DIR, { recursive: true });
    await writeFile(
      join(process.env.ROOMLOG_SCREENSHOT_DIR, "recall-player.png"),
      Buffer.from((await cmd("Page.captureScreenshot", { format: "png" })).data, "base64"),
    );
  }
  await evaluate(`document.querySelector('#play-toggle').click()`);
  await waitFor(`document.querySelector('#play-toggle').getAttribute('aria-label')==='Play'`);
  await evaluate(`(()=>{const s=document.querySelector('#seek');s.value='0.2';s.dispatchEvent(new Event('input'))})()`);
  await waitFor(`document.querySelector('.w.playing')?.dataset.unit==="0"`);
  await evaluate(`document.querySelector('.turn .play').click()`);
  await waitFor(`document.querySelector('#play-toggle').getAttribute('aria-label')==='Pause'`);
  assert.deepEqual(audioRequests, ["1"], "replaying a chunk reuses its blob");
  await evaluate(`document.querySelector('#play-stop').click()`);
  await waitFor(`document.querySelector('#player').hidden&&document.querySelector('.playing')===null`);
  // A hold still selects, from the gutter beside the text.
  await press(xy, 550);
  await waitFor(`!document.querySelector('#clipbar').hidden`);
  assert.equal(await evaluate(`document.querySelector('#play-selection').hidden`), false);
  await evaluate(`document.querySelector('#save-clip').click()`);
  assert.equal(
    await evaluate(`document.querySelectorAll('#tray .chip').length`),
    1,
    "selection can be saved",
  );
  // Dictation without a session and cancelled STT both appear only when requested.
  await change("#channel", "all");
  await waitFor(`document.querySelectorAll('.turn').length===3`);
  assert.equal(
    await evaluate(`document.querySelector('[data-segment="2"] .play').disabled`),
    true,
    "dictation without a chunk shows it has no audio",
  );
  await evaluate(
    `document.querySelector('[data-segment="2"] .inspect').click()`,
  );
  assert.ok(
    (await evaluate(`document.querySelector('#sheet').textContent`)).includes(
      "dictation",
    ),
  );
  await evaluate(`document.querySelector('#scrim').click()`);
  // Malicious transcript/recipient text must remain inert DOM content.
  current = [
    {
      ...fixture.items[0],
      text: '<img src=x onerror="window.pwned=1">',
      chunk_id: "9",
      timing: "segment",
      words: null,
    },
  ];
  await evaluate(`document.querySelector('#refresh').click()`);
  await waitFor(
    `document.querySelectorAll('.turn').length===1&&!document.querySelector('#status').textContent.includes('Loading')`,
  );
  assert.equal(
    await evaluate(
      `document.querySelector('.turn p img')===null&&window.pwned===undefined`,
    ),
    true,
  );
  // A chunk whose file is unavailable marks its line and opens no player.
  await evaluate(`document.querySelector('.turn .play').click()`);
  await waitFor(`document.querySelector('.turn .play').disabled`);
  assert.equal(await evaluate(`document.querySelector('#player').hidden`), true);
  fail = true;
  await evaluate(`document.querySelector('#refresh').click()`);
  await waitFor(
    `document.querySelector('#status').textContent.includes('Stale history')`,
  );
  assert.equal(
    await evaluate(`document.querySelectorAll('.turn').length`),
    1,
    "failed refresh keeps prior view",
  );
  fail = false;
  current = [];
  await evaluate(`document.querySelector('#refresh').click()`);
  await waitFor(
    `document.querySelectorAll('.turn').length===0&&!document.querySelector('#status').textContent.includes('Loading')`,
  );
  current = fixture.items;
  await evaluate(`document.querySelector("#refresh").click()`);
  await waitFor(`document.querySelectorAll(".turn").length>0&&!document.querySelector("#status").textContent.includes("Loading")`);
  await evaluate(`document.querySelector('[data-level="week"]').click()`);
  await waitFor(`document.querySelectorAll('.day').length===7`);
  await evaluate(
    `document.querySelector('.day[data-date="${localDate(base)}"]').click()`,
  );
  await waitFor(`document.querySelectorAll('.blk').length>0`);
  await evaluate(`document.querySelector('.blk').click()`);
  await waitFor(
    `document.querySelector('[data-level="session"]').classList.contains('cur')&&document.querySelectorAll('.turn').length>0`,
  );
  await evaluate(`document.querySelector('.blk').click()`);
  await waitFor(
    `document.querySelector('[data-level="text"]').classList.contains('cur')&&document.querySelectorAll('.turn').length>0`,
  );
  const shots = process.env.ROOMLOG_SCREENSHOT_DIR;
  if (shots) {
    await mkdir(shots, { recursive: true });
    await writeFile(
      join(shots, "recall-mobile.png"),
      Buffer.from(
        (await cmd("Page.captureScreenshot", { format: "png" })).data,
        "base64",
      ),
    );
  }
  // A latest point at Oslo midnight belongs to the new day.
  const midnight = Date.parse("2026-10-02T00:00:00+02:00");
  latestKnown = midnight;
  current = [{...fixture.items[0], start_utc_ms: midnight, end_utc_ms: midnight,
    text: "Midnight point", timing: "segment", words: null}];
  await evaluate(`document.querySelector('#logout').click()`);
  await evaluate(`document.querySelector('#token').value='browser-test';document.querySelector('#login').requestSubmit()`);
  await waitFor(`document.querySelector('.turn p')?.textContent==='Midnight point'`);
  current = [{...current[0], text: "Fresh midnight point"}];
  latestKnown = midnight + 1;
  await evaluate(`document.querySelector('#latest').click()`);
  await waitFor(`document.querySelector('.turn p')?.textContent==='Fresh midnight point'&&!document.querySelector('#status').textContent.includes('Loading')`);
  await evaluate(`document.querySelector('#logout').click()`);
  assert.equal(
    await evaluate(
      `document.querySelector('#app').hidden&&!document.querySelector('#login').hidden`,
    ),
    true,
  );
  assert.deepEqual(errors, [], "no browser runtime errors");
  assert.ok(
    requests.every((url) => !url.href.includes("browser-test")),
    "token never enters request URLs",
  );
  console.log(
    "Browser passed: real modules, shared wire fixtures, mobile layout, tap-to-play audio, timed selection, tray, filters, provenance, safe text, refresh removal, stale state, zoom navigation and logout.",
  );
} finally {
  ws?.close();
  chrome.kill();
  server.closeAllConnections();
  await new Promise((r) => server.close(r));
  await new Promise((r) =>
    chrome.exitCode !== null ? r() : chrome.once("exit", r),
  );
  await rm(profile, {
    recursive: true,
    force: true,
    maxRetries: 10,
    retryDelay: 100,
  });
}
