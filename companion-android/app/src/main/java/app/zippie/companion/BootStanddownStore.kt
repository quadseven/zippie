package app.zippie.companion

import android.content.Context
import android.util.Log
import java.io.File

/**
 * The last boot stand-down, persisted so the home screen can say why the
 * phone is not contributing (zippie#180 AC3).
 *
 * DEVICE-PROTECTED STORAGE, like BootLog: BootReceiver runs on
 * LOCKED_BOOT_COMPLETED, when credential-protected storage does not exist
 * yet - a record written there would throw or silently vanish for exactly
 * the window that matters.
 *
 * Best-effort throughout: a display record must never be the reason a boot
 * fails. The serialization itself is pure and proven in BootStanddownTest;
 * this object is only the file around it.
 */
object BootStanddownStore {
    private const val TAG = "ZippieBootStanddown"
    private const val NAME = "boot_standdown.txt"

    private fun file(context: Context): File = File(
        context.applicationContext.createDeviceProtectedStorageContext().filesDir,
        NAME,
    )

    fun save(context: Context, standdown: BootStanddown) {
        try {
            file(context).writeText(standdown.serialize())
        } catch (e: Throwable) {
            Log.w(TAG, "could not save boot stand-down", e)
        }
    }

    fun load(context: Context): BootStanddown? = try {
        val f = file(context)
        if (!f.exists()) null else BootStanddown.deserialize(f.readText())
    } catch (e: Throwable) {
        Log.w(TAG, "could not load boot stand-down", e)
        null
    }

    fun clear(context: Context) {
        try {
            file(context).delete()
        } catch (e: Throwable) {
            Log.w(TAG, "could not clear boot stand-down", e)
        }
    }
}
