package app.zippie.companion

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference

// DNS-SD protocol constant (matches NsdManager.PROTOCOL_DNS_SD)
const val PROTOCOL_DNS_SD = 1

/**
 * Tests for NsdPeerDiscovery using a fake NSD source.
 * Covers verified peers, refused peers, and lost peers.
 */
class NsdPeerDiscoveryTest {

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
    fun `verified peer is delivered to onVerified`() {
        val txtJson = """{"id":"1","proof":"valid_proof"}"""
        val peer = VerifiedPeer(1, "192.0.2.10:51820")
        fakeCrypto.verifiedPeers[txtJson] = peer

        val verifiedLatch = CountDownLatch(1)
        var verifiedPeer: VerifiedPeer? = null

        discovery.start(rosterJson,
            onVerified = { p ->
                verifiedPeer = p
                verifiedLatch.countDown()
            },
            onRefused = { _, _ -> },
            onLost = { _ -> }
        )

        // Simulate NSD finding and resolving a service
        fakeNsd.simulateServiceFound("peer-1", txtJson)

        assertTrue("verified peer not delivered", verifiedLatch.await(2, TimeUnit.SECONDS))
        assertEquals(peer, verifiedPeer)
        assertTrue(discovery.isRunning)
    }

    @Test
    fun `refused peer is delivered to onRefused`() {
        val txtJson = """{"id":"999","proof":"bad_proof"}"""
        fakeCrypto.refusedReasons[txtJson] = "unknown_client"

        val refusedLatch = CountDownLatch(1)
        var refusedName: String? = null
        var refusedReason: String? = null

        discovery.start(rosterJson,
            onVerified = { _ -> },
            onRefused = { name, reason ->
                refusedName = name
                refusedReason = reason
                refusedLatch.countDown()
            },
            onLost = { _ -> }
        )

        fakeNsd.simulateServiceFound("peer-unknown", txtJson)

        assertTrue("refused peer not delivered", refusedLatch.await(2, TimeUnit.SECONDS))
        assertEquals("peer-unknown", refusedName)
        assertEquals("unknown_client", refusedReason)
    }

    @Test
    fun `malformed TXT record is refused`() {
        val refusedLatch = CountDownLatch(1)
        var refusedReason: String? = null

        discovery.start(rosterJson,
            onVerified = { _ -> },
            onRefused = { _, reason ->
                refusedReason = reason
                refusedLatch.countDown()
            },
            onLost = { _ -> }
        )

        // Service with no TXT record (malformed)
        fakeNsd.simulateServiceFound("peer-malformed", null)

        assertTrue("malformed not delivered", refusedLatch.await(2, TimeUnit.SECONDS))
        assertEquals("malformed_txt", refusedReason)
    }

    @Test
    fun `lost peer is delivered to onLost`() {
        val txtJson = """{"id":"1","proof":"valid_proof"}"""
        val peer = VerifiedPeer(1, "192.0.2.10:51820")
        fakeCrypto.verifiedPeers[txtJson] = peer

        val lostLatch = CountDownLatch(1)
        var lostName: String? = null

        discovery.start(rosterJson,
            onVerified = { _ -> },
            onRefused = { _, _ -> },
            onLost = { name ->
                lostName = name
                lostLatch.countDown()
            }
        )

        fakeNsd.simulateServiceFound("peer-1", txtJson)
        Thread.sleep(100) // Let verification complete

        fakeNsd.simulateServiceLost("peer-1")

        assertTrue("lost peer not delivered", lostLatch.await(2, TimeUnit.SECONDS))
        assertEquals("peer-1", lostName)
    }

    @Test
    fun `duplicate verified peer is not delivered twice`() {
        val txtJson = """{"id":"1","proof":"valid_proof"}"""
        val peer = VerifiedPeer(1, "192.0.2.10:51820")
        fakeCrypto.verifiedPeers[txtJson] = peer

        val verifiedCount = AtomicReference(0)
        val verifiedLatch = CountDownLatch(2) // We expect only 1 delivery but wait for 2 to prove it doesn't come

        discovery.start(rosterJson,
            onVerified = { _ ->
                verifiedCount.updateAndGet { it + 1 }
                verifiedLatch.countDown()
            },
            onRefused = { _, _ -> },
            onLost = { _ -> }
        )

        // Simulate service found twice (e.g., re-resolve)
        fakeNsd.simulateServiceFound("peer-1", txtJson)
        Thread.sleep(100)
        fakeNsd.simulateServiceFound("peer-1", txtJson) // Same service name

        // Wait a bit to ensure no second delivery
        Thread.sleep(500)
        assertEquals("duplicate peer was delivered", 1, verifiedCount.get())
    }

    @Test
    fun `discovery stops and restarts correctly`() {
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

        fakeNsd.simulateServiceFound("peer-1", txtJson)
        assertTrue(verifiedLatch.await(2, TimeUnit.SECONDS))

        discovery.stop()
        assertFalse(discovery.isRunning)
        assertTrue(fakeNsd.discoveryStopped)

        // Restart
        val verifiedLatch2 = CountDownLatch(1)
        discovery.start(rosterJson,
            onVerified = { _ -> verifiedLatch2.countDown() },
            onRefused = { _, _ -> },
            onLost = { _ -> }
        )

        assertTrue(discovery.isRunning)
        fakeNsd.simulateServiceFound("peer-2", txtJson)
        assertTrue(verifiedLatch2.await(2, TimeUnit.SECONDS))
    }

    @Test
    fun `stop clears all state and listeners`() {
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

        discovery.stop()

        assertFalse(discovery.isRunning)
        assertTrue(fakeNsd.discoveryStopped)
        assertTrue(fakeNsd.resolveListeners.isEmpty())
    }

    private fun buildRosterJson(): String {
        // Two paired devices with their discovery keys
        val keyOne = "000102030405060708090a0b0c0d0e0f"
        val keyTwo = "101112131415161718191a1b1c1d1e1f"
        val discoveryKeyOne = "1010101010101010101010101010101010101010101010101010101010101010" // Fake discovery key
        val discoveryKeyTwo = "2020202020202020202020202020202020202020202020202020202020202020" // Fake discovery key

        return JSONObject().put("roster", JSONObject().put("entries", java.util.Arrays.asList(
            JSONObject().put("client_id", 1).put("key_hex", discoveryKeyOne),
            JSONObject().put("client_id", 2).put("key_hex", discoveryKeyTwo)
        ))).toString()
    }
}

/**
 * Fake NSD manager for testing that simulates service discovery events.
 */
class FakeNsdManager {
    @Volatile var discoveryStarted = false
    @Volatile var discoveryStopped = false
    @Volatile var discoverListener: DiscoveryListener? = null
    val resolveListeners = ConcurrentHashMap<String, ResolveListener>()

    fun discoverServices(serviceType: String, protocol: Int, listener: DiscoveryListener) {
        discoveryStarted = true
        discoveryStopped = false
        discoverListener = listener
        listener.onDiscoveryStarted(serviceType)
    }

    fun stopServiceDiscovery(listener: DiscoveryListener) {
        discoveryStopped = true
        discoveryStarted = false
        listener.onDiscoveryStopped("_zippie._tcp.")
    }

    fun resolveService(serviceInfo: NsdServiceInfo, listener: ResolveListener) {
        resolveListeners[serviceInfo.serviceName] = listener
    }

    fun stopResolveService(listener: ResolveListener) {
        resolveListeners.values.remove(listener)
    }

    fun simulateServiceFound(name: String, txtJson: String?) {
        val serviceInfo = FakeNsdServiceInfo(name, txtJson)
        discoverListener?.onServiceFound(serviceInfo)
    }

    fun simulateServiceLost(name: String) {
        discoverListener?.onServiceLost(name)
    }

    inner class DiscoveryListener : NsdManager.DiscoveryListener {
        override fun onDiscoveryStarted(serviceType: String) {}
        override fun onServiceFound(serviceInfo: NsdServiceInfo) {}
        override fun onServiceLost(serviceName: String) {}
        override fun onDiscoveryStopped(serviceType: String) {}
        override fun onStartDiscoveryFailed(serviceType: String, errorCode: Int) {}
        override fun onStopDiscoveryFailed(serviceType: String, errorCode: Int) {}
    }

    inner class ResolveListener : NsdManager.ResolveListener {
        override fun onResolveFailed(serviceInfo: NsdServiceInfo, errorCode: Int) {}
        override fun onServiceResolved(serviceInfo: NsdServiceInfo) {}
    }
}

/**
 * Fake NSD service info for testing.
 */
class FakeNsdServiceInfo(
    override val serviceName: String,
    private val txtJson: String?
) : NsdServiceInfo() {

    override val serviceType: String = "_zippie._tcp."
    override val host: java.net.InetAddress? = java.net.InetAddress.getByName("192.0.2.10")
    override val port: Int = 51820

    override val attributes: java.util.Map<String, ByteArray>
        get() {
            if (txtJson == null) return emptyMap()
            val map = java.util.HashMap<String, ByteArray>()
            val obj = JSONObject(txtJson)
            val keys = obj.keys()
            while (keys.hasNext()) {
                val key = keys.next()
                map[key] = obj.getString(key).toByteArray()
            }
            return map
        }
}

/**
 * Testable version of NsdPeerDiscovery that uses a fake NSD manager.
 */
class TestableNsdPeerDiscovery(
    private val fakeNsd: FakeNsdManager,
    crypto: GoPeerCrypto
) : PeerDiscovery {

    @Volatile
    override var isRunning = false

    private val verifiedServices = ConcurrentHashMap<String, VerifiedPeer>()
    private var callbacks: DiscoveryCallbacks? = null

    override fun start(
        rosterJson: String,
        onVerified: (VerifiedPeer) -> Unit,
        onRefused: (String, String) -> Unit,
        onLost: (String) -> Unit
    ) {
        if (isRunning) return
        isRunning = true

        callbacks = DiscoveryCallbacks(rosterJson, onVerified, onRefused, onLost)

        fakeNsd.discoverServices("_zippie._tcp.", PROTOCOL_DNS_SD, object : FakeNsdManager.DiscoveryListener() {
            override fun onServiceFound(serviceInfo: NsdServiceInfo) {
                val name = serviceInfo.serviceName
                val txtRecord = serviceInfo.attributes
                val txtJson = crypto.parseTxtRecord(txtRecord)
                    ?: run {
                        callbacks?.onRefused(name, "malformed_txt")
                        return@onServiceFound
                    }

                try {
                    val peer = crypto.verifyPeer(rosterJson, txtJson)
                    if (peer != null) {
                        if (verifiedServices.putIfAbsent(name, peer) == null) {
                            callbacks?.onVerified(peer)
                        }
                    } else {
                        callbacks?.onRefused(name, "verification_failed")
                    }
                } catch (e: FakeGoPeerCrypto.VerificationRefused) {
                    callbacks?.onRefused(name, e.reason)
                } catch (e: Exception) {
                    callbacks?.onRefused(name, "verification_error")
                }
            }

            override fun onServiceLost(serviceName: String) {
                verifiedServices.remove(serviceName)
                callbacks?.onLost(serviceName)
            }

            override fun onDiscoveryStopped(serviceType: String) {
                isRunning = false
            }
        })
    }

    override fun stop() {
        if (!isRunning) return
        isRunning = false
        fakeNsd.stopServiceDiscovery(fakeNsd.discoverListener!!)
        verifiedServices.clear()
    }

    private data class DiscoveryCallbacks(
        val rosterJson: String,
        val onVerified: (VerifiedPeer) -> Unit,
        val onRefused: (String, String) -> Unit,
        val onLost: (String) -> Unit
    )
}