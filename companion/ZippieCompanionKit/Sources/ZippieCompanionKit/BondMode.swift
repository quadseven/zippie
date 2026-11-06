import Foundation

/// Which job this phone is doing right now.
///
/// ONE APP, TWO DIRECTIONS (ADR 0022), and the phone chooses rather than the
/// person:
///
///   contribute - on the router's own network, LEND this phone's cellular to
///                the bond. The phone's own traffic is untouched.
///   client     - anywhere else, BOND this phone's wifi and cellular back to
///                home. The phone's own traffic goes through it.
///
/// These are close to opposites, and running the wrong one is not a cosmetic
/// error: contributing while away from the router holds a cellular socket open
/// for a bond that cannot hear it, and client mode on the router's own wifi
/// would bond a link that is already part of the bond - traffic looping through
/// the router it came from.
public enum BondMode: String, Sendable, Equatable {
    case contribute
    case client
}

/// How the router was found, which is what decides the mode.
///
/// THE DISTINCTION THAT MATTERS: reachable and NEARBY are different facts. The
/// console answers over the tailnet from anywhere in the world, so "the router
/// replied" proves the router is alive and proves nothing about where this
/// phone is. Only an answer on the LOCAL address means we are on its network.
///
/// Getting this backwards would put the phone in contribute mode in a hotel in
/// another state, which is the exact failure the mode split exists to prevent.
public enum RouterProximity: Sendable, Equatable {
    /// Answered on its LAN address. We are on the router's network.
    case local
    /// Answered only over the tailnet. Alive, but we are somewhere else.
    case remote
    /// Did not answer at all. Either off-network with no tailnet route, or the
    /// router is down. `UnreachableReason` says which, because the phone CAN
    /// tell those apart by looking at its own addresses (#154).
    case unreachable
}

/// Why the router did not answer.
///
/// OFF THE ROUTER'S LAN THE ONLY PATH IS THE TAILNET. The tailnet name resolves
/// publicly to a CGNAT tailnet address that only Tailscale can route, so a
/// phone without Tailscale gets no DNS error and no refusal, just a timeout
/// after the full budget. That timeout looked exactly like a router that is
/// down, and the two have different fixes (#154).
public enum UnreachableReason: String, Sendable, Equatable {
    /// A tailnet console is configured and this phone holds no tailnet address.
    case noTailnetRoute
    /// The phone had a route, or no tailnet console was configured, and still
    /// nothing answered.
    case notAnswering

    /// The name logs and RUM carry, so the cause is readable without the phone.
    public var logName: String {
        switch self {
        case .noTailnetRoute: return "no_tailnet_route"
        case .notAnswering: return "console_timeout"
        }
    }

    /// Decide from what the phone can see of itself.
    ///
    /// `tailnetAddress` is this device's own tailnet address, if any. It is
    /// range-checked here rather than trusted, so a carrier CGNAT address just
    /// outside the tailnet range is never read as a route. With no tailnet console
    /// configured the tailnet is irrelevant, and blaming it would send the
    /// reader to the wrong fix.
    public static func decide(hasRemoteCandidate: Bool, tailnetAddress: String?) -> UnreachableReason {
        guard hasRemoteCandidate else { return .notAnswering }
        guard let addr = tailnetAddress, TailnetAddress.isTailnetV4(addr) else {
            return .noTailnetRoute
        }
        return .notAnswering
    }
}

/// The mode decision, with the reason attached.
///
/// The reason travels WITH the verdict rather than being reconstructed by the
/// UI. "Why is this phone in client mode" is the hardest question this app has
/// to answer, and an enum on its own cannot answer it.
public struct ModeDecision: Sendable, Equatable {
    public let mode: BondMode
    public let proximity: RouterProximity
    /// True while the phone has not yet looked. Distinct from "looked and found
    /// nothing", because showing a confident mode before the first probe is a
    /// claim made without evidence.
    public let undetermined: Bool
    /// Why the router did not answer. Nil unless `proximity` is `.unreachable`
    /// and the phone has looked, so a reached router never carries a cause.
    public let unreachableReason: UnreachableReason?

    /// `unreachableReason` defaults to `.notAnswering`, the meaning this case
    /// had before #154, so a caller that has not checked the tailnet never
    /// claims the phone is off it.
    public init(proximity: RouterProximity, undetermined: Bool = false,
                unreachableReason: UnreachableReason = .notAnswering) {
        self.proximity = proximity
        self.undetermined = undetermined
        self.unreachableReason = (proximity == .unreachable && !undetermined)
            ? unreachableReason : nil
        // CLIENT IS THE DEFAULT, and that asymmetry is deliberate. Contributing
        // requires positive proof that the router is on this network; being
        // wrong there wastes metered data on a bond that cannot hear us. Being
        // wrong in the other direction just means the phone bonds its own links
        // home, which is useful nearly everywhere.
        mode = proximity == .local ? .contribute : .client
    }

    /// The mode sentence, naming which router when unreachable and a name is
    /// known.
    ///
    /// Only the `.unreachable` case says "the router" at all - `.local` and
    /// `.remote` already describe a relationship this phone has just proven
    /// by reaching (or failing to reach) an address, which is a different
    /// kind of claim than naming an unreached device. "The router is not
    /// answering" sitting next to a working wifi router the phone IS joined
    /// to reads as a claim about that router (#44 operator follow-up,
    /// 2026-08-08: "'the travel router is not answering' tells the reader which box to go
    /// and look at, where 'the router is not answering' while sitting next to
    /// a working wifi router is actively confusing"). Same reasoning and the
    /// same `routerSubject` helper as `RelayVerdict.detail(router:)`.
    ///
    /// Defaulted to nil for the same reason as `RelayVerdict.detail(router:)`
    /// - most of this type's tests do not care about naming at all.
    public func summary(router: String? = nil) -> String {
        if undetermined { return "Working out which network this is." }
        switch proximity {
        case .local:
            return "On the router's network - lending this phone's cellular to the bond."
        case .remote:
            // DESCRIBES WHERE WE ARE, NOT WHAT WE ARE DOING. This used to say
            // "bonding this phone's wifi and cellular back home", which is the
            // INTENT of client mode and not a thing that happens: the client
            // datapath is not deployed, so the phone carries none of its own
            // traffic. Stating an unbuilt capability as current behaviour is
            // the fabrication this product forbids, and it appeared directly
            // above a list of the router's legs, which made it read as an
            // explanation of them.
            return "Away from the router. Showing the bond as the router sees it; "
                 + "this phone is not part of it from here."
        case .unreachable:
            if unreachableReason == .noTailnetRoute {
                return "This phone is not on the tailnet, so the router cannot be reached from here."
            }
            return "\(RelayVerdict.routerSubject(router)) is not answering, so "
                 + "there is nothing to show about the bond."
        }
    }

    /// Short enough for a headline.
    public var label: String {
        if undetermined { return "Checking" }
        return mode == .contribute ? "Contributing" : "Client"
    }
}
