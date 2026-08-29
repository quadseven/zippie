package app.zippie.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * What the relay screen and notification are ALLOWED to say.
 *
 * Ported from ZippieCompanionKitTests/RelayVerdictTests.swift
 * (quadseven/zippie#44): a sentence about the router may only be said once
 * something has actually ARRIVED from the router, and a datagram count is not
 * evidence of that, because upDatagrams/downDatagrams never go down.
 *
 * THE DEFECT THESE TESTS REPLACE THE COVERAGE FOR. RelayStats.summary used to
 * read:
 *
 *     upDatagrams == 0L && downDatagrams == 0L -> "Listening..."
 *     else -> "Carrying for the bond"
 *
 * so a phone that had forwarded one packet an hour ago, from a router that had
 * long since stopped dialling it, still said "Carrying for the bond". Several
 * tests below exist specifically to make that mutation fail loudly - see
 * `one packet an hour ago does not read as carrying forever`.
 */
class RelayVerdictTest {

    private val now = 1_000_000_000L

    /** A live, non-stale report with cellular ready and the listener bound -
     *  the baseline every test mutates away from. */
    private fun report(ageMs: Long = 0, mutate: RelayStats.() -> RelayStats = { this }): RelayReport {
        val stats = RelayStats(cellularReady = true, listening = true).mutate()
        return RelayReport(stats, now - ageMs)
    }

    // ---- the fabricated claim (quadseven/zippie#44's Android shape) ----

    @Test
    fun `never dialled does not claim a connection to the router`() {
        val v = RelayVerdict.evaluate(report(), now)

        assertEquals(RelayVerdict.Listening, v)
        assertEquals("Ready", v.headline)
        assertEquals("Your zippie router has not sent anything to this phone yet.", v.detail())
    }


    // ---- the router's half outranks the phone's counters (#281) ----

    /**
     * THE EXACT STATE OF `.208` ON 2026-08-23, reconstructed from both ends.
     *
     * The phone had forwarded 284 datagrams and had 536 back down over
     * cellular, and the router had just dialled it - every local signal healthy.
     * The router said of that same leg: `never_handshaked: true`,
     * `link_rx_bytes: 0`, `loss_pct: 100`. The screen read "Carrying" for hours
     * over a household outage.
     */
    @Test
    fun `the router saying nothing ever arrived outranks a healthy local count`() {
        val r = report { copy(upDatagrams = 284, lastRouterInboundAtMs = now - 1_000) }

        val v = RelayVerdict.evaluate(r, now, routerSeesNothing = true)

        assertEquals(RelayVerdict.RouterSeesNothing, v)
        assertEquals("Not arriving", v.headline)
        assertFalse("must not claim the bond", v.detail().contains("part of the bond"))
    }

    /** Without the contradiction, nothing changes. A missing router opinion is
     *  not evidence against the phone. */
    @Test
    fun `the same report with no router contradiction still reads as carrying`() {
        val r = report { copy(upDatagrams = 284, lastRouterInboundAtMs = now - 1_000) }

        assertEquals(RelayVerdict.Carrying, RelayVerdict.evaluate(r, now))
        assertEquals(RelayVerdict.Carrying, RelayVerdict.evaluate(r, now, routerSeesNothing = false))
    }

    /**
     * The contradiction is reachable on the older-build path too.
     *
     * A report with no `lastRouterInboundAtMs` falls back to the datagram count,
     * which is the branch that read "Carrying" forever in #44. It must not
     * become a way back to the same overclaim.
     */
    @Test
    fun `the no-timestamp fallback cannot claim carrying against the router`() {
        val r = report { copy(upDatagrams = 12, lastRouterInboundAtMs = null) }

        assertEquals(RelayVerdict.RouterSeesNothing, RelayVerdict.evaluate(r, now, routerSeesNothing = true))
        assertEquals(RelayVerdict.Carrying, RelayVerdict.evaluate(r, now, routerSeesNothing = false))
    }

    /**
     * A phone that has sent NOTHING keeps its own, more specific verdict.
     *
     * "The router has never heard from you" and "you have never sent anything"
     * are both true here, and the second one names the thing to go and fix.
     */
    @Test
    fun `a phone that never forwarded keeps its own diagnosis`() {
        val r = report { copy(upDatagrams = 0, lastRouterInboundAtMs = now - 1_000) }

        val v = RelayVerdict.evaluate(r, now, routerSeesNothing = true)

        assertEquals(RelayVerdict.NotForwarding(null), v)
    }

    /** A local fault outranks the contradiction: no cellular is why nothing
     *  arrived, and saying so is more useful than reporting the symptom. */
    @Test
    fun `a local fault still wins over the router contradiction`() {
        val r = RelayReport(
            RelayStats(cellularReady = false, listening = true, upDatagrams = 5),
            now,
        )

        assertEquals(RelayVerdict.NoCellular(null), RelayVerdict.evaluate(r, now, routerSeesNothing = true))
    }

    /** The specific sentence from #44 must not be reachable from ANY state. */
    @Test
    fun `no verdict ever claims a connection to the router`() {
        for (v in RelayVerdict.ALL_CASES_FOR_COPY_REVIEW) {
            assertFalse("$v says: ${v.detail()}", v.detail().lowercase().contains("connected to the router"))
            assertFalse("$v headline: ${v.headline}", v.headline.lowercase().contains("connected"))
        }
    }

    /** The same pin, restated with a router name in play. */
    @Test
    fun `no verdict ever claims a connection to the router even when named`() {
        for (v in RelayVerdict.ALL_CASES_FOR_COPY_REVIEW) {
            assertFalse(
                "$v says: ${v.detail(router = "travel-router")}",
                v.detail(router = "travel-router").lowercase().contains("connected to the router"),
            )
        }
    }

    // ---- never dialled vs went quiet ----

    @Test
    fun `never dialled and went quiet read differently`() {
        val never = RelayVerdict.evaluate(report(), now)
        val quiet = RelayVerdict.evaluate(
            report { copy(upDatagrams = 42, lastRouterInboundAtMs = now - 40_000) },
            now,
        )

        assertNotEquals(never.headline, quiet.headline)
        assertNotEquals(never.detail(), quiet.detail())
    }

    @Test
    fun `went quiet says how long ago`() {
        val v = RelayVerdict.evaluate(
            report { copy(upDatagrams = 42, lastRouterInboundAtMs = now - 40_000) },
            now,
        )

        assertEquals(RelayVerdict.RouterQuiet(40_000), v)
        assertEquals("Your zippie router stopped sending. Last packet 40s ago.", v.detail())
        assertEquals("travel-router stopped sending. Last packet 40s ago.", v.detail(router = "travel-router"))
    }

    @Test
    fun `a long silence is said in minutes`() {
        val v = RelayVerdict.evaluate(
            report { copy(upDatagrams = 42, lastRouterInboundAtMs = now - 600_000) },
            now,
        )

        assertEquals("Your zippie router stopped sending. Last packet 10m ago.", v.detail())
    }

    // ---- the quiet threshold, paired with persistent_keepalive = 15 ----

    @Test
    fun `an idle but live leg is not called quiet`() {
        for (silenceMs in listOf(0L, 5_000L, 14_900L, 24_900L)) {
            val v = RelayVerdict.evaluate(
                report { copy(upDatagrams = 1, lastRouterInboundAtMs = now - silenceMs) },
                now,
            )
            assertEquals("${silenceMs}ms of silence was called quiet", RelayVerdict.Carrying, v)
        }
    }

    @Test
    fun `silence beyond the threshold is called quiet`() {
        val v = RelayVerdict.evaluate(
            report { copy(upDatagrams = 1, lastRouterInboundAtMs = now - 25_100) },
            now,
        )

        assertTrue("25.1s of silence was still called carrying: $v", v is RelayVerdict.RouterQuiet)
    }

    // ---- carriage needs evidence in BOTH directions ----

    @Test
    fun `router sending but nothing relayed is not carrying`() {
        val v = RelayVerdict.evaluate(
            report {
                copy(
                    upDatagrams = 0,
                    lastError = "up: no route to host",
                    lastRouterInboundAtMs = now - 1_000,
                )
            },
            now,
        )

        assertEquals(RelayVerdict.NotForwarding("up: no route to host"), v)
        assertEquals("Not relaying", v.headline)
        assertTrue(v.detail(), v.detail().contains("up: no route to host"))
    }

    @Test
    fun `forwarded traffic with recent inbound is carrying`() {
        val v = RelayVerdict.evaluate(
            report { copy(upDatagrams = 12, lastRouterInboundAtMs = now - 1_000) },
            now,
        )

        assertEquals(RelayVerdict.Carrying, v)
        assertEquals("Carrying", v.headline)
    }

    // ---- local faults outrank router talk ----

    @Test
    fun `a cap reached is said before anything about the router`() {
        val v = RelayVerdict.evaluate(
            report {
                copy(
                    budgetExhausted = "Daily cap of 2 GB reached.",
                    upDatagrams = 5,
                    lastRouterInboundAtMs = now - 1_000,
                )
            },
            now,
        )

        assertEquals(RelayVerdict.Paused("Daily cap of 2 GB reached."), v)
    }

    @Test
    fun `unusable cellular is said before anything about the router`() {
        val v = RelayVerdict.evaluate(
            report {
                copy(
                    cellularReady = false,
                    lastError = "cellular unavailable (interface not usable)",
                    lastRouterInboundAtMs = now - 1_000,
                )
            },
            now,
        )

        assertEquals(RelayVerdict.NoCellular("cellular unavailable (interface not usable)"), v)
    }

    @Test
    fun `no listener is said before anything about the router`() {
        val v = RelayVerdict.evaluate(
            report {
                copy(
                    listening = false,
                    lastError = "cannot listen on 51999: address in use",
                    lastRouterInboundAtMs = now - 1_000,
                )
            },
            now,
        )

        assertEquals(RelayVerdict.NotListening("cannot listen on 51999: address in use"), v)
    }

    // ---- a report that is not current speaks for nobody ----

    @Test
    fun `a stale report never speaks for the router`() {
        val v = RelayVerdict.evaluate(
            report(ageMs = 60_000) {
                copy(upDatagrams = 9, lastRouterInboundAtMs = now - 61_000)
            },
            now,
        )

        assertEquals(RelayVerdict.NotReporting, v)
    }

    /** Never-started and freshly-stopped both read as no report at all - see
     *  the type comment on RelayVerdict for why Android cannot distinguish
     *  off/starting/stopping the way iOS does with NEVPNStatus. */
    @Test
    fun `no report yet is off, not a claim about the router`() {
        val v = RelayVerdict.evaluate(null, now)

        assertEquals(RelayVerdict.Off, v)
        assertFalse(v.detail().lowercase().contains("router"))
    }

    // ---- version skew: an older build with no inbound timestamp yet ----

    @Test
    fun `forwarded traffic with no inbound timestamp is not called never dialled`() {
        val v = RelayVerdict.evaluate(
            report { copy(upDatagrams = 9, lastRouterInboundAtMs = null) },
            now,
        )

        assertEquals(RelayVerdict.Carrying, v)
    }

    // ---- THE DEFECT ----

    /**
     * THE MUTATION TARGET. RelayStats.kt used to decide this from
     * `upDatagrams > 0`, and that count never decreases - so one packet an
     * hour ago read as "Carrying for the bond" forever, exactly as it would
     * have for a router whose leg for this phone had gone dark. This is the
     * test a count-based regression makes fail.
     */
    @Test
    fun `one packet an hour ago does not read as carrying forever`() {
        val v = RelayVerdict.evaluate(
            report { copy(upDatagrams = 1, lastRouterInboundAtMs = now - 3_600_000) },
            now,
        )

        assertTrue("an hour of silence still read as carrying: $v", v is RelayVerdict.RouterQuiet)
        assertNotEquals(RelayVerdict.Carrying, v)
    }

    // ---- house voice ----

    @Test
    fun `all copy is ascii and non-empty`() {
        for (v in RelayVerdict.ALL_CASES_FOR_COPY_REVIEW) {
            assertTrue(v.headline, v.headline.all { it.code in 0..127 })
            assertTrue(v.detail(), v.detail().all { it.code in 0..127 })
            assertFalse(v.headline.isEmpty())
            assertFalse(v.detail().isEmpty())
        }
    }

    // ---- naming the router (#44 operator follow-up, 2026-08-08) ----

    /** "The router" alone reads as the wifi router this phone is joined to -
     *  a different device entirely from the zippie router. Without a name,
     *  the sentence must still say WHAT KIND of router it means. */
    @Test
    fun `unnamed router states say your zippie router rather than the router`() {
        for (v in listOf(RelayVerdict.Listening, RelayVerdict.NotForwarding(null), RelayVerdict.RouterQuiet(40_000))) {
            assertTrue(v.detail(), v.detail().startsWith("Your zippie router"))
            assertFalse(v.detail(), v.detail().contains("The router"))
        }
    }

    /** Given a name, use it. */
    @Test
    fun `named router states use the given name`() {
        for (v in listOf(RelayVerdict.Listening, RelayVerdict.NotForwarding(null), RelayVerdict.RouterQuiet(40_000))) {
            assertTrue(v.detail(router = "travel-router"), v.detail(router = "travel-router").startsWith("travel-router "))
        }
    }

    /** An empty string is not a name. */
    @Test
    fun `an empty router name falls back to the generic phrase`() {
        assertEquals(RelayVerdict.Listening.detail(router = ""), RelayVerdict.Listening.detail(router = null))
    }

    /** Naming must not leak into states that say nothing about the router. */
    @Test
    fun `naming does not leak into states that are not about the router`() {
        val v = RelayVerdict.Carrying
        assertEquals(v.detail(), v.detail(router = "travel-router"))
    }
}
