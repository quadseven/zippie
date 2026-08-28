package app.zippie.companion

/**
 * How often the wake door may actually be answered.
 *
 * [WakeReceiver] is exported and deliberately unguarded - see the manifest for
 * why the obvious guard (BIND_DEVICE_ADMIN) is platform-signed and would lock
 * out the only legitimate caller. The threat that remains is not that a hostile
 * app can make zippie relay - AutoStartDecision still requires a flag only the
 * device owner can write - but that it could ring the bell in a loop and spend
 * the battery on disk syncs and decisions.
 *
 * So the door answers at most once per window. A wake that arrives inside the
 * window is dropped, and dropping is safe because the work behind it is
 * idempotent: the previous wake already synced the same configuration and
 * reached the same decision.
 *
 * PURE, so the rule is provable without an Android runtime, and separate from
 * the receiver so that "how often" is a decision with a test rather than a
 * subtraction buried in a callback.
 */
object WakeDebounce {

    /**
     * Generous on purpose. A legitimate MDM wakes this after an install or a
     * policy change - events measured in minutes at best - so a window this
     * size costs a real caller nothing, while a loop gets one answer a minute
     * instead of thousands.
     */
    const val WINDOW_MS: Long = 60_000L

    /**
     * @param lastAnsweredAtMs when the door was last answered, or null if never.
     * @return true when this wake should be acted on.
     */
    fun shouldAnswer(
        lastAnsweredAtMs: Long?,
        nowMs: Long,
        windowMs: Long = WINDOW_MS,
    ): Boolean {
        if (lastAnsweredAtMs == null) return true
        val since = nowMs - lastAnsweredAtMs
        // A clock that moved backwards must not lock the door until it catches
        // up. These phones sit unattended for weeks and do take NTP
        // corrections; refusing every wake in the meantime would be worse than
        // answering one extra time.
        if (since < 0) return true
        return since >= windowMs
    }
}
