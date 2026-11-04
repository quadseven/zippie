import Foundation

/// Discovers same-LAN zippie peers and publishes the ones whose Bonjour TXT
/// proof verifies against the Go core's `VerifyPeer`.
///
/// # Concurrency
///
/// This is an actor. Every read and write of the verified-peer set happens on
/// this actor, so there is exactly one writer, no lock, no race, and no nil
/// dereference. The browser callback runs on `browseQueue` (never the main
/// actor) and only yields the batch into an ordered stream, so it never waits
/// on verification. One consumer task drains that stream onto this actor in
/// arrival order, so a removal is never applied before the addition that came
/// first. No sync I/O runs on an async path: the only async point is the
/// awaited `verifier.verify(txt:)`.
///
/// Because `apply` suspends while a proof verifies, a later batch can remove
/// the same peer meanwhile. A per-name generation counter makes the late
/// verification result a no-op in that case, so a vanished peer is never
/// republished by a stale verification.
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

    /// One browser callback: the peers it added and the names it removed.
    struct Batch: Sendable {
        let added: [DiscoveredPeer]
        let removed: [String]
    }

    private let verifier: any PeerVerifying
    private nonisolated let browser: any PeerBrowsing
    private nonisolated let advertiser: (any PeerAdvertising)?
    private let config: Config
    private nonisolated let browseQueue: DispatchQueue

    private var verified: [String: VerifiedPeer] = [:]
    /// Bumped on every addition or removal of a name, so a verification that
    /// finishes after a newer change to the same name is discarded.
    private var generation: [String: UInt64] = [:]

    private let eventsStream: AsyncStream<PeerEvent>
    private let eventContinuation: AsyncStream<PeerEvent>.Continuation

    private nonisolated let batches: AsyncStream<Batch>
    private nonisolated let batchContinuation: AsyncStream<Batch>.Continuation

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
        let (batchStream, batchContinuation) = AsyncStream<Batch>.makeStream()
        self.batches = batchStream
        self.batchContinuation = batchContinuation
    }

    /// Start advertising and browsing. Browser results are delivered on
    /// `browseQueue`, never the main actor; the handler only enqueues them.
    /// One-shot: after `stop()` the service does not restart.
    public nonisolated func start() {
        let sink = batchContinuation
        browser.resultsChangedHandler = { added, removed in
            sink.yield(Batch(added: added, removed: removed))
        }
        Task { await self.drain() }
        browser.start(queue: browseQueue)
        advertiser?.start()
    }

    public nonisolated func stop() {
        browser.stop()
        advertiser?.stop()
        batchContinuation.finish()
    }

    /// Applies queued browser batches one at a time, in arrival order.
    private func drain() async {
        for await batch in batches {
            await apply(added: batch.added, removed: batch.removed)
        }
        eventContinuation.finish()
    }

    /// Applies one batch of browser results and returns when it is done.
    /// Tests call this directly to drive the service without a real network.
    /// All mutation of `verified` happens on this actor.
    public func deliver(added: [DiscoveredPeer], removed: [String]) async {
        await apply(added: added, removed: removed)
    }

    private func bump(_ name: String) -> UInt64 {
        let next = (generation[name] ?? 0) &+ 1
        generation[name] = next
        return next
    }

    private func apply(added: [DiscoveredPeer], removed: [String]) async {
        for name in removed {
            _ = bump(name)
            if verified.removeValue(forKey: name) != nil {
                eventContinuation.yield(.vanished(name))
            }
        }
        for peer in added {
            let ticket = bump(peer.name)
            let result = await verifier.verify(txt: peer.txt)
            // A newer change to this name arrived while the proof verified:
            // that change wins, and this stale result is dropped.
            guard generation[peer.name] == ticket else { continue }
            guard let verifiedPeer = result else {
                // Refused: nothing said by an unverified advertisement is
                // believed. A re-advertisement that no longer verifies also
                // withdraws whatever this name proved before.
                if verified.removeValue(forKey: peer.name) != nil {
                    eventContinuation.yield(.vanished(peer.name))
                }
                continue
            }
            if verified[peer.name] != verifiedPeer {
                verified[peer.name] = verifiedPeer
                eventContinuation.yield(.discovered(verifiedPeer))
            }
        }
    }
}
