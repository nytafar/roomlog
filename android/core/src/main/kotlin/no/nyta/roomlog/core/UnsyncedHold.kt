package no.nyta.roomlog.core

import java.io.IOException

/**
 * The edge's unsynced hold (`capture.py` `set_synced` / `release_unsynced`)
 * for a client whose sync reference is a `/v1/time` probe (ADR 0006).
 *
 * Split over the two threads that own its halves. The capture thread owns
 * the [Timeline] and this object: [update] once per block with the probe
 * state, [onClose] for every closed segment. While not synced, closed
 * segments are stamped `clock_synced: false` and remembered; on the
 * transition to synced their `start_utc` is recomputed through the timeline
 * (the server offset is applied first, so a step reaches them) and handed
 * back as [Restamp]s. The thread that writes the spool applies them with
 * [release], in queue order after the segments themselves were written.
 *
 * `n_start` identifies a segment within a run: the timeline's sample counter
 * never goes back, across epochs included.
 */
class UnsyncedHold {
    data class Held(val nStart: Long, val epoch: Int, val stepCount: Int)
    data class Restamp(val utcNs: Long, val clockStep: Boolean)

    private val held = ArrayList<Held>()

    /** What the last [update] said; closes are stamped with it. */
    var synced = false
        private set

    val heldCount: Int get() = held.size

    /** The earliest sample a held segment still needs the timeline for. */
    val minHeldN: Long? get() = held.minOfOrNull { it.nStart }

    /**
     * Feed the current probe state. [offsetNs] is the probe's `real − mono`
     * and [monoNs] the capture clock now. Returns the re-stamps (by
     * `n_start`) on a transition to synced, else null. A transition to
     * unsynced (the last probe aged out) returns null; closes after it are held.
     */
    fun update(nowSynced: Boolean, timeline: Timeline, monoNs: Long, offsetNs: Long?): Map<Long, Restamp>? {
        if (nowSynced == synced) return null
        synced = nowSynced
        if (!nowSynced) return null
        // The probe may have landed after this block's clocks were read; apply it
        // now so the step reaches the held stamps (the edge's observe_offset at sync).
        if (offsetNs != null && timeline.epoch != null) timeline.observeOffset(monoNs + offsetNs, monoNs)
        val out = LinkedHashMap<Long, Restamp>()
        for (h in held) {
            if (h.epoch !in timeline.epochs) continue // pruned: released as-is at shutdown
            out[h.nStart] = Restamp(timeline.utcNs(h.nStart, h.epoch), timeline.stepCount > h.stepCount)
        }
        held.removeAll { it.nStart in out }
        return out
    }

    /** A segment closed. Returns its `clock_synced`; false means it goes to `unsynced/` and is held. */
    fun onClose(nStart: Long, epoch: Int, timeline: Timeline): Boolean {
        if (!synced) held += Held(nStart, epoch, timeline.stepCount)
        return synced
    }

    data class Released(
        val restamped: Int,
        val moved: Int,
        val foreign: Int,
        val failed: Int,
        val kept: Int,
        /** Entries a rename or write failed for (a full disk): left where they are, not retried here. */
        val errors: Int = 0,
        val firstError: String? = null,
    )

    companion object {
        /**
         * Move `unsynced/` into `pending/`, as the edge's `release_unsynced`.
         *
         * - Entries of [runId] whose `n_start` is in [restamps] get the new
         *   `start_utc`, `clock_synced: true`, and `clock_step: true` if a step
         *   was applied since they were stamped.
         * - Other entries of [runId] stay held unless [releaseOwn] (shutdown),
         *   which moves them as-is.
         * - Entries of other runs (a killed run, or segments from before this
         *   client probed) move as-is, `clock_synced: false`: their mapping is gone.
         * - Entries whose `device_id` is not [deviceId] stay in `unsynced/`:
         *   the server would answer 403 forever and block the queue behind them.
         * - An unreadable sidecar moves the pair to `failed/`.
         *
         * An I/O error on one entry (a full disk) is counted in [Released.errors]
         * and the entry is left where it is; the rest are still handled. A held
         * entry that failed here is released as stamped at shutdown, and
         * [Spool.cleanupTmp] finishes or removes a half-done rewrite.
         */
        fun release(
            spool: Spool,
            runId: String,
            deviceId: String,
            restamps: Map<Long, Restamp> = emptyMap(),
            releaseOwn: Boolean = false,
        ): Released {
            var restamped = 0
            var moved = 0
            var foreign = 0
            var failed = 0
            var kept = 0
            var errors = 0
            var firstError: String? = null
            for (entry in spool.entries("unsynced")) {
                val meta = try {
                    entry.readMeta()
                } catch (_: IOException) {
                    null
                } catch (_: IllegalArgumentException) {
                    null
                }
                if (meta == null) {
                    try {
                        spool.move(entry, "failed")
                        failed++
                    } catch (e: IOException) {
                        errors++
                        if (firstError == null) firstError = "${entry.stem}: $e"
                    }
                    continue
                }
                if (meta["device_id"] != deviceId) {
                    foreign++
                    continue
                }
                val nStart = meta["n_start"] as? Long
                val r = if (meta["run_id"] == runId && nStart != null) restamps[nStart] else null
                try {
                    when {
                        r != null -> {
                            meta["start_utc"] = Sidecar.formatUtc(r.utcNs)
                            meta["clock_synced"] = true
                            if (r.clockStep) meta["clock_step"] = true
                            spool.rewriteMeta(entry, meta, "pending")
                            restamped++
                        }
                        meta["run_id"] == runId && !releaseOwn -> kept++
                        else -> {
                            spool.move(entry, "pending")
                            moved++
                        }
                    }
                } catch (e: IOException) {
                    errors++
                    if (firstError == null) firstError = "${entry.stem}: $e"
                }
            }
            return Released(restamped, moved, foreign, failed, kept, errors, firstError)
        }
    }
}
