package no.nyta.roomlog.spike

import android.media.AudioFormat
import android.media.MediaCodec
import android.media.MediaFormat
import no.nyta.roomlog.core.OpusCsd
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * Platform Opus encoder (`MediaCodec` `audio/opus`, 16 kHz mono, 24 kbps),
 * synchronous mode, driven from one encode thread.
 *
 * Per segment: [queuePcm] as samples arrive, then [endSegment] queues
 * `padSamples` of silence (so the encoder's lookahead is flushed and the
 * packets cover pre-skip + the real samples), EOS, drains to the EOS
 * output, and `flush()`es the codec for the next segment. The caller trims
 * the padding with the Ogg end granule.
 *
 * Codec-specific data arrives either as a `BUFFER_FLAG_CODEC_CONFIG` output
 * buffer or as `csd-*` keys on the output format; both are recorded in
 * [csd] and described in [csdNotes] for the spike's log.
 */
class OpusEncoder(private val log: (String) -> Unit) {
    val codec: MediaCodec = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_AUDIO_OPUS)
    private val info = MediaCodec.BufferInfo()

    var csd: OpusCsd? = null
        private set

    /** Where each CSD sighting came from, cleared by [takeCsdNotes]. */
    private val csdNotes = mutableListOf<String>()

    init {
        val format = MediaFormat.createAudioFormat(MediaFormat.MIMETYPE_AUDIO_OPUS, 16000, 1).apply {
            setInteger(MediaFormat.KEY_BIT_RATE, 24_000)
            setInteger(MediaFormat.KEY_PCM_ENCODING, AudioFormat.ENCODING_PCM_16BIT)
            setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, 16000 * 2) // up to 1 s per input buffer
        }
        codec.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
        codec.start()
        log("MediaCodec: ${codec.name} (${codec.codecInfo.canonicalName}) input=${codec.inputFormat}")
    }

    fun takeCsdNotes(): List<String> = csdNotes.toList().also { csdNotes.clear() }

    /** Queue [samples] whose first sample is absolute index [n]; packets produced meanwhile go to [out]. */
    fun queuePcm(samples: ShortArray, n: Long, out: MutableList<ByteArray>) {
        var off = 0
        while (off < samples.size) {
            val idx = codec.dequeueInputBuffer(10_000)
            if (idx < 0) {
                drain(out, 0)
                continue
            }
            val buf = codec.getInputBuffer(idx)!!
            buf.clear()
            val count = minOf(samples.size - off, buf.remaining() / 2)
            buf.order(ByteOrder.LITTLE_ENDIAN).asShortBuffer().put(samples, off, count)
            codec.queueInputBuffer(idx, 0, count * 2, ptsUs(n + off), 0)
            off += count
            drain(out, 0)
        }
    }

    /**
     * Close the segment: [padSamples] zeros, EOS, drain to EOS, flush.
     * [nEnd] is the absolute index after the segment's last real sample.
     */
    fun endSegment(nEnd: Long, padSamples: Int, out: MutableList<ByteArray>) {
        if (padSamples > 0) queuePcm(ShortArray(padSamples), nEnd, out)
        while (true) {
            val idx = codec.dequeueInputBuffer(10_000)
            if (idx >= 0) {
                codec.queueInputBuffer(idx, 0, 0, ptsUs(nEnd + padSamples), MediaCodec.BUFFER_FLAG_END_OF_STREAM)
                break
            }
            drain(out, 0)
        }
        val deadline = System.nanoTime() + 5_000_000_000L
        while (!drain(out, 10_000)) {
            check(System.nanoTime() < deadline) { "encoder produced no EOS within 5 s" }
        }
        codec.flush()
    }

    /** Drain available output into [out]; true once the EOS buffer was seen. */
    private fun drain(out: MutableList<ByteArray>, timeoutUs: Long): Boolean {
        while (true) {
            val idx = codec.dequeueOutputBuffer(info, timeoutUs)
            when {
                idx == MediaCodec.INFO_TRY_AGAIN_LATER -> return false
                idx == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED -> csdFromFormat(codec.outputFormat)
                idx >= 0 -> {
                    val buf = codec.getOutputBuffer(idx)!!
                    val bytes = ByteArray(info.size)
                    buf.position(info.offset)
                    buf.get(bytes)
                    val flags = info.flags
                    codec.releaseOutputBuffer(idx, false)
                    if (flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG != 0) {
                        csdFromConfig(bytes)
                    } else if (bytes.isNotEmpty()) {
                        out += bytes
                    }
                    if (flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM != 0) return true
                }
            }
        }
    }

    private fun csdFromConfig(b: ByteArray) {
        try {
            csd = OpusCsd.parse(b)
            csdNotes += "config buffer ${b.size} B shape=${csd!!.shape} ${describe(csd!!)} hex=${hex(b, 64)}"
        } catch (e: IllegalArgumentException) {
            csdNotes += "config buffer ${b.size} B unparsed (${e.message}) hex=${hex(b, 64)}"
        }
    }

    private fun csdFromFormat(f: MediaFormat) {
        val c0 = f.getByteBuffer("csd-0")?.let(::bytesOf)
        val c1 = f.getByteBuffer("csd-1")?.let(::bytesOf)
        val c2 = f.getByteBuffer("csd-2")?.let(::bytesOf)
        if (c0 == null) {
            csdNotes += "output format without csd-0: $f"
            return
        }
        try {
            val parsed = OpusCsd.parse(c0, c1, c2)
            if (csd == null || csd!!.shape != "AOPUSHDR") csd = parsed
            csdNotes += "output format csd-0=${c0.size} B csd-1=${c1?.size} csd-2=${c2?.size} ${describe(parsed)}"
        } catch (e: IllegalArgumentException) {
            csdNotes += "output format csd-0 unparsed (${e.message}) hex=${hex(c0, 64)}"
        }
    }

    fun release() {
        try {
            codec.stop()
        } catch (_: IllegalStateException) {
        }
        codec.release()
    }

    companion object {
        fun ptsUs(n: Long) = n * 1_000_000 / 16000

        fun describe(c: OpusCsd): String {
            val fromDelay = c.codecDelayNs?.let { (it * 48_000 + 500_000_000) / 1_000_000_000 }
            return "preSkip=${c.head?.preSkip} (from delay: $fromDelay) inRate=${c.head?.inputSampleRate} " +
                "ch=${c.head?.channels} delayNs=${c.codecDelayNs} prerollNs=${c.seekPreRollNs}"
        }

        private fun bytesOf(b: ByteBuffer): ByteArray {
            val d = b.duplicate()
            d.rewind()
            return ByteArray(d.remaining()).also { d.get(it) }
        }

        fun hex(b: ByteArray, max: Int) = b.take(max).joinToString("") { "%02x".format(it) } +
            if (b.size > max) "…" else ""
    }
}
