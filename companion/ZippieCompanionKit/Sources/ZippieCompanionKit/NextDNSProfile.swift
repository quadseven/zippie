import Foundation

/// Per-person NextDNS, so two phones behind one bond report to two different
/// profiles under two different device names.
///
/// NextDNS encodes both in the DoH URL itself:
///
///     https://dns.nextdns.io/<profile>/<device-name>
///
/// That is the whole reason this works EVERYWHERE the phone goes rather than
/// only on the router's wifi: the setting rides with the device, not with the
/// network. The router keeps its own profile for LAN clients that are not
/// running this app.
public struct NextDNSProfile: Sendable, Equatable, Codable {
    /// The NextDNS profile id, the short string from the dashboard URL.
    public let profileID: String
    /// What this phone should be called in that profile's logs. Optional -
    /// NextDNS attributes to the profile either way, it just cannot tell your
    /// phone from your laptop without it.
    public let deviceName: String

    public init(profileID: String, deviceName: String = "") {
        self.profileID = profileID
        self.deviceName = deviceName
    }

    /// Recover the identity from the configuration iOS actually loaded.
    /// Status must name this value rather than a desired value cached by the
    /// app, because a failed profile change can leave the previous resolver
    /// installed and active.
    public init?(dohURL: URL) {
        guard dohURL.scheme == "https", dohURL.host == "dns.nextdns.io" else {
            return nil
        }
        let components = dohURL.path.split(separator: "/").map(String.init)
        guard (1...2).contains(components.count) else { return nil }
        self.init(profileID: components[0],
                  deviceName: components.count == 2 ? components[1] : "")
        guard isValid else { return nil }
    }

    /// A profile id is hex-ish and short. Validating here rather than at the
    /// point of use means a typo is refused while the user is still looking at
    /// the field, instead of silently resolving nothing later - DNS failing is
    /// indistinguishable from the network being broken, which is the worst
    /// possible way to learn you mistyped an id.
    public var isValid: Bool {
        guard (4...16).contains(profileID.count) else { return false }
        return profileID.allSatisfy { $0.isHexDigit || $0.isLowercase && $0.isLetter }
    }

    /// Device names travel in a URL PATH SEGMENT, so anything that would need
    /// escaping is stripped rather than encoded. NextDNS shows the raw segment
    /// in its dashboard, and a percent-escaped name there reads as a bug.
    public var sanitizedDeviceName: String {
        let allowed = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-_"))
        let cleaned = deviceName.unicodeScalars.map { allowed.contains($0) ? Character($0) : "-" }
        return String(cleaned).trimmingCharacters(in: CharacterSet(charactersIn: "-"))
    }

    /// The DoH endpoint this phone should use.
    public var dohURL: URL? {
        guard isValid else { return nil }
        var path = "/\(profileID)"
        let name = sanitizedDeviceName
        if !name.isEmpty { path += "/\(name)" }
        return URL(string: "https://dns.nextdns.io\(path)")
    }

    /// The DoT hostname, for platforms that take a hostname rather than a URL
    /// (Android's Private DNS is the reason this exists).
    public var dotHostname: String? {
        guard isValid else { return nil }
        return "\(profileID).dns.nextdns.io"
    }
}
