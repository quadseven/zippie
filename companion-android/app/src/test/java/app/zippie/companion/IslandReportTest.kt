package app.zippie.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.regex.Pattern

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
            IslandReport.evaluate(relayListening = true, proximity = RouterProximity.LOCAL, carryingLegs = 0),
        )
    }

    @Test
    fun `the islanding signal ships within minutes, not days`() {
        // The acceptance criterion from #320: "A monitor fires in minutes, not
        // days, when the signal is set". The heartbeat ships router_view telemetry
        // every SHIP_INTERVAL_MS (20s), so the ROUTER_NOT_CARRYING signal reaches
        // Datadog within ~20 seconds of the router becoming islanded - well under
        // the "minutes" bound and orders of magnitude faster than the "days" the
        // old hub observer would have taken.
        //
        // SHIP_INTERVAL_MS is private in RelayService, so we parse it from source.
        // A future change that raises the interval past 5 minutes would break this
        // test and require a deliberate decision to relax the criterion.
        val src = java.io.File("src/main/java/app/zippie/companion/RelayService.kt").readText()
        val intervalLine = src.lines()
            .first { it.trim().startsWith("private const val SHIP_INTERVAL_MS") }
        val shipIntervalMs = Pattern.compile("\\d+_?\\d*L?").matcher(intervalLine)
            .results()
            .map { it.group().replace("_", "").replace("L", "").toLong() }
            .first()
        val fiveMinutesMs = 5 * 60 * 1000L
        assertTrue(
            "SHIP_INTERVAL_MS must be under 5 minutes so the islanding signal " +
                "reaches Datadog in minutes, not days. Current: ${shipIntervalMs}ms",
            shipIntervalMs < fiveMinutesMs
        )
    }

    @Test
    fun `a router that is carrying is not reported`() {
        assertEquals(
            IslandState.ROUTER_CARRYING,
            IslandReport.evaluate(relayListening = true, proximity = RouterProximity.LOCAL, carryingLegs = 2),
        )
    }

    @Test
    fun `leaving the house is not an outage`() {
        // THE FALSE ALARM THAT WOULD GET THIS MUTED. The phone is away from the
        // router most of the time; that must be silence, not a page.
        assertEquals(
            IslandState.NOT_NEAR_ROUTER,
            IslandReport.evaluate(relayListening = true, proximity = RouterProximity.UNREACHABLE, carryingLegs = null),
        )
    }

    @Test
    fun `a remote router answer is not evidence of nearby islanding`() {
        assertEquals(
            IslandState.NOT_NEAR_ROUTER,
            IslandReport.evaluate(relayListening = true, proximity = RouterProximity.REMOTE, carryingLegs = 0),
        )
    }

    @Test
    fun `a phone whose own relay is down is not a witness about the router`() {
        assertEquals(
            IslandState.RELAY_NOT_READY,
            IslandReport.evaluate(relayListening = false, proximity = RouterProximity.LOCAL, carryingLegs = 0),
        )
    }

    @Test
    fun `relay readiness is checked before proximity`() {
        // Otherwise a phone sitting on the sofa with a dead relay reports
        // NOT_NEAR_ROUTER and reads as a departure it is not.
        assertEquals(
            IslandState.RELAY_NOT_READY,
            IslandReport.evaluate(relayListening = false, proximity = RouterProximity.UNREACHABLE, carryingLegs = null),
        )
    }

    @Test
    fun `an answering console with no leg count is treated as no knowledge`() {
        // A malformed or paths-less status must not be read as zero carrying.
        // Absence of a count is not a count of zero - the same rule the whole
        // BondUiState type exists to enforce.
        assertEquals(
            IslandState.NOT_NEAR_ROUTER,
            IslandReport.evaluate(relayListening = true, proximity = RouterProximity.LOCAL, carryingLegs = null),
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
