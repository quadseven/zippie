import Network
import SwiftUI
import ZippieCompanionKit

/// Probe, rebuilt on the design system.
///
/// THIS SCREEN EXISTS TO ANSWER ONE QUESTION and it now looks like it. The old
/// version led with a button and buried the answer in a `Label` beneath two
/// rows of raw addresses - so the reader met the evidence before the finding,
/// which is backwards for a tool whose entire output is a verdict.
///
/// The verdict is now the page. The two addresses stay, underneath, as the
/// evidence for it: they are what makes the verdict checkable rather than
/// something to be believed, and v1 of this probe was WRONG in exactly the way
/// that matters (it read iCloud Private Relay exits as proof), so showing the
/// working is not decoration.
struct ProbeScreen: View {
    @State private var running = false
    @State private var verdict: ProbeVerdict?
    @State private var baseline: String?
    @State private var cellular: String?
    @State private var elapsed: String?

    var body: some View {
        Page {
            header
            control
            if baseline != nil || cellular != nil { evidence }
            method
        }
    }

    // MARK: - the finding

    private var header: some View {
        VStack(alignment: .leading, spacing: Space.snug) {
            Text(headline)
                .font(Kind.display())
                .tracking(-0.8)
                .foregroundStyle(Ink.primary)
                .fixedSize(horizontal: false, vertical: true)
            Text(detail)
                .font(Kind.body())
                .foregroundStyle(Ink.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.top, Space.section)
        .padding(.bottom, Space.roomy)
        .accessibilityElement(children: .combine)
    }

    /// Short enough to be the headline, and never overstated. "Inconclusive" is
    /// a real result here and is worded as one rather than as a soft failure -
    /// the probe not being able to tell is different from the pin not working.
    private var headline: String {
        guard let verdict else { return running ? "Probing" : "Not run" }
        switch verdict {
        case .proven:                  return "Pinned"
        case .inconclusiveSameEgress:  return "Inconclusive"
        case .maskedByPrivateRelay:    return "Masked"
        case .cellularUnavailable:     return "No cellular"
        case .baselineFailed:          return "No baseline"
        }
    }

    private var detail: String {
        if let verdict { return verdict.summary }
        return running
            ? "Fetching the public egress address twice."
            : "Checks whether a socket can actually be pinned to the cellular "
            + "radio on this device."
    }

    private var headlineTone: Color {
        guard let verdict else { return Ink.tertiary }
        switch verdict {
        case .proven:                                 return Ink.live
        case .inconclusiveSameEgress, .maskedByPrivateRelay: return Ink.degraded
        case .cellularUnavailable, .baselineFailed:   return Ink.down
        }
    }

    // MARK: - run it

    private var control: some View {
        VStack(alignment: .leading, spacing: Space.snug) {
            ActionButton(title: running ? "Probing" : (verdict == nil ? "Run probe" : "Run again"),
                         busy: running) { run() }
            if let verdict {
                // A coloured rule rather than an icon: the verdict already has
                // words, and a seal or an octagon would only restate them in a
                // form that has to be learned.
                Rectangle()
                    .fill(headlineTone)
                    .frame(height: 2)
                    .accessibilityHidden(true)
                    .accessibilityLabel(verdict.summary)
            }
        }
    }

    // MARK: - the working

    private var evidence: some View {
        VStack(alignment: .leading, spacing: 0) {
            SectionHead(title: "Observed egress",
                        note: "Different addresses are what prove the pin.")
            Readout(label: "Default path", value: baseline ?? "-")
            Hairline()
            Readout(label: "Pinned to cellular", value: cellular ?? "-")
            if let elapsed {
                Hairline()
                Readout(label: "Took", value: elapsed, tone: Ink.secondary)
            }
        }
    }

    private var method: some View {
        VStack(alignment: .leading, spacing: 0) {
            SectionHead(title: "How to run it")
            Note(text: "On wifi, with cellular data enabled, and NOT tethered to the "
               + "router under test - sharing an egress makes the result ambiguous "
               + "rather than negative.")
            Note(text: "The first version of this probe reported PROVEN when both "
               + "requests exited through iCloud Private Relay. It now speaks HTTPS "
               + "and refuses to call a relay exit proof.")
        }
    }

    // MARK: - action (unchanged behaviour)

    private func run() {
        running = true
        verdict = nil
        baseline = nil
        cellular = nil
        elapsed = nil

        Task {
            let probe = CellularProbe()
            let started = Date()
            // Fetched fresh rather than bundled: Apple rotates these ranges,
            // and a stale list would silently stop catching masked results.
            // Failure is fine - the verdict falls back to the /24 heuristic.
            let ranges = await fetchRelayRanges()

            // Sequential, not concurrent, and deliberately so: two sockets
            // opening at once can influence which path the system picks for the
            // unpinned one, which is the exact thing being measured.
            let base = await probe.egressAddress(interface: nil)
            let cell = await probe.egressAddress(interface: .cellular)

            await MainActor.run {
                baseline = describe(base)
                cellular = describe(cell)
                verdict = ProbeEvaluator.evaluate(baseline: base, cellular: cell,
                                                  relayRanges: ranges)
                let secs = Date().timeIntervalSince(started)
                elapsed = String(format: "%.1fs", secs)
                running = false
                if let v = verdict {
                    Observability.probeCompleted(v, wifi: baseline ?? "-",
                                                 cellular: cellular ?? "-", seconds: secs)
                }
            }
        }
    }

    private func fetchRelayRanges() async -> PrivateRelayRanges? {
        guard let u = URL(string: "https://mask-api.icloud.com/egress-ip-ranges.csv") else { return nil }
        var req = URLRequest(url: u); req.timeoutInterval = 8
        // Apple's range list is deliberately NOT a first-party host, so this
        // appears as a RUM resource without our trace headers being injected
        // into someone else's API.
        guard let (d, _) = try? await Observability.tracedSession.data(for: req),
              let csv = String(data: d, encoding: .utf8) else { return nil }
        return PrivateRelayRanges(csv: csv)
    }

    private func describe(_ r: Result<String, ProbeError>) -> String {
        switch r {
        case let .success(v): return v
        case let .failure(e): return e.description
        }
    }
}
