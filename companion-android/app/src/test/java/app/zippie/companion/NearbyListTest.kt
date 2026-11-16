package app.zippie.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * The Nearby list, driven through the real NsdPeerDiscovery and a fake NSD
 * source: only verified peers appear, refused ones never do, a vanished one
 * leaves in the same browse cycle, and choosing one yields a manual-edit leg.
 */
class NearbyListTest {

    private lateinit var source: FakeNsdSource
    private lateinit var crypto: FakeGoPeerCrypto
    private lateinit var discovery: NsdPeerDiscovery
    private val nearby = NearbyList()

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
        discovery.start(
            NsdPeerDiscoveryTest.ROSTER,
            onVerified = nearby::onVerified,
            onRefused = { _, _ -> },
            onLost = nearby::onLost,
        )
    }

    @Test
    fun `only verified peers appear in the nearby list`() {
        crypto.refused["3"] = "unknown_client"
        source.announce("peer-1", goodTxt)
        source.announce("peer-2", mapOf("id" to "2", "proof" to "good"))
        source.announce("peer-3", mapOf("id" to "3", "proof" to "forged"))

        assertEquals(listOf(peerOne, peerTwo), nearby.peers)
    }

    @Test
    fun `a refused peer never appears in the nearby list`() {
        crypto.refused["9"] = "unknown_client"
        source.announce("peer-9", mapOf("id" to "9", "proof" to "forged"))

        assertTrue(nearby.peers.isEmpty())
    }

    @Test
    fun `a vanished peer leaves the nearby list in the same browse cycle`() {
        source.announce("peer-1", goodTxt)
        assertEquals(listOf(peerOne), nearby.peers)

        source.lost("peer-1")

        assertTrue(nearby.peers.isEmpty())
    }

    @Test
    fun `choosing a nearby peer yields the leg a manual edit would`() {
        source.announce("peer-1", goodTxt)
        val chosen = nearby.peers.single()

        val manual = NearbyLegEdit(
            legName = "peer-1",
            fields = mapOf("label" to "Nearby peer 1", "enabled" to true),
        )
        assertEquals(manual, NearbyLeg.edit(chosen))
        assertTrue(LegName.isValid(NearbyLeg.edit(chosen).legName))
    }
}
