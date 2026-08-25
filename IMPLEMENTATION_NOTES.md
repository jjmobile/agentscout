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
10. **Stdlib only** (urllib, sqlite3). The Anthropic SDK is added in Milestone C.
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
