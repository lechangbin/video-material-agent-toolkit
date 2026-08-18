#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" != "desktop" ]]; then
    exec "$@"
fi

display="${DISPLAY:-:99}"
width="${DISPLAY_WIDTH:-1440}"
height="${DISPLAY_HEIGHT:-900}"
depth="${DISPLAY_DEPTH:-24}"

mkdir -p /data/cache /data/config /data/localappdata

children=()
cleanup() {
    if ((${#children[@]} > 0)); then
        kill "${children[@]}" 2>/dev/null || true
        wait "${children[@]}" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

Xvfb "$display" \
    -screen 0 "${width}x${height}x${depth}" \
    -nolisten tcp \
    -ac \
    >/tmp/xvfb.log 2>&1 &
children+=("$!")

for _ in {1..100}; do
    if xdpyinfo -display "$display" >/dev/null 2>&1; then
        break
    fi
    sleep 0.1
done

if ! xdpyinfo -display "$display" >/dev/null 2>&1; then
    cat /tmp/xvfb.log >&2
    echo "Xvfb did not become ready." >&2
    exit 1
fi

fluxbox -display "$display" >/tmp/fluxbox.log 2>&1 &
children+=("$!")

x11vnc \
    -display "$display" \
    -forever \
    -shared \
    -localhost \
    -nopw \
    -rfbport 5900 \
    >/tmp/x11vnc.log 2>&1 &
children+=("$!")

websockify \
    --web=/usr/share/novnc/ \
    6080 \
    127.0.0.1:5900 \
    >/tmp/novnc.log 2>&1 &
children+=("$!")

echo "Video Material Agent Toolkit is ready."
echo "Open http://127.0.0.1:6080/vnc.html for interactive platform login."
echo "Run CLI commands with: docker compose exec toolkit <command>"

set +e
wait -n "${children[@]}"
status=$?
set -e

echo "A desktop service exited unexpectedly (status ${status})." >&2
for log in /tmp/xvfb.log /tmp/fluxbox.log /tmp/x11vnc.log /tmp/novnc.log; do
    if [[ -s "$log" ]]; then
        echo "--- ${log} ---" >&2
        tail -n 50 "$log" >&2
    fi
done
exit "$status"
