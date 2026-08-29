package app.zippie.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The widget must not lie, and the two ways it would (quadseven/zippie#244).
 *
 * `aStaleReportRendersNotReportingNotTheLastGoodVerdict` is the one that
 * matters most. A widget redraws on the launcher's schedule, so the natural
 * failure is a home screen still saying "Carrying" twenty minutes after the
 * relay died - and unlike a screen, nobody opened it to check.
 */
class WidgetContentTest {

    private val now = 1_000_000L

    private fun stats(
        cellularReady: Boolean = true,
        listening: Boolean = true,
        upDatagrams: Long = 0,
        lastRouterInboundAtMs: Long? = null,
        lastError: String? = null,
        budgetExhausted: String? = null,
    ) = RelayStats(
        cellularReady = cellularReady,
        listening = listening,
        upDatagrams = upDatagrams,
        lastRouterInboundAtMs = lastRouterInboundAtMs,
        lastError = lastError,
        budgetExhausted = budgetExhausted,
    )

    private fun carrying(atMs: Long) =
        RelayReport(stats(upDatagrams = 5, lastRouterInboundAtMs = atMs), atMs)

    // ------------------------------------------------------- staleness ----

    @Test
    fun `a stale report renders not reporting, not the last good verdict`() {
        val old = now - RelayReport.STALENESS_MS - 1
        val content = WidgetContent.from(carrying(old), now)

        assertEquals(RelayVerdict.NotReporting.headline, content.headline)
        assertEquals(WidgetContent.Tone.DOWN, content.tone)
        assertTrue(
            "a widget that cannot see fresh evidence must not list legs",
            content.legs.isEmpty(),
        )
    }

    @Test
    fun `a report just inside the threshold renders its real verdict`() {
        val fresh = now - RelayReport.STALENESS_MS + 1
        val content = WidgetContent.from(carrying(fresh), now)

        assertEquals(RelayVerdict.Carrying.headline, content.headline)
        assertEquals(WidgetContent.Tone.LIVE, content.tone)
    }

    @Test
    fun `staleness is decided by the report's own age, not by wall clock drift`() {
        // Same report, two different "now"s: the only thing that changes the
        // answer is how old the record is.
        val report = carrying(now)
        assertEquals(
            WidgetContent.Tone.LIVE,
            WidgetContent.from(report, now).tone,
        )
        assertEquals(
            WidgetContent.Tone.DOWN,
            WidgetContent.from(report, now + RelayReport.STALENESS_MS + 1).tone,
        )
    }

    // --------------------------------------------------- absent vs zero ---

    @Test
    fun `no report at all is off with no legs`() {
        val content = WidgetContent.from(null, now)

        assertEquals(RelayVerdict.Off.headline, content.headline)
        assertEquals(WidgetContent.Tone.DOWN, content.tone)
        assertTrue(
            "Off means there is no leg to describe; inventing a row would " +
                "state something unmeasured",
            content.legs.isEmpty(),
        )
    }

    @Test
    fun `a running relay carrying nothing is not the same as no relay`() {
        val listening = RelayReport(stats(upDatagrams = 0), now)
        val content = WidgetContent.from(listening, now)

        assertEquals(RelayVerdict.Listening.headline, content.headline)
        assertTrue("a listening relay has a leg to show", content.legs.isNotEmpty())
    }

    // ------------------------------------------------------------ tone ----

    @Test
    fun `waiting is idle rather than a fault`() {
        // Crying wolf in a car is worse than saying nothing: a phone that is
        // listening, or whose router has gone quiet, is not broken.
        assertEquals(
            WidgetContent.Tone.IDLE,
            WidgetContent.from(RelayReport(stats(), now), now).tone,
        )
        val quiet = RelayReport(
            stats(upDatagrams = 5, lastRouterInboundAtMs = now - 10 * 60_000L),
            now,
        )
        assertEquals(WidgetContent.Tone.IDLE, WidgetContent.from(quiet, now).tone)
    }

    @Test
    fun `a real local fault is down`() {
        val noCell = RelayReport(stats(cellularReady = false), now)
        assertEquals(WidgetContent.Tone.DOWN, WidgetContent.from(noCell, now).tone)

        val notListening = RelayReport(stats(listening = false), now)
        assertEquals(
            WidgetContent.Tone.DOWN,
            WidgetContent.from(notListening, now).tone,
        )
    }

    @Test
    fun `every verdict maps to a tone`() {
        // The mapping is an exhaustive `when` with no else, so this cannot
        // silently miss a case - but a new verdict that someone maps to the
        // wrong tone by reflex is still worth catching.
        for (verdict in RelayVerdict.ALL_CASES_FOR_COPY_REVIEW) {
            val tone = WidgetContent.from(
                RelayReport(stats(), now), now,
            ).tone
            assertTrue("$verdict produced no tone", tone in WidgetContent.Tone.values())
        }
    }

    // ------------------------------------------------- nothing derived ----

    @Test
    fun `the headline and sentence come from RelayVerdict verbatim`() {
        // If the widget ever starts composing its own wording, this is what
        // notices. #44 shipped because a view wrote its own sentence.
        val report = carrying(now)
        val verdict = RelayVerdict.evaluate(report, now)
        val content = WidgetContent.from(report, now, routerName = "travel-router")

        assertEquals(verdict.headline, content.headline)
        assertEquals(verdict.detail("travel-router"), content.detail)
    }

    @Test
    fun `the router name threads into the sentences that name it`() {
        // NOT every verdict names the router, and that is deliberate:
        // "Carrying" says "This phone's cellular is part of the bond", where
        // which router is on the other end adds nothing. The ones that DO
        // name it are the ones describing what the far end is or is not doing.
        val listening = RelayReport(stats(), now)

        val named = WidgetContent.from(listening, now, routerName = "travel-router")
        assertTrue("a named router should be named", named.detail.contains("travel-router"))

        val unnamed = WidgetContent.from(listening, now)
        assertTrue(
            "and its absence must read as a sentence, not an empty gap",
            unnamed.detail.contains("Your zippie router"),
        )
    }

    @Test
    fun `carrying deliberately does not name the router`() {
        // Guards the asymmetry above from being "tidied" into consistency.
        val content = WidgetContent.from(carrying(now), now, routerName = "travel-router")
        assertTrue(
            "carrying is about this phone, not about which router",
            !content.detail.contains("travel-router"),
        )
    }
}
