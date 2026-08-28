import Foundation

/// The key this phone announces itself under.
///
/// THE NAME IS AN IDENTITY, NOT A LABEL. The router keys its leg table by this
/// string: announcing an existing name UPDATES that leg rather than adding one.
/// That is the good behaviour when a phone changes address, and a serious bug
/// when two different phones agree on a name - the second phone silently
/// becomes the first, inheriting its weight, tier and traffic. A bond that
/// looked like it had two legs would have one, and the leg list would show a
/// device that was not there. That exact confusion is what sent someone looking
/// for why another person's phone appeared as a leg when it was nowhere near
/// the router.
///
/// So the name is built from two parts:
///
///   base   - from the device name, for a human reading the leg list
///   suffix - four hex characters, generated ONCE and persisted
///
/// The suffix is what makes collisions a non-event: two phones both called
/// "iPhone" become `iphone-3f9a` and `iphone-b204`. It is persisted rather than
/// re-derived because a name that changes on relaunch would leave a trail of
/// dead legs behind it, each one lingering for its lease.
///
/// `identifierForVendor` is deliberately NOT used as the suffix source. It is
/// stable per vendor per device, which sounds ideal, but it changes when the
/// last app from that vendor is deleted - so a reinstall would rename the leg,
/// and it is long enough that it would dominate the readable part.
public enum LegName {

    /// The router's rule, which this must satisfy exactly:
    /// `^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$` - lowercase alphanumerics and
    /// hyphens, first and last character alphanumeric, 2 to 32 characters.
    /// A name that fails it is refused with a 400, so producing one here would
    /// mean a phone that can never join.
    public static let maxLength = 32
    public static let minLength = 2

    /// Characters that disappear rather than becoming a separator: the ASCII
    /// apostrophe and iOS's typographic one.
    private static let elided: Set<Unicode.Scalar> = ["'", "\u{2019}"]

    /// Turn anything into a name the router will accept, or nil if there is no
    /// usable character in it at all.
    ///
    /// Handles what real device names actually contain: apostrophes ("Operator's
    /// iPhone"), spaces, emoji, and non-Latin scripts. Emoji and non-Latin
    /// names collapse to nothing here, which is why the caller has a fallback
    /// rather than treating nil as impossible.
    public static func sanitise(_ raw: String) -> String? {
        var out = ""
        var lastWasHyphen = false
        for scalar in raw.lowercased().unicodeScalars {
            let c = Character(scalar)
            if c.isASCII && (c.isLetter || c.isNumber) {
                out.append(c)
                lastWasHyphen = false
            } else if Self.elided.contains(scalar) {
                // DROPPED, not turned into a separator. Apple's default device
                // name is "<Name>'s iPhone", so treating an apostrophe as a
                // word break gives "operator-s-iphone" - correct, accepted by the
                // router, and wrong to read. Note the character is U+2019, the
                // typographic apostrophe iOS actually inserts, not the ASCII
                // one people type; handling only the ASCII form would miss
                // every real device name.
                continue
            } else if !out.isEmpty && !lastWasHyphen {
                // Collapse any run of junk into ONE hyphen. Without this,
                // "Operator's  iPhone" yields "operator---iphone", which is legal but
                // reads as a mistake.
                out.append("-")
                lastWasHyphen = true
            }
        }
        while out.hasSuffix("-") { out.removeLast() }
        return out.isEmpty ? nil : out
    }

    /// Compose the final name, trimming the BASE rather than the whole string.
    ///
    /// Trimming the composed name would cut into the suffix, and a truncated
    /// suffix is a collision waiting to happen - the very thing it exists to
    /// prevent. A long device name loses its tail instead, which costs
    /// readability and nothing else.
    public static func compose(base: String?, suffix: String) -> String {
        let fallback = "phone"
        var head = base.flatMap(sanitise) ?? fallback
        let room = maxLength - suffix.count - 1  // -1 for the joining hyphen
        if head.count > room { head = String(head.prefix(room)) }
        while head.hasSuffix("-") { head.removeLast() }
        if head.isEmpty { head = fallback }
        return "\(head)-\(suffix)"
    }

    /// Four hex characters. 65,536 values against a household's worth of
    /// phones: the chance of a clash is small enough to ignore, and the
    /// consequence if it happened is visible immediately (a leg whose label
    /// keeps changing) rather than silent.
    public static func newSuffix(_ random: () -> UInt32 = { UInt32.random(in: 0...0xFFFF) }) -> String {
        String(format: "%04x", random() & 0xFFFF)
    }

    /// True if the router would accept this name. Mirrors its regex rather than
    /// approximating it - a validator that is merely close would pass names the
    /// router rejects at announce time, which fails on the device and nowhere
    /// else.
    public static func isValid(_ name: String) -> Bool {
        guard name.count >= minLength, name.count <= maxLength else { return false }
        guard let first = name.first, let last = name.last else { return false }
        guard first.isASCII, first.isLetter || first.isNumber, first.lowercased() == String(first) else { return false }
        guard last.isASCII, last.isLetter || last.isNumber, last.lowercased() == String(last) else { return false }
        return name.allSatisfy { c in
            c == "-" || (c.isASCII && (c.isLetter || c.isNumber) && c.lowercased() == String(c))
        }
    }

    /// Read the stored name, or mint and store one on first use.
    ///
    /// The `defaults` here must be the APP GROUP suite, not `.standard`: the
    /// name has to survive being read by both processes, and a name that
    /// differed between app and extension would put one leg in the UI and a
    /// different one in the bond.
    public static func resolve(in defaults: UserDefaults,
                               deviceName: String,
                               key: String = "legName") -> String {
        if let existing = defaults.string(forKey: key), isValid(existing) { return existing }
        let name = compose(base: deviceName, suffix: newSuffix())
        defaults.set(name, forKey: key)
        return name
    }
}
