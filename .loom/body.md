## Why

The zippie-clienthome manifest in deploy/oke/ reads as live infrastructure but has never been deployed because there is no phone client to terminate. It should be explicitly marked as parked until legacy no. 27 lands.

## What

Add a comment at the top of the manifest stating it is parked behind legacy no. 40, that no image build exists yet, and that it must not be applied until a phone client can reach it.

## Acceptance criteria

- The manifest states plainly that it is parked, why, and what unblocks it (legacy no. 27).
- Nothing in deploy/oke/ reads as deployable without either being deployed or carrying such a note.
- The Go code and manifest are retained without deletion.

Size: XS

## Out of scope

- The multi-client home protocol design itself (legacy no. 24).
- The actual image build, publish, or deployment of the workload.

Refs #128
Part of #147
