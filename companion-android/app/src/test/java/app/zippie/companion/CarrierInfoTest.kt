package app.zippie.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * CarrierInfo is the fact an Android leg knows that an iPhone leg cannot: the
 * real serving carrier, not a hand-typed label in zippie.toml (see
 * LegLabelTest for how that fact reaches the announced label).
 *
 * [CarrierInfo.read] needs a live TelephonyManager and is not exercised here -
 * this project carries no Robolectric, only plain JUnit (see
 * .github/workflows/check.android.yml) - but [CarrierInfo.summary] is pure
 * data-in data-out and is exactly the part worth pinning: what a person reads
 * on the status screen when the two fields agree, disagree, or are absent.
 */
class CarrierInfoTest {

    @Test
    fun `serving and sim on the same carrier print once`() {
        assertEquals(
            "T-Mobile",
            CarrierInfo(serving = "T-Mobile", sim = "T-Mobile").summary,
        )
    }

    /** Case must not matter - carriers do not agree on how they capitalise
     *  their own name across the two APIs. */
    @Test
    fun `serving and sim differing only by case still print once`() {
        assertEquals(
            "T-Mobile",
            CarrierInfo(serving = "T-Mobile", sim = "t-mobile").summary,
        )
    }

    /** Roaming: the network actually carrying bytes differs from the SIM's
     *  home network, and both facts matter for a relay somebody is paying for. */
    @Test
    fun `a different serving network shows both`() {
        assertEquals(
            "AT&T (SIM: Google Fi)",
            CarrierInfo(serving = "AT&T", sim = "Google Fi").summary,
        )
    }

    @Test
    fun `no service but a SIM present says so`() {
        assertEquals(
            "Google Fi - no network registered",
            CarrierInfo(serving = null, sim = "Google Fi").summary,
        )
    }

    /** Aeroplane mode, no SIM: nothing was measured, and printing "unknown"
     *  would invent a carrier that was never read. */
    @Test
    fun `no service and no SIM is null, not a placeholder`() {
        assertNull(CarrierInfo(serving = null, sim = null).summary)
    }

    @Test
    fun `serving alone with no SIM read is just the serving name`() {
        assertEquals("Verizon", CarrierInfo(serving = "Verizon", sim = null).summary)
    }
}
