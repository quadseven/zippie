import Foundation

/// One row in a widget's leg list.
///
/// A general shape rather than a hard-coded "this phone" struct, because it
/// covers both kinds of row a widget can honestly draw: the router's own legs,
/// read from a persisted `BondStatus` snapshot, and the single fallback row for
/// this phone built from `RelayVerdict` when the router's view is not
/// available. `WidgetTimeline.legRows(for:bond:now:)` is the only thing that
/// decides which.
public struct WidgetLeg: Sendable, Equatable {
    public enum State: Sendable, Equatable {
        /// Carrying traffic right now. The only state the accent is spent on.
        case carrying
        /// In the bond's own view this leg is up and holding weight, but it is
        /// NOT carrying. `weightIsNotMembership` in design/tokens.json: a leg
        /// with weight that the transport does not hold is not carrying, and a
        /// leg at weight 0 held by the anti-flap gate is not carrying either.
        ///
        /// KEPT SEPARATE FROM `.down` ON PURPOSE. The whole reason #244's medium
        /// size exists is that "it is connected but delivering nothing" is the
        /// failure this product exists to surface, and collapsing that into the
        /// failure colour destroys it.
        case presentNotCarrying
        /// The router is explicit that it holds no link for this leg
        /// (`inBond == false`). Its own decision, not a health verdict.
        case notInBond
        /// Held back by the tier gate. `reserveIsNotBroken`: a leg withheld
        /// because a cheaper tier is alive is DOING ITS JOB, and drawing it as a
        /// fault trains a reader to ignore the one row that means something.
        case reserve
        /// A genuine fault on this leg.
        case down
        /// This phone's own fallback row, with nothing measured about the far
        /// end. Deliberately NOT `.presentNotCarrying`, because that word is a
        /// claim the router made about a leg it can see, and this row is built
        /// from counters this phone kept by itself.
        case idle
    }

    public let label: String
    public let state: State

    public init(label: String, state: State) {
        self.label = label
        self.state = state
    }

    /// The one word a reader scans, decided HERE rather than in a view.
    ///
    /// A `switch` in a SwiftUI body is the same untestable decision
    /// `RelayVerdict` was extracted to remove - see the note at the top of
    /// RelayVerdict.swift - so the vocabulary lives next to the state it
    /// describes and `swift test` can reach it.
    ///
    /// THE STRINGS ARE THE SHARED VOCABULARY in `design/tokens.json`'s `state`
    /// block, which the generator emits as `StateWord` on both platforms. They
    /// are restated rather than imported because `ZippieCompanionKit` is
    /// deliberately free of SwiftUI (ADR 0020) so it builds on a bare
    /// toolchain, and `Tokens.generated.swift` cannot be a dependency of it.
    public var word: String {
        switch state {
        case .carrying:           return "carrying"
        case .presentNotCarrying: return "up, not carrying"
        case .notInBond:          return "not in the bond"
        case .reserve:            return "held in reserve"
        case .down:               return "down"
        case .idle:               return "not carrying"
        }
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
    /// the `live` / `down` / `tertiary` tokens. `.degraded` is deliberately
    /// absent: nothing `RelayVerdict` can produce maps to it, and the dot
    /// answers a question about THIS PHONE's own contribution to the bond,
    /// not about a leg's link health. See `WidgetLeg`'s note.
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
