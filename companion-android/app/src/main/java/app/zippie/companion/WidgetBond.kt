package app.zippie.companion

import android.content.Context
import java.io.File
import java.io.IOException
import org.json.JSONException
import org.json.JSONObject

/**
 * The router's own view of the bond, plus the time it was read.
 *
 * WHY A SECOND TIMESTAMP, AND WHY IT IS NOT `RelayReport`'s. The relay report
 * is written every two seconds by the relay's own heartbeat, whether or not the
 * app is in memory. The bond is fetched by the APP every five seconds, over the
 * network, from a router that is also forwarding every packet in the car. They
 * are different evidence with different half-lives, and one threshold for both
 * would be wrong in both directions: a shared one either suppresses a live relay
 * report because the console is slow, or keeps presenting a bond nobody has
 * looked at for ten minutes. So this carries its own age and its own rule.
 *
 * THE CONSEQUENCE, STATED RATHER THAN HIDDEN. This record only advances while
 * the app is in the foreground. A widget the launcher redraws in a fresh
 * process, with the app backgrounded for longer than [STALENESS_MS], therefore
 * finds the snapshot stale and falls back to the single "This phone" row built
 * from the relay report - which IS still fresh, because the relay keeps writing
 * it. That is the honest answer rather than a defect, and it is the same reason
 * `WidgetContent.legsOf` falls back rather than substituting the last good rows.
 */
data class WidgetBondSnapshot(val status: BondStatus, val updatedAtMs: Long) {
    fun isStale(nowMs: Long, thresholdMs: Long = STALENESS_MS): Boolean =
        nowMs - updatedAtMs > thresholdMs

    companion object {
        /**
         * How often the app's own poll is expected to rewrite this record.
         * Pinned to `BondViewModel.CONSOLE_INTERVAL_MS`, which is the number
         * that actually drives the fetch - restated rather than imported
         * because that constant is private to the view model.
         */
        const val REFRESH_INTERVAL_MS = 5_000L

        /**
         * Generous multiple of the refresh, for the same reason
         * `RelayReport.STALENESS_MS` is a multiple of its heartbeat: a console
         * round trip over a busy link is sometimes slow, and flapping the bond
         * list in and out on every hiccup is worse than being a few seconds
         * behind.
         *
         * Still well inside the launcher's own refresh budget for a widget,
         * which is what makes the richer list reachable at all rather than
         * decorative.
         */
        const val STALENESS_MS = 4 * REFRESH_INTERVAL_MS
    }
}

/**
 * Persists [WidgetBondSnapshot] to disk, Android-free so the properties that
 * actually matter - a fresh reader sees the real bond instead of null, a
 * half-written file is never observed, staleness survives the round trip - can
 * be proven on the JVM. Split from the store for the same reason `BootLogFile`
 * is split from `BootLog` and `RelayReportFile` from `RelayStatusStore`: the
 * Android half is one call to `createDeviceProtectedStorageContext` and cannot
 * be unit tested here.
 *
 * WHY THIS EXISTS AT ALL. The bond is fetched by [BondViewModel], which only
 * runs while a screen is on top. A widget updates in whatever process Android
 * happens to start for the broadcast, which is frequently a FRESH one that has
 * never made a console call. A widget built on the in-memory path alone would
 * render the this-phone row forever - or worse, be handed a null bond and
 * quietly agree with the wrong thing.
 *
 * THE ROUTER'S OWN BYTES ARE STORED, NOT A RE-ENCODING OF THEM. [BondStatus]
 * has twenty-odd optional fields, and an encoder written here would be a second
 * definition of the console's shape that could silently drop a field the router
 * does send - the exact class of lie this file exists to prevent. Keeping the
 * payload verbatim means a field added to the agent later needs no change here
 * at all, and [read] decodes it with the same [BondStatus.decode] the screen
 * uses, so the widget and the screen cannot disagree about a field's meaning.
 *
 * NEVER THROWS, same posture as [RelayReportFile] and [BootLogFile]. The
 * publisher is the app's foreground poll, and a full disk must not be the
 * reason the status screen stops updating.
 */
class RelayBondFile(private val file: File) {

    /**
     * Record the bond the router answered with.
     *
     * NOT THROTTLED, unlike [RelayReportFile.publish]. This is already bounded
     * by a five-second console poll made by a foreground screen, so it cannot
     * become the disk-write-every-two-seconds-forever problem that forced the
     * throttle there, and adding one would be a second rule to get wrong for no
     * saved write.
     */
    fun publish(snapshot: WidgetBondSnapshot, rawPayload: String) {
        try {
            writeAtomic(encode(snapshot.updatedAtMs, rawPayload))
        } catch (e: IOException) {
            // Best-effort. A widget that cannot read a bond draws the
            // this-phone row, which is a worse answer rather than a wrong one.
        } catch (e: SecurityException) {
            // ditto
        }
    }

    /**
     * A clean stop. Deletes rather than writing an empty bond, matching
     * [RelayStatusStore.clear] and [WidgetBondSnapshot]'s own reasoning: a
     * console nobody can reach is not the same as a router that answered, and
     * an empty bond reads like the latter.
     *
     * NOT throttled, for the same reason [publish] is not.
     */
    fun clear() {
        try {
            file.delete()
        } catch (e: SecurityException) {
            // Best-effort, same posture as above: a storage layer that refuses a
            // delete must not take the failed-poll path down with it.
        }
    }

    /**
     * What a FRESH process reads. Null when nothing was ever written, the file
     * cannot be read, its contents do not parse, they carry a version this build
     * does not understand, or the embedded payload is not a bond - all of which
     * are "nothing trustworthy on disk" to a caller, not distinct errors worth
     * telling apart. Mirrors [RelayReportFile.read]'s reasoning exactly.
     */
    fun read(): WidgetBondSnapshot? = try {
        if (file.exists()) decode(file.readText()) else null
    } catch (e: IOException) {
        null
    } catch (e: SecurityException) {
        null
    }

    /**
     * Write-to-temp-then-rename, so a reader only ever observes the old
     * complete file or the new complete file. A half-written bond read by a
     * widget would be worse than no bond at all - it would be a list of legs
     * missing whichever path happened to be mid-write.
     */
    private fun writeAtomic(json: String) {
        file.parentFile?.mkdirs()
        val tmp = File(file.parentFile, file.name + ".tmp")
        tmp.writeText(json)
        // Plain rename over the target first, which is an atomic replace on the
        // filesystems backing internal app storage. Falls back to
        // BootLogFile.rotateIfFull's delete-then-rename only if that fails,
        // because renameTo does NOT replace an existing target on every Android
        // storage backend.
        if (tmp.renameTo(file)) return
        if (file.exists()) file.delete()
        tmp.renameTo(file)
    }

    private companion object {
        /**
         * Rejects a payload from a future, incompatible shape rather than
         * misreading its fields - the same guard [RelayReportFile] uses and for
         * the same reason: a misread here should fail toward "nothing
         * trustworthy", not toward invented legs.
         */
        const val VERSION = 1

        fun encode(updatedAtMs: Long, rawPayload: String): String {
            val o = JSONObject()
            o.put("version", VERSION)
            o.put("updatedAtMs", updatedAtMs)
            o.put("payload", rawPayload)
            return o.toString()
        }

        /** Null on anything that is not a valid, current-version bond. */
        fun decode(raw: String): WidgetBondSnapshot? {
            val o = try {
                JSONObject(raw)
            } catch (e: JSONException) {
                return null
            }
            if (o.optInt("version", -1) != VERSION) return null
            val updatedAtMs = o.optLong("updatedAtMs", -1)
            if (updatedAtMs < 0) return null
            val payload = o.optString("payload", "")
            if (payload.isEmpty()) return null
            // A payload that does not parse is "nothing trustworthy", not a
            // crash: a widget must still draw if the router's console shape
            // ever changes under it.
            val status = try {
                BondStatus.decode(payload)
            } catch (e: JSONException) {
                return null
            }
            return WidgetBondSnapshot(status, updatedAtMs)
        }
    }
}

/**
 * The Android half of [RelayBondFile]: one call to pick the storage context,
 * which cannot be unit tested here.
 */
object RelayBondStore {
    private const val FILE_NAME = "widget_bond.json"

    @Volatile private var cached: RelayBondFile? = null

    private fun file(context: Context): RelayBondFile {
        cached?.let { return it }
        synchronized(this) {
            cached?.let { return it }
            // DEVICE-PROTECTED STORAGE, matching RelayStatusStore and
            // BootConfigStore: BootReceiver can start RelayService on
            // LOCKED_BOOT_COMPLETED, so the relay may already be publishing
            // while credential-encrypted storage is not readable at all, and a
            // record a widget cannot read until the phone is unlocked is
            // useless for exactly the boot this app cares about most. Nothing
            // in a bond is a credential (contrast BootConfigStore's refusal to
            // mirror the announce token).
            val dir = context.applicationContext
                .createDeviceProtectedStorageContext()
                .filesDir
            return RelayBondFile(File(dir, FILE_NAME)).also { cached = it }
        }
    }

    fun publish(context: Context, snapshot: WidgetBondSnapshot, rawPayload: String) {
        file(context).publish(snapshot, rawPayload)
        // The same redraw the relay report triggers. A leg list that stops
        // updating under a verdict sentence which still looks current is just
        // as wrong as a stale sentence, and the leg list is most of what the
        // medium size draws. Throttled inside.
        RelayWidgetRefresh.request(context)
    }

    fun clear(context: Context) {
        file(context).clear()
        // Forced, for the reason RelayStatusStore.clear is: the tile is holding
        // a bond nobody can reach, and every second of that is a claim.
        RelayWidgetRefresh.request(context, force = true)
    }

    /** What a widget process reads. Never consults in-memory state, so a brand
     *  new [RelayBondFile] pointed at the same path sees the real bond. */
    fun read(context: Context): WidgetBondSnapshot? = file(context).read()
}
