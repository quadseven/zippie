package app.zippie.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The mode decision and, since #154, why an unreachable router is unreachable.
 *
 * Mirrors BondModeTests.swift for the reason split. Off the router's LAN the
 * only path is the tailnet, and a phone without it times out exactly like a
 * router that is down; the sentence must name the real cause.
 */
class BondModeTest {

    @Test
    fun `only local evidence selects contribute`() {
        assertEquals(BondMode.CONTRIBUTE, ModeDecision(RouterProximity.LOCAL).mode)
        assertEquals(BondMode.CLIENT, ModeDecision(RouterProximity.REMOTE).mode)
        assertEquals(BondMode.CLIENT, ModeDecision(RouterProximity.UNREACHABLE).mode)
    }

    @Test
    fun `no tailnet route says the phone is not on the tailnet`() {
        val d = ModeDecision(RouterProximity.UNREACHABLE, unreachableReason = UnreachableReason.NO_TAILNET_ROUTE)
        assertEquals(
            "This phone is not on the tailnet, so the router cannot be reached from here.",
            d.summary,
        )
        assertFalse(d.summary.contains("not answering"))
        assertEquals(BondMode.CLIENT, d.mode)
    }

    @Test
    fun `not answering keeps the router sentence`() {
        val d = ModeDecision(RouterProximity.UNREACHABLE, unreachableReason = UnreachableReason.NOT_ANSWERING)
        assertEquals("The router is not answering from here.", d.summary)
    }

    @Test
    fun `unreachable defaults to not answering`() {
        assertEquals(UnreachableReason.NOT_ANSWERING, ModeDecision(RouterProximity.UNREACHABLE).reason)
    }

    @Test
    fun `reason is null when reached or undetermined`() {
        assertNull(ModeDecision(RouterProximity.LOCAL, unreachableReason = UnreachableReason.NO_TAILNET_ROUTE).reason)
        assertNull(ModeDecision(RouterProximity.REMOTE, unreachableReason = UnreachableReason.NO_TAILNET_ROUTE).reason)
        assertNull(
            ModeDecision(
                RouterProximity.UNREACHABLE,
                undetermined = true,
                unreachableReason = UnreachableReason.NO_TAILNET_ROUTE,
            ).reason,
        )
    }

    @Test
    fun `log names match the iOS app`() {
        assertEquals("no_tailnet_route", UnreachableReason.NO_TAILNET_ROUTE.logName)
        assertEquals("console_timeout", UnreachableReason.NOT_ANSWERING.logName)
    }

    @Test
    fun `no tailnet address with a tailnet console is no route`() {
        assertEquals(
            UnreachableReason.NO_TAILNET_ROUTE,
            UnreachableReason.decide(hasRemoteCandidate = true, ownAddresses = listOf("192.0.2.10")),
        )
    }

    @Test
    fun `a tailnet address means the router is not answering`() {
        assertEquals(
            UnreachableReason.NOT_ANSWERING,
            UnreachableReason.decide(hasRemoteCandidate = true, ownAddresses = listOf("192.0.2.10", "100.64.100.2")), // leak-guard-allow: synthetic test address
        )
    }

    @Test
    fun `an address just past the tailnet range is no route`() {
        assertEquals(
            UnreachableReason.NO_TAILNET_ROUTE,
            UnreachableReason.decide(hasRemoteCandidate = true, ownAddresses = listOf("100.128.0.1")),
        )
    }

    @Test
    fun `without a tailnet console the tailnet is not blamed`() {
        assertEquals(
            UnreachableReason.NOT_ANSWERING,
            UnreachableReason.decide(hasRemoteCandidate = false, ownAddresses = emptyList()),
        )
    }

    @Test
    fun `undetermined says so rather than guessing`() {
        val d = ModeDecision(RouterProximity.UNREACHABLE, undetermined = true)
        assertEquals("Checking", d.label)
        assertTrue(d.summary.startsWith("Working out"))
    }
}
