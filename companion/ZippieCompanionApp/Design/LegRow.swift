import SwiftUI
import ZippieCompanionKit

/// One connection in the bond.
///
/// The share bar is REAL DATA - this leg's measured portion of delivered
/// traffic - not a decorative progress track. A bar that is not measuring
/// something is the sparkline habit, and this app cannot afford invented
/// numbers: its entire failure history is legs that looked healthy while
/// carrying nothing.
struct LegRow: View {
    let leg: Leg

    var body: some View {
        VStack(alignment: .leading, spacing: Space.snug) {
            HStack(alignment: .firstTextBaseline, spacing: Space.snug) {
                Text(leg.name)
                    .font(Kind.label())
                    .foregroundStyle(Ink.primary)

                if leg.isYou {
                    // Co-operator's actual question is "is MY phone helping". Answering
                    // it takes one word, not an icon she has to learn.
                    Text("this phone")
                        .font(Kind.caption())
                        .foregroundStyle(Ink.secondary)
                }

                Spacer(minLength: Space.snug)

                // Shown only when it was actually measured. A "--" placeholder
                // implies a value that is missing; this leg has no RTT concept
                // at all, and an empty column says that better than a dash.
                // THE WORD, not just a colour. "degraded" and "not in the
                // bond" are different problems with different fixes, and a
                // grey bar says neither.
                Text(leg.stateWord)
                    .font(Kind.caption())
                    .foregroundStyle(leg.carrying ? Ink.live : Ink.secondary)

                if let latency = leg.latencyMS {
                    Text("\(Int(latency.rounded())) ms")
                        .font(Kind.figure(15))
                        .foregroundStyle(leg.carrying ? Ink.secondary : Ink.tertiary)
                }
            }

            TrafficBar(up: leg.upBytes, down: leg.downBytes, state: leg.state)

            if let note = leg.note {
                // The reason lives WITH the leg, not in a status line at the
                // bottom of the screen. "relay not answering" next to the leg
                // it describes is the difference between a diagnosis and a mood.
                Text(note)
                    .font(Kind.caption())
                    .foregroundStyle(leg.state == .down ? Ink.down : Ink.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            // Rendered even when the leg above is perfectly healthy, and
            // coloured the same regardless of ITS state, because this sentence
            // is not about this leg - it is about an uplink that is working and
            // in no leg at all.
            if let shadow = leg.shadowNote {
                Text(shadow)
                    .font(Kind.caption())
                    .foregroundStyle(Ink.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.vertical, Space.base)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(leg.accessibilityDescription)
    }
}

/// The two directions this leg has actually carried, as a proportion of each
/// other.
///
/// REPLACED A SHARE BAR THAT WAS DECORATION. With one leg the share is always
/// 100%, so the bar rendered as a full-width slab that said nothing while being
/// the loudest thing on the screen - the progress-bar-as-content habit exactly.
/// Up against down is genuinely measured, differs run to run, and answers a
/// question someone might have: is this phone uploading for the bond, or
/// pulling down?
///
/// A leg that is UP but has carried nothing renders as an empty track. That
/// distinction is the whole point: it is the failure this system keeps having,
/// and a filled bar would hide it.
struct TrafficBar: View {
    let up: UInt64
    let down: UInt64
    let state: LegState

    private var tint: Color {
        switch state {
        case .carrying: return Ink.live
        case .degraded: return Ink.degraded
        case .down:     return Ink.down
        case .idle:     return Ink.tertiary
        // Deliberately the quietest ink on the page. A reserve leg is not news.
        case .reserve:  return Ink.tertiary
        }
    }

    private var upFraction: Double {
        let total = Double(up + down)
        guard total > 0 else { return 0 }
        return Double(up) / total
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Space.tight) {
            GeometryReader { geo in
                // NOTHING CARRIED MEANS AN EMPTY TRACK, and this needs its own
                // branch. Left to the proportional layout below, a leg with
                // zero bytes drew the "received" capsule across the FULL width
                // - it has no width of its own and simply expands - so a leg
                // that had carried nothing at all rendered as a full bar. That
                // is the precise failure this component was built to expose,
                // reintroduced by the layout that was supposed to expose it.
                if up + down == 0 {
                    Capsule(style: .continuous)
                        .fill(Ink.rule)
                        .frame(height: 3)
                } else {
                    HStack(spacing: 2) {
                        Capsule(style: .continuous)
                            .fill(tint)
                            .frame(width: max(0, upFraction * (geo.size.width - 2)))
                        Capsule(style: .continuous)
                            .fill(tint.opacity(0.32))
                    }
                    .frame(height: 3)
                    .background(alignment: .leading) {
                        Capsule(style: .continuous).fill(Ink.rule).frame(height: 3)
                    }
                }
            }
            .frame(height: 3)
            // THE ONE AUTHORED MOTION. The split settles rather than jumps, so
            // a glance at a moving bar reads as live rather than as a redraw.
            .animation(.easeOut(duration: 0.55), value: upFraction)

            if up + down > 0 {
                HStack(spacing: Space.base) {
                    Text("\(Fmt.bytes(up)) sent")
                    Text("\(Fmt.bytes(down)) received")
                }
                .font(Kind.figure(13))
                .foregroundStyle(Ink.secondary)
            }
        }
    }
}

enum Fmt {
    static func bytes(_ b: UInt64) -> String {
        let mb = Double(b) / 1_048_576
        if mb >= 1000 { return String(format: "%.2f GB", mb / 1024) }
        if mb >= 10 { return String(format: "%.0f MB", mb) }
        if mb >= 0.1 { return String(format: "%.1f MB", mb) }
        return "\(b / 1024) KB"
    }
}

enum LegState {
    case carrying, degraded, down, idle
    /// Held back by the tier gate, not broken. A cheap SIM kept for the day
    /// everything else fails reports no traffic because it is DOING ITS JOB,
    /// and drawing that the same as a failure trains the reader to ignore the
    /// one signal on this screen that matters.
    case reserve
}

/// A leg as the UI needs it. Deliberately a view model rather than the raw
/// transport type: the app must be able to render "unknown" honestly, and the
/// datapath's types have no vocabulary for a value it has not measured yet.
struct Leg: Identifiable {
    let id: String
    let name: String
    let state: LegState
    let upBytes: UInt64
    let downBytes: UInt64
    let latencyMS: Double?
    let isYou: Bool
    let note: String?
    /// A working uplink nobody is using (#212). Separate from `note` because
    /// the fault is a missing neighbour, not this leg.
    var shadowNote: String? = nil
    /// One word for what this leg is doing. Carried from the Kit rather than
    /// derived here so the app and the router cannot disagree about it.
    let stateWord: String
    /// WHETHER IT CARRIES, which is NOT the same question as whether it is
    /// healthy, and must not be derived from `state`.
    ///
    /// `LegState` has one slot and has to spend it on how the row is drawn, so
    /// a leg that is degraded AND carrying is drawn `.degraded` - correctly,
    /// it has 12% loss. Counting membership from that slot then reported it as
    /// not carrying, which is how the screen came to say "Nothing carrying"
    /// and "0 of 3 carrying" directly above a row reading "carrying, degraded"
    /// with 402 MB sent.
    ///
    /// Taken from the router's own `isCarrying` - the same value
    /// `BondStatus.carryingCount` and `stateWord` use - so the headline, the
    /// heading, the row and the telemetry cannot disagree.
    let isCarrying: Bool

    var carrying: Bool { isCarrying }

    var latencyText: String {
        guard let latencyMS else { return "--" }
        return "\(Int(latencyMS.rounded())) ms"
    }

    var accessibilityDescription: String {
        let s: String
        switch state {
        case .carrying: s = "carrying, \(Fmt.bytes(upBytes)) sent, \(Fmt.bytes(downBytes)) received"
        case .degraded: s = "degraded"
        case .down:     s = "down"
        case .idle:     s = "idle"
        case .reserve:  s = "held in reserve"
        }
        return "\(name), \(s), \(latencyText)\(note.map { ", \($0)" } ?? "")"
    }
}
