"""The sealed contracts.  Built inside the verifier image from model.py."""
import hashlib, json
from model import make, settle

ALL = ("ACT/360", "ACT/365F", "ACT/ACT ISDA", "ACT/ACT ICMA", "30/360", "30E/360", "30E/360 ISDA")
LOW = (0.9, 1.0, 1.1, 1.25, 1.5)

SPECS = [
    # large contracts at the bounds: the runtime budget matters here
    dict(seed=7101, opening="2030-01-31", term=360, anchor=31, accrual_dates="ADJUSTED",
         day_counts=["ACT/ACT ICMA", "30E/360 ISDA", "ACT/ACT ISDA", "30/360", "ACT/365F", "30E/360", "ACT/360",
                     "ACT/ACT ICMA", "ACT/365F", "30/360"],
         kinds=["FIXED", "FLOATING", "FIXED", "FLOATING", "FLOATING", "FIXED", "FLOATING", "FLOATING", "FLOATING", "FIXED"],
         seniority=[1, 2, 2, 3, 3, 4, 5, 5, 6, 6], notice_days=3, n_prepay=75, n_changes=230, n_fix=150,
         coverage={"2": "3.50", "3": "2.20"}, collateral_mix=LOW,
         withholding={"B": "0.30", "D": "0.15", "G": "0.20", "J": "0.15"}, reserve={"opening_balance": "18000.00", "target_rate": "0.0300", "floor": "15000.00", "covers": [1, 2]},
         lockout={"B": 2, "H": 3}),
    dict(seed=7102, opening="2031-08-29", term=348, anchor=29, accrual_dates="UNADJUSTED",
         day_counts=["ACT/ACT ISDA", "ACT/360", "ACT/360", "30E/360 ISDA", "ACT/365F", "30/360", "ACT/360",
                     "ACT/ACT ICMA", "30E/360"],
         kinds=["FLOATING", "FLOATING", "FLOATING", "FIXED", "FLOATING", "FIXED", "FLOATING", "FIXED", "FLOATING"],
         seniority=[1, 1, 2, 3, 3, 4, 5, 5, 6], notice_days=5, n_prepay=70, n_changes=220, n_fix=160,
         waterfall="LEVEL_BY_LEVEL", floor_bias=True, coverage={"1": "5.00", "3": "2.00"}, collateral_mix=LOW,
         withholding={"A": "0.15", "C": "0.30", "E": "0.20"}, reserve={"opening_balance": "20000.00", "target_rate": "0.0175", "floor": "20000.00", "covers": [1, 2, 3]},
         lockout={"A": 1, "E": 3}),
    dict(seed=7103, opening="2029-03-15", term=300, anchor=15, accrual_dates="ADJUSTED",
         day_counts=list(ALL) + ["ACT/360", "ACT/365F", "ACT/360"],
         kinds=["FLOATING", "FIXED", "FLOATING", "FIXED", "FLOATING", "FIXED", "FLOATING", "FLOATING", "FIXED", "FLOATING"],
         seniority=[1, 2, 2, 2, 3, 4, 4, 5, 6, 6], notice_days=2, n_prepay=65, n_changes=210, n_fix=150,
         excess_cash="RELEASE", coverage={"2": "1.55", "4": "1.20"}, collateral_mix=LOW,
         cash_mix=(0.02, 0.1, 0.3, 0.8, 1.0, 1.0, 1.2, 1.5),
         withholding={"C": "0.20", "D": "0.15", "H": "0.30"}, reserve={"opening_balance": "0.00", "target_rate": "0.0250", "floor": "10000.00", "covers": [1, 2, 3]},
         lockout={"A": 2, "G": 2, "J": 1}),
    # small contracts aimed at specific rules
    dict(seed=6104, opening="2029-02-28", term=12, anchor=31, accrual_dates="UNADJUSTED",
         day_counts=["30E/360 ISDA", "30/360", "30E/360 ISDA"], kinds=["FIXED", "FLOATING", "FLOATING"],
         seniority=[2, 1, 2], notice_days=1, n_prepay=3, n_changes=6, n_fix=3, excess_cash="RELEASE",
         coverage={"1": "2.80"}, collateral_mix=LOW,
         withholding={"B": "0.30"}, reserve={"opening_balance": "4000.00", "target_rate": "0.0300", "floor": "5000.00", "covers": [1]},
         lockout={"C": 2}),
    dict(seed=6105, opening="2030-12-16", term=8, anchor=16, accrual_dates="UNADJUSTED",
         day_counts=["30E/360 ISDA", "ACT/ACT ISDA"], kinds=["FIXED", "FLOATING"], seniority=[1, 1],
         notice_days=2, n_prepay=2, n_changes=5, n_fix=3, waterfall="LEVEL_BY_LEVEL",
         withholding={"A": "0.15", "B": "0.20"}),
    dict(seed=6106, opening="2029-10-31", term=36, anchor=31, accrual_dates="ADJUSTED",
         day_counts=["ACT/360", "ACT/360", "ACT/ACT ICMA", "ACT/360", "30/360"], kinds=["FLOATING"] * 5,
         seniority=[1, 2, 2, 2, 3], notice_days=4, n_prepay=8, n_changes=20, n_fix=20, floor_bias=True,
         cash_mix=(0.05, 0.25, 0.6, 1.0, 1.0, 1.3), coverage={"2": "1.45"}, collateral_mix=LOW,
         withholding={"B": "0.15", "C": "0.30"}, reserve={"opening_balance": "0.00", "target_rate": "0.0150", "floor": "8000.00", "covers": [1, 2]},
         lockout={"A": 3, "C": 2, "D": 1}),
    dict(seed=6107, opening="2031-05-05", term=24, anchor=5, accrual_dates="UNADJUSTED",
         day_counts=["ACT/365F", "30/360", "ACT/360", "30E/360"], kinds=["FIXED", "FIXED", "FLOATING", "FIXED"],
         seniority=[3, 1, 2, 1], notice_days=2, n_prepay=6, n_changes=14, n_fix=6,
         waterfall="LEVEL_BY_LEVEL", excess_cash="RELEASE", coverage={"1": "2.10", "2": "1.60"}, collateral_mix=LOW,
         withholding={"A": "0.20"}, reserve={"opening_balance": "9000.00", "target_rate": "0.0200", "floor": "6000.00", "covers": [1]}),
    dict(seed=6108, opening="2033-06-30", term=10, anchor=30, accrual_dates="ADJUSTED",
         day_counts=["ACT/360", "30E/360 ISDA", "ACT/ACT ICMA"], kinds=["FLOATING", "FIXED", "FIXED"],
         seniority=[1, 2, 3], notice_days=3, n_prepay=3, n_changes=8, n_fix=4,
         reserve={"opening_balance": "2500.00", "target_rate": "0.0400", "floor": "3000.00", "covers": [1, 2]},
         lockout={"A": 2}),
]

# Hand-placed prepayments the generator would only hit by luck.
EXTRA = {
    # larger than the balance left on the tranche, so it is capped
    6108: [{"logical_id": "PP-900", "tranche_id": "B", "effective_date": "2033-10-24", "amount": "900000.00"}],
    # a month-end effective date under 30E/360 ISDA outside the termination month
    6105: [{"logical_id": "PP-901", "tranche_id": "A", "effective_date": "2031-02-28", "amount": "12500.00"}],
}

# sha256 of the canonical JSON of every (contract, expected) pair; guards against generator drift
DIGEST = "8608a181ac64ab04ebf116d88e430b9864bf2765cb9995f48e9404d080cfc049"


def build(spec):
    spec = dict(spec)
    seed = spec["seed"]
    contract = make(spec.pop("seed"), spec.pop("opening"), spec.pop("term"), spec.pop("anchor"),
                    spec.pop("accrual_dates"), spec.pop("day_counts"), spec.pop("kinds"), spec.pop("seniority"),
                    **spec)
    contract["prepayments"] = contract["prepayments"] + EXTRA.get(seed, [])
    return contract


def cases(audit=None):
    out = []
    for spec in SPECS:
        contract = build(spec)
        out.append((contract, settle(contract, audit)))
    return out


def digest(pairs):
    return hashlib.sha256(json.dumps(pairs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
