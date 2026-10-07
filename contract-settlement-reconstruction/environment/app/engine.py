"""Facility settlement engine (legacy).

Written for the original book: short fixed-rate facilities paid in full every
month, one booking per rate change, no coverage tests, no withholding and no
reserve account, every booking stamped in New York.  Kept for its output format.
"""
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from calendar_tools import add_months, modified_following, parse
from daycount import fraction

CENT = Decimal("0.01")


def current_rate(contract, tranche, start, end):
    terms = tranche["rate"]
    if terms["type"] == "FIXED":
        rate = Decimal(terms["annual_rate"])
        for b in contract["bookings"]:
            if b["action"] == "SET" and b["kind"] == "RATE_CHANGE" and b["tranche_id"] == tranche["tranche_id"] \
                    and b["effective_date"] < end.isoformat():
                rate = Decimal(b["annual_rate"])
        return rate
    seen = [Decimal(v) / 100 for k, v in contract["fixings"].items() if start.isoformat() <= k < end.isoformat()]
    average = sum(seen, Decimal(0)) / len(seen) if seen else Decimal(0)
    return max(average, Decimal(terms["floor"])) + Decimal(terms["spread"])


def reconcile(contract):
    start = parse(contract["opening_date"])
    holidays = set(contract["payment_holidays"])
    tranches = sorted(contract["tranches"], key=lambda t: t["seniority"])
    balances = {t["tranche_id"]: Decimal(t["opening_balance"]) for t in tranches}
    periods, cursor = [], start
    for number in range(1, int(contract["term_months"]) + 1):
        boundary = add_months(start, number, int(contract["anchor_day"]))
        payment = modified_following(boundary, holidays)
        cash = Decimal(contract["collections"][number - 1])
        fee = min(cash, Decimal(contract["senior_fee"]))
        cash -= fee
        rows = {}
        for t in tranches:
            tid = t["tranche_id"]
            for p in contract["prepayments"]:
                if p["tranche_id"] == tid and cursor < parse(p["effective_date"]) < boundary:
                    balances[tid] -= Decimal(p["amount"])
            rate = current_rate(contract, t, cursor, boundary)
            interest = (balances[tid] * rate * fraction(cursor, boundary, t["day_count"])).quantize(CENT, rounding=ROUND_HALF_UP)
            principal = min(Decimal(t["scheduled_principal"]), balances[tid])
            interest_paid = min(cash, interest); cash -= interest_paid
            principal_paid = min(cash, principal); cash -= principal_paid
            balances[tid] -= principal_paid
            rows[tid] = {"interest": f"{interest:.2f}", "true_up": "0.00", "overdue_interest": "0.00",
                         "interest_due": f"{interest:.2f}", "interest_paid": f"{interest_paid:.2f}", "withholding": "0.00",
                         "deferred_interest": f"{interest - interest_paid:.2f}", "principal_due": f"{principal:.2f}",
                         "principal_paid": f"{principal_paid:.2f}", "cure_paid": "0.00", "swept": "0.00",
                         "closing_balance": f"{balances[tid]:.2f}", "default_margin": False}
        periods.append({
            "number": number,
            "accrual_start": cursor.isoformat(),
            "accrual_end": boundary.isoformat(),
            "payment_date": payment.isoformat(),
            "determination_date": (payment - timedelta(days=int(contract["notice_days"]))).isoformat(),
            "collections": f"{Decimal(contract['collections'][number - 1]):.2f}",
            "fee_paid": f"{fee:.2f}",
            "reserve_draw": "0.00",
            "reserve_topup": "0.00",
            "reserve_balance": f"{Decimal((contract.get('reserve') or {}).get('opening_balance', '0')):.2f}",
            "released": f"{cash:.2f}",
            "tranches": {t["tranche_id"]: rows[t["tranche_id"]] for t in contract["tranches"]},
        })
        cursor = boundary
    return {"contract_id": contract["contract_id"], "periods": periods}
