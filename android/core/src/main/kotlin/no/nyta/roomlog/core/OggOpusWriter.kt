package no.nyta.roomlog.core

import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * Ogg Opus muxer (RFC 7845), the inverse of `edge/src/roomlog_edge/ogg.py`.
 *
 * Page 0 (BOS) carries `OpusHead`, page 1 `OpusTags` with the `ROOMLOG_*`
 * tags, then audio pages of up to about one second of packets. A page's
 * granule position is the total 48 kHz sample count of all packets completed
 * on it and before it; the first packets carry the encoder's pre-skip, so
 * the decoded length of the stream is `final granule − preSkip`. [finish]
 * flags the last page EOS and trims its granule to `preSkip + 3·nSamples`,
 * so the stream decodes to exactly `nSamples` at 16 kHz.
 *
 * One writer per segment; the whole segment (about 90 KB for 30 s) is built
 * in memory because its bytes are hashed for identity anyway.
 */
class OggOpusWriter(
    private val head: OpusHead,
    tags: Map<String, String>,
    private val serial: Int,
    vendor: String = VENDOR,
    /** Flush an audio page once it holds this much audio (48 kHz samples). */
    private val pageSamples: Int = 48_000,
) {
    private val tagsPacket = opusTags(vendor, tags)
    private val packetList = mutableListOf<ByteArray>()
    private val durations = mutableListOf<Int>()
    private val out = ByteArrayOutputStream(96 * 1024)
    private var seq = 0
    private var finished = false

    /** Packets written so far. */
    val packets: Int get() = packetList.size

    /** Total duration of the packets written so far, 48 kHz samples (pre-skip included). */
    var totalSamples48 = 0L
        private set

    /** Append one Opus packet; its duration is read from the TOC byte. */
    fun writePacket(packet: ByteArray) {
        check(!finished) { "writer already finished" }
        val samples = OpusPacket.samples48k(packet)
        require(packet.size / 255 + 1 <= 255) { "packet of ${packet.size} bytes does not fit one page" }
        packetList += packet
        durations += samples
        totalSamples48 += samples
    }

    /**
     * Lay out the pages and close the stream so it decodes to exactly
     * [nSamples16k] samples at 16 kHz. Trailing packets that start at or after
     * the end (encoder padding) are dropped, so the trim always falls inside
     * the last page (RFC 7845 §4.4). Throws if the packets do not cover
     * `preSkip + 3·nSamples16k` (the encoder did not flush its lookahead).
     */
    fun finish(nSamples16k: Long): ByteArray {
        check(!finished) { "writer already finished" }
        require(nSamples16k > 0) { "empty segment" }
        val finalGranule = head.preSkip + 3 * nSamples16k
        check(finalGranule <= totalSamples48) {
            "packets cover $totalSamples48 samples at 48 kHz, need $finalGranule " +
                "(preSkip ${head.preSkip} + 3*$nSamples16k)"
        }
        var keep = packetList.size
        var covered = totalSamples48
        while (covered - durations[keep - 1] >= finalGranule) covered -= durations[--keep]

        writePage(listOf(head.toBytes()), granule = 0, flags = FLAG_BOS)
        writePage(listOf(tagsPacket), granule = 0, flags = 0)
        var granule = 0L
        var page = mutableListOf<ByteArray>()
        var pageDur = 0
        var pageLacing = 0
        for (i in 0 until keep) {
            val p = packetList[i]
            val lacing = p.size / 255 + 1
            if (page.isNotEmpty() && (pageDur + durations[i] > pageSamples || pageLacing + lacing > 255)) {
                writePage(page, granule, flags = 0)
                page = mutableListOf()
                pageDur = 0
                pageLacing = 0
            }
            page += p
            pageDur += durations[i]
            pageLacing += lacing
            granule += durations[i]
        }
        writePage(page, finalGranule, flags = FLAG_EOS)
        finished = true
        return out.toByteArray()
    }

    private fun writePage(packets: List<ByteArray>, granule: Long, flags: Int) {
        val lacing = ByteArrayOutputStream()
        for (p in packets) {
            repeat(p.size / 255) { lacing.write(255) }
            lacing.write(p.size % 255)
        }
        val table = lacing.toByteArray()
        require(table.size <= 255)
        val bodyLen = packets.sumOf { it.size }
        val page = ByteBuffer.allocate(27 + table.size + bodyLen).order(ByteOrder.LITTLE_ENDIAN)
        page.put("OggS".toByteArray(Charsets.US_ASCII))
        page.put(0) // version
        page.put(flags.toByte())
        page.putLong(granule)
        page.putInt(serial)
        page.putInt(seq++)
        page.putInt(0) // CRC, filled below
        page.put(table.size.toByte())
        page.put(table)
        for (p in packets) page.put(p)
        val bytes = page.array()
        val crc = OggCrc.of(bytes)
        ByteBuffer.wrap(bytes, 22, 4).order(ByteOrder.LITTLE_ENDIAN).putInt(crc)
        out.write(bytes)
    }

    companion object {
        const val VENDOR = "roomlog-android OggOpusWriter"
        const val FLAG_CONTINUED = 0x01
        const val FLAG_BOS = 0x02
        const val FLAG_EOS = 0x04

        fun opusTags(vendor: String, tags: Map<String, String>): ByteArray {
            val v = vendor.toByteArray(Charsets.UTF_8)
            val items = tags.map { (k, x) -> "$k=$x".toByteArray(Charsets.UTF_8) }
            val b = ByteBuffer.allocate(8 + 4 + v.size + 4 + items.sumOf { 4 + it.size })
                .order(ByteOrder.LITTLE_ENDIAN)
            b.put("OpusTags".toByteArray(Charsets.US_ASCII))
            b.putInt(v.size).put(v)
            b.putInt(items.size)
            for (i in items) b.putInt(i.size).put(i)
            return b.array()
        }

        /** The five immutable tags of the contract, as `encode.roomlog_tags`. */
        fun roomlogTags(deviceId: String, runId: String, epoch: Int, nStart: Long, edgeVersion: String) =
            linkedMapOf(
                "ROOMLOG_DEVICE_ID" to deviceId,
                "ROOMLOG_RUN_ID" to runId,
                "ROOMLOG_EPOCH" to epoch.toString(),
                "ROOMLOG_N_START" to nStart.toString(),
                "ROOMLOG_EDGE_VERSION" to edgeVersion,
            )
    }
}

/** Ogg's CRC-32: polynomial 0x04C11DB7, MSB first, initial 0, no final xor (not zlib's). */
object OggCrc {
    private val table = IntArray(256) { i ->
        var r = i shl 24
        repeat(8) { r = if (r and 0x80000000.toInt() != 0) (r shl 1) xor 0x04C11DB7 else r shl 1 }
        r
    }

    fun of(data: ByteArray, off: Int = 0, len: Int = data.size - off): Int {
        var crc = 0
        for (i in off until off + len) {
            crc = (crc shl 8) xor table[((crc ushr 24) xor (data[i].toInt() and 0xff)) and 0xff]
        }
        return crc
    }
}

/**
 * The `OpusHead` identification header (RFC 7845 §5.1), mono, mapping family 0.
 * [inputSampleRate] is informational; roomlog writes 16000 so readers report it.
 */
data class OpusHead(
    val preSkip: Int,
    val inputSampleRate: Int = 16000,
    val channels: Int = 1,
    val outputGain: Int = 0,
) {
    fun toBytes(): ByteArray = ByteBuffer.allocate(19).order(ByteOrder.LITTLE_ENDIAN).apply {
        put("OpusHead".toByteArray(Charsets.US_ASCII))
        put(1) // version
        put(channels.toByte())
        putShort(preSkip.toShort())
        putInt(inputSampleRate)
        putShort(outputGain.toShort())
        put(0) // channel mapping family
    }.array()

    companion object {
        fun parse(b: ByteArray): OpusHead {
            require(b.size >= 19 && String(b, 0, 8, Charsets.US_ASCII) == "OpusHead") { "not an OpusHead" }
            val bb = ByteBuffer.wrap(b).order(ByteOrder.LITTLE_ENDIAN)
            return OpusHead(
                preSkip = bb.getShort(10).toInt() and 0xffff,
                inputSampleRate = bb.getInt(12),
                channels = b[9].toInt() and 0xff,
                outputGain = bb.getShort(16).toInt(),
            )
        }
    }
}

/**
 * What `MediaCodec`'s Opus encoder hands out as codec-specific data. Two
 * shapes exist in AOSP: separate `csd-0` (OpusHead), `csd-1` (codec delay,
 * ns, uint64 LE) and `csd-2` (seek pre-roll, ns) in the output format, or one
 * codec-config buffer with `AOPUSHDR`/`AOPUSDLY`/`AOPUSPRL` markers, each
 * followed by a uint64 LE length (see `OpusHeader.cpp` in frameworks/av).
 * [parse] accepts either; the spike logs which one the device produced.
 */
data class OpusCsd(
    val head: OpusHead?,
    val codecDelayNs: Long?,
    val seekPreRollNs: Long?,
    val shape: String,
) {
    /** Pre-skip in 48 kHz samples: from OpusHead if present, else from the delay. */
    val preSkip: Int?
        get() = head?.preSkip ?: codecDelayNs?.let { ((it * 48_000 + 500_000_000) / 1_000_000_000).toInt() }

    companion object {
        fun parse(csd0: ByteArray, csd1: ByteArray? = null, csd2: ByteArray? = null): OpusCsd {
            if (csd0.size >= 8 && String(csd0, 0, 8, Charsets.US_ASCII) == "AOPUSHDR") return parseMarked(csd0)
            return OpusCsd(
                head = OpusHead.parse(csd0),
                codecDelayNs = csd1?.let { u64(it, 0) },
                seekPreRollNs = csd2?.let { u64(it, 0) },
                shape = "csd-0/1/2",
            )
        }

        private fun u64(b: ByteArray, off: Int): Long {
            require(b.size >= off + 8) { "short uint64" }
            return ByteBuffer.wrap(b, off, 8).order(ByteOrder.LITTLE_ENDIAN).long
        }

        private fun parseMarked(b: ByteArray): OpusCsd {
            var head: OpusHead? = null
            var delay: Long? = null
            var preroll: Long? = null
            var pos = 0
            while (pos + 16 <= b.size) {
                val marker = String(b, pos, 8, Charsets.US_ASCII)
                val len = u64(b, pos + 8).toInt()
                val body = pos + 16
                require(len >= 0 && body + len <= b.size) { "bad $marker length $len" }
                val chunk = b.copyOfRange(body, body + len)
                when (marker) {
                    "AOPUSHDR" -> head = OpusHead.parse(chunk)
                    "AOPUSDLY" -> delay = u64(chunk, 0)
                    "AOPUSPRL" -> preroll = u64(chunk, 0)
                }
                pos = body + len
            }
            return OpusCsd(head, delay, preroll, shape = "AOPUSHDR")
        }

        /** The marked form, as Android's `WriteOpusHeaders` builds it (for tests). */
        fun marked(head: OpusHead, delayNs: Long, preRollNs: Long): ByteArray {
            val h = head.toBytes()
            val b = ByteBuffer.allocate(16 + h.size + 2 * (16 + 8)).order(ByteOrder.LITTLE_ENDIAN)
            b.put("AOPUSHDR".toByteArray(Charsets.US_ASCII)).putLong(h.size.toLong()).put(h)
            b.put("AOPUSDLY".toByteArray(Charsets.US_ASCII)).putLong(8).putLong(delayNs)
            b.put("AOPUSPRL".toByteArray(Charsets.US_ASCII)).putLong(8).putLong(preRollNs)
            return b.array()
        }
    }
}

/** Opus packet duration from the TOC byte (RFC 6716 §3.1). */
object OpusPacket {
    /** Frame size in 48 kHz samples for each of the 32 TOC configurations. */
    private fun frameSamples(config: Int): Int = when {
        config < 12 -> intArrayOf(480, 960, 1920, 2880)[config and 3] // SILK 10/20/40/60 ms
        config < 16 -> intArrayOf(480, 960)[config and 1] // Hybrid 10/20 ms
        else -> intArrayOf(120, 240, 480, 960)[config and 3] // CELT 2.5/5/10/20 ms
    }

    fun samples48k(packet: ByteArray): Int {
        require(packet.isNotEmpty()) { "empty Opus packet" }
        val toc = packet[0].toInt() and 0xff
        val frames = when (toc and 3) {
            0 -> 1
            1, 2 -> 2
            else -> {
                require(packet.size >= 2) { "code-3 packet without frame count" }
                packet[1].toInt() and 0x3f
            }
        }
        return frames * frameSamples(toc ushr 3)
    }
}
