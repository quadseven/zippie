package app.zippie.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * zippie#180 AC3: a phone standing down on budget must say so on its own
 * screen, with the reason and when it will reconsider. The durable record
 * (#181) established THAT a stand-down happened; this is the display contract
 * for it: a pure function of the stand-down, so it is provable here without a
 * Context, a screen, or a real alarm.
 */
class BootStanddownTest {

    @Test
    fun `a budget stand-down names the reason and when it will reconsider`() {
        val s = BootStanddown(
            reason = "Monthly data cap reached (500 MB of 500 MB).",
            retryArmed = true,
            retryAfterMs = 3_600_000L,
            stoodDownAtMs = 1_000_000L,
        )
        val line = s.displayLine(nowMs = 1_000_000L)
        assertTrue("must name the reason, was: $line", line.contains("Monthly data cap reached"))
        assertTrue("must say when it rechecks, was: $line", line.contains("about an hour"))
    }

    @Test
    fun `the reconsider time counts down from the stand-down, not from now`() {
        val s = BootStanddown(
            reason = "spent",
            retryArmed = true,
            retryAfterMs = 3_600_000L,
            stoodDownAtMs = 1_000_000L,
        )
        // 55 minutes later: five minutes left, not another hour.
        val line = s.displayLine(nowMs = 1_000_000L + 3_300_000L)
        assertTrue("must count down, was: $line", line.contains("about 5 minutes"))
    }

    @Test
    fun `a stand-down with no retry armed says it will not check again`() {
        val s = BootStanddown(
            reason = "spent",
            retryArmed = false,
            retryAfterMs = null,
            stoodDownAtMs = 0L,
        )
        val line = s.displayLine(nowMs = 0L)
        assertTrue("was: $line", line.contains("Will not check again"))
    }

    @Test
    fun `a stand-down record survives a serialize round trip`() {
        val s = BootStanddown(
            reason = "line with | pipe and unicode \u00e9",
            retryArmed = true,
            retryAfterMs = 3_600_000L,
            stoodDownAtMs = 123L,
        )
        assertEquals(s, BootStanddown.deserialize(s.serialize()))
    }
}
