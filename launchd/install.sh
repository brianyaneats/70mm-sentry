#!/bin/sh
# Installs (or reinstalls) the three 70mm Sentry launchd jobs on macOS.
# Idempotent: safe to rerun after moving the repo or editing templates.
set -eu
REPO="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$HOME/Library/LaunchAgents"
UID_N="$(id -u)"
[ -f "$REPO/config.json" ] || {
  echo "no config.json — run: cp config.example.json config.json (then edit)"; exit 1; }
mkdir -p "$DEST"
for t in "$REPO"/launchd/*.plist.template; do
  name="$(basename "${t%.template}")"
  label="${name%.plist}"
  sed "s|__REPO__|$REPO|g" "$t" > "$DEST/$name"
  plutil -lint "$DEST/$name" > /dev/null
  launchctl bootout "gui/$UID_N/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$UID_N" "$DEST/$name"
  echo "installed + loaded $label"
done
echo "verify: python3 $REPO/sentry.py --status"
