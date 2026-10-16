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
    /// #77: see `RelayScreen.rumViewName` for why this is named once, here.
    private static let rumViewName = "Diagnostics"

    @StateObject private var model: DiagnosticsModel

    init(consoleHost: String?, onPairingChanged: @escaping () -> Void = {}) {
        _model = StateObject(wrappedValue: DiagnosticsModel(consoleHost: consoleHost,
                                                            onPairingChanged: onPairingChanged))
    }

    var body: some View {
        Page {
            header
            rows
            tokenSection
            pairingSection
            refresh
        }
        .task { await model.measure() }
        // #77: this screen exists because something is wrong - its own RUM
        // events, including whatever the operator taps next, were attributing
        // to ApplicationLaunch like every screen was before this.
        .onAppear { Observability.viewAppeared(Self.rumViewName) }
        .onDisappear { Observability.viewDisappeared(Self.rumViewName) }
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

    private var tokenSection: some View {
        VStack(alignment: .leading, spacing: 0) {
            SectionHead(title: "Write token")

            Readout(label: "On this phone",
                    value: model.hasToken ? "stored" : "not stored",
                    tone: model.hasToken ? Ink.primary : Ink.degraded)
            Hairline()
            SecretRow(label: "Token", text: $model.tokenEntry)

            HStack(spacing: Space.snug) {
                ActionButton(title: "Store token", role: .quiet,
                             enabled: !model.tokenEntry.isEmpty) {
                    model.saveToken()
                }
                if model.hasToken {
                    ActionButton(title: "Forget", role: .quiet) { model.forgetToken() }
                }
            }
            .padding(.top, Space.snug)

            if let error = model.tokenError {
                Note(text: error, tone: .warning)
            }

            Note(text: "Reads are open; writes are not. The router generates "
               + "this token on first use and keeps it as console_token in its "
               + "state directory. Without a write token, this phone cannot "
               + "join the bond. It is held in this phone's keychain and is "
               + "never shown again once stored.")
        }
    }

    private var pairingSection: some View {
        VStack(alignment: .leading, spacing: 0) {
            SectionHead(title: "Pairing")

            Readout(label: "On this phone",
                    value: model.hasPairing ? "stored" : "not stored",
                    tone: model.hasPairing ? Ink.primary : Ink.degraded)
            Hairline()
            SecretRow(label: "Bundle", text: $model.pairingEntry)

            HStack(spacing: Space.snug) {
                ActionButton(title: "Store bundle", role: .quiet,
                             enabled: !model.pairingEntry.isEmpty) {
                    model.savePairing()
                }
                if model.hasPairing {
                    ActionButton(title: "Forget", role: .quiet) { model.forgetPairing() }
                }
            }
            .padding(.top, Space.snug)

            if let error = model.pairingError {
                Note(text: error, tone: .warning)
            }

            Note(text: "This phone's pairing credential and the discovery roster "
               + "(zippie-clienthome -discovery-roster), as one JSON bundle. With "
               + "it, this phone proves itself "
               + "to other zippie devices on the same wifi and checks theirs "
               + "before offering them as legs. Held in the keychain and never "
               + "shown again once stored.")
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

/// A password field in the same column as every other value on the page.
///
/// SecureField rather than FieldRow, because this is the one value here that is
/// a credential and shoulder-surfing a router token on a train is a real way to
/// lose control of a household's routing.
private struct SecretRow: View {
    let label: String
    @Binding var text: String

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: Space.base) {
            Text(label)
                .font(Kind.body())
                .foregroundStyle(Ink.secondary)
            Spacer(minLength: Space.snug)
            SecureField("", text: $text)
                .font(Kind.figure(17))
                .foregroundStyle(Ink.primary)
                .multilineTextAlignment(.trailing)
                .autocorrectionDisabled()
                .textInputAutocapitalization(.never)
        }
        .padding(.vertical, Space.snug)
    }
}
