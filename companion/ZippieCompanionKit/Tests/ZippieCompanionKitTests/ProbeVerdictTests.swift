import XCTest
@testable import ZippieCompanionKit

/// The verdict logic is the part that decides whether the whole companion
/// design is viable, so it is tested without a phone, a radio, or a network.
final class ProbeVerdictTests: XCTestCase {

    func testDifferentEgressProvesCellularBinding() {
        let v = ProbeEvaluator.evaluate(
            baseline: .success("203.0.113.33"),
            cellular: .success("172.56.166.35")
        )
        XCTAssertTrue(v.isProven)
        XCTAssertEqual(v, .proven(wifi: "203.0.113.33", cellular: "172.56.166.35"))
    }

    func testSameEgressIsInconclusiveNotFailure() {
        // The false negative that matters: a phone tethered to the very router
        // under test shares its egress, so identical addresses do NOT prove the
        // binding was ignored. Calling this a failure would send us chasing a
        // bug that is not there.
        let v = ProbeEvaluator.evaluate(
            baseline: .success("203.0.113.33"),
            cellular: .success("203.0.113.33")
        )
        XCTAssertFalse(v.isProven)
        XCTAssertEqual(v, .inconclusiveSameEgress(address: "203.0.113.33"))
        XCTAssertTrue(v.summary.contains("share a NAT"))
    }

    func testWhitespaceOnlyDifferenceIsNotAProof() {
        // A trailing newline from the echo endpoint must never read as a
        // different egress. A false PROVEN is far worse than a false
        // inconclusive - it would have us build on a premise that is untrue.
        let v = ProbeEvaluator.evaluate(
            baseline: .success("203.0.113.33\n"),
            cellular: .success("  203.0.113.33  ")
        )
        XCTAssertFalse(v.isProven)
        XCTAssertEqual(v, .inconclusiveSameEgress(address: "203.0.113.33"))
    }

    func testIPv6CasingIsNotADifferentEgress() {
        let v = ProbeEvaluator.evaluate(
            baseline: .success("2601:DB8::1"),
            cellular: .success("2601:db8::1")
        )
        XCTAssertFalse(v.isProven)
    }

    func testUnavailableCellularIsDistinctFromInconclusive() {
        // "cellular is switched off" and "the result was ambiguous" lead to
        // different next actions, so they must not collapse into one state.
        let v = ProbeEvaluator.evaluate(
            baseline: .success("203.0.113.33"),
            cellular: .failure(.noInterfaceAvailable)
        )
        XCTAssertEqual(v, .cellularUnavailable(reason: "no matching interface available"))
    }

    func testBaselineFailureShortCircuits() {
        // With no baseline there is nothing to compare against, so the cellular
        // result is irrelevant - even a successful one must not read as proof.
        let v = ProbeEvaluator.evaluate(
            baseline: .failure(.timedOut(seconds: 10)),
            cellular: .success("172.56.166.35")
        )
        XCTAssertEqual(v, .baselineFailed(reason: "timed out after 10s"))
        XCTAssertFalse(v.isProven)
    }

    func testHTTPBodySplitsOnCRLF() {
        let raw = "HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\n172.56.166.35\n"
        XCTAssertEqual(CellularProbe.httpBody(of: raw)?.trimmed(), "172.56.166.35")
    }

    func testHTTPBodyToleratesBareLF() {
        let raw = "HTTP/1.1 200 OK\nContent-Type: text/plain\n\n172.56.166.35"
        XCTAssertEqual(CellularProbe.httpBody(of: raw)?.trimmed(), "172.56.166.35")
    }

    func testHTTPBodyMissingSeparatorIsNil() {
        XCTAssertNil(CellularProbe.httpBody(of: "HTTP/1.1 200 OK no body here"))
    }
}

// MARK: - the false positive that actually happened

final class PrivateRelayGuardTests: XCTestCase {
    /// Two /31s lifted from Apple's real published egress list - the exact
    /// ranges the two v1 addresses fell into.
    private let ranges = PrivateRelayRanges(csv: """
    146.75.245.46/31,US,US-NY,ALBANY,
    146.75.245.72/31,US,US-NY,LIVERPOOL,
    """)

    func testTheExactV1FalsePositiveIsNowCaught() {
        // v1 reported PROVEN from these. Both are Private Relay exits, so the
        // difference measured Apple's exit choice, not the radio.
        let v = ProbeEvaluator.evaluate(
            baseline: .success("146.75.245.47"),
            cellular: .success("146.75.245.73"),
            relayRanges: ranges
        )
        XCTAssertFalse(v.isProven, "this pair must never read as proof again")
        XCTAssertEqual(v, .maskedByPrivateRelay(wifi: "146.75.245.47",
                                                cellular: "146.75.245.73"))
        XCTAssertTrue(v.summary.contains("Disable Private Relay"))
    }

    func testOneRelayExitIsEnoughToWithholdProof() {
        // Only the cellular side masked. Still not proof: we cannot tell how
        // much of the difference is the relay.
        let v = ProbeEvaluator.evaluate(
            baseline: .success("203.0.113.33"),
            cellular: .success("146.75.245.73"),
            relayRanges: ranges
        )
        XCTAssertFalse(v.isProven)
    }

    func testGenuinelyDifferentCarriersStillProve() {
        // A real home IP and a real T-Mobile CGNAT address, neither masked.
        let v = ProbeEvaluator.evaluate(
            baseline: .success("203.0.113.33"),
            cellular: .success("172.56.166.35"),
            relayRanges: ranges
        )
        XCTAssertTrue(v.isProven)
    }

    func testSharedV24WithheldEvenWithNoRangeList() {
        // Offline fallback: if the published list could not be fetched, two
        // addresses in one /24 are still not two carriers.
        let v = ProbeEvaluator.evaluate(
            baseline: .success("146.75.245.47"),
            cellular: .success("146.75.245.73"),
            relayRanges: nil
        )
        XCTAssertFalse(v.isProven, "must withhold proof without the list too")
    }

    func testDifferentV24RealCarriersUnaffectedByTheFallback() {
        let v = ProbeEvaluator.evaluate(
            baseline: .success("203.0.113.33"),
            cellular: .success("172.56.166.35"),
            relayRanges: nil
        )
        XCTAssertTrue(v.isProven)
    }

    func testCidrParsingHandlesTheRealListFormat() {
        XCTAssertTrue(ranges.contains("146.75.245.46"))
        XCTAssertTrue(ranges.contains("146.75.245.47"))
        XCTAssertFalse(ranges.contains("146.75.245.48"), "/31 is exactly two addresses")
        XCTAssertFalse(ranges.contains("8.8.8.8"))
        XCTAssertEqual(ranges.count, 2)
    }

    func testMalformedInputDoesNotCrashOrMatch() {
        let r = PrivateRelayRanges(csv: "not-a-cidr\n1.2.3.4/99,X\n\n5.6.7.8/24,US")
        XCTAssertFalse(r.contains("nonsense"))
        XCTAssertTrue(r.contains("5.6.7.9"))
    }
}

final class BondStatusTests: XCTestCase {
    /// Shape lifted from the router's real /api/status response.
    private let json = """
    {"mode":"aggregate","datapath":"route","primary":"hotspot","paths":[
      {"name":"ethernet","state":"down","effective_weight":0,"rtt_ms":null,"loss_pct":100.0},
      {"name":"hotspot","state":"up","effective_weight":112,"rtt_ms":61.9,"loss_pct":0.0},
      {"name":"dongle4g","state":"degraded","effective_weight":24,"rtt_ms":null,"loss_pct":0.0}]}
    """.data(using: .utf8)!

    func testDecodesTheRealConsoleShape() throws {
        let s = try JSONDecoder().decode(BondStatus.self, from: json)
        XCTAssertEqual(s.primary, "hotspot")
        XCTAssertEqual(s.datapath, "route")
        XCTAssertEqual(s.totalCount, 3)
    }

    func testCarryingCountsWeightNotState() throws {
        // dongle4g is "degraded" but weight 24, so it IS carrying; ethernet is
        // weight 0 so it is not. Counting `state == up` would report 1 and
        // contradict the router.
        let s = try JSONDecoder().decode(BondStatus.self, from: json)
        XCTAssertEqual(s.carryingCount, 2)
    }

    func testAnUnknownOrRenamedFieldDoesNotCrash() throws {
        let odd = #"{"mode":"aggregate","brand_new_field":42,"paths":[{"name":"x"}]}"#
        let s = try JSONDecoder().decode(BondStatus.self, from: Data(odd.utf8))
        XCTAssertEqual(s.totalCount, 1)
        XCTAssertEqual(s.carryingCount, 0)
        XCTAssertNil(s.primary)
    }
}
