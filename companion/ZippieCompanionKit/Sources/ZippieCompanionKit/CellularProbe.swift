import Foundation
import Network

/// Fetches the public egress address over a CHOSEN interface.
///
/// WHY NOT URLSession
/// ------------------
/// `URLSessionConfiguration.allowsCellularAccess` PERMITS cellular, it does not
/// FORCE it - with Wi-Fi up, URLSession will still take Wi-Fi. There is no
/// public URLSession knob that pins a request to the cellular radio. Only
/// `NWParameters.requiredInterfaceType` does that, which is why this speaks a
/// minimal HTTP/1.1 request over `NWConnection` instead.
///
/// Keep the request trivial: one GET, `Connection: close`, plain text response.
/// This is a proof harness, not an HTTP client, and every feature added here is
/// a way for the harness itself to be the thing that is broken.
public struct CellularProbe: Sendable {
    public struct Endpoint: Sendable {
        public let host: String
        public let path: String
        public let port: UInt16
        public let tls: Bool

        public init(host: String, path: String, port: UInt16 = 443, tls: Bool = true) {
            self.host = host
            self.path = path
            self.port = port
            self.tls = tls
        }

        /// HTTPS, and the reason is not paranoia - it is correctness.
        ///
        /// v1 used plain HTTP, reasoning that TLS added a handshake and a
        /// second failure mode. That reasoning was wrong: iCloud Private Relay
        /// proxies INSECURE HTTP app traffic specifically, so the probe was
        /// measuring Private Relay exits (146.75.245.47 Albany vs .73
        /// Liverpool) and reporting PROVEN. HTTPS bypasses Private Relay, so
        /// the observed address is the real egress.
        public static let ifconfigMe = Endpoint(host: "ifconfig.me", path: "/ip", port: 443, tls: true)
        public static let icanhazip = Endpoint(host: "icanhazip.com", path: "/", port: 443, tls: true)
    }

    public let timeoutSeconds: Int

    public init(timeoutSeconds: Int = 10) {
        self.timeoutSeconds = timeoutSeconds
    }

    /// Fetch the egress address, optionally pinned to one interface type.
    /// `interface: nil` means "whatever the system prefers" - the baseline.
    public func egressAddress(
        endpoint: Endpoint = .ifconfigMe,
        interface: NWInterface.InterfaceType?
    ) async -> Result<String, ProbeError> {
        let params: NWParameters = endpoint.tls ? .tls : .tcp
        if let interface {
            // The single line the entire companion design depends on.
            params.requiredInterfaceType = interface
            // Refuse to silently fall back to another path. Without this, a
            // system that ignores the requirement would quietly answer over
            // Wi-Fi and the harness would report a false PROVEN - the one
            // outcome that would send us building on a false premise.
            params.prohibitExpensivePaths = false
        }

        let connection = NWConnection(
            host: NWEndpoint.Host(endpoint.host),
            port: NWEndpoint.Port(rawValue: endpoint.port)!,
            using: params
        )

        return await withCheckedContinuation { continuation in
            let finished = Locked(false)
            func finish(_ result: Result<String, ProbeError>) {
                guard finished.exchange(true) == false else { return }
                connection.cancel()
                continuation.resume(returning: result)
            }

            let deadline = DispatchQueue.global().schedule(
                after: .init(.now() + .seconds(timeoutSeconds)),
                interval: .seconds(3600)
            ) {
                finish(.failure(.timedOut(seconds: timeoutSeconds)))
            }

            connection.stateUpdateHandler = { state in
                switch state {
                case .ready:
                    let request = """
                    GET \(endpoint.path) HTTP/1.1\r
                    Host: \(endpoint.host)\r
                    User-Agent: zippie-companion-probe\r
                    Connection: close\r
                    \r

                    """
                    connection.send(content: Data(request.utf8), completion: .contentProcessed { error in
                        if let error {
                            finish(.failure(.connectionFailed(error.localizedDescription)))
                        }
                    })
                    receive(on: connection, accumulated: Data(), finish: finish)

                case let .failed(error):
                    finish(.failure(.connectionFailed(error.localizedDescription)))

                case .waiting:
                    // `.waiting` with a required interface usually means that
                    // interface is not currently usable. Reporting it as a
                    // distinct error keeps "cellular is off" from being
                    // misread as "binding does not work".
                    if interface != nil {
                        finish(.failure(.noInterfaceAvailable))
                    }

                case .cancelled:
                    finish(.failure(.connectionFailed("cancelled")))

                default:
                    break
                }
            }

            connection.start(queue: .global(qos: .userInitiated))
            _ = deadline
        }
    }

    private func receive(
        on connection: NWConnection,
        accumulated: Data,
        finish: @escaping (Result<String, ProbeError>) -> Void
    ) {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 8192) { data, _, isComplete, error in
            if let error {
                finish(.failure(.connectionFailed(error.localizedDescription)))
                return
            }
            var buffer = accumulated
            if let data { buffer.append(data) }

            if isComplete {
                guard let text = String(data: buffer, encoding: .utf8) else {
                    finish(.failure(.badResponse("non-utf8 body")))
                    return
                }
                guard let body = Self.httpBody(of: text) else {
                    finish(.failure(.badResponse("no header/body separator")))
                    return
                }
                let trimmed = body.trimmed()
                guard !trimmed.isEmpty else {
                    finish(.failure(.badResponse("empty body")))
                    return
                }
                finish(.success(trimmed))
                return
            }
            receive(on: connection, accumulated: buffer, finish: finish)
        }
    }

    /// Split an HTTP/1.1 response at the blank line. Exposed for tests so the
    /// parsing is verified without a socket.
    public static func httpBody(of response: String) -> String? {
        if let range = response.range(of: "\r\n\r\n") {
            return String(response[range.upperBound...])
        }
        if let range = response.range(of: "\n\n") {
            return String(response[range.upperBound...])
        }
        return nil
    }
}

/// Minimal mutual-exclusion box. `NWConnection` callbacks arrive on a
/// concurrent queue and the timeout can race them, so the continuation must be
/// resumed exactly once - resuming twice is a crash, not a warning.
final class Locked<Value>: @unchecked Sendable {
    private var value: Value
    private let lock = NSLock()

    init(_ value: Value) { self.value = value }

    func exchange(_ new: Value) -> Value {
        lock.lock()
        defer { lock.unlock() }
        let old = value
        value = new
        return old
    }
}
