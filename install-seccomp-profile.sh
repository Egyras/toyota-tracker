#!/bin/sh
# Fetch the Chromium-sandbox seccomp profile onto the Docker host.
#
# Why this is needed: Docker's DEFAULT seccomp profile permits clone() only when
# no namespace flags are set, so a container cannot create the user namespace
# that Chromium's sandbox relies on. Playwright publishes Docker's full default
# profile with clone/setns/unshare additionally permitted.
#
# This is deliberately NOT a hand-written file committed to this repo: the
# profile is ~1000 lines of Docker's default allow-rules, and an abbreviated
# version would either silently weaken syscall filtering or — if defaultAction
# is left at SCMP_ACT_ERRNO with only a few syscalls listed — deny everything
# else and break the container outright.
#
# Run once on the TrueNAS host, then deploy with:
#   docker run --security-opt seccomp=/etc/docker/seccomp/playwright.json ...
#
# Usage: ./install-seccomp-profile.sh [playwright-version]
set -eu

VERSION="${1:-v1.62.0}"
DEST="${SECCOMP_DEST:-/etc/docker/seccomp/playwright.json}"
URL="https://raw.githubusercontent.com/microsoft/playwright/${VERSION}/utils/docker/seccomp_profile.json"

mkdir -p "$(dirname "$DEST")"
echo "Fetching $URL"
curl -fsSL "$URL" -o "$DEST.tmp"

# Refuse anything that is not a plausible full profile. A truncated or wrong file
# here means either a broken container or silently disabled syscall filtering,
# and both are worse than not applying a profile at all.
if ! grep -q '"defaultAction"' "$DEST.tmp"; then
    echo "ERROR: downloaded file has no defaultAction - not a seccomp profile" >&2
    rm -f "$DEST.tmp"; exit 1
fi
if ! grep -q '"unshare"' "$DEST.tmp"; then
    echo "ERROR: profile does not permit unshare - Chromium's sandbox will not start" >&2
    rm -f "$DEST.tmp"; exit 1
fi
LINES=$(wc -l < "$DEST.tmp")
if [ "$LINES" -lt 200 ]; then
    echo "ERROR: profile is only $LINES lines; expected Docker's full default (~1000)" >&2
    rm -f "$DEST.tmp"; exit 1
fi

mv "$DEST.tmp" "$DEST"
chmod 0644 "$DEST"
echo "Installed $DEST ($LINES lines)"
echo
echo "Next: in Jenkinsfile's Deploy stage,"
echo "  1. add    --security-opt seccomp=$DEST"
echo "  2. remove -e CHROMIUM_NO_SANDBOX=1"
echo "Then redeploy and confirm vessel detection still works."
