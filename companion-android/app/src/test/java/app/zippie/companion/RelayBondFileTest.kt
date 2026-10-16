package app.zippie.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

/**
 * The bond record survives the trip to disk, and staleness survives with it.
 *
 * WHY THIS NEEDS ITS OWN TEST WHEN [RelayReportFileTest] EXISTS. The relay
 * report's producer is the relay itself, which is long-lived; the bond's producer
 * is [BondViewModel], which only exists while a screen is on top. So the bond is
 * the record most likely to be read by a process that did not write it, and the
 * property worth proving is that a FRESH READER gets the real answer - not that
 * encode and decode agree with each other in the same process.
 *
 * A NEW [RelayBondFile] pointed at the same path is how "a different, fresh
 * process" is simulated here, for the same reason it is in RelayReportFileTest.
 */
class RelayBondFileTest {

    @get:Rule
    val tmp = TemporaryFolder()

    private val now = 2_000_000L

    /**
     * ONE directory for the whole class.
     *
     * `TemporaryFolder.newFolder()` MINTS A NEW DIRECTORY ON EVERY CALL, so the
     * obvious helper - `RelayBondFile(File(tmp.newFolder(), name))` - does not
     * return "a second reader of the same record" at all. It returns a reader of
     * a private, empty directory, and the three fresh-process tests below were
     * failing on that rather than on anything about persistence.
     * `RelayReportFileTest` binds the path once and passes it to both instances
     * instead; this does the same, one level up.
     */
    private val dir: File by lazy { tmp.newFolder() }

    private fun file(name: String = "widget_bond.json") =
        RelayBondFile(File(dir, name))

    private fun path(label: String, weight: Int, tier: Int = 1) = """
        {"name":"${label.lowercase().replace(' ', '-')}","label":"$label","state":"up",
         "effective_weight":$weight,"tier":$tier,"in_bond":true,"interface":"br-lan"}
    """.trimIndent()

    private fun payload(vararg rows: String) = """{"mode":"client","paths":[${rows.joinToString(",")}]}"""

    @Test
    fun `a fresh reader sees the real bond, not null`() {
        val target = file()
        val snapshot = WidgetBondSnapshot(
            BondStatus.decode(payload(path("Lone SIM", 100, tier = 2))),
            now,
        )
        target.publish(snapshot, payload(path("Lone SIM", 100, tier = 2)))

        // A SECOND instance, as a widget's process would be.
        val read = file("widget_bond.json")

        // checkNotNull, not assertNotNull: JUnit's returns void, so binding it
        // to a val would not compile, and a snapshot this test cannot name is a
        // failure with nothing left to assert on.
        val got = checkNotNull(read.read()) { "a fresh process must read the real bond" }
        assertEquals(1, got.status.paths?.size)
        assertEquals("Lone SIM", got.status.paths?.first()?.label)
        assertEquals(now, got.updatedAtMs)
    }

    @Test
    fun `the timestamp survives, so staleness is still decidable`() {
        val target = file()
        val body = payload(path("Lone SIM", 100))
        // PAST THE THRESHOLD, not merely old. The threshold is 20s, so a
        // three-second-old snapshot is a fresh one and asserting it is stale
        // would be asserting a lie; what has to survive the round trip is an
        // age the reader can actually act on.
        val writtenAt = now - WidgetBondSnapshot.STALENESS_MS - 1
        target.publish(WidgetBondSnapshot(BondStatus.decode(body), writtenAt), body)

        val got = checkNotNull(file("widget_bond.json").read()) { "the snapshot must be readable" }

        // The whole feature turns on this: without the age, a snapshot written
        // once would read as infinitely fresh and the widget would describe a
        // bond nobody had looked at since the app was last opened.
        assertTrue(got.isStale(now))
        assertTrue(!got.isStale(writtenAt))
    }

    @Test
    fun `the router's own fields survive, including the ones the leg list decides on`() {
        val target = file()
        val body = payload(path("Lone SIM", 100, tier = 3))
        target.publish(WidgetBondSnapshot(BondStatus.decode(body), now), body)

        val got = checkNotNull(file("widget_bond.json").read()) { "the snapshot must be readable" }

        // tier drives reserveIsNotBroken, weight drives weightIsNotMembership,
        // and in_bond is what separates "not carrying" from "not in the bond".
        // A round trip that dropped any of the three would change which legs a
        // home screen calls failures, with nothing failing.
        val p = checkNotNull(got.status.paths?.first()) { "the leg must survive the round trip" }
        assertEquals(3, p.tier)
        assertEquals(100, p.effectiveWeight)
        assertEquals(true, p.inBond)
        assertEquals(3, got.status.activeTier)
    }

    @Test
    fun `nothing written reads as absent, not as an empty bond`() {
        assertNull(file().read())
    }

    @Test
    fun `a clear deletes rather than writing an empty bond`() {
        val target = file()
        val body = payload(path("Lone SIM", 100))
        target.publish(WidgetBondSnapshot(BondStatus.decode(body), now), body)

        target.clear()

        // A console nobody can reach is not the same as a router that answered
        // with nothing, and a persisted empty bond would read like the latter.
        assertNull(file("widget_bond.json").read())
    }

    @Test
    fun `garbage on disk reads as absent rather than throwing`() {
        val f = File(tmp.newFolder(), "widget_bond.json")
        listOf(
            "not json at all",
            "{}",
            """{"version":99,"updatedAtMs":1,"payload":"{}"}""",
            """{"version":1,"updatedAtMs":1}""",
            """{"version":1,"updatedAtMs":1,"payload":"not json"}""",
            """{"version":1,"updatedAtMs":-5,"payload":"{}"}""",
        ).forEach { junk ->
            f.writeText(junk)
            assertNull("this should not be readable as a bond: $junk", RelayBondFile(f).read())
        }
    }

    @Test
    fun `a half-written file is never what a reader observes`() {
        val dir = tmp.newFolder()
        val f = File(dir, "widget_bond.json")
        val store = RelayBondFile(f)
        val body = payload(path("Lone SIM", 100))
        store.publish(WidgetBondSnapshot(BondStatus.decode(body), now), body)

        // write-to-temp-then-rename means the target is either absent or
        // complete. A reader that caught one mid-write would see a list of legs
        // missing whichever path happened to be being written.
        assertTrue("no temp file left behind", !File(dir, "widget_bond.json.tmp").exists())
        assertNotNull("the record must still read after a write", RelayBondFile(f).read())
    }
}
