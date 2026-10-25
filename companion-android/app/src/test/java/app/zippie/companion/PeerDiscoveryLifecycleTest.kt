package app.zippie.companion

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

/**
 * Tests for peer discovery lifecycle: stops on background, restarts on resume.
 * Verifies no leaked listeners.
 */
class PeerDiscoveryLifecycleTest {

    private lateinit var fakeNsd: FakeNsdManager
    private lateinit var fakeCrypto: FakeGoPeerCrypto
    private lateinit var discovery: TestableNsdPeerDiscovery
    private val rosterJson = buildRosterJson()

    @Before
    fun setUp() {
        fakeNsd = FakeNsdManager()
        fakeCrypto = FakeGoPeerCrypto()
        discovery = TestableNsdPeerDiscovery(fakeNsd, fakeCrypto)
    }

    @Test
    fun `discovery stops when app goes to background`() {
        val txtJson = """{"id":"1","proof":"valid_proof"}"""
        val peer = VerifiedPeer(1, "192.0.2.10:51820")
        fakeCrypto.verifiedPeers[txtJson] = peer

        val verifiedLatch = CountDownLatch(1)
        discovery.start(rosterJson,
            onVerified = { _ -> verifiedLatch.countDown() },
            onRefused = { _, _ -> },
            onLost = { _ -> }
        )

        assertTrue(discovery.isRunning)
        assertTrue(fakeNsd.discoveryStarted)

        fakeNsd.simulateServiceFound("peer-1", txtJson)
        assertTrue(verifiedLatch.await(2, TimeUnit.SECONDS))

        // Simulate app going to background
        onAppBackground()

        assertFalse("discovery should stop on background", discovery.isRunning)
        assertTrue("NSD discovery should be stopped", fakeNsd.discoveryStopped)
        assertTrue("resolve listeners should be cleared", fakeNsd.resolveListeners.isEmpty())
    }

    @Test
    fun `discovery restarts when app resumes from background`() {
        val txtJson = """{"id":"1","proof":"valid_proof"}"""
        val peer = VerifiedPeer(1, "192.0.2.10:51820")
        fakeCrypto.verifiedPeers[txtJson] = peer

        // Start initially
        val verifiedLatch1 = CountDownLatch(1)
        discovery.start(rosterJson,
            onVerified = { _ -> verifiedLatch1.countDown() },
            onRefused = { _, _ -> },
            onLost = { _ -> }
        )
        fakeNsd.simulateServiceFound("peer-1", txtJson)
        assertTrue(verifiedLatch1.await(2, TimeUnit.SECONDS))

        // Go to background
        onAppBackground()
        assertFalse(discovery.isRunning)
        assertTrue(fakeNsd.discoveryStopped)

        // Resume
        val verifiedLatch2 = CountDownLatch(1)
        onAppResume(onVerified = { _ -> verifiedLatch2.countDown() })

        assertTrue("discovery should restart on resume", discovery.isRunning)
        assertTrue("NSD discovery should be started again", fakeNsd.discoveryStarted)

        fakeNsd.simulateServiceFound("peer-2", txtJson)
        assertTrue("peer should be discovered after resume", verifiedLatch2.await(2, TimeUnit.SECONDS))
    }

    @Test
    fun `no leaked listeners after multiple background and resume cycles`() {
        val txtJson = """{"id":"1","proof":"valid_proof"}"""
        val peer = VerifiedPeer(1, "192.0.2.10:51820")
        fakeCrypto.verifiedPeers[txtJson] = peer

        // Cycle 1
        discovery.start(rosterJson,
            onVerified = { _ -> },
            onRefused = { _, _ -> },
            onLost = { _ -> }
        )
        fakeNsd.simulateServiceFound("peer-1", txtJson)
        Thread.sleep(100)
        onAppBackground()

        // Cycle 2
        onAppResume(onVerified = { _ -> }, onRefused = { _, _ -> }, onLost = { _ -> })
        fakeNsd.simulateServiceFound("peer-2", txtJson)
        Thread.sleep(100)
        onAppBackground()

        // Cycle 3
        onAppResume(onVerified = { _ -> }, onRefused = { _, _ -> }, onLost = { _ -> })
        fakeNsd.simulateServiceFound("peer-3", txtJson)
        Thread.sleep(100)
        onAppBackground()

        // Final stop
        discovery.stop()

        // Verify no leaked listeners
        assertTrue("resolve listeners leaked after cycles", fakeNsd.resolveListeners.isEmpty())
        assertFalse("discovery still running after stop", discovery.isRunning)
        assertTrue("NSD discovery not stopped", fakeNsd.discoveryStopped)
    }

    @Test
    fun `pending resolves are cancelled on background`() {
        val txtJson = """{"id":"1","proof":"valid_proof"}"""
        fakeCrypto.verifiedPeers[txtJson] = VerifiedPeer(1, "192.0.2.10:51820")

        discovery.start(rosterJson,
            onVerified = { _ -> },
            onRefused = { _, _ -> },
            onLost = { _ -> }
        )

        // Simulate service found but not yet resolved
        fakeNsd.simulateServiceFound("peer-pending", txtJson)
        Thread.sleep(50) // Give time for resolve to be initiated

        // Verify resolve listener was created
        assertFalse("resolve listener should exist", fakeNsd.resolveListeners.isEmpty())

        // Go to background
        onAppBackground()

        // All resolve listeners should be cleared
        assertTrue("resolve listeners leaked on background", fakeNsd.resolveListeners.isEmpty())
    }

    @Test
    fun `verified peer state is cleared on background`() {
        val txtJson = """{"id":"1","proof":"valid_proof"}"""
        val peer = VerifiedPeer(1, "192.0.2.10:51820")
        fakeCrypto.verifiedPeers[txtJson] = peer

        discovery.start(rosterJson,
            onVerified = { _ -> },
            onRefused = { _, _ -> },
            onLost = { _ -> }
        )

        fakeNsd.simulateServiceFound("peer-1", txtJson)
        Thread.sleep(100)

        // Peer should be in verified state
        // (We can't directly access the internal map, but we can verify
        // that a duplicate doesn't re-deliver - the test in NsdPeerDiscoveryTest
        // covers this. Here we just verify the lifecycle.)

        onAppBackground()

        // After background, the verified state should be cleared
        // so that on resume, the same peer would be re-verified
        onAppResume(onVerified = { _ -> }, onRefused = { _, _ -> }, onLost = { _ -> })

        val verifiedLatch = CountDownLatch(1)
        // Replace callback to catch re-verification
        // Note: TestableNsdPeerDiscovery doesn't support callback replacement,
        // but the real implementation would re-verify on resume
        discovery.stop()
        discovery.start(rosterJson,
            onVerified = { _ -> verifiedLatch.countDown() },
            onRefused = { _, _ -> },
            onLost = { _ -> }
        )

        fakeNsd.simulateServiceFound("peer-1", txtJson)
        assertTrue("peer should be re-verified after resume", verifiedLatch.await(2, TimeUnit.SECONDS))
    }

    private fun onAppBackground() {
        discovery.stop()
    }

    private fun onAppResume(
        onVerified: (VerifiedPeer) -> Unit,
        onRefused: (String, String) -> Unit,
        onLost: (String) -> Unit
    ) {
        discovery.start(rosterJson, onVerified = onVerified, onRefused = onRefused, onLost = onLost)
    }

    private fun buildRosterJson(): String {
        val discoveryKeyOne = "1010101010101010101010101010101010101010101010101010101010101010"
        val discoveryKeyTwo = "2020202020202020202020202020202020202020202020202020202020202020"
        return """{"entries":[{"client_id":1,"key_hex":"$discoveryKeyOne"},{"client_id":2,"key_hex":"$discoveryKeyTwo"}]}"""
    }
}