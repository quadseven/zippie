package app.zippie.companion

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.ServiceInfo
import android.graphics.drawable.Icon
import android.net.ConnectivityManager
import android.os.UserManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.os.Build
import android.os.Handler
import android.os.HandlerThread
import android.os.IBinder
import android.util.Log
import java.net.DatagramPacket
import kotlinx.coroutines.runBlocking
import java.net.DatagramSocket
import java.net.InetSocketAddress
import java.util.concurrent.atomic.AtomicLong
import kotlin.concurrent.thread

/**
 * CONTRIBUTE mode: lend this phone's cellular to the router's bond.
 *
 *     router --wifi--> [phone] --cellular--> home transport
 *
 * A port of CellularRelay.swift, including the reasoning:
 *
 *  - The phone is a HOP, not an endpoint. It never parses a frame, never
 *    decrypts anything, and has no idea what it carries. Home therefore needs
 *    no changes, the wire format can move without an app release, and nothing
 *    sensitive is readable in this process.
 *  - The router's source port is ephemeral and changes when it restarts, so the
 *    reply target is LEARNED from inbound traffic rather than configured.
 *
 * A FOREGROUND SERVICE, not a background one. Android stops background network
 * access within minutes of the screen going off, and a relay that dies when the
 * phone is pocketed is a leg the bond counts on and does not have. The
 * persistent notification is the price of that exemption, and it is also the
 * only place the person whose phone this is can see what it is doing.
 */
class RelayService : Service() {

    companion object {
        private const val TAG = "ZippieRelay"

        /** How often a repeating packet-loop complaint may re-notify (#186).
         *  Unthrottled, the downstream drops would call publish() thousands of
         *  times a second and bury the signal they exist to report. */
        private const val RENOTE_MS = 30_000L

        /** How often queued telemetry is pushed to Datadog. Frequent enough that
         *  a cold boot is visible while it is happening, rare enough to be a
         *  rounding error on a metered plan. */
        private const val SHIP_INTERVAL_MS = 20_000L

        /** Stops the relay from the notification without opening the app. */
        const val ACTION_STOP = "app.zippie.companion.action.STOP_RELAY"

        private const val CHANNEL_ID = "relay"
        private const val NOTIFICATION_ID = 1

        /** How long the adopted router must be silent before another source may
         *  replace it. Mirrors the iOS relay and the datapath's epoch takeover. */
        private const val ROUTER_TAKEOVER_IDLE_MS = 5_000L

        /** Where the byte counters survive a process kill. Without this the cap
         *  resets every time Android reclaims the app, turning a monthly budget
         *  into a per-launch one. */
        private const val KEY_LEDGER = "budgetLedger"

        private const val MAX_DATAGRAM = 65_535
        private const val LEDGER_SAVE_INTERVAL_MS = 30_000L
    }

    @Volatile private var running = false
    private var config: RelayConfiguration = RelayConfiguration()
    private var lanSocket: DatagramSocket? = null

    /** The wifi network [lanSocket]'s egress is currently pinned to, or null if
     *  it is unpinned. Compared by identity on every callback so a re-pin only
     *  happens when the network actually changed - onCapabilitiesChanged fires
     *  far too often to act on blindly. */
    @Volatile private var pinnedNetwork: Network? = null

    /** Observability, not control: the operator needs to be able to tell a leg
     *  that is unpinned from one that is merely quiet, because they look
     *  identical from the router. */
    @Volatile private var legPinned = false

    private var wifiPinCallback: ConnectivityManager.NetworkCallback? = null
    private var wifiPinThread: HandlerThread? = null

    /** Replaced whenever the radio hands us a new cellular network, so every use
     *  of it must be a fresh read - hence @Volatile rather than a local captured
     *  by the pump threads. */
    @Volatile private var cellSocket: DatagramSocket? = null
    @Volatile private var homeAddress: InetSocketAddress? = null

    /**
     * Who may speak to this relay, and where replies go. The rules and the
     * reasoning live in RouterGuard so they can be proven in a test instead of
     * only on a phone.
     */
    private val guard = RouterGuard(ROUTER_TAKEOVER_IDLE_MS)

    private val upDatagrams = AtomicLong()
    private val upBytes = AtomicLong()
    private val downDatagrams = AtomicLong()
    private val downBytes = AtomicLong()
    private val errors = AtomicLong()

    private val budgetBlocked = AtomicLong()

    // WHY THE DOWNSTREAM DROPS ARE COUNTED SEPARATELY (#186). One counter for
    // "the reply did not go back" would have been enough to SEE the fault and
    // useless for FIXING it: "no peer" means the router has never spoken to this
    // phone, "no socket" means the listener died under us, and "send error"
    // means the LAN itself refused. Three different bugs wearing one symptom.
    private val downDroppedNoPeer = AtomicLong()
    private val downDroppedNoSocket = AtomicLong()
    private val downDroppedSendError = AtomicLong()

    // Telemetry, shipped over CELLULAR so it survives the router being dark -
    // see CellularLogShipper for why that is the entire point.
    @Volatile private var shipper: CellularLogShipper? = null
    private var lastNoteAtMs = 0L
    private var lastNoteText: String? = null

    @Volatile private var lastError: String? = null
    @Volatile private var budgetExhausted: String? = null
    @Volatile private var cellularReady = false
    @Volatile private var carrier: CarrierInfo? = null

    /** When something last ARRIVED from the router. Recorded in pumpUp,
     *  BEFORE the forwarding decision - see the note there. The only evidence
     *  RelayStats carries that the far end exists (RelayVerdict.kt,
     *  quadseven/zippie#44). */
    @Volatile private var lastRouterInboundAtMs: Long? = null

    /**
     * Telling the router this phone exists. Without it the relay comes up,
     * carries perfectly and is invisible to the bond - which is what the deleted
     * static `companion-*` entries used to paper over, at the cost of claiming
     * the phone's endpoint and refusing the announced leg the bridge.
     */
    // PINNED TO WIFI. This announcer is what puts the phone in the bond, and on
    // a cold boot the router's wifi is unvalidated so the default network is
    // cellular (#168). SystemWifiRoute needs a Context, which a Service is.
    private val announcer = LegAnnouncer(HttpConsolePost(SystemWifiRoute(this)))

    /** The router's last word on this phone's announcement, in the words the
     *  router used. Null until the first attempt returns. */
    @Volatile private var announceState: String? = null

    /** The name this phone announces itself under, once resolved. Kept so a
     *  label change can be re-announced under the SAME key - a different name
     *  would be a second leg, not an update. */
    @Volatile private var legName: String? = null

    private lateinit var ledger: BudgetLedger

    private var connectivity: ConnectivityManager? = null
    private var networkCallback: ConnectivityManager.NetworkCallback? = null

    /**
     * The NetworkCallback runs on the MAIN thread unless it is given a handler,
     * and everything it does here - opening a socket, resolving the home host -
     * is exactly what NetworkOnMainThreadException exists to punish. So the
     * callback gets its own thread.
     */
    private var callbackThread: HandlerThread? = null

    private var notificationText: String? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // Before anything that can fail or return early. A service started as a
        // foreground service and stopped before startForeground crashes the app
        // with ForegroundServiceDidNotStartInTimeException, which would turn
        // every configuration mistake below into a crash report.
        ensureChannel()
        goForeground(notificationText ?: getString(R.string.relay_starting))

        if (intent?.action == ACTION_STOP) {
            stopSelf()
            return START_NOT_STICKY
        }
        if (running) return START_STICKY

        // Policy applied here, not just in the UI: this is the path that relays.
        // Reading stored config directly would make an MDM-pushed token configure
        // a screen and nothing else.
        // READING CONFIG MUST NOT KILL THE SERVICE. Before first unlock this threw
        // out of RelayConfiguration.prefs and took the process with it, one
        // second after BootReceiver had correctly started the relay:
        //
        //   E AndroidRuntime: Unable to start service RelayService
        //   I ActivityManager: Process app.zippie.companion has died
        //   W ActivityManager: Scheduling restart of crashed service in 1800000ms
        //
        // Android's own 30-MINUTE backoff is what made it look permanent rather
        // than transient. RelayConfiguration now reads device-protected storage
        // while locked, so this should not throw - but a crash here costs half an
        // hour of downtime on a phone nobody is holding, so it is caught anyway.
        val effective = runCatching { AndroidManagedConfig.effectiveConfig(this) }
            .onFailure { fail("could not read configuration: ${it.message}") }
            .getOrNull()
        if (effective == null) { stopSelf(); return START_NOT_STICKY }
        Log.i(TAG, "onStartCommand: unlocked=" +
            getSystemService(UserManager::class.java)?.isUserUnlocked +
            " console=" + effective.consoleLanHost.ifEmpty { "(none)" } +
            " home=" + effective.homeHost.ifEmpty { "(none)" } +
            " token=" + (if (effective.announceToken.isBlank()) "ABSENT" else "present"))
        // A phone provisioned by MDM before its first launch has the key set
        // already, so the role is applied on the way up rather than waiting for
        // a restrictions-changed broadcast that has already been and gone.
        HomeScreenRole.apply(this, AndroidManagedConfig.read(this))
        BootLog.record(this, "relay",
            "service started: unlocked=" +
                getSystemService(UserManager::class.java)?.isUserUnlocked +
                " console=" + effective.consoleLanHost.ifEmpty { "(none)" } +
                " token=" + (if (effective.announceToken.isBlank()) "ABSENT" else "present"))
        val validated = effective.validated().getOrElse {
            // Refuse rather than fall back to a shipped default. A relay pointed
            // at the wrong endpoint comes up green and forwards the router's
            // frames into a hole.
            fail("refusing to start: ${it.message}")
            stopSelf()
            return START_NOT_STICKY
        }
        config = validated
        ledger = BudgetLedgerCodec.decode(
            RelayConfiguration.prefs(this).getString(KEY_LEDGER, null),
            validated.budget,
        ) ?: BudgetLedger(validated.budget)
        carrier = CarrierInfo.read(this)

        // BUILT BEFORE ANY PUMP STARTS, so the earliest decisions of a cold boot
        // are already queued when cellular finally binds and the first flush
        // goes out. Built even when the token is empty: the shipper no-ops, and
        // a null-check at every call site would be one more thing to forget.
        shipper = CellularLogShipper(
            clientToken = validated.ddClientToken,
            site = validated.ddSite,
            tags = "leg:" + (legName ?: "unresolved") + ",device:" + Build.MODEL,
        )
        ship(
            "relay starting",
            fields = mapOf(
                "unlocked" to getSystemService(UserManager::class.java)?.isUserUnlocked,
                "console" to validated.consoleLanHost.ifEmpty { "(none)" },
                "home" to validated.homeHost.ifEmpty { "(none)" },
                "announce_token" to (validated.announceToken.isNotBlank()),
                "telemetry" to (validated.ddClientToken.isNotBlank()),
            ),
        )

        running = true
        // REGISTERED AT RUNTIME, not in the manifest.
        // ACTION_APPLICATION_RESTRICTIONS_CHANGED is delivered only to
        // runtime-registered receivers; a manifest entry never fires and fails
        // silently, which looks exactly like the MDM being ignored.
        registerRestrictionsReceiver()
        startListener()
        requestCellular()
        startAnnouncing()
        startHeartbeat()
        return START_STICKY
    }

    // ---- announcing ------------------------------------------------------

    /**
     * AFTER startListener, deliberately. An announcement tells the router to
     * dial this phone at a port; announcing before the socket is bound points it
     * at a port nothing answers on, and the leg's first probe fails for a reason
     * that has nothing to do with the radio.
     *
     * Started even while cellular is still coming up: a leg that is present and
     * failing its probe is a state the router draws honestly, and it is a much
     * better one than a phone that is relaying and cannot be seen at all.
     */
    private fun startAnnouncing() {
        if (lanSocket == null) {
            // NEVER ANNOUNCE AN ENDPOINT WE DO NOT HOLD. startListener failed to
            // bind - the port is taken, or another copy of this app is running -
            // and the router would dial a port nothing answers on, then report
            // the leg as down for a reason that has nothing to do with the
            // radio. The relay is stopping anyway; it must not leave a leg.
            announceState = "Not announcing: nothing is listening on ${config.listenPort}."
            publish()
            return
        }
        // stablePrefs, not prefs: the leg NAME is identity and must be the same
        // string whether or not the phone is unlocked. Reading it from
        // lock-state-dependent storage renamed the phone on a locked boot.
        val name = LegName.resolve(RelayConfiguration.stablePrefs(this), Build.MODEL)
        legName = name
        val announceConfig = config.announceConfig(name, LegLabel.forDevice(Build.MODEL, carrier))
        if (announceConfig == null) {
            // NOT A FAILURE. Without a console write token this phone cannot
            // announce, and it can still relay. Said plainly on the screen
            // because the symptom otherwise is a working relay that never
            // appears in the bond, which reads as a broken relay.
            announceState = "Not announcing: no console write token set, so the " +
                "router will not see this phone as a leg."
            publish()
            return
        }
        announcer.start(
            announceConfig,
            // Re-read every pass rather than captured: an announcement is also a
            // RENEWAL of the address, and a phone that moved on DHCP must not
            // leave the router dialling the old endpoint.
            address = { LocalAddress.wifiIPv4(this) },
        ) { outcome -> onAnnounced(name, outcome) }
    }

    /** Re-announce under the same name with the carrier the radio now reports.
     *  A no-op when nothing is being announced. */
    private fun relabelAnnouncement() {
        val name = legName ?: return
        val updated = config.announceConfig(name, LegLabel.forDevice(Build.MODEL, carrier)) ?: return
        announcer.reconfigure(updated)
    }

    private fun onAnnounced(name: String, outcome: LegAnnouncer.Outcome) {
        val text = when (outcome) {
            is LegAnnouncer.Outcome.Announced ->
                "Announced as $name, lease ${outcome.leaseRemainingS.toInt()}s."
            // The router's own words. It names the field it refused, and a
            // friendlier sentence would lose the only actionable part.
            is LegAnnouncer.Outcome.Refused ->
                "The router refused this phone's announcement: ${outcome.reason}"
            is LegAnnouncer.Outcome.Unreachable ->
                "Could not reach the router console to announce: ${outcome.reason}"
        }
        if (text == announceState) return
        announceState = text
        // Logged without the token, which lives only in the Config and is
        // redacted even in its toString.
        Log.i(TAG, text)
        publish()
    }

    // ---- wifi side -------------------------------------------------------

    /**
     * Bound before cellular is available on purpose. It makes the difference
     * between "the router cannot reach this phone" and "the router reaches it
     * but the phone has no radio" observable, and those have different fixes.
     */
    private fun startListener() {
        try {
            lanSocket = DatagramSocket(config.listenPort)
        } catch (e: Exception) {
            fail("cannot listen on ${config.listenPort}: ${e.message}")
            stopSelf()
            return
        }
        // A NEW socket carries no pin, whatever the previous one had.
        pinnedNetwork = null
        legPinned = false
        watchWifiForLegPin()
        thread(name = "zippie-relay-up") { pumpUp() }
        thread(name = "zippie-relay-down") { pumpDown() }
        publish()
    }

    /**
     * PIN THE LEG SOCKET'S REPLIES TO WIFI.
     *
     * Holding an address on the router's subnet is not enough to answer the
     * router. While the router's own uplink is down its wifi fails Android's
     * validation, so that network's routes are NOT installed in the default
     * table and CELLULAR is the default network. An unpinned socket then answers
     * a datagram that arrived on wlan0 by sending out the modem, from a source
     * address the router has never heard of. Measured on two Pixels 2026-08-23,
     * each holding an address on the router's own subnet on wlan0:
     *
     *     ip route get <the router's LAN address>  ->  dev v4-rmnet1  src 192.0.0.4
     *
     * That `192.0.0.4` is the RFC 7335 clat address - the same address, and the
     * same cause, as the console failure quoted in WifiRoute's header.
     *
     * From the router it is indistinguishable from a dead phone: it dials the
     * announced endpoint, the phone receives the packet and answers it, nothing
     * comes back, and the leg reads `never_handshaked` with rx 0 while the
     * phone's own counters show it forwarding.
     *
     * NOT the same operation as WifiRoute, which pins an OUTBOUND request it
     * creates. This pins the EGRESS of a socket that must keep receiving on the
     * wildcard: bindSocket sets the socket's routing mark and leaves inbound
     * delivery alone, which is what a listener needs.
     */
    private fun watchWifiForLegPin() {
        val cm = getSystemService(ConnectivityManager::class.java)
        if (cm == null) {
            note("no ConnectivityManager; the leg socket cannot be pinned to wifi")
            return
        }
        connectivity = cm
        // registerNetworkCallback, NOT requestNetwork: this only OBSERVES a wifi
        // the phone has already joined. requestNetwork asks the system to bring
        // one up and needs CHANGE_NETWORK_STATE; observation needs neither, and
        // a relay must never be the reason a phone turns its radio on.
        //
        // NO capability filter, deliberately. Requiring NET_CAPABILITY_VALIDATED
        // or NET_CAPABILITY_INTERNET would match only a wifi that already has an
        // uplink - which is precisely the uplink this relay exists to create.
        // That is the deadlock WifiRoute's header describes, and it would be
        // reinstated here by one extra line.
        val request = NetworkRequest.Builder()
            .addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
            .build()
        val handlerThread = HandlerThread("zippie-relay-legpin").apply { start() }
        wifiPinThread = handlerThread
        val callback = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) = pinLegSocket(network)

            // Re-pin rather than assume the pin holds. A roam or a reconnect
            // hands out a NEW Network object, and a pin to the old one fails as
            // silently as no pin at all - which is the failure mode this whole
            // method exists to end.
            override fun onCapabilitiesChanged(
                network: Network,
                caps: NetworkCapabilities,
            ) = pinLegSocket(network)

            override fun onLost(network: Network) {
                if (network == pinnedNetwork) {
                    pinnedNetwork = null
                    legPinned = false
                    publish()
                }
            }
        }
        wifiPinCallback = callback
        try {
            // Fires onAvailable for a wifi that is ALREADY joined, so no separate
            // first-time lookup is needed.
            cm.registerNetworkCallback(request, callback, Handler(handlerThread.looper))
        } catch (e: SecurityException) {
            wifiPinCallback = null
            note("cannot observe wifi to pin the leg socket: ${e.message}")
        }
    }

    private fun pinLegSocket(network: Network) {
        if (network == pinnedNetwork) return
        val socket = lanSocket ?: return
        try {
            network.bindSocket(socket)
            pinnedNetwork = network
            legPinned = true
            BootLog.record(this, "leg", "leg socket pinned to wifi; replies leave on wlan")
        } catch (e: Exception) {
            // NOT fatal. On a phone whose wifi IS the default network an unpinned
            // socket already replies correctly, and that is the configuration
            // every leg that has ever carried was running in. Degrading loudly
            // beats refusing to relay.
            pinnedNetwork = null
            legPinned = false
            note("could not pin the leg socket to wifi: ${e.message}")
        }
        publish()
    }

    // ---- cellular side ---------------------------------------------------

    /**
     * THE LINE THE WHOLE DESIGN RESTS ON.
     *
     * requestNetwork with TRANSPORT_CELLULAR and then Network.bindSocket is
     * Android's equivalent of iOS's requiredInterfaceType = .cellular. Without
     * it the forward leaves by whichever interface wins the routing table -
     * which on a phone joined to the router's wifi is that same wifi, making
     * the "second path" a loop back into the first.
     *
     * requestNetwork also needs CHANGE_NETWORK_STATE; without that permission it
     * throws SecurityException at runtime and the relay never carries.
     */
    private fun requestCellular() {
        val cm = getSystemService(ConnectivityManager::class.java)
        if (cm == null) {
            fail("no ConnectivityManager; cannot pin to cellular")
            stopSelf()
            return
        }
        connectivity = cm
        val request = NetworkRequest.Builder()
            .addTransportType(NetworkCapabilities.TRANSPORT_CELLULAR)
            .addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
            .build()

        val handlerThread = HandlerThread("zippie-relay-net").apply { start() }
        callbackThread = handlerThread

        val callback = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                bindCellular(network)
            }

            override fun onLost(network: Network) {
                // Aeroplane mode, no data plan, or the radio dropped. Say so
                // rather than sitting in a state that looks like "starting".
                closeCellular()
                cellularReady = false
                lastError = "cellular lost; the relay is not carrying"
                BootLog.record(this@RelayService, "cellular",
                    "LOST: the relay is no longer carrying")
                publish()
            }
        }
        networkCallback = callback
        try {
            cm.requestNetwork(request, callback, Handler(handlerThread.looper))
        } catch (e: SecurityException) {
            fail("cannot request cellular: ${e.message}")
            stopSelf()
        }
    }

    private fun bindCellular(network: Network) {
        try {
            val socket = DatagramSocket()
            network.bindSocket(socket)
            // Resolved ON the cellular network rather than with the default
            // resolver. A captive portal on the wifi this phone is joined to
            // answers every DNS query with its own address, and a relay that
            // believed it would spray the bond's frames at a landing page.
            val resolved = network.getByName(config.homeHost)
                ?: throw java.net.UnknownHostException(config.homeHost)
            closeCellular()
            homeAddress = InetSocketAddress(resolved, config.homePort)
            cellSocket = socket
            cellularReady = true
            lastError = null
            // THE TRANSITION, not the per-packet drop. `dropped upstream:
            // cellular not ready` sits inside the pump loop, and recording
            // there would write thousands of lines a second to flash. This is
            // the moment the answer changes, which is the only moment worth a
            // line: it is the timestamp that says how long after boot the
            // phone actually became able to carry.
            BootLog.record(this, "cellular",
                "bound: home=${config.homeHost} resolved on the cellular network")
            // Telemetry rides the SAME handle as the bond. This is the line that
            // makes the phone visible while the router is dark (#186).
            shipper?.useNetwork(network)
            ship(
                "cellular bound; relay can reach home",
                fields = mapOf("home" to config.homeHost, "home_port" to config.homePort),
            )
            carrier = CarrierInfo.read(this)
            // The radio has only now said which network it registered on. A
            // phone that started the relay in a tunnel would otherwise keep
            // announcing a label with no carrier in it, which is the very thing
            // an Android leg exists to stop doing.
            relabelAnnouncement()
        } catch (e: Exception) {
            cellularReady = false
            lastError = "cellular bind failed: ${e.message}"
            errors.incrementAndGet()
            Log.w(TAG, "cellular bind failed", e)
            // Queued even though cellular is NOT bound: the shipper holds lines
            // until it has a network, so the report of the failure survives and
            // goes out the moment one arrives. A shipper that could only report
            // successes would be worthless here.
            shipper?.useNetwork(network)
            ship("cellular bind FAILED", "error", mapOf("cause" to e.message))
        }
        publish()
    }

    private fun closeCellular() {
        val old = cellSocket
        cellSocket = null
        // Closing is what unblocks a pump parked in receive() on it.
        try {
            old?.close()
        } catch (e: Exception) {
            Log.w(TAG, "closing cellular socket: ${e.message}")
        }
    }

    // ---- forwarding ------------------------------------------------------

    private fun pumpUp() {
        val buffer = ByteArray(MAX_DATAGRAM)
        while (running) {
            val lan = lanSocket ?: break
            val packet = DatagramPacket(buffer, buffer.size)
            try {
                lan.receive(packet)
            } catch (e: Exception) {
                if (running) note("upstream receive: ${e.message}")
                continue
            }
            if (!guard.accept(packet.address, packet.port)) continue

            // THE ONE PLACE INBOUND EVIDENCE IS RECORDED, and BEFORE the
            // forwarding decision below - so a router dialling a phone whose
            // cellular has died still shows up as a router that is dialling
            // (ZippieCompanionKit/CellularRelay.swift, quadseven/zippie#44).
            // guard.accept() has already proven the source is the router;
            // everything from here on is about whether THIS PHONE can get the
            // bytes out, which is a separate question from whether the router
            // is there.
            lastRouterInboundAtMs = System.currentTimeMillis()

            // THE CAP IS ENFORCED, NOT DISPLAYED. A budget that only warns is
            // what produces a surprise bill, and this phone is the one place in
            // the system where the data is metered and someone else pays for it.
            if (!withinBudget()) {
                budgetBlocked.incrementAndGet()
                continue
            }
            val cell = cellSocket
            val home = homeAddress
            if (cell == null || home == null) {
                // Dropped rather than queued. A relay that buffers while its
                // uplink is down delivers a burst of stale packets when it
                // recovers, which is worse for a reordering transport than loss.
                note("dropped upstream: cellular not ready")
                continue
            }
            try {
                cell.send(DatagramPacket(buffer, packet.length, home))
                upDatagrams.incrementAndGet()
                upBytes.addAndGet(packet.length.toLong())
                // BOTH directions count against the cap. The carrier bills for
                // the download too, and on a phone that is the larger half.
                ledger.record(packet.length.toLong())
            } catch (e: Exception) {
                note("up: ${e.message}")
            }
        }
    }

    private fun pumpDown() {
        val buffer = ByteArray(MAX_DATAGRAM)
        while (running) {
            val cell = cellSocket
            if (cell == null) {
                // Only reached while cellular is absent, which is also the only
                // time there is no socket to block on.
                try {
                    Thread.sleep(200)
                } catch (e: InterruptedException) {
                    return
                }
                continue
            }
            val packet = DatagramPacket(buffer, buffer.size)
            try {
                cell.receive(packet)
            } catch (e: Exception) {
                // Expected when the socket is swapped out under us; the next
                // iteration picks up the replacement.
                if (running && cellSocket === cell) note("downstream receive: ${e.message}")
                continue
            }
            // THE TWO SILENT DROPS (#186).
            //
            // These were bare `?: continue`. No counter, no note, nothing - so a
            // relay in either state forwarded UPSTREAM perfectly, reported
            // `Carrying` honestly (that verdict only requires inbound-from-router
            // plus upDatagrams > 0), and dropped every reply on the floor.
            //
            // Measured 2026-08-17: home RECEIVED the router's keepalives via this
            // phone and SENT replies, while the router's own packet counters read
            // `replies_in=0` for the whole outage. Everything upstream was working
            // and the return path evaporated here, invisibly. Naming which of the
            // two it was is the difference between a diagnosis and a guess.
            val peer = guard.peer
            if (peer == null) {
                downDroppedNoPeer.incrementAndGet()
                noteRateLimited("down dropped: no router peer learned yet")
                continue
            }
            val lan = lanSocket
            if (lan == null) {
                downDroppedNoSocket.incrementAndGet()
                noteRateLimited("down dropped: LAN socket is gone")
                continue
            }
            try {
                lan.send(DatagramPacket(buffer, packet.length, peer.address, peer.port))
                downDatagrams.incrementAndGet()
                downBytes.addAndGet(packet.length.toLong())
                ledger.record(packet.length.toLong())
            } catch (e: Exception) {
                downDroppedSendError.incrementAndGet()
                note("down: ${e.message}")
            }
        }
    }

    /** True while the relay may still spend cellular. Checked on the way UP, the
     *  only direction we can actually refuse: once home has sent something down,
     *  the bytes are already paid for. */
    private fun withinBudget(): Boolean {
        val verdict = ledger.verdict()
        if (verdict.isAllowed) {
            if (budgetExhausted != null) {
                // A rollover re-opened the tap; say so rather than leaving the
                // old message describing a state that has ended.
                budgetExhausted = null
                publish()
            }
            return true
        }
        if (budgetExhausted != verdict.reason) {
            budgetExhausted = verdict.reason
            publish()
        }
        return false
    }

    // ---- reporting -------------------------------------------------------

    /**
     * Rewrites the report on a fixed interval even when nothing changed, so a
     * dead pump reads as "not reporting" rather than leaving its last counters
     * on screen looking current. It is also where the byte counters are
     * persisted, because a process kill gives no warning.
     */
    private fun startHeartbeat() {
        thread(name = "zippie-relay-heartbeat") {
            var sinceSaveMs = 0L
            var sinceShipMs = 0L
            while (running) {
                publish()
                sinceSaveMs += RelayReport.HEARTBEAT_MS
                sinceShipMs += RelayReport.HEARTBEAT_MS
                if (sinceSaveMs >= LEDGER_SAVE_INTERVAL_MS) {
                    saveLedger()
                    sinceSaveMs = 0
                }
                // Flushed from THIS thread, never the packet loops: a POST that
                // blocks for its full timeout on a dying uplink must not be able
                // to stall the bond it is reporting on.
                if (sinceShipMs >= SHIP_INTERVAL_MS) {
                    // THE ROUTER'S OWN VERDICT, CARRIED OUT OVER CELLULAR. This
                    // is the only path by which an islanded router can be heard
                    // from at all - see IslandReport. Computed on THIS thread,
                    // which is already the one allowed to block on the network.
                    ship("relay heartbeat", fields = mapOf("router_view" to routerView().wireValue))
                    shipper?.flush()
                    sinceShipMs = 0
                }
                try {
                    Thread.sleep(RelayReport.HEARTBEAT_MS)
                } catch (e: InterruptedException) {
                    return@thread
                }
            }
        }
    }

    private fun note(message: String) {
        errors.incrementAndGet()
        lastError = message
        Log.w(TAG, message)
        ship(message, "warn")
        publish()
    }

    /**
     * note() for something that can happen on EVERY datagram.
     *
     * The downstream drops live in the packet loop, so an unconditional note()
     * there would call publish() thousands of times a second and bury the very
     * signal it is reporting. This keeps the first occurrence and then at most
     * one per RENOTE_MS, which is what makes "it started at 03:47 and never
     * stopped" readable.
     */
    @Synchronized
    private fun noteRateLimited(message: String) {
        val now = System.currentTimeMillis()
        if (message == lastNoteText && now - lastNoteAtMs < RENOTE_MS) {
            errors.incrementAndGet()
            lastError = message
            return
        }
        lastNoteText = message
        lastNoteAtMs = now
        note(message)
    }

    /** Best-effort telemetry. Never throws, never blocks the caller. */
    /**
     * Ask the router, over WIFI ONLY, what it thinks of itself.
     *
     * LOCAL CANDIDATES ONLY, and that is the whole trick. The console is also
     * reachable over the tailnet, and a tailnet read would succeed only when the
     * router already has internet - which is precisely the thing in question. It
     * would make this signal silent at exactly the moment it is needed, which is
     * the same construction failure that makes the hub observer blind to
     * islanding (zippie#286).
     *
     * A failure here is NOT an error. Being away from the router is the normal
     * state of a phone, and IslandReport turns that into NOT_NEAR_ROUTER rather
     * than an alarm.
     */
    private fun routerView(): IslandState {
        val listening = lanSocket != null
        val local = config.consoleCandidates.filter { it.isLocal }
        if (local.isEmpty()) return IslandReport.evaluate(listening, false, null)
        val status = runCatching {
            runBlocking {
                BondStatusClient.probe(local, wifi = SystemWifiRoute(this@RelayService)).first
            }
        }.getOrNull() as? ConsoleResult.Ok
        return IslandReport.evaluate(
            relayListening = listening,
            consoleAnsweredOnLan = status != null,
            carryingLegs = status?.status?.carryingCount,
        )
    }

    private fun ship(message: String, status: String = "info", fields: Map<String, Any?> = emptyMap()) {
        val s = shipper ?: return
        s.event(
            message,
            status,
            fields + mapOf(
                "leg" to legName,
                "cellular_ready" to cellularReady,
                "up_datagrams" to upDatagrams.get(),
                "down_datagrams" to downDatagrams.get(),
                "down_dropped_no_peer" to downDroppedNoPeer.get(),
                "down_dropped_no_socket" to downDroppedNoSocket.get(),
                "down_dropped_send_error" to downDroppedSendError.get(),
                "budget_blocked" to budgetBlocked.get(),
            ),
        )
    }

    private fun fail(message: String) {
        lastError = message
        Log.e(TAG, message)
        publish()
    }

    /**
     * The one Android call that answers "will this process keep being
     * scheduled". Isolated here because it throws "Stub!" against the stub
     * android.jar the unit tests run on - the DECISION lives in
     * [BatteryExemption], which takes this Boolean and is provable.
     *
     * Defaults TRUE on failure, deliberately. A false reading would put a
     * warning on the screen of a phone that is fine, and a warning that cries
     * wolf is worse than none - the router's own view is the backstop.
     */
    private fun isIgnoringBatteryOptimizations(): Boolean = runCatching {
        val pm = getSystemService(android.os.PowerManager::class.java)
        pm?.isIgnoringBatteryOptimizations(packageName) ?: true
    }.getOrDefault(true)

    private fun snapshot(): RelayStats {
        val ledgerReady = ::ledger.isInitialized
        return RelayStats(
            listening = lanSocket != null,
            cellularReady = cellularReady,
            upDatagrams = upDatagrams.get(),
            upBytes = upBytes.get(),
            downDatagrams = downDatagrams.get(),
            downBytes = downBytes.get(),
            errors = errors.get(),
            rejectedSources = guard.rejected,
            budgetBlocked = budgetBlocked.get(),
            downDroppedNoPeer = downDroppedNoPeer.get(),
            downDroppedNoSocket = downDroppedNoSocket.get(),
            downDroppedSendError = downDroppedSendError.get(),
            budgetExhausted = budgetExhausted,
            lastError = lastError,
            dayUsedBytes = if (ledgerReady) ledger.dayUsed else 0,
            monthUsedBytes = if (ledgerReady) ledger.monthUsed else 0,
            budget = if (ledgerReady) ledger.budget else DataBudget.unlimited,
            carrier = carrier,
            announce = announceState,
            lastRouterInboundAtMs = lastRouterInboundAtMs,
            ignoringBatteryOptimizations = isIgnoringBatteryOptimizations(),
        )
    }

    private fun publish() {
        val stats = snapshot()
        RelayStatusStore.publish(this, stats)
        val text = stats.summary
        // Only when the words change. Rewriting an identical notification a few
        // times a second is work the system does not need.
        if (text != notificationText) {
            notificationText = text
            notificationManager()?.notify(NOTIFICATION_ID, buildNotification(text))
        }
    }

    private fun saveLedger() {
        if (!::ledger.isInitialized) return
        RelayConfiguration.prefs(this).edit()
            .putString(KEY_LEDGER, BudgetLedgerCodec.encode(ledger))
            .apply()
    }

    // ---- foreground plumbing --------------------------------------------

    private fun notificationManager(): NotificationManager? =
        getSystemService(NotificationManager::class.java)

    private fun ensureChannel() {
        val channel = NotificationChannel(
            CHANNEL_ID,
            getString(R.string.relay_channel_name),
            // LOW: ongoing and worth being able to see, never worth a sound at
            // 70mph.
            NotificationManager.IMPORTANCE_LOW,
        ).apply { description = getString(R.string.relay_channel_description) }
        notificationManager()?.createNotificationChannel(channel)
    }

    private fun goForeground(text: String) {
        // The type is passed here as well as declared in the manifest: from
        // Android 14 the system checks the value given to startForeground, and
        // connectedDevice additionally requires a permission from its
        // prerequisite list - CHANGE_NETWORK_STATE, which the relay needs anyway
        // to request the cellular network.
        startForeground(
            NOTIFICATION_ID,
            buildNotification(text),
            ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE,
        )
    }

    private fun buildNotification(text: String): Notification {
        val open = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE,
        )
        val stop = PendingIntent.getForegroundService(
            this,
            1,
            Intent(this, RelayService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE,
        )
        return Notification.Builder(this, CHANNEL_ID)
            .setContentTitle(getString(R.string.relay_notification_title))
            .setContentText(text)
            // A platform glyph: this app has no icon assets yet, and a blank
            // square in the status bar would be worse than a borrowed arrow.
            .setSmallIcon(android.R.drawable.stat_sys_upload)
            .setOngoing(true)
            .setContentIntent(open)
            .addAction(
                Notification.Action.Builder(
                    Icon.createWithResource(this, android.R.drawable.ic_menu_close_clear_cancel),
                    getString(R.string.relay_stop),
                    stop,
                ).build(),
            )
            .build()
    }

    override fun onDestroy() {
        running = false
        // FIRST, so the goodbye goes out while the process is still alive. The
        // withdraw itself happens on the announce thread - this call only signals
        // it - because onDestroy runs on the main thread, where any network call
        // is NetworkOnMainThreadException. Without the goodbye the leg would
        // linger for up to its 45s lease, and the router would keep dialling a
        // phone that has stopped relaying.
        announcer.stop()
        saveLedger()
        networkCallback?.let { callback ->
            try {
                connectivity?.unregisterNetworkCallback(callback)
            } catch (e: IllegalArgumentException) {
                Log.w(TAG, "network callback was not registered")
            }
        }
        networkCallback = null
        callbackThread?.quitSafely()
        callbackThread = null
        wifiPinCallback?.let { callback ->
            try {
                connectivity?.unregisterNetworkCallback(callback)
            } catch (e: IllegalArgumentException) {
                Log.w(TAG, "wifi pin callback was not registered")
            }
        }
        wifiPinCallback = null
        wifiPinThread?.quitSafely()
        wifiPinThread = null
        pinnedNetwork = null
        legPinned = false
        lanSocket?.close()
        lanSocket = null
        closeCellular()
        cellularReady = false
        // Cleared rather than zeroed: absent means "nothing is running", which
        // is the truth after a stop, whereas a zeroed report reads as "running
        // and carrying nothing". Mirrors to disk too (RelayReportFile.clear),
        // or a widget reading the persisted record would show this leg's
        // corpse forever.
        RelayStatusStore.clear(this)
        unregisterRestrictionsReceiver()
        super.onDestroy()
    }

    // ---------------------------------------------------------------- policy

    private var restrictionsReceiver: RestrictionsChangedReceiver? = null

    /**
     * Pick up an MDM configuration change without a restart.
     *
     * Reading policy once at startup is enough for an app somebody opens. This
     * one is meant never to be opened - a phone gets enrolled, handed a token,
     * and left alone - so a pushed token that waits for the next process start
     * may wait forever.
     *
     * Only the CONFIGURATION is refreshed. The relay is not started or
     * restarted here: a restriction edit must not be able to turn on cellular
     * relaying with no other signal, and tearing down a carrying leg to apply a
     * changed comment field would be worse than applying it late.
     */
    private fun registerRestrictionsReceiver() {
        if (restrictionsReceiver != null) return
        val r = RestrictionsChangedReceiver { updated ->
            config = updated
            startAnnouncing()
        }
        restrictionsReceiver = r
        registerReceiver(r, IntentFilter(Intent.ACTION_APPLICATION_RESTRICTIONS_CHANGED))
    }

    private fun unregisterRestrictionsReceiver() {
        restrictionsReceiver?.let {
            // Swallowed deliberately: unregistering one that was never
            // registered throws, and a crash during teardown would lose the
            // real stop reason.
            runCatching { unregisterReceiver(it) }
        }
        restrictionsReceiver = null
    }

}
