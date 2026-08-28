import Foundation

/// Detects iCloud Private Relay exit nodes.
///
/// WHY THIS EXISTS - a real false positive, caught on the first live run.
///
/// The v1 probe reported PROVEN from these two addresses:
///
///     default path        146.75.245.47
///     pinned to cellular  146.75.245.73
///
/// Both are Private Relay exits - Albany and Liverpool NY - straight out of
/// Apple's published egress list. The probe had measured which exit Apple
/// happened to assign, not which radio the packet left by. It looked like proof
/// and was not.
///
/// The root cause was a design choice: v1 used PLAIN HTTP, reasoning that TLS
/// would add a handshake and a second failure mode. But Private Relay proxies
/// insecure HTTP app traffic *specifically* - HTTPS would have bypassed it.
/// The optimisation removed the one property that kept the measurement honest.
///
/// So v2 does three things: speaks HTTPS, prefers an endpoint we control, and
/// refuses to call anything PROVEN when either address is a known relay exit.
/// A false PROVEN is worse than no result, because it makes us build on a
/// premise that is untrue.
public struct PrivateRelayRanges: Sendable {
    /// A parsed CIDR, stored as a masked integer pair so membership is a
    /// couple of integer ops - the check runs on every verdict.
    struct V4Net: Sendable {
        let base: UInt32
        let mask: UInt32
        func contains(_ ip: UInt32) -> Bool { (ip & mask) == base }
    }

    let nets: [V4Net]

    public init(csv: String) {
        var out: [V4Net] = []
        for line in csv.split(separator: "\n") {
            let cidr = line.split(separator: ",").first.map(String.init) ?? ""
            guard let net = Self.parse(cidr) else { continue }
            out.append(net)
        }
        nets = out
    }

    /// Apple's list is IPv4 and IPv6; only v4 is parsed. An IPv6 exit therefore
    /// reads as "not a relay", which is a deliberate bias toward INCONCLUSIVE
    /// over a wrong PROVEN - see `looksLikeRelay` for how that is handled.
    static func parse(_ cidr: String) -> V4Net? {
        let parts = cidr.split(separator: "/")
        guard parts.count == 2, let bits = UInt32(parts[1]), bits <= 32,
              let base = ipv4(String(parts[0])) else { return nil }
        let mask: UInt32 = bits == 0 ? 0 : ~UInt32(0) << (32 - bits)
        return V4Net(base: base & mask, mask: mask)
    }

    static func ipv4(_ s: String) -> UInt32? {
        let o = s.split(separator: ".")
        guard o.count == 4 else { return nil }
        var v: UInt32 = 0
        for part in o {
            guard let b = UInt32(part), b <= 255 else { return nil }
            v = (v << 8) | b
        }
        return v
    }

    public func contains(_ address: String) -> Bool {
        guard let ip = Self.ipv4(address.trimmed()) else { return false }
        return nets.contains { $0.contains(ip) }
    }

    public var count: Int { nets.count }
}

/// Heuristic used when the published list is unavailable (offline, fetch
/// failed). Two addresses sharing a /24 while claiming to be different
/// carriers is implausible - carriers do not share a /24 with a cafe's wifi.
/// This is the signal that caught the v1 false positive by eye.
public func sharesV24(_ a: String, _ b: String) -> Bool {
    guard let x = PrivateRelayRanges.ipv4(a.trimmed()),
          let y = PrivateRelayRanges.ipv4(b.trimmed()) else { return false }
    return (x & 0xFFFF_FF00) == (y & 0xFFFF_FF00)
}
