# Perf Loadgen Experiment Log

Date: 2026-06-30 KST
Environment: `perf`
RPC path: `https://l2-rpc-perf.dexor.trade`

## Current Ceiling

Clean max remains `146.7 trades/s`.

`time_inflight2` reached `150.2 trades/s`, but it produced taker nonce errors, so it is treated as an unsafe/dirty loadgen mode.

## Rejected Experiments

| Experiment | Result | Evidence | Decision |
| --- | ---: | --- | --- |
| `MATCH_ACCOUNT_INFLIGHT=2`, `MATCH_NONCE_MODE=normal` | `114.1 TPS` | `/tmp/perf-manual-normal-inflight2-20260630-*.log`, `943` taker nonce errors | Reject. More in-flight per account breaks clean nonce ordering. |
| Taker start staggering | `97.0`, `109.6`, `108.4 TPS` for baseline targets `240`, `300`, `360` | `/tmp/perf-stage-baseline-t*-20260630-062206.log` | Reject. Burst smoothing did not improve throughput. |
| Taker start staggering with `wide_accounts` | `98.2 TPS` at target `240`; higher targets timed out or were interrupted | `/tmp/perf-stage-wide_accounts-t*-20260630-062206.log` | Reject. Wider fanout still hit high wait latency. |
| `MATCH_MAKER_STAGGER=1` | `124.1 TPS` | `/tmp/perf-manual-maker-stagger-20260630-063048.log` | Reject. Smoother maker refresh reduced fill throughput. |
| `MATCH_MAKER_REFRESH_SEC=1` | `83.0 TPS` | `/tmp/perf-manual-maker-refresh1-20260630-063141.log` | Reject. More maker refresh traffic increased contention. |
| `MATCH_MAKER_REFRESH_SEC=4` | `90.1 TPS` | `/tmp/perf-manual-maker-refresh4-20260630-063233.log` | Reject. Less maker refresh reduced available fill flow. |
| Two worker processes, `MATCH_WORKER_COUNT=2` | `102.6 TPS` aggregate | `/tmp/perf-manual-2worker-20260630-063330-w*.log` | Reject. More local processes increased admission latency instead of raising fills. |
| `MATCH_MAKER_MODE=once` | `40.6 TPS` | `/tmp/perf-manual-maker-once-20260630-063742.log` | Reject. Resting maker liquidity was depleted quickly. |
| `MATCH_MAKER_MODE=once`, `MATCH_TAKER_SIZE=0.0001` | `0.0 TPS` | `/tmp/perf-manual-maker-once-small-taker-20260630-063913.log` | Reject. Order notional was below the engine minimum. |
| `MATCH_MAKER_MODE=once`, `MATCH_TAKER_SIZE=0.0002` | `93.9 TPS` | `/tmp/perf-manual-maker-once-taker-0p0002-20260630-*.log` | Reject. It passed minimum notional but did not generate enough submit pressure. |
| `MATCH_MAKER_COUNT=45`, `MATCH_TAKER_COUNT=90` | `87.8 TPS` | `/tmp/perf-manual-takers90-20260630-064111.log` | Reject. More taker fanout increased wait latency to `894.7ms`. |
| Existing accounts, `MATCH_MAKER_DEPOSIT=500`, `MATCH_TAKER_DEPOSIT=200` | `74.6 TPS` | `/tmp/perf-manual-deposit500-200-20260630-*.log` | Reject. Accounts were already above deposit targets; margin errors persisted. |
| Fresh accounts, default cap | `23.2 TPS` | `/tmp/perf-manual-fresh-accounts-20260630-064658.log` | Reject. Large inventory cap caused taker and maker insufficient-margin errors. |
| Fresh account retry with lower cap and larger deposits | `73.1 TPS` | `/tmp/perf-manual-fresh-deposit-cap001-20260630-*.log` | Reject. Maker insufficient-margin errors persisted after the first polluted run. |

## Code Changes

`match.py` now polls perp positions only for taker accounts. Maker positions are not used for fill counting or taker inventory control, so polling maker accounts was unnecessary RPC load.

```text
test: test_position_poll_items_skip_maker_accounts
verification: .venv/bin/python -m unittest test_match_helpers.py test_encode.py test_perf_stages.py -q
```

Measured after the polling change:

```text
summary: docs/perf-stage-summary-taker-only-poll-2026-06-30.md
result: 3470 fills in 46s = 75.3 trades/s
profile: /tmp/perf-stage-baseline-20260630-064418.pprof.pb.gz
```

This did not raise the observed ceiling because the run was dominated by maker insufficient-margin errors and the same core sequencing/snapshot paths. The change is still retained as loadgen overhead reduction, not as a proven TPS increase.

## Interpretation

The clean loadgen ceiling is not caused by local signing or a single Python event loop alone. The rejected experiments either increase nonce failures or increase RPC/core admission latency.

The remaining bottleneck diagnosis is unchanged:

```text
primary: core sequencer block creation / transaction sequencing
secondary: dirty order snapshot tracking and string-key map copy/hash work
visible but lower: SubmitTransaction / RPC admission
```

No core code changes were made.
