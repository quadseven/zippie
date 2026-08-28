import SwiftUI
import WidgetKit

/// The home-screen widget for issue #244: "is it working, and on what",
/// without opening the app.
///
/// Small and medium only. Large is explicitly out of scope until the other
/// two have been lived with (DESIGN.md rejects the hero-metric template, and
/// a large size is the easiest way to smuggle a chart back in). No
/// interactivity - starting or stopping a tunnel from the home screen is a
/// bigger decision than showing state and wants its own issue.
struct RelayWidget: Widget {
    let kind = "RelayWidget"

    var body: some WidgetConfiguration {
        StaticConfiguration(kind: kind, provider: RelayTimelineProvider()) { entry in
            RelayWidgetView(entry: entry)
        }
        .configurationDisplayName("Zippie")
        .description("Is it working, and on what.")
        .supportedFamilies([.systemSmall, .systemMedium])
    }
}
