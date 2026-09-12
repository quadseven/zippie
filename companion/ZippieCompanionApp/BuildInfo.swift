import Foundation

/// What commit produced this binary (#75), read at RUNTIME from the app's own
/// Info.plist rather than compiled in as a Swift literal.
///
/// THE VALUE IS WRITTEN BY A BUILD-TIME SCRIPT, NOT BY THIS FILE. See
/// `companion/scripts/embed-build-info.sh`, run as a postbuild phase on the
/// app target (project.yml) on every build - a developer's Cmd+R, a bare
/// `xcodebuild build`, and the TestFlight archive alike - which is what lets
/// it answer honestly rather than trusting whichever pipeline remembered to
/// pass a flag. That script also has the full reasoning for the two
/// provenance markers below.
///
///   "3233a99"              CI, clean tree: the commit IS the code running.
///   "3233a99-local"        built outside CI (Xcode's Run button, or a bare
///                          xcodebuild on a laptop). Same commit, but not
///                          necessarily the toolchain or signing the
///                          TestFlight pipeline would have produced.
///   "3233a99-dirty"        the working tree had uncommitted changes when
///                          this was built; the SHA names the nearest
///                          commit, not what was actually compiled.
///   "3233a99-local-dirty"  both at once - the common shape of a developer's
///                          own device build.
///   "unknown"              no git repository was found at build time.
enum BuildInfo {
    /// Falls back to "unknown" rather than omitting the field: a build whose
    /// Info.plist was never stamped (the build phase did not run, or Info.plist
    /// processing changed shape upstream) must still show something, rather
    /// than silently dropping the one piece of information this change exists
    /// to add.
    ///
    /// Wired into Observability.swift's Datadog attributes (`build_commit`,
    /// alongside `platform`/`device`) - the TODO this file used to carry.
    static var commitLabel: String {
        (Bundle.main.infoDictionary?["ZippieGitCommit"] as? String).flatMap {
            $0.isEmpty ? nil : $0
        } ?? "unknown"
    }
}
