#!/usr/bin/env bash
# Wireless video link over a USRP B210 — one-command demo.
#
#   ./run.sh              # live: encode -> transmit over the air -> VLC plays
#   ./run.sh file         # store-and-forward + byte-exact integrity check
#   ./run.sh cam          # realtime pipeline: camera->encode->air->VLC
#   ./run.sh selftest     # modem only, no radio
#
# Env: FREQ=915e6 TX_GAIN=50 RX_GAIN=20 PORT=18096 LOOP=1 START=0 VIDEO=/path.mp4
#   LOOP=0 -> keep streaming until Ctrl-C
#   DURATION=60 -> seconds to run in cam mode
#   VIDEO unset -> a synthetic 30 s source is generated (no camera needed)
set -euo pipefail
cd "$(dirname "$0")"

MODE="${1:-live}"
FREQ="${FREQ:-915e6}"
TX_GAIN="${TX_GAIN:-50}"
RX_GAIN="${RX_GAIN:-20}"
PORT="${PORT:-18096}"
LOOP="${LOOP:-1}"
START="${START:-0}"
VIDEO="${VIDEO:-}"
CLIP=clip.ts

say() { printf '\n=== %s ===\n' "$*"; }

say "0. sanity check"
command -v uhd_find_devices >/dev/null || { echo "UHD missing: run sudo ./setup.sh"; exit 1; }
uhd_find_devices 2>/dev/null | grep -q serial || {
  echo "no USRP found — check the USB cable and the udev rule (sudo ./setup.sh)"; exit 1; }
uhd_find_devices 2>/dev/null | grep -E 'serial|product'

if [ "$MODE" = "selftest" ]; then
  say "1. modem selftest (software only)"
  python3 sdr_link.py selftest
  exit 0
fi

if [ ! -f "$CLIP" ]; then
  if [ -n "$VIDEO" ] && [ -f "$VIDEO" ]; then
    say "1. encoding $VIDEO (480x270, 110k video + 24k audio)"
    ffmpeg -y -hide_banner -loglevel error -ss "$START" -t 30 -i "$VIDEO" \
      -vf scale=480:270 -r 20 -c:v libx264 -profile:v baseline -preset veryfast \
      -b:v 110k -maxrate 140k -bufsize 240k -g 40 -keyint_min 40 -sc_threshold 0 \
      -c:a aac -b:a 24k -ac 2 -f mpegts "$CLIP"
  else
    say "1. no VIDEO given — generating a synthetic 30 s source (lavfi testsrc2)"
    ffmpeg -y -hide_banner -loglevel error \
      -f lavfi -i testsrc2=size=640x360:rate=20 \
      -f lavfi -i sine=frequency=800:sample_rate=48000 \
      -t 30 -c:v libx264 -profile:v baseline -preset veryfast \
      -b:v 110k -maxrate 140k -bufsize 240k -g 40 -keyint_min 40 -sc_threshold 0 \
      -c:a aac -b:a 24k -ac 2 -f mpegts "$CLIP"
  fi
fi

if [ "$MODE" = "file" ]; then
  say "2. transmitting $CLIP over the air"
  python3 sdr_link.py file --input "$CLIP" --output rx_check.ts \
    --freq "$FREQ" --tx-gain "$TX_GAIN" --rx-gain "$RX_GAIN"
  say "3. integrity check"
  cmp -n "$(stat -c%s "$CLIP")" "$CLIP" rx_check.ts && echo "BYTE-EXACT over the air"
  exit 0
fi

if [ "$MODE" = "cam" ]; then
  say "2. starting VLC on udp://@127.0.0.1:$PORT"
  vlc --no-video-title-show --network-caching=1000 "udp://@127.0.0.1:$PORT" >/tmp/vlc-sdr.log 2>&1 &
  VLC_PID=$!
  sleep 2
  say "3. live pipeline: synthetic camera -> x264 -> B210 -> UDP -> VLC"
  ffmpeg -hide_banner -loglevel error -re \
    -f lavfi -i testsrc2=size=640x360:rate=20 \
    -f lavfi -i sine=frequency=800:sample_rate=48000 \
    -t "${DURATION:-60}" \
    -c:v libx264 -profile:v baseline -preset veryfast -tune zerolatency \
    -b:v 600k -maxrate 700k -bufsize 1200k -g 20 -keyint_min 20 -sc_threshold 0 \
    -c:a aac -b:a 48k -ac 2 -f mpegts - \
  | python3 sdr_link.py pipe --udp-port "$PORT" --duration "$(( ${DURATION:-60} + 15 ))" \
      --freq "$FREQ" --tx-gain "$TX_GAIN" --rx-gain "$RX_GAIN"
  exit 0
fi

say "2. starting VLC on udp://@127.0.0.1:$PORT"
vlc --no-video-title-show --network-caching=1500 "udp://@127.0.0.1:$PORT" >/tmp/vlc-sdr.log 2>&1 &
VLC_PID=$!
sleep 2

say "3. streaming $CLIP over the air"
python3 sdr_link.py live --input "$CLIP" --udp-port "$PORT" --loop "$LOOP" \
  --freq "$FREQ" --tx-gain "$TX_GAIN" --rx-gain "$RX_GAIN" 2>/dev/null

say "done — VLC keeps playing (pid $VLC_PID); close its window when finished"
