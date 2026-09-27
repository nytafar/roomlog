package no.nyta.roomlog.spike

import android.content.Context
import android.os.Handler
import android.os.Looper
import android.util.Log
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Process-scoped state shared by [CaptureService] (which records) and
 * [MainActivity] (which shows it), so recreating or closing the activity
 * neither ends the run nor loses the log. Writes from any thread are posted
 * to the main thread, where Compose reads them.
 */
object SpikeState {
    val lines = mutableStateListOf<String>()
    val recording = mutableStateOf(false)
    private val main = Handler(Looper.getMainLooper())
    private val clock = SimpleDateFormat("HH:mm:ss.SSS", Locale.ROOT)

    /** Thread-safe; also goes to logcat under tag "roomlog". */
    fun log(msg: String) {
        Log.i("roomlog", msg)
        val line = synchronized(clock) { clock.format(Date()) } + " " + msg
        main.post {
            lines += line
            if (lines.size > 2000) lines.removeRange(0, lines.size - 2000)
        }
    }

    fun setRecording(on: Boolean) {
        main.post { recording.value = on }
    }

    /** `<external files dir>/spool`, pullable with `adb pull`; internal files dir if there is no external one. */
    fun spoolDir(context: Context): File = File(context.getExternalFilesDir(null) ?: context.filesDir, "spool")
}

/**
 * Settings, in app-private shared preferences: the device id the sidecars and
 * tags carry, and the server URL and bearer token for uploads. The token is
 * never logged or shown back; the plan's Keystore-wrapped storage is v2.
 */
object Prefs {
    const val DEFAULT_DEVICE_ID = "s22"
    const val DEFAULT_SERVER_URL = "https://oma.tailf63b9a.ts.net:8480"
    private val DEVICE_RE = Regex("^[a-z0-9][a-z0-9-]{0,62}$")
    private val URL_RE = Regex("^https?://[^/\\s]+(/\\S*)?$")

    fun valid(id: String) = DEVICE_RE.matches(id)

    fun validUrl(url: String) = URL_RE.matches(url)

    private fun prefs(context: Context) = context.getSharedPreferences("spike", Context.MODE_PRIVATE)

    fun deviceId(context: Context): String =
        prefs(context).getString("device_id", null)?.takeIf(::valid) ?: DEFAULT_DEVICE_ID

    fun setDeviceId(context: Context, id: String) {
        if (valid(id)) prefs(context).edit().putString("device_id", id).apply()
    }

    fun serverUrl(context: Context): String = prefs(context).getString("server_url", null) ?: DEFAULT_SERVER_URL

    fun setServerUrl(context: Context, url: String) {
        prefs(context).edit().putString("server_url", url).apply()
    }

    /** Empty when unset. */
    fun token(context: Context): String = prefs(context).getString("token", null) ?: ""

    fun setToken(context: Context, token: String) {
        prefs(context).edit().putString("token", token).apply()
    }
}
