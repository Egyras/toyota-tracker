#!/bin/sh
# Follow-up triage for two unexplained items. Read-only, changes nothing.
#   sh investigate-accounts.sh
set -u
say() { printf '\n=== %s ===\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }

USER_Q="${SUSPECT_USER:-nova}"
IP_Q="${SUSPECT_IP:-192.168.88.2}"

say "1. Who is '$USER_Q'?"
getent passwd "$USER_Q"
id "$USER_Q" 2>/dev/null

say "Can it actually log in?"
# Field 2 of /etc/shadow: '!' or '*' means no password login is possible.
# Anything starting '\$' is a real password hash — that is the case to explain.
hash=$(grep "^$USER_Q:" /etc/shadow 2>/dev/null | cut -d: -f2)
case "$hash" in
    '!'*|'*'|'!!') echo "  LOCKED — no password login possible (reassuring)" ;;
    '$'*)          echo "  HAS A PASSWORD HASH (${hash%%\$*}\$... type) — explain this one" ;;
    '')            echo "  no shadow entry found" ;;
    *)             echo "  unusual shadow field: $hash" ;;
esac

say "Does TrueNAS middleware know about it?"
# On SCALE, /etc/passwd is regenerated from the middleware database. A user that
# middleware knows about was created through the UI/API - i.e. deliberately.
# One that exists ONLY in /etc/passwd would be far more interesting.
if have midclt; then
    midclt call user.query "[[\"username\",\"=\",\"$USER_Q\"]]" 2>/dev/null \
        || echo "  (query failed)"
else
    echo "  midclt not available"
fi

say "Which package created it, if any?"
grep -rl "$USER_Q" /var/lib/dpkg/info/*.postinst 2>/dev/null | head -5 || true
grep -rn "^$USER_Q:" /usr/lib/sysusers.d/* /etc/sysusers.d/* 2>/dev/null | head -5 || true
echo "  (empty means no packaged account definition found)"

say "Does it own anything on disk?"
find / -xdev -user "$USER_Q" 2>/dev/null | head -20
echo "  (done)"

say "Is it running anything right now?"
ps -u "$USER_Q" -o pid,etime,cmd 2>/dev/null || echo "  no processes"

say "Has it ever logged in?"
last -F "$USER_Q" 2>/dev/null | head -10 || echo "  no login records"
grep -h "$USER_Q" /var/log/auth.log* 2>/dev/null | tail -10 || echo "  nothing in auth.log"

say "Is it a container user leaking into the host view?"
if have docker; then
    for c in $(docker ps -q 2>/dev/null); do
        n=$(docker inspect -f '{{.Name}}' "$c")
        if docker exec "$c" getent passwd "$USER_Q" 2>/dev/null | grep -q .; then
            echo "  also present in container $n"
        fi
    done
    echo "  (done)"
fi

say "2. What is $IP_Q?"
echo "-- current ARP/neighbour entry (reveals MAC, and MAC reveals vendor):"
if have ip; then ip neigh show | grep -F "$IP_Q" || echo "  not currently in the neighbour table"; fi
echo "-- route to it:"
have ip && ip route get "$IP_Q" 2>/dev/null

say "Every log line mentioning $IP_Q"
grep -h -F "$IP_Q" /var/log/auth.log* 2>/dev/null | tail -20 || echo "  nothing in auth.log"

say "All sessions from that address"
have last && last -F -i 2>/dev/null | grep -F "$IP_Q" | head -10

say "Do we have an interface on that subnet?"
have ip && ip -4 addr show | grep -E 'inet ' | sed 's/^/  /'

say "Is it reachable, and what is it?"
if have ping; then ping -c 1 -W 2 "$IP_Q" >/dev/null 2>&1 && echo "  responds to ping" || echo "  no ping response"; fi
echo "-- MikroTik hypothesis: mktxp exporter config would name the router"
for f in /mnt/*/mktxp/mktxp.conf /etc/mktxp/mktxp.conf; do
    [ -f "$f" ] && { echo "  --- $f"; grep -vE '^\s*(#|$)' "$f" | head -20 | sed 's/^/     /'; }
done
have docker && docker ps --filter name=mktxp --format '  container: {{.Names}}' 2>/dev/null

printf '\n=== Done ===\n'
