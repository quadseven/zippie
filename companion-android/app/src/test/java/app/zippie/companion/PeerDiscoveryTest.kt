package app.zippie.companion

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

/**
 * Same-LAN discovery (#270), the Kotlin port of PeerDiscoveryTests.swift. An
 * NSD advertisement is a claim from anything on the wifi, so every test here
 * is about the claim NOT turning into a leg until the pairing credential
 * behind it has answered.
 */
class PeerDiscoveryTest {

    private val key = "42".repeat(32)
    private val bundle = """
        {"client_id": 3, "key_hex": "$key",
         "roster": [{"id": 7, "name": "spare-android", "key_hex": "$key"}]}
    """.trimIndent()

    private fun pairing(): PeerPairing = PeerPairing.parse(bundle) ?: error("fixture bundle refused")

    /** What the screen would offer from this roster, through the same
     *  [PeerSnapshot.offers] the Status screen calls, as client 3. */
    private fun offers(r: PeerRoster, legNames: Set<String>, legEndpoints: Set<String>) =
        PeerSnapshot(ownClientId = 3, verified = r.verifiedPeers).offers(legNames, legEndpoints)

    private fun peer(id: Long = 7, name: String = "spare-android", host: String = "10.99.0.57") =
        VerifiedPeer(clientId = id, name = name, host = host, port = 51999)

    /** Stands in for the Go binding, recording what it was asked. */
    private class FakeCrypto(
        private val verifyResult: () -> String = { throw IllegalStateException("refused by fake") },
    ) : PeerCrypto {
        override val unavailable: String? = null
        var verifiedClaim: Long? = null
        var verifiedRoster: String? = null
        var provedEndpoint: String? = null

        override fun newChallenge(): String = "c0ffee"
        override fun proof(credentialJson: String, challengeHex: String, endpoint: String): String {
            provedEndpoint = endpoint
            return "proof-for-$challengeHex"
        }
        override fun verify(rosterJson: String, claimedId: Long, challengeHex: String, proofHex: String): String {
            verifiedClaim = claimedId
            verifiedRoster = rosterJson
            return verifyResult()
        }
    }

    // ---- the advertisement ----------------------------------------------

    @Test
    fun `the service type matches the iOS app`() {
        assertEquals("_zippie._tcp", PeerDiscovery.SERVICE_TYPE)
    }

    @Test
    fun `the TXT record carries only the claimed id`() {
        val txt = PeerDiscovery.txtRecord(7)
        assertEquals(mapOf("id" to "7"), txt)
        assertEquals(7L, PeerDiscovery.claimedClientId(mapOf("id" to "7".toByteArray())))
    }

    @Test
    fun `a missing or nonsense claim is no claim`() {
        for (raw in listOf(null, "0", "-4", "seven", "4294967296")) {
            val attrs = if (raw == null) emptyMap() else mapOf("id" to raw.toByteArray())
            assertNull("claim $raw", PeerDiscovery.claimedClientId(attrs))
        }
    }

    // ---- the pairing material -------------------------------------------

    @Test
    fun `a pairing bundle splits into credential and roster`() {
        val p = pairing()
        assertEquals(3L, p.clientId)
        val cred = JSONObject(p.credentialJson)
        assertEquals(3L, cred.getLong("client_id"))
        assertEquals(key, cred.getString("key_hex"))
        assertEquals("spare-android", JSONArray(p.rosterJson).getJSONObject(0).getString("name"))
    }

    @Test
    fun `an unusable bundle is refused not defaulted`() {
        val bad = listOf(
            null, "", "{",
            """{"client_id": 0, "key_hex": "$key", "roster": []}""",
            """{"client_id": 3, "key_hex": "42", "roster": []}""",
            """{"client_id": 3, "key_hex": "$key"}""",
        )
        for (b in bad) assertNull("bundle $b", PeerPairing.parse(b))
    }

    // ---- the handshake ---------------------------------------------------

    @Test
    fun `the handshake messages round trip`() {
        assertEquals("ab12", PeerHandshake.challengeIn(PeerHandshake.challengeMessage("ab12")))
        assertEquals("cd34", PeerHandshake.proofIn(PeerHandshake.proofMessage("cd34")))
        assertNull(PeerHandshake.challengeIn(PeerHandshake.proofMessage("cd34")))
        assertNull(PeerHandshake.proofIn("not json".toByteArray()))
        assertNull(PeerHandshake.proofIn(ByteArray(PeerHandshake.MAX_MESSAGE_BYTES + 1)))
    }

    @Test
    fun `answering signs the endpoint the router should dial`() {
        val crypto = FakeCrypto()
        val reply = PeerVerification.answer(
            PeerHandshake.challengeMessage("c0ffee"), pairing(), "10.99.0.12:51999", crypto,
        )
        assertEquals("proof-for-c0ffee", PeerHandshake.proofIn(reply))
        assertEquals("10.99.0.12:51999", crypto.provedEndpoint)
    }

    @Test
    fun `a garbled challenge gets no answer`() {
        try {
            PeerVerification.answer("hello".toByteArray(), pairing(), "10.99.0.12:51999", FakeCrypto())
            fail("a garbled challenge was answered")
        } catch (expected: PeerHandshakeException) {
            // The reason travels with the exception, for the caller to log.
        }
    }

    @Test
    fun `verification asks Go about the advertised claim against the roster`() {
        val crypto = FakeCrypto { """{"client_id":7,"name":"spare-android","endpoint":"10.99.0.57:51999"}""" }
        val standing = PeerVerification.verify(7, pairing(), crypto) { PeerHandshake.proofMessage("abcd") }
        assertEquals(PeerStanding.Verified(peer()), standing)
        assertEquals(7L, crypto.verifiedClaim)
        assertEquals(pairing().rosterJson, crypto.verifiedRoster)
    }

    /** GO SAYS NO, SO IT IS NO. There is no fallback that trusts the claim. */
    @Test
    fun `a refused proof is refused`() {
        val standing = PeerVerification.verify(7, pairing(), FakeCrypto()) { PeerHandshake.proofMessage("abcd") }
        assertTrue("$standing", standing is PeerStanding.Refused)
    }

    @Test
    fun `a silent or broken peer is refused`() {
        val silent = PeerVerification.verify(7, pairing(), FakeCrypto()) { throw java.io.IOException("gone") }
        assertTrue("$silent", silent is PeerStanding.Refused)
        val garbled = PeerVerification.verify(7, pairing(), FakeCrypto()) { "??".toByteArray() }
        assertTrue("$garbled", garbled is PeerStanding.Refused)
    }

    // ---- the verified peer -----------------------------------------------

    @Test
    fun `the Go result becomes a peer with a split endpoint`() {
        assertEquals(
            peer(),
            VerifiedPeer.parse("""{"client_id":7,"name":"spare-android","endpoint":"10.99.0.57:51999"}"""),
        )
        assertNull(VerifiedPeer.parse("""{"client_id":7,"name":"x","endpoint":"10.99.0.57"}"""))
        assertNull(VerifiedPeer.parse("{}"))
    }

    @Test
    fun `the leg name is a router safe slug`() {
        assertEquals("spare-android", peer(name = "Spare Android").legName)
        assertEquals("peer-12", peer(id = 12, name = "").legName)
    }

    /** DELEGATED, or the router dials this phone instead of the peer (#252). */
    @Test
    fun `adopting a peer announces its signed endpoint as delegated`() {
        val cfg = peer().announcerConfig("10.99.0.1:8787", "tok")
        assertEquals("spare-android", cfg.name)
        assertEquals("spare-android", cfg.label)
        assertEquals(51999, cfg.listenPort)
        assertTrue(cfg.delegated)
    }

    // ---- what is offered -------------------------------------------------

    @Test
    fun `only verified peers are offered`() {
        val r = PeerRoster()
        listOf("a", "b", "c").forEach(r::found)
        r.record("b", PeerStanding.Verified(peer()))
        r.record("c", PeerStanding.Refused("bad proof"))
        assertEquals(listOf(peer()), offers(r, emptySet(), emptySet()))
        assertEquals(1, r.refusedCount)
    }

    @Test
    fun `this phone and existing legs are not offered`() {
        val r = PeerRoster()
        r.found("self"); r.record("self", PeerStanding.Verified(peer(3, name = "me")))
        r.found("already"); r.record("already", PeerStanding.Verified(peer(7)))
        assertEquals(emptyList<VerifiedPeer>(), offers(r, setOf("spare-android"), emptySet()))
    }

    @Test
    fun `a peer already carrying under another name is not offered`() {
        val r = PeerRoster()
        r.found("b"); r.record("b", PeerStanding.Verified(peer()))
        assertEquals(emptyList<VerifiedPeer>(), offers(r, setOf("pixel-ea83"), setOf("10.99.0.57:51999")))
        assertEquals("an adopted peer must still read as verified", listOf(peer()), r.verifiedPeers)
    }

    @Test
    fun `a lost service is no longer offered and a late record does not revive it`() {
        val r = PeerRoster()
        r.found("b"); r.record("b", PeerStanding.Verified(peer()))
        r.lost("b")
        r.record("b", PeerStanding.Verified(peer()))
        assertEquals(emptyList<VerifiedPeer>(), offers(r, emptySet(), emptySet()))
    }

    @Test
    fun `clearing forgets every service`() {
        val r = PeerRoster()
        r.found("a"); r.record("a", PeerStanding.Verified(peer()))
        r.found("b"); r.record("b", PeerStanding.Refused("no"))
        r.clear()
        assertEquals(emptyList<VerifiedPeer>(), r.verifiedPeers)
        assertEquals(0, r.refusedCount)
    }

    @Test
    fun `one peer advertised twice is offered once`() {
        val r = PeerRoster()
        for (s in listOf("wifi", "ethernet")) { r.found(s); r.record(s, PeerStanding.Verified(peer())) }
        assertEquals(1, offers(r, emptySet(), emptySet()).size)
    }

    // ---- the socket read and the published snapshot -----------------------

    @Test
    fun `a message is read up to its newline`() {
        val input = java.io.ByteArrayInputStream("{\"proof\":\"ab\"}\ntrailing".toByteArray())
        assertEquals("ab", PeerHandshake.proofIn(PeerHandshake.read(input)))
    }

    @Test
    fun `a peer that streams without end is cut off`() {
        val endless = java.io.ByteArrayInputStream(ByteArray(PeerHandshake.MAX_MESSAGE_BYTES * 2) { 'a'.code.toByte() })
        try {
            PeerHandshake.read(endless)
            fail("an unbounded message was read")
        } catch (expected: PeerHandshakeException) {
            // Bounded: a hostile peer cannot grow this phone's memory.
        }
    }

    /** A per-read timeout alone lets a peer trickle a byte just inside it, for
     *  hours. The whole message has one deadline. */
    @Test
    fun `a peer that trickles past the deadline is cut off`() {
        var now = 0L
        val trickle = object : java.io.InputStream() {
            override fun read(): Int {
                now += 1_000 // each byte arrives a second after the last
                return 'a'.code
            }
        }
        try {
            PeerHandshake.read(trickle, deadlineMs = 4_000, clock = { now })
            fail("a trickled message was read past its deadline")
        } catch (expected: PeerHandshakeException) {
            assertTrue(now <= 5_000)
        }
    }

    @Test
    fun `the published snapshot offers what the roster would`() {
        val snap = PeerSnapshot(ownClientId = 3, verified = listOf(peer(), peer(3, name = "me")))
        assertEquals(listOf(peer()), snap.offers(emptySet(), emptySet()))
        assertEquals(emptyList<VerifiedPeer>(), snap.offers(emptySet(), setOf("10.99.0.57:51999")))
    }

    // ---- keeping adopted legs in step -------------------------------------

    @Test
    fun `a peer that moved is re-pointed, not withdrawn and announced again`() {
        // Stopping withdraws the leg by name, and that withdraw races the new
        // announcer's first announce of the SAME name: if it lands last, the
        // leg is gone until the next renewal.
        val moved = peer(host = "10.99.0.58")
        val plan = AdoptionPlan.between(running = mapOf(7L to peer()), desired = mapOf(7L to moved))
        assertEquals(mapOf(7L to moved), plan.repoint)
        assertEquals(emptySet<Long>(), plan.stop)
        assertEquals(emptyMap<Long, VerifiedPeer>(), plan.start)
    }

    @Test
    fun `adoptions start, stop, and are left alone exactly when they should be`() {
        val nine = peer(9, name = "tablet")
        val plan = AdoptionPlan.between(
            running = mapOf(7L to peer(), 8L to peer(8, name = "old")),
            desired = mapOf(7L to peer(), 9L to nine),
        )
        assertEquals(setOf(8L), plan.stop)
        assertEquals(mapOf(9L to nine), plan.start)
        assertEquals(emptyMap<Long, VerifiedPeer>(), plan.repoint)
    }

    // ---- when discovery (re)starts ----------------------------------------

    private fun next(
        running: Boolean = false,
        current: PeerPairing? = null,
        found: PeerPairing? = null,
        failedAtMs: Long? = null,
        nowMs: Long = 1_000_000,
    ) = PeerLifecycle.next(running, current, found, failedAtMs, nowMs)

    @Test
    fun `a pairing MDM removed or replaced stops the old session at once`() {
        // Revoking a bundle must take effect now, not when the app next dies:
        // the old key would keep answering and the old roster keep verifying.
        assertEquals(PeerLifecycle.Step.RESTART, next(running = true, current = pairing(), found = null))
        val replaced = pairing().copy(rosterJson = "[]")
        assertEquals(PeerLifecycle.Step.RESTART, next(running = true, current = pairing(), found = replaced))
    }

    @Test
    fun `a pairing change clears a failure rather than waiting it out`() {
        assertEquals(PeerLifecycle.Step.RESTART, next(current = pairing(), found = null, failedAtMs = 999_999))
    }

    @Test
    fun `a running session with the same pairing is kept`() {
        assertEquals(PeerLifecycle.Step.KEEP, next(running = true, current = pairing(), found = pairing()))
    }

    @Test
    fun `a failure is retried, but not on every poll`() {
        val failed = 1_000_000L
        val soon = failed + PeerLifecycle.RETRY_AFTER_MS - 1
        val later = failed + PeerLifecycle.RETRY_AFTER_MS
        assertEquals(PeerLifecycle.Step.KEEP, next(current = pairing(), found = pairing(), failedAtMs = failed, nowMs = soon))
        assertEquals(PeerLifecycle.Step.START, next(current = pairing(), found = pairing(), failedAtMs = failed, nowMs = later))
    }

    @Test
    fun `nothing running and nothing failed starts`() {
        assertEquals(PeerLifecycle.Step.START, next())
    }
}
