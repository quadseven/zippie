package app.zippie.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * Drives the real NsdPeerDiscovery through a fake NSD source: verified,
 * refused and lost peers, and what happens to results that arrive late.
 */
class NsdPeerDiscoveryTest {

    private lateinit var source: FakeNsdSource
    private lateinit var crypto: FakeGoPeerCrypto
    private lateinit var discovery: NsdPeerDiscovery

    private val verified = mutableListOf<VerifiedPeer>()
    private val refused = mutableListOf<Pair<String, String>>()
    private val lost = mutableListOf<String>()

    private val peerOne = VerifiedPeer(1, "192.0.2.10:51820")
    private val goodTxt = mapOf("id" to "1", "proof" to "good")

    @Before
    fun setUp() {
        source = FakeNsdSource()
        crypto = FakeGoPeerCrypto()
        crypto.accepted["1:good"] = peerOne
        discovery = NsdPeerDiscovery(source, crypto)
    }

    private fun start() {
        discovery.start(
            ROSTER,
            onVerified = { verified.add(it) },
            onRefused = { name, reason -> refused.add(name to reason) },
            onLost = { lost.add(it) },
        )
    }

    @Test
    fun `start asks NSD to discover the zippie service type`() {
        start()
        assertTrue(discovery.isRunning)
        assertTrue(source.isDiscovering)
        assertEquals(1, source.discoverCalls)
    }

    @Test
    fun `a found service is resolved before anything is delivered`() {
        start()
        source.found("peer-1")
        assertEquals(setOf("peer-1"), source.activeResolves)
        assertTrue(verified.isEmpty())
        assertTrue(crypto.verifyCalls.isEmpty())
    }

    @Test
    fun `a peer the Go core verifies is delivered`() {
        start()
        source.announce("peer-1", goodTxt)
        assertEquals(listOf(peerOne), verified)
        assertTrue(refused.isEmpty())
    }

    @Test
    fun `the TXT record reaches the Go core as JSON with id and proof`() {
        start()
        source.announce("peer-1", goodTxt)
        val sent = org.json.JSONObject(crypto.verifyCalls.single())
        assertEquals("1", sent.getString("id"))
        assertEquals("good", sent.getString("proof"))
    }

    @Test
    fun `a peer the Go core refuses never reaches onVerified`() {
        crypto.refused["9"] = "unknown_client"
        start()
        source.announce("peer-9", mapOf("id" to "9", "proof" to "forged"))
        assertTrue(verified.isEmpty())
        assertEquals(listOf("peer-9" to "unknown_client"), refused)
    }

    @Test
    fun `a TXT record without a proof is refused as malformed without calling the Go core`() {
        start()
        source.announce("peer-x", mapOf("id" to "1"))
        assertTrue(verified.isEmpty())
        assertEquals(listOf("peer-x" to "malformed_txt"), refused)
        assertTrue(crypto.verifyCalls.isEmpty())
    }

    @Test
    fun `an empty TXT record is refused as malformed`() {
        start()
        source.announce("peer-x", emptyMap())
        assertEquals(listOf("peer-x" to "malformed_txt"), refused)
    }

    @Test
    fun `a failed resolve is reported as refused`() {
        start()
        source.found("peer-1")
        source.resolveFailed("peer-1", 3)
        assertEquals(listOf("peer-1" to "resolve_failed_3"), refused)
        assertTrue(verified.isEmpty())
    }

    @Test
    fun `a verified peer that goes away is reported lost`() {
        start()
        source.announce("peer-1", goodTxt)
        source.lost("peer-1")
        assertEquals(listOf("peer-1"), lost)
    }

    @Test
    fun `losing a peer that was never verified tells the app nothing`() {
        crypto.refused["9"] = "unknown_client"
        start()
        source.announce("peer-9", mapOf("id" to "9", "proof" to "forged"))
        source.lost("peer-9")
        assertTrue(lost.isEmpty())
    }

    @Test
    fun `a service lost mid-resolve has its resolve cancelled and delivers nothing`() {
        start()
        source.found("peer-1")
        source.lost("peer-1")
        assertEquals(listOf("peer-1"), source.cancelledResolves)
        // A late resolve from a platform that cannot cancel is ignored.
        source.resolved("peer-1", goodTxt)
        assertTrue(verified.isEmpty())
        assertTrue(lost.isEmpty())
    }

    @Test
    fun `a re-resolve of a verified peer is not delivered twice`() {
        start()
        source.announce("peer-1", goodTxt)
        source.announce("peer-1", goodTxt)
        assertEquals(listOf(peerOne), verified)
    }

    @Test
    fun `a peer lost and found again is delivered again`() {
        start()
        source.announce("peer-1", goodTxt)
        source.lost("peer-1")
        source.announce("peer-1", goodTxt)
        assertEquals(listOf(peerOne, peerOne), verified)
    }

    @Test
    fun `a failed discovery start leaves discovery stopped and restartable`() {
        start()
        source.discoveryFailed(0)
        assertFalse(discovery.isRunning)
        assertNull(source.listener)
        start()
        assertTrue(discovery.isRunning)
        assertEquals(2, source.discoverCalls)
    }

    @Test
    fun `a stop that lands while the Go core is verifying delivers nothing`() {
        val stopping = object : GoPeerCrypto {
            override fun peerTxt(credentialJson: String, endpoint: String): String? = null
            override fun verifyPeer(rosterJson: String, txtJson: String): VerificationResult {
                discovery.stop()
                return VerificationResult.Verified(peerOne)
            }
        }
        discovery = NsdPeerDiscovery(source, stopping)
        start()
        val listener = source.listener!!
        listener.onServiceFound("peer-1")
        listener.onServiceResolved("peer-1", goodTxt.mapValues { it.value.toByteArray() })
        assertTrue(verified.isEmpty())
        assertFalse(discovery.isRunning)
    }

    @Test
    fun `no callback runs while stop is in progress on another thread`() {
        // stop() holds the discovery lock while it retires the run, so a
        // callback either finishes before stop() starts or never runs.
        val inCallback = java.util.concurrent.CountDownLatch(1)
        val release = java.util.concurrent.CountDownLatch(1)
        discovery.start(
            ROSTER,
            onVerified = {},
            onRefused = { _, _ ->
                inCallback.countDown()
                release.await()
            },
            onLost = {},
        )
        source.found("peer-1")
        val listener = source.listener!!
        val failing = Thread { listener.onResolveFailed("peer-1", 3) }
        failing.start()
        assertTrue(inCallback.await(2, java.util.concurrent.TimeUnit.SECONDS))
        val stopper = Thread { discovery.stop() }
        stopper.start()
        stopper.join(200)
        assertTrue("stop returned while a callback was still running", stopper.isAlive)
        release.countDown()
        stopper.join(2000)
        failing.join(2000)
        assertFalse(discovery.isRunning)
    }

    @Test
    fun `start twice registers one listener`() {
        start()
        start()
        assertEquals(1, source.discoverCalls)
    }

    companion object {
        const val ROSTER =
            """{"entries":[{"client_id":1,"key_hex":"1010101010101010101010101010101010101010101010101010101010101010"}]}"""
    }
}
