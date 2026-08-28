import XCTest
@testable import ZippieCompanionKit

/// Interface resolution, which is what stands between "two pinned sockets" and
/// "two sockets on one radio reported as a bond".
final class BondInterfacesTests: XCTestCase {

    private func up(_ name: String) -> InterfaceSnapshot {
        InterfaceSnapshot(name: name, isUp: true, hasIPv4: true)
    }

    // MARK: - classification

    func testCellularIsEveryPDPContextNotJustTheFirst() {
        // The modem numbers contexts as it brings them up. Matching only
        // pdp_ip0 would leave a phone whose cellular landed on pdp_ip1 with no
        // cellular leg and no error.
        for name in ["pdp_ip0", "pdp_ip1", "pdp_ip4"] {
            XCTAssertEqual(InterfaceRole.of(name), .cellular, name)
        }
    }

    func testWifiAndUSBEthernetShareARole() {
        // en0 is wifi on every iPhone shipped so far; a USB-C ethernet adapter
        // arrives as en2 or en3, and ADR 0022's answer for the CarPlay phone
        // is exactly that adapter. Both are the local unmetered link.
        for name in ["en0", "en2", "en3"] {
            XCTAssertEqual(InterfaceRole.of(name), .wifi, name)
        }
    }

    /// THE ONE THAT MATTERS. Every name here is up and addressed on a real
    /// iPhone and would be a catastrophic thing to pin a bonded leg to.
    func testNothingElseIsEverAllowedToCarryALeg() {
        let forbidden = [
            "utun0",    // a tunnel, possibly the one carrying this very traffic
            "utun3",
            "lo0",      // delivers nothing off the device
            "awdl0",    // AirDrop / AirPlay peer-to-peer, no internet route
            "llw0",     // low-latency WLAN, same
            "ap1",      // the personal-hotspot AP: our own tethered clients
            "bridge100",
            "ipsec0",
            "anpi0",    // Apple-internal, not routable
            "",
        ]
        for name in forbidden {
            XCTAssertNil(InterfaceRole.of(name),
                         "\(name) was classified and could carry a leg")
        }
    }

    // MARK: - resolution

    func testBothRolesResolveFromARealisticInterfaceList() {
        let resolved = ResolvedInterfaces.resolve([
            up("lo0"), up("en0"), up("pdp_ip0"), up("utun0"), up("awdl0"),
        ])
        XCTAssertEqual(resolved.wifi, "en0")
        XCTAssertEqual(resolved.cellular, "pdp_ip0")
        XCTAssertEqual(resolved.devices, ["en0", "pdp_ip0"])
    }

    /// An interface that is UP with no IPv4 address produces a socket that
    /// cannot send, because the datapath binds udp4. From the app that is
    /// indistinguishable from a leg that is up and carrying nothing.
    func testAnInterfaceWithNoIPv4IsNotUsable() {
        let resolved = ResolvedInterfaces.resolve([
            InterfaceSnapshot(name: "en0", isUp: true, hasIPv4: false),
            up("pdp_ip0"),
        ])
        XCTAssertNil(resolved.wifi)
        XCTAssertEqual(resolved.cellular, "pdp_ip0")
    }

    func testADownInterfaceIsNotUsable() {
        let resolved = ResolvedInterfaces.resolve([
            InterfaceSnapshot(name: "pdp_ip0", isUp: false, hasIPv4: true),
            up("en0"),
        ])
        XCTAssertEqual(resolved.wifi, "en0")
        XCTAssertNil(resolved.cellular, "a down interface was offered as a leg")
    }

    /// Aeroplane mode, a basement, or no data plan. One leg is an honest
    /// answer; inventing a second one is not.
    func testNoCellularResolvesToOneLegRatherThanAGuess() {
        let resolved = ResolvedInterfaces.resolve([up("en0")])
        XCTAssertEqual(resolved.devices, ["en0"])
    }

    /// Determinism, not preference. A resolver that returned a different
    /// device on each start would make every restart look like a topology
    /// change, and leg ordering feeds path ids.
    func testResolutionIsDeterministicRegardlessOfReportOrder() {
        let interfaces = [up("en3"), up("pdp_ip1"), up("en0"), up("pdp_ip0")]
        let forwards = ResolvedInterfaces.resolve(interfaces)
        let backwards = ResolvedInterfaces.resolve(interfaces.reversed())
        XCTAssertEqual(forwards, backwards)
        XCTAssertEqual(forwards.wifi, "en0")
        XCTAssertEqual(forwards.cellular, "pdp_ip0")
    }

    // MARK: - admission

    /// THE FAILURE THIS TYPE EXISTS FOR. Before it, a client tunnel whose
    /// configured device names no longer matched reality logged each refusal
    /// and started anyway: VPN badge on, tunnel live, every packet into a
    /// socket that could not send.
    func testZeroLegsIsNotStartable() {
        XCTAssertEqual(LegAdmission.admit([]), .none)
        XCTAssertFalse(LegAdmission.admit([]).isStartable)
        XCTAssertFalse(LegAdmission.admit([]).isBonded)
    }

    func testOneLegStartsButIsNeverCalledABond() {
        let admission = LegAdmission.admit(["en0"])
        XCTAssertEqual(admission, .singleLeg(device: "en0"))
        XCTAssertTrue(admission.isStartable)
        XCTAssertFalse(admission.isBonded,
                       "one leg was reported as a bond, which is this project's oldest failure")
        XCTAssertTrue(admission.summary.contains("not a bond"))
    }

    func testTwoLegsIsABond() {
        let admission = LegAdmission.admit(["en0", "pdp_ip0"])
        XCTAssertEqual(admission, .bonded(devices: ["en0", "pdp_ip0"]))
        XCTAssertTrue(admission.isBonded)
    }

    /// Two sockets on one radio is one path wearing two names.
    func testTheSameDeviceTwiceIsOneLeg() {
        XCTAssertEqual(LegAdmission.admit(["en0", "en0"]), .singleLeg(device: "en0"))
    }

    func testAnEmptyDeviceNameIsNotALeg() {
        // An empty device means an UNPINNED socket, which leaves by whatever
        // wins the default route. Counting it would report a bond that is not.
        XCTAssertEqual(LegAdmission.admit(["", "en0"]), .singleLeg(device: "en0"))
        XCTAssertEqual(LegAdmission.admit([""]), .none)
    }

    // MARK: - the live reader

    /// Reads whatever machine the tests run on, so it asserts INVARIANTS
    /// rather than a specific interface list. The rule it proves is the one
    /// that matters: getifaddrs reports one entry per address family, and
    /// taking the first hit would report a dual-stack interface as having no
    /// IPv4 and silently drop a good leg.
    func testTheLiveSnapshotReportsEachInterfaceExactlyOnce() {
        let snapshot = LiveInterfaces.snapshot()
        let names = snapshot.map(\.name)
        XCTAssertEqual(names.count, Set(names).count, "an interface was reported twice")
        XCTAssertFalse(names.contains(""), "an unnamed interface cannot be pinned")
    }

    func testTheLiveResolverNeverReturnsTheSameDeviceForBothRoles() {
        let resolved = LiveInterfaces.resolved()
        if let wifi = resolved.wifi, let cellular = resolved.cellular {
            XCTAssertNotEqual(wifi, cellular)
        }
        // Whatever it found must classify. A device that does not have a role
        // has no business being pinned.
        for device in resolved.devices {
            XCTAssertNotNil(InterfaceRole.of(device), device)
        }
    }
}
