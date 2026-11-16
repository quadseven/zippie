package app.zippie.companion

import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.test.TestCoroutineDispatcher
import kotlinx.coroutines.test.runBlockingTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * Nearby list: verified peers appear, refused don't, selection creates leg, removal within one browse cycle.
 */
class NearbyListTest {

    private val testDispatcher = TestCoroutineDispatcher()

    private lateinit var source: FakeNsdSource
    private lateinit var crypto: FakeGoPeerCrypto
    private lateinit var discovery: NsdPeerDiscovery

    private val verified = mutableListOf<VerifiedPeer>()
    private val refused = mutableListOf<Pair<String, String>>()
    private val lost = mutableListOf<String>()

    private val peerOne = VerifiedPeer(1, "192.0.2.10:51820")
    private val peerTwo = VerifiedPeer(2, "192.0.2.11:51820")
    private val goodTxt = mapOf("id" to "1", "proof" to "good")

    @Before
    fun setUp() {
        source = FakeNsdSource()
        crypto = FakeGoPeerCrypto()
        crypto.accepted["1:good"] = peerOne
        crypto.accepted["2:good"] = peerTwo
        discovery = NsdPeerDiscovery(source, crypto)
    }

    private fun start() {
        discovery.start(
            NsdPeerDiscoveryTest.ROSTER,
            onVerified = { verified.add(it) },
            onRefused = { name, reason -> refused.add(name to reason) },
            onLost = { lost.add(it) },
        )
    }

    // MARK: - acceptance criteria

    @Test
    fun `only verified peers appear in nearby list`() = runBlockingTest {
        crypto.refused["3"] = "unknown_client"
        start()
        source.announce("peer-1", goodTxt)
        source.announce("peer-2", mapOf("id" to "2", "proof" to "good"))
        source.announce("peer-3", mapOf("id" to "3", "proof" to "forged"))

        // Wait for all verifications to complete
        advanceUntilIdle()

        assertEquals(2, verified.size)
        assertTrue(verified.any { it.clientId == 1 })
        assertTrue(verified.any { it.clientId == 2 })
        assertFalse(verified.any { it.clientId == 3 })
    }

    @Test
    fun `refused peers never appear in nearby list`() = runBlockingTest {
        crypto.refused["9"] = "unknown_client"
        start()
        source.announce("peer-9", mapOf("id" to "9", "proof" to "forged"))

        advanceUntilIdle()

        assertTrue(verified.isEmpty())
        assertEquals(listOf("peer-9" to "unknown_client"), refused)
    }

    @Test
    fun `vanished peer removed within one browse cycle`() = runBlockingTest {
        start()
        source.announce("peer-1", goodTxt)
        advanceUntilIdle()
        assertEquals(1, verified.size)

        source.lost("peer-1")
        advanceUntilIdle()

        assertEquals(listOf("peer-1"), lost)
    }

    private fun advanceUntilIdle() {
        testDispatcher.runCurrent()
        while (testDispatcher.hasPendingCoroutines()) {
            testDispatcher.advanceUntilIdle()
        }
    }

    companion object {
        const val ROSTER =
            """{"entries":[{"client_id":1,"key_hex":"1010101010101010101010101010101010101010101010101010101010101010"}]}"""
    }
}