package app.zippie.companion

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.util.Log
import java.util.concurrent.ConcurrentHashMap

/**
 * NSD-based implementation of PeerDiscovery for Android.
 * Discovers _zippie._tcp services, resolves their TXT records,
 * and verifies them through the Go core.
 */
class NsdPeerDiscovery(
    private val context: Context,
    private val crypto: GoPeerCrypto,
    private val serviceType: String = "_zippie._tcp.",
    private val tag: String = "NsdPeerDiscovery"
) : PeerDiscovery {

    private val nsdManager = context.getSystemService(Context.NSD_SERVICE) as NsdManager
    private val discoveryListener = DiscoveryListener()
    private val resolveListeners = ConcurrentHashMap<String, ResolveListener>()
    private val verifiedServices = ConcurrentHashMap<String, VerifiedPeer>()

    @Volatile
    private var running = false

    override val isRunning: Boolean
        get() = running

    override fun start(
        rosterJson: String,
        onVerified: (VerifiedPeer) -> Unit,
        onRefused: (String, String) -> Unit,
        onLost: (String) -> Unit
    ) {
        if (running) return
        running = true

        discoveryListener.callbacks = DiscoveryCallbacks(
            rosterJson = rosterJson,
            onVerified = onVerified,
            onRefused = onRefused,
            onLost = onLost
        )

        nsdManager.discoverServices(
            serviceType,
            NsdManager.PROTOCOL_DNS_SD,
            discoveryListener
        )
        Log.i(tag, "Started discovering $serviceType")
    }

    override fun stop() {
        if (!running) return
        running = false

        nsdManager.stopServiceDiscovery(discoveryListener)

        // Stop all pending resolves
        resolveListeners.values.forEach { listener ->
            nsdManager.stopResolveService(listener.resolveListener)
        }
        resolveListeners.clear()
        verifiedServices.clear()

        Log.i(tag, "Stopped discovering $serviceType")
    }

    private inner class DiscoveryListener : NsdManager.DiscoveryListener {

        @Volatile
        var callbacks: DiscoveryCallbacks? = null

        override fun onDiscoveryStarted(serviceType: String) {
            Log.d(tag, "Discovery started for $serviceType")
        }

        override fun onServiceFound(serviceInfo: NsdServiceInfo) {
            val name = serviceInfo.serviceName
            Log.d(tag, "Found service: $name")
            callbacks?.let { cb ->
                // Resolve the service to get its TXT record
                val resolveListener = ResolveListener(name, cb)
                resolveListeners[name] = resolveListener
                nsdManager.resolveService(serviceInfo, resolveListener.resolveListener)
            }
        }

        override fun onServiceLost(serviceName: String) {
            Log.d(tag, "Lost service: $serviceName")
            callbacks?.onLost(serviceName)
            verifiedServices.remove(serviceName)
            resolveListeners.remove(serviceName)
        }

        override fun onDiscoveryStopped(serviceType: String) {
            Log.d(tag, "Discovery stopped for $serviceType")
        }

        override fun onStartDiscoveryFailed(serviceType: String, errorCode: Int) {
            Log.e(tag, "Discovery start failed for $serviceType: $errorCode")
            running = false
        }

        override fun onStopDiscoveryFailed(serviceType: String, errorCode: Int) {
            Log.e(tag, "Discovery stop failed for $serviceType: $errorCode")
        }
    }

    private inner class ResolveListener(
        private val serviceName: String,
        private val callbacks: DiscoveryCallbacks
    ) {
        val resolveListener = object : NsdManager.ResolveListener {
            override fun onResolveFailed(serviceInfo: NsdServiceInfo, errorCode: Int) {
                Log.w(tag, "Resolve failed for $serviceName: $errorCode")
                callbacks.onRefused(serviceName, "resolve_failed_$errorCode")
                resolveListeners.remove(serviceName)
            }

            override fun onServiceResolved(serviceInfo: NsdServiceInfo) {
                Log.d(tag, "Resolved service: $serviceName at ${serviceInfo.host}:${serviceInfo.port}")
                resolveListeners.remove(serviceName)

                // Get TXT record and verify
                val txtRecord = serviceInfo.attributes
                val txtJson = crypto.parseTxtRecord(txtRecord)
                    ?: run {
                        callbacks.onRefused(serviceName, "malformed_txt")
                        return@onServiceResolved
                    }

                try {
                    val peer = crypto.verifyPeer(callbacks.rosterJson, txtJson)
                    if (peer != null) {
                        // Only deliver if not already verified (avoid duplicates on re-resolve)
                        if (verifiedServices.putIfAbsent(serviceName, peer) == null) {
                            callbacks.onVerified(peer)
                        }
                    } else {
                        callbacks.onRefused(serviceName, "verification_failed")
                    }
                } catch (e: FakeGoPeerCrypto.VerificationRefused) {
                    callbacks.onRefused(serviceName, e.reason)
                } catch (e: Exception) {
                    Log.e(tag, "Verification error for $serviceName", e)
                    callbacks.onRefused(serviceName, "verification_error")
                }
            }
        }
    }

    private data class DiscoveryCallbacks(
        val rosterJson: String,
        val onVerified: (VerifiedPeer) -> Unit,
        val onRefused: (String, String) -> Unit,
        val onLost: (String) -> Unit
    )

    companion object {
        /** The standard service type for zippie peer discovery. */
        const val ZIPPIE_SERVICE_TYPE = "_zippie._tcp."
    }
}