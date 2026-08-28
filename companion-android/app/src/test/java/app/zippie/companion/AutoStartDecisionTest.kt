package app.zippie.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class AutoStartDecisionTest {

    @Test
    fun `a configured managed phone starts without a tap`() {
        // The whole point: this is the case that has never worked.
        assertTrue(
            AutoStartDecision.decide(
                managedAutoStart = true, alreadyRunning = false,
                hasUsableConfig = true).shouldStart)
    }

    @Test
    fun `an unset flag never starts a relay`() {
        // Absent means no. A phone must not begin spending somebody's cellular
        // plan because a key was missing.
        val v = AutoStartDecision.decide(
            managedAutoStart = false, alreadyRunning = false, hasUsableConfig = true)
        assertFalse(v.shouldStart)
        assertTrue((v as AutoStartDecision.Stand).reason.contains("not set"))
    }

    @Test
    fun `an already running relay is left alone`() {
        // Starting a live service is a no-op that logs like a success - the
        // exact shape that hid the frozen relay for three outages.
        assertFalse(
            AutoStartDecision.decide(
                managedAutoStart = true, alreadyRunning = true,
                hasUsableConfig = true).shouldStart)
    }

    @Test
    fun `no usable config refuses rather than relaying into the void`() {
        val v = AutoStartDecision.decide(
            managedAutoStart = true, alreadyRunning = false, hasUsableConfig = false)
        assertFalse(v.shouldStart)
        assertTrue((v as AutoStartDecision.Stand).reason.contains("console"))
    }

    @Test
    fun `every stand carries a reason, because silence is what hid this bug`() {
        // The handset that found this logged NOTHING - no refusal, no reason -
        // and that absence was the only evidence available.
        val stands = listOf(
            AutoStartDecision.decide(false, false, true),
            AutoStartDecision.decide(true, true, true),
            AutoStartDecision.decide(true, false, false),
        )
        stands.forEach {
            assertTrue(it is AutoStartDecision.Stand)
            assertTrue((it as AutoStartDecision.Stand).reason.isNotBlank())
        }
        assertEquals(3, stands.size)
    }
}

/**
 * The wake door is exported and unguarded because the only idiomatic guard,
 * BIND_DEVICE_ADMIN, is platform-signed and no device-owner app can hold it -
 * and a broadcast to a receiver whose permission the sender lacks is dropped
 * SILENTLY, which would look exactly like the wake never being sent.
 *
 * What stops an unguarded door being a battery drain is this.
 */
class WakeDebounceTest {

    @Test
    fun `the first wake is always answered`() {
        assertTrue(WakeDebounce.shouldAnswer(lastAnsweredAtMs = null, nowMs = 1_000))
    }

    @Test
    fun `a repeat inside the window is dropped`() {
        assertFalse(WakeDebounce.shouldAnswer(1_000, 1_000 + WakeDebounce.WINDOW_MS - 1))
    }

    @Test
    fun `a wake after the window is answered`() {
        assertTrue(WakeDebounce.shouldAnswer(1_000, 1_000 + WakeDebounce.WINDOW_MS))
    }

    @Test
    fun `a backwards clock does not lock the door until it catches up`() {
        // Unattended for weeks, NTP corrections happen. Refusing every wake in
        // the meantime is worse than answering one extra time.
        assertTrue(WakeDebounce.shouldAnswer(lastAnsweredAtMs = 5_000, nowMs = 1_000))
    }

    @Test
    fun `the window is generous enough that a real caller never notices`() {
        // A legitimate MDM wakes this after an install or a policy change -
        // minutes apart at best. The window exists for loops, not for callers.
        assertTrue(WakeDebounce.WINDOW_MS >= 30_000)
    }
}
