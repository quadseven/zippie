package app.zippie.companion

/**
 * A discovered peer that has been verified through the Go core.
 */
data class VerifiedPeer(
    val clientId: Long,
    val endpoint: String
)

/**
 * Result of a peer verification attempt.
 */
sealed interface VerificationResult {
    data class Verified(val peer: VerifiedPeer) : VerificationResult
    data class Refused(val reason: String) : VerificationResult
    data class Malformed(val reason: String) : VerificationResult
}

/**
 * Interface for discovering zippie peers on the local network.
 * Implementations use NSD (Android) or Bonjour (iOS) to find _zippie._tcp services,
 * then verify them through the Go core.
 */
interface PeerDiscovery {

    /**
     * Starts discovering peers. Verified peers are delivered to [onVerified].
     * Refused peers are delivered to [onRefused] for logging/debugging.
     * Lost peers (service removed) are delivered to [onLost].
     */
    fun start(
        rosterJson: String,
        onVerified: (VerifiedPeer) -> Unit,
        onRefused: (String, String) -> Unit,  // serviceName, reason
        onLost: (String) -> Unit               // serviceName
    )

    /** Stops discovery and releases all resources. */
    fun stop()

    /** Whether discovery is currently active. */
    val isRunning: Boolean
}