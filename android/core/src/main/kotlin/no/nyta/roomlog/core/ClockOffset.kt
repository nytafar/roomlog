package no.nyta.roomlog.core

/**
 * The server's clock as the sync reference (ADR 0006): Cristian's method
 * over one `GET /v1/time` round trip.
 *
 * All `mono` values are the capture clock (`elapsedRealtimeNanos()`). The
 * server stamped `serverUtcNs` somewhere inside `[tSendMono, tRecvMono]`;
 * assuming the midpoint, `offset = serverUtc − (tSend + tRecv)/2` is the
 * `real − mono` term the timeline stamps with, accurate to ± rtt/2.
 */
class ClockOffset(
    /** A probe older than this no longer makes the clock "synced" (contract: ten minutes). */
    val validityNs: Long = 10 * 60 * 1_000_000_000L,
    /** Probes with a longer round trip are too imprecise to count as success. */
    val maxRttNs: Long = 5_000_000_000L,
) {
    data class Probe(val offsetNs: Long, val rttNs: Long, val atMonoNs: Long)

    /** The last successful probe, if any. */
    var last: Probe? = null
        private set

    /** `real − mono` in the server's timebase, or null before the first success. */
    val offsetNs: Long? get() = last?.offsetNs

    /** Record one round trip; returns the probe, or null if it was rejected. */
    fun record(tSendMono: Long, tRecvMono: Long, serverUtcNs: Long): Probe? {
        val rtt = tRecvMono - tSendMono
        if (rtt < 0 || rtt > maxRttNs) return null
        val mid = tSendMono + rtt / 2
        return Probe(serverUtcNs - mid, rtt, tRecvMono).also { last = it }
    }

    /**
     * Run one probe: [monoNow] reads the capture clock, [fetchServerUtcNs]
     * performs the HTTP call and returns `utc_ns`. Any exception is a failed
     * probe and leaves the previous one in place.
     */
    fun probe(monoNow: () -> Long, fetchServerUtcNs: () -> Long): Probe? {
        val t0 = monoNow()
        val server = try {
            fetchServerUtcNs()
        } catch (_: Exception) {
            return null
        }
        return record(t0, monoNow(), server)
    }

    /** True within [validityNs] of the last successful probe. */
    fun synced(nowMono: Long): Boolean {
        val p = last ?: return false
        val age = nowMono - p.atMonoNs
        return age in 0..validityNs
    }
}
