# zippie datapath: mobile bindings

`gomobile bind` output of the core datapath, for the iOS Network Extension and
the Android VpnService. One engine, three callers (router, iOS, Android).

## Why this is a separate module

`gomobile bind` requires `golang.org/x/mobile`, which would give the CORE
datapath a `go.sum` and a dependency tree. That module is deliberately
stdlib-only: the CI gate sets `cache: false` precisely because there is no
`go.sum`, and the fuzz corpus and Python golden vectors all run against a
package with nothing underneath it. Keeping the binding here lets the phone
build have dependencies without the router build growing any.

## What gomobile can carry

Only `string`, `int`, `bool`, `[]byte`, `error`, and exported methods on
exported structs from THIS package. No maps, no slices of structs, no
generics, no variadics. That is why configuration and stats cross as JSON
strings rather than as the rich types the core uses.

The constraint is also a feature: it forces one narrow contract that both
platforms call, in the same vocabulary the router already drives over its
control socket.

## Building

    gomobile bind -target=ios     -o Zippie.xcframework github.com/quadseven/zippie-datapath/mobile
    gomobile bind -target=android -androidapi 29 -o zippie.aar github.com/quadseven/zippie-datapath/mobile

Android needs `ANDROID_HOME` and `ANDROID_NDK_HOME`. Only the macOS runner
has both Xcode and the Android SDK, so CI runs this there.

`tools.go` exists because nothing in this package imports `x/mobile`, so
`go mod tidy` prunes it and the bind then fails with "missing
golang.org/x/mobile dependency" - which reads like gomobile is not installed
rather than like a pruned requirement.

## Status

Both artifacts build (2026-08-05): xcframework with device and simulator
slices, and a ~10 MB aar. NEITHER IS WIRED INTO AN APP YET - that is client
mode (#2247 iOS, #2248 Android). Building is not carrying.
