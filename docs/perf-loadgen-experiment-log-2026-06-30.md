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

## Push-Blocked Retest

Push was attempted after committing the follow-up sweep docs, but GitHub denied
write access for the current account:

```text
git push origin main
ERROR: Permission to JayChoi1736/alphasec-perp-bot.git denied to probepark.
```

Retest command:

```text
.venv/bin/python perf_stages.py --config config.perf.json --target 240 --duration 30 --stages final_poll --log-dir /tmp --summary docs/perf-stage-summary-final-poll-retest-20260630-075002.md --stage-timeout 180 --pprof-url https://l2-pprof-perf.dexor.trade/debug/pprof/profile --pprof-seconds 20 --pprof-stages final_poll --env MATCH_INVENTORY_CAP=1.0
```

Result:

```text
summary: docs/perf-stage-summary-final-poll-retest-20260630-075002.md
profile: /tmp/perf-stage-final_poll-20260630-075002.pprof.pb.gz
final_poll target=240: 1470 fills in 31s = 47.4 TPS
taker_submit_ok=1440
maker_submit_ok=1377
maker insufficient-margin=128
taker avg latency=577.9ms
taker sign avg=2.5ms
taker in-flight wait avg=608.5ms
```

Profile:

```text
Sequencer.createBlock: 19.02s / 75.75% cum
ExecutionEngine.sequenceTransactionsWithBlockMutex: 18.97s / 75.55% cum
OrderBook.SnapshotDirtyTracking: 16.25s / 64.72% cum
book.copyBoolMap: 16.24s / 64.68% cum
SubmitTransaction: 0.70s / 2.79% cum
```

Interpretation:

```text
The latest retest is lower than the 70.6 TPS current-state high and far below
the 146.7 TPS clean max. It confirms the same bottleneck shape: local signing is
low-millisecond, RPC admission/SubmitTransaction is not the primary CPU cost,
and core sequencing plus dirty snapshot copy/hash work dominate while maker
insufficient-margin churn keeps this account set from producing a clean max run.
```

## Maker Error Cooldown Experiment

Loadgen change:

```text
match.py: add optional MATCH_MAKER_ERROR_COOLDOWN_THRESHOLD and MATCH_MAKER_ERROR_COOLDOWN_SEC
match.py: expose maker_cooldown and maker_cooldown_skipped in DONE summary
perf_stages.py: add maker_cooldown stage
```

Reason:

```text
If a small subset of maker accounts repeatedly hits insufficient-margin, those
accounts can waste maker submit budget. The cooldown option pauses a maker after
repeated insufficient-margin errors without changing default final_poll behavior.
```

Results:

```text
summary: docs/perf-stage-summary-maker-cooldown-20260630-075610.md
final_poll target=240: 1675 fills in 31s = 54.0 TPS, maker insufficient-margin=111
maker_cooldown threshold=2 target=240: 1558 fills in 31s = 50.2 TPS, maker insufficient-margin=44

summary: docs/perf-stage-summary-maker-cooldown-aggressive-20260630-075825.md
maker_cooldown threshold=1 target=240: 1575 fills in 31s = 50.8 TPS, maker insufficient-margin=17

summary: docs/perf-stage-summary-maker-cooldown-counters-20260630-080137.md
maker_cooldown threshold=1 target=240: 1556 fills in 31s = 50.1 TPS
maker_cooldown=7
maker_cooldown_skipped=4
maker insufficient-margin=7
taker avg latency=546.1ms
taker sign avg=2.5ms
taker in-flight wait avg=573.3ms
```

Profile from the counter-verified run:

```text
profile: /tmp/perf-stage-maker_cooldown-20260630-080137.pprof.pb.gz
Sequencer.createBlock: 19.01s / 75.47% cum
ExecutionEngine.sequenceTransactionsWithBlockMutex: 18.95s / 75.23% cum
OrderBook.SnapshotDirtyTracking: 16.11s / 63.95% cum
book.copyBoolMap: 16.11s / 63.95% cum
SubmitTransaction: 0.78s / 3.10% cum
```

Interpretation:

```text
Cooldown reduces maker insufficient-margin errors, but it also reduces maker
liquidity/repost pressure enough that filled TPS stays below final_poll. This
does not become the max-TPS path. It strengthens the current diagnosis: failed
maker submit churn is a retest hygiene issue, while the throughput ceiling is
still core sequencing plus dirty snapshot copy/hash work.
```

## Healthy-Accounts Prep Filter Experiment

Loadgen change:

```text
match.py: add MATCH_SKIP_PREP_FAILED=1 to exclude accounts that fail prep
match.py: expose prep_skipped in DONE summary
perf_stages.py: add healthy_accounts stage
```

Reason:

```text
prep warn uses DEX command 0x45, which is CMD_PERP_SET_LEVERAGE. Current perf
accounts have makers that fail set leverage before load starts. This experiment
tests whether excluding those polluted accounts recovers throughput.
```

Result:

```text
summary: docs/perf-stage-summary-healthy-accounts-20260630-080727.md
final_poll target=240: 1461 fills in 31s = 47.1 TPS
healthy_accounts target=240: 1448 fills in 31s = 46.6 TPS
prep_skipped=4
filtered accounts: makers=4, takers=0
healthy maker insufficient-margin=46
healthy taker avg latency=580.1ms
healthy taker sign avg=2.6ms
healthy taker in-flight wait avg=614.4ms
```

Profile:

```text
profile: /tmp/perf-stage-healthy_accounts-20260630-080727.pprof.pb.gz
Sequencer.createBlock: 18.55s / 75.84% cum
ExecutionEngine.sequenceTransactionsWithBlockMutex: 18.49s / 75.59% cum
OrderBook.SnapshotDirtyTracking: 15.88s / 64.92% cum
book.copyBoolMap: 15.88s / 64.92% cum
SubmitTransaction: 0.83s / 3.39% cum
```

Interpretation:

```text
Skipping prep-failed accounts removes visibly polluted makers, but it does not
increase filled TPS. It lowers maker liquidity slightly and the profile remains
dominated by core sequencing and dirty snapshot copy/hash work. Keep the option
as diagnostics/hygiene, not as the max-TPS path.
```

## Healthy Maker Pool Selection Experiment

Loadgen change:

```text
match.py: add MATCH_MAKER_POOL_COUNT, MATCH_HEALTHY_MAKER_MIN_FREE, MATCH_HEALTHY_MAKER_MAX_ABS_POS
match.py: select makers from a larger pool by free margin and absolute position
match.py: expose health_maker_skipped in DONE summary
perf_stages.py: add healthy_makers stage
```

Reason:

```text
The first 30 maker accounts all have residual positions, and 29/30 have
abs(position) > 0.05. Across the full 150 maker pool there are enough accounts
with free margin >= 500 and abs(position) <= 0.02, so the test checks whether a
cleaner maker set recovers TPS.
```

Result:

```text
summary: docs/perf-stage-summary-healthy-makers-20260630-081630.md
final_poll target=240: 1346 fills in 31s = 43.4 TPS
healthy_makers target=240: 1050 fills in 31s = 33.8 TPS
healthy maker pool selected 30/150
health_maker_skipped=120
healthy_makers errors={}
healthy taker avg latency=837.6ms
healthy taker sign avg=2.6ms
healthy taker in-flight wait avg=865.5ms
```

Profile:

```text
profile: /tmp/perf-stage-healthy_makers-20260630-081630.pprof.pb.gz
Sequencer.createBlock: 19.30s / 77.11% cum
ExecutionEngine.sequenceTransactionsWithBlockMutex: 19.24s / 76.87% cum
OrderBook.SnapshotDirtyTracking: 17.46s / 69.76% cum
book.copyBoolMap: 17.46s / 69.76% cum
SubmitTransaction: 0.61s / 2.44% cum
```

Interpretation:

```text
Selecting healthier makers removes margin errors but reduces fill throughput
and raises taker wait latency. It is not the max-TPS path. The clean maker set
still spends most core CPU in sequencing and dirty snapshot copy/hash work, so
the remaining ceiling is not explained by local account selection alone.
```

## Push-Blocked Retest

Push status:

```text
git push origin main
ERROR: Permission to JayChoi1736/alphasec-perp-bot.git denied to probepark.
origin/main is not updated; local main remains ahead by 16 commits.
```

Commands:

```text
.venv/bin/python perf_stages.py --config config.perf.json --target 240 --duration 30 --stages final_poll --log-dir /tmp --summary docs/perf-stage-summary-push-retest-20260630-082107.md --stage-timeout 180 --pprof-url https://l2-pprof-perf.dexor.trade/debug/pprof/profile --pprof-seconds 20 --pprof-stages final_poll --env MATCH_INVENTORY_CAP=1.0

.venv/bin/python perf_stages.py --config config.perf.json --target 300 --duration 30 --stages final_poll --log-dir /tmp --summary docs/perf-stage-summary-push-retest-t300-20260630-082235.md --stage-timeout 180 --env MATCH_INVENTORY_CAP=1.0
```

Results:

```text
target=240: 1140 fills in 31s = 36.6 TPS, errors={}, taker avg=758.0ms, taker sign avg=3.2ms, taker in-flight wait avg=792.4ms
target=300: 1139 fills in 31s = 36.7 TPS, errors={}, taker avg=972.1ms, taker sign avg=2.6ms, taker in-flight wait avg=1009.0ms
```

Profile from target=240:

```text
profile: /tmp/perf-stage-final_poll-20260630-082107.pprof.pb.gz
Sequencer.createBlock: 19.18s / 76.51% cum
ExecutionEngine.sequenceTransactionsWithBlockMutex: 19.12s / 76.27% cum
OrderBook.SnapshotDirtyTracking: 17.01s / 67.85% cum
book.copyBoolMap: 17.01s / 67.85% cum
SubmitTransaction: 0.72s / 2.87% cum
```

Interpretation:

```text
Increasing final_poll target from 240 to 300 did not increase filled TPS.
The extra target pressure mostly increased taker wait latency. Current clean
final_poll ceiling is about 36-37 TPS in this account/core state, while the
retained clean historical max remains 146.7 TPS from the immediate
post-GOMAXPROCS fix run. The current profile still points at core sequencing
plus dirty snapshot copy/hash work, not local signing or RPC admission as the
primary bottleneck.
```

## Workers=2 Rejected Follow-Up Experiments

Loadgen diagnostic change:

```text
match.py: include maker_size_backoff in DONE summary.
perf_stages.py: aggregate maker_size_backoff from worker results.
test_match_helpers.py: cover maker_size_backoff summary counter.
```

Experiments:

```text
summary: docs/perf-stage-summary-workers2-maker-backoff-20260630-084909.md
condition: workers=2 final_poll with MATCH_MAKER_SIZE_BACKOFF=0.5 and MATCH_MAKER_MIN_SIZE=0.001
target=300: 40.5 TPS, errors={}
target=360: 41.4 TPS, maker_error:insufficient_margin=60
target=420: 40.0 TPS, maker_error:insufficient_margin=41

summary: docs/perf-stage-summary-workers2-maker-backoff-counters-20260630-085341.md
condition: same as above, target=360 after exposing maker_size_backoff
result: 39.3 TPS, maker_error:insufficient_margin=42, maker_size_backoff=10 on worker 1

summary: docs/perf-stage-summary-workers2-healthy-pool-relaxed-20260630-085500.md
condition: MATCH_MAKER_POOL_COUNT=150, MATCH_HEALTHY_MAKER_MIN_FREE=100, MATCH_HEALTHY_MAKER_MAX_ABS_POS=0.10
result: 39.0 TPS, maker_error:insufficient_margin=30, health_maker_skipped=105 per worker

summary: docs/perf-stage-summary-workers2-account-fanout-20260630-085608.md
condition: MATCH_PER_ACCOUNT_TPS=6 at target=300, so maker/taker count rises to 50 each
result: 38.5 TPS, maker_error:insufficient_margin=55

summary: docs/perf-stage-summary-workers2-time-inflight-probe-20260630-085746.md
condition: MATCH_ACCOUNT_INFLIGHT=2 and MATCH_NONCE_MODE=time
target=300: 34.8 TPS, errors={}, taker avg=2091.2ms, wait avg=1586.6ms
target=360: 35.1 TPS, maker_error:insufficient_margin=23
```

Interpretation:

```text
None of the follow-up loadgen changes beats the workers=2 target=300 clean
ceiling. Maker backoff does trigger, but it does not remove margin errors and
reduces throughput. Relaxed healthy maker selection and wider account fanout
also reduce TPS. Time nonce with account_inflight=2 avoids nonce errors in the
target=300 probe, but it halves maker submit throughput and raises RPC latency
to roughly 2s, so it is not a viable max-TPS path.
```

## Stage Runner Multi-Worker Experiment

Loadgen changes:

```text
perf_stages.py: add --workers to launch MATCH_WORKER_INDEX/COUNT subprocesses and aggregate DONE lines.
accounts.py: avoid rewriting an existing keystore when no new accounts are needed; write generated accounts through os.replace() to avoid multi-worker JSON read/write races.
perf_stages.py: return non-zero when a worker exits non-zero or times out.
test_accounts.py/test_perf_stages.py: cover keystore no-rewrite, worker env, aggregate summary, and failed result detection.
```

Root cause found during the first runner test:

```text
worker 1 crashed in accounts.py json.load(open(path)) with JSONDecodeError.
Cause: load_or_create() always opened the keystore with "w" after load, even when created=0. Parallel workers could read while another worker had truncated the file.
Fix: only persist when created>0, and persist with a per-process temp file plus os.replace().
```

Commands after fix:

```text
.venv/bin/python perf_stages.py --config config.perf.json --target 300 --duration 30 --stages final_poll --workers 2 --log-dir /tmp --summary docs/perf-stage-summary-workers-fixed-20260630-083515.md --stage-timeout 180 --env MATCH_INVENTORY_CAP=1.0

.venv/bin/python perf_stages.py --config config.perf.json --target 300 --duration 30 --stages final_poll --workers 4 --log-dir /tmp --summary docs/perf-stage-summary-workers4-fixed-20260630-083617.md --stage-timeout 180 --env MATCH_INVENTORY_CAP=1.0
```

Results:

```text
workers=2 target=300: 1293 fills in 31s = 41.6 TPS, taker avg=855.2ms, taker sign avg=2.5ms, wait avg=885.6ms
workers=4 target=300: 1182 fills in 31s = 38.1 TPS, taker avg=956.6ms, taker sign avg=3.3ms, wait avg=976.7ms
```

Interpretation:

```text
Multi-worker fanout is now repeatable and no longer races on the keystore.
It improves over the current single-process target=300 result (36.7 TPS) but
does not keep scaling: workers=4 is lower than workers=2. This supports the
same bottleneck diagnosis: local process fanout is not the main limiter once
core/RPC sequencing wait dominates. Keep --workers for reproducible fanout
experiments; current best clean runner setting in this degraded account/core
state is workers=2 at 41.6 TPS.
```

## Workers=2 Target Sweep and Clean Profile

Loadgen change:

```text
perf_stages.py: parse DONE errors={...}, aggregate worker error maps, and show Workers/Errors columns in stage summaries.
```

Sweep command:

```text
.venv/bin/python perf_stages.py --config config.perf.json --target-sweep 240,300,360,420 --duration 30 --stages final_poll --workers 2 --log-dir /tmp --summary docs/perf-stage-summary-workers2-sweep-20260630-084027.md --stage-timeout 180 --env MATCH_INVENTORY_CAP=1.0
```

Sweep result:

```text
workers=2 target=240: 39.8 TPS, errors={}
workers=2 target=300: 42.0 TPS, errors={}
workers=2 target=360: 42.6 TPS, maker_error:insufficient_margin=53
workers=2 target=420: 42.3 TPS, maker_error:insufficient_margin=54
```

Clean profile command:

```text
.venv/bin/python perf_stages.py --config config.perf.json --target 300 --duration 30 --stages final_poll --workers 2 --log-dir /tmp --summary docs/perf-stage-summary-workers2-clean-pprof-20260630-084342.md --stage-timeout 180 --pprof-url https://l2-pprof-perf.dexor.trade/debug/pprof/profile --pprof-seconds 20 --pprof-stages final_poll --env MATCH_INVENTORY_CAP=1.0
```

Clean profile result:

```text
workers=2 target=300: 1269 fills in 31s = 40.9 TPS, errors={}
taker avg=877.3ms, taker sign avg=3.2ms, wait avg=905.5ms
profile: /tmp/perf-stage-final_poll-20260630-084342.pprof.pb.gz
```

Profile:

```text
ExecutionEngine.sequenceTransactionsWithBlockMutex: 19.65s / 75.75% cum
OrderBook.SnapshotDirtyTracking: 17.88s / 68.93% cum
book.copyBoolMap: 17.88s / 68.93% cum
SendRawTransaction: 0.65s / 2.51% cum
SubmitTransaction: 0.58s / 2.24% cum
PublishTransaction: 0.58s / 2.24% cum
```

Interpretation:

```text
Current degraded-state clean max is workers=2 target=300 at 42.0 TPS from the
sweep. Higher targets do not produce a clean higher max; they add maker margin
errors and higher wait. The clean profile again keeps most CPU in core
sequencing plus dirty snapshot copy/hash work, while local signing remains
~3ms and SubmitTransaction/RPC admission remains around 2-3% cum.
```

## 09:03-09:09 KST Retest After Live f905097 Verification

Pre-checks:

```text
devops live ArgoCD revision: f905097f3bdf0d18b18a86971d1fe9b004287f60
ArgoCD status: Synced / Healthy
perf core pod: perf-kaia-orderbook-dex-core-nitro-0
core replicas: 1
core image: asia-northeast3-docker.pkg.dev/orderbook-dex-dev/dev-docker-registry/kaia-orderbook-dex-core:dev@sha256:c20cfa320a46e6386e1823a610f90809a5796daf4be5f6cff7758688e5c127d8
core resources: requests cpu=8 memory=32Gi, limits cpu=15 memory=60Gi
live env: DD_ENV=perf, DD_SERVICE=kaia-orderbook-dex-core, no GOMAXPROCS, no GOGC
loadgen push: blocked, probepark has no push permission to JayChoi1736/alphasec-perp-bot.git
```

Local verification:

```text
.venv/bin/python -m unittest test_accounts.py test_match_helpers.py test_encode.py test_perf_stages.py -q
Ran 75 tests in 0.094s OK
.venv/bin/python -m py_compile match.py dex.py accounts.py perf_stages.py test_accounts.py test_match_helpers.py test_perf_stages.py
git diff --check
```

Retest results:

```text
workers=2 target=300 with pprof: 1174 fills in 31s = 37.9 TPS, errors={}, taker avg=953.1ms, taker sign avg=2.6ms, wait avg=984.4ms
workers=2 target=300 no pprof: 1096 fills in 31s = 35.4 TPS, errors={}, taker avg=1028.0ms, taker sign avg=3.3ms, wait avg=1054.4ms
workers=2 target=360 no pprof: 1121 fills in 31s = 36.1 TPS, maker_error:insufficient_margin=54, taker avg=1189.8ms, taker sign avg=3.6ms, wait avg=1217.0ms
healthy maker filter target=300: 1043 fills in 31s = 33.6 TPS, errors={}, health_maker_skipped=112 per worker
```

Current profile:

```text
profile: /tmp/perf-stage-final_poll-20260630-090350.pprof.pb.gz
Sequencer.createBlock: 19.58s / 77.06% cum
ExecutionEngine.sequenceTransactionsWithBlockMutex: 19.55s / 76.94% cum
OrderBook.SnapshotDirtyTracking: 17.90s / 70.44% cum
book.copyBoolMap: 17.90s / 70.44% cum
SendRawTransaction: 0.69s / 2.72% cum
SubmitTransaction: 0.64s / 2.52% cum
PublishTransaction: 0.63s / 2.48% cum
```

Maker account health sample:

```text
maker accounts: 150/150 queried
order_margin: min=0.0 p50=362.862 p75=534.416 p90=722.298 max=770.650
free: min=0.0 p50=1593.182 p75=1648.934 p90=1852.859 max=2104.034
abs_pos: min=0.0 p50=0.029 p75=0.060 p90=0.142 max=0.372
pass free>=1000 and abs_pos<=0.10: 71 accounts
range 0:38 pass free>=100 abs_pos<=0.10: 18/38
range 38:76 pass free>=100 abs_pos<=0.10: 30/38
range 76:114 pass free>=100 abs_pos<=0.10: 23/38
range 114:150 pass free>=100 abs_pos<=0.10: 0/36
```

Interpretation:

```text
Current clean ceiling after live f905097 verification is 35-38 TPS. target=360 is not a valid clean max because maker insufficient-margin errors return. Healthy maker filtering does not raise throughput, so the current dominant limiter is still core sequencing plus dirty order snapshot copy/hash work. Account state remains a secondary operational issue for higher target attempts, but it is not explaining the clean target=300 ceiling in this retest. Local signing remains around 2.6-3.6ms and RPC admission remains around 2.5% cum in the profile.
```

## 09:15-09:41 KST Loadgen Fanout and Maker-Once Probes

Hypothesis:

```text
The current 35-38 TPS ceiling could be caused by local loadgen shape rather than core:
1. maker refresh/cancel churn may inflate dirty order snapshots;
2. 38 taker accounts with account_inflight=1 and ~1s RPC latency create a ~38 TPS local ceiling;
3. client RPC timeout could be too low once core latency rises.
```

Experiments:

```text
workers=2 maker once, maker_size=0.03, 38 takers:
summary: docs/perf-stage-summary-workers2-maker-once-probe-20260630-091546.md
result: 37.6 TPS, errors={}, taker avg=955.4ms, wait avg=990.1ms

wide_accounts stage, 300 takers, continuous position polling:
summary: docs/perf-stage-summary-wide-accounts-current-20260630-091701.md
result: 37.2 TPS, dirty with poll TimeoutError=276, taker TimeoutError=69, nonce errors=10

300 takers, final_poll, maker refresh:
summary: docs/perf-stage-summary-wide-finalpoll-current-20260630-091919.md
result: 36.1 TPS, dirty with maker_error:insufficient_margin=3, taker avg=7135.3ms

300 takers, maker once, maker_size=0.03:
summary: docs/perf-stage-summary-wide-once-current-20260630-092141.md
result: 38.5 TPS, dirty with maker_seed_error:insufficient_margin=2, taker avg=6916.6ms

300 takers, maker once, time nonce, account_inflight=2:
summary: docs/perf-stage-summary-wide-once-time-inflight2-20260630-092443.md
result: 39.1 TPS, dirty with taker TimeoutError=433 and nonce=31

same as above with MATCH_RPC_TIMEOUT=30:
summary: docs/perf-stage-summary-wide-once-time-inflight2-timeout30-20260630-092709.md
result: 33.3 TPS, maker_seed_error:insufficient_margin=2, taker TimeoutError removed

generated 300 additional taker accounts, total takers=600, gas top-up=0.00005 KAIA each:
summary: docs/perf-stage-summary-takers600-once-probe-20260630-093018.md
result: 35.4 TPS, maker_seed_error:insufficient_margin=2, taker avg=7269.0ms

300 takers, maker once, normal nonce, account_inflight=2:
summary: docs/perf-stage-summary-wide-once-normal-inflight2-20260630-093518.md
result: 37.5 TPS, dirty with taker_error:nonce=349

600 takers, healthy maker pool, maker once, normal nonce, account_inflight=1:
summary: docs/perf-stage-summary-takers600-healthy-once-clean-20260630-093746.md
result: 37.1 TPS, errors={}, taker avg=9593.3ms, wait avg=8895.1ms
```

Interpretation:

```text
Maker refresh/cancel churn is not the current loadgen-side ceiling: maker_mode=once keeps TPS at the same 37-39 range. Taker fanout is also not the current ceiling: increasing takers from 300 to 600 and selecting healthy makers still gives 37.1 clean TPS. account_inflight=2 creates more local pressure but turns dirty through nonce errors or timeouts and does not create a valid higher clean max. Raising MATCH_RPC_TIMEOUT from 10s to 30s removes taker TimeoutError in the time-nonce probe, but TPS drops to 33.3, so client timeout is a symptom of core/RPC latency rather than the primary ceiling.

Current clean max remains 37-38 TPS in this perf state. The load generator now has enough prepared taker accounts (600) to rule out the earlier 38-account local ceiling, and the remaining limit is core/RPC response latency under sequencer/snapshot work.
```

## 09:43-09:47 KST High-Fanout Clean Profile

Command:

```text
.venv/bin/python perf_stages.py --config config.perf.json --target 300 --duration 30 --stages final_poll --workers 1 --log-dir /tmp --summary docs/perf-stage-summary-takers600-healthy-once-pprof-20260630-094305.md --stage-timeout 480 --pprof-url https://l2-pprof-perf.dexor.trade/debug/pprof/profile --pprof-seconds 20 --pprof-stages final_poll --env MATCH_INVENTORY_CAP=1.0 --env MATCH_TAKER_COUNT=600 --env MATCH_MAKER_COUNT=40 --env MATCH_PER_ACCOUNT_TPS=1 --env MATCH_ACCOUNT_INFLIGHT=1 --env MATCH_NONCE_MODE=normal --env MATCH_MAKER_MODE=once --env MATCH_MAKER_SIZE=0.02 --env MATCH_MAKER_MIN_SIZE=0.02 --env MATCH_GAS_ETH=0.00005 --env MATCH_RPC_TIMEOUT=30 --env MATCH_MAKER_POOL_COUNT=150 --env MATCH_HEALTHY_MAKER_MIN_FREE=1000 --env MATCH_HEALTHY_MAKER_MAX_ABS_POS=0.10
```

Result:

```text
summary: docs/perf-stage-summary-takers600-healthy-once-pprof-20260630-094305.md
profile: /tmp/perf-stage-final_poll-20260630-094305.pprof.pb.gz
result: 1151 fills in 31s = 37.1 TPS, errors={}
taker avg=9724.5ms, taker sign avg=3.1ms, wait avg=9027.2ms
```

Profile:

```text
Sequencer.createBlock: 12.95s / 76.54% cum
ExecutionEngine.sequenceTransactionsWithBlockMutex: 12.93s / 76.42% cum
OrderBook.SnapshotDirtyTracking: 12.13s / 71.69% cum
book.copyBoolMap: 12.13s / 71.69% cum
SendRawTransaction: 0.27s / 1.60% cum
SubmitTransaction: 0.27s / 1.60% cum
PublishTransaction: 0.26s / 1.54% cum
```

Interpretation:

```text
The high-fanout clean profile confirms the same bottleneck after ruling out local taker count and maker refresh churn. 600 prepared takers keep enough local work pending, but filled TPS remains 37.1 and core CPU is still dominated by sequencer block creation plus dirty order snapshot copy/hash. SubmitTransaction/PublishTransaction cumulative CPU falls to about 1.5-1.6%, so the remote NEG RPC admission path is not the current primary bottleneck.
```
