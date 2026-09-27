package no.nyta.roomlog.core

import java.util.concurrent.Semaphore
import java.util.concurrent.TimeUnit

/**
 * The client's upload thread: `/v1/time` probes (ADR 0006) and
 * [Uploader] passes over `pending/`, with the uploader's backoff on retries.
 *
 * Probing: first pass at once, then every [probeIntervalNs] while probes
 * succeed and every [probeRetryNs] while they fail. [ClockOffset] keeps the
 * last success; the capture thread reads it (offset and `synced`) and does
 * the re-stamping itself, since it owns the timeline.
 *
 * Waiting: after an idle pass the loop sleeps [Uploader.idlePollS] or until
 * [wake] (a new segment was spooled). After a retry it sleeps the full
 * backoff; [wake] does not shorten it, [stop] does. [run] makes one last
 * pass after [stop], so a user stop still sends the shutdown segment when
 * the server is reachable.
 */
class UploadLoop(
    private val uploader: Uploader,
    private val clock: ClockOffset,
    /** `GET /v1/time` → `utc_ns`; throws on any failure. */
    private val fetchServerUtcNs: () -> Long,
    /** The capture clock (`elapsedRealtimeNanos()` on Android). */
    private val monoNow: () -> Long,
    private val log: (String) -> Unit,
    /** The device wall clock, only to log how far it is from the server. */
    private val realNow: (() -> Long)? = null,
    val probeIntervalNs: Long = 5 * 60 * NS,
    val probeRetryNs: Long = 30 * NS,
) {
    data class Next(val delayS: Double, val backoff: Boolean)

    private var lastAttemptNs: Long? = null
    private var lastOk = false
    private val signal = Semaphore(0)

    @Volatile
    var stopped = false
        private set

    fun probeDue(nowMono: Long): Boolean {
        val last = lastAttemptNs ?: return true
        return nowMono - last >= if (lastOk) probeIntervalNs else probeRetryNs
    }

    /** One probe, logged. Returns the accepted probe or null. */
    fun probe(): ClockOffset.Probe? {
        val prev = clock.last
        val t0 = monoNow()
        lastAttemptNs = t0
        val server = try {
            fetchServerUtcNs()
        } catch (e: Exception) {
            lastOk = false
            log("clock probe failed: ${e::class.simpleName}: ${e.message}")
            return null
        }
        val t1 = monoNow()
        val real = realNow?.invoke()
        val p = clock.record(t0, t1, server)
        lastOk = p != null
        if (p == null) {
            log("clock probe rejected: rtt ${(t1 - t0) / MS} ms")
            return null
        }
        val parts = mutableListOf("rtt ${p.rttNs / MS} ms")
        if (prev == null) parts += "first probe" else parts += "offset moved ${(p.offsetNs - prev.offsetNs) / MS} ms"
        if (real != null) parts += "device clock ${(real - (t1 + p.offsetNs)) / MS} ms off server"
        log("clock probe ok: ${parts.joinToString(", ")}")
        return p
    }

    /** Probe if due, then one upload pass. */
    fun step(): Next {
        if (probeDue(monoNow())) probe()
        val outs = uploader.runOnce()
        for (o in outs) {
            when (o.action) {
                UploadPolicy.Action.ACK -> log("uploaded ${o.stem}: ${o.status}")
                UploadPolicy.Action.FAIL -> log("upload ${o.stem} REJECTED ${o.status}: ${o.detail} (moved to failed/)")
                UploadPolicy.Action.RETRY -> Unit
            }
        }
        val backoff = outs.lastOrNull()?.action == UploadPolicy.Action.RETRY
        val d = uploader.nextDelayS(outs)
        if (backoff) {
            val o = outs.last()
            log("upload ${o.stem}: ${o.status ?: "no response"} ${o.detail.trim()}; retry ${uploader.failures} in ${"%.0f".format(d)} s")
        }
        return Next(d, backoff)
    }

    /** A segment was spooled; ends an idle wait (not a backoff). */
    fun wake() = signal.release()

    fun stop() {
        stopped = true
        signal.release()
    }

    /** Runs until [stop], then one last pass. Exceptions in a pass are logged and retried after [probeRetryNs]. */
    fun run() {
        while (!stopped) {
            val next = try {
                step()
            } catch (e: Exception) {
                log("upload loop: $e")
                Next(probeRetryNs / NS.toDouble(), true)
            }
            await(next)
        }
        try {
            step()
        } catch (e: Exception) {
            log("upload loop, last pass: $e")
        }
    }

    private fun await(next: Next) {
        val deadline = System.nanoTime() + (next.delayS * NS).toLong()
        while (!stopped) {
            val left = deadline - System.nanoTime()
            if (left <= 0) return
            if (signal.tryAcquire(left, TimeUnit.NANOSECONDS) && !next.backoff) return
        }
    }

    private companion object {
        const val NS = 1_000_000_000L
        const val MS = 1_000_000L
    }
}
