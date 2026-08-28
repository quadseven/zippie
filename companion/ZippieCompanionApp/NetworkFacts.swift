import CoreTelephony
import Foundation
import Network
import NetworkExtension

/// What the phone can actually tell us about its own radios.
///
/// WHAT iOS GIVES YOU, AND WHAT IT FLATLY DOES NOT
/// ------------------------------------------------
/// Available:
///   - Wi-Fi SSID, BSSID and signal strength (0...1), via NEHotspotNetwork.
///     Needs the Access WiFi Information entitlement AND location permission -
///     Apple treats "which network are you on" as location data, because a
///     BSSID is a geolocation lookup away.
///   - Cellular radio access technology (5G NR, LTE, ...) via CoreTelephony.
///   - Which interfaces the system currently considers usable, via NWPath.
///
/// NOT available to any third-party app, at any entitlement level:
///   - Cellular signal strength. No RSRP, no RSSI, no bars. There is no public
///     API and there never has been. Anything claiming otherwise is either
///     private API or a guess derived from throughput.
///   - Carrier name is deprecated from iOS 16 and returns a placeholder.
///
/// That gap matters less than it looks. Zippie does not schedule on bars - it
/// schedules on measured RTT and loss. A measured round trip over a pinned
/// interface is strictly better evidence than a vendor's signal indicator,
/// because it reflects the path end to end rather than the first hop.
struct NetworkFacts {
    var ssid: String?
    var bssid: String?
    /// 0.0...1.0. Apple does not document a dBm mapping, so treat it as ordinal.
    var wifiSignal: Double?
    var radioTech: String?
    var hasWifi = false
    var hasCellular = false
    var isConstrained = false   // Low Data Mode
    var isExpensive = false     // cellular or personal hotspot

    /// Radio technology, normalised. CoreTelephony returns verbose constants
    /// like "CTRadioAccessTechnologyNRNSA" which are noise in a dashboard.
    static func shortRadio(_ raw: String?) -> String? {
        guard let raw else { return nil }
        let t = raw.replacingOccurrences(of: "CTRadioAccessTechnology", with: "")
        switch t {
        case "NR": return "5G SA"
        case "NRNSA": return "5G NSA"
        case "LTE": return "LTE"
        case "WCDMA", "HSDPA", "HSUPA": return "3G"
        case "GPRS", "Edge", "CDMA1x": return "2G"
        default: return t.isEmpty ? nil : t
        }
    }

    static func current() async -> NetworkFacts {
        var f = NetworkFacts()

        // Interface availability comes from NWPathMonitor rather than being
        // inferred: "cellular exists" and "cellular is usable right now" are
        // different questions, and the relay cares about the second.
        let monitor = NWPathMonitor()
        // MODULE-QUALIFIED on purpose. This file imports both Network and
        // NetworkExtension, and both define a type called NWPath: the modern
        // Network.NWPath struct, and NetworkExtension's deprecated ObjC class
        // of the same name. A bare `NWPath` is ambiguous and fails to compile
        // against the iOS 26 SDK - the error names neither import, so the fix
        // is far from obvious from the message alone.
        let path: Network.NWPath = await withCheckedContinuation { cont in
            var resumed = false
            monitor.pathUpdateHandler = { p in
                guard !resumed else { return }
                resumed = true
                cont.resume(returning: p)
            }
            monitor.start(queue: .global(qos: .userInitiated))
        }
        monitor.cancel()
        f.hasWifi = path.availableInterfaces.contains { $0.type == .wifi }
        f.hasCellular = path.availableInterfaces.contains { $0.type == .cellular }
        f.isConstrained = path.isConstrained
        f.isExpensive = path.isExpensive

        f.radioTech = shortRadio(
            CTTelephonyNetworkInfo().serviceCurrentRadioAccessTechnology?.values.first
        )

        if let net = await currentHotspot() {
            f.ssid = net.ssid
            f.bssid = net.bssid
            f.wifiSignal = net.signalStrength
        }
        return f
    }

    private static func currentHotspot() async -> NEHotspotNetwork? {
        await withCheckedContinuation { cont in
            NEHotspotNetwork.fetchCurrent { cont.resume(returning: $0) }
        }
    }

    /// Flattened for Datadog. Nils are dropped rather than sent as "unknown",
    /// so a missing SSID reads as absent instead of as a network named unknown.
    var attributes: [String: Encodable] {
        var a: [String: Encodable] = [
            "net.wifi_available": hasWifi,
            "net.cellular_available": hasCellular,
            "net.constrained": isConstrained,
            "net.expensive": isExpensive,
        ]
        if let ssid { a["net.ssid"] = ssid }
        if let bssid { a["net.bssid"] = bssid }
        if let wifiSignal { a["net.wifi_signal"] = wifiSignal }
        if let radioTech { a["net.radio"] = radioTech }
        return a
    }
}
