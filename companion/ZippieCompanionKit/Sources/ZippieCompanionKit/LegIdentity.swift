import Foundation

/// Deciding whether a companion leg on the router is THIS phone.
///
/// THE WORST BUG THIS APP COULD HAVE lives here. The bond carries two legs
/// labelled "iPhone (Verizon)" and "Co-operator iPhone (Verizon)". Marking the wrong
/// one as yours would tell Co-operator her phone is contributing while displaying
/// somebody else's traffic - wrong, invisible, and reassuring, which is the
/// worst combination available.
///
/// So the rule is: match on evidence or do not match at all. The router
/// publishes the address:port it dials for each companion leg; if that is this
/// phone's own wifi address and listen port, the router is literally sending to
/// this device. Anything short of that is a guess, and this type returns false
/// for all of them.
///
/// Lives in the Kit, not the app, because it is the piece most worth testing
/// and the app target has no test bundle.
public enum LegIdentity {

    /// True only when `endpoint` provably names this phone.
    ///
    /// Both halves must match. Two phones on one wifi differ only by address,
    /// so the host alone is nearly enough - but a stale config naming the right
    /// host and the wrong port describes a leg that can never carry, and
    /// claiming it as yours would hide exactly that fault behind a friendly row.
    public static func identifies(endpoint: String?,
                                  listenPort: UInt16,
                                  localIP: String?) -> Bool {
        guard let endpoint, let localIP,
              !endpoint.isEmpty, !localIP.isEmpty else { return false }

        // rsplit on the LAST colon: an IPv6 literal is full of them, and
        // splitting on the first would produce nonsense rather than no match.
        guard let colon = endpoint.lastIndex(of: ":") else { return false }
        let host = String(endpoint[endpoint.startIndex..<colon])
        let port = String(endpoint[endpoint.index(after: colon)...])

        guard let parsed = UInt16(port), parsed == listenPort else { return false }
        return normalise(host) == normalise(localIP)
    }

    /// IPv6 literals arrive bracketed in an endpoint and bare from the
    /// interface list, and comparing those two forms directly never matches.
    private static func normalise(_ host: String) -> String {
        var h = host.trimmingCharacters(in: .whitespaces)
        if h.hasPrefix("["), h.hasSuffix("]") { h = String(h.dropFirst().dropLast()) }
        // A scope id ("fe80::1%en0") describes how to reach the address, not
        // which address it is.
        if let pct = h.firstIndex(of: "%") { h = String(h[h.startIndex..<pct]) }
        return h.lowercased()
    }
}
