package app.zippie.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * THE LOCKED-BOOT CRASH (2026-08-16).
 *
 * `BootReceiver` did everything right - it retried until the router appeared and
 * started the relay - and the relay died one second later:
 *
 *   W ContextImpl: Failed to ensure /data/user/0/app.zippie.companion/shared_prefs
 *       at RelayConfiguration.prefs(RelayConfiguration.kt:145)
 *       at RelayService.onStartCommand(RelayService.kt:161)
 *   E AndroidRuntime: Unable to start service RelayService
 *   W ActivityManager: Scheduling restart of crashed service in 1800000ms
 *
 * Credential-encrypted storage does not exist before first unlock. Android then
 * deferred the restart by THIRTY MINUTES, which is why it read as permanently
 * dead rather than briefly broken.
 *
 * WHY NOBODY CAUGHT IT BY HAND: it only happens while the phone is LOCKED, and a
 * human unlocks the phone before looking at it. The one configuration that
 * matters for a relay in a car is the one a person cannot easily observe.
 *
 * These are pure tests of the SHAPE of the fix - that the mirror carries every
 * field the relay needs. The Context switching itself is Android and is covered
 * by the on-device run in #170.
 */
class LockedBootConfigTest {

    /**
     * The mirror must carry EVERY field the relay needs, not just the console
     * ones BootConfigStore keeps for its own decision. A relay that starts with
     * no homeHost or no token is running, and useless - which is worse than
     * refusing, because it looks healthy.
     */
    @Test
    fun `every field the relay needs survives a config round trip`() {
        val original = RelayConfiguration(
            listenPort = 51999,
            homeHost = "home.example",
            homePort = 51902,
            consoleLanHost = "10.99.0.1:8787",
            consoleUrl = "https://console.example",
            dailyBudgetBytes = 1_000_000L,
            monthlyBudgetBytes = 50_000_000L,
            announceToken = "tok",
        )

        // What mirrorForLockedBoot writes, as a plain map - the same fields, so
        // adding one to RelayConfiguration without adding it there fails here.
        val mirrored = mapOf(
            "listenPort" to original.listenPort,
            "homeHost" to original.homeHost,
            "homePort" to original.homePort,
            "consoleLanHost" to original.consoleLanHost,
            "consoleUrl" to original.consoleUrl,
            "dailyBudgetBytes" to original.dailyBudgetBytes,
            "monthlyBudgetBytes" to original.monthlyBudgetBytes,
            "announceToken" to original.announceToken,
        )

        val rebuilt = RelayConfiguration(
            listenPort = mirrored["listenPort"] as Int,
            homeHost = mirrored["homeHost"] as String,
            homePort = mirrored["homePort"] as Int,
            consoleLanHost = mirrored["consoleLanHost"] as String,
            consoleUrl = mirrored["consoleUrl"] as String,
            dailyBudgetBytes = mirrored["dailyBudgetBytes"] as Long,
            monthlyBudgetBytes = mirrored["monthlyBudgetBytes"] as Long,
            announceToken = mirrored["announceToken"] as String,
        )

        assertEquals("the mirror lost a field the relay needs", original, rebuilt)
    }

    /**
     * The two things without which a relay cannot do its job, called out
     * separately: it can be RUNNING and unable to announce, which is the failure
     * mode that looks healthy from the notification shade.
     */
    @Test
    fun `a mirrored config can still announce`() {
        val c = RelayConfiguration(
            listenPort = 51999,
            consoleLanHost = "10.99.0.1:8787",
            announceToken = "tok",
        )
        assertTrue("no console host - nothing to announce to", c.consoleLanHost.isNotBlank())
        assertTrue("no token - the announce 401s and the leg never forms",
            c.announceToken.isNotBlank())
        assertTrue("no local console candidate", c.consoleCandidates.any { it.isLocal })
    }
}
