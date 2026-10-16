package app.zippie.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * The exemption warning must reach the relay screen (#178 AC2).
 *
 * The decision (BatteryExemption.decide) and the data
 * (RelayStats.batteryExemption, populated by RelayService.snapshot) both
 * exist, but nothing reads them: a phone one setting away from a dead leg
 * says nothing on its own screen. This pins the display contract the screen
 * wires up: at-risk shows the reason, everything else shows nothing.
 *
 * Orthogonal to the verdict by design (#267): the warning never rewrites the
 * headline, so a carrying relay still warns and the warning never claims the
 * phone is not carrying.
 */
class BatteryExemptionWarningTest {

    @Test
    fun `an at-risk phone shows the reason on its screen`() {
        assertEquals(
            BatteryExemption.AT_RISK_REASON,
            BatteryExemption.warningLine(
                BatteryExemption.AtRisk(BatteryExemption.AT_RISK_REASON)
            )
        )
    }

    @Test
    fun `an exempt phone shows no warning`() {
        assertNull(BatteryExemption.warningLine(BatteryExemption.Granted))
    }

    @Test
    fun `a phone that does not need the exemption shows no warning`() {
        assertNull(BatteryExemption.warningLine(BatteryExemption.NotNeeded))
    }
}
