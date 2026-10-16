package app.zippie.companion

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.util.Log
import java.io.IOException
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.RejectedExecutionException
import java.util.concurrent.Semaphore

/**
 * Same-LAN discovery over NSD (#270): register `_zippie._tcp`, discover it,
 * challenge every device found, and offer the ones that prove a pairing as
 * legs. The Android counterpart of PeerDiscoveryService.swift; every decision
 * is made by the tested code in PeerDiscovery.kt and, for trust, by Go.
 *
 * WHAT IS TRUSTED. An NSD result is a claim from anything on the wifi. It
 * becomes a [VerifiedPeer] only when the binding has checked its proof against
 * the roster, and an adopted leg dials the endpoint inside that proof, never
 * the address NSD resolved.
 *
 * ADOPTION IS A LEASE. Adding a peer starts a delegated [LegAnnouncer] for it,
 * renewed only while the peer is still discovered and verified; when it
 * leaves, the announcer withdraws it, and when it comes back verified,
 * renewing resumes without another tap.
 *
 * ONE THREAD OWNS THE STATE. NSD calls back on binder threads and the sockets
 * block, so every roster and adoption change is posted to [worker]; blocking
 * socket work runs on [io] and posts its result back. Both go through [post]
 * and [spawn], which drop work once [close] has run: a late callback must not
 * throw on a thread nothing catches on, which would take the app down.
 */
class NsdPeerDiscovery(
    private val context: Context,
    private val publish: (PeerSnapshot) -> Unit,
    private val crypto: PeerCrypto = GoPeerCrypto(),
) {
    private val nsd: NsdManager? = context.getSystemService(NsdManager::class.java)
    private val worker: ExecutorService = Executors.newSingleThreadExecutor()
    private val io: ExecutorService = Executors.newCachedThreadPool()

    /** Verifications of discovered devices, a few at a time. Each is bounded
     *  by the handshake deadline; the pool bounds how many run at once. */
    private val verifying: ExecutorService = Executors.newFixedThreadPool(MAX_VERIFYING)

    /** Bounds the challenges answered at once. Anything on the wifi can
     *  connect, and a thread per connection would let it exhaust the phone. */
    private val answering = Semaphore(MAX_ANSWERING)

    private val roster = PeerRoster()
    private var snapshot = PeerSnapshot()
    private var pairing: PeerPairing? = null
    private var server: ServerSocket? = null
    private var registration: NsdManager.RegistrationListener? = null
    private var discovery: NsdManager.DiscoveryListener? = null
    private val adopted = mutableSetOf<Long>()
    private val running = mutableMapOf<Long, Adoption>()

    /** Older Android refuses a second resolve while one is in flight, so they
     *  are queued and run one at a time. */
    private val toResolve = ArrayDeque<NsdServiceInfo>()
    private var resolving = false

    private class Adoption(val peer: VerifiedPeer, val announcer: LegAnnouncer)

    /** Starts if a pairing is present and discovery is not already running.
     *  Cheap to call repeatedly, so a pairing pushed by MDM later is picked up
     *  by the next call. */
    fun start() {
        post { startOnWorker() }
    }

    fun adopt(peer: VerifiedPeer) {
        post {
            adopted += peer.clientId
            reconcile()
        }
    }

    fun release(peer: VerifiedPeer) {
        post {
            adopted -= peer.clientId
            snapshot = snapshot.copy(outcomes = snapshot.outcomes - peer.clientId)
            reconcile()
        }
    }

    /** Stops everything, withdraws adopted legs, and releases the threads. */
    fun close() {
        post { stopOnWorker() }
        worker.shutdown()
        io.shutdownNow()
        verifying.shutdownNow()
    }

    private fun post(task: () -> Unit): Boolean = submit(worker, task)

    private fun spawn(task: () -> Unit): Boolean = submit(io, task)

    private fun check(task: () -> Unit): Boolean = submit(verifying, task)

    private fun submit(executor: ExecutorService, task: () -> Unit): Boolean = try {
        executor.execute(task)
        true
    } catch (e: RejectedExecutionException) {
        Log.i(TAG, "discovery is closed; dropped a late callback")
        false
    }

    // ---- lifecycle, on the worker -----------------------------------------

    private fun startOnWorker() {
        // A failure stays on screen rather than being retried every poll.
        if (server != null || snapshot.status == PeerSnapshot.Status.FAILED) return
        val nsd = this.nsd ?: return fail("this phone has no network service discovery")
        val managed = AndroidManagedConfig.read(context)
        val found = ManagedConfig.peerPairing(managed) ?: return unpaired(managed)
        pairing = found
        try {
            val socket = ServerSocket(0)
            server = socket
            spawn { serve(socket, found) }
            register(nsd, found, socket.localPort)
            browse(nsd)
        } catch (e: IOException) {
            return fail("advertising: ${e.message}")
        }
        emit(PeerSnapshot(status = PeerSnapshot.Status.RUNNING, ownClientId = found.clientId))
    }

    /** No usable pairing. Said differently when MDM pushed one that does not
     *  parse, so a broken push does not read as no push at all. */
    private fun unpaired(managed: Map<String, Any?>) {
        val pushed = !(managed[ManagedConfig.KEY_PEER_PAIRING] as? String).isNullOrBlank()
        val reason = if (pushed) "The pushed pairing bundle is unusable: it needs client_id, key_hex and roster." else null
        if (reason != null && snapshot.failure != reason) Log.w(TAG, reason)
        emit(PeerSnapshot(status = PeerSnapshot.Status.UNPAIRED, failure = reason))
    }

    private fun stopOnWorker() {
        server?.close()
        server = null
        // A listener NSD already dropped (after a failure callback) throws
        // here; that is a log line, not a reason to crash the app.
        try {
            registration?.let { nsd?.unregisterService(it) }
            discovery?.let { nsd?.stopServiceDiscovery(it) }
        } catch (e: IllegalArgumentException) {
            Log.w(TAG, "NSD teardown: ${e.message}")
        }
        registration = null
        discovery = null
        toResolve.clear()
        // The operator's choices in [adopted] are kept, as on iOS: a restart
        // resumes announcing them once they verify again.
        roster.clear()
        reconcile()
    }

    private fun fail(reason: String) {
        Log.w(TAG, "peer discovery stopped: $reason")
        stopOnWorker()
        emit(PeerSnapshot(status = PeerSnapshot.Status.FAILED, failure = reason))
    }

    private fun emit(next: PeerSnapshot) {
        snapshot = next
        publish(next)
    }

    // ---- advertising this phone -------------------------------------------

    private fun register(nsd: NsdManager, pairing: PeerPairing, port: Int) {
        val info = NsdServiceInfo().apply {
            serviceName = "zippie-${pairing.clientId}"
            serviceType = PeerDiscovery.SERVICE_TYPE
            setPort(port)
            PeerDiscovery.txtRecord(pairing.clientId).forEach { (k, v) -> setAttribute(k, v) }
        }
        val listener = object : NsdManager.RegistrationListener {
            override fun onServiceRegistered(info: NsdServiceInfo) {
                Log.i(TAG, "advertising ${info.serviceName}")
            }
            override fun onRegistrationFailed(info: NsdServiceInfo, code: Int) {
                post {
                    registration = null // NSD has already dropped this listener
                    fail("advertising failed (NSD error $code)")
                }
            }
            override fun onServiceUnregistered(info: NsdServiceInfo) {
                Log.i(TAG, "stopped advertising ${info.serviceName}")
            }
            override fun onUnregistrationFailed(info: NsdServiceInfo, code: Int) {
                Log.w(TAG, "could not stop advertising (NSD error $code)")
            }
        }
        registration = listener
        nsd.registerService(info, NsdManager.PROTOCOL_DNS_SD, listener)
    }

    /** Answers challenges until the socket is closed by [stopOnWorker]. */
    private fun serve(socket: ServerSocket, pairing: PeerPairing) {
        while (!socket.isClosed) {
            val client = try {
                socket.accept()
            } catch (e: IOException) {
                // A closed socket is the stop signal; anything else is a fault.
                if (!socket.isClosed) post { fail("accepting: ${e.message}") }
                return
            }
            if (!answering.tryAcquire()) {
                Log.w(TAG, "too many challenges at once; refused a connection")
                client.close()
                continue
            }
            val started = spawn {
                try {
                    answer(client, pairing)
                } finally {
                    answering.release()
                }
            }
            if (!started) {
                answering.release()
                client.close()
            }
        }
    }

    private fun answer(client: Socket, pairing: PeerPairing) = client.use { s ->
        try {
            s.soTimeout = TIMEOUT_MS
            val endpoint = relayEndpoint()
                ?: throw PeerHandshakeException("this phone has no wifi address to sign")
            val reply = PeerVerification.answer(PeerHandshake.read(s.getInputStream()), pairing, endpoint, crypto)
            s.getOutputStream().write(reply)
        } catch (e: Exception) {
            Log.w(TAG, "peer challenge unanswered: ${e.message}")
        }
    }

    /** The endpoint this phone signs: its wifi address and the relay's listen
     *  port, which is what the router dials for any phone leg. */
    private fun relayEndpoint(): String? {
        val address = LocalAddress.wifiIPv4(context) ?: return null
        return "$address:${AndroidManagedConfig.effectiveConfig(context).listenPort}"
    }

    // ---- finding and challenging others -----------------------------------

    private fun browse(nsd: NsdManager) {
        val listener = object : NsdManager.DiscoveryListener {
            override fun onDiscoveryStarted(serviceType: String) {
                Log.i(TAG, "browsing $serviceType")
            }
            override fun onDiscoveryStopped(serviceType: String) {
                Log.i(TAG, "stopped browsing $serviceType")
            }
            override fun onStartDiscoveryFailed(serviceType: String, code: Int) {
                post {
                    discovery = null // NSD has already dropped this listener
                    fail("browsing failed (NSD error $code)")
                }
            }
            override fun onStopDiscoveryFailed(serviceType: String, code: Int) {
                Log.w(TAG, "could not stop browsing (NSD error $code)")
            }
            override fun onServiceFound(info: NsdServiceInfo) {
                post { found(info) }
            }
            override fun onServiceLost(info: NsdServiceInfo) {
                post {
                    roster.lost(info.serviceName)
                    reconcile()
                }
            }
        }
        discovery = listener
        nsd.discoverServices(PeerDiscovery.SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, listener)
    }

    private fun found(info: NsdServiceInfo) {
        roster.found(info.serviceName)
        toResolve.addLast(info)
        resolveNext()
    }

    private fun resolveNext() {
        val nsd = this.nsd ?: return
        if (resolving) return
        val next = toResolve.removeFirstOrNull() ?: return
        resolving = true
        // resolveService is deprecated from API 34 in favour of
        // registerServiceInfoCallback, which minSdk 29 does not have.
        @Suppress("DEPRECATION")
        nsd.resolveService(next, object : NsdManager.ResolveListener {
            override fun onServiceResolved(info: NsdServiceInfo) {
                post {
                    resolving = false
                    resolved(info)
                    resolveNext()
                }
            }
            override fun onResolveFailed(info: NsdServiceInfo, code: Int) {
                post {
                    resolving = false
                    settle(info.serviceName, PeerStanding.Refused("could not be resolved (NSD error $code)"))
                    resolveNext()
                }
            }
        })
    }

    private fun resolved(info: NsdServiceInfo) {
        val pairing = this.pairing ?: return
        val service = info.serviceName
        val claimed = PeerDiscovery.claimedClientId(info.attributes)
        @Suppress("DEPRECATION") // getHost: see resolveNext.
        val host = info.host
        when {
            // This phone's own advertisement comes back to its own browser.
            claimed == pairing.clientId -> roster.lost(service)
            claimed == null || host == null -> settle(service, PeerStanding.Refused("no client id advertised"))
            else -> check {
                val standing = PeerVerification.verify(claimed, pairing, crypto) { exchange(host, info.port, it) }
                post { settle(service, standing) }
            }
        }
    }

    private fun exchange(host: InetAddress, port: Int, message: ByteArray): ByteArray =
        Socket().use { s ->
            s.connect(InetSocketAddress(host, port), TIMEOUT_MS)
            s.soTimeout = TIMEOUT_MS
            s.getOutputStream().write(message)
            PeerHandshake.read(s.getInputStream())
        }

    private fun settle(service: String, standing: PeerStanding) {
        roster.record(service, standing)
        if (standing is PeerStanding.Refused) Log.w(TAG, "peer refused: ${standing.reason}")
        reconcile()
    }

    // ---- adopting a peer as a leg -----------------------------------------

    /** One announcer per adopted peer that is verified RIGHT NOW, none for the
     *  rest, then publish. Called after every change. */
    private fun reconcile() {
        val desired = roster.verifiedPeers.filter { it.clientId in adopted }.associateBy { it.clientId }
        stopStale(desired)
        val blocker = startMissing(desired)
        emit(
            snapshot.copy(
                verified = roster.verifiedPeers,
                refusedCount = roster.refusedCount,
                adopted = adopted.toSet(),
                blocker = blocker,
            ),
        )
    }

    private fun stopStale(desired: Map<Long, VerifiedPeer>) {
        val stale = running.filter { (id, adoption) -> desired[id] != adoption.peer }
        for ((id, adoption) in stale) {
            running.remove(id)
            adoption.announcer.stop()
        }
    }

    /** Starts what is missing; returns why nothing could start, if anything. */
    private fun startMissing(desired: Map<Long, VerifiedPeer>): String? {
        val pending = desired.filterKeys { it !in running }
        if (pending.isEmpty()) return null
        val config = AndroidManagedConfig.effectiveConfig(context)
        if (config.announceToken.isEmpty() || config.consoleLanHost.isBlank()) {
            return "Set the router write token on the Relay tab to add a device as a leg."
        }
        for ((id, peer) in pending) {
            val announcer = LegAnnouncer(HttpConsolePost(SystemWifiRoute(context)))
            running[id] = Adoption(peer, announcer)
            announcer.start(peer.announcerConfig(config.consoleLanHost, config.announceToken), { peer.host }) { outcome ->
                post { emit(snapshot.copy(outcomes = snapshot.outcomes + (id to outcome))) }
            }
        }
        return null
    }

    private companion object {
        const val TAG = "ZippiePeers"

        /** On the same wifi a reply takes milliseconds; a device silent this
         *  long is refused rather than left "checking" forever. */
        const val TIMEOUT_MS = 4_000

        /** A household has a handful of devices; more simultaneous challenges
         *  than this is something other than a household. */
        const val MAX_ANSWERING = 8

        const val MAX_VERIFYING = 4
    }
}
