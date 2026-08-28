//go:build tools

// gomobile bind needs golang.org/x/mobile present in the module graph even
// though no source here imports it - without this, `go mod tidy` drops it and
// the bind fails with "missing golang.org/x/mobile dependency", which reads
// like gomobile is not installed rather than like a pruned requirement.
package mobile

import (
	_ "golang.org/x/mobile/bind"
)
