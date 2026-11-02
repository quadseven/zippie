package app.zippie.companion

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.os.Build
import android.util.Log
import java.util.concurrent.ConcurrentHashMap

/**
 * [NsdSource] over Android's NsdManager. It keeps the NsdServiceInfo each
 * found service arrived with, because resolveService needs it, and one
 * ResolveListener per resolve so cancelResolve can reach the right one.
 *
 * Below API 34 NsdManager cannot cancel a resolve. cancelResolve then only
 * forgets the listener, and its late answer is dropped here because the
 * listener is no longer the one on record for that name.
 */
class AndroidNsdSource(
    context: Context,
    private val tag: String = "NsdPeerDiscovery",
) : NsdSource {

    private val nsd = context.getSystemService(Context.NSD_SERVICE) as NsdManager

    @Volatile
    private var active: Active? = null

    override fun discover(serviceType: String, listener: NsdSource.Listener) {
        val run = Active(listener)
        active = run
        nsd.discoverServices(serviceType, NsdManager.PROTOCOL_DNS_SD, run.discovery)
        Log.i(tag, "discovering $serviceType")
    }

    override fun stopDiscovery() {
        val run = active ?: return
        active = null
        run.resolves.keys.toList().forEach { run.cancel(it) }
        run.found.clear()
        try {
            nsd.stopServiceDiscovery(run.discovery)
        } catch (e: IllegalArgumentException) {
            // The listener was never registered: discovery failed to start.
            Log.w(tag, "stopServiceDiscovery on an unregistered listener", e)
        }
    }

    override fun resolve(serviceName: String) {
        val run = active ?: return
        val info = run.found[serviceName] ?: return
        val listener = run.resolveListener(serviceName)
        run.resolves[serviceName] = listener
        @Suppress("DEPRECATION") // registerServiceInfoCallback needs API 34; minSdk is 29.
        nsd.resolveService(info, listener)
    }

    override fun cancelResolve(serviceName: String) {
        active?.cancel(serviceName)
    }

    private inner class Active(val listener: NsdSource.Listener) {
        val found = ConcurrentHashMap<String, NsdServiceInfo>()
        val resolves = ConcurrentHashMap<String, NsdManager.ResolveListener>()

        fun cancel(serviceName: String) {
            val pending = resolves.remove(serviceName) ?: return
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
                try {
                    nsd.stopServiceResolution(pending)
                } catch (e: IllegalArgumentException) {
                    // Already answered; nothing left to stop.
                }
            }
        }

        fun resolveListener(serviceName: String): NsdManager.ResolveListener =
            object : NsdManager.ResolveListener {
                override fun onServiceResolved(serviceInfo: NsdServiceInfo) {
                    if (!resolves.remove(serviceName, this)) return
                    listener.onServiceResolved(serviceName, serviceInfo.attributes)
                }

                override fun onResolveFailed(serviceInfo: NsdServiceInfo, errorCode: Int) {
                    if (!resolves.remove(serviceName, this)) return
                    listener.onResolveFailed(serviceName, errorCode)
                }
            }

        val discovery = object : NsdManager.DiscoveryListener {
            override fun onDiscoveryStarted(serviceType: String) = Unit

            override fun onServiceFound(serviceInfo: NsdServiceInfo) {
                if (active !== this@Active) return
                found[serviceInfo.serviceName] = serviceInfo
                listener.onServiceFound(serviceInfo.serviceName)
            }

            override fun onServiceLost(serviceInfo: NsdServiceInfo) {
                if (active !== this@Active) return
                found.remove(serviceInfo.serviceName)
                listener.onServiceLost(serviceInfo.serviceName)
            }

            override fun onDiscoveryStopped(serviceType: String) = Unit

            override fun onStartDiscoveryFailed(serviceType: String, errorCode: Int) {
                Log.e(tag, "discovery of $serviceType failed to start: $errorCode")
                if (active === this@Active) listener.onDiscoveryFailed(errorCode)
            }

            override fun onStopDiscoveryFailed(serviceType: String, errorCode: Int) {
                Log.e(tag, "discovery of $serviceType failed to stop: $errorCode")
            }
        }
    }
}
