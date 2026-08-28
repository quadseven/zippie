import SwiftUI

/// The shared parts of the two task screens.
///
/// Extracted rather than duplicated because Relay and Probe were the surfaces
/// still wearing stock `List` + `LabeledContent`, and rebuilding them
/// independently is how two screens in a three-screen app end up with two
/// button styles.
///
/// NO CARDS HERE EITHER. Same rule as the status screen: hairlines and space.
/// A card is three edges and a shadow spent saying what a gap already says.

/// A section heading with the rhythm the page expects - more air above than
/// below, so a heading belongs to what follows it rather than floating between.
struct SectionHead: View {
    let title: String
    var note: String?

    var body: some View {
        VStack(alignment: .leading, spacing: Space.hair) {
            Text(title)
                .font(Kind.section())
                .foregroundStyle(Ink.primary)
            if let note {
                Text(note)
                    .font(Kind.caption())
                    .foregroundStyle(Ink.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.top, Space.section)
        .padding(.bottom, Space.snug)
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// A label with its value on the right, sharing the page's single left edge.
///
/// The value uses tabular figures so a changing number does not shift the
/// layout under a thumb - the reason this is not just an HStack.
struct Readout: View {
    let label: String
    let value: String
    var tone: Color = Ink.primary

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: Space.base) {
            Text(label)
                .font(Kind.body())
                .foregroundStyle(Ink.secondary)
            Spacer(minLength: Space.snug)
            Text(value)
                .font(Kind.figure(17))
                .foregroundStyle(tone)
                .multilineTextAlignment(.trailing)
        }
        .padding(.vertical, Space.snug)
        .accessibilityElement(children: .combine)
    }
}

/// The one button style in the app.
///
/// FULL WIDTH AND FLAT, because every primary action here is the only thing
/// worth doing on its screen, and it is pressed in a moving car. A destructive
/// action takes the same shape in a different ink rather than a different
/// shape - the muscle memory should be identical, the colour is the warning.
struct ActionButton: View {
    enum Role { case primary, destructive, quiet }

    let title: String
    var role: Role = .primary
    var busy = false
    var enabled = true
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: Space.tight) {
                Text(title)
                    .font(Kind.label())
                if busy {
                    ProgressView()
                        .controlSize(.small)
                        .tint(foreground)
                }
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, Space.base)
            .foregroundStyle(foreground)
            .background(background)
            .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .strokeBorder(role == .quiet ? Ink.rule : .clear)
            )
        }
        .disabled(!enabled || busy)
        .opacity(enabled ? 1 : 0.4)
        .buttonStyle(.plain)
    }

    private var foreground: Color {
        switch role {
        case .primary:     return Ink.raised
        case .destructive: return Ink.down
        case .quiet:       return Ink.primary
        }
    }

    private var background: Color {
        switch role {
        case .primary:     return Ink.live
        case .destructive: return Ink.down.opacity(0.10)
        case .quiet:       return .clear
        }
    }
}

/// An editable setting. The field is right-aligned so its value sits in the
/// same column as every read-only value on the page - a form that jumps its
/// alignment when it becomes editable reads as two different pages.
struct FieldRow: View {
    let label: String
    @Binding var text: String
    var keyboard: UIKeyboardType = .default

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: Space.base) {
            Text(label)
                .font(Kind.body())
                .foregroundStyle(Ink.secondary)
            Spacer(minLength: Space.snug)
            TextField("", text: $text)
                .font(Kind.figure(17))
                .foregroundStyle(Ink.primary)
                .multilineTextAlignment(.trailing)
                .keyboardType(keyboard)
                .autocorrectionDisabled()
                .textInputAutocapitalization(.never)
        }
        .padding(.vertical, Space.snug)
    }
}

/// A sentence that explains or warns, in the tone its content earns.
struct Note: View {
    enum Tone { case plain, warning, bad }

    let text: String
    var tone: Tone = .plain

    var body: some View {
        Text(text)
            .font(Kind.caption())
            .foregroundStyle(colour)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.top, Space.tight)
    }

    private var colour: Color {
        switch tone {
        case .plain:   return Ink.tertiary
        case .warning: return Ink.degraded
        case .bad:     return Ink.down
        }
    }
}

/// The page shell: one margin, one ground, room to breathe at the bottom so the
/// last row is not jammed against the tab bar.
struct Page<Content: View>: View {
    @ViewBuilder let content: Content

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                content
            }
            .padding(.horizontal, Space.margin)
            .padding(.bottom, Space.major)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(Ink.ground.ignoresSafeArea())
        .scrollDismissesKeyboard(.interactively)
    }
}
