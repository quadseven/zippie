package app.zippie.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The two gates issue #54's acceptance criteria name directly: router
 * proximity and the data budget. Proven here without a Context, a network
 * call, or a real Service - see BootReceiver and BootConfigStore for why that
 * split exists, and why this is the one part of the boot path this module's
 * test suite can actually run (the stub android.jar throws on real framework
 * calls, same as every other test in this package).
 */
class BootRelayDecisionTest {

    @Test
    fun `starts when the router is local and the budget allows it`() {
        val decision = BootRelayDecision.decide(
            RouterProximity.LOCAL,
            budgetAllowed = true,
            budgetReason = null,
        )
        assertEquals(BootRelayDecision.Start, decision)
    }

    /**
     * AC3: "The relay does not start when the router is unreachable
     * (RouterProximity says REMOTE or UNREACHABLE)." Contributing away from
     * the router holds a cellular socket open for a bond that cannot hear it.
     */
    @Test
    fun `does not start when the router is remote`() {
        val decision = BootRelayDecision.decide(
            RouterProximity.REMOTE,
            budgetAllowed = true,
            budgetReason = null,
        )
        assertTrue(decision is BootRelayDecision.Skip)
        assertTrue(
            "the reason should name what was actually wrong",
            (decision as BootRelayDecision.Skip).reason.contains("REMOTE"),
        )
    }

    @Test
    fun `does not start when the router is unreachable`() {
        val decision = BootRelayDecision.decide(
            RouterProximity.UNREACHABLE,
            budgetAllowed = true,
            budgetReason = null,
        )
        assertTrue(decision is BootRelayDecision.Skip)
        assertTrue((decision as BootRelayDecision.Skip).reason.contains("UNREACHABLE"))
    }

    /**
     * AC5: an already-exhausted budget must not be spent again just because
     * the process restarted. Refusing to start at all is the strongest form
     * of "stays exhausted" available from this decision alone - see
     * BootConfigStore's class doc for the residual gap this does not close
     * (a PARTIALLY spent budget still resets inside RelayService's own
     * ledger on this path).
     */
    @Test
    fun `does not start when the budget is already exhausted, even on the router's own network`() {
        val decision = BootRelayDecision.decide(
            RouterProximity.LOCAL,
            budgetAllowed = false,
            budgetReason = "Monthly data cap reached (500 MB of 500 MB). Relaying resumes next month.",
        )
        assertTrue(decision is BootRelayDecision.Skip)
        assertEquals(
            "Monthly data cap reached (500 MB of 500 MB). Relaying resumes next month.",
            (decision as BootRelayDecision.Skip).reason,
        )
    }

    /** A missing reason must not produce a blank explanation - there is
     *  nobody at the phone to ask, so the log line is the only record. */
    @Test
    fun `a missing budget reason still explains itself`() {
        val decision = BootRelayDecision.decide(
            RouterProximity.LOCAL,
            budgetAllowed = false,
            budgetReason = null,
        )
        assertEquals(
            "data budget already exhausted",
            (decision as BootRelayDecision.Skip).reason,
        )
    }

    /** Proximity is checked FIRST: a phone away from the router should say
     *  so, not blame a budget that was never the reason it did not start. */
    @Test
    fun `proximity is reported even when the budget would also have refused`() {
        val decision = BootRelayDecision.decide(
            RouterProximity.REMOTE,
            budgetAllowed = false,
            budgetReason = "spent",
        )
        assertTrue((decision as BootRelayDecision.Skip).reason.contains("REMOTE"))
    }

    // ------------------------------------------- the boot race (#176)

    /**
     * THE COLD-BOOT DEADLOCK, pinned.
     *
     * Power both devices on together and the GL-MT3000 takes 60-90s to come up
     * while the Pixel finishes sooner. The phone probes a console that is not
     * listening yet, gets UNREACHABLE, and used to stand down PERMANENTLY -
     * while the router sat waiting for that exact phone to announce so it could
     * have an uplink at all. Each waiting for the other, only one re-checking.
     *
     * Verified on the device 2026-08-16: `dumpsys activity services` showed no
     * ServiceRecord hours after a cold-boot attempt.
     */
    @Test
    fun `an unreachable router at boot is retryable, because it may just be booting`() {
        val decision = BootRelayDecision.decide(
            RouterProximity.UNREACHABLE,
            budgetAllowed = true,
            budgetReason = null,
        )
        assertTrue(
            "a router that has not finished booting must not settle the question forever",
            (decision as BootRelayDecision.Skip).retryable,
        )
    }

    @Test
    fun `a remote router is also retryable - the phone may simply come home`() {
        val decision = BootRelayDecision.decide(
            RouterProximity.REMOTE,
            budgetAllowed = true,
            budgetReason = null,
        )
        assertTrue((decision as BootRelayDecision.Skip).retryable)
    }

    /**
     * IT MUST RE-ASK. This test previously asserted the opposite (zippie#256).
     *
     * The old assertion carried the reasoning "an exhausted cap does not become
     * false because a router appeared, and re-probing would spend the radio to
     * re-learn something already known". The first clause is true. The
     * conclusion does not follow from it: a cap does not move because a router
     * appeared, it moves because the BILLING PERIOD ROLLS OVER, and nothing was
     * re-asking. A phone that stood down on budget stayed down until a human
     * opened the app, however many days later the cap reset.
     *
     * That is the same error #176 corrected one branch above, and the same shape
     * as the give-up bound below: a permanent decision taken on a reading that is
     * guaranteed to change.
     *
     * The old reasoning's real content survives as the CADENCE, not as a refusal
     * - see `a budget stand-down waits on its own clock`.
     */
    @Test
    fun `an exhausted budget is retryable, because a cap resets on a clock`() {
        val decision = BootRelayDecision.decide(
            RouterProximity.LOCAL,
            budgetAllowed = false,
            budgetReason = "spent",
        )
        assertTrue((decision as BootRelayDecision.Skip).retryable)
    }

    /**
     * IT MUST NOT GIVE UP. This test previously asserted the opposite, and the
     * device proved that assertion wrong on 2026-08-16:
     *
     *   RETRY: giving up after 10 attempts - the router is absent, not late
     *
     * The router had hung on a reboot and was gone for 76 minutes. "Longer than
     * 20 minutes" is not "absent", and a relay phone in a car has nobody to tap
     * it afterwards - so a permanent stand-down is a permanent outage.
     *
     * The old bound existed to stop a phone far from the router waking its radio
     * forever. That cost is not real here: the probe goes over WIFI (#168), so
     * with no wifi it fails locally in microseconds and touches no radio. The
     * expensive thing is RELAYING away from the router, and this only ever asks.
     */
    @Test
    fun `the retry backs off but never gives up`() {
        val first = BootRelayDecision.retryDelayMs(1)!!
        assertTrue("first retry must land inside the router's boot time", first <= 30_000L)

        val later = BootRelayDecision.retryDelayMs(6)!!
        assertTrue("delays must grow, not stay flat", later > first)

        assertEquals("attempt 0 is not a retry", null, BootRelayDecision.retryDelayMs(0))

        // The point of the whole change: far past the old cutoff, still asking.
        for (n in intArrayOf(10, 50, 1_000, 100_000)) {
            val d = BootRelayDecision.retryDelayMs(n)
            assertTrue("gave up at attempt $n - a hung router is not an absent one", d != null)
            assertTrue("attempt $n backs off to a slow poll, not a busy loop", d!! >= 300_000L)
        }
    }

    /** A slow poll, not a vigil: bounded ABOVE so it cannot become a busy loop. */
    @Test
    fun `the steady-state interval is slow enough to be free`() {
        val steady = BootRelayDecision.retryDelayMs(1_000)!!
        assertTrue("steady-state polling faster than 5 min is wasteful", steady >= 300_000L)
        assertTrue("steady-state slower than an hour is too sleepy to recover", steady <= 3_600_000L)
    }


    // ------------------------------------------------------- zippie#256 ----
    //
    // A budget stand-down used to be permanent. The comment justifying that
    // said an exhausted cap "does not become false because a router appeared"
    // - true - and concluded never to re-ask, which does not follow. A cap
    // moves when the BILLING PERIOD ROLLS OVER, and nothing was re-asking, so
    // the leg stayed down until a human opened the app days later.

    @Test
    fun `a budget stand-down waits on its own clock, not the boot ladder`() {
        val skip = BootRelayDecision.decide(
            proximity = RouterProximity.LOCAL,
            budgetAllowed = false,
            budgetReason = "monthly cap reached",
        ) as BootRelayDecision.Skip

        assertEquals(BootRelayDecision.BUDGET_RECHECK_MS, skip.retryAfterMs)
        // The ladder starts at 15s and tops out at 15m. Climbing it toward a
        // data cap just wakes the radio to re-read a number that cannot have
        // moved.
        assertTrue(
            "a budget recheck must be far slower than the boot ladder",
            skip.retryAfterMs!! > BootRelayDecision.retryDelayMs(99)!!,
        )
    }

    @Test
    fun `a proximity stand-down keeps the fast ladder`() {
        // The two stand-downs are on different clocks and must not be merged:
        // a router that has not finished booting is worth asking about in
        // seconds, which is the whole of #176.
        val skip = BootRelayDecision.decide(
            proximity = RouterProximity.UNREACHABLE,
            budgetAllowed = true,
            budgetReason = null,
        ) as BootRelayDecision.Skip

        assertTrue(skip.retryable)
        assertEquals(null, skip.retryAfterMs)
    }

    @Test
    fun `a budget that resets while the phone sits idle lets the leg return`() {
        // AC4, as a state machine rather than a wall clock: stand down while
        // exhausted, and the moment the same question is asked again after a
        // rollover, start. The retry that asks it is armed by the first branch.
        val exhausted = BootRelayDecision.decide(
            RouterProximity.LOCAL, budgetAllowed = false, budgetReason = "cap reached",
        )
        assertTrue((exhausted as BootRelayDecision.Skip).retryable)

        val afterRollover = BootRelayDecision.decide(
            RouterProximity.LOCAL, budgetAllowed = true, budgetReason = null,
        )
        assertEquals(BootRelayDecision.Start, afterRollover)
    }
}
