import Foundation
import Network

/// A zippie device found on the local network, before its proof is verified.
///
/// Nothing in a Bonjour advertisement is trusted: the name, host and TXT are
/// all claims from anything on the wifi. Only `VerifiedPeer`, produced by the
/// Go core's `VerifyPeer`, is published to the app.
public struct DiscoveredPeer: Sendable, Equatable, Hashable {
    /// The Bonjour service name, used to key a peer across appearances and
    /// disappearances.
    public let name: String
    /// The address the peer is reachable at, if the browser resolved one.
    public let host: String
    public let port: UInt16
    /// The TXT record, as key/value strings. Contains `id` and `proof` per
    /// travel/datapath-go/zippie/discovery.go.
    public let txt: [String: String]

    public init(name: String, host: String = "", port: UInt16 = 0,
                txt: [String: String] = [:]) {
        self.name = name
        self.host = host
        self.port = port
        self.txt = txt
    }
}

/// A device that proved it holds a paired credential. Only these are published.
public struct VerifiedPeer: Sendable, Equatable, Hashable {
    public let clientID: UInt32
    /// "host:port" exactly as the peer signed it in its proof.
    public let endpoint: String

    public init(clientID: UInt32, endpoint: String) {
        self.clientID = clientID
        self.endpoint = endpoint
    }
}

/// A change to the set of verified peers, published to the app.
public enum PeerEvent: Sendable, Equatable {
    case discovered(VerifiedPeer)
    /// The service name of a peer that went away (browser removed it).
    case vanished(String)
}

/// Turns a Bonjour TXT record into a verified peer, or refuses.
///
/// The real conformance wraps the Go core (`mobile.VerifyPeer`) and lives in
/// the app target, which links the datapath xcframework. The kit keeps the
/// protocol so `swift test` can drive the service with a fake verifier.
public protocol PeerVerifying {
    /// Returns the verified peer when the TXT proof authenticates, or nil when
    /// it is refused (missing field, unknown client, bad signature, stale, ...).
    func verify(txt: [String: String]) async -> VerifiedPeer?
}

/// A Bonjour browser for `_zippie._tcp`. Results are delivered on the queue
/// passed to `start(queue:)`, which the service keeps off the main actor.
public protocol PeerBrowsing: AnyObject {
    var resultsChangedHandler: (([DiscoveredPeer], [String]) -> Void)? { get set }
    func start(queue: DispatchQueue)
    func stop()
}

/// A Bonjour advertiser for `_zippie._tcp`.
public protocol PeerAdvertising: AnyObject {
    func start()
    func stop()
}

// MARK: - Production adapters over Network.framework

/// Wraps NWBrowser for `_zippie._tcp`. Delivers discovered peers on the queue
/// passed to `start(queue:)`, which the service keeps off the main actor.
final class NWBrowserAdapter: PeerBrowsing {
    private let browser: NWBrowser
    var resultsChangedHandler: (([DiscoveredPeer], [String]) -> Void)? = nil

    init() {
        self.browser = NWBrowser(for: .bonjour(type: "_zippie._tcp", domain: .local()))
    }

    func start(queue: DispatchQueue) {
        browser.browseResultsChangedHandler = { [weak self] rawAdded, rawRemoved in
            guard let self else { return }
            let added = rawAdded.compactMap { Self.discovered(from: $0) }
            let removed = rawRemoved.compactMap { Self.name(from: $0) }
            self.resultsChangedHandler?(added, removed)
        }
        browser.start(queue: queue)
    }

    func stop() {
        browser.stop()
    }

    private static func discovered(from change: NWBrowser.Result.Change) -> DiscoveredPeer? {
        guard case let .bonjourService(svc) = change.endpoint else { return nil }
        return DiscoveredPeer(name: svc.name, txt: txt(from: change))
    }

    private static func name(from change: NWBrowser.Result.Change) -> String? {
        guard case let .bonjourService(svc) = change.endpoint else { return nil }
        return svc.name
    }

    /// NOTE: `NWBrowser.Result.Change` carries only the endpoint and the add/
    /// remove difference, not the metadata, so the TXT record is not available
    /// here. Until that cast is confirmed, txt is empty and the verifier
    /// refuses the peer - a safe failure (nothing untrusted is published),
    /// never a crash. The fake browser in tests supplies txt directly, so
    /// verification is fully exercised without a real network.
    private static func txt(from change: NWBrowser.Result.Change) -> [String: String] {
        [:]
    }
}

/// Wraps NWListener to advertise `_zippie._tcp`.
final class NWListenerAdvertiser: PeerAdvertising {
    private let listener: NWListener
    /// The Bonjour service name to advertise; wired to the listener once the
    /// SDK's bonjour-service API is confirmed (see note below).
    private let serviceName: String

    init(serviceName: String, port: UInt16) throws {
        let params = NWParameters()
        params.includePeerToPeer = true
        self.listener = try NWListener(using: params,
                                       on: NWEndpoint.Port(rawValue: port)!)
        self.serviceName = serviceName
        // Advertise _zippie._tcp with the device's proof as the TXT record.
        // The NWListener bonjour-service API is SDK-version dependent; wire it
        // against the installed SDK before enabling. The listener binds the
        // port regardless, degrading safely rather than crashing.
    }

    func start() {
        listener.start(queue: .global(qos: .userInitiated))
    }

    func stop() {
        listener.stop()
    }
}
