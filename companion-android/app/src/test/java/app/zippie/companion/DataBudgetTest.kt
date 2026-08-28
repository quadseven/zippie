package app.zippie.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.LocalDate

/**
 * The cap has to REFUSE, not warn. Every test here fails if the enforcement is
 * softened into a display value.
 */
class DataBudgetTest {

    private val day = LocalDate.of(2026, 8, 5)

    @Test
    fun `an unconfigured budget never blocks`() {
        val ledger = BudgetLedger(DataBudget.unlimited, day)
        ledger.record(50L * 1024 * 1024 * 1024, day)
        assertTrue(ledger.verdict(day).isAllowed)
        assertFalse(DataBudget.unlimited.isConfigured)
    }

    @Test
    fun `spending up to the daily cap exhausts it`() {
        val ledger = BudgetLedger(DataBudget(dailyBytes = 10 * MB), day)
        ledger.record(9 * MB, day)
        assertTrue(ledger.verdict(day).isAllowed)
        ledger.record(1 * MB, day)
        val verdict = ledger.verdict(day)
        assertFalse("at the cap is spent, not nearly spent", verdict.isAllowed)
        assertTrue(verdict is BudgetVerdict.DailyExhausted)
        assertNotNull(verdict.reason)
        assertTrue(verdict.reason!!.contains("10 MB of 10 MB"))
    }

    @Test
    fun `the monthly cap outranks the daily one`() {
        val ledger = BudgetLedger(DataBudget(dailyBytes = 100 * MB, monthlyBytes = 5 * MB), day)
        ledger.record(6 * MB, day)
        assertTrue(ledger.verdict(day) is BudgetVerdict.MonthlyExhausted)
    }

    @Test
    fun `a new day reopens the daily cap but not the monthly one`() {
        val ledger = BudgetLedger(DataBudget(dailyBytes = 10 * MB, monthlyBytes = 15 * MB), day)
        ledger.record(10 * MB, day)
        assertFalse(ledger.verdict(day).isAllowed)

        val tomorrow = day.plusDays(1)
        assertTrue("the day rolled over", ledger.verdict(tomorrow).isAllowed)
        assertEquals(0L, ledger.dayUsed)
        assertEquals("the month must not roll with the day", 10 * MB, ledger.monthUsed)

        ledger.record(5 * MB, tomorrow)
        assertTrue(ledger.verdict(tomorrow) is BudgetVerdict.MonthlyExhausted)
    }

    @Test
    fun `a new month clears both counters`() {
        val ledger = BudgetLedger(DataBudget(dailyBytes = 10 * MB, monthlyBytes = 15 * MB), day)
        ledger.record(15 * MB, day)
        val nextMonth = LocalDate.of(2026, 9, 1)
        assertTrue(ledger.verdict(nextMonth).isAllowed)
        assertEquals(0L, ledger.dayUsed)
        assertEquals(0L, ledger.monthUsed)
    }

    /**
     * Wrapping past Long.MAX_VALUE would hand back a small "used" figure, which
     * is the one arithmetic outcome that silently switches the cap off.
     */
    @Test
    fun `counters saturate rather than wrap`() {
        val ledger = BudgetLedger(DataBudget(monthlyBytes = 1 * MB), day)
        ledger.record(Long.MAX_VALUE - 1, day)
        ledger.record(Long.MAX_VALUE - 1, day)
        assertEquals(Long.MAX_VALUE, ledger.monthUsed)
        assertFalse(ledger.verdict(day).isAllowed)
    }

    @Test
    fun `nothing is counted for an empty or negative datagram`() {
        val ledger = BudgetLedger(DataBudget(dailyBytes = 1 * MB), day)
        ledger.record(0, day)
        ledger.record(-5, day)
        assertEquals(0L, ledger.dayUsed)
    }

    @Test
    fun `counters survive a restart through the codec`() {
        val ledger = BudgetLedger(DataBudget(dailyBytes = 10 * MB), day)
        ledger.record(3 * MB, day)
        val restored = BudgetLedgerCodec.decode(
            BudgetLedgerCodec.encode(ledger),
            DataBudget(dailyBytes = 10 * MB),
        )
        assertNotNull(restored)
        assertEquals(3 * MB, restored!!.dayUsed)
        assertEquals(3 * MB, restored.monthUsed)
        assertEquals(BudgetLedger.dayIndex(day), restored.dayStamp)
        assertEquals(BudgetLedger.monthIndex(day), restored.monthStamp)
    }

    /**
     * An unreadable record must not decode into "nothing spent yet" - that is
     * the failure that costs money.
     */
    @Test
    fun `an unreadable record is rejected instead of read as zero`() {
        val budget = DataBudget(dailyBytes = 1 * MB)
        assertNull(BudgetLedgerCodec.decode(null, budget))
        assertNull(BudgetLedgerCodec.decode("", budget))
        assertNull(BudgetLedgerCodec.decode("1|2|3", budget))
        assertNull("a future format is not this format",
            BudgetLedgerCodec.decode("2|1|1|1|1", budget))
        assertNull(BudgetLedgerCodec.decode("1|x|1|1|1", budget))
        assertNull(BudgetLedgerCodec.decode("1|-1|1|1|1", budget))
    }

    companion object {
        private const val MB = 1_048_576L
    }
}
