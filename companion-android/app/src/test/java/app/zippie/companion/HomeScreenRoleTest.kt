package app.zippie.companion

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Whether this phone offers itself as a home screen (#222).
 *
 * The decision is tested rather than the plumbing, because the failure being
 * guarded against is a decision error. An enabled CATEGORY_HOME component makes
 * Android ask the user which home app to use the next time HOME is pressed, and
 * the phone carrying this household's traffic must never start asking that
 * because it took an app update. So "absent means no" is not a default, it is
 * the safety property, and it is what these tests exist to pin.
 */
class HomeScreenRoleTest {

    @Test
    fun `a configuration that never mentions the key means no`() {
        // THE ONE THAT MATTERS. Every phone in the field today is this case,
        // including the leg carrying the bond. If this ever returns true, an
        // app update changes the home screen of a phone nobody is holding.
        assertFalse(HomeScreenRole.shouldOfferHomeScreen(emptyMap()))
    }

    @Test
    fun `other keys present but not this one still means no`() {
        val managed = mapOf(
            ManagedConfig.KEY_HOME_HOST to "example.test",
            ManagedConfig.KEY_AUTO_START to true,
        )
        assertFalse(HomeScreenRole.shouldOfferHomeScreen(managed))
    }

    @Test
    fun `an explicit true offers the home screen`() {
        assertTrue(HomeScreenRole.shouldOfferHomeScreen(
            mapOf(ManagedConfig.KEY_HOME_SCREEN_MODE to true)))
    }

    @Test
    fun `an explicit false does not`() {
        assertFalse(HomeScreenRole.shouldOfferHomeScreen(
            mapOf(ManagedConfig.KEY_HOME_SCREEN_MODE to false)))
    }

    @Test
    fun `the string true is accepted, because some channels carry no booleans`() {
        assertTrue(HomeScreenRole.shouldOfferHomeScreen(
            mapOf(ManagedConfig.KEY_HOME_SCREEN_MODE to "true")))
        assertTrue(HomeScreenRole.shouldOfferHomeScreen(
            mapOf(ManagedConfig.KEY_HOME_SCREEN_MODE to "TRUE")))
    }

    @Test
    fun `values that merely look affirmative are refused`() {
        // Not guessed at. "1" and "yes" are plausible intentions and neither is
        // worth acting on when the consequence is the phone's home screen - a
        // wrong yes is disruptive and a wrong no changes nothing.
        for (v in listOf("1", "yes", "on", "enabled", "Y")) {
            assertFalse("'$v' must not enable the home screen",
                HomeScreenRole.shouldOfferHomeScreen(
                    mapOf(ManagedConfig.KEY_HOME_SCREEN_MODE to v)))
        }
    }

    @Test
    fun `a null or an unexpected type means no`() {
        assertFalse(HomeScreenRole.shouldOfferHomeScreen(
            mapOf(ManagedConfig.KEY_HOME_SCREEN_MODE to null)))
        assertFalse(HomeScreenRole.shouldOfferHomeScreen(
            mapOf(ManagedConfig.KEY_HOME_SCREEN_MODE to 1)))
    }

    @Test
    fun `the key is registered, or the MDM can never deliver it`() {
        // A key the app reads but does not publish in KEYS is one an operator
        // cannot set and no MDM will show - unit-tested and never wired.
        assertTrue(ManagedConfig.KEYS.contains(ManagedConfig.KEY_HOME_SCREEN_MODE))
    }
}
