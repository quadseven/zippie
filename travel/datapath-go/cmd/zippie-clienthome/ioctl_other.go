//go:build !linux

package main

import "errors"

// Never reached - openTUN refuses non-Linux before it gets here - but the
// symbol has to exist so the package compiles and its tests run on a Mac.
func ioctl(fd, req, arg uintptr) error { return errors.New("ioctl: linux only") }
