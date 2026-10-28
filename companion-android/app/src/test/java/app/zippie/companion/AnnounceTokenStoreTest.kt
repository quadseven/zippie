package app.zippie.companion

import android.content.SharedPreferences
import java.lang.reflect.Proxy
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class AnnounceTokenStoreTest {
    private class MemoryPrefs {
        val values = mutableMapOf<String, Any>()
        var failCommit = false
        val prefs: SharedPreferences = Proxy.newProxyInstance(
            SharedPreferences::class.java.classLoader,
            arrayOf(SharedPreferences::class.java),
        ) { _, method, args ->
            when (method.name) {
                "contains" -> values.containsKey(args!![0] as String)
                "getString", "getBoolean" -> values[args!![0] as String] ?: args[1]
                "edit" -> editor()
                else -> error("unexpected preference call ${method.name}")
            }
        } as SharedPreferences

        private fun editor(): SharedPreferences.Editor {
            val changes = mutableMapOf<String, Any?>()
            lateinit var proxy: SharedPreferences.Editor
            proxy = Proxy.newProxyInstance(
                SharedPreferences.Editor::class.java.classLoader,
                arrayOf(SharedPreferences.Editor::class.java),
            ) { _, method, args ->
                when (method.name) {
                    "putString", "putBoolean" -> { changes[args!![0] as String] = args[1]; proxy }
                    "remove" -> { changes[args!![0] as String] = null; proxy }
                    "commit" -> {
                        changes.forEach { (key, value) ->
                            if (value == null) values.remove(key) else values[key] = value
                        }
                        !failCommit
                    }
                    else -> error("unexpected editor call ${method.name}")
                }
            } as SharedPreferences.Editor
            return proxy
        }
    }

    @Test
    fun `legacy token migrates durably and survives locked boot`() {
        val ce = MemoryPrefs()
        val de = MemoryPrefs()
        ce.values["announceToken"] = "legacy"
        assertEquals("legacy", AnnounceTokenStore.read(ce.prefs, de.prefs, false))
        assertFalse(ce.values.containsKey("announceToken"))
        assertEquals("legacy", AnnounceTokenStore.read(de.prefs, de.prefs, true))
    }

    @Test
    fun `existing device token wins over stale legacy token`() {
        val ce = MemoryPrefs()
        val de = MemoryPrefs()
        ce.values["announceToken"] = "old"
        de.values["announceToken"] = "new"
        de.values["announceTokenDeviceAuthority"] = true
        assertEquals("new", AnnounceTokenStore.read(ce.prefs, de.prefs, false))
        assertFalse(ce.values.containsKey("announceToken"))
    }

    @Test
    fun `legacy credential value wins over an outdated mirror`() {
        val ce = MemoryPrefs()
        val de = MemoryPrefs()
        ce.values["announceToken"] = "newer"
        de.values["announceToken"] = "old mirror"
        assertEquals("newer", AnnounceTokenStore.read(ce.prefs, de.prefs, false))
        assertEquals("newer", de.values["announceToken"])
        assertFalse(ce.values.containsKey("announceToken"))
    }

    @Test
    fun `failed durable write never deletes legacy token`() {
        val ce = MemoryPrefs()
        val de = MemoryPrefs()
        ce.values["announceToken"] = "old"
        de.failCommit = true
        assertEquals("old", AnnounceTokenStore.read(ce.prefs, de.prefs, false))
        AnnounceTokenStore.write(ce.prefs, de.prefs, false, "new")
        assertEquals("old", ce.values["announceToken"])
        // Android updates in-memory values even when commit reports failure.
        assertEquals("new", de.values["announceToken"])
        assertEquals("new", AnnounceTokenStore.read(ce.prefs, de.prefs, false))
        assertEquals("old", ce.values["announceToken"])
        de.failCommit = false
        assertEquals("new", AnnounceTokenStore.read(ce.prefs, de.prefs, false))
        assertFalse(ce.values.containsKey("announceToken"))
    }

    @Test
    fun `locked reads and writes never access credential storage`() {
        val inaccessible = Proxy.newProxyInstance(
            SharedPreferences::class.java.classLoader,
            arrayOf(SharedPreferences::class.java),
        ) { _, _, _ -> error("credential storage is unavailable before unlock") } as SharedPreferences
        val de = MemoryPrefs()
        AnnounceTokenStore.write(inaccessible, de.prefs, true, "locked")
        assertEquals("locked", AnnounceTokenStore.read(inaccessible, de.prefs, true))
    }

    @Test
    fun `unlock after storage snapshot cannot remove the device token`() {
        val ce = MemoryPrefs()
        val de = MemoryPrefs()
        AnnounceTokenStore.write(de.prefs, de.prefs, true, "token")
        // The operation began while locked. Unlock occurs before it reads;
        // the original locked snapshot must still govern that operation.
        assertEquals("token", AnnounceTokenStore.read(de.prefs, de.prefs, true))
        assertEquals("token", de.values["announceToken"])
        assertEquals("token", AnnounceTokenStore.read(ce.prefs, de.prefs, false))
    }

    @Test
    fun `writing while locked updates only device storage`() {
        val ce = MemoryPrefs()
        val de = MemoryPrefs()
        ce.values["announceToken"] = "old"
        AnnounceTokenStore.write(de.prefs, de.prefs, true, "new")
        assertEquals("new", AnnounceTokenStore.read(de.prefs, de.prefs, true))
        assertEquals("new", AnnounceTokenStore.read(ce.prefs, de.prefs, false))
        assertTrue(ce.values.isEmpty())
    }
}
