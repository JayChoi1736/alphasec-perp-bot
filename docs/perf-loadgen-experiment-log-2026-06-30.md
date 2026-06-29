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
| `MATCH_MAKER_CANCEL_EVERY=1` | `63.3 TPS` | `/tmp/perf-manual-cancel-every1-20260630-065353.log` | Reject. It removed margin errors but cancel traffic reduced fill throughput. |
| `MATCH_LEVELS=1` | `76.1 TPS` | `/tmp/perf-manual-levels1-20260630-065453.log` | Reject. Lower open-order notional reduced submit pressure and did not recover TPS. |
| Existing accounts, `MATCH_INVENTORY_CAP=0.01` | `71.4 TPS` | `/tmp/perf-manual-cap001-existing-20260630-065616.log` | Reject. Taker inventory stayed bounded, but maker insufficient-margin errors persisted. |
| Fresh accounts, `MATCH_GAS_ETH=0.0005`, default deposit/cap smoke | `11.4 TPS` | `/tmp/perf-manual-fresh-lowgas-20260630-065924.log` | Reject for TPS. Useful finding: low-gas fresh account creation/funding works. |
| Fresh accounts, low gas, `deposit=500`, `cap=0.005`, `maker_size=0.005` | `28.5 TPS` | `/tmp/perf-manual-fresh-lowgas-cap005-20260630-070012.log` | Reject. Maker insufficient-margin persisted because maker order notional was still too high. |
| Fresh accounts, low gas, `deposit=500`, `cap=0.005`, `maker_size=0.001` | `29.2 TPS` | `/tmp/perf-manual-fresh-lowgas-makersmall-20260630-070100.log` | Reject. Margin was cleaner, but maker liquidity/submit pressure was too low. |
| `perf_stages.py --fresh-keystore-dir` smoke with low-gas env overrides | `27.1 TPS` | `docs/perf-stage-summary-fresh-runner-smoke-2026-06-30.md` | Reject for TPS. Useful finding: staged runner can now create isolated fresh-account configs. |

## Current Account/Funding State

The perf owner account had about `0.09` L2 ETH before the low-gas fresh-account smoke tests, about `0.045` L2 ETH after the manual low-gas tests, and about `0.03` L2 ETH after the staged fresh-runner smoke. That is not enough for another broad fresh-account sweep with the current default gas target (`MATCH_GAS_ETH=0.1` per generated sub-account).

```text
owner: 0xE1A44ca6F8c577458232A59080D0DC6A22419c33
eth_balance: 0.09
eth_balance_after_lowgas_smoke: 0.045
eth_balance_after_fresh_runner_smoke: 0.03
```

The current low-TPS runs are dominated by maker-side insufficient-margin errors. Reducing inventory cap, reducing levels, and canceling more often did not restore the earlier clean max.

Fresh account creation can still be done with a smaller gas target:

```text
MATCH_GAS_ETH=0.0005
```

However, the low-gas fresh runs did not recover TPS. `maker_size=0.005` still exhausted maker margin, while `maker_size=0.001` reduced margin errors but did not provide enough book depth/submit pressure.

Runner support added:

```text
perf_stages.py --fresh-keystore-dir <dir>
perf_stages.py --env KEY=VALUE
```

These options make isolated-account staged runs reproducible without manually writing temporary config files.

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

## Retest After Fresh Runner Commit

Local commit:

```text
46edd1c perf: add fresh keystore stage runner options
```

Push status:

```text
git push origin main failed because current GitHub account has READ permission on JayChoi1736/alphasec-perp-bot.
origin/main is not updated; local main is ahead by 8 commits.
```

Command:

```text
.venv/bin/python perf_stages.py --config config.perf.json --target-sweep 80,120,160 --duration 30 --stages baseline,maker_guard --log-dir /tmp --summary docs/perf-stage-summary-retest-20260630-070902.md --stage-timeout 120 --pprof-url https://l2-pprof-perf.dexor.trade/debug/pprof/profile --pprof-seconds 20 --pprof-stages baseline
```

Results:

```text
baseline target=80: 1209 fills in 31s = 39.0 TPS, maker insufficient-margin=50
maker_guard target=80: 1247 fills in 31s = 40.2 TPS, maker_submit_ok=60
baseline target=120: 1492 fills in 31s = 48.1 TPS, maker insufficient-margin=51
maker_guard target=120: 1768 fills in 31s = 57.0 TPS, maker_submit_ok=90
baseline target=160: 1598 fills in 31s = 51.5 TPS, maker insufficient-margin=83
maker_guard target=160: 1630 fills in 31s = 52.6 TPS, maker_submit_ok=480
```

Profiles from baseline stages:

```text
t80: Sequencer.createBlock 67.61% cum, SequenceTransactions 67.16% cum, SnapshotDirtyTracking/copyBoolMap 56.32% cum, SubmitTransaction 3.46% cum
t120: Sequencer.createBlock 67.68% cum, SequenceTransactions 67.27% cum, SnapshotDirtyTracking/copyBoolMap 54.05% cum, SubmitTransaction 3.90% cum
t160: Sequencer.createBlock 66.39% cum, SequenceTransactions 66.01% cum, SnapshotDirtyTracking/copyBoolMap 54.76% cum, SubmitTransaction 4.30% cum
```

Interpretation:

```text
This retest does not replace the clean max. Highest observed was 57.0 TPS, but the current existing-account state is polluted: baseline runs still hit maker insufficient-margin, and maker_guard removes those errors by suppressing maker submissions rather than increasing clean liquidity. Core CPU profile still points at sequencer transaction sequencing and dirty order snapshot copy/hash work, not local signing or DNS.
```

## Wide Recheck And Maker Adaptive Experiment

Wide fanout recheck:

```text
summary: docs/perf-stage-summary-wide-recheck-20260630-071917.md
command: perf_stages.py --target 240 --duration 30 --stages wide_accounts
result: 2021 fills in 31s = 65.1 TPS
taker_submit_ok: 2425
maker_submit_ok: 1854
taker avg latency: 3459.2ms
taker in-flight wait avg: 3356.1ms
maker insufficient-margin: 12
```

Interpretation:

```text
Wider account fanout did not recover throughput. It raised RPC/core admission latency into multi-second territory, so this is not a local signing or local account-count ceiling.
```

Loadgen change:

```text
match.py: add optional per-maker order-size backoff after maker insufficient-margin
perf_stages.py: add maker_adaptive stage with MATCH_MAKER_SIZE_BACKOFF=0.5 and MATCH_MAKER_MIN_SIZE=0.001
```

Adaptive retest:

```text
summary: docs/perf-stage-summary-maker-adaptive-20260630-072656.md
command: perf_stages.py --target 240 --duration 30 --stages baseline,maker_adaptive
baseline: 1662 fills in 31s = 53.6 TPS, maker insufficient-margin=154
maker_adaptive: 1671 fills in 31s = 53.9 TPS, maker insufficient-margin=84
```

Interpretation:

```text
maker_adaptive reduced failed maker traffic but did not materially increase filled TPS. Keep it as an optional loadgen hygiene stage, not as the current max-TPS path.
```

## Final-Only Position Poll Experiment

Loadgen change:

```text
match.py: add MATCH_POSITION_POLL_MODE=continuous|final|off
match.py: continuous mode now performs one final position poll after load cleanup
perf_stages.py: add final_poll stage with MATCH_POSITION_POLL_MODE=final and MATCH_INVENTORY_CAP=1.0
```

Target 240 comparison:

```text
summary: docs/perf-stage-summary-final-poll-20260630-073400.md
baseline target=240: 1910 fills in 31s = 61.6 TPS, taker avg latency=418.8ms, wait=456.1ms, maker insufficient-margin=158
final_poll target=240: 2190 fills in 31s = 70.6 TPS, taker avg latency=365.7ms, wait=399.1ms, maker insufficient-margin=135
```

Final-poll target sweep:

```text
summary: docs/perf-stage-summary-final-poll-sweep-20260630-073539.md
final_poll target=300: 2150 fills in 31s = 69.3 TPS, taker avg latency=487.1ms, wait=520.7ms, maker insufficient-margin=149
final_poll target=360: 2069 fills in 31s = 66.6 TPS, taker avg latency=613.4ms, wait=649.2ms, maker insufficient-margin=240
```

Profiles:

```text
target=300 profile=/tmp/perf-stage-final_poll-t300-20260630-073539.pprof.pb.gz
Sequencer.createBlock 70.42% cum, SequenceTransactions 69.98% cum, SnapshotDirtyTracking/copyBoolMap 60.27% cum, SubmitTransaction 4.84% cum

target=360 profile=/tmp/perf-stage-final_poll-t360-20260630-073539.pprof.pb.gz
Sequencer.createBlock 70.11% cum, SequenceTransactions 69.50% cum, SnapshotDirtyTracking/copyBoolMap 58.54% cum, SubmitTransaction 4.65% cum
```

Interpretation:

```text
Final-only position polling removes continuous read RPC pressure and improves current target=240 throughput, but it does not recover the old 146.7 TPS clean max. Higher targets now increase taker latency and maker insufficient-margin again. This keeps the bottleneck diagnosis on core transaction sequencing plus dirty order snapshot copy/hash work under the current account state.
```
## Follow-up Current-State Sweeps

Additional runs after the final_poll change did not beat the `70.6 TPS`
current-state result. These runs were intentionally varied to test whether the
remaining ceiling was caused by low-index account pollution, maker traffic,
account fanout, maker order size, or per-account in-flight limits.

```text
slice60 summary: docs/perf-stage-summary-final-poll-slice60-20260630-073954.md
slice60 final_poll target=240: 1786 fills in 31s = 57.6 TPS

taker-only summary: docs/perf-stage-summary-final-poll-taker-only-20260630-074055.md
taker-only final_poll target=240: 1721 fills in 31s = 55.5 TPS

fanout60 summary: docs/perf-stage-summary-final-poll-fanout60-20260630-074150.md
fanout60 final_poll target=240: 1432 fills in 31s = 46.1 TPS

maker-size-0.0025 summary: docs/perf-stage-summary-final-poll-makersize0025-20260630-074254.md
maker-size-0.0025 final_poll target=240: 1566 fills in 31s = 50.5 TPS

inflight2 summary: docs/perf-stage-summary-final-poll-inflight2-20260630-074346.md
inflight2 final_poll target=240: 1597 fills in 31s = 51.5 TPS

local sweep summary: docs/perf-stage-summary-final-poll-local-sweep-20260630-074440.md
final_poll target=180: 1443 fills in 31s = 46.5 TPS
final_poll target=220: 1638 fills in 31s = 52.8 TPS
final_poll target=260: 1677 fills in 31s = 54.1 TPS
```

Interpretation:

```text
None of the follow-up variants moved the max above 70.6 TPS. Removing maker
traffic lowers fill throughput, wider maker/taker fanout worsens latency, lower
maker size removes some pressure but reduces liquidity, and per-account
in-flight=2 does not help the clean path. The current degraded account state is
not producing a stable clean max search; the strongest confirmed ceiling is
still core transaction sequencing plus dirty order snapshot copy/hash work, with
maker insufficient-margin churn as the immediate retest blocker.
```
