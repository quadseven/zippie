package app.zippie.companion

/**
 * Whether a relay that EXISTS is actually working, and what supervision should
 * do about it.
 *
 * THE GAP THIS CLOSES. `BootReceiver` already re-enters forever - the retry
 * chain ends at "every 15 minutes, indefinitely" and uses
 * `setAndAllowWhileIdle`, which the system delivers even in Doze. So the phone
 * already wakes up. What it does on waking is ask whether the relay is
 * RUNNING, and then call `startForegroundService`, which on a live service (in
 * the existing comment's words) "just delivers another onStartCommand".
 *
 * A frozen relay IS running. It holds a bound socket, it keeps announcing on a
 * timer, and it never services a packet - so every one of those wake-ups
 * confirms the broken state and changes nothing. Observed three times on real
 * hardware: the router dials the leg and gets silence while the phone shows a
 * healthy relay.
 *
 * WHY THIS IS BETTER THAN THE BATTERY EXEMPTION. The exemption needs a human to
 * tap a dialog, and this phone is in a car with nobody near it - so a prompt is
 * a manual step wearing a fix's clothes. This covers strictly more: the case
 * where the exemption was never granted, AND the case where it was granted and
 * Android froze the process anyway, which no dialog can help with.
 *
 * THE SIGNAL IS THE HEARTBEAT, NOT THE TRAFFIC. `RelayReport.updatedAtMs` is
 * rewritten every [RelayReport.HEARTBEAT_MS] even when nothing changed, so a
 * stale report means the relay's own thread stopped being scheduled. Deciding
 * from traffic instead would be wrong twice over: a genuinely idle bond sends
 * nothing, and a router that has stopped dialling is not this phone's fault.
 *
 * Pure, and takes only a report and a clock, so every branch is provable
 * without an Android runtime - the same split [BootRelayDecision] uses.
 */
sealed class RelayLiveness {

    /** No report at all: never started, or the process was reclaimed and the
     *  in-memory store went with it. An ordinary start is correct. */
    object Absent : RelayLiveness()

    /** Reporting on schedule. Do not touch it - restarting a working relay
     *  drops the bond's leg for no reason. */
    object Healthy : RelayLiveness()

    /**
     * The process is alive and its heartbeat has stopped. `startForegroundService`
     * will NOT fix this - the service object already exists, so the call is a
     * no-op that has been repeating every 15 minutes. It has to be stopped and
     * started.
     */
    data class Frozen(val quietForMs: Long) : RelayLiveness()

    /** True when supervision must do more than deliver another onStartCommand. */
    val needsHardRestart: Boolean get() = this is Frozen

    companion object {
        /**
         * Six heartbeats. [RelayReport.STALENESS_MS] is five, and is what the
         * UI uses to say "not reporting" - a display decision, where being
         * early is free. This is a RESTART decision, where being early costs a
         * healthy leg, so it deliberately waits longer than the screen does.
         */
        const val FROZEN_AFTER_MS: Long = 6 * RelayReport.HEARTBEAT_MS

        fun evaluate(
            report: RelayReport?,
            nowMs: Long,
            frozenAfterMs: Long = FROZEN_AFTER_MS,
        ): RelayLiveness {
            if (report == null) return Absent
            val quiet = nowMs - report.updatedAtMs
            // A clock that went backwards (NTP correction, and these phones do
            // sit unattended for weeks) must never read as frozen. Waiting one
            // more cycle costs 15 minutes; a restart loop costs the bond.
            if (quiet < 0) return Healthy
            return if (quiet >= frozenAfterMs) Frozen(quiet) else Healthy
        }
    }
}
