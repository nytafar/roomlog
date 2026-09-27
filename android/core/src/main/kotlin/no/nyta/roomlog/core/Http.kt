package no.nyta.roomlog.core

import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

/**
 * The HTTP side of the upload contract, for [Uploader]'s `put` and the
 * `/v1/time` probe of [UploadLoop]. `HttpURLConnection` with a fixed-length
 * body, so `Content-Length` is always explicit (the server answers 411 to
 * chunked bodies). Connection errors and timeouts surface as [IOException],
 * which the uploader treats as retry. Plain `java.net`, so it lives in
 * `:core` and runs on the JVM against a real ingest (`LiveIngestTest`).
 * The token never appears in exceptions or logs.
 */
class Http(
    baseUrl: String,
    private val token: String,
    private val timeoutMs: Int = 30_000,
    private val connectTimeoutMs: Int = 10_000,
) {
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
            connectTimeout = connectTimeoutMs
            readTimeout = timeoutMs
            useCaches = false
            instanceFollowRedirects = false
            setRequestProperty("Authorization", "Bearer $token")
        }

    private fun body(c: HttpURLConnection): ByteArray =
        (if (c.responseCode >= 400) c.errorStream else c.inputStream)?.use { it.readBytes() } ?: ByteArray(0)
}
