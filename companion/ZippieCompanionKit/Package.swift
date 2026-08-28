// swift-tools-version: 5.9
import PackageDescription

// Platform-neutral core for the Zippie companion (ADR 0020).
//
// Deliberately free of SwiftUI and UIKit so it builds and tests on a bare
// toolchain - `swift test` on the Mac mini runner, no Xcode project, no
// simulator, no signing. Same reason MacchinaCompanionKit is structured this
// way: the logic worth testing must be testable without a device.
let package = Package(
    name: "ZippieCompanionKit",
    platforms: [.iOS(.v16), .macOS(.v13)],
    products: [
        .library(name: "ZippieCompanionKit", targets: ["ZippieCompanionKit"]),
    ],
    targets: [
        .target(name: "ZippieCompanionKit"),
        .testTarget(
            name: "ZippieCompanionKitTests",
            dependencies: ["ZippieCompanionKit"],
            // Console JSON captured from the live router. Declared explicitly
            // so it reaches Bundle.module - without this the fixture is simply
            // absent at runtime and the decoding tests fail on a missing file
            // rather than on anything they were written to check.
            resources: [.process("Fixtures")]
        ),
    ]
)
