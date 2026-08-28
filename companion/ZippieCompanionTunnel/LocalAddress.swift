import Foundation

#if canImport(Darwin)
import Darwin
#endif

/// This phone's own address on the network the router is on.
///
/// DUPLICATED FROM THE APP TARGET, deliberately and narrowly. The app's copy
/// lives in ZippieCompanionApp and an extension cannot import an app target.
/// Moving it to the Kit would be the tidier answer and is worth doing when
/// anything else needs it; today two small readers of getifaddrs is less risk
/// than reshaping the shared package while the relay is live.
///
/// en* covers wifi and a USB-C ethernet adapter, which iOS numbers en2 or en3.
/// pdp_ip* is cellular - the router's LAN is not reached over the carrier - and
/// utun* are tunnels, including the one this extension is running inside.
enum LocalAddress {

    static func wifiIPv4() -> String? {
        var head: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&head) == 0, let first = head else { return nil }
        defer { freeifaddrs(head) }

        for ptr in sequence(first: first, next: { $0.pointee.ifa_next }) {
            let flags = Int32(ptr.pointee.ifa_flags)
            guard flags & IFF_UP != 0, flags & IFF_LOOPBACK == 0 else { continue }
            guard let addr = ptr.pointee.ifa_addr,
                  addr.pointee.sa_family == UInt8(AF_INET) else { continue }
            guard String(cString: ptr.pointee.ifa_name).hasPrefix("en") else { continue }

            var host = [CChar](repeating: 0, count: Int(NI_MAXHOST))
            guard getnameinfo(addr, socklen_t(addr.pointee.sa_len),
                              &host, socklen_t(host.count),
                              nil, 0, NI_NUMERICHOST) == 0 else { continue }
            let text = String(cString: host)
            // RFC1918 only. A public address on an en* interface is some other
            // network, and announcing it would have the router dial a stranger.
            guard !text.isEmpty, isPrivateV4(text) else { continue }
            return text
        }
        return nil
    }

    static func isPrivateV4(_ ip: String) -> Bool {
        let parts = ip.split(separator: ".").compactMap { UInt8($0) }
        guard parts.count == 4 else { return false }
        switch (parts[0], parts[1]) {
        case (10, _):        return true
        case (192, 168):     return true
        case (172, 16...31): return true
        default:             return false
        }
    }
}
