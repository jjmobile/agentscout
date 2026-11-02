#!/bin/sh
# Apply the operator profile without changing/removing the persistent volume.
set -eu
cd "$(dirname "$0")/.."

docker compose -f compose.yaml -f compose.airdrop.yaml config --quiet
# Stop here if the daemon is inaccessible; never claim a local build is deployed.
docker info --format '{{.ServerVersion}}' >/dev/null

AGENTSCOUT_SOURCE_REVISION=$(python3 - <<'PY'
import hashlib
from pathlib import Path
import subprocess
digest = hashlib.sha256()
paths = [Path('Dockerfile'), Path('pyproject.toml'), Path('README.md'), Path('LICENSE')]
paths += list(Path('src').rglob('*.py')) + list(Path('scripts').glob('*'))
paths += list(Path('src/agentscout/data').glob('*'))
for path in sorted(set(paths)):
    if path.is_file():
        digest.update(str(path).encode() + b'\0' + path.read_bytes() + b'\0')
commit = subprocess.check_output(['git', 'rev-parse', '--short', 'HEAD'], text=True).strip()
print(commit + '-source-' + digest.hexdigest()[:16])
PY
)
export AGENTSCOUT_SOURCE_REVISION

docker compose -f compose.yaml -f compose.airdrop.yaml up -d --build agentscout
docker compose -f compose.yaml -f compose.airdrop.yaml exec -T agentscout \
    python scripts/check_identity.py --key /data/identity.key \
    --expected-did did:key:z6MkwNoeDd24jWouuvbQkuCwf3a1o14ToqJiKezPcBQc3A7q
docker inspect --format 'Image {{.Image}}; state {{.State.Status}}; health {{if .State.Health}}{{.State.Health.Status}}{{else}}unknown{{end}}' agentscout-agent
printf '%s\n' "Source: $AGENTSCOUT_SOURCE_REVISION" \
  'Profile applied. FLOP spending is disabled; paper-deal routines are disabled.' \
  'Use the read-only airdrop status command to verify the runtime after startup.'
