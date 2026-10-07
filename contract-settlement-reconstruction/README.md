# contract-settlement-reconstruction

Explanations of the difficulty, the solution and the verification live in `task.toml`; this file only maps the bundle.

| path | role |
| --- | --- |
| `environment/app/engine.py` | legacy engine the agent rewrites (the only collected artifact) |
| `environment/app/SETTLEMENT_SPEC.md` | the settlement rules, output format and input bounds |
| `environment/app/data/examples/` | one fully funded contract with expected output |
| `environment/app/data/sample_contract.json` | a 60-period contract using nearly every feature, including two standing rebooked rates, without an answer |
| `tests/model.py` | independent exact-arithmetic model (memoised, not incremental) and contract generator |
| `tests/cases.py` | the eight sealed contract specs and their SHA-256 digest |
| `tests/build_check.py` | build-time check of the digest, the bounds, rounding margins and the cases the contracts must reach; saves the sealed pairs for the test |
| `tests/test_task.py` | runs the engine unprivileged, twice per contract, and compares every field |
| `solution/engine.py` | reference engine |

To change a sealed contract, edit `tests/cases.py`, run `python tests/build_check.py` from `tests/`, and copy the printed digest into `DIGEST`.
