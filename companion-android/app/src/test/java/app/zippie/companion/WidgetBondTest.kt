package app.zippie.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The router's bond view, as a widget is allowed to use it (quadseven/zippie#244).
 *
 * Two separate claims are under test, and both are ones a widget gets wrong by
 * default:
 *
 *  1. A BOND SNAPSHOT THAT HAS GONE STALE must stop describing the bond. It is
 *     fetched on the app's foreground poll, not on the launcher's schedule, so a
 *     redraw with the app backgrounded is the NORMAL case - and holding the
 *     last good leg list across it is a home screen claiming to know something
 *     nobody has looked at for minutes.
 *  2. The leg list must keep `weightIsNotMembership` and `reserveIsNotBroken`
 *     apart. A leg held in reserve reports no traffic BECAUSE THAT IS ITS JOB,
 *     and a leg with weight the transport does not hold is not carrying.
 *     Collapse either into "down" and the reader is trained to ignore the one
 *     row that means something.
 */
class WidgetBondTest {

    private val now = 2_000_000L

    // ------------------------------------------------------------ helpers ---

    /** Only the fields a test cares about, so every default here is a deliberate
     *  "the router said nothing" rather than an accident of a shared fixture. */
    private fun path(
        name: String,
        label: String? = null,
        weight: Int = 0,
        tier: Int? = 1,
        state: String? = "up",
        inBond: Boolean? = null,
        iface: String? = "br-lan",
        relayEndpoint: String? = null,
    ) = BondStatus.Path(
        name = name,
        label = label,
        state = state,
        tier = tier,
        effectiveWeight = weight,
        rttMs = null,
        lossPct = null,
        txBytes = null,
        rxBytes = null,
        linkTxBytes = null,
        linkRxBytes = null,
        interfaceName = iface,
        lastError = null,
        neverHandshaked = null,
        shadowedInterfaces = null,
        relayEndpoint = relayEndpoint,
        maxKbps = null,
        costClass = null,
        monthlyCapGb = null,
        usageGb = null,
        overSoftLimit = null,
        inBond = inBond,
    )

    private fun bond(vararg paths: BondStatus.Path) = BondStatus(
        mode = "client",
        datapath = "packet",
        primary = null,
        paths = paths.toList(),
    )

    private fun snapshot(status: BondStatus, ageMs: Long) =
        WidgetBondSnapshot(status, now - ageMs)

    /** A relay report good enough that the verdict is not one of the
     *  short-circuiting ones, so the leg list is reached at all.
     *
     *  `listening = true` IS LOAD-BEARING and was left off by accident. It
     *  defaults to false, and `RelayVerdict.evaluate` reads that before it
     *  looks at a single counter - a relay that never bound its LAN socket is
     *  `NotListening`, not `Carrying`. So without it this helper produced a
     *  report whose verdict is `NotListening`, which `WidgetContent.legsOf`
     *  answers with a DOWN row: every test below that expects the carrying
     *  fallback row was silently asserting the wrong thing, and the bond's own
     *  staleness gate was never the thing under test. */
    private fun carryingReport(ageMs: Long = 0) = RelayReport(
        RelayStats(
            listening = true,
            cellularReady = true,
            upDatagrams = 12,
            lastRouterInboundAtMs = now - 1,
        ),
        now - ageMs,
    )

    // ------------------------------------------------- the one that matters ---

    /**
     * THE TEST THAT MUST FAIL IF THE WIDGET KEEPS SHOWING A STALE BOND.
     *
     * A leg list that was genuinely correct a while ago, read past
     * [WidgetBondSnapshot.STALENESS_MS], must render as the this-phone fallback
     * row - never as the legs the snapshot happened to be holding.
     *
     * CONFIRMED BY BREAKING IT: short-circuiting [WidgetContent.legsOf]'s
     * staleness check (evaluating against the snapshot's own write time instead
     * of the caller's `now`, the exact bug this guards against) turns this red -
     * two router legs, neither of them "This phone".
     */
    @Test
    fun `a stale bond snapshot renders the this-phone row, not the legs it was holding`() {
        val stale = snapshot(
            bond(path("lte", label = "Lone SIM", weight = 100), path("wifi", label = "Cafe wifi", weight = 50)),
            WidgetBondSnapshot.STALENESS_MS + 1,
        )

        val content = WidgetContent.from(carryingReport(), now, bond = stale)

        assertEquals(
            "a widget cannot describe a bond nobody has looked at for longer than the " +
                "threshold; it must fall back to the row it can still vouch for",
            listOf(WidgetContent.Leg(WidgetContent.THIS_PHONE, WidgetContent.Leg.State.CARRYING)),
            content.legs,
        )
        assertEquals(
            "the relay report is a separate record with its own age and is still fresh here, " +
                "so the headline must not be dragged down by the bond",
            RelayVerdict.Carrying.headline,
            content.headline,
        )
    }

    /** Just inside the threshold, the same snapshot is used. */
    @Test
    fun `a fresh bond snapshot just inside the threshold renders the legs`() {
        val fresh = snapshot(
            bond(path("lte", label = "Lone SIM", weight = 100)),
            WidgetBondSnapshot.STALENESS_MS - 1,
        )

        val content = WidgetContent.from(carryingReport(), now, bond = fresh)

        assertEquals(
            listOf(WidgetContent.Leg("Lone SIM", WidgetContent.Leg.State.CARRYING)),
            content.legs,
        )
    }

    /** No snapshot at all is the same answer as a stale one: the fallback is the
     *  absence of evidence, not a penalty for having stale evidence. */
    @Test
    fun `no bond snapshot falls back to the this-phone row`() {
        val content = WidgetContent.from(carryingReport(), now, bond = null)
        assertEquals(
            listOf(WidgetContent.Leg(WidgetContent.THIS_PHONE, WidgetContent.Leg.State.CARRYING)),
            content.legs,
        )
    }

    /** A snapshot the router answered with no `paths` key is NOT an empty bond
     *  and is not a licence to describe anything. `neverStateUnmeasured`. */
    @Test
    fun `a fresh snapshot with no paths falls back rather than inventing rows`() {
        val noPaths = BondStatus(mode = "client", datapath = null, primary = null, paths = null)
        val content = WidgetContent.from(carryingReport(), now, bond = snapshot(noPaths, 0))
        assertEquals(
            listOf(WidgetContent.Leg(WidgetContent.THIS_PHONE, WidgetContent.Leg.State.CARRYING)),
            content.legs,
        )
    }

    /** A fresh bond cannot rescue a corpse relay report. Rule 4 applies to the
     *  whole list, not just to the report, and a widget whose relay has stopped
     *  checking in has no standing to describe the bond it last saw. */
    @Test
    fun `a fresh bond does not paper over a relay that stopped reporting`() {
        val fresh = snapshot(bond(path("lte", label = "Lone SIM", weight = 100)), 0)
        val corpse = carryingReport(ageMs = RelayReport.STALENESS_MS + 1)

        val content = WidgetContent.from(corpse, now, bond = fresh)

        assertEquals(RelayVerdict.NotReporting.headline, content.headline)
        assertEquals(WidgetContent.Tone.DOWN, content.tone)
        assertTrue(
            "a widget with no live evidence of its own must not list the bond's legs",
            content.legs.isEmpty(),
        )
    }

    // ------------------------------------------------- weightIsNotMembership ---

    /** `weightIsNotMembership`: "a leg with weight that the transport does not
     *  hold is NOT carrying". [BondStatus.Path.isCarrying] already encodes it
     *  and ReserveTierTests pins the getter; what this pins is that the WIDGET
     *  reads that getter rather than reaching for `effectiveWeight` itself,
     *  which is the change that produced four legs drawn as carrying while the
     *  transport held exactly one. */
    @Test
    fun `a leg with weight but no membership is not drawn carrying`() {
        val rows = WidgetContent.legsOfBond(
            bond(path("lte", label = "Lone SIM", weight = 100, inBond = false)),
        )
        assertEquals(
            listOf(WidgetContent.Leg("Lone SIM", WidgetContent.Leg.State.NOT_IN_BOND)),
            rows,
        )
    }

    /** The same leg at weight 0 and up - the anti-flap gate holding a recovered
     *  leg out while it proves itself. Present, not carrying, and emphatically
     *  not a fault. */
    @Test
    fun `a leg at weight zero is present-not-carrying and not down`() {
        val rows = WidgetContent.legsOfBond(
            bond(path("lte", label = "Lone SIM", weight = 0, inBond = true)),
        )
        assertEquals(
            listOf(WidgetContent.Leg("Lone SIM", WidgetContent.Leg.State.PRESENT_NOT_CARRYING)),
            rows,
        )
    }

    /** `state == "up"` alone is not carrying either, the other half of the rule. */
    @Test
    fun `a leg that is up with weight is present-not-carrying`() {
        val rows = WidgetContent.legsOfBond(bond(path("wifi", label = "Cafe wifi", weight = 0)))
        assertEquals(
            listOf(WidgetContent.Leg("Cafe wifi", WidgetContent.Leg.State.PRESENT_NOT_CARRYING)),
            rows,
        )
    }

    // --------------------------------------------------- reserveIsNotBroken ---

    /** `reserveIsNotBroken`: "a leg held back by the tier gate is doing its job
     *  and must not be drawn as a failure". This is the acceptance criterion in
     *  one assertion, and the one a two-state enum cannot express. */
    @Test
    fun `a reserve leg is drawn reserve, not failure`() {
        val rows = WidgetContent.legsOfBond(
            bond(
                path("lte", label = "Lone SIM", weight = 0, tier = 3),
                path("wifi", label = "Cafe wifi", weight = 100, tier = 1),
            ),
        )

        assertEquals(WidgetContent.Leg.State.RESERVE, rows[0].state)
        assertEquals(WidgetContent.Leg.State.CARRYING, rows[1].state)
        assertNotEquals(
            "a tier-3 leg with no traffic is doing its job; drawing it as a failure " +
                "trains the reader to ignore the one row that means something",
            WidgetContent.Leg.State.DOWN,
            rows[0].state,
        )
    }

    /** Decided against the ACTIVE tier, not against tier 1. With the bond fallen
     *  to tier 2, a tier-2 leg is live and only tier 3 is in reserve. */
    @Test
    fun `reserve is decided against the active tier, not tier one`() {
        val rows = WidgetContent.legsOfBond(
            bond(
                path("lte", label = "Lone SIM", weight = 0, tier = 3),
                path("wifi", label = "Cafe wifi", weight = 100, tier = 2),
            ),
        )
        assertEquals(WidgetContent.Leg.State.RESERVE, rows[0].state)
        assertEquals(WidgetContent.Leg.State.CARRYING, rows[1].state)
    }

    /** A bond where nothing carries has no active tier, and a total outage can
     *  never be dressed up as a deliberate gate. */
    @Test
    fun `nothing carrying means nothing is in reserve`() {
        val rows = WidgetContent.legsOfBond(
            bond(path("lte", label = "Lone SIM", weight = 0, tier = 3, state = "down")),
        )
        assertEquals(
            listOf(WidgetContent.Leg("Lone SIM", WidgetContent.Leg.State.DOWN)),
            rows,
        )
    }

    // ---------------------------------------------------- shape and wording ---

    /** The word is decided in the model, never in a composable. The strings are
     *  the shared vocabulary in `design/tokens.json`'s `state` block. */
    @Test
    fun `each leg state carries its own word`() {
        fun word(s: WidgetContent.Leg.State) = WidgetContent.Leg("a", s).word
        assertEquals("carrying", word(WidgetContent.Leg.State.CARRYING))
        assertEquals("up, not carrying", word(WidgetContent.Leg.State.PRESENT_NOT_CARRYING))
        assertEquals("not in the bond", word(WidgetContent.Leg.State.NOT_IN_BOND))
        assertEquals("held in reserve", word(WidgetContent.Leg.State.RESERVE))
        assertEquals("down", word(WidgetContent.Leg.State.DOWN))
        assertEquals("not carrying", word(WidgetContent.Leg.State.IDLE))
    }

    /** A leg the router named nothing is still a row. Dropping it would make an
     *  unnamed leg look like a rendering fault; an empty section would violate
     *  `neverStateUnmeasured`. */
    @Test
    fun `a leg with no name still draws a row`() {
        val rows = WidgetContent.legsOfBond(bond(path("")))
        assertEquals(listOf(WidgetContent.Leg("Unnamed leg", WidgetContent.Leg.State.PRESENT_NOT_CARRYING)), rows)
    }

    /** Configuration, not a connection. A path with neither an interface nor a
     *  relay endpoint is filtered out of the bond by the app's own [BondLegs],
     *  and padding the widget's list with it would be inventing rows. */
    @Test
    fun `a path with no interface and no relay endpoint is not drawn`() {
        val rows = WidgetContent.legsOfBond(
            bond(
                path("unplugged", label = "Spare dongle", iface = null),
                path("lte", label = "Lone SIM", weight = 100),
            ),
        )
        assertEquals(listOf("Lone SIM"), rows.map { it.label })
    }

    /** A companion leg has no interface - it is reached over the relay socket -
     *  so filtering on the interface alone would drop this phone's own row from
     *  the very widget that exists to show it. */
    @Test
    fun `a companion leg with no interface is still drawn`() {
        val rows = WidgetContent.legsOfBond(
            bond(path("iphone", label = "This phone", weight = 100, iface = null, relayEndpoint = "10.0.0.5:51999")),
        )
        assertEquals(
            listOf(WidgetContent.Leg("This phone", WidgetContent.Leg.State.CARRYING)),
            rows,
        )
    }
}
