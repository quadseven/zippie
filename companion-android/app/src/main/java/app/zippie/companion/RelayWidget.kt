package app.zippie.companion

import android.content.Context
import android.util.Log
import androidx.compose.ui.unit.DpSize
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.glance.GlanceId
import androidx.glance.GlanceModifier
import androidx.glance.LocalSize
import androidx.glance.appwidget.GlanceAppWidget
import androidx.glance.appwidget.GlanceAppWidgetReceiver
import androidx.glance.appwidget.SizeMode
import androidx.glance.appwidget.appWidgetBackground
import androidx.glance.appwidget.cornerRadius
import androidx.glance.appwidget.provideContent
import androidx.glance.appwidget.updateAll
import androidx.glance.background
import androidx.glance.layout.Alignment
import androidx.glance.layout.Box
import androidx.glance.layout.Column
import androidx.glance.layout.Row
import androidx.glance.layout.Spacer
import androidx.glance.layout.fillMaxSize
import androidx.glance.layout.fillMaxWidth
import androidx.glance.layout.height
import androidx.glance.layout.padding
import androidx.glance.layout.width
import androidx.glance.text.FontWeight
import androidx.glance.text.Text
import androidx.glance.text.TextStyle
import androidx.glance.color.ColorProvider as colorProvider
import androidx.glance.unit.ColorProvider
import app.zippie.companion.design.Space
import app.zippie.companion.design.Tok
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch

/**
 * The Android home-screen widget: "is it working, and on what", without opening
 * the app (quadseven/zippie#244).
 *
 * ## THIS FILE LAYS OUT AND DECIDES NOTHING
 *
 * Every headline, sentence, tone and leg row arrives in [WidgetContent], already
 * built by `WidgetContent.from` from [RelayVerdict] - the same state machine the
 * app's own screens read, and the twin of ZippieCompanionKit's `WidgetTimeline`
 * that feeds the iOS widget. There is no branch in here that turns evidence
 * into a claim. `WidgetContentTest` is what holds the claims honest.
 *
 * ## COLOURS COME FROM THE GENERATED TOKENS
 *
 * [WidgetPalette] reads `design/Tokens.generated.kt`, which `design/generate.py`
 * emits from `design/tokens.json` - the same source the iOS widget and the hub
 * read. The obvious alternative, `GlanceTheme.colors`, is the MATERIAL palette:
 * a different design system, and the one surface a token change would have
 * missed. `WidgetPaletteWiringTest` fails if a colour here stops coming from
 * `Tok`.
 *
 * ## LIGHT, DARK, AND THE LAUNCHER'S THEMED MODE
 *
 * Every colour is a `ColorProvider(day, night)`, so the system picks the right
 * one per configuration with no branching in this file. Themed mode - the
 * launcher tinting widgets from the wallpaper - is handled by DECLARING OUR OWN
 * BACKGROUND: `appWidgetBackground()` plus an explicit `ground` fill means the
 * widget paints the zippie palette over whatever the launcher tinted behind it,
 * rather than letting a themed scrim wash out a sentence that is read at a
 * glance in a car.
 *
 * ## NO CLICK TARGET
 *
 * Deliberately, and not only because interactivity is out of scope for #244: the
 * iOS widget has none either, and two surfaces behaving differently about the
 * same fact is the failure this whole feature is guarded against.
 */
class RelayWidget : GlanceAppWidget() {

    /**
     * Two sizes, matching the `targetCellWidth`/`targetCellHeight` in
     * `res/xml/relay_widget_info.xml`.
     *
     * RESPONSIVE rather than `Exact` because a launcher is free to give a widget
     * any size it likes; `Exact` pins the rendering to one and gets clipped
     * everywhere else, which is a very literal way to lose the medium tile that
     * is the whole point of the feature. Large is absent on purpose - see
     * ZippieCompanionWidget/RelayWidget.swift, which reaches the same decision
     * from the other platform.
     */
    override val sizeMode: SizeMode = SizeMode.Responsive(
        setOf(DpSize(110.dp, 110.dp), DpSize(250.dp, 110.dp)),
    )

    override suspend fun provideGlance(context: Context, id: GlanceId) {
        // From DISK, not from RelayStatusStore.report. Android starts a fresh
        // process for the broadcast far more often than not, and an in-memory
        // StateFlow would answer null for every one of them - a widget
        // confidently rendering "Off" while the relay, in its own process, was
        // carrying traffic.
        val content = WidgetContent.from(
            report = RelayStatusStore.readPersisted(context),
            bond = RelayBondStore.read(context),
            nowMs = System.currentTimeMillis(),
        )
        provideContent {
            val wide = LocalSize.current.width >= MEDIUM_MIN_WIDTH

            Column(
                modifier = GlanceModifier
                    .fillMaxSize()
                    .appWidgetBackground()
                    .cornerRadius(CORNER)
                    .background(WidgetPalette.page)
                    .padding(Space.base),
            ) {
                Row(verticalAlignment = Alignment.Vertical.CenterVertically) {
                    // THE ONE ACCENT DOT, and a bullet glyph rather than an oval
                    // shape because Glance has no tintable shape primitive: a
                    // drawable's colour is fixed at build time, so it could not
                    // follow the token through a day/night change, and a
                    // hand-drawn circle would have put the one accent on this
                    // surface outside the token system. Spelled as an escape so
                    // this file stays ASCII.
                    Text(text = DOT, style = TextStyle(color = WidgetPalette.tone(content.tone)))
                    Spacer(GlanceModifier.width(Space.hair))
                    Text(text = content.headline, style = HEADLINE, maxLines = 1)
                }
                Text(text = content.detail, style = BODY, maxLines = if (wide) 2 else 3)

                // No leg section at all when there is nothing honest to say
                // about a leg yet - an empty section with a dash would be
                // exactly the placeholder `neverStateUnmeasured` forbids.
                //
                // A bond with more legs than the tile can show is CLIPPED by the
                // launcher rather than trimmed here. A silent cap would make a
                // partial list read as the whole bond, which is the same class
                // of lie as a zeroed counter.
                if (wide && content.legs.isNotEmpty()) {
                    Spacer(GlanceModifier.height(Space.hair))
                    Box(
                        modifier = GlanceModifier
                            .fillMaxWidth()
                            .height(HAIRLINE)
                            .background(WidgetPalette.hairline),
                    ) {}
                    Spacer(GlanceModifier.height(Space.hair))
                    Column {
                        content.legs.forEach { leg ->
                            Row(verticalAlignment = Alignment.Vertical.CenterVertically) {
                                Text(
                                    text = DOT,
                                    style = TextStyle(
                                        color = WidgetPalette.leg(leg.state),
                                        fontSize = LABEL_SIZE,
                                    ),
                                )
                                Spacer(GlanceModifier.width(Space.tight))
                                Text(text = leg.label, style = LABEL, maxLines = 1)
                                Spacer(GlanceModifier.width(Space.hair))
                                Text(
                                    text = leg.word,
                                    style = LABEL.copy(color = legWordColor(leg.state)),
                                    maxLines = 1,
                                )
                            }
                        }
                    }
                }
            }
        }
    }

    companion object {
        /**
         * Where the medium tile starts. Below this there is no room for a leg
         * list beside the sentence, so the widget draws the small layout rather
         * than a medium one squeezed into a small tile.
         */
        val MEDIUM_MIN_WIDTH = 180.dp
    }
}

/**
 * The accent is spent on carrying and on nothing else; every other row's word is
 * supporting text.
 *
 * NOT annotated: it composes nothing, it only picks a colour, so it is a plain
 * function the layout can call from inside a Glance composition.
 */
private fun legWordColor(state: WidgetContent.Leg.State): ColorProvider =
    if (state == WidgetContent.Leg.State.CARRYING) WidgetPalette.live else WidgetPalette.body

/**
 * Type. NOT the app's 32sp display size - a widget tile is a fraction of the
 * height, and the app's size would clip the sentence that IS the content.
 *
 * Declared once, at file scope, because these are plain immutable values with
 * no Android dependency - Glance builds its RemoteViews from whatever
 * `TextStyle` it is handed at composition time.
 */
private val HEADLINE =
    TextStyle(color = WidgetPalette.text, fontSize = 18.sp, fontWeight = FontWeight.Bold)
private val BODY = TextStyle(color = WidgetPalette.body, fontSize = 12.sp)
private val LABEL = TextStyle(color = WidgetPalette.text, fontSize = 12.sp)

private const val DOT = "\u2022"
private val CORNER = 16.dp
private val HAIRLINE = 1.dp
private val LABEL_SIZE = 12.sp

/**
 * Declares the widget to the launcher. `GlanceAppWidgetReceiver` IS an
 * `AppWidgetProvider`, so this is the whole registration on the code side; the
 * manifest entry carries the intent filter and the provider XML.
 */
class RelayWidgetReceiver : GlanceAppWidgetReceiver() {
    override val glanceAppWidget: GlanceAppWidget = RelayWidget()
}

/**
 * The colours, read out of the generated tokens.
 *
 * THREE COLOURS FOR SIX LEG STATES, and the collapse is the point: `live` is
 * spent on carrying and nothing else, and a reserve leg and a leg that is up but
 * not carrying are both `tertiary` because neither is news - which is what
 * `design/tokens.json` means by "the quietest text; a reserve leg is not news".
 * The DISTINCTION rides the row's word, decided by `WidgetContent.Leg.word`, so
 * `reserveIsNotBroken` survives without spending a second accent on it.
 *
 * `ColorProvider` is both an interface (`androidx.glance.unit`) and a factory
 * function (`androidx.glance.color`), hence the aliased import above.
 */
internal object WidgetPalette {
    val live: ColorProvider = colorProvider(Tok.liveLight, Tok.liveDark)
    val down: ColorProvider = colorProvider(Tok.downLight, Tok.downDark)
    val tertiary: ColorProvider = colorProvider(Tok.tertiaryLight, Tok.tertiaryDark)
    val text: ColorProvider = colorProvider(Tok.primaryLight, Tok.primaryDark)
    val body: ColorProvider = colorProvider(Tok.secondaryLight, Tok.secondaryDark)
    val page: ColorProvider = colorProvider(Tok.groundLight, Tok.groundDark)
    val hairline: ColorProvider = colorProvider(Tok.ruleLight, Tok.ruleDark)

    fun tone(tone: WidgetContent.Tone): ColorProvider = when (tone) {
        WidgetContent.Tone.LIVE -> live
        WidgetContent.Tone.IDLE -> tertiary
        WidgetContent.Tone.DOWN -> down
    }

    fun leg(state: WidgetContent.Leg.State): ColorProvider = when (state) {
        WidgetContent.Leg.State.CARRYING -> live
        WidgetContent.Leg.State.DOWN -> down
        WidgetContent.Leg.State.RESERVE,
        WidgetContent.Leg.State.NOT_IN_BOND,
        WidgetContent.Leg.State.PRESENT_NOT_CARRYING,
        WidgetContent.Leg.State.IDLE -> tertiary
    }
}

/**
 * Asks the launcher to redraw the tile, throttled.
 *
 * ## WHY THIS EXISTS AT ALL
 *
 * A widget with no refresh trigger renders once, at placement, and then shows
 * whatever was on disk at that moment for the rest of its life. Both records
 * this feature reads are written continuously by a long-lived relay, so a tile
 * that never redraws is a tile that is wrong within seconds of being correct -
 * and wrong in the reassuring direction, which is the direction nobody checks.
 *
 * The alternative, `updatePeriodMillis` in the provider XML, is floored by the
 * platform at thirty minutes and cannot be relied on at all; see
 * `res/xml/relay_widget_info.xml`.
 *
 * ## WHY IT IS THROTTLED
 *
 * The relay republishes every two seconds for as long as it runs, which on this
 * phone is potentially forever, and each `updateAll` makes the launcher rebuild
 * and redraw. Thirty seconds is well inside the window in which a stale claim
 * would matter - `RelayReport.STALENESS_MS` is ten - and it is a fifteenth of
 * the redraws. Correctness does not depend on the cadence at all: freshness is
 * decided inside `WidgetContent.from`, at the moment of the read, so a tile
 * redrawn late reads a late-but-honest answer rather than a stale one.
 */
internal object RelayWidgetRefresh {
    private const val TAG = "RelayWidgetRefresh"
    private const val MIN_INTERVAL_MS = 30_000L

    // LAZY ON PURPOSE. `request` is called from the relay's heartbeat thread and
    // from BootReceiver's direct-boot path, neither of which may be able to fail:
    // Glance's own state lives in credential-protected storage, so a launch
    // before first unlock throws, and that has to happen inside the try below
    // rather than in a field initialiser on the caller's thread.
    private val widget by lazy { RelayWidget() }
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)

    @Volatile
    private var lastRequestMs = 0L

    /**
     * @param force skip the throttle. Only for a clean stop, where the tile is
     *   currently holding a record that no longer exists and any delay is a
     *   claim the relay is still carrying.
     */
    fun request(context: Context, force: Boolean = false) {
        val now = System.currentTimeMillis()
        // Unsynchronised on purpose: this is a rate limiter, not a lock. Two
        // racing callers may both get through and issue one extra redraw, which
        // costs the launcher a frame and nothing else; making it correct would
        // mean putting a lock on the relay's heartbeat thread.
        if (!force && now - lastRequestMs < MIN_INTERVAL_MS) return
        lastRequestMs = now
        val app = context.applicationContext
        scope.launch {
            try {
                widget.updateAll(app)
            } catch (e: Exception) {
                Log.w(TAG, "home-screen widget refresh failed", e)
            }
        }
    }
}
