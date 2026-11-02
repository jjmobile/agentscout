# FLOP participation operations

Updated 21 October 2026. The operator authorized the five-step strategy. This build prepares the existing
identity, launch monitoring, useful work, and durable spending evidence. It does not contain an official
testnet faucet client or production settlement verifier: their published contracts are still required.
The [official network status](https://ask.flop.finance/docs/network-testnet) remains the launch check;
the November 3 working target is not permission to enable an invented API.

## Activation status

The existing identity backup passed a local signing and verification check. Two useful jobs are saved
in `.airdrop/workloads.db`; repeated preparation creates no duplicates. Offline checks cover the new
guards, journal, workload queue, status report and watcher. No paid inference has been performed.

**Watcher activation verified; container deployment still unverified.** The October 19 follow-up
confirmed the login LaunchAgent is loaded with notifications enabled, its first run exited 0, and
all 11 official sources were fetched successfully at 06:29:20 UTC (14:29:20 Taipei). This workspace
still cannot access the Docker daemon, so the new image and deployed profile remain unverified.
The original in-session bootstrap failed with error 5; the subsequent host installation succeeded.
At the October 21 shutdown handoff, watcher health remained healthy: all 11 sources last fetched
successfully at 02:32:13 UTC / 10:32:13 Taipei, with heartbeat at 04:22:21 UTC. The prepared code remains
uncommitted on `main` at base commit `aef9e0d`; do not discard the working-tree implementation.
To install or reapply the setup from the operator's normal macOS Terminal with Docker Desktop available:

```sh
sh /Users/jjhuus/work/technocore/agentscout/scripts/activate_airdrop.sh
```

This installs the watcher for future logins with local macOS notifications, applies the operator
profile, checks the existing identity and prints status. It stops if an operation fails; rerunning
is supported. Service installation and successful source fetches are separate checks. FLOP spending
stays disabled after activation. Review the printed container health and source status; neither
the script nor its existence is evidence of earned allocation.

Validation on October 19: **299 tests passed, one skipped** (the local environment lacks
`py-sr25519-bindings`). The required syntax/name lint gate, patch whitespace check, shell syntax
and merged Compose validation passed. These are offline checks, not a live protocol or deployment test.

## Identity and deployment

The production DID is pinned in `compose.airdrop.yaml`. Startup refuses a missing key, a different key,
or a conflicting identity already recorded in the database. `scripts/show_did.py` now loads only;
it never creates a key. An existing backup can be verified locally without displaying key material:

```sh
.venv/bin/python scripts/check_identity.py \
  --key /path/to/existing/identity.backup \
  --expected-did did:key:z6MkwNoeDd24jWouuvbQkuCwf3a1o14ToqJiKezPcBQc3A7q
```

The operator profile disables payer paper trades, worker paper trades and daily self-audit task offers.
It preserves the observer, existing volume, and other configured services. It also forces both FLOP
live/provisional flags off. Deploy from this directory with:

```sh
sh scripts/deploy_airdrop.sh
```

The script validates Compose, requires a reachable Docker daemon, fingerprints the build sources,
rebuilds with the operator profile, checks the existing identity, and prints image and health status.
It never deletes a volume. The source fingerprint is stored in the image and runtime database;
an old commit alone cannot imply that new working-tree changes were deployed. The profile is operator
specific; forks must use their own existing identity.

Deployment must be verified on the host. A successful local test run or Compose validation does not
prove the container has been rebuilt. Docker permission errors leave deployment pending.

## Launch monitoring

See [LAUNCHWATCH.md](LAUNCHWATCH.md) for the fixed official source list, installation and health checks.
The standalone watcher does not need the agent container, credentials, a browser or an open chat.
It records failed fetches honestly and never changes spending settings based on web text.
The default installer only generates a repo-local plist; `--apply` loads it for the current login.
The activation command above explicitly uses `--install-at-login` to install this one watcher in the
user's LaunchAgents directory. That host installation is now verified.

Alerts are local macOS notifications titled **AgentScout FLOP monitor**, on official-source changes
and monitoring-health transitions. Sources are fetched every two hours; the initial healthy baseline
is silent. This watcher sends no Telegram, email, phone or Codex-chat messages. A notification test
from the coding session failed with AppleScript error -2741, but scheduled host runs subsequently
recorded successful native notification requests, most recently October 20 at 12:31:18 UTC / 20:31:18
Taipei. User-visible delivery remains unverified. Focus/notification settings can suppress presentation, and a stopped
watcher or sleeping Mac cannot deliver an alert. Review the named sources when a change alert appears;
the watcher does not infer that a faucet is live or decide an action automatically.

## Useful work

Two substantive documentation drafts are prepared in [workloads/initial_jobs.json](workloads/initial_jobs.json):
operator troubleshooting and interpreting the census correctly. Each has intended readers, bounded
source snapshots, exact hashes and review criteria. They are preparation, not executed or rewarded jobs.

```sh
mkdir -p .airdrop
.venv/bin/python scripts/prepare_workloads.py --db .airdrop/workloads.db
PYTHONPATH=src .venv/bin/python -m agentscout.workload --db .airdrop/workloads.db list
PYTHONPATH=src .venv/bin/python -m agentscout.workload --db .airdrop/workloads.db export JOB_ID
```

Repeating the seed command with identical inputs does not multiply work. Review source freshness and
continued usefulness before execution. Existing useful summaries can supply ongoing demand when
the official provider is available; do not regenerate unchanged material to inflate counters.
See [workload documentation](workloads/README.md) for capturing additional genuine tasks.

## Spending and settlement evidence

The new request journal is independent of any future HTTP or chain adapter. It records intent and
reserves integer base units before a submission, persists acknowledgements/transcripts before sending,
and stores the final receipt before requesting closure. Identical requests do not blindly resubmit
after a restart or timeout. Failed and ambiguous attempts retain their liabilities until resolved.
Old session rows migrate as `LEGACY_UNVERIFIED`, with escrow retained and fees explicitly unknown.

Default pilot limits are 1 FLOP escrow, 0.1 FLOP fee allowance, 1.1 FLOP total daily exposure and
1.1 FLOP outstanding exposure. They are local risk limits, not protocol rules or reward targets.
Amounts remain exact decimal strings or integers. The daily reservation budget applies to the date
an intent is recorded; outstanding liabilities span days. The allowance is not an authoritative maximum
network fee: the official adapter must quote/bound fees before signing. Actual verified fees, including
overruns, belong in reconciliation. The Anthropic USD ledger does not price FLOP activity.

The provisional transport is blocked by default, both at provider selection and construction.
FLOP startup checks provider readiness without buying a synthetic smoke-test request; the first
useful request validates actual output without consuming the pilot allowance on a repeated test prompt.
`FLOP_LIVE_ENABLED` and `FLOP_ALLOW_PROVISIONAL_TRANSPORT` are development gates, not a recipe for
production participation. The current operator profile forces them off. The direct chain signer also
requires `FLOP_EXPECTED_GENESIS_HASH` to match before accessing the private seed.

`InferenceJournal.reconcile` stores supplied evidence as unverified. A trusted adapter must independently
establish finality and bind network, account, channel, receipt root, amounts and transaction evidence
before it can release liability or record finalized expenditure. There is deliberately no command that
trusts a miner response or imported JSON merely because it contains `verified: true`.
No automatic close retry, escrow reclaim or production verifier is implemented until the official
protocol defines them. Uncertain requests stop safely for reconciliation instead of being repeated.

Read the local evidence without changing the database:

```sh
docker compose exec -T agentscout python -m agentscout.airdrop_status --db /data/agentscout.db
```

The report separates paper deals, local session labels, journal liabilities/finalized spend and official
allocation. Runtime timestamps show recorded activity, not independently proven process liveness.
Missing tables or inaccessible production data mean unknown. Finalized spend remains distinct from
official allocation, and unverified allocation files cannot turn either into claimable tokens.

## Official launch acceptance

Before releasing the first job, verify the official network and supported onboarding route, preserve
the existing DID attribution, confirm faucet cadence/limits, review current minimum activity and
independent-demand rules, and implement the official receipt/status verification interface.
Prefer the intended signed-HTTP route if released; the custom Substrate path is optional.

Then run one small useful request, confirm successful finalized settlement independently, reconcile
official scoring when available, and test restart/timeout recovery against that interface. Only then
increase useful throughput within the permitted faucet and exposure limits. Never send demand to a
miner under common control or multiply identities for faucet access.

Keep the operative rules and allocation evidence for snapshot review. No local count, transaction
submission, closed receipt, or script result proves an awarded allocation by itself.
