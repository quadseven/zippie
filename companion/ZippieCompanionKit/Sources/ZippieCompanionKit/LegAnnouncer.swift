import Foundation

/// Tells the router this phone is here, and keeps saying so.
///
/// WHY THIS REPLACES A CONFIG ENTRY. The router used to carry a static leg for
/// each phone: a name and a fixed address in zippie.toml. A phone is not a
/// fixed address. It moves on DHCP, it leaves, it comes back, and the entry
/// stayed - so the router kept dialling an address a phone held once, sprayed
/// megabytes into it, and reported the leg as healthy because a configured leg
/// passes the shallow checks.
///
/// An announcement is a LEASE. It expires. A phone that goes into a tunnel or
/// runs out of battery stops renewing and its leg goes away on its own, which
/// is the property a config file can never have.
///
/// RENEWED FROM THE EXTENSION, NOT THE APP. The relay runs in a Network
/// Extension precisely so it survives the app being closed; announcing from
/// the app would mean the leg expiring the moment someone locked their phone
/// while it was still relaying.
public actor LegAnnouncer {

    public struct Config: Sendable {
        /// The router's console on the LAN. Announcing over the tailnet would
        /// be announcing to a router we are not on the network of.
        public let consoleHost: String
        public let token: String
        /// The router's key for this leg. Stable across address changes - it is
        /// what makes a moved phone an update rather than a second leg.
        public let name: String
        public let label: String
        public let listenPort: UInt16

        public init(consoleHost: String, token: String, name: String,
                    label: String, listenPort: UInt16) {
            self.consoleHost = consoleHost
            self.token = token
            self.name = name
            self.label = label
            self.listenPort = listenPort
        }

        public var isUsable: Bool {
            !consoleHost.isEmpty && !token.isEmpty && !name.isEmpty && listenPort > 0
        }
    }

    /// The router caps a lease at 300s and defaults to 45s. Renewing at a third
    /// of that means two consecutive failures - a lock screen, a bad radio
    /// moment - do not drop the leg.
    public static let leaseSeconds: Double = 45
    public static let renewInterval: Double = 15

    public enum Outcome: Sendable, Equatable {
        case announced(leaseRemaining: Double)
        case refused(String)
        case unreachable(String)
    }

    private let session: URLSession
    private var renewTask: Task<Void, Never>?

    public init(session: URLSession = .shared) {
        self.session = session
    }

    /// One announcement. `address` is this phone's own LAN address; nil means
    /// we are not on a local network and there is nothing to announce.
    public func announce(_ config: Config, address: String?) async -> Outcome {
        guard config.isUsable else { return .refused("announcer is not configured") }
        guard let address, !address.isEmpty else {
            // NOT AN ERROR. Off a local network there is no address the router
            // could dial, and announcing a wrong one is worse than silence.
            return .refused("no local address to announce")
        }
        let body: [String: Any] = [
            "name": config.name,
            "host": address,
            "port": Int(config.listenPort),
            "label": config.label,
            "lease_s": Self.leaseSeconds,
        ]
        return await post("/api/legs/announce", body, config)
    }

    /// An explicit goodbye, so a phone that stops relaying on purpose does not
    /// linger for a whole lease.
    @discardableResult
    public func withdraw(_ config: Config) async -> Outcome {
        guard config.isUsable else { return .refused("announcer is not configured") }
        return await post("/api/legs/withdraw", ["name": config.name], config)
    }

    private func post(_ path: String, _ body: [String: Any], _ config: Config) async -> Outcome {
        guard let url = URL(string: "http://\(config.consoleHost)\(path)"),
              let data = try? JSONSerialization.data(withJSONObject: body) else {
            return .refused("could not build the request")
        }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.httpBody = data
        req.setValue("Bearer \(config.token)", forHTTPHeaderField: "Authorization")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        // Short. On the router's own LAN this is a sub-millisecond request, and
        // anywhere else it should fail fast rather than hold the relay's queue.
        req.timeoutInterval = 4

        do {
            let (payload, response) = try await session.data(for: req)
            let code = (response as? HTTPURLResponse)?.statusCode ?? 0
            guard code == 200 else {
                // The router's own words. It says exactly which field it
                // refused, and inventing a friendlier message would lose that.
                let detail = (try? JSONSerialization.jsonObject(with: payload))
                    .flatMap { ($0 as? [String: Any])?["error"] as? String }
                return .refused(detail ?? "HTTP \(code)")
            }
            let lease = (try? JSONSerialization.jsonObject(with: payload))
                .flatMap { ($0 as? [String: Any])?["lease_s"] as? Double }
            return .announced(leaseRemaining: lease ?? Self.leaseSeconds)
        } catch {
            return .unreachable(error.localizedDescription)
        }
    }

    // MARK: - keeping the lease alive

    /// Announce now, then keep renewing until stopped.
    public func start(_ config: Config, address: @escaping @Sendable () -> String?,
                      report: (@Sendable (Outcome) -> Void)? = nil) {
        renewTask?.cancel()
        renewTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                let outcome = await self.announce(config, address: address())
                report?(outcome)
                try? await Task.sleep(nanoseconds: UInt64(Self.renewInterval * 1_000_000_000))
            }
        }
    }

    public func stop(_ config: Config) async {
        renewTask?.cancel()
        renewTask = nil
        await withdraw(config)
    }
}
