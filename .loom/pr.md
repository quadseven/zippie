# T321: Complete criteria left from PR #320

## Why
The lane pull request marked these ticket criteria as not built.

## What
Complete the remaining acceptance criteria.

## Acceptance criteria
- [ ] The islanding signal (ROUTER_NOT_CARRYING) is generated when the router answers on LAN with no carrying legs
- [ ] The signal ships within minutes: SHIP_INTERVAL_MS is under 5 minutes so Datadog receives it promptly
- [ ] A monitor fires in minutes, not days, when the signal is set

## Size
S

## Out of scope
- None

Part of #308