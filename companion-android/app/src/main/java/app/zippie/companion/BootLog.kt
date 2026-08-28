package app.zippie.companion

import android.content.Context
import android.os.SystemClock
import android.util.Log
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * The phone's own account of a cold boot, on the phone, surviving the reboot
 * (#186).
 *
 * WHY NOT logcat: `logcat -G` is not persistent, so the Pixel is back to a
 * 256 KiB buffer on every boot and has rotated past the interesting minutes
 * within the hour. `setprop persist.logd.size` is denied without root. Every
 * cold-boot diagnosis so far has therefore had to be made from the ROUTER's
 * side, which is the wrong way round - the phone is the half we understand
 * least.
 *
 * WHY NOT CellularLogShipper: that ships to Datadog OVER CELLULAR, and the
 * failure being diagnosed is "cellular never came up". An instrument that needs
 * the thing under test to work cannot report on it failing. The shipper is the
 * right tool when the phone is healthy and the ROUTER is dark; this file is the
 * right tool when the phone itself is the suspect. They are not redundant.
 *
 * DEVICE-PROTECTED STORAGE, so it is writable before first unlock. BootReceiver
 * is directBootAware and runs on LOCKED_BOOT_COMPLETED, when credential-
 * protected storage is not available at all - a log written there would throw
 * or silently vanish for exactly the window that matters. BootConfigStore
 * already had to solve this; this follows it.
 */
object BootLog {
    private const val TAG = "BootLog"

    /**
     * MILLISECONDS SINCE BOOT IS THE LOAD-BEARING NUMBER, not the wall clock.
     *
     * At the point these lines are written the phone may not have reached the
     * network, so the clock may not have been corrected yet - and the whole
     * question being asked is "how long after boot did the relay start", which
     * is a duration, not a date. `elapsedRealtime` is monotonic from boot and
     * cannot be dragged by an NTP correction landing mid-sequence. The wall
     * clock is recorded too, second, because it is what correlates the phone's
     * account with the router's.
     */
    private fun stamp(): String {
        val since = SystemClock.elapsedRealtime()
        val wall = SimpleDateFormat("HH:mm:ss", Locale.US).format(Date())
        return "+%08d %s".format(since, wall)
    }

    private fun file(context: Context): BootLogFile {
        val dir: File = context.applicationContext
            .createDeviceProtectedStorageContext()
            .filesDir
        return BootLogFile.of(dir)
    }

    /**
     * Record one event. Best-effort by construction: see [BootLogFile.append].
     *
     * `tag` is the phase (boot, relay, cellular) and `message` says what
     * happened. Kept as two fields so the file can be scanned for one phase
     * without a parser.
     */
    fun record(context: Context, tag: String, message: String) {
        try {
            val line = "${stamp()} [$tag] $message"
            file(context).append(line)
            // Mirrored to logcat as well, because while the phone is reachable
            // that is still the fastest place to read it. The file is what
            // survives; this is convenience.
            Log.i(TAG, line)
        } catch (e: Throwable) {
            // A logging call must never be the reason a boot fails. Swallowing
            // Throwable is deliberate and is the one place in this app it is.
            Log.w(TAG, "could not record boot log line", e)
        }
    }

    /** Everything recorded, oldest first, for the diagnostics screen. */
    fun readAll(context: Context): List<String> = try {
        file(context).read()
    } catch (e: Throwable) {
        listOf("<boot log unavailable: ${e.javaClass.simpleName}>")
    }

    /** Total bytes on disk across both generations. */
    fun sizeBytes(context: Context): Long = try {
        file(context).sizeBytes()
    } catch (e: Throwable) {
        0L
    }
}
