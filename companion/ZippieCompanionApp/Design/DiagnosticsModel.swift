import Foundation
import ZippieCompanionKit

/// Takes the measurements the diagnostics screen reports.
///
/// MEASURES, THEN PUBLISHES ONCE. Each probe writes into a local value and the
/// whole `Diagnostics` is assigned at the end, so the screen never shows a
/// half-measured mixture where the MDM row is from this attempt and the tailnet
/// row is from the last one. A screen of readings taken at different moments,
/// all presented as current, is the failure this type exists to avoid.
@MainActor
final class DiagnosticsModel: ObservableObject {
    @Published private(set) var diagnostics = Diagnostics()
    @Published private(set) var measuring = false

    private let consoleHost: String?
    private let mdmHost: String
    private let session: URLSession

    /// NO DEFAULT (#156). This named one specific deployment's MDM in every
    /// build, extractable from the binary. Nothing on iOS writes a real value
    /// in here either - there is no Settings field and no managed-configuration
    /// key that reaches it - so a blank host is the only value this can ever
    /// carry today, and `measure()` skips the probe rather than building
    /// `https:///`, which would render as a network fault instead of "not
    /// configured". Matches Android's `DiagnosticsMeasurer.mdmHost`, which is
    /// the same blank-by-default, skip-when-blank shape.
    init(consoleHost: String?,
         mdmHost: String = "",
         session: URLSession = .shared) {
        self.consoleHost = consoleHost
        self.mdmHost = mdmHost
        self.session = session
    }

    func measure() async {
        guard !measuring else { return }
        measuring = true
        defer { measuring = false }

        var d = Diagnostics()
        let facts = await NetworkFacts.current()
        d.ssid = facts.ssid
        // .unknown, NOT .none. iOS exposes no public API for the resolver DHCP
        // supplied, and reporting "this network offered no DNS server" on every
        // healthy iPhone would be a red row for a fault that does not exist -
        // which is the fastest way to teach somebody to stop reading a screen.
        // Android can answer this; iOS cannot, and the type says so.
        d.dhcpResolver = .unknown

        d.captive = await probe(url: "http://captive.apple.com/hotspot-detect.html",
                               expecting: "Success")
        // A blank host must SKIP the probe rather than build "https:///" - an
        // empty host renders as a network fault, which is the failure shape
        // this screen exists to avoid (see the init above).
        d.mdm = mdmHost.trimmingCharacters(in: .whitespaces).isEmpty
            ? .notChecked
            : await probe(url: "https://\(mdmHost)/", expecting: nil)

        // Direct vs via-router is decided by whether THIS phone runs Tailscale,
        // not by whether the tailnet answered. Both states answer; only one
        // survives changing network, and conflating them is the bug.
        if let node = TailnetPresence.address() {
            d.tailnet = d.mdm.isOK ? .direct(nodeName: node) : .unreachable(.noRoute)
        } else if d.mdm.isOK {
            d.tailnet = .viaRouter(host: consoleHost ?? "this network's router")
        } else {
            d.tailnet = .unreachable(.noRoute)
        }

        d.measuredAt = Date()
        diagnostics = d
    }

    /// Classifies rather than reports a boolean. "failed" sends nobody
    /// anywhere; "HTTP 401" and "timed out after 12s" send them to different
    /// places, and naming which one is the entire value of this screen.
    private func probe(url: String, expecting: String?) async -> DiagnosticState {
        guard let u = URL(string: url) else { return .notChecked }
        var req = URLRequest(url: u)
        req.timeoutInterval = 12
        req.cachePolicy = .reloadIgnoringLocalCacheData
        do {
            let (data, response) = try await session.data(for: req)
            if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
                return .failed(.http(status: http.statusCode))
            }
            if let want = expecting {
                let body = String(data: data, encoding: .utf8) ?? ""
                guard body.contains(want) else {
                    // A body that is not what was asked for is a captive portal
                    // or an interceptor, not an outage, and iOS treats it as
                    // "no internet" either way.
                    return .failed(.http(status: 200))
                }
            }
            return .ok(detail: nil)
        } catch let e as URLError {
            switch e.code {
            case .timedOut:                     return .failed(.timedOut(seconds: 12))
            case .cannotFindHost, .dnsLookupFailed:
                return .failed(.nameNotResolved(u.host ?? url))
            case .secureConnectionFailed, .serverCertificateUntrusted:
                return .failed(.tls(e.localizedDescription))
            case .cannotConnectToHost, .networkConnectionLost, .notConnectedToInternet:
                return .failed(.noRoute)
            default:                            return .failed(.noRoute)
            }
        } catch {
            return .failed(.noRoute)
        }
    }
}
