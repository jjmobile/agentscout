# Useful work and evidence status

This offline queue prepares two documentation drafts with an intended reader, source snapshots and
review criteria. It does not buy inference, add fake conversations, cycle paper deals, or claim rewards.
The existing summarizer remains the workload for genuine future summary requests; do not regenerate
unchanged material simply to increase spend.

From the `agentscout` directory, prepare the reviewed tasks in a **separate** local database:

```sh
.venv/bin/python scripts/prepare_workloads.py --db data/workloads.db
PYTHONPATH=src .venv/bin/python -m agentscout.workload --db data/workloads.db list
PYTHONPATH=src .venv/bin/python -m agentscout.workload --db data/workloads.db export JOB_ID
```

Export writes a provider-neutral JSON request to stdout. No provider is called and export does not mark
work executed or completed. Capture a file using shell redirection only if it is useful to review.
Re-enqueueing the same task and source contents returns `added: false`; a different title, path or
purpose does not multiply work. A changed task or genuinely changed source may produce a new item.
The captured inputs, hashes, purpose and quality criteria stay attached to the item even when source
files change. Review freshness before execution, particularly rules that can change before testnet.
If a task is no longer useful, do not execute it. There is deliberately no automatic executor yet.

To add a substantive task, copy the format in `initial_jobs.json`. It requires `title`, `purpose`,
`task`, `deliverable`, `quality_criteria` and `sources`. Source paths are relative to `--source-root`,
must stay inside it, and must be public documentation or JSON; keys and secret directories are refused.
Use bounded inputs (256 KiB per source, 1 MiB per job). A declared source URL is provenance supplied
by the author, not proof that the local file is an official/current copy.

Read production status without opening secrets or migrating the agent database:

```sh
PYTHONPATH=src .venv/bin/python -m agentscout.airdrop_status --db /path/to/agentscout.db
```

Inside an already-running container the equivalent is:

```sh
docker compose exec -T agentscout python -m agentscout.airdrop_status --db /data/agentscout.db
```

This is a local evidence report, not a chain query. It keeps these facts separate:

| Field | Meaning |
|---|---|
| Runtime settings and heartbeat age | Recorded identity, revision and configuration; historical evidence, not proof that a process is still running |
| Paper commerce states | Local payer/worker records; no inferred airdrop credit |
| Local session states | Submitted/open/closed labels; a label alone is not finalized evidence |
| Reserved escrow and fees | Sum of historical reservations in the request journal |
| Outstanding liability | Funds still conservatively at risk, including unresolved failures |
| Finalized spend and fees | Amounts attributed by the trusted local reconciliation path, grouped by provider/network/account |
| Official allocation / claimable | Unknown unless independently established; never derived from any of the above |

Base amounts are decimal strings in units of 10^-18 FLOP. No SQLite floating-point sums are used.
Different providers, networks, accounts, assets and denominations are never merged. Missing tables are unavailable/unknown, not zero.
Legacy session records retain escrow as a liability floor; their historical fees remain unknown.
A zero in an existing journal describes that journal only; it is not an eligibility or network balance claim.

An optional `--allocation-evidence supplied.json` attaches a claimed source with fields `source_url`,
`network`, `account`, `snapshot`, `allocation_base`. The report fingerprints the file and always labels
it `UNVERIFIED_SOURCE_PROVIDED`, even if the file says `verified: true`. Claimed allocation stays
separate from credited allocation and claimable tokens, which remain `null`. No arbitrary imported file
can bless its own authenticity. Official source authentication, snapshot/account matching, finalized
settlements and any dispute-window/claim rules still need independent verification.
