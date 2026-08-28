import SwiftUI
import WidgetKit
import ZippieCompanionKit

/// Picks small or medium. Nothing else - no large, no interactive buttons,
/// per the issue's explicit scope. `Theme.swift` is shared straight from the
/// app target (see project.yml) rather than copied, so `Ink` stays the one
/// place a colour is defined and a token change moves both surfaces together.
struct RelayWidgetView: View {
    @Environment(\.widgetFamily) private var family
    let entry: RelayEntry

    var body: some View {
        switch family {
        case .systemMedium:
            MediumRelayView(content: entry.content)
        default:
            // .systemSmall is the only other family this widget declares
            // (see RelayWidget.swift's supportedFamilies), so this default
            // is not a silent catch-all for a size the app never claimed to
            // support.
            SmallRelayView(content: entry.content)
        }
    }
}

/// Small: the verdict headline and sentence, plus one state dot. No numbers -
/// DESIGN.md's whole argument is that a number cannot say whether traffic is
/// actually moving, and a widget has even less room to earn one than a screen
/// does.
struct SmallRelayView: View {
    let content: WidgetContent

    var body: some View {
        VStack(alignment: .leading, spacing: Space.tight) {
            HStack(alignment: .center, spacing: Space.hair) {
                StateDot(tone: content.tone)
                Text(content.headline)
                    // Kind.display() is 40pt - sized for a phone screen, not a
                    // widget tile. Keeping the SAME weight and system stack
                    // (see Theme.swift) carries the identity of "the state
                    // sentence" without a size this space cannot fit.
                    .font(.system(size: 20, weight: .semibold))
                    .foregroundStyle(Ink.primary)
                    .lineLimit(1)
                    .minimumScaleFactor(0.8)
            }
            Text(content.detail)
                .font(Kind.caption())
                .foregroundStyle(Ink.secondary)
                .lineLimit(3)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .padding(Space.base)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
        .widgetBackground(Ink.ground)
    }
}

/// Medium: the small content, plus the leg list. "Is MY phone one of the
/// legs" is the question DESIGN.md says only this list can answer at a
/// glance - see WidgetLeg's doc comment in the Kit for why this PR's list has
/// exactly one honest row rather than the router's full bond.
struct MediumRelayView: View {
    let content: WidgetContent

    var body: some View {
        VStack(alignment: .leading, spacing: Space.tight) {
            HStack(alignment: .center, spacing: Space.hair) {
                StateDot(tone: content.tone)
                Text(content.headline)
                    .font(.system(size: 22, weight: .semibold))
                    .foregroundStyle(Ink.primary)
                    .lineLimit(1)
                    .minimumScaleFactor(0.8)
            }
            Text(content.detail)
                .font(Kind.caption())
                .foregroundStyle(Ink.secondary)
                .lineLimit(2)
                .fixedSize(horizontal: false, vertical: true)

            // No leg section at all when there is nothing honest to say about
            // one yet (off, starting, a corpse report) - an empty section
            // with a dash would be exactly the placeholder rule 4 forbids.
            if !content.legs.isEmpty {
                Hairline()
                    .padding(.vertical, Space.hair)
                VStack(alignment: .leading, spacing: Space.hair) {
                    ForEach(content.legs, id: \.label) { leg in
                        LegLine(leg: leg)
                    }
                }
            }
            Spacer(minLength: 0)
        }
        .padding(Space.base)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
        .widgetBackground(Ink.ground)
    }
}

/// One row of the leg list: label, then the word for its state. The colour
/// carries the same three-way distinction `LegRow`'s `TrafficBar` uses for a
/// full leg row (`Ink.live` / `Ink.down` / `Ink.tertiary`) - `.tertiary`
/// because an idle leg is not news, matching that view's own comment.
struct LegLine: View {
    let leg: WidgetLeg

    private var dotColor: Color {
        switch leg.state {
        case .carrying: return Ink.live
        case .down:     return Ink.down
        case .idle:     return Ink.tertiary
        }
    }

    /// "carrying" / "not carrying" only, matching `BondModel`'s own
    /// single-phone fallback row - it does not surface "idle" vs "down" as
    /// words either, only as the dot colour, so this widget does not invent a
    /// vocabulary the app's own equivalent row does not use.
    private var stateWord: String { leg.state == .carrying ? "carrying" : "not carrying" }

    var body: some View {
        HStack(spacing: Space.hair) {
            Circle().fill(dotColor).frame(width: 6, height: 6)
            Text(leg.label)
                .font(Kind.caption())
                .foregroundStyle(Ink.primary)
            Text(stateWord)
                .font(Kind.caption())
                .foregroundStyle(leg.state == .carrying ? Ink.live : Ink.secondary)
        }
    }
}

/// The one accent dot. `.live` is the only state colour that means anything
/// other than "not carrying, and here is why" - see `WidgetContent.Tone`.
struct StateDot: View {
    let tone: WidgetContent.Tone

    private var color: Color {
        switch tone {
        case .live:    return Ink.live
        case .down:    return Ink.down
        // Never the failure colour for a state that has not failed - off,
        // starting, or simply idle all read the same as LegRow's `.idle`.
        case .neutral: return Ink.tertiary
        }
    }

    var body: some View {
        Circle().fill(color).frame(width: 10, height: 10)
    }
}

private extension View {
    /// iOS 17 requires a widget to declare its background this way, or the
    /// system paints a default one over whatever this view drew; iOS 16 (the
    /// deployment target in project.yml) has no `containerBackground` at all
    /// and expects a plain `.background`. Both must be satisfied at once.
    @ViewBuilder
    func widgetBackground(_ color: Color) -> some View {
        if #available(iOSApplicationExtension 17.0, *) {
            containerBackground(color, for: .widget)
        } else {
            background(color)
        }
    }
}
