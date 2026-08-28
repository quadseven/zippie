#!/bin/sh
# Is any leg actually CARRYING traffic right now?
#
# SOURCED, NOT EXECUTED. Both self-healing actors on this router need this exact
# question answered before they are allowed to act, and they must not answer it
# differently:
#
#   watchdog.sh   - may tear zippie down, but only if zippie is in the path
#   lan-guard.sh  - may revert the config, but only if the config is in the path
#
# Two hand-written copies of a predicate this load-bearing is precisely the shape
# that drifts. It already drifted once: the corrected form below lived only on
# the router and never in git, so the next deploy would have reinstalled the
# broken one over it.
#
# CARRYING IS in_bond AND WEIGHT, NOT in_bond ALONE.
#
# The first version of this grepped for `"in_bond": true` and that was wrong,
# measured 2026-08-16: it held 17 times and failed 3, and 3 is all it takes.
# `in_bond` means "present in the transport's link table" - the agent sets it as
# `pid in self._transport_links and not shed_for_latency`. A DEAD leg stays in
# that table:
#
#   ethernet   state=down   in_bond=True   effective_weight=0
#
# So the guard saw "something is carrying", stood aside, and the teardown ran
# exactly as if the guard were not there. The router came up with no WAN, zippie
# started correctly 26s in, and was torn down anyway.
#
# A leg only carries if it is in the table AND has weight. python3 rather than
# grep because that is a per-path conjunction and grep cannot express it - it can
# only tell you both strings appear SOMEWHERE, which is how the first version got
# this wrong. python3 is already a hard dependency here: the agent is written in
# it.
#
# FAILS CLOSED, deliberately. If the console cannot be read - agent down, port
# shut, malformed body - this returns false, meaning "not carrying". Both callers
# treat "not carrying" as "do not act", so the uncertain case never triggers a
# teardown or a revert. An unreadable console is exactly the state where both
# remedies are most useless and most harmful.

any_leg_carrying() {
    _s=$(curl -sS --max-time 4 "http://127.0.0.1:${WD_CONSOLE_PORT:-8787}/api/status" 2>/dev/null) || return 1
    [ -n "$_s" ] || return 1
    echo "$_s" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)          # unreadable body -> fail closed -> no action
for p in d.get("paths", []):
    if p.get("in_bond") and (p.get("effective_weight") or 0) > 0:
        sys.exit(0)      # something really is carrying
sys.exit(1)
' 2>/dev/null
}
