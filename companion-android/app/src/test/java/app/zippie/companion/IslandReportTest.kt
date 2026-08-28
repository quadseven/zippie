package app.zippie.companion

import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * The signal that would have caught the two outages nothing caught (zippie#286),
 * and - just as important - the three states that must NOT alert.
 */
class IslandReportTest {

    @Test
    fun `a router answering on the LAN with nothing carrying is the islanding signal`() {
        // 2026-08-23: router powered, agent running, LAN fine, no uplink at all.
        assertEquals(
            IslandState.ROUTER_NOT_CARRYING,
            IslandReport.evaluate(relayListening = true, consoleAnsweredOnLan = true, carryingLegs = 0),
        )
    }

    @Test
    fun `a router that is carrying is not reported`() {
        assertEquals(
            IslandState.ROUTER_CARRYING,
            IslandReport.evaluate(relayListening = true, consoleAnsweredOnLan = true, carryingLegs = 2),
        )
    }

    @Test
    fun `leaving the house is not an outage`() {
        // THE FALSE ALARM THAT WOULD GET THIS MUTED. The phone is away from the
        // router most of the time; that must be silence, not a page.
        assertEquals(
            IslandState.NOT_NEAR_ROUTER,
            IslandReport.evaluate(relayListening = true, consoleAnsweredOnLan = false, carryingLegs = null),
        )
    }

    @Test
    fun `a phone whose own relay is down is not a witness about the router`() {
        assertEquals(
            IslandState.RELAY_NOT_READY,
            IslandReport.evaluate(relayListening = false, consoleAnsweredOnLan = true, carryingLegs = 0),
        )
    }

    @Test
    fun `relay readiness is checked before proximity`() {
        // Otherwise a phone sitting on the sofa with a dead relay reports
        // NOT_NEAR_ROUTER and reads as a departure it is not.
        assertEquals(
            IslandState.RELAY_NOT_READY,
            IslandReport.evaluate(relayListening = false, consoleAnsweredOnLan = false, carryingLegs = null),
        )
    }

    @Test
    fun `an answering console with no leg count is treated as no knowledge`() {
        // A malformed or paths-less status must not be read as zero carrying.
        // Absence of a count is not a count of zero - the same rule the whole
        // BondUiState type exists to enforce.
        assertEquals(
            IslandState.NOT_NEAR_ROUTER,
            IslandReport.evaluate(relayListening = true, consoleAnsweredOnLan = true, carryingLegs = null),
        )
    }

    @Test
    fun `wire values are stable lowercase and unique`() {
        // A monitor filters on these strings. Renaming one silently breaks it,
        // which is the failure this whole issue is about.
        val wire = IslandState.values().map { it.wireValue }
        assertEquals(
            listOf("relay_not_ready", "not_near_router", "router_carrying", "router_not_carrying"),
            wire,
        )
        assertEquals(wire.size, wire.toSet().size)
    }
}
