package app.zippie.companion

/**
 * Whether an MDM-provisioned phone should begin relaying without anyone
 * tapping anything.
 *
 * THE DEFECT THIS CLOSES. `autoStartRelay` has existed for months as a
 * managed-configuration key: declared in app_restrictions.xml so an MDM can
 * set it, parsed by [ManagedConfig.autoStart], wrapped by
 * [AndroidManagedConfig.shouldAutoStart], covered by five unit tests, and
 * documented in three places as "whether the relay starts on boot".
 *
 * Nothing read it. `grep -rn shouldAutoStart app/src/` returned exactly one
 * line - its own definition. The tests passed because they test the PARSER;
 * the wiring never existed.
 *
 * The consequence, found on a real handset on 2026-08-23: a phone enrolled by
 * the MDM, installed, configured with the correct console, token and
 * `autoStartRelay=true`, sat with its relay port closed and NOTHING in logcat.
 * Not a refusal, not a Skip reason - nothing, because no code ever ran to have
 * an opinion. `MainActivity.startContributing()` is reachable only from a
 * button tap, so an unattended phone could never begin relaying at all.
 *
 * WHY A DECISION OBJECT FOR ONE BOOLEAN. Because the interesting part is not
 * the flag, it is the three things that must all be true before an app starts
 * sending a household's traffic over somebody's cellular plan without being
 * asked. Writing them as one expression in an Activity is how the last one got
 * lost.
 */
sealed class AutoStartDecision {

    /** Start relaying now, without a tap. */
    object Start : AutoStartDecision()

    /** Do nothing. [reason] is for the log, and exists because the failure this
     *  replaces was diagnosed only by noticing that NOTHING had been written. */
    data class Stand(val reason: String) : AutoStartDecision()

    val shouldStart: Boolean get() = this is Start

    companion object {
        fun decide(
            managedAutoStart: Boolean,
            alreadyRunning: Boolean,
            hasUsableConfig: Boolean,
        ): AutoStartDecision = when {
            // NOT "the user has not said no". A phone must never begin relaying
            // because a key was missing - ManagedConfig.autoStart already
            // defaults absent to false and this preserves that.
            !managedAutoStart ->
                Stand("autoStartRelay is not set; a relay only starts when asked")
            // Starting a running relay is a no-op that logs like a success, and
            // this app has already been bitten by exactly that shape.
            alreadyRunning ->
                Stand("relay is already running")
            // A relay with no console to announce to forwards into the void and
            // reports itself healthy. Refusing is louder than that.
            !hasUsableConfig ->
                Stand("no usable console configuration to announce to")
            else -> Start
        }
    }
}
