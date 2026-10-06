# IMPLEMENTATION_NOTES.md — Milestone A

Built 2026-08-25 against technocore.chat agent.json `version 0.7.0`. Deviations and verified facts:

1. **Paging.** `GET /r/<room>?since=N&limit=200` returns the *newest* 200 messages with seq > N, not the
   oldest — there is no backward paging. First start therefore sees at most the newest 200 per room
   (`PROCESS_BACKLOG_ON_FIRST_START=true`) or just the cursor (`false`). Gaps are recorded.
2. **`since=0` is a server 500.** The client omits `since` when the cursor is 0.
3. **`first_seq` is `null`** on an empty slice; handled.
4. **Signed detection.** The read API never returns the signature. A message is treated as signed iff
   `from` starts with `did:key:z6Mk` and `nonce` is present — the server verified it before storing.
5. **`/kv/did` is at the 5120-per-namespace cap** (5051 fingerprint-shaped keys + ~70 other keys on
   2026-08-25). New agents may be unable to publish a DID note until idle notes expire. This is why
   notes carry zero score weight and why agents seen in messages are fetched first even when their
   fingerprint is not in the listing.
6. **Note reads are text/plain** with an `!! UNTRUSTED CONTENT` banner line; the client strips banner and
   `#` lines and joins the rest.
7. **Rate limits are discovered** from agent.json at startup; the read budget is capped at 20 % of the
   published reads-per-minute (600 → 120). A version change is logged as protocol drift.
8. **No long-polling in Milestone A.** With ~10–15 rooms round-robin every 15 s, plain `since` polling stays
   under the budget and is simpler; `wait=` is supported by the client for later.
9. **Milestone flags are enforced at startup**: `DRY_RUN` must be true and all LLM/reply flags false. There is
   no code path that writes to Technocore or calls any LLM in this build.
10. **Stdlib + `cryptography`** (Ed25519 for the identity; urllib, sqlite3 for everything else). The Anthropic SDK is added in Milestone C.
11. Local dev machine runs Python 3.9; code is 3.9-compatible. The container uses `python:3.12-slim`.

## Findings from the first live dry-run cycles (2026-08-25, read-only)

12. **`/r/events` backlog is large** (~15 new public rooms per hour). Watching every announced room for 48 h
    blew the read budget (one cycle > 10 min). Now: all announcements are *recorded* in `rooms_seen`, but only
    the newest `SCOUT_MAX_EVENT_ROOMS` (30) are *watched*, for `SCOUT_NEW_ROOM_WATCH_HOURS` (6), and polled on
    a slower cadence (`SCOUT_EVENT_ROOM_POLL_SECONDS`, 120 s). Config rooms are polled every cycle.
13. **Transient HTTP 500s are common** on every endpoint (agent.json, rooms, notes). The client retries with
    backoff (max 4 attempts); a cycle typically sees 2–5 of them. Not an error in our code.
14. **Owner scan is incremental** (`SCOUT_OWNERS_PER_CYCLE`), rooms we have messages from first; the
    `room-owners` namespace lists ~900 rooms.
15. **Ciphertext bots**: several DIDs post `enc:v1:…` ciphertext 20–30 times across 5–8 rooms. Their texts differ,
    so the duplicate penalty misses them; the `opaque` penalty (SCORING.md) catches them.
16. **Cheap breadth**: one DID owned 19 empty `d-` rooms and posted once in each. Owned rooms now cap at one for
    scoring and breadth counts only rooms with ≥ 2 signed messages.
17. **Day-one "new agents"**: backfilled history is never "new". `observation_started_at` is recorded at first
    start and the digest counts only agents first seen after it.
18. A first cycle on an empty DB takes ~70 s (≈60 reads incl. retries); steady-state cycles are a few seconds.
19. **Identity pulled forward from Milestone B** (operator request): Ed25519 seed at `/data/identity.key`
    (0600, created once), DID encoded as `z` + base58btc(0xed01 ‖ pubkey); verified by round-tripping the W3C
    did:key spec example `…2doK` (the same key Technocore's manual uses as its rendering example). Signing is
    implemented and tested but has no caller in this milestone.

## Milestone B (2026-08-25)

20. **Write lane**: `POST /r/<room>` `{did,sig,nonce,text}` (nonce as a decimal string), signature over
    `<room>|<nonce>|<swept text>`. 429 is retried after the stated wait (the write did not happen); **5xx and
    timeouts are never blindly retried** — the room is read back (`limit=50`) for our DID + the line's marker
    (e.g. `AGENTSCOUT DIGEST 2026-08-25`); only if absent is a fresh nonce minted and the line re-signed.
21. **400 on post** (stale nonce / bad sig): the nonce floor is re-synced from the newest 200 ring messages of our
    DID and the post retried next cycle (bounded to 5 attempts, then `FAILED_FINAL` + alert).
22. **403** means the room is not ours any more: publishing disables itself until the next startup verification.
23. **kv notes** are written with `POST /kv/<ns>/<key>` and `if`/`if_absent`; a 409 body (banner-stripped) is the
    live value: if it is not what we last wrote, `NOTE_TAMPERED` is logged and our value is rewritten (3 tries).
24. **DID note**: `/kv/did/<fp>` refreshed every 72 h; the namespace was at its 5120 cap on 2026-08-25, so the write
    may fail until idle notes expire — logged, retried next window, not fatal.
25. **Telegram**: outbound `sendMessage` only; token from a Docker secret file; WARNING+ log records are forwarded
    through a logging handler with an hourly cap; the token never appears in logs (tested).
26. Technocore client retry lines were downgraded to INFO so transient 500s do not flood Telegram.
27. **Protocol drift found on go-live (2026-08-25 02:18Z)**: the documented unsigned ownership claim
    `GET /kv/room-owners/d-<room>/set/<did>?if_absent=1` now returns 403 — the server requires the signed lane
    `/kv/room-owners/<room>/set-signed/<did>/<sig>/<nonce>/<did>` (signature over `room-owners|<room>|<nonce>|<did>`,
    nonce greater than the server-written `/kv/room-nonce/<room>`, which is absent (404 → 0) for a fresh room).
    `scripts/claim_room.py` now does exactly that. Ownership verification at startup caught the failed claim and
    kept publishing disabled.

## Milestone C (2026-08-25)

28. SDK: `anthropic` 0.125 (`client.messages.parse(model, max_tokens, system=[…cache_control…], messages, output_format=<Pydantic>,
    output_config={"effort": …})`). No `thinking` parameter is sent (adaptive by default on Opus 5); no sampling params.
29. Refusals: `stop_reason == "refusal"` or a missing `parsed_output` → `SKIPPED_REFUSAL`, recorded so it is not retried each
    cycle. Server-side refusal fallbacks (`fallbacks` beta) are not used — a skipped summary is harmless.
30. Cost: `usage_ledger` persists tokens per call; `data/pricing.json` holds $/MTok (editable); the guard compares today's
    estimated spend against `MAX_ESTIMATED_DAILY_API_COST_USD` before every call. Console-side spend limits are a second layer.
31. Blend: 10 % of the model `signal`, deterministic components scaled to 90 % (SCORING.md). Renders identically with no summaries.
32. Summaries run inside the main loop (≤3 per cycle, each a few seconds); the public bot and publisher read them from SQLite.
33. **Dependencies pinned** (2026-08-25): cryptography 50.0.0, pydantic 2.13.4, anthropic 1.0.0 in the image (Python 3.12) and
    0.125.0 for local Python 3.9 dev — both expose the same `messages.parse(output_format=…)` surface and both passed the
    startup smoke check. `main` is protected (no force-push/deletion, linear history); Dependabot vulnerability alerts on.
    Bump pins deliberately: edit pyproject, rebuild, watch the smoke check, commit.

## W5 — FLOP compute-channel client (2026-10-06, built the day flop.finance published the testnet/airdrop draft)

34. **Why now.** flop.finance/airdrop (2026-10-05): the agent pool (1.2bn) is shared pro rata to *compute purchased in
    settled sessions* during the ~90-day testnet; faucet grants and holding earn nothing. The Yellow Paper's only agent
    metric is the same (E.38/E.40 placeholder). So the one thing to have ready on testnet day one is a client that can
    open a channel, verify every turn, co-sign, and hand over a receipt — the modules below. Nothing here runs today:
    without `FLOP_RPC_URL` the provider reports itself unavailable and the summarizer stays off (honest stub, as before).
35. **Modules.** `flopchannel.py` — Appendix F wire format, stdlib only: channel id (F.1), V0–V3 leaves, Merkle
    accumulator, per-turn ack and agent receipt v1 preimages, FCC4 transcript encode/decode, SCALE `VerifiedTurn`,
    fail-closed checks; every byte is pinned by `tests/data/flop-wire-format-v1.json` (Labs' public-canonical corpus,
    incl. the 15 negative cases). `flopkeys.py` — the sr25519 *session* key (`agent_key`; F.0 Substrate context),
    0600 file, rotates after 9 days (§6.2 ≤10-day cap), old file kept as `.prev`; `py-sr25519-bindings` imported lazily.
    `flopchain.py` — `open_channel` as our ed25519 account via substrate-interface (duck-typed `iface`, tested with a
    double); the channel id is derived locally and cross-checked against the `ChannelOpened` event (an F.1 mismatch is
    a stop). `flopsession.py` — one session end to end with guards (daily cap, escrow + 11 FLOP headroom), strictly
    consecutive turns, `h_in`/`h_out` must hash our prompt / the delivered output, cooperative close pays the full
    escrow (R12.1a). `inference.FlopProvider` renders the summarizer's call into one prompt, runs a session, validates
    the JSON against `output_format` — the summarizer and its guards are untouched.
36. **Verified.** The corpus signatures (receipt, ack, V3 leaf) verify under `sr25519.verify` with the Substrate
    context on python:3.12 linux/arm64 — curve and byte layouts confirmed, not assumed. `h_ids` was cross-checked
    against the corpus too. Locally (macOS, Python 3.9) the sr25519 test skips: no wheel, no Rust toolchain.
37. **Provisional, by design.** The miner-facing transport (`HttpMinerTransport`: hello / turns / acks / close as JSON)
    is our own shape because Labs has not published the SOFT-tier stream protocol (E.33); it is one class to replace.
    Pallet/call names and `sla`/`precision`/`settlement_class` encodings follow App. G.1 and are configurable
    (`FLOP_PALLET`) because the runtime metadata, not the paper, is final. `decode_policy_hash` comes from the miner's
    hello and is pinned for the channel (R12.1j); leaves with another policy or a V0/V1 tag are rejected (F.3 cutoff).
38. **Dependencies.** `pyproject` optional group `flop = py-sr25519-bindings==0.2.4, substrate-interface==1.8.1` (pip's
    resolver report on python:3.12 linux/arm64, 2026-10-06: all 32 packages ship wheels — no compiler needed; the first
    install is slow only because the resolver backtracks through substrate-interface's loose pins). The
    image still installs `.` (no new code in the live container during the hold). Go-live = set `FLOP_RPC_URL`,
    `FLOP_MINER_URL`, `SCOUT_INFERENCE_PROVIDER=flop`, change the Dockerfile's `pip install .` to `.[flop]`, rebuild,
    watch the smoke check; the first `flop_sessions` row in state CLOSED/SETTLED is the W5 definition of done.
39. **Chain facts the client relies on** (Yellow Paper v0.5.0 + corpus): u128 base units are 10⁻¹⁸ FLOP; agent wallet
    caps 100 FLOP/tx and 500 FLOP/day (defaults: 1 FLOP escrow/session, ≤100 sessions/day); identity stake 10 FLOP;
    session key ≤ 864,000 blocks; `settle` is the miner's extrinsic carrying our receipt; disputes are fraud-only.
