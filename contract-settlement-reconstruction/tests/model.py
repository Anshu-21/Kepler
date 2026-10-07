"""Independent settlement model and sealed-contract generator.

Only the verifier image contains this file.  `settle` is written straight from
SETTLEMENT_SPEC.md in exact rational arithmetic (fractions.Fraction) and shares
no code with the candidate engine.  `make` builds one contract from a seed and
a feature mix; `settle` produces the statement the specification implies.
"""
from fractions import Fraction as F
from datetime import date, datetime, timedelta, timezone
import calendar, functools, random

DAY_COUNTS = ("ACT/360", "ACT/365F", "ACT/ACT ISDA", "ACT/ACT ICMA", "30/360", "30E/360", "30E/360 ISDA")


def parse(x):
    return date.fromisoformat(x)


def add_months(start, n, anchor):
    serial = start.year * 12 + start.month - 1 + n
    y, m = divmod(serial, 12)
    return date(y, m + 1, min(anchor, calendar.monthrange(y, m + 1)[1]))


def is_business(day, holidays):
    return day.weekday() < 5 and day.isoformat() not in holidays


def modified_following(day, holidays):
    f = day
    while not is_business(f, holidays):
        f += timedelta(days=1)
    if f.month == day.month:
        return f
    b = day
    while not is_business(b, holidays):
        b -= timedelta(days=1)
    return b


def back_business(day, n, holidays):
    while n:
        day -= timedelta(days=1)
        if is_business(day, holidays):
            n -= 1
    return day


def last_of_month(d):
    return d.day == calendar.monthrange(d.year, d.month)[1]


def fraction(a, b, conv, termination, regular=None):
    if conv == "ACT/360":
        return F((b - a).days, 360)
    if conv == "ACT/365F":
        return F((b - a).days, 365)
    if conv == "ACT/ACT ISDA":
        total = F(0)
        cur = a
        while cur < b:
            nxt = min(b, date(cur.year + 1, 1, 1))
            total += F((nxt - cur).days, 366 if calendar.isleap(cur.year) else 365)
            cur = nxt
        return total
    if conv == "ACT/ACT ICMA":
        total = F(0)
        for lo, hi in zip(regular, regular[1:]):
            days = (min(b, hi) - max(a, lo)).days
            if days > 0:
                total += F(days, 12 * (hi - lo).days)
        return total
    d1, d2 = a.day, b.day
    if conv == "30/360":
        d1 = min(d1, 30)
        if d2 == 31 and d1 == 30:
            d2 = 30
    elif conv == "30E/360":
        d1, d2 = min(d1, 30), min(d2, 30)
    elif conv == "30E/360 ISDA":
        d1 = 30 if last_of_month(a) else min(d1, 30)
        if last_of_month(b) and not (b == termination and b.month == 2):
            d2 = 30
        else:
            d2 = min(d2, 30)
    else:
        raise ValueError(conv)
    return F(360 * (b.year - a.year) + 30 * (b.month - a.month) + d2 - d1, 360)


class Audit:
    """Tracks how close any rounding came to an exact half unit."""
    def __init__(self):
        self.closest = F(1)

    def half_up(self, x, unit):
        scaled = x / unit
        frac = scaled - (scaled.numerator // scaled.denominator)
        self.closest = min(self.closest, abs(frac - F(1, 2)))
        return F((scaled + F(1, 2)).__floor__()) * unit


CENT = F(1, 100)
RATE_UNIT = F(1, 10 ** 7)


def money(x):
    cents = int(x * 100)
    assert F(cents, 100) == x
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def nth_sunday(year, month, n):
    first = date(year, month, 1)
    return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))


def cutoff(day):
    """17:00 New York time on day, as an aware UTC instant."""
    summer = nth_sunday(day.year, 3, 2) <= day < nth_sunday(day.year, 11, 1)
    return datetime(day.year, day.month, day.day, 21 if summer else 22, tzinfo=timezone.utc)


def instant(stamp):
    return datetime.fromisoformat(stamp).astimezone(timezone.utc)


@functools.lru_cache(maxsize=None)
def regular_boundaries(opening, anchor, term):
    """Unadjusted boundaries 0 .. term + 1 (the last one only bounds a notional period)."""
    start = parse(opening)
    return tuple([start] + [add_months(start, n, anchor) for n in range(1, term + 2)])


def known_bookings(c, knowledge):
    best = {}
    limit = cutoff(parse(knowledge))
    for e in c["bookings"]:
        if instant(e["recorded_at"]) > limit:
            continue
        key = e["logical_id"]
        if key not in best or e["revision"] > best[key]["revision"]:
            best[key] = e
    return [e for e in best.values() if e["action"] == "SET"]


def schedule(c):
    start = parse(c["opening_date"]); anchor = int(c["anchor_day"]); ph = set(c["payment_holidays"])
    rows = []
    prev = start
    for k in range(1, int(c["term_months"]) + 1):
        regular = add_months(start, k, anchor)
        end = modified_following(regular, ph) if c["accrual_dates"] == "ADJUSTED" else regular
        pay = modified_following(regular, ph)
        det = back_business(pay, int(c["notice_days"]), ph)
        rows.append({"number": k, "start": prev, "end": end, "payment": pay, "determination": det})
        prev = end
    return rows


def compounded(c, fix, a, b, lookback, audit, end=None, lockout=0):
    rh = set(c["rate_holidays"])
    locked = back_business(end, lockout, rh) if lockout else None
    product = F(1); t = a
    while t < b:
        base = t
        while not is_business(base, rh):
            base -= timedelta(days=1)
        n = 0
        while t < b and (t == base or not is_business(t, rh)):
            n += 1; t += timedelta(days=1)
        if locked is not None and base >= locked:
            base = back_business(locked, 1, rh)
        obs = back_business(base, lookback, rh)
        product *= 1 + F(fix[obs.isoformat()]) / 100 * n / 360
    return audit.half_up((product - 1) * F(360, (b - a).days), RATE_UNIT)


class Knowledge:
    """Rates and fixings as known on one determination date."""
    def __init__(self, c, day):
        live = known_bookings(c, day)
        self.fixings = dict(c["fixings"])
        for e in live:
            if e["kind"] == "FIXING_CORRECTION":
                self.fixings[e["fixing_date"]] = e["fixing"]
        self.changes = sorted((e for e in live if e["kind"] == "RATE_CHANGE"),
                              key=lambda e: (e["effective_date"], e["logical_id"]))
        self.corrections = sorted((e["fixing_date"], e["fixing"]) for e in live if e["kind"] == "FIXING_CORRECTION")


def accrue_period(c, know, row, tranche, opening_balance, termination, audit, margin=F(0)):
    """Interest accrued by one tranche over one period, on its actual balances."""
    tid = tranche["tranche_id"]; terms = tranche["rate"]
    S, E = row["start"], row["end"]
    fixed = F(terms["annual_rate"]) if terms["type"] == "FIXED" else None
    spread = F(terms["spread"]) if terms["type"] == "FLOATING" else None
    for e in know.changes:
        if e["tranche_id"] == tid and parse(e["effective_date"]) <= S:
            if fixed is not None:
                fixed = F(e["annual_rate"])
            else:
                spread = F(e["spread"])
    moves = []
    for p in c["prepayments"]:
        if p["tranche_id"] == tid and S < parse(p["effective_date"]) < E:
            moves.append((p["effective_date"], 0, p["logical_id"], p))
    for e in know.changes:
        if e["tranche_id"] == tid and S < parse(e["effective_date"]) < E:
            moves.append((e["effective_date"], 0, e["logical_id"], e))
    moves.sort(key=lambda m: (m[0], m[2]))
    bal = opening_balance; cursor = S; total = F(0)
    regular = regular_boundaries(c["opening_date"], int(c["anchor_day"]), int(c["term_months"]))

    def seg(a, b):
        if a >= b:
            return F(0)
        if fixed is not None:
            rate = fixed + margin
        else:
            rate = max(compounded(c, know.fixings, a, b, int(terms["lookback_days"]), audit, E,
                                  int(terms.get("lockout_days", 0))), F(terms["floor"])) + spread + margin
        return audit.half_up(bal * rate * fraction(a, b, tranche["day_count"], termination, regular), CENT)

    for d in sorted({m[0] for m in moves}):
        day = parse(d)
        total += seg(cursor, day); cursor = day
        for m in moves:
            if m[0] != d:
                continue
            e = m[3]
            if "amount" in e:
                bal -= min(F(e["amount"]), bal)
            elif fixed is not None:
                fixed = F(e["annual_rate"])
            else:
                spread = F(e["spread"])
    total += seg(cursor, E)
    return total, bal


def pari_passu(cash, dues):
    """Split cash over dues (dict id -> amount); returns (paid dict, cash used)."""
    total = sum(dues.values(), F(0))
    if cash >= total:
        return dict(dues), total
    paid = {}; drop = {}
    for t, d in dues.items():
        exact = cash * d / total
        paid[t] = F(int(exact * 100), 100); drop[t] = exact - paid[t]
    left = int((cash - sum(paid.values(), F(0))) * 100)
    # desk practice: leftover cents go to the largest amounts first, not the largest remainders
    for t in sorted(dues, key=lambda t: (-dues[t], t))[:left]:
        paid[t] += CENT
    return paid, cash


class AccrualMemo:
    """Pure memoisation of accrue_period, keyed by every input it reads."""
    def __init__(self, c, rows, termination, audit):
        self.c, self.rows, self.termination, self.audit = c, rows, termination, audit
        self.memo = {}

    def __call__(self, know, j, tranche, opening, margin):
        row = self.rows[j]; tid = tranche["tranche_id"]
        changes = tuple((e["effective_date"], e["logical_id"], e.get("annual_rate"), e.get("spread"))
                        for e in know.changes if e["tranche_id"] == tid and parse(e["effective_date"]) < row["end"])
        fixes = ()
        if tranche["rate"]["type"] == "FLOATING":
            lo = (row["start"] - timedelta(days=40)).isoformat(); hi = row["end"].isoformat()
            fixes = tuple(x for x in know.corrections if lo <= x[0] <= hi)
        key = (tid, j, opening, margin, changes, fixes)
        if key not in self.memo:
            self.memo[key] = accrue_period(self.c, know, row, tranche, opening, self.termination, self.audit, margin)
        return self.memo[key]


def ceil_cent(x):
    return F(-((-x * 100).__floor__()), 100)


def round_cent(x):
    """Half up to the cent, for exact products (withholding, reserve target)."""
    return F((x * 100 + F(1, 2)).__floor__(), 100)


def smallest_cent(lo, hi, ok):
    """Smallest whole-cent x in [lo, hi] with ok(x), for a monotone ok; None if there is none."""
    a, b = int(lo * 100), int(hi * 100)
    if not ok(F(b, 100)):
        return None
    while a < b:
        m = (a + b) // 2
        if ok(F(m, 100)):
            b = m
        else:
            a = m + 1
    return F(a, 100)


def gross_up(net, rate):
    """Smallest whole-cent gross whose amount after withholding is at least net."""
    if rate == 0:
        return net
    return smallest_cent(net, net / (1 - rate) + 1, lambda g: g - round_cent(g * rate) >= net)


def settle(c, audit=None, cash_rule=None):
    """cash_rule(k, total_due) -> collections; used only by the generator."""
    audit = audit or Audit()
    rows = schedule(c); term = len(rows); termination = rows[-1]["end"]
    memo = AccrualMemo(c, rows, termination, audit)
    tranches = c["tranches"]; ids = [t["tranche_id"] for t in tranches]
    levels = sorted({t["seniority"] for t in tranches})
    by_level = {lv: [t["tranche_id"] for t in tranches if t["seniority"] == lv] for lv in levels}
    triggers = {int(k): F(v) for k, v in c["coverage_tests"].items()}
    dmargin = F(c["default_margin"])
    wrate = {t["tranche_id"]: F(t.get("withholding_rate", "0")) for t in tranches}
    rsv = c.get("reserve")
    reserve = F(rsv["opening_balance"]) if rsv else F(0)
    bal = {t["tranche_id"]: F(t["opening_balance"]) for t in tranches}
    starts = []; flags = []  # actual opening balance and default flag of each period
    recognised = {t: F(0) for t in ids}; credit = {t: F(0) for t in ids}
    deferred = {t: F(0) for t in ids}; arrears = {t: F(0) for t in ids}; fee_arrears = F(0)
    collections = []
    periods = []
    for row in rows:
        k = row["number"]
        know = Knowledge(c, row["determination"].isoformat())
        starts.append(dict(bal)); flags.append({t: deferred[t] > 0 for t in ids})
        restated = {t: F(0) for t in ids}; cur = {}; after_prepay = {}
        for j in range(k):
            for t in tranches:
                tid = t["tranche_id"]
                amount, end_bal = memo(know, j, t, starts[j][tid], dmargin if flags[j][tid] else F(0))
                if j < k - 1:
                    restated[tid] += amount
                else:
                    cur[tid] = amount; after_prepay[tid] = end_bal
        line = {}
        for tid in ids:
            true_up = restated[tid] - recognised[tid]
            recognised[tid] = restated[tid] + cur[tid]
            net = cur[tid] + true_up + credit[tid]
            current, credit[tid] = (net, F(0)) if net >= 0 else (F(0), net)
            overdue = F(0)
            if k > 1:
                days = (row["payment"] - rows[k - 2]["payment"]).days
                # desk practice: the tranche's own day count over the payment-date gap
                gap = fraction(rows[k - 2]["payment"], row["payment"], tranches[ids.index(tid)]["day_count"], termination,
                               regular_boundaries(c["opening_date"], int(c["anchor_day"]), int(c["term_months"])))
                overdue = audit.half_up(deferred[tid] * F(c["overdue_rate"]) * gap, CENT)
            b = after_prepay[tid]
            pdue = b if k == term else min(F(tranches[ids.index(tid)]["scheduled_principal"]) + arrears[tid], b)
            line[tid] = {"interest": cur[tid], "true_up": true_up, "overdue_interest": overdue,
                         "interest_due": current + deferred[tid] + overdue, "principal_due": pdue,
                         "interest_paid": F(0), "withholding": F(0), "principal_paid": F(0), "cure_paid": F(0),
                         "swept": F(0), "bal": b}
            line[tid]["gross_due"] = gross_up(line[tid]["interest_due"], wrate[tid])
        fee_due = F(c["senior_fee"]) + fee_arrears
        if cash_rule is not None:
            need = fee_due + sum(v["gross_due"] + v["principal_due"] for v in line.values())
            got_cash, got_collateral = cash_rule(k, need, sum(v["bal"] for v in line.values()))
            collections.append((got_cash, got_collateral))
            c["collections"] = [money(x[0]) for x in collections]
            c["collateral"] = [money(x[1]) for x in collections]
        cash = F(c["collections"][k - 1])
        collateral = F(c["collateral"][k - 1])
        fee_paid = min(cash, fee_due); cash -= fee_paid; fee_arrears = fee_due - fee_paid
        drawn = topup = F(0)
        if rsv and k == term:
            cash += reserve; drawn += reserve; reserve = F(0)

        def left(t):
            return line[t]["bal"] - line[t]["principal_paid"]

        def share(level, amounts, targets):
            nonlocal cash
            got, used = pari_passu(cash, amounts)
            for t in got:
                for target in targets:
                    line[t][target] += got[t]
            cash -= used

        def interest(lv):
            nonlocal cash, reserve, drawn
            dues = {t: line[t]["gross_due"] for t in by_level[lv]}
            if rsv and lv in rsv["covers"]:
                draw = min(max(F(0), sum(dues.values(), F(0)) - cash), reserve)
                cash += draw; reserve -= draw; drawn += draw
            got, used = pari_passu(cash, dues)
            for t in got:
                tax = round_cent(got[t] * wrate[t])
                line[t]["withholding"] += tax; line[t]["interest_paid"] += got[t] - tax
            cash -= used

        def principal(lv):
            share(lv, {t: max(F(0), line[t]["principal_due"] - line[t]["principal_paid"]) for t in by_level[lv]},
                  ("principal_paid",))

        def cure(lv):
            nonlocal cash
            if lv not in triggers:
                return
            senior = [x for x in levels if x <= lv]
            den = sum((left(t) for x in senior for t in by_level[x]), F(0))
            if den * triggers[lv] <= collateral:
                return
            need = ceil_cent(den - collateral / triggers[lv])
            for x in senior:
                if need <= 0 or cash <= 0:
                    break
                amounts = {t: left(t) for t in by_level[x]}
                budget = min(need, cash, sum(amounts.values(), F(0)))
                got, used = pari_passu(budget, amounts)
                for t in got:
                    line[t]["principal_paid"] += got[t]; line[t]["cure_paid"] += got[t]
                cash -= used; need -= used

        if c["waterfall"] == "INTEREST_FIRST":
            for lv in levels:
                interest(lv); cure(lv)
            for lv in levels:
                principal(lv)
        else:
            for lv in levels:
                interest(lv); cure(lv); principal(lv)
        if rsv and k < term:
            remaining = sum((left(t) for t in ids), F(0))

            def covered(r):
                swept = min(cash - r, remaining) if c["excess_cash"] == "SWEEP" else F(0)
                target = max(F(rsv["floor"]), round_cent(F(rsv["target_rate"]) * (remaining - swept)))
                return reserve + r >= target
            topup = smallest_cent(F(0), cash, covered)
            if topup is None:
                topup = cash
            cash -= topup; reserve += topup
        if c["excess_cash"] == "SWEEP":
            for lv in levels:
                share(lv, {t: left(t) for t in by_level[lv]}, ("swept",))
        released = cash
        out = {}
        for tid in ids:
            v = line[tid]
            deferred[tid] = v["interest_due"] - v["interest_paid"]
            arrears[tid] = max(F(0), v["principal_due"] - v["principal_paid"])
            bal[tid] = v["bal"] - v["principal_paid"] - v["swept"]
            out[tid] = {"interest": money(v["interest"]), "true_up": money(v["true_up"]),
                        "overdue_interest": money(v["overdue_interest"]), "interest_due": money(v["interest_due"]),
                        "interest_paid": money(v["interest_paid"]), "withholding": money(v["withholding"]),
                        "deferred_interest": money(deferred[tid]),
                        "principal_due": money(v["principal_due"]), "principal_paid": money(v["principal_paid"]),
                        "cure_paid": money(v["cure_paid"]), "swept": money(v["swept"]),
                        "closing_balance": money(bal[tid]), "default_margin": flags[k - 1][tid]}
        periods.append({"number": k, "accrual_start": row["start"].isoformat(), "accrual_end": row["end"].isoformat(),
                        "payment_date": row["payment"].isoformat(), "determination_date": row["determination"].isoformat(),
                        "collections": c["collections"][k - 1], "fee_paid": money(fee_paid),
                        "reserve_draw": money(drawn), "reserve_topup": money(topup), "reserve_balance": money(reserve),
                        "released": money(released),
                        "tranches": out})
    return {"contract_id": c["contract_id"], "periods": periods}


# ---------------------------------------------------------------- generator

OFFSETS = (0, 0, -300, -240, -420, 60, 330, 540, -180)


def _amount(rng, lo, hi):
    return f"{rng.randrange(lo, hi)}.{rng.randrange(100):02d}"


def _rate(rng, lo=4000000, hi=9000000):
    return f"0.{rng.randrange(lo, hi):08d}"


def _spread(rng):
    return f"0.0{rng.randrange(150, 400):03d}"


def make(seed, opening, term, anchor, accrual_dates, day_counts, kinds, seniority, notice_days=2,
         n_prepay=4, n_changes=6, n_fix=4, waterfall="INTEREST_FIRST", excess_cash="SWEEP",
         cash_mix=(0.08, 0.3, 0.8, 1.0, 1.0, 1.15, 1.4), floor_bias=False, coverage=None,
         collateral_mix=(1.05, 1.15, 1.25, 1.35, 1.5, 1.7), withholding=None, reserve=None, lockout=None):
    """Build one contract.  day_counts/kinds/seniority give one entry per tranche."""
    rng = random.Random(seed)
    start = parse(opening)
    ids = [chr(ord("A") + i) for i in range(len(day_counts))]
    regular = [start] + [add_months(start, k, anchor) for k in range(1, term + 1)]
    ph = set()
    for k in rng.sample(range(1, term + 1), min(4, term)):
        d = regular[k]
        if d.weekday() < 5:
            ph.add(d.isoformat())
    while len(ph) < 9:
        d = start + timedelta(days=rng.randrange(1, (regular[-1] - start).days))
        if d.weekday() < 5:
            ph.add(d.isoformat())
    rh = set(rng.sample(sorted(ph), 4))
    while len(rh) < 10:
        d = start + timedelta(days=rng.randrange(-25, (regular[-1] - start).days))
        if d.weekday() < 5:
            rh.add(d.isoformat())
    fixings = {}; level = rng.randrange(420, 520)
    d = start - timedelta(days=40)
    while d <= regular[-1] + timedelta(days=10):
        if is_business(d, rh):
            level = max(380, min(590, level + rng.choice((-3, -1, 0, 0, 0, 1, 2, 4))))
            fixings[d.isoformat()] = f"{level // 100}.{level % 100:02d}"
        d += timedelta(days=1)
    lowest = min(int(v.replace(".", "")) for v in fixings.values())
    tranches = []
    for tid, dc, kind, lv in zip(ids, day_counts, kinds, seniority):
        bal = rng.randrange(100000, 400000)
        t = {"tranche_id": tid, "seniority": lv, "opening_balance": f"{bal}.{rng.randrange(100):02d}",
             "day_count": dc, "scheduled_principal": f"{bal // (term * 3)}.{rng.randrange(100):02d}"}
        if kind == "FIXED":
            t["rate"] = {"type": "FIXED", "annual_rate": _rate(rng)}
        else:
            floor = (lowest + rng.randrange(20, 60)) if floor_bias else rng.choice((0, 0, 100, 250))
            t["rate"] = {"type": "FLOATING", "index": "SOFR", "spread": _spread(rng),
                         "floor": f"0.{floor:04d}", "lookback_days": rng.randrange(2, 6)}
        if lockout and tid in lockout:
            t["rate"]["lockout_days"] = lockout[tid]
        if withholding and tid in withholding:
            t["withholding_rate"] = withholding[tid]
        tranches.append(t)
    c = {"contract_id": f"FAC-{seed}", "currency": "USD", "opening_date": opening, "anchor_day": anchor,
         "term_months": term, "accrual_dates": accrual_dates, "notice_days": notice_days,
         "payment_holidays": sorted(ph), "rate_holidays": sorted(rh),
         "senior_fee": _amount(rng, 900, 2500), "overdue_rate": _rate(rng, 9000000, 14000000),
         "waterfall": waterfall, "excess_cash": excess_cash, "default_margin": f"0.0{rng.randrange(100, 300):03d}",
         "coverage_tests": dict(coverage or {}), "reserve": dict(reserve) if reserve else None,
         "fixings": fixings, "tranches": tranches, "prepayments": [], "bookings": []}
    rows = schedule(c)

    def inside(k):
        lo = regular[k - 1] + timedelta(days=5); hi = regular[k] - timedelta(days=5)
        while True:
            d = lo + timedelta(days=rng.randrange((hi - lo).days + 1))
            if is_business(d, ph):
                return d

    for n in range(n_prepay):
        k = rng.randrange(1, term)
        c["prepayments"].append({"logical_id": f"PP-{n + 1:03d}", "tranche_id": rng.choice(ids),
                                 "effective_date": inside(k).isoformat(), "amount": _amount(rng, 3000, 25000)})
    floating = {t["tranche_id"] for t in tranches if t["rate"]["type"] == "FLOATING"}
    bookings = []

    def at(day):
        return datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(seconds=rng.randrange(86400))

    def near_cutoff(day):
        # within the hour either side of 17:00 New York, where a fixed offset or a local date goes wrong
        roll = rng.random()
        if roll < 0.1:
            return cutoff(day)
        return cutoff(day) + timedelta(seconds=rng.choice((-1, 1)) * rng.randrange(1, 3600))

    def stamp(moment):
        offset = rng.choice(OFFSETS)
        local = moment.astimezone(timezone(timedelta(minutes=offset)))
        text = local.replace(tzinfo=None).isoformat(timespec="seconds")
        if offset == 0 and rng.random() < 0.5:
            return text + "Z"
        sign = "-" if offset < 0 else "+"
        return f"{text}{sign}{abs(offset) // 60:02d}:{abs(offset) % 60:02d}"

    def booked(k, pattern):
        row = rows[k - 1]
        if pattern == "advance":
            return at(row["start"] + timedelta(days=rng.randrange(0, 8)))
        if pattern == "edge_det":
            return near_cutoff(row["determination"])
        if pattern == "edge_pay":
            return at(row["payment"])
        later = rows[min(term - 1, k + rng.randrange(0, 3))]
        if rng.random() < 0.3:
            return near_cutoff(later["determination"])
        return at(later["determination"] - timedelta(days=rng.randrange(0, 12)))

    def revise(lid, body, rec, k, alter):
        roll = rng.random()
        if roll < 0.62:
            later = rows[min(term - 1, k + rng.randrange(0 if roll < 0.45 else 1, 4))]
            if rng.random() < 0.35:
                r2 = near_cutoff(later["determination"])
            else:
                r2 = at(later["determination"] - timedelta(days=rng.randrange(-3, 10)))
            r2 = max(rec + timedelta(minutes=rng.randrange(1, 90)), r2)
            if roll < 0.45:
                bookings.append({"logical_id": lid, "revision": 2, "action": "SET", "recorded_at": stamp(r2), **alter(dict(body))})
            else:
                bookings.append({"logical_id": lid, "revision": 2, "action": "RETRACT", "recorded_at": stamp(r2)})

    for n in range(n_changes):
        k = rng.randrange(1, term); tid = rng.choice(ids)
        body = {"kind": "RATE_CHANGE", "tranche_id": tid, "effective_date": inside(k).isoformat()}
        if tid in floating:
            body["spread"] = _spread(rng)
        else:
            body["annual_rate"] = _rate(rng, 1000000 if rng.random() < 0.3 else 4000000, 9500000)
        rec = booked(k, rng.choice(("advance", "edge_det", "edge_pay", "late", "late")))
        lid = f"RC-{n + 1:03d}"
        bookings.append({"logical_id": lid, "revision": 1, "action": "SET", "recorded_at": stamp(rec), **body})

        def alter(b):
            if "spread" in b:
                b["spread"] = _spread(rng)
            else:
                b["annual_rate"] = _rate(rng, 1000000, 9500000)
            return b
        revise(lid, body, rec, k, alter)
    used = set()
    for n in range(n_fix):
        k = rng.randrange(1, term)
        row = rows[k - 1]
        while True:
            day = row["start"] + timedelta(days=rng.randrange(0, max(1, (row["end"] - row["start"]).days)))
            if day.isoformat() in fixings and day.isoformat() not in used:
                break
        used.add(day.isoformat())
        old = int(fixings[day.isoformat()].replace(".", ""))
        new = max(100, old + rng.choice((-150, -60, -25, 30, 80, 200)))
        body = {"kind": "FIXING_CORRECTION", "fixing_date": day.isoformat(), "fixing": f"{new // 100}.{new % 100:02d}"}
        rec = booked(k, rng.choice(("edge_det", "edge_pay", "late", "late")))
        lid = f"FX-{n + 1:03d}"
        bookings.append({"logical_id": lid, "revision": 1, "action": "SET", "recorded_at": stamp(rec), **body})

        def alter(b):
            v = max(100, old + rng.choice((-90, -10, 15, 120)))
            b["fixing"] = f"{v // 100}.{v % 100:02d}"
            return b
        revise(lid, body, rec, k, alter)
    for e, eid in zip(bookings, rng.sample(range(1000, 9000), len(bookings))):
        e["booking_id"] = eid
    rng.shuffle(bookings); rng.shuffle(c["prepayments"])
    c["bookings"] = bookings

    def cash_rule(k, need, total):
        f = rng.choice(cash_mix)
        if k == term:
            f = min(f, 1.02)
        g = F(rng.choice(collateral_mix)).limit_denominator(100) + F(rng.randrange(-300, 300), 10000)
        return F(int(need * F(f).limit_denominator(100) * 100), 100), F(int(total * g * 100), 100)
    c["collections"] = []; c["collateral"] = []
    settle(c, Audit(), cash_rule)
    return c
