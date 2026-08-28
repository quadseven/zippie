package app.zippie.companion

import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayOutputStream
import java.io.InputStream
import java.io.OutputStream
import java.net.HttpURLConnection
import java.net.URL

/**
 * THE COLD-BOOT DEADLOCK (#168).
 *
 * Observed live 2026-08-14. The router and the Pixel powered on together, the
 * Pixel joined the router's wifi, and the app reported:
 *
 *     failed to connect to /10.20.0.1 (port 8787) from /192.0.0.4 (port 51358)
 *
 * `192.0.0.4` is the RFC 7335 address Android's clatd puts on the 464XLAT
 * interface for an IPv6-only carrier. The phone dialled the router's LAN address
 * THROUGH CELLULAR, because the router had no uplink yet, so Android had left the
 * wifi unvalidated, so the default network was cellular.
 *
 * Every step was individually correct and the whole was a deadlock: no console,
 * so no announce, so no leg, so no uplink for the router, so the wifi never
 * validated.
 *
 * These tests pin the routing decision - WHICH network each request leaves by -
 * because that is the whole of the bug. They do not need a device: the decision
 * is made in `WifiRoute`, and a fake one records what was asked of it.
 */
class WifiPinnedConsoleTest {

    /** A WifiRoute that records what it was asked to open, and can be absent. */
    private class FakeWifi(
        private val body: String = """{"legs":[]}""",
        private val present: Boolean = true,
    ) : WifiRoute {
        val opened = mutableListOf<String>()

        override fun open(url: String): HttpURLConnection? {
            opened += url
            return if (present) FakeConnection(url, body) else null
        }
    }

    private class FakeConnection(
        url: String,
        private val body: String,
        private val code: Int = 200,
    ) : HttpURLConnection(URL(url)) {
        val written = ByteArrayOutputStream()
        override fun connect() {}
        override fun disconnect() {}
        override fun usingProxy() = false
        override fun getResponseCode() = code
        override fun getInputStream(): InputStream = body.byteInputStream()
        override fun getOutputStream(): OutputStream = written
    }

    private fun local(url: String) = ConsoleCandidate(url, isLocal = true)
    private fun remote(url: String) = ConsoleCandidate(url, isLocal = false)

    // ------------------------------------------------------- the reported bug

    @Test
    fun `the LAN console is dialled over wifi, not the default network`() = runBlocking {
        val wifi = FakeWifi()
        val (result, proximity) = BondStatusClient.probe(
            listOf(local("http://10.20.0.1:8787/api/status")),
            wifi = wifi,
        )

        assertEquals(
            "the LAN console was not dialled over wifi, so on a cold boot it " +
                "would leave via clat and never arrive",
            listOf("http://10.20.0.1:8787/api/status"),
            wifi.opened,
        )
        assertTrue("the console should have answered", result is ConsoleResult.Ok)
        assertEquals(RouterProximity.LOCAL, proximity)
    }

    @Test
    fun `the tailnet console is NOT forced over wifi`() = runBlocking {
        val wifi = FakeWifi()
        BondStatusClient.probe(
            listOf(remote("https://suzu.example.ts.net/api/status")),
            wifi = wifi,
        )

        assertTrue(
            "the tailnet address was pinned to wifi, which would break the one " +
                "path that is supposed to work over cellular from anywhere: ${wifi.opened}",
            wifi.opened.isEmpty(),
        )
    }

    @Test
    fun `with both candidates only the local one is pinned`() = runBlocking {
        val wifi = FakeWifi()
        BondStatusClient.probe(
            listOf(
                local("http://10.20.0.1:8787/api/status"),
                remote("https://suzu.example.ts.net/api/status"),
            ),
            wifi = wifi,
        )

        assertEquals(
            listOf("http://10.20.0.1:8787/api/status"),
            wifi.opened,
        )
    }

    @Test
    fun `no wifi network is reported, not quietly sent over cellular`() = runBlocking {
        val wifi = FakeWifi(present = false)
        val (result, proximity) = BondStatusClient.probe(
            listOf(local("http://10.20.0.1:8787/api/status")),
            wifi = wifi,
        )

        val failed = result as? ConsoleResult.Failed
        assertTrue("expected a failure, got $result", failed != null)
        assertTrue(
            "the message must name the missing wifi - it sends an operator " +
                "somewhere different from a router that did not answer: ${failed?.message}",
            failed!!.message.contains("no wifi network"),
        )
        assertEquals(RouterProximity.UNREACHABLE, proximity)
    }

    // ------------------------------------- the announce, which breaks the loop

    @Test
    fun `the announce is posted over wifi`() {
        val wifi = FakeWifi(body = """{"lease_s":45}""")
        val outcome = LegAnnouncer(HttpConsolePost(wifi)).announce(
            LegAnnouncer.Config(
                consoleHost = "10.20.0.1:8787",
                token = "tok",
                name = "pixel-6a-17d0",
                label = "Pixel 6a (Google Fi)",
                listenPort = 51999,
            ),
            "10.20.0.174",
        )

        assertEquals(
            "the announce did not go over wifi - this is the request that puts " +
                "the phone in the bond and gives the router its uplink",
            1,
            wifi.opened.size,
        )
        assertTrue(
            "announced to the wrong place: ${wifi.opened.first()}",
            wifi.opened.first().startsWith("http://10.20.0.1:8787"),
        )
        assertTrue("expected the announce to be accepted, got $outcome",
            outcome is LegAnnouncer.Outcome.Announced)
    }

    @Test
    fun `an announce with no wifi is unreachable rather than sent over cellular`() {
        val wifi = FakeWifi(present = false)
        val outcome = LegAnnouncer(HttpConsolePost(wifi)).announce(
            LegAnnouncer.Config(
                consoleHost = "10.20.0.1:8787",
                token = "tok",
                name = "pixel-6a-17d0",
                label = "Pixel 6a (Google Fi)",
                listenPort = 51999,
            ),
            "10.20.0.174",
        )

        val unreachable = outcome as? LegAnnouncer.Outcome.Unreachable
        assertTrue("expected Unreachable, got $outcome", unreachable != null)
        assertTrue(
            "the reason must name the missing wifi: ${unreachable?.reason}",
            unreachable!!.reason.contains("no wifi network"),
        )
    }

    @Test
    fun `an unpinned post still uses the default network, so the seam is real`() {
        // Guards the test doubles themselves: if HttpConsolePost ignored its
        // WifiRoute entirely, every assertion above would pass vacuously.
        val wifi = FakeWifi()
        HttpConsolePost(wifi).post("http://10.20.0.1:8787/api/legs", "tok", "{}", 500)
        assertFalse("the route was never consulted", wifi.opened.isEmpty())
    }
}
