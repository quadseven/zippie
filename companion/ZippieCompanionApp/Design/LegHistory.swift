import SwiftUI
import ZippieCompanionKit

/// One leg, over time.
///
/// THE QUESTION: "is this leg actually any good", which the status screen
/// cannot answer because it only ever shows the current tick. A leg that flaps
/// between carrying and dead every twenty seconds and a leg that has been solid
/// all day look identical there.
///
/// THE RULE, and it outranks every visual consideration in this file: nothing
/// is drawn that was not measured. Where the router reported no value the line
/// STOPS and the stretch is shaded; where the router reported nothing at all
/// the same thing happens for a different stated reason. No interpolation
/// across a hole, no zero-fill, no carrying the last value forward. This
/// project's entire failure history is links that looked healthy while carrying
/// nothing, and a chart that smooths over its own missing data is that failure
/// with a nicer typeface.
///
/// NO CARDS, no gridlines, no axis furniture. A hairline is the zero line, the
/// scale is stated in words above the plot, and the numbers a chart cannot
/// label are in one sentence underneath. Where there is not enough measured
/// data to draw something honest, the sentence is the whole section.
struct LegHistoryView: View {
    @StateObject private var model: LegHistoryModel
    private let title: String
    private let usage: LegUsage?

    /// - Parameters:
    ///   - legID: the ROUTER's name for the leg ("hotspot"), which is the key
    ///     /api/series stores it under, not the human label.
    ///   - title: what to call it on screen.
    ///   - usage: the monthly total from /api/status. Optional because the
    ///     history endpoint does not carry usage at all - see the note in the
    ///     usage section.
    init(legID: String, title: String, usage: LegUsage? = nil) {
        _model = StateObject(wrappedValue: LegHistoryModel(leg: legID))
        self.title = title
        self.usage = usage
    }

    var body: some View {
        Page {
            header
            history
            if let usage { monthly(usage) }
            footer
        }
        .task { await model.run() }
        .refreshable { await model.refresh() }
    }

    // MARK: - what it is doing now

    private var header: some View {
        VStack(alignment: .leading, spacing: Space.snug) {
            Text(title)
                .font(Kind.title())
                .foregroundStyle(Ink.secondary)

            Text(model.headline)
                .font(Kind.display())
                .tracking(-0.8)
                .foregroundStyle(Ink.primary)
                .fixedSize(horizontal: false, vertical: true)

            Text(model.subhead)
                .font(Kind.body())
                .foregroundStyle(Ink.secondary)
                .fixedSize(horizontal: false, vertical: true)

            if let caveat = model.caveat {
                Note(text: caveat, tone: .warning)
            }
        }
        .padding(.top, Space.roomy)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    // MARK: - the window

    @ViewBuilder
    private var history: some View {
        if let track = model.track, let window = track.window, !track.isEmpty {
            reporting(track, window)
            plot(.rtt, track, window)
            plot(.loss, track, window)
            plot(.weight, track, window)
        }
        // No else. The header's subhead already says why there is nothing, and
        // an empty-state block repeating it would be the second sentence saying
        // the same thing louder.
    }

    /// Continuity first, because it qualifies everything below it. A chart of
    /// three measurements taken over ten minutes is not a picture of ten
    /// minutes, and this strip is where that becomes visible.
    private func reporting(_ track: LegTrack,
                           _ window: (start: Date, end: Date)) -> some View {
        VStack(alignment: .leading, spacing: Space.tight) {
            SectionHead(title: "Reporting")
            ReportingStrip(track: track, start: window.start, end: window.end)
                .accessibilityElement(children: .ignore)
                .accessibilityLabel("Reporting continuity. \(model.reportingSentence)")
            Hairline()
            if let span = model.windowText {
                Text(span)
                    .font(Kind.figure(13))
                    .foregroundStyle(Ink.tertiary)
            }
            Note(text: model.reportingSentence)
        }
    }

    @ViewBuilder
    private func plot(_ metric: LegTrack.Metric,
                      _ track: LegTrack,
                      _ window: (start: Date, end: Date)) -> some View {
        let measured = track.measuredCount(metric)
        let upper = HistoryFmt.upperBound(track.summary(metric)?.highest ?? 0, metric)

        VStack(alignment: .leading, spacing: Space.tight) {
            SectionHead(title: HistoryFmt.title(metric))

            // A chart with nothing measured in it is decoration, and decoration
            // that implies measurement is the failure this app exists to catch.
            // The sentence below is the whole answer in that case.
            if measured > 0, window.end > window.start {
                Text("0 to \(HistoryFmt.value(upper, metric))")
                    .font(Kind.figure(13))
                    .foregroundStyle(Ink.tertiary)
                    .frame(maxWidth: .infinity, alignment: .trailing)
                MetricPlot(track: track,
                           metric: metric,
                           start: window.start,
                           end: window.end,
                           upper: upper,
                           tint: metric == .weight ? Ink.live : Ink.primary)
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel("\(HistoryFmt.title(metric)). \(model.caption(for: metric))")
                Hairline()
            }

            Note(text: model.caption(for: metric))
        }
    }

    // MARK: - what it has cost

    private func monthly(_ usage: LegUsage) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            SectionHead(title: "This month")

            if let used = usage.usedGB {
                Readout(label: "Used", value: usage.capGB.map {
                    "\(HistoryFmt.gigabytes(used)) of \(HistoryFmt.gigabytes($0))"
                } ?? HistoryFmt.gigabytes(used))
                if let cap = usage.capGB {
                    Hairline()
                    Readout(label: "Left",
                            value: HistoryFmt.gigabytes(max(0, cap - used)),
                            tone: usage.overSoftLimit == true ? Ink.degraded : Ink.primary)
                }
            }

            if usage.capGB == nil {
                Note(text: "No monthly cap is set for this leg, so nothing here is "
                         + "counting down.")
            }
            if usage.overSoftLimit == true {
                Note(text: "Past its soft limit. The router will lean on other legs "
                         + "until the month rolls over.", tone: .warning)
            }
            // Said plainly rather than drawn as a trend, because it is not one.
            Note(text: "The router's running total as of the last reply. The history "
                     + "above does not carry usage, so there is no trend to draw.")
        }
    }

    private var footer: some View {
        VStack(alignment: .leading, spacing: Space.tight) {
            Hairline()
                .padding(.top, Space.section)
                .padding(.bottom, Space.base)
            Text("Lines join measurements that are next to each other in time. "
               + "Where nothing was measured the line stops and the stretch is "
               + "shaded, so a leg that went quiet cannot be mistaken for a leg "
               + "that reported zero.")
                .font(Kind.caption())
                .foregroundStyle(Ink.tertiary)
                .fixedSize(horizontal: false, vertical: true)
            Text(model.sourceText)
                .font(Kind.caption())
                .foregroundStyle(Ink.tertiary)
                .fixedSize(horizontal: false, vertical: true)
            if let stamp = model.freshnessText {
                Text(stamp)
                    .font(Kind.caption())
                    .foregroundStyle(Ink.tertiary)
            }
        }
    }
}

/// The monthly total for one leg, as /api/status reports it.
///
/// A SNAPSHOT, NOT A SERIES. /api/series carries rtt, loss, weight and the two
/// throughput fields and nothing else - no usage, no cap - so this arrives from
/// the status endpoint the caller already holds. It is a separate type rather
/// than a couple of loose Doubles so the "there is nothing to say" case is
/// spelled `nil` at the call site instead of two optional parameters that can
/// contradict each other.
struct LegUsage {
    let usedGB: Double?
    /// Nil when the leg is uncapped. The router publishes 0.0 for that, which
    /// would read as "a zero-byte allowance" if it were passed straight through.
    let capGB: Double?
    let overSoftLimit: Bool?

    init?(usedGB: Double?, capGB: Double?, overSoftLimit: Bool? = nil) {
        let cap = (capGB ?? 0) > 0 ? capGB : nil
        guard usedGB != nil || cap != nil else { return nil }
        self.usedGB = usedGB
        self.capGB = cap
        self.overSoftLimit = overSoftLimit
    }

    init?(path: BondStatus.Path) {
        self.init(usedGB: path.usageGB,
                  capGB: path.monthlyCapGB,
                  overSoftLimit: path.overSoftLimit)
    }
}

/// One metric over the window.
///
/// Drawn with a Canvas rather than Swift Charts on purpose: the whole value of
/// this view is in what it REFUSES to draw, and a charting library's job is to
/// produce a continuous line from whatever it is handed. Every gap here is a
/// deliberate absence, which is easier to guarantee than to configure.
private struct MetricPlot: View {
    let track: LegTrack
    let metric: LegTrack.Metric
    let start: Date
    let end: Date
    let upper: Double
    let tint: Color

    /// Tall enough to show shape, short enough that three of them plus the
    /// strip fit above the fold on a phone.
    private static let height: CGFloat = 72

    var body: some View {
        Canvas { context, size in
            let span = end.timeIntervalSince(start)
            // One point of headroom top and bottom so a 1.5pt stroke at the
            // extremes is not sliced in half by the frame.
            let plotHeight = max(1, size.height - 2)

            func x(_ date: Date) -> CGFloat {
                guard span > 0 else { return size.width }
                return CGFloat(date.timeIntervalSince(start) / span) * size.width
            }
            func y(_ value: Double) -> CGFloat {
                guard upper > 0 else { return plotHeight + 1 }
                let clamped = min(max(value, 0), upper)
                return 1 + plotHeight - CGFloat(clamped / upper) * plotHeight
            }

            // SHADED FIRST, UNDER EVERYTHING. An absence drawn as bare
            // background is indistinguishable from a line that happens to be
            // out of frame; a band is unambiguous and costs one fill.
            for gap in track.gaps(metric) {
                let from = x(gap.from)
                let to = x(gap.to)
                guard to > from else { continue }
                context.fill(Path(CGRect(x: from, y: 0, width: to - from, height: size.height)),
                             with: .color(Ink.rule.opacity(0.55)))
            }

            let spans = track.spans(metric)
            for span in spans {
                // A stroked path through one point renders as nothing at all,
                // so a lone measurement would silently disappear - and a lone
                // measurement is exactly the case worth seeing.
                if span.isSinglePoint, let only = span.first {
                    context.fill(dot(at: CGPoint(x: x(only.at), y: y(only.value)), radius: 2),
                                 with: .color(tint))
                    continue
                }
                var path = Path()
                for (index, plot) in span.plots.enumerated() {
                    let point = CGPoint(x: x(plot.at), y: y(plot.value))
                    if index == 0 { path.move(to: point) } else { path.addLine(to: point) }
                }
                context.stroke(path,
                               with: .color(tint),
                               style: StrokeStyle(lineWidth: 1.5, lineCap: .round, lineJoin: .round))
            }

            // The newest MEASURED value, marked so the eye lands on it. Not
            // necessarily at the right edge: if the leg stopped being measured
            // the mark sits back where the measurements stopped, which is the
            // honest place for it.
            if let latest = spans.last?.last {
                context.fill(dot(at: CGPoint(x: x(latest.at), y: y(latest.value)), radius: 2.5),
                             with: .color(tint))
            }
        }
        .frame(height: Self.height)
    }

    private func dot(at point: CGPoint, radius: CGFloat) -> Path {
        Path(ellipseIn: CGRect(x: point.x - radius, y: point.y - radius,
                               width: radius * 2, height: radius * 2))
    }
}

/// Did this leg report, and what was it doing when it did.
///
/// One block per tick, coloured by the state at that tick, and BLANK where no
/// tick arrived. The blank is the point of the component: it is the only place
/// on this screen where "the leg was absent" is visible at a glance across the
/// whole window.
private struct ReportingStrip: View {
    let track: LegTrack
    let start: Date
    let end: Date

    var body: some View {
        Canvas { context, size in
            let span = end.timeIntervalSince(start)
            guard span > 0 else {
                // A single reading has no width to speak of. Draw the block it
                // earned and nothing more.
                context.fill(Path(CGRect(x: 0, y: 0, width: 3, height: size.height)),
                             with: .color(colour(track.readings.first)))
                return
            }
            // Each block covers the tick it opened, so consecutive reports form
            // a solid run and a missed tick leaves a hole of its own width.
            let tick = track.cadence ?? span
            for reading in track.readings {
                let x = CGFloat(reading.at.timeIntervalSince(start) / span) * size.width
                let width = max(1, CGFloat(tick / span) * size.width)
                context.fill(Path(CGRect(x: x, y: 0,
                                         width: min(width, max(0, size.width - x)),
                                         height: size.height)),
                             with: .color(colour(reading)))
            }
        }
        .frame(height: 10)
    }

    private func colour(_ reading: LegTrack.Reading?) -> Color {
        guard let reading else { return Ink.tertiary }
        // Ink.live still means exactly one thing - carrying traffic - it is
        // simply being said about a moment in the past rather than about now.
        if reading.isCarrying { return Ink.live }
        switch reading.state {
        case "degraded": return Ink.degraded
        case "down":     return Ink.down
        default:         return Ink.tertiary
        }
    }
}
