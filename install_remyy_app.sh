#!/bin/bash
# Builds Remyy.app: a one-click, full-screen launcher for Remyy.
#
# Usage (Terminal, from the folder holding these files):
#   bash install_remyy_app.sh                 -> installs to ~/Applications/Remyy.app
#   bash install_remyy_app.sh /Applications   -> installs for all users (may need sudo)
#
# Needs next to this script: remyy.html, remyy_server.py, remyy_icon.png (icon is optional).
# Re-run it any time you update remyy.html or remyy_server.py.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="${1:-$HOME/Applications}"
APP="$DEST/Remyy.app"

for f in remyy.html remyy_server.py; do
  [ -f "$HERE/$f" ] || { echo "Missing $f. Put it in the same folder as this script."; exit 1; }
done

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$HERE/remyy.html" "$HERE/remyy_server.py" "$APP/Contents/Resources/"

# ---------- launcher ----------
cat > "$APP/Contents/MacOS/Remyy" <<'LAUNCHER'
#!/bin/bash
# Remyy launcher: starts the helper, opens Chrome full screen (kiosk), stops the helper when Chrome quits.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
RES="$(cd "$(dirname "$0")/../Resources" && pwd)"
PORT=8765
URL="http://localhost:$PORT/remyy.html"
DATA="$HOME/.remyy"
CHROME="${REMYY_CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
mkdir -p "$DATA"
LOG="$DATA/launcher.log"

alert() {
  osascript -e 'on run argv' -e 'display alert "Remyy" message (item 1 of argv)' -e 'end run' "$1" >/dev/null 2>&1 || echo "$1" >&2
}

[ -x "$CHROME" ] || { alert "Remyy needs Google Chrome. Install it from google.com/chrome and try again."; exit 1; }
command -v python3 >/dev/null 2>&1 || { alert "Remyy needs python3. Open Terminal and run: xcode-select --install"; exit 1; }

# stop a leftover Remyy helper from a previous run (only if the saved pid is really ours)
if [ -f "$DATA/helper.pid" ]; then
  OLD="$(cat "$DATA/helper.pid" 2>/dev/null)"
  if [ -n "$OLD" ] && ps -p "$OLD" -o command= 2>/dev/null | grep -q remyy_server.py; then
    kill "$OLD" 2>/dev/null; sleep 0.6
  fi
  rm -f "$DATA/helper.pid"
fi
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then
  alert "Port $PORT is being used by another program. Quit it and open Remyy again."
  exit 1
fi

python3 "$RES/remyy_server.py" --port "$PORT" --no-browser >>"$LOG" 2>&1 &
SRV=$!
echo "$SRV" > "$DATA/helper.pid"
cleanup() { kill "$SRV" 2>/dev/null; rm -f "$DATA/helper.pid"; }
trap cleanup EXIT
trap 'exit 0' TERM INT HUP

UP=0
for _ in $(seq 1 60); do
  if curl -fs -o /dev/null "$URL"; then UP=1; break; fi
  kill -0 "$SRV" 2>/dev/null || break
  sleep 0.2
done
[ "$UP" = 1 ] || { alert "The Remyy helper did not start. Details are in $LOG"; exit 1; }

# own Chrome profile: keeps Remyy separate from your normal Chrome, so the flags below always apply
"$CHROME" \
  --user-data-dir="$DATA/chrome-profile" \
  --app="$URL" --kiosk \
  --no-first-run --no-default-browser-check \
  --disable-session-crashed-bubble --disable-features=Translate \
  --autoplay-policy=no-user-gesture-required \
  >>"$LOG" 2>&1
LAUNCHER
chmod +x "$APP/Contents/MacOS/Remyy"

# ---------- icon ----------
ICON_KEY=""
if [ -f "$HERE/remyy_icon.png" ] && command -v sips >/dev/null 2>&1 && command -v iconutil >/dev/null 2>&1; then
  SET="$(mktemp -d)/Remyy.iconset"; mkdir -p "$SET"
  for s in 16 32 128 256 512; do
    sips -z $s $s "$HERE/remyy_icon.png" --out "$SET/icon_${s}x${s}.png" >/dev/null
    sips -z $((s*2)) $((s*2)) "$HERE/remyy_icon.png" --out "$SET/icon_${s}x${s}@2x.png" >/dev/null
  done
  iconutil -c icns "$SET" -o "$APP/Contents/Resources/Remyy.icns" && ICON_KEY="<key>CFBundleIconFile</key><string>Remyy</string>"
fi

# ---------- Info.plist ----------
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Remyy</string>
  <key>CFBundleDisplayName</key><string>Remyy</string>
  <key>CFBundleIdentifier</key><string>local.remyy.app</string>
  <key>CFBundleExecutable</key><string>Remyy</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
  $ICON_KEY
</dict></plist>
PLIST

# files from a browser download carry a quarantine flag; clear it, then sign locally
xattr -cr "$APP" 2>/dev/null || true
codesign --force --deep -s - "$APP" >/dev/null 2>&1 || true
touch "$APP"

echo "Installed: $APP"
echo "Open it from Finder or Spotlight (type Remyy), or drag it to your Dock."
echo "Quit with Cmd+Q. The first launch asks for microphone and camera access."
