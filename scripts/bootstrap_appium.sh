#!/usr/bin/env bash
set -euo pipefail

# Run this on the private host that can reach the selected cloud phone.
# It does not discover or select devices. Lobster must supply the udid/device ID.

if ! command -v node >/dev/null 2>&1; then
  echo 'Node.js is required. Install a supported current LTS Node.js first.' >&2
  exit 1
fi

npm install --global appium@3
appium driver install uiautomator2 || appium driver update uiautomator2
appium driver doctor uiautomator2

cat <<'EOF'

Start Appium with:
  appium --address 127.0.0.1 --port 4723

For a LAN/cloud device farm, bind Appium only to a private interface and firewall it.
Do not expose Appium directly to the public Internet and do not use --relaxed-security.
EOF
