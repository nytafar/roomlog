package no.nyta.roomlog.spike

import no.nyta.roomlog.core.Json
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

/**
 * The `:app` side of the upload contract, for `Uploader.put` and
 * `ClockOffset.probe`. `HttpURLConnection` with a fixed-length body, so
 * `Content-Length` is always explicit (the server answers 411 to chunked
 * bodies). Connection errors and timeouts surface as [IOException], which
 * the uploader treats as retry. Not wired into the P2 spike, which does not
 * upload; P3 uses it.
 */
class Http(baseUrl: String, private val token: String, private val timeoutMs: Int = 30_000) {
    private val base = baseUrl.trimEnd('/')

    init {
        require(base.startsWith("https://") || base.startsWith("http://")) { "bad server URL $baseUrl" }
    }

    /** `PUT /v1/chunks/{sha}`; returns (status, body). */
    fun put(opus: ByteArray, metaWire: String, sha256: String): Pair<Int, ByteArray> {
        val c = open("/v1/chunks/$sha256", "PUT")
        try {
            c.doOutput = true
            c.setFixedLengthStreamingMode(opus.size)
            c.setRequestProperty("Content-Type", "audio/ogg")
            c.setRequestProperty("X-Roomlog-Meta", metaWire)
            c.outputStream.use { it.write(opus) }
            return c.responseCode to body(c)
        } finally {
            c.disconnect()
        }
    }

    /** `GET /v1/time` → the server's `utc_ns`; any non-200 is an [IOException]. */
    fun serverUtcNs(): Long {
        val c = open("/v1/time", "GET")
        try {
            val status = c.responseCode
            val b = body(c).decodeToString()
            if (status != 200) throw IOException("GET /v1/time: $status $b")
            return (Json.parseObject(b)["utc_ns"] as? Long) ?: throw IOException("GET /v1/time: no utc_ns in $b")
        } catch (e: IllegalArgumentException) {
            throw IOException("GET /v1/time: unparseable body", e)
        } finally {
            c.disconnect()
        }
    }

    private fun open(path: String, method: String): HttpURLConnection =
        (URL(base + path).openConnection() as HttpURLConnection).apply {
            requestMethod = method
            connectTimeout = timeoutMs
            readTimeout = timeoutMs
            useCaches = false
            instanceFollowRedirects = false
            setRequestProperty("Authorization", "Bearer $token")
        }

    private fun body(c: HttpURLConnection): ByteArray =
        (if (c.responseCode >= 400) c.errorStream else c.inputStream)?.use { it.readBytes() } ?: ByteArray(0)
}
