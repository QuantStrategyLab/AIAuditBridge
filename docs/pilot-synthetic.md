# Synthetic Protocol Probe

Opt-in pilot for `protocol_probe`. It exercises the end-to-end task protocol with a frozen synthetic input. There is no market data, no strategy executor and no orders.

## Scope and ownership

- Step 3 adds the same three independently owned modules under `ai_service.scenarios`: `pilot`, `pilot_domain` and `pilot_cli`. They ship in 2.0.11 and share no core code with the personal package.
- `examples/pilots/instance.json` is a sample and stays `enabled=false`. Only an approved activation candidate can compile a task. A sample may be copied while still disabled so the exact configured `model_ref` can be set before the reviewed candidate; the script sets its project and revision.
- Existing authentication and the V2 envelope are unchanged; the seven V2 fields are the same.
- Accepted `data_classification` is `synthetic` only.

## What the pilot does

`pilot.prepare` requires a single Dot stage and a synthetic input. The objective asks the worker to read the frozen input, claim the task once, and return the exact `output_schema` object using the current attempt id and revision. The worker must not access markets, brokers, files or other tools.

`pilot_cli` exposes `dry-run`, `submit` and `verify`; `submit` writes a mode-600 intent receipt before networking and `.submitted` on success, and `verify` checks the caller readback.

## Input contract

The fixture is `synthetic.protocol-input.v1`:

- `no_order` must be `true`.
- `series` is a list of 2 to 16 integers, each with absolute value at most 1000.

The adapter rejects anything else. All fixture strings, organizations and hosts are generic.

Version 2.0.13 also accepts the fixed text fixture in
`examples/pilots/protocol-probe`: `quant.synthetic-probe.v1` with exactly
`expected_output={"probe":"ok"}`. Its separate adapter returns only that fixed
result. Both formats require the existing `protocol_probe` template, a single
Dot stage and synthetic classification; the series format above is unchanged.
The new example stays disabled and has no activation approval.

## Output contract

The result is fully fixed by the input:

- `synthetic=true`, `no_order=true`, `adoption_allowed=false`
- `sample_count` is the series length
- `difference_sum` is `series[-1] - series[0]`

For the checked sample, `sample_count=4`, `difference_sum=5`, `synthetic=true`, `no_order=true`, `adoption_allowed=false`.

## What this is not

This is a protocol probe, not an economic backtest and not a real executor. It computes the sum of adjacent differences in a small synthetic integer series. A production backtest and forward test are a later step, not part of this pilot.

## Offline tests

Offline tests drive `TaskClient -> Application -> TaskRuntime -> DotEventProvider -> signed callback fixture -> MCP claim/report -> caller readback -> delete subscription -> gone`. Network is denied in the new integration tests; they do not prove OAuth, the UI or live webhook delivery. Fixtures live under `examples/pilots`. Unit tests: `python -m unittest discover -s tests/task_service -v`, plus the existing CI.

## Evidence limits

Hashes are consistency checks only and do not prove compliance or economic correctness. No production data is involved, and no order or adoption path exists.
