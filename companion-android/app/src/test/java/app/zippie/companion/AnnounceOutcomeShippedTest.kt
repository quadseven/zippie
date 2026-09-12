package app.zippie.companion

import java.io.File
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The announce outcome must reach Datadog, not only logcat (#18).
 *
 * WHY THIS MATTERS. logcat resets its buffer every boot and is unreachable
 * from outside the device exactly when the router is dark - the same
 * reasoning [CellularLogShipper]'s own header comment gives for shipping over
 * a cellular-pinned network handle in the first place. Before this, a failed
 * announcement during the cold-start deadlock (#18) was visible only to
 * someone watching logcat live at the exact moment it happened, which is why
 * the 2026-08-29 incident took a full afternoon to reconstruct from the
 * router's side instead of a Datadog query from the phone's.
 *
 * Source-text assertion because `RelayService` is an Android `Service` with
 * no test target of its own (#48) - the same crude tripwire pattern
 * [ConfigSourceWiringTest] already uses for this file.
 */
class AnnounceOutcomeShippedTest {

    private fun source(relative: String): String {
        var dir: File? = File("").absoluteFile
        while (dir != null) {
            val f = File(dir, relative)
            if (f.isFile) return f.readText()
            dir = dir.parentFile
        }
        throw AssertionError("cannot find $relative - if it moved, move this check with it")
    }

    @Test
    fun `onAnnounced ships the outcome, not only logcat`() {
        val text = source("app/src/main/java/app/zippie/companion/RelayService.kt")
        val body = text.substringAfter("private fun onAnnounced(")
            .substringBefore("\n    }")
        assertTrue(
            "onAnnounced no longer calls ship(...) - an announce failure during " +
                "the cold-start deadlock would again be visible only to someone " +
                "watching logcat live",
            body.contains("ship(text,"),
        )
        assertTrue(
            "the shipped event has no queryable outcome kind - a Datadog facet " +
                "on announce_outcome needs this field",
            body.contains("\"announce_outcome\""),
        )
    }
}
