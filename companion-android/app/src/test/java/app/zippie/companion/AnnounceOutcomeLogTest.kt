package app.zippie.companion

import java.io.File
import org.junit.Assert.assertTrue
import org.junit.Test

class AnnounceOutcomeLogTest {

    private fun source(relative: String): String {
        var dir: File? = File("").absoluteFile
        while (dir != null) {
            val f = File(dir, relative)
            if (f.isFile) return f.readText()
            dir = dir.parentFile
        }
        throw AssertionError("cannot find $relative - if it moved, move this check with it")
    }

    @Test
    fun `onAnnounced records the outcome in logcat`() {
        val text = source("app/src/main/java/app/zippie/companion/RelayService.kt")
        val body = text.substringAfter("private fun onAnnounced(")
            .substringBefore("\n    }")
        assertTrue("onAnnounced must log the outcome", body.contains("Log.i(TAG, text)"))
    }
}
