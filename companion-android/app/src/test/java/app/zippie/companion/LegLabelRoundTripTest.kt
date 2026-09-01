package app.zippie.companion

import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * A repeater leg names itself from the SSID it is associated to (#153), and an
 * SSID is arbitrary bytes chosen by whoever runs the access point - spaces,
 * accents, emoji, CJK. The agent side already proves the label survives its own
 * JSON round trip; this is the other half of that acceptance criterion, which
 * says the label must survive "round-trip to BOTH apps".
 *
 * Worth testing rather than assuming. The label is the ONLY leg field derived
 * from a string this project does not control, so it is the only one where a
 * hotel access point called `Café ☕` can produce a row that reads `Caf?` or
 * `Wi-Fi Repeater - ` in a car, on a phone, with nobody able to explain why.
 * The failure would be silent and cosmetic-looking, which is exactly the kind
 * that survives for months.
 */
class LegLabelRoundTripTest {

    private fun statusWith(label: String): String = """
        {"mode":"aggregate","datapath":"packet","paths":[
          {"name":"hotspot","label":${org.json.JSONObject.quote(label)},
           "interface":"apclix0",
           "state":"up","effective_weight":32,"in_bond":true}
        ]}
    """.trimIndent()

    private fun legName(label: String): String {
        val status = BondStatus.decode(statusWith(label))
        return BondLegs.rows(status, listenPort = 51999, localIp = null).single().name
    }

    @Test
    fun `an ssid with spaces survives into the leg row`() {
        assertEquals("Wi-Fi Repeater - Guest Network 5G",
            legName("Wi-Fi Repeater - Guest Network 5G"))
    }

    @Test
    fun `accented characters survive`() {
        assertEquals("Wi-Fi Repeater - Café", legName("Wi-Fi Repeater - Café"))
    }

    @Test
    fun `non-latin scripts survive`() {
        // A real shape on any hotel or cafe uplink abroad, which is precisely
        // where a travel router earns its name.
        assertEquals("Wi-Fi Repeater - 東京カフェ", legName("Wi-Fi Repeater - 東京カフェ"))
    }

    @Test
    fun `emoji outside the basic plane survive`() {
        // Surrogate pairs. A decoder that counts bytes rather than code points
        // truncates here, and the damage looks like a rendering bug.
        assertEquals("Wi-Fi Repeater - ☕ Coffee 🚀", legName("Wi-Fi Repeater - ☕ Coffee 🚀"))
    }

    @Test
    fun `an ssid containing a quote survives`() {
        // iwinfo does not escape a quote embedded in an SSID, which the agent's
        // parser already handles; this pins that the app does not re-break it.
        assertEquals("""Wi-Fi Repeater - Bob"s AP""", legName("""Wi-Fi Repeater - Bob"s AP"""))
    }

    @Test
    fun `an empty label falls back to the leg name rather than showing blank`() {
        // The auto label is deliberately absent for an unassociated station
        // rather than holding a stale SSID or the literal word "unknown". The
        // row must still be identifiable when that happens.
        assertEquals("hotspot", legName(""))
    }
}
