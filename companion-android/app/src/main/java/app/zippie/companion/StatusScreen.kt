package app.zippie.companion

import androidx.compose.animation.core.LinearOutSlowInEasing
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Info
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import app.zippie.companion.design.Hairline as Rule
import app.zippie.companion.design.Ink
import app.zippie.companion.design.Kind
import app.zippie.companion.design.Space

// TRACK GEOMETRY, NOT SPACING. A 3dp rule is the mark itself - the same call
// LegRow.swift makes - and the spacing scale in Space starts at 4dp because it
// measures gaps between things, not the things. Naming these here is cheaper
// than reaching for a token that does not mean this.
private val TrackHeight = 3.dp
private val SplitGap = 2.dp

/**
 * The screen that answers the two-second question: is this bond carrying, and
 * is THIS phone one of the legs.
 *
 * Refused, deliberately: the hero-throughput template every bonding app ships.
 * A big number cannot say whether a leg is held in reserve or broken, and those
 * are the two states this system keeps confusing. The hero here is a sentence,
 * because the answer is a sentence.
 *
 * NARROWED TO WHAT THE OPERATOR ACTUALLY OPENS THIS SCREEN FOR, matching
 * BondScreen.swift's scope. Until now this file also carried the relay
 * start/stop control, the announce token editor and the client-mode fallback -
 * everything the app can do, in declaration order, on one scroll. That is the
 * exact shape RelayScreen.swift's own header comment rejects for the SAME
 * reason: the thing someone opens this screen to check was three sections down
 * a page that also asked them to think about a router write token. Those
 * controls now live on their own destination - see RelayScreen.kt - and this
 * file is left with the sentence, the chart, the legs, and what it cost.
 */
@Composable
fun StatusScreen(
    state: BondUiState,
    onRefresh: () -> Unit,
    onOpenDiagnostics: () -> Unit = {},
) {
    Column(
        Modifier
            .verticalScroll(rememberScrollState())
            .padding(horizontal = Space.margin, vertical = Space.roomy),
    ) {
        // TOP RIGHT, on the screen people already open. A diagnostics screen
        // behind a tab nobody visits does not exist at the moment it is needed,
        // which is when the phone has gone quiet. AN ICON, not a text button -
        // BondScreen.swift reaches for the same top-right icon-only chrome for
        // the same reason: the words belong to the sentence below, not to the
        // row above it.
        Row(Modifier.fillMaxWidth()) {
            Spacer(Modifier.weight(1f))
            IconButton(onClick = onOpenDiagnostics) {
                Icon(
                    Icons.Filled.Info,
                    contentDescription = "Diagnostics",
                    tint = Ink.live,
                )
            }
        }
        Text(state.headline, style = Kind.display, color = Ink.primary)
        Spacer(Modifier.height(Space.tight))
        Text(state.subhead, style = Kind.body, color = Ink.secondary)
        Spacer(Modifier.height(Space.hair))
        Text(state.decision.summary, style = Kind.caption, color = Ink.tertiary)

        // THE GLANCE ANSWER, above the detail that explains it - the same order
        // BondScreen.swift uses. The leg rows below are what this chart
        // decomposes into, and reading them the other way round means scanning
        // five rows to work out a total the chart states as a shape.
        //
        // Drawn whenever there is a history to draw, INCLUDING while the console
        // is failing. The rows are dropped in that case because they claim to be
        // current; the chart claims to be history, and an outage is part of the
        // history - it shows up as the break in the bars that it actually was.
        if (state.throughput.snapshotCount > 0) {
            Spacer(Modifier.height(Space.section))
            SectionTitle("Throughput", "Stacked by connection. The top edge is the total.")
            BondThroughputChart(state.throughput)
        }

        // THE HEADING TELLS THE TRUTH ABOUT SCOPE, pre-computed by
        // BondLegs.legsHeading so this screen never has to guess: "Connections
        // - 2 of 4 carrying" when the router answers, "What this phone
        // carried" when only this phone's own counters are knowable.
        Spacer(Modifier.height(Space.section))
        SectionTitle(state.legsHeading)

        if (state.legs.isEmpty()) {
            Text(
                when {
                    state.consoleError != null -> "The console did not answer, so the bond cannot be listed from here."
                    state.pathsMissing -> "The router answered, but its reply carried no list of links."
                    else -> "The router lists no links at all."
                },
                style = Kind.body,
                color = Ink.secondary,
            )
        } else {
            state.legs.forEachIndexed { index, leg ->
                if (index > 0) Rule()
                LegRow(leg)
            }
        }

        // WHAT IT COST. A distinct headline stat, matching BondScreen.swift's
        // "Session total" - not folded into the byte counts on the Relay tab,
        // which answer a different question (day/month spend against a cap).
        // This one answers "how much has THIS run carried", and it is the last
        // thing on the page before the footer, same as iOS.
        SessionTotal(state)

        Spacer(Modifier.height(Space.section))
        Rule()
        Spacer(Modifier.height(12.dp))
        // Said once, plainly, because it is true and load-bearing: this is why
        // lending a phone's cellular is not a privacy question.
        Footnote("This phone forwards traffic without reading it.")
        state.consoleUrl?.let { Footnote("Console: $it") }
        state.fetchedAtMs?.let { Footnote("Router answered ${Fmt.age(state.nowMs, it)}.") }
        if (state.fetchedAtMs == null) Footnote("The router has not answered yet.")
        state.consoleError?.let {
            Footnote("Last console error: $it", Ink.down)
        }
        Spacer(Modifier.height(12.dp))
        OutlinedButton(onClick = onRefresh, colors = ButtonDefaults.outlinedButtonColors(contentColor = Ink.live)) { Text("Refresh now") }
    }
}

/**
 * What this run has actually relayed, matching BondScreen.swift's "Session
 * total". Shown only while the relay report is live and CURRENT - a corpse's
 * byte count under a fresh Connections list would read as today's spending
 * when it is whatever the relay had counted before it stopped, the same trap
 * [RelayStats.sessionBytes]'s doc warns about.
 */
@Composable
private fun SessionTotal(state: BondUiState) {
    val report = state.relay ?: return
    if (report.isStale(state.nowMs)) return
    Spacer(Modifier.height(Space.section))
    SectionTitle("Session total")
    Row(verticalAlignment = Alignment.Bottom) {
        Text(Fmt.bytes(report.stats.sessionBytes), style = Kind.figure(17, FontWeight.Medium), color = Ink.primary)
        Spacer(Modifier.width(Space.tight))
        Text("of cellular relayed", style = Kind.body, color = Ink.secondary)
    }
    // Not an error colour: the budget working as designed is not a fault, the
    // same reasoning RelaySection gives it on the Relay tab.
    report.stats.budgetExhausted?.let {
        Text(it, style = Kind.caption, color = Ink.degraded)
    }
}

/**
 * One connection in the bond.
 *
 * Ported to match ZippieCompanionApp/Design/LegRow.swift, which this screen had
 * drifted a long way from: the row was four stacked lines of grey caption text
 * with no bar, no alignment and nothing to scan down.
 *
 * THE ROW SAYS TWO THINGS THAT ARE NOT THE SAME THING. Whether the leg is
 * carrying, and whether it is well. The state word is coloured by the first and
 * the traffic bar is tinted by the second, so a leg that is degraded AND
 * carrying reads "carrying, degraded" in the live accent above an amber bar -
 * which is exactly what it is. Colouring that word by health instead would
 * spend the one accent in the language on something other than "carrying right
 * now", and Theme.kt says in as many words that this is what makes an accent
 * stop meaning anything.
 */
@Composable
private fun LegRow(leg: Leg) {
    val reading = LegTraffic.read(leg.upBytes, leg.downBytes)
    Column(
        Modifier
            .padding(vertical = Space.snug)
            // ONE ELEMENT TO A SCREEN READER. Seven separate texts read out one
            // at a time turn a row that is scannable in a second into seven
            // swipes, and Leg.accessibilityDescription says the same thing in
            // the order someone would ask for it.
            .semantics(mergeDescendants = true) {
                contentDescription = leg.accessibilityDescription
            },
    ) {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            // WEIGHTED, so the state word and the round-trip time on the right
            // are measured first and can never be squeezed off the row by a long
            // leg label. The name ellipsizes instead - it is the one thing here
            // that stays recognisable from its first few words.
            Row(Modifier.weight(1f), verticalAlignment = Alignment.CenterVertically) {
                Text(
                    leg.name,
                    style = Kind.label,
                    color = Ink.primary,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.weight(1f, fill = false),
                )
                if (leg.isYou) {
                    Spacer(Modifier.width(Space.tight))
                    // The actual question someone holding this phone has is "is
                    // MY phone helping". Answering it takes one word, not an
                    // icon they have to learn. In secondary ink, not the accent:
                    // being this phone is not the same as carrying, and a leg
                    // that is both would otherwise say it twice in one colour.
                    Text("this phone", style = Kind.caption, color = Ink.secondary)
                }
            }
            Spacer(Modifier.width(Space.tight))
            // THE WORD, not just a colour. "degraded" and "not in the bond" are
            // different problems with different fixes, and a grey bar says
            // neither.
            Text(
                leg.stateWord,
                style = Kind.caption,
                color = if (leg.isCarrying) Ink.live else Ink.secondary,
            )
            // Shown only where it was actually measured. A "--" placeholder
            // implies a value that exists and is being withheld; a leg with no
            // RTT concept at all is better served by an empty column. The full
            // sentence is still in the screen-reader description.
            leg.rttShort?.let {
                Spacer(Modifier.width(Space.tight))
                Text(
                    it,
                    style = Kind.figure(15),
                    color = if (leg.isCarrying) Ink.secondary else Ink.tertiary,
                )
            }
        }

        Spacer(Modifier.height(Space.tight))
        TrafficBar(reading, leg.state)

        leg.usageNote?.let {
            Text(it, style = Kind.figure(13), color = Ink.secondary)
        }
        leg.note?.let {
            // The reason lives WITH the leg, not in a status line at the bottom
            // of the screen. The router's own words next to the row they
            // describe are the difference between a diagnosis and a mood.
            Text(
                it,
                style = Kind.caption,
                color = if (leg.state == LegState.DOWN) Ink.down else Ink.secondary,
            )
        }
        leg.shadowNote?.let {
            // Rendered even when the leg above is perfectly healthy, and
            // coloured the same regardless of ITS state, because this sentence
            // is not about this leg - it is about an uplink that is working and
            // in no leg at all (#212).
            Text(it, style = Kind.caption, color = Ink.secondary)
        }
    }
}

/**
 * The two directions this leg has actually carried, as a proportion of each
 * other.
 *
 * REPLACED A SHARE BAR THAT WAS DECORATION on iOS: with one leg the share is
 * always 100%, so the bar rendered as a full-width slab that said nothing while
 * being the loudest thing on the screen - the progress-bar-as-content habit
 * exactly. Up against down is genuinely measured, differs run to run, and
 * answers a question someone might have: is this phone uploading for the bond,
 * or pulling down?
 *
 * The three readings come from LegTraffic, which is plain Kotlin with tests
 * behind it. This function only paints them.
 */
@Composable
private fun TrafficBar(reading: TrafficReading, state: LegState) {
    val tint = barTint(state)
    when (reading) {
        // NO TRACK AT ALL. An empty track is a measurement of zero, and this is
        // the absence of a measurement - the router published no counter for
        // this leg, and the screen must not invent one.
        TrafficReading.Unmeasured ->
            Text(LegTraffic.UNMEASURED_CAPTION, style = Kind.figure(13), color = Ink.tertiary)

        // A leg that is UP but has carried nothing renders as an empty track.
        // That distinction is the whole point: it is the failure this system
        // keeps having, and a filled bar would hide it.
        TrafficReading.Nothing ->
            Spacer(
                Modifier
                    .fillMaxWidth()
                    .height(TrackHeight)
                    .background(Ink.rule, CircleShape),
            )

        is TrafficReading.Split -> {
            // THE ONE AUTHORED MOTION IN THE APP. The split settles rather than
            // jumps, so a glance at a moving bar reads as live rather than as a
            // redraw.
            val fraction by animateFloatAsState(
                targetValue = reading.upFraction,
                animationSpec = tween(durationMillis = 550, easing = LinearOutSlowInEasing),
                label = "trafficSplit",
            )
            // CLAMPED OFF BOTH ENDS. Row weights must be strictly positive - a
            // weight of 0f throws - and a leg that is 100% one direction is the
            // normal case here, not the exotic one: every companion leg starts
            // out having sent and received nothing but probes.
            val up = fraction.coerceIn(0.001f, 0.999f)
            Row(Modifier.fillMaxWidth().height(TrackHeight)) {
                Spacer(Modifier.weight(up).fillMaxHeight().background(tint, CircleShape))
                Spacer(Modifier.width(SplitGap))
                Spacer(
                    Modifier
                        .weight(1f - up)
                        .fillMaxHeight()
                        .background(tint.copy(alpha = 0.32f), CircleShape),
                )
            }
            Spacer(Modifier.height(Space.hair))
            Text(LegTraffic.caption(reading), style = Kind.figure(13), color = Ink.secondary)
        }
    }
}

/**
 * The bar's tint says HEALTH, while the state word above it says membership.
 * See the note on LegRow for why those are deliberately two different colours
 * on one row.
 */
@Composable
private fun barTint(state: LegState): Color = when (state) {
    // `live` is the ONLY accent in the language and means exactly one thing:
    // carrying traffic right now. Spending it anywhere else is what makes an
    // accent stop meaning anything.
    LegState.CARRYING -> Ink.live
    LegState.DEGRADED -> Ink.degraded
    LegState.DOWN -> Ink.down
    // The quietest ink on the page, on purpose. A reserve leg is not news, and
    // drawing it like a fault is the failure this screen exists to avoid.
    LegState.RESERVE -> Ink.tertiary
    LegState.IDLE -> Ink.tertiary
    // Same ink as RESERVE and IDLE, and for the same reason: an absent leg
    // is not news either.
    LegState.ABSENT -> Ink.tertiary
}

/**
 * A section heading with the rhythm the page expects - more air above than
 * below, matching Controls.swift's SectionHead. Not private: RelayScreen.kt
 * shares this rather than growing its own copy.
 */
@Composable
fun SectionTitle(text: String, note: String? = null) {
    // COLOUR STATED. Without it the Text inherits LocalContentColor, which on a
    // Surface painted with a token colour Material does not recognise resolves
    // to a near-black - so "Links", "This phone" and "Client mode" were all but
    // invisible on the dark ground. Every section heading on the screen.
    Text(text, style = Kind.section, color = Ink.secondary)
    // The note says how to READ the thing below, which for a chart is not
    // optional: stacked and overlaid look nearly identical and mean completely
    // different things.
    note?.let { Text(it, style = Kind.caption, color = Ink.tertiary) }
    Spacer(Modifier.height(Space.tight))
}

/** Not private for the same reason as [SectionTitle]. */
@Composable
fun Footnote(text: String, color: Color = Ink.secondary) {
    Text(text, style = Kind.caption, color = color)
}

// The local 1dp bar is gone: design/Theme.kt's Hairline draws Dp.Hairline, the
// thinnest line the display can render, which is what keeps a column of rules
// reading as structure rather than as a table.
