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
        var resultsChangedHandler: (([DiscoveredPeer], [String]) -> Void)?
        func start(queue: DispatchQueue) {}
        func stop() {}
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
        let s = makeService(verifier: v)

        await s.deliver(added: [discovered(id: 1), discovered(id: 2), discovered(id: 3)], removed: [])

        let peers = await s.peers
        XCTAssertEqual(peers.map(\.clientID), [1, 2])
    }

    /// Refused peers never appear in the nearby list.
    func testRefusedPeersNeverAppearInNearbyList() async {
        let v = FakeVerifier()
        v.refusedIDs = [9]
        let s = makeService(verifier: v)

        await s.deliver(added: [discovered(id: 9)], removed: [])

        let peers = await s.peers
        XCTAssertTrue(peers.isEmpty, "a refused peer must not be published")
    }

    /// A vanished peer is removed from the nearby list within one browse cycle.
    func testVanishedPeerRemovedWithinOneBrowseCycle() async {
        let v = FakeVerifier()
        v.verifiedByID[5] = peer(id: 5)
        let s = makeService(verifier: v)

        await s.deliver(added: [discovered(id: 5)], removed: [])
        let before = await s.peers
        XCTAssertEqual(before.count, 1)

        await s.deliver(added: [], removed: ["peer-5"])

        let after = await s.peers
        XCTAssertTrue(after.isEmpty, "a vanished peer is removed from the published set")
    }

    /// Choosing a peer creates the same leg config a manual edit would.
    func testChoosingAPeerYieldsTheManualEditLegConfig() {
        let chosen = peer(id: 7)

        var manual = LegEdit()
        manual.set(.label, .text("Nearby peer 7"))
        manual.set(.enabled, .flag(true))

        XCTAssertEqual(NearbyLeg.edit(for: chosen), manual)
        XCTAssertTrue(LegName.isValid(NearbyLeg.name(for: chosen)))
    }
}
