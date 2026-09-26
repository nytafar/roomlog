package no.nyta.roomlog.core

import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * Test-only Ogg Opus reader: a port of `edge/src/roomlog_edge/ogg.py`
 * (pages, packet reassembly, OpusHead, OpusTags, duration from the last
 * granule), plus the page facts ogg.py does not check: CRC, sequence numbers,
 * serial and BOS/EOS flags.
 */
object OggReader {
    class OggError(message: String) : IllegalArgumentException(message)

    data class Page(
        val flags: Int, val granule: Long, val serial: Int, val seq: Int,
        val crc: Int, val crcOk: Boolean, val table: IntArray, val body: ByteArray,
    )

    data class OpusInfo(
        val channels: Int,
        val preSkip: Int,
        val inputSampleRate: Int,
        val vendor: String,
        val tags: Map<String, String>,
        val durationS: Double,
        val lastGranule: Long,
        val pages: List<Page>,
    )

    fun pages(data: ByteArray): List<Page> {
        val out = mutableListOf<Page>()
        var pos = 0
        while (pos < data.size) {
            if (pos + 27 > data.size) throw OggError("truncated page header")
            if (String(data, pos, 4, Charsets.US_ASCII) != "OggS") throw OggError("no OggS capture pattern at $pos")
            val h = ByteBuffer.wrap(data, pos, 27).order(ByteOrder.LITTLE_ENDIAN)
            if (data[pos + 4].toInt() != 0) throw OggError("unsupported ogg version")
            val flags = data[pos + 5].toInt() and 0xff
            val granule = h.getLong(pos + 6)
            val serial = h.getInt(pos + 14)
            val seq = h.getInt(pos + 18)
            val crc = h.getInt(pos + 22)
            val nsegs = data[pos + 26].toInt() and 0xff
            if (pos + 27 + nsegs > data.size) throw OggError("truncated segment table")
            val table = IntArray(nsegs) { data[pos + 27 + it].toInt() and 0xff }
            val bodyStart = pos + 27 + nsegs
            val bodyLen = table.sum()
            if (bodyStart + bodyLen > data.size) throw OggError("truncated page body")
            val pageBytes = data.copyOfRange(pos, bodyStart + bodyLen)
            for (i in 22 until 26) pageBytes[i] = 0 // the CRC is computed with its own field zeroed
            val crcOk = OggCrc.of(pageBytes) == crc
            out += Page(flags, granule, serial, seq, crc, crcOk, table, data.copyOfRange(bodyStart, bodyStart + bodyLen))
            pos = bodyStart + bodyLen
        }
        return out
    }

    /** (packet, granule of the page it ended on). */
    fun packets(data: ByteArray): List<Pair<ByteArray, Long>> {
        val out = mutableListOf<Pair<ByteArray, Long>>()
        var buf = java.io.ByteArrayOutputStream()
        for (p in pages(data)) {
            var off = 0
            for (seg in p.table) {
                buf.write(p.body, off, seg)
                off += seg
                if (seg < 255) {
                    out += buf.toByteArray() to p.granule
                    buf = java.io.ByteArrayOutputStream()
                }
            }
        }
        return out
    }

    fun parse(data: ByteArray): OpusInfo {
        val pk = packets(data)
        if (pk.size < 2) throw OggError("fewer than two packets")
        val head = pk[0].first
        val tags = pk[1].first
        if (String(head, 0, 8, Charsets.US_ASCII) != "OpusHead") throw OggError("first packet is not OpusHead")
        val hb = ByteBuffer.wrap(head).order(ByteOrder.LITTLE_ENDIAN)
        val channels = head[9].toInt() and 0xff
        val preSkip = hb.getShort(10).toInt() and 0xffff
        val rate = hb.getInt(12)
        if (String(tags, 0, 8, Charsets.US_ASCII) != "OpusTags") throw OggError("second packet is not OpusTags")
        val tb = ByteBuffer.wrap(tags).order(ByteOrder.LITTLE_ENDIAN)
        var pos = 8
        val vlen = tb.getInt(pos)
        pos += 4
        val vendor = String(tags, pos, vlen, Charsets.UTF_8)
        pos += vlen
        val count = tb.getInt(pos)
        pos += 4
        val comments = LinkedHashMap<String, String>()
        repeat(count) {
            val clen = tb.getInt(pos)
            pos += 4
            val item = String(tags, pos, clen, Charsets.UTF_8)
            pos += clen
            comments[item.substringBefore('=').uppercase()] = item.substringAfter('=', "")
        }
        val pages = pages(data)
        val last = pages.lastOrNull { it.granule > 0 }?.granule ?: 0
        return OpusInfo(
            channels, preSkip, rate, vendor, comments,
            durationS = maxOf(0L, last - preSkip) / 48000.0, lastGranule = last, pages = pages,
        )
    }

    /** Audio packets only (after OpusHead and OpusTags). */
    fun audioPackets(data: ByteArray): List<ByteArray> = packets(data).drop(2).map { it.first }
}
