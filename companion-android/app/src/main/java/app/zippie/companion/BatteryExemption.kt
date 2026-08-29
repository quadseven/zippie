package app.zippie.companion

/**
 * Whether this phone is allowed to keep relaying while the screen is off, and
 * what to say when it is not.
 *
 * WHY THIS EXISTS AT ALL. A foreground service of type `connectedDevice` is
 * SUPPOSED to be exempt from Doze and App Standby, and nothing in this app has
 * ever asked for more than that. On a Pixel with Adaptive Battery it is not
 * enough, and the failure is the worst shape available: the relay keeps
 * announcing on its timer while its UDP socket stops being serviced, so the
 * ROUTER sees a leg that is present, in the bond, and answering nothing.
 *
 * Observed live on the travel router, twice, and again on 2026-08-22 after a cold boot:
 *
 *     <phone>  port OPEN, not serviced   announcing every 15s
 *     router:  <leg>  degraded  "no reply yet - nothing is answering at
 *                                this leg's address"

 * (Addresses elided deliberately: the shape is the finding, and a literal
 * household address compiled into a shipped binary is what #156 exists to
 * stop - the operator-hosts ratchet catches it, correctly.)
 *
 * Nothing on the phone said anything was wrong. The app showed a healthy
 * relay, because from inside the process it WAS healthy - it simply was not
 * being scheduled. The whole point of this file is that a phone one setting
 * away from being a dead leg must SAY SO, on its own screen and in its report,
 * rather than leaving the router to infer it from silence.
 *
 * DELIBERATELY PURE. The Android call that answers this
 * (`PowerManager.isIgnoringBatteryOptimizations`) throws "Stub!" against the
 * stub android.jar these tests run on, so the DECISION lives here, takes a
 * Boolean, and is provable in a plain unit test. The platform read is done by
 * the caller - the same split `BootRelayDecision` uses for the same reason.
 */
sealed class BatteryExemption {

    /** Exempt, or on a device that does not restrict. Nothing to say. */
    object Granted : BatteryExemption()

    /**
     * Not exempt AND being asked to relay. This is the only combination worth
     * warning about: a phone that is not contributing does not need the
     * exemption, and telling it so would train the reader to dismiss the one
     * message on this screen that predicts an invisible outage.
     */
    data class AtRisk(val reason: String) : BatteryExemption()

    /** Not exempt and not contributing. Correct, and silent. */
    object NotNeeded : BatteryExemption()

    val isAtRisk: Boolean get() = this is AtRisk

    companion object {
        /**
         * Said in terms of the CONSEQUENCE, not the setting. "Battery
         * optimization is on" describes Android; "the router will see this
         * phone answer nothing" describes what breaks, and only the second one
         * tells a reader why they should care about a battery menu.
         */
        const val AT_RISK_REASON: String =
            "Android may freeze this relay while the screen is off. " +
                "It will keep announcing and stop answering, and the router " +
                "cannot tell that apart from a phone that left. " +
                "Set Battery to Unrestricted for this app."

        /**
         * @param isIgnoringBatteryOptimizations what PowerManager reports.
         * @param isContributing whether this phone is meant to be a leg.
         */
        fun decide(
            isIgnoringBatteryOptimizations: Boolean,
            isContributing: Boolean,
        ): BatteryExemption = when {
            isIgnoringBatteryOptimizations -> Granted
            !isContributing -> NotNeeded
            else -> AtRisk(AT_RISK_REASON)
        }
    }
}
