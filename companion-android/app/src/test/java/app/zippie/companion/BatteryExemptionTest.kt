package app.zippie.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The combination that matters is "not exempt AND contributing", because that
 * is the one that produces an invisible outage: the relay announces, the
 * socket stops being serviced, and the router cannot tell that apart from a
 * phone that left the network.
 */
class BatteryExemptionTest {

    @Test
    fun `a contributing phone without the exemption is at risk`() {
        val v = BatteryExemption.decide(
            isIgnoringBatteryOptimizations = false, isContributing = true)
        assertTrue(v.isAtRisk)
        assertEquals(BatteryExemption.AtRisk(BatteryExemption.AT_RISK_REASON), v)
    }

    @Test
    fun `an exempt phone says nothing`() {
        assertEquals(
            BatteryExemption.Granted,
            BatteryExemption.decide(
                isIgnoringBatteryOptimizations = true, isContributing = true))
    }

    @Test
    fun `a phone that is not contributing does not need the exemption`() {
        // Warning here would train the reader to dismiss the one message on
        // this screen that predicts an outage they cannot otherwise see.
        val v = BatteryExemption.decide(
            isIgnoringBatteryOptimizations = false, isContributing = false)
        assertEquals(BatteryExemption.NotNeeded, v)
        assertFalse(v.isAtRisk)
    }

    @Test
    fun `exemption wins over contributing state, both ways`() {
        assertEquals(
            BatteryExemption.Granted,
            BatteryExemption.decide(
                isIgnoringBatteryOptimizations = true, isContributing = false))
    }

    @Test
    fun `the reason names the consequence, not the android setting`() {
        // "Battery optimization is on" describes Android. It has to describe
        // what BREAKS, or nobody connects a battery menu to a dead router leg.
        val reason = BatteryExemption.AT_RISK_REASON
        assertTrue("must say what the router sees", reason.contains("router"))
        assertTrue("must say it stops answering", reason.contains("answering"))
        assertTrue("must name the fix", reason.contains("Unrestricted"))
    }
}

/**
 * The exemption must stay ORTHOGONAL to the verdict. A relay that is carrying
 * is the one most worth warning - it has something to lose - so "carrying" must
 * never suppress the warning, and the warning must never claim it is not
 * carrying. #267 was exactly this mistake in the other direction.
 */
class BatteryExemptionIsOrthogonalToTheVerdictTest {

    private fun stats(exempt: Boolean, listening: Boolean) = RelayStats(
        listening = listening,
        cellularReady = true,
        ignoringBatteryOptimizations = exempt,
    )

    @Test
    fun `a healthy carrying relay still warns when it is not exempt`() {
        val s = stats(exempt = false, listening = true)
        assertTrue(s.batteryExemption.isAtRisk)
    }

    @Test
    fun `an exempt relay never warns, carrying or not`() {
        assertFalse(stats(exempt = true, listening = true).batteryExemption.isAtRisk)
        assertFalse(stats(exempt = true, listening = false).batteryExemption.isAtRisk)
    }

    @Test
    fun `an idle relay does not warn`() {
        assertFalse(stats(exempt = false, listening = false).batteryExemption.isAtRisk)
    }

    @Test
    fun `the default is exempt, so an older report never invents a warning`() {
        // Reports from a build before this field existed decode with the
        // default. Defaulting false there would put a warning on every phone
        // running an older relay, which is a lie about a device we know
        // nothing about.
        assertTrue(RelayStats().ignoringBatteryOptimizations)
    }
}
