import json, os, re, subprocess, tempfile
from pathlib import Path

from cases import DIGEST, cases, digest


def sealed():
    """The pairs build_check.py saved at image build time, regenerated only if they are missing."""
    saved = Path(__file__).with_name("sealed.json")
    if saved.is_file():
        pairs = [tuple(pair) for pair in json.loads(saved.read_text())]
        assert digest(pairs) == DIGEST, "sealed contracts changed since the build"
        return pairs
    return cases()

MONEY = re.compile(r"^-?(0|[1-9][0-9]*)\.[0-9]{2}$")
PERIOD_KEYS = {"number", "accrual_start", "accrual_end", "payment_date", "determination_date", "collections", "fee_paid",
               "reserve_draw", "reserve_topup", "reserve_balance", "released", "tranches"}
MONEY_KEYS = {"interest", "true_up", "overdue_interest", "interest_due", "interest_paid", "withholding", "deferred_interest",
              "principal_due", "principal_paid", "cure_paid", "swept", "closing_balance"}
TRANCHE_KEYS = MONEY_KEYS | {"default_margin"}
LIMIT = 6


def run_candidate(contract, folder, tag):
    input_dir = folder / f"{tag}-input"; output_dir = folder / f"{tag}-output"
    input_dir.mkdir(); output_dir.mkdir()
    source = input_dir / "contract.json"; output = output_dir / "result.json"
    raw = json.dumps(contract, separators=(",", ":")).encode(); source.write_bytes(raw)
    os.chmod(folder, 0o711); os.chmod(input_dir, 0o555); os.chmod(source, 0o444); os.chmod(output_dir, 0o777)
    result = subprocess.run(["setpriv", "--reuid=65534", "--regid=65534", "--clear-groups", "python",
                             "/app/candidate_runner.py", str(source), str(output)],
                            cwd="/app", text=True, capture_output=True, timeout=LIMIT)
    assert result.returncode == 0, result.stderr[-3000:]
    assert source.read_bytes() == raw
    return json.loads(output.read_text())


def validate(got, want):
    assert set(got) == {"contract_id", "periods"} and got["contract_id"] == want["contract_id"]
    assert len(got["periods"]) == len(want["periods"])
    for a, w in zip(got["periods"], want["periods"]):
        where = f"{want['contract_id']} period {w['number']}"
        assert set(a) == PERIOD_KEYS, where
        for key in ("number", "accrual_start", "accrual_end", "payment_date", "determination_date"):
            assert a[key] == w[key], f"{where} {key}"
        assert set(a["tranches"]) == set(w["tranches"]), where
        for tid, row in a["tranches"].items():
            assert set(row) == TRANCHE_KEYS, f"{where} {tid}"
            for key in MONEY_KEYS:
                value = row[key]
                assert isinstance(value, str) and MONEY.fullmatch(value), f"{where} {tid} {key} format"
                assert value == w["tranches"][tid][key], f"{where} {tid} {key}"
            assert row["default_margin"] is w["tranches"][tid]["default_margin"], f"{where} {tid} default_margin"
        for key in ("collections", "fee_paid", "reserve_draw", "reserve_topup", "reserve_balance", "released"):
            assert isinstance(a[key], str) and MONEY.fullmatch(a[key]) and a[key] == w[key], f"{where} {key}"


def test_settlement_statements():
    engine = Path("/app/engine.py")
    assert engine.is_file() and not engine.is_symlink()
    original = engine.read_bytes()
    with tempfile.TemporaryDirectory(prefix="settle-") as name:
        folder = Path(name)
        for i, (contract, expected) in enumerate(sealed()):
            first = run_candidate(contract, folder, f"c{i}a")
            second = run_candidate(contract, folder, f"c{i}b")
            assert first == second, f"{contract['contract_id']} differs between runs"
            validate(first, expected)
    assert engine.read_bytes() == original
