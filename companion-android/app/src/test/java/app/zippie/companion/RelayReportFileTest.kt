package app.zippie.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

/**
 * quadseven/zippie#244 step 2: RelayStatusStore used to be a MutableStateFlow
 * and nothing else, so a widget process started fresh by the system - which is
 * the common case, not the exception - read `null` ("nothing is running")
 * while the relay carried on in its own long-lived process. These tests exist
 * to prove the fix reads back the REAL report in that situation, not just that
 * encode/decode round-trips in the same process that wrote it.
 *
 * A NEW RelayReportFile POINTED AT THE SAME PATH is how "a different, fresh
 * process" is simulated here: [RelayReportFile.read] consults only the file,
 * never in-memory state, so a second instance with an empty [lastWrittenAtMs]
 * has to prove it by reading disk - exactly what a widget's process does.
 */
class RelayReportFileTest {

    @get:Rule
    val tmp = TemporaryFolder()

    private fun store(persistIntervalMs: Long = RelayReport.STALENESS_MS / 2): RelayReportFile =
        RelayReportFile(File(tmp.newFolder(), "relay_report.json"), persistIntervalMs)

    private fun reportAt(nowMs: Long, upBytes: Long = 4_096L) = RelayReport(
        RelayStats(
            listening = true,
            cellularReady = true,
            upDatagrams = 12,
            upBytes = upBytes,
            lastError = "up: no route to host",
            carrier = CarrierInfo(serving = "Visible", sim = "Verizon"),
            budget = DataBudget(dailyBytes = 500_000_000L, monthlyBytes = 15_000_000_000L),
            dayUsedBytes = 1_234_567L,
            announce = "leg accepted",
            lastRouterInboundAtMs = nowMs - 500,
        ),
        nowMs,
    )

    // ---- fresh-process read -----------------------------------------------

    @Test
    fun `a fresh instance reads back what an earlier instance wrote, not null`() {
        val path = File(tmp.newFolder(), "relay_report.json")
        val writer = RelayReportFile(path)
        val now = System.currentTimeMillis()
        writer.publish(reportAt(now))

        // A SEPARATE INSTANCE, same file, no shared state - the whole point.
        val reader = RelayReportFile(path)
        val back = reader.read()

        assertNotNull("a fresh process must see the real report, not null", back)
        assertEquals(now, back!!.updatedAtMs)
        assertEquals(12L, back.stats.upDatagrams)
        assertEquals(4_096L, back.stats.upBytes)
        assertEquals("up: no route to host", back.stats.lastError)
        assertEquals("Visible", back.stats.carrier?.serving)
        assertEquals("Verizon", back.stats.carrier?.sim)
        assertEquals(500_000_000L, back.stats.budget.dailyBytes)
        assertEquals(1_234_567L, back.stats.dayUsedBytes)
        assertEquals("leg accepted", back.stats.announce)
        assertEquals(now - 500, back.stats.lastRouterInboundAtMs)
    }

    @Test
    fun `nothing persisted reads as null, not a zeroed report`() {
        // A fresh install, or a process whose relay never published: there is
        // no file at all, and that must not be confused with a real report
        // full of zeros - RelayVerdict.evaluate treats those completely
        // differently (Off vs. a report to evaluate).
        assertNull(store().read())
    }

    // ---- staleness survives the round trip ---------------------------------

    @Test
    fun `a persisted record older than the staleness threshold reads back as stale`() {
        val path = File(tmp.newFolder(), "relay_report.json")
        val writer = RelayReportFile(path)
        val writtenAt = 1_000_000L
        writer.publish(reportAt(writtenAt))

        val reader = RelayReportFile(path)
        val back = reader.read()
        assertNotNull(back)

        val longAfter = writtenAt + RelayReport.STALENESS_MS + 1
        assertTrue(
            "a record from before STALENESS_MS must read as stale, not current",
            back!!.isStale(longAfter),
        )
    }

    @Test
    fun `a persisted record within the staleness threshold reads back as current`() {
        val path = File(tmp.newFolder(), "relay_report.json")
        val writer = RelayReportFile(path)
        val writtenAt = 1_000_000L
        writer.publish(reportAt(writtenAt))

        val reader = RelayReportFile(path)
        val back = reader.read()
        assertNotNull(back)

        val shortlyAfter = writtenAt + 1_000
        assertFalse(back!!.isStale(shortlyAfter))
    }

    // ---- absent vs. stale are distinguishable ------------------------------

    @Test
    fun `absent and stale are different, distinguishable outcomes`() {
        val nothingPersisted = store().read()

        val stalePath = File(tmp.newFolder(), "relay_report.json")
        val stale = RelayReportFile(stalePath)
        val writtenAt = 1_000_000L
        stale.publish(reportAt(writtenAt))
        val backFromDisk = RelayReportFile(stalePath).read()

        assertNull("nothing published ever must read as null", nothingPersisted)
        assertNotNull(
            "a stale-but-real record must still come back as a report, not null",
            backFromDisk,
        )
        assertTrue(backFromDisk!!.isStale(writtenAt + RelayReport.STALENESS_MS + 1))
    }

    // ---- write volume: throttled, but never on the first or a clear -------

    @Test
    fun `two publishes inside the interval only write to disk once`() {
        val path = File(tmp.newFolder(), "relay_report.json")
        val s = RelayReportFile(path, persistIntervalMs = 10_000L)
        s.publish(reportAt(0L, upBytes = 111L))
        s.publish(reportAt(4_000L, upBytes = 222L)) // inside the 10s window - dropped

        val back = RelayReportFile(path).read()
        assertEquals("the throttled write must not have landed", 111L, back!!.stats.upBytes)
    }

    @Test
    fun `a publish past the interval reaches disk`() {
        val path = File(tmp.newFolder(), "relay_report.json")
        val s = RelayReportFile(path, persistIntervalMs = 10_000L)
        s.publish(reportAt(0L, upBytes = 111L))
        s.publish(reportAt(11_000L, upBytes = 222L))

        val back = RelayReportFile(path).read()
        assertEquals(222L, back!!.stats.upBytes)
    }

    @Test
    fun `the very first publish always writes, regardless of the interval`() {
        val s = store(persistIntervalMs = 1_000_000L)
        s.publish(reportAt(System.currentTimeMillis()))
        assertNotNull(
            "a process that just started relaying must not wait out the throttle " +
                "before anything lands on disk",
            s.read(),
        )
    }

    // ---- a clean stop clears the persisted record --------------------------

    @Test
    fun `clear deletes the persisted record`() {
        val path = File(tmp.newFolder(), "relay_report.json")
        val s = RelayReportFile(path)
        s.publish(reportAt(System.currentTimeMillis()))
        assertNotNull(s.read())

        s.clear()

        assertNull(
            "a clean stop must leave no corpse for a widget to read",
            RelayReportFile(path).read(),
        )
    }

    @Test
    fun `clear is never throttled`() {
        val s = store(persistIntervalMs = 1_000_000L)
        s.publish(reportAt(0L))
        s.clear()
        // clear() must win even though the throttle window from the publish()
        // above has not elapsed - a stop must not be swallowed as "too soon".
        assertNull(s.read())
    }

    // ---- corrupt or foreign-shaped data reads as absent, not thrown -------

    @Test
    fun `an unparsable file reads as absent rather than throwing`() {
        val dir = tmp.newFolder()
        val f = File(dir, "relay_report.json")
        f.writeText("not json at all")
        assertNull(RelayReportFile(f).read())
    }

    @Test
    fun `a record from a different version reads as absent`() {
        val dir = tmp.newFolder()
        val f = File(dir, "relay_report.json")
        f.writeText("""{"version":99,"updatedAtMs":1}""")
        assertNull(
            "a future, incompatible shape must not be misread as today's fields",
            RelayReportFile(f).read(),
        )
    }
}
