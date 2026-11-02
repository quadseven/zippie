import Network
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

    private func peer(id: UInt32, endpoint: String? = nil) -> VerifiedPeer {
        VerifiedPeer(clientID: id, endpoint: endpoint ?? "192.0.2.\(id):51999")
    }

    private func discovered(id: UInt32) -> DiscoveredPeer {
        DiscoveredPeer(name: "peer-\(id)", txt: ["id": String(id), "proof": "proof-\(id)"])
    }

    // MARK: - fake collaborators

    /// Verifies from a table. When `gate` is set, each verification waits
    /// for it to open first, so a test can hold a proof mid-verification.
    private final class FakeVerifier: PeerVerifying, @unchecked Sendable {
        var verifiedByID: [UInt32: VerifiedPeer] = [:]
        var refusedIDs: [UInt32] = []
        var calls: [[String: String]] = []
        var gate: Gate?

        func verify(txt: [String: String]) async -> VerifiedPeer? {
            calls.append(txt)
            if let gate { await gate.wait() }
            guard let idText = txt["id"], let id = UInt32(idText) else {
                return nil
            }
            if refusedIDs.contains(id) { return nil }
            return verifiedByID[id]
        }
    }

    /// A one-shot latch: `wait()` suspends until `open()` is called.
    private actor Gate {
        private var isOpen = false
        private var waiters: [CheckedContinuation<Void, Never>] = []
        private(set) var waiting = 0

        func wait() async {
            if isOpen { return }
            waiting += 1
            await withCheckedContinuation { waiters.append($0) }
        }

        func open() {
            isOpen = true
            waiters.forEach { $0.resume() }
            waiters.removeAll()
        }
    }

    /// Polls the service until `condition` holds or about two seconds pass.
    private func eventually(_ s: PeerDiscoveryService,
                            _ condition: @escaping ([VerifiedPeer]) -> Bool) async -> Bool {
        for _ in 0..<200 {
            if condition(await s.peers) { return true }
            try? await Task.sleep(nanoseconds: 10_000_000)
        }
        return false
    }

    private final class FakeBrowser: PeerBrowsing, @unchecked Sendable {
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

        let peers = await s.peers
        XCTAssertEqual(peers, [peer(id: 7)])
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

        let peers = await s.peers
        XCTAssertEqual(peers, [peer(id: 1)])
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

        let peers = await s.peers
        XCTAssertTrue(peers.isEmpty, "a refused peer must not be published")
    }

    func testUnparseableIdIsRefused() async {
        let v = FakeVerifier()
        let b = FakeBrowser()
        let s = makeService(verifier: v, browser: b)

        await s.deliver(added: [DiscoveredPeer(name: "weird", txt: ["id": "not-a-number", "proof": "x"])],
                        removed: [])

        let peers = await s.peers
        XCTAssertTrue(peers.isEmpty)
    }

    // MARK: - vanished peer

    func testVanishedPeerIsRemoved() async {
        let v = FakeVerifier()
        v.verifiedByID[5] = peer(id: 5)
        let b = FakeBrowser()
        let s = makeService(verifier: v, browser: b)

        await s.deliver(added: [discovered(id: 5)], removed: [])
        let peers = await s.peers
        XCTAssertEqual(peers, [peer(id: 5)])

        await s.deliver(added: [], removed: ["peer-5"])
        let after = await s.peers
        XCTAssertTrue(after.isEmpty, "a vanished peer is removed from the published set")
    }

    func testVanishedUnknownPeerIsNoOp() async {
        let s = makeService()
        await s.deliver(added: [], removed: ["peer-does-not-exist"])
        let peers = await s.peers
        XCTAssertTrue(peers.isEmpty)
    }

    func testPeerReappearsIsRepublished() async {
        let v = FakeVerifier()
        v.verifiedByID[4] = peer(id: 4, endpoint: "192.0.2.4:51999")
        let b = FakeBrowser()
        let s = makeService(verifier: v, browser: b)

        await s.deliver(added: [discovered(id: 4)], removed: [])
        await s.deliver(added: [], removed: ["peer-4"])
        let peers = await s.peers
        XCTAssertTrue(peers.isEmpty)

        // The same service name comes back with a fresh proof.
        v.verifiedByID[4] = peer(id: 4, endpoint: "192.0.2.4:52000")
        await s.deliver(added: [discovered(id: 4)], removed: [])
        let after = await s.peers
        XCTAssertEqual(after, [peer(id: 4, endpoint: "192.0.2.4:52000")])
    }

    // MARK: - actor contract (state changes on one actor)

    func testConcurrentDeliveriesSerializeOnOneActor() async {
        let v = FakeVerifier()
        for id: UInt32 in 1...8 { v.verifiedByID[id] = peer(id: id) }
        let b = FakeBrowser()
        let s = makeService(verifier: v, browser: b)

        // Deliver from several concurrent tasks; the actor serializes them so
        // every verified peer ends up published exactly once, with no lost
        // updates or corrupted dictionary.
        await withTaskGroup(of: Void.self) { group in
            for id: UInt32 in 1...8 {
                group.addTask {
                    await s.deliver(added: [self.discovered(id: id)], removed: [])
                }
            }
        }

        let peers = await s.peers
        XCTAssertEqual(Set(peers), Set((UInt32(1)...8).map { self.peer(id: $0) }))
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

    // MARK: - browser callback path (criterion 2)

    /// The browser callback only enqueues: it returns while the proof is still
    /// held mid-verification, so a callback on any queue never waits on the
    /// verifier. The peer is published once verification finishes.
    func testBrowserCallbackReturnsWithoutWaitingOnVerification() async {
        let gate = Gate()
        let v = FakeVerifier()
        v.verifiedByID[3] = peer(id: 3)
        v.gate = gate
        let b = FakeBrowser()
        let s = makeService(verifier: v, browser: b)
        s.start()

        b.deliver(added: [discovered(id: 3)], removed: [])

        var held = false
        for _ in 0..<200 where !held {
            held = await gate.waiting == 1
            if !held { try? await Task.sleep(nanoseconds: 10_000_000) }
        }
        XCTAssertTrue(held, "the callback returned while verification was still held")
        let before = await s.peers
        XCTAssertTrue(before.isEmpty, "nothing is published before the proof verifies")

        await gate.open()
        let published = await eventually(s) { $0 == [self.peer(id: 3)] }
        XCTAssertTrue(published, "the verified peer is published after the callback path")
        s.stop()
    }

    /// Callbacks are applied in the order the browser sent them: an add then
    /// a remove of the same peer leaves nothing published.
    func testBrowserCallbacksApplyInArrivalOrder() async {
        let v = FakeVerifier()
        for id: UInt32 in 1...4 { v.verifiedByID[id] = peer(id: id) }
        let b = FakeBrowser()
        let s = makeService(verifier: v, browser: b)
        s.start()

        b.deliver(added: [discovered(id: 1), discovered(id: 2)], removed: [])
        b.deliver(added: [], removed: ["peer-1"])
        b.deliver(added: [discovered(id: 3)], removed: ["peer-2"])
        b.deliver(added: [discovered(id: 4)], removed: [])

        let settled = await eventually(s) { $0 == [self.peer(id: 3), self.peer(id: 4)] }
        XCTAssertTrue(settled, "every callback applied, in order")
        s.stop()
    }

    // MARK: - stale verification

    /// A peer removed while its proof is still verifying stays removed: the
    /// late verification result is discarded.
    func testPeerRemovedDuringVerificationIsNotRepublished() async {
        let gate = Gate()
        let v = FakeVerifier()
        v.verifiedByID[6] = peer(id: 6)
        v.gate = gate
        let s = makeService(verifier: v)

        let adding = Task { await s.deliver(added: [discovered(id: 6)], removed: []) }
        var held = false
        for _ in 0..<200 where !held {
            held = await gate.waiting == 1
            if !held { try? await Task.sleep(nanoseconds: 10_000_000) }
        }
        XCTAssertTrue(held)

        await s.deliver(added: [], removed: ["peer-6"])
        await gate.open()
        await adding.value

        let peers = await s.peers
        XCTAssertTrue(peers.isEmpty, "a stale verification must not republish a vanished peer")
    }

    /// A known peer that re-advertises with a proof that no longer verifies
    /// is withdrawn, not left published on its old proof.
    func testReadvertisementThatIsRefusedWithdrawsThePeer() async {
        let v = FakeVerifier()
        v.verifiedByID[8] = peer(id: 8)
        let s = makeService(verifier: v)
        await s.deliver(added: [discovered(id: 8)], removed: [])

        v.refusedIDs = [8]
        await s.deliver(added: [discovered(id: 8)], removed: [])

        let peers = await s.peers
        XCTAssertTrue(peers.isEmpty)
    }

    // MARK: - endpoint naming

    /// Bonjour browse results carry a `.service` endpoint; its service name is
    /// the key added and removed results are matched on.
    func testPeerNameComesFromTheBonjourServiceEndpoint() {
        let endpoint = NWEndpoint.service(name: "peer-11", type: "_zippie._tcp",
                                          domain: "local.", interface: nil)
        XCTAssertEqual(NWBrowserAdapter.peerName(from: endpoint), "peer-11")
    }

    func testEmptyServiceNameIsSkipped() {
        let endpoint = NWEndpoint.service(name: "", type: "_zippie._tcp",
                                          domain: "local.", interface: nil)
        XCTAssertNil(NWBrowserAdapter.peerName(from: endpoint))
    }
}
