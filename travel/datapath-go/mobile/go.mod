// A SEPARATE MODULE ON PURPOSE.
//
// gomobile bind pulls in golang.org/x/mobile, which would give the core
// datapath a go.sum and dependencies. That module is deliberately stdlib-only
// - the CI gate sets `cache: false` precisely because there is no go.sum to
// cache, and the fuzz corpus and golden vectors all run against a package with
// nothing under it. Keeping the binding here means the phone build can have
// dependencies without the router build growing any.
module github.com/quadseven/zippie-datapath/mobile

go 1.25.0

require (
	github.com/quadseven/zippie-datapath v0.0.0
	golang.org/x/mobile v0.0.0-20260803200217-62cee1672c8e
)

require (
	golang.org/x/mod v0.38.0 // indirect
	golang.org/x/sync v0.22.0 // indirect
	golang.org/x/tools v0.48.0 // indirect
)

replace github.com/quadseven/zippie-datapath => ../
