package no.nyta.roomlog.core

/**
 * Starts threads whose bodies run one at a time, in start order: a thread
 * started while the previous one is still alive first waits for it to end,
 * then runs its body. The client keeps one per process for the upload thread,
 * so a new run never has two upload loops on one spool, even when the service
 * that started the previous loop was destroyed while its last pass was still
 * running. The waiting thread is started at once, so the caller does not block.
 */
class SerialThreads(private val name: String) {
    private var last: Thread? = null

    /**
     * Starts a thread that runs [body] once the previously started thread has
     * ended. [onWait] runs on the new thread, before it waits, only if it has
     * to wait.
     */
    @Synchronized
    fun start(onWait: () -> Unit = {}, body: () -> Unit): Thread {
        val prev = last?.takeIf { it.isAlive }
        val t = Thread({
            if (prev != null) {
                onWait()
                joinUninterruptibly(prev)
            }
            body()
        }, name)
        last = t
        t.start()
        return t
    }

    private fun joinUninterruptibly(t: Thread) {
        var interrupted = false
        while (true) {
            try {
                t.join()
                break
            } catch (_: InterruptedException) {
                interrupted = true
            }
        }
        if (interrupted) Thread.currentThread().interrupt()
    }
}
