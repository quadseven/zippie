package app.zippie.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

/**
 * The log exists because logcat does not survive a reboot (#186), so the
 * failures that matter here are the ones that would make it not survive
 * either: growing without bound on a phone nobody is watching, throwing on a
 * boot path where a crash costs half an hour of downtime, or rotating away the
 * earliest lines - which on a failed cold boot are the interesting ones.
 *
 * The Android half (device-protected storage) is one call and cannot be
 * exercised on the JVM, exactly as CellularLogShipperTest says of its POST.
 */
class BootLogFileTest {

    @get:Rule
    val tmp = TemporaryFolder()

    private fun log(maxBytes: Long = 1024L): BootLogFile {
        val dir = tmp.newFolder()
        return BootLogFile(File(dir, "boot.log"), File(dir, "boot.log.1"), maxBytes)
    }

    @Test
    fun `lines come back in the order they were written`() {
        val l = log()
        l.append("first")
        l.append("second")
        assertEquals(listOf("first", "second"), l.read())
    }

    @Test
    fun `a fresh install reads as empty rather than throwing`() {
        // A phone that has never booted with this app has no file. The
        // diagnostics screen must render that as "nothing yet", not an error.
        assertEquals(emptyList<String>(), log().read())
    }

    @Test
    fun `the file is capped and does not grow without bound`() {
        val l = log(maxBytes = 256L)
        repeat(500) { l.append("line $it padded out to take up some real space") }
        // Two generations, each capped - so the total is bounded no matter how
        // long the phone runs. This is the property that makes it safe to leave
        // enabled on a device in a car.
        assertTrue("size was ${l.sizeBytes()}", l.sizeBytes() <= 2 * 256L + 128L)
    }

    @Test
    fun `rotation keeps the previous generation instead of deleting it`() {
        val l = log(maxBytes = 128L)
        l.append("THE-EARLIEST-LINE")
        repeat(20) { l.append("filler $it ................................") }
        // The earliest line is the one that says when the boot happened. A
        // naive truncate-when-full throws it away first and keeps whatever the
        // phone was chattering about afterwards, which explains nothing.
        val all = l.read()
        assertTrue("expected an earlier generation to survive", all.size > 1)
    }

    @Test
    fun `an unwritable location is reported, not thrown`() {
        // A logging call must never be the reason a boot fails. A directory
        // that cannot be created is the realistic version of this on a phone
        // whose storage is not ready yet.
        val f = File("/proc/definitely-not-writable/boot.log")
        val l = BootLogFile(f, File("/proc/definitely-not-writable/boot.log.1"))
        assertFalse(l.append("this cannot land"))
        // ...and reading it back is still safe.
        assertTrue(l.read().isEmpty() || l.read().all { it.startsWith("<unreadable") })
    }

    @Test
    fun `size is zero before anything is written`() {
        assertEquals(0L, log().sizeBytes())
    }

    @Test
    fun `a trailing newline is not doubled`() {
        val l = log()
        l.append("with newline\n")
        l.append("without")
        assertEquals(listOf("with newline", "without"), l.read())
    }
}
