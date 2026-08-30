package app.zippie.companion

import android.content.Context
import java.io.File
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * What the relay has actually done. Every field is a MEASUREMENT, not a
 * configuration echo.
 */
data class RelayStats(
    /** The LAN socket is bound and the router could reach us. */
    val listening: Boolean = false,
    /**
     * A cellular socket exists and is bound to the cellular network. Distinct
     * from [listening]: the wifi side can be up while cellular is unusable, and
     * conflating those hides the failure that matters.
     */
    val cellularReady: Boolean = false,
    val upDatagrams: Long = 0,
    val upBytes: Long = 0,
    val downDatagrams: Long = 0,
    val downBytes: Long = 0,
    val errors: Long = 0,
    /**
     * Datagrams refused because the sender was not a plausible router: a
     * non-local source, or a second source trying to displace a live one. A
     * number that climbs on a network you trust is worth looking at; on an
     * untrusted one it is the guard doing its job.
     */
    val rejectedSources: Long = 0,
    /**
     * Datagrams the relay declined to forward because the cap was spent. Exists
     * so the gate can be PROVEN wired: a budget that is computed and then
     * ignored looks identical to one that is enforced, until the bill arrives.
     */
    val budgetBlocked: Long = 0,
    /** Downstream replies dropped, split by CAUSE (#186). These were three bare
     *  `?: continue` branches with no counter at all, which is how a relay that
     *  forwarded upstream perfectly and returned nothing looked healthy for
     *  hours. Split rather than summed: each one is a different bug. */
    val downDroppedNoPeer: Long = 0,
    val downDroppedNoSocket: Long = 0,
    val downDroppedSendError: Long = 0,
    /** Set when the relay has stopped carrying because the cap is spent.
     *  Deliberately not [lastError]: this is not a failure, it is the budget
     *  working, and the UI says so differently. */
    val budgetExhausted: String? = null,
    val lastError: String? = null,
    val dayUsedBytes: Long = 0,
    val monthUsedBytes: Long = 0,
    val budget: DataBudget = DataBudget.unlimited,
    /** The carrier this phone's cellular leg is on, when the radio reported one. */
    val carrier: CarrierInfo? = null,
    /**
     * What the router said about this phone's last announcement, in the router's
     * own words - or why nothing was announced at all.
     *
     * A MEASUREMENT like the rest, not a configuration echo: it is the reply to
     * a request that was actually made. It is here because a relay that carries
     * perfectly and is invisible to the bond looks, from the phone, exactly like
     * a relay that works.
     */
    val announce: String? = null,
    /**
     * Whether Android will let this relay keep running with the screen off.
     *
     * REPORTED, not merely acted on. A frozen relay looks perfectly healthy
     * from inside its own process - it keeps announcing on a timer while its
     * socket stops being serviced - so the phone had no way to say the one
     * thing that predicts the outage. False here is the difference between
     * "the router sees nothing and we are guessing" and "the router sees
     * nothing BECAUSE this phone is not exempt" (#254).
     */
    val ignoringBatteryOptimizations: Boolean = true,
    /**
     * When something last ARRIVED from the router, or null if nothing ever has.
     * Ported from CellularRelay.Stats.lastRouterInboundAt
     * (ZippieCompanionKit/CellularRelay.swift, quadseven/zippie#44) - the only
     * evidence on this struct that the far end exists; every other field is a
     * fact about THIS PHONE.
     *
     * A COUNT WOULD NOT DO. [upDatagrams] already proves something arrived at
     * some point, and it never goes down - so once a single packet had
     * crossed, a summary built from it alone read "carrying" forever,
     * including an hour after the router died. Recorded in
     * RelayService.pumpUp, when the packet ARRIVES and before the forwarding
     * decision, so a router dialling a phone with dead cellular still shows up
     * as a router that is dialling. See RelayVerdict.kt for the state this
     * drives.
     */
    val lastRouterInboundAtMs: Long? = null,
) {
    /** One line for the notification and the top of the relay section.
     *
     *  Delegates to [RelayVerdict] so the notification and the on-screen
     *  sentence can never say two different things about the same evidence.
     *  Wrapped in a report stamped with "now": this call is always live, made
     *  by the same process that is writing these counters, so there is no
     *  separate "did the reporting process die" question to ask the way
     *  [RelayReport.isStale] has to for a report read back off disk. */
    val summary: String
        get() = RelayVerdict.evaluate(RelayReport(this, System.currentTimeMillis())).detail()

    /**
     * What this run has actually relayed, both directions summed.
     *
     * THIS PHONE'S OWN COUNTERS, which the router cannot report - matching
     * BondModel.swift's `BudgetSummary.usedBytes`. It is deliberately not
     * [dayUsedBytes] or [monthUsedBytes]: those persist across restarts of the
     * relay and answer "how much of the cap is spent", where this answers "how
     * much has THIS run of the app carried" - the "Session total" line on the
     * Status screen. The caller is responsible for suppressing it while the
     * report is stale, the same way the byte counts above it already are: a
     * stopped relay's last count sitting under a live leg table would read as
     * current spending, and it is not.
     */
    val sessionBytes: Long
        get() = (upBytes + downBytes).coerceAtLeast(0)

    /**
     * Whether Android is about to make a liar of everything above.
     *
     * DELIBERATELY NOT A [RelayVerdict] CASE. The verdict says what the relay
     * is DOING; this says whether it will be allowed to keep doing it. They are
     * orthogonal - a relay that is carrying perfectly right now is also the
     * relay most worth warning, because it is the one with something to lose.
     * Folding this into the verdict would force a single slot to choose between
     * them, and the app has already been bitten by exactly that: a leg that was
     * degraded AND carrying collapsed to "degraded" and the screen reported
     * "Nothing carrying" over a row reading "carrying, degraded" (#267).
     *
     * [listening] stands for "meant to be a leg": the socket is bound, so this
     * phone is contributing rather than idle.
     */
    val batteryExemption: BatteryExemption
        get() = BatteryExemption.decide(
            isIgnoringBatteryOptimizations = ignoringBatteryOptimizations,
            isContributing = listening,
        )
}

/**
 * A relay report plus the time it was written.
 *
 * The timestamp is not decoration, even though - unlike iOS, where the relay
 * lives in a separate Network Extension process - this store and the relay
 * share a process. The relay's pumps are plain threads: one can die on an
 * unexpected exception while the service object and the UI carry on, and the
 * counters it left behind would then sit on screen looking current. A report
 * older than the heartbeat means "not reporting", not "idle".
 */
data class RelayReport(val stats: RelayStats, val updatedAtMs: Long) {
    fun isStale(nowMs: Long, thresholdMs: Long = STALENESS_MS): Boolean =
        nowMs - updatedAtMs > thresholdMs

    companion object {
        /** How often the relay rewrites the record even when nothing changed. */
        const val HEARTBEAT_MS = 2_000L

        /** Generous multiple of the heartbeat. A phone under memory pressure can
         *  stall a thread for a few seconds without being dead, and flapping
         *  "stale" on every hiccup trains the operator to ignore it. */
        const val STALENESS_MS = 5 * HEARTBEAT_MS
    }
}

/**
 * The relay's latest report, for the UI.
 *
 * Null means NOTHING IS RUNNING, which is deliberately different from a zeroed
 * report: a zeroed report would read as "running and carrying nothing", and
 * those two states need different words on screen.
 *
 * IN-MEMORY ONLY ABOVE [report] ITSELF - the StateFlow starts every process at
 * null regardless of what the relay last published, which is fine for the
 * screen that only exists while the process holding the relay is also alive,
 * and wrong for a home-screen widget, which is frequently asked to redraw in a
 * fresh process with nothing in it (quadseven/zippie#244). [publish] and
 * [clear] below also mirror to disk through [RelayReportFile] so [readPersisted]
 * can answer that fresh-process question honestly; [RelayReportFile]'s own doc
 * covers the write-volume and atomicity reasoning.
 */
object RelayStatusStore {
    private val _report = MutableStateFlow<RelayReport?>(null)
    val report: StateFlow<RelayReport?> = _report.asStateFlow()

    /** Built once per process and cached, so [RelayReportFile]'s write
     *  throttle - how long since the last DISK write - survives across every
     *  heartbeat instead of resetting (and losing the point of throttling) on
     *  every call. */
    @Volatile private var file: RelayReportFile? = null

    private fun file(context: Context): RelayReportFile {
        file?.let { return it }
        synchronized(this) {
            file?.let { return it }
            return RelayReportFile(reportFile(context)).also { file = it }
        }
    }

    /**
     * DEVICE-PROTECTED STORAGE, matching BootConfigStore. BootReceiver can
     * start RelayService on LOCKED_BOOT_COMPLETED, before first unlock, so the
     * relay may already be publishing reports while credential-encrypted
     * storage is not readable at all - a record a widget cannot read until
     * the phone is unlocked is useless for exactly the boot this app cares
     * about most. Nothing in RelayStats is a credential (contrast
     * BootConfigStore's refusal to mirror the announce token), so there is no
     * BootConfigStore-style reason to keep any of it out of this storage.
     */
    private fun reportFile(context: Context): File =
        File(
            context.applicationContext.createDeviceProtectedStorageContext().filesDir,
            "relay_report.json",
        )

    fun publish(context: Context, stats: RelayStats, nowMs: Long = System.currentTimeMillis()) {
        val report = RelayReport(stats, nowMs)
        _report.value = report
        file(context).publish(report)
    }

    /** Called on a clean stop. See the type comment for why this is not a
     *  zeroed publish. */
    fun clear(context: Context) {
        _report.value = null
        file(context).clear()
    }

    /**
     * What a FRESH process reads: the persisted record, not [report] above,
     * which would answer null for every process that has not itself called
     * [publish]. A widget calls this rather than collecting [report].
     */
    fun readPersisted(context: Context): RelayReport? = file(context).read()
}
