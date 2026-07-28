#!/bin/sh
# Stop the scraper container from reaching anything on the local network.
#
# WHY THIS EXISTS
# ---------------
# The scraper runs Chromium against a third-party site we do not control, so it
# is the most likely place in this system to get code execution. Docker can give
# a container internet access, or no access, but it cannot express "internet yes,
# LAN no" — a container on a normal bridge network can route to 192.168.x.x just
# as freely as to the internet. That is the path from a compromised browser to
# the TrueNAS web UI, to SMB/NFS, and above all to Jenkins, which has Docker
# daemon access and is therefore equivalent to root on the host.
#
# These rules go in DOCKER-USER, the chain Docker guarantees is evaluated BEFORE
# its own generated rules and which Docker will not overwrite.
#
# WHAT IT DOES NOT DO
# -------------------
# It does not restrict the web container, which still needs normal networking.
# It does not persist across reboot on its own — see the note at the end.
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

# Two allow-rules go in FIRST, because DOCKER-USER is evaluated at the top of
# the FORWARD chain — BEFORE Docker's own conntrack accept. Without them, reply
# packets on connections the web container opened would hit the DROP rules below
# and the scraper would look dead.
#
# Note 172.16.0.0/12 spans 172.16.0.0-172.31.255.255, so the scraper's own
# egress subnet falls inside the range being denied. Hence the explicit RETURNs.
rules_established="-I DOCKER-USER 1 -s $SUBNET -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN"
rules_allow="-I DOCKER-USER 2 -s $SUBNET -d $SUBNET -j RETURN"

echo "Scraper subnet : $SUBNET"
echo "Blocking to    : $PRIVATE"
echo

if [ "$MODE" = "--verify" ]; then
    echo "Current DOCKER-USER chain:"
    iptables -L DOCKER-USER -n --line-numbers
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
        echo "removed RETURN established"
    done
    echo "Done. The scraper can reach the LAN again."
    exit 0
fi

if [ "$MODE" = "--dry-run" ]; then
    echo "Would run:"
    echo "  iptables $rules_established"
    echo "  iptables $rules_allow"
    for net in $PRIVATE; do
        echo "  iptables -A DOCKER-USER -s $SUBNET -d $net -j DROP"
    done
    echo
    echo "(no changes made)"
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

# Allow-rules first so they are evaluated before the denies below.
iptables -C DOCKER-USER -s "$SUBNET" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN 2>/dev/null || \
    iptables $rules_established
echo "allow  established/related replies from $SUBNET"
iptables -C DOCKER-USER -s "$SUBNET" -d "$SUBNET" -j RETURN 2>/dev/null || \
    iptables $rules_allow
echo "allow  $SUBNET -> $SUBNET (scraper to web container)"

for net in $PRIVATE; do
    if iptables -C DOCKER-USER -s "$SUBNET" -d "$net" -j DROP 2>/dev/null; then
        echo "exists DROP $SUBNET -> $net"
    else
        iptables -A DOCKER-USER -s "$SUBNET" -d "$net" -j DROP
        echo "added  DROP $SUBNET -> $net"
    fi
done

echo
echo "Verify from inside the scraper:"
echo "  docker exec toyota-scraper python -c \"import socket;socket.create_connection(('192.168.8.211',80),3)\""
echo "    -> should TIME OUT or be refused"
echo "  docker exec toyota-scraper python -c \"import socket;socket.create_connection(('www.myshiptracking.com',443),10);print('internet ok')\""
echo "    -> should print 'internet ok'"
echo
echo "NOTE: iptables rules do not survive a reboot. Persist them with"
echo "      iptables-save, or re-run this script from a boot script / TrueNAS"
echo "      init task, or the scraper silently regains LAN access after a restart."
