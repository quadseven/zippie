import Foundation

/// What choosing a nearby peer writes.
///
/// The result is an ordinary `LegEdit` for `PUT /api/legs/<name>`, built from
/// the same fields a manual edit sets, so choosing a peer and typing the same
/// values produce the same leg config.
public enum NearbyLeg {

    /// The leg name for a verified peer; always valid under `LegName`.
    public static func name(for peer: VerifiedPeer) -> String {
        "peer-\(peer.clientID)"
    }

    /// The edit that adds `peer` as an enabled leg labeled with its id.
    public static func edit(for peer: VerifiedPeer) -> LegEdit {
        var edit = LegEdit()
        edit.set(.label, .text("Nearby peer \(peer.clientID)"))
        edit.set(.enabled, .flag(true))
        return edit
    }
}
