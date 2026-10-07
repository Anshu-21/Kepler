`/app/engine.py` settles our multi-tranche loan facilities, but it was written for short fixed-rate deals where every payment date was fully funded. It ignores when rate bookings were made (and our offices stamp them in their own time zones), never trues up interest settled on stale rates, approximates SOFR with an average and knows nothing of lockouts, only knows three day counts, pays tranches one after another with no deferral, default margin, coverage cure, sweep, withholding gross-up or reserve account, and was never meant for the fifty-year concession books we now run.

Rewrite it so `reconcile(contract)` follows `/app/SETTLEMENT_SPEC.md` exactly, for every period of every tranche, to the cent.

`/app/data/examples/` has one contract with its expected output. It is fully funded and doesn't touch most of the spec, so matching it proves little. `/app/data/sample_contract.json` exercises nearly everything but has no answer. To run one:

```sh
python /app/run_engine.py /app/data/sample_contract.json /app/output/sample.json
```

The verifier runs eight other contracts within the bounds at the end of the spec. Some sit at the bounds, up to 600 periods and twenty tranches. Each is run twice in a fresh process with 6 seconds per run, and both runs must agree. Only `/app/engine.py` is collected; you can import the helpers next to it, but changes to them won't be kept. Keep `reconcile(contract)` as the entry point.

You have 9000 seconds to complete this task. Do not cheat by using online solutions or hints specific to this task.
