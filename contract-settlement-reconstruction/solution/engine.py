"""Facility settlement engine (reference solution).

Layers, kept apart on purpose:

* calendar: the period schedule and both business-day calendars;
* knowledge: the rate changes and fixings known on a determination date;
* accrual: one tranche over one period, on its recorded opening balance and
  default flag;
* restatement: a per-tranche cache of period accruals that is invalidated
  only where the bookings that changed since the last determination date can
  reach, so a 360-period facility is not re-accrued 360 times over;
* settlement: true-ups, credits, overdue interest, withholding gross-ups,
  coverage cures, the reserve account and the priority of payments.

Money is Decimal in the default context; the specification guarantees no
accrual rounding sits close enough to a half for 28 significant digits to
matter, and withholding and reserve targets are exact products.  The two
"smallest whole-cent amount" rules are solved by checking the definition, not
by a formula: a gross-up can sit a cent either side of net / (1 - rate), and a
top-up changes the sweep its own target is sized on.
"""
import bisect
import calendar
from datetime import date, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP, ROUND_DOWN

from calendar_tools import add_months, modified_following, parse

CENT = Decimal("0.01")
RATE_STEP = Decimal("0.0000001")
ZERO = Decimal(0)


class Calendar:
    def __init__(self, holidays):
        self.holidays = {date.fromisoformat(h) for h in holidays}
        self._previous = {}
        self._floor = {}

    def is_business(self, day):
        return day.weekday() < 5 and day not in self.holidays

    def previous(self, day, count):
        key = (day, count)
        hit = self._previous.get(key)
        if hit is None:
            hit = day
            while count:
                hit -= timedelta(days=1)
                if self.is_business(hit):
                    count -= 1
            self._previous[key] = hit
        return hit

    def on_or_before(self, day):
        hit = self._floor.get(day)
        if hit is None:
            hit = self._on_or_before(day)
            self._floor[day] = hit
        return hit

    def _on_or_before(self, day):
        while not self.is_business(day):
            day -= timedelta(days=1)
        return day


def build_schedule(contract):
    start = parse(contract["opening_date"])
    anchor = int(contract["anchor_day"])
    payment_cal = Calendar(contract["payment_holidays"])
    adjusted = contract["accrual_dates"] == "ADJUSTED"
    periods, previous_end = [], start
    for number in range(1, int(contract["term_months"]) + 1):
        regular = add_months(start, number, anchor)
        payment = modified_following(regular, set(contract["payment_holidays"]))
        end = payment if adjusted else regular
        periods.append({
            "number": number, "start": previous_end, "end": end, "payment": payment,
            "determination": payment_cal.previous(payment, int(contract["notice_days"])),
        })
        previous_end = end
    return periods


def _last_day(day):
    return day.day == calendar.monthrange(day.year, day.month)[1]


def year_fraction(convention, a, b, termination):
    days = (b - a).days
    if convention == "ACT/360":
        return Decimal(days) / 360
    if convention == "ACT/365F":
        return Decimal(days) / 365
    if convention == "ACT/ACT ISDA":
        total, cursor = ZERO, a
        while cursor < b:
            stop = min(b, date(cursor.year + 1, 1, 1))
            total += Decimal((stop - cursor).days) / (366 if calendar.isleap(cursor.year) else 365)
            cursor = stop
        return total
    d1, d2 = a.day, b.day
    if convention == "30/360":
        d1 = min(d1, 30)
        if d2 == 31 and d1 == 30:
            d2 = 30
    elif convention == "30E/360":
        d1, d2 = min(d1, 30), min(d2, 30)
    elif convention == "30E/360 ISDA":
        d1 = 30 if _last_day(a) else min(d1, 30)
        if _last_day(b) and not (b == termination and b.month == 2):
            d2 = 30
        else:
            d2 = min(d2, 30)
    else:
        raise ValueError(f"unknown day count {convention}")
    return Decimal(360 * (b.year - a.year) + 30 * (b.month - a.month) + d2 - d1) / 360


def interest_amount(balance, rate, convention, a, b, termination):
    """Round once: balance * rate * fraction, with the fraction's division last."""
    if convention in ("ACT/360", "ACT/365F"):
        value = balance * rate * (b - a).days / (360 if convention == "ACT/360" else 365)
    else:
        value = balance * rate * year_fraction(convention, a, b, termination)
    return value.quantize(CENT, rounding=ROUND_HALF_UP)




class BookingLog:
    """Winning bookings as the knowledge date moves forward, with what changed."""

    def __init__(self, contract):
        self.records = sorted(contract["bookings"], key=lambda r: r["recorded_at"])
        self.next = 0
        self.winners = {}
        self.live = {}

    def advance(self, day):
        """Admit records up to day; return (old, new) live records of each changed booking."""
        touched = set()
        while self.next < len(self.records) and self.records[self.next]["recorded_at"] <= day:
            record = self.records[self.next]
            self.next += 1
            held = self.winners.get(record["logical_id"])
            if held is None or int(record["revision"]) > int(held["revision"]):
                self.winners[record["logical_id"]] = record
                touched.add(record["logical_id"])
        changes = []
        for lid in touched:
            old = self.live.get(lid)
            new = self.winners[lid] if self.winners[lid]["action"] == "SET" else None
            if old is not new:
                changes.append((old, new))
                if new is None:
                    self.live.pop(lid, None)
                else:
                    self.live[lid] = new
        return changes


class Knowledge:
    """Rate changes and fixings as known on one determination date."""

    def __init__(self, contract, live, base_fixings, rate_cal):
        self.live = live
        self.base = base_fixings
        self.corrections = {date.fromisoformat(r["fixing_date"]): Decimal(r["fixing"]) / 100
                            for r in live.values() if r["kind"] == "FIXING_CORRECTION"}
        self.changes = {}
        for r in live.values():
            if r["kind"] == "RATE_CHANGE":
                self.changes.setdefault(r["tranche_id"], []).append(r)
        self.rate_cal = rate_cal
        self.cache = {}

    def fixing(self, day):
        hit = self.corrections.get(day)
        return hit if hit is not None else self.base[day]

    def compounded(self, a, b, lookback):
        key = (a, b, lookback)
        if key not in self.cache:
            cal, factor, day = self.rate_cal, Decimal(1), a
            while day < b:
                base = cal.on_or_before(day)
                run = 0
                while day < b and cal.on_or_before(day) == base:
                    run += 1
                    day += timedelta(days=1)
                factor *= 1 + self.fixing(cal.previous(base, lookback)) * run / 360
            rate = (factor - 1) * 360 / (b - a).days
            self.cache[key] = rate.quantize(RATE_STEP, rounding=ROUND_HALF_UP)
        return self.cache[key]


def accrue(prepayments, know, tranche, period, opening, termination, margin):
    """Interest of one tranche over one period; returns (interest, balance at accrual end)."""
    tid, terms = tranche["tranche_id"], tranche["rate"]
    start, end = period["start"], period["end"]
    floating = terms["type"] == "FLOATING"
    field = "spread" if floating else "annual_rate"
    level = Decimal(terms[field])
    breaks = []
    for change in know.changes.get(tid, []):
        eff = parse(change["effective_date"])
        if eff <= start:
            breaks.append((eff, change["logical_id"], "pre", change))
        elif eff < end:
            breaks.append((eff, change["logical_id"], "rate", change))
    for pre in prepayments.get(tid, []):
        eff = parse(pre["effective_date"])
        if start < eff < end:
            breaks.append((eff, pre["logical_id"], "cash", pre))
    breaks.sort(key=lambda b: (b[0], b[1]))
    for eff, _, what, change in breaks:
        if what == "pre":
            level = Decimal(change[field])

    def segment(a, b, bal, lvl):
        if a >= b or bal == 0:
            return ZERO
        if floating:
            rate = max(know.compounded(a, b, int(terms["lookback_days"])), Decimal(terms["floor"])) + lvl + margin
        else:
            rate = lvl + margin
        return interest_amount(bal, rate, tranche["day_count"], a, b, termination)

    balance, cursor, total = opening, start, ZERO
    inside = [b for b in breaks if b[2] != "pre"]
    for day in sorted({b[0] for b in inside}):
        total += segment(cursor, day, balance, level)
        cursor = day
        for eff, _, what, record in inside:
            if eff != day:
                continue
            if what == "cash":
                balance -= min(Decimal(record["amount"]), balance)
            else:
                level = Decimal(record[field])
    total += segment(cursor, end, balance, level)
    return total, balance


class Restatement:
    """Per-tranche period accruals, a stale set, and a running total.

    A changed rate change makes its tranche stale from the period containing its
    effective date onward, because a rate persists until superseded. A changed
    fixing makes stale every floating period whose segments could look it up.
    """

    REACH = timedelta(days=40)  # lookback runs never reach further back than this

    def __init__(self, schedule, tranches):
        self.ends = [p["end"] for p in schedule]
        self.starts = [p["start"] for p in schedule]
        self.floating = [t["tranche_id"] for t in tranches if t["rate"]["type"] == "FLOATING"]
        self.cache = {t["tranche_id"]: [] for t in tranches}
        self.stale = {t["tranche_id"]: set() for t in tranches}
        self.total = {t["tranche_id"]: ZERO for t in tranches}

    def invalidate(self, changes):
        for pair in changes:
            for record in pair:
                if record is None:
                    continue
                if record["kind"] == "RATE_CHANGE":
                    tid = record["tranche_id"]
                    cut = bisect.bisect_right(self.ends, parse(record["effective_date"]))
                    self.stale[tid].update(range(cut, len(self.cache[tid])))
                else:
                    day = parse(record["fixing_date"])
                    lo = bisect.bisect_right(self.ends, day)
                    hi = bisect.bisect_right(self.starts, day + self.REACH)
                    for tid in self.floating:
                        self.stale[tid].update(j for j in range(lo, hi) if j < len(self.cache[tid]))

    def refresh(self, tid, idx, compute):
        """Bring periods 0..idx of one tranche up to date; return (restated before idx, current)."""
        cache = self.cache[tid]
        todo = self.stale[tid]
        if len(cache) <= idx:
            todo.update(range(len(cache), idx + 1))
            cache.extend([None] * (idx + 1 - len(cache)))
        for j in sorted(todo):
            fresh = compute(j)
            if cache[j] is not None:
                self.total[tid] -= cache[j][0]
            cache[j] = fresh
            self.total[tid] += fresh[0]
        todo.clear()
        return self.total[tid] - cache[idx][0], cache[idx]


def pari_passu(cash, dues):
    """Share cash over dues in integer cents; returns (paid, cash used)."""
    cents = {t: int(d * 100) for t, d in dues.items()}
    total = sum(cents.values())
    have = int(cash * 100)
    if have >= total:
        return dict(dues), Decimal(total) / 100
    shares, dropped = {}, {}
    for t, c in cents.items():
        shares[t], dropped[t] = divmod(have * c, total)
    left = have - sum(shares.values())
    for t in sorted(cents, key=lambda t: (-dropped[t], t))[:left]:
        shares[t] += 1
    return {t: Decimal(s) / 100 for t, s in shares.items()}, cash


def money(value):
    value = value.quantize(CENT)
    if value == 0:
        value = abs(value)
    return f"{value:.2f}"


def ceil_cent(value):
    return value.quantize(CENT, rounding=ROUND_CEILING)


def withheld(gross, rate):
    return (gross * rate).quantize(CENT, rounding=ROUND_HALF_UP)


def gross_up(net, rate):
    """Smallest whole-cent gross whose amount after withholding covers net.

    net / (1 - rate) is only a starting point: withholding is rounded on the
    gross, so the answer can sit a cent either side of it.
    """
    if rate == 0 or net <= 0:
        return net
    gross = ceil_cent(net / (1 - rate))
    while gross - withheld(gross, rate) < net:
        gross += CENT
    while gross > 0 and gross - CENT - withheld(gross - CENT, rate) >= net:
        gross -= CENT
    return gross


def smallest_cent(limit, ok):
    """Smallest whole-cent amount in [0, limit] satisfying a monotone ok, else None."""
    lo, hi = 0, int(limit * 100)
    if not ok(Decimal(hi) / 100):
        return None
    while lo < hi:
        mid = (lo + hi) // 2
        if ok(Decimal(mid) / 100):
            hi = mid
        else:
            lo = mid + 1
    return Decimal(lo) / 100


def reconcile(contract):
    schedule = build_schedule(contract)
    termination = schedule[-1]["end"]
    tranches = contract["tranches"]
    ids = [t["tranche_id"] for t in tranches]
    levels = {}
    for t in tranches:
        levels.setdefault(int(t["seniority"]), []).append(t["tranche_id"])
    order = sorted(levels)
    triggers = {int(k): Decimal(v) for k, v in contract["coverage_tests"].items()}
    margin_rate = Decimal(contract["default_margin"])
    overdue_rate = Decimal(contract["overdue_rate"])
    wrate = {t["tranche_id"]: Decimal(t.get("withholding_rate", "0")) for t in tranches}
    rsv = contract.get("reserve")
    reserve = Decimal(rsv["opening_balance"]) if rsv else ZERO
    covers = set(rsv["covers"]) if rsv else set()
    prepayments = {}
    for p in contract["prepayments"]:
        prepayments.setdefault(p["tranche_id"], []).append(p)
    base_fixings = {date.fromisoformat(d): Decimal(v) / 100 for d, v in contract["fixings"].items()}
    log = BookingLog(contract)
    rate_cal = Calendar(contract["rate_holidays"])
    restate = Restatement(schedule, tranches)

    balance = {t["tranche_id"]: Decimal(t["opening_balance"]) for t in tranches}
    zero = lambda: {tid: ZERO for tid in ids}
    recognised, credit, deferred, arrears = zero(), zero(), zero(), zero()
    fee_arrears = ZERO
    openings, margins, periods = [], [], []
    for idx, period in enumerate(schedule):
        number = period["number"]
        final = number == len(schedule)
        restate.invalidate(log.advance(period["determination"].isoformat()))
        know = Knowledge(contract, log.live, base_fixings, rate_cal)
        openings.append(dict(balance))
        margins.append({tid: margin_rate if deferred[tid] > 0 else ZERO for tid in ids})
        rows = {}
        for t in tranches:
            tid = t["tranche_id"]
            restated, (interest, after) = restate.refresh(
                tid, idx, lambda j: accrue(prepayments, know, t, schedule[j], openings[j][tid], termination, margins[j][tid]))
            true_up = restated - recognised[tid]
            recognised[tid] = restated + interest
            current = interest + true_up + credit[tid]
            credit[tid] = min(current, ZERO)
            current = max(current, ZERO)
            overdue = ZERO
            if idx:
                days = (period["payment"] - schedule[idx - 1]["payment"]).days
                overdue = (deferred[tid] * overdue_rate * days / 360).quantize(CENT, rounding=ROUND_HALF_UP)
            principal_due = after if final else min(Decimal(t["scheduled_principal"]) + arrears[tid], after)
            rows[tid] = {"interest": interest, "true_up": true_up, "overdue_interest": overdue,
                         "interest_due": current + deferred[tid] + overdue, "interest_paid": ZERO, "withholding": ZERO,
                         "principal_due": principal_due, "principal_paid": ZERO, "cure_paid": ZERO,
                         "swept": ZERO, "after": after}
            rows[tid]["gross_due"] = gross_up(rows[tid]["interest_due"], wrate[tid])
        cash = Decimal(contract["collections"][idx])
        collateral = Decimal(contract["collateral"][idx])
        fee_due = Decimal(contract["senior_fee"]) + fee_arrears
        fee_paid = min(cash, fee_due)
        fee_arrears = fee_due - fee_paid
        cash -= fee_paid
        drawn = topup = ZERO
        if rsv and final:
            cash += reserve
            drawn, reserve = reserve, ZERO

        def outstanding(tid):
            return rows[tid]["after"] - rows[tid]["principal_paid"]

        def distribute(amounts, *targets, budget=None):
            nonlocal cash
            limit = cash if budget is None else budget
            paid, used = pari_passu(limit, amounts)
            for tid, amount in paid.items():
                for target in targets:
                    rows[tid][target] += amount
            cash -= used
            return used

        def pay_interest(lv):
            nonlocal cash, reserve, drawn
            dues = {tid: rows[tid]["gross_due"] for tid in levels[lv]}
            if lv in covers:
                draw = min(max(ZERO, sum(dues.values(), ZERO) - cash), reserve)
                cash += draw
                reserve -= draw
                drawn += draw
            paid, used = pari_passu(cash, dues)
            for tid, amount in paid.items():
                tax = withheld(amount, wrate[tid])
                rows[tid]["withholding"] += tax
                rows[tid]["interest_paid"] += amount - tax
            cash -= used

        def pay_principal(lv):
            distribute({tid: max(ZERO, rows[tid]["principal_due"] - rows[tid]["principal_paid"])
                        for tid in levels[lv]}, "principal_paid")

        def cure(lv):
            if lv not in triggers:
                return
            covered = [x for x in order if x <= lv]
            senior = sum((outstanding(tid) for x in covered for tid in levels[x]), ZERO)
            if senior * triggers[lv] <= collateral:
                return
            need = ceil_cent(senior - collateral / triggers[lv])
            for x in covered:
                if need <= 0 or cash <= 0:
                    break
                amounts = {tid: outstanding(tid) for tid in levels[x]}
                budget = min(need, cash, sum(amounts.values(), ZERO))
                need -= distribute(amounts, "principal_paid", "cure_paid", budget=budget)

        if contract["waterfall"] == "INTEREST_FIRST":
            for lv in order:
                pay_interest(lv)
                cure(lv)
            for lv in order:
                pay_principal(lv)
        else:
            for lv in order:
                pay_interest(lv)
                cure(lv)
                pay_principal(lv)
        if rsv and not final:
            remaining = sum((outstanding(tid) for tid in ids), ZERO)
            sweep = contract["excess_cash"] == "SWEEP"
            floor, target_rate = Decimal(rsv["floor"]), Decimal(rsv["target_rate"])

            def topped_up(r):
                left = remaining - (min(cash - r, remaining) if sweep else ZERO)
                return reserve + r >= max(floor, (target_rate * left).quantize(CENT, rounding=ROUND_HALF_UP))

            topup = smallest_cent(cash, topped_up)
            if topup is None:
                topup = cash
            cash -= topup
            reserve += topup
        if contract["excess_cash"] == "SWEEP":
            for lv in order:
                distribute({tid: outstanding(tid) for tid in levels[lv]}, "swept")
        out = {}
        for tid in ids:
            r = rows[tid]
            deferred[tid] = r["interest_due"] - r["interest_paid"]
            arrears[tid] = max(ZERO, r["principal_due"] - r["principal_paid"])
            balance[tid] = r["after"] - r["principal_paid"] - r["swept"]
            out[tid] = {"interest": money(r["interest"]), "true_up": money(r["true_up"]),
                        "overdue_interest": money(r["overdue_interest"]), "interest_due": money(r["interest_due"]),
                        "interest_paid": money(r["interest_paid"]), "withholding": money(r["withholding"]),
                        "deferred_interest": money(deferred[tid]),
                        "principal_due": money(r["principal_due"]), "principal_paid": money(r["principal_paid"]),
                        "cure_paid": money(r["cure_paid"]), "swept": money(r["swept"]),
                        "closing_balance": money(balance[tid]), "default_margin": margins[idx][tid] > 0}
        periods.append({
            "number": number,
            "accrual_start": period["start"].isoformat(),
            "accrual_end": period["end"].isoformat(),
            "payment_date": period["payment"].isoformat(),
            "determination_date": period["determination"].isoformat(),
            "collections": money(Decimal(contract["collections"][idx])),
            "fee_paid": money(fee_paid),
            "reserve_draw": money(drawn),
            "reserve_topup": money(topup),
            "reserve_balance": money(reserve),
            "released": money(cash),
            "tranches": out,
        })
    return {"contract_id": contract["contract_id"], "periods": periods}
