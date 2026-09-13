"""Pairings: partner ranking over board frames and payee choice."""
import json

from agentscout import partners

P = ["did:key:z6Mk" + f"payer{i:02d}".ljust(44, "x") for i in range(30)]
W1, W2, W3, ME = ("did:key:z6Mk" + n.ljust(44, "w") for n in ("worker1", "worker2", "worker3", "meself"))


def cycle(contract, payer, worker):
    return [(payer, json.dumps({"type": "lock", "from": payer, "contract": contract, "rail": "paper"})),
            (worker, json.dumps({"type": "reveal", "from": worker, "contract": contract, "secret": "0x00"})),
            (payer, json.dumps({"type": "receipt", "from": payer, "contract": contract, "outcome": "claimed"}))]


def test_rank_keeps_diverse_workers_and_drops_self_dealing_farms_and_small_keys():
    frames = []
    for i in range(25):                                   # W1: 25 distinct payers, never a payer itself
        frames += cycle(f"0x{'a' * 60}{i:04x}", P[i], W1)
    for i in range(25):                                   # W2: 25 payers, but it pays 20 of the same keys back
        frames += cycle(f"0x{'b' * 60}{i:04x}", P[i], W2)
    for i in range(20):
        frames += cycle(f"0x{'c' * 60}{i:04x}", W2, P[i])
    for i in range(5):                                    # W3: too few payers
        frames += cycle(f"0x{'d' * 60}{i:04x}", P[i], W3)
    for i in range(25):                                   # our own key never lists itself
        frames += cycle(f"0x{'e' * 60}{i:04x}", P[i], ME)
    frames += cycle("0x" + "f" * 64, W1, W1)              # payer == payee never counts
    ranked = partners.rank(frames, ME)
    assert [p.did for p in ranked] == [W1]
    assert ranked[0].distinct_payers == 25 and ranked[0].cycles == 25


def test_choose_payee_prefers_fresh_partner_then_partner_then_earliest():
    found = {W1: 503, W2: 501, W3: 502}
    assert partners.choose_payee(found, set(), set()) == W2                  # no partners: earliest accept
    assert partners.choose_payee(found, {W1, W3}, set()) == W3               # earliest partner
    assert partners.choose_payee(found, {W1, W3}, {W3}) == W1                # partner not yet dealt with today
    assert partners.choose_payee(found, {W1, W3}, {W1, W3}) == W3            # all used: earliest partner again
