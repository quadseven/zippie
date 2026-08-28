import SwiftUI
import WidgetKit

/// The extension's entry point. `@main` on a `WidgetBundle` is the whole of
/// what a modern WidgetKit extension's Info.plist needs to declare (see
/// ZippieCompanionWidget/Info.plist) - no NSExtensionPrincipalClass, unlike
/// the packet-tunnel extension, which is an older extension point that still
/// requires one.
@main
struct ZippieCompanionWidgetBundle: WidgetBundle {
    var body: some Widget {
        RelayWidget()
    }
}
