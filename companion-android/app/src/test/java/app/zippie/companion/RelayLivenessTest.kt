package app.zippie.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Supervision already wakes forever - `setAndAllowWhileIdle`, every 15 minutes,
 * indefinitely. What it lacked was the ability to tell a working relay from a
 * frozen one, so every wake-up confirmed the broken state and changed nothing.
 */
class RelayLivenessTest {

    private val now = 1_000_000L

    private fun report(ageMs: Long) =
        RelayReport(RelayStats(listening = true), updatedAtMs = now - ageMs)

    @Test
    fun `no report at all is absent, not frozen`() {
        // The process was reclaimed and the in-memory store went with it. An
        // ordinary start is right; stopping first would be a pointless no-op.
        val v = RelayLiveness.evaluate(null, now)
        assertEquals(RelayLiveness.Absent, v)
        assertFalse(v.needsHardRestart)
    }

    @Test
    fun `a relay reporting on schedule is healthy and must not be touched`() {
        val v = RelayLiveness.evaluate(report(RelayReport.HEARTBEAT_MS), now)
        assertEquals(RelayLiveness.Healthy, v)
        assertFalse("restarting a working relay drops the bond's leg", v.needsHardRestart)
    }

    @Test
    fun `a heartbeat that stopped is frozen and needs a hard restart`() {
        val quiet = RelayLiveness.FROZEN_AFTER_MS + 1
        val v = RelayLiveness.evaluate(report(quiet), now)
        assertTrue(v.needsHardRestart)
        assertEquals(RelayLiveness.Frozen(quiet), v)
    }

    @Test
    fun `the restart threshold is later than the screen's staleness threshold`() {
        // The UI may say "not reporting" early - being wrong is free there.
        // This decision costs a healthy leg when it is wrong, so it waits.
        assertTrue(
            "a restart must never fire before the screen would even complain",
            RelayLiveness.FROZEN_AFTER_MS > RelayReport.STALENESS_MS)
        val borderline = report(RelayReport.STALENESS_MS + 1)
        assertFalse(
            "stale enough to display is NOT stale enough to restart",
            RelayLiveness.evaluate(borderline, now).needsHardRestart)
    }

    @Test
    fun `exactly at the threshold counts as frozen`() {
        assertTrue(RelayLiveness.evaluate(
            report(RelayLiveness.FROZEN_AFTER_MS), now).needsHardRestart)
    }

    @Test
    fun `a clock that went backwards never reads as frozen`() {
        // These phones sit unattended for weeks and do get NTP corrections.
        // Waiting one more cycle costs 15 minutes; a restart loop costs the bond.
        val future = RelayReport(RelayStats(), updatedAtMs = now + 60_000)
        assertEquals(RelayLiveness.Healthy, RelayLiveness.evaluate(future, now))
    }
}
