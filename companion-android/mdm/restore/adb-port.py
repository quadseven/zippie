"""Ask the LAN which adb port Android picked.

Run this ON THE ROUTER, which shares the phone's LAN:

    ssh root@<router> 'python3 -' < adb-port.py

WHY THIS EXISTS. Android's wireless debugging port is chosen by adbd every time
it is enabled, so a number read off the phone's screen goes stale - and it does
not survive a reboot at all. Android advertises the live port over mDNS, which
is how Android Studio finds devices, so the network can simply be asked.

    _adb-tls-connect._tcp   the port to `adb connect`
    _adb-tls-pairing._tcp   the port `adb pair` uses, and ONLY while the pairing
                            dialog is open on the phone
    _adb._tcp               legacy

JOINING THE MULTICAST GROUP IS NOT OPTIONAL. mDNS responders reply to the group
address, not to the sender's ephemeral port, so a socket that only sends never
hears an answer - it reports "nothing found" on a LAN full of responders. That
false negative cost real time on 2026-08-15: it read as "wireless debugging is
off" when the port was live the whole time.

The check that catches it: query `_services._dns-sd._udp.local` first. Every
mDNS host answers that. If NOTHING answers, the prober is deaf and any negative
it reports about adb is worthless.

Stdlib only - the router's python3.9 has no third-party packages.
"""
import socket
import struct
import sys
import time

MCAST, PORT = "224.0.0.251", 5353
SERVICES = [
    "_services._dns-sd._udp.local",   # the sanity query, see module docstring
    "_adb-tls-connect._tcp.local",
    "_adb-tls-pairing._tcp.local",
    "_adb._tcp.local",
]


def encode_name(name):
    out = b""
    for label in name.split("."):
        if label:
            out += bytes([len(label)]) + label.encode()
    return out + b"\x00"


def query(names):
    header = struct.pack("!6H", 0, 0, len(names), 0, 0, 0)
    body = b"".join(encode_name(n) + struct.pack("!2H", 12, 1) for n in names)
    return header + body


def read_name(buf, off, depth=0):
    parts = []
    while True:
        if off >= len(buf) or depth > 16:
            return ".".join(parts), off
        ln = buf[off]
        if ln == 0:
            return ".".join(parts), off + 1
        if ln & 0xC0 == 0xC0:
            ptr = struct.unpack("!H", buf[off:off + 2])[0] & 0x3FFF
            tail, _ = read_name(buf, ptr, depth + 1)
            parts.append(tail)
            return ".".join(parts), off + 2
        parts.append(buf[off + 1:off + 1 + ln].decode("utf-8", "replace"))
        off += 1 + ln


def records(buf):
    try:
        _, _, qd, an, ns, ar = struct.unpack("!6H", buf[:12])
    except struct.error:
        return
    off = 12
    for _ in range(qd):
        _, off = read_name(buf, off)
        off += 4
    for _ in range(an + ns + ar):
        name, off = read_name(buf, off)
        if off + 10 > len(buf):
            return
        rtype, _cls, _ttl, rdlen = struct.unpack("!2HIH", buf[off:off + 10])
        off += 10
        if rtype == 33 and rdlen >= 6:                       # SRV
            port = struct.unpack("!3H", buf[off:off + 6])[2]
            target, _ = read_name(buf, off + 6)
            yield ("SRV", name, (target, port))
        elif rtype == 1 and rdlen == 4:                      # A
            yield ("A", name, socket.inet_ntoa(buf[off:off + 4]))
        off += rdlen


def main():
    lan = sys.argv[1] if len(sys.argv) > 1 else "10.20.0.1"
    seconds = int(sys.argv[2]) if len(sys.argv) > 2 else 8

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except (AttributeError, OSError):
        pass
    s.bind(("", PORT))
    # THE LINE THAT MATTERS. Without the group join this socket never receives a
    # single reply and every answer below is a false negative.
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                 socket.inet_aton(MCAST) + socket.inet_aton(lan))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(lan))
    s.settimeout(1.0)

    pkt = query(SERVICES)
    s.sendto(pkt, (MCAST, PORT))
    srv, addr, responders = {}, {}, set()
    end = time.time() + seconds
    while time.time() < end:
        try:
            data, src = s.recvfrom(9000)
        except socket.timeout:
            s.sendto(pkt, (MCAST, PORT))
            continue
        responders.add(src[0])
        for kind, name, val in records(data):
            if kind == "SRV":
                srv[name] = val
            elif kind == "A":
                addr[name] = val

    if not responders:
        print("NOTHING answered mDNS at all - the prober is deaf, not the phone.")
        print("Check the LAN address passed in (got %r) and that this host is on"
              " the phone's network." % lan)
        return 2

    adb = {n: v for n, v in srv.items() if "adb" in n}
    if not adb:
        print("mDNS is working (%d responders) but no adb service is advertised."
              % len(responders))
        print("That is a real negative: wireless debugging is off, or the screen"
              " is asleep.")
        return 1

    for name, (target, port) in sorted(adb.items()):
        kind = ("connect" if "connect" in name else
                "pairing" if "pairing" in name else "legacy")
        print("%-8s %s port %d   (%s)" % (kind, addr.get(target, target), port, name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
