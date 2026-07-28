#!/bin/sh
# Read-only compromise triage for the TrueNAS host.
#
# Makes NO changes: no installs, no config edits, no file writes outside the
# report it prints to stdout. Safe to run on a production box.
#
#   sh host-triage.sh                 print report
#   sh host-triage.sh > triage.txt    save it
#
# Ordered by this system's actual threat model: the realistic path to this host
# was a compromised container pivoting to Jenkins (which has Docker daemon
# access, and therefore root-equivalent). So Docker and Jenkins come first,
# then generic persistence.
#
# IMPORTANT: a compromised host cannot reliably audit itself. Clean output here
# raises confidence, it does not prove anything. The findings that matter most
# are the ones you can corroborate from outside the box.

say() { printf '\n=== %s ===\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }

printf 'Host triage — %s on %s\n' "$(date -u '+%Y-%m-%d %H:%M UTC')" "$(hostname)"
[ "$(id -u)" = "0" ] || echo "WARNING: not root — several checks will be incomplete"

# ── Docker: the container-to-host pivot surface ──────────────────────────────
say "Containers (anything you did not create is a finding)"
have docker && docker ps -a --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}' || echo "docker not found"

say "Containers with the Docker socket mounted (= root on this host)"
if have docker; then
    found=0
    for c in $(docker ps -aq 2>/dev/null); do
        if docker inspect "$c" 2>/dev/null | grep -q 'docker\.sock'; then
            echo "  FINDING: $(docker inspect -f '{{.Name}}' "$c") mounts docker.sock"
            found=1
        fi
    done
    [ "$found" = "0" ] && echo "  none (expected)"
fi

say "Privileged or host-network containers"
if have docker; then
    docker ps -aq 2>/dev/null | while read -r c; do
        n=$(docker inspect -f '{{.Name}}' "$c" 2>/dev/null)
        p=$(docker inspect -f '{{.HostConfig.Privileged}}' "$c" 2>/dev/null)
        m=$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$c" 2>/dev/null)
        [ "$p" = "true" ] && echo "  FINDING: $n is --privileged"
        [ "$m" = "host" ] && echo "  NOTE:    $n uses host networking"
    done
    echo "  (done)"
fi

say "Images — look for tags you never built or pulled"
have docker && docker images --format 'table {{.Repository}}:{{.Tag}}\t{{.CreatedSince}}\t{{.Size}}' | head -30

say "Host processes started by containers, running as root"
have docker && docker ps -q 2>/dev/null | while read -r c; do
    echo "  $(docker inspect -f '{{.Name}} user={{.Config.User}}' "$c" 2>/dev/null)"
done

# ── Jenkins: the crown jewel in this setup ──────────────────────────────────
say "Jenkins — jobs and recent builds you did not trigger"
for d in /var/lib/jenkins /var/jenkins_home /root/.jenkins; do
    [ -d "$d" ] || continue
    echo "  Jenkins home: $d"
    ls -la "$d/jobs" 2>/dev/null | head -20
    echo "  -- config.xml last modified:"
    find "$d" -maxdepth 2 -name 'config.xml' -printf '     %TY-%Tm-%Td %TH:%TM  %p\n' 2>/dev/null | head -20
    echo "  -- credential store mtime (unexpected change = investigate):"
    ls -la "$d/credentials.xml" 2>/dev/null
    echo "  -- Groovy scripts on disk (script console abuse leaves traces here):"
    find "$d" -name '*.groovy' -newermt '-90 days' 2>/dev/null | head -10
done
[ -d /var/lib/jenkins ] || [ -d /var/jenkins_home ] || echo "  no Jenkins home on this host (may be containerised)"

# ── Persistence: where an attacker stays after a reboot ─────────────────────
say "SSH authorized_keys (every key here should be one you recognise)"
for f in /root/.ssh/authorized_keys /home/*/.ssh/authorized_keys /mnt/*/home/*/.ssh/authorized_keys; do
    [ -f "$f" ] || continue
    echo "  $f:"
    awk '{print "     " $1 " ... " $NF}' "$f" 2>/dev/null
done

say "UID 0 accounts (should be exactly one: root)"
awk -F: '$3==0 {print "  " $1 " uid=" $3 " shell=" $7}' /etc/passwd

say "Accounts with a login shell"
awk -F: '$7 !~ /(nologin|false|sync)$/ {print "  " $1 " uid=" $3 " shell=" $7}' /etc/passwd

say "Recently modified accounts"
ls -la /etc/passwd /etc/shadow /etc/group 2>/dev/null
[ -f /etc/shadow ] && awk -F: '{print $1}' /etc/shadow | wc -l | sed 's/^/  shadow entries: /'

say "Cron"
for f in /etc/crontab /etc/cron.d/* /var/spool/cron/crontabs/*; do
    [ -f "$f" ] || continue
    echo "  --- $f"
    grep -vE '^\s*(#|$)' "$f" 2>/dev/null | sed 's/^/     /'
done

say "Systemd timers and recently-enabled units"
have systemctl && systemctl list-timers --all --no-pager 2>/dev/null | head -20
echo "  -- unit files changed in the last 90 days:"
find /etc/systemd /lib/systemd -name '*.service' -newermt '-90 days' 2>/dev/null | head -20

say "Shell rc files modified in the last 90 days (classic persistence)"
find /root /home /etc/profile.d -maxdepth 3 \
     \( -name '.bashrc' -o -name '.profile' -o -name '.bash_profile' -o -name '*.sh' \) \
     -newermt '-90 days' 2>/dev/null | head -20

# ── Network ─────────────────────────────────────────────────────────────────
say "Listening sockets (anything unexplained facing the LAN is a finding)"
have ss && ss -tulpn 2>/dev/null | head -40 || netstat -tulpn 2>/dev/null | head -40

say "Established outbound connections"
have ss && ss -tunp state established 2>/dev/null | head -40

say "What the Cloudflare tunnel actually exposes"
for f in /etc/cloudflared/config.yml /root/.cloudflared/config.yml; do
    [ -f "$f" ] && { echo "  --- $f"; grep -vE '^\s*#' "$f" | sed 's/^/     /'; }
done
have docker && docker ps --filter name=cloudflared --format '  container: {{.Names}} {{.Status}}'

# ── Integrity ───────────────────────────────────────────────────────────────
say "TrueNAS boot-pool snapshots (a rollback point predating any concern)"
have zfs && zfs list -t snapshot -o name,creation -s creation 2>/dev/null | grep -i boot | tail -15

say "Root filesystem writability (TrueNAS SCALE keeps this read-only by design)"
mount | grep -E ' on / ' | sed 's/^/  /'

say "Debian package integrity — files differing from their package"
if have dpkg; then
    echo "  (this is slow; missing/altered files listed below, empty is good)"
    dpkg --verify 2>/dev/null | head -25
fi

say "SUID binaries outside the usual locations"
find / -xdev -perm -4000 -type f 2>/dev/null \
  | grep -vE '^/(usr/bin|usr/sbin|bin|sbin|usr/lib|usr/libexec)/' | head -20
echo "  (done — entries above are worth explaining)"

say "Auth log: recent successful logins and sudo use"
for f in /var/log/auth.log /var/log/secure; do
    [ -f "$f" ] || continue
    echo "  --- $f"
    grep -E 'Accepted (password|publickey)' "$f" 2>/dev/null | tail -15 | sed 's/^/     /'
    echo "  -- failed password attempts (volume indicates exposure):"
    grep -c 'Failed password' "$f" 2>/dev/null | sed 's/^/     /'
done
have last && { echo "  -- last logins:"; last -n 15 2>/dev/null | sed 's/^/     /'; }

printf '\n=== Report complete ===\n'
echo "Remember: this ran ON the host it is auditing. Corroborate anything"
echo "suspicious from your router/firewall logs or another machine."
