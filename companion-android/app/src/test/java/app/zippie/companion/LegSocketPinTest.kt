package app.zippie.companion

import java.io.File
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The leg socket's EGRESS must be pinned to wifi, and the wifi it pins to must
 * NOT be required to have an uplink.
 *
 * THE FAILURE THIS CLOSES, measured on two Pixels 2026-08-23. Both phones held
 * an address on the router's own subnet on wlan0 and still routed to the router
 * through the modem:
 *
 *     ip route get 10.20.0.1  ->  dev v4-rmnet1  src 192.0.0.4
 *
 * While the router's uplink is down its wifi fails Android's validation, so that
 * network's routes are not installed in the default table and cellular is the
 * default network. An unpinned socket answers a datagram that arrived on wlan0
 * by replying out the modem, from a source the router has never heard of.
 *
 * Both ends were honest and disagreed. The phone's own screen read "Carrying",
 * with 284 datagrams forwarded up and 536 received back down, at the same moment
 * the router reported the leg `never_handshaked` with rx 0. The phone counted
 * what it sent; the router counted what arrived; the reply died in between.
 *
 * TWO WAYS TO BREAK THIS AGAIN, both one line each, both silent:
 *
 *  1. Drop the pin. The relay still runs, still announces, still counts traffic,
 *     and never carries.
 *  2. Add NET_CAPABILITY_VALIDATED or NET_CAPABILITY_INTERNET to the wifi
 *     request. It would then match only a wifi that ALREADY has an uplink -
 *     which is the uplink this relay exists to create. That is the deadlock
 *     WifiRoute's header describes, reinstated from the other side.
 *
 * Source-text assertions because the app target has no test target (#48), the
 * same trade-off as ConfigSourceWiringTest.
 */
class LegSocketPinTest {

    private val relativePath = "app/src/main/java/app/zippie/companion/RelayService.kt"

    private fun source(): String {
        var dir: File? = File("").absoluteFile
        while (dir != null) {
            val f = File(dir, relativePath)
            if (f.isFile) return f.readText()
            dir = dir.parentFile
        }
        throw AssertionError("cannot find $relativePath - if it moved, move this check with it")
    }

    /**
     * Comments stripped before any assertion about what the code USES.
     *
     * `watchWifiForLegPin` explains, at length, the two constants it must never
     * add - so a naive substring check fires on the warning against the mistake
     * rather than the mistake. CI caught exactly that on this test's first run.
     * An assertion that cannot tell a caution from a call is worse than none.
     */
    private fun codeOnly(text: String): String =
        text.replace(Regex("""/\*.*?\*/""", RegexOption.DOT_MATCHES_ALL), "")
            .lineSequence()
            .joinToString("\n") { line -> line.substringBefore("//") }

    /** The text of one private method, so an assertion about the WIFI request
     *  cannot be satisfied - or broken - by the CELLULAR one in the same file.
     *  Cellular legitimately requires NET_CAPABILITY_INTERNET; wifi must not. */
    private fun body(source: String, signature: String): String {
        val start = source.indexOf(signature)
        assertTrue(
            "$signature is gone from RelayService - the leg socket pin has been " +
                "removed or renamed, and a renamed pin is an unwatched pin",
            start >= 0,
        )
        val rest = source.substring(start + signature.length)
        val end = rest.indexOf("\n    private fun ")
        return if (end < 0) rest else rest.substring(0, end)
    }

    @Test
    fun `the leg socket is pinned to a wifi network`() {
        val pin = codeOnly(body(source(), "private fun pinLegSocket("))
        assertTrue(
            "pinLegSocket must call Network.bindSocket - without it the reply " +
                "follows the default route out the modem and the router sees silence",
            pin.contains("bindSocket("),
        )
    }

    @Test
    fun `the wifi the leg pins to is not required to have an uplink`() {
        val watch = codeOnly(body(source(), "private fun watchWifiForLegPin("))

        assertTrue(
            "the leg pin must watch for a WIFI transport",
            watch.contains("TRANSPORT_WIFI"),
        )
        assertFalse(
            "NET_CAPABILITY_VALIDATED in the leg's wifi request matches only a " +
                "wifi that already has an uplink - the exact deadlock this relay " +
                "exists to break. See WifiRoute's header.",
            watch.contains("NET_CAPABILITY_VALIDATED"),
        )
        assertFalse(
            "NET_CAPABILITY_INTERNET in the leg's wifi request has the same " +
                "effect as VALIDATED here: the router's wifi has no internet " +
                "until this relay gives it one.",
            watch.contains("NET_CAPABILITY_INTERNET"),
        )
    }

    @Test
    fun `observing wifi never asks the system to bring a radio up`() {
        val watch = codeOnly(body(source(), "private fun watchWifiForLegPin("))
        assertTrue(
            "must use registerNetworkCallback: the phone has already joined this " +
                "wifi and observation needs no CHANGE_NETWORK_STATE",
            watch.contains("registerNetworkCallback("),
        )
        assertFalse(
            "requestNetwork would ask the system to bring wifi UP. A relay must " +
                "never be the reason a phone turns a radio on.",
            watch.contains("requestNetwork("),
        )
    }

    @Test
    fun `a replaced wifi network is re-pinned rather than assumed`() {
        // codeOnly here too: a comment that MENTIONS onCapabilitiesChanged must
        // not satisfy a check that the override exists.
        val watch = codeOnly(body(source(), "private fun watchWifiForLegPin("))
        assertTrue(
            "a roam or reconnect hands out a NEW Network object; a pin to the old " +
                "one fails as silently as no pin at all, so capability changes " +
                "must re-drive the pin",
            watch.contains("onCapabilitiesChanged"),
        )
    }
}
