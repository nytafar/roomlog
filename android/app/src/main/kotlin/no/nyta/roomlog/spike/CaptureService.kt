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
import android.os.SystemClock
import no.nyta.roomlog.core.ClockOffset
import no.nyta.roomlog.core.Http
import no.nyta.roomlog.core.Spool
import no.nyta.roomlog.core.UploadLoop
import no.nyta.roomlog.core.Uploader

/**
 * Foreground service of type `microphone` that owns the [SpikeRecorder] and
 * the upload thread ([UploadLoop]: `/v1/time` probes and PUTs of `pending/`).
 * With no server URL or token set it records without uploading.
 * Without it a backgrounded or screen-off app gets silence from `AudioRecord`
 * (API 28+) with no error. Started from the visible activity after the
 * microphone grant; `START_STICKY`. Stop (from the activity or the
 * notification) cuts the shutdown segment, lets the encoder finish it, then
 * leaves the foreground.
 *
 * A sticky restart after the process was killed is a background start, which
 * Android 11+ does not allow to use the microphone; the service then posts a
 * "tap to resume" notification instead of recording silence.
 *
 * Stop: the recorder cuts and spools the shutdown segment, then the upload
 * thread makes one last pass, then the service leaves the foreground.
 * Whatever is still in `pending/` uploads on the next Start.
 */
class CaptureService : Service() {
    private var recorder: SpikeRecorder? = null
    private var uploads: UploadLoop? = null
    private var uploadThread: Thread? = null
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
        if (recorder != null || uploads != null) return START_STICKY // running, or finishing its last upload pass
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
        val clock = ClockOffset()
        val loop = startUploads(clock)
        val r = SpikeRecorder(
            applicationContext, SpikeState.spoolDir(this), deviceId, SpikeState::log, clock,
            onSpooled = { loop?.wake() },
        ) { finished ->
            main.post { onRecorderFinished(finished) }
        }
        recorder = r
        SpikeState.setRecording(true)
        r.start()
        return START_STICKY
    }

    /** The upload thread, if a server URL and token are set. */
    private fun startUploads(clock: ClockOffset): UploadLoop? {
        val url = Prefs.serverUrl(this)
        val token = Prefs.token(this)
        if (token.isBlank() || !Prefs.validUrl(url)) {
            SpikeState.log("upload: no server URL or token set, recording only (segments stay in the spool)")
            return null
        }
        val http = try {
            Http(url, token)
        } catch (e: IllegalArgumentException) {
            SpikeState.log("upload: ${e.message}; recording only")
            return null
        }
        val spool = Spool(SpikeState.spoolDir(this), minFreeFraction = 0.002)
        val loop = UploadLoop(
            Uploader(spool, http::put, idlePollS = 60.0),
            clock,
            fetchServerUtcNs = http::serverUtcNs,
            monoNow = SystemClock::elapsedRealtimeNanos,
            log = SpikeState::log,
            realNow = { System.currentTimeMillis() * 1_000_000 },
        )
        SpikeState.log("upload: to $url")
        uploads = loop
        uploadThread = Thread({
            loop.run()
            SpikeState.log("upload: stopped")
            main.post { onUploadsFinished(loop) }
        }, "roomlog-upload").apply { start() }
        return loop
    }

    private fun onRecorderFinished(r: SpikeRecorder) {
        if (recorder !== r) return
        recorder = null
        // the UI stays in "recording" (Start disabled) until the last upload pass is done,
        // so a new run never starts a second upload thread on the same spool
        val loop = uploads
        if (loop != null) {
            loop.stop() // one last pass, then onUploadsFinished
        } else {
            leave()
        }
    }

    private fun onUploadsFinished(loop: UploadLoop) {
        if (uploads !== loop) return
        uploads = null
        uploadThread = null
        if (recorder == null) leave()
    }

    private fun leave() {
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
        uploads?.stop()
        uploadThread?.join(3_000)
        uploads = null
        uploadThread = null
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
