package app.zippie.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Every test here is a fault this estate actually had on 2026-08-11, and each
 * was invisible from the phone at the time.
 *
 * These deliberately mirror DiagnosticsTests.swift assertion for assertion. If
 * the two apps drift, one of them starts describing the same bond in different
 * words, and the operator debugging at 2am pays for it.
 */
class DiagnosticsTest {

    // ----- not checked is a third state

    @Test
    fun `not checked is neither good nor bad`() {
        val mdm = Diagnostics().rows().first { it.label == "MDM" }
        assertEquals("not checked", mdm.value)
        assertEquals(
            "an unmeasured row rendered as good lies; as bad it teaches the reader to ignore red",
            Tone.UNKNOWN, mdm.tone,
        )
    }

    // ----- the fault that started it

    @Test
    fun `leaving the router is reported as losing the tailnet`() {
        val d = Diagnostics(
            mdm = DiagnosticState.Failed(DiagnosticFailure.NoRoute),
            tailnet = TailnetPath.Unreachable(DiagnosticFailure.NoRoute),
        )
        assertEquals("Cannot reach the tailnet", d.headline)
        val row = d.rows().first { it.label == "Tailnet" }
        assertEquals(Tone.BAD, row.tone)
        assertEquals("install Tailscale on this phone to fix it everywhere", row.hint)
    }

    /** The distinction the whole type exists for. */
    @Test
    fun `via router is not green and says it will not survive leaving`() {
        val d = Diagnostics(tailnet = TailnetPath.ViaRouter("travel-router"))
        val row = d.rows().first { it.label == "Tailnet" }
        assertEquals("via travel-router", row.value)
        assertNotEquals(
            "reachable-only-here must not look identical to reachable-anywhere",
            Tone.GOOD, row.tone,
        )
        assertEquals("only on this network - leaving it loses the MDM", row.hint)
        assertFalse(d.tailnet.survivesLeavingThisNetwork)
    }

    @Test
    fun `direct tailnet survives leaving the network`() {
        val d = Diagnostics(tailnet = TailnetPath.Direct("pixel-6a"))
        assertTrue(d.tailnet.survivesLeavingThisNetwork)
        val row = d.rows().first { it.label == "Tailnet" }
        assertEquals(Tone.GOOD, row.tone)
        assertEquals("this phone is pixel-6a", row.hint)
    }

    // ----- the silent 401

    @Test
    fun `a refused announce says why and what to do`() {
        val d = Diagnostics(
            lastAnnounce = DiagnosticState.Failed(
                DiagnosticFailure.Refused("bad or missing bearer token"),
            ),
        )
        assertEquals(
            "The router refused this phone - refused: bad or missing bearer token",
            d.headline,
        )
        val row = d.rows().first { it.label == "Last announce" }
        assertEquals(Tone.BAD, row.tone)
        assertEquals("store the router's write token in this app", row.hint)
    }

    @Test
    fun `standing by is not shown when the router is actually refusing`() {
        // The exact wrong sentence: the phone said "Standing by" for hours while
        // every announce it made was answered 401.
        val d = Diagnostics(
            carrying = false,
            lastAnnounce = DiagnosticState.Failed(DiagnosticFailure.Refused("nope")),
        )
        assertNotEquals("Standing by", d.headline)
    }

    // ----- the DHCP fault

    @Test
    fun `no resolver is reported above the symptoms it causes`() {
        val d = Diagnostics(
            mdm = DiagnosticState.Failed(DiagnosticFailure.TimedOut(12)),
            captive = DiagnosticState.Failed(DiagnosticFailure.NoResolverOffered),
        )
        assertEquals(
            "reporting the symptom above the cause is how a DNS fault read as a wifi fault",
            "This network has no DNS", d.headline,
        )
    }

    @Test
    fun `a missing dhcp resolver is said out loud in the network row`() {
        val d = Diagnostics(ssid = "MAIN", dhcpResolver = ResolverFact.None)
        val row = d.rows().first { it.label == "Network" }
        assertEquals("this network offered no DNS server", row.hint)
        assertEquals(Tone.BAD, row.tone)
    }

    /** Android CAN read the resolver, but the type must still express the
     *  unknown case or the shared vocabulary breaks at the platform boundary. */
    @Test
    fun `an unknown resolver is not reported as a missing one`() {
        val d = Diagnostics(ssid = "MAIN", dhcpResolver = ResolverFact.Unknown)
        val row = d.rows().first { it.label == "Network" }
        assertNull(row.hint)
        assertNotEquals(
            "a platform that will not say is not a network fault",
            Tone.BAD, row.tone,
        )
    }

    @Test
    fun `a present resolver is named`() {
        val d = Diagnostics(ssid = "TravelRouter", dhcpResolver = ResolverFact.Address("10.99.0.1"))
        val row = d.rows().first { it.label == "Network" }
        assertEquals("TravelRouter", row.value)
        assertEquals("DNS from DHCP: 10.99.0.1", row.hint)
    }

    // ----- failure kinds are named, not "failed"

    @Test
    fun `each failure kind says something different`() {
        val kinds = listOf(
            DiagnosticFailure.NoResolverOffered,
            DiagnosticFailure.NameNotResolved("mdm.example"),
            DiagnosticFailure.TimedOut(12),
            DiagnosticFailure.Tls("bad cert"),
            DiagnosticFailure.Http(401),
            DiagnosticFailure.Refused("nope"),
            DiagnosticFailure.NoRoute,
        )
        assertEquals(
            "two failure kinds sharing a sentence makes the screen useless",
            kinds.size, kinds.map { it.summary }.toSet().size,
        )
        assertTrue(
            "a 401 and a 404 send you to different places, so the status must survive",
            DiagnosticFailure.Http(401).summary.contains("401"),
        )
    }

    // ----- staleness

    @Test
    fun `an old measurement says how old and asks for a refresh`() {
        val now = 1_000_000_000L
        val d = Diagnostics(measuredAtEpochMs = now - 300_000)
        val row = d.rows(now).first { it.label == "Measured" }
        assertEquals("300s ago", row.value)
        assertEquals("tap refresh - these may have moved", row.hint)
    }

    @Test
    fun `a fresh measurement does not nag`() {
        val now = 1_000_000_000L
        val d = Diagnostics(measuredAtEpochMs = now - 2_000)
        val row = d.rows(now).first { it.label == "Measured" }
        assertEquals("just now", row.value)
        assertNull(row.hint)
    }

    @Test
    fun `a stale check-in is red and says so`() {
        val now = 1_000_000_000L
        val d = Diagnostics(lastCheckInEpochMs = now - 3_600_000)
        val row = d.rows(now).first { it.label == "Last check-in" }
        assertEquals(Tone.BAD, row.tone)
        assertEquals("60 min ago", row.value)
    }

    // ----- the happy path still reads well

    @Test
    fun `carrying says carrying`() {
        val d = Diagnostics(
            legName = "pixel", carrying = true,
            lastAnnounce = DiagnosticState.Ok(),
            mdm = DiagnosticState.Ok(),
            tailnet = TailnetPath.Direct("pixel"),
        )
        assertEquals("Carrying", d.headline)
        val bond = d.rows().first { it.label == "Bond" }
        assertEquals(Tone.GOOD, bond.tone)
        assertEquals("known to the router as pixel", bond.hint)
    }

    @Test
    fun `reachable only here is its own headline`() {
        val d = Diagnostics(mdm = DiagnosticState.Ok(), tailnet = TailnetPath.ViaRouter("travel-router"))
        assertEquals("Reachable, but only on this network", d.headline)
    }

    @Test
    fun `byte formatting is readable`() {
        assertEquals("512 B", Diagnostics.humanBytes(512))
        assertEquals("2.0 KB", Diagnostics.humanBytes(2048))
        assertEquals("16 MB", Diagnostics.humanBytes(16_818_685))
    }

    /** The two apps must describe the same bond in the same words. */
    @Test
    fun `wording matches the ios kit`() {
        assertEquals(
            "no route from this network",
            DiagnosticFailure.NoRoute.summary,
        )
        assertEquals(
            "this network offered no DNS server",
            DiagnosticFailure.NoResolverOffered.summary,
        )
    }
}

/**
 * The range check that decides "this phone is on the tailnet".
 *
 * Mirrors TailnetAddressTests in the iOS Kit case for case. An off-by-one octet
 * would classify a hotel's 100.130.4.5 as tailnet and the screen would
 * confidently report direct access on a phone that has none.
 */
class TailnetRangeTest {
    @Test
    fun `the cgnat range is recognised`() {
        assertTrue(DiagnosticsMeasurer.isTailnetV4("100.64.0.1"))
        assertTrue(DiagnosticsMeasurer.isTailnetV4("100.127.255.254"))
        assertTrue(DiagnosticsMeasurer.isTailnetV4("100.64.100.2"))
    }

    @Test
    fun `addresses just outside the range are rejected`() {
        assertFalse("below 100.64", DiagnosticsMeasurer.isTailnetV4("100.63.255.255"))
        assertFalse(
            "100.128/9 is ordinary public space, not CGNAT",
            DiagnosticsMeasurer.isTailnetV4("100.128.0.1"),
        )
    }

    @Test
    fun `unrelated and malformed addresses are rejected`() {
        listOf("192.168.1.1", "10.99.0.1", "192.0.2.2", "", "100", "not.an.ip.x", "100.64.0.300")
            .forEach { assertFalse("$it is not a tailnet address", DiagnosticsMeasurer.isTailnetV4(it)) }
    }

    /** Zippie's own tunnel shares the interface namespace; the range keeps them apart. */
    @Test
    fun `zippies own tunnel address is not mistaken for tailscale`() {
        assertFalse(DiagnosticsMeasurer.isTailnetV4("192.0.2.2"))
    }
}
