package app.zippie.companion

/**
 * Guard that decides whether a second-chance pass (BOOT_COMPLETED, PACKAGE_REPLACED)
 * or a supervision RETRY pass should leave the relay alone.
 *
 * Gated on ANNOUNCED-NESS, not liveness (#237):
 * A relay that half-starts at locked boot (LOCKED_BOOT_COMPLETED) forwards bytes
 * but has no console token and cannot announce. Testing liveness (report != null)
 * treated that half-started state as healthy and skipped recovery forever.
 *
 * Testing announced-ness means:
 * - If running and announcing: untouched (skip recovery).
 * - If running but NOT announcing: needs recovery to deliver the token.
 * - If not running: needs start.
 */
object SecondChanceGuard {

    /**
     * Whether to skip recovery.
     * Returns true ONLY if the relay is already running AND announcing.
     */
    fun shouldSkip(report: RelayReport?): Boolean =
        report != null && report.isAnnounced

    /**
     * Whether a relay requires recovery.
     * True if the relay is running but has not successfully announced.
     */
    fun needsRecovery(report: RelayReport?): Boolean =
        report != null && !report.isAnnounced
}
