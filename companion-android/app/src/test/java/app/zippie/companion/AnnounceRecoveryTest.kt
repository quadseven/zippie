package app.zippie.companion

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * #179: supervision must test announced-ness, not liveness.
 *
 * A relay phone that half-starts on LOCKED_BOOT_COMPLETED forwards bytes but
 * cannot announce - the console write token is deliberately kept out of
 * device-protected storage, so it is unreadable until first unlock. The old
 * second-chance guard asked whether the relay was RUNNING
 * (`RelayStatusStore.report.value != null`); a half-started relay IS running,
 * so BOOT_COMPLETED returned early and supervision's RETRY passes never
 * restarted it. The leg stayed invisible until a human intervened.
 *
 * These tests pin the pure half of the fix: what "announced" means, read off
 * the exact strings RelayService writes into RelayStats.announce. The wiring
 * half - the BOOT_COMPLETED guard consulting announced-ness, and start()
 * stopping before starting when a token recovery is due - is pinned by
 * BootAnnounceRecoveryWiringTest's source-text assertions, because
 * BootReceiver needs a real Context (the #48 trade-off).
 */
class AnnounceRecoveryTest {

    // The exact strings RelayService writes. Copied from its startAnnouncing()
    // and onAnnounced() - if those change, these tests MUST change with them,
    // because the receiver reads the same field.
    private val noToken =
        "Not announcing: no console write token set, so the router will not see this phone as a leg."
    private val noListener = "Not announcing: nothing is listening on 19321."
    private val announced = "Announced as pixel-6a, lease 295s."
    private val refused = "The router refused this phone's announcement: unknown leg"
    private val unreachable = "Could not reach the router console to announce: timeout"

    // hasAnnounced: announced-ness, not liveness.

    @Test
    fun `an announced leg counts as announced`() {
        assertTrue(BootRelayDecision.hasAnnounced(announced))
    }

    @Test
    fun `no announce record counts as not announced`() {
        assertFalse(BootRelayDecision.hasAnnounced(null))
    }

    @Test
    fun `a tokenless relay counts as not announced`() {
        assertFalse(BootRelayDecision.hasAnnounced(noToken))
    }

    @Test
    fun `a relay that never bound counts as not announced`() {
        assertFalse(BootRelayDecision.hasAnnounced(noListener))
    }

    @Test
    fun `a refused announcement counts as not announced`() {
        assertFalse(BootRelayDecision.hasAnnounced(refused))
    }

    @Test
    fun `an unreachable console counts as not announced`() {
        assertFalse(BootRelayDecision.hasAnnounced(unreachable))
    }

    // needsTokenRecovery: only the never-announced state earns a restart.

    @Test
    fun `a tokenless relay needs token recovery`() {
        // THE TRAP. Up, carrying, invisible - and a startForegroundService on
        // the live service would only deliver another onStartCommand, which
        // cannot make it re-read the token. It must be stopped and started.
        assertTrue(BootRelayDecision.needsTokenRecovery(noToken))
    }

    @Test
    fun `a relay that never published needs token recovery once it is live`() {
        assertTrue(BootRelayDecision.needsTokenRecovery(null))
    }

    @Test
    fun `a relay that never bound needs token recovery`() {
        assertTrue(BootRelayDecision.needsTokenRecovery(noListener))
    }

    @Test
    fun `an announced relay needs no token recovery`() {
        // AC3: running AND announcing is left alone. Restarting it would drop
        // the bond's leg for no reason.
        assertFalse(BootRelayDecision.needsTokenRecovery(announced))
    }

    @Test
    fun `a refused announcement is the announcer's retry loop, not a restart`() {
        // The router named the field it refused; restarting re-announces the
        // same thing the router just said no to, and drops a carrying leg to
        // do it.
        assertFalse(BootRelayDecision.needsTokenRecovery(refused))
    }

    @Test
    fun `an unreachable console is the announcer's retry loop, not a restart`() {
        // The announcer renews on its own timer; when the console comes back
        // its retry succeeds with no restart. Tearing down the relay to
        // re-attempt something already being re-attempted buys nothing.
        assertFalse(BootRelayDecision.needsTokenRecovery(unreachable))
    }
}
