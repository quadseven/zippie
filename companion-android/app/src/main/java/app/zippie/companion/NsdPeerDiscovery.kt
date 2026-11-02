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
 * App callbacks run under the discovery lock: keep them short and do not
 * block in them on another thread that may call start() or stop().
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
        val open = synchronized(lock) {
            val current = run ?: return
            run = null
            current.retire()
        }
        open.forEach(source::cancelResolve)
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
                deliver { onRefused(serviceName, REASON_MALFORMED_TXT) }
                return
            }
            // The Go call runs outside the lock; its answer is delivered under it.
            when (val result = crypto.verifyPeer(rosterJson, txtJson)) {
                is VerificationResult.Refused -> deliver { onRefused(serviceName, result.reason) }
                is VerificationResult.Verified -> deliver {
                    if (verified.putIfAbsent(serviceName, result.peer) == null) onVerified(result.peer)
                }
            }
        }

        override fun onResolveFailed(serviceName: String, errorCode: Int) {
            deliver {
                if (resolving.remove(serviceName)) onRefused(serviceName, "resolve_failed_$errorCode")
            }
        }

        override fun onServiceLost(serviceName: String) {
            val wasResolving = synchronized(lock) {
                if (!live) return
                // The app only ever saw verified peers, so only those can be lost.
                if (verified.remove(serviceName) != null) onLost(serviceName)
                resolving.remove(serviceName)
            }
            if (wasResolving) source.cancelResolve(serviceName)
        }

        override fun onDiscoveryFailed(errorCode: Int) {
            val open = synchronized(lock) {
                if (!live || run !== this) return
                run = null
                retire()
            }
            open.forEach(source::cancelResolve)
            source.stopDiscovery()
        }

        /**
         * Runs an app callback only while this run is live, holding the
         * lock so stop() cannot retire the run between the check and the
         * call. Once stop() returns, no callback from this run can start.
         * The lock is reentrant, so a callback may itself call stop().
         */
        private inline fun deliver(block: () -> Unit) = synchronized(lock) {
            if (live) block()
        }
    }

    companion object {
        /** The standard service type for zippie peer discovery. */
        const val ZIPPIE_SERVICE_TYPE = "_zippie._tcp."
        const val REASON_MALFORMED_TXT = "malformed_txt"
    }
}
