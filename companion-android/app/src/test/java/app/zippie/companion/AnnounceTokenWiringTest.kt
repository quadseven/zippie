package app.zippie.companion

import java.io.File
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class AnnounceTokenWiringTest {
    private fun source(name: String): String {
        var directory: File? = File("").absoluteFile
        while (directory != null) {
            val file = File(directory, "app/src/main/java/app/zippie/companion/$name.kt")
            if (file.isFile) return file.readText()
            directory = directory.parentFile
        }
        error("source not found: $name")
    }

    @Test
    fun `relay configuration uses only device token store`() {
        val config = source("RelayConfiguration")
        assertTrue(config.contains("AnnounceTokenStore.read("))
        assertTrue(config.contains("AnnounceTokenStore.write("))
        assertFalse(config.contains("KEY_ANNOUNCE_TOKEN"))
        assertFalse(config.contains(".putString(\"announceToken\""))
        assertTrue(config.contains("storage.isDeviceProtectedStorage"))
        val load = config.substringAfter("fun load(context: Context)").substringBefore("fun saveAnnounceToken")
        val save = config.substringAfter("fun saveAnnounceToken(context: Context").substringBefore("fun save(context:")
        for (operation in listOf(load, save)) {
            assertTrue(operation.split("storageContext(context)").size == 2)
            assertTrue(operation.contains("storage.getSharedPreferences("))
            assertTrue(operation.contains("storage.isDeviceProtectedStorage"))
        }
    }
}
