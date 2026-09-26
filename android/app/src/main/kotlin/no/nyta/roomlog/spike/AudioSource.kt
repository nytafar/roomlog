package no.nyta.roomlog.spike

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.PackageManager
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.AudioTimestamp
import android.media.MediaRecorder
import android.os.Handler
import android.os.Looper

/**
 * `AudioRecord` at 16 kHz mono PCM16. Source `UNPROCESSED` when the device
 * declares support, else `VOICE_RECOGNITION` (both skip the call-oriented
 * AGC/NS chain). A 2 s buffer makes real overruns rare; the timeline's
 * lateness rule catches the rest.
 */
class AudioSource(private val context: Context, private val log: (String) -> Unit) {
    val rate = 16000
    var sourceName = "?"
        private set
    private var record: AudioRecord? = null
    private val ts = AudioTimestamp()

    /** Set by the routing callback; the capture loop turns it into a new epoch. */
    @Volatile
    var routingChanged = false

    @SuppressLint("MissingPermission") // checked on the first line
    fun open() {
        check(context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
            "RECORD_AUDIO not granted"
        }
        val am = context.getSystemService(AudioManager::class.java)
        val unprocessed = am.getProperty(AudioManager.PROPERTY_SUPPORT_AUDIO_SOURCE_UNPROCESSED) == "true"
        val source = if (unprocessed) MediaRecorder.AudioSource.UNPROCESSED else MediaRecorder.AudioSource.VOICE_RECOGNITION
        sourceName = if (unprocessed) "UNPROCESSED" else "VOICE_RECOGNITION"
        val minBuf = AudioRecord.getMinBufferSize(rate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val bufBytes = maxOf(minBuf * 2, 2 * rate * 2) // two seconds
        val r = AudioRecord.Builder()
            .setAudioSource(source)
            .setAudioFormat(
                AudioFormat.Builder()
                    .setSampleRate(rate)
                    .setChannelMask(AudioFormat.CHANNEL_IN_MONO)
                    .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                    .build(),
            )
            .setBufferSizeInBytes(bufBytes)
            .build()
        check(r.state == AudioRecord.STATE_INITIALIZED) { "AudioRecord not initialised (source $sourceName)" }
        r.addOnRoutingChangedListener({ routingChanged = true }, Handler(Looper.getMainLooper()))
        r.startRecording()
        record = r
        val dev = r.routedDevice
        log(
            "AudioRecord: source=$sourceName rate=${r.sampleRate} minBuf=$minBuf buf=${r.bufferSizeInFrames} frames " +
                "routed=${dev?.productName} type=${dev?.type} id=${dev?.id}",
        )
    }

    /** Blocking read of up to `buf.size` samples; negative values are AudioRecord error codes. */
    fun read(buf: ShortArray): Int = record!!.read(buf, 0, buf.size, AudioRecord.READ_BLOCKING)

    /** `(framePosition, nanoTime)` on the BOOTTIME clock, or null if unsupported right now. */
    fun timestamp(): Pair<Long, Long>? {
        val r = record ?: return null
        return if (r.getTimestamp(ts, AudioTimestamp.TIMEBASE_BOOTTIME) == AudioRecord.SUCCESS) {
            ts.framePosition to ts.nanoTime
        } else {
            null
        }
    }

    fun close() {
        record?.let {
            try {
                it.stop()
            } catch (_: IllegalStateException) {
            }
            it.release()
        }
        record = null
    }
}
