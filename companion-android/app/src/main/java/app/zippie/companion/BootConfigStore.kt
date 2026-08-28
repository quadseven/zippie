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
 * WHAT DELIBERATELY DOES NOT LIVE HERE. RelayConfiguration.announceToken is
 * the router console's WRITE token - a credential, not configuration, and the
 * thing that lets this phone add a leg the router will dial. Mirroring it
 * into device-protected storage would make an authentication secret readable
 * by anyone with physical access to a locked, powered-on phone: unlike
 * credential-encrypted storage, device-protected storage is not gated by the
 * user's lock-screen credential at all, only by the OS's normal per-app
 * sandboxing. That is a real weakening of the security posture for a phone
 * whose whole point is sitting unattended in a car, so it is refused here.
 *
 * THE CONSEQUENCE, STATED RATHER THAN HIDDEN: a relay started before first
 * unlock RELAYS - it forwards bytes - but does not ANNOUNCE, because
 * RelayService (unedited by this change) still reads the token from
 * credential-encrypted storage via RelayConfiguration.load and finds it
 * empty there. See BootReceiver's class doc for exactly what does and does
 * not resume, and what would be needed to close that gap without weakening
 * where the token lives.
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
