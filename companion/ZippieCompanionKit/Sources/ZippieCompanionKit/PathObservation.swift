import Network

/// A testable seam onto `NWPath`, for the client tunnel's Ethernet problem (#64).
///
/// THE PROBLEM THIS OBSERVES, NOT YET FIXES. `ClientTunnel.start()` pins each
/// leg to an interface name once, at startup (`LiveInterfaces.resolved()`).
/// When a USB-C Ethernet adapter for CarPlay is inserted or removed mid-drive,
/// nothing tells the extension its interface list just changed - the pinned
/// sockets carry on regardless, and the reported symptom (relay goes silent,
/// Maps freezes until the cable is pulled) is indistinguishable, from inside
/// the process, between "the far end stopped answering" and "the interface
/// this leg was bound to is no longer the one carrying traffic". Rebuilding
/// the legs is #65's job. This only has to be able to tell those two apart and
/// say so - the acceptance criterion is a distinguishable status, not a fix.
///
/// WHY A SEPARATE VALUE TYPE AND NOT `NWPath` ITSELF
/// `NWPath` and `NWInterface` have no public initialiser - a test cannot build
/// one to represent "Ethernet just appeared". `PathSnapshot.from(_:)` is the
/// ONE place a real `NWPath` is read; everything past that line is a plain
/// value type, so the Ethernet-insertion scenario in the acceptance criteria
/// is just two `PathSnapshot` literals, not a device.
public struct PathInterfaceSnapshot: Sendable, Hashable {
    public let name: String
    public let type: NWInterface.InterfaceType

    public init(name: String, type: NWInterface.InterfaceType) {
        self.name = name
        self.type = type
    }
}

/// What the path looked like at one instant. Deliberately carries no address:
/// an interface's name and type are what "why is this phone quiet" needs, and
/// neither is user traffic or a payload (#64's logging-stays-bounded criterion).
public struct PathSnapshot: Sendable, Equatable {
    public let interfaces: [PathInterfaceSnapshot]
    public let isSatisfied: Bool
    public let supportsIPv4: Bool
    public let supportsIPv6: Bool

    public init(interfaces: [PathInterfaceSnapshot], isSatisfied: Bool,
               supportsIPv4: Bool, supportsIPv6: Bool) {
        self.interfaces = interfaces
        self.isSatisfied = isSatisfied
        self.supportsIPv4 = supportsIPv4
        self.supportsIPv6 = supportsIPv6
    }

    /// The only line in this file that touches a live `NWPath`.
    public static func from(_ path: Network.NWPath) -> PathSnapshot {
        PathSnapshot(
            interfaces: path.availableInterfaces.map {
                PathInterfaceSnapshot(name: $0.name, type: $0.type)
            },
            isSatisfied: path.status == .satisfied,
            supportsIPv4: path.supportsIPv4,
            supportsIPv6: path.supportsIPv6
        )
    }

    public var hasCellular: Bool { interfaces.contains { $0.type == .cellular } }
}

/// One thing that changed between two snapshots.
public enum PathTransition: Sendable, Hashable {
    case interfaceAdded(PathInterfaceSnapshot)
    case interfaceRemoved(PathInterfaceSnapshot)
    case satisfactionChanged(from: Bool, to: Bool)
    case addressFamilySupportChanged(supportsIPv4: Bool, supportsIPv6: Bool)
}

/// Turns two snapshots into what changed, and what changed plus the current
/// `LegAdmission` into one status - reusing that existing three-way split
/// rather than inventing a second vocabulary for "how many legs are carrying".
public enum PathObserver {

    /// Every transition between two snapshots. Empty for the FIRST snapshot
    /// (there is nothing to compare it to - a baseline is not a transition)
    /// and empty when nothing actually changed.
    public static func transitions(from previous: PathSnapshot?,
                                   to current: PathSnapshot) -> [PathTransition] {
        guard let previous else { return [] }
        var out: [PathTransition] = []

        let previousByName = Dictionary(uniqueKeysWithValues: previous.interfaces.map { ($0.name, $0) })
        let currentByName = Dictionary(uniqueKeysWithValues: current.interfaces.map { ($0.name, $0) })

        // Sorted so two runs over the same two snapshots report transitions
        // in the same order - a log line that reordered on every replay would
        // make identical incidents look different from each other.
        for name in currentByName.keys.sorted() where previousByName[name] == nil {
            out.append(.interfaceAdded(currentByName[name]!))
        }
        for name in previousByName.keys.sorted() where currentByName[name] == nil {
            out.append(.interfaceRemoved(previousByName[name]!))
        }
        if previous.isSatisfied != current.isSatisfied {
            out.append(.satisfactionChanged(from: previous.isSatisfied, to: current.isSatisfied))
        }
        if previous.supportsIPv4 != current.supportsIPv4 || previous.supportsIPv6 != current.supportsIPv6 {
            out.append(.addressFamilySupportChanged(supportsIPv4: current.supportsIPv4,
                                                     supportsIPv6: current.supportsIPv6))
        }
        return out
    }

    /// The status model #64 asked for: distinguishes "no cellular radio to
    /// fall back to" from "the interfaces look fine but nothing is carrying"
    /// from "something about the path just moved and legs have not caught up
    /// yet" - three failure shapes that a bare `.waiting` or a generic
    /// "cellular unavailable" message could not tell apart, which is exactly
    /// what turned a wired-Ethernet insertion into a silent relay and four
    /// operator screenshots before anyone knew which one it was.
    public enum Status: Sendable, Equatable {
        case healthy
        case cellularUnavailable
        case endpointUnreachable
        case pathChanged
    }

    public static func status(previous: PathSnapshot?, current: PathSnapshot,
                              legs: LegAdmission) -> Status {
        if !current.hasCellular { return .cellularUnavailable }
        if !transitions(from: previous, to: current).isEmpty { return .pathChanged }
        if legs == .none { return .endpointUnreachable }
        return .healthy
    }
}
