package no.nyta.roomlog.spike

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.graphics.drawable.Icon
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper

/**
 * Foreground service of type `microphone` that owns the [SpikeRecorder].
 * Without it a backgrounded or screen-off app gets silence from `AudioRecord`
 * (API 28+) with no error. Started from the visible activity after the
 * microphone grant; `START_STICKY`. Stop (from the activity or the
 * notification) cuts the shutdown segment, lets the encoder finish it, then
 * leaves the foreground.
 *
 * A sticky restart after the process was killed is a background start, which
 * Android 11+ does not allow to use the microphone; the service then posts a
 * "tap to resume" notification instead of recording silence.
 */
class CaptureService : Service() {
    private var recorder: SpikeRecorder? = null
    private val main = Handler(Looper.getMainLooper())

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL, "Recording", NotificationManager.IMPORTANCE_LOW).apply {
                description = "Shown while the spike records"
            },
        )
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            val r = recorder
            if (r == null) {
                stopSelf()
            } else {
                SpikeState.log("stop requested: cutting the shutdown segment")
                r.stop() // onFinished leaves the foreground once the encoder is done
            }
            return START_NOT_STICKY
        }
        if (recorder != null) return START_STICKY
        val deviceId = intent?.getStringExtra(EXTRA_DEVICE_ID) ?: Prefs.deviceId(this)
        if (intent == null) SpikeState.log("service restarted by the system (START_STICKY)")
        try {
            val n = notification("Recording as $deviceId")
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                startForeground(NOTE_RECORDING, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
            } else {
                startForeground(NOTE_RECORDING, n)
            }
        } catch (e: Exception) {
            // e.g. ForegroundServiceStartNotAllowedException / SecurityException on a background restart
            SpikeState.log("cannot record from the background ($e): posted a resume notification")
            postResume()
            stopSelf()
            return START_NOT_STICKY
        }
        val r = SpikeRecorder(applicationContext, SpikeState.spoolDir(this), deviceId, SpikeState::log) { finished ->
            main.post { onRecorderFinished(finished) }
        }
        recorder = r
        SpikeState.setRecording(true)
        r.start()
        return START_STICKY
    }

    private fun onRecorderFinished(r: SpikeRecorder) {
        if (recorder !== r) return
        recorder = null
        SpikeState.setRecording(false)
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    override fun onDestroy() {
        recorder?.let {
            // the system is tearing us down: cut and finish what we can
            it.stop()
            it.join(3_000)
        }
        recorder = null
        SpikeState.setRecording(false)
        super.onDestroy()
    }

    private fun openActivity(): PendingIntent = PendingIntent.getActivity(
        this, 0, Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
        PendingIntent.FLAG_IMMUTABLE,
    )

    private fun notification(text: String): Notification {
        val stop = PendingIntent.getService(
            this, 1, Intent(this, CaptureService::class.java).setAction(ACTION_STOP), PendingIntent.FLAG_IMMUTABLE,
        )
        return Notification.Builder(this, CHANNEL)
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setContentTitle("roomlog spike")
            .setContentText(text)
            .setOngoing(true)
            .setContentIntent(openActivity())
            .addAction(
                Notification.Action.Builder(
                    Icon.createWithResource(this, android.R.drawable.ic_media_pause), "Stop", stop,
                ).build(),
            )
            .build()
    }

    private fun postResume() {
        val n = Notification.Builder(this, CHANNEL)
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setContentTitle("roomlog spike stopped")
            .setContentText("Recording was interrupted. Tap to open and press Start.")
            .setContentIntent(openActivity())
            .setAutoCancel(true)
            .build()
        getSystemService(NotificationManager::class.java).notify(NOTE_RESUME, n)
    }

    companion object {
        const val ACTION_START = "no.nyta.roomlog.spike.START"
        const val ACTION_STOP = "no.nyta.roomlog.spike.STOP"
        const val EXTRA_DEVICE_ID = "device_id"
        private const val CHANNEL = "capture"
        private const val NOTE_RECORDING = 1
        private const val NOTE_RESUME = 2

        fun start(context: Context, deviceId: String) {
            context.startForegroundService(
                Intent(context, CaptureService::class.java).setAction(ACTION_START).putExtra(EXTRA_DEVICE_ID, deviceId),
            )
        }

        fun stop(context: Context) {
            context.startService(Intent(context, CaptureService::class.java).setAction(ACTION_STOP))
        }
    }
}
