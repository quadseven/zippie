package app.zippie.companion

import java.io.File
import java.io.IOException
import org.json.JSONException
import org.json.JSONObject

/**
 * Persists RelayStatusStore's report to disk, Android-free so the properties
 * that actually matter here - a fresh reader sees the real record instead of
 * null, a half-written file is never observed, staleness survives the round
 * trip - can be proven on the JVM. Split from RelayStatusStore for the same
 * reason BootLogFile is split from BootLog: the Android half is one call
 * (createDeviceProtectedStorageContext) and cannot be unit tested; everything
 * that actually decides correctness can.
 *
 * WHY THIS EXISTS AT ALL. RelayStatusStore.report is a MutableStateFlow held
 * in a process-static object, with nothing writing it to disk. A home-screen
 * widget updates in whatever process Android happens to start for the
 * broadcast, which is frequently a FRESH one - the relay's own long-lived
 * process is not required to be alive for the system to ask a widget to
 * redraw. A fresh process starts with an empty StateFlow, so a widget built
 * on the in-memory store alone would confidently render "Off" while the
 * relay, in its own process, was carrying traffic. quadseven/zippie#244.
 *
 * NULL STAYS NULL. Absent (never published, or cleared by a clean stop) and
 * present-but-stale (a process that stopped reporting) are different facts
 * with different words on screen, exactly as RelayStatusStore's own type doc
 * says for the in-memory case - this class must not collapse them into one
 * "nothing useful" result. [read] returns null for the former and a
 * [RelayReport] - with its own [RelayReport.isStale] answering honestly - for
 * the latter.
 */
class RelayReportFile(
    private val file: File,
    /**
     * The floor on how often a published report actually reaches disk.
     *
     * RelayService republishes on a ~2s heartbeat (RelayReport.HEARTBEAT_MS)
     * for as long as the relay runs, which on a phone is potentially forever.
     * A naive write-through on every publish() is a disk write every 2s
     * without end. Of the two defensible fixes named in #244 - throttle by
     * time, or write only on a meaningful change plus a slower keepalive -
     * this is the simpler one, and it loses little here: almost every
     * heartbeat DOES change something (byte counters move whenever the relay
     * is actually carrying), so change-detection would rarely get to skip a
     * write that time-throttling does not already skip.
     *
     * MUST STAY WELL UNDER RelayReport.STALENESS_MS, not merely under it. The
     * on-disk timestamp only advances when a write lands, so the worst-case
     * age of the persisted record - the instant before the next throttled
     * write finally lands - is about this interval rounded up to the next
     * heartbeat tick. Half the staleness threshold leaves a full extra
     * heartbeat of scheduling jitter as margin, so a relay that is genuinely
     * alive can never be read back from disk as NotReporting.
     */
    private val persistIntervalMs: Long = RelayReport.STALENESS_MS / 2,
) {
    /** Null until the first publish() in this instance's lifetime - see the
     *  "writes immediately" note on [publish]. */
    @Volatile private var lastWrittenAtMs: Long? = null

    /**
     * Record the relay's latest report, throttled per [persistIntervalMs].
     *
     * THE FIRST CALL AFTER CONSTRUCTION ALWAYS WRITES. A process that just
     * started relaying - or a fresh RelayReportFile built for a process that
     * has never published before - must not leave a stale-or-absent disk
     * record sitting for up to [persistIntervalMs] before anything reads
     * it honestly.
     *
     * NEVER THROWS, same posture as BootLogFile.append. RelayService calls
     * this from its heartbeat thread on every tick, the SAME thread that also
     * saves the budget ledger and ships telemetry (RelayService.startHeartbeat)
     * - a full disk or a storage layer that is not ready yet must not be able
     * to kill that thread and silently take the other two down with it.
     */
    @Synchronized
    fun publish(report: RelayReport) {
        val last = lastWrittenAtMs
        if (last != null && report.updatedAtMs - last < persistIntervalMs) return
        try {
            writeAtomic(encode(report))
            lastWrittenAtMs = report.updatedAtMs
        } catch (e: IOException) {
            // Best-effort. lastWrittenAtMs deliberately NOT updated: the
            // write did not land, so the next heartbeat should try again
            // rather than waiting out a full throttle interval on a failure.
        } catch (e: SecurityException) {
            // ditto
        }
    }

    /**
     * A clean stop. Deletes the file rather than writing a zeroed record,
     * matching RelayStatusStore.clear(): absent means "nothing is running",
     * and a zeroed-but-present file would read back as "running and carrying
     * nothing" - the wrong claim for a relay that was deliberately stopped.
     *
     * NEVER throttled, unlike [publish]. A stop a widget cannot see for up to
     * [persistIntervalMs] shows a corpse for that entire window - exactly the
     * failure rule 3 of #244 exists to close.
     */
    @Synchronized
    fun clear() {
        lastWrittenAtMs = null
        try {
            file.delete()
        } catch (e: SecurityException) {
            // Best-effort, same posture as BootLogFile.append: a storage
            // layer that refuses a delete must not be able to take the stop
            // path down with it.
        }
    }

    /**
     * Read back whatever was last written. Null when nothing was ever
     * written, the file cannot be read, its contents do not parse, or they
     * were written by an incompatible future version - all of which are
     * "nothing trustworthy on disk" to a caller, not distinct errors worth
     * telling apart (mirrors BudgetLedgerCodec.decode's own reasoning).
     *
     * THIS is the fresh-process read: no in-memory state is consulted here,
     * only the file, so a brand new RelayReportFile pointed at the same path
     * as one that already published sees the real report.
     */
    fun read(): RelayReport? = try {
        if (!file.exists()) null else decode(file.readText())
    } catch (e: IOException) {
        null
    } catch (e: SecurityException) {
        null
    }

    /**
     * Write-to-temp-then-rename, so a reader only ever observes the old
     * complete file or the new complete file, never a half-written one - a
     * half-written record read by a widget is worse than no record at all.
     *
     * Tries a plain rename over the existing target first, which is an
     * atomic replace on the ext4/f2fs filesystems backing internal app
     * storage. Falls back to BootLogFile.rotateIfFull's delete-then-rename
     * only if that fails, because renameTo does NOT replace an existing
     * target on every Android storage backend (the same gap BootLogFile
     * already had to work around) - but unlike a rotating log, the value
     * here is a single snapshot, so it is worth trying to keep the OLD
     * record intact until the new one is confirmed in place, rather than
     * deleting it first on every write.
     */
    private fun writeAtomic(json: String) {
        file.parentFile?.mkdirs()
        val tmp = File(file.parentFile, file.name + ".tmp")
        tmp.writeText(json)
        if (tmp.renameTo(file)) return
        if (file.exists()) file.delete()
        tmp.renameTo(file)
    }

    companion object {
        /** Rejects a payload from a future, incompatible shape rather than
         *  misreading its fields - the same guard BudgetLedgerCodec uses and
         *  for the same reason: a misread here should fail toward "nothing
         *  trustworthy", not toward invented counters. */
        private const val VERSION = 1

        private fun encode(report: RelayReport): String {
            val s = report.stats
            val o = JSONObject()
            o.put("version", VERSION)
            o.put("updatedAtMs", report.updatedAtMs)
            o.put("listening", s.listening)
            o.put("cellularReady", s.cellularReady)
            o.put("upDatagrams", s.upDatagrams)
            o.put("upBytes", s.upBytes)
            o.put("downDatagrams", s.downDatagrams)
            o.put("downBytes", s.downBytes)
            o.put("errors", s.errors)
            o.put("rejectedSources", s.rejectedSources)
            o.put("budgetBlocked", s.budgetBlocked)
            o.put("downDroppedNoPeer", s.downDroppedNoPeer)
            o.put("downDroppedNoSocket", s.downDroppedNoSocket)
            o.put("downDroppedSendError", s.downDroppedSendError)
            o.put("budgetExhausted", s.budgetExhausted ?: JSONObject.NULL)
            o.put("lastError", s.lastError ?: JSONObject.NULL)
            o.put("dayUsedBytes", s.dayUsedBytes)
            o.put("monthUsedBytes", s.monthUsedBytes)
            o.put("dailyBudgetBytes", s.budget.dailyBytes)
            o.put("monthlyBudgetBytes", s.budget.monthlyBytes)
            o.put("carrierServing", s.carrier?.serving ?: JSONObject.NULL)
            o.put("carrierSim", s.carrier?.sim ?: JSONObject.NULL)
            o.put("announce", s.announce ?: JSONObject.NULL)
            o.put("lastRouterInboundAtMs", s.lastRouterInboundAtMs ?: JSONObject.NULL)
            return o.toString()
        }

        /** Null on anything that is not a valid, current-version record. See
         *  the class doc on [read] for why every failure shape collapses to
         *  the same "absent" answer. */
        private fun decode(raw: String): RelayReport? {
            val o = try {
                JSONObject(raw)
            } catch (e: JSONException) {
                return null
            }
            if (o.intOrNull("version") != VERSION) return null
            val updatedAtMs = o.longOrNull("updatedAtMs") ?: return null

            val carrierServing = o.stringOrNull("carrierServing")
            val carrierSim = o.stringOrNull("carrierSim")
            val carrier = if (carrierServing != null || carrierSim != null) {
                CarrierInfo(carrierServing, carrierSim)
            } else {
                null
            }

            val stats = RelayStats(
                listening = o.optBoolean("listening", false),
                cellularReady = o.optBoolean("cellularReady", false),
                upDatagrams = o.longOrNull("upDatagrams") ?: 0L,
                upBytes = o.longOrNull("upBytes") ?: 0L,
                downDatagrams = o.longOrNull("downDatagrams") ?: 0L,
                downBytes = o.longOrNull("downBytes") ?: 0L,
                errors = o.longOrNull("errors") ?: 0L,
                rejectedSources = o.longOrNull("rejectedSources") ?: 0L,
                budgetBlocked = o.longOrNull("budgetBlocked") ?: 0L,
                downDroppedNoPeer = o.longOrNull("downDroppedNoPeer") ?: 0L,
                downDroppedNoSocket = o.longOrNull("downDroppedNoSocket") ?: 0L,
                downDroppedSendError = o.longOrNull("downDroppedSendError") ?: 0L,
                budgetExhausted = o.stringOrNull("budgetExhausted"),
                lastError = o.stringOrNull("lastError"),
                dayUsedBytes = o.longOrNull("dayUsedBytes") ?: 0L,
                monthUsedBytes = o.longOrNull("monthUsedBytes") ?: 0L,
                budget = DataBudget(
                    dailyBytes = o.longOrNull("dailyBudgetBytes") ?: 0L,
                    monthlyBytes = o.longOrNull("monthlyBudgetBytes") ?: 0L,
                ),
                carrier = carrier,
                announce = o.stringOrNull("announce"),
                lastRouterInboundAtMs = o.longOrNull("lastRouterInboundAtMs"),
            )
            return RelayReport(stats, updatedAtMs)
        }
    }
}
