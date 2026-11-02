package app.zippie.companion

import android.content.SharedPreferences

/** The announce credential has one durable home, available before first unlock. */
internal object AnnounceTokenStore {
    private const val KEY = "announceToken"
    private const val MIGRATED = "announceTokenDeviceAuthority"
    private var unconfirmed = false

    @Synchronized
    fun read(current: SharedPreferences, device: SharedPreferences, locked: Boolean): String {
        if (!locked && current.contains(KEY)) {
            // The old mirror may lag behind the credential copy. Only a value
            // written by this store is authoritative over the old copy.
            // Commit before removing the old copy so a crash cannot lose both.
            val authoritative = device.getBoolean(MIGRATED, false)
            val value = if (authoritative) device.getString(KEY, "") else current.getString(KEY, "")
            val copied = if (authoritative && !unconfirmed) true else
                device.edit().putString(KEY, value).putBoolean(MIGRATED, true).commit()
            unconfirmed = !copied
            if (copied) {
                current.edit().remove(KEY).commit()
            } else {
                return value.orEmpty()
            }
        }
        return device.getString(KEY, "").orEmpty()
    }

    @Synchronized
    fun write(current: SharedPreferences, device: SharedPreferences, locked: Boolean, token: String) {
        // Do not remove the old copy if the new value was not made durable.
        val saved = device.edit().putString(KEY, token).putBoolean(MIGRATED, true).commit()
        unconfirmed = !saved
        if (saved && !locked) current.edit().remove(KEY).commit()
    }
}
