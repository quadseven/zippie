import SwiftUI

// THESIS: this app answers "is it working, and on what" in one glance from a
// car dashboard. It refuses the hero-metric template every bonding app ships -
// giant speed number, small label, stat row - because speed is not the question
// mid-drive and a number cannot say whether YOUR phone is helping.
// OWN-WORLD: paper-white ground, near-black text, a single ink-blue accent used
// only for live carriage. Hairline rules instead of cards. Tabular numerals.
// Space as the primary structural device; no chrome that is not load-bearing.
// STORY: the reader sees a plain sentence of state, then the legs that back it,
// then what it has cost. They leave knowing whether to worry.
// FIRST VIEWPORT: state sentence at display scale, legs beneath as
// hairline-separated rows carrying a real share bar and latency, budget last.
// FORM: calm utility, user-pinned over the roll.
// FINISH: unreviewed and undocumented is unfinished; this build ends with the
// finish review, the verdict, and DESIGN.md.

/// The design system. One file, because a two-screen app with a scattered
/// token layer is how drift starts.
///
/// LIGHT-FIRST, AND THAT IS A DECISION FROM THE USE SCENE, not a default. This
/// is read on a car dashboard in daylight far more often than in the dark, and
/// a dark UI in direct sun is a mirror. Dark mode is fully supported because
/// iOS demands it, but the light rendition is the one that was designed.
enum Ink {
    /// The ground. Warmer than pure white so large fields do not glare, but
    /// nowhere near cream - this is paper, not parchment.
    static let ground = Color(light: .init(white: 0.985), dark: .init(white: 0.055))
    /// Raised surfaces. Used sparingly; most of this UI sits directly on ground.
    static let raised = Color(light: .init(white: 1.0), dark: .init(white: 0.105))

    static let primary = Color(light: .init(white: 0.08), dark: .init(white: 0.96))
    /// Secondary text is TINTED from the ground, never neutral gray - gray on a
    /// warm ground reads as dirty at low contrast.
    static let secondary = Color(light: .init(red: 0.36, green: 0.37, blue: 0.40),
                                 dark: .init(red: 0.62, green: 0.64, blue: 0.68))
    static let tertiary = Color(light: .init(red: 0.55, green: 0.56, blue: 0.60),
                                dark: .init(red: 0.45, green: 0.47, blue: 0.51))

    /// Hairlines. The primary structural device in place of cards.
    static let rule = Color(light: .init(white: 0.88), dark: .init(white: 0.20))

    /// THE ONLY ACCENT, and it means exactly one thing: this is carrying
    /// traffic right now. Spending it on decoration would cost the app its
    /// single unambiguous signal.
    static let live = Color(light: .init(red: 0.09, green: 0.34, blue: 0.84),
                            dark: .init(red: 0.44, green: 0.64, blue: 1.0))
    /// Degraded is amber rather than red: a leg holding on is not a failure,
    /// and crying wolf in a car is worse than saying nothing.
    static let degraded = Color(light: .init(red: 0.70, green: 0.44, blue: 0.02),
                                dark: .init(red: 0.98, green: 0.72, blue: 0.25))
    static let down = Color(light: .init(red: 0.72, green: 0.18, blue: 0.15),
                            dark: .init(red: 1.0, green: 0.45, blue: 0.40))
}

extension Color {
    init(light: UIColor.Components, dark: UIColor.Components) {
        self.init(uiColor: UIColor { $0.userInterfaceStyle == .dark ? dark.ui : light.ui })
    }
    struct ComponentsBox {}
}

extension UIColor {
    struct Components {
        var red: CGFloat = 0, green: CGFloat = 0, blue: CGFloat = 0, white: CGFloat? = nil
        init(white: CGFloat) { self.white = white }
        init(red: CGFloat, green: CGFloat, blue: CGFloat) {
            self.red = red; self.green = green; self.blue = blue
        }
        var ui: UIColor {
            if let w = white { return UIColor(white: w, alpha: 1) }
            return UIColor(red: red, green: green, blue: blue, alpha: 1)
        }
    }
}

/// Type. SF, used properly rather than replaced - a system stack is the right
/// workhorse for a task surface, and a display face here would be costume.
/// What earns the craft is the SCALE, the tracking, and tabular numerals
/// everywhere a number can change without the layout moving.
enum Kind {
    /// The state sentence. Large, tight, and the only thing at this size.
    static func display() -> Font {
        .system(size: 40, weight: .semibold, design: .default)
    }
    static func title() -> Font { .system(size: 22, weight: .semibold) }
    static func body() -> Font { .system(size: 17, weight: .regular) }
    /// Row labels: the leg's name.
    static func label() -> Font { .system(size: 17, weight: .medium) }
    /// Every changing number. Monospaced DIGITS only - not a mono face, which
    /// would be technical costume - so values do not jitter as they update.
    static func figure(_ size: CGFloat = 17, _ weight: Font.Weight = .regular) -> Font {
        .system(size: size, weight: weight).monospacedDigit()
    }
    static func caption() -> Font { .system(size: 13, weight: .regular) }
    /// Section headers. Not tracked-out micro caps: that is the eyebrow habit
    /// wearing a different hat.
    static func section() -> Font { .system(size: 15, weight: .semibold) }
}

/// Spacing. One rhythm, and more space above a heading than below it.
enum Space {
    static let hair: CGFloat = 4
    static let tight: CGFloat = 8
    static let snug: CGFloat = 12
    static let base: CGFloat = 16
    static let roomy: CGFloat = 24
    static let section: CGFloat = 40
    static let major: CGFloat = 56
    /// The single horizontal margin. Everything aligns to it, including the
    /// hairlines, so the page has one left edge.
    static let margin: CGFloat = 20
}

/// A hairline that respects the display scale. A 1pt "hairline" on a 3x screen
/// is three device pixels and reads as a border; this is the real thing.
struct Hairline: View {
    var inset: CGFloat = 0
    var body: some View {
        Rectangle()
            .fill(Ink.rule)
            .frame(height: 1 / UIScreen.main.scale)
            .padding(.leading, inset)
    }
}
