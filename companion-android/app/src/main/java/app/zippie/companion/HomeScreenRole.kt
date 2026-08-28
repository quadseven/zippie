package app.zippie.companion

import android.content.ComponentName
import android.content.Context
import android.content.pm.PackageManager
import android.util.Log

/**
 * Whether this phone should offer itself as the home screen (#222).
 *
 * On a dedicated leg - a phone whose entire job is carrying the bond - the
 * status screen IS what a home screen should show: the legs, which one is
 * carrying, how much each has moved. Headwind could set a wallpaper because it
 * is a launcher and draws the home screen itself. Fleet cannot: the Android
 * Management API manages policy and has no field to SET a wallpaper, only
 * `setWallpaperDisabled` to stop the user changing one. Being the launcher
 * ourselves answers that better than porting a wallpaper would.
 *
 * THE DEFAULT IS OFF AND THAT IS THE POINT. An enabled component carrying
 * CATEGORY_HOME makes this app eligible as a home screen, and Android then asks
 * the user which home app to use the next time HOME is pressed. The phone
 * carrying this household's traffic must never start asking that because it
 * took an app update. So the alias ships disabled and only managed
 * configuration turns it on - which means a device that is never sent the key
 * is never affected, including by this file existing.
 */
object HomeScreenRole {
    private const val TAG = "HomeScreenRole"

    /** The manifest alias. Disabled there; this object is the only thing that
     *  ever enables it. */
    private const val ALIAS = "app.zippie.companion.HomeScreenAlias"

    /**
     * The decision, as a pure function of the managed configuration.
     *
     * Separated from the act of applying it so the rule can be tested without
     * a PackageManager. The failure being guarded against - "an app update
     * silently made the phone ask which home screen to use" - is a decision
     * error, not a plumbing error, so the decision is what gets the tests.
     *
     * Absent means NO. A device whose configuration has never mentioned
     * `homeScreenMode` must behave exactly as it did before this existed, so
     * the missing key and an explicit false are the same answer.
     */
    fun shouldOfferHomeScreen(managed: Map<String, Any?>): Boolean =
        when (val v = managed[ManagedConfig.KEY_HOME_SCREEN_MODE]) {
            is Boolean -> v
            // A string arrives when the value came through a channel that has
            // no boolean type. Only an explicit "true" counts; anything else,
            // including "1" or "yes", is not something to guess at when the
            // consequence is the phone's home screen.
            is String -> v.equals("true", ignoreCase = true)
            else -> false
        }

    /**
     * Bring the alias in line with the decision.
     *
     * REVERSIBLE, deliberately. Turning the key off disables the alias again -
     * a one-way switch would mean a phone that was briefly a launcher stays one
     * forever, and the only fix would be a factory reset.
     *
     * DONT_KILL_APP because enabling a component otherwise restarts the process,
     * and this app's process is the relay carrying traffic. Restarting it to
     * change a home-screen setting would drop the leg.
     */
    fun apply(context: Context, managed: Map<String, Any?>) {
        val wanted = shouldOfferHomeScreen(managed)
        val target = if (wanted) {
            PackageManager.COMPONENT_ENABLED_STATE_ENABLED
        } else {
            PackageManager.COMPONENT_ENABLED_STATE_DISABLED
        }
        val component = ComponentName(context.packageName, ALIAS)
        val pm = context.packageManager
        try {
            if (pm.getComponentEnabledSetting(component) == target) return
            pm.setComponentEnabledSetting(component, target, PackageManager.DONT_KILL_APP)
            Log.i(TAG, if (wanted) {
                "offering this phone as a home screen (homeScreenMode=true)"
            } else {
                "no longer offering this phone as a home screen"
            })
        } catch (e: Throwable) {
            // A home-screen preference must never be the reason the relay fails
            // to start. Logged and dropped.
            Log.w(TAG, "could not change the home-screen role", e)
        }
    }
}
