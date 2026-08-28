// GENERATED FROM design/tokens.json - DO NOT EDIT.
// Run design/generate.py after changing a token.

import SwiftUI

/// The zippie visual language, shared with the hub and the Android app.
enum Tok {
    enum Color_ {}
}

extension Ink {
    /// the page
    static let gen_ground = Color(
        light: .init(red: 0.9843, green: 0.9843, blue: 0.9843),
        dark: .init(red: 0.0549, green: 0.0549, blue: 0.0549))
    /// raised surfaces, used sparingly
    static let gen_raised = Color(
        light: .init(red: 1.0000, green: 1.0000, blue: 1.0000),
        dark: .init(red: 0.1059, green: 0.1059, blue: 0.1059))
    /// text
    static let gen_primary = Color(
        light: .init(red: 0.0784, green: 0.0784, blue: 0.0784),
        dark: .init(red: 0.9608, green: 0.9608, blue: 0.9608))
    /// supporting text, tinted from the ground and never neutral gray
    static let gen_secondary = Color(
        light: .init(red: 0.3608, green: 0.3725, blue: 0.4000),
        dark: .init(red: 0.6196, green: 0.6392, blue: 0.6824))
    /// the quietest text; a reserve leg is not news
    static let gen_tertiary = Color(
        light: .init(red: 0.5490, green: 0.5608, blue: 0.6000),
        dark: .init(red: 0.4510, green: 0.4706, blue: 0.5098))
    /// hairlines, the structural device in place of cards
    static let gen_rule = Color(
        light: .init(red: 0.8784, green: 0.8784, blue: 0.8784),
        dark: .init(red: 0.2000, green: 0.2000, blue: 0.2000))
    /// CARRYING TRAFFIC RIGHT NOW - the only accent, and it means exactly one thing
    static let gen_live = Color(
        light: .init(red: 0.0902, green: 0.3412, blue: 0.8392),
        dark: .init(red: 0.4392, green: 0.6392, blue: 1.0000))
    /// holding on, not failing - amber because crying wolf in a car is worse than saying nothing
    static let gen_degraded = Color(
        light: .init(red: 0.7020, green: 0.4431, blue: 0.0196),
        dark: .init(red: 0.9804, green: 0.7216, blue: 0.2510))
    /// not carrying
    static let gen_down = Color(
        light: .init(red: 0.7216, green: 0.1804, blue: 0.1490),
        dark: .init(red: 1.0000, green: 0.4510, blue: 0.4000))
}

enum StateWord {
    static let carrying = "carrying"
    static let carryingDegraded = "carrying, degraded"
    static let reserve = "held in reserve"
    static let notInBond = "not in the bond"
    static let upNotCarrying = "up, not carrying"
    static let notConnected = "not connected"
    static let down = "down"
    static let idle = "idle"
}
