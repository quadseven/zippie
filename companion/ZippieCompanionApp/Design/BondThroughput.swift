import SwiftUI
import ZippieCompanionKit

/// The live picture of the whole bond: every leg's throughput, stacked, moving.
///
/// WHY THIS IS ON THE MAIN SCREEN AND THE PER-LEG HISTORY IS NOT. The question
/// this answers - "is it working, and which links are doing the work" - is the
/// one someone has while glancing at a dashboard. Per-leg RTT and loss are for
/// after you already know something is wrong, which is why they live one tap
/// down.
///
/// STACKED, NOT OVERLAID. Overlaid lines answer "how fast is each leg", which
/// nobody asks. Stacked answers "how much are we getting, and who is providing
/// it" in one shape, and the total is the top edge.
///
/// The bars are DELIVERED BYTES from the transport's own per-link counters.
/// Until tonight the series carried tx_bps/rx_bps that were null on every
/// sample ever recorded - the field existed, was serialised, and was computed
/// from a sysfs path packet mode does not create. A chart drawn from that would
/// have been a permanently empty box.
struct BondThroughput: View {
    let samples: [BondSeries.Point]
    /// Stable colour per leg name, so a leg does not change colour when
    /// another one joins or leaves.
    let order: [String]
    /// Router name -> human label. The series keys by the internal name; the
    /// leg list directly below this chart shows labels, and a legend reading
    /// "companion-co-operator" under a row reading "Co-operator iPhone (Verizon)" makes the
    /// two look like different things.
    var labels: [String: String] = [:]

    private static let barWidth: CGFloat = 3
    private static let gap: CGFloat = 1

    var body: some View {
        VStack(alignment: .leading, spacing: Space.tight) {
            if let peak = peakTotal, peak > 0 {
                chart(peak: peak)
                legend
            } else {
                // NOT AN EMPTY CHART. A blank axis reads as "broken"; words
                // read as "nothing is moving", which is a different and often
                // correct state.
                Text(samples.isEmpty
                     ? "No history from the router yet."
                     : "No traffic across the bond in this window.")
                    .font(Kind.caption())
                    .foregroundStyle(Ink.tertiary)
                    .padding(.vertical, Space.base)
            }
        }
    }

    private func chart(peak: Double) -> some View {
        GeometryReader { geo in
            let visible = Int(geo.size.width / (Self.barWidth + Self.gap))
            let shown = Array(samples.suffix(max(1, visible)))
            HStack(alignment: .bottom, spacing: Self.gap) {
                ForEach(Array(shown.enumerated()), id: \.offset) { _, point in
                    VStack(spacing: 0) {
                        Spacer(minLength: 0)
                        ForEach(order, id: \.self) { leg in
                            let bps = throughput(point, leg)
                            if bps > 0 {
                                Rectangle()
                                    .fill(tint(for: leg))
                                    .frame(height: geo.size.height * (bps / peak))
                            }
                        }
                    }
                    .frame(width: Self.barWidth)
                }
            }
            .frame(maxWidth: .infinity, alignment: .trailing)
        }
        .frame(height: 72)
        .accessibilityLabel(accessibilitySummary)
    }

    private var legend: some View {
        VStack(alignment: .leading, spacing: Space.tight) {
            // FLOWING, NOT ONE ROW. Four legs on one line truncated every name
            // to "Co-operator iP..." and "Repeat...", which identifies nothing - a
            // legend whose labels are unreadable is decoration. Wrapping costs
            // one line and keeps the names whole.
            FlowRow(spacing: Space.base, rowSpacing: Space.hair) {
                ForEach(order.filter { carried($0) }, id: \.self) { leg in
                    HStack(spacing: Space.hair) {
                        Circle().fill(tint(for: leg)).frame(width: 6, height: 6)
                        Text(labels[leg] ?? leg)
                            .font(Kind.caption())
                            .foregroundStyle(Ink.secondary)
                            .fixedSize()
                    }
                }
            }
            if let peak = peakTotal, peak > 0 {
                Text("peak \(Fmt.rate(peak))")
                    .font(Kind.figure(12))
                    .foregroundStyle(Ink.tertiary)
            }
        }
    }

    // MARK: - data

    private func throughput(_ p: BondSeries.Point, _ leg: String) -> Double {
        guard let m = p.paths?[leg] else { return 0 }
        // WEIGHT ZERO MEANS KEEPALIVES, NOT TRAFFIC. A companion leg whose
        // phone has left still receives a probe every 500ms, which is real
        // bytes on a real socket and charted as ~50 kbps of throughput - so a
        // black hole appeared to be contributing. The scheduler gives a
        // weight-0 leg no data, so anything it moves is overhead.
        guard (m.weight ?? 0) > 0 else { return 0 }
        // Both directions, because a bond's value is total capacity and an
        // upload-heavy leg is doing just as much work as a download-heavy one.
        return (m.txBps ?? 0) + (m.rxBps ?? 0)
    }

    private func carried(_ leg: String) -> Bool {
        samples.contains { throughput($0, leg) > 0 }
    }

    private var peakTotal: Double? {
        samples.map { p in order.reduce(0) { $0 + throughput(p, $1) } }.max()
    }

    /// Colour by POSITION in a stable order, not by hashing the name - a hash
    /// gives two legs the same colour often enough to matter with five of them.
    private func tint(for leg: String) -> Color {
        guard let i = order.firstIndex(of: leg) else { return Ink.tertiary }
        let palette: [Color] = [Ink.live, Ink.degraded, Ink.down,
                                Ink.secondary, Ink.tertiary]
        return palette[i % palette.count]
    }

    private var accessibilitySummary: String {
        guard let peak = peakTotal, peak > 0 else { return "No traffic in this window." }
        let active = order.filter { carried($0) }
        return "Throughput over time. \(active.count) links carrying, peak \(Fmt.rate(peak))."
    }
}

extension Fmt {
    /// Bits per second, in the units people actually say.
    static func rate(_ bps: Double) -> String {
        if bps >= 1_000_000 { return String(format: "%.1f Mbps", bps / 1_000_000) }
        if bps >= 1_000 { return String(format: "%.0f kbps", bps / 1_000) }
        return String(format: "%.0f bps", bps)
    }
}

/// Lays children left to right, wrapping to a new row when the width runs out.
///
/// SwiftUI has no built-in flow layout, and the alternatives are both worse
/// here: an HStack truncates every label to a stub, and a LazyVGrid forces a
/// fixed column count that leaves ragged gaps with four items of very
/// different widths.
struct FlowRow: Layout {
    var spacing: CGFloat = 8
    var rowSpacing: CGFloat = 4

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = proposal.width ?? .infinity
        var x: CGFloat = 0, y: CGFloat = 0, rowHeight: CGFloat = 0
        for v in subviews {
            let size = v.sizeThatFits(.unspecified)
            if x > 0, x + size.width > width {
                x = 0
                y += rowHeight + rowSpacing
                rowHeight = 0
            }
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
        }
        return CGSize(width: proposal.width ?? x, height: y + rowHeight)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize,
                       subviews: Subviews, cache: inout ()) {
        var x = bounds.minX, y = bounds.minY, rowHeight: CGFloat = 0
        for v in subviews {
            let size = v.sizeThatFits(.unspecified)
            if x > bounds.minX, x + size.width > bounds.maxX {
                x = bounds.minX
                y += rowHeight + rowSpacing
                rowHeight = 0
            }
            v.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(size))
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
        }
    }
}
