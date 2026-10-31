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
import no.nyta.roomlog.core.SerialThreads
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
 * Whatever is still in `pending/` uploads on the next Start. From the Stop
 * request on, the notification says so and has no Stop action; a further
 * Stop (the screen's button stays enabled until the pass ends) only logs.
 *
 * One upload thread per spool: the thread comes from a process-wide
 * [SerialThreads], so if this service is destroyed while a last pass is still
 * running (onDestroy waits only 3 s), the next Start records at once but its
 * upload thread waits for that pass to end before its first probe or PUT.
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
            when {
                r != null -> {
                    SpikeState.log("stop requested: cutting the shutdown segment")
                    showStatus("Stopping: closing the last segment")
                    r.stop() // onFinished leaves the foreground once the encoder is done
                }
                // already stopped and in the last upload pass: let it finish, onUploadsFinished leaves
                uploads != null -> SpikeState.log("stop: already stopping, the last upload pass is still running")
                else -> stopSelf()
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
        val loop = startUploads(clock, deviceId)
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
    private fun startUploads(clock: ClockOffset, deviceId: String): UploadLoop? {
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
            Uploader(spool, http::put, idlePollS = 60.0, deviceId = deviceId),
            clock,
            fetchServerUtcNs = http::serverUtcNs,
            monoNow = SystemClock::elapsedRealtimeNanos,
            log = SpikeState::log,
            realNow = { System.currentTimeMillis() * 1_000_000 },
        )
        SpikeState.log("upload: to $url")
        uploads = loop
        uploadThread = uploadThreads.start(
            onWait = { SpikeState.log("upload: waiting for the previous run's last upload pass to end") },
        ) {
            loop.run()
            SpikeState.log("upload: stopped")
            main.post { onUploadsFinished(loop) }
        }
        return loop
    }

    private fun onRecorderFinished(r: SpikeRecorder) {
        if (recorder !== r) return
        recorder = null
        // the UI stays in "recording" (Start disabled) until the last upload pass is done;
        // uploadThreads covers the paths around that (a destroy before the pass ends)
        val loop = uploads
        if (loop != null) {
            showStatus("Stopped: uploading what is left in the spool")
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
        uploadThread?.let {
            it.join(3_000)
            // the next Start's upload thread waits for it (uploadThreads), so it is never two at once
            if (it.isAlive) SpikeState.log("upload: last pass still running after the service ended; it finishes on its own")
        }
        uploads = null
        uploadThread = null
        SpikeState.setRecording(false)
        super.onDestroy()
    }

    private fun openActivity(): PendingIntent = PendingIntent.getActivity(
        this, 0, Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
        PendingIntent.FLAG_IMMUTABLE,
    )

    /** The ongoing notification; with the Stop action only while recording. */
    private fun notification(text: String, stopAction: Boolean = true): Notification {
        val b = Notification.Builder(this, CHANNEL)
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setContentTitle("roomlog spike")
            .setContentText(text)
            .setOngoing(true)
            .setContentIntent(openActivity())
        if (stopAction) {
            val stop = PendingIntent.getService(
                this, 1, Intent(this, CaptureService::class.java).setAction(ACTION_STOP), PendingIntent.FLAG_IMMUTABLE,
            )
            b.addAction(
                Notification.Action.Builder(
                    Icon.createWithResource(this, android.R.drawable.ic_media_pause), "Stop", stop,
                ).build(),
            )
        }
        return b.build()
    }

    /** Replaces the ongoing notification's text once capture is stopping (no Stop action). */
    private fun showStatus(text: String) {
        getSystemService(NotificationManager::class.java).notify(NOTE_RECORDING, notification(text, stopAction = false))
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

        /** Process-wide: at most one upload loop runs on the spool, across service instances. */
        private val uploadThreads = SerialThreads("roomlog-upload")

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
