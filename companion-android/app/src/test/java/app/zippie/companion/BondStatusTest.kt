package app.zippie.companion

import org.json.JSONException
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Decoded against a REAL payload, fetched from the live router on 2026-08-05
 * (app/src/test/resources/router-status-2026-08-05.json). Only the operator's
 * home endpoint and its public address were replaced in that file; every field
 * the decoder reads is exactly what the router sent.
 *
 * A hand-written fixture would have been agreeable and wrong: the two facts
 * these tests pin - that a weighted leg can be out of the bond, and that the
 * packet datapath reports bytes in link_tx_bytes while tx_bytes stays 0 - are
 * both things nobody would have invented.
 */
class BondStatusTest {

    private fun fixture(): BondStatus {
        val stream = javaClass.classLoader!!.getResourceAsStream("router-status-2026-08-05.json")
        assertNotNull("fixture missing", stream)
        return BondStatus.decode(stream!!.bufferedReader().use { it.readText() })
    }

    private fun path(name: String): BondStatus.Path =
        fixture().paths!!.first { it.name == name }

    @Test
    fun `decodes every path the router listed`() {
        val status = fixture()
        assertEquals("aggregate", status.mode)
        assertEquals("packet", status.datapath)
        assertEquals("hotspot", status.primary)
        assertEquals(5, status.paths!!.size)
    }

    /**
     * THE BUG THIS FILE EXISTS FOR. ethernet carries effective_weight 40 and is
     * NOT in the bond. Deciding "carrying" from weight showed four legs carrying
     * while the transport held exactly one.
     */
    @Test
    fun `weight without bond membership is not carrying`() {
        val ethernet = path("ethernet")
        assertEquals(40, ethernet.effectiveWeight)
        assertEquals(false, ethernet.inBond)
        assertFalse("weight alone must not count as carrying", ethernet.isCarrying)
        assertEquals("not in the bond", ethernet.stateWord)
    }

    @Test
    fun `the one leg the transport holds is the only one carrying`() {
        val status = fixture()
        assertEquals(1, status.carryingCount)
        assertTrue(path("hotspot").isCarrying)
        assertEquals("carrying", path("hotspot").stateWord)
        assertEquals(1, status.activeTier)
    }

    /**
     * A leg gated by tier is not broken. Ethernet sits on tier 2 while tier 1 is
     * carrying, so it is in reserve; and if the bond ever falls to tier 2, the
     * same leg stops being in reserve without anything about it changing.
     */
    @Test
    fun `reserve is measured against the active tier, not against tier one`() {
        val ethernet = path("ethernet")
        assertTrue(ethernet.isHeldInReserve(activeTier = 1))
        assertFalse(ethernet.isHeldInReserve(activeTier = 2))
        assertFalse("unknown active tier cannot imply reserve",
            ethernet.isHeldInReserve(activeTier = null))
    }

    /**
     * An unmeasured RTT must stay unmeasured. org.json's optDouble would have
     * answered 0.0 here, and "0 ms" is the most confident lie this screen could
     * tell.
     */
    @Test
    fun `absent measurements decode to null and measured zeroes do not`() {
        val ethernet = path("ethernet")
        assertNull("rtt_ms was null in the payload", ethernet.rttMs)
        assertEquals("a measured zero is a value", 0.0, ethernet.lossPct!!, 0.0001)
        assertNull("no interface was matched", path("dongle4g").interfaceName)
        assertEquals(56.72495000180788, path("hotspot").rttMs!!, 0.000001)
    }

    /**
     * In packet mode the router reports per-leg traffic in link_tx_bytes /
     * link_rx_bytes; tx_bytes and rx_bytes are hard 0 on every leg. Reading the
     * old pair drew the one carrying leg as having moved nothing.
     */
    @Test
    fun `carried bytes come from the packet datapath counters`() {
        val hotspot = path("hotspot")
        assertEquals(0L, hotspot.txBytes)
        assertEquals(11018643L, hotspot.linkTxBytes)
        assertEquals(11018643L, hotspot.carriedTxBytes)
        assertEquals(31703754L, hotspot.carriedRxBytes)
    }

    @Test
    fun `the router's own words survive decoding`() {
        assertEquals("awaiting transport", path("ethernet").lastError)
        assertEquals("no interface matched", path("dongle4g").lastError)
        assertNull("a healthy leg has no error text", path("hotspot").lastError)
    }

    @Test
    fun `companion legs are identified by the endpoint the router dials`() {
        assertTrue(path("companion-co-operator").isCompanion)
        assertEquals("10.99.0.100:51999", path("companion-co-operator").relayEndpoint)
        assertFalse("a physical leg has an empty relay endpoint", path("ethernet").isCompanion)
        assertTrue(path("ethernet").isPresent)
        assertFalse("configured but absent is not present", path("dongle4g").isPresent)
    }

    @Test
    fun `a payload with none of our fields decodes to unknowns, not to zeroes`() {
        val status = BondStatus.decode("""{"paths":[{"name":"x"}]}""")
        val p = status.paths!!.single()
        assertNull(p.effectiveWeight)
        assertNull(p.inBond)
        assertNull(p.rttMs)
        assertNull(p.tier)
        assertNull(p.carriedTxBytes)
        assertFalse("no weight means not carrying", p.isCarrying)
        assertEquals("idle", p.stateWord)
        assertNull("nothing carries, so there is no active tier", status.activeTier)
    }

    @Test
    fun `a missing paths key is not an empty bond`() {
        assertNull(BondStatus.decode("""{"mode":"aggregate"}""").paths)
        assertEquals(0, BondStatus.decode("""{"paths":[]}""").paths!!.size)
    }

    @Test(expected = JSONException::class)
    fun `a body that is not json is rejected rather than guessed at`() {
        BondStatus.decode("<html>login required</html>")
    }

    /**
     * A router old enough not to publish in_bond must keep the previous
     * behaviour rather than reading as "nothing is carrying".
     */
    @Test
    fun `without in_bond the decision falls back to weight`() {
        val p = BondStatus.decode("""{"paths":[{"name":"x","effective_weight":10}]}""")
            .paths!!.single()
        assertTrue(p.isCarrying)
    }
}
