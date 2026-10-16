import SwiftUI
import ZippieCompanionKit

/// Other paired zippie devices on this wifi, offered as legs (#270).
///
/// ONLY PROVEN DEVICES ARE NAMED. The bond UI is built on the rule that
/// nothing is stated that has not been proven, and a Bonjour advertisement is
/// an unproven claim from anything on the network. So this lists the peers
/// `PeerRoster.offers` returns - verified against the roster in Go, not this
/// phone, not already a leg - and reduces every refusal to a count. A refused
/// device's name is its own claim; printing it beside real legs would let a
/// stranger on hotel wifi appear here as "Co-operator iPhone".
struct NearbyPeers: View {
    @ObservedObject var discovery: PeerDiscoveryService
    /// The router's legs, so a peer that is already one is not offered twice.
    let paths: [BondStatus.Path]

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            SectionHead(title: "Nearby",
                        note: "Paired zippie devices on this wifi.")
            content
        }
    }

    @ViewBuilder
    private var content: some View {
        switch discovery.status {
        case .unpaired:
            Note(text: "Store this phone's pairing bundle on Diagnostics to find "
                     + "other paired devices on this wifi.")
        case .failed(let reason):
            Note(text: "Discovery stopped: \(reason)", tone: .warning)
        case .running:
            peers
        }
    }

    @ViewBuilder
    private var peers: some View {
        let offers = discovery.roster.offers(ownClientID: discovery.ownClientID ?? 0,
                                             legNames: legNames, legEndpoints: legEndpoints)
        let added = discovery.roster.verifiedPeers.filter { discovery.adopted.contains($0.clientID) }

        if offers.isEmpty && added.isEmpty {
            Note(text: "No other paired device has proved itself on this wifi.")
        }
        ForEach(added, id: \.clientID) { peer in
            row(peer, action: "Stop", role: .quiet) { discovery.release(peer) }
        }
        ForEach(offers.filter { !discovery.adopted.contains($0.clientID) }, id: \.clientID) { peer in
            row(peer, action: "Add as leg", role: .primary) { discovery.adopt(peer) }
        }
        if let blocker = discovery.adoptionBlocker {
            Note(text: blocker, tone: .warning)
        }
        if discovery.roster.refusedCount > 0 {
            Note(text: refusedText(discovery.roster.refusedCount), tone: .warning)
        }
    }

    private func row(_ peer: VerifiedPeer, action: String, role: ActionButton.Role,
                     perform: @escaping () -> Void) -> some View {
        VStack(alignment: .leading, spacing: Space.hair) {
            Readout(label: peer.name.isEmpty ? peer.legName : peer.name, value: peer.endpoint)
            if let outcome = discovery.lastOutcome[peer.clientID] {
                Note(text: outcomeText(outcome), tone: outcomeTone(outcome))
            }
            ActionButton(title: action, role: role, action: perform)
            Hairline()
        }
    }

    private var legNames: Set<String> {
        Set(paths.compactMap(\.name))
    }

    private var legEndpoints: Set<String> {
        Set(paths.compactMap(\.relayEndpoint).filter { !$0.isEmpty })
    }

    private func refusedText(_ count: Int) -> String {
        count == 1
            ? "1 device on this wifi did not prove a pairing and is not shown."
            : "\(count) devices on this wifi did not prove a pairing and are not shown."
    }

    private func outcomeText(_ outcome: LegAnnouncer.Outcome) -> String {
        switch outcome {
        case .announced: return "Announced to the router."
        case .refused(let why): return "The router refused it: \(why)"
        case .unreachable(let why): return "The router did not answer: \(why)"
        }
    }

    private func outcomeTone(_ outcome: LegAnnouncer.Outcome) -> Note.Tone {
        if case .announced = outcome { return .plain }
        return .warning
    }
}
