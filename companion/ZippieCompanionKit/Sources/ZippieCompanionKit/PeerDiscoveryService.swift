import Foundation

/// Discovers same-LAN zippie peers and publishes the ones whose Bonjour TXT
/// proof verifies against the Go core's `VerifyPeer`.
///
/// # Concurrency
///
/// This is an actor. Every read and write of the verified-peer set happens on
/// this actor, so there is exactly one writer, no lock, no race, and no nil
/// dereference. The browser callback runs on `browseQueue` (never the main
/// actor) and funnels through `deliver`, which hops to this actor. No sync I/O
/// runs on an async path: the only async point is the awaited
/// `verifier.verify(txt:)`.
///
/// # The bugs this replaces
///
/// The draft reviewed for #270 had (1) a race on the peer dictionary, (2) a
/// nil dereference from force-unwrapping a browser result, and (3) synchronous
/// I/O on the browser's async path. This version removes all three: state is
/// owned by one actor, results are bound-checked before use, and verification
/// is awaited rather than performed synchronously.
public actor PeerDiscoveryService {
    public struct Config: Sendable {
        /// The Bonjour service name this device advertises as.
        public let serviceName: String
        /// The loopback/LAN port a verified peer should dial this device on.
        public let listenPort: UInt16
        /// The TXT record this device advertises (its own proof).
        public let advertiseTxt: [String: String]

        public init(serviceName: String, listenPort: UInt16,
                    advertiseTxt: [String: String] = [:]) {
            self.serviceName = serviceName
            self.listenPort = listenPort
            self.advertiseTxt = advertiseTxt
        }
    }

    private let verifier: any PeerVerifying
    private let browser: any PeerBrowsing
    private let advertiser: (any PeerAdvertising)?
    private let config: Config
    private let browseQueue: DispatchQueue

    private var verified: [String: VerifiedPeer] = [:]

    private let eventsStream: AsyncStream<PeerEvent>
    private let eventContinuation: AsyncStream<PeerEvent>.Continuation

    /// The verified peers, sorted by client id for stable output to the app.
    public var peers: [VerifiedPeer] {
        verified.values.sorted { $0.clientID < $1.clientID }
    }

    /// Live changes to the verified set, consumed from the app (typically the
    /// main actor); values are produced on this actor.
    public var events: AsyncStream<PeerEvent> {
        eventsStream
    }

    public init(config: Config, verifier: any PeerVerifying,
                browser: any PeerBrowsing,
                advertiser: (any PeerAdvertising)? = nil) {
        self.config = config
        self.verifier = verifier
        self.browser = browser
        self.advertiser = advertiser
        self.browseQueue = DispatchQueue(label: "app.zippie.peer-discovery",
                                         qos: .userInitiated)
        let (stream, continuation) = AsyncStream<PeerEvent>.makeStream()
        self.eventsStream = stream
        self.eventContinuation = continuation
    }

    /// Start advertising and browsing. Browser results are delivered on
    /// `browseQueue`, never the main actor.
    public nonisolated func start() {
        browser.resultsChangedHandler = { [weak self] added, removed in
            guard let self else { return }
            Task { await self.deliver(added: added, removed: removed) }
        }
        browser.start(queue: browseQueue)
        advertiser?.start()
    }

    public nonisolated func stop() {
        browser.stop()
        advertiser?.stop()
    }

    /// The single entry point for browser results. Tests call this directly to
    /// drive the service without a real network; production calls it from the
    /// browser's `resultsChangedHandler`. All mutation of `verified` happens
    /// here, on this actor.
    public nonisolated func deliver(added: [DiscoveredPeer], removed: [String]) async {
        await apply(added: added, removed: removed)
    }

    private func apply(added: [DiscoveredPeer], removed: [String]) async {
        for name in removed where verified[name] != nil {
            verified.removeValue(forKey: name)
            eventContinuation.yield(.vanished(name))
        }
        for peer in added {
            guard let verifiedPeer = await verifier.verify(txt: peer.txt) else {
                // Refused: nothing said by an unverified advertisement is
                // believed, so the peer is not published.
                continue
            }
            if verified[peer.name] != verifiedPeer {
                verified[peer.name] = verifiedPeer
                eventContinuation.yield(.discovered(verifiedPeer))
            }
        }
    }
}
