import Foundation

/// One row in a widget's leg list.
///
/// A general shape rather than a hard-coded "this phone" struct, because it
/// mirrors `LegRow.LegState` (the app's real vocabulary for a leg) minus the
/// two cases that need the router's own multi-leg view - `.degraded` and
/// `.reserve` - which are BondStatus.Path concepts (weight, tier) that do not
/// exist on `CellularRelay.Stats` at all. `BondModel` fetches BondStatus only
/// on the app's foreground timer, so anything persisted from it into the app
/// group would usually be many minutes stale by the time WidgetKit chooses to
/// refresh - rule 2 would then correctly suppress it on almost every render,
/// which means shipping it now would be tested code nothing exercises. So this
/// PR's leg list has exactly the three states a widget can back with evidence
/// that is guaranteed fresh - `RelayStatus`, written continuously by the
/// packet-tunnel extension for as long as the tunnel runs, independent of
/// whether the app itself is even in memory. Wiring the router's full bond in
/// is real follow-on work, not a shortcut taken here.
public struct WidgetLeg: Sendable, Equatable {
    public enum State: Sendable, Equatable {
        case carrying
        case down
        case idle
    }
    public let label: String
    public let state: State

    public init(label: String, state: State) {
        self.label = label
        self.state = state
    }
}

/// Everything a widget view needs, already decided.
///
/// THE VIEW'S ONLY JOB IS TO LAY THESE FIELDS OUT. See the note at the top of
/// RelayVerdict.swift: a view that decides its own sentence cannot be tested,
/// which is how #44 shipped. `WidgetTimeline.build` is the one place that
/// turns evidence into this struct, and it lives in the Kit for the same
/// reason.
public struct WidgetContent: Sendable, Equatable {
    /// The dot colour's MEANING, not its RGB - the widget view maps this to
    /// `Ink.live` / `Ink.down` / `Ink.tertiary`. `.degraded` is deliberately
    /// absent: nothing this PR's evidence (`RelayStatus` alone) can honestly
    /// produce maps to it - see `WidgetLeg`'s note. Adding a case a mapping
    /// function can never reach is the same "tested, never wired" trap as
    /// shipping the `.degraded` leg state would have been.
    public enum Tone: Sendable, Equatable {
        /// Carrying traffic right now - the app's one accent.
        case live
        /// Not carrying, and it is a fault rather than a wait.
        case down
        /// Nothing to claim either way - off, starting, stopping, or simply
        /// present and quiet. Never the failure colour for a state that is not
        /// a failure.
        case neutral
    }

    public let headline: String
    public let detail: String
    public let tone: Tone
    /// Rows for the medium size. Empty whenever there is nothing honest to say
    /// about a leg yet - off, starting first, stopping, or a corpse report -
    /// which the small widget never renders and the medium widget renders as
    /// no leg section rather than a placeholder row (rule 4: unmeasured is
    /// absent, not a dash).
    public let legs: [WidgetLeg]

    public init(headline: String, detail: String, tone: Tone, legs: [WidgetLeg] = []) {
        self.headline = headline
        self.detail = detail
        self.tone = tone
        self.legs = legs
    }
}
