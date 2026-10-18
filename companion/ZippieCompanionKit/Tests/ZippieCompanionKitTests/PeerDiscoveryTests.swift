import XCTest
@testable import ZippieCompanionKit

/// Same-LAN discovery (#270). A `_zippie._tcp` advertisement is a claim from
/// anything on the wifi, so every test here is about the claim NOT turning
/// into a leg until the pairing credential behind it has answered.
final class PeerDiscoveryTests: XCTestCase {

    // MARK: - fixtures

    private static let key = String(repeating: "42", count: 32)
    private static let bundle = """
        {"client_id": 3, "key_hex": "\(key)",
         "roster": [{"id": 7, "name": "spare-android", "key_hex": "\(key)"}]}
        """

    private func pairing() throws -> PeerPairing {
        try XCTUnwrap(PeerPairing(bundleJSON: Self.bundle))
    }

    private func peer(_ id: UInt32 = 7, name: String = "spare-android",
                      host: String = "10.99.0.57") -> VerifiedPeer {
        VerifiedPeer(clientID: id, name: name, host: host, port: 51999)
    }

    /// Stands in for the Go binding. Records what it was asked so a test can
    /// tell the flow handed Go the right values, and answers as told.
    private final class FakeCrypto: PeerCrypto, @unchecked Sendable {
        var verifyResult: Result<String, Error> = .failure(Refused())
        var verifiedClaim: Int?
        var verifiedRoster: String?
        var provedEndpoint: String?

        struct Refused: Error {}

        func newChallenge() throws -> String { "c0ffee" }
        func proof(credentialJSON: String, challengeHex: String, endpoint: String) throws -> String {
            provedEndpoint = endpoint
            return "proof-for-\(challengeHex)"
        }
        func verify(rosterJSON: String, claimedID: Int, challengeHex: String,
                    proofHex: String) throws -> String {
            verifiedClaim = claimedID
            verifiedRoster = rosterJSON
            return try verifyResult.get()
        }
    }

    // MARK: - the advertisement

    func testTheServiceTypeIsTheOneInfoPlistDeclares() {
        // NSBonjourServices must list this exact string, or iOS refuses to
        // browse it and reports nothing at all.
        XCTAssertEqual(PeerDiscovery.serviceType, "_zippie._tcp")
    }

    func testTheTXTRecordCarriesOnlyTheClaimedID() {
        let txt = PeerDiscovery.txtRecord(clientID: 7)
        XCTAssertEqual(txt, ["id": "7"])
        XCTAssertEqual(PeerDiscovery.claimedClientID(in: txt), 7)
    }

    func testAMissingOrNonsenseClaimIsNoClaim() {
        XCTAssertNil(PeerDiscovery.claimedClientID(in: [:]))
        XCTAssertNil(PeerDiscovery.claimedClientID(in: ["id": "0"]))
        XCTAssertNil(PeerDiscovery.claimedClientID(in: ["id": "-4"]))
        XCTAssertNil(PeerDiscovery.claimedClientID(in: ["id": "seven"]))
        XCTAssertNil(PeerDiscovery.claimedClientID(in: ["id": "4294967296"]))
    }

    // MARK: - the pairing material

    func testAPairingBundleSplitsIntoCredentialAndRoster() throws {
        let p = try pairing()
        XCTAssertEqual(p.clientID, 3)
        let cred = try JSONSerialization.jsonObject(with: Data(p.credentialJSON.utf8)) as? [String: Any]
        XCTAssertEqual((cred?["client_id"] as? NSNumber)?.uint32Value, 3)
        XCTAssertEqual(cred?["key_hex"] as? String, Self.key)
        let roster = try JSONSerialization.jsonObject(with: Data(p.rosterJSON.utf8)) as? [[String: Any]]
        XCTAssertEqual(roster?.first?["name"] as? String, "spare-android")
    }

    func testAnUnusableBundleIsRefusedNotDefaulted() {
        XCTAssertNil(PeerPairing(bundleJSON: ""))
        XCTAssertNil(PeerPairing(bundleJSON: "{"))
        XCTAssertNil(PeerPairing(bundleJSON: #"{"client_id": 0, "key_hex": "\#(Self.key)", "roster": []}"#))
        XCTAssertNil(PeerPairing(bundleJSON: #"{"client_id": 3, "key_hex": "42", "roster": []}"#))
        XCTAssertNil(PeerPairing(bundleJSON: #"{"client_id": 3, "key_hex": "\#(Self.key)"}"#))
    }

    // MARK: - the handshake

    func testTheHandshakeMessagesRoundTrip() {
        XCTAssertEqual(PeerHandshake.challenge(in: PeerHandshake.challengeMessage("ab12")), "ab12")
        XCTAssertEqual(PeerHandshake.proof(in: PeerHandshake.proofMessage("cd34")), "cd34")
        XCTAssertNil(PeerHandshake.challenge(in: PeerHandshake.proofMessage("cd34")))
        XCTAssertNil(PeerHandshake.proof(in: Data("not json".utf8)))
        XCTAssertNil(PeerHandshake.proof(in: Data(count: PeerHandshake.maxMessageBytes + 1)))
    }

    func testAnsweringSignsTheEndpointTheRouterShouldDial() throws {
        let crypto = FakeCrypto()
        let reply = try PeerVerification.answer(PeerHandshake.challengeMessage("c0ffee"),
                                                pairing: try pairing(),
                                                endpoint: "10.99.0.12:51999", crypto: crypto)
        XCTAssertEqual(PeerHandshake.proof(in: reply), "proof-for-c0ffee")
        XCTAssertEqual(crypto.provedEndpoint, "10.99.0.12:51999")
    }

    func testAGarbledChallengeGetsNoAnswer() throws {
        let p = try pairing()
        XCTAssertThrowsError(try PeerVerification.answer(Data("hello".utf8), pairing: p,
                                                         endpoint: "10.99.0.12:51999",
                                                         crypto: FakeCrypto()))
    }

    func testVerificationAsksGoAboutTheAdvertisedClaimAgainstTheRoster() async throws {
        let crypto = FakeCrypto()
        crypto.verifyResult = .success(#"{"client_id":7,"name":"spare-android","endpoint":"10.99.0.57:51999"}"#)
        let standing = await PeerVerification.verify(claimedID: 7, pairing: try pairing(),
                                                     crypto: crypto) { _ in
            PeerHandshake.proofMessage("abcd")
        }
        XCTAssertEqual(standing, .verified(peer()))
        XCTAssertEqual(crypto.verifiedClaim, 7)
        XCTAssertEqual(crypto.verifiedRoster, try pairing().rosterJSON)
    }

    /// GO SAYS NO, SO IT IS NO. There is no fallback that trusts the claim.
    func testARefusedProofIsRefused() async throws {
        let standing = await PeerVerification.verify(claimedID: 7, pairing: try pairing(),
                                                     crypto: FakeCrypto()) { _ in
            PeerHandshake.proofMessage("abcd")
        }
        guard case .refused = standing else { return XCTFail("expected refused, got \(standing)") }
    }

    func testASilentOrBrokenPeerIsRefused() async throws {
        struct Gone: Error {}
        let silent = await PeerVerification.verify(claimedID: 7, pairing: try pairing(),
                                                   crypto: FakeCrypto()) { _ in throw Gone() }
        guard case .refused = silent else { return XCTFail("expected refused, got \(silent)") }
        let garbled = await PeerVerification.verify(claimedID: 7, pairing: try pairing(),
                                                    crypto: FakeCrypto()) { _ in Data("??".utf8) }
        guard case .refused = garbled else { return XCTFail("expected refused, got \(garbled)") }
    }

    // MARK: - the verified peer

    func testGoResultBecomesAPeerWithASplitEndpoint() {
        let p = VerifiedPeer(verifiedJSON: #"{"client_id":7,"name":"spare-android","endpoint":"10.99.0.57:51999"}"#)
        XCTAssertEqual(p, peer())
        XCTAssertNil(VerifiedPeer(verifiedJSON: #"{"client_id":7,"name":"x","endpoint":"10.99.0.57"}"#))
        XCTAssertNil(VerifiedPeer(verifiedJSON: "{}"))
    }

    /// The leg name must pass the router's slug rule or the announce is a 400
    /// and the peer can never join. Roster names are written by a person.
    func testTheLegNameIsARouterSafeSlug() {
        XCTAssertEqual(peer(name: "Spare Android").legName, "spare-android")
        XCTAssertEqual(peer(12, name: "").legName, "peer-12")
    }

    /// DELEGATED, or the router dials the announcing phone instead of the peer
    /// (it prefers the request's source address, #252).
    func testAdoptingAPeerAnnouncesItsSignedEndpointAsDelegated() {
        let cfg = peer().announcerConfig(consoleHost: "10.99.0.1:8787", token: "tok")
        XCTAssertEqual(cfg.name, "spare-android")
        XCTAssertEqual(cfg.label, "spare-android")
        XCTAssertEqual(cfg.listenPort, 51999)
        XCTAssertTrue(cfg.delegated)
    }

    // MARK: - what is offered

    func testOnlyVerifiedPeersAreOffered() {
        var r = PeerRoster()
        r.found(service: "a")
        r.found(service: "b")
        r.found(service: "c")
        r.record(service: "b", .verified(peer()))
        r.record(service: "c", .refused("bad proof"))
        XCTAssertEqual(r.offers(ownClientID: 3, legNames: [], legEndpoints: []), [peer()])
    }

    func testThisPhoneAndExistingLegsAreNotOffered() {
        var r = PeerRoster()
        r.found(service: "self")
        r.record(service: "self", .verified(peer(3, name: "me")))
        r.found(service: "already")
        r.record(service: "already", .verified(peer(7)))
        XCTAssertEqual(r.offers(ownClientID: 3, legNames: ["spare-android"], legEndpoints: []), [])
    }

    /// A peer that already announces ITSELF is a leg under its own name. The
    /// endpoint is what identifies it; offering it again under the roster name
    /// would put two legs on one device.
    func testAPeerAlreadyCarryingUnderAnotherNameIsNotOffered() {
        var r = PeerRoster()
        r.found(service: "b")
        r.record(service: "b", .verified(peer()))
        XCTAssertEqual(r.offers(ownClientID: 3, legNames: ["pixel-ea83"],
                                legEndpoints: ["10.99.0.57:51999"]), [])
    }

    func testALostServiceIsNoLongerOffered() {
        var r = PeerRoster()
        r.found(service: "b")
        r.record(service: "b", .verified(peer()))
        r.lost(service: "b")
        XCTAssertEqual(r.offers(ownClientID: 3, legNames: [], legEndpoints: []), [])
    }

    /// A late verification for a service that has gone must not resurrect it.
    func testARecordForAnUnknownServiceIsIgnored() {
        var r = PeerRoster()
        r.record(service: "ghost", .verified(peer()))
        XCTAssertEqual(r.offers(ownClientID: 3, legNames: [], legEndpoints: []), [])
    }

    /// An ADOPTED peer stops being offered (it is a leg now) but must still
    /// read as verified, or the phone would stop renewing its lease the moment
    /// the router listed it.
    func testAnAdoptedPeerIsStillVerifiedAfterItStopsBeingOffered() {
        var r = PeerRoster()
        r.found(service: "b")
        r.record(service: "b", .verified(peer()))
        XCTAssertEqual(r.offers(ownClientID: 3, legNames: ["spare-android"], legEndpoints: []), [])
        XCTAssertEqual(r.verifiedPeers, [peer()])
        r.found(service: "c")
        r.record(service: "c", .refused("no"))
        XCTAssertEqual(r.refusedCount, 1)
    }

    func testOnePeerAdvertisedTwiceIsOfferedOnce() {
        var r = PeerRoster()
        for s in ["wifi", "ethernet"] {
            r.found(service: s)
            r.record(service: s, .verified(peer()))
        }
        XCTAssertEqual(r.offers(ownClientID: 3, legNames: [], legEndpoints: []).count, 1)
    }

    // MARK: - keeping adopted legs in step

    /// Stopping withdraws the leg by name, and that withdraw races the new
    /// announcer's first announce of the SAME name: if it lands last, the leg
    /// is gone until the next renewal.
    func testAPeerThatMovedIsRepointedNotWithdrawnAndAnnouncedAgain() {
        let moved = peer(host: "10.99.0.58")
        let plan = AdoptionPlan(running: [7: peer()], desired: [7: moved])
        XCTAssertEqual(plan.repoint, [7: moved])
        XCTAssertEqual(plan.stop, [])
        XCTAssertEqual(plan.start, [:])
    }

    func testAdoptionsStartStopAndAreLeftAloneExactlyWhenTheyShould() {
        let nine = peer(9, name: "tablet")
        let plan = AdoptionPlan(running: [7: peer(), 8: peer(8, name: "old")],
                                desired: [7: peer(), 9: nine])
        XCTAssertEqual(plan.stop, [8])
        XCTAssertEqual(plan.start, [9: nine])
        XCTAssertEqual(plan.repoint, [:])
        XCTAssertFalse(plan.announcesNothing)
        XCTAssertTrue(AdoptionPlan(running: [7: peer()], desired: [7: peer()]).announcesNothing)
    }
}
