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
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.input.PasswordVisualTransformation
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
 */
@Composable
fun StatusScreen(
    state: BondUiState,
    onStartRelay: () -> Unit,
    onStopRelay: () -> Unit,
    onStartClient: () -> Unit,
    onRefresh: () -> Unit,
    onSaveToken: (String) -> Unit,
    onOpenDiagnostics: () -> Unit = {},
) {
    Column(
        Modifier
            .verticalScroll(rememberScrollState())
            .padding(horizontal = Space.margin, vertical = Space.roomy),
    ) {
        // TOP RIGHT, on the screen people already open. A diagnostics screen
        // behind a tab nobody visits does not exist at the moment it is needed,
        // which is when the phone has gone quiet.
        // The mode used to be stamped above the headline in uppercase -
        // "CONTRIBUTING" - which is a kicker, and a kicker is an ornament that
        // exists to announce a heading that can announce itself. Theme.swift
        // rejects the same habit for section headers in as many words. The mode
        // is already said properly in decision.summary two lines down, so
        // deleting it loses nothing and the sentence gets to lead.
        Row(Modifier.fillMaxWidth()) {
            Spacer(Modifier.weight(1f))
            TextButton(
                onClick = onOpenDiagnostics,
                colors = ButtonDefaults.textButtonColors(contentColor = Ink.live),
            ) { Text("Diagnostics", style = Kind.label) }
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

        Spacer(Modifier.height(Space.section))
        SectionTitle(if (state.legs.isEmpty()) "Links" else "Links in the bond")

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

        Spacer(Modifier.height(Space.section))
        SectionTitle("This phone")
        RelaySection(state, onStartRelay, onStopRelay, onSaveToken)

        Spacer(Modifier.height(Space.section))
        SectionTitle("Client mode")
        Text(
            // Said plainly rather than hidden behind a disabled button: the
            // tunnel comes up and carries NOTHING until the gomobile datapath
            // exists (#2246), and a button that looks ready would be a lie.
            "Bonding this phone's own links back home needs the gomobile datapath, " +
                "which is not in this build yet (#2246). The tunnel will establish and " +
                "carry nothing.",
            style = Kind.caption,
            color = Ink.secondary,
        )
        Spacer(Modifier.height(8.dp))
        OutlinedButton(onClick = onStartClient, colors = ButtonDefaults.outlinedButtonColors(contentColor = Ink.live)) { Text("Establish the tunnel anyway") }

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

@Composable
private fun RelaySection(
    state: BondUiState,
    onStartRelay: () -> Unit,
    onStopRelay: () -> Unit,
    onSaveToken: (String) -> Unit,
) {
    val report = state.relay
    val stats = report?.stats
    // THE ONE SENTENCE, evidence-gated (RelayVerdict.kt, ported from
    // ZippieCompanionKit/RelayVerdict.swift and quadseven/zippie#44): a claim
    // about the ROUTER is made only once something has actually arrived from
    // it, decided from a TIMESTAMP rather than a datagram count.
    // upDatagrams/downDatagrams never go down, so a count-based rule reads
    // "Carrying" forever after a single packet, including long after the
    // router stopped dialling this phone - that was the defect here.
    // AND the router's half, when this screen already holds it. The leg the
    // console publishes for THIS phone carries `never_handshaked`, and until
    // #281 that fact was rendered in the leg row below while the headline above
    // it said "Carrying" - the same screen contradicting itself for hours
    // during an outage. `isYou` is matched by relay endpoint, not by name, so a
    // relabelled leg still resolves.
    val routerSeesNothing = state.legs.any { it.isYou && it.neverAnswered }
    val verdict = RelayVerdict.evaluate(report, state.nowMs, routerSeesNothing = routerSeesNothing)

    // A carrier name is something iOS cannot report at all, so it is stated
    // here rather than inferred from the router's hand-written leg label.
    Text(
        state.carrier?.summary?.let { "Carrier: $it" } ?: "Carrier: not reported by the radio",
        style = Kind.caption,
        color = Ink.secondary,
    )
    Text(
        state.localIp?.let { "Wifi address: $it, listening on ${state.listenPort}" }
            ?: "No wifi address, so no leg can be matched to this phone",
        style = Kind.caption,
        color = Ink.secondary,
    )
    Spacer(Modifier.height(8.dp))

    val alarmed = verdict is RelayVerdict.NotReporting
    Text(
        verdict.headline,
        style = Kind.body,
        color = if (alarmed) Ink.down else Ink.primary,
    )
    Text(
        // Not yet named on Android - see the note on RelayVerdict.detail(router:)
        // for why there is no display-name source here to pass.
        verdict.detail(),
        style = Kind.caption,
        color = if (alarmed) Ink.down else Ink.secondary,
    )

    // Raw measurements, shown only while the report is live and current - a
    // corpse's byte counts sitting under "not reporting" would look like more
    // evidence when they are the opposite.
    if (stats != null && verdict !is RelayVerdict.Off && verdict !is RelayVerdict.NotReporting) {
        Text(
            "${Fmt.bytes(stats.upBytes)} up in ${stats.upDatagrams} datagrams, " +
                "${Fmt.bytes(stats.downBytes)} down in ${stats.downDatagrams}.",
            style = Kind.caption,
            color = Ink.secondary,
        )
        Text(
            if (stats.budget.isConfigured) {
                "Budget: ${Fmt.bytes(stats.dayUsedBytes)} today, " +
                    "${Fmt.bytes(stats.monthUsedBytes)} this month."
            } else {
                "No data cap set, so the relay will spend whatever the bond asks for."
            },
            style = Kind.caption,
            color = Ink.secondary,
        )
        if (stats.rejectedSources > 0) {
            Text(
                "Refused ${stats.rejectedSources} datagrams from senders that were not the router.",
                style = Kind.caption,
                color = Ink.secondary,
            )
        }
        stats.budgetExhausted?.let {
            // Not an error colour: the budget working as designed is not a
            // fault, and colouring it red would train the reader to fix it.
            Text("$it (${stats.budgetBlocked} datagrams refused)",
                style = Kind.caption)
        }
        stats.announce?.let {
            // NOT an error colour by default. "No token set" is a configuration
            // fact, and a refusal is the router's answer - only the second is
            // worth alarming about.
            Text(
                it,
                style = Kind.caption,
                color = Ink.secondary,
            )
        }
        stats.lastError?.let {
            Text(it, style = Kind.caption, color = Ink.down)
        }
    }

    Spacer(Modifier.height(Space.base))
    // ONE CONTROL, decided by the verdict already computed above.
    //
    // Both buttons used to render unconditionally, so a phone that was actively
    // carrying still offered "Start contributing" - which reads as "this is not
    // running" on the one screen whose entire job is saying whether it is. The
    // operator reported exactly that. RelayScreen.swift has said `if running`
    // since it was written; this is Android catching up.
    //
    // Off is the only verdict meaning no relay exists (RelayVerdict.Off: "no
    // report exists ... never been started, or just stopped"). Every other
    // verdict - including NotReporting, where the service is alive but wedged -
    // is a state you leave by stopping, so Stop is the honest control there.
    if (verdict is RelayVerdict.Off) {
        Button(
            onClick = onStartRelay,
            colors = ButtonDefaults.buttonColors(containerColor = Ink.live, contentColor = Ink.ground),
        ) { Text("Start contributing", style = Kind.label) }
    } else {
        OutlinedButton(
            onClick = onStopRelay,
            colors = ButtonDefaults.outlinedButtonColors(contentColor = Ink.live),
        ) { Text("Stop contributing", style = Kind.label) }
    }

    AnnounceSettings(state, onSaveToken)
}

/**
 * The one thing this phone needs from a person before it can join the bond.
 *
 * WHY IT IS ON THIS SCREEN AT ALL. Announcing is authenticated - the router
 * answers 401 without the token - and there is no settings screen to put it on.
 * A build with no way to enter the token would be an announce path that can
 * never run, which is worse than not having one: it looks finished.
 *
 * Masked, never echoed back, and never logged. The field starts empty even when
 * a token IS stored, because reading one back onto a screen is how a token ends
 * up in a screenshot of a bug report.
 */
@Composable
private fun AnnounceSettings(state: BondUiState, onSaveToken: (String) -> Unit) {
    var typed by remember { mutableStateOf("") }

    Spacer(Modifier.height(16.dp))
    Text(
        state.legName?.let { "This phone announces itself as \"$it\"." }
            ?: "This phone has not chosen a leg name yet.",
        style = Kind.caption,
        color = Ink.secondary,
    )
    Text(
        if (state.hasAnnounceToken) {
            "A console write token is stored, so this phone can announce itself. " +
                "A token saved while the relay is running takes effect when it is " +
                "next started."
        } else {
            "No console write token, so the router will refuse to add this phone " +
                "as a leg. It is the router's /var/lib/zippie/console_token."
        },
        style = Kind.caption,
        color = Ink.secondary,
    )
    Spacer(Modifier.height(8.dp))
    OutlinedTextField(
        value = typed,
        onValueChange = { typed = it },
        label = { Text("Console write token") },
        singleLine = true,
        visualTransformation = PasswordVisualTransformation(),
        modifier = Modifier.fillMaxWidth(),
        textStyle = Kind.body,
        // The caret and the focus ring ship as Material defaults and belong to
        // no design system until they are named.
        colors = OutlinedTextFieldDefaults.colors(
            focusedBorderColor = Ink.live,
            unfocusedBorderColor = Ink.rule,
            cursorColor = Ink.live,
            focusedLabelColor = Ink.live,
            unfocusedLabelColor = Ink.secondary,
            focusedTextColor = Ink.primary,
            unfocusedTextColor = Ink.primary,
        ),
    )
    Spacer(Modifier.height(8.dp))
    OutlinedButton(
        onClick = {
            onSaveToken(typed)
            typed = ""
        },
        colors = ButtonDefaults.outlinedButtonColors(contentColor = Ink.live),
    ) {
        // Saving an empty field is how a token is REMOVED, which is the only
        // way to stop a phone announcing without uninstalling the app.
        Text(if (typed.isBlank()) "Clear the stored token" else "Save the token")
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
}

@Composable
private fun SectionTitle(text: String, note: String? = null) {
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

@Composable
private fun Footnote(text: String, color: Color = Ink.secondary) {
    Text(text, style = Kind.caption, color = color)
}

// The local 1dp bar is gone: design/Theme.kt's Hairline draws Dp.Hairline, the
// thinnest line the display can render, which is what keeps a column of rules
// reading as structure rather than as a table.
