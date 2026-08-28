package app.zippie.companion

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import java.net.HttpURLConnection
import java.net.URL

/**
 * Opening a connection over THE WIFI NETWORK, whatever the default network is.
 *
 * WHY THIS EXISTS. On a cold boot the router and the phone come up together, and
 * the router has no uplink yet because the phone IS its uplink. Android probes
 * for a captive portal through the router, gets nothing, and leaves the wifi
 * network UNVALIDATED - "Connected / No internet access". Android then demotes an
 * unvalidated wifi network below cellular when it picks the DEFAULT network, which
 * is correct behaviour for ordinary apps and fatal for this one.
 *
 * `URL(x).openConnection()` uses that default. So a request to the router's own
 * LAN address left over cellular and could never arrive. Observed live
 * 2026-08-14 (#168):
 *
 *     failed to connect to /10.20.0.1 (port 8787) from /192.0.0.4 (port 51358)
 *
 * `192.0.0.4` is the RFC 7335 address Android's clatd puts on the 464XLAT
 * interface for an IPv6-only carrier. That source address IS the bug, in one
 * line: the phone dialled the router through the cellular network.
 *
 * The result was a deadlock, every step of it individually correct - the console
 * was unreachable, so the phone could not announce, so it never joined the bond,
 * so the router never got an uplink, so the wifi never validated.
 *
 * VALIDATION IS DELIBERATELY NOT REQUIRED. A wifi network with no internet is
 * precisely the case this exists to serve; asking for NET_CAPABILITY_VALIDATED
 * or NET_CAPABILITY_INTERNET here would reinstate the deadlock exactly.
 *
 * An interface, not a static call, so the decision "this request must go over
 * wifi" is testable without an Android framework on the JVM - the unit tests here
 * run on plain JUnit with no Robolectric.
 */
fun interface WifiRoute {

    /**
     * An unconnected connection that will leave over wifi.
     *
     * @return null when this phone has NO wifi network at all. Null rather than a
     *   default-network fallback on purpose: falling back would send a LAN request
     *   over cellular, which is the entire defect. The caller reports it instead,
     *   because "there is no wifi" and "the router did not answer" send an
     *   operator to completely different places.
     */
    fun open(url: String): HttpURLConnection?
}

/** The real one, backed by ConnectivityManager. */
class SystemWifiRoute(private val context: Context) : WifiRoute {

    override fun open(url: String): HttpURLConnection? {
        val cm = context.getSystemService(ConnectivityManager::class.java) ?: return null
        @Suppress("DEPRECATION") // getAllNetworks is deprecated in API 31, but it is
        // the only way to ask about a transport other than the default one without
        // holding a callback registration open for the life of the app. Same
        // trade-off, and the same suppression, as LocalAddress.wifiIPv4.
        val network = cm.allNetworks.firstOrNull { n ->
            cm.getNetworkCapabilities(n)?.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) == true
        } ?: return null
        // Network.openConnection, NOT bindProcessToNetwork: binding the process
        // would send EVERY request over wifi, including the tailnet console read
        // that is supposed to work over cellular from anywhere, and including
        // whatever the rest of the app does next.
        return network.openConnection(URL(url)) as? HttpURLConnection
    }
}
