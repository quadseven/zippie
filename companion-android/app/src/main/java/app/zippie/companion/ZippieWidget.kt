package app.zippie.companion

import android.content.Context
import android.content.res.Configuration
import androidx.compose.ui.unit.DpSize
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.glance.GlanceId
import androidx.glance.GlanceModifier
import androidx.glance.LocalContext
import androidx.glance.LocalSize
import androidx.glance.appwidget.GlanceAppWidget
import androidx.glance.appwidget.SizeMode
import androidx.glance.appwidget.provideContent
import androidx.glance.background
import androidx.glance.layout.Alignment
import androidx.glance.layout.Box
import androidx.glance.layout.Column
import androidx.glance.layout.Row
import androidx.glance.layout.Spacer
import androidx.glance.layout.cornerRadius
import androidx.glance.layout.fillMaxSize
import androidx.glance.layout.height
import androidx.glance.layout.padding
import androidx.glance.layout.size
import androidx.glance.layout.width
import androidx.glance.text.FontWeight
import androidx.glance.text.Text
import androidx.glance.text.TextStyle
import androidx.glance.unit.ColorProvider
import androidx.compose.runtime.Composable
import app.zippie.companion.design.Tok

/**
 * The home-screen widget: "is it working, and on what", without opening the app
 * (quadseven/zippie#176).
 *
 * THE WIDGET DERIVES NOTHING. [provideGlance] reads the persisted report
 * ([RelayStatusStore.readPersisted] - the fresh-process read, never the
 * in-memory flow, which is empty in whatever process the launcher happens to
 * start for the redraw) and hands it to [WidgetContent.from]. Every field on
 * screen comes from that one object. See WidgetContent.kt for why a third
 * surface deriving its own claim would be the worst kind of wrong.
 *
 * STALENESS IS EVALUATED AT DRAW TIME. [WidgetContent.from] defaults nowMs to
 * the instant of the call, and we call it inside [provideGlance] - so a widget
 * the launcher redraws twenty minutes after the relay died shows NotReporting,
 * not the last good verdict. That is rule 2 of #176, and it is the reason we
 * do NOT cache the content between draws.
 *
 * Small: the verdict word and the sentence, one accent dot. Medium: small
 * plus the leg list. No numbers, no buttons (interactivity is out of scope
 * for #176), no chart.
 */
class ZippieWidget : GlanceAppWidget() {

    override val sizeMode: SizeMode = SizeMode.Responsive(setOf(SmallSize, MediumSize))

    override suspend fun provideGlance(context: Context, id: GlanceId) {
        val content = WidgetContent.from(RelayStatusStore.readPersisted(context))
        provideContent {
            if (LocalSize.current.width <= SmallSize.width) {
                WidgetColumn(content, withLegs = false)
            } else {
                WidgetColumn(content, withLegs = true)
            }
        }
    }

    companion object {
        val SmallSize = DpSize(120.dp, 110.dp)
        val MediumSize = DpSize(220.dp, 120.dp)
    }
}

/** Light or dark, resolved from the configuration - Glance has no Compose
 *  theme here, so this is the manual equivalent of Ink.pick. Every colour
 *  still comes from [Tok]; a token change moves the widget with everything
 *  else (acceptance criterion 4 of #176). */
@Composable
private fun isDark(): Boolean {
    val uiMode = LocalContext.current.resources.configuration.uiMode
    return (uiMode and Configuration.UI_MODE_NIGHT_MASK) == Configuration.UI_MODE_NIGHT_YES
}

/** The one accent, and it means exactly one thing: TRAFFIC IS MOVING RIGHT NOW.
 *  LIVE -> live, IDLE -> degraded (holding on, not failing - amber, because
 *  crying wolf in a car is worse than saying nothing), DOWN -> down. */
@Composable
private fun toneColor(tone: WidgetContent.Tone): ColorProvider {
    val dark = isDark()
    return ColorProvider(
        when (tone) {
            WidgetContent.Tone.LIVE -> if (dark) Tok.liveDark else Tok.liveLight
            WidgetContent.Tone.IDLE -> if (dark) Tok.degradedDark else Tok.degradedLight
            WidgetContent.Tone.DOWN -> if (dark) Tok.downDark else Tok.downLight
        },
    )
}

@Composable
private fun groundColor(): ColorProvider {
    val dark = isDark()
    return ColorProvider(if (dark) Tok.groundDark else Tok.groundLight)
}

@Composable
private fun primaryText(): ColorProvider {
    val dark = isDark()
    return ColorProvider(if (dark) Tok.primaryDark else Tok.primaryLight)
}

@Composable
private fun secondaryText(): ColorProvider {
    val dark = isDark()
    return ColorProvider(if (dark) Tok.secondaryDark else Tok.secondaryLight)
}

@Composable
private fun WidgetColumn(content: WidgetContent, withLegs: Boolean) {
    Column(
        modifier = GlanceModifier.fillMaxSize()
            .background(groundColor())
            .cornerRadius(16.dp)
            .padding(16.dp),
        verticalAlignment = Alignment.Top,
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Box(
                modifier = GlanceModifier.size(10.dp)
                    .cornerRadius(5.dp)
                    .background(toneColor(content.tone)),
            ) {}
            Spacer(GlanceModifier.width(8.dp))
            Text(
                text = content.headline,
                style = TextStyle(
                    color = primaryText(),
                    fontSize = 17.sp,
                    fontWeight = FontWeight.Bold,
                ),
                maxLines = 1,
            )
        }
        Spacer(GlanceModifier.height(6.dp))
        Text(
            text = content.detail,
            style = TextStyle(
                color = secondaryText(),
                fontSize = 13.sp,
            ),
            maxLines = 4,
        )
        if (withLegs && content.legs.isNotEmpty()) {
            Spacer(GlanceModifier.height(8.dp))
            content.legs.forEach { leg ->
                LegRow(leg)
                Spacer(GlanceModifier.height(4.dp))
            }
        }
    }
}

/**
 * One leg row for the medium widget. The state dot reuses the tone mapping:
 * carrying is the live accent, idle is degraded, down is down. There is no
 * fourth state here - WidgetContent.Leg deliberately carries only what
 * RelayStats can back with evidence (see its doc), so a reserve leg never
 * appears as a failure and never appears at all from this surface.
 */
@Composable
private fun LegRow(leg: WidgetContent.Leg) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Box(
            modifier = GlanceModifier.size(8.dp)
                .cornerRadius(4.dp)
                .background(
                    toneColor(
                        when (leg.state) {
                            WidgetContent.Leg.State.CARRYING -> WidgetContent.Tone.LIVE
                            WidgetContent.Leg.State.IDLE -> WidgetContent.Tone.IDLE
                            WidgetContent.Leg.State.DOWN -> WidgetContent.Tone.DOWN
                        },
                    ),
                ),
        ) {}
        Spacer(GlanceModifier.width(8.dp))
        Text(
            text = leg.label,
            style = TextStyle(
                color = primaryText(),
                fontSize = 13.sp,
            ),
            maxLines = 1,
        )
    }
}
