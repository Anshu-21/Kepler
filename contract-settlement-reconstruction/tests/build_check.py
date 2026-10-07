"""Runs at verifier image build time; the build fails if any check fails.

* the sealed contracts regenerate byte for byte (digest in cases.py);
* every contract respects the bounds stated in SETTLEMENT_SPEC.md;
* no accrual, SOFR or overdue rounding in any sealed contract lies within
  1e-7 of a half unit, so a Decimal implementation and an exact one cannot
  disagree on a cent (withholding and reserve targets are exact products);
* the contracts reach the cases a formula shortcut gets wrong: gross-ups
  that differ from ceil(net / (1 - rate)) and from its rounding, reserve
  top-ups that change the sweep they are sized on, bookings whose knowledge
  flips under a local-date, fixed-offset or exclusive cutoff, ICMA accrual
  periods that span two regular periods, and lockouts inside a period that
  is split by a segment.
"""
import sys
from datetime import timedelta
from fractions import Fraction as F
from datetime import datetime, timezone
from model import Audit, add_months, cutoff, instant, is_business, parse, schedule
import cases

audit = Audit()
pairs = cases.cases(audit)
digest = cases.digest(pairs)
if cases.DIGEST is not None and digest != cases.DIGEST:
    sys.exit(f"sealed contracts drifted: {digest}")
if audit.closest <= 1e-7:
    sys.exit(f"a rounding lies {float(audit.closest)} from a half")
for contract, _ in pairs:
    cid = contract["contract_id"]
    start = parse(contract["opening_date"])
    tr = contract["tranches"]
    assert 2 <= len(tr) <= 20, cid
    assert 6 <= contract["term_months"] <= 600 and 1 <= contract["anchor_day"] <= 31, cid
    assert 1 <= contract["notice_days"] <= 5, cid
    assert len(contract["bookings"]) <= 12000 and len(contract["prepayments"]) <= 120, cid
    assert len(contract["collateral"]) == contract["term_months"], cid
    assert len(contract["collections"]) == contract["term_months"], cid
    assert is_business(start, set(contract["payment_holidays"])), cid
    regular = [start] + [add_months(start, k, contract["anchor_day"]) for k in range(1, contract["term_months"] + 1)]

    def placed(day):
        d = parse(day)
        assert regular[0] < d < regular[-1], cid
        assert min(abs((d - r).days) for r in regular) >= 5, cid

    seen, fixing_dates = {}, {}
    for e in contract["bookings"]:
        revs = seen.setdefault(e["logical_id"], set())
        assert e["revision"] not in revs, cid
        revs.add(e["revision"])
        if e["action"] == "SET" and e["kind"] == "RATE_CHANGE":
            placed(e["effective_date"])
        if e["action"] == "SET" and e["kind"] == "FIXING_CORRECTION":
            assert fixing_dates.setdefault(e["fixing_date"], e["logical_id"]) == e["logical_id"], cid
    for p in contract["prepayments"]:
        placed(p["effective_date"])
    for t in tr:
        if t["rate"]["type"] == "FLOATING":
            assert 2 <= t["rate"]["lookback_days"] <= 5, cid
flip_local = flip_fixed = flip_edge = icma_span = locked_split = 0
for contract, _ in pairs:
    rows = schedule(contract)
    dets = sorted({r["determination"] for r in rows})
    for e in contract["bookings"]:
        moment = instant(e["recorded_at"])
        local = parse(e["recorded_at"][:10])
        for d in dets:
            truth = moment <= cutoff(d)
            flip_local += truth != (local <= d)
            flip_fixed += truth != (moment <= datetime(d.year, d.month, d.day, 22, tzinfo=timezone.utc)) or \
                truth != (moment <= datetime(d.year, d.month, d.day, 21, tzinfo=timezone.utc))
            flip_edge += moment == cutoff(d)
    start = parse(contract["opening_date"])
    regular = {add_months(start, k, contract["anchor_day"]) for k in range(1, contract["term_months"] + 2)}
    dcs = {t["day_count"] for t in contract["tranches"]}
    if "ACT/ACT ICMA" in dcs:
        icma_span += sum(1 for r in rows if any(r["start"] < x < r["end"] for x in regular))
    if any("lockout_days" in t["rate"] for t in contract["tranches"]):
        locked_split += len(contract["prepayments"])
from collections import Counter
most = max(max(Counter(e["logical_id"] for e in c["bookings"]).values()) for c, _ in pairs)
assert most >= 400, most  # a standing booking revised at almost every determination date
assert flip_local >= 50 and flip_fixed >= 50 and flip_edge >= 5 and icma_span >= 20 and locked_split >= 20, \
    (flip_local, flip_fixed, flip_edge, icma_span, locked_split)
off_ceil = off_round = circular = 0
for contract, want in pairs:
    rates = {t["tranche_id"]: F(t.get("withholding_rate", "0")) for t in contract["tranches"]}
    for p in want["periods"]:
        for tid, r in p["tranches"].items():
            w, due = rates[tid], F(r["interest_due"])
            if w and due > 0 and F(r["deferred_interest"]) == 0:
                gross = (F(r["interest_paid"]) + F(r["withholding"])) * 100
                exact = due * 100 / (1 - w)
                off_ceil += gross != -((-exact.numerator) // exact.denominator)
                off_round += gross != (exact + F(1, 2)).__floor__()
        if F(p["reserve_topup"]) > 0 and any(F(r["swept"]) > 0 for r in p["tranches"].values()):
            circular += 1
assert off_ceil >= 100 and off_round >= 20 and circular >= 5, (off_ceil, off_round, circular)
import json, os
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "sealed.json"), "w") as handle:
    json.dump(pairs, handle, separators=(",", ":"))
print("sealed contracts ok", digest, f"closest rounding {float(audit.closest):.3g}",
      f"gross-ups off ceil {off_ceil}, off round {off_round}, top-ups against a sweep {circular}",
      f"cutoff flips local {flip_local} fixed {flip_fixed} exact {flip_edge}, ICMA spans {icma_span}")
