#!/usr/bin/env bash
# One-time host setup for the USRP B210: UHD, USB udev rules, FPGA images.
#   sudo ./setup.sh
#
# Needed because a fresh install of uhd-host cannot (a) let a normal user open
# the B210 over USB, or (b) find the firmware/FPGA bitstreams Debian strips out.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "run as root: sudo $0"; exit 1; }

echo "== 1/4 apt install uhd =="
export DEBIAN_FRONTEND=noninteractive
apt-get install -y --no-install-recommends libuhd-dev uhd-host python3-uhd

echo "== 2/4 udev rules =="
RULES=/usr/libexec/uhd/utils/uhd-usrp.rules
[ -f "$RULES" ] || RULES=/usr/share/uhd/utils/uhd-usrp.rules
install -Dm644 "$RULES" /etc/udev/rules.d/10-uhd-usrp.rules
udevadm control --reload-rules
udevadm trigger --subsystem-match=usb --action=change || true
echo "installed /etc/udev/rules.d/10-uhd-usrp.rules"

echo "== 3/4 FPGA images =="
uhd_images_downloader -t b2 || true

echo "== 4/4 check =="
uhd_find_devices 2>/dev/null | grep -E 'serial|product' || \
  echo "device not found yet — replug the B210 and run uhd_find_devices"
echo "done"
