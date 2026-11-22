#!/usr/bin/env bash
# Install the runner as a systemd user service for the current user (rootless podman).
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
config=${HW_REVIEW_RUNNER_CONFIG:-$HOME/.config/hw-review/runner.toml}
python=$(command -v python3)
if [[ ! -f $config ]]; then
    echo "Write $config first; start from runner/runner.example.toml." >&2
    exit 1
fi
if ! "$python" -c 'import sys, tomllib; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    echo "The runner needs python3 3.11 or newer (for tomllib); $python is older." >&2
    exit 1
fi

unit=$HOME/.config/systemd/user/hw-review-runner.service
mkdir -p "$(dirname "$unit")"
cat > "$unit" <<EOF
[Unit]
Description=hw-review job runner
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=$python $repo/runner/review_runner.py --config $config
Restart=on-failure
RestartSec=30
# On stop the runner stops the container and reports the job; give it time.
TimeoutStopSec=180

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now hw-review-runner.service
echo "Installed $unit."
echo "Logs: journalctl --user -u hw-review-runner -f"
echo "To keep it running while you are logged out: loginctl enable-linger $USER"
