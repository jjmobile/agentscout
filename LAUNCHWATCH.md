# Persistent FLOP launch monitoring

`src/agentscout/launchwatch.py` reads official public documents without importing
AgentScout settings, keys, wallets, inference clients, or notification integrations.
It uses only the Python 3.9+ standard library. It does not execute document content,
submit forms, claim tokens, or enable inference. Its participation status always
remains `awaiting_official_onboarding`; a person must verify the released network
and onboarding procedure separately.

The October 19, 2026 source review still found draft airdrop/testnet terms and no
Yellow Paper GitHub releases. The [official timeline FAQ](https://ask.flop.finance/docs/testnet-and-timeline)
gives November 3 as a working target, not a switch-on date. The [network status](https://ask.flop.finance/docs/network-testnet)
requires published network identity, artifacts and admission details. Intermittent
fetch failures on Ask FLOP are recorded as failures, not proof of launch or closure.

## Run and inspect

From the `agentscout` directory:

```sh
python3 src/agentscout/launchwatch.py check
python3 src/agentscout/launchwatch.py status
```

`check` fetches now; `tick` fetches only when due and otherwise updates a local
heartbeat. `status` performs no network requests and recomputes health against the
current clock, so a stopped scheduler cannot look healthy indefinitely. Exit codes:
0 healthy (or another instance already running), 2 pending/degraded/stale, 3 fatal
state or execution error. All commands accept `--state-dir /absolute/path`.

The default state directory is `.launchwatch/` inside this checkout:

- `state.json`: per-source last attempt/success, normalized SHA-256, final URL,
  consecutive and total failures, last error, latest change summary, poll schedule,
  and the latest 100 baseline/change records.
- `status.json`: compact health view written every scheduler tick. Its `as_of`
  timestamp matters; use the status command for current health.
- `snapshots/<source>/<sha256>.txt`: normalized source evidence. Latest snapshots
  and the last 100 change records are retained. This is a bounded monitoring cache;
  copy operative launch/snapshot rules separately into the allocation evidence.
- `watch.lock`: OS-released process lock; a crash does not leave a stale lock.
- Generated launchd plist and local stdout/stderr logs after installation.

Each source is polled every two hours. One failure is degraded; two consecutive
failures or four hours since the last successful fetch is stale. A missing scheduler
heartbeat becomes stale after 21 minutes. A failed fetch never replaces a good
snapshot or advances its success timestamp. Corrupt state stops the run and is
preserved for investigation. Writes use temporary files, fsync and atomic rename.

HTTP reads allow only fixed official HTTPS hosts, reject redirects outside them,
cap each response at 2 MiB and kill each fetch subprocess after 25 seconds. Three
fetches run concurrently. Empty GitHub release lists are valid; error responses,
unexpected content and truncated pages are failures. HTML menus/scripts and GitHub
stars/follower counters are excluded from change detection. Changes get deterministic
line counts and excerpts for review; no language model interprets eligibility.

## Install independently of Codex on this Mac

Generate and inspect the plist first (this does not load a service):

```sh
python3 scripts/install_launchwatch.py --notify
plutil -lint .launchwatch/chat.technocore.agentscout.launchwatch.plist
```

There are two explicit activation modes. **Current login only**, with all files
inside this checkout:

```sh
python3 scripts/install_launchwatch.py --apply --notify
launchctl print gui/$(id -u)/chat.technocore.agentscout.launchwatch
```

**This login and future logins**, run by the operator in a normal macOS terminal:

```sh
python3 scripts/install_launchwatch.py --install-at-login --notify
```

Only `--install-at-login` writes
`~/Library/LaunchAgents/chat.technocore.agentscout.launchwatch.plist`, then loads and
verifies that known job. The default and `--apply` never write that directory.
The login-install flag was not run by this coding task because that directory is
outside the permitted writable roots. If activation fails, the script prints a
concise error and preserves generated files; it does not claim the service is live
or try an alternative execution path. A saved plist alone does not confirm an
active service or successful network fetches.

The job uses an absolute Python interpreter and script path, runs immediately, then
ticks every ten minutes. Source fetches remain on the two-hour cadence. It survives
closing Codex or a terminal. Re-running `--apply` replaces only this job. Use a stable
Python path with `--python` if the default interpreter may be removed.

In repo-only mode, bootstrap again after logout/reboot. Login mode lets launchd
load the plist on future GUI logins after reboot; it does not run before login or
while powered off. Keep this checkout and interpreter at their current paths.

`--notify` enables local native macOS notifications on official-document changes and
monitoring-health transitions. A successful first baseline is quiet. Transitions are
saved before requesting a notification, so unchanged sources and process restarts
do not repeatedly alert. Native notification errors are recorded in status, with
no repeated retry until another transition. macOS notification permissions and Focus
settings can suppress presentation; `native_notification_requested` is not proof that
the user saw it. Notification text contains fixed source names and health labels,
never remote prose. Omit `--notify` to use stdout and local status only.

Sleeping or powered-off machines cannot fetch or deliver alerts. On wake, the next
tick fetches overdue sources. Local status can detect a stopped service when read;
the service cannot notify while it itself is stopped. There are no Telegram, email,
or social messages. Check the
local status daily and after a reboot; use an always-on host later if unattended
availability is required. The launchd logs are local and may be rotated manually.

Unload without deleting evidence:

```sh
python3 scripts/install_launchwatch.py --remove
```

`--remove` leaves the login plist intact, so login mode would load again next time.
To disable future loading as well, the operator can remove that one plist:

```sh
rm "$HOME/Library/LaunchAgents/chat.technocore.agentscout.launchwatch.plist"
```

## Sources and review procedure

The fixed source list covers airdrop terms, testnet rules, timeline FAQ, testnet
network status, agent integration, operator hub, both web and GitHub Yellow Papers,
official repository discovery, and Yellow Paper/Technocore GitHub releases. The
mirror and website are separate because they may lag one another.

New official repositories generate a discovery change. The watcher does not
automatically trust or install their software. Review a new node/agent repository
and add its release URL to `SOURCES` when FLOP publishes it. This avoids inventing
an endpoint or confusing a private development repository with public onboarding.

For each relevant change, inspect the canonical source and its stored snapshot.
Confirm the eligible network/genesis, supported release, official API, identity
attribution, faucet rules and settled-spend evidence before implementing onboarding.
Keep historical testnet/devnet data distinct. No watched text or calendar date can
change the agent's spending settings.

Offline verification:

```sh
python3 -m pytest tests/test_launchwatch.py -q
```
