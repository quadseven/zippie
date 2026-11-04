package app.zippie.companion

import org.json.JSONObject

/**
 * The Go core's peer functions, reached through the gomobile binding:
 *   - PeerTXT(credentialJSON, endpoint) -> (txtJSON, error)
 *   - VerifyPeer(rosterJSON, txtJSON) -> (verifiedPeerJSON, error)
 * where verifiedPeerJSON is {"client_id": N, "endpoint": "host:port"}.
 */
interface GoPeerCrypto {

    /**
     * The TXT record this device should advertise, as JSON with "id" and
     * "proof", or null when the core cannot produce one.
     */
    fun peerTxt(credentialJson: String, endpoint: String): String?

    /**
     * Verifies a discovered peer's TXT record (JSON with "id" and "proof")
     * against the roster of discovery keys.
     */
    fun verifyPeer(rosterJson: String, txtJson: String): VerificationResult
}

/**
 * Production implementation. The gomobile AAR is not on the classpath yet
 * (see the commented `libs/zippie.aar` line in app/build.gradle.kts), so
 * every peer is refused with a reason that says why, rather than verified
 * by a stand-in. Wiring the binding replaces the two bodies below.
 */
class GoPeerCryptoImpl : GoPeerCrypto {

    override fun peerTxt(credentialJson: String, endpoint: String): String? = null

    override fun verifyPeer(rosterJson: String, txtJson: String): VerificationResult =
        VerificationResult.Refused(REASON_CORE_UNAVAILABLE)

    companion object {
        const val REASON_CORE_UNAVAILABLE = "go_core_unavailable"
    }
}

/**
 * Turns a resolved service's TXT attributes into the JSON VerifyPeer reads.
 * Returns null when "id" or "proof" is missing or empty, so a record that
 * cannot be a zippie peer is refused before the Go core is called.
 */
fun txtRecordJson(txt: Map<String, ByteArray?>): String? {
    val id = txt["id"]?.toString(Charsets.UTF_8)
    val proof = txt["proof"]?.toString(Charsets.UTF_8)
    if (id.isNullOrEmpty() || proof.isNullOrEmpty()) return null
    return JSONObject().put("id", id).put("proof", proof).toString()
}
