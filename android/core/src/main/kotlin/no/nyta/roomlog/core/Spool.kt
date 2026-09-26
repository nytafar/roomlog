package no.nyta.roomlog.core

import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.nio.channels.FileChannel
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.nio.file.StandardOpenOption
import java.security.MessageDigest

/**
 * Spool directory, port of `edge/src/roomlog_edge/spool.py`.
 *
 * Layout under [root]: `tmp/` (in-progress writes), `pending/` (ready to
 * upload), `unsynced/` (held until the clock reference is verified),
 * `failed/` (rejected for good). A chunk is `<start_utc compact>_<sha8>.opus`
 * plus the same stem `.json`; lexical order is upload order. Nothing here
 * ever deletes audio that has not been acknowledged: [delete] is the
 * uploader's call after a matching ack.
 */
class Spool(
    val root: File,
    val maxBytes: Long = 2L * 1024 * 1024 * 1024,
    val minFreeFraction: Double = 0.05,
    /** fsync of a directory after renames; best effort (see [fsyncDir]). */
    private val dirSync: (File) -> Unit = ::fsyncDir,
) {
    data class Entry(val stem: String, val opus: File, val json: File) {
        val size: Long get() = opus.length()
        fun readMeta(): LinkedHashMap<String, Any?> = Json.parseObject(json.readText(Charsets.UTF_8))
    }

    data class Stats(
        val pendingFiles: Int,
        val pendingBytes: Long,
        val unsyncedFiles: Int,
        val failedFiles: Int,
        val totalBytes: Long,
    ) {
        val files: Int get() = pendingFiles + unsyncedFiles
    }

    /** Filesystem size and free space, injectable for the guard's tests. */
    data class Usage(val total: Long, val free: Long)

    init {
        for (d in DIRS) {
            val f = File(root, d)
            if (!f.isDirectory && !f.mkdirs()) throw IOException("cannot create $f")
        }
    }

    fun dir(name: String): File = File(root, name)

    // -- writing -----------------------------------------------------------

    /**
     * Remove leftovers in `tmp/`. A crash between the two renames of [write] or
     * [rewriteMeta] leaves `<stem>.opus` in its destination with
     * `<stem>.json.tmp` still here: that pair is finished, not deleted.
     * Returns the number of files touched.
     */
    fun cleanupTmp(): Int {
        var n = 0
        for (p in (dir("tmp").listFiles() ?: emptyArray()).sortedBy { it.name }) {
            if (!p.isFile) continue
            if (p.name.endsWith(".json.tmp")) {
                val stem = p.name.removeSuffix(".json.tmp")
                val dest = listOf("pending", "unsynced").firstOrNull { File(dir(it), "$stem.opus").exists() }
                if (dest != null) {
                    replace(p, File(dir(dest), "$stem.json"))
                    dirSync(dir(dest))
                } else {
                    p.delete()
                }
            } else {
                p.delete()
            }
            n++
        }
        // a sidecar whose audio is gone (rewriteMeta renamed the .opus away before the crash) is useless
        for (dest in listOf("pending", "unsynced")) {
            for (js in dir(dest).listFiles { f -> f.name.endsWith(".json") } ?: emptyArray()) {
                if (!File(dir(dest), js.name.removeSuffix(".json") + ".opus").exists()) {
                    js.delete()
                    n++
                }
            }
        }
        return n
    }

    /**
     * Write `.opus` and `.json` via `tmp/` (write + fsync) and rename both into
     * [dest]. `meta["sha256"]` is set from the bytes here; the caller need not.
     */
    fun write(opus: ByteArray, meta: Map<String, Any?>, dest: String = "pending"): Entry {
        require(dest == "pending" || dest == "unsynced") { "bad destination $dest" }
        val m = LinkedHashMap(meta)
        m["sha256"] = sha256Hex(opus)
        val stem = stemFor(m)
        val tOpus = File(dir("tmp"), "$stem.opus.tmp")
        val tJson = File(dir("tmp"), "$stem.json.tmp")
        writeFsync(tOpus, opus)
        writeFsync(tJson, Sidecar.dumpsFile(m).toByteArray(Charsets.US_ASCII))
        val target = dir(dest)
        val fOpus = File(target, "$stem.opus")
        val fJson = File(target, "$stem.json")
        replace(tOpus, fOpus)
        replace(tJson, fJson)
        dirSync(target)
        return Entry(stem, fOpus, fJson)
    }

    /** Replace the sidecar (start_utc may change, so the stem may too) and move
     *  both files into [dest]. */
    fun rewriteMeta(entry: Entry, meta: Map<String, Any?>, dest: String): Entry {
        val stem = stemFor(meta)
        val tmpJson = File(dir("tmp"), "$stem.json.tmp")
        writeFsync(tmpJson, Sidecar.dumpsFile(meta).toByteArray(Charsets.US_ASCII))
        val target = dir(dest)
        val fOpus = File(target, "$stem.opus")
        val fJson = File(target, "$stem.json")
        replace(entry.opus, fOpus)
        replace(tmpJson, fJson)
        if (entry.json.absoluteFile != fJson.absoluteFile) entry.json.delete()
        dirSync(target)
        return Entry(stem, fOpus, fJson)
    }

    // -- listing -----------------------------------------------------------

    fun entries(name: String = "pending"): List<Entry> {
        val d = dir(name)
        return (d.listFiles { f -> f.name.endsWith(".opus") } ?: emptyArray())
            .sortedBy { it.name }
            .mapNotNull { opus ->
                val stem = opus.name.removeSuffix(".opus")
                val js = File(d, "$stem.json")
                if (js.exists()) Entry(stem, opus, js) else null
            }
    }

    fun move(entry: Entry, dest: String): Entry {
        val target = dir(dest)
        val fOpus = File(target, entry.opus.name)
        val fJson = File(target, entry.json.name)
        replace(entry.opus, fOpus)
        replace(entry.json, fJson)
        return Entry(entry.stem, fOpus, fJson)
    }

    fun delete(entry: Entry) {
        entry.opus.delete()
        entry.json.delete()
    }

    fun stats(): Stats {
        fun count(name: String): Pair<Int, Long> {
            var files = 0
            var bytes = 0L
            for (p in dir(name).listFiles() ?: emptyArray()) {
                if (!p.isFile) continue // also covers a file the uploader deleted meanwhile
                if (p.name.endsWith(".opus") && File(p.parentFile, p.name.removeSuffix(".opus") + ".json").exists()) {
                    files++
                }
                bytes += p.length()
            }
            return files to bytes
        }
        val (pf, pb) = count("pending")
        val (uf, ub) = count("unsynced")
        val (ff, fb) = count("failed")
        val (_, tb) = count("tmp")
        return Stats(pf, pb, uf, ff, pb + ub + fb + tb)
    }

    // -- disk guard --------------------------------------------------------

    /** False when the spool is above [maxBytes] or the filesystem has less
     *  than [minFreeFraction] free. Never deletes anything. */
    fun diskOk(usage: Usage? = null): Boolean {
        if (stats().totalBytes > maxBytes) return false
        val u = usage ?: Usage(root.totalSpace, root.usableSpace)
        return u.free >= minFreeFraction * u.total
    }

    companion object {
        val DIRS = listOf("tmp", "pending", "unsynced", "failed")

        fun sha256Hex(data: ByteArray): String =
            MessageDigest.getInstance("SHA-256").digest(data).joinToString("") { "%02x".format(it) }

        fun stemFor(meta: Map<String, Any?>): String =
            "${Sidecar.compactUtc(meta["start_utc"] as String)}_${(meta["sha256"] as String).take(8)}"

        /** `os.replace`: atomic rename that overwrites the target. */
        private fun replace(from: File, to: File) {
            Files.move(from.toPath(), to.toPath(), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING)
        }

        private fun writeFsync(f: File, data: ByteArray) {
            FileOutputStream(f).use { out ->
                out.write(data)
                out.flush()
                out.fd.sync()
            }
        }

        /**
         * fsync a directory so renames into it survive power loss. Works on
         * Linux JVMs and Android (open(2) O_RDONLY on a directory); where the
         * platform refuses, the rename is still atomic, only not yet durable.
         */
        fun fsyncDir(dir: File) {
            try {
                FileChannel.open(dir.toPath(), StandardOpenOption.READ).use { it.force(true) }
            } catch (_: IOException) {
            } catch (_: UnsupportedOperationException) {
            }
        }
    }
}
