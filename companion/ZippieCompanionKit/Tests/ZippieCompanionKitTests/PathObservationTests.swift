import Network
import XCTest
@testable import ZippieCompanionKit

/// #64: an Ethernet insertion/removal has to be representable here, not only
/// on a real iPhone with a real USB-C adapter - see `PathSnapshot.from(_:)`
/// for why a plain literal stands in for a live `NWPath` in every test below.
final class PathObservationTests: XCTestCase {

    private func snapshot(_ interfaces: [(String, NWInterface.InterfaceType)],
                          satisfied: Bool = true,
                          v4: Bool = true, v6: Bool = true) -> PathSnapshot {
        PathSnapshot(interfaces: interfaces.map { PathInterfaceSnapshot(name: $0.0, type: $0.1) },
                    isSatisfied: satisfied, supportsIPv4: v4, supportsIPv6: v6)
    }

    // MARK: - transitions

    func testAFirstSnapshotIsABaselineNotATransition() {
        let first = snapshot([("pdp_ip0", .cellular)])
        XCTAssertEqual(PathObserver.transitions(from: nil, to: first), [],
                       "nothing to compare the first reading to should not read as a change")
    }

    func testTwoIdenticalSnapshotsProduceNoTransitions() {
        let s = snapshot([("en0", .wifi), ("pdp_ip0", .cellular)])
        XCTAssertEqual(PathObserver.transitions(from: s, to: s), [])
    }

    func testEthernetAdapterInsertionIsAnInterfaceAdded() {
        let before = snapshot([("en0", .wifi), ("pdp_ip0", .cellular)])
        let after = snapshot([("en0", .wifi), ("en2", .wiredEthernet), ("pdp_ip0", .cellular)])
        XCTAssertEqual(PathObserver.transitions(from: before, to: after),
                      [.interfaceAdded(PathInterfaceSnapshot(name: "en2", type: .wiredEthernet))],
                      "a USB-C Ethernet adapter appearing is exactly this scenario")
    }

    func testEthernetAdapterRemovalIsAnInterfaceRemoved() {
        let before = snapshot([("en0", .wifi), ("en2", .wiredEthernet), ("pdp_ip0", .cellular)])
        let after = snapshot([("en0", .wifi), ("pdp_ip0", .cellular)])
        XCTAssertEqual(PathObserver.transitions(from: before, to: after),
                      [.interfaceRemoved(PathInterfaceSnapshot(name: "en2", type: .wiredEthernet))])
    }

    func testSatisfactionChangeIsReported() {
        let before = snapshot([("pdp_ip0", .cellular)], satisfied: true)
        let after = snapshot([("pdp_ip0", .cellular)], satisfied: false)
        XCTAssertEqual(PathObserver.transitions(from: before, to: after),
                      [.satisfactionChanged(from: true, to: false)])
    }

    func testAddressFamilySupportChangeIsReported() {
        let before = snapshot([("pdp_ip0", .cellular)], v4: true, v6: true)
        let after = snapshot([("pdp_ip0", .cellular)], v4: true, v6: false)
        XCTAssertEqual(PathObserver.transitions(from: before, to: after),
                      [.addressFamilySupportChanged(supportsIPv4: true, supportsIPv6: false)])
    }

    func testSeveralSimultaneousChangesAllSurface() {
        let before = snapshot([("en0", .wifi), ("pdp_ip0", .cellular)], satisfied: true)
        let after = snapshot([("en0", .wifi), ("en2", .wiredEthernet)], satisfied: false)
        let changes = PathObserver.transitions(from: before, to: after)
        XCTAssertEqual(Set(changes), Set([
            .interfaceAdded(PathInterfaceSnapshot(name: "en2", type: .wiredEthernet)),
            .interfaceRemoved(PathInterfaceSnapshot(name: "pdp_ip0", type: .cellular)),
            .satisfactionChanged(from: true, to: false),
        ]), "an Ethernet insertion that also knocks cellular off the path is three facts, not one")
    }

    // MARK: - status

    func testNoCellularInterfaceAtAllIsCellularUnavailable() {
        let s = snapshot([("en0", .wifi)])
        XCTAssertEqual(PathObserver.status(previous: s, current: s, legs: .bonded(devices: ["en0"])),
                       .cellularUnavailable,
                       "no cellular interface in the path at all outranks everything else")
    }

    func testAFreshInterfaceChangeIsPathChangedEvenWithLegsUp() {
        let before = snapshot([("en0", .wifi), ("pdp_ip0", .cellular)])
        let after = snapshot([("en0", .wifi), ("en2", .wiredEthernet), ("pdp_ip0", .cellular)])
        XCTAssertEqual(PathObserver.status(previous: before, current: after,
                                          legs: .bonded(devices: ["en0", "pdp_ip0"])),
                       .pathChanged,
                       "legs still being reported as up does not mean they are pinned to the "
                     + "interfaces that exist right now")
    }

    func testStableInterfacesWithNoLegsIsEndpointUnreachable() {
        let s = snapshot([("en0", .wifi), ("pdp_ip0", .cellular)])
        XCTAssertEqual(PathObserver.status(previous: s, current: s, legs: .none),
                       .endpointUnreachable,
                       "the path itself is fine and unchanged - nothing carrying is a different "
                     + "problem than the interface list")
    }

    func testStableInterfacesWithLegsUpIsHealthy() {
        let s = snapshot([("en0", .wifi), ("pdp_ip0", .cellular)])
        XCTAssertEqual(PathObserver.status(previous: s, current: s,
                                          legs: .bonded(devices: ["en0", "pdp_ip0"])),
                       .healthy)
    }

    func testFirstEverSnapshotWithLegsUpIsHealthyNotPathChanged() {
        let s = snapshot([("en0", .wifi), ("pdp_ip0", .cellular)])
        XCTAssertEqual(PathObserver.status(previous: nil, current: s,
                                          legs: .bonded(devices: ["en0", "pdp_ip0"])),
                       .healthy,
                       "startup's own first reading must not read as a transition")
    }
}
