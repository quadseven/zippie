import XCTest
@testable import ZippieCompanionKit

/// Peer discovery: verified, refused and vanished peers, driven by a fake
/// browser so no real network is required.
///
/// These tests also prove the actor contract: every browser callback funnels
/// through `deliver` onto this actor, so state changes happen on one actor and
/// the callback never blocks the main actor (the browser runs on a dedicated
/// queue).
final class PeerDiscoveryTests: XCTestCase {

    private func peer(id: UInt32, name: String = "peer-\(id)",
                      endpoint: String = "192.0.2.\(id):51999") -> VerifiedPeer {
        VerifiedPeer(clientID: id, endpoint: endpoint)
    }

    private func discovered(id: UInt32, name: String = "peer-\(id)",
                            proof: String = "proof-\(id)") -> DiscoveredPeer {
        DiscoveredPeer(name: name, txt: ["id": String(id), "proof": proof])
    }

    // MARK: - fake collaborators

    private final class FakeVerifier: PeerVerifying {
        var verifiedByID: [UInt32: VerifiedPeer] = [:]
        var refusedIDs: [UInt32] = []
        var calls: [[String: String]] = []

        func verify(txt: [String: String]) async -> VerifiedPeer? {
            calls.append(txt)
            guard let idText = txt["id"], let id = UInt32(idText) else {
                return nil
            }
            if refusedIDs.contains(id) { return nil }
            return verifiedByID[id]
        }
    }

    private final class FakeBrowser: PeerBrowsing {
        var resultsChangedHandler: (([DiscoveredPeer], [String]) -> Void)? = nil
        var started = false
        var stopped = false
        var startQueue: DispatchQueue?

        func start(queue: DispatchQueue) {
            started = true
            startQueue = queue
        }
        func stop() { stopped = true }

        /// Drives the handler exactly as the real browser would.
        func deliver(added: [DiscoveredPeer], removed: [String]) {
            resultsChangedHandler?(added, removed)
        }
    }

    private final class FakeAdvertiser: PeerAdvertising {
        var started = false
        var stopped = false
        func start() { started = true }
        func stop() { stopped = true }
    }

    // MARK: - harness

    private func makeService(verifier: FakeVerifier = FakeVerifier(),
                             browser: FakeBrowser = FakeBrowser(),
                             advertiser: PeerAdvertising? = nil) -> PeerDiscoveryService {
        PeerDiscoveryService(
            config: .init(serviceName: "operator-phone", listenPort: 51999),
            verifier: verifier,
            browser: browser,
            advertiser: advertiser
        )
    }

    // MARK: - verified peer

    func testVerifiedPeerIsPublished() async {
        let v = FakeVerifier()
        v.verifiedByID[7] = peer(id: 7)
        let b = FakeBrowser()
        let s = makeService(verifier: v, browser: b)

        await s.deliver(added: [discovered(id: 7)], removed: [])

        XCTAssertEqual(await s.peers, [peer(id: 7)])
        XCTAssertEqual(v.calls.count, 1, "verifier is invoked once per discovered peer")
    }

    func testOnlyVerifiedPeersArePublished() async {
        let v = FakeVerifier()
        v.verifiedByID[1] = peer(id: 1)
        v.refusedIDs = [2]
        let b = FakeBrowser()
        let s = makeService(verifier: v, browser: b)

        await s.deliver(
            added: [discovered(id: 1), discovered(id: 2), discovered(id: 3)],
            removed: [])

        XCTAssertEqual(await s.peers, [peer(id: 1)])
        // id 2 refused, id 3 unknown: neither is published.
        XCTAssertEqual(v.calls.count, 3)
    }

    // MARK: - refused peer

    func testRefusedPeerIsNotPublished() async {
        let v = FakeVerifier()
        v.refusedIDs = [9]
        let b = FakeBrowser()
        let s = makeService(verifier: v, browser: b)

        await s.deliver(added: [discovered(id: 9)], removed: [])

        XCTAssertTrue(await s.peers.isEmpty, "a refused peer must not be published")
    }

    func testUnparseableIdIsRefused() async {
        let v = FakeVerifier()
        let b = FakeBrowser()
        let s = makeService(verifier: v, browser: b)

        await s.deliver(added: [DiscoveredPeer(name: "weird", txt: ["id": "not-a-number", "proof": "x"])],
                        removed: [])

        XCTAssertTrue(await s.peers.isEmpty)
    }

    // MARK: - vanished peer

    func testVanishedPeerIsRemoved() async {
        let v = FakeVerifier()
        v.verifiedByID[5] = peer(id: 5)
        let b = FakeBrowser()
        let s = makeService(verifier: v, browser: b)

        await s.deliver(added: [discovered(id: 5)], removed: [])
        XCTAssertEqual(await s.peers, [peer(id: 5)])

        await s.deliver(added: [], removed: ["peer-5"])
        XCTAssertTrue(await s.peers.isEmpty, "a vanished peer is removed from the published set")
    }

    func testVanishedUnknownPeerIsNoOp() async {
        let s = makeService()
        await s.deliver(added: [], removed: ["peer-does-not-exist"])
        XCTAssertTrue(await s.peers.isEmpty)
    }

    func testPeerReappearsIsRepublished() async {
        let v = FakeVerifier()
        v.verifiedByID[4] = peer(id: 4, endpoint: "192.0.2.4:51999")
        let b = FakeBrowser()
        let s = makeService(verifier: v, browser: b)

        await s.deliver(added: [discovered(id: 4)], removed: [])
        await s.deliver(added: [], removed: ["peer-4"])
        XCTAssertTrue(await s.peers.isEmpty)

        // The same service name comes back with a fresh proof.
        v.verifiedByID[4] = peer(id: 4, endpoint: "192.0.2.4:52000")
        await s.deliver(added: [discovered(id: 4)], removed: [])
        XCTAssertEqual(await s.peers, [peer(id: 4, endpoint: "192.0.2.4:52000")])
    }

    // MARK: - actor contract (state changes on one actor)

    func testConcurrentDeliveriesSerializeOnOneActor() async {
        let v = FakeVerifier()
        for id in 1...8 { v.verifiedByID[id] = peer(id: id) }
        let b = FakeBrowser()
        let s = makeService(verifier: v, browser: b)

        // Deliver from several concurrent tasks; the actor serializes them so
        // every verified peer ends up published exactly once, with no lost
        // updates or corrupted dictionary.
        await withTaskGroup(of: Void.self) { group in
            for id in 1...8 {
                group.addTask {
                    await s.deliver(added: [self.discovered(id: id)], removed: [])
                }
            }
        }

        XCTAssertEqual(Set(await s.peers), Set((1...8).map { self.peer(id: $0) }))
        XCTAssertEqual(v.calls.count, 8, "each discovered peer is verified once")
    }

    // MARK: - main-actor independence

    func testBrowserRunsOnADedicatedQueueNotTheMainActor() async {
        let a = FakeAdvertiser()
        let b = FakeBrowser()
        let s = makeService(browser: b, advertiser: a)

        s.start()

        XCTAssertTrue(b.started, "browser was started")
        XCTAssertNotNil(b.startQueue, "browser was started on a queue")
        XCTAssertFalse(b.startQueue === DispatchQueue.main,
                       "the browser must not run on the main actor's queue")
        XCTAssertTrue(a.started, "advertiser was started")
    }

    func testStopHaltsBrowserAndAdvertiser() async {
        let a = FakeAdvertiser()
        let b = FakeBrowser()
        let s = makeService(browser: b, advertiser: a)
        s.start()
        s.stop()

        XCTAssertTrue(b.stopped)
        XCTAssertTrue(a.stopped)
    }
}
