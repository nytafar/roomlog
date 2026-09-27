# roomlog Android thin client

Two Gradle modules. The plan and its decisions live in `~/hvelv/repos/roomlog/android/plan-thin-client.md`
and ADRs 0005 to 0007, not here.

| Module | What |
|---|---|
| `:core` | Pure Kotlin/JVM, no Android imports, JVM-tested. Each class mirrors an edge module so the pytest vectors port one to one: `Timeline` (`timeline.py`), `RawSegmenter` (fixed 30 s raw segments, ADR 0005), `OggOpusWriter` (inverse of `ogg.py`, `OpusCsd`, `OggCrc`), `Sidecar` (`sidecar.py`, plus a minimal `Json`), `Spool` (`spool.py`), `UploadPolicy` and `Uploader` (`uploader.py`), `ClockOffset` (`GET /v1/time`, ADR 0006), `Retention` (the edge's `_retain_from_n`), and `CaptureClock` (Android-specific: block capture time from `AudioRecord.getTimestamp`, loss from `framePosition` against frames read). |
| `:app` | The P2 codec spike: one Compose screen and a microphone foreground service running `AudioRecord` → `CaptureClock` → `Timeline` → `RawSegmenter` → `MediaCodec` Opus → `OggOpusWriter` → `Spool`. It does not upload. `Http` (the `HttpURLConnection` side of `Uploader` and `ClockOffset`) is there for P3 and not wired in. `minSdk 29`, `targetSdk 35`. |

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

`:core:test` needs no device. The `OggOpusWriter` tests shell out to `ffmpeg`/`ffprobe` (they
re-mux packets from `encode.py`'s ffmpeg command line and check the decoded length, the
decoded samples and the tags) and to `python3` (the edge's `ogg.py` parses the output); each
of those tests is skipped when its tool is missing. Test reports:
`core/build/reports/tests/test/index.html`.

## Run the spike

```sh
adb install -r app/build/outputs/apk/debug/app-debug.apk
adb shell am start -n no.nyta.roomlog.spike/.MainActivity
```

Set the device id (default `s22`, persisted; it goes into every sidecar and the
`ROOMLOG_DEVICE_ID` tag, so it must match the server token's device), tap Start and grant the
microphone and notifications.

Recording runs in a foreground service of type `microphone`, with an ongoing notification
that has a Stop action. The screen may turn off and the activity may be closed or recreated
without ending the run; this is how the overnight screen-off run of plan §4 P2 is done.
Without the service Android hands a background app silence, with no error. If the system
kills the process, the service is not restarted into recording (Android does not allow a
background start to use the microphone); a "recording stopped" notification appears instead.

The log shows the `AudioRecord` source, buffer and routing, the codec name, each
codec-specific-data sighting (shape, pre-skip, delay), capture-clock events (first
timestamp, re-bases, overruns and losses with the frame they start at), new epochs, a
summary every 600 reads (maximum arrival-to-capture time, timestamp failures and outliers),
and one line per segment: `n_start`, `n_samples`, bytes, `sha8`, packet count and sizes,
samples covered versus needed. The same lines go to logcat: `adb logcat -s roomlog`. Tap Stop
(screen or notification) to cut the shutdown segment.

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

Segments land in the app's external files dir, in `unsynced/` because the spike has no clock
probe (`clock_synced: false`):

```sh
mkdir -p ~/scratch
adb pull /sdcard/Android/data/no.nyta.roomlog.spike/files/spool ~/scratch/spike-spool
ls ~/scratch/spike-spool/unsynced/     # <start_utc compact>_<sha8>.opus + .json
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

The spike does not upload, so P2 checks ingest with one pulled `.opus`/`.json` pair. This needs
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
