package no.nyta.roomlog.core

import java.io.IOException
import kotlin.random.Random

/** Status → action and backoff, as `edge/src/roomlog_edge/uploader.py` and the contract's table. */
object UploadPolicy {
    enum class Action { ACK, FAIL, RETRY }

    fun classify(status: Int): Action = when (status) {
        200, 201 -> Action.ACK
        409, 413, 422 -> Action.FAIL
        else -> Action.RETRY // 401, 403, 411, 5xx and anything unexpected
    }

    /** `min(maxBackoffS, 2**k)` seconds with full jitter. */
    fun backoffS(k: Int, maxBackoffS: Double = 300.0, rng: Random = Random.Default): Double {
        val cap = minOf(maxBackoffS, Math.pow(2.0, minOf(k, 30).toDouble()))
        return rng.nextDouble() * cap
    }
}

/**
 * Uploader: `spool/pending/` → `put` → delete on a matching ack. The HTTP call
 * is injected as [put] `(opus, metaWire, sha256) -> (status, body)`, which
 * throws [IOException] on connection errors and timeouts; the `:app` side
 * implements it with `HttpURLConnection` and a fixed-length body.
 */
class Uploader(
    private val spool: Spool,
    private val put: (opus: ByteArray, metaWire: String, sha256: String) -> Pair<Int, ByteArray>,
    val maxBackoffS: Double = 300.0,
    val idlePollS: Double = 5.0,
    private val rng: Random = Random.Default,
    private val nowNs: () -> Long = { System.currentTimeMillis() * 1_000_000 },
) {
    data class Outcome(val action: UploadPolicy.Action, val status: Int?, val detail: String, val stem: String = "")

    var failures = 0
        private set
    var lastSuccessNs: Long? = null
        private set
    var lastError: String? = null
        private set
    var uploadedTotal = 0
        private set
    var failedTotal = 0
        private set

    fun uploadOne(entry: Spool.Entry): Outcome = uploadOneInner(entry).copy(stem = entry.stem)

    private fun uploadOneInner(entry: Spool.Entry): Outcome {
        val meta: Map<String, Any?>
        val opus: ByteArray
        try {
            meta = entry.readMeta()
            opus = entry.opus.readBytes()
        } catch (e: IOException) {
            spool.move(entry, "failed")
            return Outcome(UploadPolicy.Action.FAIL, null, "unreadable: $e")
        } catch (e: IllegalArgumentException) { // JSON parse error
            spool.move(entry, "failed")
            return Outcome(UploadPolicy.Action.FAIL, null, "unreadable: $e")
        }
        val sha = meta["sha256"] as? String ?: run {
            spool.move(entry, "failed")
            return Outcome(UploadPolicy.Action.FAIL, null, "unreadable: sidecar has no sha256")
        }
        val (status, body) = try {
            put(opus, Sidecar.dumpsWire(meta), sha)
        } catch (e: IOException) {
            return Outcome(UploadPolicy.Action.RETRY, null, "${e::class.simpleName}: ${e.message}")
        }
        val action = UploadPolicy.classify(status)
        val detail = body.copyOf(minOf(body.size, 200)).decodeToString()
        when (action) {
            UploadPolicy.Action.ACK -> {
                val got = try {
                    Json.parseObject(body.decodeToString())["sha256"]
                } catch (_: IllegalArgumentException) {
                    null
                }
                if (got != sha) return Outcome(UploadPolicy.Action.RETRY, status, "response sha256 mismatch: $got")
                spool.delete(entry)
            }
            UploadPolicy.Action.FAIL -> spool.move(entry, "failed")
            UploadPolicy.Action.RETRY -> Unit
        }
        return Outcome(action, status, detail)
    }

    /** Upload pending files in order until one needs a retry. */
    fun runOnce(maxFiles: Int? = null): List<Outcome> {
        val outcomes = mutableListOf<Outcome>()
        for ((i, entry) in spool.entries("pending").withIndex()) {
            if (maxFiles != null && i >= maxFiles) break
            val o = uploadOne(entry)
            outcomes += o
            when (o.action) {
                UploadPolicy.Action.ACK -> {
                    failures = 0
                    lastSuccessNs = nowNs()
                    lastError = null
                    uploadedTotal++
                }
                UploadPolicy.Action.FAIL -> {
                    failedTotal++
                    lastError = "${o.status}: ${o.detail}"
                }
                UploadPolicy.Action.RETRY -> {
                    failures++
                    lastError = "${o.status}: ${o.detail}"
                    break
                }
            }
        }
        return outcomes
    }

    fun nextDelayS(outcomes: List<Outcome>): Double = when {
        outcomes.isNotEmpty() && outcomes.last().action == UploadPolicy.Action.RETRY ->
            UploadPolicy.backoffS(failures, maxBackoffS, rng)
        outcomes.isNotEmpty() -> 0.0
        else -> idlePollS
    }
}
