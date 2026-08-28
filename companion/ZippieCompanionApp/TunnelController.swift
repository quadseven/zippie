import Foundation
import NetworkExtension
import UIKit
import ZippieCompanionKit

/// One supervision pass, kept together so the screen can ask two different
/// questions of it.
///
/// The REMEDY carries the sentence a person reads; the VERDICT is what decides
/// whether the sentence is worth showing at all. Splitting them into two
/// published properties invited exactly one bug - a screen rendering last
/// tick's reason next to this tick's verdict - so they move as a pair.
struct SupervisionPass: Equatable {
    let verdict: RelaySupervision
    let remedy: RelayRemedy
}

/// App-side control of the packet-tunnel extension (ADR 0020, phase 2).
///
/// The app never touches the relay any more. It installs a VPN configuration
/// that names the extension, starts and stops it, and reads whatever the
/// extension leaves in the shared app group. That indirection is the entire
/// point: the relay now outlives the app, so the app has to treat it as a
/// separate, possibly-dead process rather than as an object it owns.
///
/// THE NETunnelProviderManager TRAPS, ALL OF WHICH FAIL QUIETLY
///
///   1. `loadAllFromPreferences` returns an EMPTY array until something has
///      been saved once. Empty is "never installed", not an error, and the UI
///      has to be able to say so.
///   2. After `saveToPreferences` you MUST `loadFromPreferences` again before
///      `startVPNTunnel`. The in-memory manager is stale the instant it is
///      saved, and starting from it fails with NEVPNError.configurationInvalid
///      - an error whose text says nothing about reloading. This is the single
///      most common way this code is written wrong.
///   3. `isEnabled` must be true BEFORE the save. A disabled configuration
///      saves happily and then refuses to start.
///   4. `providerBundleIdentifier` must equal the embedded extension's bundle
///      id exactly. A typo produces a configuration that saves, appears in
///      Settings, and never starts.
///   5. The first save triggers the system "would like to add VPN
///      configurations" prompt. The user can say no, and that arrives as a
///      thrown error rather than a callback - so the failure has to be shown,
///      not swallowed.
///   6. None of this works in the simulator. NetworkExtension needs a device.
///   7. `stopVPNTunnel()` returns before the tunnel is down - the teardown runs
///      in the system daemon. A `startVPNTunnel()` issued straight afterwards
///      is accepted against a connection that is still up and does nothing at
///      all, which is trap 2 in a different costume: the call succeeds and the
///      restart did not happen. See `supervise` and `waitForDisconnect`.
@MainActor
final class TunnelController: ObservableObject {
    @Published private(set) var status: NEVPNStatus = .invalid
    /// False until a configuration has been saved once. Distinct from
    /// "disconnected", which means installed but not running.
    @Published private(set) var installed = false
    @Published private(set) var lastError: String?
    /// The extension's own report, or nil when it has never reported.
    @Published private(set) var report: RelayStatus?
    /// The last supervision pass, INCLUDING every one that did nothing.
    ///
    /// Published rather than kept private because the answer that matters most
    /// is usually the refusal - "the relay has heard nothing for four minutes
    /// and I am holding because supervision already tried nine minutes ago" is
    /// the sentence that stops somebody debugging the router. Nil only before
    /// the first evaluation, or when there is no installed tunnel to judge.
    @Published private(set) var supervision: SupervisionPass?

    private var manager: NETunnelProviderManager?
    private var observer: NSObjectProtocol?
    /// True while a restart is in flight. The Relay screen polls once a second
    /// and a restart takes several, so without this the second tick would stop
    /// a tunnel the first tick had just started.
    private var restarting = false

    // MARK: - discovery

    func refresh() async {
        do {
            let managers = try await NETunnelProviderManager.loadAllFromPreferences()
            // Match on the provider bundle id rather than taking the first
            // entry: the user may have other VPN configurations from other
            // apps, and adopting one of those would be a spectacular bug.
            manager = managers.first {
                ($0.protocolConfiguration as? NETunnelProviderProtocol)?.providerBundleIdentifier
                    == RelayConfiguration.tunnelBundleIdentifier
            }
            installed = manager != nil
            status = manager?.connection.status ?? .invalid
            observeStatus()
        } catch {
            lastError = Self.describe(error)
        }
        await refreshReport()
    }

    // MARK: - install + start

    /// Saves the configuration and starts the tunnel in one operation, because
    /// there is no useful state between the two: an installed-but-never-started
    /// tunnel is just a confusing entry in the user's VPN settings.
    ///
    /// THE MODE IS DECIDED HERE AND NOWHERE ELSE (#48). iOS runs exactly ONE
    /// packet-tunnel provider at a time and this app has two opposite jobs for
    /// it, so the choice is made once, as a value, by `TunnelPlan.decide` - and
    /// the profile that carries it is written by `TunnelProfile`, in the Kit,
    /// where both are tested. This method used to assemble the profile inline
    /// and always wrote the relay's flat dictionary, so the `client` key the
    /// extension reads had no producer anywhere in the tree and client mode
    /// could not be entered at all.
    ///
    /// - Parameters:
    ///   - config: the relay endpoint from the form. Used only if the plan
    ///     comes out as contribute.
    ///   - decision: where this phone is, from the live console probe
    ///     (`BondModel`). Undetermined never starts client mode - see
    ///     `TunnelPlan.decide`.
    ///   - client: this phone's pairing, or nil when it has none. NIL AT EVERY
    ///     CALL SITE TODAY and that is not an oversight: there is no pairing
    ///     ceremony (#31), so nothing in the app can mint a client id or key.
    ///     The parameter exists so that the day #31 lands, the start path does
    ///     not have to be reopened - and so `TunnelPlan.decide` gets the same
    ///     inputs its tests give it.
    func startTunnel(with config: RelayConfiguration,
                     decision: ModeDecision,
                     client: ClientConfig?) async {
        lastError = nil

        var config = config
        announceSettings(into: &config)

        let plan = TunnelPlan.decide(decision, relay: config, client: client)
        // A held plan installs NOTHING, and says why in the operator's words
        // rather than "check host and ports" for every possible cause. A
        // half-configured client must not quietly become a relay in a hotel.
        guard let profile = TunnelProfile(plan: plan) else {
            lastError = plan.summary
            return
        }

        // The app group copy is the CONTRIBUTOR's fallback and the channel the
        // extension reports back on, so it is written even though the profile
        // is what the provider actually reads. Only on a contribute plan: it
        // can never produce client mode (see TunnelPlan.installed), and writing
        // it from a client start would let a half-filled form overwrite a
        // known-good stored host with nothing.
        if case let .contribute(relay, _) = plan,
           let shared = RelayConfiguration.sharedDefaults {
            relay.write(to: shared)
        }

        let m = manager ?? NETunnelProviderManager()
        // Bundle id, display address, provider dictionary and the SSID-scoped
        // on-demand rule (#2250), all replaced rather than merged - see
        // TunnelProfile.install.
        profile.install(on: m)

        do {
            try await m.saveToPreferences()
            // Trap 2. Without this reload the start below fails with a
            // configuration error that mentions nothing about staleness.
            try await m.loadFromPreferences()
            manager = m
            installed = true
            observeStatus()
            try m.connection.startVPNTunnel()
        } catch {
            lastError = Self.describe(error)
        }
    }

    func stopTunnel() {
        // A COOLDOWN LEFT BY AN AUTOMATIC RESTART MUST NOT SUPPRESS THE FIRST
        // SUPERVISION OF A RELAY A PERSON JUST STARTED. The extension clears it
        // too, on `.userInitiated`, which covers a stop from iOS Settings; this
        // covers the button on the Relay screen without waiting for the
        // extension to notice.
        if let shared = RelayConfiguration.sharedDefaults {
            RelaySupervisionStore.clear(from: shared)
        }
        supervision = nil
        manager?.connection.stopVPNTunnel()
    }

    // MARK: - supervision

    /// Restart the extension when it is running and not being serviced.
    ///
    /// THE FAULT THIS IS FOR, seen live 2026-08-22: a companion leg sitting in
    /// the router's list as degraded at 65% loss with `rtt_ms: null` - never
    /// one completed round trip, on wifi at -35 dBm. The extension was up, the
    /// socket was bound, the lease was renewing, and nothing was servicing a
    /// packet. The router dialled a leg that was announced, in the bond, and
    /// deaf.
    ///
    /// THE APP'S LEVER IS THE STRONGER ONE AND THE RARER ONE. `stopVPNTunnel()`
    /// followed by `startVPNTunnel()` genuinely tears the extension process
    /// down and builds a new one - there is no equivalent of Android's trap,
    /// where `startForegroundService` on a live service only delivers another
    /// `onStartCommand` and supervision confirms the broken state forever. But
    /// it only runs while somebody has this app open, and the phone this exists
    /// for is in a car. The unattended half is `PacketTunnelProvider`'s own
    /// check, which cancels the tunnel from inside and lets the on-demand rule
    /// reconnect it. Neither can reach an extension that is alive and not being
    /// scheduled with the app closed; see `RelaySupervision` for why nothing on
    /// iOS can.
    ///
    /// Every threshold, the cooldown and the reasons are `RelaySupervision` in
    /// the Kit, under test. Nothing is decided here.
    func supervise(now: Date = Date()) async {
        guard let manager, status == .connected else {
            supervision = nil
            return
        }
        guard !restarting else { return }

        let shared = RelayConfiguration.sharedDefaults
        let verdict = RelaySupervision.evaluate(
            run: .running,
            report: report,
            // `NEVPNConnection.connectedDate` is when this connection last
            // reached .connected, and it is the only anchor the app has: the
            // tunnel may have been started by an on-demand rule hours before
            // anybody opened this screen, so "when did I press the button" says
            // nothing. RelaySupervision refuses to judge silence without one
            // rather than guessing, and that refusal is why this is passed
            // through instead of defaulted.
            runningSince: manager.connection.connectedDate,
            now: now)
        let remedy = verdict.remedy(
            for: .app,
            // Consulted only for the extension, whose lever depends on it - the
            // app starts the tunnel itself. Passed truthfully anyway rather
            // than hardcoded, so this call site cannot be read as claiming
            // something about the profile that is not so.
            onDemandArmed: manager.isOnDemandEnabled,
            // ONE COOLDOWN, SHARED BY BOTH SUPERVISORS. Two processes deciding
            // independently to restart the same tunnel is worse than one, and
            // the marker outlives the restart it caused precisely so the second
            // one can see it.
            lastRemedyAt: shared.flatMap { RelaySupervisionStore.lastRemedy(from: $0) },
            now: now)
        supervision = SupervisionPass(verdict: verdict, remedy: remedy)

        guard case let .restartTunnel(why) = remedy else { return }
        restarting = true
        defer { restarting = false }
        // Recorded BEFORE the restart. The app survives its own remedy, unlike
        // the extension, but being killed mid-restart is ordinary on iOS and a
        // marker written afterwards would simply not exist.
        if let shared { RelaySupervisionStore.recordRemedy(at: now, to: shared) }
        lastError = why

        manager.connection.stopVPNTunnel()
        // TRAP 7, and the same shape as trap 2 above. `stopVPNTunnel()` returns
        // immediately while the teardown happens in the system daemon, so a
        // start issued straight after is accepted against a connection that is
        // still up and quietly does nothing - which would leave the wedged
        // extension exactly where it was, having spent the cooldown.
        await waitForDisconnect(manager.connection)
        do {
            try manager.connection.startVPNTunnel()
        } catch {
            lastError = Self.describe(error)
        }
    }

    /// Wait for the connection to actually go down, with a bound.
    ///
    /// POLLED RATHER THAN DRIVEN OFF `.NEVPNStatusDidChange`, because this has
    /// to be able to give up. A stop that never completes must not leave
    /// `restarting` pinned true forever - that would disable every future
    /// supervision of this tunnel silently, which is precisely the failure mode
    /// this whole file is being extended to remove. Timing out and letting the
    /// start fail loudly is the better half of a bad choice.
    ///
    /// `.invalid` ends the wait too: it means the configuration went away
    /// underneath us, and waiting for a disconnect that will never be reported
    /// is the same trap in a different costume.
    private func waitForDisconnect(_ connection: NEVPNConnection,
                                   timeout: TimeInterval = 10) async {
        let deadline = Date().addingTimeInterval(timeout)
        while connection.status != .disconnected,
              connection.status != .invalid,
              Date() < deadline {
            try? await Task.sleep(nanoseconds: 200_000_000)
        }
    }

    /// Fill in what the extension needs to ANNOUNCE ITSELF as a leg (#2252).
    ///
    /// Done here rather than in the screen that builds the configuration
    /// because this is the one choke point every start goes through - a second
    /// caller that forgot these fields would produce a relay that works
    /// perfectly and is invisible to the router, which is indistinguishable
    /// from a broken one when you are looking at the leg list.
    ///
    /// A missing token is NOT an error. The phone can still relay, and its leg
    /// can still exist as a static entry in the router's config; announcing is
    /// the thing that makes the static entry unnecessary. `announceConfig`
    /// returns nil and the extension simply does not announce.
    private func announceSettings(into config: inout RelayConfiguration) {
        config.consoleHost = Settings.consoleLANHost
        config.announceToken = ConsoleWriteToken.shared.read() ?? ""

        // SINCE iOS 16 THIS IS THE MODEL, NOT THE OWNER'S NAME. Apple made
        // `UIDevice.current.name` return "iPhone" for apps without the
        // user-assigned-device-name entitlement, so both phones in this house
        // report the same string. The name stays unique anyway because
        // `LegName` appends persisted random hex - which is the whole reason
        // it does, rather than trusting a device name to be distinctive.
        let device = UIDevice.current.name
        if let shared = RelayConfiguration.sharedDefaults {
            config.legName = LegName.resolve(in: shared, deviceName: device)
        }
        // The label is what a person reads in the leg list, and it is the one
        // the router lets them overwrite from the app. Sending it on every
        // announce would stamp on a rename, so it is sent only to seed a leg
        // that has never had one.
        config.legLabel = device
    }

    /// Removes the VPN configuration entirely, so the entry disappears from the
    /// user's Settings. Worth having: an app that can install a system-level
    /// VPN profile and cannot remove it is a bad citizen.
    func removeConfiguration() async {
        guard let m = manager else { return }
        do {
            try await m.removeFromPreferences()
            manager = nil
            installed = false
            status = .invalid
            report = nil
            supervision = nil
            if let shared = RelayConfiguration.sharedDefaults {
                RelayStatusStore.clear(from: shared)
                // Same reason the report goes: a cooldown that outlives the
                // configuration it belonged to would suppress the first
                // supervision of whatever is installed next.
                RelaySupervisionStore.clear(from: shared)
            }
        } catch {
            lastError = Self.describe(error)
        }
    }

    // MARK: - status

    private func observeStatus() {
        if let observer { NotificationCenter.default.removeObserver(observer) }
        guard let connection = manager?.connection else { return }
        observer = NotificationCenter.default.addObserver(
            forName: .NEVPNStatusDidChange, object: connection, queue: .main
        ) { [weak self] _ in
            // MainActor hop is explicit: the notification is delivered on the
            // main queue but the closure is not main-actor isolated, and
            // publishing from off the actor is a SwiftUI runtime warning at
            // best and a race at worst.
            //
            // The connection is re-read from `self` rather than captured.
            // NEVPNConnection is not Sendable, so capturing it here is a
            // Swift 6 error waiting to happen, and `self` is main-actor
            // isolated so reading it on the hop is free.
            Task { @MainActor in
                guard let self else { return }
                self.status = self.manager?.connection.status ?? .invalid
                await self.refreshReport()
            }
        }
    }

    /// Pulls the extension's latest report.
    ///
    /// TWO SOURCES, AND THEY ANSWER DIFFERENT QUESTIONS
    ///
    /// The app group mailbox is the base case: it is readable whether or not
    /// the extension is alive, which is the whole point, since "the extension
    /// died" is the failure we most need to display. It is a poll because
    /// nothing notifies one process that another wrote UserDefaults.
    ///
    /// `sendProviderMessage` is then tried on top. A reply is direct PROOF the
    /// extension process is alive and servicing requests - stronger evidence
    /// than a fresh timestamp, which a process can leave behind moments before
    /// it is killed. No reply is not treated as failure: the tunnel may simply
    /// be between states, and the staleness check on the mailbox is the
    /// authority on whether the extension has actually gone.
    func refreshReport() async {
        if let shared = RelayConfiguration.sharedDefaults {
            report = RelayStatusStore.read(from: shared)
        }
        guard status == .connected,
              let session = manager?.connection as? NETunnelProviderSession else { return }
        guard let reply = try? await sendProviderMessage(session),
              let live = try? JSONDecoder().decode(RelayStatus.self, from: reply) else { return }
        report = live
    }

    private func sendProviderMessage(_ session: NETunnelProviderSession) async throws -> Data? {
        try await withCheckedThrowingContinuation { cont in
            do {
                // The payload is unused - the provider answers any message with
                // its current counters. A request body would be a protocol to
                // keep in sync across two signed binaries for no gain.
                try session.sendProviderMessage(Data()) { cont.resume(returning: $0) }
            } catch {
                cont.resume(throwing: error)
            }
        }
    }

    // `reportIsStale` lived here until #44 and is deliberately gone. Staleness
    // is RelayStatus's rule and RelayVerdict applies it; a second copy in the
    // app was one of the two places the screen decided things for itself, and
    // the one that survives has tests.

    static func statusText(_ s: NEVPNStatus) -> String {
        switch s {
        case .invalid: return "not installed"
        case .disconnected: return "stopped"
        case .connecting: return "starting"
        case .connected: return "running"
        case .reasserting: return "reconnecting"
        case .disconnecting: return "stopping"
        @unknown default: return "unknown"
        }
    }

    /// NEVPNError codes are integers with no useful `localizedDescription`.
    /// Translating the two that actually happen saves the next person a
    /// search for "NEVPNErrorDomain error 1".
    private static func describe(_ error: Error) -> String {
        let ns = error as NSError
        guard ns.domain == NEVPNErrorDomain else { return error.localizedDescription }
        switch NEVPNError.Code(rawValue: ns.code) {
        case .configurationInvalid:
            return "VPN configuration invalid - is the tunnel extension embedded and its bundle id correct?"
        case .configurationDisabled:
            return "VPN configuration disabled in Settings"
        case .configurationReadWriteFailed:
            return "could not save the VPN configuration - permission was likely denied"
        case .configurationStale:
            return "VPN configuration stale - reload before starting"
        case .connectionFailed:
            return "tunnel failed to start - check the extension's log"
        default:
            return "VPN error \(ns.code)"
        }
    }
}
