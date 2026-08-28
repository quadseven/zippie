import SwiftUI
import ZippieCompanionKit

/// Why this phone is, or is not, reachable - said in one sentence and then in
/// detail.
///
/// The view RENDERS and does not decide. Every judgement (which failure, which
/// tone, whether via-router counts as good) lives in `Diagnostics` in the Kit,
/// where `swift test` reaches it. That split is not decoration: RelayVerdict
/// exists because a sentence computed inside a SwiftUI view shipped wrong and
/// could not be tested.
struct DiagnosticsScreen: View {
    @StateObject private var model: DiagnosticsModel

    init(consoleHost: String?) {
        _model = StateObject(wrappedValue: DiagnosticsModel(consoleHost: consoleHost))
    }

    var body: some View {
        Page {
            header
            rows
            refresh
        }
        .task { await model.measure() }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: Space.snug) {
            Text(model.diagnostics.headline)
                .font(Kind.display())
                .tracking(-0.8)
                .foregroundStyle(Ink.primary)
                .fixedSize(horizontal: false, vertical: true)

            // The one thing worth saying above everything else. A phone that is
            // reachable ONLY because some router forwards for it is one SSID
            // change from unmanageable, and that is exactly how a managed Pixel
            // went dark for half an hour.
            if case .viaRouter = model.diagnostics.tailnet {
                Note(text: "This phone has no Tailscale of its own. It can reach "
                   + "the MDM only while on this network.")
            }
        }
        .padding(.bottom, Space.base)
    }

    private var rows: some View {
        VStack(alignment: .leading, spacing: 0) {
            SectionHead(title: "Detail")
            ForEach(Array(model.diagnostics.rows().enumerated()), id: \.offset) { i, row in
                if i > 0 { Hairline() }
                VStack(alignment: .leading, spacing: 2) {
                    Readout(label: row.label, value: row.value, tone: tone(row.tone))
                    if let hint = row.hint {
                        Text(hint)
                            .font(Kind.caption())
                            .foregroundStyle(Ink.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(.vertical, 2)
            }
        }
    }

    private var refresh: some View {
        ActionButton(title: model.measuring ? "Measuring..." : "Refresh",
                     role: .quiet, enabled: !model.measuring) {
            Task { await model.measure() }
        }
        .padding(.top, Space.base)
    }

    /// The ONLY place tone becomes colour. Kept to a switch so a new tone in
    /// the Kit is a compile error here rather than a silently grey row.
    private func tone(_ t: DiagnosticRow.Tone) -> Color {
        switch t {
        case .good:    return Ink.primary
        case .bad:     return Ink.down
        case .unknown: return Ink.secondary
        case .note:    return Ink.degraded
        }
    }
}
