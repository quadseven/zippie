import Foundation

/// What the phone can say about why it is, or is not, reachable.
///
/// WHY THIS TYPE EXISTS (#131). On 2026-08-11 the managed Pixel went silent for
/// half an hour and the app could say nothing useful. Nothing was broken: the
/// phone had moved from the travel router's wifi to the house VLAN to take a
/// 3GB OS update on unmetered bandwidth, which is the correct thing to do. But
/// the MDM lives on the tailnet and the phone has no Tailscale of its own - it
/// had only ever reached the tailnet because the travel router forwards and
/// masquerades for its LAN. On a network that does not do that, the route
/// simply does not exist.
///
/// The screen showed "Standing by". True, useless, and indistinguishable from
/// four other faults seen the same night: a stale write token 401'ing every
/// sixteen seconds in silence, a DHCP server handing out no resolver at all, an
/// `http://` URL that 308-redirected to https with a client that would not
/// follow, and a relay that answered nothing on its own port.
///
/// Every case below is one of those. The rule they share: SAY WHICH.
public enum DiagnosticFailure: Equatable, Sendable {
    /// DHCP gave an address and named no resolver. A real fault on this
    /// estate - nextdns took port 53 and dnsmasq stopped advertising itself,
    /// so every client got an address it could not use.
    case noResolverOffered
    /// A resolver exists and did not answer for this name.
    case nameNotResolved(String)
    case timedOut(seconds: TimeInterval)
    case tls(String)
    /// Reached, answered, and said no. The status is carried because a 401 and
    /// a 404 send you to different places.
    case http(status: Int)
    /// The router refused a write, and said why. Since the agent started
    /// logging refusals, "bad or missing bearer token" is a sentence the phone
    /// can repeat instead of guessing.
    case refused(reason: String)
    /// Resolved fine, nothing to route to. This is what "the tailnet is not
    /// reachable from this network" actually looks like.
    case noRoute

    public var summary: String {
        switch self {
        case .noResolverOffered:      return "this network offered no DNS server"
        case .nameNotResolved(let n): return "could not resolve \(n)"
        case .timedOut(let s):        return "timed out after \(Int(s))s"
        case .tls(let d):             return "TLS failed: \(d)"
        case .http(let s):            return "HTTP \(s)"
        case .refused(let r):         return "refused: \(r)"
        case .noRoute:                return "no route from this network"
        }
    }
}

/// A measured fact, or an honest admission that it was not measured.
///
/// `notChecked` is NOT a failure and must never render as one. A screen that
/// paints unchecked rows red teaches the reader to ignore red; one that paints
/// them green lies. It is a third state because it is a third state.
public enum DiagnosticState: Equatable, Sendable {
    case notChecked
    case ok(detail: String?)
    case failed(DiagnosticFailure)

    public var isOK: Bool { if case .ok = self { return true }; return false }
}

/// How this phone reaches the tailnet, which is not a yes/no question.
///
/// DIRECT and VIA-ROUTER ARE DIFFERENT STATES WITH DIFFERENT FIXES, and
/// collapsing them into one green dot is how the 2026-08-11 blackout stayed
/// confusing. A phone reaching the tailnet through a router that forwards for
/// it is one SSID change away from losing everything; a phone with its own
/// Tailscale is not. The screen has to be able to say which one you have.
public enum TailnetPath: Equatable, Sendable {
    case notChecked
    /// This phone runs Tailscale itself. Works on any network.
    case direct(nodeName: String?)
    /// Some router on this network forwards to the tailnet. Works HERE only.
    case viaRouter(host: String)
    case unreachable(DiagnosticFailure)

    /// Whether losing this network also loses management.
    ///
    /// The honest answer for `.viaRouter` is yes, and it is the single most
    /// useful thing this screen can tell an operator who is about to walk out
    /// of the house.
    public var survivesLeavingThisNetwork: Bool {
        if case .direct = self { return true }
        return false
    }
}

/// What DHCP said about DNS, which has THREE answers and not two.
///
/// `nil` was wrong here and briefly shipped that way. "This network offered no
/// resolver" is a genuine, serious fault - it is what took the house wifi down
/// on 2026-08-11. "This platform will not tell us" is not a fault at all: iOS
/// exposes no public API for the DHCP-supplied resolver. Rendering the second
/// as the first puts a red row on a healthy phone, which is the fastest way to
/// make somebody stop reading the screen.
public enum ResolverFact: Equatable, Sendable {
    /// The platform cannot answer. Not a fault.
    case unknown
    /// Measured, and there was none. A real and serious fault.
    case none
    case address(String)
}

/// The CGNAT range Tailscale allocates from (RFC 6598).
///
/// IN THE KIT, NOT THE APP, because it is a DECISION and not a syscall. The
/// interface scan that finds an address belongs to the platform; deciding
/// whether that address means "this phone is on the tailnet" is the part that
/// can be wrong, so it lives where `swift test` can reach it.
public enum TailnetAddress {
    public static func isTailnetV4(_ ip: String) -> Bool {
        let parts = ip.split(separator: ".").compactMap { UInt8($0) }
        guard parts.count == 4, parts[0] == 100 else { return false }
        // 100.64.0.0/10 is 100.64.x.x through 100.127.x.x. Getting this bound
        // wrong by one octet would classify ordinary public addresses in
        // 100.128/9 as tailnet, and the screen would confidently say a phone
        // is on the tailnet because a hotel handed it 100.130.4.5.
        return parts[1] >= 64 && parts[1] <= 127
    }
}

/// One line on the diagnostics screen.
public struct DiagnosticRow: Equatable, Sendable {
    public enum Tone: Equatable, Sendable { case good, bad, unknown, note }

    public let label: String
    public let value: String
    public let tone: Tone
    /// Present only when there is something to do about it.
    public let hint: String?

    public init(label: String, value: String, tone: Tone, hint: String? = nil) {
        self.label = label
        self.value = value
        self.tone = tone
        self.hint = hint
    }
}

/// Everything measured, and when.
///
/// The timestamps are not decoration. An editor that shows stale values as
/// current is the failure LegEditorModel already refuses to commit (it drops
/// `reported` rather than keep the last good snapshot), and a diagnostics
/// screen has exactly the same duty: a reading nobody has taken since the
/// network changed is worse than no reading, because it is believed.
public struct Diagnostics: Equatable, Sendable {
    public var legName: String?
    public var carrying: Bool
    public var lastAnnounce: DiagnosticState
    public var mdm: DiagnosticState
    public var tailnet: TailnetPath
    public var ssid: String?
    public var dhcpResolver: ResolverFact
    public var captive: DiagnosticState
    public var lastCheckIn: Date?
    public var bytesCarried: Int64?
    public var measuredAt: Date?

    public init(legName: String? = nil,
                carrying: Bool = false,
                lastAnnounce: DiagnosticState = .notChecked,
                mdm: DiagnosticState = .notChecked,
                tailnet: TailnetPath = .notChecked,
                ssid: String? = nil,
                dhcpResolver: ResolverFact = .unknown,
                captive: DiagnosticState = .notChecked,
                lastCheckIn: Date? = nil,
                bytesCarried: Int64? = nil,
                measuredAt: Date? = nil) {
        self.legName = legName
        self.carrying = carrying
        self.lastAnnounce = lastAnnounce
        self.mdm = mdm
        self.tailnet = tailnet
        self.ssid = ssid
        self.dhcpResolver = dhcpResolver
        self.captive = captive
        self.lastCheckIn = lastCheckIn
        self.bytesCarried = bytesCarried
        self.measuredAt = measuredAt
    }

    /// The single sentence at the top. It answers the question the operator
    /// actually has, which is never "what is the state of six subsystems".
    ///
    /// ORDERED BY WHAT BLOCKS WHAT. No resolver means nothing else can work, so
    /// it is reported first even though the MDM row is also red - reporting the
    /// symptom above the cause is how a DNS fault got diagnosed as a wifi fault
    /// for several hours.
    public var headline: String {
        if case .failed(.noResolverOffered) = captive { return "This network has no DNS" }
        if case .failed(.noResolverOffered) = mdm { return "This network has no DNS" }
        if case .unreachable = tailnet { return "Cannot reach the tailnet" }
        if case .failed(let f) = mdm { return "Cannot reach the MDM - \(f.summary)" }
        if case .failed(let f) = lastAnnounce { return "The router refused this phone - \(f.summary)" }
        if carrying { return "Carrying" }
        if case .viaRouter = tailnet { return "Reachable, but only on this network" }
        return "Standing by"
    }

    public func rows(now: Date = Date()) -> [DiagnosticRow] {
        var out: [DiagnosticRow] = []

        out.append(DiagnosticRow(
            label: "Bond",
            value: carrying ? "carrying" : "not carrying",
            tone: carrying ? .good : .unknown,
            hint: legName.map { "known to the router as \($0)" }))

        out.append(Self.row("Last announce", lastAnnounce,
                            okDefault: "accepted",
                            hint: Self.announceHint(lastAnnounce)))

        out.append(Self.row("MDM", mdm, okDefault: "reachable"))

        out.append(tailnetRow())

        out.append(DiagnosticRow(
            label: "Network",
            value: ssid ?? "unknown",
            tone: resolverTone,
            hint: resolverHint))

        out.append(Self.row("Captive check", captive, okDefault: "passes"))

        if let seen = lastCheckIn {
            let age = Int(now.timeIntervalSince(seen))
            out.append(DiagnosticRow(label: "Last check-in",
                                     value: age < 90 ? "just now" : "\(age / 60) min ago",
                                     tone: age < 600 ? .good : .bad,
                                     hint: age >= 600 ? "the server has not heard from this phone" : nil))
        } else {
            out.append(DiagnosticRow(label: "Last check-in", value: "never", tone: .unknown))
        }

        if let b = bytesCarried {
            out.append(DiagnosticRow(label: "Carried this session",
                                     value: Self.humanBytes(b), tone: .note))
        }

        // Last, and always present when known. A screen of measurements with no
        // measurement time is the thing this type exists to prevent.
        if let at = measuredAt {
            let age = Int(now.timeIntervalSince(at))
            out.append(DiagnosticRow(label: "Measured",
                                     value: age < 5 ? "just now" : "\(age)s ago",
                                     tone: age > 60 ? .unknown : .note,
                                     hint: age > 60 ? "tap refresh - these may have moved" : nil))
        }
        return out
    }

    /// A network that hands out no resolver is a fault worth shouting about;
    /// a platform that will not say is worth nothing at all. Two states, two
    /// treatments, and never the same row.
    private var resolverHint: String? {
        switch dhcpResolver {
        case .unknown:            return nil
        case .none:               return "this network offered no DNS server"
        case .address(let a):     return "DNS from DHCP: \(a)"
        }
    }

    private var resolverTone: DiagnosticRow.Tone {
        switch dhcpResolver {
        case .none:    return .bad
        case .address: return .note
        case .unknown: return ssid == nil ? .unknown : .note
        }
    }

    private func tailnetRow() -> DiagnosticRow {
        switch tailnet {
        case .notChecked:
            return DiagnosticRow(label: "Tailnet", value: "not checked", tone: .unknown)
        case .direct(let node):
            return DiagnosticRow(label: "Tailnet", value: "direct",
                                 tone: .good,
                                 hint: node.map { "this phone is \($0)" }
                                     ?? "works on any network")
        case .viaRouter(let host):
            // Deliberately NOT `.good`. It works, and it stops working the
            // moment this phone changes network - which is exactly what
            // happened, and the screen should have been able to warn.
            return DiagnosticRow(label: "Tailnet", value: "via \(host)",
                                 tone: .note,
                                 hint: "only on this network - leaving it loses the MDM")
        case .unreachable(let f):
            return DiagnosticRow(label: "Tailnet", value: f.summary, tone: .bad,
                                 hint: "install Tailscale on this phone to fix it everywhere")
        }
    }

    private static func announceHint(_ s: DiagnosticState) -> String? {
        if case .failed(.refused) = s {
            return "store the router's write token in this app"
        }
        return nil
    }

    private static func row(_ label: String, _ state: DiagnosticState,
                            okDefault: String, hint: String? = nil) -> DiagnosticRow {
        switch state {
        case .notChecked:
            return DiagnosticRow(label: label, value: "not checked", tone: .unknown, hint: hint)
        case .ok(let d):
            return DiagnosticRow(label: label, value: d ?? okDefault, tone: .good, hint: hint)
        case .failed(let f):
            return DiagnosticRow(label: label, value: f.summary, tone: .bad, hint: hint)
        }
    }

    static func humanBytes(_ n: Int64) -> String {
        if n < 1024 { return "\(n) B" }
        let units = ["KB", "MB", "GB", "TB"]
        var v = Double(n) / 1024.0
        var i = 0
        while v >= 1024 && i < units.count - 1 { v /= 1024; i += 1 }
        return String(format: v < 10 ? "%.1f %@" : "%.0f %@", v, units[i])
    }
}
