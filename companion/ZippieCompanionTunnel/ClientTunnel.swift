import Foundation
import Network
import NetworkExtension
import ZippieCompanionKit
import os

#if canImport(Zippie)
import Zippie
#endif

/// Client mode: this phone's OWN traffic, bonded over wifi and cellular to home.
///
/// THE OPPOSITE OF THE RELAY. The contributor path forwards the travel router's
/// frames and captures nothing of this device's traffic - that is why the
/// tunnel it installs is inert (`includedRoutes = []`). Here the tunnel is the
/// point: every packet the phone sends is read off `packetFlow`, handed to the
/// Go datapath, sprayed across whichever links are up, and reassembled at home.
///
/// WHAT PROTECTS IT. The datapath frames are sealed (AES-256-GCM, see seal.go)
/// because the phone is originating its own IP packets across hotel wifi and a
/// carrier network. The relay never needed that - what it carried was already
/// WireGuard ciphertext - and getting this wrong would have been a downgrade
/// dressed up as a new feature.
///
/// THE LOOPBACK HOP. Packets reach the datapath through a UDP socket on
/// 127.0.0.1 rather than a direct function call. That is not laziness: it is the
/// SAME interface the travel router's WireGuard uses, so the phone exercises the
/// code path that has been carrying real traffic for months instead of a second
/// one written for it. The cost is one loopback send per packet.
@available(iOS 16.0, *)
final class ClientTunnel {

    private static let log = Logger(subsystem: "app.zippie.companion.tunnel",
                                    category: "client")

    /// The phone's address inside the tunnel.
    ///
    /// A /32 in a range home owns. It is NOT negotiated - there is no control
    /// protocol yet - so it comes from the pairing configuration and home is
    /// told the same value. A mismatch means home cannot route replies back,
    /// which is why it is configured in one place rather than defaulted here.
    private let config: ClientConfig
    private let packetFlow: NEPacketTunnelFlow

    // BEHIND THE GUARD, not merely used behind it. The type itself comes from
    // the gomobile framework, so a build without it must not even name the
    // property - otherwise the whole extension fails to compile rather than
    // degrading to "client mode unavailable".
    #if canImport(Zippie)
    private var datapath: MobileClient?
    #endif
    private var socket: NWConnection?
    private let queue = DispatchQueue(label: "app.zippie.client", qos: .userInitiated)
    private var running = false

    init(config: ClientConfig, packetFlow: NEPacketTunnelFlow) {
        self.config = config
        self.packetFlow = packetFlow
    }

    // MARK: - settings

    /// The capturing settings. Everything the phone sends goes through here.
    ///
    /// `includedRoutes = [default]` is the line that separates this from the
    /// relay's inert tunnel, and it is worth being explicit about what it costs:
    /// with this set, a datapath that stops carrying takes the phone's
    /// connectivity with it. That is the correct behaviour for a VPN - the
    /// alternative is traffic silently leaving in the clear - but it means a
    /// dead extension is an outage, not a degradation.
    static func networkSettings(_ config: ClientConfig) -> NEPacketTunnelNetworkSettings {
        let s = NEPacketTunnelNetworkSettings(tunnelRemoteAddress: config.homeHost)

        let ipv4 = NEIPv4Settings(addresses: [config.tunnelAddress],
                                  subnetMasks: ["255.255.255.255"])
        // ONLY WHAT THE CONFIG ASKS FOR. This used to be an unconditional
        // default route, which on iOS is not merely "capture everything" - it
        // is "capture everything INSTEAD OF Tailscale", because the OS runs one
        // packet tunnel at a time. A phone in client mode with a full tunnel
        // and no tailnet route cannot reach any of the user's own
        // infrastructure.
        ipv4.includedRoutes = config.routes.map {
            $0.isDefaultRoute ? NEIPv4Route.default()
                              : NEIPv4Route(destinationAddress: $0.address,
                                            subnetMask: $0.mask)
        }
        // The home transport's own address must NOT go through the tunnel, or
        // the datapath's packets to home would be routed into the tunnel that
        // carries them - a loop that presents as total silence.
        if let excluded = config.homeExcludedRoute {
            ipv4.excludedRoutes = [excluded]
        }
        s.ipv4Settings = ipv4

        // DNS through the tunnel, but ONLY on a full tunnel.
        //
        // On a full tunnel, leaving the local resolver in place would send
        // every lookup out the hotel's wifi in the clear while the traffic
        // itself went home - leaking exactly the thing most worth protecting,
        // and invisibly.
        //
        // On a SPLIT tunnel, claiming every query would be wrong in the other
        // direction: the phone is still using its own network for everything
        // outside the routed ranges, and hijacking its resolver would break
        // captive portals and local discovery for no benefit.
        if !config.dnsServers.isEmpty {
            let dns = NEDNSSettings(servers: config.dnsServers)
            dns.matchDomains = config.isFullTunnel ? [""] : ["ts.net"]
            s.dnsSettings = dns
        }

        // Room for the datapath header plus the AEAD nonce and tag. Too high
        // and the phone emits packets that fragment or are dropped on the
        // narrowest leg; the loss looks like a flaky carrier rather than an
        // MTU mistake.
        s.mtu = NSNumber(value: config.mtu)
        return s
    }

    // MARK: - lifecycle

    func start() throws {
        #if canImport(Zippie)
        // PIN THE LEGS TO INTERFACES THAT EXIST RIGHT NOW, BEFORE ANYTHING IS
        // ALLOCATED (#48).
        //
        // `Link.device` is a string that was correct when it was written down.
        // iOS does not promise those names: cellular is `pdp_ip0` through
        // `pdp_ipN` depending on how many contexts the modem brought up, and
        // `en0` is wifi only until a USB-C ethernet adapter arrives as `en2`.
        // The Go side turns the name into a socket with `net.InterfaceByName`
        // plus `IP_BOUND_IF`, so a stale name is an `AddLink` error - which
        // this function used to log and carry on past, coming up with a VPN
        // badge and no path home. A leg that cannot be pinned is DROPPED, never
        // carried through unpinned, because an unpinned socket leaves by
        // whichever interface wins the default route and two of those are one
        // path wearing two names.
        let pinned = config.repinned(using: LiveInterfaces.resolved())
        let admission = LegAdmission.admit(pinned.links.map(\.device))
        // THE ZERO CASE WAS SILENT. No legs means every packet the OS hands us
        // goes into a socket that cannot send, which from the phone is
        // indistinguishable from a dead network.
        guard admission.isStartable else { throw ClientTunnelError.noLegs }
        Self.log.log("client legs: \(admission.summary, privacy: .public)")

        var err: NSError?
        guard let client = MobileNewClient(config.datapathJSON, &err) else {
            throw err ?? ClientTunnelError.datapathFailed("unknown")
        }
        // A client that silently fell back to cleartext would look identical
        // from here: same counters, same behaviour, traffic readable on the
        // wire. Refusing to start is the only honest response.
        guard client.sealed() else {
            throw ClientTunnelError.notSealed
        }
        datapath = client

        for link in pinned.links {
            do {
                try client.addLink(link.pathID, name: link.name,
                                   device: link.device, remote: config.homeEndpoint,
                                   weight: link.weight)
                Self.log.info("client leg up: \(link.name, privacy: .public) on \(link.device, privacy: .public)")
            } catch {
                // One leg failing is not fatal - a phone with no cellular
                // signal still has wifi - but it must be visible, because a
                // "bond" quietly running on one leg is this project's oldest
                // failure mode. The device name is no longer a suspect here:
                // it was resolved from the live interface list moments ago.
                Self.log.error("client leg \(link.name, privacy: .public) refused: \(error.localizedDescription, privacy: .public)")
            }
        }
        client.start()

        try openLoopback(port: Int(client.localPort()))
        running = true
        readFromTunnel()
        #else
        throw ClientTunnelError.datapathMissing
        #endif
    }

    func stop() {
        running = false
        socket?.cancel()
        socket = nil
        #if canImport(Zippie)
        datapath?.stop()
        datapath = nil
        #endif
    }

    // MARK: - the two directions

    private func openLoopback(port: Int) throws {
        guard let p = NWEndpoint.Port(rawValue: UInt16(port)) else {
            throw ClientTunnelError.datapathFailed("bad local port \(port)")
        }
        let c = NWConnection(host: .ipv4(.loopback), port: p, using: .udp)
        c.stateUpdateHandler = { state in
            if case let .failed(e) = state {
                Self.log.error("loopback failed: \(e.localizedDescription, privacy: .public)")
            }
        }
        c.start(queue: queue)
        socket = c
        readFromDatapath()
    }

    /// Phone -> home. Every packet the OS wants to send.
    private func readFromTunnel() {
        packetFlow.readPacketObjects { [weak self] packets in
            guard let self, self.running else { return }
            for packet in packets {
                self.socket?.send(content: packet.data, completion: .idempotent)
            }
            // Re-arm. NEPacketTunnelFlow delivers one batch per call and stops
            // if the caller does not ask again - a missing re-arm is a tunnel
            // that carries exactly one batch and then looks hung.
            self.readFromTunnel()
        }
    }

    /// Home -> phone. Reassembled packets come back over the same loopback
    /// socket the datapath heard from.
    private func readFromDatapath() {
        socket?.receiveMessage { [weak self] data, _, _, error in
            guard let self, self.running else { return }
            if let data, !data.isEmpty {
                // The protocol number has to match the packet, or the OS
                // silently discards it - and a tunnel that receives bytes and
                // delivers nothing is indistinguishable from a dead one.
                let family = (data[0] >> 4) == 6 ? AF_INET6 : AF_INET
                self.packetFlow.writePackets([data], withProtocols: [NSNumber(value: family)])
            }
            if let error {
                Self.log.error("loopback receive: \(error.localizedDescription, privacy: .public)")
                return
            }
            self.readFromDatapath()
        }
    }

    func statsJSON() -> String {
        #if canImport(Zippie)
        return datapath?.statsJSON() ?? "{}"
        #else
        return "{}"
        #endif
    }
}

enum ClientTunnelError: LocalizedError {
    case datapathMissing
    case datapathFailed(String)
    case notSealed
    case noLegs

    var errorDescription: String? {
        switch self {
        case .datapathMissing:
            return "This build has no bonding datapath, so client mode cannot run."
        case let .datapathFailed(why):
            return "The datapath would not start: \(why)"
        case .notSealed:
            return "This phone has no pairing key, so its traffic would cross "
                 + "the internet unencrypted. Refusing to start."
        case .noLegs:
            return "None of this phone's configured links is up with an address, "
                 + "so the bond would carry nothing. Refusing to start."
        }
    }
}
