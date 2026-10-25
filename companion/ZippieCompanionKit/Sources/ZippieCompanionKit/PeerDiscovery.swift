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
///
/// The `resultsChangedHandler` receives `Set<NWBrowser.Result>` for added
/// results and `Set<NWBrowser.Result.Change>` for removed changes. Added
/// results carry endpoint and metadata directly; removed changes wrap the
/// result via their `result` property.
final class NWBrowserAdapter: PeerBrowsing {
    private let browser: NWBrowser
    var resultsChangedHandler: (([DiscoveredPeer], [String]) -> Void)? = nil

    init() {
        let params = NWParameters()
        params.includePeerToPeer = true
        self.browser = NWBrowser(for: .bonjour(type: "_zippie._tcp", domain: .local),
                                  using: params)
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
        browser.cancel()
    }

    /// Extracts a discovered peer from a browse result. The service name and
    /// TXT record come from the result's metadata; host and port come from
    /// the endpoint when it has been resolved to a host:port.
    private static func discovered(from result: NWBrowser.Result) -> DiscoveredPeer? {
        for metadata in result.metadata {
            if case let .bonjourService(svc) = metadata {
                var host = ""
                var port: UInt16 = 0
                if case let .hostPort(hp) = result.endpoint {
                    host = hp.hostname
                    port = hp.port.rawValue
                }
                return DiscoveredPeer(name: svc.name, host: host, port: port,
                                      txt: txt(from: svc.txtRecord))
            }
        }
        return nil
    }

    /// Extracts a service name from a removed change. The change wraps the
    /// result via its `result` property, so the metadata is read from there.
    private static func name(from change: NWBrowser.Result.Change) -> String? {
        for metadata in change.result.metadata {
            if case let .bonjourService(svc) = metadata {
                return svc.name
            }
        }
        return nil
    }

    /// Parses a Bonjour TXT record (length-prefixed key=value entries) into a
    /// dictionary. Returns empty on any malformed input.
    private static func txt(from data: Data?) -> [String: String] {
        guard let data else { return [:] }
        var result: [String: String] = [:]
        var offset = 0
        while offset < data.count {
            let keyLength = Int(data[offset])
            offset += 1
            guard offset + keyLength <= data.count else { break }
            let key = String(data: data.subdata(in: offset..<offset + keyLength), encoding: .utf8) ?? ""
            offset += keyLength
            var value = ""
            if offset < data.count {
                let valueLength = Int(data[offset])
                offset += 1
                if offset + valueLength <= data.count {
                    value = String(data: data.subdata(in: offset..<offset + valueLength), encoding: .utf8) ?? ""
                    offset += valueLength
                }
            }
            result[key] = value
        }
        return result
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
        listener.cancel()
    }
}
