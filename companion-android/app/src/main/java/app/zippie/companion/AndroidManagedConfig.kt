package app.zippie.companion

import android.content.Context
import android.content.RestrictionsManager
import android.os.Bundle

/**
 * The Android half of managed configuration: read the restrictions Bundle and
 * hand it to [ManagedConfig] as a plain map.
 *
 * DELIBERATELY DECIDES NOTHING. Every rule about precedence, blank values and
 * port validity lives in [ManagedConfig], where the JVM unit tests reach it.
 * This file exists only because `RestrictionsManager` cannot be constructed off
 * a device, and it is kept to a shape where being wrong is obvious on sight.
 */
object AndroidManagedConfig {

    /** The restrictions this app has been given, flattened. Empty when the
     *  device is unmanaged or no policy has been set - both ordinary. */
    fun read(context: Context): Map<String, Any?> {
        val rm = context.getSystemService(Context.RESTRICTIONS_SERVICE) as? RestrictionsManager
            ?: return emptyMap()
        val b: Bundle = rm.applicationRestrictions ?: return emptyMap()
        return ManagedConfig.KEYS.mapNotNull { k ->
            if (b.containsKey(k)) k to b.get(k) else null
        }.toMap()
    }

    /**
     * Configuration with MDM policy applied over stored values.
     *
     * The single call site everything else should use, so no code path can read
     * stored configuration and miss the policy on top of it - which would look
     * like the MDM setting being ignored at random.
     */
    fun effectiveConfig(context: Context): RelayConfiguration =
        ManagedConfig.merge(RelayConfiguration.load(context), read(context))

    fun shouldAutoStart(context: Context): Boolean = ManagedConfig.autoStart(read(context))
}
