package no.nyta.roomlog.core

import java.math.BigDecimal
import java.math.RoundingMode
import java.time.Instant
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter

/**
 * Sidecar JSON (contract §Sidecar) and a light validator mirroring the schema.
 * Port of `edge/src/roomlog_edge/sidecar.py`; a sidecar is a
 * `LinkedHashMap<String, Any?>` whose insertion order is the wire order.
 */
object Sidecar {
    const val SCHEMA_VERSION = 1L
    val CUT_REASONS = listOf("silence", "cap", "discontinuity", "shutdown")
    val KINDS = listOf("speech", "raw")
    val REQUIRED = listOf(
        "schema_version", "device_id", "sha256", "start_utc", "duration_s",
        "run_id", "epoch", "discontinuity", "clock_synced",
    )

    private val DEVICE_RE = Regex("^[a-z0-9][a-z0-9-]{0,62}$")
    private val SHA_RE = Regex("^[0-9a-f]{64}$")
    private val UTC_RE = Regex("^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\\.\\d{3}Z$")
    private val UUID_RE = Regex(
        "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", RegexOption.IGNORE_CASE,
    )
    private val SECONDS_FMT = DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss").withZone(ZoneOffset.UTC)

    /** ISO 8601 with millisecond precision and a Z suffix (rounded to the nearest ms). */
    fun formatUtc(utcNs: Long): String {
        val ms = Math.floorDiv(utcNs + 500_000, 1_000_000L)
        val sec = Math.floorDiv(ms, 1000L)
        val frac = Math.floorMod(ms, 1000L)
        return SECONDS_FMT.format(Instant.ofEpochSecond(sec)) + "." + frac.toString().padStart(3, '0') + "Z"
    }

    /** `2026-09-26T10:15:32.417Z` → `20260926T101532417Z`. */
    fun compactUtc(startUtc: String): String = startUtc.replace(Regex("[-:.]"), "")

    /** Python's `round(n_samples / sample_rate, 3)`: the double nearest to the
     *  exact binary quotient rounded half-even to three decimals. */
    fun durationS(nSamples: Long, sampleRate: Int): Double =
        BigDecimal(nSamples.toDouble() / sampleRate).setScale(3, RoundingMode.HALF_EVEN).toDouble()

    /**
     * Same keys and order as `sidecar.build`. [kind], when given, goes right
     * after `schema_version` (the contract's additive field); [vad] null omits
     * the `vad` object, as a raw segment has none.
     */
    fun build(
        deviceId: String,
        sha256: String,
        utcNs: Long,
        nStart: Long,
        nSamples: Long,
        runId: String,
        epoch: Int,
        discontinuity: Boolean,
        clockStep: Boolean,
        clockSynced: Boolean,
        cutReason: String,
        vad: Map<String, Any?>?,
        edgeVersion: String,
        sampleRate: Int = 16000,
        kind: String? = null,
    ): LinkedHashMap<String, Any?> {
        val m = LinkedHashMap<String, Any?>()
        m["schema_version"] = SCHEMA_VERSION
        if (kind != null) m["kind"] = kind
        m["device_id"] = deviceId
        m["sha256"] = sha256
        m["start_utc"] = formatUtc(utcNs)
        m["duration_s"] = durationS(nSamples, sampleRate)
        m["sample_rate"] = sampleRate.toLong()
        m["run_id"] = runId
        m["epoch"] = epoch.toLong()
        m["n_start"] = nStart
        m["n_samples"] = nSamples
        m["discontinuity"] = discontinuity
        m["clock_step"] = clockStep
        m["clock_synced"] = clockSynced
        m["cut_reason"] = cutReason
        if (vad != null) m["vad"] = LinkedHashMap(vad)
        m["edge_version"] = edgeVersion
        m["session_hint"] = null
        m["multi_speaker"] = null
        return m
    }

    /** The `X-Roomlog-Meta` header value: one line, ASCII only, no spaces. */
    fun dumpsWire(meta: Map<String, Any?>): String = Json.dumps(meta)

    /** The spool's `.json` file: `json.dumps(meta, ensure_ascii=True, indent=2) + "\n"`. */
    fun dumpsFile(meta: Map<String, Any?>): String = Json.dumps(meta, indent = 2) + "\n"

    private fun isInt(v: Any?) = v is Long || v is Int
    private fun isNum(v: Any?) = v is Long || v is Int || v is Double || v is Float
    private fun num(v: Any?) = (v as Number).toDouble()

    /** Problems with [meta]; empty means valid. Same checks and messages as
     *  `sidecar.validate`, plus the `kind` enum. */
    fun validate(meta: Any?): List<String> {
        if (meta !is Map<*, *>) return listOf("sidecar is not an object")
        val errs = mutableListOf<String>()
        for (k in REQUIRED) if (k !in meta) errs += "missing $k"
        if (errs.isNotEmpty()) return errs
        if (!isInt(meta["schema_version"]) || (meta["schema_version"] as Number).toLong() != 1L) {
            errs += "schema_version must be 1"
        }
        if ("kind" in meta && meta["kind"] !in KINDS) errs += "kind"
        if (!matches(meta["device_id"], DEVICE_RE)) errs += "device_id pattern"
        if (!matches(meta["sha256"], SHA_RE)) errs += "sha256 pattern"
        if (!matches(meta["start_utc"], UTC_RE)) errs += "start_utc pattern"
        val d = meta["duration_s"]
        if (!isNum(d) || !(num(d) > 0 && num(d) <= 31)) errs += "duration_s range"
        if (!matches(meta["run_id"], UUID_RE)) errs += "run_id uuid"
        val ep = meta["epoch"]
        if (!isInt(ep) || (ep as Number).toLong() < 0) errs += "epoch"
        for (k in listOf("discontinuity", "clock_synced")) {
            if (meta[k] !is Boolean) errs += "$k must be boolean"
        }
        if ("clock_step" in meta && meta["clock_step"] !is Boolean) errs += "clock_step must be boolean"
        if ("sample_rate" in meta && !(isInt(meta["sample_rate"]) && (meta["sample_rate"] as Number).toLong() == 16000L)) {
            errs += "sample_rate"
        }
        if ("n_start" in meta && !(isInt(meta["n_start"]) && (meta["n_start"] as Number).toLong() >= 0)) errs += "n_start"
        if ("n_samples" in meta && !(isInt(meta["n_samples"]) && (meta["n_samples"] as Number).toLong() >= 1)) {
            errs += "n_samples"
        }
        if ("cut_reason" in meta && meta["cut_reason"] !in CUT_REASONS) errs += "cut_reason"
        if ("vad" in meta && meta["vad"] !is Map<*, *>) errs += "vad must be an object"
        if ("edge_version" in meta && meta["edge_version"] !is String) errs += "edge_version"
        if ("session_hint" in meta && !(meta["session_hint"] == null || meta["session_hint"] is String)) {
            errs += "session_hint"
        }
        if ("multi_speaker" in meta && !(meta["multi_speaker"] == null || meta["multi_speaker"] is Boolean)) {
            errs += "multi_speaker"
        }
        return errs
    }

    private fun matches(v: Any?, re: Regex) = v is String && re.matches(v)
}
