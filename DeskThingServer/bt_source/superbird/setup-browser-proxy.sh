#!/bin/sh
# Point the device's browser at the internet-sharing on-ramp, or undo that.
#
#   setup-browser-proxy.sh enable    add the proxy flags to chromium
#   setup-browser-proxy.sh disable   restore the original command line
#   setup-browser-proxy.sh status    report what is configured
#
# Chromium is launched by supervisord from a fixed command line. We add:
#   --proxy-server=socks5://127.0.0.1:1080   send web traffic to the mux's
#                                            SOCKS on-ramp, which tunnels it
#   --proxy-bypass-list="localhost;..."      EXCEPT loopback, so DeskThing's
#                                            own traffic stays direct
#
# The bypass list is not optional. This browser is Chromium 69, which predates
# Chrome 72's implicit localhost bypass — without it the client's own
# http://localhost:8891 requests would be sent through the proxy, and the
# transport probe at /__bt would stop being answered locally.
#
# The value is quoted because ';' starts a comment in supervisord's INI
# parser; unquoted, the bypass list would be silently truncated.
#
# Idempotent, and keeps a pristine copy the first time it changes anything.

set -e

CONF=/etc/supervisord.conf
ORIG=/etc/supervisord.conf.deskthing-orig
PROXY='--proxy-server=socks5://127.0.0.1:1080 --proxy-bypass-list="localhost;127.0.0.1;[::1]" '
ANCHOR='chromium-browser/chrome '

usage() {
    echo "usage: $0 enable|disable|status" >&2
    exit 2
}

is_enabled() {
    grep -q -- '--proxy-server=socks5://127.0.0.1:1080' "$CONF"
}

case "$1" in
enable)
    if is_enabled; then
        echo "browser proxy: already enabled"
        exit 0
    fi
    mount -o remount,rw / 2>/dev/null || true
    [ -f "$ORIG" ] || cp "$CONF" "$ORIG"
    # Insert the flags immediately after the binary. Using # as the sed
    # delimiter keeps the // in the proxy URL from needing escapes.
    sed -i "s#${ANCHOR}#${ANCHOR}${PROXY}#" "$CONF"
    if ! is_enabled; then
        echo "browser proxy: FAILED to patch $CONF" >&2
        exit 1
    fi
    supervisorctl reread >/dev/null 2>&1 || true
    supervisorctl update >/dev/null 2>&1 || true
    supervisorctl restart chromium >/dev/null 2>&1 || true
    echo "browser proxy: enabled"
    ;;
disable)
    if [ -f "$ORIG" ]; then
        mount -o remount,rw / 2>/dev/null || true
        cp "$ORIG" "$CONF"
    else
        mount -o remount,rw / 2>/dev/null || true
        sed -i "s#${PROXY}##" "$CONF"
    fi
    supervisorctl reread >/dev/null 2>&1 || true
    supervisorctl update >/dev/null 2>&1 || true
    supervisorctl restart chromium >/dev/null 2>&1 || true
    echo "browser proxy: disabled"
    ;;
status)
    if is_enabled; then
        echo "browser proxy: enabled"
    else
        echo "browser proxy: disabled"
    fi
    ;;
*)
    usage
    ;;
esac
