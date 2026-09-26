package no.nyta.roomlog.core

import org.junit.jupiter.api.Assumptions.assumeTrue
import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.file.Files
import java.util.concurrent.TimeUnit
import kotlin.math.PI
import kotlin.math.sin
import kotlin.test.Test
import kotlin.test.assertContentEquals
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertTrue

class OggOpusWriterTest {
    private val run = "5e6f7a8b-9c0d-4e1f-8a2b-3c4d5e6f7a8b"
    private val tags = OggOpusWriter.roomlogTags("s22", run, 3, 1_440_000, "android-0.1.0")

    // -- pure checks -------------------------------------------------------

    @Test
    fun oggCrcMatchesTheReferenceCheckValue() {
        // CRC-32, poly 0x04C11DB7, init 0, no reflection, no xorout: check("123456789") = 0x89A1897F
        assertEquals(0x89A1897F.toInt(), OggCrc.of("123456789".toByteArray()))
        assertEquals(0, OggCrc.of(ByteArray(0)))
    }

    @Test
    fun tocDurations() {
        fun toc(config: Int, code: Int) = (config shl 3) or code
        assertEquals(960, OpusPacket.samples48k(byteArrayOf(toc(1, 0).toByte()))) // SILK NB 20 ms
        assertEquals(960, OpusPacket.samples48k(byteArrayOf(toc(9, 0).toByte()))) // SILK WB 20 ms
        assertEquals(480, OpusPacket.samples48k(byteArrayOf(toc(12, 0).toByte()))) // Hybrid 10 ms
        assertEquals(960, OpusPacket.samples48k(byteArrayOf(toc(31, 0).toByte()))) // CELT 20 ms
        assertEquals(1920, OpusPacket.samples48k(byteArrayOf(toc(9, 1).toByte(), 0))) // two 20 ms frames
        assertEquals(2880, OpusPacket.samples48k(byteArrayOf(toc(3, 0).toByte()))) // SILK 60 ms
        assertEquals(3 * 960, OpusPacket.samples48k(byteArrayOf(toc(9, 3).toByte(), 3)))
    }

    @Test
    fun csdParsesBothAndroidShapes() {
        val head = OpusHead(preSkip = 312, inputSampleRate = 16000)
        val delayNs = 6_500_000L
        val prerollNs = 80_000_000L
        val sep = OpusCsd.parse(head.toBytes(), le64(delayNs), le64(prerollNs))
        assertEquals("csd-0/1/2", sep.shape)
        assertEquals(312, sep.preSkip)
        assertEquals(delayNs, sep.codecDelayNs)
        val marked = OpusCsd.parse(OpusCsd.marked(head, delayNs, prerollNs))
        assertEquals("AOPUSHDR", marked.shape)
        assertEquals(head, marked.head)
        assertEquals(prerollNs, marked.seekPreRollNs)
        // pre-skip from the delay alone: 6.5 ms at 48 kHz = 312
        assertEquals(312, OpusCsd(null, delayNs, null, "x").preSkip)
        assertEquals(head, OpusHead.parse(head.toBytes()))
    }

    private fun le64(v: Long) = ByteBuffer.allocate(8).order(ByteOrder.LITTLE_ENDIAN).putLong(v).array()

    /** Synthetic SILK-WB 20 ms packets with a payload byte tagging their index. */
    private fun fakePacket(i: Int, size: Int = 60) = ByteArray(size) { if (it == 0) (9 shl 3).toByte() else (i + it).toByte() }

    @Test
    fun pagesGranulesAndTrim() {
        val w = OggOpusWriter(OpusHead(preSkip = 312), tags, serial = 0x1234abcd)
        val n = 480_000L // 30 s at 16 kHz
        val needed = 312 + 3 * n
        val packets = ((needed + 959) / 960 + 3).toInt() // three packets of encoder padding beyond the end
        repeat(packets) { w.writePacket(fakePacket(it)) }
        val bytes = w.finish(n)
        val info = OggReader.parse(bytes)
        val pages = info.pages
        assertTrue(pages.all { it.crcOk }, "CRC")
        assertEquals((0 until pages.size).toList(), pages.map { it.seq })
        assertTrue(pages.all { it.serial == 0x1234abcd })
        assertEquals(OggOpusWriter.FLAG_BOS, pages[0].flags)
        assertTrue(pages.drop(1).dropLast(1).all { it.flags == 0 })
        assertEquals(OggOpusWriter.FLAG_EOS, pages.last().flags)
        assertEquals(listOf(0L, 0L), pages.take(2).map { it.granule })
        // audio pages: at most one second, granule = cumulative packet samples, last trimmed
        val audio = pages.drop(2)
        assertTrue(audio.all { it.table.size <= 50 })
        var cum = 0L
        for (p in audio.dropLast(1)) {
            cum += p.table.size * 960L
            assertEquals(cum, p.granule)
        }
        assertEquals(needed, pages.last().granule)
        assertTrue(pages.last().granule > audio[audio.size - 2].granule)
        // padding packets past the end were dropped: the last kept packet starts before the end
        val kept = OggReader.audioPackets(bytes).size
        assertEquals(((needed + 959) / 960).toInt(), kept)
        assertEquals(30.0, info.durationS)
        assertEquals(312, info.preSkip)
        assertEquals(16000, info.inputSampleRate)
        assertEquals(1, info.channels)
        assertEquals(tags, info.tags)
        assertEquals(OggOpusWriter.VENDOR, info.vendor)
    }

    @Test
    fun finishRefusesWhenPacketsDoNotCoverTheSegment() {
        val w = OggOpusWriter(OpusHead(preSkip = 312), tags, serial = 1)
        repeat(50) { w.writePacket(fakePacket(it)) } // 48000 samples at 48 kHz
        // 16000 samples need 312 + 48000: the encoder did not flush its lookahead
        val e = assertFailsWith<IllegalStateException> { w.finish(16_000) }
        assertTrue("need 48312" in e.message!!, e.message)
        val ok = OggOpusWriter(OpusHead(preSkip = 312), tags, serial = 1)
        repeat(50) { ok.writePacket(fakePacket(it)) }
        assertEquals(15_896.0 / 16000, OggReader.parse(ok.finish(15_896)).durationS)
    }

    // -- against a real encoder and decoder --------------------------------

    private fun have(cmd: String) = try {
        ProcessBuilder(cmd, if (cmd == "python3") "--version" else "-version").redirectErrorStream(true).start().let {
            it.inputStream.readAllBytes()
            it.waitFor(10, TimeUnit.SECONDS) && it.exitValue() == 0
        }
    } catch (e: java.io.IOException) {
        false
    }

    private fun exec(cmd: List<String>, stdin: ByteArray? = null): ByteArray {
        val p = ProcessBuilder(cmd).start()
        val err = StringBuilder()
        val errThread = Thread { err.append(p.errorStream.readAllBytes().decodeToString()) }.apply { start() }
        val writer = Thread {
            p.outputStream.use { if (stdin != null) it.write(stdin) }
        }.apply { start() }
        val out = p.inputStream.readAllBytes()
        writer.join()
        errThread.join()
        check(p.waitFor(60, TimeUnit.SECONDS)) { "timeout: $cmd" }
        check(p.exitValue() == 0) { "${cmd.first()} exited ${p.exitValue()}: $err" }
        return out
    }

    /** A 2.5 s chirp, 16 kHz mono int16 LE. */
    private fun chirpPcm(n: Int): ByteArray {
        val b = ByteBuffer.allocate(2 * n).order(ByteOrder.LITTLE_ENDIAN)
        for (i in 0 until n) {
            val t = i / 16000.0
            b.putShort((8000 * sin(2 * PI * (200 * t + 600 * t * t))).toInt().toShort())
        }
        return b.array()
    }

    /** encode.py's ffmpeg command line, verbatim by default; the knobs make packet-size edge cases. */
    private fun ffmpegEncode(
        pcm: ByteArray, dir: File, bitrate: String = "24k", application: String = "voip",
        vbr: String = "on", frameDuration: String = "20", name: String = "ffmpeg.opus",
    ): ByteArray = exec(
        listOf(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "s16le", "-ar", "16000", "-ac", "1", "-i", "pipe:0",
            "-c:a", "libopus", "-b:a", bitrate, "-application", application,
            "-vbr", vbr, "-frame_duration", frameDuration, "-f", "ogg", "pipe:1",
        ),
        pcm,
    ).also { File(dir, name).writeBytes(it) }

    /** Lacing values of every audio page (pages after OpusHead and OpusTags). */
    private fun audioLacing(bytes: ByteArray) = OggReader.pages(bytes).drop(2).map { it.table.toList() }

    // -- lacing edge cases -------------------------------------------------

    /** A SILK-WB 20 ms packet of exactly [size] bytes. */
    private fun sized(size: Int, tag: Int = 0) = ByteArray(size) { if (it == 0) (9 shl 3).toByte() else (tag + it).toByte() }

    @Test
    fun packetsOf255And256BytesLaceAndReassemble() {
        val w = OggOpusWriter(OpusHead(preSkip = 312), tags, serial = 9)
        val sizes = listOf(255, 256, 254, 510, 511, 1, 765)
        sizes.forEachIndexed { i, s -> w.writePacket(sized(s, i)) }
        val bytes = w.finish((sizes.size * 960L - 312) / 3)
        val page = audioLacing(bytes).single()
        // 255 → 255,0 (a zero terminates it); 256 → 255,1; 510 → 255,255,0; 765 → 255,255,255,0
        assertEquals(
            listOf(255, 0, 255, 1, 254, 255, 255, 0, 255, 255, 1, 1, 255, 255, 255, 0),
            page,
        )
        val got = OggReader.audioPackets(bytes)
        assertEquals(sizes, got.map { it.size })
        sizes.forEachIndexed { i, s -> assertContentEquals(sized(s, i), got[i]) }
        assertTrue(OggReader.pages(bytes).all { it.crcOk })
    }

    @Test
    fun lacingLimitSplitsPagesBeforeOneSecond() {
        // 2.5 ms CELT packets (TOC config 16): 400 per second, but a page holds 255 lacing values
        val w = OggOpusWriter(OpusHead(preSkip = 120), tags, serial = 10)
        val n = 1000
        repeat(n) { w.writePacket(byteArrayOf((16 shl 3).toByte(), it.toByte(), 7)) }
        val bytes = w.finish((n * 120L - 120) / 3)
        val pages = OggReader.pages(bytes).drop(2)
        assertEquals(listOf(255, 255, 255, 235), pages.map { it.table.size })
        assertEquals(listOf(255L * 120, 510L * 120, 765L * 120, n * 120L), pages.map { it.granule })
        assertEquals(n, OggReader.audioPackets(bytes).size)
        // and with 20 ms packets of 1275 bytes (6 lacing values each) the split comes at 42 packets
        val big = OggOpusWriter(OpusHead(preSkip = 312), tags, serial = 11)
        repeat(60) { big.writePacket(sized(1275, it)) }
        val bb = big.finish((60 * 960L - 312) / 3)
        assertEquals(listOf(252, 108), OggReader.pages(bb).drop(2).map { it.table.size })
        assertEquals(60, OggReader.audioPackets(bb).size)
        assertTrue(OggReader.pages(bb).all { it.crcOk })
    }

    @Test
    fun realPacketsAtLacingEdgesDecodeExactly() {
        assumeTrue(have("ffmpeg"), "ffmpeg not on PATH")
        val dir = Files.createTempDirectory("oggedges").toFile()
        try {
            val pcm = chirpPcm(48_000) // 3 s
            data class Case(val name: String, val src: ByteArray, val check: (List<List<Int>>) -> Unit)
            val cases = listOf(
                // CBR 102 kbps at 20 ms: every packet exactly 255 bytes, laced 255,0
                Case("cbr255", ffmpegEncode(pcm, dir, "102000", "audio", "off", "20", "cbr255.opus")) { lacing ->
                    assertTrue(lacing.all { t -> t.windowed(2, 2).all { it == listOf(255, 0) } }, "255,0 pairs")
                },
                // CBR 102.4 kbps: 256-byte packets, laced 255,1
                Case("cbr256", ffmpegEncode(pcm, dir, "102400", "audio", "off", "20", "cbr256.opus")) { lacing ->
                    assertTrue(lacing.all { t -> t.windowed(2, 2).all { it == listOf(255, 1) } }, "255,1 pairs")
                },
                // 2.5 ms frames: 400 packets a second, pages split at 255 lacing values
                Case("f2.5", ffmpegEncode(pcm, dir, frameDuration = "2.5", application = "audio", name = "f25.opus")) { lacing ->
                    assertTrue(lacing.dropLast(1).all { it.size == 255 }, "full lacing tables: ${lacing.map { it.size }}")
                },
            )
            for (c in cases) {
                val info = OggReader.parse(c.src)
                val packets = OggReader.audioPackets(c.src)
                val n = 47_000
                val w = OggOpusWriter(OpusHead(preSkip = info.preSkip), tags, serial = c.name.hashCode())
                packets.forEach(w::writePacket)
                val bytes = w.finish(n.toLong())
                c.check(audioLacing(bytes))
                assertTrue(OggReader.pages(bytes).all { it.crcOk && it.table.size <= 255 }, c.name)
                // the reader gets the packets back byte for byte
                val back = OggReader.audioPackets(bytes)
                back.forEachIndexed { i, p -> assertContentEquals(packets[i], p, "${c.name} packet $i") }
                val f = File(dir, "remux-${c.name}.opus").apply { writeBytes(bytes) }
                assertEquals(3 * n, decodedSamples(f, rate = null), "${c.name}: 48 kHz decode")
                assertEquals(n, decodedSamples(f, rate = 16000), "${c.name}: 16 kHz decode")
                val srcFile = File(dir, "src-${c.name}.opus").apply { writeBytes(c.src) }
                val ref = exec(listOf("ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", srcFile.path,
                    "-f", "s16le", "pipe:1"))
                val ours = exec(listOf("ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", f.path,
                    "-f", "s16le", "pipe:1"))
                assertContentEquals(ref.copyOf(6 * n), ours, "${c.name}: decode differs from the source's")
            }
        } finally {
            dir.deleteRecursively()
        }
    }

    private fun decodedSamples(file: File, rate: Int?): Int {
        val cmd = mutableListOf("ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", file.path)
        if (rate != null) cmd += listOf("-ar", rate.toString())
        cmd += listOf("-ac", "1", "-f", "s16le", "pipe:1")
        return exec(cmd).size / 2
    }

    @Test
    fun remuxedFfmpegPacketsDecodeToExactlyNSamplesWithTags() {
        assumeTrue(have("ffmpeg") && have("ffprobe"), "ffmpeg/ffprobe not on PATH")
        val dir = Files.createTempDirectory("oggwriter").toFile()
        try {
            val nSource = 40_000 // 2.5 s
            val source = ffmpegEncode(chirpPcm(nSource), dir)
            // our CRC agrees with libogg's on every page ffmpeg wrote
            val srcInfo = OggReader.parse(source)
            assertTrue(srcInfo.pages.all { it.crcOk }, "CRC of ffmpeg's pages")
            assertEquals(srcInfo.preSkip + 3L * nSource, srcInfo.lastGranule)
            val packets = OggReader.audioPackets(source)
            val decodedSource = exec(
                listOf("ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", File(dir, "ffmpeg.opus").path,
                    "-f", "s16le", "pipe:1"),
            )

            for (n in listOf(nSource, 33_333, 480)) {
                val w = OggOpusWriter(OpusHead(preSkip = srcInfo.preSkip), tags, serial = 0x5eed + n)
                packets.forEach(w::writePacket)
                val bytes = w.finish(n.toLong())
                val f = File(dir, "remux-$n.opus").apply { writeBytes(bytes) }

                // the real decoder: exactly n samples at 16 kHz is exactly 3n at Opus's native 48 kHz
                assertEquals(3 * n, decodedSamples(f, rate = null), "48 kHz decode of n=$n")
                assertEquals(n, decodedSamples(f, rate = 16000), "16 kHz decode of n=$n")
                // and sample-identical to the source stream's decode over that span
                val ours = exec(listOf("ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", f.path,
                    "-f", "s16le", "pipe:1"))
                assertContentEquals(decodedSource.copyOf(6 * n), ours, "decode differs for n=$n")

                val probe = exec(listOf("ffprobe", "-v", "error", "-show_entries", "stream_tags:format_tags",
                    "-of", "default=noprint_wrappers=1", f.path)).decodeToString()
                for ((k, v) in tags) assertTrue("TAG:$k=$v" in probe, "ffprobe misses $k in:\n$probe")

                val info = OggReader.parse(bytes)
                assertEquals(srcInfo.preSkip, info.preSkip)
                assertEquals(tags, info.tags)
                assertEquals(n / 16000.0, info.durationS)
                assertTrue(info.pages.all { it.crcOk })
            }
        } finally {
            dir.deleteRecursively()
        }
    }

    @Test
    fun edgeOggPyParsesTheWritersOutput() {
        assumeTrue(have("ffmpeg") && have("python3"), "ffmpeg/python3 not on PATH")
        val dir = Files.createTempDirectory("oggpy").toFile()
        try {
            val source = ffmpegEncode(chirpPcm(16_000), dir)
            val info0 = OggReader.parse(source)
            val w = OggOpusWriter(OpusHead(preSkip = info0.preSkip), tags, serial = 7)
            OggReader.audioPackets(source).forEach(w::writePacket)
            val f = File(dir, "w.opus").apply { writeBytes(w.finish(12_345)) }
            val edgeSrc = TestSupport.repoRoot.resolve("edge/src").path
            val script = "import sys, json; sys.path.insert(0, sys.argv[1]); from roomlog_edge import ogg; " +
                "i = ogg.parse(open(sys.argv[2], 'rb').read()); " +
                "print(json.dumps({'pre_skip': i.pre_skip, 'rate': i.input_sample_rate, 'channels': i.channels, " +
                "'tags': i.tags, 'duration_s': i.duration_s}))"
            val got = Json.parseObject(exec(listOf("python3", "-c", script, edgeSrc, f.path)).decodeToString())
            assertEquals(info0.preSkip.toLong(), got["pre_skip"])
            assertEquals(16000L, got["rate"])
            assertEquals(1L, got["channels"])
            assertEquals(tags, got["tags"])
            assertEquals(12_345 / 16000.0, got["duration_s"])
        } finally {
            dir.deleteRecursively()
        }
    }
}
