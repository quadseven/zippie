package app.zippie.companion

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import java.net.Inet4Address

/**
 * This phone's own address on the router's wifi.
 *
 * Only used to answer "which leg on the router is me" (see LegIdentity), and it
 * has to be the WIFI address specifically. Enumerating NetworkInterface would
 * be shorter and would happily return the cellular interface's address, which
 * the router never dials - the match would then fail silently and no row would
 * ever be marked as this phone.
 *
 * Null when there is no wifi network or it has no IPv4 address. Null is the
 * honest answer: it makes LegIdentity refuse to claim any leg, which is what
 * should happen when the phone cannot prove which one it is.
 */
object LocalAddress {

    fun wifiIPv4(context: Context): String? {
        val cm = context.getSystemService(ConnectivityManager::class.java) ?: return null
        @Suppress("DEPRECATION") // getAllNetworks is deprecated in API 31 but is the
        // only way to ask about a transport other than the default one without
        // holding a callback registration open for the life of the app.
        val networks = cm.allNetworks
        for (network in networks) {
            val caps = cm.getNetworkCapabilities(network) ?: continue
            if (!caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) continue
            val props = cm.getLinkProperties(network) ?: continue
            for (address in props.linkAddresses) {
                val a = address.address
                if (a is Inet4Address && !a.isLoopbackAddress) return a.hostAddress
            }
        }
        return null
    }
}
