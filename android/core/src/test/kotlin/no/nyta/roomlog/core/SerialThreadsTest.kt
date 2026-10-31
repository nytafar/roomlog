package no.nyta.roomlog.core

import java.util.Collections
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

/** A thread started while its predecessor is alive waits for it; bodies never overlap. */
class SerialThreadsTest {
    @Test
    fun secondWaitsForTheFirstToFinish() {
        val serial = SerialThreads("t")
        val release = CountDownLatch(1)
        val firstIn = CountDownLatch(1)
        val order = Collections.synchronizedList(mutableListOf<String>())
        val waits = AtomicInteger(0)
        val a = serial.start(onWait = { waits.incrementAndGet() }) {
            firstIn.countDown()
            release.await()
            order += "a done"
        }
        assertTrue(firstIn.await(5, TimeUnit.SECONDS))
        val secondIn = CountDownLatch(1)
        val b = serial.start(onWait = { waits.incrementAndGet() }) {
            order += "b start"
            secondIn.countDown()
        }
        // b is alive but its body must not run while a's is still running
        assertFalse(secondIn.await(200, TimeUnit.MILLISECONDS))
        assertTrue(b.isAlive)
        release.countDown()
        assertTrue(secondIn.await(5, TimeUnit.SECONDS))
        a.join(5_000)
        b.join(5_000)
        assertEquals(listOf("a done", "b start"), order)
        assertEquals(1, waits.get(), "only the second start waited")
    }

    @Test
    fun noWaitWhenThePreviousHasEnded() {
        val serial = SerialThreads("t")
        serial.start {}.join(5_000)
        var waited = false
        val ran = CountDownLatch(1)
        serial.start(onWait = { waited = true }) { ran.countDown() }.join(5_000)
        assertTrue(ran.await(0, TimeUnit.SECONDS))
        assertFalse(waited)
    }

    @Test
    fun manyStartsNeverOverlap() {
        val serial = SerialThreads("t")
        val inside = AtomicInteger(0)
        val maxInside = AtomicInteger(0)
        val done = AtomicInteger(0)
        val threads = (1..20).map {
            serial.start {
                maxInside.accumulateAndGet(inside.incrementAndGet(), ::maxOf)
                Thread.sleep(5)
                inside.decrementAndGet()
                done.incrementAndGet()
            }
        }
        threads.forEach { it.join(10_000) }
        assertEquals(20, done.get())
        assertEquals(1, maxInside.get())
    }
}
