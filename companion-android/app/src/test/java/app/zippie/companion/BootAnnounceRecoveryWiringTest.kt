package app.zippie.companion

import java.io.File
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * #179, the wiring half: the pure announced-ness decision is pinned by
 * AnnounceRecoveryTest; this pins that BootReceiver actually USES it.
 *
 * Source-text assertions because the app target has no test target (#48),
 * the same trade-off as BootSyncGateTest and ConfigSourceWiringTest - the
 * receiver needs a real Context (goAsync, AlarmManager, UserManager), and
 * this module's tests run against the stub android.jar.
 *
 * The regression scenario, as code shape: LOCKED_BOOT_COMPLETED starts a
 * relay with no token (credential-encrypted storage is unreadable), so the
 * relay is RUNNING but never announced. BOOT_COMPLETED must not return early
 * on liveness, and start() must turn its start into a real restart -
 * otherwise the leg stays invisible until a human intervenes, which is the
 * defect.
 */
class BootAnnounceRecoveryWiringTest {

    private val relativePath = "app/src/main/java/app/zippie/companion/BootReceiver.kt"

    private fun source(): String {
        var dir: File? = File("").absoluteFile
        while (dir != null) {
            val f = File(dir, relativePath)
            if (f.isFile) return f.readText()
            dir = dir.parentFile
        }
        throw AssertionError("cannot find $relativePath - if it moved, move this check with it")
    }

    /** Comments stripped before asserting what the code USES - a substring
     *  check that cannot tell a caution from a call is worse than no check
     *  (BootSyncGateTest). */
    private fun codeOnly(text: String): String =
        text.replace(Regex("""/\*.*?\*/""", RegexOption.DOT_MATCHES_ALL), "")
            .lineSequence()
            .joinToString("\n") { line -> line.substringBefore("//") }

    /** The BOOT_COMPLETED branch, isolated - PACKAGE_REPLACED keeps its own
     *  liveness guard on purpose (an update is not the boot trap), so the
     *  assertions below must not see it. */
    private fun bootCompletedBranch(): String {
        val code = codeOnly(source())
        val start = code.indexOf("Intent.ACTION_BOOT_COMPLETED")
        assertTrue("BOOT_COMPLETED branch is gone from BootReceiver - move this check with it", start >= 0)
        val rest = code.substring(start)
        val end = rest.indexOf("Intent.ACTION_MY_PACKAGE_REPLACED")
        return if (end < 0) rest else rest.substring(0, end)
    }

    @Test
    fun `the second-chance guard tests announced-ness, not liveness`() {
        val branch = bootCompletedBranch()
        assertTrue(
            "BOOT_COMPLETED must consult announced-ness (hasAnnounced), not " +
                "just whether a report exists - a half-started relay IS running",
            branch.contains("hasAnnounced"),
        )
        assertFalse(
            "the old liveness-only early return is #179: " +
                "`report.value != null -> return` suppresses the second chance " +
                "for exactly the relay that needs it",
            Regex("""if\s*\(\s*RelayStatusStore\.report\.value\s*!=\s*null\s*\)""")
                .containsMatchIn(branch),
        )
    }

    @Test
    fun `start stops before starting when a token recovery is due`() {
        val code = codeOnly(source())
        assertTrue(
            "start() must consult needsTokenRecovery - a no-op onStartCommand " +
                "on the live service cannot deliver the token",
            code.contains("needsTokenRecovery"),
        )
        assertTrue(
            "the token recovery must stop the live instance first, or the " +
                "start below is not a real start",
            code.contains("RelayService.ACTION_STOP"),
        )
    }

    @Test
    fun `the token recovery only fires while unlocked`() {
        // Restarting while locked cannot produce a token (credential-encrypted
        // storage) and would churn the relay every 15 minutes for as long as
        // the phone sits locked - the fix must not become a battery drain
        // wearing a recovery's clothes.
        val code = codeOnly(source())
        assertTrue(
            "tokenRecovery must be gated on the device being unlocked",
            code.contains("isUserUnlocked"),
        )
    }
}
