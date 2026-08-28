package app.zippie.companion

import java.io.File
import java.io.IOException

/**
 * An append-only, size-capped log on disk, with no Android in it.
 *
 * Split from [BootLog] for the same reason CellularLogShipper's queue is split
 * from its POST: the part that has actually bitten is the bookkeeping - a log
 * that grows without limit on a phone nobody is watching, or one that rotates
 * away the lines that explain the incident - and that part can be proven on the
 * JVM. The Android half is one call to createDeviceProtectedStorageContext()
 * and cannot be unit tested here.
 *
 * ROTATION KEEPS THE OLD FILE, IT DOES NOT DELETE IT. A cold boot that fails is
 * exactly the case where the interesting lines are the EARLIEST ones - the boot
 * itself, the first relay attempt - and a naive "truncate when full" throws
 * those away first while keeping whatever the phone was chattering about
 * afterwards. Two generations means the previous boot survives the current one.
 */
class BootLogFile(
    private val current: File,
    private val previous: File,
    private val maxBytes: Long = DEFAULT_MAX_BYTES,
) {
    companion object {
        /**
         * Per generation, so the on-disk total is twice this. Deliberately
         * small: this lives in device-protected storage on a phone, it is read
         * by a human debugging one boot, and a megabyte of it would be neither
         * more useful nor obviously bounded.
         */
        const val DEFAULT_MAX_BYTES: Long = 64L * 1024L

        /** Both generations, oldest first - the order a reader wants. */
        fun of(dir: File, name: String = "boot.log"): BootLogFile =
            BootLogFile(File(dir, name), File(dir, "$name.1"))
    }

    /**
     * Append one line. Returns false if it could not be written.
     *
     * NEVER THROWS. This is called from BootReceiver and from the relay's retry
     * loop, where a full disk or a storage layer that is not ready yet must not
     * be the reason a phone fails to come up. A log that can take the boot down
     * is worse than no log.
     */
    @Synchronized
    fun append(line: String): Boolean = try {
        rotateIfFull(line.length.toLong() + 1L)
        current.parentFile?.mkdirs()
        current.appendText(line.trimEnd('\n') + "\n")
        true
    } catch (e: IOException) {
        false
    } catch (e: SecurityException) {
        false
    }

    /**
     * Everything on disk, oldest generation first.
     *
     * Returns an empty list rather than throwing when nothing has been written
     * yet - a fresh install has no log, which is not an error and must not
     * render as one on the diagnostics screen.
     */
    @Synchronized
    fun read(): List<String> {
        val out = mutableListOf<String>()
        for (f in listOf(previous, current)) {
            try {
                if (f.exists()) out += f.readLines()
            } catch (e: IOException) {
                out += "<unreadable: ${f.name}>"
            } catch (e: SecurityException) {
                out += "<unreadable: ${f.name}>"
            }
        }
        return out
    }

    /** Total bytes across both generations, for the diagnostics screen. */
    @Synchronized
    fun sizeBytes(): Long =
        listOf(previous, current).sumOf { if (it.exists()) it.length() else 0L }

    private fun rotateIfFull(incoming: Long) {
        val len = if (current.exists()) current.length() else 0L
        if (len + incoming <= maxBytes) return
        // delete-then-rename: File.renameTo does NOT replace an existing target
        // on every Android storage backend, and a rotation that silently fails
        // leaves `current` growing past the cap forever - the exact unbounded
        // growth this class exists to prevent.
        if (previous.exists()) previous.delete()
        if (!current.renameTo(previous)) {
            // Renaming failed, so the cap can only be honoured by dropping what
            // is there. Losing a generation beats growing without limit on a
            // device with no one watching.
            current.delete()
        }
    }
}
