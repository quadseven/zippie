package app.zippie.companion

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import app.zippie.companion.design.Hairline as Rule
import app.zippie.companion.design.Ink
import app.zippie.companion.design.Kind
import app.zippie.companion.design.Space

/**
 * This phone's own half of the bond: whether it is contributing, the single
 * control that changes that, and the fallback nobody should need.
 *
 * ORDERED THE WAY RelayScreen.swift ORDERS ITSELF, and for the same stated
 * reason: "it now opens with the state, then the single control that changes
 * it, then the numbers backing it, and only then the endpoint fields... ordered
 * by what the reader needs first" rather than the order the features were
 * built in. This file WAS that build order - carrier and wifi address printed
 * above the verdict that explains why they matter - moved here verbatim out of
 * StatusScreen.kt (#parity), which is the moment to fix the ordering along with
 * the address.
 *
 * SPLIT OUT OF StatusScreen.kt, not rewritten. Every sentence, every colour
 * rule and every test this content had stays true; only its screen changed.
 * The screen it left behind was the one the operator actually opens (see
 * StatusScreen.kt's own header comment), and it had grown to hold a router
 * write token editor a glance from a dashboard never needed to see.
 */
@Composable
fun RelayScreen(
    state: BondUiState,
    onStartRelay: () -> Unit,
    onStopRelay: () -> Unit,
    onStartClient: () -> Unit,
    onSaveToken: (String) -> Unit,
) {
    Column(
        Modifier
            .verticalScroll(rememberScrollState())
            .padding(horizontal = Space.margin, vertical = Space.roomy),
    ) {
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

    // THE STATE COMES FIRST, matching RelayScreen.swift's `header`. This used
    // to be three sentences below the carrier and wifi-address readout that
    // explains it - which is the exact "opened on a control... the thing you
    // actually came for three sections down" complaint RelayScreen.swift's own
    // header comment makes about ITS previous version.
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

    // THE NUMBERS BACKING IT, next - matching RelayScreen.swift's ordering.
    // Raw measurements, shown only while the report is live and current - a
    // corpse's byte counts sitting under "not reporting" would look like more
    // evidence when they are the opposite.
    if (stats != null && verdict !is RelayVerdict.Off && verdict !is RelayVerdict.NotReporting) {
        Spacer(Modifier.height(Space.base))
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

    // THE ENDPOINT, last before the fallback - matching RelayScreen.swift's
    // `endpoint` section. A carrier name is something iOS cannot report at
    // all, so it is stated here rather than inferred from the router's
    // hand-written leg label.
    Spacer(Modifier.height(Space.base))
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
