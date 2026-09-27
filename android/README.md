# roomlog Android thin client

Two Gradle modules. The plan and its decisions live in `~/hvelv/repos/roomlog/android/plan-thin-client.md`
and ADRs 0005 to 0007, not here.

| Module | What |
|---|---|
| `:core` | Pure Kotlin/JVM, no Android imports, JVM-tested. Each class mirrors an edge module so the pytest vectors port one to one: `Timeline` (`timeline.py`), `RawSegmenter` (fixed 30 s raw segments, ADR 0005), `OggOpusWriter` (inverse of `ogg.py`, `OpusCsd`, `OggCrc`), `Sidecar` (`sidecar.py`, plus a minimal `Json`), `Spool` (`spool.py`), `UploadPolicy` and `Uploader` (`uploader.py`), `ClockOffset` (`GET /v1/time`, ADR 0006), `Retention` (the edge's `_retain_from_n`), `CaptureClock` (Android-specific: block capture time from `AudioRecord.getTimestamp`, loss from `framePosition` against frames read), and for P3 `UnsyncedHold` (the edge's unsynced hold and re-stamp, driven by the probe), `UploadLoop` (probe schedule, upload passes, backoff) and `Http` (`HttpURLConnection`, fixed-length PUT, `GET /v1/time`). |
| `:app` | The P3 client: one Compose screen (device id, server URL, token, Start/Stop, log) and a microphone foreground service running `AudioRecord` → `CaptureClock` → `Timeline` → `RawSegmenter` → `MediaCodec` Opus → `OggOpusWriter` → `Spool`, plus an upload thread (`UploadLoop`) that probes `/v1/time` and PUTs `pending/` to the server. The package is still `no.nyta.roomlog.spike`, so an install keeps the spike's settings and spool. `minSdk 29`, `targetSdk 35`. |

## Build

Toolchain: JDK 21 (the Android Gradle plugin rejects newer JDKs; do not use the system default
if it is newer), the Android SDK with `platforms;android-35` and `build-tools;35.0.0`. Gradle
comes from the wrapper (8.10.2); AGP 8.7.3, Kotlin 2.0.21.

```sh
cd android
echo "sdk.dir=$HOME/Android/Sdk" > local.properties          # gitignored
export JAVA_HOME=~/.local/share/mise/installs/java/21.0.2     # or any JDK 17 to 21
./gradlew :core:test            # the gate for :core
./gradlew :app:assembleDebug    # app/build/outputs/apk/debug/app-debug.apk
```

`:core:test` needs no device or network. `LiveIngestTest` runs only when pointed at a
throwaway ingest (see "Test the upload path without the phone"). The `OggOpusWriter` tests shell out to `ffmpeg`/`ffprobe` (they
re-mux packets from `encode.py`'s ffmpeg command line and check the decoded length, the
decoded samples and the tags) and to `python3` (the edge's `ogg.py` parses the output); each
of those tests is skipped when its tool is missing. Test reports:
`core/build/reports/tests/test/index.html`.

## Run on the device (P3)

```sh
adb install -r app/build/outputs/apk/debug/app-debug.apk    # versionCode 4, installs over rc1 (3)
adb shell am start -n no.nyta.roomlog.spike/.MainActivity
```

A debug build from another machine is signed with a different debug key; `adb install -r`
then fails with `INSTALL_FAILED_UPDATE_INCOMPATIBLE` and only an uninstall helps, which
deletes the spool. Pull `files/spool` first if it holds anything.

Setup fields (persisted in app-private preferences; editable only while idle):

| Field | Value |
|---|---|
| device id | `lass22` on the S22+. Goes into every sidecar and the `ROOMLOG_DEVICE_ID` tag; must be the device the token belongs to on the server (otherwise `403`) |
| server URL | `https://oma.tailf63b9a.ts.net:8480` (the default; port 8480, see ADR 0007's amendment) |
| token | the bearer token for that device from the server's `tokens.toml`. Paste it once; it is never shown or logged again (the label says `token: set`). Empty token = record only, nothing uploads |

Tap Start and grant the microphone and notifications. Recording runs in a foreground
service of type `microphone`, with an ongoing notification that has a Stop action. The
screen may turn off and the activity may be closed or recreated without ending the run.
Without the service Android hands a background app silence, with no error. If the system
kills the process, the service is not restarted into recording (Android does not allow a
background start to use the microphone); a "recording stopped" notification appears instead.

### What happens to a segment

1. Every 30 s a segment closes. If a `/v1/time` probe succeeded in the last 10 minutes it is
   stamped in the server's timebase with `clock_synced: true` and goes to `pending/`;
   otherwise it is stamped from the last known offset (or the device clock before the first
   probe) with `clock_synced: false` and held in `unsynced/` (ADR 0006).
2. When a probe succeeds, the held segments of the current run are re-stamped through the
   run's timeline (server offset, `clock_step: true` if the offset moved more than 50 ms
   since they were stamped) and moved to `pending/`.
3. The upload thread PUTs `pending/` oldest first. A segment is deleted only after a
   `200`/`201` whose body carries the same sha256. `409`/`413`/`422` move it to `failed/`.
   Anything else (no network, `401`, `403`, `5xx`) keeps it and backs off
   `min(300 s, 2^k)` with full jitter; the queue waits behind it. Uploads run on any network.
4. Probes run at start, every 5 minutes while they succeed, every 30 s while they fail.
5. Stop: the shutdown segment is cut; held segments that never saw a probe move to
   `pending/` as stamped; the upload thread makes one last pass; the service ends. What is
   still in `pending/` goes on the next Start. Nothing uploads while the app is stopped.

On Start, `unsynced/` segments left by earlier runs (the P2 spike, or a run killed by
force-stop) move to `pending/` as stamped, `clock_synced: false`: their run's timeline is
gone, so they cannot be re-stamped. Segments whose `device_id` is not the current device id
(for example spike runs recorded as `s22` before the id was set to `lass22`) stay in
`unsynced/` and are never uploaded, since the server would answer `403` forever and block
the queue. Pull them with `adb pull` if they are wanted, and PUT them with the matching token.

### What the log shows (P3 lines)

- `upload: to <url>` at Start, or `upload: no server URL or token set, recording only`.
- `spool: <n> earlier unsynced segments moved to pending as-is (clock_synced false), <m> of
  other device ids left in unsynced/, ...` once at Start, if there were any.
- `clock probe ok: rtt <ms>, first probe, device clock <ms> off server`, then every 5 min
  `offset moved <ms>`. `clock probe failed: <reason>` while the server is unreachable
  (`401`/`403` in the reason = token or device id wrong).
- `clock synced: re-stamping <n> held segments`, then `spool: <n> re-stamped to pending ...`.
- Per segment: `seg <i> cap epoch=... synced=true ...` then `uploaded <stem>: 201`
  (`200` = the server already had it).
- During an outage: `upload <stem>: no response ConnectException ...; retry <k> in <s> s`, and
  after 10 min without a probe `clock: no probe for 10 min, new segments held in unsynced/`.
  When the server is back: a probe, the re-stamp line, and a burst of `uploaded` lines in order.
- `upload <stem> REJECTED <status>: <body> (moved to failed/)` is a contract problem; report it.
- The 600-read capture summary ends in `clock_synced <bool>, held <n>`.

### Confirm on oma

```sh
~/services/apps/roomlog/server/.venv/bin/roomlog status
```

The `devices:` block has a `lass22` line with `raw=<n> raw_pending=<m> last_raw=<start_utc>`;
`raw` grows by about two per minute while the phone records and uploads, and `last_raw`
stays within a minute or so of now. The `raw` line above it shows `failed=0`. After
`raw_idle_s` (120 s) the worker segments the backlog into speech chunks; `roomlog search`
then finds what was said.

## Capture details

In the first lines of a run, look for the routing callback. Android usually reports the
initial route once; that must appear as `routing callback: <device> id=<n> unchanged,
ignored`. A `routing callback: ... (was id=...): new epoch` line means the input really
changed (or this device reports the first route differently); it opens an epoch and ends the
previous segment with `discontinuity`. A `WARNING: no getTimestamp(BOOTTIME)` line means the
device gives no timestamps: stamps then come from read arrival, and a stalled read thread
will look like loss.

An overrun logs `overrun: <n> frames lost (counted by framePosition)` or `(not counted by
framePosition)`, and every 600-read summary ends in `losses <n>, counted lost <frames>`. After
the first loss, `counted lost` above zero means this device's `framePosition` counts frames
dropped in an overrun; `counted lost 0` with `losses` above zero means it does not. Note which
in the P2 findings: the two behaviours have different blind spots (below).

Known limits of the capture clock (`CaptureClock`'s class doc has the detail):

- Loss upstream of the client buffer while the buffer is not full (a HAL glitch, not a stalled
  reader) is placed up to about 1.4 reads late when `framePosition` does not count dropped
  frames, and is not detected at all when it does.
- After the first successful timestamp, reads without one keep the previous mapping, so a loss
  during a timestamp outage shows only when timestamps return.
- The smallest loss that opens an epoch is just over 10 ms when `framePosition` counts dropped
  frames and just over 5 ms when it does not.

The spool is in the app's external files dir: `pending/` (waiting to upload, deleted on
ack), `unsynced/` (held for a probe, or another device id's), `failed/` (rejected). To look:

```sh
mkdir -p ~/scratch
adb pull /sdcard/Android/data/no.nyta.roomlog.spike/files/spool ~/scratch/spike-spool
ls ~/scratch/spike-spool/unsynced/     # <start_utc compact>_<sha8>.opus + .json
```

## Test the upload path without the phone

`LiveIngestTest` drives `Http`, `UploadLoop` and `UnsyncedHold` against a real ingest:
an outage (nothing listening), the first probe, re-stamping held segments, PUT, ack,
delete, and an idempotent re-PUT. Run a throwaway ingest on another loopback port with its
own data dir and token, never the production one:

```sh
L=$(mktemp -d); mkdir $L/data $L/segs
python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > $L/token
printf 's22 = "%s"\n' "$(cat $L/token)" > $L/tokens.toml     # the segments' device_id
printf '[paths]\ndata_dir = "%s/data"\ntokens_file = "%s/tokens.toml"\n[segmenter]\nvad = "energy"\n' $L $L > $L/server.toml
cp ~/scratch/spike-spool/unsynced/* $L/segs/                  # real raw segments of one run
(cd ../server && ROOMLOG_CONFIG=$L/server.toml ROOMLOG_BIND=127.0.0.1:18480 uv run roomlog ingest &)
ROOMLOG_LIVE_URL=http://127.0.0.1:18480 ROOMLOG_LIVE_TOKEN_FILE=$L/token ROOMLOG_LIVE_SEGMENTS=$L/segs \
  ./gradlew :core:test --tests '*LiveIngest*' --rerun-tasks -i
(cd ../server && ROOMLOG_CONFIG=$L/server.toml uv run roomlog status && ROOMLOG_CONFIG=$L/server.toml uv run roomlog segment)
```

## Validate with the server's decoder (P2 pass criteria)

From the repo root, with the server's environment (PyAV):

```sh
cd server && uv run python - ~/scratch/spike-spool/unsynced/*.opus <<'EOF'
import json, sys, hashlib, pathlib
from roomlog_server.audio import decode_opus
sys.path.insert(0, "../edge/src")
from roomlog_edge import ogg
for p in map(pathlib.Path, sys.argv[1:]):
    meta = json.loads(p.with_suffix(".json").read_text())
    data = p.read_bytes()
    info = ogg.parse(data)
    n = len(decode_opus(p))
    print(p.name, meta["n_start"], meta["n_samples"], "decoded", n,
          "OK" if n == meta["n_samples"] else "MISMATCH",
          "sha", hashlib.sha256(data).hexdigest() == meta["sha256"],
          "tags", info.tags.get("ROOMLOG_N_START") == str(meta["n_start"]))
EOF
```

PyAV's resampler to 16 kHz returns nothing for streams under about 50 samples (3 ms) even
though they decode exactly at 48 kHz; a tiny shutdown tail can show as a mismatch for that
reason alone. Each full segment must decode to exactly 480000 samples, and within one `(run_id, epoch)`
`n_start + n_samples` of a segment must equal the next `n_start`. Concatenating the decodes
and cross-correlating against a known source (play a chirp near the phone) checks that the
segment seams are sample-exact.

P2 checklist, in addition to the decode checks above:

- The first lines show the routing callback as `unchanged, ignored`, and no `WARNING: no
  getTimestamp(BOOTTIME)`.
- Which `framePosition` behaviour the device has, from the `counted lost` summary line after
  a loss (see "Run the spike").
- One pulled segment is accepted by the server (next section).

## Ingest one segment by hand (P2)

The app uploads by itself since P3; this is for checking a pulled `.opus`/`.json` pair, or
sending segments left in `unsynced/` under another device id with that device's token. This needs
a server that has the raw-segment support (`kind: "raw"`, merged separately from this client)
and a bearer token configured on the server for the device id the spike ran with, saved in a
file `token` (secrets stay out of the repo, e.g. under `~/.config/roomlog*/`).

```sh
cd ~/scratch/spike-spool/unsynced
f=20260926T101532417Z_9f2c1a3b        # one pair: $f.opus and $f.json
sha=$(sha256sum "$f.opus" | cut -d' ' -f1)
meta=$(python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1])), ensure_ascii=True, separators=(",",":")))' "$f.json")
curl -sS -w '\nHTTP %{http_code}\n' -X PUT "https://oma.tailf63b9a.ts.net:8480/v1/chunks/$sha" \
  -H "Authorization: Bearer $(cat token)" \
  -H 'Content-Type: audio/ogg' \
  -H "X-Roomlog-Meta: $meta" \
  --data-binary @"$f.opus"
```

Expected: `HTTP 201` with a body carrying the same sha,
`{"sha256":"<sha>","status":"created","path":"..."}`. Running it again gives `HTTP 200` with
`"status":"exists"`. On `422`, read the `error` in the body: the sidecar fails the schema or the
raw rules in `contract/CONTRACT.md` (`kind: "raw"`, `n_start` and `n_samples` present,
`n_samples == 480000` with `cut_reason: "cap"` except for the short last segment of an epoch or
run, which carries `"discontinuity"` or `"shutdown"`, no `vad`); fix `Sidecar`, not the pulled
file. `401` is a wrong token, `403` a sidecar `device_id` that is not the token's device, `409`
a sha mismatch between the URL, the body and `meta.sha256`.
