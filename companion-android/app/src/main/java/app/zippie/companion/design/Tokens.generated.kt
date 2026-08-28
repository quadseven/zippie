// GENERATED FROM design/tokens.json - DO NOT EDIT.
// Run design/generate.py after changing a token.

package app.zippie.companion.design

import androidx.compose.ui.graphics.Color

/** The zippie visual language, shared with the hub and the iOS app. */
object Tok {
    /** the page */
    val groundLight = Color(0xFFFBFBFB)
    val groundDark = Color(0xFF0E0E0E)
    /** raised surfaces, used sparingly */
    val raisedLight = Color(0xFFFFFFFF)
    val raisedDark = Color(0xFF1B1B1B)
    /** text */
    val primaryLight = Color(0xFF141414)
    val primaryDark = Color(0xFFF5F5F5)
    /** supporting text, tinted from the ground and never neutral gray */
    val secondaryLight = Color(0xFF5C5F66)
    val secondaryDark = Color(0xFF9EA3AE)
    /** the quietest text; a reserve leg is not news */
    val tertiaryLight = Color(0xFF8C8F99)
    val tertiaryDark = Color(0xFF737882)
    /** hairlines, the structural device in place of cards */
    val ruleLight = Color(0xFFE0E0E0)
    val ruleDark = Color(0xFF333333)
    /** CARRYING TRAFFIC RIGHT NOW - the only accent, and it means exactly one thing */
    val liveLight = Color(0xFF1757D6)
    val liveDark = Color(0xFF70A3FF)
    /** holding on, not failing - amber because crying wolf in a car is worse than saying nothing */
    val degradedLight = Color(0xFFB37105)
    val degradedDark = Color(0xFFFAB840)
    /** not carrying */
    val downLight = Color(0xFFB82E26)
    val downDark = Color(0xFFFF7366)
    const val space_hair = 4
    const val space_tight = 8
    const val space_snug = 12
    const val space_base = 16
    const val space_roomy = 24
    const val space_section = 40
    const val space_major = 56
    const val space_margin = 20

    object StateWord {
        const val carrying = "carrying"
        const val carryingDegraded = "carrying, degraded"
        const val reserve = "held in reserve"
        const val notInBond = "not in the bond"
        const val upNotCarrying = "up, not carrying"
        const val notConnected = "not connected"
        const val down = "down"
        const val idle = "idle"
    }
}
