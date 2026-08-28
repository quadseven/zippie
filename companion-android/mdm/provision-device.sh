#!/usr/bin/env bash
# Device-level settings for a Pixel that lives as a permanent bond leg.
#
# WHY THIS IS A SCRIPT AND NOT MDM POLICY. Headwind delivers managed
# configuration to APPLICATIONS, and that works (see #137 - the announce token
# reaches the companion app with no hands). It cannot write arbitrary
# Settings.Secure values, and neither can any Device Owner: DevicePolicyManager
# restricts secure-setting writes to a short allowlist that
# charge_optimization_mode is not on. So the settings below have no policy path
# and would otherwise be a tap sequence somebody has to remember per device -
# which is exactly the thing that gets forgotten on device two.
#
# WHY 80% AND NOT 100%. A bond leg is a phone that sits on a charger
# permanently. Holding a lithium cell at full charge is the worst case for
# ageing, and it is the case a dedicated leg is in every hour of its life.
# Capping the charge is the single biggest lever on how long these phones last,
# and the cost is headroom nobody uses on a device that never leaves the mains.
# Android still tops up to 100% occasionally on its own to keep the capacity
# estimate honest, so this does not blind the battery health readout.
#
# EVERY WRITE IS READ BACK. `settings put` prints nothing on success AND
# nothing when it silently fails to stick, so trusting its exit code would make
# a phone that ignored the setting look identical to one that took it.
#
# Usage:
#   ./provision-device.sh                 apply, then verify
#   ./provision-device.sh --check         verify only; non-zero exit on drift
#   ADB_SERIAL=<serial> ./provision-device.sh    target one of several devices
set -euo pipefail

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

ADB=(adb)
[ -n "${ADB_SERIAL:-}" ] && ADB=(adb -s "$ADB_SERIAL")

# namespace | key | required value | what it means on the phone
SETTINGS=(
    "secure|charge_optimization_mode|1|Battery health > Charging optimization = Limit to 80%"
    "secure|adaptive_charging_enabled|0|Adaptive Charging off - it is the other half of the same radio group"
)

if ! "${ADB[@]}" shell true >/dev/null 2>&1; then
    echo "no device reachable over adb" >&2
    echo "  (wireless debugging reassigns its port on every reboot; re-pair if this is a fresh boot)" >&2
    exit 1
fi

model=$("${ADB[@]}" shell getprop ro.product.model 2>/dev/null | tr -d '\r')
serial=$("${ADB[@]}" shell getprop ro.serialno 2>/dev/null | tr -d '\r')
echo "device: ${model:-unknown} (${serial:-unknown})"

drift=0
for row in "${SETTINGS[@]}"; do
    IFS='|' read -r ns key want why <<<"$row"
    have=$("${ADB[@]}" shell settings get "$ns" "$key" 2>/dev/null | tr -d '\r')

    if [ "$have" = "$want" ]; then
        printf '  ok      %s.%s=%s\n' "$ns" "$key" "$have"
        continue
    fi

    if [ "$CHECK_ONLY" = 1 ]; then
        printf '  DRIFT   %s.%s=%s (want %s) - %s\n' "$ns" "$key" "${have:-unset}" "$want" "$why"
        drift=1
        continue
    fi

    printf '  set     %s.%s: %s -> %s\n' "$ns" "$key" "${have:-unset}" "$want"
    "${ADB[@]}" shell settings put "$ns" "$key" "$want" >/dev/null 2>&1 || true

    # The read-back is the actual test. See the header.
    now=$("${ADB[@]}" shell settings get "$ns" "$key" 2>/dev/null | tr -d '\r')
    if [ "$now" != "$want" ]; then
        printf '  FAILED  %s.%s is %s after writing %s - %s\n' "$ns" "$key" "${now:-unset}" "$want" "$why" >&2
        drift=1
    fi
done

if [ "$drift" != 0 ]; then
    [ "$CHECK_ONLY" = 1 ] && echo "drift found; run without --check to correct it" >&2
    exit 1
fi

echo "all settings correct"
