package app.zippie.companion

import java.io.File
import java.time.Instant
import java.time.ZoneId
import java.util.Locale
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Pins #238 acceptance criterion 3: a phone standing down on budget displays
 * the reason and when it will reconsider on its own screen.
 *
 * #246 wired the text inline into the StatusScreen composable, which no JVM
 * unit test can render, so the criterion went unproven (#255). The line now
 * lives in [BootConfigStore.BudgetStandDown.statusLine], asserted here, and a
 * source check pins that the screen draws that line rather than its own copy -
 * the same split BatteryExemptionWarningTest and ConfigSourceWiringTest use.
 */
class BudgetStandDownStatusTest {

    private val reason =
        "Monthly data cap reached (500 MB of 500 MB). Relaying resumes next month."

    @Test
    fun `the status line carries the stand-down reason and the reconsideration time`() {
        val standDown = BootConfigStore.BudgetStandDown(
            reason = reason,
            reconsiderAtEpochMs = Instant.parse("2026-11-01T00:00:00Z").toEpochMilli(),
        )
        assertEquals(
            "Relay paused: $reason Rechecking at Nov 1, 00:00 UTC.",
            standDown.statusLine(ZoneId.of("UTC"), Locale.US),
        )
    }

    @Test
    fun `the reconsideration time is shown in the phone's own zone`() {
        // The boundary is a local midnight; a phone in New York must not be
        // told a UTC wall-clock time it would misread by five hours.
        val standDown = BootConfigStore.BudgetStandDown(
            reason = reason,
            reconsiderAtEpochMs = Instant.parse("2026-10-16T04:00:00Z").toEpochMilli(),
        )
        val line = standDown.statusLine(ZoneId.of("America/New_York"), Locale.US)
        assertTrue(line, line.endsWith("Rechecking at Oct 16, 00:00 EDT."))
    }

    @Test
    fun `the status screen draws the tested line`() {
        val screen = source("app/src/main/java/app/zippie/companion/StatusScreen.kt")
        assertTrue(
            "StatusScreen must render BudgetStandDown.statusLine() so the tested text is the shown text",
            screen.contains(".statusLine("),
        )
        assertTrue(
            "StatusScreen must not build its own copy of the stand-down line",
            !screen.contains("Relay paused:"),
        )
        val activity = source("app/src/main/java/app/zippie/companion/MainActivity.kt")
        assertTrue(
            "MainActivity must hand the recorded stand-down to StatusScreen",
            activity.contains("BootConfigStore.budgetStandDown("),
        )
    }

    private fun source(relative: String): String {
        var dir: File? = File("").absoluteFile
        while (dir != null) {
            val f = File(dir, relative)
            if (f.isFile) return f.readText()
            dir = dir.parentFile
        }
        throw AssertionError("cannot find $relative - if it moved, move this check with it")
    }
}
