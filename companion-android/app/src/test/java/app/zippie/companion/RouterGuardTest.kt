package app.zippie.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.net.InetAddress

/**
 * The relay's only security layer, proven rather than assumed.
 *
 * It forwards opaque bytes and cannot authenticate what it carries, so the
 * question "who may talk to it" is the whole of the defence. Each test here
 * corresponds to a hole that was live in the shipped iOS build until
 * 2026-08-05.
 */
class RouterGuardTest {

    private fun addr(s: String): InetAddress = InetAddress.getByName(s)

    @Test
    fun `a router on the LAN is accepted and becomes the reply target`() {
        val guard = RouterGuard()
        assertTrue(guard.accept(addr("10.99.0.1"), 40000, nowMs = 0))
        assertEquals(addr("10.99.0.1"), guard.peer!!.address)
        assertEquals(40000, guard.peer!!.port)
        assertEquals(0L, guard.rejected)
    }

    /**
     * THE OPEN RELAY. A public source on hotel wifi could otherwise have its
     * bytes forwarded over someone else's cellular into the home transport.
     */
    @Test
    fun `a non-local source is refused and never becomes the reply target`() {
        val guard = RouterGuard()
        assertFalse(guard.accept(addr("8.8.8.8"), 40000, nowMs = 0))
        assertFalse(guard.accept(addr("100.100.100.100"), 40000, nowMs = 0))
        assertEquals("carrier and tailnet space is not the router", 2L, guard.rejected)
        assertEquals(null, guard.peer)
    }

    @Test
    fun `every private range the router can legitimately sit in is accepted`() {
        for (host in listOf("10.0.0.1", "192.168.8.1", "172.16.0.1", "172.31.255.1",
                "169.254.1.1", "127.0.0.1")) {
            assertTrue(host, RouterGuard.isPlausibleRouter(addr(host)))
        }
        for (host in listOf("8.8.8.8", "172.15.0.1", "172.32.0.1", "100.64.0.1")) {
            assertFalse(host, RouterGuard.isPlausibleRouter(addr(host)))
        }
    }

    /**
     * THE REPLY HIJACK. One datagram from anyone else on the LAN must not
     * redirect home's replies to them mid-session.
     */
    @Test
    fun `a second local source cannot displace a live router`() {
        val guard = RouterGuard(takeoverIdleMs = 5_000)
        assertTrue(guard.accept(addr("10.99.0.1"), 40000, nowMs = 1_000))
        assertFalse(guard.accept(addr("10.99.0.55"), 40000, nowMs = 2_000))
        assertEquals(addr("10.99.0.1"), guard.peer!!.address)
        assertEquals(1L, guard.rejected)
    }

    @Test
    fun `a router that has gone quiet can be replaced`() {
        val guard = RouterGuard(takeoverIdleMs = 5_000)
        assertTrue(guard.accept(addr("10.99.0.1"), 40000, nowMs = 1_000))
        assertFalse(guard.accept(addr("10.99.0.55"), 40000, nowMs = 5_999))
        assertTrue("past the idle window the relay must recover",
            guard.accept(addr("10.99.0.55"), 40000, nowMs = 6_001))
        assertEquals(addr("10.99.0.55"), guard.peer!!.address)
    }

    /**
     * The router's source port is ephemeral. A restart must not be locked out
     * for the length of the takeover window - that would strand the leg for
     * exactly as long as the guard is protecting it.
     */
    @Test
    fun `the same router on a new port is followed immediately`() {
        val guard = RouterGuard(takeoverIdleMs = 5_000)
        assertTrue(guard.accept(addr("10.99.0.1"), 40000, nowMs = 1_000))
        assertTrue(guard.accept(addr("10.99.0.1"), 41111, nowMs = 1_500))
        assertEquals(41111, guard.peer!!.port)
        assertEquals(0L, guard.rejected)
    }

    @Test
    fun `continued traffic from the live router keeps the takeover window shut`() {
        val guard = RouterGuard(takeoverIdleMs = 5_000)
        assertTrue(guard.accept(addr("10.99.0.1"), 40000, nowMs = 0))
        assertTrue(guard.accept(addr("10.99.0.1"), 40000, nowMs = 4_000))
        assertTrue(guard.accept(addr("10.99.0.1"), 40000, nowMs = 8_000))
        assertFalse("the window is measured from the last packet, not from adoption",
            guard.accept(addr("10.99.0.99"), 40000, nowMs = 9_000))
    }
}
