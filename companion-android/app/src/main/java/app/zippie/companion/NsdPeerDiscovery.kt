package app.zippie.companion

/**
 * Finds _zippie._tcp services through an [NsdSource], resolves each one,
 * and hands its TXT record to the Go core. Only a peer the core verifies
 * reaches onVerified.
 *
 * Every start creates a fresh listener tied to that run. stop() cancels the
 * resolves still in flight and retires the listener, so an event the
 * platform delivers late (Android cannot cancel a resolve before API 34)
 * finds a retired listener and is dropped instead of reaching the app.
 */
class NsdPeerDiscovery(
    private val source: NsdSource,
    private val crypto: GoPeerCrypto,
    private val serviceType: String = ZIPPIE_SERVICE_TYPE,
) : PeerDiscovery {

    private val lock = Any()
    private var run: Run? = null

    override val isRunning: Boolean
        get() = synchronized(lock) { run != null }

    override fun start(
        rosterJson: String,
        onVerified: (VerifiedPeer) -> Unit,
        onRefused: (String, String) -> Unit,
        onLost: (String) -> Unit,
    ) {
        val fresh = synchronized(lock) {
            if (run != null) return
            Run(rosterJson, onVerified, onRefused, onLost).also { run = it }
        }
        source.discover(serviceType, fresh)
    }

    override fun stop() {
        val ending = synchronized(lock) {
            val current = run ?: return
            run = null
            current
        }
        ending.retire().forEach(source::cancelResolve)
        source.stopDiscovery()
    }

    /** One start-to-stop run; its state dies with it. */
    private inner class Run(
        val rosterJson: String,
        val onVerified: (VerifiedPeer) -> Unit,
        val onRefused: (String, String) -> Unit,
        val onLost: (String) -> Unit,
    ) : NsdSource.Listener {

        private var live = true
        private val resolving = mutableSetOf<String>()
        private val verified = mutableMapOf<String, VerifiedPeer>()

        /** Marks the run dead and returns the resolves it still had open. */
        fun retire(): Set<String> = synchronized(lock) {
            live = false
            verified.clear()
            resolving.toSet().also { resolving.clear() }
        }

        override fun onServiceFound(serviceName: String) {
            synchronized(lock) {
                if (!live || !resolving.add(serviceName)) return
            }
            source.resolve(serviceName)
        }

        override fun onServiceResolved(serviceName: String, txt: Map<String, ByteArray>) {
            synchronized(lock) {
                if (!live || !resolving.remove(serviceName)) return
            }
            val txtJson = txtRecordJson(txt)
            if (txtJson == null) {
                onRefused(serviceName, REASON_MALFORMED_TXT)
                return
            }
            when (val result = crypto.verifyPeer(rosterJson, txtJson)) {
                is VerificationResult.Refused -> if (isLive()) onRefused(serviceName, result.reason)
                is VerificationResult.Verified -> {
                    val first = synchronized(lock) {
                        live && verified.putIfAbsent(serviceName, result.peer) == null
                    }
                    if (first) onVerified(result.peer)
                }
            }
        }

        override fun onResolveFailed(serviceName: String, errorCode: Int) {
            synchronized(lock) {
                if (!live || !resolving.remove(serviceName)) return
            }
            onRefused(serviceName, "resolve_failed_$errorCode")
        }

        override fun onServiceLost(serviceName: String) {
            val (wasResolving, wasVerified) = synchronized(lock) {
                if (!live) return
                resolving.remove(serviceName) to (verified.remove(serviceName) != null)
            }
            if (wasResolving) source.cancelResolve(serviceName)
            // The app only ever saw verified peers, so only those can be lost.
            if (wasVerified) onLost(serviceName)
        }

        override fun onDiscoveryFailed(errorCode: Int) {
            val ending = synchronized(lock) {
                if (!live || run !== this) return
                run = null
                this
            }
            ending.retire().forEach(source::cancelResolve)
            source.stopDiscovery()
        }

        private fun isLive() = synchronized(lock) { live }
    }

    companion object {
        /** The standard service type for zippie peer discovery. */
        const val ZIPPIE_SERVICE_TYPE = "_zippie._tcp."
        const val REASON_MALFORMED_TXT = "malformed_txt"
    }
}
