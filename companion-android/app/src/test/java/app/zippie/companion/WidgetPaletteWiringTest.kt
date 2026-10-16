package app.zippie.companion

import java.io.File
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Issue #244's acceptance criterion that is a build configuration rather than a
 * behaviour: "colours come from the generated tokens so a token change moves the
 * widget with everything else".
 *
 * ## WHY A TEXT TEST AND NOT A COLOUR ASSERTION
 *
 * A Glance composable cannot be executed by a JVM unit test, and its
 * `ColorProvider`s can only be read back through a `Context`. So the question
 * "does the widget draw from the tokens" is asked of the source instead - the
 * same technique `ConfigSourceWiringTest` and the iOS `CallSiteWiringTests` use
 * for files no test target can run, and for the same reason: it is the only
 * tripwire available, and it fails the moment somebody hardcodes a colour.
 *
 * ## WHAT IT WOULD HAVE CAUGHT
 *
 * `GlanceTheme.colors` is the obvious way to write a Glance widget and it is
 * wrong here: it is the MATERIAL palette, a different design system from the one
 * `design/tokens.json` defines. A widget built that way would have looked fine,
 * passed every behavioural test, and been the single surface a token change
 * never reached. `Tokens.generated.kt` itself sat in this package from
 * 2026-08-06 with zero call sites anywhere in the app until `Theme.kt` started
 * using it, so "the tokens are available" was never evidence of "the widget uses
 * them".
 *
 * The value assertions at the bottom close the other half of the chain -
 * `design/tokens.json` -> `design/generate.py` -> `Tok` - so the whole path from
 * the token file to the widget is covered by two cheap checks rather than one
 * clever untestable one. `check.design-tokens.yml` covers the generator itself;
 * this covers that what the generator produced is what the widget reads.
 */
class WidgetPaletteWiringTest {

    private fun source(relative: String): String {
        // Walk up from the working directory rather than assuming it: gradle runs
        // tests from the module dir, and a moved file must fail loudly here
        // rather than quietly stop being watched.
        var dir: File? = File("").absoluteFile
        while (dir != null) {
            val f = File(dir, relative)
            if (f.isFile) return f.readText()
            dir = dir.parentFile
        }
        throw AssertionError("cannot find $relative - if it moved, move this check with it")
    }

    private fun codeOnly(text: String): String =
        text.split("\n")
            .filterNot { it.trimStart().startsWith("//") || it.trimStart().startsWith("*") ||
                it.trimStart().startsWith("/*") }
            .joinToString("\n")

    private val widget = codeOnly(
        source("app/src/main/java/app/zippie/companion/RelayWidget.kt"),
    )

    @Test
    fun `every colour the widget draws is read out of the generated tokens`() {
        // One per WidgetPalette member, so adding a role and forgetting the token
        // fails rather than quietly borrowing an existing one.
        for (token in listOf(
            "Tok.liveLight", "Tok.liveDark",
            "Tok.downLight", "Tok.downDark",
            "Tok.tertiaryLight", "Tok.tertiaryDark",
            "Tok.primaryLight", "Tok.primaryDark",
            "Tok.secondaryLight", "Tok.secondaryDark",
            "Tok.groundLight", "Tok.groundDark",
            "Tok.ruleLight", "Tok.ruleDark",
        )) {
            assertTrue("$token is missing from the widget's palette", widget.contains(token))
        }
    }

    @Test
    fun `the widget does not reach for the Material palette`() {
        // GlanceTheme.colors is a DIFFERENT DESIGN SYSTEM. A widget built on it
        // would pass every behavioural test and be the one surface a token
        // change never moved.
        assertTrue(
            "the widget is drawing from GlanceTheme - the Material palette - instead of " +
                "design/tokens.json, so it would be the one surface a token change misses",
            !widget.contains("GlanceTheme"),
        )
    }

    @Test
    fun `the widget hardcodes no colour of its own`() {
        // A literal here is the exact regression: someone copies a value out of
        // a screenshot or an old file and the widget silently stops following
        // the token system. Token values live in Tok and only in Tok.
        assertTrue(
            "a literal ARGB colour is in the widget; every colour must come from Tok",
            !Regex("""Color\(0x""").containsMatchIn(widget),
        )
        assertTrue(
            "a '#rrggbb' literal is in the widget; every colour must come from Tok",
            !Regex(""""#[0-9a-fA-F]{6,8}"""").containsMatchIn(widget),
        )
    }

    @Test
    fun `the widget's spacing comes from the generated scale too`() {
        // Space resolves to Tok.space_* in design/Theme.kt, so naming Space
        // rather than a dp literal is the same single-source property as colour.
        //
        // SPACING POSITIONS ONLY - padding/width/height. tokens.json has one
        // scale, the rhythm, and nothing this file uses outside those three is
        // on it: the responsive size set and the medium breakpoint are widget
        // geometry, the corner radius and the hairline have no token at all.
        // Asserting on EVERY dp in the file therefore fails on those four while
        // saying nothing about the rhythm, and a check that is always red is a
        // check somebody switches off.
        assertTrue(
            "the widget is using a raw dp instead of the Space scale",
            !Regex("""\.(padding|width|height)\(\s*\d+(\.\d+)?\.dp""").containsMatchIn(widget),
        )
    }

    @Test
    fun `the generated tokens the widget reads are the ones in design tokens json`() {
        // The other half of the chain. If Tok drifted from tokens.json, the
        // assertions above would still pass while the widget drew a colour the
        // hub and the iOS app had moved away from.
        val tokens = JSONObject(
            File("../design/tokens.json").takeIf { it.isFile }?.readText()
                ?: searchUpwards("design/tokens.json"),
        )
        val color = tokens.getJSONObject("color")

        val generated = source(
            "app/src/main/java/app/zippie/companion/design/Tokens.generated.kt",
        )
        for (name in listOf("live", "down", "tertiary", "primary", "secondary", "ground", "rule")) {
            val entry = color.getJSONObject(name)
            for (variant in listOf("Light", "Dark")) {
                // tokens.json stores "#1757D6"; design/generate.py strips the '#'
                // before emitting `Color(0xFF1757D6)`. Without lstrip here the
                // expected literal reads `Color(0xFF#1757D6)`, which matches
                // nothing and fails on the very first colour - a check that can
                // only ever be red is not a check.
                val hex = entry.getString(if (variant == "Light") "light" else "dark")
                    .removePrefix("#")
                    .uppercase()
                val literal = "Color(0xFF$hex)"
                assertTrue(
                    "Tok.${name}${variant} should be $literal, but the generated file says " +
                        "otherwise - regenerate with design/generate.py",
                    generated.contains("${name}${variant} = $literal"),
                )
            }
        }
    }

    /** tokens.json lives at the repo root, two levels above the gradle module. */
    private fun searchUpwards(relative: String): String {
        var dir: File? = File("").absoluteFile
        while (dir != null) {
            val f = File(dir, relative)
            if (f.isFile) return f.readText()
            dir = dir.parentFile
        }
        throw AssertionError("cannot find $relative - if it moved, move this check with it")
    }
}
