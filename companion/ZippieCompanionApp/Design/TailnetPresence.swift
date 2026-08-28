import Foundation
import ZippieCompanionKit

/// Whether THIS phone runs Tailscale, as opposed to merely being on a network
/// that can reach the tailnet.
///
/// The difference is the whole point of the diagnostics screen. A phone with
/// its own Tailscale keeps its MDM on hotel wifi and on cellular; a phone
/// borrowing a router's forwarding loses it the moment it changes SSID - which
/// is exactly what happened when the managed Pixel walked from the travel
/// router onto the house VLAN and went dark for half an hour.
///
/// DETECTED BY ADDRESS, NOT BY ASKING. There is no API to ask "is Tailscale
/// running", and a URL probe cannot tell the two cases apart because both
/// answer. What separates them is where the address lives: Tailscale hands this
/// device an address in 100.64.0.0/10 on a tunnel interface, and forwarding by
/// somebody else's router gives this device nothing at all.
enum TailnetPresence {

    /// This device's tailnet address, if it has one.
    ///
    /// SCANS utun* ON PURPOSE, which `LocalAddress` deliberately excludes. That
    /// type is looking for the router's LAN and a tunnel is never it; this one
    /// is looking for precisely a tunnel.
    ///
    /// Zippie's own packet tunnel is also a utun, and cannot be mistaken for
    /// this one: it addresses itself in TEST-NET-1 (192.0.2.2, see
    /// PacketTunnelProvider), which is not in 100.64.0.0/10. The range check is
    /// what keeps the two apart, so it is not incidental.
    static func address() -> String? {
        var head: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&head) == 0, let first = head else { return nil }
        defer { freeifaddrs(head) }

        for ptr in sequence(first: first, next: { $0.pointee.ifa_next }) {
            let flags = Int32(ptr.pointee.ifa_flags)
            guard flags & IFF_UP != 0, flags & IFF_LOOPBACK == 0 else { continue }
            guard let addr = ptr.pointee.ifa_addr,
                  addr.pointee.sa_family == UInt8(AF_INET) else { continue }
            let name = String(cString: ptr.pointee.ifa_name)
            guard name.hasPrefix("utun") else { continue }

            var host = [CChar](repeating: 0, count: Int(NI_MAXHOST))
            let rc = getnameinfo(addr, socklen_t(addr.pointee.sa_len),
                                &host, socklen_t(host.count),
                                nil, 0, NI_NUMERICHOST)
            guard rc == 0 else { continue }
            let text = String(cString: host)
            if TailnetAddress.isTailnetV4(text) { return text }
        }
        return nil
    }

    static var isRunning: Bool { address() != nil }
}
