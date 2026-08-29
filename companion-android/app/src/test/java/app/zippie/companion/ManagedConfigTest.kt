package app.zippie.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The merge rule, which is the only part of managed configuration that can be
 * wrong in a way nobody notices.
 *
 * The dangerous direction is SUBTRACTION. Android hands an app an empty
 * restrictions Bundle in entirely ordinary situations - unmanaged device, no
 * policy set yet, a policy that configures only some keys. If absent meant
 * "clear it", every unmanaged phone would wipe its own working configuration on
 * launch, and a policy that pushed only the token would blank the router
 * address with it.
 */
class ManagedConfigTest {

    private val stored = RelayConfiguration(
        listenPort = 51999,
        homeHost = "home.example",
        homePort = 51902,
        consoleLanHost = "10.99.0.1:8787",
        announceToken = "locally-typed-token",
    )

    // ----- the point of the feature

    @Test
    fun `a pushed token replaces the local one`() {
        val out = ManagedConfig.merge(stored, mapOf(ManagedConfig.KEY_TOKEN to "from-mdm"))
        assertEquals("from-mdm", out.announceToken)
    }

    @Test
    fun `a pushed console host replaces the local one`() {
        val out = ManagedConfig.merge(
            stored, mapOf(ManagedConfig.KEY_CONSOLE_LAN_HOST to "10.99.0.1:8787 "),
        )
        assertEquals("trailing whitespace must not survive into a URL", "10.99.0.1:8787", out.consoleLanHost)
    }

    // ----- the dangerous direction

    @Test
    fun `an empty bundle changes nothing`() {
        assertEquals(
            "an unmanaged phone must keep working; empty is the NORMAL case",
            stored, ManagedConfig.merge(stored, emptyMap()),
        )
    }

    @Test
    fun `a policy that sets only the token leaves every other field alone`() {
        val out = ManagedConfig.merge(stored, mapOf(ManagedConfig.KEY_TOKEN to "from-mdm"))
        assertEquals(stored.consoleLanHost, out.consoleLanHost)
        assertEquals(stored.homeHost, out.homeHost)
        assertEquals(stored.homePort, out.homePort)
        assertEquals(stored.listenPort, out.listenPort)
    }

    @Test
    fun `a blank value does not clear a stored one`() {
        val out = ManagedConfig.merge(
            stored,
            mapOf(ManagedConfig.KEY_TOKEN to "   ", ManagedConfig.KEY_CONSOLE_LAN_HOST to ""),
        )
        assertEquals(
            "blank must read as 'not set', or a half-filled MDM form silently unconfigures a phone",
            "locally-typed-token", out.announceToken,
        )
        assertEquals("10.99.0.1:8787", out.consoleLanHost)
    }

    @Test
    fun `a null value does not clear a stored one`() {
        val out = ManagedConfig.merge(stored, mapOf(ManagedConfig.KEY_TOKEN to null))
        assertEquals("locally-typed-token", out.announceToken)
    }

    // ----- ports arrive as strings from Headwind

    @Test
    fun `ports pushed as text are accepted`() {
        val out = ManagedConfig.merge(
            stored,
            mapOf(ManagedConfig.KEY_LISTEN_PORT to "51888", ManagedConfig.KEY_HOME_PORT to "51903"),
        )
        assertEquals(
            "Headwind's settings UI stores everything as text; refusing that makes the feature look broken",
            51888, out.listenPort,
        )
        assertEquals(51903, out.homePort)
    }

    @Test
    fun `ports pushed as numbers are accepted`() {
        val out = ManagedConfig.merge(stored, mapOf(ManagedConfig.KEY_LISTEN_PORT to 51888))
        assertEquals(51888, out.listenPort)
    }

    @Test
    fun `a nonsense port is ignored rather than applied`() {
        for (bad in listOf("0", "70000", "-1", "abc", "")) {
            val out = ManagedConfig.merge(stored, mapOf(ManagedConfig.KEY_LISTEN_PORT to bad))
            assertEquals(
                "$bad must not become a listen port - binding 0 would take a random one and " +
                    "the router would dial a port nothing is on",
                51999, out.listenPort,
            )
        }
    }

    // ----- auto start is an action, not a setting

    @Test
    fun `auto start defaults to false when absent`() {
        assertFalse(
            "a phone must never start relaying because a key was missing",
            ManagedConfig.autoStart(emptyMap()),
        )
    }

    @Test
    fun `auto start accepts both boolean and text`() {
        assertTrue(ManagedConfig.autoStart(mapOf(ManagedConfig.KEY_AUTO_START to true)))
        assertTrue(ManagedConfig.autoStart(mapOf(ManagedConfig.KEY_AUTO_START to "true")))
        assertTrue(ManagedConfig.autoStart(mapOf(ManagedConfig.KEY_AUTO_START to "TRUE")))
        assertFalse(ManagedConfig.autoStart(mapOf(ManagedConfig.KEY_AUTO_START to "false")))
        assertFalse(ManagedConfig.autoStart(mapOf(ManagedConfig.KEY_AUTO_START to "yes")))
    }

    // ----- honesty on the diagnostics screen

    @Test
    fun `managedKeys reports only what was actually pushed`() {
        assertEquals(emptyList<String>(), ManagedConfig.managedKeys(emptyMap()))
        assertEquals(
            listOf(ManagedConfig.KEY_TOKEN),
            ManagedConfig.managedKeys(
                mapOf(ManagedConfig.KEY_TOKEN to "x", ManagedConfig.KEY_HOME_HOST to "  "),
            ),
        )
    }

    /** The token is what makes this feature worth having, so the merged result
     *  must actually produce a usable announce config. */
    @Test
    fun `a phone with no local token becomes announce-capable from policy alone`() {
        val blank = stored.copy(announceToken = "")
        assertEquals(null, blank.announceConfig("pixel", "Pixel"))
        val managed = ManagedConfig.merge(blank, mapOf(ManagedConfig.KEY_TOKEN to "from-mdm"))
        val cfg = managed.announceConfig("pixel", "Pixel")
        assertTrue("policy alone should make the phone able to announce", cfg != null)
        assertEquals("from-mdm", cfg!!.token)
    }

    // ----- the no-defaults world (#156)

    /**
     * Once the compiled defaults are empty, an all-blank stored configuration is
     * the COMMON starting case, not an edge case. Every test above starts from a
     * populated fixture, so none of them exercised it.
     */
    @Test
    fun `an unconfigured phone merges a full policy without keeping any blanks`() {
        val blank = RelayConfiguration()
        assertEquals("", blank.homeHost)
        assertEquals("", blank.consoleLanHost)
        assertEquals("", blank.consoleUrl)

        val out = ManagedConfig.merge(
            blank,
            mapOf(
                ManagedConfig.KEY_HOME_HOST to "home.example",
                ManagedConfig.KEY_CONSOLE_LAN_HOST to "192.168.8.1:8787",
                ManagedConfig.KEY_CONSOLE_URL to "https://console.example/api/status",
                ManagedConfig.KEY_TOKEN to "from-mdm",
            ),
        )
        assertEquals("home.example", out.homeHost)
        assertEquals("192.168.8.1:8787", out.consoleLanHost)
        assertEquals("https://console.example/api/status", out.consoleUrl)
        assertEquals("from-mdm", out.announceToken)
    }

    @Test
    fun `an unconfigured phone with no policy stays inert rather than guessing`() {
        val out = ManagedConfig.merge(RelayConfiguration(), emptyMap())
        assertEquals("nothing may be invented to fill a blank", "", out.homeHost)
        assertEquals("", out.consoleLanHost)
        assertEquals("", out.consoleUrl)
    }

    // ----- the credential boundary

    @Test
    fun `a public console address is REFUSED, because that is where the token goes`() {
        for (bad in listOf(
            "8.8.8.8:8787", "1.1.1.1:80", "203.0.113.5:8787",
            "evil.example.com:8787",          // hostname: unjudgeable, so refused
            "172.32.0.1:8787",                // just outside 172.16/12
            "192.169.0.1:8787",               // just outside 192.168/16
        )) {
            val out = ManagedConfig.merge(stored, mapOf(ManagedConfig.KEY_CONSOLE_LAN_HOST to bad))
            assertEquals(
                "$bad must be refused - accepting it posts the router write token in " +
                    "cleartext to a host the operator does not control",
                stored.consoleLanHost, out.consoleLanHost,
            )
        }
    }

    @Test
    fun `private console addresses are accepted across every RFC1918 range`() {
        for (good in listOf(
            "10.99.0.1:8787", "172.16.0.1:8787", "172.31.255.254:8787",
            "192.168.8.1:8787", "127.0.0.1:8787", "localhost:8787",
        )) {
            val out = ManagedConfig.merge(stored, mapOf(ManagedConfig.KEY_CONSOLE_LAN_HOST to good))
            assertEquals("$good is a legitimate console address", good, out.consoleLanHost)
        }
    }

    @Test
    fun `a malformed console address is refused rather than half-parsed`() {
        for (bad in listOf("10.99.0.1", "10.99.0.1:", ":8787", "10.99.0.1:0", "10.99.0.1:99999")) {
            val out = ManagedConfig.merge(stored, mapOf(ManagedConfig.KEY_CONSOLE_LAN_HOST to bad))
            assertEquals(stored.consoleLanHost, out.consoleLanHost)
        }
    }

    @Test
    fun `the tailnet console URL must be https, never http`() {
        val out = ManagedConfig.merge(
            stored, mapOf(ManagedConfig.KEY_CONSOLE_URL to "http://console.example/api/status"),
        )
        assertEquals(
            "http reaches everywhere on earth, so there is no trusted-LAN argument for it",
            stored.consoleUrl, out.consoleUrl,
        )
        val ok = ManagedConfig.merge(
            stored, mapOf(ManagedConfig.KEY_CONSOLE_URL to "https://console.example/api/status"),
        )
        assertEquals("https://console.example/api/status", ok.consoleUrl)
    }

    // ---- telemetry credential (#186) ----------------------------------

    @Test
    fun `an MDM may push the datadog token`() {
        val out = ManagedConfig.merge(
            RelayConfiguration(),
            mapOf(ManagedConfig.KEY_DD_TOKEN to "pubXYZ", ManagedConfig.KEY_DD_SITE to "datadoghq.eu"),
        )
        assertEquals("pubXYZ", out.ddClientToken)
        assertEquals("datadoghq.eu", out.ddSite)
    }

    @Test
    fun `a blank push does not wipe a token the device already has`() {
        // Headwind sends every declared key, blank ones included. Treating blank
        // as "clear" would silence a phone's telemetry on the next MDM sync.
        val stored = RelayConfiguration(ddClientToken = "keepme", ddSite = "datadoghq.com")
        val out = ManagedConfig.merge(
            stored,
            mapOf(ManagedConfig.KEY_DD_TOKEN to "   ", ManagedConfig.KEY_DD_SITE to ""),
        )
        assertEquals("keepme", out.ddClientToken)
        assertEquals("datadoghq.com", out.ddSite)
    }

    @Test
    fun `the datadog keys are pushable at all`() {
        // KEYS is what AndroidManagedConfig reads out of the restrictions
        // Bundle. A field that merge() honours but KEYS omits can never be set
        // by an MDM - a credential path that looks wired and is not.
        assertTrue(ManagedConfig.KEYS.contains(ManagedConfig.KEY_DD_TOKEN))
        assertTrue(ManagedConfig.KEYS.contains(ManagedConfig.KEY_DD_SITE))
    }
}
