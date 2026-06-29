# Perf Loadgen Bottleneck Report

Date: 2026-06-30 KST
Environment: `perf`
RPC path: `https://l2-rpc-perf.dexor.trade`
Scope: local `alphasec-perp-bot` load generator, perf core runtime config, and profiling evidence

## Verdict

`GOMAXPROCS=4` was a real perf-only config cap. Removing it and matching prod runtime env behavior raised the best observed filled IOC throughput from `74.0` to `146.7` trades/s.

After that config fix, the remaining ceiling is still core-side. The latest profile during post-change load is led by sequencer block creation and transaction sequencing. Local signing, DNS, backend whitelist/proxy, and simple account fanout are not the current primary bottlenecks.

## GitOps Change

Devops change pushed:

```text
repo: ../kaia-orderbook-dex-devops
commit: f905097 perf: match core runtime env with prod
file: charts/kaia-orderbook-dex-core/values-perf.yaml
change: removed perf-only GOMAXPROCS=4 and GOGC=100 overrides
```

ArgoCD/live verification:

```text
ArgoCD revision: f905097f3bdf0d18b18a86971d1fe9b004287f60
sync: Synced
health: Healthy
live pod env:
  DD_ENV=perf
  DD_SERVICE=kaia-orderbook-dex-core
  no GOMAXPROCS
  no GOGC
```

## Post-Change Measurements

Best post-change baseline with overlapping pprof:

```text
log: /tmp/perf-post-gomax-pprof-baseline-20260630-055553.log
profile: /tmp/core-cpu-post-gomax-20260630-055553.pb.gz
target: 300 trades/s
duration: 45s
result: 6756 fills in 46s = 146.7 trades/s
taker submit: 6849
maker submit: 4222
taker avg send latency: 182.7ms
maker avg send latency: 170.3ms
taker signing avg latency: 2.6ms
taker in-flight wait avg: 223.7ms
```

Staged retest summary:

```text
summary: docs/perf-stage-summary-after-gomaxprod-2026-06-30.md
baseline: 2815 fills in 21s = 134.0 trades/s
wide_accounts: 2105 fills in 21s = 99.3 trades/s
```

The earlier immediate post-change baseline also reached:

```text
log: /tmp/perf-stage-baseline-20260630-054748.log
result: 3038 fills in 21s = 144.3 trades/s
```

## Post-Change CPU Profile

Profile captured during the `146.7` trades/s run:

```text
profile: /tmp/core-cpu-post-gomax-20260630-055553.pb.gz
duration: 20.07s
total samples: 16.23s
```

Relevant `go tool pprof -top -cum -nodefraction=0` lines:

```text
7.43s 45.78% github.com/kaiachain/kaia-orderbook-dex-core/execution/gethexec.(*Sequencer).createBlock
7.27s 44.79% github.com/kaiachain/kaia-orderbook-dex-core/execution/gethexec.(*ExecutionEngine).sequenceTransactionsWithBlockMutex
2.57s 15.83% github.com/ethereum/go-ethereum/core/perp/v1/book.(*OrderBook).SnapshotDirtyTracking
2.57s 15.83% github.com/ethereum/go-ethereum/core/perp/v1/book.copyBoolMap
2.20s 13.56% github.com/ethereum/go-ethereum/internal/ethapi.SubmitTransaction
1.91s 11.77% runtime.mapassign_faststr
1.07s  6.59% aeshashbody
```

Interpretation:

```text
After removing the scheduler cap, dirty snapshot copy is less dominant than before,
but the largest cumulative path is still core sequencer block creation and transaction sequencing.
SubmitTransaction is visible now, but it remains below the sequencer path.
Local signing stays around 2.5-2.7ms and is not the throughput ceiling.
```

## Pre-Change CPU Profile

Before the runtime env change, the profile was more heavily dominated by dirty order snapshot copying:

```text
profile: /tmp/core-cpu-filled-20260630-042410.pb.gz
duration: 30.18s
total samples: 38.73s

29.85s 77.07% gethexec.(*Sequencer).createBlock
29.79s 76.92% ExecutionEngine.sequenceTransactionsWithBlockMutex
26.78s 69.15% book.(*OrderBook).SnapshotDirtyTracking
26.77s 69.12% book.copyBoolMap
13.02s 33.62% aeshashbody
 7.28s 18.80% runtime.mapassign_faststr
 1.43s  3.69% internal/ethapi.SubmitTransaction
```

Core source locations matching the profile:

```text
../kaia-orderbook-dex-core/go-ethereum/core/perp/v1/book/orderbook.go
- dirtyOrders/deletedOrders are string-keyed maps.
- SnapshotDirtyTracking copies those maps at snapshot/block boundaries.
- copyBoolMap creates a new map and assigns every string key.

../kaia-orderbook-dex-core/go-ethereum/core/perp/v1/dispatcher/perp_dispatcher_snapshot.go
- GetOrderDeltaAndReset collects dirty/deleted order state from engines and resets tracking.

../kaia-orderbook-dex-core/go-ethereum/core/perp/v1/engine/perp_symbol_engine_dirty.go
- dirtyOrderIDs/deletedOrderIDs are string-keyed sets.
```

## Exclusions

Local signing is not the bottleneck:

```text
post-change baseline signing: 2.6ms
wide_accounts signing: 2.5ms
```

Account fanout did not raise filled TPS:

```text
wide_accounts:
  makers=40
  takers=300
  MATCH_PER_ACCOUNT_TPS=1
  MATCH_ACCOUNT_INFLIGHT=1
  result=99.3 trades/s
  taker avg send latency=1311.6ms
  taker in-flight wait avg=1258.6ms
```

Backend whitelist/proxy is not on this test path:

```text
loadgen RPC: https://l2-rpc-perf.dexor.trade
devops mapping: perf-core-http-neg, port 8547
backend RPC proxy: l2-sequencer-perf.dexor.trade, port 8545
```

DNS is not supported by the profiles as the primary limiter. The hot samples are inside core execution, and the load generator can repeatedly reach 130-146 trades/s through the NEG host.

## Local Loadgen Changes

Relevant local bot changes:

```text
match.py
- async aiohttp JSON-RPC hot path
- shared Web3 setup session
- latency counters for send/sign/in-flight wait
- separate maker/taker account fanout controls
- nonce mode support
- cancellation-safe taker in-flight task cleanup

perf_stages.py
- staged baseline/wide runner
- markdown summary output
- stage timeout guard so a hung subprocess cannot block the whole run
- optional `--pprof-url` capture that starts after the load generator prints `ready`
- raw pprof profile path included in summary output
- optional `--target-sweep` to run the same stages across multiple target TPS values without log collisions

dex.py
- reusable signing path with cached chain metadata
- shared Web3 session support
```

## Current Max TPS

Current best observed filled IOC throughput:

```text
146.7 trades/s
```

This is about `1.98x` the previous best `74.0 trades/s` measured while perf had `GOMAXPROCS=4`.

## Next Bottleneck Work

Highest impact:

```text
1. Core transaction sequencing/block creation path.
2. Core dirty order snapshot/copy path, especially string-key map copy/hash/assignment.
3. SubmitTransaction/RPC admission path after the above is reduced.
```

Load generator work remaining:

```text
1. Add a maker-liquidity guard stage that avoids avoidable insufficient-margin churn.
2. Keep staged runs on direct NEG RPC unless explicitly testing backend proxy behavior.
3. Use `--target-sweep` for longer max-search runs after each core/config change.
```

## Verification

Commands run:

```text
helm template perf-kaia-orderbook-dex-core charts/kaia-orderbook-dex-core -f charts/kaia-orderbook-dex-core/values-perf.yaml
kubectl -n argocd get application perf-kaia-orderbook-dex-core
kubectl -n kaia-dex-perf exec perf-kaia-orderbook-dex-core-nitro-0 -- printenv
.venv/bin/python -m unittest test_match_helpers.py test_encode.py test_perf_stages.py -q
.venv/bin/python -m py_compile match.py dex.py accounts.py perf_stages.py test_match_helpers.py test_perf_stages.py
go tool pprof -top -cum -nodefraction=0 /tmp/core-cpu-post-gomax-20260630-055553.pb.gz
.venv/bin/python perf_stages.py --config config.perf.json --target 120 --duration 8 --stages baseline --pprof-url https://l2-pprof-perf.dexor.trade/debug/pprof/profile --pprof-stages baseline --pprof-seconds 3 --summary /tmp/perf-stage-summary-pprof-smoke-20260630.md --stage-timeout 120
go tool pprof -top -cum -nodefraction=0 /tmp/perf-stage-baseline-20260630-060150.pprof.pb.gz
.venv/bin/python perf_stages.py --config config.perf.json --target-sweep 80,120 --duration 5 --stages baseline --summary /tmp/perf-stage-summary-target-sweep-smoke-20260630.md --stage-timeout 120
```

No core code changes were made.

## Retest After Local Docs Commit

Push status:

```text
git push origin main
ERROR: Permission to JayChoi1736/alphasec-perp-bot.git denied to probepark.
viewerPermission: READ
```

Retest command:

```text
.venv/bin/python perf_stages.py --config config.perf.json --target 360 --duration 45 --stages baseline,maker_guard --summary docs/perf-stage-summary-retest-2026-06-30.md --stage-timeout 240 --pprof-url https://l2-pprof-perf.dexor.trade/debug/pprof/profile --pprof-seconds 20 --pprof-stages baseline,maker_guard
```

Latest clean retest:

```text
summary: docs/perf-stage-summary-retest-2026-06-30.md
baseline: 6144 fills in 46s = 133.5 trades/s
maker_guard: 5193 fills in 46s = 112.8 trades/s
```

Latest pprof evidence:

```text
baseline profile: /tmp/perf-stage-baseline-20260630-061402.pprof.pb.gz
Duration: 20.17s, Total samples = 19.20s
Sequencer.createBlock: 10.09s / 52.55% cum
ExecutionEngine.sequenceTransactionsWithBlockMutex: 10.01s / 52.14% cum
OrderBook.SnapshotDirtyTracking: 5.68s / 29.58% cum
SubmitTransaction: 1.91s / 9.95% cum

maker_guard profile: /tmp/perf-stage-maker_guard-20260630-061402.pprof.pb.gz
Duration: 20.13s, Total samples = 18.95s
Sequencer.createBlock: 11.18s / 59.00% cum
ExecutionEngine.sequenceTransactionsWithBlockMutex: 11.06s / 58.36% cum
OrderBook.SnapshotDirtyTracking: 8.57s / 45.22% cum
SubmitTransaction: 1.47s / 7.76% cum
```

Current clean max remains `146.7 trades/s`. Experimental `time_inflight2` reached `150.2 trades/s`, but it produced two taker nonce errors, so it is not counted as the clean ceiling.

Additional local loadgen experiments are recorded in `docs/perf-loadgen-experiment-log-2026-06-30.md`. The tested variants did not beat the current clean max; the core-side bottleneck diagnosis remains unchanged.

Latest loadgen-side code change:

```text
match.py: poll taker positions only; maker positions are no longer polled because they are not used for fill counting or taker inventory control.
test: test_position_poll_items_skip_maker_accounts
```

Latest profile after that change:

```text
summary: docs/perf-stage-summary-taker-only-poll-2026-06-30.md
result: 3470 fills in 46s = 75.3 trades/s
profile: /tmp/perf-stage-baseline-20260630-064418.pprof.pb.gz
Sequencer.createBlock: 27.82s / 58.88% cum
ExecutionEngine.sequenceTransactionsWithBlockMutex: 27.61s / 58.43% cum
OrderBook.SnapshotDirtyTracking: 19.64s / 41.57% cum
SubmitTransaction: 3.79s / 8.02% cum
```

This run did not change the clean max because maker insufficient-margin errors dominated the test. The code change only removes unnecessary local RPC load.

## Current Retest State

Later retests are no longer reproducing the `146.7 trades/s` clean max. The strongest immediate cause is test account state, not local signing or DNS:

```text
MATCH_MAKER_CANCEL_EVERY=1: 1966 fills in 31s = 63.3 trades/s, margin errors removed but cancel traffic dominated
MATCH_LEVELS=1: 2359 fills in 31s = 76.1 trades/s
MATCH_INVENTORY_CAP=0.01: 3290 fills in 46s = 71.4 trades/s, maker insufficient-margin errors persisted
owner L2 ETH: ~0.03 after low-gas fresh-account smoke tests, not enough for another broad fresh-account sweep with MATCH_GAS_ETH=0.1
fresh low-gas smoke: MATCH_GAS_ETH=0.0005 works for account creation/funding, but measured only 11.4-29.2 trades/s in tested combinations
fresh runner smoke: perf_stages.py --fresh-keystore-dir works end-to-end, measured 27.1 trades/s with low-gas env overrides
```

Interpretation: the earlier clean ceiling remains the best measured max, but the current account set is not clean enough for a fair max search. A next clean run should fund the owner for fresh sub-accounts and then use the new `--fresh-keystore-dir` / `--env` runner options with a better maker sizing/depth strategy before comparing TPS again.

## Retest After Fresh Runner Commit

```text
summary: docs/perf-stage-summary-retest-20260630-070902.md
local commit: 46edd1c perf: add fresh keystore stage runner options
push status: git push origin main blocked, viewerPermission=READ on JayChoi1736/alphasec-perp-bot
rpc path: https://l2-rpc-perf.dexor.trade
owner L2 ETH before retest: 0.03
best retest result: maker_guard target=120, 1768 fills in 31s = 57.0 trades/s
baseline target=80: 39.0 TPS, maker insufficient-margin=50
baseline target=120: 48.1 TPS, maker insufficient-margin=51
baseline target=160: 51.5 TPS, maker insufficient-margin=83
maker_guard target=80: 40.2 TPS, maker_submit_ok=60
maker_guard target=120: 57.0 TPS, maker_submit_ok=90
maker_guard target=160: 52.6 TPS, maker_submit_ok=480
```

Latest profile evidence from the retest:

```text
t80 baseline: Sequencer.createBlock 67.61% cum, SequenceTransactions 67.16% cum, SnapshotDirtyTracking/copyBoolMap 56.32% cum, SubmitTransaction 3.46% cum
t120 baseline: Sequencer.createBlock 67.68% cum, SequenceTransactions 67.27% cum, SnapshotDirtyTracking/copyBoolMap 54.05% cum, SubmitTransaction 3.90% cum
t160 baseline: Sequencer.createBlock 66.39% cum, SequenceTransactions 66.01% cum, SnapshotDirtyTracking/copyBoolMap 54.76% cum, SubmitTransaction 4.30% cum
```

Current diagnosis:

```text
The latest retest does not replace the clean max of 146.7 TPS. Current account state is the immediate test blocker: baseline still emits maker insufficient-margin, while maker_guard avoids the error by reducing maker submissions and therefore cannot prove a higher clean ceiling. Core profile remains consistent with the earlier diagnosis: sequencer transaction sequencing and dirty order snapshot copy/hash work dominate CPU. No core code changes were made.
```

## Wide Recheck And Maker Adaptive Experiment

```text
wide recheck summary: docs/perf-stage-summary-wide-recheck-20260630-071917.md
wide_accounts target=240: 2021 fills in 31s = 65.1 TPS
wide taker avg latency: 3459.2ms
wide taker in-flight wait avg: 3356.1ms
wide maker insufficient-margin: 12
```

Result: wider fanout currently worsens RPC/core admission latency and does not recover the earlier max.

Optional loadgen change:

```text
match.py: per-maker order-size backoff after maker insufficient-margin
perf_stages.py: maker_adaptive stage
test coverage: maker_size_after_error helper and maker/taker loop call arity checks
```

Adaptive result:

```text
summary: docs/perf-stage-summary-maker-adaptive-20260630-072656.md
baseline target=240: 1662 fills in 31s = 53.6 TPS, maker insufficient-margin=154
maker_adaptive target=240: 1671 fills in 31s = 53.9 TPS, maker insufficient-margin=84
```

Current diagnosis remains:

```text
maker_adaptive reduces failed maker traffic, but it does not move max TPS. Current ceiling evidence still points to core transaction sequencing and order-book snapshot/copy work under the present perf/account state. The latest measured clean max remains 146.7 TPS.
```

## Final-Only Position Poll Experiment

Optional loadgen change:

```text
match.py: MATCH_POSITION_POLL_MODE=continuous|final|off
perf_stages.py: final_poll stage
```

Reason:

```text
Continuous position polling is accurate, but it adds read RPC pressure during the load window. final_poll keeps initial position reads, skips continuous position reads during load, then performs one final position poll after load cleanup. TPS denominator is frozen before the final read, so measurement read time is not mixed into load time.
```

Results:

```text
summary: docs/perf-stage-summary-final-poll-20260630-073400.md
baseline target=240: 1910 fills in 31s = 61.6 TPS, taker avg latency=418.8ms, wait=456.1ms
final_poll target=240: 2190 fills in 31s = 70.6 TPS, taker avg latency=365.7ms, wait=399.1ms

summary: docs/perf-stage-summary-final-poll-sweep-20260630-073539.md
final_poll target=300: 2150 fills in 31s = 69.3 TPS, taker avg latency=487.1ms, wait=520.7ms
final_poll target=360: 2069 fills in 31s = 66.6 TPS, taker avg latency=613.4ms, wait=649.2ms
```

Profile evidence:

```text
target=300: Sequencer.createBlock 70.42% cum, SequenceTransactions 69.98% cum, SnapshotDirtyTracking/copyBoolMap 60.27% cum, SubmitTransaction 4.84% cum
target=360: Sequencer.createBlock 70.11% cum, SequenceTransactions 69.50% cum, SnapshotDirtyTracking/copyBoolMap 58.54% cum, SubmitTransaction 4.65% cum
```

Current diagnosis:

```text
final_poll improves the current degraded run from 61.6 to 70.6 TPS at target=240 by reducing read pressure, but higher targets still fall as core sequencing latency and maker margin failures rise. It is a useful max-load stage, not a new clean max. Clean max remains 146.7 TPS from the earlier post-GOMAXPROCS run.
```

## Follow-up Current-State Retests

Retests after `final_poll` tried to isolate whether the lower current TPS was
caused by polluted low-index accounts, maker traffic, broad account fanout,
maker order size, or per-account in-flight settings.

```text
slice60 final_poll target=240: 57.6 TPS
taker-only final_poll target=240: 55.5 TPS
fanout60 final_poll target=240: 46.1 TPS
maker-size-0.0025 final_poll target=240: 50.5 TPS
inflight2 final_poll target=240: 51.5 TPS
local sweep final_poll target=180: 46.5 TPS
local sweep final_poll target=220: 52.8 TPS
local sweep final_poll target=260: 54.1 TPS
```

Current conclusion:

```text
These variants did not beat the 70.6 TPS current-state final_poll result and
do not replace the 146.7 TPS clean max. The local generator is no longer the
dominant proven bottleneck: signing remains low-millisecond, taker-only traffic
lowers fills, wider fanout increases latency, and in-flight=2 is not beneficial
on the clean path. The bottleneck evidence remains core sequencing plus dirty
snapshot copy/hash work, while maker insufficient-margin churn is the immediate
reason current retests underperform the clean post-GOMAXPROCS run.
```

## Latest Retest After Push Attempt

Push remained blocked by repository permissions:

```text
git push origin main
ERROR: Permission to JayChoi1736/alphasec-perp-bot.git denied to probepark.
```

The requested retest was still executed locally:

```text
summary: docs/perf-stage-summary-final-poll-retest-20260630-075002.md
profile: /tmp/perf-stage-final_poll-20260630-075002.pprof.pb.gz
stage: final_poll
target: 240 TPS
duration: 30s
result: 1470 fills in 31s = 47.4 TPS
taker_submit_ok: 1440
maker_submit_ok: 1377
maker insufficient-margin: 128
taker avg latency: 577.9ms
taker signing avg latency: 2.5ms
taker in-flight wait avg: 608.5ms
```

Profile evidence:

```text
Sequencer.createBlock: 19.02s / 75.75% cum
ExecutionEngine.sequenceTransactionsWithBlockMutex: 18.97s / 75.55% cum
OrderBook.SnapshotDirtyTracking: 16.25s / 64.72% cum
book.copyBoolMap: 16.24s / 64.68% cum
SubmitTransaction: 0.70s / 2.79% cum
```

Conclusion:

```text
Latest retest confirms the same diagnosis. Local signing is not the limiter,
SubmitTransaction/RPC admission is visible but small in CPU, and the dominant
cost is core block sequencing plus dirty order snapshot copy/hash work. Current
accounts are degraded by maker insufficient-margin churn, so 47.4 TPS does not
replace the clean 146.7 TPS max.
```

## Maker Cooldown Rejection

An optional maker error cooldown was added to test whether repeated
insufficient-margin makers were wasting enough submit budget to become the
current bottleneck.

```text
code: MATCH_MAKER_ERROR_COOLDOWN_THRESHOLD, MATCH_MAKER_ERROR_COOLDOWN_SEC
stage: maker_cooldown
default behavior: unchanged unless the env vars are set
```

Measured results:

```text
summary: docs/perf-stage-summary-maker-cooldown-20260630-075610.md
final_poll target=240: 54.0 TPS
maker_cooldown threshold=2 target=240: 50.2 TPS

summary: docs/perf-stage-summary-maker-cooldown-aggressive-20260630-075825.md
maker_cooldown threshold=1 target=240: 50.8 TPS

summary: docs/perf-stage-summary-maker-cooldown-counters-20260630-080137.md
maker_cooldown threshold=1 target=240: 50.1 TPS
maker_cooldown=7
maker_cooldown_skipped=4
maker insufficient-margin=7
```

Profile from the latest cooldown run:

```text
Sequencer.createBlock: 19.01s / 75.47% cum
ExecutionEngine.sequenceTransactionsWithBlockMutex: 18.95s / 75.23% cum
OrderBook.SnapshotDirtyTracking: 16.11s / 63.95% cum
book.copyBoolMap: 16.11s / 63.95% cum
SubmitTransaction: 0.78s / 3.10% cum
```

Conclusion:

```text
Maker cooldown reduces insufficient-margin errors but lowers filled TPS versus
final_poll. It is rejected as the max-TPS path. This narrows the diagnosis:
failed maker submits are not the primary throughput ceiling; core sequencing and
dirty snapshot copy/hash work remain the dominant bottleneck.
```

## Prep-Failed Account Filter Rejection

The repeated setup warning was traced to DEX command `0x45`, which is
`CMD_PERP_SET_LEVERAGE`. A loadgen-only filter was added so a stage can exclude
accounts that fail prep:

```text
code: MATCH_SKIP_PREP_FAILED=1
stage: healthy_accounts
default behavior: unchanged unless the env var is set
```

Measured result:

```text
summary: docs/perf-stage-summary-healthy-accounts-20260630-080727.md
final_poll target=240: 47.1 TPS
healthy_accounts target=240: 46.6 TPS
prep_skipped=4
filtered accounts: makers=4, takers=0
```

Profile from `healthy_accounts`:

```text
Sequencer.createBlock: 18.55s / 75.84% cum
ExecutionEngine.sequenceTransactionsWithBlockMutex: 18.49s / 75.59% cum
OrderBook.SnapshotDirtyTracking: 15.88s / 64.92% cum
book.copyBoolMap: 15.88s / 64.92% cum
SubmitTransaction: 0.83s / 3.39% cum
```

Conclusion:

```text
Filtering prep-failed makers is useful diagnostics and keeps dirty accounts out
of a run, but it did not improve max TPS. The dominant bottleneck remains core
sequencing plus dirty snapshot copy/hash work.
```

## Healthy Maker Pool Rejection

The first 30 makers are heavily polluted with residual positions, so the
loadgen now has an optional maker pool selector:

```text
code: MATCH_MAKER_POOL_COUNT, MATCH_HEALTHY_MAKER_MIN_FREE, MATCH_HEALTHY_MAKER_MAX_ABS_POS
stage: healthy_makers
default behavior: unchanged unless the env vars are set
```

Measured result:

```text
summary: docs/perf-stage-summary-healthy-makers-20260630-081630.md
final_poll target=240: 43.4 TPS
healthy_makers target=240: 33.8 TPS
selected makers: 30/150
health_maker_skipped=120
healthy_makers errors={}
```

Profile from `healthy_makers`:

```text
Sequencer.createBlock: 19.30s / 77.11% cum
ExecutionEngine.sequenceTransactionsWithBlockMutex: 19.24s / 76.87% cum
OrderBook.SnapshotDirtyTracking: 17.46s / 69.76% cum
book.copyBoolMap: 17.46s / 69.76% cum
SubmitTransaction: 0.61s / 2.44% cum
```

Conclusion:

```text
Healthy maker selection is useful for proving that margin errors are not the
primary throughput ceiling. It removes errors but lowers filled TPS and keeps
the profile dominated by core sequencing plus dirty snapshot copy/hash work.
```

## Push-Blocked Retest Update

Push did not update GitHub because the current account has read-only access:

```text
ERROR: Permission to JayChoi1736/alphasec-perp-bot.git denied to probepark.
```

Retest results from local `main`:

```text
summary: docs/perf-stage-summary-push-retest-20260630-082107.md
target=240 final_poll: 36.6 TPS, errors={}, taker avg=758.0ms, taker sign avg=3.2ms, wait avg=792.4ms

summary: docs/perf-stage-summary-push-retest-t300-20260630-082235.md
target=300 final_poll: 36.7 TPS, errors={}, taker avg=972.1ms, taker sign avg=2.6ms, wait avg=1009.0ms
```

Profile from target=240:

```text
Sequencer.createBlock: 19.18s / 76.51% cum
ExecutionEngine.sequenceTransactionsWithBlockMutex: 19.12s / 76.27% cum
OrderBook.SnapshotDirtyTracking: 17.01s / 67.85% cum
book.copyBoolMap: 17.01s / 67.85% cum
SubmitTransaction: 0.72s / 2.87% cum
```

Updated diagnosis:

```text
Current clean final_poll throughput is 36-37 TPS and no longer rises when
target increases from 240 to 300. The historical clean max remains 146.7 TPS.
This retest does not change the bottleneck call: local signing is still only
~3ms, RPC admission/SubmitTransaction is small in CPU profile, and the dominant
CPU path is core sequencer block creation plus dirty order snapshot copy/hash
work.
```
