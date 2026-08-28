package app.zippie.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The chart's arithmetic, which is the whole of the chart worth proving.
 *
 * There is no Android SDK on the machine this was written on, so the maths was
 * deliberately kept out of the Compose file and into plain Kotlin - and these
 * are the assertions that make that split pay for itself.
 */
class BondThroughputTest {

    private val minute = 60_000L

    private fun leg(
        name: String,
        tx: Long? = 0,
        rx: Long? = 0,
        carrying: Boolean = true,
        label: String? = null,
    ) = BondThroughput.LegSample(name, label, tx, rx, carrying)

    private fun snap(atMs: Long, vararg legs: BondThroughput.LegSample) =
        BondThroughput.Snapshot(atMs, legs.toList())

    /** One second apart, 125,000 bytes moved: 1,000,000 bits per second. */
    @Test
    fun `a rate is the byte difference over the time between two polls`() {
        val chart = BondThroughput.chart(
            listOf(
                snap(1_000, leg("hotspot", tx = 0, rx = 0)),
                snap(2_000, leg("hotspot", tx = 25_000, rx = 100_000)),
            ),
            maxIntervalMs = minute,
        )
        assertEquals(1, chart.bars.size)
        assertEquals(1_000_000.0, chart.bars[0].total, 0.001)
        assertEquals(1_000_000.0, chart.peakBps, 0.001)
        assertEquals("1.0 Mbps", Fmt.rate(chart.peakBps))
    }

    @Test
    fun `the top edge of a bar is the sum of its legs`() {
        val chart = BondThroughput.chart(
            listOf(
                snap(0, leg("a"), leg("b")),
                snap(1_000, leg("a", tx = 1_000, rx = 0), leg("b", tx = 0, rx = 3_000)),
            ),
            maxIntervalMs = minute,
        )
        val bar = chart.bars.single()
        assertEquals(2, bar.slices.size)
        assertEquals(8_000.0 + 24_000.0, bar.total, 0.001)
    }

    /**
     * THE KEEPALIVE INCIDENT. A companion leg whose phone has left still
     * receives a probe every 500ms - real bytes on a real socket, charted as
     * ~50 kbps - so a black hole appeared to be contributing.
     */
    @Test
    fun `a leg outside the bond contributes nothing however many bytes it moves`() {
        val chart = BondThroughput.chart(
            listOf(
                snap(0, leg("gone", tx = 0, rx = 0, carrying = false)),
                snap(1_000, leg("gone", tx = 6_250, rx = 0, carrying = false)),
            ),
            maxIntervalMs = minute,
        )
        assertTrue("keepalives are not throughput", chart.bars.single().slices.isEmpty())
        assertFalse(chart.hasTraffic)
        // Measured, though: we watched, and nothing the bond can use moved.
        assertTrue(chart.bars.single().measured)
    }

    /**
     * A degraded leg that is still carrying belongs in the chart. Carrying and
     * health are orthogonal - see the note on Leg.isCarrying - and a chart that
     * dropped a struggling-but-working leg would show a total lower than the
     * bond is actually delivering.
     */
    @Test
    fun `a carrying leg is charted whatever its health`() {
        val chart = BondThroughput.chart(
            listOf(
                snap(0, leg("degraded-but-working")),
                snap(1_000, leg("degraded-but-working", tx = 12_500, rx = 0)),
            ),
            maxIntervalMs = minute,
        )
        assertEquals(100_000.0, chart.peakBps, 0.001)
    }

    /**
     * THE ONE HAZARD THIS PORT HAS THAT iOS DOES NOT. iOS reads rates the
     * router already computed; this reads counters and subtracts. A router
     * restart resets link_tx_bytes to zero, and a naive subtraction turns that
     * into a negative bar.
     */
    @Test
    fun `a counter that goes backwards is a restart, not a negative rate`() {
        val chart = BondThroughput.chart(
            listOf(
                snap(0, leg("hotspot", tx = 900_000, rx = 900_000)),
                snap(1_000, leg("hotspot", tx = 12, rx = 40)),
            ),
            maxIntervalMs = minute,
        )
        val bar = chart.bars.single()
        assertTrue(bar.slices.isEmpty())
        assertEquals(0.0, bar.total, 0.0)
        assertFalse("a restart is a hole in the record, not a quiet minute", bar.measured)
    }

    @Test
    fun `an unmeasured counter is not a zero`() {
        val chart = BondThroughput.chart(
            listOf(
                snap(0, leg("mystery", tx = null, rx = null)),
                snap(1_000, leg("mystery", tx = null, rx = null)),
            ),
            maxIntervalMs = minute,
        )
        val bar = chart.bars.single()
        assertTrue(bar.slices.isEmpty())
        // No reading and no reset: we watched a leg the router says nothing
        // about, which is not the same as the router having gone away.
        assertTrue(bar.measured)
    }

    /**
     * A phone whose screen was off for an hour comes back with two readings an
     * hour apart. The byte difference is real; the SHAPE of that hour was never
     * observed, and one bar the same width as a five-second one claims it was.
     */
    @Test
    fun `an interval longer than the limit is a gap, not an average`() {
        val chart = BondThroughput.chart(
            listOf(
                snap(0, leg("hotspot", tx = 0, rx = 0)),
                snap(60 * minute, leg("hotspot", tx = 5_000_000_000, rx = 0)),
            ),
            maxIntervalMs = 20_000,
        )
        val bar = chart.bars.single()
        assertFalse(bar.measured)
        assertTrue(bar.slices.isEmpty())
        assertFalse(chart.hasTraffic)
    }

    @Test
    fun `nothing to draw says which kind of nothing it is`() {
        assertEquals(
            "No history from the router yet.",
            BondThroughput.chart(emptyList(), minute).emptyMessage,
        )
        val quiet = BondThroughput.chart(
            listOf(snap(0, leg("a")), snap(1_000, leg("a"))),
            maxIntervalMs = minute,
        )
        assertFalse(quiet.hasTraffic)
        assertEquals("No traffic across the bond in this window.", quiet.emptyMessage)
    }

    /** The legend lists what actually drew pixels. An entry for a leg that
     *  contributed nothing sends someone hunting for a colour that is not there. */
    @Test
    fun `only legs that carried something appear in the legend`() {
        val chart = BondThroughput.chart(
            listOf(
                snap(0, leg("busy"), leg("quiet")),
                snap(1_000, leg("busy", tx = 1_000), leg("quiet")),
            ),
            maxIntervalMs = minute,
        )
        assertEquals(listOf("busy", "quiet"), chart.order)
        assertEquals(listOf("busy"), chart.carrying)
    }

    /** The chart keys by the router's internal name; every screen around it
     *  shows the human label, and a legend reading "companion-co-operator" under a row
     *  reading "Co-operator iPhone" makes the two look like different things. */
    @Test
    fun `a leg is labelled with the name a human gave it`() {
        val chart = BondThroughput.chart(
            listOf(
                snap(0, leg("companion-x", label = "A Phone")),
                snap(1_000, leg("companion-x", tx = 1_000, label = "A Phone")),
            ),
            maxIntervalMs = minute,
        )
        assertEquals("A Phone", chart.label("companion-x"))
        // A leg the router never labelled falls back to its internal name -
        // which is ugly and correct. Inventing a prettier one would put a name
        // on the legend that appears nowhere else in the system.
        assertEquals("companion-y", chart.label("companion-y"))
    }

    @Test
    fun `the newest poll decides the order and a departed leg keeps its place`() {
        val chart = BondThroughput.chart(
            listOf(
                snap(0, leg("wifi"), leg("dongle")),
                snap(1_000, leg("wifi"), leg("dongle")),
                snap(2_000, leg("wifi"), leg("hotspot")),
            ),
            maxIntervalMs = minute,
        )
        assertEquals(listOf("wifi", "hotspot", "dongle"), chart.order)
    }

    @Test
    fun `the summary a screen reader hears names the peak`() {
        val chart = BondThroughput.chart(
            listOf(
                snap(0, leg("a")),
                snap(1_000, leg("a", tx = 25_000, rx = 100_000)),
            ),
            maxIntervalMs = minute,
        )
        assertEquals(
            "Throughput over time. 1 link carrying, peak 1.0 Mbps.",
            chart.accessibilitySummary,
        )
    }

    // ---- the rolling window ------------------------------------------------

    @Test
    fun `the window holds a bounded number of polls`() {
        var history = ThroughputHistory(window = 3, maxIntervalMs = minute)
        for (i in 0..9) history = history.plus(snap(i * 1_000L, leg("a", tx = i * 1_000L)))
        assertEquals(3, history.snapshots.size)
        assertEquals(9_000L, history.snapshots.last().atMs)
        assertEquals(2, history.chart().bars.size)
    }

    /**
     * A clock that steps backwards makes every interval spanning the step
     * arithmetic rather than measurement. Dropping the sample instead would
     * wedge the chart until the clock caught up - wrong, and it looks fine.
     */
    @Test
    fun `a stamp that does not advance restarts the window`() {
        val history = ThroughputHistory(maxIntervalMs = minute)
            .plus(snap(10_000, leg("a")))
            .plus(snap(11_000, leg("a")))
            .plus(snap(5_000, leg("a")))
        assertEquals(1, history.snapshots.size)
        assertEquals(5_000L, history.snapshots.single().atMs)
        assertEquals("No history from the router yet.", history.chart().emptyMessage)
    }

    @Test
    fun `a leg seen for the first time has no interval to measure`() {
        val chart = ThroughputHistory(maxIntervalMs = minute)
            .plus(snap(0, leg("a", tx = 1_000)))
            .plus(snap(1_000, leg("a", tx = 1_000), leg("new", tx = 999_999)))
            .chart()
        assertTrue(
            "a leg's first sighting is a starting point, not a burst",
            chart.bars.single().slices.isEmpty(),
        )
    }
}
