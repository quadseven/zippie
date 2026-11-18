import NetworkExtension
import OSLog
import ZippieCompanionKit

enum Observability {
    private static let log = Logger(subsystem: "app.zippie.companion", category: "app")

    static func start() {
        log.info("app launched build=\(BuildInfo.commitLabel, privacy: .public)")
    }

    static func tunnelConnectRequested() {
        log.info("tunnel connect requested")
    }

    static func tunnelStatus(_ status: NEVPNStatus, error: String?) {
        log.info("tunnel status=\(tunnelStatusName(status), privacy: .public) error=\(error ?? "", privacy: .private)")
    }

    private static func tunnelStatusName(_ s: NEVPNStatus) -> String {
        switch s {
        case .invalid: return "invalid"
        case .disconnected: return "disconnected"
        case .connecting: return "connecting"
        case .connected: return "connected"
        case .reasserting: return "reasserting"
        case .disconnecting: return "disconnecting"
        @unknown default: return "unknown"
        }
    }

    static func probeCompleted(_ verdict: ProbeVerdict, seconds: Double) {
        let outcome: String
        switch verdict {
        case .proven: outcome = "proven"
        case .inconclusiveSameEgress: outcome = "inconclusive_same_egress"
        case .maskedByPrivateRelay: outcome = "masked_by_private_relay"
        case .cellularUnavailable: outcome = "cellular_unavailable"
        case .baselineFailed: outcome = "baseline_failed"
        }
        log.info("probe verdict=\(outcome, privacy: .public) duration_s=\(seconds, privacy: .public)")
    }

    static func consoleUnreachable(_ reason: UnreachableReason) {
        log.warning("console unreachable: \(reason.logName, privacy: .public)")
    }

    static func relayControlTapped(control: String, action: String) {
        log.debug("relay control=\(control, privacy: .public) action=\(action, privacy: .public)")
    }
}
