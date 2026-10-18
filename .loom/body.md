# T216: SSID-scoped on-demand rule for contributor tunnel

## Why
The issue requests adding an SSID-scoped NEOnDemandRuleConnect for the travel-router wifi so the contributor tunnel survives jetsam and restarts automatically when the phone rejoins the router's network without opening the app.

## What
Implement a NEOnDemandRuleConnect matched to the configured travel-router SSID (read via NetworkFacts) with an explicit disconnect rule for all other networks. Store the policy as plain data in the shared package since NetworkExtension types cannot be constructed off-device, and ensure a blank SSID field does not silently become unconditional always-on behavior.

## Acceptance criteria
- [ ] Kill the extension; rejoin travel-router wifi; tunnel restarts without opening the app
- [ ] Off travel-router wifi the tunnel does not start on its own
- [ ] Behavior with the local-network permission unknown (phase-2 risk) recorded

Size: S

## Out of scope
- Client-mode on-demand policy (different question: it wants to run everywhere)

Part of #109
