package app.zippie.companion

import java.io.File
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Every code path that decides whether this phone is CONFIGURED must ask
 * [AndroidManagedConfig.effectiveConfig], never [RelayConfiguration.load].
 *
 * `load()` reads stored SharedPreferences only. The MDM writes
 * `RestrictionsManager`, and `ManagedConfig.merge` is what puts the second on
 * top of the first. `effectiveConfig`'s own doc says it is "the single call
 * site everything else should use, so no code path can read stored
 * configuration and miss the policy on top of it - which would look like the
 * MDM setting being ignored at random."
 *
 * THAT IS EXACTLY WHAT HAPPENED, 2026-08-23, on two handsets. WakeReceiver and
 * MainActivity both read `load()` and stood down with "no usable console
 * configuration to announce to" while `dumpsys device_policy` showed all six
 * keys present. The refusal message was honest; the question was asked of the
 * wrong source. Force-stopping the app made the same configuration usable
 * immediately, which is the signature of a stale read rather than absent data.
 *
 * Source-text assertions because the app target has no test target (#48).
 * Crude, and the only tripwire available for these files.
 */
class ConfigSourceWiringTest {

    private fun source(relative: String): String {
        // Walk up from the working directory rather than assuming it: gradle
        // runs tests from the module dir, and a moved file must fail loudly
        // here rather than quietly stop being watched.
        var dir: File? = File("").absoluteFile
        while (dir != null) {
            val f = File(dir, relative)
            if (f.isFile) return f.readText()
            dir = dir.parentFile
        }
        throw AssertionError("cannot find $relative - if it moved, move this check with it")
    }

    private val decidingFiles = listOf(
        "app/src/main/java/app/zippie/companion/WakeReceiver.kt",
        "app/src/main/java/app/zippie/companion/MainActivity.kt",
    )

    @Test
    fun `the auto-start deciders read effective config, not stored config`() {
        decidingFiles.forEach { path ->
            val text = source(path)
            assertTrue(
                "$path must call AndroidManagedConfig.effectiveConfig - reading " +
                    "RelayConfiguration.load() here means an MDM-provisioned phone " +
                    "reports itself unconfigured while the platform holds every key",
                text.contains("AndroidManagedConfig.effectiveConfig("),
            )
        }
    }

    @Test
    fun `no deciding file reads RelayConfiguration load for the config gate`() {
        decidingFiles.forEach { path ->
            val text = source(path)
            assertTrue(
                "$path is reading RelayConfiguration.load() again. That source is " +
                    "stored-only and the MDM never writes it; see this class's doc " +
                    "for the two handsets it cost.",
                !text.contains("RelayConfiguration.load("),
            )
        }
    }

    @Test
    fun `RelayService still reads effective config too, so all three agree`() {
        // Not a deciding file, but if it ever regressed the three entry points
        // would disagree about whether the same phone is configured.
        val text = source("app/src/main/java/app/zippie/companion/RelayService.kt")
        assertTrue(
            "RelayService must read effectiveConfig",
            text.contains("AndroidManagedConfig.effectiveConfig("),
        )
    }
}
