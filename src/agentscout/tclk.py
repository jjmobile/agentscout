"""tclk/1 (github.com/flop-labs/tclk) — the payer-side subset AgentScout needs, stdlib only.

Frames are single-line room messages: `tclk1 ` + canonical JSON (sorted keys, compact,
ASCII-escaped). Ids are domain-tagged sha256 hashes over those exact wire bytes; the id and
contract hashes here are byte-identical to the reference TypeScript library (verified against
golden vectors from @flop-labs/tclk in tests). Hash locks only — point locks / adaptor
signatures are deliberately out of scope. The paper rail settles NOTHING (its own docstring:
"a settlement rail that settles nothing"); a paper deal is a rehearsal of the choreography,
never a payment.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from typing import Dict, List, Optional, Tuple

PREFIX = "tclk1 "
DOMAIN = "FLOP::tclk::v1"
MAX_FRAME_CHARS = 4096
OFFERS_ROOM = "tclk-offers"

_HEX64 = re.compile(r"^0x[0-9a-f]{64}$")
_HEX66 = re.compile(r"^0x[0-9a-f]{66}$")
_DID = re.compile(r"^did:key:z6Mk[1-9A-HJ-NP-Za-km-z]{40,50}$")
_NONCE = re.compile(r"^[0-9a-f]{16}$")
_AMOUNT = re.compile(r"^[0-9]+$")

_KEYS = {
    "offer": (["type", "from", "role", "amount", "asset", "lock", "rails", "claimByMs",
               "refundAfterMs", "expiresMs", "paymentKey", "job", "nonce", "id"],
              ["from", "role", "amount", "asset", "lock", "rails", "claimByMs",
               "refundAfterMs", "expiresMs", "nonce", "id"]),
    "accept": (["type", "from", "ref", "statement", "paymentKey", "nonce", "contract"],
               ["from", "ref", "statement", "nonce", "contract"]),
    "lock": (["type", "from", "contract", "rail", "ref", "presig"],
             ["from", "contract", "rail", "ref"]),
    "reveal": (["type", "from", "contract", "secret"], ["from", "contract", "secret"]),
    "refund": (["type", "from", "contract"], ["from", "contract"]),
    "cancel": (["type", "from", "contract"], ["from", "contract"]),
    "receipt": (["type", "from", "contract", "outcome", "rail", "ref"],
                ["from", "contract", "outcome"]),
}


class TclkError(ValueError):
    pass


def _fail(why: str) -> None:
    raise TclkError(f"tclk: {why}")


def canonical(value) -> str:
    """Sorted keys, compact separators, every non-ASCII char \\uXXXX-escaped — the exact
    bytes the reference encodeFrame puts on the wire (and the bytes the ids hash)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _domain_hash(tag: str, payload: str) -> str:
    return "0x" + hashlib.sha256(f"{DOMAIN}|{tag}|{payload}".encode("ascii")).hexdigest()


def offer_id(fields: Dict) -> str:
    """sha256 over the domain-tagged canonical offer fields, `id` excluded."""
    return _domain_hash("offer", canonical({k: v for k, v in fields.items() if k != "id"}))


def contract_id(offer: Dict, accept_core: Dict) -> str:
    """sha256 over the canonical {offer, accept} pair (full offer, id included)."""
    return _domain_hash("contract", canonical({"offer": offer, "accept": accept_core}))


def generate_hash_lock() -> Tuple[str, str]:
    """(preimage, statement): payee-side mint; here for tests and completeness."""
    preimage = "0x" + secrets.token_hex(32)
    return preimage, "0x" + hashlib.sha256(bytes.fromhex(preimage[2:])).hexdigest()


def secret_opens(statement: str, secret: str) -> bool:
    if not _HEX64.fullmatch(secret or "") or not _HEX64.fullmatch(statement or ""):
        return False
    return "0x" + hashlib.sha256(bytes.fromhex(secret[2:])).hexdigest() == statement


def make_offer(from_did: str, amount: str, expires_ms: int, claim_by_ms: int,
               refund_after_ms: int, job_id: Optional[str] = None,
               asset: str = "FLOP", rails: Optional[List[str]] = None,
               nonce: Optional[str] = None) -> Dict:
    """A payer-side hash-lock offer. `nonce` is random by default (the venue's duplicate
    filter 422s repeated texts; no two offers may serialize identically)."""
    fields: Dict = {
        "type": "offer", "from": from_did, "role": "payer", "lock": "hash",
        "amount": amount, "asset": asset, "rails": rails or ["paper"],
        "claimByMs": claim_by_ms, "refundAfterMs": refund_after_ms, "expiresMs": expires_ms,
        "nonce": nonce or secrets.token_hex(8),
    }
    if job_id:
        fields["job"] = {"id": job_id, "proto": "a2a"}
    frame = dict(fields)
    frame["id"] = offer_id(fields)
    return validate_frame(frame)


def make_frame(type_: str, from_did: str, contract: str, **extra) -> Dict:
    return validate_frame({"type": type_, "from": from_did, "contract": contract, **extra})


def validate_frame(value) -> Dict:
    """Fail-closed: unknown type, unknown key, missing field or malformed value rejects."""
    if not isinstance(value, dict):
        _fail("frame is not an object")
    t = value.get("type")
    if t not in _KEYS:
        _fail(f"unknown frame type {t!r}")
    allowed, required = _KEYS[t]
    for k in value:
        if k not in allowed:
            _fail(f"{t} frame has unknown key {k!r}")
    for k in required:
        if k not in value:
            _fail(f"{t} frame is missing {k!r}")
    if not _DID.fullmatch(str(value["from"])):
        _fail("from is not a did:key")
    if t == "offer":
        if value["role"] not in ("payer", "payee"):
            _fail("role must be payer|payee")
        if value["lock"] not in ("hash", "point"):
            _fail("lock must be hash|point")
        if not _AMOUNT.fullmatch(str(value["amount"])):
            _fail("amount must be a decimal integer string")
        if not isinstance(value["rails"], list) or not value["rails"] or \
                not all(isinstance(r, str) and r for r in value["rails"]):
            _fail("rails must be a non-empty list of ids")
        for k in ("claimByMs", "refundAfterMs", "expiresMs"):
            if not isinstance(value[k], int) or value[k] <= 0:
                _fail(f"{k} must be a positive integer (unix ms)")
        if not value["claimByMs"] < value["refundAfterMs"]:
            _fail("claimByMs must be strictly before refundAfterMs")
        if not _NONCE.fullmatch(str(value["nonce"])):
            _fail("nonce must be 16 hex chars")
        if value["id"] != offer_id(value):
            _fail("offer id does not hash its own fields")
        if "job" in value and (not isinstance(value["job"], dict) or "id" not in value["job"]):
            _fail("job must be an object with an id")
    elif t == "accept":
        if not _HEX64.fullmatch(str(value["ref"])):
            _fail("ref must be a 32-byte hex id")
        if not (_HEX64.fullmatch(str(value["statement"])) or _HEX66.fullmatch(str(value["statement"]))):
            _fail("statement must be 32 or 33 bytes of hex")
        if not _NONCE.fullmatch(str(value["nonce"])):
            _fail("nonce must be 16 hex chars")
        if not _HEX64.fullmatch(str(value["contract"])):
            _fail("contract must be a 32-byte hex id")
    elif t in ("lock", "reveal", "refund", "cancel", "receipt"):
        if not _HEX64.fullmatch(str(value["contract"])):
            _fail("contract must be a 32-byte hex id")
        if t == "lock" and (not isinstance(value["rail"], str) or not value["rail"] or not value["ref"]):
            _fail("lock needs rail and ref")
        if t == "reveal" and not _HEX64.fullmatch(str(value["secret"])):
            _fail("secret must be 32 bytes of hex")
        if t == "receipt" and value["outcome"] not in ("claimed", "refunded", "cancelled"):
            _fail("receipt outcome must be claimed|refunded|cancelled")
    return value


def encode_frame(frame: Dict) -> str:
    line = PREFIX + canonical(validate_frame(frame))
    if len(line) > MAX_FRAME_CHARS:
        _fail(f"frame exceeds {MAX_FRAME_CHARS} chars")
    if not all(0x20 <= ord(c) <= 0x7E for c in line):
        _fail("frame line contains non-printable-ASCII characters")
    return line


def is_tclk_line(text: str) -> bool:
    return isinstance(text, str) and text.startswith(PREFIX)


def decode_frame(text: str) -> Dict:
    if not is_tclk_line(text):
        _fail("not a tclk/1 line")
    try:
        parsed = json.loads(text[len(PREFIX):])
    except ValueError:
        _fail("frame is not valid JSON")
    return validate_frame(parsed)


# ---- state machine (§4): pure, fail-closed, never touches money -------------------------------

TERMINAL = ("claimed", "refunded", "cancelled")


def open_contract(offer: Dict) -> Dict:
    validate_frame(offer)
    return {"offer": offer, "accept": None, "contract": None, "state": "proposed"}


def parties(state: Dict) -> Tuple[str, Optional[str]]:
    """(payer, payee) — our subset always has the offerer as payer."""
    payer = state["offer"]["from"]
    payee = state["accept"]["from"] if state["accept"] else None
    return payer, payee


def apply_frame(state: Dict, frame: Dict, now_ms: int) -> Tuple[Dict, bool, str]:
    """Returns (next_state, ok, reason); an invalid frame leaves the state untouched."""
    try:
        validate_frame(frame)
    except TclkError as exc:
        return state, False, str(exc)
    s, offer = state["state"], state["offer"]
    payer, payee = parties(state)
    t = frame["type"]
    if t == "accept":
        if s != "proposed":
            return state, False, "accept out of turn"
        if frame["ref"] != offer["id"]:
            return state, False, "accept references another offer"
        if frame["from"] == payer:
            return state, False, "accept.from must differ from offer.from"
        if now_ms >= offer["expiresMs"]:
            return state, False, "offer expired"
        if offer["lock"] == "hash" and not _HEX64.fullmatch(frame["statement"]):
            return state, False, "statement does not fit a hash lock"
        core = {k: frame[k] for k in ("from", "ref", "statement", "paymentKey", "nonce") if k in frame}
        if frame["contract"] != contract_id(offer, core):
            return state, False, "contract id mismatch"
        nxt = dict(state, accept=frame, contract=frame["contract"], state="accepted")
        return nxt, True, "accepted"
    if frame.get("contract") != state.get("contract") and t != "accept":
        return state, False, "frame names another contract"
    if t == "lock":
        if s != "accepted":
            return state, False, "lock out of turn"
        if frame["from"] != payer:
            return state, False, "only the payer locks"
        if frame["rail"] not in offer["rails"]:
            return state, False, "rail not offered"
        return dict(state, state="locked", lock_frame=frame), True, "locked"
    if t == "reveal":
        if s != "locked":
            return state, False, "reveal out of turn"
        if frame["from"] != payee:
            return state, False, "only the payee reveals"
        if now_ms >= offer["refundAfterMs"]:
            return state, False, "reveal after refundAfterMs"
        if not secret_opens(state["accept"]["statement"], frame["secret"]):
            return state, False, "secret does not open the statement"
        return dict(state, state="claimed", secret=frame["secret"]), True, "claimed"
    if t == "refund":
        if s != "locked":
            return state, False, "refund out of turn"
        if frame["from"] != payer:
            return state, False, "only the payer refunds"
        if now_ms < offer["refundAfterMs"]:
            return state, False, "refund before refundAfterMs"
        return dict(state, state="refunded"), True, "refunded"
    if t == "cancel":
        if s not in ("proposed", "accepted"):
            return state, False, "cancel out of turn"
        if frame["from"] not in (payer, payee):
            return state, False, "cancel from a non-party"
        return dict(state, state="cancelled"), True, "cancelled"
    if t == "receipt":
        return state, False, "receipt makes no transition"
    return state, False, f"no transition for {t} in {s}"


# ---- paper rail record (settles nothing; world-writable rehearsal notes) ----------------------

PAPER_PREFIX = "tclkpaper1"


def paper_note(contract: str) -> Tuple[str, str]:
    if not _HEX64.fullmatch(contract):
        _fail(f"malformed contract id: {contract}")
    return f"tclk-paper-{contract[2:4]}", contract[4:18]


def encode_paper_record(status: str, lock: str, statement: str, refund_after_ms: int,
                        secret: Optional[str] = None) -> str:
    head = f"{PAPER_PREFIX} {status} {lock} {statement} {refund_after_ms}"
    return head if secret is None else f"{head} {secret}"


def decode_paper_record(value: str) -> Optional[Dict]:
    parts = (value or "").split(" ")
    if len(parts) < 5 or len(parts) > 6:
        return None
    prefix, status, lock, statement, refund_after = parts[:5]
    secret = parts[5] if len(parts) == 6 else None
    if prefix != PAPER_PREFIX or status not in ("locked", "claimed", "refunded"):
        return None
    if lock not in ("hash", "point"):
        return None
    if not re.fullmatch(r"0x[0-9a-f]{64,66}", statement):
        return None
    try:
        refund_ms = int(refund_after)
    except ValueError:
        return None
    if refund_ms <= 0:
        return None
    if secret is not None and not _HEX64.fullmatch(secret):
        return None
    if (status == "claimed") != (secret is not None):
        return None
    out = {"status": status, "lock": lock, "statement": statement, "refundAfterMs": refund_ms}
    if secret is not None:
        out["secret"] = secret
    return out


def deal_room(contract: str) -> str:
    """Everything from lock onward moves to the derived, signed-only deal room."""
    if not _HEX64.fullmatch(contract):
        _fail(f"malformed contract id: {contract}")
    return f"mb-p-tclk-{contract[2:18]}"
