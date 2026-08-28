package app.zippie.companion

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.net.wifi.WifiManager
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.IOException
import java.net.HttpURLConnection
import java.net.SocketTimeoutException
import java.net.URL
import java.net.UnknownHostException
import javax.net.ssl.SSLException

/**
 * Takes the measurements [DiagnosticsScreen] reports.
 *
 * MEASURES, THEN PUBLISHES ONCE. Every probe writes into a local value and the
 * whole [Diagnostics] is returned at the end, so the screen never shows a
 * half-measured mixture where the MDM row is from this attempt and the tailnet
 * row is from the last one.
 *
 * Android can answer more than iOS here, and the shared type is built to let it.
 * LinkProperties exposes the resolver DHCP actually supplied, which is the fault
 * that took the house wifi down on 2026-08-11 and which an iPhone cannot see at
 * all.
 */
class DiagnosticsMeasurer(
    private val context: Context,
    /**
     * The MDM this fleet enrols against, for the "can this phone still be
     * managed" row.
     *
     * NO DEFAULT. It named one specific deployment's MDM on every install, and a
     * blank value must SKIP the probe rather than build "https:///" - an empty
     * host produces a MalformedURLException that renders as a network fault,
     * which is the failure shape this whole screen exists to stop.
     */
    private val mdmHost: String = "",
    private val routerHost: String? = null,
    /** Injected so the probes can be exercised without an Android framework. */
    private val wifi: WifiRoute = SystemWifiRoute(context),
) {
    suspend fun measure(): Diagnostics = withContext(Dispatchers.IO) {
        val cm = context.getSystemService(ConnectivityManager::class.java)
        // THE WIFI NETWORK, NOT activeNetwork. This screen reports on the wifi -
        // it labels every row with that SSID - and activeNetwork is CELLULAR
        // exactly when the wifi is unvalidated, which is the failure the screen
        // exists to explain (#168). Reading facts off cellular and printing them
        // under a wifi heading is how this screen would say the network is fine
        // while the operator stares at "No internet access".
        @Suppress("DEPRECATION") // see SystemWifiRoute for why getAllNetworks
        val network = cm?.allNetworks?.firstOrNull {
            cm.getNetworkCapabilities(it)?.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) == true
        } ?: cm?.activeNetwork
        val caps = network?.let { cm?.getNetworkCapabilities(it) }
        val link = network?.let { cm?.getLinkProperties(it) }

        val resolver = when {
            link == null -> ResolverFact.Unknown
            link.dnsServers.isEmpty() -> ResolverFact.None
            else -> ResolverFact.Address(link.dnsServers.first().hostAddress ?: "unknown")
        }

        val ssid = if (caps?.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) == true) {
            ssidOrNull()
        } else {
            null
        }

        // ORDER MATTERS, and it is the order of causation. A network with no
        // resolver cannot reach anything by name, so probing the MDM first would
        // report a DNS fault as an MDM fault - which is precisely how a DHCP
        // misconfiguration read as a wifi problem for hours.
        if (resolver is ResolverFact.None) {
            return@withContext Diagnostics(
                ssid = ssid,
                dhcpResolver = resolver,
                captive = DiagnosticState.Failed(DiagnosticFailure.NoResolverOffered),
                mdm = DiagnosticState.Failed(DiagnosticFailure.NoResolverOffered),
                tailnet = TailnetPath.Unreachable(DiagnosticFailure.NoResolverOffered),
                measuredAtEpochMs = System.currentTimeMillis(),
            )
        }

        val captive = probe("http://captive.apple.com/hotspot-detect.html", expecting = "Success")
        val mdm = if (mdmHost.isBlank()) {
            DiagnosticState.NotChecked
        } else {
            probe("https://$mdmHost/", expecting = null)
        }

        // Direct vs via-router is decided by whether THIS phone holds a tailnet
        // address, not by whether the tailnet answered. Both states answer; only
        // one survives changing network.
        val tailnetAddr = tailnetAddress()
        val tailnet = when {
            tailnetAddr != null && mdm.isOk -> TailnetPath.Direct(tailnetAddr)
            tailnetAddr != null -> TailnetPath.Unreachable(DiagnosticFailure.NoRoute)
            mdm.isOk -> TailnetPath.ViaRouter(routerHost ?: "this network's router")
            else -> TailnetPath.Unreachable(DiagnosticFailure.NoRoute)
        }

        Diagnostics(
            ssid = ssid,
            dhcpResolver = resolver,
            captive = captive,
            mdm = mdm,
            tailnet = tailnet,
            measuredAtEpochMs = System.currentTimeMillis(),
        )
    }

    /**
     * This device's tailnet address, if it has one.
     *
     * Detected by ADDRESS rather than by asking, because there is no API to ask
     * whether Tailscale is running and a URL probe cannot separate "this phone
     * is on the tailnet" from "this network forwards to it" - both answer.
     *
     * The CGNAT range check is [TailnetAddress]-equivalent logic and is kept in
     * [isTailnetV4] alongside its unit tests, not inlined here, because it is
     * the part that can be wrong.
     */
    private fun tailnetAddress(): String? =
        try {
            java.net.NetworkInterface.getNetworkInterfaces().toList()
                .asSequence()
                .filter { it.isUp && !it.isLoopback }
                .flatMap { it.inetAddresses.toList().asSequence() }
                .mapNotNull { it.hostAddress }
                .firstOrNull { isTailnetV4(it) }
        } catch (_: Exception) {
            null
        }

    private fun ssidOrNull(): String? = try {
        @Suppress("DEPRECATION")
        val wifi = context.applicationContext
            .getSystemService(Context.WIFI_SERVICE) as? WifiManager
        @Suppress("DEPRECATION")
        wifi?.connectionInfo?.ssid?.trim('"')?.takeIf { it.isNotEmpty() && it != "<unknown ssid>" }
    } catch (_: SecurityException) {
        // Reading the SSID needs location permission. Not having it is not a
        // network fault, and reporting it as one would be the same lie the
        // resolver's three-state type exists to prevent.
        null
    }

    /**
     * Classifies rather than reports a boolean. "failed" sends nobody anywhere;
     * "HTTP 401" and "timed out after 12s" send them to different places.
     */
    private fun probe(url: String, expecting: String?): DiagnosticState {
        var conn: HttpURLConnection? = null
        return try {
            // OVER WIFI, for the same reason the facts above come from wifi: a
            // captive-portal probe answered by CELLULAR says nothing about the
            // network this screen is describing, and would report a dead wifi as
            // healthy (#168).
            val opened = wifi.open(url)
                ?: return DiagnosticState.Failed(DiagnosticFailure.NoRoute)
            conn = opened.apply {
                connectTimeout = TIMEOUT_MS
                readTimeout = TIMEOUT_MS
                useCaches = false
                instanceFollowRedirects = true
            }
            val code = conn.responseCode
            if (code !in 200..299) {
                DiagnosticState.Failed(DiagnosticFailure.Http(code))
            } else if (expecting != null &&
                !conn.inputStream.bufferedReader().use { it.readText() }.contains(expecting)
            ) {
                // A body that is not what was asked for is a captive portal or
                // an interceptor, not an outage - and the OS treats it as "no
                // internet" either way.
                DiagnosticState.Failed(DiagnosticFailure.Http(200))
            } else {
                DiagnosticState.Ok()
            }
        } catch (e: SocketTimeoutException) {
            DiagnosticState.Failed(DiagnosticFailure.TimedOut(TIMEOUT_MS / 1000))
        } catch (e: UnknownHostException) {
            DiagnosticState.Failed(DiagnosticFailure.NameNotResolved(hostOf(url)))
        } catch (e: SSLException) {
            DiagnosticState.Failed(DiagnosticFailure.Tls(e.message ?: "handshake failed"))
        } catch (e: IOException) {
            DiagnosticState.Failed(DiagnosticFailure.NoRoute)
        } finally {
            conn?.disconnect()
        }
    }

    private fun hostOf(url: String): String = try {
        URL(url).host ?: url
    } catch (_: Exception) {
        url
    }

    companion object {
        const val TIMEOUT_MS = 12_000

        /**
         * The CGNAT range Tailscale allocates from (RFC 6598), 100.64.0.0/10.
         *
         * Mirrors `TailnetAddress.isTailnetV4` in the iOS Kit deliberately. An
         * off-by-one octet would classify a hotel's 100.130.4.5 as tailnet and
         * the screen would confidently report direct access on a phone with
         * none, so both bounds are unit-tested on both platforms.
         */
        fun isTailnetV4(ip: String): Boolean {
            val parts = ip.split(".").mapNotNull { it.toIntOrNull() }
            if (parts.size != 4 || parts.any { it !in 0..255 }) return false
            return parts[0] == 100 && parts[1] in 64..127
        }
    }
}
