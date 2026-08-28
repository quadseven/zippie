import Foundation
import XCTest
@testable import ZippieCompanionKit

@MainActor
final class DNSSettingsControllerTests: XCTestCase {
    private enum Failure: Error { case requested }

    private final class Manager: DNSSettingsManaging {
        var isEnabled = false
        var serverURL: URL?
        var loadError: Error?
        var saveError: Error?
        var removeError: Error?
        var loadErrorCall: Int?
        var savesServerURL = true
        private(set) var loadCallCount = 0

        func load() async throws {
            loadCallCount += 1
            if loadCallCount == loadErrorCall { throw Failure.requested }
            if let loadError { throw loadError }
        }

        func save(serverURL: URL, localizedDescription: String) async throws {
            if let saveError { throw saveError }
            if savesServerURL { self.serverURL = serverURL }
        }

        func remove() async throws {
            if let removeError { throw removeError }
            serverURL = nil
            isEnabled = false
        }
    }

    func testRefreshNamesTheProfileThatIsActuallyInstalled() async {
        let manager = Manager()
        manager.serverURL = NextDNSProfile(
            profileID: "aaaa11", deviceName: "old-phone").dohURL
        manager.isEnabled = true
        let controller = DNSSettingsController(manager: manager)

        await controller.refreshStatus()

        XCTAssertEqual(controller.status, .active(NextDNSProfile(
            profileID: "aaaa11", deviceName: "old-phone")))
    }

    func testFailedProfileChangeDoesNotRelabelTheInstalledProfile() async {
        let manager = Manager()
        let installed = NextDNSProfile(profileID: "aaaa11", deviceName: "old-phone")
        manager.serverURL = installed.dohURL
        manager.isEnabled = true
        manager.saveError = Failure.requested
        let controller = DNSSettingsController(manager: manager)

        let saved = await controller.apply(NextDNSProfile(
            profileID: "bbbb22", deviceName: "new-phone"))

        XCTAssertFalse(saved, "the editor would persist a profile the system refused")
        XCTAssertEqual(manager.serverURL, installed.dohURL,
                       "a failed save replaced the installed resolver")
        await controller.refreshStatus()
        XCTAssertEqual(controller.status, .active(installed),
                       "the old resolver was relabelled with the failed new identity")
    }

    func testSuccessfulSaveReportsTheConfigurationReadBackFromTheManager() async {
        let manager = Manager()
        let controller = DNSSettingsController(manager: manager)
        let profile = NextDNSProfile(profileID: "bbbb22", deviceName: "new-phone")

        let saved = await controller.apply(profile)

        XCTAssertTrue(saved)
        XCTAssertEqual(controller.status, .configuredButDisabled(profile))
    }

    func testSaveIsNotCommittedWhenReadbackFails() async {
        let manager = Manager()
        manager.loadErrorCall = 2
        let controller = DNSSettingsController(manager: manager)

        let saved = await controller.apply(NextDNSProfile(profileID: "bbbb22"))

        XCTAssertFalse(saved, "the editor would persist a profile that was not confirmed")
        guard case .failed = controller.status else {
            return XCTFail("the readback failure was hidden")
        }
    }

    func testSaveIsNotCommittedWhenReadbackStillReportsTheOldProfile() async {
        let manager = Manager()
        let installed = NextDNSProfile(profileID: "aaaa11", deviceName: "old-phone")
        manager.serverURL = installed.dohURL
        manager.isEnabled = true
        manager.savesServerURL = false
        let controller = DNSSettingsController(manager: manager)

        let saved = await controller.apply(NextDNSProfile(
            profileID: "bbbb22", deviceName: "new-phone"))

        XCTAssertFalse(saved, "the editor would persist a profile iOS did not install")
        XCTAssertEqual(controller.status, .active(installed))
    }

    func testUnknownInstalledDoHIdentityIsNeverCalledNextDNS() async {
        let manager = Manager()
        manager.serverURL = URL(string: "https://dns.example.test/dns-query")
        manager.isEnabled = true
        let controller = DNSSettingsController(manager: manager)

        await controller.refreshStatus()

        guard case .failed = controller.status else {
            return XCTFail("an unknown active resolver was presented as NextDNS")
        }
    }

    func testRemoveFailureKeepsTheConfigurationVisible() async {
        let manager = Manager()
        let profile = NextDNSProfile(profileID: "aaaa11")
        manager.serverURL = profile.dohURL
        manager.isEnabled = true
        manager.removeError = Failure.requested
        let controller = DNSSettingsController(manager: manager)

        let removed = await controller.remove()

        XCTAssertFalse(removed)
        XCTAssertEqual(manager.serverURL, profile.dohURL)
        XCTAssertFalse(controller.status == .notConfigured)
    }
}
