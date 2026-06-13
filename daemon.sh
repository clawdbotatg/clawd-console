#!/usr/bin/env bash
# Run clawd-console as a launchd LaunchAgent so it survives closing the terminal,
# restarts if it dies (KeepAlive), and starts at login (RunAtLoad). The server
# itself re-attaches to the live Claude session via --resume on each (re)start.
#
#   ./daemon.sh install [WORKDIR]   # default WORKDIR = this dir
#   ./daemon.sh uninstall
#   ./daemon.sh restart
#   ./daemon.sh status
#   ./daemon.sh logs
set -euo pipefail

LABEL="com.clawd.console"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/clawd-console.log"
PY="$(command -v python3)"
CLAUDE_BIN="${CLAUDE_BIN:-$(command -v claude)}"   # absolute path — launchd has a bare PATH
PORT="${PORT:-7900}"
BIND="${BIND:-0.0.0.0}"
DOMAIN="gui/$(id -u)"

cmd="${1:-}"
case "$cmd" in
  install)
    WORKDIR="${2:-$HERE}"
    # free the port if something (e.g. a manual test run) is holding it
    pkill -f "$HERE/server.py" 2>/dev/null || true
    sleep 1
    mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
    cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$HERE/server.py</string>
  </array>
  <key>WorkingDirectory</key><string>$HERE</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PORT</key><string>$PORT</string>
    <key>BIND</key><string>$BIND</string>
    <key>WORKDIR</key><string>$WORKDIR</string>
    <key>CLAUDE_BIN</key><string>$CLAUDE_BIN</string>
    <key>PATH</key><string>$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLISTEOF
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$PLIST"
    launchctl enable "$DOMAIN/$LABEL" 2>/dev/null || true
    echo "installed + loaded. workdir=$WORKDIR  log=$LOG"
    echo "tokenized URL is printed in the log:"
    sleep 2; grep -E "local|phone|⚠" "$LOG" | tail -3 || true
    ;;
  uninstall)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "uninstalled."
    ;;
  restart)
    launchctl kickstart -k "$DOMAIN/$LABEL"
    echo "restarted."
    ;;
  status)
    launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -E "state =|pid =" || echo "not loaded"
    ;;
  logs)
    tail -f "$LOG"
    ;;
  *)
    echo "usage: $0 {install [WORKDIR]|uninstall|restart|status|logs}"; exit 1 ;;
esac
