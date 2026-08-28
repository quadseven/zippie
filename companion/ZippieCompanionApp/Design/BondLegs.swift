import Foundation
import ZippieCompanionKit

/// Turns the router's view of the bond into the rows the screen draws.
///
/// THE HARD PART IS NOT THE MAPPING, IT IS SAYING WHICH LEG IS YOURS. The bond
/// carries two companion legs labelled "iPhone (Verizon)" and "Co-operator iPhone
/// (Verizon)". Picking the wrong one would tell Co-operator her phone is contributing
/// while showing somebody else's traffic - a confident, invisible lie, and the
/// single worst failure this UI could have.
///
/// So identity is evidence or nothing: the router publishes the address:port it
/// dials for each companion leg, and a leg is only marked as yours when that
/// endpoint matches this phone's own wifi address and listen port. No match
/// means no row is marked, which is the honest outcome when the phone is on
/// cellular, on another network, or behind a config that no longer matches.
enum BondLegs {

    /// Rows for the whole bond, in the router's own order - which is priority
    /// order, so the leg most likely to be carrying is nearest the top.
    static func rows(from status: BondStatus,
                     localIP: String?,
                     listenPort: UInt16) -> [Leg] {
        let active = status.activeTier
        return (status.paths ?? [])
            .filter(\.isPresent)
            .compactMap { path in
                guard let name = path.name, !name.isEmpty else { return nil }
                let mine = LegIdentity.identifies(endpoint: path.relayEndpoint,
                                                  listenPort: listenPort,
                                                  localIP: localIP)

                return Leg(
                    id: name,
                    name: displayName(path) ?? name,
                    state: state(of: path, activeTier: active),
                    upBytes: path.carriedTx,
                    downBytes: path.carriedRx,
                    latencyMS: path.rttMs,
                    isYou: mine,
                    note: note(for: path, activeTier: active),
                    shadowNote: shadowNote(for: path),
                    stateWord: path.stateWord,
                    isCarrying: path.isCarrying
                )
            }
    }

    private static func displayName(_ p: BondStatus.Path) -> String? {
        guard let label = p.label, !label.isEmpty else { return nil }
        return label
    }

    /// The router's state string is not enough on its own.
    ///
    /// `up` with weight 0 is the anti-flap gate holding a recovered leg out of
    /// the bond while it proves itself. It is genuinely not carrying, and
    /// drawing it as carrying would contradict the router's own console - and
    /// repeat the exact failure this app exists to expose.
    private static func state(of p: BondStatus.Path, activeTier: Int?) -> LegState {
        // RESERVE BEATS EVERY OTHER READING. A tier-3 leg with no traffic is
        // not idle, degraded or down - it is being deliberately withheld, and
        // any of those three words would send someone looking for a fault that
        // does not exist.
        if p.isHeldInReserve(activeTier: activeTier) { return .reserve }
        switch p.state {
        case "up":       return p.isCarrying ? .carrying : .idle
        case "degraded": return .degraded
        case "down":     return .down
        default:         return .idle
        }
    }

    /// Prefer the router's own words. "healthy, held out of bond until proven
    /// (2.5/8)" tells the reader both what is happening and that it is
    /// resolving itself; any sentence this app invented would be vaguer.
    /// A usable uplink this leg's pattern matched and nobody took (#212).
    ///
    /// SEPARATE FROM `note`, not folded into it: the problem is not with this
    /// leg, which may be perfectly healthy - it is that a neighbour is missing.
    /// Putting it in the note chain would mean a healthy leg either hides it or
    /// pretends to be unwell. Android does the same, deliberately.
    private static func shadowNote(for p: BondStatus.Path) -> String? {
        let hidden = (p.shadowedInterfaces ?? []).filter { !$0.isEmpty }
        guard !hidden.isEmpty else { return nil }
        let which = hidden.joined(separator: ", ")
        return hidden.count == 1
            ? "\(which) is a working uplink that no leg is using."
            : "\(which) are working uplinks that no leg is using."
    }

    private static func note(for p: BondStatus.Path, activeTier: Int?) -> String? {
        if p.isHeldInReserve(activeTier: activeTier) {
            // The router's own last_error for a reserve leg describes the
            // anti-flap gate - true, and completely beside the point when the
            // leg was never going to be used anyway.
            var why = "Held in reserve - only used if everything above it fails."
            if let cap = p.maxKbps, cap > 0 { why += " Capped at \(cap) kbit/s." }
            return why
        }
        // OUTRANKS lastError AND loss, deliberately. The router's own
        // lastError here reads "no reply yet", which is true and describes a
        // moment; "has never been answered" describes the leg's whole life and
        // is the one that says what to go and fix - the address it dials,
        // rather than the quality of the link.
        if p.neverHandshaked == true {
            return "Never answered. This leg has sent traffic and had none "
                + "back, ever - check the address it is dialling rather than "
                + "the signal."
        }
        if let e = p.lastError, !e.isEmpty { return e }
        if let loss = p.lossPct, loss > 0 {
            return String(format: "%.0f%% of packets lost.", loss)
        }
        return nil
    }

    /// The sentence at the top, derived from the bond rather than from this
    /// phone alone.
    ///
    /// Counts CARRYING legs, not configured ones. "5 connections" when four are
    /// down is the reassuring-but-wrong answer; "1 of 5 connections" is the
    /// true one and takes the same space.
    static func headline(for status: BondStatus, rows: [Leg]) -> String {
        let carrying = rows.filter(\.isCarrying).count
        if carrying == 0 { return "Nothing carrying" }
        return carrying == 1 ? "1 connection" : "\(carrying) connections"
    }

    static func subhead(for status: BondStatus, rows: [Leg]) -> String {
        let carrying = rows.filter(\.isCarrying)
        if carrying.isEmpty {
            return "The router is up but no link is carrying traffic right now."
        }
        // Whether YOUR phone is in it is the question Co-operator actually has, so it
        // is answered in the sentence rather than left to be inferred from a
        // row further down the page.
        if let me = rows.first(where: \.isYou) {
            return me.isCarrying
                ? "This phone is one of them."
                : "This phone is not one of them."
        }
        return carrying.count == rows.count
            ? "Every link is carrying."
            : "\(carrying.count) of \(rows.count) links are carrying."
    }
}
