package app.zippie.companion

import java.time.LocalDate

/**
 * A hard ceiling on how much cellular the relay may spend.
 *
 * WHY THIS EXISTS. The relay holds a cellular socket open in a foreground
 * service, so it keeps forwarding with the screen off and the phone in a
 * pocket. Nobody is watching, and the person paying for the plan is often not
 * the person who started it - two Pixels are going into this bond and one of
 * them is not the operator's.
 *
 * A BUDGET THAT ONLY WARNS IS NOT A BUDGET. Exceeding it stops the bytes. The
 * alternative - carry on and show a badge - is the behaviour that produces a
 * surprise bill.
 */
data class DataBudget(
    /** Bytes per calendar day. Zero means unlimited, which is the correct
     *  default for a feature nobody has configured: inventing a cap would
     *  silently throttle a working relay. */
    val dailyBytes: Long = 0,
    /** Bytes per calendar month. Zero means unlimited. */
    val monthlyBytes: Long = 0,
) {
    val isConfigured: Boolean get() = dailyBytes > 0 || monthlyBytes > 0

    companion object {
        val unlimited = DataBudget()
    }
}

/** What the relay is allowed to do right now. */
sealed class BudgetVerdict {
    object Allowed : BudgetVerdict()
    data class DailyExhausted(val used: Long, val limit: Long) : BudgetVerdict()
    data class MonthlyExhausted(val used: Long, val limit: Long) : BudgetVerdict()

    val isAllowed: Boolean get() = this is Allowed

    /** Said in plain words, because "relay stopped" with no reason is
     *  indistinguishable from a crash. */
    val reason: String?
        get() = when (this) {
            is Allowed -> null
            is DailyExhausted ->
                "Daily data cap reached (${mb(used)} of ${mb(limit)}). Relaying resumes tomorrow."
            is MonthlyExhausted ->
                "Monthly data cap reached (${mb(used)} of ${mb(limit)}). Relaying resumes next month."
        }

    private fun mb(b: Long): String = "${b / 1_048_576} MB"
}

/**
 * Counts relayed bytes against the budget and rolls the counters over.
 *
 * Counts BOTH directions. A relay that only counted upstream would leave the
 * download half of a bonded session unmetered, which on a phone is the larger
 * half - the carrier bills for it either way.
 *
 * Synchronised because the two forwarding threads (up and down) both record
 * into it, and a lost increment is a byte that was spent and not counted.
 */
class BudgetLedger private constructor(
    var budget: DataBudget,
    dayUsed: Long,
    monthUsed: Long,
    dayStamp: Int,
    monthStamp: Int,
) {
    var dayUsed: Long = dayUsed
        private set
    var monthUsed: Long = monthUsed
        private set

    /**
     * Which day and month the counters belong to, as a comparable stamp rather
     * than a timer. A timer would have to survive the process being killed,
     * which on a phone it will not; a date comparison survives anything.
     */
    var dayStamp: Int = dayStamp
        private set
    var monthStamp: Int = monthStamp
        private set

    constructor(budget: DataBudget = DataBudget.unlimited, today: LocalDate = LocalDate.now()) :
        this(budget, 0, 0, dayIndex(today), monthIndex(today))

    /** Rolls the day and month counters if the calendar has moved on.
     *  Idempotent, and safe to call on every datagram. */
    @Synchronized
    fun rollover(today: LocalDate = LocalDate.now()) {
        val d = dayIndex(today)
        val m = monthIndex(today)
        if (d != dayStamp) {
            dayUsed = 0
            dayStamp = d
        }
        if (m != monthStamp) {
            monthUsed = 0
            monthStamp = m
        }
    }

    @Synchronized
    fun record(bytes: Long, today: LocalDate = LocalDate.now()) {
        if (bytes <= 0) return
        rollover(today)
        dayUsed = saturatingAdd(dayUsed, bytes)
        monthUsed = saturatingAdd(monthUsed, bytes)
    }

    @Synchronized
    fun verdict(today: LocalDate = LocalDate.now()): BudgetVerdict {
        rollover(today)
        if (budget.monthlyBytes > 0 && monthUsed >= budget.monthlyBytes) {
            return BudgetVerdict.MonthlyExhausted(monthUsed, budget.monthlyBytes)
        }
        if (budget.dailyBytes > 0 && dayUsed >= budget.dailyBytes) {
            return BudgetVerdict.DailyExhausted(dayUsed, budget.dailyBytes)
        }
        return BudgetVerdict.Allowed
    }

    companion object {
        /** Restores counters that outlived the process. Separate from the public
         *  constructor so a restore can never be mistaken for a fresh start:
         *  starting fresh is exactly how a cap gets silently reset. */
        fun restore(
            budget: DataBudget,
            dayUsed: Long,
            monthUsed: Long,
            dayStamp: Int,
            monthStamp: Int,
        ) = BudgetLedger(budget, dayUsed, monthUsed, dayStamp, monthStamp)

        fun dayIndex(d: LocalDate): Int = d.toEpochDay().toInt()

        fun monthIndex(d: LocalDate): Int = d.year * 12 + d.monthValue

        /**
         * Saturating rather than wrapping. Wrapping past Long.MAX_VALUE would
         * hand back a small - or negative - "used" figure, and that is the one
         * arithmetic outcome that silently disables the cap.
         */
        private fun saturatingAdd(a: Long, b: Long): Long {
            val sum = a + b
            return if (sum < a) Long.MAX_VALUE else sum
        }
    }
}

/**
 * The ledger as a single string, for SharedPreferences.
 *
 * Counters must outlive the process or the cap resets every time Android
 * reclaims the app, which turns a monthly budget into a per-launch one. The
 * leading version field exists so that a future format change is REJECTED
 * rather than parsed as counters - a misread field here reads as "nothing spent
 * yet", the failure that costs money.
 */
object BudgetLedgerCodec {
    private const val VERSION = 1

    fun encode(ledger: BudgetLedger): String =
        listOf(VERSION, ledger.dayUsed, ledger.monthUsed, ledger.dayStamp, ledger.monthStamp)
            .joinToString("|")

    /** Null when the string is absent, truncated, from another version, or not
     *  numeric. The caller starts a fresh ledger, which is the only honest
     *  answer when the record cannot be read. */
    fun decode(raw: String?, budget: DataBudget): BudgetLedger? {
        if (raw.isNullOrBlank()) return null
        val parts = raw.split("|")
        if (parts.size != 5) return null
        if (parts[0].toIntOrNull() != VERSION) return null
        val dayUsed = parts[1].toLongOrNull() ?: return null
        val monthUsed = parts[2].toLongOrNull() ?: return null
        val dayStamp = parts[3].toIntOrNull() ?: return null
        val monthStamp = parts[4].toIntOrNull() ?: return null
        if (dayUsed < 0 || monthUsed < 0) return null
        return BudgetLedger.restore(budget, dayUsed, monthUsed, dayStamp, monthStamp)
    }
}
