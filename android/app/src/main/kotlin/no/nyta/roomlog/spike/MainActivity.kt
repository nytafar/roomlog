package no.nyta.roomlog.spike

import android.Manifest
import android.content.pm.PackageManager
import android.os.Bundle
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * P2 spike: Start/Stop and a live log. Segments go to
 * `<external files dir>/spool/unsynced/` so `adb pull` can fetch them.
 * The activity stays on screen (keep-screen-on while recording); no service.
 */
class MainActivity : ComponentActivity() {
    private val lines = mutableStateListOf<String>()
    private val recording = mutableStateOf(false)
    private var recorder: SpikeRecorder? = null
    private val clock = SimpleDateFormat("HH:mm:ss.SSS", Locale.ROOT)

    private val askMic = registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if (granted) start() else log("RECORD_AUDIO denied")
    }

    private val spoolDir: File get() = File(getExternalFilesDir(null) ?: filesDir, "spool")

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MaterialTheme {
                Surface(Modifier.fillMaxSize()) {
                    Screen(
                        recording = recording.value,
                        lines = lines,
                        onStart = {
                            if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
                                start()
                            } else {
                                askMic.launch(Manifest.permission.RECORD_AUDIO)
                            }
                        },
                        onStop = ::stop,
                    )
                }
            }
        }
        log("spool: ${spoolDir.absolutePath}")
    }

    private fun start() {
        if (recorder != null) return
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        recording.value = true
        recorder = SpikeRecorder(applicationContext, spoolDir, ::log).also { it.start() }
    }

    private fun stop() {
        val r = recorder ?: return
        recorder = null
        r.stop()
        Thread {
            r.join()
            runOnUiThread {
                recording.value = false
                window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
            }
        }.start()
    }

    override fun onDestroy() {
        recorder?.stop()
        super.onDestroy()
    }

    /** Thread-safe: posts to the UI thread; also goes to logcat under tag "roomlog". */
    private fun log(msg: String) {
        android.util.Log.i("roomlog", msg)
        val line = "${clock.format(Date())} $msg"
        runOnUiThread {
            lines += line
            if (lines.size > 1000) lines.removeRange(0, lines.size - 1000)
        }
    }
}

@Composable
private fun Screen(recording: Boolean, lines: List<String>, onStart: () -> Unit, onStop: () -> Unit) {
    val listState = rememberLazyListState()
    LaunchedEffect(lines.size) {
        if (lines.isNotEmpty()) listState.animateScrollToItem(lines.size - 1)
    }
    Column(Modifier.fillMaxSize().safeDrawingPadding().padding(8.dp)) {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = onStart, enabled = !recording) { Text("Start") }
            Button(onClick = onStop, enabled = recording) { Text("Stop") }
            Text(if (recording) "recording" else "idle", Modifier.padding(12.dp))
        }
        LazyColumn(state = listState, modifier = Modifier.fillMaxSize()) {
            items(lines) { Text(it, fontFamily = FontFamily.Monospace, fontSize = 11.sp, lineHeight = 13.sp) }
        }
    }
}
