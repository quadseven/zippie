package app.zippie.companion

import java.io.File
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Updating this app must not take the router's uplink away.
 *
 * WHAT HAPPENED. Installing a new APK stops the app's foreground service, and
 * nothing was listening for that. Measured 2026-08-17 during an unattended test
 * run: after `adb install -r`, `dumpsys activity services` showed zero
 * ServiceRecords, the router's `pixel-6a-a554` leg expired off the bond
 * entirely, and the only path back was the supervision ladder's 15-MINUTE
 * steady state. On the one device whose entire job is being someone's only
 * internet, shipping an update was a quarter-hour outage.
 *
 * WHY THIS IS A FILE-PARSING TEST. The fix is two declarations - a manifest
 * intent-filter and a `when` branch - and neither is reachable from a JVM unit
 * test: BroadcastReceiver dispatch needs a real Android runtime. The failure
 * mode worth guarding is somebody deleting one of the two halves and leaving
 * the other, which reads as wired and is not. Parsing the sources catches
 * exactly that, and it is the same approach already used to pin the watchdog's
 * constants against its shell script.
 */
class PackageReplacedTest {

    private fun repoFile(rel: String): File {
        // Tests run with the module directory as CWD.
        val f = File(rel)
        assertTrue("expected to find $rel from ${File(".").absolutePath}", f.isFile)
        return f
    }

    @Test
    fun `the manifest subscribes to MY_PACKAGE_REPLACED`() {
        val manifest = repoFile("src/main/AndroidManifest.xml").readText()
        assertTrue(
            "BootReceiver does not listen for MY_PACKAGE_REPLACED, so updating the " +
                "app leaves the relay stopped until the 15-minute supervision retry",
            manifest.contains("android.intent.action.MY_PACKAGE_REPLACED"),
        )
    }

    @Test
    fun `the receiver actually handles it`() {
        val src = repoFile("src/main/java/app/zippie/companion/BootReceiver.kt").readText()
        assertTrue(
            "the manifest subscribes to MY_PACKAGE_REPLACED but BootReceiver has no " +
                "branch for it - the broadcast would arrive and fall through to Unit",
            src.contains("Intent.ACTION_MY_PACKAGE_REPLACED"),
        )
    }

    @Test
    fun `it does not start a second relay when one is already running`() {
        // The same guard BOOT_COMPLETED carries. A replace that somehow left the
        // service alive must not produce two relays fighting over one LAN port.
        val src = repoFile("src/main/java/app/zippie/companion/BootReceiver.kt").readText()
        val branch = src.substringAfter("Intent.ACTION_MY_PACKAGE_REPLACED")
            .substringBefore("else -> Unit")
        assertTrue(
            "the MY_PACKAGE_REPLACED branch does not check RelayStatusStore first",
            branch.contains("RelayStatusStore.report.value != null"),
        )
    }

    @Test
    fun `the boot actions survive alongside it`() {
        // Adding an action to an intent-filter is a one-line edit next to two
        // others, which is exactly where a careless change drops one. Losing
        // LOCKED_BOOT_COMPLETED would be invisible until the next unattended
        // cold boot - the case this whole project exists for.
        val manifest = repoFile("src/main/AndroidManifest.xml").readText()
        for (action in listOf(
            "android.intent.action.LOCKED_BOOT_COMPLETED",
            "android.intent.action.BOOT_COMPLETED",
            "android.intent.action.MY_PACKAGE_REPLACED",
        )) {
            assertTrue("manifest lost $action", manifest.contains(action))
        }
    }
}
