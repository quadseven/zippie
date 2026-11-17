import Foundation

/// Errors from the pairing API.
public struct PairingError: Error, Equatable, Sendable {
    public let message: String
    public init(_ m: String) { message = m }
}

/// The response from POST /api/legs/pair (first ask).
public struct PairingAskResponse: Codable, Sendable, Equatable {
    public let pairing: String // "pending" or "approved"
    public let code: String?
    public let expires_s: Double?
    public let token: String?
}

/// The response from POST /api/legs/pair/approve (operator approval).
public struct PairingApproveResponse: Codable, Sendable, Equatable {
    public let pairing: String // "approved"
    public let name: String?
    public let host: String?
    public let token: String?
}

/// The request body for POST /api/legs/pair (first ask).
public struct PairingAskRequest: Codable, Sendable, Equatable {
    public let name: String
    public let label: String
    public let link: String // "ethernet"
}

/// The request body for POST /api/legs/pair/approve.
public struct PairingApproveRequest: Codable, Sendable, Equatable {
    public let code: String
}

/// The request body for POST /api/legs/pair (collect with code).
public struct PairingCollectRequest: Codable, Sendable, Equatable {
    public let code: String
}

/// Client for the router's pairing API.
///
/// The pairing flow:
/// 1. Phone POSTs /api/legs/pair with {name, label, link: "ethernet"} -> gets code
/// 2. Phone shows code to operator
/// 3. Operator POSTs /api/legs/pair/approve with bearer token and {code}
/// 4. Phone POSTs /api/legs/pair with {code} -> gets token
public enum PairingClient {
    /// Step 1: Ask for a pairing code. Returns the code and TTL.
    public static func ask(
        url: URL,
        name: String,
        label: String,
        timeout: TimeInterval = 8,
        session: URLSession = .shared
    ) async -> Result<PairingAskResponse, PairingError> {
        let body = PairingAskRequest(name: name, label: label, link: "ethernet")
        let bodyData = try? JSONEncoder().encode(body)
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.timeoutInterval = timeout
        req.cachePolicy = .reloadIgnoringLocalCacheData
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = bodyData
        do {
            let (data, resp) = try await session.data(for: req)
            if let http = resp as? HTTPURLResponse, http.statusCode != 200 {
                return .failure(PairingError("HTTP \(http.statusCode)"))
            }
            return .success(try JSONDecoder().decode(PairingAskResponse.self, from: data))
        } catch {
            return .failure(PairingError(error.localizedDescription))
        }
    }

    /// Step 2: Operator approves the pairing code (requires bearer token).
    public static func approve(
        url: URL,
        token: String,
        code: String,
        timeout: TimeInterval = 8,
        session: URLSession = .shared
    ) async -> Result<PairingApproveResponse, PairingError> {
        let body = PairingApproveRequest(code: code)
        let bodyData = try? JSONEncoder().encode(body)
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.timeoutInterval = timeout
        req.cachePolicy = .reloadIgnoringLocalCacheData
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        req.httpBody = bodyData
        do {
            let (data, resp) = try await session.data(for: req)
            if let http = resp as? HTTPURLResponse, http.statusCode != 200 {
                return .failure(PairingError("HTTP \(http.statusCode)"))
            }
            return .success(try JSONDecoder().decode(PairingApproveResponse.self, from: data))
        } catch {
            return .failure(PairingError(error.localizedDescription))
        }
    }

    /// Step 3: Phone collects the approved token using the code.
    public static func collect(
        url: URL,
        code: String,
        timeout: TimeInterval = 8,
        session: URLSession = .shared
    ) async -> Result<PairingAskResponse, PairingError> {
        let body = PairingCollectRequest(code: code)
        let bodyData = try? JSONEncoder().encode(body)
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.timeoutInterval = timeout
        req.cachePolicy = .reloadIgnoringLocalCacheData
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = bodyData
        do {
            let (data, resp) = try await session.data(for: req)
            if let http = resp as? HTTPURLResponse, http.statusCode != 200 {
                return .failure(PairingError("HTTP \(http.statusCode)"))
            }
            return .success(try JSONDecoder().decode(PairingAskResponse.self, from: data))
        } catch {
            return .failure(PairingError(error.localizedDescription))
        }
    }
}