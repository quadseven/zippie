import XCTest
@testable import ZippieCompanionKit

/// Telling a leg held in RESERVE apart from a leg that has FAILED.
///
/// Both report no traffic. One is working exactly as configured - a cheap SIM
/// kept for the day everything else dies - and the other needs attention.
/// Drawing them the same trains the reader to ignore the one signal on the
/// status screen that matters.
final class ReserveTierTests: XCTestCase {

    private func path(_ name: String, tier: Int, weight: Int,
                      maxKbps: Int? = nil) throws -> BondStatus.Path {
        var obj: [String: Any] = [
            "name": name, "state": weight > 0 ? "up" : "down",
            "effective_weight": weight, "interface": "eth0", "tier": tier,
        ]
        if let maxKbps { obj["max_kbps"] = maxKbps }
        let data = try JSONSerialization.data(withJSONObject: obj)
        return try JSONDecoder().decode(BondStatus.Path.self, from: data)
    }

    private func bond(_ paths: [BondStatus.Path]) throws -> BondStatus {
        let data = try JSONSerialization.data(withJSONObject: [
            "paths": try paths.map { p -> [String: Any] in
                let d = try JSONEncoder().encode(EncodableProxy(p))
                return (try JSONSerialization.jsonObject(with: d) as? [String: Any]) ?? [:]
            }
        ])
        return try JSONDecoder().decode(BondStatus.self, from: data)
    }

    /// The everyday case: tier 1 is carrying, so the cheap tier-3 SIM is in
    /// reserve rather than broken.
    func testACheapSimIsReserveWhileTheGoodLinksCarry() throws {
        let verizon = try path("verizon", tier: 1, weight: 100)
        let att = try path("att", tier: 3, weight: 0, maxKbps: 500)

        let active = 1
        XCTAssertFalse(verizon.isHeldInReserve(activeTier: active))
        XCTAssertTrue(att.isHeldInReserve(activeTier: active),
                      "the reserve SIM reads as broken rather than withheld")
    }

    /// THE SUBTLE ONE. If the bond has FALLEN to tier 2, a tier-2 leg is live
    /// and only tier 3 is still reserve. Comparing against tier 1 rather than
    /// the ACTIVE tier would label a carrying leg as held back.
    func testReserveIsRelativeToTheActiveTierNotToTierOne() throws {
        let starlink = try path("starlink", tier: 2, weight: 100)
        let att = try path("att", tier: 3, weight: 0)

        let active = 2   // tier 1 is gone; the bond has fallen to tier 2
        XCTAssertFalse(starlink.isHeldInReserve(activeTier: active),
                       "a leg that is actually carrying was labelled reserve")
        XCTAssertTrue(att.isHeldInReserve(activeTier: active))
    }

    /// When NOTHING carries there is no active tier, and nothing may be called
    /// reserve - that state is a total outage, which is far worse than
    /// "running on a lower tier" and must not be dressed up as deliberate.
    func testNothingCarryingMeansNothingIsReserve() throws {
        let a = try path("verizon", tier: 1, weight: 0)
        let b = try path("att", tier: 3, weight: 0)

        let active: Int? = nil
        XCTAssertFalse(a.isHeldInReserve(activeTier: active))
        XCTAssertFalse(b.isHeldInReserve(activeTier: active),
                       "a total outage was presented as a leg held in reserve")
    }

    /// A router too old to publish tier must not have legs invented as reserve.
    func testAPathWithNoTierIsNeverReserve() throws {
        let data = try JSONSerialization.data(withJSONObject: [
            "name": "old", "state": "down", "effective_weight": 0, "interface": "eth0",
        ])
        let p = try JSONDecoder().decode(BondStatus.Path.self, from: data)
        XCTAssertFalse(p.isHeldInReserve(activeTier: 1))
    }

    func testTheDeliberateCapDecodes() throws {
        let att = try path("att", tier: 3, weight: 0, maxKbps: 500)
        XCTAssertEqual(att.maxKbps, 500,
                       "the cap did not decode, so a throttled leg would read "
                     + "as merely slow")
    }
}

/// Minimal re-encoder so a decoded Path can be put back into a BondStatus for
/// the activeTier tests. Only the fields those tests read.
private struct EncodableProxy: Encodable {
    let name: String?
    let state: String?
    let effectiveWeight: Int?
    let tier: Int?
    let interface: String?

    init(_ p: BondStatus.Path) {
        name = p.name; state = p.state; effectiveWeight = p.effectiveWeight
        tier = p.tier; interface = p.interface
    }

    enum CodingKeys: String, CodingKey {
        case name, state, tier, interface
        case effectiveWeight = "effective_weight"
    }
}

// MARK: - membership is not weight

extension ReserveTierTests {

    private func decode(_ obj: [String: Any]) throws -> BondStatus.Path {
        let data = try JSONSerialization.data(withJSONObject: obj)
        return try JSONDecoder().decode(BondStatus.Path.self, from: data)
    }

    /// THE BUG THIS CLOSES. A tier-gated leg keeps the weight the policy last
    /// computed and carries nothing. Reading "carrying" from weight showed
    /// four legs carrying while the transport held exactly one.
    func testWeightWithoutMembershipIsNotCarrying() throws {
        let p = try decode(["name": "ethernet", "state": "degraded",
                            "effective_weight": 40, "interface": "eth0",
                            "in_bond": false])
        XCTAssertFalse(p.isCarrying,
                       "a leg with weight but no transport link read as carrying")
        XCTAssertEqual(p.stateWord, "not in the bond")
    }

    func testWeightWithMembershipIsCarrying() throws {
        let p = try decode(["name": "hotspot", "state": "up",
                            "effective_weight": 8, "interface": "apclix0",
                            "in_bond": true])
        XCTAssertTrue(p.isCarrying)
        XCTAssertEqual(p.stateWord, "carrying")
    }

    /// Carrying AND degraded is a real, common state - it is what the M2000
    /// does - and collapsing it to either word alone loses the point.
    func testCarryingButDegradedSaysBoth() throws {
        let p = try decode(["name": "hotspot", "state": "degraded",
                            "effective_weight": 8, "interface": "apclix0",
                            "in_bond": true])
        XCTAssertEqual(p.stateWord, "carrying, degraded")
    }

    /// A router too old to publish membership must keep the previous
    /// behaviour rather than reading every leg as excluded.
    func testAnOlderRouterFallsBackToWeight() throws {
        let p = try decode(["name": "hotspot", "state": "up",
                            "effective_weight": 8, "interface": "apclix0"])
        XCTAssertTrue(p.isCarrying, "membership absent was treated as excluded")
    }

    func testAZeroWeightLegIsNeverCarryingEvenIfInBond() throws {
        let p = try decode(["name": "att", "state": "up", "effective_weight": 0,
                            "interface": "eth0", "in_bond": true])
        XCTAssertFalse(p.isCarrying)
        XCTAssertEqual(p.stateWord, "up, not carrying")
    }
}

// MARK: - which byte counters are real

extension ReserveTierTests {

    /// PACKET MODE ZEROES THE OBVIOUS COUNTERS. tx_bytes/rx_bytes come from
    /// /sys/class/net/<wg_iface>, and packet mode has no per-leg wg interface,
    /// so they are hard zero however much a leg has carried. Reading only
    /// those drew a leg that had moved 30 MB as an EMPTY bar - the "healthy
    /// but carrying nothing" lie this app exists to expose, told backwards.
    func testPacketModeUsesTheTransportsLinkCounters() throws {
        let data = try JSONSerialization.data(withJSONObject: [
            "name": "hotspot", "state": "up", "effective_weight": 128,
            "interface": "apclix0", "in_bond": true,
            "tx_bytes": 0, "rx_bytes": 0,
            "link_tx_bytes": 11018643, "link_rx_bytes": 31703754,
        ])
        let p = try JSONDecoder().decode(BondStatus.Path.self, from: data)

        XCTAssertEqual(p.carriedTx, 11018643,
                       "the leg reads as having sent nothing while the transport "
                     + "counted 11 MB")
        XCTAssertEqual(p.carriedRx, 31703754)
    }

    /// Route mode has no link counters, and must keep working.
    func testRouteModeFallsBackToTheInterfaceCounters() throws {
        let data = try JSONSerialization.data(withJSONObject: [
            "name": "ethernet", "state": "up", "effective_weight": 100,
            "interface": "eth0", "tx_bytes": 5000, "rx_bytes": 9000,
        ])
        let p = try JSONDecoder().decode(BondStatus.Path.self, from: data)
        XCTAssertEqual(p.carriedTx, 5000)
        XCTAssertEqual(p.carriedRx, 9000)
    }

    func testALegThatHasCarriedNothingReadsAsZeroNotAsMissing() throws {
        let data = try JSONSerialization.data(withJSONObject: [
            "name": "att", "state": "down", "effective_weight": 0, "interface": "eth1",
        ])
        let p = try JSONDecoder().decode(BondStatus.Path.self, from: data)
        XCTAssertEqual(p.carriedTx, 0)
        XCTAssertEqual(p.carriedRx, 0)
    }
}
