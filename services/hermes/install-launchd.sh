#!/bin/sh
# Keep the test Hermes container running: a LaunchAgent that runs run.sh and restarts it if it exits
# (Apple `container` has no restart policy; architecture §3). Pass --load to start it now.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
label=com.victoria.hermes
plist="$HOME/Library/LaunchAgents/$label.plist"
logs="${ASSISTANT_LOG_DIR:-$HOME/Library/Logs/Victoria Voice Assistant}"
mkdir -p "$logs" "$(dirname "$plist")"

cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$label</string>
    <key>ProgramArguments</key>
    <array><string>/bin/sh</string><string>$here/run.sh</string></array>
    <key>EnvironmentVariables</key>
    <dict><key>PATH</key><string>/usr/local/bin:/usr/bin:/bin</string></dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>10</integer>
    <key>StandardOutPath</key><string>$logs/hermes.log</string>
    <key>StandardErrorPath</key><string>$logs/hermes-error.log</string>
</dict>
</plist>
EOF
plutil -lint "$plist"
echo "Wrote $plist"
if [ "${1:-}" = "--load" ]; then
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$plist"
    echo "Loaded $label"
fi
