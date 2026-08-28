import Foundation

#if canImport(Darwin)
import Darwin
#endif

/// Which radio a bonded leg rides.
///
/// The bond has exactly two roles today, and they are not interchangeable:
/// one is the local link the phone is already using, the other is metered and
/// exists so the first one can die without taking the connection with it.
public enum InterfaceRole: String, Sendable, Equatable, CaseIterable {
    /// The local link. Wifi on every iPhone shipped so far, and ALSO a USB-C
    /// ethernet adapter, which iOS numbers `en2` or `en3`. Both are the same
    /// thing from the bond's point of view - an unmetered link the phone is
    /// already on - and the ethernet case is not hypothetical: ADR 0022's
    /// answer to the CarPlay phone (which cannot join the router's wifi
    /// because CarPlay owns the radio) is exactly that adapter.
    case wifi
    /// The carrier. `pdp_ip0` through `pdp_ipN`.
    case cellular
}

/// One interface as the OS currently reports it.
///
/// A value type rather than a live query so the classification rules - which
/// are the part that can be wrong - are testable on a machine with no radios.
public struct InterfaceSnapshot: Sendable, Equatable {
    public let name: String
    public let isUp: Bool
    /// Whether the interface has an IPv4 address right now.
    ///
    /// LOAD-BEARING, not decoration. The datapath binds `udp4`, so an
    /// interface that is UP with no IPv4 address produces a socket that
    /// cannot send anything - which from the app looks exactly like a leg
    /// that is up and carrying nothing.
    public let hasIPv4: Bool

    public init(name: String, isUp: Bool, hasIPv4: Bool) {
        self.name = name
        self.isUp = isUp
        self.hasIPv4 = hasIPv4
    }

    /// True when this interface could actually carry a pinned leg.
    public var isUsable: Bool { isUp && hasIPv4 && InterfaceRole.of(name) != nil }
}

extension InterfaceRole {

    /// Classify an interface name, or nil when it must never carry a leg.
    ///
    /// ALLOWLIST, DELIBERATELY. Everything not named here returns nil, which
    /// is the only safe default: iOS is full of interfaces that are up, have
    /// addresses, and route nothing useful. Naming them to exclude them would
    /// mean a future iOS release inventing one more and this function
    /// cheerfully pinning a leg to it.
    ///
    /// What the allowlist keeps out, and why each one would hurt:
    ///
    ///   - `utun*` - tunnels, INCLUDING the one this extension is running
    ///     inside. Pinning a leg to the tunnel that carries it is a loop that
    ///     presents as total silence, not as an error.
    ///   - `awdl0`, `llw0` - Apple Wireless Direct Link and low-latency WLAN.
    ///     Up whenever AirDrop or AirPlay is active, carrying no internet
    ///     route.
    ///   - `ap1`, `bridge*` - the personal-hotspot AP and its bridge. A leg
    ///     pinned there sends the phone's own packets at the phone's own
    ///     tethered clients.
    ///   - `lo0` - loopback, which would look like a perfectly healthy leg
    ///     that delivers nothing off the device.
    ///   - `ipsec*`, `anpi*` - other people's tunnels, and Apple-internal
    ///     interfaces that are not routable.
    public static func of(_ name: String) -> InterfaceRole? {
        if name.hasPrefix("pdp_ip") { return .cellular }
        if name.hasPrefix("en") { return .wifi }
        return nil
    }
}

/// The device name to pin each role to, right now.
///
/// Nil for a role means there is no usable interface for it - which is a fact
/// worth carrying rather than an error, because a phone in a basement with no
/// signal genuinely has one leg and the honest answer is one leg.
public struct ResolvedInterfaces: Sendable, Equatable {
    public let wifi: String?
    public let cellular: String?

    public init(wifi: String? = nil, cellular: String? = nil) {
        self.wifi = wifi
        self.cellular = cellular
    }

    public func device(for role: InterfaceRole) -> String? {
        switch role {
        case .wifi: return wifi
        case .cellular: return cellular
        }
    }

    /// Every device that resolved, in a stable order. Stable because leg
    /// ordering feeds path ids and chart colours, and a set that reorders
    /// between starts would make one leg look like two.
    public var devices: [String] { [wifi, cellular].compactMap { $0 } }

    /// Resolve the roles from what the OS reports.
    ///
    /// The tie-break when several interfaces share a role is LOWEST NAME,
    /// ordered as text. That picks `en0` over `en2` (wifi over a USB-C
    /// ethernet adapter) and `pdp_ip0` over `pdp_ip1`. It is a tie-break, not
    /// a preference with a reason: bonding more than one local link is a
    /// three-leg bond and is out of scope. What matters is that it is
    /// DETERMINISTIC - a resolver that returned a different device on each
    /// start would make every restart look like a topology change.
    public static func resolve(_ interfaces: [InterfaceSnapshot]) -> ResolvedInterfaces {
        var byRole: [InterfaceRole: String] = [:]
        for snapshot in interfaces.sorted(by: { $0.name < $1.name }) {
            guard snapshot.isUp, snapshot.hasIPv4,
                  let role = InterfaceRole.of(snapshot.name),
                  byRole[role] == nil else { continue }
            byRole[role] = snapshot.name
        }
        return ResolvedInterfaces(wifi: byRole[.wifi], cellular: byRole[.cellular])
    }
}

/// Whether what came up is a bond, one leg, or nothing.
///
/// THIS TYPE EXISTS BECAUSE THE ZERO CASE WAS SILENT. `ClientTunnel` logged
/// each refused leg and started anyway, so a phone whose configured device
/// names no longer matched reality came up with a VPN badge, a live tunnel,
/// and no path to home at all. Every packet the OS handed it went into a
/// socket that could not send. From the phone that is indistinguishable from a
/// dead network.
public enum LegAdmission: Sendable, Equatable {
    /// Two or more legs. The only case that is actually a bond.
    case bonded(devices: [String])
    /// One leg. Allowed - a phone with no signal still has wifi - but it must
    /// never be described as a bond, because "bonded, quietly on one leg" is
    /// this project's oldest failure.
    case singleLeg(device: String)
    /// Nothing came up. Not startable.
    case none

    public static func admit(_ devices: [String]) -> LegAdmission {
        // De-duplicated because two sockets on one radio is one path wearing
        // two names, and counting it as two would report a bond that does not
        // exist.
        var seen: [String] = []
        for device in devices where !device.isEmpty && !seen.contains(device) {
            seen.append(device)
        }
        switch seen.count {
        case 0: return .none
        case 1: return .singleLeg(device: seen[0])
        default: return .bonded(devices: seen)
        }
    }

    /// False only for `.none`. A start that cannot carry must fail loudly
    /// rather than come up green.
    public var isStartable: Bool { self != .none }

    /// True only when more than one leg is really carrying. The UI must ask
    /// this rather than counting configured links.
    public var isBonded: Bool {
        if case .bonded = self { return true }
        return false
    }

    /// Said plainly, because this line ends up in a log that somebody reads at
    /// the side of a road.
    public var summary: String {
        switch self {
        case let .bonded(devices):
            return "bonded over \(devices.joined(separator: " + "))"
        case let .singleLeg(device):
            return "one leg only (\(device)) - this is not a bond"
        case .none:
            return "no legs came up"
        }
    }
}

#if canImport(Darwin)
/// Reads the interface list from the running OS.
///
/// Separate from the rules above on purpose: this part cannot be unit tested
/// honestly (it reports whatever the machine running the test happens to
/// have), and the part that decides which name means what can.
///
/// Related to but NOT the same as the tunnel target's `LocalAddress`, which
/// answers "what is this phone's address on the router's LAN". This answers
/// "which devices exist and what are they for".
public enum LiveInterfaces {

    public static func snapshot() -> [InterfaceSnapshot] {
        var head: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&head) == 0, let first = head else { return [] }
        defer { freeifaddrs(head) }

        // Merged by name: getifaddrs reports one entry per address family, so
        // an interface with IPv4 and IPv6 appears twice. Taking the first hit
        // would report an IPv6-only view of a dual-stack interface as having
        // no IPv4 and drop a perfectly good leg.
        var order: [String] = []
        var byName: [String: InterfaceSnapshot] = [:]

        for pointer in sequence(first: first, next: { $0.pointee.ifa_next }) {
            let name = String(cString: pointer.pointee.ifa_name)
            let flags = Int32(pointer.pointee.ifa_flags)
            let isUp = flags & IFF_UP != 0 && flags & IFF_LOOPBACK == 0
            let hasIPv4 = pointer.pointee.ifa_addr?.pointee.sa_family == UInt8(AF_INET)

            if let existing = byName[name] {
                byName[name] = InterfaceSnapshot(name: name,
                                                 isUp: existing.isUp || isUp,
                                                 hasIPv4: existing.hasIPv4 || hasIPv4)
            } else {
                order.append(name)
                byName[name] = InterfaceSnapshot(name: name, isUp: isUp, hasIPv4: hasIPv4)
            }
        }
        return order.compactMap { byName[$0] }
    }

    public static func resolved() -> ResolvedInterfaces {
        ResolvedInterfaces.resolve(snapshot())
    }
}
#endif
