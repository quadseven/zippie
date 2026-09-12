import XCTest
@testable import ZippieCompanionKit

/// #65: `ClientConfig.rebuildPlan(from:to:)` is the pure decision behind
/// re-pinning a live client tunnel - given the legs the datapath was last
/// told about and what `repinned(using:)` says should exist now, which
/// `pathID`s to drop and which links to (re)attach. Everything about
/// simulating a live Ethernet insertion or removal is just two `[Link]`
/// literals here; nothing in this file touches `NWPathMonitor` or the
/// datapath itself.
final class LegRebuildPlanTests: XCTestCase {

    private let wifi = ClientConfig.Link(pathID: 0, name: "wifi", device: "en0", weight: 10)
    private let cell = ClientConfig.Link(pathID: 1, name: "cell", device: "pdp_ip0", weight: 10)

    func testNothingChangedMeansNothingToDo() {
        let plan = ClientConfig.rebuildPlan(from: [wifi, cell], to: [wifi, cell])
        XCTAssertTrue(plan.isEmpty)
    }

    /// The Ethernet-insertion scenario: a new leg with a pathID that was
    /// never configured before did not exist a moment ago and does now.
    func testANewlyResolvedLegIsAddedNotRemoved() {
        let ethernet = ClientConfig.Link(pathID: 2, name: "spare", device: "en2", weight: 10)
        let plan = ClientConfig.rebuildPlan(from: [wifi], to: [wifi, ethernet])
        XCTAssertEqual(plan.toAdd, [ethernet])
        XCTAssertEqual(plan.toRemove, [])
    }

    /// The Ethernet-removal scenario: a role that no longer resolves drops
    /// its pathID out of `repinned(using:)`'s output entirely - see
    /// `ClientConfig.repinned`, which never leaves a leg unpinned.
    func testALegThatNoLongerResolvesIsRemoved() {
        let plan = ClientConfig.rebuildPlan(from: [wifi, cell], to: [wifi])
        XCTAssertEqual(plan.toRemove, [1])
        XCTAssertEqual(plan.toAdd, [])
    }

    /// The same pathID, a different device: cellular came up on a new
    /// context number (`pdp_ip0` -> `pdp_ip1`). The old socket is bound to a
    /// name that no longer means anything, so this must be an add (the Go
    /// side's `AddLink` replaces the existing pathID's socket), not a no-op.
    func testAPathIDThatMovedToADifferentDeviceIsReAdded() {
        let movedCell = ClientConfig.Link(pathID: 1, name: "cell", device: "pdp_ip1", weight: 10)
        let plan = ClientConfig.rebuildPlan(from: [wifi, cell], to: [wifi, movedCell])
        XCTAssertEqual(plan.toAdd, [movedCell])
        XCTAssertEqual(plan.toRemove, [], "the old device moved, it did not vanish - no remove is needed")
    }

    func testTheFirstEverRebuildFromNoPriorLegsAddsEverything() {
        let plan = ClientConfig.rebuildPlan(from: [], to: [wifi, cell])
        XCTAssertEqual(plan.toAdd, [wifi, cell])
        XCTAssertEqual(plan.toRemove, [])
    }

    func testEverythingDisappearingRemovesEveryPathIDAndAddsNothing() {
        let plan = ClientConfig.rebuildPlan(from: [wifi, cell], to: [])
        XCTAssertEqual(Set(plan.toRemove), Set([0, 1]))
        XCTAssertEqual(plan.toAdd, [])
    }

    /// Simultaneous transitions, the way a real path update can carry more
    /// than one fact at once: cellular's context number moved AND wifi
    /// disappeared AND a wired adapter arrived, all in the same snapshot.
    func testASimultaneousMoveDisappearanceAndArrivalAreAllRepresented() {
        let movedCell = ClientConfig.Link(pathID: 1, name: "cell", device: "pdp_ip1", weight: 10)
        let ethernet = ClientConfig.Link(pathID: 2, name: "spare", device: "en2", weight: 10)
        let plan = ClientConfig.rebuildPlan(from: [wifi, cell], to: [movedCell, ethernet])
        XCTAssertEqual(plan.toRemove, [0], "wifi's pathID vanished and must be dropped")
        XCTAssertEqual(plan.toAdd, [movedCell, ethernet])
    }

    func testResultsAreOrderedByPathIDRegardlessOfInputOrder() {
        let plan = ClientConfig.rebuildPlan(from: [], to: [cell, wifi])
        XCTAssertEqual(plan.toAdd.map(\.pathID), [0, 1],
                       "a log line that reordered on every replay would make identical "
                     + "incidents look different from each other")
    }
}
