package no.nyta.roomlog.spike

import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/**
 * P2 spike screen: device id, Start/Stop and the live log. Recording runs in
 * [CaptureService] (foreground, type microphone), so it survives the activity
 * being recreated, backgrounded or the screen turning off; this screen only
 * starts and stops it and shows [SpikeState].
 */
class MainActivity : ComponentActivity() {
    private val askPermissions = registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { res ->
        if (res[Manifest.permission.RECORD_AUDIO] == true) {
            startCapture()
        } else {
            SpikeState.log("RECORD_AUDIO denied")
        }
        if (res[NOTIFICATIONS] == false) SpikeState.log("notifications denied: recording still works, the notification is hidden")
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (SpikeState.lines.isEmpty()) SpikeState.log("spool: ${SpikeState.spoolDir(this).absolutePath}")
        setContent {
            MaterialTheme {
                Surface(Modifier.fillMaxSize()) {
                    var deviceId by remember { mutableStateOf(Prefs.deviceId(this)) }
                    Screen(
                        recording = SpikeState.recording.value,
                        lines = SpikeState.lines,
                        deviceId = deviceId,
                        onDeviceId = {
                            deviceId = it
                            Prefs.setDeviceId(this, it)
                        },
                        onStart = ::requestStart,
                        onStop = { CaptureService.stop(this) },
                    )
                }
            }
        }
    }

    private fun requestStart() {
        val wanted = buildList {
            if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
                add(Manifest.permission.RECORD_AUDIO)
            }
            if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
                add(NOTIFICATIONS)
            }
        }
        if (wanted.isEmpty()) startCapture() else askPermissions.launch(wanted.toTypedArray())
    }

    private fun startCapture() {
        CaptureService.start(this, Prefs.deviceId(this))
    }

    private companion object {
        const val NOTIFICATIONS = "android.permission.POST_NOTIFICATIONS"
    }
}

@Composable
private fun Screen(
    recording: Boolean,
    lines: List<String>,
    deviceId: String,
    onDeviceId: (String) -> Unit,
    onStart: () -> Unit,
    onStop: () -> Unit,
) {
    val listState = rememberLazyListState()
    LaunchedEffect(lines.size) {
        if (lines.isNotEmpty()) listState.scrollToItem(lines.size - 1)
    }
    val valid = Prefs.valid(deviceId)
    Column(Modifier.fillMaxSize().safeDrawingPadding().padding(8.dp)) {
        OutlinedTextField(
            value = deviceId,
            onValueChange = { onDeviceId(it.trim().lowercase()) },
            label = { Text(if (valid) "device id" else "device id: a-z, 0-9 and -, max 63") },
            isError = !valid,
            enabled = !recording,
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.padding(vertical = 4.dp)) {
            Button(onClick = onStart, enabled = !recording && valid) { Text("Start") }
            Button(onClick = onStop, enabled = recording) { Text("Stop") }
            Text(if (recording) "recording (foreground service)" else "idle", Modifier.padding(12.dp))
        }
        LazyColumn(state = listState, modifier = Modifier.fillMaxSize()) {
            items(lines) { Text(it, fontFamily = FontFamily.Monospace, fontSize = 11.sp, lineHeight = 13.sp) }
        }
    }
}
