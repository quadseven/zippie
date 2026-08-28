import NetworkExtension
import SwiftUI
import ZippieCompanionKit

private struct RouterSSIDRow: Identifiable {
    let id = UUID()
    var name: String
}

/// Relay, rebuilt on the design system.
///
/// WHAT CHANGED AND WHY. The old screen was five stock `List` sections in
/// declaration order: background, settings, status, foreground, wiring. That
/// order is the order the features were BUILT in, not the order anyone needs
/// them. Opening it, the first thing you met was a control; the thing you
/// actually came for - is this phone contributing - was three sections down in
/// grey `LabeledContent`.
///
/// It now opens with the state, then the single control that changes it, then
/// the numbers backing it, and only then the endpoint fields and the fallback
/// that almost nobody should touch. Same capabilities, same behaviour, ordered
/// by what the reader needs first.
///
/// The foreground relay is still here and still last. It is the only relay that
/// works on a build whose extension cannot be signed yet (ADR 0020), and
/// deleting a proven capability because it is unglamorous would leave the
/// operator with nothing.
struct RelayScreen: View {
    /// The LIVE mode decision, not a second copy of the reasoning.
    ///
    /// `BondModel` probes the console every five seconds and is the only thing
    /// that knows whether the router answered on its LAN address - which is the
    /// entire basis for contribute versus client (ADR 0022). Before #48 that
    /// verdict was rendered on the Status screen and acted on nowhere, so the
    /// start button below could only ever install a contributor profile.
    @ObservedObject var bond: BondModel

    @StateObject private var tunnel = TunnelController()

    @State private var homeHost = Settings.homeHost
    @State private var homePort = String(Settings.homePort)
    @State private var listenPort = String(Settings.listenPort)
    @State private var routerSSIDRows: [RouterSSIDRow] = {
        let configured = Settings.routerSSIDs
        return (configured.isEmpty ? [""] : configured).map(RouterSSIDRow.init(name:))
    }()
    @State private var currentSSID: String?

    @State private var foregroundRelay: CellularRelay?
    @State private var foregroundStats = CellularRelay.Stats()
    @State private var foregroundRunning = false
    @State private var startError: String?
    @State private var showsAdvanced = false

    private var editingLocked: Bool {
        foregroundRunning || tunnel.status == .connected || tunnel.status == .connecting
    }

    private var running: Bool {
        tunnel.status == .connected || tunnel.status == .connecting
            || tunnel.status == .reasserting
    }

    var body: some View {
        Page {
            header
            control
            numbers
            endpoint
            routerNetworks
            advanced
        }
        .task { await tunnel.refresh() }
        .task {
            while !Task.isCancelled {
                currentSSID = await NetworkFacts.current().ssid
                try? await Task.sleep(nanoseconds: 5_000_000_000)
            }
        }
        .task {
            // Polled because there is no cross-process notification when the
            // extension writes its report. One second is well inside the
            // staleness threshold, so a dead extension shows up within a couple
            // of ticks rather than never.
            while !Task.isCancelled {
                await tunnel.refreshReport()
                // Supervision reads the report this call just refreshed, so it
                // rides the same tick rather than owning a timer that could
                // judge a snapshot one second out of date. Its own thresholds
                // are minutes wide; the polling rate is not the thing that
                // decides anything (see RelaySupervision).
                await tunnel.supervise()
                try? await Task.sleep(nanoseconds: 1_000_000_000)
            }
        }
    }

    // MARK: - state, first

    private var header: some View {
        VStack(alignment: .leading, spacing: Space.snug) {
            Text(verdict.headline)
                .font(Kind.display())
                .tracking(-0.8)
                .foregroundStyle(Ink.primary)
                .fixedSize(horizontal: false, vertical: true)
            // Named when known, so "the router" is never left to be read as
            // the wifi router this phone is joined to (#44 operator
            // follow-up, 2026-08-08). See Settings.routerDisplayName.
            Text(verdict.detail(router: Settings.routerDisplayName))
                .font(Kind.body())
                .foregroundStyle(Ink.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if foregroundRunning {
                // Said here as well as in Advanced, because Advanced is
                // collapsed by default and this caveat decides whether the leg
                // survives the next thirty seconds.
                Note(text: "Running inside the app. Keep this screen open and "
                   + "the phone charging.")
            }
        }
        .padding(.top, Space.section)
        .padding(.bottom, Space.roomy)
        .accessibilityElement(children: .combine)
    }

    /// What the screen may honestly say, decided in the Kit (`RelayVerdict`).
    ///
    /// THE VIEW COMPUTES NO TRUTH. It maps `NEVPNStatus` onto the Kit's
    /// vocabulary and renders the answer. The version that decided for itself
    /// is the one that shipped "Connected to the router, waiting for traffic to
    /// carry." on a phone the router had never dialled (#44, seen live
    /// 2026-08-07) - derived from `cellularReady`, which is a fact about this
    /// phone's own radio and says nothing whatsoever about the far end.
    private var verdict: RelayVerdict {
        if foregroundRunning {
            // In-process counters, so they are current by construction: there
            // is no second binary here that could have died holding them, which
            // is the entire reason RelayStatus carries a timestamp at all.
            return RelayVerdict.evaluate(
                run: .running,
                report: RelayStatus(stats: foregroundStats, updatedAt: Date()))
        }
        let run: RelayRun
        switch tunnel.status {
        case .connected:                run = .running
        case .connecting, .reasserting: run = .starting
        case .disconnecting:            run = .stopping
        default:                        run = .off
        }
        return RelayVerdict.evaluate(run: run, report: tunnel.report)
    }

    // MARK: - the one control

    private var control: some View {
        VStack(alignment: .leading, spacing: Space.snug) {
            if running {
                ActionButton(title: "Stop relaying", role: .destructive) {
                    tunnel.stopTunnel()
                }
            } else {
                ActionButton(title: tunnel.installed ? "Start relaying" : "Install and start",
                             enabled: !foregroundRunning) { startBackground() }
            }

            Note(text: "Runs in a Network Extension, so the leg survives the screen "
               + "locking. iOS shows a VPN badge while it runs; the tunnel carries "
               + "none of this phone's own traffic. Keep the phone charging - "
               + "relaying over cellular runs the radio hot.")

            if let e = tunnel.lastError ?? startError {
                Note(text: e, tone: .warning)
            }
            // WHAT SUPERVISION MADE OF IT, shown only when it found a fault.
            //
            // A relay that is fine has nothing to say here and a permanent
            // "supervision: healthy" row is furniture that trains the eye to
            // skip the place a real sentence would appear - the same reason the
            // Errors row below is hidden at zero. But when the relay IS wedged,
            // the sentence includes why nothing is being done about it, which
            // is the line that stops somebody debugging the router: on
            // 2026-08-22 the router showed 65% loss and a null round trip while
            // this screen said "Ready" and no part of the app explained the
            // gap.
            if let pass = tunnel.supervision, pass.verdict.isFault {
                Note(text: pass.remedy.why, tone: .warning)
            }
        }
    }

    // MARK: - what backs it

    /// The extension's report wins when the tunnel is up; otherwise the
    /// foreground relay's counters. They are never both live - each control
    /// disables the other - because two processes bound to one UDP port is a
    /// bug, not redundancy.
    private var shownStats: CellularRelay.Stats {
        if tunnel.status == .connected, let r = tunnel.report { return r.stats }
        return foregroundStats
    }

    /// "nothing yet" rather than "never": the relay may have started a second
    /// ago, and "never" reads as a verdict on the router when it is a statement
    /// about how long we have been listening.
    private var routerInboundText: String {
        guard let at = shownStats.lastRouterInboundAt else { return "nothing yet" }
        let age = Date().timeIntervalSince(at)
        return age < 2 ? "just now" : "\(RelayVerdict.ago(age)) ago"
    }

    private var numbers: some View {
        VStack(alignment: .leading, spacing: 0) {
            SectionHead(title: "Carried")
            Readout(label: "To home",
                    value: "\(shownStats.upDatagrams) pkt  \(Fmt.bytes(UInt64(max(0, shownStats.upBytes))))")
            Hairline()
            Readout(label: "To router",
                    value: "\(shownStats.downDatagrams) pkt  \(Fmt.bytes(UInt64(max(0, shownStats.downBytes))))")
            Hairline()
            // THE EVIDENCE BEHIND THE HEADLINE, shown rather than implied. Every
            // other row here is a fact about this phone; this is the only one
            // that says the router exists, and #44 was a headline that claimed
            // it without a row like this to back it.
            Readout(label: "From router",
                    value: routerInboundText,
                    tone: shownStats.lastRouterInboundAt == nil ? Ink.degraded : Ink.primary)
            Hairline()
            Readout(label: "Cellular",
                    value: shownStats.cellularReady ? "ready" : "not ready",
                    tone: shownStats.cellularReady ? Ink.primary : Ink.down)
            // Shown only when non-zero. A permanent "Errors 0" row is a line of
            // furniture that trains the eye to skip the place a real number
            // would appear.
            if shownStats.errors > 0 {
                Hairline()
                Readout(label: "Errors", value: "\(shownStats.errors)", tone: Ink.degraded)
            }
            if shownStats.rejectedSources > 0 {
                Hairline()
                Readout(label: "Refused senders", value: "\(shownStats.rejectedSources)",
                        tone: Ink.degraded)
            }
            if let e = shownStats.lastError {
                Note(text: e, tone: .warning)
            }
        }
    }

    // MARK: - where it points

    /// The heading that made a reader ask "which router is it connected to?"
    /// (#44). This block is the HOME end - where the relay forwards TO - and it
    /// sat directly under a headline about the router with nothing to separate
    /// them. Only "Listen on" faces the router, so the note says which is which
    /// rather than leaving the reader to infer it from three field names.
    private var endpointNote: String {
        let what = "Where this phone forwards to: the home end, not the router. "
                 + "The router reaches this phone on the listen port below."
        return editingLocked ? what + " Stop the relay to change these." : what
    }

    private var endpoint: some View {
        VStack(alignment: .leading, spacing: 0) {
            SectionHead(title: "Home transport", note: endpointNote)
            FieldRow(label: "Host", text: $homeHost)
            Hairline()
            FieldRow(label: "Port", text: $homePort, keyboard: .numberPad)
            Hairline()
            FieldRow(label: "Listen on", text: $listenPort, keyboard: .numberPad)
            Note(text: "Point the router's leg at this phone's wifi address on port "
               + "\(listenPort).")
        }
        .disabled(editingLocked)
        .opacity(editingLocked ? 0.5 : 1)
    }

    private var routerNetworks: some View {
        let policy = OnDemandPolicy(routerSSIDs: formRouterSSIDs)
        return VStack(alignment: .leading, spacing: 0) {
            SectionHead(
                title: "Router wifi",
                note: "The contributor tunnel starts on any network listed here. "
                    + "Add every wifi name the router broadcasts. Matching is exact."
            )
            ForEach($routerSSIDRows) { $row in
                let index = routerSSIDRows.firstIndex { $0.id == row.id } ?? 0
                if index > 0 { Hairline() }
                HStack(spacing: Space.snug) {
                    FieldRow(label: "Name \(index + 1)", text: $row.name)
                    if routerSSIDRows.count > 1 {
                        Button {
                            routerSSIDRows.removeAll { $0.id == row.id }
                        } label: {
                            Image(systemName: "minus.circle")
                                .foregroundStyle(Ink.degraded)
                        }
                        .accessibilityLabel("Remove wifi name \(index + 1)")
                    }
                }
            }
            Button {
                routerSSIDRows.append(RouterSSIDRow(name: ""))
            } label: {
                Label("Add wifi name", systemImage: "plus.circle")
                    .font(Kind.caption())
                    .foregroundStyle(Ink.live)
                    .padding(.top, Space.tight)
            }
            .buttonStyle(.plain)
            Note(text: policy.explain(currentSSID: currentSSID),
                 tone: policy.shouldRun(onSSID: currentSSID) ? .plain : .warning)
        }
        .disabled(editingLocked)
        .opacity(editingLocked ? 0.5 : 1)
    }

    private var formRouterSSIDs: [String] {
        RelayConfiguration.normalizedRouterSSIDs(routerSSIDRows.map(\.name))
    }

    // MARK: - the part almost nobody should touch

    /// Collapsed by default, and that is not tidying. The foreground relay and
    /// removing the VPN profile are both ways to end up with a phone that looks
    /// configured and carries nothing; putting them behind a deliberate tap
    /// keeps them reachable without offering them.
    private var advanced: some View {
        VStack(alignment: .leading, spacing: 0) {
            SectionHead(title: "Advanced")

            Button {
                withAnimation(.snappy(duration: 0.2)) { showsAdvanced.toggle() }
            } label: {
                HStack {
                    Text(showsAdvanced ? "Hide" : "Show")
                        .font(Kind.body())
                        .foregroundStyle(Ink.live)
                    Spacer()
                    Image(systemName: showsAdvanced ? "chevron.up" : "chevron.down")
                        .font(.footnote)
                        .foregroundStyle(Ink.live)
                }
                .padding(.vertical, Space.snug)
            }
            .buttonStyle(.plain)

            if showsAdvanced {
                VStack(alignment: .leading, spacing: Space.snug) {
                    Hairline()
                    ActionButton(title: foregroundRunning
                                 ? "Stop foreground relay" : "Start foreground relay",
                                 role: .quiet,
                                 enabled: !running) { toggleForeground() }
                    Note(text: "Runs the relay inside the app, as it did before the "
                       + "Network Extension existed. Only useful on a build whose "
                       + "tunnel cannot be signed yet - iOS suspends background apps, "
                       + "so the leg dies when you leave this screen.")

                    if tunnel.installed {
                        ActionButton(title: "Remove VPN configuration",
                                     role: .destructive) {
                            Task { await tunnel.removeConfiguration() }
                        }
                    }
                }
                .transition(.opacity)
            }
        }
    }

    // MARK: - actions (unchanged behaviour)

    /// Reads the form, and refuses rather than substituting a default. A relay
    /// pointed at the wrong host looks identical to a working one from here.
    private func configFromForm() -> RelayConfiguration? {
        guard let hp = RelayConfiguration.port(homePort),
              let lp = RelayConfiguration.port(listenPort),
              !homeHost.trimmingCharacters(in: .whitespaces).isEmpty else { return nil }
        return RelayConfiguration(homeHost: homeHost.trimmingCharacters(in: .whitespaces),
                                  homePort: hp, listenPort: lp,
                                  routerSSIDs: formRouterSSIDs)
    }

    private func startBackground() {
        guard let config = configFromForm() else {
            startError = "Check the host and ports below."
            return
        }
        startError = nil
        persist(config)
        Task {
            // `client: nil` is the honest value, not a placeholder. Nothing can
            // mint a `ClientConfig` until the pairing ceremony exists (#31), so
            // `TunnelPlan.decide` resolves to contribute with the reason
            // `clientModeNotConfigured` and the profile installed is byte for
            // byte the one that ships today.
            //
            // A refusal arrives as `tunnel.lastError`, which the header above
            // already prefers over `startError` - so the plan's own sentence
            // reaches the screen without a second copy of it here.
            await tunnel.startTunnel(with: config, decision: bond.decision, client: nil)
            Observability.tunnelStatus(tunnel.status, error: tunnel.lastError)
        }
    }

    private func persist(_ config: RelayConfiguration) {
        Settings.homeHost = config.homeHost
        Settings.homePort = Int(config.homePort)
        Settings.listenPort = Int(config.listenPort)
        Settings.routerSSIDs = config.routerSSIDs
    }

    private func toggleForeground() {
        if foregroundRunning {
            Task {
                await foregroundRelay?.stop()
                foregroundRelay = nil
                foregroundRunning = false
            }
            return
        }
        guard let config = configFromForm() else {
            startError = "Check the host and ports below."
            return
        }
        persist(config)
        startError = nil

        let r = CellularRelay(config: config.relayConfig)
        foregroundRelay = r
        Task {
            do {
                try await r.start { s in
                    Task { @MainActor in foregroundStats = s }
                    Observability.relayStats(s)
                }
                await MainActor.run { foregroundRunning = true }
            } catch {
                await MainActor.run {
                    startError = "Start failed: \(error.localizedDescription)"
                    foregroundRelay = nil
                }
            }
        }
    }
}
