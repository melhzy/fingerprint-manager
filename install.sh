#!/bin/sh
# Add a launcher for this checkout to the current user's app grid.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
launcher="$apps/io.github.melhzy.FingerprintManager.desktop"

mkdir -p "$apps"
cat > "$launcher" <<EOF
[Desktop Entry]
Type=Application
Name=Fingerprint Manager
Comment=Enroll and test fingerprints with step-by-step guidance
Exec="$here/fingerprint_manager.py"
Icon=auth-fingerprint-symbolic
Terminal=false
Categories=Settings;Security;
Keywords=fingerprint;enroll;biometric;sensor;
EOF

echo "Launcher written to $launcher"
