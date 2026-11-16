import Foundation
import ZippieCompanionKit

#if canImport(Zippie)
import Zippie
#endif

/// Wraps the Go core's `mobile.VerifyPeer` for peer verification.
///
/// This lives in the app target (not the kit) because it requires the
/// `Zippie.xcframework` which is only linked to the app and tunnel targets.
/// The kit keeps the `PeerVerifying` protocol so tests can use a fake verifier
/// without the Go framework.
@available(iOS 16.0, *)
public final class GoPeerVerifier: PeerVerifying, @unchecked Sendable {

    private let rosterClient: RosterClient
    private var cachedRoster: String?
    private var rosterExpiry: Date = .distantPast

    public init(rosterClient: RosterClient = .shared) {
        self.rosterClient = rosterClient
    }

    /// Verifies a discovered peer's TXT record against the paired-device roster.
    ///
    /// Returns the verified peer when the TXT proof authenticates, or nil when
    /// it is refused (missing field, unknown client, bad signature, stale, ...).
    public func verify(txt: [String: String]) async -> VerifiedPeer? {
        do {
            let rosterJSON = try await fetchRoster()
            let txtJSON = try JSONSerialization.data(withJSONObject: txt)
            let txtString = String(data: txtJSON, encoding: .utf8) ?? "{}"

            #if canImport(Zippie)
            let result = Zippie.VerifyPeer(rosterJSON, txtString)
            if let error = result.error {
                return nil
            }
            if let output = result.output {
                return parseVerifiedPeer(output)
            }
            return nil
            #else
            // Build without Zippie framework - cannot verify
            return nil
            #endif
        } catch {
            return nil
        }
    }

    private func fetchRoster() async throws -> String {
        if Date() < rosterExpiry, let cached = cachedRoster {
            return cached
        }
        let roster = try await rosterClient.fetch()
        cachedRoster = roster
        rosterExpiry = Date().addingTimeInterval(300) // 5 minute cache
        return roster
    }

    private func parseVerifiedPeer(_ json: String) -> VerifiedPeer? {
        guard let data = json.data(using: .utf8) else { return nil }
        struct VerifiedPeerJSON: Codable {
            let client_id: UInt32
            let endpoint: String
        }
        guard let decoded = try? JSONDecoder().decode(VerifiedPeerJSON.self, from: data) else {
            return nil
        }
        return VerifiedPeer(clientID: decoded.client_id, endpoint: decoded.endpoint)
    }
}

/// Fetches the discovery roster from the router's console.
///
/// The roster is a JSON array of objects with `client_id` and `key_hex` (the
/// discovery key, derived from the pairing key via `PeerDiscoveryKey`).
protocol RosterClient: Sendable {
    func fetch() async throws -> String
}

extension RosterClient {
    static var shared: RosterClient {
        DefaultRosterClient()
    }
}

private struct DefaultRosterClient: RosterClient {
    func fetch() async throws -> String {
        let consoleURL = Settings.consoleURL
        guard let url = URL(string: consoleURL) else {
            throw URLError(.badURL)
        }
        // The roster endpoint returns the discovery keys for all paired devices.
        // Format: [{"client_id": 1, "key_hex": "..."}, ...]
        let rosterURL = url.appendingPathComponent("/api/discovery/roster")
        var req = URLRequest(url: rosterURL)
        req.timeoutInterval = 8
        req.cachePolicy = .reloadIgnoringLocalCacheData

        if let token = ConsoleWriteToken.shared.read() {
            req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }

        let (data, resp) = try await URLSession.shared.data(for: req)
        if let http = resp as? HTTPURLResponse, http.statusCode != 200 {
            throw URLError(.badServerResponse)
        }
        return String(data: data, encoding: .utf8) ?? "[]"
    }
}