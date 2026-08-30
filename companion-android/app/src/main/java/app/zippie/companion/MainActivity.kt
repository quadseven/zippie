package app.zippie.companion

import android.Manifest
import android.app.Activity
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.VpnService
import android.os.Build
import android.os.Bundle
import android.util.Log
import androidx.activity.ComponentActivity
import androidx.activity.enableEdgeToEdge
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawing
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.filled.CheckCircle
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.NavigationBarItemDefaults
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import app.zippie.companion.design.Ink
import androidx.compose.ui.Modifier
import androidx.compose.material3.Surface
import androidx.compose.runtime.getValue
import androidx.core.content.ContextCompat
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch

/**
 * The two destinations on the shell's bottom bar, matching the Status/Relay
 * pairing ZippieCompanionApp.swift's own TabView uses.
 *
 * ONLY TWO, NOT THREE. iOS also has a Probe tab - a cellular-pin egress check
 * that opens a socket bound to the cellular network and compares its public
 * address against the default route's. Android has no equivalent capability
 * today; building one is new networking code, not a UI rearrangement, so it is
 * left for its own change rather than stubbed here as a tab that does nothing.
 *
 * Diagnostics is not a third destination here any more than it is a third TAB
 * on iOS - both reach it from an icon on Status, pushed over the bar rather
 * than beside it.
 */
private enum class Destination { STATUS, RELAY }

/**
 * The shell.
 *
 * The UI's one real job, per ADR 0022, is to make plain WHICH WAY TRAFFIC IS
 * FLOWING - contributing this phone's cellular to the router, or bonding this
 * phone's own links back home. Two modes in one app is a trap otherwise.
 *
 * The mode shown is decided by evidence (see RouterProximity), not by these
 * buttons. The buttons exist because the automatic start is not written yet and
 * because someone must always be able to stop lending their data.
 */
class MainActivity : ComponentActivity() {

    private companion object { const val TAG = "ZippieMain" }

    /**
     * Asked BEFORE the relay starts, not after. Without it the foreground
     * service still runs - Android 13 does not require the permission to hold
     * the exemption - but its notification is hidden, and the only visible sign
     * that a phone is spending cellular for someone else disappears along with
     * the only way to stop it from the shade.
     */
    private val notificationPermission =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) {
            startRelayService()
        }

    /** VpnService requires one-time consent before establish() does anything. */
    private val vpnConsent =
        registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
            if (result.resultCode == Activity.RESULT_OK) {
                startService(Intent(this, ZippieVpnService::class.java))
            }
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // EDGE TO EDGE IS NOT OPTIONAL AT targetSdk 35+, and at 36 the opt-out
        // attribute is deprecated AND ignored, so this is declared rather than
        // inherited. Without the inset padding below, the top of the densest
        // screen in the app draws underneath the status bar.
        enableEdgeToEdge()
        refreshBootConfig()
        setContent {
            MaterialTheme {
                // safeDrawing, not statusBars: it also covers the navigation bar
                // and the display cutout, and this phone has a gesture bar at the
                // bottom that would otherwise sit on top of the relay controls.
                // Applied ONCE here rather than per screen, so a new screen
                // cannot forget it.
                // `ground` is the token literally described as "the page". Without
                // naming it the Surface falls back to Material's own surface
                // colour, so every glyph on top came from the design system and
                // the paper underneath did not - which is the sort of near-miss
                // that reads as "slightly off" without ever being locatable.
                // THE INSET GOES ON THE CONTENT, NOT THE SURFACE. Padding the
                // Surface shrinks the painted area too, so the strip behind the
                // status bar fell through to whatever was underneath - on a
                // managed Pixel that is the launcher wallpaper, which looked
                // like the app failing to draw its own background. Fill the
                // window with ground, then inset what sits on top of it.
                Surface(Modifier.fillMaxSize(), color = Ink.ground) {
                    Box(Modifier.windowInsetsPadding(WindowInsets.safeDrawing)) {
                    val model: BondViewModel = viewModel()
                    val state by model.state.collectAsStateWithLifecycle()

                    // ONE BOOLEAN, NOT A NAV GRAPH. There are two screens and
                    // the second is a leaf; a navigation library here would be
                    // more moving parts than the thing it navigates.
                    var showDiagnostics by rememberSaveable { mutableStateOf(false) }
                    var destination by rememberSaveable { mutableStateOf(Destination.STATUS) }
                    var diagnostics by remember { mutableStateOf(Diagnostics()) }
                    var measuring by remember { mutableStateOf(false) }
                    val scope = rememberCoroutineScope()
                    val measurer = remember { DiagnosticsMeasurer(applicationContext) }

                    // MEASURES ON OPEN AND ON TAP, and never on a timer. This
                    // phone's job is relaying; a diagnostics screen that polls
                    // in the background would spend battery to answer a
                    // question nobody is asking.
                    fun remeasure() {
                        if (measuring) return
                        measuring = true
                        scope.launch {
                            diagnostics = measurer.measure()
                            measuring = false
                        }
                    }

                    if (showDiagnostics) {
                        // BOTH exits, and the visible one is not redundant. The
                        // gesture is the only affordance a phone on gesture
                        // navigation offers, and it is invisible - this screen
                        // was reported as inescapable on 2026-08-12 with this
                        // handler already in place and working.
                        BackHandler { showDiagnostics = false }
                        LaunchedEffect(Unit) { remeasure() }
                        // Read when the screen opens rather than held in
                        // state: it changes only across a reboot, and a stale
                        // copy on a screen whose whole purpose is telling the
                        // truth about the last boot would be worse than none.
                        val bootLog = remember { BootLog.readAll(this@MainActivity) }
                        DiagnosticsScreen(
                            diagnostics = diagnostics,
                            measuring = measuring,
                            onRefresh = ::remeasure,
                            onBack = { showDiagnostics = false },
                            bootLog = bootLog,
                        )
                    } else {
                        // Colours stated explicitly for the same reason
                        // SectionTitle in StatusScreen.kt states them:
                        // Material's own tab-bar defaults do not know about
                        // `live`, the one accent this language spends on
                        // "carrying traffic right now" - leaving them out
                        // would give the bar its own, second opinion about
                        // what the accent colour is.
                        Scaffold(
                            containerColor = Ink.ground,
                            bottomBar = {
                                NavigationBar(containerColor = Ink.raised) {
                                    NavigationBarItem(
                                        selected = destination == Destination.STATUS,
                                        onClick = { destination = Destination.STATUS },
                                        icon = {
                                            Icon(Icons.Filled.CheckCircle, contentDescription = null)
                                        },
                                        label = { Text("Status") },
                                        colors = NavigationBarItemDefaults.colors(
                                            selectedIconColor = Ink.live,
                                            selectedTextColor = Ink.live,
                                            indicatorColor = Ink.live.copy(alpha = 0.12f),
                                            unselectedIconColor = Ink.secondary,
                                            unselectedTextColor = Ink.secondary,
                                        ),
                                    )
                                    NavigationBarItem(
                                        selected = destination == Destination.RELAY,
                                        onClick = { destination = Destination.RELAY },
                                        icon = {
                                            Icon(Icons.AutoMirrored.Filled.Send, contentDescription = null)
                                        },
                                        label = { Text("Relay") },
                                        colors = NavigationBarItemDefaults.colors(
                                            selectedIconColor = Ink.live,
                                            selectedTextColor = Ink.live,
                                            indicatorColor = Ink.live.copy(alpha = 0.12f),
                                            unselectedIconColor = Ink.secondary,
                                            unselectedTextColor = Ink.secondary,
                                        ),
                                    )
                                }
                            },
                        ) { padding ->
                            Box(Modifier.padding(padding)) {
                                when (destination) {
                                    Destination.STATUS -> StatusScreen(
                                        state = state,
                                        onRefresh = model::refresh,
                                        onOpenDiagnostics = { showDiagnostics = true },
                                    )
                                    Destination.RELAY -> RelayScreen(
                                        state = state,
                                        onStartRelay = ::startContributing,
                                        onStopRelay = ::stopContributing,
                                        onStartClient = ::startClientMode,
                                        onSaveToken = model::saveAnnounceToken,
                                    )
                                }
                            }
                        }
                        }
                    }
                }
            }
        }
    }

    /**
     * Refresh BootReceiver's device-protected mirror of the router address,
     * port and data budget (#54), so the next reboot - which may leave the
     * phone locked with nobody to open the app - uses today's configuration
     * rather than whatever was last synced. See BootConfigStore's class doc
     * for why the announce token is never part of this mirror.
     *
     * A fresh, unscoped CoroutineScope rather than lifecycleScope: the write
     * is two small SharedPreferences commits and must finish even if the
     * user backs out of the activity immediately after opening it, which
     * lifecycleScope would cancel mid-write.
     */
    private fun refreshBootConfig() {
        val context = applicationContext
        CoroutineScope(Dispatchers.IO).launch {
            BootConfigStore.sync(context)
            maybeAutoStart(context)
        }
    }

    /**
     * Honour `autoStartRelay`, which until now nothing read.
     *
     * The key was declared in app_restrictions.xml, parsed by
     * ManagedConfig.autoStart, wrapped by AndroidManagedConfig.shouldAutoStart,
     * covered by five unit tests and documented as "whether the relay starts on
     * boot" - and `grep -rn shouldAutoStart` returned exactly one line, its own
     * definition. An MDM-provisioned phone therefore could not begin relaying
     * without somebody tapping the button, which is the opposite of the point.
     *
     * HERE, rather than in BootReceiver, deliberately. BootReceiver already
     * starts the relay unconditionally when proximity and budget allow, and
     * gating THAT on autoStartRelay would stop the phones which have been
     * running for months without the key set. This adds a path that did not
     * exist; it takes none away.
     *
     * Starting the relay also clears Android's stopped state for this package,
     * which is what lets BOOT_COMPLETED reach it on every subsequent boot - so
     * one launch is enough and the phone is autonomous afterwards.
     */
    private fun maybeAutoStart(context: Context) {
        // effectiveConfig, NOT load - see WakeReceiver for what reading the
        // stored-only source cost. Both entry points must ask the same source
        // or a policy-woken phone and a hand-opened phone disagree about
        // whether they are configured.
        val cfg = AndroidManagedConfig.effectiveConfig(context)
        val decision = AutoStartDecision.decide(
            managedAutoStart = AndroidManagedConfig.shouldAutoStart(context),
            alreadyRunning = RelayStatusStore.report.value != null,
            // A relay with no console cannot announce, so it would forward into
            // the void and report itself healthy. Refusing says more.
            hasUsableConfig = cfg.consoleLanHost.isNotBlank(),
        )
        // LOGGED EITHER WAY. The handset that exposed this defect wrote NOTHING
        // when it declined - no refusal, no reason - and that absence was the
        // only evidence available. A decision nobody can read is how this class
        // of bug survives.
        when (decision) {
            is AutoStartDecision.Start -> {
                Log.i(TAG, "autoStartRelay: starting the relay without a tap")
                BootLog.record(context, TAG, "autoStart: starting")
                startRelayService()
            }
            is AutoStartDecision.Stand -> {
                Log.i(TAG, "autoStartRelay: not starting - ${decision.reason}")
                // DURABLE, not just logcat. Logcat resets on the reboot being
                // diagnosed, which is why .177's silence cost two hours.
                BootLog.record(context, TAG, "autoStart: stood down - ${decision.reason}")
            }
        }
    }

    private fun startContributing() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            notificationPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
            return
        }
        startRelayService()
    }

    private fun startRelayService() {
        startForegroundService(Intent(this, RelayService::class.java))
    }

    private fun stopContributing() {
        // Through the service's own stop action rather than stopService, so the
        // notification button and this button take exactly one code path.
        startForegroundService(
            Intent(this, RelayService::class.java).setAction(RelayService.ACTION_STOP),
        )
    }

    private fun startClientMode() {
        val consent = VpnService.prepare(this)
        if (consent != null) {
            vpnConsent.launch(consent)
            return
        }
        startService(Intent(this, ZippieVpnService::class.java))
    }
}
