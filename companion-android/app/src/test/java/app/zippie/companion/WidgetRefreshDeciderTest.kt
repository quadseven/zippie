package app.zippie.companion

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * When the widget redraws, and when it must not.
 *
 * A Glance widget redraws when we ask it to - there is no data binding. The
 * relay publishes a report every ~2s for as long as it runs, so redrawing on
 * every publish would churn the launcher and burn battery for zero new
 * information. Redrawing only when the visible content actually changed is
 * what keeps the widget cheap; redrawing on the first publish is what keeps
 * it from showing nothing until something changes.
 *
 * This pins the DECISION. The Android half (actually performing the update)
 * lives in ZippieWidgetRefresh and cannot be unit-tested; the decision can.
 */
class WidgetRefreshDeciderTest {

    private fun content(
        headline: String = "Carrying",
        detail: String = "This phone's cellular is part of the bond.",
        tone: WidgetContent.Tone = WidgetContent.Tone.LIVE,
        legs: List<WidgetContent.Leg> = emptyList(),
    ) = WidgetContent(headline, detail, tone, legs)

    @Test
    fun `first draw always refreshes`() {
        assertTrue(
            "a widget that has never drawn must draw, even if the content " +
                "somehow equals a default",
            WidgetRefreshDecider.shouldRefresh(null, content()),
        )
    }

    @Test
    fun `identical content does not refresh`() {
        val last = content()
        assertFalse(
            "redrawing the same headline, detail, tone and legs every " +
                "heartbeat is churn with no new information",
            WidgetRefreshDecider.shouldRefresh(last, content()),
        )
    }

    @Test
    fun `changed headline refreshes`() {
        val last = content()
        assertTrue(
            WidgetRefreshDecider.shouldRefresh(last, content(headline = "Off")),
        )
    }

    @Test
    fun `changed detail refreshes`() {
        val last = content()
        assertTrue(
            "the sentence carries the verdict's news; a new sentence must reach " +
                "the home screen",
            WidgetRefreshDecider.shouldRefresh(last, content(detail = "Something else.")),
        )
    }

    @Test
    fun `changed tone refreshes`() {
        val last = content()
        assertTrue(
            "the accent dot is the glanceable signal; a tone change is the " +
                "one thing a driver must not miss",
            WidgetRefreshDecider.shouldRefresh(last, content(tone = WidgetContent.Tone.DOWN)),
        )
    }

    @Test
    fun `changed legs refresh`() {
        val last = content()
        val withLeg = content(
            legs = listOf(WidgetContent.Leg("This phone", WidgetContent.Leg.State.CARRYING)),
        )
        assertTrue(
            WidgetRefreshDecider.shouldRefresh(last, withLeg),
        )
    }
}
