package app.zippie.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * RelayStats.summary is the exact call site the defect lived in: it feeds
 * both the foreground-service notification (RelayService.publish) and, until
 * this change, the top line of the relay section on StatusScreen.
 *
 * THE DEFECT. It used to be:
 *
 *     upDatagrams == 0L && downDatagrams == 0L -> "Listening..."
 *     else -> "Carrying for the bond"
 *
 * Counters never decrease, so a single packet an hour ago read "Carrying for
 * the bond" forever, whether or not the router was still there.
 */
class RelayStatsTest {

    @Test
    fun `one packet an hour ago does not summarise as carrying`() {
        val now = System.currentTimeMillis()
        val stats = RelayStats(
            listening = true,
            cellularReady = true,
            upDatagrams = 1,
            lastRouterInboundAtMs = now - 3_600_000L,
        )

        assertNotEquals("Carrying for the bond", stats.summary)
        assertNotEquals("This phone's cellular is part of the bond.", stats.summary)
        assertEquals(RelayVerdict.evaluate(RelayReport(stats, now)).detail(), stats.summary)
        assertTrue(stats.summary, stats.summary.contains("stopped sending"))
    }

    @Test
    fun `never dialled does not claim a connection`() {
        val stats = RelayStats(listening = true, cellularReady = true)

        // "Your zippie router", not "The router": #44's follow-up was that the
        // bare phrase reads as the WIFI router this phone is joined to, which is
        // a different box. The default stands in until a real name is known.
        assertEquals("Your zippie router has not sent anything to this phone yet.", stats.summary)
    }

    @Test
    fun `recent inbound and forwarded traffic summarises as carrying`() {
        val now = System.currentTimeMillis()
        val stats = RelayStats(
            listening = true,
            cellularReady = true,
            upDatagrams = 12,
            lastRouterInboundAtMs = now - 1_000L,
        )

        assertEquals("This phone's cellular is part of the bond.", stats.summary)
    }
}
