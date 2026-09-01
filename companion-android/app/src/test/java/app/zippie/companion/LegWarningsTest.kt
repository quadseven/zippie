package app.zippie.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Two conditions the agent publishes and the phone must actually show (#214).
 *
 * `never_handshaked` (#204) - a leg that has transmitted and never once been
 * answered. `shadowed_interfaces` (#212) - a usable uplink no leg took. Both
 * exist BECAUSE the underlying conditions were invisible for weeks; an alarm
 * that stops at the console has not finished the job, since the surface the
 * operator looks at is this app, in a car, away from any terminal.
 */
class LegWarningsTest {

    private fun status(path: String): BondStatus =
        BondStatus.decode("""{"mode":"aggregate","datapath":"packet","paths":[$path]}""")

    /** Each path below names an interface: rows() draws only legs that are
     *  bound to something, so a fixture without one is not a row to assert on. */
    private fun leg(path: String) =
        BondLegs.rows(status(path), listenPort = 51999, localIp = null).single()

    @Test
    fun `a never-answered leg says so instead of showing a bare last_error`() {
        val l = leg("""{"name":"ethernet","interface":"eth0","state":"degraded","never_handshaked":true,
                        "last_error":"no reply yet - nothing is answering at this leg's address"}""")
        assertTrue(l.note!!, l.note!!.contains("Never answered"))
        assertTrue("must point at the address, not the signal",
            l.note!!.contains("dialling"))
    }

    @Test
    fun `it outranks loss, which would otherwise describe the same silence twice`() {
        val l = leg("""{"name":"ethernet","interface":"eth0","state":"degraded","never_handshaked":true,"loss_pct":100.0}""")
        assertTrue(l.note!!.contains("Never answered"))
    }

    @Test
    fun `it does NOT override the reserve explanation`() {
        // A reserve leg was never going to carry, so "never answered" would
        // send someone hunting a fault that is a configuration choice.
        //
        // THE FIXTURE HAS TO BUILD THE STATE, and this one took two attempts.
        // `activeTier` is COMPUTED - the lowest tier among CARRYING paths - not
        // a field in the payload, and `isCarrying` needs effective_weight > 0.
        // So a reserve leg cannot exist alone: something must be carrying below
        // it, or there is no active tier for it to be held above. isReserve
        // also requires isPresent, hence the interface.
        //
        // Both earlier versions failed against an implementation that was
        // already correct, which is the honest shape of this bug: a fixture
        // that does not construct the state under test proves nothing about it,
        // it just fails elsewhere and looks like a defect.
        val l = BondLegs.rows(
            BondStatus.decode("""{"paths":[
                {"name":"primary","state":"up","tier":1,"interface":"eth0",
                 "effective_weight":32,"in_bond":true},
                {"name":"backup","state":"up","tier":3,"interface":"wwan0",
                 "never_handshaked":true}]}"""),
            listenPort = 51999, localIp = null)
            .first { it.id == "backup" }
        assertTrue(l.note!!, l.note!!.contains("Held in reserve"))
    }

    @Test
    fun `a shadowed uplink is named`() {
        val l = leg("""{"name":"hotspot","interface":"apclix0","state":"up","effective_weight":32,
                        "shadowed_interfaces":["apcli0"]}""")
        assertEquals("apcli0 is a working uplink that no leg is using.", l.shadowNote)
    }

    @Test
    fun `it is reported even when this leg is perfectly healthy`() {
        // THE POINT. The fault is not with this leg - it is that a neighbour is
        // missing - so hiding it behind an unhealthy state would hide it always.
        val l = leg("""{"name":"hotspot","interface":"apclix0","state":"up","effective_weight":32,"in_bond":true,
                        "shadowed_interfaces":["apcli0"]}""")
        assertNull("healthy leg has no note", l.note)
        assertTrue(l.shadowNote!!.contains("apcli0"))
    }

    @Test
    fun `two hidden uplinks read as plural`() {
        val l = leg("""{"name":"hotspot","interface":"apclix0","state":"up","shadowed_interfaces":["apcli0","wwan0"]}""")
        assertEquals("apcli0, wwan0 are working uplinks that no leg is using.", l.shadowNote)
    }

    @Test
    fun `a router that publishes neither field changes nothing`() {
        // Absent-safe: an older agent must render exactly as it does today.
        val l = leg("""{"name":"hotspot","interface":"apclix0","state":"up","effective_weight":32,"in_bond":true}""")
        assertNull(l.shadowNote)
        assertNull(l.note)
    }

    @Test
    fun `an empty shadow list is not a warning`() {
        val l = leg("""{"name":"hotspot","interface":"apclix0","state":"up","shadowed_interfaces":[]}""")
        assertNull(l.shadowNote)
    }
}
