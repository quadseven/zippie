package app.zippie.companion

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log

/**
 * React when the MDM changes this app's configuration.
 *
 * WHY IT MATTERS HERE MORE THAN USUAL. Reading restrictions at startup is
 * enough for an app somebody opens. This one is meant never to be opened: the
 * whole point of managed configuration is that a phone can be enrolled, handed
 * a token, and left alone. Without this receiver a pushed token would sit
 * unread until the process happened to restart - which on a device nobody
 * touches could be never, and would look exactly like the MDM setting being
 * ignored.
 *
 * `ACTION_APPLICATION_RESTRICTIONS_CHANGED` is delivered only to receivers
 * registered at RUNTIME - a manifest-declared receiver never fires. That is why
 * this is registered from the relay service rather than in AndroidManifest.xml,
 * and it is the mistake worth documenting because the manifest version fails
 * silently: correct-looking code, no error, nothing delivered.
 *
 * Deliberately does NOT start the relay. It re-reads configuration and lets the
 * announcer pick it up; starting on a policy change would mean a restriction
 * edit could turn on cellular relaying with no other signal.
 */
class RestrictionsChangedReceiver(
    private val onChanged: (RelayConfiguration) -> Unit,
) : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_APPLICATION_RESTRICTIONS_CHANGED) return
        val cfg = AndroidManagedConfig.effectiveConfig(context)
        // The KEYS that were set, never their values - one of them is the
        // router's write token, and a log line is forever.
        Log.i(TAG, "managed configuration changed; keys set: " +
            ManagedConfig.managedKeys(AndroidManagedConfig.read(context)))
        // Applied here as well as at service start, because the operator can
        // turn it on for a phone that is already running and should not have to
        // reboot it to take effect (#222). Reversible: clearing the key
        // withdraws the role again.
        HomeScreenRole.apply(context, AndroidManagedConfig.read(context))
        onChanged(cfg)
    }

    companion object {
        private const val TAG = "ZippieManagedConfig"
    }
}
