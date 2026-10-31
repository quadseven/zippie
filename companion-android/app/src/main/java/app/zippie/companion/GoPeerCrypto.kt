package app.zippie.companion

import org.json.JSONObject

/**
 * Interface to the Go core's peer verification functions via gomobile.
 * The gomobile binding exposes:
 *   - PeerTXT(credentialJSON, endpoint) -> (txtJSON, error)
 *   - VerifyPeer(rosterJSON, txtJSON) -> (verifiedPeerJSON, error)
 * where verifiedPeerJSON is {"client_id": N, "endpoint": "host:port"}
 */
interface GoPeerCrypto {

    /**
     * Generates the TXT record this device should advertise.
     * @param credentialJson JSON with client_id and key_hex (pairing key)
     * @param endpoint The "host:port" a peer should dial to reach this device
     * @return JSON object string with "id" and "proof" fields, or null on error
     */
    fun peerTxt(credentialJson: String, endpoint: String): String?

    /**
     * Verifies a discovered peer's TXT record against the roster.
     * @param rosterJson JSON array of {client_id, key_hex} where key_hex is the DISCOVERY key
     * @param txtJson JSON object with "id" and "proof" fields from the discovered service
     * @return VerifiedPeer if verification succeeds, null if it fails (with reason logged)
     */
    fun verifyPeer(rosterJson: String, txtJson: String): VerifiedPeer?

    /**
     * Parses the TXT record from an NSD service into the JSON format expected by verifyPeer.
     * NSD delivers TXT records as a map of byte arrays; we convert to string map then JSON.
     */
    fun parseTxtRecord(txtRecord: Map<String, ByteArray>): String?
}

/**
 * Production implementation that calls the gomobile binding.
 * The gomobile-generated class is zippie.datapath.ZippieDatapath.
 */
class GoPeerCryptoImpl : GoPeerCrypto {

    override fun peerTxt(credentialJson: String, endpoint: String): String? {
        // TODO: Call gomobile binding when available
        // return ZippieDatapath.PeerTXT(credentialJson, endpoint)
        return null
    }

    override fun verifyPeer(rosterJson: String, txtJson: String): VerifiedPeer? {
        // TODO: Call gomobile binding when available
        // val result = ZippieDatapath.VerifyPeer(rosterJson, txtJson)
        // return parseVerifiedPeer(result)
        return null
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

    private fun parseVerifiedPeer(json: String): VerifiedPeer? {
        return try {
            val obj = JSONObject(json)
            VerifiedPeer(
                clientId = obj.getLong("client_id"),
                endpoint = obj.getString("endpoint")
            )
        } catch (e: Exception) {
            null
        }
    }
}