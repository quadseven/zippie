package app.zippie.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The mapping from the router's view to the rows on screen, against the same
 * live payload the decoder tests use.
 */
class BondLegsTest {

    private val status: BondStatus by lazy {
        val stream = javaClass.classLoader!!.getResourceAsStream("router-status-2026-08-05.json")
        BondStatus.decode(stream!!.bufferedReader().use { it.readText() })
    }

    private fun rows(localIp: String? = null, listenPort: Int = 51999) =
        BondLegs.rows(status, listenPort, localIp)

    private fun row(name: String, localIp: String? = null) =
        rows(localIp).first { it.id == name }

    @Test
    fun `a gated but healthy leg reads as reserve, never as a failure`() {
        val ethernet = row("ethernet")
        assertEquals(LegState.RESERVE, ethernet.state)
        assertNotNull(ethernet.note)
        assertTrue(
            "the note must explain the gate rather than repeat the router's transport error",
            ethernet.note!!.startsWith("Held in reserve"),
        )
    }

    /**
     * The divergence from iOS, and the reason for it: dongle4g is on a gated
     * tier AND has no interface at all. Calling that "held in reserve" would
     * promise a fallback that cannot be called on.
     */
    @Test
    fun `a down leg is down even when its tier is gated`() {
        val dongle = row("dongle4g")
        assertEquals(LegState.DOWN, dongle.state)
        assertEquals("no interface matched", dongle.note)
    }

    @Test
    fun `the headline counts carrying legs, not configured ones`() {
        val rows = rows()
        assertEquals(5, rows.size)
        assertEquals("1 connection", BondLegs.headline(rows))
    }

    /**
     * Identity is evidence or nothing. With this phone's wifi address matching
     * the endpoint the router dials for Co-operator's leg, that row - and only that row
     * - is marked, and the headline sentence answers the question the person
     * holding the phone actually has.
     */
    @Test
    fun `only the leg the router dials at this phone is marked as this phone`() {
        val rows = rows(localIp = "10.99.0.100")
        assertEquals(1, rows.count { it.isYou })
        assertTrue(row("companion-co-operator", localIp = "10.99.0.100").isYou)
        assertEquals("This phone is not one of them.", BondLegs.subhead(rows))
    }

    @Test
    fun `no local address means no leg is claimed`() {
        assertEquals(0, rows(localIp = null).count { it.isYou })
        assertFalse(rows(localIp = "10.99.0.100", listenPort = 51998).any { it.isYou })
    }

    @Test
    fun `traffic comes from the packet counters so a carrying leg is not drawn as empty`() {
        val hotspot = row("hotspot")
        assertEquals(LegState.CARRYING, hotspot.state)
        assertEquals(11018643L, hotspot.upBytes)
        assertEquals(31703754L, hotspot.downBytes)
    }

    @Test
    fun `usage is only stated where the router measured it`() {
        assertEquals("0.04 GB used of a 50 GB cap.", row("hotspot").usageNote)
    }

    /**
     * CARRYING AND HEALTH ARE ORTHOGONAL, and this is the regression test for
     * the screen that proved it. iOS derived membership from the drawing state
     * and reported "Nothing carrying" and "0 of 3 carrying" directly above a row
     * reading "carrying, degraded" with 402 MB sent.
     *
     * This file used to dodge that by making the DRAWING lossy instead - a
     * degraded leg with weight became LegState.CARRYING, so the count was right
     * and the row was wrong: a leg losing packets painted in the healthy accent.
     * Both facts survive now, and this asserts both at once because the failure
     * is always one of them quietly taking the other's slot.
     */
    @Test
    fun `a degraded leg that is carrying is drawn degraded and counted as carrying`() {
        val status = BondStatus.decode(
            """{"paths":[{"name":"lte","state":"degraded","effective_weight":64,
               "in_bond":true,"loss_pct":12.0,"interface":"wwan0",
               "link_tx_bytes":421527552,"link_rx_bytes":13000}]}""",
        )
        val leg = BondLegs.rows(status, 51999, null).single()
        assertEquals("the row must show the loss it is suffering", LegState.DEGRADED, leg.state)
        assertTrue("the router says it is in the bond, so it is", leg.isCarrying)
        assertEquals("carrying, degraded", leg.stateWord)
        val rows = listOf(leg)
        assertEquals("1 connection", BondLegs.headline(rows))
        assertEquals("Every link is carrying.", BondLegs.subhead(rows))
    }

    /** A leg held out of the bond by the anti-flap gate is NOT carrying, even
     *  though the router still calls its transport degraded rather than down. */
    @Test
    fun `a degraded leg outside the bond is not counted as carrying`() {
        val status = BondStatus.decode(
            """{"paths":[{"name":"lte","state":"degraded","effective_weight":64,
               "in_bond":false,"interface":"wwan0"}]}""",
        )
        val leg = BondLegs.rows(status, 51999, null).single()
        assertEquals(LegState.DEGRADED, leg.state)
        assertFalse(leg.isCarrying)
        assertEquals("not in the bond", leg.stateWord)
    }

    /** The chart reads the same counters and the same membership the rows do,
     *  so the top edge of the chart cannot disagree with the list under it. */
    @Test
    fun `a snapshot carries the packet counters and the router's own membership`() {
        val snapshot = BondLegs.snapshot(status, atMs = 1_700_000_000_000)
        assertEquals(5, snapshot.legs.size)
        val hotspot = snapshot.legs.first { it.name == "hotspot" }
        assertEquals("Phone hotspot", hotspot.label)
        assertEquals(11018643L, hotspot.txBytes)
        assertEquals(31703754L, hotspot.rxBytes)
        assertTrue(hotspot.isCarrying)
        assertFalse(snapshot.legs.first { it.name == "ethernet" }.isCarrying)
    }

    @Test
    fun `nothing carrying says so rather than showing a count`() {
        val nothing = BondStatus.decode(
            """{"paths":[{"name":"a","state":"down","effective_weight":0,"in_bond":false}]}""",
        )
        val rows = BondLegs.rows(nothing, 51999, null)
        assertEquals("Nothing carrying", BondLegs.headline(rows))
        assertEquals(
            "The router answered but no link is carrying traffic right now.",
            BondLegs.subhead(rows),
        )
    }

    // MARK: - legsHeading

    /**
     * "one out of two" was the question BondModel.swift's own comment names.
     * Answered in the heading rather than left for someone to count rows -
     * against the live fixture, which has exactly one leg (hotspot) carrying
     * out of five configured.
     */
    @Test
    fun `legsHeading counts carrying legs out of the whole bond when the router answers`() {
        assertEquals(
            "Connections - 1 of 5 carrying",
            BondLegs.legsHeading(rows(), bondReachable = true),
        )
    }

    /**
     * MUST TELL THE TRUTH ABOUT SCOPE. When the router cannot be reached, the
     * rows on screen are at most this phone's own fallback leg, and a heading
     * that still claimed to count "connections" would be a claim about the
     * whole bond nothing here measured - the exact failure BondModel.swift's
     * `legsHeading` was written to avoid.
     */
    @Test
    fun `legsHeading admits it can only speak for this phone when the bond is unreachable`() {
        assertEquals(
            "What this phone carried",
            BondLegs.legsHeading(rows(), bondReachable = false),
        )
        assertEquals(
            "What this phone carried",
            BondLegs.legsHeading(emptyList(), bondReachable = false),
        )
    }

    // MARK: - thisPhoneFallback

    @Test
    fun `no report draws no fallback row`() {
        assertEquals(emptyList<Leg>(), BondLegs.thisPhoneFallback(null, nowMs = 1_000L))
    }

    /**
     * A stale report is a corpse's counters, not current spending - the same
     * rule BondModel.swift's `budget` enforces on iOS. Drawing a row from it
     * would present a stopped relay's last count as if the phone were still
     * reporting.
     */
    @Test
    fun `a stale report draws no fallback row`() {
        val stats = RelayStats(listening = true, cellularReady = true, upDatagrams = 1, upBytes = 500)
        val now = 1_000_000L
        val stale = RelayReport(stats, updatedAtMs = now - RelayReport.STALENESS_MS - 1)
        assertEquals(emptyList<Leg>(), BondLegs.thisPhoneFallback(stale, now))
    }

    @Test
    fun `a carrying relay draws one carrying row named This phone`() {
        val now = 1_000_000L
        val stats = RelayStats(
            listening = true,
            cellularReady = true,
            upDatagrams = 40,
            upBytes = 4_000,
            downBytes = 1_000,
            lastRouterInboundAtMs = now - 1_000,
        )
        val fallback = BondLegs.thisPhoneFallback(RelayReport(stats, now), now)
        assertEquals(1, fallback.size)
        val leg = fallback.single()
        assertEquals("This phone", leg.name)
        assertEquals(LegState.CARRYING, leg.state)
        assertTrue(leg.isCarrying)
        assertEquals(4_000L, leg.upBytes)
        assertEquals(1_000L, leg.downBytes)
        assertFalse("the fallback row is not matched by endpoint, so it must never claim isYou", leg.isYou)
    }

    /** Off - no report - draws nothing (covered above); everything short of
     *  Carrying draws DOWN or IDLE, never a false CARRYING, because this row is
     *  the one place on the Status tab a router-unreachable phone can still be
     *  mistaken for helping. */
    @Test
    fun `a relay that has heard nothing from the router draws idle, not carrying`() {
        val now = 1_000_000L
        val stats = RelayStats(listening = true, cellularReady = true)
        val fallback = BondLegs.thisPhoneFallback(RelayReport(stats, now), now)
        val leg = fallback.single()
        assertEquals(LegState.IDLE, leg.state)
        assertFalse(leg.isCarrying)
    }

    @Test
    fun `a relay with no cellular draws down`() {
        val now = 1_000_000L
        val stats = RelayStats(listening = true, cellularReady = false, lastError = "cellular unavailable")
        val fallback = BondLegs.thisPhoneFallback(RelayReport(stats, now), now)
        val leg = fallback.single()
        assertEquals(LegState.DOWN, leg.state)
        assertFalse(leg.isCarrying)
    }
}
