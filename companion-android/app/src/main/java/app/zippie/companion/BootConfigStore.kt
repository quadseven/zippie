package app.zippie.companion

import android.content.Context
import android.content.SharedPreferences

/**
 * The slice of RelayConfiguration - plus the budget ledger - that BootReceiver
 * needs to decide whether to start the relay BEFORE the device is unlocked.
 *
 * WHY A SEPARATE STORE, IN DEVICE-PROTECTED STORAGE. RelayConfiguration's
 * SharedPreferences (RelayConfiguration.prefs) live in credential encrypted
 * storage - the default for Context.getSharedPreferences - which Android does
 * not make available until the user has unlocked the device at least once
 * since boot (developer.android.com/privacy-and-security/direct-boot). A
 * phone that reboots and is left locked never reaches that unlock, so
 * BootReceiver - which runs BEFORE unlock, via LOCKED_BOOT_COMPLETED, because
 * it is marked directBootAware - cannot read RelayConfiguration.prefs at all
 * at the moment it matters most. This store lives under
 * Context.createDeviceProtectedStorageContext() instead, a physically
 * different location on disk that survives the locked window.
 *
 * THE ANNOUNCE TOKEN. The router write token lives in the separate
 * device-protected relay store so unattended locked boots can announce. This
 * accepts the risk that the credential is available before first unlock,
 * protected by the app sandbox rather than the lock-screen credential. The
 * per-device certificate migration is intended to replace this shared token.
 *
 * KEPT IN SYNC FROM MainActivity.onCreate, which cannot run before unlock -
 * a locked device shows the keyguard before any launcher activity gets a
 * chance to run, so there is no earlier point that could feed this honestly.
 * That means the mirror is only ever as fresh as the last time the app was
 * opened. Stated plainly rather than left implicit: it is the one soft edge
 * in an otherwise hard boundary, and it is why [read] falls back to
 * RelayConfiguration's own field defaults - this deployment's real router and
 * home addresses, not placeholders - when nothing has been synced yet.
 */
object BootConfigStore {
    private const val STORE_NAME = "zippie_boot"

    private const val KEY_CONSOLE_LAN = "consoleLanHost"
    private const val KEY_CONSOLE_URL = "consoleUrl"
    private const val KEY_LISTEN_PORT = "listenPort"
    private const val KEY_DAILY_BUDGET = "dailyBudgetBytes"
    private const val KEY_MONTHLY_BUDGET = "monthlyBudgetBytes"
    private const val KEY_STAND_DOWN_REASON = "budgetStandDownReason"
    private const val KEY_STAND_DOWN_AT = "budgetStandDownAt"

    /**
     * THE SAME LITERAL KEY RelayService uses for its ledger inside
     * RelayConfiguration.prefs ("budgetLedger" there, as RelayService.KEY_LEDGER).
     * Restated rather than imported: that constant is private to RelayService,
     * deliberately, since this change must not edit RelayService.kt to expose
     * it. If that key ever changes there, this mirror silently stops picking
     * up new ledger values and [Snapshot.budgetVerdict] falls back to "no
     * ledger recorded" - the fail-OPEN direction. BootRelayDecisionTest
     * documents that fallback (an absent ledger reads as unspent, matching
     * BudgetLedgerCodec's own default) so the failure mode is at least a
     * known, tested one rather than a silent divergence.
     */
    private const val KEY_LEDGER_MIRROR = "budgetLedger"

    private fun store(context: Context): SharedPreferences =
        context.applicationContext
            .createDeviceProtectedStorageContext()
            .getSharedPreferences(STORE_NAME, Context.MODE_PRIVATE)

    /** Everything BootReceiver needs to decide, read from device-protected
     *  storage. Never throws on a fresh install - see [read]'s fallback. */
    data class Snapshot(
        val consoleLanHost: String,
        val consoleUrl: String,
        val listenPort: Int,
        val budget: DataBudget,
        val ledgerRaw: String?,
    ) {
        /**
         * Mirrors RelayConfiguration.consoleCandidates exactly, by running
         * the SAME pure computation on a throwaway instance built from this
         * snapshot's fields - RelayConfiguration.kt is not edited to expose
         * that logic any other way, and it takes no Context, so this is not
         * a new probe surface, just a reuse of an existing pure getter.
         */
        val consoleCandidates: List<ConsoleCandidate>
            get() = RelayConfiguration(
                listenPort = listenPort,
                consoleLanHost = consoleLanHost,
                consoleUrl = consoleUrl,
            ).consoleCandidates

        /**
         * What the mirrored ledger allows right now. An absent or
         * unreadable ledger decodes to a fresh one (BudgetLedgerCodec's own
         * documented behaviour), which reads as "nothing spent yet" - the
         * correct answer on a fresh install, and the fail-open answer if the
         * mirror ever falls out of sync with RelayService's real ledger (see
         * [KEY_LEDGER_MIRROR]).
         */
        val budgetVerdict: BudgetVerdict
            get() {
                val ledger = BudgetLedgerCodec.decode(ledgerRaw, budget) ?: BudgetLedger(budget)
                return ledger.verdict()
            }
    }

    data class BudgetStandDown(val reason: String, val reconsiderAtEpochMs: Long) {
        /**
         * The line StatusScreen shows for a phone standing down on budget: the
         * reason and when it will reconsider, in the phone's own zone (#238
         * AC3). Kept here, Android-free, so a JVM test can assert the exact
         * text the screen draws (#255).
         */
        fun statusLine(
            zone: java.time.ZoneId = java.time.ZoneId.systemDefault(),
            locale: java.util.Locale = java.util.Locale.getDefault(),
        ): String {
            val reconsiderAt = java.time.Instant.ofEpochMilli(reconsiderAtEpochMs)
                .atZone(zone)
                .format(java.time.format.DateTimeFormatter.ofPattern("MMM d, HH:mm z", locale))
            return "Relay paused: $reason Rechecking at $reconsiderAt."
        }
    }

    fun recordBudgetStandDown(context: Context, reason: String, reconsiderAtEpochMs: Long): Boolean =
        store(context).edit()
            .putString(KEY_STAND_DOWN_REASON, reason)
            .putLong(KEY_STAND_DOWN_AT, reconsiderAtEpochMs)
            .commit()

    fun clearBudgetStandDown(context: Context) {
        store(context).edit().remove(KEY_STAND_DOWN_REASON).remove(KEY_STAND_DOWN_AT).apply()
    }

    fun budgetStandDown(context: Context): BudgetStandDown? {
        val prefs = store(context)
        val reason = prefs.getString(KEY_STAND_DOWN_REASON, null) ?: return null
        if (!prefs.contains(KEY_STAND_DOWN_AT)) return null
        return BudgetStandDown(reason, prefs.getLong(KEY_STAND_DOWN_AT, 0L))
    }

    /**
     * Refreshes the mirror from the real, credential-encrypted configuration.
     * Only ever called from MainActivity, which cannot run before unlock -
     * see the class doc for why that is the honest bound on how fresh this
     * gets.
     */
    fun sync(context: Context) {
        // Mirror the FULL relay configuration too, not just the fields this
        // store decides with. RelayService reads the whole thing on a locked
        // boot, and a relay that starts with no homeHost or no token is
        // running and useless (#locked-boot crash, 2026-08-16).
        runCatching { RelayConfiguration.mirrorForLockedBoot(context) }
        // Boot path too: a phone that reboots unattended must come back with the
        // same policy it was running, not the last thing a human typed.
        val config = AndroidManagedConfig.effectiveConfig(context)
        val ledgerRaw = RelayConfiguration.prefs(context).getString(KEY_LEDGER_MIRROR, null)
        val editor = store(context).edit()
            .putString(KEY_CONSOLE_LAN, config.consoleLanHost)
            .putString(KEY_CONSOLE_URL, config.consoleUrl)
            .putInt(KEY_LISTEN_PORT, config.listenPort)
            .putLong(KEY_DAILY_BUDGET, config.dailyBudgetBytes)
            .putLong(KEY_MONTHLY_BUDGET, config.monthlyBudgetBytes)
        if (ledgerRaw != null) editor.putString(KEY_LEDGER_MIRROR, ledgerRaw) else editor.remove(KEY_LEDGER_MIRROR)
        editor.apply()
    }

    /**
     * Preserve a capped ledger for the next locked boot. Partial spending stays
     * in credential-protected storage; mirroring only an exhausted ledger keeps
     * boot from resetting a cap without expanding the locked-storage policy.
     */
    fun persistExhaustedLedger(context: Context, ledgerRaw: String): Boolean {
        val snapshot = read(context)
        val ledger = BudgetLedgerCodec.decode(ledgerRaw, snapshot.budget) ?: return false
        if (ledger.verdict().isAllowed) return false
        return store(context).edit()
            .putString(KEY_LEDGER_MIRROR, ledgerRaw)
            .commit()
    }

    /**
     * Read the mirror. Falls back to RelayConfiguration's own field defaults
     * for anything never synced - see the class doc.
     */
    fun read(context: Context): Snapshot {
        val prefs = store(context)
        val defaults = RelayConfiguration()
        return Snapshot(
            consoleLanHost = prefs.getString(KEY_CONSOLE_LAN, null).orEmpty()
                .ifEmpty { defaults.consoleLanHost },
            consoleUrl = prefs.getString(KEY_CONSOLE_URL, null).orEmpty()
                .ifEmpty { defaults.consoleUrl },
            listenPort = prefs.getInt(KEY_LISTEN_PORT, defaults.listenPort),
            budget = DataBudget(
                dailyBytes = prefs.getLong(KEY_DAILY_BUDGET, defaults.dailyBudgetBytes),
                monthlyBytes = prefs.getLong(KEY_MONTHLY_BUDGET, defaults.monthlyBudgetBytes),
            ),
            ledgerRaw = prefs.getString(KEY_LEDGER_MIRROR, null),
        )
    }
}
