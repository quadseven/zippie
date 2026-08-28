import WidgetKit
import ZippieCompanionKit

/// One rendered moment, with the content already fully decided.
///
/// Carries a `WidgetContent`, not a `RelayStatus` - the view has nothing left
/// to compute (see the note on `WidgetContent` in the Kit), so there is no
/// state a SwiftUI body could get wrong.
struct RelayEntry: TimelineEntry {
    let date: Date
    let content: WidgetContent
}

/// Reads the app-group report and asks the Kit what it may honestly say.
///
/// THIS FILE DOES NO DECIDING. Every branch that turns evidence into a
/// sentence, a tone, or a leg row lives in `WidgetTimeline.build` where
/// `swift test` can reach it - see that type's doc comment for why (#244,
/// echoing #44: a view/provider that decides its own truth cannot be tested,
/// which is how the wrong string shipped before). This file's only jobs are
/// I/O (read the shared defaults) and scheduling (ask WidgetKit to try again
/// later).
struct RelayTimelineProvider: TimelineProvider {

    /// Cheap and synchronous - a UserDefaults read, no network - so reusing it
    /// for the placeholder and the snapshot is not a shortcut, it is the same
    /// real content those two are documented to accept. See rule 4: a
    /// placeholder built from made-up copy would be exactly the invented
    /// value that rule forbids, and WidgetKit redacts this view on its own
    /// while it is shown, so there is nothing to gain by faking it.
    func placeholder(in context: Context) -> RelayEntry { currentEntry() }

    func getSnapshot(in context: Context, completion: @escaping (RelayEntry) -> Void) {
        completion(currentEntry())
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<RelayEntry>) -> Void) {
        let entry = currentEntry()
        let nextRequest = entry.date.addingTimeInterval(WidgetTimeline.requestedRefreshInterval)
        // `.after` is a request, not a guarantee - see the constant's doc
        // comment. The content's own staleness check is what actually keeps
        // this honest between whatever refreshes WidgetKit grants.
        completion(Timeline(entries: [entry], policy: .after(nextRequest)))
    }

    private func currentEntry() -> RelayEntry {
        let now = Date()
        guard let defaults = UserDefaults(suiteName: RelayConfiguration.appGroupIdentifier) else {
            // Same fallback the Kit's own store functions imply: no readable
            // suite means no report, which WidgetTimeline.build already turns
            // into the honest ".off" content rather than a fault.
            return RelayEntry(date: now, content: WidgetTimeline.build(report: nil, now: now))
        }
        let report = RelayStatusStore.read(from: defaults)
        let content = WidgetTimeline.build(report: report, now: now, router: routerDisplayName(from: defaults))
        return RelayEntry(date: now, content: content)
    }

    /// `Settings.routerDisplayName`'s exact rule (trim, nil when empty),
    /// re-read here rather than pulling the whole `Settings` type into this
    /// target - that type also owns console URLs and NextDNS settings that
    /// have nothing to do with a widget, and its `store` accessor runs a
    /// one-time migration from `UserDefaults.standard` that is the app's job
    /// to own, not a widget process that may run before the app ever has.
    /// The legacy first-SSID key remains the shared short display name while
    /// the app's full list drives on-demand matching.
    private func routerDisplayName(from defaults: UserDefaults) -> String? {
        let ssid = (defaults.string(forKey: RelayConfiguration.Key.routerSSID) ?? "")
            .trimmingCharacters(in: .whitespaces)
        return ssid.isEmpty ? nil : ssid
    }
}
