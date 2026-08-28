package app.zippie.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The name is the router's KEY for this leg, and it arrives over the network.
 * Two phones agreeing on one would silently become one leg; a name the router's
 * regex rejects is a phone that can never join, and it fails on the device and
 * nowhere else.
 */
class LegNameTest {

    /** dynamic.py `_NAME_RE`, character for character. The point of the tests
     *  below is that everything this file can produce satisfies it. */
    private val routerRule = Regex("^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$")

    @Test
    fun `a model name becomes a slug`() {
        assertEquals("pixel-8-pro", LegName.sanitise("Pixel 8 Pro"))
        assertEquals("sm-g991b", LegName.sanitise("SM-G991B"))
    }

    @Test
    fun `runs of junk collapse into one hyphen`() {
        assertEquals("pixel-8", LegName.sanitise("Pixel   8"))
        assertEquals("pixel-8", LegName.sanitise("  Pixel _ 8 !! "))
    }

    /** "Operator's Pixel" must not read "operator-s-pixel" - legal, and reads as a
     *  mistake. Both apostrophes, because the typographic one is what a phone
     *  inserts. */
    @Test
    fun `apostrophes are dropped rather than separating`() {
        assertEquals("operators-pixel", LegName.sanitise("Operator's Pixel"))
        assertEquals("operators-pixel", LegName.sanitise("Operator\u2019s Pixel"))
    }

    @Test
    fun `a name with nothing usable in it is null, not empty`() {
        assertNull(LegName.sanitise(""))
        assertNull(LegName.sanitise("   "))
        assertNull(LegName.sanitise("!!!"))
    }

    @Test
    fun `compose falls back rather than producing an invalid name`() {
        assertTrue(routerRule.matches(LegName.compose(null, "3f9a")))
        assertTrue(routerRule.matches(LegName.compose("!!!", "3f9a")))
        assertEquals("phone-3f9a", LegName.compose("!!!", "3f9a"))
    }

    /**
     * Trimming the composed name would cut into the suffix, and a truncated
     * suffix is the collision it exists to prevent.
     */
    @Test
    fun `a long model loses its tail and never the suffix`() {
        val name = LegName.compose("A".repeat(60), "3f9a")
        assertTrue(name.length <= LegName.MAX_LENGTH)
        assertTrue("suffix was trimmed: $name", name.endsWith("-3f9a"))
        assertTrue(routerRule.matches(name))
    }

    /** A base whose tail lands on a hyphen would compose "pixel--3f9a". */
    @Test
    fun `truncation never leaves a doubled hyphen`() {
        val name = LegName.compose("Pixel 8 Pro Extremely Long Edition", "3f9a")
        assertFalse("doubled hyphen in $name", name.contains("--"))
        assertTrue(routerRule.matches(name))
    }

    @Test
    fun `every composed name satisfies the router's rule`() {
        val models = listOf(
            "Pixel 8", "Pixel 8 Pro", "SM-G991B", "Operator's Pixel", "moto g(60)",
            "", "   ", "!!!", "-leading", "trailing-", "A".repeat(120),
            "Pixel 8", "\u30d4\u30af\u30bb\u30eb",
        )
        for (model in models) {
            val name = LegName.compose(model, LegName.newSuffix { 0x0042 })
            assertTrue("router would refuse $name (from $model)", routerRule.matches(name))
            assertTrue("isValid disagrees with the rule for $name", LegName.isValid(name))
        }
    }

    @Test
    fun `the suffix is always four hex characters`() {
        assertEquals("0000", LegName.newSuffix { 0 })
        assertEquals("ffff", LegName.newSuffix { 0xFFFF })
        assertEquals("003f", LegName.newSuffix { 0x3F })
        // Anything wider than 16 bits is masked rather than widening the name.
        assertEquals("beef", LegName.newSuffix { 0x1234BEEF })
    }

    @Test
    fun `isValid mirrors the router rather than approximating it`() {
        assertTrue(LegName.isValid("pixel-8-3f9a"))
        assertTrue(LegName.isValid("a1"))
        assertFalse("single character", LegName.isValid("a"))
        assertFalse("uppercase", LegName.isValid("Pixel-3f9a"))
        assertFalse("underscore", LegName.isValid("pixel_3f9a"))
        assertFalse("leading hyphen", LegName.isValid("-pixel"))
        assertFalse("trailing hyphen", LegName.isValid("pixel-"))
        assertFalse("empty", LegName.isValid(""))
        assertFalse("33 characters", LegName.isValid("a".repeat(33)))
        assertTrue("32 characters", LegName.isValid("a".repeat(32)))
    }

    /**
     * THE PROPERTY THAT MATTERS ACROSS RESTARTS. A name minted afresh on every
     * start would leave a trail of dead legs, each lingering for its lease.
     */
    @Test
    fun `a resolved name is minted once and then reused`() {
        var stored: String? = null
        var mints = 0
        val suffix = { mints++; "3f9a" }

        val first = LegName.resolve("Pixel 8", { stored }, { stored = it }, suffix)
        val second = LegName.resolve("Pixel 8", { stored }, { stored = it }, suffix)

        assertEquals("pixel-8-3f9a", first)
        assertEquals(first, second)
        assertEquals("the name was minted twice", 1, mints)
    }

    /** Two phones of the SAME model must not become one leg. Build.MODEL is a
     *  model name, not a device name, so the suffix is the only thing keeping
     *  them apart. */
    @Test
    fun `two phones of one model get different names`() {
        var a: String? = null
        var b: String? = null
        val first = LegName.resolve("Pixel 8", { a }, { a = it }, { "3f9a" })
        val second = LegName.resolve("Pixel 8", { b }, { b = it }, { "b204" })
        assertFalse("both phones announced as $first", first == second)
    }

    /** A stored value that the router would refuse - hand-edited, or written by
     *  an older build - must be replaced, not announced. */
    @Test
    fun `a stored name the router would refuse is replaced`() {
        var stored: String? = "Pixel_8"
        val name = LegName.resolve("Pixel 8", { stored }, { stored = it }, { "3f9a" })
        assertEquals("pixel-8-3f9a", name)
        assertEquals("pixel-8-3f9a", stored)
    }
}

/**
 * The label is the one thing an Android leg knows that an iPhone leg cannot:
 * which carrier it is actually on.
 */
class LegLabelTest {

    @Test
    fun `the serving carrier is in the label`() {
        assertEquals(
            "Pixel 8 (T-Mobile)",
            LegLabel.forDevice("Pixel 8", CarrierInfo(serving = "T-Mobile", sim = "T-Mobile")),
        )
    }

    /** The network the leg is registered on is the one spending the data, so it
     *  wins over the SIM's home carrier while roaming. */
    @Test
    fun `the serving carrier wins over the SIM's home carrier`() {
        assertEquals(
            "Pixel 8 (AT&T)",
            LegLabel.forDevice("Pixel 8", CarrierInfo(serving = "AT&T", sim = "Google Fi")),
        )
    }

    @Test
    fun `with no registration the SIM name still says which phone this is`() {
        assertEquals(
            "Pixel 8 (Google Fi)",
            LegLabel.forDevice("Pixel 8", CarrierInfo(serving = null, sim = "Google Fi")),
        )
    }

    /** Never the word "unknown" dressed as a carrier: an empty operator string
     *  means the radio did not answer, and printing it would invent one. */
    @Test
    fun `a silent radio leaves the carrier out entirely`() {
        assertEquals("Pixel 8", LegLabel.forDevice("Pixel 8", null))
        assertEquals("Pixel 8", LegLabel.forDevice("Pixel 8", CarrierInfo(null, null)))
        assertEquals("Pixel 8", LegLabel.forDevice("Pixel 8", CarrierInfo("  ", "")))
    }

    @Test
    fun `an empty model still produces something readable`() {
        assertEquals("Android phone (T-Mobile)", LegLabel.forDevice("", CarrierInfo("T-Mobile", null)))
    }

    /** The router truncates at 64. Doing it here means the cut is ours. */
    @Test
    fun `a very long label is cut to what the router keeps`() {
        val label = LegLabel.forDevice("M".repeat(80), CarrierInfo("T-Mobile", null))
        assertEquals(LegLabel.MAX_LENGTH, label.length)
    }
}
