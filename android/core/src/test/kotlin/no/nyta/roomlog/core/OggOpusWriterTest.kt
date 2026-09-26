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

    /** encode.py's ffmpeg command line, verbatim. */
    private fun ffmpegEncode(pcm: ByteArray, dir: File): ByteArray = exec(
        listOf(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "s16le", "-ar", "16000", "-ac", "1", "-i", "pipe:0",
            "-c:a", "libopus", "-b:a", "24k", "-application", "voip",
            "-vbr", "on", "-frame_duration", "20", "-f", "ogg", "pipe:1",
        ),
        pcm,
    ).also { File(dir, "ffmpeg.opus").writeBytes(it) }

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
