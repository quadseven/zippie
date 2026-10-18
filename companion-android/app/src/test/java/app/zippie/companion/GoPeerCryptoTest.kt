package app.zippie.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

/**
 * The bridge to the gomobile binding is REFLECTIVE until zippie.aar is a build
 * dependency, so the two things worth pinning are that its absence fails
 * closed and that, when present, the calls match gomobile's Java shape: static
 * methods, Go `int` as a primitive `long`.
 */
class GoPeerCryptoTest {

    @Test
    fun `without the binding nothing is verified`() {
        val crypto = GoPeerCrypto(bindingClass = "no.such.Binding")
        try {
            crypto.newChallenge()
            fail("a challenge was made with no binding")
        } catch (expected: IllegalStateException) {
            // Fail closed: the caller refuses the device with this reason.
        }
        val pairing = PeerPairing.parse("""{"client_id": 3, "key_hex": "${"42".repeat(32)}", "roster": []}""")!!
        val standing = PeerVerification.verify(7, pairing, crypto) { it }
        assertTrue("$standing", standing is PeerStanding.Refused)
    }

    /** Said BEFORE anything is advertised, so a build that cannot answer a
     *  challenge neither offers one nor blames every other device for failing
     *  a check it could never have passed. */
    @Test
    fun `a build without the binding says so up front`() {
        val missing = GoPeerCrypto(bindingClass = "no.such.Binding").unavailable
        assertTrue("$missing", missing?.contains("no datapath binding") == true)
        assertNull(GoPeerCrypto(bindingClass = FakeMobileBinding::class.java.name).unavailable)
    }

    @Test
    fun `calls reach the binding in gomobile's shape`() {
        val crypto = GoPeerCrypto(bindingClass = FakeMobileBinding::class.java.name)
        assertEquals("challenge", crypto.newChallenge())
        assertEquals("proof:cred:ch:10.99.0.12:51999", crypto.proof("cred", "ch", "10.99.0.12:51999"))
        assertEquals("verified:roster:7:ch:pr", crypto.verify("roster", 7, "ch", "pr"))
    }

    /** The binding's error surfaces as itself, not as a reflection wrapper. */
    @Test
    fun `a refusal from the binding keeps its own message`() {
        val crypto = GoPeerCrypto(bindingClass = FakeMobileBinding::class.java.name)
        try {
            crypto.verify("roster", 99, "ch", "pr")
            fail("the fake's refusal was lost")
        } catch (e: Exception) {
            assertEquals("unknown client", e.message)
        }
    }
}

/** Stands in for gomobile's `mobile.Mobile`: static methods, `long` for Go int. */
object FakeMobileBinding {
    @JvmStatic fun newPeerChallenge(): String = "challenge"

    @JvmStatic fun peerProof(credentialJSON: String, challengeHex: String, endpoint: String): String =
        "proof:$credentialJSON:$challengeHex:$endpoint"

    @JvmStatic fun verifyPeer(rosterJSON: String, claimedID: Long, challengeHex: String, proofHex: String): String {
        if (claimedID == 99L) throw Exception("unknown client")
        return "verified:$rosterJSON:$claimedID:$challengeHex:$proofHex"
    }
}
