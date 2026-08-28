package app.zippie.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The three states a traffic bar has to be able to draw, and the reason the
 * third one exists.
 *
 * The bug this guards against is a rendering one that iOS shipped twice: a leg
 * with zero bytes drew the "received" capsule across the FULL width, because it
 * has no width of its own and simply expands - so a leg that had carried
 * nothing at all rendered as a full bar. Keeping "nothing" out of the
 * proportional case entirely is what stops a layout from being able to do that.
 */
class LegTrafficTest {

    @Test
    fun `no counters at all is unmeasured, which is not zero`() {
        assertEquals(TrafficReading.Unmeasured, LegTraffic.read(null, null))
        assertEquals("traffic not measured", LegTraffic.UNMEASURED_CAPTION)
    }

    @Test
    fun `measured zero is its own state and never a proportion`() {
        assertEquals(TrafficReading.Nothing, LegTraffic.read(0, 0))
    }

    @Test
    fun `a leg that has carried something splits by direction`() {
        val split = LegTraffic.read(300, 100) as TrafficReading.Split
        assertEquals(0.75f, split.upFraction, 0.0001f)
        assertEquals("0 KB sent, 0 KB received", LegTraffic.caption(split))
    }

    /**
     * One direction measured and the other not is still a bar, because the leg
     * demonstrably carried something - but the caption must not turn the
     * missing half into a zero.
     */
    @Test
    fun `a half-reported leg draws, and says which half is missing`() {
        val split = LegTraffic.read(null, 50_000_000) as TrafficReading.Split
        assertEquals(0f, split.upFraction, 0.0001f)
        assertEquals("unknown sent, 48 MB received", LegTraffic.caption(split))
    }

    @Test
    fun `a leg that has only sent is entirely sent`() {
        val split = LegTraffic.read(1_048_576, 0) as TrafficReading.Split
        assertEquals(1f, split.upFraction, 0.0001f)
        assertTrue(LegTraffic.caption(split).startsWith("1.0 MB sent"))
    }

    /** Bytes and bits are different units and are never mixed on one row: a
     *  total is what a data cap counts, a rate is what a link is sold in. */
    @Test
    fun `rates read in bits and totals read in bytes`() {
        assertEquals("500 bps", Fmt.rate(500.0))
        assertEquals("64 kbps", Fmt.rate(64_000.0))
        assertEquals("12.5 Mbps", Fmt.rate(12_500_000.0))
        assertEquals("48 MB", Fmt.bytes(50_000_000))
    }
}
