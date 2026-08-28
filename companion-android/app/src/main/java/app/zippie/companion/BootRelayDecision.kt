package app.zippie.companion

/**
 * Whether BootReceiver should start RelayService, and why - or why not.
 *
 * KEPT ANDROID-FREE ON PURPOSE. Everything else BootReceiver does - probing
 * the console over HTTP, reading device-protected storage, calling
 * startForegroundService - needs either the network or a real Context, and
 * this module's unit tests run against the stub android.jar (see
 * DataBudgetTest and RouterGuardTest for the same shape), where those calls
 * throw "Stub!" instead of running. This is the one part of the boot decision
 * that can actually be proven in that suite, so the two gates issue #54's
 * acceptance criteria name explicitly - router proximity and the data budget -
 * live here rather than inline in the receiver.
 */
sealed class BootRelayDecision {

    /** Start RelayService as a foreground service. */
    object Start : BootRelayDecision()

    /**
     * Do not start it. LOGGED rather than shown anywhere - there is no
     * screen to show it on when this fires, since the whole point is that
     * nobody is looking at the phone yet.
     */
    data class Skip(
        val reason: String,
        val retryable: Boolean = false,
        /**
         * Wait this long instead of the usual backoff, for a stand-down whose
         * cause changes on a CLOCK rather than on a probe.
         *
         * Null means "use the normal schedule", which climbs from 15s to a
         * 15-minute poll - right for a router that has not finished booting,
         * and wrong for a data cap, which will not move for hours no matter how
         * often it is asked.
         */
        val retryAfterMs: Long? = null,
    ) : BootRelayDecision()

    companion object {
        /**
         * Proximity first, budget second.
         *
         * PROXIMITY FIRST because it is the correctness gate: contributing
         * away from the router holds a cellular socket open for a bond that
         * cannot hear it (see BondMode.kt - RouterProximity), which is true
         * whether or not there is any budget left to spend on it.
         *
         * BUDGET SECOND, and still checked even when the router is right
         * there - AC5 in #54: "an unattended phone that comes back must not
         * spend a month's cellular because a counter reset." Refusing to
         * start at all is the strongest available form of "stays exhausted"
         * from this file alone; see BootConfigStore's class doc for why a
         * PARTIALLY spent budget still resets inside RelayService's own
         * ledger on this path, and what would close that the rest of the
         * way.
         */
        fun decide(
            proximity: RouterProximity,
            budgetAllowed: Boolean,
            budgetReason: String?,
        ): BootRelayDecision {
            if (proximity != RouterProximity.LOCAL) {
                // RETRYABLE, and that word is the whole of #176.
                //
                // On a cold boot this reading is not just pessimistic, it is
                // GUARANTEED pessimistic: the GL-MT3000 takes 60-90s to come up
                // and the Pixel finishes booting sooner, so it probes a console
                // that is not listening yet. Deciding once meant standing down
                // permanently while the router sat waiting for this exact phone
                // to announce - each device waiting for the other, and only the
                // router still checking.
                //
                // The rule itself is right and unchanged: do not hold a cellular
                // socket open for a bond that cannot hear it. What changes is
                // that "cannot hear it" is re-asked rather than assumed forever.
                return Skip(
                    "router proximity is $proximity, not LOCAL - contributing away " +
                        "from the router holds a cellular socket open for a bond that " +
                        "cannot hear it",
                    retryable = true,
                )
            }
            if (!budgetAllowed) {
                // RETRYABLE, SLOWLY. The old comment here said an exhausted cap
                // "does not become false because a router appeared", which is
                // true, and then concluded never to re-ask, which does not
                // follow. A cap does not move because a router appeared - it
                // moves because the BILLING PERIOD ROLLS OVER, and nothing was
                // re-asking. A phone that stood down on budget stayed down until
                // a human opened the app, however many days later the cap reset.
                //
                // Exactly the reasoning error #176 fixed one branch above: a
                // permanent decision taken on a reading guaranteed to change.
                //
                // The old comment's real point survives as the CADENCE. Re-asking
                // costs a local counter read (free) plus a console probe over
                // wifi (free, and it is the same probe the proximity branch
                // already makes), but it also wakes the radio, so it happens
                // hourly rather than on the 15-second boot ladder. An hour is
                // small against a monthly cap and still returns the leg on its
                // own, which is the whole point.
                return Skip(
                    budgetReason ?: "data budget already exhausted",
                    retryable = true,
                    retryAfterMs = BUDGET_RECHECK_MS,
                )
            }
            return Start
        }

        /**
         * How long to wait before asking again. NEVER null - it does not give up.
         *
         * IT USED TO GIVE UP AFTER ~20 MINUTES, and that was wrong for this
         * device. Observed 2026-08-16, from the phone's own log:
         *
         *   RETRY: not starting - router proximity is UNREACHABLE, not LOCAL
         *   RETRY: giving up after 10 attempts - the router is absent, not late
         *
         * The retry itself worked exactly as designed. The GIVE-UP was the
         * defect: the router had hung on a reboot and was gone for 76 minutes,
         * which is longer than the window - and "longer than 20 minutes" is not
         * the same as "absent". A relay phone in a car has nobody to tap it
         * afterwards, so a permanent stand-down is a permanent outage.
         *
         * THE PROBE IS FREE WHEN THERE IS NOTHING TO PROBE. It goes over the
         * wifi network specifically (#168), so with no wifi at all
         * WifiRoute.open returns null and the attempt costs no radio and no
         * traffic - it fails locally in microseconds. The cellular-socket cost
         * the proximity gate exists to prevent is in RELAYING, not in asking,
         * and this only ever asks.
         *
         * So it backs off to a slow poll and stays there. Once every 15 minutes,
         * forever, is cheaper than one person driving somewhere to tap a button.
         */
        /**
         * How long to leave a budget stand-down before re-asking.
         *
         * Not the period boundary itself, deliberately. The boundary is knowable
         * - `usage_period_start` is published - but a phone that sleeps through
         * a single alarm would then wait a whole further period, and an alarm
         * set weeks out is exactly the kind nothing guarantees. An hourly poll
         * cannot miss a rollover by more than an hour and needs no clock
         * arithmetic to be right.
         */
        const val BUDGET_RECHECK_MS = 3_600_000L

        fun retryDelayMs(attempt: Int): Long? = when {
            attempt < 1 -> null          // attempt 0 is not a retry
            attempt <= 2 -> 15_000L      // 15s, 30s   - router still POSTing
            attempt <= 4 -> 30_000L      // 60s, 90s   - the usual boot window
            attempt <= 6 -> 60_000L      // 2.5m, 3.5m
            attempt <= 9 -> 300_000L     // 8.5m, 13.5m, 18.5m
            else -> 900_000L             // every 15 minutes, indefinitely
        }
    }
}
