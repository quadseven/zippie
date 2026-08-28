import SwiftUI
import ZippieCompanionKit

/// One leg, in depth: how it has behaved, and what you can change about it.
///
/// TWO SCREENS' WORTH OF MATERIAL, ONE DESTINATION. History answers "is this
/// leg any good" and the editor answers "then change it" - and they are the
/// same thought a few seconds apart. Splitting them across two navigation
/// destinations would mean going back to the list to act on what you just
/// read.
///
/// History comes FIRST, deliberately. The editor's controls change routing for
/// the whole household, and the evidence for a change belongs above the
/// controls that make it, not behind a second tap.
struct LegDetail: View {
    let leg: Leg
    @ObservedObject var model: BondModel

    var body: some View {
        LegHistoryView(
            legID: leg.id,
            title: leg.name,
            // The series carries no usage or cap, so those come from the
            // status snapshot the model already holds rather than a second
            // fetch that could disagree with what the list is showing.
            usage: model.bond?.paths?
                .first { $0.name == leg.id }
                .flatMap(LegUsage.init(path:))
        )
        .safeAreaInset(edge: .bottom) {
            NavigationLink {
                LegEditor(legName: leg.id, legLabel: leg.name)
            } label: {
                Text("Change this connection")
                    .font(Kind.label())
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, Space.base)
                    .foregroundStyle(Ink.raised)
                    .background(Ink.live)
                    .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                    .padding(.horizontal, Space.margin)
                    .padding(.bottom, Space.tight)
            }
            .buttonStyle(.plain)
            .background(.ultraThinMaterial)
        }
    }
}
