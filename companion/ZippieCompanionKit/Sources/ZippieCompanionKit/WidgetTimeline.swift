import Foundation

/// Turns whatever `RelayStatusStore` holds into a `WidgetContent`, for a
/// WidgetKit timeline entry.
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
    /// the app-group report, and (optionally) the operator-set router name
    /// for the same disambiguation `RelayVerdict.detail(router:)` gives every
    /// other surface (#44 operator follow-up). No network call, no other
    /// store - see `WidgetLeg`'s doc comment for why the router's full bond
    /// view is not part of this yet.
    public static func build(report: RelayStatus?,
                             now: Date = Date(),
                             router: String? = nil) -> WidgetContent {
        let verdict = RelayVerdict.evaluate(run: report == nil ? .off : .running,
                                            report: report, now: now)
        return WidgetContent(headline: verdict.headline,
                             detail: verdict.detail(router: router),
                             tone: tone(for: verdict),
                             legs: legRows(for: verdict))
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

    /// One honest row - this phone - or none. Matches `BondModel`'s own
    /// single-phone fallback exactly (same verdicts produce a row, same
    /// carrying/down/idle mapping), so the Status screen and the widget can
    /// never describe this phone differently from the same report.
    static func legRows(for verdict: RelayVerdict) -> [WidgetLeg] {
        switch verdict {
        case .off, .starting, .stopping, .awaitingFirstReport, .notReporting:
            // Nothing measured yet, or the measurement is a corpse - see rule
            // 4 (neverStateUnmeasured). No row beats a fabricated one.
            return []
        default:
            let state: WidgetLeg.State = verdict.isCarryingLeg ? .carrying
                                        : (verdict.isDownLeg ? .down : .idle)
            return [WidgetLeg(label: "This phone", state: state)]
        }
    }
}
