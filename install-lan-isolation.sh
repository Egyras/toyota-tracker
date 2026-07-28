#!/bin/sh
# Stop the scraper container from reaching the local network OR this host.
#
# WHY THIS EXISTS
# ---------------
# The scraper runs Chromium against a third-party site we do not control, so it
# is the most likely place in this system to get code execution. Docker can give
# a container internet access, or no access, but it cannot express "internet yes,
# LAN no". That is the path from a compromised browser to the TrueNAS UI, to
# SMB/NFS, and above all to Jenkins, which mounts docker.sock and is therefore
# equivalent to root on this host.
#
# TWO CHAINS ARE NEEDED — this is the part that is easy to get wrong.
#
#   FORWARD (via DOCKER-USER): traffic ROUTED THROUGH this box to other machines
#                              on the LAN, e.g. 192.168.8.1.
#   INPUT:                     traffic addressed to THIS HOST'S OWN IP. It is
#                              delivered locally and never enters FORWARD, so
#                              DOCKER-USER rules do not apply to it at all.
#
# Only doing the first leaves every -p 0.0.0.0 published port reachable on the
# host address — including Jenkins on 30017. A test against the host IP also
# gives a misleading "Connection refused" (a RST from the host) rather than the
# timeout a DROP produces, which makes it look like the rules work when they
# have not even been consulted.
#
# Usage:
#   ./install-lan-isolation.sh --dry-run     show the rules, change nothing
#   ./install-lan-isolation.sh               apply them
#   ./install-lan-isolation.sh --uninstall   remove them
#   ./install-lan-isolation.sh --verify      show what is currently installed
set -eu

SUBNET="${SCRAPER_SUBNET:-172.31.77.0/24}"   # must match toyota-egress in Jenkinsfile
PRIVATE="10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16"
MODE="${1:-apply}"

echo "Scraper subnet : $SUBNET"
echo "Blocking to    : $PRIVATE"
echo "Plus           : this host itself (INPUT chain)"
echo

if [ "$MODE" = "--verify" ]; then
    echo "--- DOCKER-USER (routed traffic) ---"
    iptables -L DOCKER-USER -n --line-numbers
    echo
    echo "--- INPUT rules for $SUBNET (traffic to this host) ---"
    iptables -L INPUT -n --line-numbers | grep -F "${SUBNET%/*}" || echo "  none installed"
    exit 0
fi

if [ "$MODE" = "--dry-run" ]; then
    echo "Would run:"
    echo "  # replies to connections the web container opened must survive"
    echo "  iptables -I DOCKER-USER 1 -s $SUBNET -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN"
    echo "  iptables -I DOCKER-USER 2 -s $SUBNET -d $SUBNET -j RETURN"
    for net in $PRIVATE; do
        echo "  iptables -A DOCKER-USER -s $SUBNET -d $net -j DROP"
    done
    echo "  # this host's own IP - NOT covered by DOCKER-USER"
    echo "  iptables -I INPUT 1 -s $SUBNET -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT"
    echo "  iptables -I INPUT 2 -s $SUBNET -j DROP"
    echo
    echo "(no changes made)"
    exit 0
fi

if [ "$MODE" = "--uninstall" ]; then
    for net in $PRIVATE; do
        while iptables -C DOCKER-USER -s "$SUBNET" -d "$net" -j DROP 2>/dev/null; do
            iptables -D DOCKER-USER -s "$SUBNET" -d "$net" -j DROP
            echo "removed DROP $SUBNET -> $net"
        done
    done
    while iptables -C DOCKER-USER -s "$SUBNET" -d "$SUBNET" -j RETURN 2>/dev/null; do
        iptables -D DOCKER-USER -s "$SUBNET" -d "$SUBNET" -j RETURN
        echo "removed RETURN $SUBNET -> $SUBNET"
    done
    while iptables -C DOCKER-USER -s "$SUBNET" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN 2>/dev/null; do
        iptables -D DOCKER-USER -s "$SUBNET" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
        echo "removed RETURN established (forward)"
    done
    while iptables -C INPUT -s "$SUBNET" -j DROP 2>/dev/null; do
        iptables -D INPUT -s "$SUBNET" -j DROP
        echo "removed DROP $SUBNET -> this host"
    done
    while iptables -C INPUT -s "$SUBNET" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null; do
        iptables -D INPUT -s "$SUBNET" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
        echo "removed ACCEPT established (input)"
    done
    echo "Done. The scraper can reach the LAN and this host again."
    exit 0
fi

if [ "$(id -u)" != "0" ]; then
    echo "ERROR: must run as root to modify iptables" >&2
    exit 1
fi
if ! iptables -L DOCKER-USER -n >/dev/null 2>&1; then
    echo "ERROR: DOCKER-USER chain not found. Is Docker running on this host?" >&2
    exit 1
fi

# ── FORWARD: other machines on the LAN ──────────────────────────────────────
# The allow-rules go first. DOCKER-USER is evaluated at the top of FORWARD,
# BEFORE Docker's own conntrack accept, so without them the reply packets of a
# web->scraper connection would hit the DROPs and the scraper would look dead.
# Note 172.16.0.0/12 spans 172.16.0.0-172.31.255.255, which contains the
# scraper's own egress subnet — hence the explicit RETURNs.
iptables -C DOCKER-USER -s "$SUBNET" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN 2>/dev/null || \
    iptables -I DOCKER-USER 1 -s "$SUBNET" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
echo "allow  established/related replies from $SUBNET (forward)"

iptables -C DOCKER-USER -s "$SUBNET" -d "$SUBNET" -j RETURN 2>/dev/null || \
    iptables -I DOCKER-USER 2 -s "$SUBNET" -d "$SUBNET" -j RETURN
echo "allow  $SUBNET -> $SUBNET"

for net in $PRIVATE; do
    if iptables -C DOCKER-USER -s "$SUBNET" -d "$net" -j DROP 2>/dev/null; then
        echo "exists DROP $SUBNET -> $net"
    else
        iptables -A DOCKER-USER -s "$SUBNET" -d "$net" -j DROP
        echo "added  DROP $SUBNET -> $net"
    fi
done

# ── INPUT: this host's own IP ───────────────────────────────────────────────
# Every port published with -p 0.0.0.0 answers on the host address, Jenkins
# (30017) included. Replies to connections the WEB container opened are
# ESTABLISHED and stay allowed, so the internal API is unaffected.
if iptables -C INPUT -s "$SUBNET" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null; then
    echo "exists ACCEPT established from $SUBNET (input)"
else
    iptables -I INPUT 1 -s "$SUBNET" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    echo "added  ACCEPT established from $SUBNET (input)"
fi
if iptables -C INPUT -s "$SUBNET" -j DROP 2>/dev/null; then
    echo "exists DROP $SUBNET -> this host"
else
    iptables -I INPUT 2 -s "$SUBNET" -j DROP
    echo "added  DROP $SUBNET -> this host (blocks Jenkins, TrueNAS UI, SSH)"
fi

cat <<'VERIFY'

Verify — read the ERROR TYPE, not just "it failed":
  DROP  -> TimeoutError            (packet discarded, rule worked)
  no rule -> ConnectionRefusedError (a RST came back, nothing was blocking)

  # 1. another LAN machine (FORWARD path) — expect TimeoutError
  docker exec toyota-scraper python -c \
    "import socket;socket.create_connection(('192.168.8.1',80),4)"

  # 2. Jenkins on this host (INPUT path) — expect TimeoutError
  docker exec toyota-scraper python -c \
    "import socket;socket.create_connection(('192.168.8.211',30017),4)"

  # 3. the internet must still work (also proves DNS survived)
  docker exec toyota-scraper python -c \
    "import socket;socket.create_connection(('www.myshiptracking.com',443),10);print('internet ok')"

  # 4. the app must still work: web -> scraper over the internal network
  docker exec toyota-tracker python -c \
    "import os,urllib.request;print(urllib.request.urlopen(os.environ['SCRAPER_URL']+'/healthz',timeout=5).read())"

NOTE: iptables rules do not survive a reboot. On TrueNAS SCALE add this script
      as a POSTINIT task under System Settings > Advanced > Init/Shutdown
      Scripts, from a path on a dataset (NOT /tmp, which is cleared on boot).
      Otherwise the scraper silently regains access after any restart.
VERIFY
