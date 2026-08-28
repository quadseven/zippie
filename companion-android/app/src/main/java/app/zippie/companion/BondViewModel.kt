package app.zippie.companion

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * What the status screen renders.
 *
 * THE RULE THIS TYPE EXISTS TO ENFORCE: never state something that has not been
 * measured. Every failure this system has had reads as "connected but carrying
 * nothing" - a leg up on keepalives while delivering zero. So nulls survive all
 * the way to the screen instead of being defaulted on the way through.
 */
data class BondUiState(
    val decision: ModeDecision = ModeDecision(RouterProximity.UNREACHABLE, undetermined = true),
    val legs: List<Leg> = emptyList(),
    val headline: String = "Checking",
    val subhead: String = "Asking the router what the bond looks like.",
    /** Null until the router has answered at least once. */
    val datapath: String? = null,
    /** The router answered with a shape that carried no `paths` key at all -
     *  different from a bond with no links, and worth saying out loud. */
    val pathsMissing: Boolean = false,
    /** Verbatim, because it is usually the only clue: a cleartext-policy refusal
     *  and a router that is switched off produce very different messages and the
     *  same blank screen. */
    val consoleError: String? = null,
    val consoleUrl: String? = null,
    val fetchedAtMs: Long? = null,
    val relay: RelayReport? = null,
    val carrier: CarrierInfo? = null,
    val localIp: String? = null,
    val listenPort: Int = RelayConfiguration().listenPort,
    /** The key this phone announces itself under, so the row in the router's
     *  list can be found by name rather than guessed at. Null until one has been
     *  minted. */
    val legName: String? = null,
    /** Whether a console write token is stored. The token itself is never read
     *  back into the UI: announcing needs it, looking at it does not. */
    val hasAnnounceToken: Boolean = false,
    /**
     * The bond's throughput over the last few minutes, computed from the byte
     * counters in polls this app was already making.
     *
     * SURVIVES A FAILED POLL, unlike [legs]. The rows are dropped when the
     * console stops answering because a stale row claims to be current; a chart
     * is explicitly a record of the past, and dropping it would hide the one
     * thing that outage is evidence of.
     */
    val throughput: BondThroughput.Chart = BondThroughput.Chart.EMPTY,
    val nowMs: Long = System.currentTimeMillis(),
)

class BondViewModel(app: Application) : AndroidViewModel(app) {

    private val _state = MutableStateFlow(BondUiState())
    val state: StateFlow<BondUiState> = _state.asStateFlow()

    /**
     * The rolling window the throughput chart is drawn from.
     *
     * Only ever touched from `refresh`, which runs on viewModelScope's main
     * dispatcher, so the reassignment needs no lock. Held here rather than in
     * BondUiState because the state object is what the screen reads and the raw
     * snapshots are not something any screen should be reading.
     *
     * FOUR POLLS is what counts as a gap. Three consecutive failed reads is
     * already a router that is not answering, and averaging the bytes either
     * side of that into a single bar would claim a shape for a stretch of time
     * nobody observed.
     */
    private var history = ThroughputHistory(maxIntervalMs = CONSOLE_INTERVAL_MS * 4)

    init {
        // The console is polled far more slowly than the local relay report. It
        // is a network round trip to a small router that is also forwarding
        // every packet in the car; a one-second poll would be self-inflicted
        // load for data that changes on the order of seconds.
        viewModelScope.launch {
            while (true) {
                refresh()
                delay(CONSOLE_INTERVAL_MS)
            }
        }
        // The relay reports on its own heartbeat, and the screen must follow it
        // rather than the console poll: it is the one number the phone itself
        // measured.
        viewModelScope.launch {
            RelayStatusStore.report.collect { report ->
                _state.value = _state.value.copy(
                    relay = report,
                    nowMs = System.currentTimeMillis(),
                )
            }
        }
    }

    fun refresh() {
        viewModelScope.launch {
            val context = getApplication<Application>()
            // All three touch disk or the telephony binder, and this runs every
            // few seconds behind a screen that must stay responsive.
            val config = withContext(Dispatchers.IO) { AndroidManagedConfig.effectiveConfig(context) }
            val localIp = withContext(Dispatchers.IO) { LocalAddress.wifiIPv4(context) }
            val carrier = withContext(Dispatchers.IO) { CarrierInfo.read(context) }
            // READ, never minted here. The relay service mints the name when it
            // first announces; showing a name the service has not used would put
            // a leg on this screen that the router has never heard of.
            val legName = withContext(Dispatchers.IO) {
                RelayConfiguration.prefs(context).getString(LegName.KEY, null)
            }
            val (result, proximity) = BondStatusClient.probe(
                config.consoleCandidates,
                wifi = SystemWifiRoute(context),
            )
            val now = System.currentTimeMillis()

            when (result) {
                is ConsoleResult.Ok -> {
                    val status = result.status
                    val rows = BondLegs.rows(status, config.listenPort, localIp)
                    // STAMPED WITH THIS PHONE'S CLOCK, not the router's, because
                    // the interval a rate is computed over is the interval
                    // between two of THESE polls - see BondLegs.snapshot.
                    history = history.plus(BondLegs.snapshot(status, now))
                    _state.value = _state.value.copy(
                        decision = ModeDecision(proximity),
                        legs = rows,
                        headline = BondLegs.headline(rows),
                        subhead = BondLegs.subhead(rows),
                        datapath = status.datapath,
                        pathsMissing = status.paths == null,
                        consoleError = null,
                        consoleUrl = config.consoleCandidates.firstOrNull {
                            it.isLocal == (proximity == RouterProximity.LOCAL)
                        }?.url,
                        fetchedAtMs = now,
                        carrier = carrier,
                        localIp = localIp,
                        listenPort = config.listenPort,
                        legName = legName,
                        hasAnnounceToken = config.announceToken.isNotEmpty(),
                        throughput = history.chart(),
                        nowMs = now,
                    )
                }

                is ConsoleResult.Failed -> {
                    // The last good snapshot is DROPPED rather than left on
                    // screen pretending to be current. A bond that cannot be
                    // observed is not the same as a bond that is fine.
                    //
                    // `throughput` is deliberately NOT in this copy, so the
                    // chart carries over. It is the one thing on the screen that
                    // does not claim to be current, and the outage draws itself
                    // as the break in the bars it really is once the router
                    // answers again.
                    _state.value = _state.value.copy(
                        decision = ModeDecision(proximity),
                        legs = emptyList(),
                        headline = "The router is not answering",
                        subhead = "Nothing on this screen can be trusted to be current.",
                        datapath = null,
                        pathsMissing = false,
                        consoleError = result.message,
                        consoleUrl = config.consoleCandidates.firstOrNull()?.url,
                        fetchedAtMs = _state.value.fetchedAtMs,
                        carrier = carrier,
                        localIp = localIp,
                        listenPort = config.listenPort,
                        legName = legName,
                        hasAnnounceToken = config.announceToken.isNotEmpty(),
                        nowMs = now,
                    )
                }
            }
        }
    }

    /**
     * Store the router's write token, or clear it when the field was left empty.
     *
     * NEVER LOGGED, and normalised on the way in: a token pasted from a terminal
     * carries a trailing newline, and a token pasted from a header carries
     * "Bearer ". Both produce a 401 that reads as "wrong token" and send someone
     * back to the router to copy the same string again.
     *
     * The relay reads the token when it starts announcing, so a token saved
     * while the relay is running takes effect at its next start. Said in the UI
     * rather than silently restarting the service under someone.
     */
    fun saveAnnounceToken(raw: String) {
        viewModelScope.launch {
            val context = getApplication<Application>()
            val token = ConsoleWriteToken.normalise(raw).orEmpty()
            withContext(Dispatchers.IO) {
                RelayConfiguration.saveAnnounceToken(context, token)
            }
            refresh()
        }
    }

    companion object {
        private const val CONSOLE_INTERVAL_MS = 5_000L
    }
}
