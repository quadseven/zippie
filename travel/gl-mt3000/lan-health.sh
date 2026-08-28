#!/bin/sh
# Is the LAN actually usable by a CLIENT - not "does the router have internet".
#
# WHY THIS EXISTS. On 2026-08-11 suzu's DHCP handed out an address and no
# resolver for hours. The router itself was perfectly online the whole time, so
# watchdog.sh - which asks only "can the ROUTER reach the internet" - saw
# nothing wrong. Every phone on the wifi was dead. iOS hid the wifi icon and
# moved to cellular; the fault looked like a wifi problem and was DNS.
#
# WHAT IT CHECKS, AND WHY IN THIS ORDER
#   1. DHCP advertises a resolver at all. This is the exact fault above, and it
#      is invisible from every router-side check.
#   2. That resolver answers. Advertising a dead resolver is the same outage
#      with a different cause.
#   3. The captive URL returns the body iOS requires. This is the check phones
#      ACTUALLY make, so it is the one that decides whether a phone believes
#      the network works.
#
# WHAT IT DELIBERATELY DOES NOT DO
#   No veth, no netns, no synthetic `ip route get`. A veth added to br-lan makes
#   netifd regenerate and reload dnsmasq - observed repeatedly on 2026-08-11 -
#   so a probe that built one every minute would be re-rolling DHCP all day to
#   ask whether DHCP works. And per watchdog.sh's own history, a synthetic
#   routing query already took this router off the network once.
#
# Exit 0 = healthy. Exit 1 = a client on this LAN cannot use it.
set -u

CONF=$(ls /var/etc/dnsmasq.conf.cfg* 2>/dev/null | head -1)
fail=""

# 1. Does DHCP name a resolver?
if [ -z "$CONF" ]; then
    fail="${fail}no-dnsmasq-config "
elif ! grep -q "^dhcp-option=lan,6," "$CONF"; then
    fail="${fail}dhcp-advertises-no-dns "
fi

RESOLVER=$(sed -n 's/^dhcp-option=lan,6,//p' "$CONF" 2>/dev/null | cut -d, -f1)

# 2. Does it answer? Two names, because one NXDOMAIN-ing upstream is not an
#    outage but a resolver that answers nothing is.
if [ -n "$RESOLVER" ]; then
    ok=0
    for n in captive.apple.com cloudflare.com; do
        nslookup "$n" "$RESOLVER" >/dev/null 2>&1 && ok=$((ok + 1))
    done
    [ "$ok" -eq 0 ] && fail="${fail}resolver-${RESOLVER}-answers-nothing "
else
    [ -n "$CONF" ] && fail="${fail}no-resolver-configured "
fi

# 3. The check a phone actually makes, resolved through the LAN's resolver.
#
#    NOT `curl --dns-servers`: this box ships libcurl 7.83.1 without c-ares, so
#    that flag makes curl exit immediately - which the first version of this
#    script read as "the network is down". A probe whose own unsupported flag
#    reports an outage is worse than no probe. Resolve first, then pin the
#    address with --resolve, which needs no special libcurl build.
#
#    The address is taken from the answer section only. busybox nslookup prints
#    the SERVER's own address first, and using that would pin captive.apple.com
#    to the router and always "succeed".
ADDR=""
if [ -n "$RESOLVER" ]; then
    ADDR=$(nslookup captive.apple.com "$RESOLVER" 2>/dev/null \
           | sed -n '/^Name:/,$p' \
           | sed -n 's/^Address[ 0-9]*: *\([0-9.]*\)$/\1/p' | head -1)
fi
if [ -z "$ADDR" ]; then
    body=""
else
    body=$(curl -sS -m 12 --resolve "captive.apple.com:80:$ADDR" \
           http://captive.apple.com/hotspot-detect.html 2>/dev/null) || body=""
fi
case "$body" in
    *Success*) : ;;
    "")        fail="${fail}captive-check-no-response " ;;
    *)         fail="${fail}captive-check-unexpected-body " ;;
esac

if [ -n "$fail" ]; then
    echo "UNHEALTHY: $fail"
    exit 1
fi
echo "healthy: dhcp-dns=$RESOLVER resolver-answers captive-ok"
exit 0
