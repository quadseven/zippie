import XCTest
@testable import ZippieCompanionKit

/// The router's bond view, as a widget is allowed to use it (#244).
///
/// Two separate claims are under test here, and both are ones a widget gets
/// wrong by default:
///
///  1. A BOND SNAPSHOT THAT HAS GONE STALE must stop describing the bond. It
///     is fetched on the app's foreground poll, not on WidgetKit's schedule,
///     so a launcher redraw with the app in the background is the normal case
///     and holding the last good leg list across it would be a home screen
///     claiming to know something nobody has looked at for minutes.
///  2. The leg list must keep `weightIsNotMembership` and `reserveIsNotBroken`
///     apart. A leg held in reserve reports no traffic BECAUSE THAT IS ITS
///     JOB, and a leg with weight the transport does not hold is not carrying;
///     collapse either into "down" and the reader is trained to ignore the one
///     row that means something.
final class WidgetBondTests: XCTestCase {

    private let now = Date(timeIntervalSince1970: 2_000_000)

    // MARK: - helpers

    /// A router path with only the fields a test cares about spelled out, so
    /// every default here is a deliberate "the router said nothing" rather than
    /// an accident of a shared fixture.
    private func path(name: String,
                      label: String? = nil,
                      weight: Int? = 0,
                      tier: Int? = 1,
                      state: String? = "up",
                      inBond: Bool? = nil,
                      iface: String? = "br-lan") -> [String: Any] {
        var p: [String: Any] = ["name": name, "interface": iface ?? NSNull()]
        if let label { p["label"] = label }
        if let state { p["state"] = state }
        if let weight { p["effective_weight"] = weight }
        if let tier { p["tier"] = tier }
        if let inBond { p["in_bond"] = inBond }
        return p
    }

    private func bond(_ paths: [[String: Any]]) throws -> BondStatus {
        try JSONDecoder().decode(
            BondStatus.self,
            from: try JSONSerialization.data(withJSONObject: ["paths": paths]))
    }

    private func snapshot(_ paths: [[String: Any]],
                          age: TimeInterval) throws -> WidgetBondSnapshot {
        WidgetBondSnapshot(status: try bond(paths), updatedAt: now.addingTimeInterval(-age))
    }

    /// A relay report good enough that the verdict is not one of the
    /// short-circuiting ones, so the leg list is reached at all.
    private func carryingReport(age: TimeInterval = 0) -> RelayStatus {
        var stats = CellularRelay.Stats()
        stats.cellularReady = true
        stats.upDatagrams = 12
        stats.lastRouterInboundAt = now.addingTimeInterval(-1)
        return RelayStatus(stats: stats, updatedAt: now.addingTimeInterval(-age))
    }

    // MARK: - the single most important behaviour

    /// THE TEST THAT MUST FAIL IF THE WIDGET KEEPS SHOWING A STALE BOND.
    ///
    /// A leg list that was genuinely correct a while ago, read past
    /// `WidgetBondSnapshot.stalenessThreshold`, must render as the this-phone
    /// fallback row - never as the legs the snapshot happened to be holding.
    ///
    /// CONFIRMED BY BREAKING IT: short-circuiting `legRows`' staleness check
    /// (evaluating against `bond.updatedAt` instead of the caller's `now`, the
    /// exact bug this guards against) turns this red - four router legs, none
    /// of them "This phone" - before the check was restored.
    func testAStaleBondSnapshotRendersTheThisPhoneRowNotTheLegsItWasHolding() throws {
        let stale = try snapshot([
            path(name: "lte", label: "Lone SIM", weight: 100),
            path(name: "wifi", label: "Cafe wifi", weight: 50),
        ], age: WidgetBondSnapshot.stalenessThreshold + 1)

        let content = WidgetTimeline.build(report: carryingReport(),
                                           now: now,
                                           bond: stale)

        XCTAssertEqual(content.legs, [WidgetLeg(label: "This phone", state: .carrying)],
                       "a widget cannot describe a bond nobody has looked at for longer than "
                       + "the threshold; it must fall back to the row it can still vouch for")
        XCTAssertEqual(content.headline, "Carrying",
                       "the relay report is a separate record with its own age, and it is "
                     + "still fresh here - the headline must not be dragged down by the bond")
    }

    /// Just inside the threshold, the same snapshot is used.
    func testAFreshBondSnapshotJustInsideTheThresholdRendersTheLegs() throws {
        let fresh = try snapshot([path(name: "lte", label: "Lone SIM", weight: 100)],
                                 age: WidgetBondSnapshot.stalenessThreshold - 0.5)

        let content = WidgetTimeline.build(report: carryingReport(), now: now, bond: fresh)

        XCTAssertEqual(content.legs, [WidgetLeg(label: "Lone SIM", state: .carrying)])
    }

    /// No snapshot at all is the same answer as a stale one - the fallback is
    /// the absence of evidence, not a penalty for having stale evidence.
    func testNoBondSnapshotFallsBackToTheThisPhoneRow() {
        let content = WidgetTimeline.build(report: carryingReport(), now: now, bond: nil)
        XCTAssertEqual(content.legs, [WidgetLeg(label: "This phone", state: .carrying)])
    }

    /// A snapshot the router answered with no `paths` key is NOT an empty bond
    /// and is not a licence to describe anything. Rule 4: unmeasured is absent.
    func testAFreshSnapshotWithNoPathsDrawsNoLegs() throws {
        let empty = WidgetBondSnapshot(
            status: try JSONDecoder().decode(
                BondStatus.self,
                from: try JSONSerialization.data(withJSONObject: ["mode": "client"])),
            updatedAt: now)

        let content = WidgetTimeline.build(report: carryingReport(), now: now, bond: empty)
        XCTAssertEqual(content.legs, [WidgetLeg(label: "This phone", state: .carrying)])
    }

    // MARK: - weightIsNotMembership

    /// `weightIsNotMembership`: "a leg with weight that the transport does not
    /// hold is NOT carrying". `BondStatus.Path.isCarrying` already encodes it,
    /// and `ReserveTierTests` pins the getter - what this pins is that the
    /// WIDGET reads that getter rather than reaching for `effectiveWeight`
    /// itself, which is the change that produced four legs drawn as carrying
    /// while the transport held exactly one.
    func testALegWithWeightButNoMembershipIsNotDrawnCarrying() throws {
        let rows = WidgetTimeline.bondLegRows(try bond([
            path(name: "lte", label: "Lone SIM", weight: 100, inBond: false),
        ]))

        XCTAssertEqual(rows, [WidgetLeg(label: "Lone SIM", state: .notInBond)])
    }

    /// The same leg, at weight 0 and up - the anti-flap gate holding a
    /// recovered leg out while it proves itself. Present, not carrying, and
    /// emphatically not a fault.
    func testALegAtWeightZeroIsPresentNotCarryingAndNotDown() throws {
        let rows = WidgetTimeline.bondLegRows(try bond([
            path(name: "lte", label: "Lone SIM", weight: 0, inBond: true),
        ]))

        XCTAssertEqual(rows, [WidgetLeg(label: "Lone SIM", state: .presentNotCarrying)])
        let state = try XCTUnwrap(rows.first?.state)
        XCTAssertNotEqual(state, .carrying,
                          "weight 0 is not carrying - drawing it as such would contradict "
                        + "the router's own console")
        XCTAssertNotEqual(state, .down,
                          "a leg the gate is holding back has not failed")
    }

    /// `state == "up"` alone is not carrying either, which is the other half of
    /// the same rule.
    func testALegThatIsUpWithWeightIsPresentNotCarrying() throws {
        let rows = WidgetTimeline.bondLegRows(try bond([
            path(name: "wifi", label: "Cafe wifi", weight: 0, state: "up"),
        ]))
        XCTAssertEqual(rows, [WidgetLeg(label: "Cafe wifi", state: .presentNotCarrying)])
    }

    // MARK: - reserveIsNotBroken

    /// `reserveIsNotBroken`: "a leg held back by the tier gate is doing its job
    /// and must not be drawn as a failure". This is the AC's headline case and
    /// the one a two-state enum cannot express.
    func testAReserveLegIsDrawnReserveNotFailure() throws {
        let rows = WidgetTimeline.bondLegRows(try bond([
            path(name: "lte", label: "Lone SIM", weight: 0, tier: 3, state: "up"),
            path(name: "wifi", label: "Cafe wifi", weight: 100, tier: 1, state: "up"),
        ]))

        XCTAssertEqual(rows.first?.state, .reserve)
        XCTAssertNotEqual(rows.first?.state, WidgetLeg.State.down,
                          "a tier-3 leg with no traffic is doing its job; drawing it as a "
                        + "failure trains the reader to ignore the one row that means something")
    }

    /// Decided against the ACTIVE tier, not against tier 1. With the bond
    /// fallen to tier 2, a tier-2 leg is live and only tier 3 is in reserve.
    func testReserveIsDecidedAgainstTheActiveTierNotTierOne() throws {
        let rows = WidgetTimeline.bondLegRows(try bond([
            path(name: "lte", label: "Lone SIM", weight: 0, tier: 3, state: "up"),
            path(name: "wifi", label: "Cafe wifi", weight: 100, tier: 2, state: "up"),
        ]))

        XCTAssertEqual(rows[0].state, .reserve, "tier 3 above an active tier 2")
        XCTAssertEqual(rows[1].state, .carrying, "the active tier is live")
    }

    /// A bond where nothing carries has no active tier, and a total outage can
    /// never be dressed up as a deliberate gate.
    func testNothingCarryingMeansNothingIsInReserve() throws {
        let rows = WidgetTimeline.bondLegRows(try bond([
            path(name: "lte", label: "Lone SIM", weight: 0, tier: 3, state: "down"),
        ]))

        XCTAssertEqual(rows, [WidgetLeg(label: "Lone SIM", state: .down)],
                       "with no carrying leg there is no active tier to gate against, and a "
                     + "down leg is down however its tier is numbered")
    }

    // MARK: - shape and vocabulary

    /// The word is decided in the Kit, never in a SwiftUI body - see
    /// `WidgetLeg.word`.
    func testEachStateCarriesItsOwnWord() {
        XCTAssertEqual(WidgetLeg(label: "a", state: .carrying).word, "carrying")
        XCTAssertEqual(WidgetLeg(label: "a", state: .presentNotCarrying).word, "up, not carrying")
        XCTAssertEqual(WidgetLeg(label: "a", state: .notInBond).word, "not in the bond")
        XCTAssertEqual(WidgetLeg(label: "a", state: .reserve).word, "held in reserve")
        XCTAssertEqual(WidgetLeg(label: "a", state: .down).word, "down")
        XCTAssertEqual(WidgetLeg(label: "a", state: .idle).word, "not carrying")
    }

    /// A leg the router named nothing is still a row. Dropping it would make an
    /// unnamed leg look like a rendering fault; an empty section would violate
    /// rule 4.
    func testALegWithNoNameStillDrawsARow() throws {
        let rows = WidgetTimeline.bondLegRows(try bond([path(name: "")]))
        XCTAssertEqual(rows, [WidgetLeg(label: "Unnamed leg", state: .presentNotCarrying)])
    }

    /// Configuration, not a connection. A path with neither an interface nor a
    /// relay endpoint is filtered out of the bond by the app's own `Leg`
    /// builder, and padding the widget's list with it would be inventing rows.
    func testAPathWithNoInterfaceAndNoRelayEndpointIsNotDrawn() throws {
        let rows = WidgetTimeline.bondLegRows(try bond([
            path(name: "unplugged", label: "Spare dongle", iface: nil),
            path(name: "lte", label: "Lone SIM", weight: 100),
        ]))

        XCTAssertEqual(rows.map(\.label), ["Lone SIM"])
    }

    /// The round trip through the app group must preserve everything the leg
    /// list reads, and the timestamp with it - a snapshot that decoded back
    /// without its age would read as infinitely fresh.
    func testTheSnapshotSurvivesTheAppGroupRoundTrip() throws {
        let suite = "WidgetBondTests.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }

        let status = try bond([path(name: "lte", label: "Lone SIM", weight: 100, tier: 2)])
        WidgetBondStore.write(status, to: defaults, at: now)

        let read = try XCTUnwrap(WidgetBondStore.read(from: defaults))
        XCTAssertEqual(read.status.paths?.count, 1)
        XCTAssertEqual(read.status.paths?.first?.label, "Lone SIM")
        XCTAssertEqual(read.status.paths?.first?.effectiveWeight, 100)
        XCTAssertEqual(read.status.activeTier, 2, "tier drives the reserve decision; a round "
                     + "trip that lost it would silently change which legs read as reserve")
        XCTAssertEqual(read.updatedAt.timeIntervalSince1970,
                       now.timeIntervalSince1970, accuracy: 0.001)

        WidgetBondStore.clear(from: defaults)
        XCTAssertNil(WidgetBondStore.read(from: defaults),
                     "cleared means absent, matching RelayStatusStore.clear - a snapshot "
                   + "left behind would keep describing a bond nobody can reach")
    }
}
