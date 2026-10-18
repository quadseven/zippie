package app.zippie.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * #181 (legacy no. 257): the on-disk boot log must distinguish "asked and
 * started" from "asked and skipped".
 *
 * BootLog.record fires for "decision started", TIMED OUT and FAILED, but the
 * outcome branches are what answer the cold-boot question - and logcat, which
 * used to carry the answer, does not survive the reboot being diagnosed. The
 * exact wording of those outcome lines is therefore pinned here, Android-free
 * like the decision itself, so a future edit cannot silently un-ask the
 * question.
 */
class BootOutcomeLogTest {

    @Test
    fun `start line names the source broadcast`() {
        val line = BootRelayDecision.outcomeLogLine(
            "LOCKED_BOOT_COMPLETED",
            BootRelayDecision.Start,
            RouterProximity.LOCAL,
        )
        assertEquals("LOCKED_BOOT_COMPLETED: started (proximity=LOCAL)", line)
    }

    @Test
    fun `skip line names the reason and that a retry is armed`() {
        val reason = "router proximity is UNREACHABLE, not LOCAL - contributing away " +
            "from the router holds a cellular socket open for a bond that cannot hear it"
        val line = BootRelayDecision.outcomeLogLine(
            "RETRY",
            BootRelayDecision.Skip(reason, retryable = true),
            RouterProximity.UNREACHABLE,
        )
        assertEquals("RETRY: stood down - $reason (retry armed)", line)
    }

    @Test
    fun `skip line without retry reads as a permanent stand-down`() {
        val line = BootRelayDecision.outcomeLogLine(
            "BOOT_COMPLETED",
            BootRelayDecision.Skip("data budget already exhausted", retryable = false),
            RouterProximity.LOCAL,
        )
        assertTrue(
            "a permanent stand-down must read differently on disk from one that will re-ask",
            line.contains("retry NOT armed"),
        )
    }

    @Test
    fun `start and skip lines are distinguishable on disk`() {
        val started = BootRelayDecision.outcomeLogLine(
            "LOCKED_BOOT_COMPLETED",
            BootRelayDecision.Start,
            RouterProximity.LOCAL,
        )
        val skipped = BootRelayDecision.outcomeLogLine(
            "LOCKED_BOOT_COMPLETED",
            BootRelayDecision.Skip("data budget already exhausted", retryable = false),
            RouterProximity.LOCAL,
        )
        assertTrue(started.contains("started"))
        assertTrue(skipped.contains("stood down"))
        assertTrue(
            "asked-and-started must not read as asked-and-skipped",
            !started.contains("stood down") && !skipped.contains("started (proximity"),
        )
    }

    @Test
    fun `no credential can reach the outcome line`() {
        // The line is built from (source, outcome, proximity) only - there is
        // no parameter a token could ride in on. This pins the actual reasons
        // decide() produces, so a future change that interpolates a credential
        // into a reason breaks here instead of on a handset.
        val tokenShaped = Regex("[0-9a-f]{32,}")
        val proximities =
            listOf(RouterProximity.LOCAL, RouterProximity.REMOTE, RouterProximity.UNREACHABLE)
        for (proximity in proximities) {
            for (budgetAllowed in listOf(true, false)) {
                val outcome = BootRelayDecision.decide(
                    proximity,
                    budgetAllowed,
                    "data budget already exhausted",
                )
                val line = BootRelayDecision.outcomeLogLine("BOOT_COMPLETED", outcome, proximity)
                assertTrue(
                    "line must not contain a token-shaped secret: $line",
                    !tokenShaped.containsMatchIn(line),
                )
                assertTrue(
                    "line must not name a token field: $line",
                    !line.contains("token="),
                )
            }
        }
    }
}
