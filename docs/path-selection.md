# Path selection: which link carries your traffic

## Short answers

| Question | Answer |
|---|---|
| Non-bonding: does it pick one internet? | **Yes** — mode `prefer` (default). One default route. |
| Is there priority? | **Yes** — `priority` (lower wins), then `cost_class`, monthly soft caps, RTT. |
| Does one download span several WANs? | **Not in `prefer`.** Use `aggregate` for multi-flow, or MPTCP for a true single-stream stripe. |

## Modes

### `prefer` (default) — non-bonding, smart pick

Only **one** path carries user traffic at a time.

Pick order among healthy paths:

1. **Health** — `up` before `degraded` (dead paths out)
2. **Budget** — not over soft monthly limit before over-limit
3. **cost_class** — `free` < `unlimited` < `throttle_ok` < `metered` < `expensive`
4. **priority** — lower number wins (10 before 20)
5. **RTT** — lower wins
6. **Sticky** — keep current primary if same tier and RTT within `sticky_rtt_slack_ms`

This is the mode that just works: it picks a good link and stays on it.

`failover` is an alias of `prefer`.

### `aggregate` — bonding-ish (multi-flow)

Weighted multipath across all healthy tunnels. Good when you want **max throughput across many connections** (browsers, updates, several devices). A *single* large download still sticks to one hash bucket (kernel multipath), so it will not stripe one TCP flow across links.

### `redundant`

Reserved for packet duplication. Routing behaves like aggregate today.

## Your family kit (~250GB soft pool)

Example (also in `configs/examples/zippie.toml`):

| Path | Cap | cost_class | Role |
|---|---|---|---|
| Starlink | 50GB | `metered` | Fast when available; demote after ~85% |
| Operator Google Fi | 50GB | `metered` | Cellular A |
| Operator Verizon | 50GB | `throttle_ok` | Prefer after metered soft-caps; still OK if throttled |
| Co-operator Google Fi | 50GB | `metered` | Extra pool |
| Co-operator Verizon | 50GB | `throttle_ok` | Extra throttle-ok pool |

**Prefer-mode day trip:** burns Starlink first (priority 10) until soft cap or loss, then Fi lines, then Verizon lines as workhorses.

**Aggregate-mode hotel siege:** set `mode = "aggregate"` and watch the dashboard weights; expect higher GB burn across all SIMs.

Usage file (the agent writes it; a human only reads it):

```bash
# /var/lib/zippie/usage.json
{"version": 2, "legs": {"starlink": {"usage_gb": 40, "period_start": "2026-08-01",
                                     "previous_usage_gb": 41.2,
                                     "previous_period_start": "2026-07-01"}}}
```

When `usage_gb >= monthly_cap_gb * soft_limit_pct`, that path is demoted (still used if nothing better is up).

`usage_gb` counts ONE billing period, and `period_start` says which. When the
period containing today is later than that anchor, the counter rolls to zero
and last period's total is kept as `previous_usage_gb` - so a demotion expires
with the plan window instead of lasting forever, and the number that caused it
is still there to look at afterwards.

The period is a calendar month unless the leg has a `billing_day` in
`legs.json`, which is the carrier's own cycle day (a plan that resets on the
14th should say `14`). The roll is applied on the next START as well as in the
control loop, because a router in a car is powered off across most boundaries.

## What works today (honest)

| Capability | Zippie now |
|---|---|
| Auto use multiple WANs | Yes (join + tunnels) |
| Smart primary selection | Yes (`prefer`) |
| Instant failover | Yes (probe loop ~500ms) |
| Cost / data-cap awareness | Yes (soft caps + cost_class) |
| Combine bandwidth on one download | Partial (`aggregate` multi-flow; true stripe = OMR/MPTCP) |
| Phone app polish | No (dashboard + CLI) |

## When to buy Starlink unlimited

If `prefer` keeps Starlink primary and you still hit 50GB before the trip ends, unlimited is the right product lever — Zippie will happily sit on Starlink when `cost_class = "unlimited"` and `monthly_cap_gb = 0`.
