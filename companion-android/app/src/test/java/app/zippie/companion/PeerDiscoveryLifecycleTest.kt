package app.zippie.companion

import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleOwner
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * Discovery follows the app: the lifecycle observer stops it when the app
 * goes to the background and starts it again on resume, and nothing NSD was
 * given stays registered across the cycle.
 */
class PeerDiscoveryLifecycleTest {

    private lateinit var source: FakeNsdSource
    private lateinit var crypto: FakeGoPeerCrypto
    private lateinit var discovery: NsdPeerDiscovery
    private lateinit var observer: PeerDiscoveryLifecycle

    private val verified = mutableListOf<VerifiedPeer>()
    private val lost = mutableListOf<String>()

    private val peerOne = VerifiedPeer(1, "192.0.2.10:51820")
    private val goodTxt = mapOf("id" to "1", "proof" to "good")

    // The observer never reads the owner; the callbacks only need one to exist.
    private val owner = object : LifecycleOwner {
        override val lifecycle: Lifecycle
            get() = throw UnsupportedOperationException("not used")
    }

    @Before
    fun setUp() {
        source = FakeNsdSource()
        crypto = FakeGoPeerCrypto()
        crypto.accepted["1:good"] = peerOne
        discovery = NsdPeerDiscovery(source, crypto)
        observer = PeerDiscoveryLifecycle(
            discovery,
            rosterJson = { NsdPeerDiscoveryTest.ROSTER },
            onVerified = { verified.add(it) },
            onRefused = { _, _ -> },
            onLost = { lost.add(it) },
        )
    }

    private fun foreground() = observer.onStart(owner)
    private fun background() = observer.onStop(owner)

    @Test
    fun `discovery starts when the app comes to the foreground`() {
        foreground()
        assertTrue(discovery.isRunning)
        assertTrue(source.isDiscovering)
    }

    @Test
    fun `discovery stops when the app goes to the background`() {
        foreground()
        source.announce("peer-1", goodTxt)
        background()
        assertFalse(discovery.isRunning)
        assertFalse("NSD listener still registered", source.isDiscovering)
        assertEquals(1, source.stopCalls)
    }

    @Test
    fun `a resolve in flight at background is cancelled and its late result dropped`() {
        foreground()
        source.found("peer-1")
        val staleListener = source.listener!!
        background()
        assertTrue("resolve leaked past background", source.activeResolves.isEmpty())
        assertEquals(listOf("peer-1"), source.cancelledResolves)
        staleListener.onServiceResolved("peer-1", goodTxt.mapValues { it.value.toByteArray() })
        assertTrue(verified.isEmpty())
    }

    @Test
    fun `events from the old listener after background reach nobody`() {
        foreground()
        source.announce("peer-1", goodTxt)
        val staleListener = source.listener!!
        background()
        staleListener.onServiceLost("peer-1")
        staleListener.onServiceFound("peer-2")
        assertTrue(lost.isEmpty())
        assertTrue(source.activeResolves.isEmpty())
    }

    @Test
    fun `discovery restarts on resume and delivers peers through the same callbacks`() {
        foreground()
        source.announce("peer-1", goodTxt)
        background()
        foreground()
        assertTrue(discovery.isRunning)
        assertEquals(2, source.discoverCalls)
        // The same peer is verified afresh after resume, not suppressed as a duplicate.
        source.announce("peer-1", goodTxt)
        assertEquals(listOf(peerOne, peerOne), verified)
    }

    @Test
    fun `repeated cycles leave one listener at most and no resolves`() {
        repeat(3) { cycle ->
            foreground()
            source.found("peer-$cycle")
            background()
            assertFalse(source.isDiscovering)
            assertTrue(source.activeResolves.isEmpty())
        }
        assertEquals(3, source.discoverCalls)
        assertEquals(3, source.stopCalls)
    }

    @Test
    fun `background without a foreground does not touch NSD`() {
        background()
        assertEquals(0, source.stopCalls)
    }
}
