#!/bin/sh
# Run in the operator's own Terminal to activate the tested profile and launchwatch.
set -eu
cd "$(dirname "$0")/.."

# Installs only this watcher label at login; does not touch the identity or agent volume.
python3 scripts/install_launchwatch.py --install-at-login --notify
sh scripts/deploy_airdrop.sh
docker compose -f compose.yaml -f compose.airdrop.yaml exec -T agentscout \
    python -m agentscout.airdrop_status --db /data/agentscout.db
python3 src/agentscout/launchwatch.py status
