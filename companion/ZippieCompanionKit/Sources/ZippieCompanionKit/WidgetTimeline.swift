import Foundation

/// Turns whatever `RelayStatusStore` and `WidgetBondStore` hold into a
/// `WidgetContent`, for a WidgetKit timeline entry.
///
/// THE RULE THIS TYPE EXISTS TO ENFORCE (issue #244, rule 2): a widget
/// refreshes on the SYSTEM's schedule, not the app's, so the default failure
/// is a widget that read a good report once and then kept showing "Carrying"
/// for the twenty minutes until its next refresh happened to land after the
/// tunnel died. `RelayVerdict.evaluate` already refuses to do that - it reads
/// `RelayStatus.isStale(asOf:)` and returns `.notReporting` - so the fix is to
/// go through that function on every build and never cache or shortcut around
/// it. `WidgetTimelineTests.testStaleReportRendersNotReportingNotLastGoodVerdict`
/// is the test that fails if a future edit here starts doing that.
///
/// THE SAME RULE APPLIES TO THE BOND, AND THAT IS WHY IT IS A SEPARATE
/// ARGUMENT. The router's view of the bond is a second, differently-aged piece
/// of evidence (see `WidgetBondSnapshot`), so it gets its own staleness gate
/// rather than being folded into the report's. Passing a snapshot that is past
/// its threshold renders the same this-phone fallback row an absent one does -
/// never the legs it happened to be holding.
public enum WidgetTimeline {

    /// Requested cadence for the next timeline refresh.
    ///
    /// NOT A PROMISE. WidgetKit budgets refreshes system-wide and a request
    /// this short will usually be throttled to something coarser - that is
    /// fine, because freshness is owned by `RelayStatus.isStale`, not by how
    /// often the system actually honours this request. Fifteen minutes is a
    /// reasonable ask for a status widget without spending the whole budget
    /// other widgets on the same home screen are also drawing from.
    public static let requestedRefreshInterval: TimeInterval = 15 * 60

    /// Build from exactly the evidence a widget process can read on its own:
    /// the app-group relay report, the app-group bond snapshot, and (optionally)
    /// the operator-set router name for the same disambiguation
    /// `RelayVerdict.detail(router:)` gives every other surface (#44 operator
    /// follow-up). No network call - a widget process has no business opening
    /// one, and a fetch it could not finish would be a worse source of truth
    /// than the record the app already left behind.
    ///
    /// `bond` is last, after the defaulted arguments, so the call sites that
    /// only ever had a report keep saying exactly what they meant.
    public static func build(report: RelayStatus?,
                             now: Date = Date(),
                             router: String? = nil,
                             bond: WidgetBondSnapshot? = nil) -> WidgetContent {
        let verdict = RelayVerdict.evaluate(run: report == nil ? .off : .running,
                                            report: report, now: now)
        return WidgetContent(headline: verdict.headline,
                             detail: verdict.detail(router: router),
                             tone: tone(for: verdict),
                             legs: legRows(for: verdict, bond: bond, now: now))
    }

    /// The headline dot's colour. Mirrors `LegRow`'s own restraint: a phone
    /// that is merely idle (`.listening`, `.routerQuiet`) is not painted as a
    /// fault, and `.degraded` is not reachable from this evidence at all - see
    /// `WidgetContent.Tone`.
    static func tone(for verdict: RelayVerdict) -> WidgetContent.Tone {
        if verdict.isCarryingLeg { return .live }
        // A report that stopped checking in is a fault even though nothing on
        // THIS phone's radio is necessarily wrong - the relay itself died.
        if verdict.isDownLeg || verdict == .notReporting { return .down }
        return .neutral
    }

    /// The leg list, in the order the surface prefers them.
    ///
    /// ORDER IS THE ARGUMENT, and it is not cosmetic. The router's own bond is
    /// strictly more informative than the one row this phone could build about
    /// itself, so a FRESH snapshot wins outright and replaces that row rather
    /// than joining it - this phone's own leg is already in the router's list,
    /// and printing it twice would be a worse lie than either version.
    ///
    /// The "nothing measured" verdicts come FIRST and short-circuit everything
    /// else, including a perfectly fresh bond. A widget whose relay has stopped
    /// checking in has no standing to describe the bond it was last able to
    /// see, and rule 4 (`neverStateUnmeasured`) applies to the whole list, not
    /// just to the report.
    static func legRows(for verdict: RelayVerdict,
                        bond: WidgetBondSnapshot? = nil,
                        now: Date = Date()) -> [WidgetLeg] {
        switch verdict {
        case .off, .starting, .stopping, .awaitingFirstReport, .notReporting:
            // Nothing measured yet, or the measurement is a corpse - see rule
            // 4 (neverStateUnmeasured). No row beats a fabricated one.
            return []
        default:
            break
        }
        if let bond, !bond.isStale(asOf: now) {
            let rows = bondLegRows(bond.status)
            // A fresh snapshot that resolves to nothing is still an absence,
            // not a licence to invent the this-phone row: `pathsMissing` and a
            // genuinely empty bond are different router answers and the widget
            // is not the place to tell them apart.
            if !rows.isEmpty { return rows }
        }
        return thisPhoneRow(for: verdict)
    }

    /// The one row a widget can build without the router: this phone, from the
    /// same verdict the headline was built from, so the two can never disagree.
    /// Matches `BondModel`'s own single-phone fallback exactly.
    private static func thisPhoneRow(for verdict: RelayVerdict) -> [WidgetLeg] {
        let state: WidgetLeg.State = verdict.isCarryingLeg ? .carrying
                                    : (verdict.isDownLeg ? .down : .idle)
        return [WidgetLeg(label: "This phone", state: state)]
    }

    /// The router's legs, in its own order, each mapped through the same
    /// precedence `BondLegs` uses on the Status screen.
    ///
    /// SHARED RATHER THAN RE-DERIVED is the point: a widget that disagreed
    /// with the screen it summarises about which legs are in reserve would be
    /// worse than having no widget, because it would be believed.
    static func bondLegRows(_ status: BondStatus) -> [WidgetLeg] {
        let activeTier = status.activeTier
        return (status.paths ?? [])
            .filter(\.isPresent)
            .map { path in
                WidgetLeg(label: Self.label(for: path), state: legState(path, activeTier: activeTier))
            }
    }

    /// What a human called this link. `label` is the display name and `name` an
    /// internal id, which is a poor thing to read at a glance; a leg with
    /// neither is named after its own absence rather than being dropped, since
    /// an unnamed row in the middle of a list reads as a rendering fault.
    static func label(for path: BondStatus.Path) -> String {
        if let label = path.label, !label.isEmpty { return label }
        if let name = path.name, !name.isEmpty { return name }
        return "Unnamed leg"
    }

    /// `weightIsNotMembership` AND `reserveIsNotBroken`, in the order
    /// `BondLegs.swift` already puts them, because the order is the argument.
    ///
    /// - The tier gate wins outright. A tier-3 leg with no traffic is not
    ///   idle, degraded or down - it is being deliberately withheld, and any of
    ///   those three words sends someone looking for a fault that does not
    ///   exist.
    /// - Carrying is `Path.isCarrying`, never weight alone. That getter already
    ///   encodes both halves: weight above zero AND `inBond` not false.
    /// - `inBond == false` is the router saying it holds no link, which is a
    ///   decision rather than a health verdict, so it gets its own word.
    /// - Only then is `state == "down"` a fault.
    static func legState(_ path: BondStatus.Path, activeTier: Int?) -> WidgetLeg.State {
        if path.isHeldInReserve(activeTier: activeTier) { return .reserve }
        if path.isCarrying { return .carrying }
        if path.inBond == false { return .notInBond }
        if path.state == "down" { return .down }
        return .presentNotCarrying
    }
}
