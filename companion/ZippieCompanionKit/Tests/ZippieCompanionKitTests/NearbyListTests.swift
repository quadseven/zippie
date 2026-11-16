import XCTest
@testable import ZippieCompanionKit

/// Nearby list: verified peers appear, refused don't, selection creates leg, removal within one browse cycle.
final class NearbyListTests: XCTestCase {

    private func peer(id: UInt32, endpoint: String? = nil) -> VerifiedPeer {
        VerifiedPeer(clientID: id, endpoint: endpoint ?? "192.0.2.\(id):51999")
    }

    private func discovered(id: UInt32) -> DiscoveredPeer {
        DiscoveredPeer(name: "peer-\(id)", txt: ["id": String(id), "proof": "proof-\(id)"])
    }

    // MARK: - fake collaborators

    private final class FakeVerifier: PeerVerifying, @unchecked Sendable {
        var verifiedByID: [UInt32: VerifiedPeer] = [:]
        var refusedIDs: [UInt32] = []

        func verify(txt: [String: String]) async -> VerifiedPeer? {
            guard let idText = txt["id"], let id = UInt32(idText) else {
                return nil
            }
            if refusedIDs.contains(id) { return nil }
            return verifiedByID[id]
        }
    }

    private final class FakeBrowser: PeerBrowsing, @unchecked Sendable {
        var resultsChangedHandler: (([DiscoveredPeer], [String]) -> Void)? = nil
        var started = false
        var stopped = false

        func start(queue: DispatchQueue) { started = true }
        func stop() { stopped = true }

        func deliver(added: [DiscoveredPeer], removed: [String]) {
            resultsChangedHandler?(added, removed)
        }
    }

    private func makeService(verifier: FakeVerifier = FakeVerifier(),
                              browser: FakeBrowser = FakeBrowser()) -> PeerDiscoveryService {
        PeerDiscoveryService(
            config: .init(serviceName: "operator-phone", listenPort: 51999),
            verifier: verifier,
            browser: browser,
            advertiser: nil
        )
    }

    // MARK: - acceptance criteria

    /// Only verified peers appear in the nearby list.
    func testOnlyVerifiedPeersAppearInNearbyList() async {
        let v = FakeVerifier()
        v.verifiedByID[1] = peer(id: 1)
        v.verifiedByID[2] = peer(id: 2)
        v.refusedIDs = [3]
        let b = FakeBrowser()
        let s = makeService(verifier: v, browser: b)
        s.start()

        b.deliver(added: [discovered(id: 1), discovered(id: 2), discovered(id: 3)], removed: [])

        // Wait for verification to complete
        for _ in 0..<200 {
            let peers = await s.peers
            if peers.count == 2 { break }
            try? await Task.sleep(nanoseconds: 10_000_000)
        }

        let peers = await s.peers
        XCTAssertEqual(peers.count, 2)
        XCTAssertTrue(peers.contains { $0.clientID == 1 })
        XCTAssertTrue(peers.contains { $0.clientID == 2 })
        XCTAssertFalse(peers.contains { $0.clientID == 3 })
    }

    /// Refused peers never appear in the nearby list.
    func testRefusedPeersNeverAppearInNearbyList() async {
        let v = FakeVerifier()
        v.refusedIDs = [9]
        let b = FakeBrowser()
        let s = makeService(verifier: v, browser: b)
        s.start()

        b.deliver(added: [discovered(id: 9)], removed: [])

        for _ in 0..<200 {
            let peers = await s.peers
            if peers.isEmpty { break }
            try? await Task.sleep(nanoseconds: 10_000_000)
        }

        let peers = await s.peers
        XCTAssertTrue(peers.isEmpty, "a refused peer must not be published")
    }

    /// A vanished peer is removed from the nearby list within one browse cycle.
    func testVanishedPeerRemovedWithinOneBrowseCycle() async {
        let v = FakeVerifier()
        v.verifiedByID[5] = peer(id: 5)
        let b = FakeBrowser()
        let s = makeService(verifier: v, browser: b)
        s.start()

        b.deliver(added: [discovered(id: 5)], removed: [])
        for _ in 0..<200 {
            let peers = await s.peers
            if peers.count == 1 { break }
            try? await Task.sleep(nanoseconds: 10_000_000)
        }
        XCTAssertEqual((await s.peers).count, 1)

        b.deliver(added: [], removed: ["peer-5"])
        for _ in 0..<200 {
            let peers = await s.peers
            if peers.isEmpty { break }
            try? await Task.sleep(nanoseconds: 10_000_000)
        }

        let peers = await s.peers
        XCTAssertTrue(peers.isEmpty, "a vanished peer is removed from the published set")
    }
}