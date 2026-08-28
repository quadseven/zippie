import SwiftUI
import ZippieCompanionKit

/// The user-reachable half of per-person DNS (#25).
///
/// The controller owns the testable state transitions and its system adapter
/// owns the NetworkExtension API. This screen only edits a desired profile and
/// reads status back: iOS owns the resolver slot and remains the authority on
/// whether this setting is in effect.
struct NextDNSSettingsScreen: View {
    @Environment(\.scenePhase) private var scenePhase
    @StateObject private var controller = DNSSettingsController(
        manager: SystemDNSSettingsManager())
    @State private var profileID = Settings.nextDNSProfileID
    @State private var deviceName = Settings.nextDNSDeviceName
    @State private var updating = false

    private var editedProfile: NextDNSProfile {
        NextDNSProfile(
            profileID: profileID.trimmingCharacters(in: .whitespacesAndNewlines),
            deviceName: deviceName.trimmingCharacters(in: .whitespacesAndNewlines))
    }

    var body: some View {
        Page {
            status

            SectionHead(title: "Your resolver",
                        note: "Find the profile ID in your NextDNS dashboard URL.")
            FieldRow(label: "Profile ID", text: $profileID)
            Hairline()
            FieldRow(label: "Device name", text: $deviceName)

            if !profileID.isEmpty && !editedProfile.isValid {
                Note(text: "Use the 4-16 character lowercase profile ID from NextDNS.",
                     tone: .warning)
            }

            ActionButton(title: updating ? "Saving..." : "Use NextDNS",
                         enabled: editedProfile.isValid && !updating) {
                apply()
            }
            .padding(.top, Space.roomy)

            if controller.status.hasConfiguration || Settings.nextDNSProfile != nil {
                ActionButton(title: "Stop using NextDNS",
                             role: .destructive,
                             enabled: !updating) {
                    remove()
                }
                .padding(.top, Space.tight)
            }

            Note(text: "This setting follows this phone across wifi and cellular. "
               + "A full-tunnel Zippie connection uses the tunnel's resolver instead; "
               + "iOS can also disable this setting when another DNS provider takes over.")
        }
        .navigationTitle("NextDNS")
        .navigationBarTitleDisplayMode(.inline)
        .task {
            await controller.refreshStatus()
        }
        .onChange(of: scenePhase) { phase in
            guard phase == .active else { return }
            Task { await controller.refreshStatus() }
        }
    }

    private var status: some View {
        VStack(alignment: .leading, spacing: Space.snug) {
            Text(statusHeadline)
                .font(Kind.title())
                .foregroundStyle(statusColour)
            Text(statusDetail)
                .font(Kind.body())
                .foregroundStyle(Ink.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.top, Space.section)
        .padding(.bottom, Space.tight)
        .accessibilityElement(children: .combine)
    }

    private var statusHeadline: String {
        switch controller.status {
        case .notConfigured:             return "Not configured"
        case .active:                    return "Using NextDNS"
        case .configuredButDisabled:     return "NextDNS is disabled"
        case .failed:                    return "Could not update DNS"
        }
    }

    private var statusDetail: String {
        switch controller.status {
        case .notConfigured:
            return "Add your profile so queries from this phone appear under your account."
        case .active(let profile):
            let name = profile.sanitizedDeviceName
            return name.isEmpty
                ? "iOS is using NextDNS profile \(profile.profileID)."
                : "iOS is using profile \(profile.profileID) with the device name \(name)."
        case .configuredButDisabled:
            return "iOS has the profile, but is not using it. Check VPN, DNS & "
                + "Device Management in Settings."
        case .failed(let message):
            return message
        }
    }

    private var statusColour: Color {
        switch controller.status {
        case .active:                    return Ink.live
        case .configuredButDisabled:     return Ink.degraded
        case .failed:                    return Ink.down
        case .notConfigured:             return Ink.primary
        }
    }

    private func apply() {
        let profile = editedProfile
        guard profile.isValid else { return }
        updating = true
        Task {
            if await controller.apply(profile) {
                Settings.nextDNSProfileID = profile.profileID
                Settings.nextDNSDeviceName = profile.deviceName
            }
            updating = false
        }
    }

    private func remove() {
        updating = true
        Task {
            if await controller.remove() {
                Settings.nextDNSProfileID = ""
                Settings.nextDNSDeviceName = ""
                profileID = ""
                deviceName = ""
            }
            updating = false
        }
    }
}
