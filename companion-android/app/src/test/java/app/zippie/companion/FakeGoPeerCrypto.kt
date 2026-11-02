package app.zippie.companion

import org.json.JSONObject

/**
 * Stands in for the Go core's VerifyPeer. A TXT record verifies when its
 * "id" and "proof" match an entry in [accepted]; an id in [refused] is
 * refused with that reason; anything else is refused as unknown.
 */
class FakeGoPeerCrypto : GoPeerCrypto {

    /** "id:proof" -> peer the Go core would return. */
    val accepted = mutableMapOf<String, VerifiedPeer>()

    /** id -> refusal reason the Go core would return. */
    val refused = mutableMapOf<String, String>()

    val verifyCalls = mutableListOf<String>()

    override fun peerTxt(credentialJson: String, endpoint: String): String? = null

    override fun verifyPeer(rosterJson: String, txtJson: String): VerificationResult {
        verifyCalls.add(txtJson)
        val obj = JSONObject(txtJson)
        val id = obj.optString("id")
        val proof = obj.optString("proof")
        accepted["$id:$proof"]?.let { return VerificationResult.Verified(it) }
        return VerificationResult.Refused(refused[id] ?: "unknown_client")
    }
}
