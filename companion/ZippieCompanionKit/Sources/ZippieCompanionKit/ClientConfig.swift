import Foundation
import NetworkExtension

/// Everything client mode needs, in one value.
///
/// Lives in the Kit so it can be TESTED. The extension that consumes it cannot
/// be constructed off-device, and the JSON handed to the Go datapath is exactly
/// the sort of thing that is written once, never verified, and silently wrong -
/// which already happened here: `client_id` and `key_hex` sat in the binding's
/// config struct unread, so every frame the phone sent was unauthenticated.
public struct ClientConfig: Sendable, Equatable {

    /// One route client mode pulls into the tunnel.
    public struct Route: Sendable, Equatable {
        public let address: String
        public let mask: String

        public init(address: String, mask: String) {
            self.address = address
            self.mask = mask
        }

        /// The tailnet. Tailscale hands out 100.64.0.0/10 (CGNAT space), and
        /// this is the route that lets a phone on zippie still reach every
        /// machine on the tailnet - via home, which is already a member.
        public static let tailnet = Route(address: "100.64.0.0", mask: "255.192.0.0")

        /// Everything. A full tunnel, which is a deliberate choice and not a
        /// default: see ClientConfig.routes.
        public static let everything = Route(address: "0.0.0.0", mask: "0.0.0.0")

        public var isDefaultRoute: Bool { address == "0.0.0.0" && mask == "0.0.0.0" }
    }

    /// One bonded leg.
    public struct Link: Sendable, Equatable {
        public let pathID: Int
        public let name: String
        /// The interface to pin the socket to. On iOS `en0` is wifi and
        /// `pdp_ip0` is cellular. WITHOUT THIS THERE IS NO BOND: the kernel
        /// picks a source interface from its own routing table, so every leg
        /// leaves over whichever one currently wins the default route - N
        /// sockets, one path, and a UI confidently reporting two.
        public let device: String
        public let weight: Int

        public init(pathID: Int, name: String, device: String, weight: Int) {
            self.pathID = pathID
            self.name = name
            self.device = device
            self.weight = weight
        }
    }

    public let clientID: UInt32
    /// Hex, because it travels as part of a JSON blob to the Go binding and a
    /// readable single string is easier to verify by eye than base64.
    public let keyHex: String
    public let homeHost: String
    public let homePort: UInt16
    public let tunnelAddress: String
    public let dnsServers: [String]
    public let links: [Link]
    public let mtu: Int
    /// What client mode CAPTURES, in CIDR form.
    ///
    /// SPLIT TUNNEL BY DEFAULT, and that is a decision rather than caution.
    /// iOS runs exactly one packet-tunnel provider at a time, so bringing
    /// zippie up takes Tailscale DOWN - and if zippie then captures
    /// everything, the tailnet becomes unreachable from this phone entirely.
    /// Routing the tailnet range through the bond instead reaches the same
    /// resources by way of home, which is already on the tailnet.
    ///
    /// A full tunnel is still expressible - pass 0.0.0.0/0 - it just is not
    /// the default, because the default should not silently cost the user
    /// their entire private network.
    public let routes: [Route]

    /// 1280 is the IPv6 minimum every path must carry, minus nothing - the
    /// datapath's own header and AEAD overhead come off the OUTER packet, not
    /// this one. Chosen conservatively because too high is not a clean failure:
    /// it presents as loss on the narrowest leg and reads like a flaky carrier.
    public static let defaultMTU = 1280

    public init(clientID: UInt32,
                keyHex: String,
                homeHost: String,
                homePort: UInt16,
                tunnelAddress: String,
                dnsServers: [String] = [],
                links: [Link],
                mtu: Int = ClientConfig.defaultMTU,
                routes: [Route] = [.tailnet]) {
        self.clientID = clientID
        self.keyHex = keyHex
        self.homeHost = homeHost
        self.homePort = homePort
        self.tunnelAddress = tunnelAddress
        self.dnsServers = dnsServers
        self.links = links
        self.mtu = mtu
        self.routes = routes
    }

    public var homeEndpoint: String { "\(homeHost):\(homePort)" }

    /// Rebuild from the tunnel's providerConfiguration.
    ///
    /// CARRIED IN providerConfiguration RATHER THAN THE APP GROUP, for the same
    /// reason the relay's settings are: `UserDefaults(suiteName:)` returns a
    /// working object even without the App Group entitlement and then discards
    /// every write in silence. A signing mistake would leave the extension
    /// reading an empty store and starting an unconfigured client.
    ///
    /// Returns nil rather than a partial config. A client that started with a
    /// missing key would be refused by the datapath anyway - but by then the
    /// tunnel is up and the failure reads as a network fault.
    public init?(providerConfiguration raw: [String: Any]) {
        guard let clientID = (raw["client_id"] as? NSNumber)?.uint32Value,
              let keyHex = raw["key_hex"] as? String,
              let homeHost = raw["home_host"] as? String,
              let homePort = (raw["home_port"] as? NSNumber)?.uint16Value,
              let tunnelAddress = raw["tunnel_address"] as? String else { return nil }

        let links = (raw["links"] as? [[String: Any]] ?? []).compactMap { l -> Link? in
            guard let pathID = (l["path_id"] as? NSNumber)?.intValue,
                  let name = l["name"] as? String,
                  let device = l["device"] as? String, !device.isEmpty,
                  let weight = (l["weight"] as? NSNumber)?.intValue else { return nil }
            return Link(pathID: pathID, name: name, device: device, weight: weight)
        }

        // An EMPTY route list is refused rather than defaulted. A client that
        // captured nothing would come up, show a VPN badge, and carry not one
        // packet - the failure this project keeps rediscovering.
        let rawRoutes = raw["routes"] as? [[String: Any]] ?? []
        let routes = rawRoutes.compactMap { r -> Route? in
            guard let a = r["address"] as? String, let m = r["mask"] as? String,
                  !a.isEmpty, !m.isEmpty else { return nil }
            return Route(address: a, mask: m)
        }
        guard !routes.isEmpty else { return nil }

        self.init(clientID: clientID,
                  keyHex: keyHex,
                  homeHost: homeHost,
                  homePort: homePort,
                  tunnelAddress: tunnelAddress,
                  dnsServers: raw["dns"] as? [String] ?? [],
                  links: links,
                  mtu: (raw["mtu"] as? NSNumber)?.intValue ?? ClientConfig.defaultMTU,
                  routes: routes)

        guard isUsable else { return nil }
    }

    /// The inverse, for the app to write.
    public var providerConfiguration: [String: Any] {
        [
            "client_id": NSNumber(value: clientID),
            "key_hex": keyHex,
            "home_host": homeHost,
            "home_port": NSNumber(value: homePort),
            "tunnel_address": tunnelAddress,
            "dns": dnsServers,
            "mtu": NSNumber(value: mtu),
            "routes": routes.map { ["address": $0.address, "mask": $0.mask] },
            "links": links.map {
                [
                    "path_id": NSNumber(value: $0.pathID),
                    "name": $0.name,
                    "device": $0.device,
                    "weight": NSNumber(value: $0.weight),
                ]
            },
        ]
    }

    /// The datapath must not route its OWN packets into the tunnel it carries.
    ///
    /// Without excluding home, every frame the datapath sends to home is itself
    /// matched by the default route and handed back to the tunnel - a loop that
    /// presents as total silence rather than as an error.
    public var homeExcludedRoute: NEIPv4Route? {
        // Only an IP literal can be excluded; a hostname is resolved after the
        // routing table is installed, which is too late. When home is named
        // rather than numbered the exclusion is skipped, and the tunnel relies
        // on the extension's own socket being outside it - which iOS does
        // provide for a packet-tunnel provider.
        guard isIPv4Literal(homeHost) else { return nil }
        return NEIPv4Route(destinationAddress: homeHost,
                           subnetMask: "255.255.255.255")
    }

    private func isIPv4Literal(_ s: String) -> Bool {
        let parts = s.split(separator: ".")
        guard parts.count == 4 else { return false }
        return parts.allSatisfy { UInt8($0) != nil }
    }

    /// Whether this configuration can actually start.
    ///
    /// A key is REQUIRED. Client mode without one would send the phone's own
    /// packets across hotel wifi unauthenticated and in the clear, and it would
    /// look identical from the UI.
    public var isUsable: Bool {
        clientID != 0
            && keyHex.count >= 32          // 16 bytes, hex
            && keyHex.allSatisfy(\.isHexDigit)
            && !homeHost.isEmpty
            && homePort > 0
            && !tunnelAddress.isEmpty
            && !links.isEmpty
            && !routes.isEmpty
    }

    /// True when this configuration takes over ALL of the phone's traffic.
    /// Worth naming: it is the setting that costs the user their tailnet.
    public var isFullTunnel: Bool { routes.contains(where: \.isDefaultRoute) }

    /// The same configuration with every leg pinned to the interface that
    /// plays its ROLE right now.
    ///
    /// WHY A CONFIGURED DEVICE NAME IS NOT ENOUGH. `Link.device` is a string,
    /// and the Go datapath turns it into a socket by looking the name up with
    /// `net.InterfaceByName` and setting `IP_BOUND_IF` on the result. iOS does
    /// not promise those names. Cellular is `pdp_ip0` through `pdp_ipN`
    /// depending on how many contexts the modem has brought up, and `en0` is
    /// wifi only until a USB-C ethernet adapter appears as `en2`. A name that
    /// was right when it was typed is a leg that silently refuses to attach
    /// later, and the app cannot tell that from bad signal.
    ///
    /// A LINK THAT CANNOT BE PINNED IS DROPPED, never carried through
    /// unpinned. An unpinned socket leaves by whichever interface currently
    /// wins the default route, so two of them are one path wearing two names -
    /// a bond that is not one, reported as if it were. The datapath's own
    /// `dial_other.go` refuses for exactly this reason; this is the same
    /// refusal, made early enough to be visible.
    public func repinned(using resolved: ResolvedInterfaces) -> ClientConfig {
        var pinned: [Link] = []
        var taken: Set<String> = []
        for link in links {
            // The CONFIGURED name is read only for its role. Its exact spelling
            // is treated as stale by construction.
            guard let role = InterfaceRole.of(link.device),
                  let device = resolved.device(for: role),
                  !taken.contains(device) else { continue }
            taken.insert(device)
            pinned.append(Link(pathID: link.pathID, name: link.name,
                               device: device, weight: link.weight))
        }
        return ClientConfig(clientID: clientID, keyHex: keyHex, homeHost: homeHost,
                            homePort: homePort, tunnelAddress: tunnelAddress,
                            dnsServers: dnsServers, links: pinned, mtu: mtu,
                            routes: routes)
    }

    /// The JSON the Go binding parses.
    ///
    /// Field names are the binding's, not Swift's. They are spelled here once
    /// and asserted in tests, because a typo produces a config the binding
    /// parses happily while ignoring the field - which is exactly how the
    /// identity came to be dropped.
    public var datapathJSON: String {
        let obj: [String: Any] = [
            "local_port": 0,          // let the OS choose; read back via localPort()
            "reorder_ms": 250,
            "client_id": clientID,
            "key_hex": keyHex,
        ]
        guard let data = try? JSONSerialization.data(withJSONObject: obj),
              let s = String(data: data, encoding: .utf8) else {
            return "{}"
        }
        return s
    }
}
