package app.zippie.companion

import org.json.JSONObject

/**
 * Fake implementation for testing that simulates the Go core's verification logic.
 * Uses pre-configured responses to test the discovery flow without the Go binding.
 */
class FakeGoPeerCrypto(
    private val verifiedPeers: Map<String, VerifiedPeer> = emptyMap(),  // txtJson -> VerifiedPeer
    private val refusedReasons: Map<String, String> = emptyMap()        // txtJson -> reason
) : GoPeerCrypto {

    override fun peerTxt(credentialJson: String, endpoint: String): String? {
        // Not used in discovery tests
        return "{\"id\":\"1\",\"proof\":\"fake\"}"
    }

    override fun verifyPeer(rosterJson: String, txtJson: String): VerifiedPeer? {
        return verifiedPeers[txtJson] ?: run {
            refusedReasons[txtJson]?.let { reason ->
                throw VerificationRefused(reason)
            }
            null
        }
    }

    override fun parseTxtRecord(txtRecord: Map<String, ByteArray>): String? {
        val map = mutableMapOf<String, String>()
        txtRecord.forEach { (key, value) ->
            map[key] = String(value)
        }
        return try {
            JSONObject(map).toString()
        } catch (e: Exception) {
            null
        }
    }

    class VerificationRefused(val reason: String) : RuntimeException(reason)
}