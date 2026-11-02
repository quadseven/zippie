package app.zippie.companion

/**
 * A discovered peer that has been verified through the Go core.
 */
data class VerifiedPeer(
    val clientId: Long,
    val endpoint: String,
)

/**
 * The Go core's answer for one discovered TXT record.
 */
sealed interface VerificationResult {
    data class Verified(val peer: VerifiedPeer) : VerificationResult
    data class Refused(val reason: String) : VerificationResult
}

/**
 * Discovers zippie peers on the local network. Implementations find
 * _zippie._tcp services, then verify each one through the Go core; only a
 * verified peer reaches [start]'s onVerified.
 */
interface PeerDiscovery {

    /**
     * Starts discovering peers. A call while already running is ignored.
     *
     * @param onVerified a peer the Go core verified, once per appearance.
     * @param onRefused service name and reason, for logs; never shown as a peer.
     * @param onLost service name of a previously verified peer that went away.
     */
    fun start(
        rosterJson: String,
        onVerified: (VerifiedPeer) -> Unit,
        onRefused: (String, String) -> Unit,
        onLost: (String) -> Unit,
    )

    /** Stops discovery and releases every listener handed to the platform. */
    fun stop()

    /** Whether discovery is currently active. */
    val isRunning: Boolean
}

/**
 * The slice of the platform's DNS-SD API that discovery needs. On Android
 * this is [AndroidNsdSource] over NsdManager; tests use an in-memory fake.
 * Kept free of android.* types so the discovery logic runs as a plain JVM
 * unit test.
 */
interface NsdSource {

    /** Begins discovery of [serviceType]; events go to [listener] until [stopDiscovery]. */
    fun discover(serviceType: String, listener: Listener)

    /** Ends discovery and unregisters the listener given to [discover]. */
    fun stopDiscovery()

    /** Resolves a found service; the answer arrives as onServiceResolved or onResolveFailed. */
    fun resolve(serviceName: String)

    /** Abandons a resolve. Its result, if the platform still sends one, must not be delivered. */
    fun cancelResolve(serviceName: String)

    interface Listener {
        fun onServiceFound(serviceName: String)
        fun onServiceLost(serviceName: String)
        fun onServiceResolved(serviceName: String, txt: Map<String, ByteArray>)
        fun onResolveFailed(serviceName: String, errorCode: Int)
        fun onDiscoveryFailed(errorCode: Int)
    }
}
