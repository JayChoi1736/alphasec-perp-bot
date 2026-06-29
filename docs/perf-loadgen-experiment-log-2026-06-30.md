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

## Interpretation

The clean loadgen ceiling is not caused by local signing or a single Python event loop alone. The rejected experiments either increase nonce failures or increase RPC/core admission latency.

The remaining bottleneck diagnosis is unchanged:

```text
primary: core sequencer block creation / transaction sequencing
secondary: dirty order snapshot tracking and string-key map copy/hash work
visible but lower: SubmitTransaction / RPC admission
```

No core code changes were made.
