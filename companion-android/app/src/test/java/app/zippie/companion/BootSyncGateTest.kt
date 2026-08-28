package app.zippie.companion

import java.io.File
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The boot config mirror must be refreshed whenever the device is UNLOCKED,
 * never gated on which broadcast happened to wake the receiver (zippie#255).
 *
 * THE TRAP THIS CLOSES, and it had no exit. A phone that half-starts on
 * `LOCKED_BOOT_COMPLETED` relays bytes without a console token and cannot
 * announce - the class doc says so outright: "relays, does not announce, until
 * something else restarts it." `BOOT_COMPLETED`, the second chance, returns
 * early because the relay is already running. Supervision then re-enters every
 * ~15 minutes carrying `source = RETRY`, which is not `BOOT_COMPLETED`, so the
 * sync was skipped on every pass forever.
 *
 * The phone was unlocked the entire time. The one call that would have fixed it
 * was gated on the name of a broadcast that had already been and gone. Observed
 * on the Pixel 6a on 2026-08-11: enrolled, configured, carrying nothing until a
 * human opened the app and tapped start.
 *
 * The original reasoning was correct and only its TEST was wrong: sync reads
 * credential-encrypted storage, which is unreadable before first unlock. That is
 * a fact about the lock state. It was being asked as a question about the
 * broadcast.
 *
 * Source-text assertions because the app target has no test target (#48), the
 * same trade-off as ConfigSourceWiringTest and LegSocketPinTest.
 */
class BootSyncGateTest {

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

    /** Comments stripped before asserting what the code USES. This file
     *  explains the mistake at length, and a substring check that cannot tell a
     *  caution from a call is worse than no check - learned on LegSocketPinTest,
     *  whose first version fired on its own warning. */
    private fun codeOnly(text: String): String =
        text.replace(Regex("""/\*.*?\*/""", RegexOption.DOT_MATCHES_ALL), "")
            .lineSequence()
            .joinToString("\n") { line -> line.substringBefore("//") }

    private fun decideBody(): String {
        val s = codeOnly(source())
        val start = s.indexOf("private suspend fun decide(")
        assertTrue("decide() is gone from BootReceiver - move this check with it", start >= 0)
        val rest = s.substring(start)
        val end = rest.indexOf("\n    private fun ")
        return if (end < 0) rest else rest.substring(0, end)
    }

    @Test
    fun `the mirror refresh consults the lock state`() {
        val body = decideBody()
        assertTrue(
            "sync must be gated on whether the user is unlocked, which is the " +
                "actual precondition for reading credential-encrypted storage",
            body.contains("isUserUnlocked"),
        )
    }

    @Test
    fun `the mirror refresh is not gated on which broadcast arrived`() {
        val body = decideBody()
        assertFalse(
            "gating sync on source == BOOT_COMPLETED is #255: supervision re-enters " +
                "as RETRY forever and never syncs, on a phone that has been unlocked " +
                "for hours",
            Regex("""if\s*\(\s*source\s*==\s*BOOT_COMPLETED\s*\)""").containsMatchIn(body),
        )
    }

    @Test
    fun `an unreadable lock state falls back rather than losing the old behaviour`() {
        // STRICTLY ADDITIVE. If the lock state cannot be read, the old rule must
        // still apply - this change may only ever create sync opportunities,
        // never remove one that exists today.
        val body = decideBody()
        assertTrue(
            "the null branch must fall back to the previous BOOT_COMPLETED rule",
            body.contains("BOOT_COMPLETED"),
        )
    }
}
