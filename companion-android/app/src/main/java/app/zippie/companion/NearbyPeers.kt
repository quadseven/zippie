package app.zippie.companion

import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.height
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import app.zippie.companion.design.Hairline as Rule
import app.zippie.companion.design.Ink
import app.zippie.companion.design.Kind
import app.zippie.companion.design.Space

/**
 * Other paired zippie devices on this wifi, offered as legs (#270). The
 * counterpart of NearbyPeers.swift.
 *
 * ONLY PROVEN DEVICES ARE NAMED. The bond UI is built on the rule that nothing
 * is stated that has not been proven, and an NSD advertisement is an unproven
 * claim from anything on the network. So this lists [PeerSnapshot.offers] -
 * verified against the roster in Go, not this phone, not already a leg - and
 * reduces every refusal to a count. A refused device's name is its own claim.
 */
@Composable
fun NearbyPeers(
    peers: PeerSnapshot,
    legNames: Set<String>,
    legEndpoints: Set<String>,
    onAdopt: (VerifiedPeer) -> Unit,
    onRelease: (VerifiedPeer) -> Unit,
) {
    Spacer(Modifier.height(Space.section))
    SectionTitle("Nearby", "Paired zippie devices on this wifi.")
    when (peers.status) {
        PeerSnapshot.Status.UNPAIRED -> Footnote(
            peers.failure ?: "No pairing bundle has been pushed to this phone, so it cannot find other paired devices.",
            if (peers.failure != null) Ink.degraded else Ink.secondary,
        )
        PeerSnapshot.Status.FAILED -> Footnote("Discovery stopped: ${peers.failure}", Ink.degraded)
        PeerSnapshot.Status.RUNNING -> PeerRows(peers, legNames, legEndpoints, onAdopt, onRelease)
    }
}

@Composable
private fun PeerRows(
    peers: PeerSnapshot,
    legNames: Set<String>,
    legEndpoints: Set<String>,
    onAdopt: (VerifiedPeer) -> Unit,
    onRelease: (VerifiedPeer) -> Unit,
) {
    val added = peers.verified.filter { it.clientId in peers.adopted }
    val offers = peers.offers(legNames, legEndpoints).filter { it.clientId !in peers.adopted }
    if (added.isEmpty() && offers.isEmpty()) {
        Footnote("No other paired device has proved itself on this wifi.")
    }
    added.forEach { PeerRow(it, peers.outcomes[it.clientId], "Stop") { onRelease(it) } }
    offers.forEach { PeerRow(it, null, "Add as leg") { onAdopt(it) } }
    peers.blocker?.let { Footnote(it, Ink.degraded) }
    if (peers.refusedCount > 0) {
        val n = peers.refusedCount
        Footnote(
            if (n == 1) {
                "1 device on this wifi did not prove a pairing and is not shown."
            } else {
                "$n devices on this wifi did not prove a pairing and are not shown."
            },
            Ink.degraded,
        )
    }
}

@Composable
private fun PeerRow(peer: VerifiedPeer, outcome: LegAnnouncer.Outcome?, action: String, onClick: () -> Unit) {
    Text(peer.name.ifEmpty { peer.legName }, style = Kind.label, color = Ink.primary)
    Footnote(peer.endpoint)
    outcome?.let { Footnote(outcomeText(it), if (it is LegAnnouncer.Outcome.Announced) Ink.secondary else Ink.degraded) }
    OutlinedButton(onClick = onClick, colors = ButtonDefaults.outlinedButtonColors(contentColor = Ink.live)) {
        Text(action)
    }
    Rule()
}

private fun outcomeText(outcome: LegAnnouncer.Outcome): String = when (outcome) {
    is LegAnnouncer.Outcome.Announced -> "Announced to the router."
    is LegAnnouncer.Outcome.Refused -> "The router refused it: ${outcome.reason}"
    is LegAnnouncer.Outcome.Unreachable -> "The router did not answer: ${outcome.reason}"
}
