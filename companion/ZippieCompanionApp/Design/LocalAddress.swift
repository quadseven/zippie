import Foundation

#if canImport(Darwin)
import Darwin
#endif

/// This phone's own address on the wifi it is joined to.
///
/// WHY THIS EXISTS. The router's console lists every leg, including two named
/// "companion-...". Deciding WHICH of them is this phone cannot be a guess -
/// labelling Co-operator's leg as yours is exactly the kind of confident wrong answer
/// this product forbids, and it would be invisible because both rows look
/// plausible.
///
/// The router's config gives each companion leg a `relay_endpoint`: the
/// address:port it dials to reach that phone. So the phone can identify itself
/// by MATCHING that endpoint against its own wifi address and listen port. That
/// is evidence, not inference: if it matches, the router is literally dialling
/// this device.
///
/// The comparison itself lives in the Kit as LegIdentity, where it has a
/// test bundle; this file only supplies the address to compare against.
///
/// The proper answer eventually comes from the pairing ceremony (#2251), where
/// each phone holds a client id the router already knows. Until then this is
/// the honest fallback, and it fails CLOSED - no match means no leg is marked,
/// rather than marking the first one that looks about right.
enum LocalAddress {

    /// This phone's IPv4 on whichever LOCAL-NETWORK interface it is using -
    /// wifi, or a USB-C ethernet adapter.
    ///
    /// NO LONGER PINNED TO en0. That was right while the only way onto the
    /// router's network was wifi, and wrong the moment a USB-C ethernet adapter
    /// is plugged in: iOS numbers that interface en2 or en3, so matching only
    /// en0 would return nil, no companion leg would ever be marked "this
    /// phone", and the failure would look like the feature simply not working
    /// rather than like a missing interface name.
    ///
    /// WHAT IS STILL EXCLUDED, and why an "any interface" scan is wrong:
    ///   - pdp_ip0 is CELLULAR. A leg reached over the carrier is not the
    ///     router's LAN, and matching it would claim proximity we do not have.
    ///   - utun* are TUNNELS, and this phone runs a packet tunnel of its own.
    ///     Scanning everything would happily return the tunnel's own
    ///     TEST-NET-1 address and match nothing, forever.
    ///   - Anything without a private address. The router's LAN is RFC1918;
    ///     a public address on an en* interface is not it.
    static func localIPv4() -> String? {
        var head: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&head) == 0, let first = head else { return nil }
        defer { freeifaddrs(head) }

        for ptr in sequence(first: first, next: { $0.pointee.ifa_next }) {
            let flags = Int32(ptr.pointee.ifa_flags)
            guard flags & IFF_UP != 0, flags & IFF_LOOPBACK == 0 else { continue }
            guard let addr = ptr.pointee.ifa_addr,
                  addr.pointee.sa_family == UInt8(AF_INET) else { continue }
            let name = String(cString: ptr.pointee.ifa_name)
            // en* covers wifi (en0) and USB-C ethernet (en2, en3, ...). It
            // deliberately does NOT cover pdp_ip* or utun*.
            guard name.hasPrefix("en") else { continue }

            var host = [CChar](repeating: 0, count: Int(NI_MAXHOST))
            let rc = getnameinfo(addr, socklen_t(addr.pointee.sa_len),
                                &host, socklen_t(host.count),
                                nil, 0, NI_NUMERICHOST)
            guard rc == 0 else { continue }
            let text = String(cString: host)
            guard !text.isEmpty, isPrivateV4(text) else { continue }
            return text
        }
        return nil
    }

    /// RFC1918 only. The router's LAN is private by definition, and a public
    /// address on an en* interface belongs to some other network entirely.
    static func isPrivateV4(_ ip: String) -> Bool {
        let parts = ip.split(separator: ".").compactMap { UInt8($0) }
        guard parts.count == 4 else { return false }
        switch (parts[0], parts[1]) {
        case (10, _):                     return true
        case (192, 168):                  return true
        case (172, 16...31):              return true
        // Link-local: a DHCP failure, not a network. Excluded because a phone
        // that self-assigned 169.254.x.x is not on the router's LAN even
        // though the cable is plugged in.
        default:                          return false
        }
    }
}
