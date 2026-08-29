package app.zippie.companion

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Claiming the wrong leg would tell someone their phone is contributing while
 * showing another phone's traffic. Evidence or nothing.
 */
class LegIdentityTest {

    @Test
    fun `an endpoint that names this phone matches`() {
        assertTrue(LegIdentity.identifies("10.99.0.100:51999", 51999, "10.99.0.100"))
    }

    @Test
    fun `the other phone on the same wifi does not match`() {
        assertFalse(LegIdentity.identifies("10.99.0.151:51999", 51999, "10.99.0.100"))
    }

    /**
     * A config naming the right host and the wrong port describes a leg that can
     * never carry. Claiming it would hide that fault behind a friendly row.
     */
    @Test
    fun `the port must match too`() {
        assertFalse(LegIdentity.identifies("10.99.0.100:51998", 51999, "10.99.0.100"))
        assertFalse(LegIdentity.identifies("10.99.0.100", 51999, "10.99.0.100"))
        assertFalse(LegIdentity.identifies("10.99.0.100:", 51999, "10.99.0.100"))
        assertFalse(LegIdentity.identifies("10.99.0.100:abc", 51999, "10.99.0.100"))
    }

    /**
     * ISSUE #53 CRITERION 4, ON THIS SIDE OF THE WIRE. Two phones behind one
     * NAT (or one travel router's LAN) can share a host and differ only by
     * listen port - the router's own exclusion in `match_interfaces` keys on
     * `relay_endpoint`, host AND port concatenated (dynamic.py
     * `DynamicLeg.relay_endpoint`, agent.py `used_relays`), for exactly this
     * reason: a key built from the address alone would treat two distinct
     * phones as one and silently drop whichever announced second. This phone
     * telling ITSELF apart from a leg that merely shares its address is the
     * same rule applied locally - an address-only comparison here would let
     * this phone wrongly claim a neighbour's leg as its own the moment the
     * two addresses matched, port or no port.
     */
    @Test
    fun `two legs sharing a host but not a port are never confused`() {
        val localIp = "10.99.0.100"
        assertTrue(
            "this phone's own endpoint must still match",
            LegIdentity.identifies("10.99.0.100:51999", 51999, localIp),
        )
        assertFalse(
            "a neighbour phone on the same address but a different port must not " +
                "be claimed as this one",
            LegIdentity.identifies("10.99.0.100:52000", 51999, localIp),
        )
    }

    @Test
    fun `missing evidence never matches`() {
        assertFalse(LegIdentity.identifies(null, 51999, "10.99.0.100"))
        assertFalse(LegIdentity.identifies("", 51999, "10.99.0.100"))
        assertFalse(LegIdentity.identifies("10.99.0.100:51999", 51999, null))
        assertFalse(LegIdentity.identifies("10.99.0.100:51999", 51999, ""))
    }

    @Test
    fun `an IPv6 endpoint matches the bare form the interface reports`() {
        assertTrue(LegIdentity.identifies("[fe80::1]:51999", 51999, "fe80::1"))
        assertTrue(LegIdentity.identifies("[FE80::1]:51999", 51999, "fe80::1%wlan0"))
        assertFalse(LegIdentity.identifies("[fe80::2]:51999", 51999, "fe80::1"))
    }
}
