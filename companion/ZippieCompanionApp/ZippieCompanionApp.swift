import SwiftUI

/// Zippie companion (ADR 0020).
///
/// Three surfaces, in the order they became useful:
///
///   Probe   - does `requiredInterfaceType = .cellular` actually pin a socket
///             to the radio. v1 answered this wrongly (it measured iCloud
///             Private Relay exits and reported PROVEN); v2 speaks HTTPS and
///             refuses to call a relay exit proof.
///   Relay   - the actual feature: forward zippie frames between the router
///             over wifi and the home transport over cellular, making this
///             phone a bonded leg.
///   Bond    - read-only view of the router's own status, so the phone can say
///             what the bond is doing without an SSH session.
@main
struct ZippieCompanionApp: App {
    @StateObject private var bond = BondModel()
    @State private var tab = Tab.initial

    enum Tab: Hashable {
        case status, relay, probe

        /// DEBUG builds accept `-startTab relay` so a screenshot pass can open
        /// a given surface directly. The simulator offers no way to tap, and
        /// clicking at guessed window coordinates is the kind of check that
        /// passes by accident.
        static var initial: Tab {
            #if DEBUG
            switch UserDefaults.standard.string(forKey: "startTab") {
            case "relay": return .relay
            case "probe": return .probe
            default:      return .status
            }
            #else
            return .status
            #endif
        }
    }

    init() { Observability.start() }

    var body: some Scene {
        WindowGroup {
            // Status leads. The two-second question is "is it working, and on
            // what", so the screen that answers it is the one the app opens on -
            // not a probe harness, which is a builder's tool that happened to
            // ship first.
            TabView(selection: $tab) {
                BondScreen(model: bond)
                    .tabItem { Label("Status", systemImage: "point.3.connected.trianglepath.dotted") }
                    .tag(Tab.status)
                // The SAME model the Status tab renders. The mode decision is
                // measured once, in one place, and both the sentence on Status
                // and the profile the Relay tab installs come from it - a
                // second probe here would let the two screens disagree about
                // which network this phone is on (#48).
                RelayScreen(bond: bond)
                    .tabItem { Label("Relay", systemImage: "arrow.left.arrow.right") }
                    .tag(Tab.relay)
                ProbeScreen()
                    .tabItem { Label("Probe", systemImage: "antenna.radiowaves.left.and.right") }
                    .tag(Tab.probe)
            }
            .tint(Ink.live)
            .task { await bond.refresh() }
        }
    }
}
