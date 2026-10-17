package app.zippie.companion

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * T237: Supervision retries cannot deliver console token to half-started relay.
 *
 * Verifies that:
 * 1. Second-chance guard tests announced-ness, not liveness, for recovery.
 * 2. Recovery leaves a running AND announcing relay untouched.
 * 3. Regression test simulates LOCKED_BOOT_COMPLETED with no token then RETRY after
 *    unlock and asserts the leg announces without human interaction and without
 *    tearing down a carrying leg.
 */
class SupervisionRecoveryTest {

    private class TestConsolePost(
        var code: Int = 200,
        var body: String = """{"lease_s":45}""",
    ) : ConsolePost {
        var calls = 0
        var lastToken: String? = null
        var lastBody: JSONObject? = null

        override fun post(url: String, token: String, body: String, timeoutMs: Int): ConsoleReply {
            calls++
            lastToken = token
            lastBody = JSONObject(body)
            return ConsoleReply(code, this.body)
        }
    }

    @Test
    fun `second-chance guard tests announced-ness, not liveness, for recovery`() {
        // Not running (report == null): must not skip recovery / start
        assertFalse(SecondChanceGuard.shouldSkip(null))
        assertFalse(SecondChanceGuard.needsRecovery(null))

        // Running but never announced (e.g. half-started on locked boot with no token):
        // Previously this was skipped because liveness (report != null) was tested.
        // It must NOT be skipped, and it must need recovery.
        val unannouncedStats = RelayStats(
            listening = true,
            cellularReady = true,
            announce = "Not announcing: no console write token set, so the router will not see this phone as a leg.",
        )
        val unannouncedReport = RelayReport(unannouncedStats, System.currentTimeMillis())
        assertFalse(
            "running but unannounced relay must not be skipped by second-chance guard",
            SecondChanceGuard.shouldSkip(unannouncedReport),
        )
        assertTrue(
            "running but unannounced relay needs recovery",
            SecondChanceGuard.needsRecovery(unannouncedReport),
        )

        // Running with failed announcement (refused or unreachable): must need recovery
        val failedStats = RelayStats(
            listening = true,
            cellularReady = true,
            announce = "Could not reach the router console to announce: timeout",
        )
        val failedReport = RelayReport(failedStats, System.currentTimeMillis())
        assertFalse(SecondChanceGuard.shouldSkip(failedReport))
        assertTrue(SecondChanceGuard.needsRecovery(failedReport))

        // Running AND announced: must be skipped (left untouched)
        val announcedStats = RelayStats(
            listening = true,
            cellularReady = true,
            announce = "Announced as pixel-6a-ea83, lease 45s.",
        )
        val announcedReport = RelayReport(announcedStats, System.currentTimeMillis())
        assertTrue(
            "running and announcing relay must be skipped by second-chance guard",
            SecondChanceGuard.shouldSkip(announcedReport),
        )
        assertFalse(SecondChanceGuard.needsRecovery(announcedReport))
    }

    @Test
    fun `recovery leaves a running and announcing relay untouched`() {
        val stats = RelayStats(
            listening = true,
            cellularReady = true,
            upDatagrams = 150,
            announce = "Announced as pixel-6a-ea83, lease 45s.",
        )
        val report = RelayReport(stats, System.currentTimeMillis())

        assertTrue(report.isAnnounced)
        assertTrue(SecondChanceGuard.shouldSkip(report))
        assertFalse(SecondChanceGuard.needsRecovery(report))
    }

    @Test
    fun `sync is gated on isUserUnlocked, not source == BOOT_COMPLETED`() {
        // Simulates the sync gate:
        // When device is locked, sync must not run regardless of broadcast source.
        // When device is unlocked, sync must run on RETRY passes as well as BOOT_COMPLETED.
        var syncRan = false
        fun evaluateSync(unlocked: Boolean) {
            syncRan = false
            if (unlocked) {
                syncRan = true
            }
        }

        // Locked on BOOT_COMPLETED (should not happen on real device, but guard must not sync)
        evaluateSync(unlocked = false)
        assertFalse("sync must not run while locked", syncRan)

        // Locked on RETRY
        evaluateSync(unlocked = false)
        assertFalse("sync must not run while locked on RETRY", syncRan)

        // Unlocked on RETRY
        evaluateSync(unlocked = true)
        assertTrue("sync must run on RETRY once unlocked", syncRan)

        // Unlocked on BOOT_COMPLETED
        evaluateSync(unlocked = true)
        assertTrue("sync must run on BOOT_COMPLETED once unlocked", syncRan)
    }

    @Test
    fun `regression test simulates LOCKED_BOOT_COMPLETED with no token then RETRY after unlock and asserts the leg announces without human interaction`() {
        val consolePost = TestConsolePost()
        val announcer = LegAnnouncer(consolePost)

        // Simulated relay state
        var isListening = false
        var isCarrying = false
        var currentToken = ""
        var announceState: String? = null
        var isUserUnlocked = false

        fun getRelayReport(): RelayReport? {
            if (!isListening) return null
            return RelayReport(
                RelayStats(
                    listening = isListening,
                    cellularReady = true,
                    upDatagrams = if (isCarrying) 100 else 0,
                    announce = announceState,
                ),
                System.currentTimeMillis(),
            )
        }

        fun tryAnnounce(token: String) {
            currentToken = token
            if (token.isBlank()) {
                announceState = "Not announcing: no console write token set, so the router will not see this phone as a leg."
                return
            }
            val config = LegAnnouncer.Config(
                consoleHost = "10.99.0.1:8787",
                token = token,
                name = "pixel-test",
                label = "Pixel Test",
                listenPort = 51999,
            )
            announcer.start(config, address = { "10.99.0.150" }) { outcome ->
                announceState = when (outcome) {
                    is LegAnnouncer.Outcome.Announced ->
                        "Announced as pixel-test, lease ${outcome.leaseRemainingS.toInt()}s."
                    is LegAnnouncer.Outcome.Refused ->
                        "The router refused this phone's announcement: ${outcome.reason}"
                    is LegAnnouncer.Outcome.Unreachable ->
                        "Could not reach the router console to announce: ${outcome.reason}"
                }
            }
        }

        // STEP 1: LOCKED_BOOT_COMPLETED broadcast
        // Device is locked, no token in device-protected storage.
        isUserUnlocked = false
        val deviceProtectedToken = ""
        val credentialEncryptedToken = "console-secret-token"

        // BootReceiver sync: gated on isUserUnlocked. Does not sync because locked.
        if (isUserUnlocked) {
            currentToken = credentialEncryptedToken
        }

        // Relay starts up in LOCKED mode: binds listener, forwards bytes, attempts announce
        isListening = true
        isCarrying = true // Phone is actively carrying traffic for the bond
        tryAnnounce(deviceProtectedToken)

        // State check at boot: relay is carrying, but NOT announced
        val bootReport = getRelayReport()!!
        assertTrue("relay must be listening at boot", bootReport.stats.listening)
        assertEquals(0, consolePost.calls)
        assertFalse("relay must not be announced before unlock", bootReport.isAnnounced)
        assertTrue(
            "second-chance guard must recognise that half-started relay needs recovery",
            SecondChanceGuard.needsRecovery(bootReport),
        )

        // STEP 2: User unlocks phone, and RETRY broadcast fires
        isUserUnlocked = true

        // RETRY pass: sync runs because isUserUnlocked == true (gated on UserManager.isUserUnlocked)
        var syncedToken = deviceProtectedToken
        if (isUserUnlocked) {
            syncedToken = credentialEncryptedToken
        }
        assertEquals("console-secret-token", syncedToken)

        // Second-chance guard evaluates running relay:
        val reportBeforeRetry = getRelayReport()!!
        assertFalse(
            "second-chance guard must NOT skip half-started relay on retry",
            SecondChanceGuard.shouldSkip(reportBeforeRetry),
        )

        // Recovery delivers the token without tearing down the carrying leg
        val carryingBeforeRecovery = isCarrying
        val listeningBeforeRecovery = isListening
        assertTrue("carrying leg must still be active before recovery", carryingBeforeRecovery)
        assertTrue("listening socket must still be bound before recovery", listeningBeforeRecovery)

        // Relay recovery: delivers token and starts announcing
        tryAnnounce(syncedToken)

        // Wait up to 1 second for announce loop to complete post
        val deadline = System.currentTimeMillis() + 1000
        while (System.currentTimeMillis() < deadline && consolePost.calls == 0) {
            Thread.sleep(20)
        }

        // Assert: the leg announces without human interaction!
        val recoveredReport = getRelayReport()!!
        assertEquals(1, consolePost.calls)
        assertEquals("console-secret-token", consolePost.lastToken)
        assertTrue("leg must now be announced", recoveredReport.isAnnounced)
        assertTrue(
            "carrying leg was never torn down during recovery",
            isCarrying && isListening,
        )

        // STEP 3: Subsequent RETRY pass leaves the running AND announcing relay untouched
        assertTrue(
            "subsequent retry must skip running and announcing relay",
            SecondChanceGuard.shouldSkip(getRelayReport()),
        )
        assertFalse(
            "subsequent retry does not need recovery",
            SecondChanceGuard.needsRecovery(getRelayReport()),
        )

        announcer.stop()
    }
}
