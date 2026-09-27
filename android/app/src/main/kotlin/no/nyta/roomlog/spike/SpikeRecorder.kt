package no.nyta.roomlog.spike

import android.content.Context
import android.media.AudioRecord
import android.os.Process
import android.os.SystemClock
import no.nyta.roomlog.core.CaptureClock
import no.nyta.roomlog.core.OggOpusWriter
import no.nyta.roomlog.core.OpusHead
import no.nyta.roomlog.core.RawSegmenter
import no.nyta.roomlog.core.Retention
import no.nyta.roomlog.core.SampleRing
import no.nyta.roomlog.core.Sidecar
import no.nyta.roomlog.core.Spool
import no.nyta.roomlog.core.Timeline
import java.io.File
import java.util.UUID
import java.util.concurrent.LinkedBlockingQueue
import kotlin.random.Random

/**
 * The P2 codec spike: AudioRecord → Timeline → RawSegmenter → MediaCodec
 * Opus → OggOpusWriter → Spool, no upload.
 *
 * Two threads. The capture thread (urgent-audio priority) does 100 ms
 * blocking reads, feeds the timeline and the segmenter, stamps closed
 * segments, and passes commands to the encode thread through a queue; it
 * never blocks on the codec. The encode thread owns the `MediaCodec`, the
 * writer and the spool.
 *
 * Timing: `mono` is `elapsedRealtimeNanos()` (BOOTTIME), `real` the device
 * wall clock, so `clock_synced` is false and segments go to `unsynced/`
 * (the spike has no `/v1/time` probe). Each block's capture time comes from
 * `AudioRecord.getTimestamp(TIMEBASE_BOOTTIME)`, taken after every read,
 * through [CaptureClock]: a stalled read thread is not loss, and loss is
 * detected from `framePosition` against frames read (AudioRecord has no
 * overflow flag). A dead AudioRecord or a routing change forces a new epoch.
 */
class SpikeRecorder(
    private val context: Context,
    private val spoolDir: File,
    /** Goes into every sidecar and the ROOMLOG_DEVICE_ID tag; must match the server token's device. */
    private val deviceId: String,
    private val log: (String) -> Unit,
    /** Called on the encode thread once the last segment is spooled (or capture failed). */
    private val onFinished: (SpikeRecorder) -> Unit = {},
) {
    val runId: String = UUID.randomUUID().toString()
    private val edgeVersion = "android-0.1.0-spike"
    private val blockFrames = 1600 // 100 ms

    @Volatile
    private var running = false
    private var captureThread: Thread? = null
    private var encodeThread: Thread? = null

    private sealed interface Cmd {
        data class Open(val epoch: Int, val nStart: Long, val discontinuity: Boolean) : Cmd
        data class Pcm(val n: Long, val samples: ShortArray) : Cmd
        data class Close(val seg: RawSegmenter.Segment, val utcNs: Long, val clockStep: Boolean) : Cmd
        data object Stop : Cmd
    }

    private val queue = LinkedBlockingQueue<Cmd>()

    /** In-flight and unsynced-held segments whose anchors the timeline must keep. */
    private val retention = Retention()

    fun start() {
        check(!running)
        running = true
        log("run_id=$runId device_id=$deviceId spool=${spoolDir.absolutePath}")
        encodeThread = Thread(::encodeLoop, "roomlog-encode").apply { start() }
        captureThread = Thread(::captureLoop, "roomlog-capture").apply { start() }
    }

    /** Stop capture; the capture thread cuts the shutdown segment and the encode thread finishes it. */
    fun stop() {
        running = false
    }

    fun join(timeoutMs: Long = 0) {
        captureThread?.join(timeoutMs)
        encodeThread?.join(timeoutMs)
    }

    // -- capture thread ------------------------------------------------------

    private fun captureLoop() {
        Process.setThreadPriority(Process.THREAD_PRIORITY_URGENT_AUDIO)
        val timeline = Timeline()
        val segmenter = RawSegmenter(latenessLimitNs = timeline.latenessLimitNs)
        val ring = SampleRing(3 * 16000)
        val source = AudioSource(context, log)
        val buf = ShortArray(blockFrames)
        var clock: CaptureClock? = null
        var forceNewEpoch = false
        var blocks = 0L
        var maxLatenessNs = 0L
        var maxAdcNs = 0L
        var tsFailures = 0
        try {
            source.open()
            clock = CaptureClock(source.bufferFrames)
            log("capture clock: buffer ${source.bufferFrames} frames")
            while (running) {
                val got = source.read(buf, clock!!.nextReadFrames(blockFrames))
                val mono = SystemClock.elapsedRealtimeNanos()
                val real = System.currentTimeMillis() * 1_000_000
                if (got == AudioRecord.ERROR_DEAD_OBJECT) {
                    log("AudioRecord dead object: reopening, next block starts a new epoch")
                    source.close()
                    source.open()
                    clock = CaptureClock(source.bufferFrames)
                    forceNewEpoch = true
                    continue
                }
                if (got < 0) {
                    log("AudioRecord.read error $got: stopping")
                    break
                }
                if (got == 0) continue
                val ts = source.timestamp()
                if (ts == null) tsFailures++
                val step = clock.onRead(got, mono, real, ts)
                step.event?.let { log("capture clock: $it") }
                if (blocks == 20L && !clock.usingTimestamps) {
                    log("WARNING: no getTimestamp(BOOTTIME) in 20 reads; stamping from arrival, stalls will look like loss")
                }
                if (source.routingChanged) {
                    source.routingChanged = false
                    forceNewEpoch = true
                }
                val block = if (forceNewEpoch) step.block.copy(inputOverflow = true) else step.block
                val res = timeline.feed(block)
                forceNewEpoch = false
                ring.write(res.nStart, buf, got)
                blocks++
                maxLatenessNs = maxOf(maxLatenessNs, res.latenessNs)
                maxAdcNs = maxOf(maxAdcNs, block.adcLatencyNs)
                if (res.newEpoch && res.epoch > 0) {
                    log("new epoch ${res.epoch} at n=${res.epochStartN} (lateness ${res.latenessNs / 1_000_000} ms)")
                }
                res.clockStepNs?.let { log("clock step ${it / 1_000_000} ms at n=${res.nStart}") }
                if (res.latenessNs > timeline.latenessLimitNs && !res.newEpoch) {
                    log("late block at n=${res.nStart}: ${res.latenessNs / 1_000_000} ms (held)")
                }
                dispatch(segmenter.feed(res), ring, timeline)
                timeline.retainFromN = retention.retainFromN(segmenter.retainFromN)
                if (blocks % 600 == 0L) {
                    log(
                        "capture: $blocks reads, last 600: max lateness ${maxLatenessNs / 1_000_000} ms, " +
                            "max arrival-capture ${maxAdcNs / 1_000_000} ms, timestamp failures $tsFailures, " +
                            "outliers ${clock.outliers}, losses ${clock.losses}, counted lost ${clock.countedLost}",
                    )
                    maxLatenessNs = 0
                    maxAdcNs = 0
                    tsFailures = 0
                }
            }
        } catch (e: Exception) {
            log("capture failed: $e")
        } finally {
            try {
                dispatch(segmenter.shutdown(), ring, timeline)
            } catch (e: Exception) {
                log("shutdown cut failed: $e")
            }
            source.close()
            queue.put(Cmd.Stop)
            running = false
            log("capture stopped after ${timeline.n / 16000.0} s")
        }
    }

    private fun dispatch(events: List<RawSegmenter.Event>, ring: SampleRing, timeline: Timeline) {
        for (e in events) {
            when (e) {
                is RawSegmenter.Event.Open -> queue.put(Cmd.Open(e.epoch, e.nStart, e.discontinuity))
                is RawSegmenter.Event.Samples -> queue.put(Cmd.Pcm(e.nStart, ring.read(e.nStart, e.nEnd)))
                is RawSegmenter.Event.Close -> {
                    val s = e.segment
                    retention.startInflight(s.nStart)
                    queue.put(Cmd.Close(s, timeline.utcNs(s.nStart, s.epoch), timeline.steppedIn(s.nStart, s.nEnd)))
                }
            }
        }
    }

    // -- encode thread -------------------------------------------------------

    private fun encodeLoop() {
        // the edge's 5% floor is 11 GB on a 225 GB phone; 0.2% (~450 MB) still leaves room
        val spool = Spool(spoolDir, minFreeFraction = 0.002)
        val cleaned = spool.cleanupTmp()
        if (cleaned > 0) log("spool: cleaned $cleaned leftover files")
        var encoder: OpusEncoder? = null
        var packets = mutableListOf<ByteArray>()
        var open: Cmd.Open? = null
        var index = 0
        try {
            encoder = OpusEncoder(log)
            while (true) {
                when (val c = queue.take()) {
                    is Cmd.Open -> {
                        open = c
                        packets = mutableListOf()
                    }
                    is Cmd.Pcm -> encoder.queuePcm(c.samples, c.n, packets)
                    is Cmd.Close -> {
                        index++
                        try {
                            finishSegment(index, c, open, encoder, packets, spool)
                        } finally {
                            retention.endInflight(c.seg.nStart)
                        }
                        open = null
                    }
                    Cmd.Stop -> break
                }
            }
        } catch (e: Exception) {
            log("encode failed: $e")
            // keep draining so the capture thread never blocks on a full queue
            while (queue.take() != Cmd.Stop) Unit
        } finally {
            encoder?.release()
            val st = spool.stats()
            log("spool: pending=${st.pendingFiles} unsynced=${st.unsyncedFiles} failed=${st.failedFiles} bytes=${st.totalBytes}")
            onFinished(this)
        }
    }

    private fun finishSegment(
        index: Int, c: Cmd.Close, open: Cmd.Open?, encoder: OpusEncoder, packets: MutableList<ByteArray>, spool: Spool,
    ) {
        val seg = c.seg
        check(open != null && open.nStart == seg.nStart) { "close of ${seg.nStart} without matching open" }
        val preSkipGuess = encoder.csd?.preSkip ?: DEFAULT_PRE_SKIP
        // enough silence for the lookahead (preSkip at 48 kHz) plus one 20 ms frame
        val pad = (preSkipGuess + 2) / 3 + 320
        val t0 = SystemClock.elapsedRealtime()
        encoder.endSegment(seg.nEnd, pad, packets)
        val csd = encoder.csd
        for (note in encoder.takeCsdNotes()) log("seg $index CSD: $note")
        val preSkip = csd?.preSkip ?: DEFAULT_PRE_SKIP.also { log("seg $index: no CSD seen, assuming preSkip $it") }
        val tags = OggOpusWriter.roomlogTags(deviceId, runId, seg.epoch, seg.nStart, edgeVersion)
        val writer = OggOpusWriter(OpusHead(preSkip = preSkip), tags, serial = Random.nextInt())
        packets.forEach(writer::writePacket)
        val sizes = packets.map { it.size }
        val bytes = try {
            writer.finish(seg.nSamples)
        } catch (e: IllegalStateException) {
            log("seg $index n_start=${seg.nStart}: NOT WRITTEN: ${e.message}")
            return
        }
        val meta = Sidecar.build(
            deviceId = deviceId, sha256 = "0".repeat(64), utcNs = c.utcNs, nStart = seg.nStart,
            nSamples = seg.nSamples, runId = runId, epoch = seg.epoch, discontinuity = seg.discontinuity,
            clockStep = c.clockStep, clockSynced = false, cutReason = seg.reason.wire, vad = null,
            edgeVersion = edgeVersion, kind = "raw",
        )
        if (!spool.diskOk()) {
            log("seg $index: spool full or disk low, segment dropped (nothing deleted)")
            return
        }
        val entry = spool.write(bytes, meta, dest = "unsynced")
        // re-stamped through this run's timeline when a /v1/time probe succeeds (P3)
        retention.hold(entry.stem, seg.nStart)
        val sha = Spool.sha256Hex(bytes)
        val problems = Sidecar.validate(entry.readMeta())
        log(
            "seg $index ${seg.reason.wire} epoch=${seg.epoch} n_start=${seg.nStart} n_samples=${seg.nSamples} " +
                "bytes=${bytes.size} sha8=${sha.take(8)} packets=${packets.size} " +
                "(min ${sizes.minOrNull()} max ${sizes.maxOrNull()} B) cover48=${writer.totalSamples48} " +
                "need48=${preSkip + 3 * seg.nSamples} preSkip=$preSkip pad=$pad disc=${seg.discontinuity} " +
                "start=${Sidecar.formatUtc(c.utcNs)} close ${SystemClock.elapsedRealtime() - t0} ms" +
                if (problems.isEmpty()) "" else " SIDECAR INVALID $problems",
        )
    }

    companion object {
        /** libopus's lookahead at 48 kHz (6.5 ms), used only if the codec reports no CSD. */
        const val DEFAULT_PRE_SKIP = 312
    }
}
