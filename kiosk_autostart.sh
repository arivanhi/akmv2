#!/bin/bash
# kiosk_autostart.sh - Peluncur Chromium Kiosk Fullscreen untuk AKMv2 di Raspberry Pi (labwc)
# Letakkan pemanggilan script ini di ~/.config/labwc/autostart

# Cegah duplikasi instans kiosk
exec 200>/tmp/akmv2_kiosk.lock
flock -n 200 || exit 0

export XDG_RUNTIME_DIR="/run/user/1000"
export WAYLAND_DISPLAY="wayland-0"
export DISPLAY=":0"

# Tunggu sampai server Docker AKMv2 (http://127.0.0.1:5000) siap menerima koneksi (maks 60 dtk)
for i in $(seq 1 60); do
    if curl -s -o /dev/null http://127.0.0.1:5000/login; then
        break
    fi
    sleep 1
done

# Bersihkan flag crash lama Chromium agar tidak muncul popup "Restore pages?"
PREF_FILE="/home/admin/.config/chromium/Default/Preferences"
if [ -f "$PREF_FILE" ]; then
    sed -i 's/"exited_cleanly":false/"exited_cleanly":true/g' "$PREF_FILE" 2>/dev/null || true
    sed -i 's/"exit_type":"Crashed"/"exit_type":"Normal"/g' "$PREF_FILE" 2>/dev/null || true
fi

exec /usr/bin/chromium \
    --kiosk \
    --ozone-platform=wayland \
    --enable-features=UseOzonePlatform \
    --enable-wayland-ime \
    --touch-events=enabled \
    --noerrdialogs \
    --disable-infobars \
    --disable-session-crashed-bubble \
    --check-for-update-interval=31536000 \
    http://127.0.0.1:5000
