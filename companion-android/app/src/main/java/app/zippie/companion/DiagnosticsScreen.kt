package app.zippie.companion

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import app.zippie.companion.design.Ink
import app.zippie.companion.design.Kind
import app.zippie.companion.design.Space

/**
 * Why this phone is, or is not, reachable - one sentence, then the detail.
 *
 * The composable RENDERS and does not decide. Every judgement (which failure,
 * which tone, whether via-router counts as good) lives in [Diagnostics], where
 * the JVM unit tests reach it. The iOS app splits the same way and for the same
 * reason: a sentence computed inside a view shipped wrong once and could not be
 * tested.
 */
@Composable
fun DiagnosticsScreen(
    diagnostics: Diagnostics,
    measuring: Boolean,
    onRefresh: () -> Unit,
    onBack: () -> Unit,
    // PASSED IN, not read from a Context here, so this stays a pure function of
    // its arguments and can be previewed and tested without device-protected
    // storage. Defaulted so existing callers and previews are unaffected.
    bootLog: List<String> = emptyList(),
) {
    Column(
        Modifier
            .verticalScroll(rememberScrollState())
            .padding(horizontal = 20.dp, vertical = 24.dp),
    ) {
        // A VISIBLE WAY OUT, because the system gesture is not one.
        //
        // This screen shipped with `BackHandler` as its ONLY exit. That is a
        // correct handler and still a trap: the Pixel runs gesture navigation,
        // so there is no back BUTTON anywhere on the device, and the whole
        // screen is a vertical scroller - an edge swipe on a phone in a case,
        // or by anyone who has not learned the gesture, does nothing visible.
        // Reported from the device on 2026-08-12 as simply "cant exit
        // diagnostics screen", which is exactly what it looks like from the
        // outside: a screen with no door.
        //
        // Deliberately at the TOP, before the scrolling content: a control
        // placed after the rows is below the fold on a screen whose length
        // depends on how many diagnostics there are, which is the same bug
        // wearing a different hat.
        TextButton(onClick = onBack, contentPadding = PaddingValues(0.dp)) {
            Text("< Back")
        }
        Spacer(Modifier.height(4.dp))
        Text("DIAGNOSTICS", style = Kind.section)
        Spacer(Modifier.height(6.dp))
        Text(diagnostics.headline, style = Kind.display)

        // The one thing worth saying above everything else. A phone reachable
        // ONLY because some router forwards for it is one SSID change from
        // unmanageable - which is exactly how a managed Pixel went dark.
        if (diagnostics.tailnet is TailnetPath.ViaRouter) {
            Spacer(Modifier.height(10.dp))
            Text(
                "This phone has no Tailscale of its own. It can reach the MDM " +
                    "only while on this network.",
                style = Kind.body,
                color = Ink.secondary,
            )
        }

        Spacer(Modifier.height(20.dp))
        diagnostics.rows().forEachIndexed { i, row ->
            if (i > 0) HorizontalDivider()
            DiagnosticRowView(row)
        }

        Spacer(Modifier.height(20.dp))
        OutlinedButton(onClick = onRefresh, enabled = !measuring) {
            Text(if (measuring) "Measuring..." else "Refresh")
        }

        // THE PHONE'S OWN ACCOUNT OF THE LAST BOOT (#186). Surfaced here
        // because the alternative is adb, and the phone this matters most for
        // is one in a car that nobody can plug into. Newest last, matching the
        // file, so the end of the list is the most recent thing that happened.
        if (bootLog.isNotEmpty()) {
            Spacer(Modifier.height(28.dp))
            Text("BOOT LOG", style = Kind.section)
            Spacer(Modifier.height(6.dp))
            Text(
                "Written to storage that survives a reboot and is readable " +
                    "before unlock. logcat is not.",
                style = Kind.body,
                color = Ink.secondary,
            )
            Spacer(Modifier.height(10.dp))
            bootLog.forEach { line ->
                Text(line, style = Kind.body, color = Ink.secondary)
            }
        }
    }
}

@Composable
private fun DiagnosticRowView(row: DiagnosticRow) {
    Column(Modifier.padding(vertical = 10.dp)) {
        Row(Modifier.fillMaxWidth()) {
            Text(row.label, style = Kind.body)
            Spacer(Modifier.weight(1f))
            Text(
                row.value,
                style = Kind.body,
                color = toneColor(row.tone),
            )
        }
        row.hint?.let {
            Spacer(Modifier.height(2.dp))
            Text(
                it,
                style = Kind.caption,
                color = Ink.secondary,
            )
        }
    }
}

/**
 * The ONLY place a tone becomes a colour, and a `when` with no else so a new
 * tone in [Diagnostics] is a compile error here rather than a silently grey row.
 */
@Composable
private fun toneColor(tone: Tone): Color = when (tone) {
    Tone.GOOD -> Ink.primary
    Tone.BAD -> Ink.down
    Tone.UNKNOWN -> Ink.secondary
    Tone.NOTE -> Ink.degraded
}
