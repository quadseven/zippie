package app.zippie.companion.design

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.background
import androidx.compose.runtime.Composable
import androidx.compose.runtime.ReadOnlyComposable
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/**
 * The semantic layer over [Tok], mirroring ZippieCompanionApp/Design/Theme.swift.
 *
 * WHY THIS FILE EXISTS AT ALL. `Tokens.generated.kt` has been sitting in this
 * package since 2026-08-06, emitted from design/tokens.json by the same
 * generator that produces the iOS and hub copies - and nothing in the app ever
 * referenced it. The Android app has been rendering raw Material 3 defaults
 * while the iOS app renders a deliberate palette from the same source, which is
 * the entire reason one looks designed and the other does not.
 *
 * The names are deliberately IDENTICAL to the Swift ones - Ink, Kind, Space,
 * Hairline - because a shared visual language whose two halves use different
 * vocabulary is two languages. A change to one file should have an obvious
 * counterpart in the other.
 */

/**
 * Colour, resolved for the current theme.
 *
 * Composable getters rather than a static palette: Android can change theme
 * under a running process, and a value captured once would be the wrong one for
 * the rest of the session.
 */
object Ink {
    /** the page */
    val ground: Color
        @Composable @ReadOnlyComposable get() = pick(Tok.groundLight, Tok.groundDark)

    /** raised surfaces, used sparingly */
    val raised: Color
        @Composable @ReadOnlyComposable get() = pick(Tok.raisedLight, Tok.raisedDark)

    /** text */
    val primary: Color
        @Composable @ReadOnlyComposable get() = pick(Tok.primaryLight, Tok.primaryDark)

    /** supporting text, tinted from the ground and never neutral gray */
    val secondary: Color
        @Composable @ReadOnlyComposable get() = pick(Tok.secondaryLight, Tok.secondaryDark)

    /** the quietest text; a reserve leg is not news */
    val tertiary: Color
        @Composable @ReadOnlyComposable get() = pick(Tok.tertiaryLight, Tok.tertiaryDark)

    /** hairlines, the structural device in place of cards */
    val rule: Color
        @Composable @ReadOnlyComposable get() = pick(Tok.ruleLight, Tok.ruleDark)

    /** CARRYING TRAFFIC RIGHT NOW - the only accent, and it means exactly one thing */
    val live: Color
        @Composable @ReadOnlyComposable get() = pick(Tok.liveLight, Tok.liveDark)

    /** holding on, not failing */
    val degraded: Color
        @Composable @ReadOnlyComposable get() = pick(Tok.degradedLight, Tok.degradedDark)

    /** not carrying */
    val down: Color
        @Composable @ReadOnlyComposable get() = pick(Tok.downLight, Tok.downDark)

    @Composable
    @ReadOnlyComposable
    private fun pick(light: Color, dark: Color) = if (isSystemInDarkTheme()) dark else light
}

/**
 * Type. Structure mirrors Kind in Theme.swift; the sizes are Android's, because
 * a 17sp body is an iOS convention wearing an Android costume.
 */
object Kind {
    /** The state sentence. The only thing at this size, so it needs no ornament
     *  above it to announce itself. */
    val display = TextStyle(fontSize = 32.sp, lineHeight = 38.sp, fontWeight = FontWeight.SemiBold)

    val title = TextStyle(fontSize = 20.sp, lineHeight = 26.sp, fontWeight = FontWeight.SemiBold)

    val body = TextStyle(fontSize = 16.sp, lineHeight = 22.sp)

    /** Row labels: the leg's name. */
    val label = TextStyle(fontSize = 16.sp, lineHeight = 22.sp, fontWeight = FontWeight.Medium)

    val caption = TextStyle(fontSize = 13.sp, lineHeight = 18.sp)

    /** Section headers. NOT tracked-out micro caps - that is the eyebrow habit
     *  wearing a different hat, and this screen had a literal one. */
    val section = TextStyle(fontSize = 15.sp, lineHeight = 20.sp, fontWeight = FontWeight.SemiBold)

    /**
     * Every changing number.
     *
     * MONOSPACED DIGITS ONLY, via the OpenType `tnum` feature - not a monospace
     * FACE, which would be technical costume. Byte counts and round-trip times
     * update every couple of seconds, and proportional digits make the whole row
     * twitch sideways on each repaint. `tnum` fixes the advance width so only
     * the glyphs change.
     */
    fun figure(size: Int = 16, weight: FontWeight = FontWeight.Normal) = TextStyle(
        fontSize = size.sp,
        lineHeight = (size * 1.35).sp,
        fontWeight = weight,
        fontFamily = FontFamily.Default,
        fontFeatureSettings = "tnum",
    )
}

/** The spacing scale, straight off the generated tokens so it cannot drift. */
object Space {
    val hair = Tok.space_hair.dp
    val tight = Tok.space_tight.dp
    val snug = Tok.space_snug.dp
    val base = Tok.space_base.dp
    val roomy = Tok.space_roomy.dp
    val section = Tok.space_section.dp
    val major = Tok.space_major.dp

    /** Screen margin. */
    val margin = Tok.space_margin.dp
}

/**
 * The structural device, in place of cards.
 *
 * One physical pixel where the platform allows it - `Dp.Hairline` renders the
 * thinnest line the display can draw rather than a chunky 1dp bar, which is what
 * keeps a list of rules reading as structure instead of as a table.
 */
@Composable
fun Hairline(modifier: Modifier = Modifier, inset: Dp = 0.dp) {
    Box(
        modifier
            .fillMaxWidth()
            .padding(start = inset)
            .height(Dp.Hairline)
            .background(Ink.rule),
    )
}
