package app.zippie.companion

import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Supervision, and why it reuses the retry ladder rather than adding a timer.
 *
 * `BootReceiver` was the ONLY thing that ever started the relay. Anything that
 * stopped it later - a crash, Android reclaiming the process, a wifi flap at the
 * wrong moment - left the phone powered on and doing nothing until the next
 * reboot. On a relay phone in a car that is an outage nobody can see or clear,
 * and the operator's requirement is explicit: "it should just be relaying
 * automatically, I never want to touch this android."
 *
 * The retry chain already backs off to a slow indefinite poll, so supervision is
 * the same alarm re-entered at a high attempt number.
 */
class RelaySupervisionTest {

    /**
     * Supervision must land in the SLOW steady state, not re-enter the fast boot
     * ladder - a healthy relay being re-checked every 15 seconds forever would
     * be a busy loop wearing the same clothes as a fix.
     */
    @Test
    fun `supervision polls at the slow steady-state interval`() {
        val d = BootRelayDecision.retryDelayMs(20)
        assertTrue("supervision must still be scheduled", d != null)
        assertTrue("supervision must not re-enter the fast boot ladder: ${d}ms",
            d!! >= 300_000L)
        assertTrue("supervision slower than an hour is too sleepy to recover",
            d <= 3_600_000L)
    }

    /**
     * And it must never terminate. A relay that stops being supervised after
     * some number of checks is a relay that eventually stays dead.
     */
    @Test
    fun `supervision never terminates`() {
        for (n in intArrayOf(20, 100, 10_000, 1_000_000)) {
            assertTrue("supervision gave up at attempt $n",
                BootRelayDecision.retryDelayMs(n) != null)
        }
    }
}
