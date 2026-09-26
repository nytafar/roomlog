package no.nyta.roomlog.core

/**
 * Which samples the timeline must still be able to stamp, as the edge's
 * `Pipeline._retain_from_n` (`capture.py`): the minimum over the live capture
 * state (open segment, held blocks), segments in flight between the capture
 * and encode threads, and every segment of this run held in `unsynced/`
 * (re-stamped through the same epoch mapping when a `/v1/time` probe
 * succeeds). Feed the result to [Timeline.retainFromN]; anchors and epochs
 * before it may be pruned.
 *
 * Thread-safe: the encode thread adds and removes, the capture thread reads.
 */
class Retention {
    private val inflight = HashMap<Long, Long>()
    private val held = HashMap<String, Long>()

    /** A closed segment was handed to the encoder; [key] is its `n_start` (unique within a run). */
    @Synchronized
    fun startInflight(key: Long, nStart: Long = key) {
        inflight[key] = nStart
    }

    /** The encoder finished with it (written, held or dropped). */
    @Synchronized
    fun endInflight(key: Long) {
        inflight.remove(key)
    }

    /** A segment of this run sits in `unsynced/` under [key] (its stem) and will need re-stamping. */
    @Synchronized
    fun hold(key: String, nStart: Long) {
        held[key] = nStart
    }

    /** It was re-stamped and moved to `pending/`, or deleted. */
    @Synchronized
    fun release(key: String) {
        held.remove(key)
    }

    @get:Synchronized
    val heldCount: Int get() = held.size

    /** The earliest sample still needed, given the live capture state's own minimum [liveFromN]. */
    @Synchronized
    fun retainFromN(liveFromN: Long): Long {
        var n = liveFromN
        inflight.values.minOrNull()?.let { n = minOf(n, it) }
        held.values.minOrNull()?.let { n = minOf(n, it) }
        return maxOf(n, 0L)
    }
}
