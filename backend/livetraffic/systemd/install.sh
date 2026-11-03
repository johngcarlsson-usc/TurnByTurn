#!/usr/bin/env bash
# Install the Exagion collectors as permanent user services (survive reboot and logout).
# Replaces the transient units of the same names that a session may have started.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
# default: every collector; pass unit names to install fewer (e.g. a machine that already runs Photon)
units=("${@:-exagion-livetraffic exagion-livetraffic-open exagion-cctv exagion-photon}")
units=(${units[@]})
for u in "${units[@]}"; do systemctl --user stop "$u" 2>/dev/null || true; done
if [[ " ${units[*]} " == *" exagion-photon "* ]]; then
  pkill -f 'photon-1.3.0.jar serve' 2>/dev/null || true   # a Photon started by hand
fi
mkdir -p ~/.config/systemd/user
cp "$here"/*.service "$here"/*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now "${units[@]/%/.service}" exagion-nightly.timer
loginctl enable-linger "$USER"      # keep user services running without a login
systemctl --user --no-pager list-units 'exagion-*'
