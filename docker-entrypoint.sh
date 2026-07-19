#!/bin/sh
set -e
# Railway mounts volumes as root; fix /data ownership before the app starts.
if [ "$(stat -c %u /data 2>/dev/null || echo 0)" = "0" ]; then
    chown -R becoming:becoming /data
fi
exec runuser -u becoming -- "$@"
