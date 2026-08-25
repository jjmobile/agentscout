#!/usr/bin/env bash
# Copy AgentScout's private identity key out of the Docker volume to a file only you can read.
# usage: scripts/backup_identity.sh <destination-file>
# restore: docker compose cp <file> agentscout:/data/identity.key   (container must be stopped first, then start it)
set -euo pipefail
dest="${1:?destination file required}"
umask 077
docker compose cp agentscout:/data/identity.key "$dest"
chmod 600 "$dest"
echo "identity key copied to $dest (mode 600). Treat it like a password."
