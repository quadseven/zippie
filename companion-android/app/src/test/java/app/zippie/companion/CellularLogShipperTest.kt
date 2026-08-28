package app.zippie.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The shipper is the only thing that can report on this phone while the router
 * is dark, so its failure modes matter more than its happy path.
 *
 * WHAT IS TESTED HERE AND WHAT IS NOT. `flush()` needs a real `android.net.Network`
 * to do the one thing that makes this class worth having - `network.openConnection`,
 * which pins the POST to cellular instead of the default route - and that cannot
 * be constructed in a JVM unit test. So the QUEUE behaviour is proven here, and
 * the pinning is proven on the device the first time a cold boot reports itself.
 *
 * The queue is the half that has actually bitten: an unbounded one on a phone
 * whose uplink is down is a memory leak, and one that drops the NEWEST lines
 * throws away the end of the incident - which is the part that explains it.
 */
class CellularLogShipperTest {

    private fun shipper(token: String = "tok") = CellularLogShipper(clientToken = token)

    @Test
    fun `an event is valid json with the fields as top-level attributes`() {
        val s = shipper()
        s.event("hello", "warn", mapOf("count" to 3, "ok" to true, "who" to "router"))
        val line = drain(s).single()
        // Facetable in Datadog without parsing the message - the whole point of
        // attributes over string formatting.
        assertTrue(line, line.contains("\"message\":\"hello\""))
        assertTrue(line, line.contains("\"status\":\"warn\""))
        assertTrue(line, line.contains("\"count\":3"))
        assertTrue(line, line.contains("\"ok\":true"))
        assertTrue(line, line.contains("\"who\":\"router\""))
        assertTrue(line, line.startsWith("{") && line.endsWith("}"))
    }

    @Test
    fun `quotes and newlines cannot break the json`() {
        val s = shipper()
        s.event("he said \"no\"\nthen left\ttab", fields = mapOf("k" to "a\\b"))
        val line = drain(s).single()
        assertTrue(line, line.contains("\\\"no\\\""))
        assertTrue(line, line.contains("\\n"))
        assertTrue(line, line.contains("\\t"))
        assertTrue(line, line.contains("a\\\\b"))
        // One object, not two: an unescaped newline would have split the batch.
        assertEquals(1, line.count { it == '{' })
    }

    @Test
    fun `a control character is escaped rather than emitted raw`() {
        val s = shipper()
        s.event("bell\u0007here")
        assertTrue(drain(s).single().contains("\\u0007"))
    }

    @Test
    fun `the queue is bounded so a long outage cannot grow memory`() {
        val s = shipper()
        repeat(CellularLogShipper.MAX_QUEUE + 250) { s.event("line $it") }
        assertEquals(CellularLogShipper.MAX_QUEUE.toLong(), drain(s).size.toLong())
        assertEquals(250L, s.dropped.get())
    }

    @Test
    fun `it drops the OLDEST lines, never the newest`() {
        val s = shipper()
        repeat(CellularLogShipper.MAX_QUEUE + 10) { s.event("line $it") }
        val kept = drain(s)
        // The tail of an incident explains it; the head rarely does.
        assertTrue(kept.last(), kept.last().contains("line ${CellularLogShipper.MAX_QUEUE + 9}"))
        assertTrue(kept.first(), kept.first().contains("line 10"))
    }

    @Test
    fun `flush without a network keeps the lines instead of losing them`() {
        val s = shipper()
        s.event("before cellular exists")
        s.flush()   // no network yet - this is the cold-boot case
        assertEquals(0L, s.shipped.get())
        assertEquals(0L, s.dropped.get())
        assertEquals(
            "a report queued before cellular bound was thrown away - that is " +
                "exactly the report a cold boot needs to send",
            1,
            drain(s).size,
        )
    }

    @Test
    fun `an empty token disables shipping entirely`() {
        val s = shipper(token = "")
        s.event("nobody provisioned this device")
        s.flush()
        assertEquals(0L, s.shipped.get())
        assertEquals(0L, s.failures.get())
    }

    @Test
    fun `service and tags ride on every line so legs can be told apart`() {
        val s = CellularLogShipper(clientToken = "t", service = "zippie-companion", tags = "leg:pixel-6a-a554")
        s.event("x")
        val line = drain(s).single()
        assertTrue(line, line.contains("\"service\":\"zippie-companion\""))
        assertTrue(line, line.contains("\"ddtags\":\"leg:pixel-6a-a554\""))
        assertTrue(line, line.contains("\"ddsource\":\"android\""))
    }

    /** Reads the queue without a network, which is all a JVM test can reach.
     *
     *  Cast to the concrete queue type rather than `java.util.Collection`:
     *  Kotlin resolves `ArrayList(...)` against `kotlin.collections.Collection`,
     *  and the Java interface is not applicable to it. */
    private fun drain(s: CellularLogShipper): List<String> {
        val f = CellularLogShipper::class.java.getDeclaredField("queue")
        f.isAccessible = true
        @Suppress("UNCHECKED_CAST")
        val q = f.get(s) as java.util.concurrent.ConcurrentLinkedQueue<String>
        return q.toList()
    }
}
