import Foundation

/// What a pair of probes proves about cellular binding.
///
/// The whole companion design rests on one claim: that
/// `NWParameters.requiredInterfaceType = .cellular` really does force a socket
/// onto the cellular radio while Wi-Fi is up and preferred. ADR 0020 requires
/// proving that BEFORE any UI exists, because there is no fallback if it is
/// false - a phone that cannot contribute its cellular while on Wi-Fi cannot be
/// a bonded leg at all.
///
/// This type is deliberately pure. The verdict logic is the part worth testing,
/// and it must be testable on a bare toolchain with no device, no radios, and
/// no network - the same reason MacchinaCompanionKit stays UIKit-free.
public enum ProbeVerdict: Equatable, Sendable {
    /// Two different public addresses. Cellular binding works: the forced
    /// socket left by a different path than the default one.
    case proven(wifi: String, cellular: String)

    /// Same public address on both. Either the binding was ignored and both
    /// went out Wi-Fi, or - the false negative that matters - the phone is
    /// genuinely NAT'd behind the same egress, e.g. tethered to the very
    /// router under test. Never report this as failure without saying so.
    case inconclusiveSameEgress(address: String)

    /// The cellular probe could not connect at all. Distinct from
    /// `inconclusiveSameEgress`: this says the interface was unusable, not that
    /// the result was ambiguous.
    case cellularUnavailable(reason: String)

    /// The baseline probe failed, so there is nothing to compare against.
    case baselineFailed(reason: String)

    /// Both addresses are iCloud Private Relay exits, so the difference
    /// between them says which exit Apple assigned - NOT which radio the
    /// packet left by. This case exists because v1 reported exactly this as
    /// PROVEN (146.75.245.47 Albany vs .73 Liverpool) and it was wrong.
    case maskedByPrivateRelay(wifi: String, cellular: String)

    public var isProven: Bool {
        if case .proven = self { return true }
        return false
    }

    /// One line for the harness UI and for the log line shipped to Datadog.
    public var summary: String {
        switch self {
        case let .proven(wifi, cellular):
            return "PROVEN - wifi egress \(wifi), cellular egress \(cellular)"
        case let .inconclusiveSameEgress(address):
            return "INCONCLUSIVE - both probes egressed \(address); "
                + "binding may be ignored, or both paths share a NAT"
        case let .cellularUnavailable(reason):
            return "CELLULAR UNAVAILABLE - \(reason)"
        case let .baselineFailed(reason):
            return "BASELINE FAILED - \(reason)"
        case let .maskedByPrivateRelay(wifi, cellular):
            return "MASKED BY PRIVATE RELAY - \(wifi) and \(cellular) are both "
                + "iCloud Private Relay exits, so this measures Apple's exit "
                + "choice, not the interface. Disable Private Relay and re-run."
        }
    }
}

/// Decide the verdict from two probe outcomes.
///
/// Split out from the networking so the interesting cases - especially the
/// same-egress ambiguity - can be tested without a phone.
public enum ProbeEvaluator {
    public static func evaluate(
        baseline: Result<String, ProbeError>,
        cellular: Result<String, ProbeError>,
        relayRanges: PrivateRelayRanges? = nil
    ) -> ProbeVerdict {
        let wifi: String
        switch baseline {
        case let .success(address):
            wifi = address
        case let .failure(error):
            return .baselineFailed(reason: error.description)
        }

        switch cellular {
        case let .success(address):
            // Trimmed and compared case-insensitively: some echo endpoints
            // return a trailing newline, and an IPv6 literal's hex casing is
            // not semantically meaningful. A whitespace difference must never
            // read as "different egress" - that would be a false PROVEN, which
            // is far worse than a false inconclusive.
            let a = wifi.trimmed()
            let b = address.trimmed()
            if a.caseInsensitiveCompare(b) == .orderedSame {
                return .inconclusiveSameEgress(address: a)
            }
            // Guard BEFORE declaring proof. Two different Private Relay exits
            // look exactly like two different carriers unless you check.
            if let relayRanges, relayRanges.contains(a) || relayRanges.contains(b) {
                return .maskedByPrivateRelay(wifi: a, cellular: b)
            }
            // Fallback when the published list could not be fetched: two
            // genuinely different carriers do not share a /24 with each other
            // or with a cafe wifi. Bias toward withholding proof.
            if sharesV24(a, b) {
                return .maskedByPrivateRelay(wifi: a, cellular: b)
            }
            return .proven(wifi: a, cellular: b)
        case let .failure(error):
            return .cellularUnavailable(reason: error.description)
        }
    }
}

public enum ProbeError: Error, Equatable, Sendable {
    case noInterfaceAvailable
    case timedOut(seconds: Int)
    case connectionFailed(String)
    case badResponse(String)

    public var description: String {
        switch self {
        case .noInterfaceAvailable:
            return "no matching interface available"
        case let .timedOut(seconds):
            return "timed out after \(seconds)s"
        case let .connectionFailed(detail):
            return "connection failed: \(detail)"
        case let .badResponse(detail):
            return "bad response: \(detail)"
        }
    }
}

extension String {
    func trimmed() -> String {
        trimmingCharacters(in: .whitespacesAndNewlines)
    }
}
