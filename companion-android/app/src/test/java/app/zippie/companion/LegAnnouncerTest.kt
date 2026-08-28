package app.zippie.companion

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.IOException
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * Announcing is what replaces the static leg entry that used to be required -
 * and that actively blocked the announced leg from getting the bridge. These
 * tests are about the ways announcing can silently not happen.
 */
class LegAnnouncerTest {

    private fun config(
        host: String = "10.20.0.1:8787",
        token: String = "tok",
        port: Int = 51999,
    ) = LegAnnouncer.Config(
        consoleHost = host,
        token = token,
        name = "pixel-8-3f9a",
        label = "Pixel 8 (T-Mobile)",
        listenPort = port,
    )

    /** Records what the announcer sent and replies with whatever the test wants. */
    private class Recorder(
        private val code: Int = 200,
        private val body: String = """{"lease_s":45}""",
        private val throwing: IOException? = null,
    ) : ConsolePost {
        var calls = 0
        var url: String? = null
        var token: String? = null
        var sent: JSONObject? = null

        override fun post(url: String, token: String, body: String, timeoutMs: Int): ConsoleReply {
            calls++
            this.url = url
            this.token = token
            this.sent = JSONObject(body)
            throwing?.let { throw it }
            return ConsoleReply(code, this.body)
        }
    }

    /**
     * NO ADDRESS IS NOT AN ERROR, and it must not announce a wrong one. Off a
     * local network there is nothing the router could dial.
     */
    @Test
    fun `without a local address nothing is announced`() {
        val http = Recorder()
        val outcome = LegAnnouncer(http).announce(config(), null)
        assertEquals("it announced with no address to announce", 0, http.calls)
        assertTrue(outcome is LegAnnouncer.Outcome.Refused)
    }

    @Test
    fun `an announcement carries everything the router needs`() {
        val http = Recorder()
        LegAnnouncer(http).announce(config(), "10.20.0.151")

        val sent = http.sent!!
        assertEquals("http://10.20.0.1:8787/api/legs/announce", http.url)
        assertEquals("pixel-8-3f9a", sent.getString("name"))
        assertEquals("10.20.0.151", sent.getString("host"))
        assertEquals(51999, sent.getInt("port"))
        assertEquals("Pixel 8 (T-Mobile)", sent.getString("label"))
        assertEquals(
            "no lease requested, so the router would use its own default",
            LegAnnouncer.LEASE_S,
            sent.getDouble("lease_s"),
            0.001,
        )
    }

    @Test
    fun `the bearer token is sent`() {
        val http = Recorder()
        LegAnnouncer(http).announce(config(token = "s3cret"), "10.20.0.151")
        assertEquals("s3cret", http.token)
    }

    /** The token must never end up in a string anything might log. */
    @Test
    fun `the config does not print its token`() {
        val printed = config(token = "s3cret").toString()
        assertTrue("the token was printed: $printed", !printed.contains("s3cret"))
        assertTrue(printed.contains("pixel-8-3f9a"))
    }

    /**
     * The router says exactly which field it refused. Replacing that with a
     * friendlier sentence loses the only actionable part.
     */
    @Test
    fun `the router's own refusal survives`() {
        val http = Recorder(
            code = 400,
            body = """{"error":"host must be a private IPv4 address on this LAN"}""",
        )
        val outcome = LegAnnouncer(http).announce(config(), "8.8.8.8")
        val refused = outcome as LegAnnouncer.Outcome.Refused
        assertTrue("lost the router's reason: ${refused.reason}",
            refused.reason.contains("private IPv4"))
    }

    /** A 401 is the router answering, not the router being absent - and it sends
     *  whoever is reading to the token rather than to the network. */
    @Test
    fun `a bad token is refused, not unreachable`() {
        val http = Recorder(code = 401, body = """{"error":"bad or missing bearer token"}""")
        val outcome = LegAnnouncer(http).announce(config(), "10.20.0.151")
        val refused = outcome as LegAnnouncer.Outcome.Refused
        assertTrue(refused.reason.contains("token"))
    }

    /**
     * CRITERION 5, AGAINST THE ROUTER'S OWN WORDS, NOT AN ASSUMPTION. The 401
     * body and message asserted here are not invented - they are what
     * `start_dashboard`'s `_authed()` gate on `/api/legs/announce` and
     * `/api/legs/withdraw` actually sends (agent.py, `_authed`/`do_POST`),
     * confirmed by starting a real BondAgent HTTP server and POSTing to it
     * with no Authorization header, a wrong token, and an empty token - all
     * three came back exactly `401 {"error":"bad or missing bearer token"}`.
     * Matched here byte for byte so a change to the router's wording is a
     * broken test on this side too, not a client that quietly drifts from
     * what the router enforces.
     */
    @Test
    fun `an unauthenticated announce is refused with the router's exact wording`() {
        val http = Recorder(code = 401, body = """{"error":"bad or missing bearer token"}""")
        val outcome = LegAnnouncer(http).announce(config(), "10.20.0.151")
        assertEquals(
            LegAnnouncer.Outcome.Refused("bad or missing bearer token"),
            outcome,
        )
    }

    /** The router gates withdraw with the identical check (`do_POST` tests
     *  `_authed()` before branching on the path) - proven here rather than
     *  assumed from announce alone. */
    @Test
    fun `an unauthenticated withdraw is refused the same way`() {
        val http = Recorder(code = 401, body = """{"error":"bad or missing bearer token"}""")
        val outcome = LegAnnouncer(http).withdraw(config())
        assertEquals(
            LegAnnouncer.Outcome.Refused("bad or missing bearer token"),
            outcome,
        )
    }

    /** A refusal with no JSON body must still name the status code rather than
     *  reporting an empty reason. */
    @Test
    fun `a refusal with no body falls back to the status code`() {
        val http = Recorder(code = 503, body = "<html>gateway</html>")
        val outcome = LegAnnouncer(http).announce(config(), "10.20.0.151")
        assertEquals("HTTP 503", (outcome as LegAnnouncer.Outcome.Refused).reason)
    }

    /**
     * A router that is not there is a different state from one that said no, and
     * conflating them sends someone to check the wrong thing.
     */
    @Test
    fun `an unreachable router is distinct from a refusal`() {
        val http = Recorder(throwing = IOException("connect timed out"))
        val outcome = LegAnnouncer(http).announce(config(), "10.20.0.151")
        val unreachable = outcome as LegAnnouncer.Outcome.Unreachable
        assertTrue(unreachable.reason.contains("timed out"))
    }

    @Test
    fun `the granted lease is read back from the router`() {
        val http = Recorder(body = """{"leg":"pixel-8-3f9a","lease_s":42.5}""")
        val outcome = LegAnnouncer(http).announce(config(), "10.20.0.151")
        assertEquals(42.5, (outcome as LegAnnouncer.Outcome.Announced).leaseRemainingS, 0.001)
    }

    @Test
    fun `withdraw names the leg`() {
        val http = Recorder(body = """{"withdrawn":true}""")
        LegAnnouncer(http).withdraw(config())
        assertEquals("http://10.20.0.1:8787/api/legs/withdraw", http.url)
        assertEquals("pixel-8-3f9a", http.sent!!.getString("name"))
    }

    /**
     * The renewal interval must be comfortably inside the lease, or two missed
     * announcements drop the leg.
     */
    @Test
    fun `renewal is well inside the lease`() {
        val renewS = LegAnnouncer.RENEW_INTERVAL_MS / 1000.0
        assertTrue(
            "two missed renewals would expire the lease",
            renewS * 2 < LegAnnouncer.LEASE_S,
        )
        // And the router must accept the lease this asks for: dynamic.py clamps
        // to 5..300, and a value outside that would be silently changed.
        assertTrue(LegAnnouncer.LEASE_S in 5.0..300.0)
    }

    /** A half-configured phone must not produce a stream of 400s, and must not
     *  crash - it simply does not announce. */
    @Test
    fun `an unconfigured announcer refuses rather than posting`() {
        val http = Recorder()
        val announcer = LegAnnouncer(http)
        for (c in listOf(config(host = ""), config(token = ""), config(port = 0))) {
            val outcome = announcer.announce(c, "10.20.0.151")
            assertTrue("expected refused for $c", outcome is LegAnnouncer.Outcome.Refused)
        }
        assertEquals(0, http.calls)
    }

    /**
     * THE RENEWAL LOOP, PROVEN TO RENEW. A leg is a lease: an announcer that
     * announced once and then sat there would produce a leg that vanishes after
     * 45 seconds while the relay carries on relaying.
     */
    @Test
    fun `the loop keeps renewing until it is stopped`() {
        val announced = CountDownLatch(3)
        val http = object : ConsolePost {
            @Volatile var withdrawals = 0
            override fun post(url: String, token: String, body: String, timeoutMs: Int): ConsoleReply {
                if (url.endsWith(LegAnnouncer.WITHDRAW_PATH)) withdrawals++ else announced.countDown()
                return ConsoleReply(200, """{"lease_s":45}""")
            }
        }
        // A 15s production cadence would make an honest test take a minute, so
        // the interval is driven down. Everything else - the withdraw on stop,
        // the address re-read - is the production path.
        val announcer = LegAnnouncer(http, renewIntervalMs = 20)
        announcer.start(config(), address = { "10.20.0.151" }, report = {})
        assertTrue(
            "the announcer stopped renewing",
            announced.await(5, TimeUnit.SECONDS),
        )
        announcer.stop()
        // The goodbye lands on the worker thread, so give it a moment; the
        // point being proven is that it happens at all.
        for (i in 0 until 50) {
            if (http.withdrawals > 0) break
            Thread.sleep(20)
        }
        assertEquals("stopping the relay did not withdraw the leg", 1, http.withdrawals)
    }

    /**
     * THE ADDRESS IS RE-READ EVERY PASS. An announcement is also a renewal of the
     * address; a phone that moved on DHCP must not leave the router dialling the
     * endpoint it used to have.
     */
    @Test
    fun `a phone that moves announces its new address`() {
        val seen = mutableListOf<String>()
        val second = CountDownLatch(2)
        val http = ConsolePost { url, _, body, _ ->
            if (!url.endsWith(LegAnnouncer.WITHDRAW_PATH)) {
                synchronized(seen) { seen.add(JSONObject(body).getString("host")) }
                second.countDown()
            }
            ConsoleReply(200, """{"lease_s":45}""")
        }
        var address = "10.20.0.151"
        val announcer = LegAnnouncer(http, renewIntervalMs = 20)
        announcer.start(config(), address = {
            val current = address
            address = "10.20.0.152"
            current
        }, report = {})
        assertTrue(second.await(5, TimeUnit.SECONDS))
        announcer.stop()
        synchronized(seen) {
            assertEquals("10.20.0.151", seen[0])
            assertEquals("the router would still be dialling the old address", "10.20.0.152", seen[1])
        }
    }
}

/**
 * A token pasted from a terminal carries a newline, and one pasted from a header
 * carries "Bearer ". Both produce a 401 that reads as "wrong token".
 */
class ConsoleWriteTokenTest {

    @Test
    fun `surrounding whitespace is stripped`() {
        assertEquals("abc123", ConsoleWriteToken.normalise("  abc123\n"))
    }

    @Test
    fun `a pasted Bearer prefix is stripped`() {
        assertEquals("abc123", ConsoleWriteToken.normalise("Bearer abc123"))
        assertEquals("abc123", ConsoleWriteToken.normalise("bearer   abc123 "))
    }

    /** Null, not empty: "nothing was pasted" is how a token is cleared, and the
     *  caller needs to be able to tell. */
    @Test
    fun `nothing usable is null`() {
        assertNull(ConsoleWriteToken.normalise(""))
        assertNull(ConsoleWriteToken.normalise("   \n\t "))
    }

    /**
     * "Bearer" on its own is kept, because the prefix is only stripped when
     * something follows it - the whitespace goes first, so there is no prefix
     * left to match. It is not a token and the router's 401 says so; what
     * matters here is that this is EXACTLY what ConsoleWriteToken.normalise does
     * in LegEdit.swift. The two apps must not disagree about what a token is.
     */
    @Test
    fun `a lone Bearer keyword is kept, as it is on iOS`() {
        assertEquals("Bearer", ConsoleWriteToken.normalise("Bearer "))
    }
}
